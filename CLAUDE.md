# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`maatr` is a single-module Python CLI that renames and reorganizes a media library, and reduces the audio tracks inside MKV files. It operates directly on the user's real media — large files (tens of GB) on external drives — so **the cost of a bug here is destroyed or misplaced media, not a failed build**. The invariants below exist because each one was a real data-loss bug.

## Commands

The installed CLI is `maatr`, aliased `mtr`.

```bash
pipx install --force .          # reinstall after changes (pipx is how this is used)
python test_naming.py           # naming/metadata rules  (27 cases)
python test_audio_rules.py      # audio selection rules  (18 cases)
```

Both test files are plain scripts with no test runner: they print one line per case and exit non-zero on failure. They import `maatr`, so run them with an interpreter that has `click` and `guessit` — the pipx venv works: `~/.local/pipx/venvs/maatr/bin/python test_naming.py`.

Running a single case means commenting out the others; the suites are deliberately flat tables.

### External binaries

`mkvmerge`, `mkvpropedit` (MKVToolNix) and `ffprobe` (FFmpeg) must be on PATH. Code calls them via `subprocess` and must degrade gracefully when they fail — every call site already handles `CalledProcessError`/`OSError`.

## Architecture

Everything lives in `maatr.py`. Keep it that way unless it grows a lot; if it splits, the natural seams are config, naming, audio, and CLI.

Three commands: `organize` (rename/move), `audio` (track cleanup), `undo` (reverse moves).

### The two-phase pattern

Both `organize` and `audio` **plan everything before touching anything**:

1. Snapshot the file list (`collect_media`) — never walk and move at the same time, or `os.walk` re-consumes files the tool just moved into subdirectories.
2. Build a complete plan, detecting collisions across the whole run.
3. Execute.

This is why identification failures cost nothing: the folder is still untouched when the problem is found. Preserve this shape when adding features.

### Naming: three sources, in order of authority

`enrich_guess` combines them, because filenames are often abbreviated past usefulness:

1. **MKV container title** (`embedded_title`) — wins for title and year when plausible.
2. **The filename**, via guessit on the *relative path* (so parent folders supply series names) — resolution, season, episode.
3. **The folder name** (`year_from_folders`) — year only, 1900–2100, and only when nothing else supplied one.

### Audio: gated rule

`plan_audio` is a **pure function** over parsed track data — no I/O beyond size lookups. This is what makes the 18-case rule table testable without any MKVs. Keep new audio logic in there rather than in the execution path.

It returns one of four actions: `remux` (drop tracks — expensive, rewrites the file), `flags` (mkvpropedit, instant, in-place), `none`, or `skip`.

The rule only fires when **both** configured languages are present; otherwise it degrades to a flag-only default fix. Track size comes from the `tag_number_of_bytes` statistics tag via `mkvmerge -J` when present, falling back to summing packet sizes with ffprobe — exact but slow, and slow matters at 50GB over USB.

## Invariants — do not regress these

- **Never invent identifying data.** No default title, season or episode. A file whose title is unknown is reported and left alone. A `"Unknown"` fallback once collapsed an entire series into one file.
- **Never overwrite.** `move_without_overwrite` reserves the destination with `O_CREAT|O_EXCL` before moving. `shutil.move` silently clobbers on POSIX.
- **Sanitize every template value, never the template.** `sanitize_component` strips separators and control characters so a parsed title cannot escape the target directory; the template's own `/` are intentional. Targets are also checked with `commonpath`.
- **`organize` is all-or-nothing per folder.** Each immediate subdirectory of the working directory succeeds or fails as a unit, so a season is never left half-renamed. `--partial` opts out. `audio` is deliberately per-file instead — a file that fails keeps its original audio, which is untouched rather than half-broken.
- **Remux is verify-then-replace.** Write to `TEMP_SUFFIX`, verify with `verify_remux`, and only then `os.replace`. On any failure the temp file is discarded and the original is left byte-identical.
- **Persist the undo log after every move**, so an interrupted run stays revertible. Rollback must trim the entries it reverses.
- **A file whose audio step failed is excluded from renaming**, so everything that moves matches the plan the user approved.
- `mtr undo` reverses moves only. It cannot reverse a remux — the confirmation prompt is the only safety net there.

## Conventions

- User-facing output goes through `click.secho`; green for actions taken, yellow for skipped, red for failures. Every skipped file is reported **with its reason** — silent skips are how the original data loss went unnoticed.
- Ties in track selection break deterministically (size, channels, sampling rate, then track id) so repeat runs agree.
- Filenames avoid `:` and `*?"<>|`, which are illegal on exFAT/NTFS/SMB where media drives usually live.
- Keep examples in docs, comments and tests generic (`Example Movie`, `Example Series`). The repo should not describe the provenance of any real file.
- `*.mkv`/`*.mp4`/`*.avi` are gitignored; never commit media.
