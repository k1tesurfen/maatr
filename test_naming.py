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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
