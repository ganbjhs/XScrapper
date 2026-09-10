"""
test_report_contract.py — the report tool's pull contract (REPORT_TOOL_PLAN.md).

What report.vedictech.in needs from GET /api/links and GET /api/project, and
the properties that are easy to break by accident later:

  1. the envelope carries `items` AS WELL AS `rows` — portal/scraper.py's
     normalize_many() accepts posts|data|items|results|records and NOTHING
     else, so a body carrying only `rows` fails their sync outright;
  2. every row carries `platform`, `group` and a non-null `day`;
  3. `day` falls back to the IST date the link was seen when the tab is not
     named like a date, and SAYS SO in status_note;
  4. media elements carry `thumbnail_url` beside `thumb`;
  5. `rows` and `items` are never renamed away (LINKS_CONSUMER_HANDOVER.md:
     fields are added, never renamed — Watch-Tower reads `rows`);
  6. the handshake counts what is there, including the non-X links we are NOT
     measuring.

Offline: no network, no X, no Google. Run: python3 tests/test_report_contract.py
"""

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import links as links_mod
from store import Store, _ist_day_of

FAILED = []


def _ok(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


async def _store(tmp):
    st = Store(str(pathlib.Path(tmp) / "results.db"))
    await st.open()
    return st


def test_ist_day(ok):
    print("== _ist_day_of: the campaign day is IST, always ==")
    ok(_ist_day_of("2026-09-08T10:00:00+00:00") == "2026-09-08", "a UTC morning is the same IST day")
    ok(_ist_day_of("2026-09-08T20:30:00+00:00") == "2026-09-09",
       "20:30 UTC is 02:00 IST the NEXT day — the +05:30 offset is applied, not ignored")
    ok(_ist_day_of("2026-09-08T18:29:00+00:00") == "2026-09-08" and
       _ist_day_of("2026-09-08T18:31:00+00:00") == "2026-09-09",
       "the boundary sits exactly at 18:30 UTC")
    ok(_ist_day_of(1757486400000) == _ist_day_of(1757486400), "epoch ms and epoch s agree")
    for bad in ("", None, "garbage", "not-a-date"):
        ok(_ist_day_of(bad) == "",
           f"{bad!r} is an empty day, never a guess at today")


async def run_snapshot(tmp, ok):
    print("== /api/links: the row and the envelope ==")
    st = await _store(tmp)
    try:
        proj = await st.create_project("Varanasi Campaign")
        pid = proj["project_id"]
        tid = 2073389948066214090

        # A DAY tab, exactly as the real sheet names them (D/M/YY).
        day_wl = await st.create_watchlist(pid, "7/9/26", "links")
        st.db.execute("UPDATE watchlists SET sheet_tab = ?, sheet_day = ? "
                      "WHERE watchlist_id = ?", ("7/9/26", "2026-09-07",
                                                 day_wl["watchlist_id"]))
        await st.add_links(day_wl["watchlist_id"],
                           [(tid, f"https://x.com/anujakapurindia/status/{tid}", 31,
                             "National X Influencers")], via="sheet")

        # An ARCHIVE tab: a real tab in the real sheet, and NOT a date.
        arc = await st.create_watchlist(pid, "Tweet LInks", "links")
        st.db.execute("UPDATE watchlists SET sheet_tab = ?, sheet_day = NULL "
                      "WHERE watchlist_id = ?", ("Tweet LInks", arc["watchlist_id"]))
        await st.add_links(arc["watchlist_id"],
                           [(tid + 1, f"https://x.com/b/status/{tid + 1}", 4, "Counter Comments")],
                           via="sheet")
        st.db.commit()

        snap = await st.links_snapshot(pid)

        ok(snap["items"] is snap["rows"],
           "`items` is the SAME list as `rows` — their normalize_many() reads items, "
           "Watch-Tower reads rows, and neither is a copy")
        ok(set(("total", "rows", "items", "limit", "offset")) <= set(snap),
           f"the envelope keeps every key it had: {sorted(snap)}")
        ok(snap["total"] == 2, f"both links are counted (total={snap['total']})")

        by_id = {r["tweet_id"]: r for r in snap["items"]}
        dated = by_id[str(tid)]
        archived = by_id[str(tid + 1)]

        ok(dated["platform"] == "x",
           "every row states its platform — their platform_of() files anything "
           "unrecognised as X, so silence here misfiles FB/IG/YouTube later")
        ok(dated["group"] == dated["section"] == "National X Influencers",
           "`group` carries the sheet heading verbatim beside `section`")
        ok(dated["day"] == "2026-09-07",
           f"a date-named tab keeps its own day ({dated['day']})")
        ok(dated["status_note"] is None,
           "…and says nothing about it — a note is for when we GUESSED")

        ok(archived["day"] and archived["day"] != "",
           f"a tab that is not a date still gets a day ({archived['day']}) — `day` is "
           f"required by the report tool and drives every day-wise view it renders")
        ok("inferred" in (archived["status_note"] or ""),
           f"…and the row admits the day was inferred: {archived['status_note']!r}")
        ok(archived["day"] == _ist_day_of(archived["added_at"]),
           "the inferred day is when we first SAW the link, never the scrape date — "
           "the scrape date would pile every historical post onto today")

        ok(isinstance(dated["tweet_id"], str),
           "tweet_id is a string: X ids exceed 2^53 and their dashboard uses JSON.parse")

        # tweet ids are stored as INTEGER; the string must be exact, not rounded.
        ok(dated["tweet_id"] == str(tid),
           f"…and it is exact, digit for digit ({dated['tweet_id']})")

        print("== a PAUSED watchlist is fetched by nobody and served to nobody ==")
        # The operator's one visible "this list is not live" control. It
        # already stopped the collector fetching; serving the rows anyway was
        # the hole — stale numbers, and for an archive tab an inferred day and
        # a date label for a category.
        # create_watchlist already compiled the list to its stream; pausing is
        # a flag on that row, exactly as the dashboard's Pause button sets it.
        st.db.execute("UPDATE streams SET paused = 1 WHERE label = ?",
                      (f"wl:{arc['watchlist_id']}:0",))
        st.db.commit()
        snap2 = await st.links_snapshot(pid)
        ok(snap2["total"] == 1 and len(snap2["items"]) == 1,
           f"the paused list's rows leave the snapshot ({snap2['total']} of 2 remain)")
        ok(all(r["watchlist_id"] != arc["watchlist_id"] for r in snap2["items"]),
           "…and it is the paused one that went")
        hs2 = await st.links_handshake(pid, project=proj)
        ok(hs2["counters"]["total"] == snap2["total"],
           f"the handshake counts what is SERVED, so its total and /api/links "
           f"agree ({hs2['counters']['total']} vs {snap2['total']})")
        ok(hs2["paused"]["watchlists"] == 1 and hs2["paused"]["links"] == 1,
           f"…and it says out loud what is being withheld: {hs2['paused']}")
        # put it back for the checks below
        st.db.execute("UPDATE streams SET paused = 0 WHERE label = ?",
                      (f"wl:{arc['watchlist_id']}:0",))
        st.db.commit()

        print("== /api/project: the handshake ==")
        hs = await st.links_handshake(pid, project=proj)
        ok(hs["project"]["id"] == pid and hs["project"]["name"] == "Varanasi Campaign",
           "the handshake names the project, so a wrong key is caught at wiring time")
        ok(hs["counters"]["total"] == 2 and hs["counters"]["pending"] == 2,
           f"it counts links by status: {hs['counters']}")
        ok(hs["watchlist"]["tabs"] == 2 and hs["watchlist"]["dated_tabs"] == 1,
           f"it separates dated tabs from the rest: {hs['watchlist']}")
        ok(hs["limits"]["max_limit"] == links_mod.MAX_LIMIT
           and hs["limits"]["requests_per_minute"] == links_mod.RATE_PER_MIN,
           "it publishes the limits rather than letting a caller discover them at 3am")
        ok(hs["refresh_in_progress"] is False,
           "nothing is mid-scrape on a fresh project")
    finally:
        await st.close()


async def run_media(tmp, ok):
    print("== media: thumbnail_url beside thumb ==")
    st = await _store(tmp)
    try:
        proj = await st.create_project("M")
        pid = proj["project_id"]
        tid = 999000000000000001
        wl = await st.create_watchlist(pid, "1/1/26", "links")
        st.db.execute("UPDATE watchlists SET sheet_day = ? WHERE watchlist_id = ?",
                      ("2026-01-01", wl["watchlist_id"]))
        await st.add_links(wl["watchlist_id"], [(tid, f"https://x.com/a/status/{tid}", 1, None)])
        st.db.execute(
            "INSERT INTO tweets (tweet_id, url, created_at, created_ms, text, "
            "author_username, media_json, collected_at, collected_ms, "
            "last_seen_at, lag_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"https://x.com/a/status/{tid}", "2026-01-01T00:00:00+00:00",
             1767225600000, "hi", "a",
             '[{"type":"video","url":"https://v/x.mp4","thumb":"https://t/x.jpg"}]',
             "2026-01-01T00:01:00+00:00", 1767225660000,
             "2026-01-01T00:01:00+00:00", 60000))
        st.db.commit()
        row = (await st.links_snapshot(pid))["items"][0]
        m = row["media"][0]
        ok(m.get("thumbnail_url") == "https://t/x.jpg",
           "their FIELD_MAP reads media.0.thumbnail_url")
        ok(m.get("thumb") == "https://t/x.jpg",
           "…and `thumb` is still there — the panel reads it, and we never rename")
    finally:
        await st.close()


