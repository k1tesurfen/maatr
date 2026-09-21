import os
import json
import re
import shutil
import sys
import subprocess
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import click
from guessit import guessit

STATE_FILE = ".maatr_history.json"
LOCAL_CONFIG = "maatr.toml"
GLOBAL_CONFIG_DIR = os.path.expanduser("~/.config/maatr")
GLOBAL_CONFIG_FILE = os.path.join(GLOBAL_CONFIG_DIR, "maatr.toml")
DEFAULT_LOOKUP_CACHE = "~/.cache/maatr/lookup.json"

DEFAULT_CONFIG = """
[templates.movie]
# Available variables: title, year, resolution, audio, ext
folder = "{title} ({year})"
file = "{title} ({year}) [{resolution}] [{audio}]{ext}"

[templates.episode]
# Available variables: title, season, episode, season_pad, episode_pad, resolution, audio, ext
folder = "{title}/{title} Season {season}/{title} S{season_pad}E{episode_pad}"
file = "{title} S{season_pad}E{episode_pad} [{resolution}] [{audio}]{ext}"

[naming]
# Filenames are written to exFAT/NTFS/SMB drives, where macOS' decomposed
# umlauts and the NAS' composed ones are not the same bytes. Going to plain
# ASCII sidesteps the whole problem.
ascii_only = true
colon = " - "
drop = "'’‘`,!?¿¡"

# Applied before the accent stripping, so 'Ä' becomes 'Ae' and not 'A'.
[naming.replace]
"Ä" = "Ae"
"Ö" = "Oe"
"Ü" = "Ue"
"ä" = "ae"
"ö" = "oe"
"ü" = "ue"
"ß" = "ss"
"&" = " and "

[organize]
# One folder per movie, one movie file per movie folder: anything else in a
# movie's folder is not the movie. In a folder whose files parse as a movie the
# largest one wins, provided it is clearly larger — two near-equal files are an
# ambiguity worth reporting, not a sample. Season folders are exempt, so every
# episode survives.
primary_by_size = true
primary_size_ratio = 4
extra_tokens = ["sample", "trailer", "proof"]
extra_dirs = ["sample", "samples", "trailer", "trailers", "proof",
              "extra", "extras", "featurette", "featurettes", "bonus",
              "behind the scenes"]

[lookup]
# German releases carry dubbed German titles. Look the film up on TMDB and use
# its international English title instead. A free key: themoviedb.org ->
# Settings -> API -> Developer. $TMDB_API_KEY overrides the value here.
enabled = true
provider = "tmdb"
# Either credential works: the 32-hex "API Key" or the "API Read Access
# Token" (a JWT, sent as a bearer header). $TMDB_API_KEY overrides this.
api_key = ""
language = "en-US"
timeout = 8
cache = "~/.cache/maatr/lookup.json"

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


def format_audio_tag(mapped_langs, config):
    """Orders already-mapped language codes into the {audio} part of a name.

    Shared so a predicted tag and a probed one can never disagree on ordering.
    """
    audio_cfg = config.get("audio", {})
    fallback = audio_cfg.get("default_fallback", "ENG")
    enforce_first = audio_cfg.get("enforce_first", "ENG")

    langs_list = sorted(set(mapped_langs))
    if not langs_list:
        return fallback
    if enforce_first in langs_list:
        langs_list.remove(enforce_first)
        langs_list.insert(0, enforce_first)
    return "-".join(langs_list)


def audio_tag_from_plan(plan, config):
    """The {audio} tag the file will carry once this audio plan has run.

    A dry run has not dropped anything yet, so probing the file would describe
    the tracks that are about to go and preview a name the live run never
    produces. The plan already knows exactly which tracks survive.
    """
    mapping = config.get("audio", {}).get("mapping", {})
    mapped = []
    for track in plan.get("keep", []):
        lang = track_language(track)
        if not lang or lang == "und":
            continue
        mapped.append(mapping.get(lang, lang.upper()[:3]))
    return format_audio_tag(mapped, config)


def get_audio_languages(filepath, config):
    """Probes for audio and uses the config mappings."""
    audio_cfg = config.get("audio", {})
    mapping = audio_cfg.get("mapping", {})
    fallback = audio_cfg.get("default_fallback", "ENG")

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

        return format_audio_tag(langs, config)
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


def parse_duration_tag(value):
    """'02:00:17.418000000' -> nanoseconds, or None if it is not one."""
    if not value:
        return None
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:\.(\d+))?", str(value).strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    total = (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1_000_000_000
    if fraction:
        total += int(round(float(f"0.{fraction}") * 1_000_000_000))
    return total


def track_duration_ns(track):
    return parse_duration_tag((track.get("properties") or {}).get("tag_duration"))


def longest_track_ns(tracks):
    """The duration of the longest track we can measure, or None."""
    durations = [d for d in (track_duration_ns(t) for t in tracks) if d is not None]
    return max(durations) if durations else None


def expected_duration_ns(source_info, plan):
    """How long the file should be once the dropped tracks are gone.

    A Matroska container is as long as its longest track, so dropping the
    longest one legitimately shortens the file — a German release may carry a
    Russian dub running twenty seconds past the picture. Comparing the old
    container duration with the new one calls that a corrupt remux.
    """
    keep_ids = {t["id"] for t in plan["keep"]}
    surviving = [
        track
        for track in source_info.get("tracks", [])
        if track.get("type") != "audio" or track.get("id") in keep_ids
    ]
    return longest_track_ns(surviving)


def human_duration(nanoseconds):
    if nanoseconds is None:
        return "unknown"
    seconds, fraction = divmod(int(nanoseconds), 1_000_000_000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction // 1_000_000:03d}"


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

    tolerance = 2_000_000_000  # 2s in ns

    # The video is copied through untouched, so its length is the sharpest
    # truncation check there is, and dropping audio cannot affect it.
    old_video_ns = longest_track_ns(
        [t for t in source_info.get("tracks", []) if t.get("type") == "video"]
    )
    new_video_ns = longest_track_ns(
        [t for t in info.get("tracks", []) if t.get("type") == "video"]
    )
    if old_video_ns and new_video_ns and abs(old_video_ns - new_video_ns) > tolerance:
        return (
            f"video duration changed ({human_duration(old_video_ns)} -> "
            f"{human_duration(new_video_ns)})"
        )

    old_ns = (source_info.get("container", {}).get("properties", {}) or {}).get("duration")
    new_ns = (info.get("container", {}).get("properties", {}) or {}).get("duration")
    expected_ns = expected_duration_ns(source_info, plan)
    if new_ns and expected_ns:
        # Compare against the longest *surviving* track, not the old container.
        if abs(expected_ns - new_ns) > tolerance:
            return (
                f"duration changed (expected {human_duration(expected_ns)}, "
                f"got {human_duration(new_ns)})"
            )
    elif old_ns and new_ns and abs(old_ns - new_ns) > tolerance:
        # No per-track duration tags to reason with: fall back to the old
        # comparison rather than skip the check.
        return (
            f"duration changed ({human_duration(old_ns)} -> {human_duration(new_ns)})"
        )

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


# --- The international title ---------------------------------------------
#
# German releases are named after the dubbed German title, which is often a
# different film's name entirely. TMDB knows both, so we ask it for the
# English one. Scene .nfo sidecars usually carry an IMDb id, which turns the
# lookup from a fuzzy search into an exact one.

TMDB_BASE = "https://api.themoviedb.org/3"
IMDB_ID_RE = re.compile(r"\btt\d{7,8}\b")


def tmdb_key(config):
    """The credential, environment first so it need not be written to disk."""
    from_env = os.environ.get("TMDB_API_KEY", "").strip()
    if from_env:
        return from_env
    return str((config or {}).get("lookup", {}).get("api_key", "") or "").strip()


def is_bearer_token(key):
    """TMDB hands out two credentials; tell them apart rather than 401.

    The "API Key" is 32 hex characters and goes in the query string (v3 auth).
    The "API Read Access Token" is a JWT — three dot-separated parts — and goes
    in an Authorization header. Both work against the v3 endpoints used here.
    """
    return key.count(".") == 2 and len(key) > 40


def tmdb_request(endpoint, params, config):
    """One TMDB call. Returns (payload, error); exactly one is None.

    Never raises: the caller keeps whatever title it already had.
    """
    key = tmdb_key(config)
    if not key:
        return None, "no TMDB key"
    timeout = (config or {}).get("lookup", {}).get("timeout", 8)

    headers = {}
    if is_bearer_token(key):
        headers["Authorization"] = f"Bearer {key}"
        query = urllib.parse.urlencode(params)
    else:
        query = urllib.parse.urlencode(dict(params, api_key=key))

    request = urllib.request.Request(f"{TMDB_BASE}{endpoint}?{query}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        # A rejected credential is worth saying out loud: "unreachable" would
        # send you looking at the network instead of at the key.
        if exc.code in (401, 403):
            kind = "read access token" if is_bearer_token(key) else "API key"
            return None, f"TMDB rejected the {kind} (HTTP {exc.code})"
        if exc.code == 429:
            return None, "TMDB rate limit reached (HTTP 429)"
        return None, f"TMDB returned HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        # URLError, timeouts and unparseable bodies alike.
        return None, f"TMDB unreachable ({exc.__class__.__name__})"


def imdb_id_from_sidecar(path):
    """The IMDb id from a .nfo next to the media file, if one is there.

    Scene .nfo files are CP437 ASCII art, not UTF-8, so they are read
    leniently; we only want the one id out of them.
    """
    directory = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None

    sidecars = [name for name in entries if name.lower().endswith(".nfo")]
    # The one sharing the media file's name wins; the rest are a fallback.
    sidecars.sort(key=lambda name: (os.path.splitext(name)[0] != stem, name))
    for name in sidecars:
        try:
            with open(
                os.path.join(directory, name), "r", encoding="cp437", errors="replace"
            ) as f:
                text = f.read(200_000)
        except OSError:
            continue
        match = IMDB_ID_RE.search(text)
        if match:
            return match.group(0)
    return None


def pick_tmdb_movie(results, year):
    """Chooses among search hits, or refuses to. Pure: no I/O.

    Returns (result, reason); exactly one of the two is None. Without a year to
    check against there is no way to tell the right film from a remake, so the
    answer is no answer rather than a guess.
    """
    if not results:
        return None, "no TMDB result"
    if year is None:
        return None, "no year to verify the match against"

    near = []
    for result in results:
        released = str(result.get("release_date") or "")[:4]
        if len(released) == 4 and released.isdigit():
            if abs(int(released) - int(year)) <= 1:
                near.append(result)
    if not near:
        return None, f"no TMDB result within a year of {year}"

    # Deterministic: popularity, then id, so repeat runs agree.
    near.sort(key=lambda r: (-(r.get("popularity") or 0), r.get("id") or 0))
    return near[0], None


def _tmdb_result_title(result, language):
    """The English title and year out of a TMDB movie result."""
    title = (result.get("title") or "").strip()
    if not title:
        return None, None
    released = str(result.get("release_date") or "")[:4]
    year = int(released) if len(released) == 4 and released.isdigit() else None
    return title, year


def lookup_title(guess, path, config, cache=None):
    """Asks TMDB for the international title of this film.

    Returns (title, year, source, reason). On any failure the title is None and
    the reason says why, so the caller can keep the name it already had and
    report it. Nothing here ever invents a title.
    """
    lookup_cfg = (config or {}).get("lookup", {}) or {}
    if not lookup_cfg.get("enabled", True):
        return None, None, None, None
    if not tmdb_key(config):
        return None, None, None, "no TMDB key (set $TMDB_API_KEY or [lookup].api_key)"

    language = lookup_cfg.get("language", "en-US")
    cache = cache if cache is not None else {}
    imdb_id = imdb_id_from_sidecar(path)
    local_title = guess.get("title")
    year = guess.get("year")

    if imdb_id:
        cache_key = f"tt:{imdb_id}|{language}"
        source = f"TMDB via {imdb_id}"
    elif local_title:
        cache_key = f"q:{str(local_title).casefold()}|{year or ''}|{language}"
        source = "TMDB title search"
    else:
        return None, None, None, "no title to look up"

    cached = cache.get(cache_key)
    if cached is not None:
        if cached.get("title"):
            return cached["title"], cached.get("year"), f"{source}, cached", None
        return None, None, None, f"{cached.get('reason', 'no match')} (cached)"

    def remember(title, found_year, reason):
        cache[cache_key] = {"title": title, "year": found_year, "reason": reason}

    if imdb_id:
        payload, error = tmdb_request(
            f"/find/{imdb_id}",
            {"external_source": "imdb_id", "language": language},
            config,
        )
        if payload is None:
            # A refusal or an outage is not a verdict about the film, so it is
            # not cached: the next run asks again.
            return None, None, None, f"{error} for {imdb_id}"
        results = payload.get("movie_results") or []
        if not results:
            remember(None, None, f"{imdb_id} is not a film on TMDB")
            return None, None, None, f"{imdb_id} is not a film on TMDB"
        title, found_year = _tmdb_result_title(results[0], language)
        if not title:
            remember(None, None, f"{imdb_id} has no English title")
            return None, None, None, f"{imdb_id} has no English title"
        remember(title, found_year, None)
        return title, found_year, source, None

    params = {"query": str(local_title), "language": language}
    if year:
        params["year"] = year
    payload, error = tmdb_request("/search/movie", params, config)
    if payload is None:
        return None, None, None, f"{error} for {local_title!r}"

    result, reason = pick_tmdb_movie(payload.get("results") or [], year)
    if result is None:
        remember(None, None, reason)
        return None, None, None, reason
    title, found_year = _tmdb_result_title(result, language)
    if not title:
        remember(None, None, "TMDB match has no English title")
        return None, None, None, "TMDB match has no English title"
    remember(title, found_year, None)
    return title, found_year, source, None


def lookup_cache_path(config):
    setting = (config or {}).get("lookup", {}).get("cache", DEFAULT_LOOKUP_CACHE)
    return os.path.expanduser(setting or DEFAULT_LOOKUP_CACHE)


def load_lookup_cache(config):
    try:
        with open(lookup_cache_path(config), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_lookup_cache(config, cache):
    """Best effort: an unwritable cache must never fail a run."""
    path = lookup_cache_path(config)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError:
        pass


def enrich_guess(guess, path, media_type, config=None, lookup=False, cache=None):
    """Adds title and year from the MKV metadata, the folder name and TMDB.

    The filename stays in charge of everything else (resolution, episode
    numbers); this only fills in what abbreviated filenames tend to omit.

    Notes come back as (text, colour) pairs so the caller can print a failed
    lookup differently from a successful one.
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
                notes.append((f"title from MKV metadata: {inner_title!r}", None))
            guess["title"] = inner_title
            if inner_year:
                guess["year"] = inner_year

    if not guess.get("year"):
        year, problem = year_from_folders(path)
        if year:
            guess["year"] = year
            notes.append((f"year {year} from folder name", None))
        elif problem:
            notes.append((problem, None))

    # The German title is the dubbed one; TMDB knows what the film is really
    # called. Series are left alone. A failure here costs nothing: the title
    # parsed from the filename simply stands.
    if lookup and media_type == "movie":
        title, year, source, reason = lookup_title(guess, path, config, cache)
        if title:
            previous = guess.get("title")
            if str(title) != str(previous):
                notes.append((f"title from {source}: {previous!r} -> {title!r}", None))
            guess["title"] = title
            # TMDB's casing is the real one; do not re-case it later.
            guess["_title_verbatim"] = True
            if year:
                guess["year"] = year
        elif reason:
            notes.append((f"keeping {guess.get('title')!r}: {reason}", "yellow"))

    return guess, notes


