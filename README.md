<h1 align="center">
  <img src="./maatr-logo.svg" alt="plug" width="180">
  <br>
  maatr - filename cleaning for media files
</h1>

Maatr is a lightweight CLI tool designed to bring order to media file chaos. It automatically renames and organizes movies and TV shows into a structured library by extracting metadata from filenames and probing for audio languages.

### The Name

Maatr is named after **Maat**, the ancient Egyptian goddess who personified truth, balance, order, and harmony. She regulated the stars, seasons, and the actions of mortals and the deities who had brought order from chaos at the moment of creation. Maatr aims to bring that same divine order to the chaos of your media library.

---

## Installation

On macOS, Python environments can be tricky due to Homebrew's management. The recommended way to install `maatr` globally is using [pipx](https://github.com/pypa/pipx), which installs the tool in an isolated environment while making the command available everywhere.

1. **Install pipx** (if you haven't already):

   ```bash
   brew install pipx
   pipx ensurepath
   ```

2. **Install Maatr**:
   From the project root, run:
   ```bash
   pipx install .
   ```

> **Note:** Maatr requires `ffprobe` (part of FFmpeg) to detect audio languages. Install it via:
> `brew install ffmpeg`

---

## Usage

Maatr provides two aliases: `maatr` and `mtr`.

### Organize

Run this in the directory containing your media files:

```bash
maatr organize
```

- Use `--dry-run` to see what would happen without moving files.
- Use `--partial` to organize every file that can be identified instead of skipping its whole folder.
- Use `--yes` to apply the plan as it stands, without the review prompt.
- Use `--no-lookup` to keep the title as parsed instead of asking TMDB for the international one.
- Use `--no-cache` to ignore stored TMDB lookups and ask again.

#### The review list

Nothing is moved until you have seen every name. Maatr numbers the plan — one entry per movie, one per season, one per file it could not identify — and waits:

```
Planned names:
[1] Beispiel.Film.2024.German.DL.1080p-XYZ/beispiel.film.2024.1080p.mkv
      -> Example Movie (2024)/Example Movie (2024) [1080p] [ENG-GER].mkv
[2] Example Series  (12 episode(s))
      Example Series/Example Series Season 1/Example Series S01E01 [1080p] [ENG-GER].mkv
      ... 8 more
      series title unified to 'Example Series' (1 file(s) parsed as something else)
[3] ???  Some.Release.x264.mkv
      unknown title (pick 3 to name it)
----------------------------------------
3 item(s): 1 movie(s), 1 season(s), 1 unidentified.
Apply these names? [y/N/number]:
```

- **y** applies the plan.
- **n** (or Enter) aborts the whole run — with `--audio`, not a single track has been dropped at that point either.
- **a number** puts that name in front of the cursor to be edited. Type the name you want, hit Enter, and Maatr reads the title, year, resolution and language tag back out of it and re-renders the folder to match. A season asks for the series title instead, and re-renders every episode in it. Then it asks again, so you can correct one entry after another.

An unidentified file is named the same way: give it a name and it joins the plan. A name that would collide with another file is refused on the spot, naming the file that already has it, so the list you finally approve cannot overwrite anything. A name missing its year or resolution is accepted, with a note; a name with no title at all is refused, because a guessed title is what merges a whole series into one file.

#### One series title per folder

Some releases put the *episode* title in each filename, which used to scatter one season across a dozen folders. Within a folder, the episodes now have to agree: the title most of them carry wins, a tie is broken by the folder's own name, and the override is reported in the review list. A folder with no majority and no usable folder name is left alone rather than guessed at.

#### All or nothing per folder

Each immediate subdirectory of the directory you run in is organized as a unit. If a single file in it cannot be identified, that whole folder is left exactly as it was — no half-renamed seasons to pick apart by hand.

Folders are independent of each other, so running in a show's directory:

```
Season 1/   <- one unreadable file: the whole folder stays put
Season 2/   <- clean: organized normally
```

Fix the one file in `Season 1`, re-run, and it goes through. Files sitting loose in the working directory belong to no folder, so each one stands or falls on its own.

If a move fails partway through a folder (a disk error, or a target that appeared mid-run), the files already moved from that folder are put back and the undo log is trimmed accordingly.

#### Where the name comes from

Filenames are sometimes abbreviated to the point of being useless — `abc-x.1080p.mkv` yields only a lowercase "x" and no year at all. Maatr therefore reads four sources, in this order of authority:

1. **TMDB**, for the international title of a film — see below. This overrules everything else.
2. **The MKV's own title metadata.** Files often store the full title, and frequently the year with it. When present and plausible, this wins for title and year.
3. **The filename**, for everything else: resolution, season and episode numbers.
4. **The folder name**, as a last resort for the year only — a plain number between 1900 and 2100. Resolution tokens (`2160p`, `1080p`, `1920x1080`) can never be read as years. If a folder offers two candidates, Maatr refuses to pick and says so.

```
Example.Movie.2018.1080p-ABC/abc-x.1080p.mkv
  (MKV metadata says "Example Movie: The Subtitle (2018)")
  -> Example Movie - The Subtitle (2018)/Example Movie - The Subtitle (2018) [1080p] [ENG-GER].mkv
```

This works whether you run Maatr inside the folder or above it.

Two guards keep this from making things worse:

- **Tool defaults are ignored.** Placeholder titles like `video`, `untitled` or `Encoded by <tool>` are rejected, and Maatr falls back to the filename.
- **An episode's container title is usually the *episode* name**, not the series. An episode title will never replace the series name. The embedded title is only trusted for a series file when it actually looks like a series designation, e.g. `Example Series S01E03`.

A generic parent folder like `Movies` or `Downloads` is never used as a title — only as a possible source of a year.

#### The international title

German releases are named after the dubbed title, which is often a different film's name entirely. Maatr looks the film up on [TMDB](https://www.themoviedb.org) and uses the international English title instead:

```
Phantastische.Tierwesen.Grindelwalds.Verbrechen.2018.German...mkv
  -> Fantastic Beasts - The Crimes of Grindelwald (2018)/...mkv
```

There are two ways in, and the first is exact:

1. **The IMDb id from a `.nfo` sidecar.** Scene releases ship one, and it identifies the film outright — no matching, no ambiguity. The `.nfo` is only read; it is never moved, renamed or deleted, and neither is the folder it sits in.
2. **A title-and-year search.** Used when there is no `.nfo`. A hit is only accepted when its release year is within one year of the year Maatr already knows. Without a year to check against, or with no hit near it, **the title stays as it was** and the file is reported with the reason. A wrong film is worse than a German name.

A successful lookup also takes the year from TMDB, and the title is used with TMDB's own casing (so `WALL-E` stays `WALL-E`).

This needs a free credential: themoviedb.org → Settings → API → Developer. That page gives you two, and **either works** — Maatr uses the v3 endpoints and tells them apart automatically:

- the **API Key**, 32 hex characters, sent as `?api_key=…` (v3 auth);
- the **API Read Access Token**, a JWT, sent as an `Authorization: Bearer` header — it never appears in a URL, so it cannot leak into a log.

Put it in `$TMDB_API_KEY`, or in `[lookup] api_key` in your config — preferably the global one at `~/.config/maatr/maatr.toml`, so it does not end up in a repository. Without a key, or without a network, the lookup is skipped, every affected file is reported, and the run carries on with the parsed name. Results are cached in `~/.cache/maatr/lookup.json`, so a `--dry-run` warms the cache for the live run.

Series are not looked up; this applies to films only.

#### Samples and other extras

Releases often ship a small preview beside the film. Both files parse to the same title and year, so both want the same name — which used to fail the whole folder. Two rules sort it out, in order:

1. **By name.** A file is an extra when `sample`, `trailer` or `proof` sits at the *end* of its name (`...-sample.mkv`, `movie.sample.mkv`), or when it lives in a `Sample/`, `Extras/`, `Featurettes/` or `Bonus/` folder. The token must be at the end, so a real film called *Sample People (2000)* is never caught.
2. **By structure.** One folder per movie, one movie file per movie folder: in a folder whose files parse as a **movie**, the largest one is the film and the rest are extras — whatever they are called. This catches anything the vocabulary misses.

Rule 2 is deliberately fenced in:

- **Season folders are exempt.** A folder containing episodes is never reduced; every episode survives.
- **The winner must be at least 4× the runner-up.** A sample is about 1% of the film, so the rule always fires for the real case. Two comparable files — a genuine double feature, or a CD1/CD2 split — are an ambiguity, and the folder fails loudly instead of quietly sidelining half a movie.
- **A folder of nothing but extras organizes nothing**, so a 64 MB preview never lands in your library under the film's name.

Extras are **left exactly where they are**, like the `.nfo` — never moved, renamed or deleted — and listed at the end of the run with the reason. They are also skipped by the audio pass, so a sample never costs you a remux.

```
Die.purpurnen.Fluesse.2000...CONTRiBUTiON/
  ...contribution.mkv         10G  -> The Crimson Rivers (2000)/...mkv
  ...contribution.sample.mkv   64M -> left untouched: sample file, not the feature
```

#### Names that survive the trip to a NAS

Media drives are read over SMB from more than one operating system, so titles are reduced to plain ASCII. macOS stores umlauts decomposed and a NAS expects them composed; the two are different bytes and do not compare equal, which is how the same file ends up visible under two names, or under none.

| | |
|---|---|
| `Ä Ö Ü ä ö ü ß` | `Ae Oe Ue ae oe ue ss` — German first, so `Ä` becomes `Ae` and not `A` |
| `Amélie`, `Pokémon` | `Amelie`, `Pokemon` — remaining accents are stripped to their base letter |
| `Solo: A Star Wars Story` | `Solo - A Star Wars Story` — any `:`, with or without a space |
| `Mike's Dream`, `Mamma Mia!` | `Mikes Dream`, `Mamma Mia` — `' ’ , ! ?` are dropped |
| `Fire & Ice` | `Fire and Ice` |
| `/ \ * ? " < > \|` and control characters | removed; a title can never steer the move |

Names are also capped at 255 bytes per level, keeping the extension.

This applies to new runs only — files you organized earlier keep their names until you run `organize` over them again. The table can be extended under `[naming]` in the config.

#### Safety rules

Maatr will never destroy a file it cannot identify:

- **No guessed names.** A file is only moved once its title is known, and an episode also needs a season and an episode number. Nothing is invented, so a folder of cryptic filenames can no longer collapse into a single `Unknown` file.
- **No overwriting.** If two files resolve to the same target name, the second one stays where it is and is reported. The destination is reserved with an exclusive create before the move, so a collision can never silently replace an existing file.
- **Folder context is used.** Maatr feeds the whole relative path to the parser, so a cryptic `ep1.mkv` inside `Example Series/Season 2/` is still recognised.
- **Titles cannot escape the directory.** Path separators and control characters are stripped from parsed values, and every target is verified to stay inside the working directory.
- **Undo log is written as it goes.** The history file is saved after every move, so an interrupted run is still revertible, and `undo` refuses to overwrite a file that reappeared at the original path.

Files that are left untouched are listed at the end of every run, with the reason.

### Audio cleanup

Maatr can reduce the audio tracks in your MKVs to one preferred language at the best available quality and one secondary language at the smallest available size, with the preferred one flagged as default.

```bash
maatr audio              # plan, confirm, then work
maatr audio --dry-run    # show the plan and exit
maatr organize --audio   # clean audio first, then rename
```

With `organize --audio`, both halves of the run are approved before anything is written: the audio plan first, then the review list of new names — which already shows the `[ENG-GER]` tag the file will carry *after* the remux. Declining the names leaves the tracks alone too.

Nothing is changed until you approve the plan. Maatr prints every audio track of every file, marked KEEP or DROP with its size, and asks once:

```
Show.S01E01.1080p.mkv
  DROP  ger AC-3 5.1     471KB
  KEEP  ger AAC 2.0       94KB
  KEEP  eng AC-3 5.1     330KB  -> default
  DROP  eng AAC 2.0       71KB
  DROP  ita AAC 2.0      118KB
  SUBS  ger default flag cleared

1 file(s): 1 remux, 0 flag-only. Frees about 659KB.
Remuxing is irreversible: dropped tracks cannot be recovered.
Proceed? [y/N]:
```

#### The rule

**Both languages present** (the usual case — 2× German, 2× English): the file is reduced to exactly one of each. English is the *largest* track, German the *smallest*, and English becomes the default. Tracks in any other language, including untagged `und` tracks, are dropped. This requires a remux.

**Only one of them present** (e.g. German + Portuguese + Spanish, or English + Italian): nothing is removed. Maatr only corrects the default flag — an instant in-place edit, no rewrite. All tracks are kept.

Commentary tracks never win a slot. A small German commentary track will not be mistaken for "the smallest German track". Detection uses the Matroska commentary flag where present, and the track name otherwise.

Subtitles, chapters, attachments and tags are always preserved. Any default *subtitle* flag is cleared, so nothing auto-enables itself over your English audio.

#### Safety

Each file is handled on its own, one at a time: remux to a temporary file, verify it, and only then replace the original. Verification checks that the surviving audio tracks are exactly the ones planned, that video and subtitle counts are unchanged, that exactly one default audio track exists in the right language, and that the duration still matches. If anything fails, the temporary file is deleted and **the original is left untouched**.

A file whose audio step fails is also left out of the renaming pass and reported, so everything that does get renamed matches the plan you approved.

`mtr undo` reverses moves, not remuxes. The confirmation prompt is the safety net — dropped tracks are gone for good.

#### Configuration

```toml
[audio]
preferred = "eng"         # kept at best quality, becomes default
secondary = "ger"         # kept at smallest size
fallback_default = "ger"  # default when preferred is absent
```

### Undo

If you made a mistake, you can revert the last organization run:

```bash
maatr undo
```

### Init
Create a default configuration file:
```bash
maatr init
```
- Use `--global` to create the config in `~/.config/maatr/maatr.toml` instead of the current directory.

### Configuration
Maatr searches for a `maatr.toml` file in the following order:
1. The current working directory.
2. `~/.config/maatr/maatr.toml`.

If no file is found, it uses its internal defaults. You can edit the config file to customize your naming templates and audio language mappings.

```toml
[lookup]
enabled = true            # or organize --no-lookup for one run
api_key = ""              # $TMDB_API_KEY wins over this
language = "en-US"
timeout = 8
cache = "~/.cache/maatr/lookup.json"

[naming]
ascii_only = true
colon = " - "
drop = "'’‘`,!?¿¡"

[organize]
primary_by_size = true      # largest file in a movie folder is the film
primary_size_ratio = 4      # ...but only if it is this much bigger
extra_tokens = ["sample", "trailer", "proof"]
extra_dirs = ["sample", "extras", "featurettes", "bonus"]

[naming.replace]          # extends the built-in table, does not replace it
"ß" = "ss"
"&" = " and "
```

Note that config files are **not** merged with the defaults — the first file found is used as-is. Any setting you leave out falls back to Maatr's built-in value, so a partial file is safe.

