import os
import json
import shutil
import subprocess
import tomllib
import click
from guessit import guessit

STATE_FILE = ".maatr_history.json"
LOCAL_CONFIG = "maatr.toml"
GLOBAL_CONFIG_DIR = os.path.expanduser("~/.config/maatr")
GLOBAL_CONFIG_FILE = os.path.join(GLOBAL_CONFIG_DIR, "maatr.toml")

DEFAULT_CONFIG = """
[templates.movie]
# Available variables: title, year, resolution, audio, ext
folder = "{title} ({year})"
file = "{title} ({year}) [{resolution}] [{audio}]{ext}"

[templates.episode]
# Available variables: title, season, episode, season_pad, episode_pad, resolution, audio, ext
folder = "{title}/{title} Season {season}/{title} S{season_pad}E{episode_pad}"
file = "{title} S{season_pad}E{episode_pad} [{resolution}] [{audio}]{ext}"

[audio]
enforce_first = "ENG"
default_fallback = "ENG"

[audio.mapping]
deu = "GER"
ger = "GER"
de = "GER"
ita = "ITA"
it = "ITA"
fra = "FRE"
fre = "FRE"
fr = "FRE"
spa = "SPA"
es = "SPA"
eng = "ENG"
en = "ENG"
"""


def load_config():
    """Loads config from local dir or global ~/.config/maatr/."""
    if os.path.exists(LOCAL_CONFIG):
        with open(LOCAL_CONFIG, "rb") as f:
            return tomllib.load(f)

    if os.path.exists(GLOBAL_CONFIG_FILE):
        with open(GLOBAL_CONFIG_FILE, "rb") as f:
            return tomllib.load(f)

    # Fallback to default if no file exists (don't auto-create)
    return tomllib.loads(DEFAULT_CONFIG)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return []


def save_state(history):
    with open(STATE_FILE, "w") as f:
        json.dump(history, f, indent=4)


def cleanup_empty_dirs(directory):
    cleaned_count = 0
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in dirs:
            folder_path = os.path.join(root, name)
            try:
                os.rmdir(folder_path)
                click.secho(f"Swept empty folder: {folder_path}", dim=True)
                cleaned_count += 1
            except OSError:
                pass
    return cleaned_count


def get_audio_languages(filepath, config):
    """Probes for audio and uses the config mappings."""
    audio_cfg = config.get("audio", {})
    mapping = audio_cfg.get("mapping", {})
    fallback = audio_cfg.get("default_fallback", "ENG")
    enforce_first = audio_cfg.get("enforce_first", "ENG")

    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index:stream_tags=language",
            "-of",
            "json",
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        langs = set()
        for stream in data.get("streams", []):
            lang = stream.get("tags", {}).get("language")
            if lang and lang.lower() != "und":
                lang_clean = lang.lower().strip()
                mapped_lang = mapping.get(lang_clean, lang_clean.upper()[:3])
                langs.add(mapped_lang)

        if not langs:
            return fallback

        langs_list = list(langs)
        if enforce_first in langs_list:
            langs_list.remove(enforce_first)
            langs_list.insert(0, enforce_first)

        return "-".join(langs_list)
    except Exception:
        return fallback


def format_path(template, data):
    """Injects data into the template and cleans up missing variable artifacts."""
    # Replace None values with empty strings so they can be cleaned up
    safe_data = {k: (v if v is not None else "") for k, v in data.items()}

    result = template.format(**safe_data)

    # Clean up empty brackets/parentheses caused by missing data
    result = result.replace("[]", "").replace("()", "")
    result = result.replace(" []", "").replace(" ()", "")
    # Clean up double spaces
    result = " ".join(result.split())
    # Fix potential space before the file extension
    result = result.replace(" .", ".")

    return result