# --- Names that survive the trip to the NAS ------------------------------
#
# The library lives on an external drive and is read over SMB. macOS hands out
# decomposed umlauts (NFD), the NAS expects composed ones (NFC), and the two do
# not compare equal, so the same file appears under two names or under none.
# Transliterating to ASCII removes the question entirely.

# Applied before the accent stripping below: 'Ä' has to become 'Ae', not 'A'.
DEFAULT_REPLACE = {
    "Ä": "Ae",
    "Ö": "Oe",
    "Ü": "Ue",
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Æ": "Ae",
    "æ": "ae",
    "Ø": "O",
    "ø": "o",
    "Å": "Aa",
    "å": "aa",
    "Þ": "Th",
    "þ": "th",
    "Ð": "D",
    "ð": "d",
    "&": " and ",
    "·": "-",
    "…": "...",
    "–": "-",
    "—": "-",
    "‐": "-",
    " ": " ",
}
# Legal in a filename, but noise in a title and awkward in a URL.
DEFAULT_DROP = "'’‘`,!?¿¡"

# Mutable so an optional [naming] section can extend it; apply_naming_config()
# fills it in, and the defaults here stand on their own when no config says
# otherwise (load_config does not merge, so every setting needs a Python-side
# default).
NAMING = {
    "ascii_only": True,
    "colon": " - ",
    "replace": dict(DEFAULT_REPLACE),
    "drop": DEFAULT_DROP,
}

