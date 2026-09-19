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

#### Safety rules

Maatr will never destroy a file it cannot identify:

- **No guessed names.** A file is only moved once its title is known, and an episode also needs a season and an episode number. Nothing is invented, so a folder of cryptic filenames can no longer collapse into a single `Unknown` file.
- **No overwriting.** If two files resolve to the same target name, the second one stays where it is and is reported. The destination is reserved with an exclusive create before the move, so a collision can never silently replace an existing file.
- **Folder context is used.** Maatr feeds the whole relative path to the parser, so a cryptic `ep1.mkv` inside `Breaking Bad/Season 2/` is still recognised.
- **Titles cannot escape the directory.** Path separators and control characters are stripped from parsed values, and every target is verified to stay inside the working directory.
- **Undo log is written as it goes.** The history file is saved after every move, so an interrupted run is still revertible, and `undo` refuses to overwrite a file that reappeared at the original path.

Files that are left untouched are listed at the end of every run, with the reason.

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