def process_media(guess, filepath, config, media_type):
    """Extracts variables and generates the final relative path based on config."""
    # Build the massive dictionary of variables that templates can use
    data = {
        "title": guess.get("title", "Unknown").title(),
        "year": guess.get("year", ""),
        "season": guess.get("season", 1),
        "episode": guess.get("episode", 1),
        "season_pad": str(guess.get("season", 1)).zfill(2),
        "episode_pad": str(guess.get("episode", 1)).zfill(2),
        "resolution": guess.get("screen_size", ""),
        "audio": get_audio_languages(filepath, config),
        "ext": os.path.splitext(filepath)[1],
    }

    templates = config.get("templates", {}).get(media_type, {})
    folder_template = templates.get("folder", "")
    file_template = templates.get("file", "{title}{ext}")  # fallback

    target_folder = format_path(folder_template, data) if folder_template else ""
    target_file = format_path(file_template, data)

    return os.path.join(target_folder, target_file)


@click.group()
def cli():
    """Maatr: Bring balance and order to your media library."""
    pass


@cli.command()
@click.option("--global", "is_global", is_flag=True, help="Create config in ~/.config/maatr/")
def init(is_global):
    """Initialize a default maatr.toml configuration file."""
    target = GLOBAL_CONFIG_FILE if is_global else LOCAL_CONFIG

    if os.path.exists(target):
        if not click.confirm(f"{target} already exists. Overwrite?"):
            return

    if is_global:
        os.makedirs(GLOBAL_CONFIG_DIR, exist_ok=True)

    with open(target, "w") as f:
        f.write(DEFAULT_CONFIG.strip())

    click.secho(f"Created config: {target}", fg="green")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview changes without moving files.")

@click.option("--ask", is_flag=True, help="Ask for confirmation on unknown files.")
def organize(dry_run, ask):
    """Organize media files in the current directory."""
    cwd = os.getcwd()
    config = load_config()
    history = load_state()
    valid_exts = (".mkv", ".mp4", ".avi")

    click.echo(f"Running Maatr in {'DRY-RUN mode' if dry_run else 'LIVE mode'}...")
    click.echo("-" * 40)

    for root, _, files in os.walk(cwd):
        for file in files:
            if not file.lower().endswith(valid_exts):
                continue

            original_path = os.path.join(root, file)

            # Simple check to avoid processing files we already organized
            # (Assumes your templates use these brackets. Can be refined later.)
            if "[" in file and "]" in file and "Season" in root:
                continue

            guess = guessit(file)
            media_type = guess.get("type")

            if media_type not in ["movie", "episode"]:
                if ask:
                    click.secho(f"\n[?] Unknown media: {file}", fg="yellow")
                    choice = click.prompt(
                        "Is this a [m]ovie, [e]pisode, or [s]kip?", type=str
                    ).lower()
                    if choice == "m":
                        media_type = "movie"
                    elif choice == "e":
                        media_type = "episode"
                    else:
                        continue
                else:
                    click.secho(f"Skipping unknown: {file}", fg="red")
                    continue

            relative_new_path = process_media(guess, original_path, config, media_type)
            new_path = os.path.join(cwd, relative_new_path)

            click.echo(f"Found: {file}")
            click.secho(f"  -> {relative_new_path}\n", fg="green")

            if not dry_run:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                shutil.move(original_path, new_path)
                history.append({"original": original_path, "new": new_path})

    if not dry_run:
        save_state(history)
        click.echo("-" * 40)
        click.echo("Cleaning up leftover directories...")
        swept = cleanup_empty_dirs(cwd)
        click.echo(f"Organization complete. Swept {swept} empty folders.")
        click.echo("Run 'mtr undo' to revert.")


@cli.command()
def undo():
    """Revert the last organization run using the state log."""
    history = load_state()
    if not history:
        click.echo("No history found. Nothing to undo.")
        return

    click.secho("Reverting changes...", fg="yellow")
    for action in reversed(history):
        orig = action["original"]
        new = action["new"]

        if os.path.exists(new):
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.move(new, orig)
            click.echo(f"Reverted: {os.path.basename(new)}")
            try:
                os.removedirs(os.path.dirname(new))
            except OSError:
                pass

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    click.secho("Undo complete!", fg="green")


if __name__ == "__main__":
    cli()
