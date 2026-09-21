"""Rules for filling in what an abbreviated filename leaves out."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maatr

results = []

def check(name, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + f"{name}: got {got!r}"
          + ("" if ok else f" want {want!r}"))
    results.append(ok)

print("== year from folder name ==")
for folder, want in [
    ("Some.Long.Movie.Title.2018.German.1080p.H265-ABC", 2018),
    ("Another.Film.2001.German.1080p-XYZ", 2001),
    ("Example Movie (1979)", 1979),
    # resolution tokens must never be mistaken for years
    ("Movie.2160p", None),
    ("Movie.1080p", None),
    ("Movie.720p", None),
    ("Movie.1920x1080", None),
    ("Movie.2018.2160p", 2018),
    # out of the 1900-2100 range
    ("Movie.1899", None),
    ("Movie.2101", None),
    # ambiguous: refuse rather than guess
    ("Example.Movie.1999.Remastered.2015.1080p", None),
]:
    year, _ = maatr.year_from_folders(f"/tmp/{folder}/file.mkv", levels=1)
    check(f"{folder[:46]:48}", year, want)

print("== ambiguity is reported, not silent ==")
_, note = maatr.year_from_folders("/tmp/Example.Movie.1999.Remastered.2015/f.mkv", levels=1)
check("two years produce a note", bool(note), True)
_, note = maatr.year_from_folders("/tmp/Movie.2160p/f.mkv", levels=1)
check("no year produces no note", note, None)

print("== junk container titles are rejected ==")
for title, want in [
    ("Example Movie: The Subtitle (2018)", True),
    ("Example Movie", True),
    ("Example Movie 1979 Directors Cut", True),
    ("Example Series S01E03", True),
    ("video", False),
    ("movie", False),
    ("untitled", False),
    ("Encoded by SomeTool", False),
    ("converted with SomeTool", False),
    ("", False),
    ("   ", False),
]:
    # exercise the guard directly, without needing a real MKV
    junk = (maatr.JUNK_TITLE_RE.search(title) is not None)
    words = [w for w in maatr.re.findall(r"[a-z0-9]+", title.lower())]
    usable = bool(words) and not junk and not (
        len(words) == 1 and words[0] in maatr.JUNK_TITLE_WORDS)
    check(f"{title[:34]!r:38}", usable, want)

print("== path components stay safe for exFAT/SMB drives ==")
check("colon becomes ' -'", maatr.sanitize_component("Example Movie: The Subtitle"),
      "Example Movie - The Subtitle")
check("separators stripped", maatr.sanitize_component("../etc/passwd"), "etc passwd")
check("illegal chars stripped", maatr.sanitize_component('Who? What* "X"'), "Who What X")

print("== names survive the trip to the NAS (ASCII only) ==")
for raw, want in [
    # German first: 'Ae', not 'A'
    ("Ödipussi", "Oedipussi"),
    ("Über Alles", "Ueber Alles"),
    ("Die Straße", "Die Strasse"),
    # macOS hands out decomposed umlauts; they must land on the same name
    ("München", "Muenchen"),
    ("München", "Muenchen"),
    # everything else loses its accents rather than its letters
    ("Amélie", "Amelie"),
    ("WALL·E", "WALL-E"),
    ("Pokémon", "Pokemon"),
    ("Léon: Der Profi", "Leon - Der Profi"),
    # colon, with and without the space
    ("Solo: A Star Wars Story", "Solo - A Star Wars Story"),
    ("Halo 2:Anniversary", "Halo 2 - Anniversary"),
    # punctuation that is legal but awkward
    ("Mike's Dream", "Mikes Dream"),
    ("Mamma Mia!", "Mamma Mia"),
    ("Wer ist Hanna?", "Wer ist Hanna"),
    ("Fire & Ice", "Fire and Ice"),
    ("Fire&Ice", "Fire and Ice"),
    # a title that already has a dash must not end up with two
    ("Spider-Man: No Way Home", "Spider-Man - No Way Home"),
    ("Alien - : Romulus", "Alien - Romulus"),
    # a backslash is a separator the moment the drive is read from Windows
    ("Foo\\Bar", "Foo Bar"),
    # control characters, and a name that is nothing but banned characters
    ("Tab\there", "Tab here"),
    ("...", ""),
    ("'?!", ""),
]:
    check(f"{raw[:30]!r:34}", maatr.sanitize_component(raw), want)

check(
    "long names capped to 255 bytes",
    len(maatr.cap_component(("Very Long Title " * 30) + ".mkv").encode("utf-8")) <= 255,
    True,
)
check(
    "cap keeps the extension",
    maatr.cap_component(("Very Long Title " * 30) + ".mkv").endswith(".mkv"),
    True,
)

print("== samples and extras are told apart from the feature ==")
for path, want in [
    # scene releases put the token at the end of the stem, or in a folder
    ("Movie.2000.1080p-GRP/movie.2000.1080p-grp.sample.mkv", True),
    ("Movie.2000.1080p-GRP/movie-sample.mkv", True),
    ("Movie.2000.1080p-GRP/movie_sample.mkv", True),
    ("Movie.2000.1080p-GRP/sample.mkv", True),
    ("Movie.2000.1080p-GRP/Sample/anything.mkv", True),
    ("Movie.2000.1080p-GRP/Extras/deleted-scene.mkv", True),
    ("Movie.2000.1080p-GRP/movie-trailer.mkv", True),
    ("Movie.2000.1080p-GRP/movie.proof.mkv", True),
    # the token has to be at the END, or a real film loses its name
    ("Sample People (2000)/Sample People (2000).mkv", False),
    ("Trailer Park Boys/Trailer Park Boys S01E01.mkv", False),
    ("Movie.2000.1080p-GRP/sampler.mkv", False),
    ("Movie.2000.1080p-GRP/movie.2000.1080p-grp.mkv", False),
]:
    got, _ = maatr.is_extra_media(path)
    check(f"{path[-40:]:42}", got, want)

print("== one movie per movie folder, but only when it is unambiguous ==")
SIZES = {"feature.mkv": 10_000_000_000, "sample.mkv": 64_000_000,
         "part-a.mkv": 5_200_000_000, "part-b.mkv": 4_800_000_000,
         "ep1.mkv": 2_000_000_000, "ep2.mkv": 2_000_000_000}
for name, entries, want_winner, want_dropped in [
    ("film beside its sample: largest wins",
     [("feature.mkv", "movie"), ("sample.mkv", "movie")], "feature.mkv", 1),
    ("two near-equal films: refuse, let the folder fail",
     [("part-a.mkv", "movie"), ("part-b.mkv", "movie")], None, 0),
    ("a season is never reduced to one episode",
     [("ep1.mkv", "episode"), ("ep2.mkv", "episode")], None, 0),
    ("a mixed group is too odd to judge",
     [("feature.mkv", "movie"), ("ep1.mkv", "episode")], None, 0),
    ("a single file needs no choosing",
     [("feature.mkv", "movie")], None, 0),
]:
    winner, losers = maatr.pick_primary_movie(entries, SIZES.get)
    check(f"{name:48}", (winner, len(losers)), (want_winner, want_dropped))

print("== TMDB matches are verified against the year, never guessed ==")
HITS = [
    {"id": 1, "title": "Right Film", "release_date": "2018-11-14", "popularity": 9},
    {"id": 2, "title": "Wrong Film", "release_date": "2011-01-01", "popularity": 99},
    {"id": 3, "title": "Also Near", "release_date": "2019-06-01", "popularity": 5},
]
for name, hits, year, want in [
    ("exact year wins", HITS, 2018, "Right Film"),
    ("a year out still counts", HITS, 2020, "Also Near"),
    ("popularity breaks ties inside the window", HITS, 2010, "Wrong Film"),
    ("three years out is refused", HITS, 2015, None),
    ("no year means no verdict", HITS, None, None),
    ("no results means no verdict", [], 2018, None),
]:
    result, reason = maatr.pick_tmdb_movie(hits, year)
    check(f"{name:42}", result["title"] if result else None, want)
    check(f"{'  ...with a reason' if want is None else '  ...silently':42}",
          reason is None, want is not None)

print("== one series title per folder ==")
for name, entries, folder, want_title, want_overrides in [
    ("a clear majority wins",
     [("a", "Example Series", "episode"),
      ("b", "Example Series", "episode"),
      ("c", "The Long Night", "episode")],
     None, "Example Series", 1),
    ("a tie is broken by the folder name",
     [("a", "The Long Night", "episode"),
      ("b", "Example Series", "episode")],
     "Example Series", "Example Series", 1),
    ("no majority and no folder title changes nothing",
     [("a", "The Long Night", "episode"),
      ("b", "Another Night", "episode")],
     None, None, 0),
    ("movies are never unified",
     [("a", "Example Movie", "movie"),
      ("b", "Other Movie", "movie")],
     "Example Movie", None, 0),
    ("a single episode needs no rule",
     [("a", "The Long Night", "episode")],
     "Example Series", None, 0),
    ("already agreeing files are left alone",
     [("a", "Example Series", "episode"),
      ("b", "Example Series", "episode")],
     None, "Example Series", 0),
]:
    title, overridden = maatr.unify_series_titles(entries, folder)
    check(f"{name:48}", (title, len(overridden)), (want_title, want_overrides))

print("== an edited name is read back into fields ==")
for name, text, want in [
    ("a full movie name",
     "Example Movie (2024) [1080p] [ENG-GER].mkv",
     ("Example Movie", 2024, "1080p", "ENG-GER", "movie")),
    ("no year is accepted",
     "Example Movie [1080p] [ENG].mkv",
     ("Example Movie", None, "1080p", "ENG", "movie")),
    ("no resolution is accepted",
     "Example Movie (2024).mkv",
     ("Example Movie", 2024, None, None, "movie")),
    ("no extension is fine",
     "Example Movie (2024) [1080p] [ENG-GER]",
     ("Example Movie", 2024, "1080p", "ENG-GER", "movie")),
    ("an episode name keeps its numbers",
     "Example Series S01E04 [1080p] [ENG-GER].mkv",
     ("Example Series", None, "1080p", "ENG-GER", "episode")),
    ("a codec tag is not our audio tag",
     "Example Movie (2024) [1080p] [x264].mkv",
     ("Example Movie", 2024, "1080p", None, "movie")),
]:
    fields, _ = maatr.parse_name_edit(text)
    got = (fields.get("title"), fields.get("year"),
           str(fields["screen_size"]) if fields.get("screen_size") else None,
           fields.get("audio"), fields.get("_media_type"))
    check(f"{name:48}", got, want)

fields, warnings = maatr.parse_name_edit("[1080p] [ENG].mkv")
check("an empty title is refused, never invented", "title" in fields, False)
check("  ...and says why", bool(warnings), True)
_, warnings = maatr.parse_name_edit("Example Movie [1080p].mkv")
check("a missing year is a warning, not a refusal", warnings, ["no year in that name"])

print("== a series line is read back into title and year ==")
for text, want in [
    ("Example Series (2019)", ("Example Series", 2019)),
    ("Example Series", ("Example Series", None)),
    ("Example Series 2019", ("Example Series", 2019)),
    ("Example 1899", ("Example 1899", None)),
    ("", (None, None)),
]:
    check(f"{text!r:48}", maatr.parse_series_edit(text), want)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
