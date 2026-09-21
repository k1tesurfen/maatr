# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`maatr` is a single-module Python CLI that renames and reorganizes a media library, and reduces the audio tracks inside MKV files. It operates directly on the user's real media — large files (tens of GB) on external drives — so **the cost of a bug here is destroyed or misplaced media, not a failed build**. The invariants below exist because each one was a real data-loss bug.

## Commands

The installed CLI is `maatr`, aliased `mtr`.

```bash
pipx install --force .          # reinstall after changes (pipx is how this is used)
python test_naming.py           # naming/metadata rules  (100 cases)
python test_audio_rules.py      # audio selection rules  (29 cases)
```

Both test files are plain scripts with no test runner: they print one line per case and exit non-zero on failure. They import `maatr`, so run them with an interpreter that has `click` and `guessit` — the pipx venv works: `~/.local/pipx/venvs/maatr/bin/python test_naming.py`.

Running a single case means commenting out the others; the suites are deliberately flat tables.

### External binaries

`mkvmerge`, `mkvpropedit` (MKVToolNix) and `ffprobe` (FFmpeg) must be on PATH. Code calls them via `subprocess` and must degrade gracefully when they fail — every call site already handles `CalledProcessError`/`OSError`.

## Architecture

Everything lives in `maatr.py`. Keep it that way unless it grows a lot; if it splits, the natural seams are config, naming, audio, and CLI.

Three commands: `organize` (rename/move, with an interactive review of the names), `audio` (track cleanup), `undo` (reverse moves).

### The two-phase pattern

Both `organize` and `audio` **plan everything before touching anything**:

1. Snapshot the file list (`collect_media`) — never walk and move at the same time, or `os.walk` re-consumes files the tool just moved into subdirectories.
2. Build a complete plan, detecting collisions across the whole run.
3. Execute.

This is why identification failures cost nothing: the folder is still untouched when the problem is found. Preserve this shape when adding features.

`organize --audio` extends it across both halves: `audio_review` plans and confirms the track cleanup but executes nothing, the rename plan is built on top of the *predicted* `{audio}` tags (`audio_tag_from_plan`), the review list is approved, and only then does `apply_audio` run, followed by the moves. Declining the names must therefore leave the tracks intact too. `audio_pass` is the thin wrapper that keeps the standalone `audio` command's behaviour.

### The review list

`organize` shows every planned name, numbered, and lets each one be corrected before anything is written (`build_review`, `render_review`, `review_names`, `edit_item`). One entry per movie, per season and per unidentified file; numbers are handed out once and never move, so an edit cannot renumber the line the user is about to pick.

An edited line is read back into fields by `parse_name_edit` (a movie filename) or `parse_series_edit` (a series title), both **pure functions**, and the whole target is re-rendered through `process_media` → `format_path` → `sanitize_component`. Never take a typed name as a path: the folder has to follow the corrected title, and a hand-typed title must be sanitized exactly like a parsed one. A typed title counts as verbatim, so `.title()` never re-cases it.

An edit that cannot stand — no title, still-missing fields, a collision with another target or a file on disk — is rejected and the entry is restored from a snapshot; the approved list is always conflict-free. `--yes` prints the list without prompting, `--dry-run` prints it and moves nothing.

Unidentified files are *not* skipped during the resolve pass any more: they become review entries, and only what the user leaves unnamed is reported as a skip. There is no `--ask` flag; naming happens here.

### Naming: four sources, in order of authority

`enrich_guess` combines them, because filenames are often abbreviated past usefulness:

1. **TMDB** (`lookup_title`) — movies only, overrules everything for title and year.
2. **MKV container title** (`embedded_title`) — wins for title and year when plausible.
3. **The filename**, via guessit on the *relative path* (so parent folders supply series names) — resolution, season, episode.
4. **The folder name** (`year_from_folders`) — year only, 1900–2100, and only when nothing else supplied one.

`enrich_guess` returns notes as `(text, colour)` pairs; the caller prints `None` dim and a colour as-is.

### The title lookup

German releases carry dubbed titles, so `organize` asks TMDB for the international English one. Two routes: an IMDb id scraped from a sibling `.nfo` (`imdb_id_from_sidecar`, exact) or a title+year search. `pick_tmdb_movie` is a **pure function** over the search hits — that is what makes the match rule testable without a key or a network, the same reason `plan_audio` is pure. Keep new matching logic there.

`.nfo` sidecars are **read only**. They are never moved, renamed or deleted, and the source release folder is never renamed.

### Naming hardening

`sanitize_component` transliterates to plain ASCII, because macOS hands out NFD umlauts and the NAS expects NFC. Order is load-bearing: NFC → the German table → NFKD accent strip. Reversing the first two turns `Ä` into `A` instead of `Ae`. Defaults live in `DEFAULT_REPLACE`/`DEFAULT_DROP`, and `apply_naming_config` folds an optional `[naming]` section in — it *extends* the table rather than replacing it, since `load_config` does not merge with defaults and a user adding one mapping must not silently lose `ß`.

