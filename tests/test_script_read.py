"""
test_script_read.py — reading a sheet through its own Apps Script.

The credential-free door (REPORT_TOOL_PLAN.md Part 9): no service account, no
cloud project, no JSON key, no sharing step, and the sheet stays private.

This does NOT re-implement the script in Python. It runs the REAL
`sheets.SCRIPT_SOURCE` in node behind a tiny HTTP server (appsscript_harness.js)
and points the REAL `links.read_sheet_via_script` at it over real HTTP, so what
is proved is the source we tell an operator to paste.

Offline: node + httpx, no Google. Run: python3 tests/test_script_read.py
"""

import asyncio
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import links as links_mod
import sheets as sheets_mod

HERE = pathlib.Path(__file__).resolve().parent
FAILED = []

TOKEN = "tok_" + "a" * 20

# The real sheet's shape: day tabs named D/M/YY, an archive tab that is not a
# date, a hidden tab, and headings as merged one-cell rows.
FIXTURE = {
    "title": "Varansi Day Wise Data",
    "tabs": [
        {"gid": 0, "title": "Tweet LInks", "hidden": False, "values": [
            ["Date- 4-7-26", "", ""],
            ["", "https://x.com/anujakapurindia/status/2073389948066214090", "8658"],
        ]},
        {"gid": 1264297118, "title": "8/9/26", "hidden": False, "values": [
            ["Posts", "views", "Likes"],
            ["National X Influencers", "", ""],
            ["https://x.com/ishivauvach/status/2073390255927926937", "30,000", "60,000"],
            ["3 Party Pages Posting", "", ""],
            ["https://www.facebook.com/reel/27814387954926960/", "30,000", "60,000"],
        ]},
        {"gid": 7, "title": "scratch", "hidden": True, "values": [
            ["https://x.com/nobody/status/1111111111111111111", "", ""],
        ]},
    ],
}


