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
- Use `--ask` to manually confirm media types for files that can't be automatically identified.

### Undo

If you made a mistake, you can revert the last organization run:

```bash
maatr undo
```

### Configuration

On the first run, Maatr creates a `maatr.toml` file in the current directory. You can edit this file to customize your naming templates and audio language mappings.