### Audio: gated rule

`plan_audio` is a **pure function** over parsed track data — no I/O beyond size lookups. This is what makes the rule table testable without any MKVs. Keep new audio logic in there rather than in the execution path.

It returns one of four actions: `remux` (drop tracks — expensive, rewrites the file), `flags` (mkvpropedit, instant, in-place), `none`, or `skip`.

The rule only fires when **both** configured languages are present; otherwise it degrades to a flag-only default fix. Track size comes from the `tag_number_of_bytes` statistics tag via `mkvmerge -J` when present, falling back to summing packet sizes with ffprobe — exact but slow, and slow matters at 50GB over USB.

## Invariants — do not regress these

- **Never invent identifying data.** No default title, season or episode. A file whose title is unknown is reported and left alone. A `"Unknown"` fallback once collapsed an entire series into one file. The same applies to the lookup: no key, no network, or no confident match means the parsed title stands and the file is reported with the reason — never a best guess at which film it is.
- **Never overwrite.** `move_without_overwrite` reserves the destination with `O_CREAT|O_EXCL` before moving. `shutil.move` silently clobbers on POSIX.
- **Sanitize every template value, never the template.** `sanitize_component` strips separators and control characters so a parsed title cannot escape the target directory; the template's own `/` are intentional. Targets are also checked with `commonpath`.
- **One series title per folder.** `unify_series_titles` is a **pure function**: releases that carry the *episode* name in each filename parse as a series per file and scatter one season across a dozen folders. Inside a folder the episodes must agree — the majority title wins, the folder's own parsed name breaks a tie, and a group with neither is left exactly as it was and reported. Never invent the series.
- **One movie file per movie folder, but only when unambiguous.** `is_extra_media` drops named extras (token at the *end* of the stem, or a `Sample/`-style folder — end-anchoring is what keeps a film called "Sample People" safe). `pick_primary_movie` then keeps the largest file in a group, and is a **pure function** with `size_of` injected so the rule is testable without files. It declines — deliberately — for a group holding any `episode` (a season must stay whole), for a single entry, and when the winner is under `primary_size_ratio` (4×) the runner-up, so two comparable files fail the folder loudly instead of being silently halved. Extras are reported with `breaks_group=False` and left on disk, never moved or deleted.
- **Target names are claimed after the whole group is known.** `organize` plans in three passes (per-file resolve → per-group primary pick → claim). Claiming inside the per-file loop made the outcome depend on `collect_media`'s lexicographic order: with a `Sample/` subfolder the sample sorts before the feature (`S` < `m`) and the *feature* was the file rejected.
- **`organize` is all-or-nothing per folder.** Each immediate subdirectory of the working directory succeeds or fails as a unit, so a season is never left half-renamed. `--partial` opts out. `audio` is deliberately per-file instead — a file that fails keeps its original audio, which is untouched rather than half-broken.
- **Remux is verify-then-replace.** Write to `TEMP_SUFFIX`, verify with `verify_remux`, and only then `os.replace`. On any failure the temp file is discarded and the original is left byte-identical.
- **Verify duration against the surviving tracks, never the old container.** A Matroska container is as long as its longest track, so dropping the longest one shortens the file legitimately — a German release may carry a Russian dub running 18s past the picture. `expected_duration_ns` computes the length from the tracks that survive the plan, and the video track's own duration is compared before and after as the truncation check. Comparing old container to new rejected correct remuxes.
- **Persist the undo log after every move**, so an interrupted run stays revertible. Rollback must trim the entries it reverses.
- **A file whose audio step failed is excluded from renaming**, so everything that moves matches the plan the user approved.
- **A dry run must preview the name the live run would produce.** `organize --audio --dry-run` has not dropped any tracks yet, so the `{audio}` tag comes from `audio_tag_from_plan` (the plan's surviving tracks) rather than from probing the file. `audio_review` therefore returns `confirmed=True` on a dry run — it distinguishes "nothing was executed" from "the user declined", and conflating the two once made `--audio --dry-run` stop before the rename plan.
- `mtr undo` reverses moves only. It cannot reverse a remux — the confirmation prompt is the only safety net there.

## Conventions

- User-facing output goes through `click.secho`; green for actions taken, yellow for skipped, red for failures. Every skipped file is reported **with its reason** — silent skips are how the original data loss went unnoticed.
- Ties in track selection break deterministically (size, channels, sampling rate, then track id) so repeat runs agree.
- Filenames avoid `:` and `*?"<>|`, which are illegal on exFAT/NTFS/SMB where media drives usually live.
- Keep examples in docs, comments and tests generic (`Example Movie`, `Example Series`). The repo should not describe the provenance of any real file.
- `*.mkv`/`*.mp4`/`*.avi` are gitignored; never commit media.