# exFAT and most SMB servers cap a single name at 255 units.
MAX_COMPONENT_BYTES = 255


def apply_naming_config(config):
    """Folds an optional [naming] section into the module-wide naming rules."""
    naming = (config or {}).get("naming", {}) or {}
    NAMING["ascii_only"] = naming.get("ascii_only", True)
    NAMING["colon"] = naming.get("colon", " - ")
    NAMING["drop"] = naming.get("drop", DEFAULT_DROP)
    # Extend rather than replace: a user adding one mapping should not lose ß.
    NAMING["replace"] = dict(DEFAULT_REPLACE, **(naming.get("replace", {}) or {}))


def to_ascii(text):
    """Transliterates to plain ASCII, German first, then accents in general."""
    for source, target in NAMING["replace"].items():
        text = text.replace(source, target)
    if not NAMING["ascii_only"]:
        return text
    # Split the remaining accented letters into base + combining mark, drop the
    # marks, then discard whatever still has no ASCII form at all.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "ignore").decode("ascii")


def sanitize_component(value):
    """Strips anything from a template variable that could escape the target dir.

    Template variables come from filenames we did not write, so a title like
    "../../etc" or "Foo/Bar" must never be able to steer the move. On top of
    that the result has to be spellable on every filesystem the library is read
    from, which means plain ASCII and no punctuation that needs quoting.
    """
    # Compose first: a macOS-supplied "A + combining diaeresis" has to look
    # like 'Ä' before the transliteration table can recognise it.
    text = unicodedata.normalize("NFC", str(value))
    text = to_ascii(text)

    text = text.replace(os.sep, " ")
    if os.altsep:
        text = text.replace(os.altsep, " ")
    # os.altsep is None on POSIX, so a backslash would survive here and turn
    # into a separator the moment the drive is read from Windows.
    text = text.replace("\\", " ")

    # ':' is legal on APFS but not on exFAT/NTFS/SMB, where media drives
    # usually live. " - " is the usual media-server convention, and it is
    # applied to a bare ':' too so none can ever reach a filename.
    text = text.replace(":", NAMING["colon"])
    text = re.sub(r'[*?"<>|]', "", text)
    if NAMING["drop"]:
        text = text.translate({ord(c): None for c in NAMING["drop"]})
    # Whitespace controls stand in for a space, or "Tab\there" becomes one
    # word; the remaining C0 controls, DEL and the C1 block just go.
    text = re.sub(r"[\t\n\r\v\f]", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)

    # Collapse what the replacements above left behind: " - - " from a title
    # that already contained a dash, and runs of spaces from "&" -> " and ".
    text = " ".join(text.split())
    text = re.sub(r"(?:\s-){2,}(?=\s)", " -", text)
    text = text.strip(" .-")
    return " ".join(text.split())