def _ok(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Harness:
    """The real SCRIPT_SOURCE, running in node, over HTTP."""

    def __init__(self, tmp, source=None):
        self.tmp = pathlib.Path(tmp)
        self.port = _free_port()
        js = self.tmp / "script.js"
        js.write_text(source if source is not None
                      else sheets_mod.script_source(TOKEN), encoding="utf-8")
        fx = self.tmp / "fixture.json"
        fx.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.proc = subprocess.Popen(
            ["node", str(HERE / "appsscript_harness.js"), str(js), str(fx), str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        if not line.startswith("READY"):
            raise RuntimeError(f"harness did not start: {line}{self.proc.stderr.read()}")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/exec"

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


async def run_read(tmp, ok):
    import httpx

    print("== the real Apps Script answers the read protocol ==")
    h = Harness(tmp)
    try:
        async with httpx.AsyncClient() as client:
            snap, err = await links_mod.read_sheet_via_script(client, h.url, TOKEN)
            ok(not err, f"a read with the right token succeeds ({err or 'no error'})")
            ok(snap is not None and snap.title == "Varansi Day Wise Data",
               f"the spreadsheet's own title comes back: {snap and snap.title!r}")
            titles = [t.title for t in (snap.tabs if snap else [])]
            ok(titles == ["Tweet LInks", "8/9/26"],
               f"hidden tabs are skipped, the rest keep their order: {titles}")
            ok(all(isinstance(t.gid, int) for t in snap.tabs)
               and snap.tabs[1].gid == 1264297118,
               "each tab carries its real numeric gid, so a RENAME keeps its links")

            day = snap.tabs[1]
            ok(day.values and day.values[0][0] == "Posts",
               f"cells arrive as rows of strings: {day.values[0]}")
            ok(day.values[2][1] == "30,000",
               "…as DISPLAY values, exactly as the REST path returns them — "
               "'30,000' not 30000")

            print("== …and the scanner reads it the same as the REST path would ==")
            scan = links_mod.scan_values(day.values)
            ok(len(scan.items) == 1 and scan.items[0].section == "National X Influencers",
               f"the merged heading row becomes the section: "
               f"{[(i.tweet_id, i.section) for i in scan.items]}")
            ok(scan.skipped == 1,
               f"the Facebook link is counted as skipped, not silently dropped "
               f"({scan.skipped})")
            ok(links_mod.parse_tab_day(day.title) == "2026-09-08",
               "the tab's own name is still what dates it")

            print("== the failures an operator will actually hit ==")
            snap, err = await links_mod.read_sheet_via_script(client, h.url, "wrong-token")
            ok(snap is None and "token" in err.lower(),
               f"a wrong token is refused, and the message says which end may be wrong: {err[:70]}…")
            ok("not knowable from here" in err,
               "…without claiming to know which of the two strings is wrong")

            snap, err = await links_mod.read_sheet_via_script(
                client, f"http://127.0.0.1:{_free_port()}/exec", TOKEN)
            ok(snap is None and err,
               f"an unreachable script is an error, never an empty sheet ({err[:50]}…)")
    finally:
        h.close()


async def run_old_deployment(tmp, ok):
    """A version-1 script — every deployment pasted before 2026-09-10."""
    import httpx

    print("== an OLD deployment says so, instead of looking broken ==")
    old = '''
var TOKEN = '%TOKEN%';
var VERSION = 1;
function doPost(e) {
  var body = JSON.parse(e.postData.contents);
  if (!TOKEN || body.token !== TOKEN) { return reply({ error: 'bad token' }); }
  return reply({ ok: true, appended: 0 });
}
// The real v1 doGet, verbatim — it carried no version at all.
function doGet() {
  return reply({ ok: true, note: 'X Collector sheet endpoint. POST only.' });
}
function reply(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
                       .setMimeType(ContentService.MimeType.JSON);
}
'''.replace("%TOKEN%", TOKEN)
    h = Harness(tmp, source=old)
    try:
        async with httpx.AsyncClient() as client:
            snap, err = await links_mod.read_sheet_via_script(client, h.url, TOKEN)
            # A v1 script answers {ok:true, appended:0} to an unknown action —
            # no error at all — and would insert a stray "Sheet1" tab on the
            # way. The read must refuse BEFORE posting, and must never call
            # that an empty sheet: an empty sheet marks every link 'removed'.
            ok(snap is None and err,
               f"a script that cannot read never yields an empty snapshot ({err[:60]}…)")
            ok("re-paste" in err.lower() or "version" in err.lower(),
               f"…and the message tells the operator what to DO: {err[:90]}…")
    finally:
        h.close()


def test_source(ok):
    print("== the pasted script and the Python that talks to it agree ==")
    m = re.search(r"var VERSION = (\d+);", sheets_mod.SCRIPT_SOURCE)
    ok(m and int(m.group(1)) == sheets_mod.SCRIPT_VERSION,
       f"SCRIPT_SOURCE says v{m and m.group(1)}, sheets.SCRIPT_VERSION says "
       f"v{sheets_mod.SCRIPT_VERSION} — a script claiming a version it cannot "
       f"speak is worse than one that admits it is old")
    src = sheets_mod.script_source(TOKEN)
    ok("%TOKEN%" not in src and TOKEN in src,
       "the operator's token is substituted into the script they paste")
    ok("getDisplayValues" in src,
       "values are read as DISPLAY strings, matching the REST path")
    ok("isSheetHidden" in src,
       "the tab list carries the hidden flag — the one thing the published-CSV "
       "route cannot tell you")
    # The append path is what every existing deployment does; a caller that
    # sends no action must still append.
    ok("body.action || 'append'" in src,
       "an action-less POST still appends, so an older Collector is unaffected")


def _has_node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


async def run_dated_only(tmp, ok):
    """
    tabs_mode='dated' — the fix for what the live sheet did on 2026-09-10.

    Binding the real sheet took its two ARCHIVE tabs as watchlists: 674 links
    that duplicate the day tabs, re-fetched daily against the X budget, with a
    `day` that could only be inferred (a 4 July post landed on 10 September)
    and a `section` of "Date- 4-7-26" — the date label in column A, handed to a
    metrics consumer as a category.
    """
    import httpx
    from store import Store

    print("== tabs_mode='dated' skips the archive tabs ==")
    h = Harness(tmp)
    st = Store(str(pathlib.Path(tmp) / "results.db"))
    await st.open()
    try:
        proj = await st.create_project("Varanasi Client")
        pid = proj["project_id"]

        bound = await st.bind_link_sheet(pid, "1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0",
                                         script_url=None, script_token_env=None,
                                         tabs_mode="dated")
        ok(bound.get("tabs_mode") == "dated", f"the mode is stored ({bound.get('tabs_mode')})")

        async with httpx.AsyncClient() as client:
            snap, err = await links_mod.read_sheet_via_script(client, h.url, TOKEN)
            ok(not err, "the sheet still reads")
            # sync_sheet needs the bound row; feed it the one we just made,
            # with the script fields pointed at the harness.
            sheet = dict(bound)
            sheet["script_url"] = h.url
            sheet["script_token_env"] = "HARNESS_TOKEN"
            os.environ["HARNESS_TOKEN"] = TOKEN
            res = await links_mod.sync_sheet(st, client, sheet)

        names = [w["name"] for w in res["watchlists"]]
        ok(names == ["8/9/26"],
           f"ONLY the dated tab became a watchlist: {names}")
        ok(res.get("tabs_skipped") == 1 and res.get("skipped_tabs") == ["Tweet LInks"],
           f"…and the archive tab is COUNTED as skipped, not hidden: "
           f"{res.get('tabs_skipped')} {res.get('skipped_tabs')}")

        rows = (await st.links_snapshot(pid))["items"]
        ok(all(r["day"] == "2026-09-08" for r in rows),
           f"every row carries the tab's real day: {sorted({r['day'] for r in rows})}")
        ok(all("inferred" not in (r["status_note"] or "") for r in rows),
           "no row needs an inferred day any more — that note was the symptom")
        ok(all(r["group"] != "Date- 4-7-26" for r in rows),
           "and no row's category is a DATE LABEL scraped from column A")

        print("== 'all' is still the default, so nothing already bound changes ==")
        st2 = Store(str(pathlib.Path(tmp) / "other.db"))
        await st2.open()
        p2 = await st2.create_project("Legacy")
        b2 = await st2.bind_link_sheet(p2["project_id"], "1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0")
        ok(not b2.get("tabs_mode"),
           "a sheet bound without the field keeps NULL, which reads as 'all'")
        bad = await st2.bind_link_sheet(p2["project_id"],
                                        "1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0",
                                        tabs_mode="sometimes")
        ok("error" in bad, f"an unknown mode is refused rather than stored ({bad.get('error')})")
        await st2.close()
    finally:
        await st.close()
        h.close()


def run(tmp, ok=_ok):
    tmp = pathlib.Path(tmp)
    # The source checks are pure text and always run — they are the ones that
    # catch the script and the Python drifting apart.
    test_source(ok)
    if not _has_node():
        print("  --   node is not installed; skipping the live-script checks")
        return
    d = tmp / "read"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_read(d, ok))
    d = tmp / "old"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_old_deployment(d, ok))
    d = tmp / "dated"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_dated_only(d, ok))


def main():
    print("reading a sheet through its own Apps Script\n")
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, _ok)
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
