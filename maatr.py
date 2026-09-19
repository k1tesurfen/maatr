import os
import json
import re
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

        langs_list = sorted(langs)
        if enforce_first in langs_list:
            langs_list.remove(enforce_first)
            langs_list.insert(0, enforce_first)

        return "-".join(langs_list)
    except Exception:
        return fallback


def sanitize_component(value):
    """Strips anything from a template variable that could escape the target dir.

    Template variables come from filenames we did not write, so a title like
    "../../etc" or "Foo/Bar" must never be able to steer the move.
    """
    text = str(value)
    text = text.replace(os.sep, " ")
    if os.altsep:
        text = text.replace(os.altsep, " ")
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = text.strip(" .")
    return " ".join(text.split())


def format_path(template, data):
    """Injects data into the template and cleans up missing variable artifacts."""
    # Sanitize values (not the template: its separators are intentional) and
    # replace None with empty strings so they can be cleaned up.
    safe_data = {
        k: (sanitize_component(v) if v is not None else "") for k, v in data.items()
    }
    safe_data["ext"] = data.get("ext", "")

    result = template.format(**safe_data)

    # Clean up empty brackets/parentheses caused by missing data
    result = result.replace("[]", "").replace("()", "")
    result = result.replace(" []", "").replace(" ()", "")
    # Clean up double spaces
    result = " ".join(result.split())
    # Fix potential space before the file extension
    result = result.replace(" .", ".")

    return result


# Fields that must be known before a file may be moved. Nothing here gets a
# default: a guessed default is what silently merges a whole series into one file.
REQUIRED_FIELDS = {
    "movie": ["title"],
    "episode": ["title", "season", "episode"],
}


def missing_fields(guess, media_type, overrides=None):
    """Returns the required identifying fields guessit could not determine."""
    overrides = overrides or {}
    missing = []
    for field in REQUIRED_FIELDS.get(media_type, []):
        value = overrides.get(field, guess.get(field))
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
        elif isinstance(value, list):
            # guessit returns a list for multi-episode files; take it as known
            # only if it is non-empty.
            if not value:
                missing.append(field)
    return missing