def cap_component(name, limit=MAX_COMPONENT_BYTES):
    """Trims one path component to the filesystem's name limit, extension kept."""
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, ext = os.path.splitext(name)
    room = limit - len(ext.encode("utf-8"))
    if room <= 0:
        return name[:limit]
    while len(stem.encode("utf-8")) > room:
        # Prefer cutting at a word boundary; fall back to a hard cut.
        cut = stem.rfind(" ")
        stem = stem[:cut] if cut > 0 else stem[: max(1, room)]
    return stem.strip(" .-") + ext


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

    # The template's own separators are intentional, so each level is capped on
    # its own. A long English title plus the resolution and audio tags gets
    # close to NAME_MAX on SMB.
    return "/".join(cap_component(part) for part in result.split("/"))


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


def process_media(guess, filepath, config, media_type, overrides=None, audio_tag=None):
    """Extracts variables and generates the final relative path based on config.

    `audio_tag` overrides the probe, for a preview of a file whose audio has
    been planned but not yet rewritten.
    """
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

    # A title from TMDB (or typed in by the user) is already correctly cased;
    # .title() would turn "WALL-E" into "Wall-E" and "McDonald" into "Mcdonald".
    verbatim = bool(guess.get("_title_verbatim")) or "title" in overrides

    year = field("year")
    resolution = field("screen_size")

    data = {
        "title": str(title) if verbatim else str(title).title(),
        "year": year if year is not None else "",
        "season": season if season is not None else "",
        "episode": episode if episode is not None else "",
        "season_pad": str(season).zfill(2) if season is not None else "",
        "episode_pad": str(episode).zfill(2) if episode is not None else "",
        "resolution": resolution if resolution is not None else "",
        "audio": audio_tag or get_audio_languages(filepath, config),
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


# --- Reading a hand-typed name back into fields --------------------------
#
# The review list shows each file under the name it would get, and lets that
# line be edited. What comes back has to become fields again, so the folder is
# re-rendered from the corrected title instead of drifting away from it.

# The [ENG-GER] part of a name. guessit does not know this tag is ours, so it
# is lifted out before the line is parsed and put back verbatim. Only a bracket
# whose parts are all language codes counts: "[x264]" and "[DTS]" are not ours.
AUDIO_TAG_RE = re.compile(r"\[((?:[A-Za-z]{2,4})(?:[-+][A-Za-z]{2,4})*)\]")
AUDIO_TAG_CODES = {code.upper() for code in LANG_ALIASES} | {
    code.upper() for code in LANG_ALIASES.values()
} | {"UND"}
TRAILING_YEAR_RE = re.compile(r"[\s(\[]*(\d{4})[)\]\s]*$")


def parse_name_edit(text, media_type=None, audio_codes=None):
    """Turns an edited filename back into naming fields.

    Pure, so the rule is testable without files. Returns (fields, warnings):
    `fields` uses guessit's own keys, plus "audio" for our tag and
    "_media_type" for what the line looks like. An empty title is left out
    entirely -- the caller refuses the edit rather than inventing one.
    """
    warnings = []
    line = str(text).strip()
    stem, ext = os.path.splitext(line)
    if ext.lower() not in (".mkv", ".mp4", ".avi"):
        # A name typed without its extension is fine; keep the whole thing.
        stem, ext = line, ""

    audio = None
    known = {str(code).upper() for code in (audio_codes or AUDIO_TAG_CODES)}
    for match in AUDIO_TAG_RE.finditer(stem):
        parts = re.split(r"[-+]", match.group(1))
        if all(part.upper() in known for part in parts):
            audio = "-".join(part.upper() for part in parts)
            stem = stem.replace(match.group(0), " ")
            break

    guess = guessit(stem)
    fields = {
        "title": (str(guess["title"]).strip() if guess.get("title") else None),
        "year": guess.get("year"),
        "season": guess.get("season"),
        "episode": guess.get("episode"),
        "screen_size": guess.get("screen_size"),
        "audio": audio,
    }
    for key in ("season", "episode"):
        if isinstance(fields[key], list) and fields[key]:
            fields[key] = fields[key][0]

    fields["_media_type"] = (
        "episode"
        if fields["season"] is not None and fields["episode"] is not None
        else "movie"
    )

    if not fields["title"]:
        warnings.append("no title in that name")
        fields.pop("title")
        return fields, warnings

    if fields["year"] is None:
        warnings.append("no year in that name")
    if fields["screen_size"] is None:
        warnings.append("no resolution in that name")
    if media_type == "episode" and fields["_media_type"] != "episode":
        warnings.append("no season/episode in that name")

    return fields, warnings


def parse_series_edit(text):
    """Reads 'Example Series (2019)' into (title, year). Pure."""
    line = str(text).strip()
    year = None
    match = TRAILING_YEAR_RE.search(line)
    if match and MIN_YEAR <= int(match.group(1)) <= MAX_YEAR:
        year = int(match.group(1))
        line = line[: match.start()].strip()
    line = line.strip(" -")
    return (line or None), year


def unify_series_titles(entries, folder_title=None):
    """One series title per folder. Pure.

    `entries` is [(path, title, media_type)] for one group. Some releases put
    the EPISODE name in the filename, so each file parses as a series of its
    own and the season scatters across folders. Within a folder the episodes
    must agree: the title most of them carry wins, a tie is broken by the
    folder's own name, and a group with no majority and no usable folder title
    is left exactly as it was -- reported, never guessed.

    Returns (chosen_title, {path: previous title}) for the files it overrode.
    """
    episodes = [(path, title) for path, title, kind in entries if kind == "episode"]
    if len(episodes) < 2:
        return None, {}

    counts = {}
    for _, title in episodes:
        if title:
            counts[str(title)] = counts.get(str(title), 0) + 1
    if not counts:
        return None, {}

    best = max(counts.values())
    leaders = sorted(title for title, count in counts.items() if count == best)
    if len(leaders) == 1:
        chosen = leaders[0]
    elif folder_title and str(folder_title) in leaders:
        chosen = str(folder_title)
    elif folder_title:
        chosen = str(folder_title)
    else:
        # No majority and nothing to break the tie with: changing anything here
        # would be a guess at which of them is the series.
        return None, {}

    overridden = {
        path: str(title) for path, title in episodes if str(title) != chosen
    }
    return chosen, overridden


# --- Telling the film from the things shipped next to it -----------------
#
# A release folder often carries a small preview beside the feature. Both parse
# to the same title and year, so both want the same name, and the collision
# used to fail the whole folder. Two rules sort it out, in this order.

DEFAULT_EXTRA_TOKENS = ["sample", "trailer", "proof"]
DEFAULT_EXTRA_DIRS = [
    "sample",
    "samples",
    "trailer",
    "trailers",
    "proof",
    "extra",
    "extras",
    "featurette",
    "featurettes",
    "bonus",
    "behind the scenes",
]
DEFAULT_PRIMARY_RATIO = 4.0


def is_extra_media(relative_path, config=None):
    """(True, reason) for a sample/trailer/proof file, else (False, None).

    Pure: this reads the path, never the disk. The token has to sit at the END
    of the filename stem, or be a directory name, so a real film called
    "Sample People (2000)" is never mistaken for a preview.
    """
    organize_cfg = (config or {}).get("organize", {}) or {}
    tokens = [str(t).lower() for t in organize_cfg.get("extra_tokens", DEFAULT_EXTRA_TOKENS)]
    directories = [str(d).lower() for d in organize_cfg.get("extra_dirs", DEFAULT_EXTRA_DIRS)]

    parts = str(relative_path).replace("\\", "/").split("/")
    stem = os.path.splitext(parts[-1])[0].lower()

    for folder in parts[:-1]:
        if folder.strip().lower() in directories:
            return True, f"in a '{folder}' folder, not the feature"

    for token in tokens:
        # Either the whole name, or the tail after a '.', '-' or '_'.
        if stem == token or re.search(rf"[.\-_ ]{re.escape(token)}$", stem):
            return True, f"{token} file, not the feature"

    return False, None


def pick_primary_movie(entries, size_of, ratio=DEFAULT_PRIMARY_RATIO):
    """Which of a folder's movie files is the film. Pure: size_of is injected.

    `entries` is a list of (path, media_type). Returns (winner, losers) where
    losers is a list of (path, reason). Returns (None, []) — do nothing — when
    the folder is not unambiguously one movie plus leftovers:

    * a single entry needs no choosing;
    * a group holding any episode is a season, and every episode must survive;
    * a winner that is not clearly larger than the runner-up is not a film
      beside its sample, it is two files worth reporting.
    """
    if len(entries) < 2:
        return None, []
    if any(media_type != "movie" for _, media_type in entries):
        return None, []

    # Size first, then path: repeat runs must agree.
    ranked = sorted(entries, key=lambda e: (-size_of(e[0]), e[0]))
    winner = ranked[0][0]
    runner_up = ranked[1][0]

    winner_size = size_of(winner)
    runner_up_size = size_of(runner_up)
    if runner_up_size <= 0 or winner_size < runner_up_size * ratio:
        return None, []

    losers = [
        (
            path,
            f"not the movie in this folder ({human_size(size_of(path))} "
            f"beside {human_size(winner_size)})",
        )
        for path, _ in ranked[1:]
    ]
    return winner, losers


# --- The review list -----------------------------------------------------
#
# Every name is shown, numbered, before anything is written. A number picks a
# line to correct by hand; the corrected line is read back into fields and the
# whole target -- folder included -- is rendered again, so a hand-typed title
# can never leave the folder saying something else. A season is one entry: the
# series title belongs to the folder, not to any one episode.

MAX_GROUP_PREVIEW = 4


def recompute_entry(entry, config):
    """Re-renders one file's target from its (possibly edited) fields."""
    entry["relative_new"] = process_media(
        entry["guess"],
        entry["path"],
        config,
        entry["media_type"],
        entry["overrides"],
        entry["audio_tag"],
    )
    entry["target"] = os.path.normpath(os.path.join(entry["cwd"], entry["relative_new"]))
    return entry


def entry_field(entry, name):
    """The value in force for a field: the user's edit wins over the guess."""
    if name in entry["overrides"]:
        return entry["overrides"][name]
    return entry["guess"].get(name)


def target_conflict(entries, claimed, cwd):
    """Why these targets cannot be used, or None. Checked before an edit sticks."""
    own = {e["path"] for e in entries}
    seen = {}
    for entry in entries:
        target = entry.get("target")
        if not target:
            continue
        if os.path.commonpath([cwd, target]) != cwd:
            return f"{entry['relative_new']} would land outside this directory"
        if target in seen:
            return f"two files would both become {os.path.relpath(target, cwd)}"
        seen[target] = entry["path"]
        owner = claimed.get(target)
        if owner and owner not in own:
            return (
                f"{os.path.relpath(target, cwd)} is already taken by "
                f"{os.path.relpath(owner, cwd)}"
            )
        if os.path.exists(target) and target not in own:
            return f"{os.path.relpath(target, cwd)} already exists"
    return None


def claim_targets(entries, claimed):
    """Records these targets, dropping whatever these files claimed before."""
    own = {e["path"] for e in entries}
    for target in [t for t, path in claimed.items() if path in own]:
        del claimed[target]
    for entry in entries:
        if entry.get("target"):
            claimed[entry["target"]] = entry["path"]


def build_review(plan_entries, unknown_entries):
    """The numbered model: one item per movie, per season, per unknown file."""
    items = []
    groups = {}
    for entry in plan_entries:
        if entry["media_type"] == "episode":
            item = groups.get(entry["key"])
            if item is None:
                item = {"kind": "group", "key": entry["key"], "entries": []}
                groups[entry["key"]] = item
                items.append(item)
            item["entries"].append(entry)
        else:
            items.append({"kind": "movie", "key": entry["key"], "entries": [entry]})
    for entry in unknown_entries:
        items.append({"kind": "unknown", "key": entry["key"], "entries": [entry]})

    # Numbers are handed out once and never move: an edit must not renumber the
    # line the user is about to pick next.
    for number, item in enumerate(items, 1):
        item["num"] = number
    return items


def item_line(item):
    """The text put in front of the cursor when this item is edited."""
    entry = item["entries"][0]
    if item["kind"] == "group":
        title = entry_field(entry, "title") or ""
        year = entry_field(entry, "year")
        return f"{title} ({year})" if year else str(title)
    if entry.get("relative_new"):
        return os.path.basename(entry["relative_new"])
    return os.path.basename(entry["path"])


def render_review(items, cwd):
    """Prints the numbered plan."""
    click.secho("Planned names:", bold=True)
    for item in items:
        number = f"[{item['num']}]"
        edited = "  (edited)" if item.get("edited") else ""
        entries = item["entries"]

        if item["kind"] == "group":
            title = item_line(item)
            click.secho(
                f"{number} {title}  ({len(entries)} episode(s)){edited}", bold=True
            )
            for entry in entries[:MAX_GROUP_PREVIEW]:
                click.secho(f"      {entry['relative_new']}", fg="green")
            if len(entries) > MAX_GROUP_PREVIEW:
                click.secho(
                    f"      ... {len(entries) - MAX_GROUP_PREVIEW} more", dim=True
                )
        elif item["kind"] == "unknown" and not entries[0].get("relative_new"):
            entry = entries[0]
            click.secho(
                f"{number} ???  {os.path.relpath(entry['path'], cwd)}", fg="yellow"
            )
            click.secho(
                f"      {entry['reason']} (pick {item['num']} to name it)", fg="yellow"
            )
        else:
            entry = entries[0]
            click.secho(f"{number} {os.path.relpath(entry['path'], cwd)}", dim=True)
            click.secho(f"      -> {entry['relative_new']}{edited}", fg="green")

        for note in item.get("notes", []):
            click.secho(f"      {note}", fg="yellow")

    movies = sum(1 for i in items if i["kind"] == "movie")
    seasons = sum(1 for i in items if i["kind"] == "group")
    unknown = sum(
        1 for i in items if i["kind"] == "unknown" and not i["entries"][0].get("relative_new")
    )
    click.echo("-" * 40)
    click.echo(
        f"{len(items)} item(s): {movies} movie(s), {seasons} season(s), "
        f"{unknown} unidentified."
    )


def editable_readline():
    """A readline that can put text in front of the cursor, or None.

    macOS ships libedit behind the stdlib `readline`, and libedit ignores the
    startup hook: the name would come up as an empty prompt and have to be
    retyped in full. gnureadline is the real thing, so it is preferred, and
    everything falls back to a plain prompt with a default.
    """
    for name in ("gnureadline", "readline"):
        try:
            module = __import__(name)
        except ImportError:
            continue
        if getattr(module, "backend", "readline") == "editline":
            continue
        if hasattr(module, "set_startup_hook") and hasattr(module, "insert_text"):
            return module
    return None


def prompt_line(label, current):
    """Asks for a line with `current` already typed in, ready to be edited."""
    readline = editable_readline()

    if readline is not None and sys.stdin.isatty():
        def hook():
            readline.insert_text(current)
            readline.redisplay()

        readline.set_startup_hook(hook)
        try:
            return input(label)
        except EOFError:
            return ""
        finally:
            readline.set_startup_hook()

    # No line editing available: show the name and let Enter keep it.
    click.secho(f"  current: {current}", dim=True)
    return click.prompt(label.rstrip(), default=current, show_default=False)


def edit_item(item, claimed, config, cwd):
    """Renames one item in place. Leaves it untouched if the edit cannot stand."""
    entries = item["entries"]
    is_group = item["kind"] == "group"
    label = f"Series name [{item['num']}]: " if is_group else f"New name [{item['num']}]: "

    answer = prompt_line(label, item_line(item)).strip()
    if not answer:
        click.secho("  unchanged.", dim=True)
        return

    if is_group:
        title, year = parse_series_edit(answer)
        if not title:
            click.secho("  !! a series needs a title; nothing changed.", fg="red")
            return
        updates = {"title": title}
        if year is not None:
            updates["year"] = year
        warnings = []
    else:
        entry = entries[0]
        fields, warnings = parse_name_edit(
            answer, entry.get("media_type"), audio_codes_for(config)
        )
        if "title" not in fields:
            click.secho("  !! a name needs a title; nothing changed.", fg="red")
            return
        updates = {
            key: fields[key]
            for key in ("title", "year", "season", "episode", "screen_size")
        }

    # Snapshot, so a rejected edit leaves the plan exactly as it was.
    before = [
        (
            dict(e["overrides"]),
            e["audio_tag"],
            e["media_type"],
            e.get("relative_new"),
            e.get("target"),
        )
        for e in entries
    ]

    for entry in entries:
        entry["overrides"].update(updates)
        if not is_group:
            if fields.get("audio"):
                entry["audio_tag"] = fields["audio"]
            if item["kind"] == "unknown" or fields["_media_type"] == "episode":
                entry["media_type"] = fields["_media_type"]
        recompute_entry(entry, config)

    missing = missing_fields(
        entries[0]["guess"], entries[0]["media_type"], entries[0]["overrides"]
    )
    problem = None
    if missing:
        problem = f"still missing {', '.join(missing)}"
    else:
        problem = target_conflict(entries, claimed, cwd)

    if problem:
        click.secho(f"  !! {problem}; nothing changed.", fg="red")
        for entry, state in zip(entries, before):
            (
                entry["overrides"],
                entry["audio_tag"],
                entry["media_type"],
                entry["relative_new"],
                entry["target"],
            ) = (dict(state[0]), state[1], state[2], state[3], state[4])
        return

    claim_targets(entries, claimed)
    item["edited"] = True
    # The notes describe what Maatr worked out; the user has just overruled it.
    item["notes"] = []
    for warning in warnings:
        click.secho(f"  note: {warning}", fg="yellow")


def audio_codes_for(config):
    """Every tag the {audio} part of a name can carry, for reading one back."""
    mapping = (config.get("audio", {}) or {}).get("mapping", {}) or {}
    codes = set(AUDIO_TAG_CODES)
    codes.update(str(value).upper() for value in mapping.values())
    fallback = (config.get("audio", {}) or {}).get("default_fallback")
    if fallback:
        codes.add(str(fallback).upper())
    return codes


def review_names(items, claimed, config, cwd):
    """Shows the plan and lets it be corrected. True to apply, False to abort."""
    while True:
        click.echo("-" * 40)
        render_review(items, cwd)
        answer = click.prompt(
            "Apply these names? [y/N/number]", default="n", show_default=False
        ).strip().lower()

        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        if answer.isdigit():
            chosen = next((i for i in items if i["num"] == int(answer)), None)
            if chosen is None:
                click.secho(f"  !! no item {answer}.", fg="yellow")
                continue
            edit_item(chosen, claimed, config, cwd)
            continue
        click.secho("  !! enter y, n, or an item number.", fg="yellow")


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


def audio_review(cwd, config, dry_run, assume_yes, paths=None):
    """Plan, show, confirm -- but execute nothing.

    Kept separate from the execution so `organize` can get both halves of a run
    approved before it writes anything: the rename list is built from the
    predicted tags, and declining it costs no remux.

    Returns (actionable, predicted tags, confirmed). `confirmed` is False only
    when the user declined; a dry run confirms, because it has a rename plan
    left to show and nothing was declined.
    """
    own_extras = []
    if paths is None:
        # Run on its own, so nothing has filtered the samples out yet.
        paths = []
        for path in collect_media(cwd, (".mkv", ".mp4", ".avi")):
            is_extra, reason = is_extra_media(os.path.relpath(path, cwd), config)
            if is_extra:
                own_extras.append((os.path.relpath(path, cwd), reason))
            else:
                paths.append(path)

    actionable, untouched = build_audio_plans(paths, config, cwd)
    untouched = own_extras + untouched

    for path, plan, _ in actionable:
        render_audio_plan(os.path.relpath(path, cwd), plan)
        click.echo()

    if untouched:
        click.secho("No audio change needed:", dim=True)
        for relative, reason in untouched:
            click.secho(f"  {relative}: {reason}", dim=True)
        click.echo()

    # What the {audio} part of the new name will say once these plans have run.
    # Probing the file would describe the tracks we are about to drop, so a
    # dry-run preview would show a name the live run never produces.
    predicted = {path: audio_tag_from_plan(plan, config) for path, plan, _ in actionable}

    if not actionable:
        click.secho("No audio changes to make.", fg="green")
        return [], {}, True

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
        # Nothing was executed, but nothing was declined either: `organize
        # --audio --dry-run` still has a rename plan to show, and it is the
        # only way to preview both halves of the run together.
        return actionable, predicted, True

    if not assume_yes:
        click.secho(
            "Remuxing is irreversible: dropped tracks cannot be recovered.", fg="yellow"
        )
        if not click.confirm("Proceed?", default=False):
            click.secho("Aborted, nothing changed.", fg="yellow")
            return [], {}, False

    return actionable, predicted, True


def apply_audio(actionable, cwd):
    """Runs the approved audio plans. Returns the files that failed."""
    click.echo("-" * 40)
    failed = run_audio_phase(actionable, cwd)
    click.echo("-" * 40)
    click.secho(
        f"Audio complete. {len(actionable) - len(failed)} changed, {len(failed)} failed.",
        fg="green" if not failed else "yellow",
    )
    return failed


def audio_pass(cwd, config, dry_run, assume_yes, paths=None):
    """Plan, show, confirm, execute. The standalone `audio` command."""
    actionable, _, confirmed = audio_review(cwd, config, dry_run, assume_yes, paths)
    if not confirmed or dry_run or not actionable:
        return {}
    return apply_audio(actionable, cwd)


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
@click.option(
    "--partial",
    is_flag=True,
    help="Organize every file that can be identified, instead of skipping its whole folder.",
)
@click.option("--audio", "do_audio", is_flag=True, help="Clean up audio tracks first.")
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip both confirmation prompts and apply the plan as it stands.",
)
@click.option(
    "--no-lookup",
    "no_lookup",
    is_flag=True,
    help="Do not ask TMDB for the international title; use the name as parsed.",
)
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Ignore the stored TMDB lookups."
)
def organize(dry_run, partial, do_audio, assume_yes, no_lookup, no_cache):
    """Organize media files in the current directory."""
    cwd = os.getcwd()
    config = load_config()
    apply_naming_config(config)
    history = load_state()
    valid_exts = (".mkv", ".mp4", ".avi")

    lookup = not no_lookup and config.get("lookup", {}).get("enabled", True)
    lookup_cache = {} if no_cache else load_lookup_cache(config)
    cache_before = len(lookup_cache)

    click.echo(f"Running Maatr in {'DRY-RUN mode' if dry_run else 'LIVE mode'}...")
    click.echo("-" * 40)

    # Snapshot the file list before touching anything: moving files into
    # subdirectories of a tree we are still walking would make os.walk hand us
    # our own output again.
    candidates = collect_media(cwd, valid_exts)

    # Samples and trailers are not worth a remux or a lookup. They are found by
    # name here; the structural rule (one movie file per movie folder) runs
    # later, once every target name is known.
    extras = {}
    for path in candidates:
        is_extra, reason = is_extra_media(os.path.relpath(path, cwd), config)
        if is_extra:
            extras[path] = reason

    # A folder holding nothing but extras has no feature to organize. Naming a
    # 64MB preview after the film would put a stub in the library.
    by_group = {}
    for path in candidates:
        by_group.setdefault(group_for(path, cwd)[0], []).append(path)
    for group_paths in by_group.values():
        if all(path in extras for path in group_paths):
            for path in group_paths:
                extras[path] = "only sample/extra files in this folder"

    feature_candidates = [path for path in candidates if path not in extras]

    # Audio is planned and approved first, but not executed: the whole run --
    # remux and rename together -- is approved before a single byte is written,
    # so declining the names costs no tracks. The {audio} part of every new
    # name therefore comes from the approved plan, not from probing a file that
    # still carries the tracks that plan drops.
    audio_actionable = []
    predicted_audio = {}
    if do_audio:
        audio_actionable, predicted_audio, confirmed = audio_review(
            cwd, config, dry_run, assume_yes, feature_candidates
        )
        if not confirmed:
            return
        click.echo("-" * 40)

    resolved = {}  # group key -> entries, before names are claimed
    unknown = []  # entries the review list can still rescue by name
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
        relative_source = os.path.relpath(original_path, cwd)
        key, label = group_for(original_path, cwd)
        labels.setdefault(key, label)

        # Named extras go before anything expensive: a sample should not cost a
        # TMDB lookup, and it must not condemn its folder.
        if original_path in extras:
            note_skip(original_path, extras[original_path], breaks_group=False)
            continue

        # Feed the whole relative path to guessit: when the filename itself is
        # cryptic, the folder it sits in often still carries the series name.
        guess = guessit(relative_source)
        media_type = guess.get("type")

        entry = {
            "path": original_path,
            "cwd": cwd,
            "key": key,
            "guess": guess,
            "media_type": media_type,
            "overrides": {},
            "audio_tag": predicted_audio.get(original_path),
            "relative_new": None,
            "target": None,
        }

        if media_type not in ("movie", "episode"):
            # Not skipped yet: the review list offers it for naming, and only a
            # file left unnamed there counts as one we could not identify.
            entry["reason"] = "could not tell movie from episode"
            unknown.append(entry)
            continue

        # Abbreviated filenames often omit the title and year that the
        # container metadata and the folder name still carry.
        guess, notes = enrich_guess(
            guess, original_path, media_type, config, lookup, lookup_cache
        )
        entry["guess"] = guess
        for note, colour in notes:
            click.secho(
                f"  {relative_source}: {note}", fg=colour, dim=colour is None
            )

        missing = missing_fields(guess, media_type)
        if missing:
            entry["reason"] = f"unknown {', '.join(missing)}"
            unknown.append(entry)
            continue

        recompute_entry(entry, config)

        # A sanitized title can't escape cwd, but verify rather than trust.
        if os.path.commonpath([cwd, entry["target"]]) != cwd:
            note_skip(original_path, "target would land outside this directory")
            continue

        if entry["target"] == original_path:
            # Nothing to do and nothing wrong: a second run over a tidy folder.
            note_skip(original_path, "already in place", breaks_group=False)
            continue

        resolved.setdefault(key, []).append(entry)

    # Pass B: the group-wide rules. Both need every target in a group before
    # they can choose, and neither may depend on the order files were walked in
    # -- a sample in a 'Sample/' subfolder sorts before the feature.
    organize_cfg = config.get("organize", {}) or {}
    if organize_cfg.get("primary_by_size", True):
        ratio = float(organize_cfg.get("primary_size_ratio", DEFAULT_PRIMARY_RATIO))

        def size_of(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0

        for key, entries in list(resolved.items()):
            winner, losers = pick_primary_movie(
                [(e["path"], e["media_type"]) for e in entries], size_of, ratio
            )
            if not losers:
                continue
            dropped = {path for path, _ in losers}
            for path, reason in losers:
                note_skip(path, reason, breaks_group=False)
            resolved[key] = [e for e in entries if e["path"] not in dropped]

    # A release that puts the EPISODE name in each filename parses as a series
    # per file, which scatters one season across a dozen folders. Inside a
    # folder the episodes have to agree on the series.
    series_notes = {}
    for key, entries in resolved.items():
        folder_title = None
        if key[0] == "dir":
            folder_title = guessit(key[1]).get("title")
        chosen, overridden = unify_series_titles(
            [(e["path"], entry_field(e, "title"), e["media_type"]) for e in entries],
            folder_title,
        )
        if not overridden:
            continue
        for entry in entries:
            if entry["path"] in overridden:
                entry["overrides"]["title"] = chosen
                recompute_entry(entry, config)
        series_notes[key] = (
            f"series title unified to {chosen!r} "
            f"({len(overridden)} file(s) parsed as something else)"
        )

    # Pass C: claim the target names. Anything colliding this late is a real
    # ambiguity, so it still fails its folder.
    plan_entries = []
    for key, entries in resolved.items():
        for entry in entries:
            problem = target_conflict([entry], claimed, cwd)
            if problem:
                note_skip(entry["path"], problem)
                continue
            claim_targets([entry], claimed)
            plan_entries.append(entry)

    # Planning is over, so every lookup that was going to happen has happened.
    # Saving now means a --dry-run warms the cache for the live run.
    if lookup and not no_cache and len(lookup_cache) != cache_before:
        save_lookup_cache(config, lookup_cache)

    # The review: every name, numbered, correctable by hand before anything is
    # written. A dry run shows the same list without the prompt.
    items = build_review(plan_entries, unknown)
    for item in items:
        note = series_notes.get(item["key"])
        if note:
            item.setdefault("notes", []).append(note)

    if items:
        if dry_run or assume_yes:
            click.echo("-" * 40)
            render_review(items, cwd)
        elif not review_names(items, claimed, config, cwd):
            click.secho("Aborted, nothing changed.", fg="yellow")
            return
    elif not skipped:
        click.secho("Nothing to organize.", fg="green")
        return

    # Whatever the review did not rescue is a file we could not identify.
    plan_entries = [e for e in plan_entries if e.get("target")]
    for entry in unknown:
        if entry.get("target"):
            plan_entries.append(entry)
        else:
            note_skip(entry["path"], entry["reason"])

    # Approved. Audio goes first: dropping tracks is what the new name says.
    audio_failed = {}
    if do_audio and audio_actionable and not dry_run:
        audio_failed = apply_audio(audio_actionable, cwd)

    # Its audio is not what the approved plan said, so its name would not be
    # either. Leave it alone entirely rather than move it under a wrong name.
    if audio_failed:
        kept = []
        for entry in plan_entries:
            reason = audio_failed.get(entry["path"])
            if reason:
                note_skip(
                    entry["path"], f"audio step failed ({reason})", breaks_group=False
                )
            else:
                kept.append(entry)
        plan_entries = kept

    planned = {}
    for entry in plan_entries:
        planned.setdefault(entry["key"], []).append(
            (entry["path"], entry["target"], entry["relative_new"])
        )

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
            if dry_run:
                # The review list above already showed every name; repeating it
                # here would just print the same plan twice.
                continue

            click.echo(f"Found: {os.path.relpath(original_path, cwd)}")
            click.secho(f"  -> {relative_new_path}\n", fg="green")

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