def test_rate_limit(ok):
    print("== the rate limit is declared, and it does not bind a correct caller ==")
    import web

    web._RATE_HITS.clear()
    key = "k_" + "r" * 40
    # A full walk of the real sheet: 1,869 links at limit=500 is FOUR requests.
    allowed = all(web._rate_ok(key, now=1000.0 + i)[0] for i in range(4))
    ok(allowed, "four requests — a whole watchlist — pass without a pause")

    web._RATE_HITS.clear()
    verdicts = [web._rate_ok(key, now=2000.0)[0]
                for _ in range(links_mod.RATE_PER_MIN + 5)]
    ok(all(verdicts[:links_mod.RATE_PER_MIN]),
       f"the first {links_mod.RATE_PER_MIN} in a minute all pass")
    ok(not any(verdicts[links_mod.RATE_PER_MIN:]),
       "…and the ones past the ceiling are refused")
    okay, retry = web._rate_ok(key, now=2000.0)
    ok(not okay and retry > 0, f"a refusal carries a Retry-After ({retry}s)")
    ok(web._rate_ok(key, now=2061.0)[0],
       "the window slides: a minute later the same key is served again")

    web._RATE_HITS.clear()
    other = "k_" + "s" * 40
    for _ in range(links_mod.RATE_PER_MIN):
        web._rate_ok(key, now=3000.0)
    ok(web._rate_ok(other, now=3000.0)[0],
       "one key exhausting its budget never throttles another — Watch-Tower and "
       "the report tool do not share a ceiling")
    ok(key not in web._RATE_HITS and len(web._RATE_HITS) >= 1,
       "keys are hashed in the counter, so a memory dump is not a key list")


def run(tmp, ok=_ok):
    """Every contract check, in contract order. `tmp` is a scratch directory."""
    tmp = pathlib.Path(tmp)
    test_ist_day(ok)
    test_rate_limit(ok)
    d = tmp / "snapshot"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_snapshot(d, ok))
    d = tmp / "media"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_media(d, ok))


def main():
    print("report tool pull contract\n")
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
