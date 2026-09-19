import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maatr

CFG = {"audio": {"preferred": "eng", "secondary": "ger", "fallback_default": "ger"}}

def t(tid, lang, size, default=False, ch=6, name=None, commentary=False, ietf=None):
    props = {"language": lang, "default_track": default, "audio_channels": ch,
             "tag_number_of_bytes": str(size)}
    if name: props["track_name"] = name
    if commentary: props["flag_commentary"] = True
    if ietf: props["language_ietf"] = ietf
    return {"id": tid, "type": "audio", "codec": "AC-3", "properties": props}

def sub(tid, lang, default=False):
    return {"id": tid, "type": "subtitles", "codec": "SubRip",
            "properties": {"language": lang, "default_track": default}}

def run(name, tracks, expect_action, expect_keep=None, expect_default=None):
    info = {"tracks": [{"id": 0, "type": "video", "codec": "AVC", "properties": {}}] + tracks}
    p = maatr.plan_audio(info, CFG, path=None)
    ok = p["action"] == expect_action
    detail = ""
    if ok and expect_keep is not None:
        got = sorted(x["id"] for x in p["keep"])
        ok = got == sorted(expect_keep)
        detail = f" keep={got} want={sorted(expect_keep)}"
    if ok and expect_default is not None:
        ok = p.get("default_id") == expect_default
        detail += f" default={p.get('default_id')} want={expect_default}"
    print(("  PASS  " if ok else "  FAIL  ") + f"{name}: action={p['action']}{detail}"
          + ("" if ok else f"  <<< {p.get('reason','')}"))
    return ok

results = []
print("== the core case ==")
results.append(run("2xGER+2xENG, ger default -> best eng + smallest ger",
    [t(1,"ger",450,default=True), t(2,"ger",80,ch=2), t(3,"eng",280), t(4,"eng",60,ch=2)],
    "remux", expect_keep=[2,3], expect_default=3))
results.append(run("same but eng already default -> still fires",
    [t(1,"ger",450), t(2,"ger",80,ch=2), t(3,"eng",280,default=True), t(4,"eng",60,ch=2)],
    "remux", expect_keep=[2,3], expect_default=3))

print("== uneven counts ==")
results.append(run("3xGER+1xENG -> smallest ger + the one eng",
    [t(1,"ger",450,default=True), t(2,"ger",180), t(3,"ger",60), t(4,"eng",280)],
    "remux", expect_keep=[3,4], expect_default=4))
results.append(run("1xGER+1xENG, ger default -> nothing to drop, flags only",
    [t(1,"ger",450,default=True), t(2,"eng",280)],
    "flags", expect_keep=[1,2], expect_default=2))
results.append(run("1xGER+1xENG, eng default already -> untouched",
    [t(1,"ger",450), t(2,"eng",280,default=True)], "none"))

print("== other languages dropped when rule fires ==")
results.append(run("ger,ger,eng,eng,ita,und -> only eng+ger survive",
    [t(1,"ger",450,default=True), t(2,"ger",80), t(3,"eng",280), t(4,"eng",60),
     t(5,"ita",200), t(6,None,150)],
    "remux", expect_keep=[2,3], expect_default=3))

print("== no english: flag-only, keep everything ==")
results.append(run("ger+por+spa, ger not default -> promote ger, no remux",
    [t(1,"ger",450), t(2,"por",200), t(3,"spa",200)],
    "flags", expect_keep=[1,2,3], expect_default=1))
results.append(run("ger+por+spa, ger already default -> untouched",
    [t(1,"ger",450,default=True), t(2,"por",200), t(3,"spa",200)], "none"))
results.append(run("2xGER only, no eng -> flags only, NO dedupe",
    [t(1,"ger",450), t(2,"ger",80)], "flags", expect_keep=[1,2], expect_default=1))

print("== english but no german ==")
results.append(run("eng+ita, ita default -> promote eng, keep all",
    [t(1,"eng",280), t(2,"ita",200,default=True)],
    "flags", expect_keep=[1,2], expect_default=1))
results.append(run("2xENG+ita -> flag only, no dedupe (needs both langs)",
    [t(1,"eng",280), t(2,"eng",60), t(3,"ita",200,default=True)],
    "flags", expect_keep=[1,2,3], expect_default=1))

print("== commentary ==")
results.append(run("3xGER incl. tiny commentary -> commentary must NOT win",
    [t(1,"ger",450,default=True), t(2,"ger",180), t(3,"ger",60,name="Audiokommentar"),
     t(4,"eng",280)],
    "remux", expect_keep=[2,4], expect_default=4))
results.append(run("commentary via flag_commentary",
    [t(1,"ger",450,default=True), t(2,"ger",180), t(3,"ger",60,commentary=True),
     t(4,"eng",280)],
    "remux", expect_keep=[2,4], expect_default=4))
results.append(run("biggest ENG is a commentary -> pick next biggest real one",
    [t(1,"ger",450,default=True), t(2,"ger",80),
     t(3,"eng",900,name="Director's Commentary"), t(4,"eng",280)],
    "remux", expect_keep=[2,4], expect_default=4))

print("== language tag variants ==")
results.append(run("deu/de-DE and en-GB normalise correctly",
    [t(1,"deu",450,default=True), t(2,None,80,ietf="de-DE"),
     t(3,None,280,ietf="en-GB"), t(4,"eng",60)],
    "remux", expect_keep=[2,3], expect_default=3))

print("== ties and edges ==")
results.append(run("two GER identical size -> deterministic (lower id)",
    [t(1,"ger",100,default=True), t(2,"ger",100), t(3,"eng",280)],
    "remux", expect_keep=[1,3], expect_default=3))
results.append(run("no audio at all -> skip",  [], "skip"))
results.append(run("only commentary in GER -> falls back, still picks one",
    [t(1,"ger",60,name="Kommentar",default=True), t(2,"eng",280)],
    "flags", expect_keep=[1,2], expect_default=2))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
