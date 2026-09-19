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

# Audio track cleanup (mtr audio / mtr organize --audio).
# When BOTH languages are present the file is reduced to one track each:
# the preferred one at best quality (and flagged default), the secondary one
# at smallest size. If only one of them is present, nothing is removed and
# only the default flag is corrected.
preferred = "eng"
secondary = "ger"
fallback_default = "ger"

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


# --- Audio track cleanup -------------------------------------------------
#
# The goal is one preferred-language track (best quality, default flag) plus one
# secondary-language track (smallest, kept only as a fallback). Everything else
# goes. Dropping tracks means rewriting the file, so the rule only fires when
# both languages are actually there; otherwise we settle for fixing the default
# flag, which is an instant in-place edit.

TEMP_SUFFIX = ".maatr-tmp.mkv"

# ISO-639 is a mess: Matroska may carry 'ger', 'deu' or the IETF 'de-DE'.
LANG_ALIASES = {
    "deu": "ger",
    "de": "ger",
    "ger": "ger",
    "eng": "eng",
    "en": "eng",
    "fra": "fre",
    "fre": "fre",
    "fr": "fre",
    "ita": "ita",
    "it": "ita",
    "spa": "spa",
    "es": "spa",
    "por": "por",
    "pt": "por",
    "nld": "dut",
    "dut": "dut",
    "nl": "dut",
}

COMMENTARY_RE = re.compile(r"commentary|kommentar|audiokommentar", re.IGNORECASE)


def normalize_lang(value):
    """Folds a Matroska language tag down to one canonical 3-letter code."""
    if not value:
        return "und"
    code = str(value).strip().lower().replace("_", "-")
    if code in LANG_ALIASES:
        return LANG_ALIASES[code]
    # 'de-DE', 'en-GB' and friends: the part before the dash is the language.
    base = code.split("-")[0]
    if base in LANG_ALIASES:
        return LANG_ALIASES[base]
    return base[:3] if base else "und"


def track_language(track):
    """Prefers the IETF tag when present; it is the more precise of the two."""
    props = track.get("properties", {})
    ietf = props.get("language_ietf")
    if ietf and ietf.lower() not in ("und", "mis", "zxx"):
        return normalize_lang(ietf)
    return normalize_lang(props.get("language"))


def is_commentary(track):
    """Best effort: the flag if it was set, otherwise the track name."""
    props = track.get("properties", {})
    if props.get("flag_commentary"):
        return True
    return bool(COMMENTARY_RE.search(props.get("track_name") or ""))


