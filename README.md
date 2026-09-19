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
- Use `--ask` to fill in the details Maatr could not read from a file (media type, title, season, episode).
- Use `--partial` to organize every file that can be identified instead of skipping its whole folder.

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

Filenames are sometimes abbreviated to the point of being useless — `abc-x.1080p.mkv` yields only a lowercase "x" and no year at all. Maatr therefore reads three sources, in this order of authority:

1. **The MKV's own title metadata.** Files often store the full title, and frequently the year with it. When present and plausible, this wins for title and year.
2. **The filename**, for everything else: resolution, season and episode numbers.
3. **The folder name**, as a last resort for the year only — a plain number between 1900 and 2100. Resolution tokens (`2160p`, `1080p`, `1920x1080`) can never be read as years. If a folder offers two candidates, Maatr refuses to pick and says so.

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

Note that `:` is replaced with ` - ` in filenames, since it is illegal on exFAT, NTFS and SMB shares, where media drives usually live.

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