def process_media(guess, filepath, config, media_type, overrides=None):
    """Extracts variables and generates the final relative path based on config."""
    overrides = overrides or {}

    def field(name):
        return overrides.get(name, guess.get(name))

    title = field("title")
    season = field("season")
    episode = field("episode")

    # Multi-episode files come back as a list; use the first number for padding.
    if isinstance(episode, list):
        episode = episode[0]
    if isinstance(season, list):
        season = season[0]

    data = {
        "title": str(title).title(),
        "year": guess.get("year", ""),
        "season": season if season is not None else "",
        "episode": episode if episode is not None else "",
        "season_pad": str(season).zfill(2) if season is not None else "",
        "episode_pad": str(episode).zfill(2) if episode is not None else "",
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


def move_without_overwrite(src, dst):
    """Moves src to dst, refusing to clobber an existing file.

    The destination is reserved with O_EXCL first, so two files that resolve to
    the same name can never silently overwrite each other.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    try:
        shutil.move(src, dst)
    except Exception:
        # Don't leave the empty reservation behind if the move failed.
        if os.path.exists(dst) and os.path.getsize(dst) == 0:
            os.remove(dst)
        raise
    return True


def prompt_for_fields(filename, media_type, missing):
    """Asks the user for the fields guessit could not determine."""
    overrides = {}
    for f in missing:
        if f == "title":
            answer = click.prompt(f"  Title for '{filename}'", type=str, default="").strip()
            if not answer:
                return None
            overrides["title"] = answer
        else:
            answer = click.prompt(f"  {f.capitalize()} number", type=int, default=-1)
            if answer < 0:
                return None
            overrides[f] = answer
    return overrides


def group_for(original_path, cwd):
    """Returns the unit that succeeds or fails together, plus a label for it.

    A media library is organized per folder: running Maatr in a show's directory
    means each immediate subdirectory is a season. A file sitting loose in the
    working directory belongs to no folder, so it stands on its own.
    """
    relative = os.path.relpath(original_path, cwd)
    parts = relative.split(os.sep)
    if len(parts) == 1:
        return ("file", relative), relative
    return ("dir", parts[0]), parts[0] + os.sep


def rollback(moves):
    """Puts a partially moved group back where it came from."""
    restored = []
    for original_path, new_path in reversed(moves):
        if not os.path.exists(new_path) or os.path.exists(original_path):
            continue
        try:
            if move_without_overwrite(new_path, original_path):
                restored.append((original_path, new_path))
                try:
                    os.removedirs(os.path.dirname(new_path))
                except OSError:
                    pass
        except OSError:
            pass
    return restored


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
@click.option(
    "--partial",
    is_flag=True,
    help="Organize every file that can be identified, instead of skipping its whole folder.",
)
def organize(dry_run, ask, partial):
    """Organize media files in the current directory."""
    cwd = os.getcwd()
    config = load_config()
    history = load_state()
    valid_exts = (".mkv", ".mp4", ".avi")

    click.echo(f"Running Maatr in {'DRY-RUN mode' if dry_run else 'LIVE mode'}...")
    click.echo("-" * 40)

    # Snapshot the file list before touching anything: moving files into
    # subdirectories of a tree we are still walking would make os.walk hand us
    # our own output again.
    candidates = []
    for root, _, files in os.walk(cwd):
        for file in sorted(files):
            if file.lower().endswith(valid_exts):
                candidates.append(os.path.join(root, file))
    candidates.sort()

    planned = {}  # group key -> list of (original_path, new_path, relative_new_path)
    labels = {}  # group key -> display name
    claimed = {}  # new_path -> original_path, to catch collisions within this run
    failed = {}  # group key -> (offending file, reason)
    skipped = []  # (original_path, reason)

    def note_skip(path, reason, breaks_group=True):
        """Records a file we won't move, and by default fails its whole folder."""
        skipped.append((os.path.relpath(path, cwd), reason))
        key, label = group_for(path, cwd)
        labels.setdefault(key, label)
        if breaks_group and not partial and key not in failed:
            failed[key] = (os.path.relpath(path, cwd), reason)

    for original_path in candidates:
        file = os.path.basename(original_path)
        relative_source = os.path.relpath(original_path, cwd)
        key, label = group_for(original_path, cwd)
        labels.setdefault(key, label)

        # Feed the whole relative path to guessit: when the filename itself is
        # cryptic, the folder it sits in usually still carries the series name.
        guess = guessit(relative_source)
        media_type = guess.get("type")
        overrides = {}

        if media_type not in ["movie", "episode"]:
            if ask:
                click.secho(f"\n[?] Unknown media: {relative_source}", fg="yellow")
                choice = click.prompt(
                    "Is this a [m]ovie, [e]pisode, or [s]kip?", type=str
                ).lower()
                if choice == "m":
                    media_type = "movie"
                elif choice == "e":
                    media_type = "episode"
                else:
                    # A deliberate choice, so it doesn't condemn the rest.
                    note_skip(original_path, "skipped by user", breaks_group=False)
                    continue
            else:
                note_skip(original_path, "could not tell movie from episode")
                continue

        missing = missing_fields(guess, media_type)
        if missing:
            if ask:
                click.secho(
                    f"\n[?] Could not determine {', '.join(missing)} for: {relative_source}",
                    fg="yellow",
                )
                answer = prompt_for_fields(file, media_type, missing)
                if answer is None:
                    note_skip(original_path, "skipped by user", breaks_group=False)
                    continue
                overrides = answer
            else:
                note_skip(
                    original_path, f"unknown {', '.join(missing)} (use --ask to fill in)"
                )
                continue

        relative_new_path = process_media(
            guess, original_path, config, media_type, overrides
        )
        new_path = os.path.normpath(os.path.join(cwd, relative_new_path))

        # A sanitized title can't escape cwd, but verify rather than trust.
        if os.path.commonpath([cwd, new_path]) != cwd:
            note_skip(original_path, "target would land outside this directory")
            continue

        if new_path == original_path:
            # Nothing to do and nothing wrong: a second run over a tidy folder.
            note_skip(original_path, "already in place", breaks_group=False)
            continue

        if new_path in claimed:
            note_skip(
                original_path,
                f"would overwrite {os.path.relpath(claimed[new_path], cwd)}",
            )
            continue

        if os.path.exists(new_path):
            note_skip(original_path, f"target already exists: {relative_new_path}")
            continue

        claimed[new_path] = original_path
        planned.setdefault(key, []).append((original_path, new_path, relative_new_path))

    # A folder is organized as a whole or not at all: an unreadable episode in
    # the middle of a season would otherwise leave that season half-renamed.
    abandoned = []
    for key, (offender, reason) in failed.items():
        for original_path, _, _ in planned.pop(key, []):
            abandoned.append(os.path.relpath(original_path, cwd))

    moved = 0
    for key, moves in planned.items():
        done = []
        group_failed = None

        for original_path, new_path, relative_new_path in moves:
            click.echo(f"Found: {os.path.relpath(original_path, cwd)}")
            click.secho(f"  -> {relative_new_path}\n", fg="green")

            if dry_run:
                continue

            try:
                reserved = move_without_overwrite(original_path, new_path)
            except OSError as exc:
                group_failed = (os.path.relpath(original_path, cwd), str(exc))
                break
            if not reserved:
                group_failed = (
                    os.path.relpath(original_path, cwd),
                    f"target appeared during the run: {relative_new_path}",
                )
                break

            done.append((original_path, new_path))
            history.append({"original": original_path, "new": new_path})
            # Persist after every move so an interrupted run is still undoable.
            save_state(history)

        if group_failed and not partial:
            offender, reason = group_failed
            click.secho(
                f"  !! {offender}: {reason}\n"
                f"  !! rolling back {labels[key]} ({len(done)} file(s) already moved)",
                fg="red",
            )
            restored = rollback(done)
            restored_set = set(restored)
            history = [
                entry
                for entry in history
                if (entry["original"], entry["new"]) not in restored_set
            ]
            save_state(history)
            failed[key] = (offender, reason)
            abandoned.extend(os.path.relpath(o, cwd) for o, _, _ in moves)
            if len(restored) != len(done):
                click.secho(
                    f"  !! only {len(restored)} of {len(done)} could be put back; "
                    f"run 'mtr undo' to finish reverting",
                    fg="red",
                )
            moved += len(done) - len(restored)
        elif group_failed:
            offender, reason = group_failed
            click.secho(f"  !! {offender}: {reason}", fg="red")
            moved += len(done)
        else:
            moved += len(done)

    # Loose files are their own group; reporting them as skipped folders would
    # just repeat the per-file list below.
    failed_folders = {k: v for k, v in failed.items() if k[0] == "dir"}
    if failed_folders:
        click.echo("-" * 40)
        click.secho(f"Skipped {len(failed_folders)} folder(s) entirely:", fg="yellow")
        for key, (offender, reason) in failed_folders.items():
            click.secho(f"  {labels[key]}  <- {offender}: {reason}", fg="yellow")

    if skipped or abandoned:
        click.echo("-" * 40)
        click.secho("Left untouched:", fg="yellow")
        for path, reason in skipped:
            click.secho(f"  {path}: {reason}", fg="yellow")
        for path in abandoned:
            click.secho(f"  {path}: folder skipped", fg="yellow")

    if not dry_run:
        save_state(history)
        click.echo("-" * 40)
        click.echo("Cleaning up leftover directories...")
        swept = cleanup_empty_dirs(cwd)
        click.echo(
            f"Organization complete. Moved {moved}, "
            f"left {len(skipped) + len(abandoned)}. Swept {swept} empty folders."
        )
        click.echo("Run 'mtr undo' to revert.")


@cli.command()
def undo():
    """Revert the last organization run using the state log."""
    history = load_state()
    if not history:
        click.echo("No history found. Nothing to undo.")
        return

    click.secho("Reverting changes...", fg="yellow")
    remaining = []
    for action in reversed(history):
        orig = action["original"]
        new = action["new"]

        if not os.path.exists(new):
            continue

        if os.path.exists(orig):
            click.secho(
                f"  !! {os.path.basename(orig)} exists again, not reverting {new}",
                fg="red",
            )
            remaining.append(action)
            continue

        os.makedirs(os.path.dirname(orig), exist_ok=True)
        if not move_without_overwrite(new, orig):
            click.secho(f"  !! could not revert {new}", fg="red")
            remaining.append(action)
            continue

        click.echo(f"Reverted: {os.path.basename(new)}")
        try:
            os.removedirs(os.path.dirname(new))
        except OSError:
            pass

    if remaining:
        save_state(list(reversed(remaining)))
        click.secho(
            f"Undo finished with {len(remaining)} entries left in the log.", fg="yellow"
        )
        return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    click.secho("Undo complete!", fg="green")


if __name__ == "__main__":
    cli()