def probe_tracks(path):
    """Returns mkvmerge's view of the file, or None if it can't be read."""
    try:
        result = subprocess.run(
            ["mkvmerge", "-J", path], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None


def packet_sum(path, audio_index):
    """Exact track size for files that carry no statistics tags.

    Reads every packet header of one audio stream, so it is slower than the tag
    lookup but gives the identical number.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                f"a:{audio_index}",
                "-show_entries",
                "packet=size",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    total = 0
    for line in result.stdout.splitlines():
        line = line.strip().rstrip(",")
        if line.isdigit():
            total += int(line)
    return total or None


def track_size(track, path, audio_index):
    """Bytes occupied by an audio track: statistics tag first, packets second."""
    tagged = track.get("properties", {}).get("tag_number_of_bytes")
    if tagged:
        try:
            return int(str(tagged).strip())
        except ValueError:
            pass
    if path is None:
        return None
    return packet_sum(path, audio_index)


def describe_track(track):
    """One-line human description used in the approval plan."""
    props = track.get("properties", {})
    channels = props.get("audio_channels")
    layout = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}ch")
    parts = [track_language(track), track.get("codec", "?"), layout]
    if is_commentary(track):
        parts.append("(commentary)")
    name = props.get("track_name")
    if name:
        parts.append(f"'{name}'")
    return " ".join(str(p) for p in parts)


def human_size(num_bytes):
    if num_bytes is None:
        return "?"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit in ("B", "KB") else f"{value:.1f}{unit}"
        value /= 1024


def pick_track(tracks, largest):
    """Deterministic best/worst pick, so repeat runs agree with each other."""

    def rank(track):
        props = track.get("properties", {})
        return (
            track["_size"],
            props.get("audio_channels") or 0,
            props.get("audio_sampling_frequency") or 0,
        )

    # Sort by id first so ties fall to the lowest track id either way.
    ordered = sorted(tracks, key=lambda t: t["id"])
    return max(ordered, key=rank) if largest else min(ordered, key=rank)


def plan_audio(info, config, path=None):
    """Decides what to do with one file's audio. Pure apart from size lookups.

    Returns a dict with an 'action' of remux, flags, none or skip.
    """
    audio_cfg = config.get("audio", {})
    preferred = normalize_lang(audio_cfg.get("preferred", "eng"))
    secondary = normalize_lang(audio_cfg.get("secondary", "ger"))
    fallback = normalize_lang(audio_cfg.get("fallback_default", "ger"))

    tracks = info.get("tracks", [])
    audio = [t for t in tracks if t.get("type") == "audio"]
    subtitles = [t for t in tracks if t.get("type") == "subtitles"]

    if not audio:
        return {"action": "skip", "reason": "no audio tracks"}

    for position, track in enumerate(audio):
        track["_lang"] = track_language(track)
        track["_index"] = position
        track["_commentary"] = is_commentary(track)
        # Tag lookup only: free, and enough to show sizes even when we end up
        # not needing them to choose. The costly fallback comes later.
        track["_size"] = track_size(track, None, position)

    by_lang = {}
    for track in audio:
        by_lang.setdefault(track["_lang"], []).append(track)

    has_preferred = preferred in by_lang
    has_secondary = secondary in by_lang

    # The full rule needs both languages present; anything else is a flag fix.
    if not (has_preferred and has_secondary):
        target_lang = preferred if has_preferred else fallback
        candidates = by_lang.get(target_lang)
        if not candidates:
            return {"action": "none", "audio": audio, "reason": "no track to promote"}
        wanted = sorted(candidates, key=lambda t: t["id"])[0]
        already = wanted["properties"].get("default_track") and not any(
            t["properties"].get("default_track") for t in audio if t["id"] != wanted["id"]
        )
        if already:
            return {"action": "none", "audio": audio, "reason": "default already correct"}
        return {
            "action": "flags",
            "audio": audio,
            "subtitles": subtitles,
            "default_id": wanted["id"],
            "keep": audio,
            "drop": [],
        }

    # Both languages present: reduce to exactly one of each.
    for track in audio:
        if track["_size"] is None:
            track["_size"] = track_size(track, path, track["_index"])

    if any(t["_size"] is None for t in audio):
        return {"action": "skip", "reason": "could not determine audio track sizes"}

    def choose(lang, largest):
        pool = [t for t in by_lang[lang] if not t["_commentary"]]
        if not pool:  # every track of this language is commentary; take them all
            pool = by_lang[lang]
        return pick_track(pool, largest)

    keep_preferred = choose(preferred, largest=True)
    keep_secondary = choose(secondary, largest=False)
    keep_ids = {keep_preferred["id"], keep_secondary["id"]}
    keep = [t for t in audio if t["id"] in keep_ids]
    drop = [t for t in audio if t["id"] not in keep_ids]

    subtitle_default = any(t["properties"].get("default_track") for t in subtitles)
    default_wrong = not keep_preferred["properties"].get("default_track") or any(
        t["properties"].get("default_track")
        for t in audio
        if t["id"] != keep_preferred["id"]
    )

    if not drop:
        if not default_wrong and not subtitle_default:
            return {"action": "none", "audio": audio, "reason": "already clean"}
        # Nothing to remove, so a flag edit is enough.
        return {
            "action": "flags",
            "audio": audio,
            "subtitles": subtitles,
            "default_id": keep_preferred["id"],
            "keep": keep,
            "drop": [],
        }

    return {
        "action": "remux",
        "audio": audio,
        "subtitles": subtitles,
        "default_id": keep_preferred["id"],
        "keep": keep,
        "drop": drop,
        "freed": sum(t["_size"] for t in drop),
    }


def apply_flags(path, plan):
    """Sets the default flag in place. No rewrite, so this is near-instant."""
    cmd = ["mkvpropedit", path]
    for track in plan["audio"]:
        wanted = "1" if track["id"] == plan["default_id"] else "0"
        # mkvpropedit counts tracks from 1, in file order.
        cmd += ["--edit", f"track:{track['id'] + 1}", "--set", f"flag-default={wanted}"]
    for track in plan.get("subtitles", []):
        if track["properties"].get("default_track"):
            cmd += ["--edit", f"track:{track['id'] + 1}", "--set", "flag-default=0"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        return f"mkvpropedit failed: {(exc.stderr or '').strip()[:200]}"
    except OSError as exc:
        return str(exc)
    return None


def verify_remux(temp_path, plan, source_info):
    """Confirms the new file really is the old one minus the dropped tracks.

    Nothing is deleted until this passes, so a bad remux costs us a temp file
    and nothing else.
    """
    info = probe_tracks(temp_path)
    if info is None:
        return "result is not a readable Matroska file"

    new_audio = [t for t in info.get("tracks", []) if t.get("type") == "audio"]
    want = sorted(track_language(t) for t in plan["keep"])
    got = sorted(track_language(t) for t in new_audio)
    if got != want:
        return f"expected audio {want}, got {got}"

    old_video = len([t for t in source_info.get("tracks", []) if t.get("type") == "video"])
    new_video = len([t for t in info.get("tracks", []) if t.get("type") == "video"])
    if new_video != old_video:
        return f"video tracks changed ({old_video} -> {new_video})"

    old_subs = len([t for t in source_info.get("tracks", []) if t.get("type") == "subtitles"])
    new_subs = len([t for t in info.get("tracks", []) if t.get("type") == "subtitles"])
    if new_subs != old_subs:
        return f"subtitle tracks changed ({old_subs} -> {new_subs})"

    defaults = [t for t in new_audio if t["properties"].get("default_track")]
    if len(defaults) != 1:
        return f"expected exactly one default audio track, found {len(defaults)}"
    expected_default = track_language(
        next(t for t in plan["keep"] if t["id"] == plan["default_id"])
    )
    if track_language(defaults[0]) != expected_default:
        return f"default track is {track_language(defaults[0])}, expected {expected_default}"

    old_ms = (source_info.get("container", {}).get("properties", {}) or {}).get("duration")
    new_ms = (info.get("container", {}).get("properties", {}) or {}).get("duration")
    if old_ms and new_ms and abs(old_ms - new_ms) > 2_000_000_000:  # 2s in ns
        return f"duration changed ({old_ms} -> {new_ms})"

    if os.path.getsize(temp_path) == 0:
        return "result is empty"
    return None


def apply_remux(path, plan, source_info):
    """Remuxes to a temp file, verifies it, and only then replaces the original."""
    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(path) or ".").free
    if free < size:
        return f"not enough free space ({human_size(free)} free, need {human_size(size)})"

    temp_path = path + TEMP_SUFFIX
    keep_ids = ",".join(str(t["id"]) for t in plan["keep"])
    cmd = ["mkvmerge", "-q", "-o", temp_path, "--audio-tracks", keep_ids]
    for track in plan["keep"]:
        flag = "1" if track["id"] == plan["default_id"] else "0"
        cmd += ["--default-track-flag", f"{track['id']}:{flag}"]
    # Subtitles and everything else (chapters, attachments, tags) come along
    # untouched; we only make sure no subtitle auto-enables itself.
    for track in plan.get("subtitles", []):
        cmd += ["--default-track-flag", f"{track['id']}:0"]
    cmd.append(path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        # mkvmerge uses exit code 1 for warnings, which are not fatal.
        if result.returncode > 1:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
    except subprocess.CalledProcessError as exc:
        _discard(temp_path)
        return f"mkvmerge failed: {(exc.stderr or '').strip()[:200]}"
    except OSError as exc:
        _discard(temp_path)
        return str(exc)

    problem = verify_remux(temp_path, plan, source_info)
    if problem:
        _discard(temp_path)
        return f"verification failed: {problem}"

    try:
        os.replace(temp_path, path)
    except OSError as exc:
        _discard(temp_path)
        return f"could not replace original: {exc}"
    return None


def _discard(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def render_audio_plan(relative_path, plan, target=None):
    """Prints the full per-track breakdown shown before the confirmation."""
    click.secho(relative_path, bold=True)
    action = plan["action"]

    if action == "skip":
        click.secho(f"  SKIP  {plan['reason']}", fg="red")
        return
    if action == "none":
        click.secho(f"  OK    {plan.get('reason', 'nothing to do')}", dim=True)
        return

    keep_ids = {t["id"] for t in plan["keep"]}
    for track in plan["audio"]:
        size = human_size(track.get("_size"))
        if track["id"] in keep_ids:
            suffix = "  -> default" if track["id"] == plan["default_id"] else ""
            click.secho(
                f"  KEEP  {describe_track(track):<34} {size:>8}{suffix}", fg="green"
            )
        else:
            click.secho(f"  DROP  {describe_track(track):<34} {size:>8}", fg="red")

    for track in plan.get("subtitles", []):
        if track["properties"].get("default_track"):
            click.secho(
                f"  SUBS  {track_language(track)} default flag cleared", fg="yellow"
            )

    if action == "flags":
        click.secho("  (flag change only, no remux)", dim=True)
    if target:
        click.secho(f"  -> {target}", fg="cyan")


# --- Filling in what the filename does not say ---------------------------
#
# Some filenames are abbreviated to the point of being useless ("abc-x.1080p.mkv")
# while the MKV's own metadata and the folder name still carry title and year.

# Placeholder names and muxing-tool defaults that are not really titles.
JUNK_TITLE_WORDS = {
    "video",
    "movie",
    "film",
    "untitled",
    "unknown",
    "encode",
    "encoded",
    "output",
    "default",
    "title",
    "mkv",
    "sample",
}
# Common muxing/transcoding tools leave their own name in the title field.
JUNK_TITLE_RE = re.compile(
    r"^(encoded|created|muxed|converted|generated)\s+(by|with)\b|^handbrake|^makemkv",
    re.IGNORECASE,
)

MIN_YEAR = 1900
MAX_YEAR = 2100


def embedded_title(path):
    """The title stored inside the Matroska container, if there is a usable one."""
    if not path.lower().endswith(".mkv"):
        return None
    info = probe_tracks(path)
    if info is None:
        return None
    title = (info.get("container", {}).get("properties", {}) or {}).get("title")
    if not title:
        return None
    title = title.strip()
    if not title:
        return None

    # Guard against tool defaults, which guessit would happily accept.
    if JUNK_TITLE_RE.search(title):
        return None
    words = re.findall(r"[a-z0-9]+", title.lower())
    if not words:
        return None
    if len(words) == 1 and words[0] in JUNK_TITLE_WORDS:
        return None
    return title


def year_from_folders(path, levels=3):
    """Last resort: a plain 4-digit year in an ancestor folder name.

    Returns (year, note). The year is None when nothing was found, or when a
    folder offers more than one candidate and we refuse to guess between them.
    """
    directory = os.path.dirname(os.path.abspath(path))
    for _ in range(levels):
        name = os.path.basename(directory)
        if not name:
            break
        # Split on the usual filename separators. Resolution tokens such as
        # 1080p keep their letter, so they never look like a bare year, and the
        # range check rules out 720/1080/2160 on their own.
        tokens = re.split(r"[^0-9A-Za-z]+", name)
        candidates = []
        for token in tokens:
            if len(token) == 4 and token.isdigit():
                value = int(token)
                if MIN_YEAR <= value <= MAX_YEAR and value not in candidates:
                    candidates.append(value)
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return None, f"folder '{name}' offers several years {candidates}"
        directory = os.path.dirname(directory)
    return None, None


def enrich_guess(guess, path, media_type):
    """Adds title and year from the MKV metadata and the folder name.

    The filename stays in charge of everything else (resolution, episode
    numbers); this only fills in what abbreviated filenames tend to omit.
    """
    notes = []
    title = embedded_title(path)
    if title:
        inner = guessit(title)
        inner_title = inner.get("title")
        inner_year = inner.get("year")

        if media_type == "episode":
            # A series file's container title is often the EPISODE name, which
            # would be a terrible series title. Only trust it when it actually
            # looks like a series designation.
            usable = inner.get("type") == "episode" and inner_title
        else:
            usable = bool(inner_title)

        if usable and inner_title:
            if inner_title != guess.get("title"):
                notes.append(f"title from MKV metadata: {inner_title!r}")
            guess["title"] = inner_title
            if inner_year:
                guess["year"] = inner_year

    if not guess.get("year"):
        year, problem = year_from_folders(path)
        if year:
            guess["year"] = year
            notes.append(f"year {year} from folder name")
        elif problem:
            notes.append(problem)

    return guess, notes


def sanitize_component(value):
    """Strips anything from a template variable that could escape the target dir.

    Template variables come from filenames we did not write, so a title like
    "../../etc" or "Foo/Bar" must never be able to steer the move.
    """
    text = str(value)
    text = text.replace(os.sep, " ")
    if os.altsep:
        text = text.replace(os.altsep, " ")
    # ':' is legal on APFS but not on exFAT/NTFS/SMB, where media drives
    # usually live. The " - " form is the usual media-server convention.
    text = text.replace(":", " -")
    text = re.sub(r'[*?"<>|]', "", text)
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


def collect_media(cwd, extensions):
    """Snapshot of every media file under cwd, in stable order."""
    found = []
    for root, _, files in os.walk(cwd):
        for file in sorted(files):
            if file.lower().endswith(extensions) and not file.endswith(TEMP_SUFFIX):
                found.append(os.path.join(root, file))
    found.sort()
    return found


def build_audio_plans(paths, config, cwd):
    """Works out what each file needs. Returns (actionable, untouched)."""
    actionable = []  # (path, plan, source_info)
    untouched = []  # (relative path, reason)

    for path in paths:
        relative = os.path.relpath(path, cwd)
        if not path.lower().endswith(".mkv"):
            untouched.append((relative, "not a Matroska file, audio left alone"))
            continue

        info = probe_tracks(path)
        if info is None:
            untouched.append((relative, "could not read with mkvmerge"))
            continue

        plan = plan_audio(info, config, path)
        if plan["action"] in ("none", "skip"):
            untouched.append((relative, plan.get("reason", plan["action"])))
            continue
        actionable.append((path, plan, info))

    return actionable, untouched


def run_audio_phase(actionable, cwd):
    """Applies each plan one file at a time. Returns the paths that failed."""
    failed = {}
    for path, plan, info in actionable:
        relative = os.path.relpath(path, cwd)
        click.echo(f"Processing: {relative}")

        if plan["action"] == "flags":
            problem = apply_flags(path, plan)
        else:
            problem = apply_remux(path, plan, info)

        if problem:
            click.secho(f"  !! {problem} (original left untouched)", fg="red")
            failed[path] = problem
        else:
            freed = plan.get("freed")
            note = f", freed {human_size(freed)}" if freed else ""
            click.secho(f"  done{note}", fg="green")
    return failed


def audio_pass(cwd, config, dry_run, assume_yes, paths=None):
    """Plan, show, confirm, execute. Shared by `audio` and `organize --audio`.

    Returns (failed paths -> reason, confirmed) where confirmed is False if the
    user declined, so the caller can stop before renaming anything.
    """
    if paths is None:
        paths = collect_media(cwd, (".mkv", ".mp4", ".avi"))

    actionable, untouched = build_audio_plans(paths, config, cwd)

    for path, plan, _ in actionable:
        render_audio_plan(os.path.relpath(path, cwd), plan)
        click.echo()

    if untouched:
        click.secho("No audio change needed:", dim=True)
        for relative, reason in untouched:
            click.secho(f"  {relative}: {reason}", dim=True)
        click.echo()

    if not actionable:
        click.secho("No audio changes to make.", fg="green")
        return {}, True

    remuxes = [p for _, p, _ in actionable if p["action"] == "remux"]
    freed = sum(p.get("freed") or 0 for p in remuxes)
    click.echo("-" * 40)
    click.echo(
        f"{len(actionable)} file(s): {len(remuxes)} remux, "
        f"{len(actionable) - len(remuxes)} flag-only. "
        f"Frees about {human_size(freed)}."
    )

    if dry_run:
        click.secho("Dry run, nothing changed.", fg="yellow")
        return {}, False

    if not assume_yes:
        click.secho(
            "Remuxing is irreversible: dropped tracks cannot be recovered.", fg="yellow"
        )
        if not click.confirm("Proceed?", default=False):
            click.secho("Aborted, nothing changed.", fg="yellow")
            return {}, False

    click.echo("-" * 40)
    failed = run_audio_phase(actionable, cwd)
    click.echo("-" * 40)
    click.secho(
        f"Audio complete. {len(actionable) - len(failed)} changed, {len(failed)} failed.",
        fg="green" if not failed else "yellow",
    )
    return failed, True


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
@click.option("--dry-run", is_flag=True, help="Show the plan and exit without changing anything.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the confirmation prompt.")
def audio(dry_run, assume_yes):
    """Reduce audio tracks to one preferred and one secondary language."""
    cwd = os.getcwd()
    config = load_config()
    click.echo(f"Scanning {cwd} ...")
    click.echo("-" * 40)
    audio_pass(cwd, config, dry_run, assume_yes)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview changes without moving files.")
@click.option("--ask", is_flag=True, help="Ask for confirmation on unknown files.")
@click.option(
    "--partial",
    is_flag=True,
    help="Organize every file that can be identified, instead of skipping its whole folder.",
)
@click.option("--audio", "do_audio", is_flag=True, help="Clean up audio tracks first.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the audio confirmation prompt.")
def organize(dry_run, ask, partial, do_audio, assume_yes):
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
    candidates = collect_media(cwd, valid_exts)

    # Audio first: dropping tracks changes the {audio} part of the new name, so
    # the rename has to see the cleaned file, not the original.
    audio_failed = {}
    if do_audio:
        audio_failed, confirmed = audio_pass(cwd, config, dry_run, assume_yes, candidates)
        if not confirmed:
            return
        click.echo("-" * 40)

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

        # Its audio is not what the approved plan said, so its name would not be
        # either. Leave it alone entirely rather than move it under a wrong name.
        if original_path in audio_failed:
            note_skip(
                original_path,
                f"audio step failed ({audio_failed[original_path]})",
                breaks_group=False,
            )
            continue

        # Feed the whole relative path to guessit: when the filename itself is
        # cryptic, the folder it sits in often still carries the series name.
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

        # Abbreviated filenames often omit the title and year that the
        # container metadata and the folder name still carry.
        guess, notes = enrich_guess(guess, original_path, media_type)
        for note in notes:
            click.secho(f"  {relative_source}: {note}", dim=True)

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
