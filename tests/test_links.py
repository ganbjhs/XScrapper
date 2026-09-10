"""
tests/test_links.py — the `links` watchlist, offline (X_LINKS_PLAN.md).

Run on its own:
    python3 tests/test_links.py
or as a section of tests/test_all.py, which passes in its own ok().

What is covered, in the order the plan builds it:
  1. links.py       URL parsing, sheet-cell scanning, due/order planning,
                    classify_detail on the payload shapes X actually sends
  2. engine.py      parse_detail pins the result set to the focal tweet
  3. store.py       kind='links' lifecycle: the empty, unwatched stream; adds,
                    revives, removals; the sheet→tab→watchlist binding keyed
                    on gid; due selection; refresh bookkeeping; the snapshot
  4. collector.py   refresh_links against a fake engine — counters overwrite,
                    collected_ms is frozen, unavailable keeps the last numbers,
                    transient backs off; and run_forever's third clock drives it
  5. links.sync_sheet against a fake Sheets API — every tab, every cell,
                    t.co resolved, adds never delete, a removed row is marked
  6. web.py         /api/links is routed and key-readable; a project-locked key
                    sees one project and four paths, nothing else
"""

import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import links as L
from engine import Engine, parse_detail
from fixtures import FakeResponse, id_at, mk_tweet, mk_user, _entry
from store import Store

FAILURES = []


def _ok(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


# --------------------------------------------------------------------------
# fixtures: a TweetDetail payload in X's real shape
# --------------------------------------------------------------------------

def detail_payload(tid, uid=11, name="alice", text="hello", likes=3, views="99",
                   followers=5, with_reply=True):
    """data.threaded_conversation_with_injections_v2.instructions[...] with the
    focal tweet as a `tweet-<id>` entry and (optionally) a reply module, the
    way X sends a conversation page."""
    user = mk_user(uid, name)
    user["legacy"]["followers_count"] = followers
    t = mk_tweet(tid, uid, text)
    t["legacy"]["favorite_count"] = likes
    t["views"] = {"count": str(views)}
    entries = [_entry(tid, t, user)]
    if with_reply:
        rid = tid + 1000
        r = mk_tweet(rid, 12, "a reply")
        entries.append({
            "entryId": f"conversationthread-{rid}",
            "content": {"entryType": "TimelineTimelineModule",
                        "items": [{"entryId": f"conversationthread-{rid}-tweet-{rid}",
                                   "item": {"itemContent": {
                                       "itemType": "TimelineTweet",
                                       "tweet_results": {"result": {
                                           **r, "core": {"user_results": {"result": mk_user(12, "bob")}}}}}}}]},
        })
    return {"data": {"threaded_conversation_with_injections_v2": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}


def deleted_payload():
    return {"errors": [{"message": "_Missing: No status found with that ID.",
                        "code": 144}], "data": {}}


def tombstone_payload(tid, text="This Post was deleted by the Post author. Learn more"):
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [{
            "entryId": f"tweet-{tid}",
            "content": {"entryType": "TimelineTimelineItem", "itemContent": {
                "itemType": "TimelineTombstone",
                "tombstoneInfo": {"text": {"text": text}},
                "tombstone": {"__typename": "TextTombstone", "text": {"text": text}}}}}]}]}}}


def unavailable_payload(tid, reason="Protected"):
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [{
            "entryId": f"tweet-{tid}",
            "content": {"entryType": "TimelineTimelineItem", "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {"result": {"__typename": "TweetUnavailable",
                                             "reason": reason}}}}}]}]}}}


# --------------------------------------------------------------------------
# 1. links.py
# --------------------------------------------------------------------------

def test_parsing(ok):
    print("== links: URL parsing ==")
    tid = 1789000000000000001
    for u in (f"https://x.com/nasa/status/{tid}",
              f"https://twitter.com/nasa/status/{tid}?s=20&t=abc",
              f"x.com/nasa/status/{tid}",
              f"https://mobile.twitter.com/nasa/statuses/{tid}",
              f"https://x.com/i/web/status/{tid}",
              f"https://vxtwitter.com/nasa/status/{tid}/photo/1",
              f"https://www.x.com/NASA/status/{tid}/video/2"):
        ok(L.parse_status_url(u) == tid, f"parses {u}")
    for u in ("https://www.instagram.com/reel/abc123/",
              "https://x.com/nasa", "https://x.com/nasa/likes",
              "https://facebook.com/nasa/posts/123", "not a link", "",
              # left-boundary traps: a longer hostname, a path that merely
              # contains x.com, and a partial id — each would burn a
              # TweetDetail per cycle for a post that does not exist
              f"https://notx.com/foo/status/{tid}",
              f"https://www.spacex.com/updates/status/{tid}",
              f"https://example.com/x.com/a/status/{tid}",
              "https://x.com/a/status/12345abc"):
        ok(L.parse_status_url(u) is None, f"rejects {u!r}")
    ok(L.parse_status_url(f"(https://x.com/a/status/{tid})") == tid
       and L.parse_status_url(f"link:x.com/a/status/{tid}") == tid,
       "…but punctuation before a real link is fine")
    ok(L.find_tco_links("https://docs.google.com/t.co/abc") == [],
       "a t.co path inside another host is not a short link")
    ok(L.parse_status_url(f"see https://x.com/a/status/{tid} and also text") == tid,
       "a link inside a sentence is found")
    two = L.find_status_links(f"https://x.com/a/status/{tid} https://x.com/b/status/{tid + 1}")
    ok([t for t, _ in two] == [tid, tid + 1], "find_status_links returns every link, in order")
    ok(L.find_tco_links("https://t.co/AbC123 and t.co/xyz99") == ["https://t.co/AbC123", "t.co/xyz99"],
       "t.co short links are recognised with or without a scheme")
    ok(L.canonical_url(tid, "NASA") == f"https://x.com/NASA/status/{tid}"
       and L.canonical_url(tid) == f"https://x.com/i/web/status/{tid}",
       "canonical_url builds x.com/<handle>/status/<id>, i/web when unknown")

    print("== links: scanning a tab ==")
    values = [
        ["Campaign A", "Notes"],
        [f"https://x.com/a/status/{tid}", "day 1"],
        ["", f"https://x.com/b/status/{tid + 1}"],
        [f"https://x.com/a/status/{tid}", "duplicate of row 2"],
        ["https://www.instagram.com/reel/abc/", "wrong platform"],
        ["https://t.co/AbC123"],
        ["just a label", "42"],
        [f"two in one cell https://x.com/c/status/{tid + 2} https://x.com/d/status/{tid + 3}"],
    ]
    scan = L.scan_values(values)
    ok([i.tweet_id for i in scan.items] == [tid, tid + 1, tid + 2, tid + 3],
       "every X link in any column is found, first occurrence wins")
    ok(scan.items[0].row == 2 and scan.items[1].row == 3,
       "each link remembers the 1-based sheet row it was first seen on")
    ok(scan.skipped == 1, f"a non-X link counts as skipped, a label does not ({scan.skipped})")
    ok(scan.tco == [(6, "https://t.co/AbC123")], "t.co links are reported for resolution")
    ok(scan.cells == 13, f"non-empty cells are counted ({scan.cells})")
    ok(L.scan_values([]).items == [] and L.scan_values(None).items == [],
       "an empty tab scans to nothing")

    print("== links: sections and day tabs ==")
    vals = [["Posts", "views", "Likes"],
            ["3 Party Pages Posting"],
            ["https://www.facebook.com/reel/27814387954926960/", "30,000", "60,000"],
            ["", "30,000", "60,000"],
            ["National X Influencers"],
            [f"https://x.com/MeghUpdates/status/{tid}?s=20", "30,000", "60,000"],
            ["Counter Comments Links "],
            [f"https://x.com/RajeshGupta5766/status/{tid + 1}", "30,000", "60,000"],
            ["30,000"],
            [f"https://x.com/x/status/{tid + 2}"]]
    scan = L.scan_values(vals)
    ok([(i.tweet_id, i.section) for i in scan.items] ==
       [(tid, "National X Influencers"), (tid + 1, "Counter Comments Links"), (tid + 2, "Counter Comments Links")],
       "a one-cell text row is a section heading; every link below carries it until the next")
    ok(scan.skipped == 1, "the Facebook reel is skipped, the header row and a bare number are not headings")
    ok(L.scan_values([[f"https://x.com/a/status/{tid}"]]).items[0].section is None,
       "no heading above -> section None")
    import datetime as _dt
    today = _dt.date(2026, 9, 9)
    for title, want in (("6/9/26", "2026-09-06"), ("16/8/26", "2026-08-16"), ("31/8/26", "2026-08-31"),
                        ("1/9/26", "2026-09-01"), ("06-09-2026", "2026-09-06"), ("2026-09-06", "2026-09-06"),
                        ("8 Sep", "2026-09-08"), ("8 Sept 2026", "2026-09-08"), ("Sep 8, 2026", "2026-09-08"),
                        (" 7/9/26 ", "2026-09-07"), ("Tweet Links", None), ("Counter Links", None),
                        ("3rd Party Posting", None), ("31/9/26", None), ("", None)):
        ok(L.parse_tab_day(title, today=today) == want, f"tab {title!r} -> {want}")


def test_planning(ok):
    print("== links: due / order ==")
    now = 2_000_000_000_000
    day = 86_400_000
    base = {"status": "ok", "refresh_every_s": 86400, "fail_streak": 0,
            "last_refresh_ms": now - day - 1, "last_attempt_ms": now - day - 1,
            "force": 0, "added_at": "2026-09-01", "tweet_id": 1}
    ok(L.is_due(dict(base), now), "ok + cadence elapsed -> due")
    ok(not L.is_due(dict(base, last_refresh_ms=now - day + 5000), now),
       "ok + cadence not yet elapsed -> not due")
    ok(L.is_due(dict(base, status="pending", last_refresh_ms=None, last_attempt_ms=None), now),
       "pending, never tried -> due immediately")
    ok(L.is_due(dict(base, last_refresh_ms=None, last_attempt_ms=None), now),
       "ok status with no refresh recorded -> due (defensive)")
    ok(not L.is_due(dict(base, status="removed", force=1), now),
       "removed -> never due, even when forced")
    ok(L.is_due(dict(base, last_refresh_ms=now - 1000, force=1), now),
       "force -> due regardless of cadence")
    # transient back-off: pending with a failed attempt waits streak * 30min
    p = dict(base, status="pending", last_refresh_ms=None, fail_streak=1,
             last_attempt_ms=now - 10 * 60_000)
    ok(not L.is_due(p, now), "pending after 1 transient miss 10 min ago -> waits (30 min back-off)")
    ok(L.is_due(dict(p, last_attempt_ms=now - 31 * 60_000), now), "…and is due after 31 min")
    ok(L.is_due(dict(p, fail_streak=100, last_attempt_ms=now - 7 * 3_600_000), now),
       "back-off is capped at 6h, so a long streak still retries")
    # ok with a transient miss AFTER the good read waits on the attempt clock
    o = dict(base, last_refresh_ms=now - 2 * day, fail_streak=1, last_attempt_ms=now - 60_000)
    ok(not L.is_due(o, now), "ok + overdue but a transient miss a minute ago -> backs off")
    # unavailable: normal cadence until the retire streak, then weekly
    u = dict(base, status="unavailable", fail_streak=1)
    ok(L.is_due(u, now), "unavailable x1, cadence elapsed -> retried on cadence")
    # (an unavailable read stamps both clocks together, like link_refreshed does)
    u3 = dict(base, status="unavailable", fail_streak=3,
              last_refresh_ms=now - 2 * day, last_attempt_ms=now - 2 * day)
    ok(not L.is_due(u3, now), "unavailable x3, 2 days ago -> not yet (weekly)")
    ok(L.is_due(dict(u3, last_refresh_ms=now - 8 * day, last_attempt_ms=now - 8 * day), now),
       "unavailable x3, 8 days ago -> due")

    links = [
        dict(base, tweet_id=1, last_refresh_ms=now - 2 * day),
        dict(base, tweet_id=2, status="pending", last_refresh_ms=None, last_attempt_ms=None),
        dict(base, tweet_id=3, last_refresh_ms=now - 3 * day),
        dict(base, tweet_id=4, last_refresh_ms=now - 100, force=1),
        dict(base, tweet_id=5, last_refresh_ms=now - 100),
        dict(base, tweet_id=6, status="removed"),
    ]
    order = [l["tweet_id"] for l in L.order_due(links, now)]
    ok(order == [4, 2, 3, 1],
       f"order: forced, then never-fetched, then longest-waiting; not-due and removed dropped ({order})")


def test_classify(ok):
    print("== links: classify_detail ==")
    tid = id_at(-60_000)
    ok(L.classify_detail(detail_payload(tid), tid, found=True) == ("ok", ""),
       "found -> ok")
    out, note = L.classify_detail(deleted_payload(), tid, found=False)
    ok(out == "unavailable" and "deleted" in note,
       f"X's 200-with-'No status found' -> unavailable ({note})")
    out, note = L.classify_detail(tombstone_payload(tid), tid, found=False)
    ok(out == "unavailable" and "deleted by the Post author" in note and "Learn more" not in note,
       f"a tombstone entry -> unavailable with X's sentence ({note})")
    out, note = L.classify_detail(unavailable_payload(tid, "Protected"), tid, found=False)
    ok(out == "unavailable" and "Protected" in note, f"TweetUnavailable(reason) -> unavailable ({note})")
    out, note = L.classify_detail(tombstone_payload(tid + 5), tid, found=False)
    ok(out == "transient", "a tombstone for a DIFFERENT id is not proof about ours -> transient")
    out, note = L.classify_detail({"data": {}}, tid, found=False)
    ok(out == "transient" and "shape" in note, f"an empty page -> transient, never unavailable ({note})")
    out, note = L.classify_detail({"errors": [{"message": "Over capacity"}]}, tid, found=False)
    ok(out == "transient" and "Over capacity" in note, "an unknown X error -> transient, with the message")


# --------------------------------------------------------------------------
# 2. engine.parse_detail
# --------------------------------------------------------------------------

def test_parse_detail(ok):
    print("== engine: parse_detail ==")
    tid = id_at(-120_000)
    det = parse_detail(FakeResponse(detail_payload(tid, likes=7, views="1234")), tid)
    ok(det.outcome == "ok" and det.tweet is not None and int(det.tweet.id) == tid,
       "the focal tweet is found and parsed")
    ok(det.page.result_ids == [tid], f"the result set is pinned to the focal id ({det.page.result_ids})")
    ok(len(det.page.tweets) >= 2 and tid + 1000 in det.page.tweets,
       "the reply is harvested as context but is NOT a result")
    ok(det.tweet.likeCount == 7 and det.tweet.viewCount == 1234,
       f"counters come off the object ({det.tweet.likeCount}, {det.tweet.viewCount})")
    ok(det.entry is not None and det.entry.get("entryId") == f"tweet-{tid}",
       "the focal entry rides along for raw_entry_json")
    ok(det.page.collected_ms > 0 and det.page.account == "alice" and det.page.rl_remaining == 47,
       "collected_ms, account and rate-limit budget come from the response like any page")
    det = parse_detail(FakeResponse(deleted_payload()), tid)
    ok(det.outcome == "unavailable" and det.tweet is None,
       "a deleted post parses to unavailable with no tweet")
    det = parse_detail(FakeResponse(tombstone_payload(tid)), tid)
    ok(det.outcome == "unavailable" and det.page.result_ids == [],
       "a tombstone parses to unavailable with an empty result set")


# --------------------------------------------------------------------------
# 3. store
# --------------------------------------------------------------------------

async def _store(tmp):
    st = Store(str(pathlib.Path(tmp) / "results.db"))
    await st.open()
    return st


async def run_store(tmp, ok):
    print("== store: kind='links' ==")
    st = await _store(tmp)
    tid = 1789000000000000001
    try:
        bad = await st.create_watchlist(1, "x", "reels")
        ok("error" in bad and "links" in bad["error"], "the kind validator names 'links'")
        made = await st.create_watchlist(1, "Campaign A", "links")
        ok("error" not in made and made["kind"] == "links", f"a links watchlist is created ({made})")
        wid = made["watchlist_id"]
        s = st.db.execute("SELECT * FROM streams WHERE label = ?", (f"wl:{wid}:0",)).fetchone()
        ok(s is not None and s["query"] == "" and s["list_id"] is None and s["watched"] == 0,
           "it compiles to ONE stream with an empty query and watched=0")
        ok(st.db.execute("SELECT 1 FROM project_streams WHERE project_id = 1 AND stream_id = ?",
                         (s["stream_id"],)).fetchone() is not None,
           "…attached to the project, so its posts join the project like any other")
        pollable = st.db.execute(
            "SELECT label FROM streams WHERE (watched = 1 OR tg_enabled = 1) AND paused = 0 "
            "AND (query != '' OR list_id IS NOT NULL)").fetchall()
        ok(all(r["label"] != f"wl:{wid}:0" for r in pollable),
           "the watcher's discovery query never selects it (nothing polls a links stream)")

        r = await st.add_links_text(wid, f"https://x.com/a/status/{tid}\n"
                                         f"https://instagram.com/reel/zzz\n"
                                         f"x.com/i/web/status/{tid + 1}?s=20\n"
                                         f"https://x.com/a/status/{tid}")
        ok(r["added"] == 2 and r["found"] == 2 and r["skipped"] == 1,
           f"paste: 2 links added, a duplicate collapsed, a non-X link skipped ({r})")
        r = await st.add_links_text(wid, f"https://x.com/a/status/{tid}")
        ok(r["added"] == 0 and r["existing"] == 1, "adding an existing link changes nothing")
        r = await st.add_links(999, [(1, "u")])
        ok("error" in r, "adding to a missing watchlist is refused")
        hw = await st.create_watchlist(1, "Handles", "query")
        r = await st.add_links(hw["watchlist_id"], [(1, "u")])
        ok("error" in r and "handles" in r["error"], "adding links to a handles watchlist is refused")

        now = int(time.time() * 1000)
        due = await st.links_due(now)
        ok([d["tweet_id"] for d in due] == [tid, tid + 1] and all(d["status"] == "pending" for d in due),
           "both new links are due at once (pending)")
        ok(due[0]["refresh_every_s"] == 86400 and due[0]["project_id"] == 1,
           "a due row carries its watchlist's cadence and project")

        await st.link_refreshed(wid, tid, "ok", "", now)
        await st.link_refreshed(wid, tid + 1, "transient", "no account", now)
        row = st.db.execute("SELECT * FROM watchlist_links WHERE tweet_id = ?", (tid,)).fetchone()
        ok(row["status"] == "ok" and row["refresh_count"] == 1 and row["fail_streak"] == 0
           and row["last_refresh_ms"] == now, "ok: status ok, count 1, streak 0, refresh clock set")
        row = st.db.execute("SELECT * FROM watchlist_links WHERE tweet_id = ?", (tid + 1,)).fetchone()
        ok(row["status"] == "pending" and row["refresh_count"] == 0 and row["fail_streak"] == 1
           and row["last_refresh_ms"] is None and row["last_attempt_ms"] == now
           and row["status_note"] == "no account",
           "transient: still pending, streak 1, attempt clock set, refresh clock untouched, note kept")
        ok(await st.links_due(now + 1000) == [], "nothing is due a second later (one refreshed, one backing off)")
        ok([d["tweet_id"] for d in await st.links_due(now + 31 * 60_000)] == [tid + 1],
           "the transient one is due again after its back-off")
        ok([d["tweet_id"] for d in await st.links_due(now + 86_400_001)] == [tid + 1, tid],
           "after a day both are due, never-fetched first")

        await st.link_refreshed(wid, tid + 1, "unavailable", "deleted", now)
        row = st.db.execute("SELECT * FROM watchlist_links WHERE tweet_id = ?", (tid + 1,)).fetchone()
        ok(row["status"] == "unavailable" and row["fail_streak"] == 2 and row["refresh_count"] == 1
           and row["last_refresh_ms"] == now,
           "unavailable: counts as a refresh (it learned something), streak grows")

        q = await st.request_links_refresh(wid)
        ok(q["queued"] == 2 and len(await st.links_due(now + 1000)) == 2,
           "Refresh now forces every live link due")
        await st.link_refreshed(wid, tid, "ok", "", now + 2000)
        ok(st.db.execute("SELECT force FROM watchlist_links WHERE tweet_id = ?",
                         (tid,)).fetchone()["force"] == 0, "a fetch clears the force flag")

        ok((await st.set_links_refresh(wid, "12h"))["refresh_every_s"] == 43200
           and (await st.set_links_refresh(wid, 7200))["refresh_every_s"] == 7200
           and "error" in await st.set_links_refresh(wid, 10)
           and "error" in await st.set_links_refresh(wid, "soon")
           and "error" in await st.set_links_refresh(hw["watchlist_id"], "24h"),
           "refresh cadence: named or seconds, one hour floor, links-only")

        # pause = the one stream's paused flag, honoured by links_due
        st.db.execute("UPDATE streams SET paused = 1 WHERE label = ?", (f"wl:{wid}:0",))
        ok(await st.links_due(now + 86_400_001) == [], "a paused list is never due")
        st.db.execute("UPDATE streams SET paused = 0 WHERE label = ?", (f"wl:{wid}:0",))

        r = await st.remove_link(wid, tid + 1)
        ok(r.get("removed") and r["tweet_id"] == str(tid + 1), "remove by hand -> status removed")
        ok("error" in await st.remove_link(wid, tid + 1), "removing twice is refused")
        ok([d["tweet_id"] for d in await st.links_due(now + 86_400_001)] == [tid],
           "a removed link is never fetched again")
        await st.link_refreshed(wid, tid + 1, "ok", "", now + 5000)
        ok(st.db.execute("SELECT status, status_note FROM watchlist_links WHERE tweet_id = ?",
                         (tid + 1,)).fetchone()[:] == ("removed", "removed by hand"),
           "a fetch that lands after a removal cannot resurrect the link")
        r = await st.add_links(wid, [(tid + 1, f"https://x.com/a/status/{tid + 1}")])
        ok(r["revived"] == 1 and st.db.execute(
            "SELECT status, fail_streak FROM watchlist_links WHERE tweet_id = ?",
            (tid + 1,)).fetchone()[:] == ("pending", 0),
           "adding a removed link revives it to pending with a clean streak")

        summ = await st.links_summary(wid)
        ok(summ["total"] == 2 and summ["ok"] == 1 and summ["pending"] == 1 and summ["removed"] == 0,
           f"summary counts by status ({summ})")
        wl = [w for w in await st.watchlists(1) if w["watchlist_id"] == wid][0]
        ok(wl["kind"] == "links" and wl["links"]["total"] == 2 and wl["paused"] is False
           and wl["sheet"] is None and wl["refresh_every_s"] == 7200,
           "watchlists() carries the links summary, pause state, cadence and (no) sheet")

        # the snapshot before any post exists: link rows with fetched=false
        snap = await st.links_snapshot(1)
        ok(snap["total"] == 2 and all(r["fetched"] is False and r["like_count"] is None
                                      for r in snap["rows"]),
           "snapshot lists every link even before its post was fetched (fetched=false, counters null)")
        ok(all(isinstance(r["tweet_id"], str) for r in snap["rows"]),
           "ids are strings on the wire (R2)")
        ok(all(r["url"] and r["url"].startswith(("https://x.com", "x.com")) for r in snap["rows"])
           and all(r["post_url"] is None for r in snap["rows"]),
           "the snapshot keeps the link's own url (as written); post_url is null until fetched")
        ok((await st.links_snapshot(1, status="ok"))["total"] == 1
           and (await st.links_snapshot(1, watchlist_id=wid + 100))["total"] == 0
           and (await st.links_snapshot(2))["total"] == 0,
           "snapshot filters by status and watchlist, and is project-scoped")

        # sheet binding: tab -> watchlist keyed on gid
        print("== store: sheets and tabs ==")
        b = await st.bind_link_sheet(1, "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrSt/edit#gid=0")
        ok(b["sheet_id"] == "1AbCdEfGhIjKlMnOpQrSt" and b["sync_every_s"] == 600,
           "binding parses the id out of the URL")
        b2 = await st.bind_link_sheet(1, "1AbCdEfGhIjKlMnOpQrSt")
        ok(b2["link_sheet_id"] == b["link_sheet_id"], "binding the same sheet twice is one binding")
        ok("error" in await st.bind_link_sheet(1, "nope"), "a non-id is refused")
        ok([s["link_sheet_id"] for s in await st.link_sheets_due(now)] == [b["link_sheet_id"]],
           "a never-synced sheet is due")
        await st.link_sheet_synced(b["link_sheet_id"], title="Reach", now_ms=now)
        ok(await st.link_sheets_due(now + 1000) == [] and
           [s["link_sheet_id"] for s in await st.link_sheets_due(now + 600_001)] == [b["link_sheet_id"]],
           "…then due again after sync_every_s")
        await st.request_sheet_sync(b["link_sheet_id"])
        ok(len(await st.link_sheets_due(now + 1000)) == 1, "Sync now makes it due at once")

        w1 = await st.links_watchlist_for_tab(b["link_sheet_id"], 100, "Campaign A")
        ok(w1["name"] == "Campaign A (2)" and w1["sheet_gid"] == 100 and w1["sheet_tab"] == "Campaign A",
           "a tab whose name collides with a pasted list gets a suffix, and remembers the tab")
        w1b = await st.links_watchlist_for_tab(b["link_sheet_id"], 100, "Campaign A")
        ok(w1b["watchlist_id"] == w1["watchlist_id"], "the same gid finds the same watchlist")
        w1c = await st.links_watchlist_for_tab(b["link_sheet_id"], 100, "Campaign A — Sept")
        ok(w1c["watchlist_id"] == w1["watchlist_id"] and w1c["name"] == "Campaign A — Sept",
           "a renamed tab renames the watchlist (gid is the key)")
        w2 = await st.links_watchlist_for_tab(b["link_sheet_id"], 200, "Campaign B")
        ok(w2["watchlist_id"] != w1["watchlist_id"] and w2["name"] == "Campaign B",
           "a new gid is a new watchlist")
        wd = await st.links_watchlist_for_tab(b["link_sheet_id"], 300, "6/9/26", day="2026-09-06")
        ok(wd["sheet_day"] == "2026-09-06" and wd["name"] == "6/9/26", "a day tab records its date")
        wd = await st.links_watchlist_for_tab(b["link_sheet_id"], 300, "6/9/26", day=None)
        ok(wd["sheet_day"] is None, "…and clears it if the tab stops being a date")
        await st.links_watchlist_for_tab(b["link_sheet_id"], 300, "6/9/26", day="2026-09-06")
        r = await st.add_links(wd["watchlist_id"], [(77, "https://x.com/a/status/77", 5, "National X Influencers")], via="sheet")
        snap = await st.links_snapshot(1, watchlist_id=wd["watchlist_id"])
        ok(r["added"] == 1 and snap["rows"][0]["day"] == "2026-09-06"
           and snap["rows"][0]["section"] == "National X Influencers",
           "the snapshot row carries the tab's day and the link's section")
        await st.add_links(wd["watchlist_id"], [(77, "https://x.com/a/status/77", 9, "Counter Comments Links")], via="sheet")
        ok((await st.links_snapshot(1, watchlist_id=wd["watchlist_id"]))["rows"][0]["section"] == "Counter Comments Links",
           "a link that moved sections in the sheet is re-sectioned on the next sync")
        wl = [w for w in await st.watchlists(1) if w["watchlist_id"] == w2["watchlist_id"]][0]
        ok(wl["sheet"] and wl["sheet"]["sheet_id"] == "1AbCdEfGhIjKlMnOpQrSt",
           "watchlists() shows the bound sheet on a tab-backed list")

        r = await st.add_links(w2["watchlist_id"], [(5, "https://x.com/a/status/5", 2),
                                                    (6, "https://x.com/b/status/6", 3)], via="sheet")
        m = await st.add_links(w2["watchlist_id"], [(7, "https://x.com/c/status/7")], via="manual")
        ok(r["added"] == 2 and m["added"] == 1, "sheet and manual adds live on one list")
        gone = await st.sync_removed_links(w2["watchlist_id"], {5})
        rows = {r["tweet_id"]: r["status"] for r in st.db.execute(
            "SELECT tweet_id, status FROM watchlist_links WHERE watchlist_id = ?",
            (w2["watchlist_id"],))}
        ok(gone == 1 and rows == {5: "pending", 6: "removed", 7: "pending"},
           f"a sheet row that left is marked removed; the manual add is untouched ({rows})")

        u = await st.unbind_link_sheet(b["link_sheet_id"])
        ok(u.get("watchlists_kept") and st.db.execute(
            "SELECT link_sheet_id, sheet_gid FROM watchlists WHERE watchlist_id = ?",
            (w2["watchlist_id"],)).fetchone()[:] == (None, None)
           and st.db.execute("SELECT COUNT(*) c FROM watchlist_links WHERE watchlist_id = ?",
                             (w2["watchlist_id"],)).fetchone()["c"] == 3,
           "unbinding a sheet keeps its watchlists and links as pasted-only lists")

        d = await st.delete_watchlist(w2["watchlist_id"])
        ok(d["removed"] and st.db.execute(
            "SELECT COUNT(*) c FROM watchlist_links WHERE watchlist_id = ?",
            (w2["watchlist_id"],)).fetchone()["c"] == 0,
           "deleting a links watchlist drops its link rows (the instruction), keeps tweets")

        print("== store: a deleted project takes its sheets and links with it ==")
        p2 = await st.create_project("Second")
        pid2 = p2["project_id"]
        b3 = await st.bind_link_sheet(pid2, "1ZyXwVuTsRqPoNmLkJiHgF")
        w3 = await st.links_watchlist_for_tab(b3["link_sheet_id"], 5, "T")
        await st.add_links(w3["watchlist_id"], [(9001, "https://x.com/a/status/9001")], via="sheet")
        dp = await st.delete_project(pid2)
        ok("error" not in dp and st.db.execute(
            "SELECT COUNT(*) c FROM link_sheets WHERE project_id = ?", (pid2,)).fetchone()["c"] == 0
           and st.db.execute("SELECT COUNT(*) c FROM watchlist_links WHERE watchlist_id = ?",
                             (w3["watchlist_id"],)).fetchone()["c"] == 0
           and await st.link_sheets_due(now + 10 ** 9) == [],
           f"delete_project removes the bound sheet and the link rows — nothing left to sync ({dp})")

        print("== store: retention never prunes a tracked post ==")
        old_id = 1_000_000_000_000_000   # a 2018-era snowflake
        st.db.execute(
            "INSERT INTO tweets(tweet_id, created_ms, collected_at, collected_ms, last_seen_at, "
            "lag_ms, source) VALUES(?,?,?,?,?,0,'result')",
            (old_id, 1_500_000_000_000, "x", now, "x"))
        st.db.execute(
            "INSERT INTO tweets(tweet_id, created_ms, collected_at, collected_ms, last_seen_at, "
            "lag_ms, source) VALUES(?,?,?,?,?,0,'result')",
            (old_id + 1, 1_500_000_000_000, "x", now, "x"))
        await st.add_links(wid, [(old_id, "https://x.com/a/status/old")])
        st.retention_days = 30
        stats = await st.maintain()
        kept = st.db.execute("SELECT 1 FROM tweets WHERE tweet_id = ?", (old_id,)).fetchone()
        gone = st.db.execute("SELECT 1 FROM tweets WHERE tweet_id = ?", (old_id + 1,)).fetchone()
        ok(kept is not None and gone is None and stats.get("tweets_pruned", 0) >= 1,
           "retention prunes the untracked old post and keeps the one on a links watchlist")
        st.retention_days = 0
    finally:
        await st.close()


# --------------------------------------------------------------------------
# 4. collector.refresh_links against a fake engine
# --------------------------------------------------------------------------

class FakeDetailEngine:
    """tweet_detail() answers from a script: id -> payload | Exception | None.
    `mutate(detail)` lets a test alter the parsed tweet before the store sees it."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.mutate = None

    async def tweet_detail(self, tweet_id):
        self.calls.append(int(tweet_id))
        what = self.script.get(int(tweet_id))
        if isinstance(what, Exception):
            raise what
        if what is None:
            rep = None
        else:
            rep = FakeResponse(what)
        det = await Engine(_FakeApi(rep)).tweet_detail(tweet_id)
        if self.mutate and det.tweet is not None:
            self.mutate(det)
        return det


class _FakeApi:
    def __init__(self, rep):
        self.rep = rep

    async def tweet_details_raw(self, twid, kv=None):
        return self.rep


async def run_collector(tmp, ok):
    print("== collector: refresh_links ==")
    import collector as C

    st = await _store(tmp)
    try:
        made = await st.create_watchlist(1, "Reach", "links")
        wid = made["watchlist_id"]
        t_ok, t_gone, t_flaky = id_at(-3_600_000), id_at(-7_200_000), id_at(-10_800_000)
        await st.add_links(wid, [(t_ok, "u1"), (t_gone, "u2"), (t_flaky, "u3")])
        C.LINKS_GAP_S = 0.01

        eng = FakeDetailEngine({t_ok: detail_payload(t_ok, likes=10, views="500", followers=1000),
                                t_gone: deleted_payload(),
                                t_flaky: None})
        col = C.Collector(eng, st, [], max_concurrency=2, log=lambda m: None)
        now = int(time.time() * 1000)
        batch = await st.links_due(now)
        summ = await col.refresh_links(batch)
        ok(summ["fetched"] == 3 and summ["ok"] == 1 and summ["unavailable"] == 1 and summ["transient"] == 1,
           f"one pass: ok / unavailable / transient each recorded ({summ})")
        ok(sorted(eng.calls) == sorted([t_ok, t_gone, t_flaky]), "one TweetDetail per link")

        row = st.db.execute("SELECT * FROM tweets WHERE tweet_id = ?", (t_ok,)).fetchone()
        ok(row is not None and row["like_count"] == 10 and row["view_count"] == 500
           and row["author_followers"] == 1000,
           "the found post is one row in `tweets` with its counters")
        first_collected = row["collected_ms"]
        sid = st.db.execute("SELECT stream_id FROM streams WHERE label = ?", (f"wl:{wid}:0",)).fetchone()[0]
        ok(st.db.execute("SELECT 1 FROM tweet_hits WHERE stream_id = ? AND tweet_id = ?",
                         (sid, t_ok)).fetchone() is not None,
           "…with a tweet_hits edge on the links stream, so it is the project's")
        ok(st.db.execute("SELECT COUNT(*) c FROM tweets WHERE tweet_id = ?",
                         (t_ok + 1000,)).fetchone()["c"] == 0,
           "the reply X sent around it was NOT stored — a links refresh writes one post")
        ok(st.db.execute("SELECT 1 FROM tweets WHERE tweet_id = ?", (t_gone,)).fetchone() is None,
           "a deleted post writes nothing to the corpus")
        st_rows = {r["tweet_id"]: dict(r) for r in st.db.execute(
            "SELECT * FROM watchlist_links WHERE watchlist_id = ?", (wid,))}
        ok(st_rows[t_ok]["status"] == "ok" and st_rows[t_gone]["status"] == "unavailable"
           and "deleted" in st_rows[t_gone]["status_note"]
           and st_rows[t_flaky]["status"] == "pending" and st_rows[t_flaky]["fail_streak"] == 1
           and "no account" in st_rows[t_flaky]["status_note"],
           "each link row records its outcome and note")
        poll = st.db.execute("SELECT * FROM polls WHERE stream_id = ? ORDER BY poll_id DESC",
                             (sid,)).fetchone()
        ok(poll["kind"] == "links" and poll["pages"] == 3 and poll["results"] == 1
           and poll["new_tweets"] == 1 and poll["orphans"] == 1 and poll["finished_ms"]
           and poll["account"] == "alice" and poll["rl_remaining"] == 47,
           f"one poll row per watchlist per pass, kind='links', with the account and budget")
        ok(st.db.execute("SELECT last_refresh_ms FROM watchlists WHERE watchlist_id = ?",
                         (wid,)).fetchone()[0], "the watchlist's own last_refresh_ms is stamped")

        # Second pass a day later: counters MOVE, collected_ms does NOT.
        eng.script[t_ok] = detail_payload(t_ok, likes=25, views="9000", followers=1200)
        eng.script[t_gone] = deleted_payload()
        eng.script[t_flaky] = detail_payload(t_flaky, likes=1, views="2")
        batch = await st.links_due(now + 86_400_001)
        ok(sorted(l["tweet_id"] for l in batch) == sorted([t_ok, t_gone, t_flaky]),
           "a day later all three are due again")
        summ = await col.refresh_links(batch)
        row = st.db.execute("SELECT * FROM tweets WHERE tweet_id = ?", (t_ok,)).fetchone()
        ok(row["like_count"] == 25 and row["view_count"] == 9000 and row["author_followers"] == 1200,
           "counters and followers overwrite in place")
        ok(row["collected_ms"] == first_collected,
           "collected_ms is frozen at first sight (WATCH_TOWER.md R3) — no re-delivery")
        ok(st.db.execute("SELECT refresh_count FROM watchlist_links WHERE tweet_id = ?",
                         (t_ok,)).fetchone()[0] == 2, "refresh_count reaches 2")
        ok(st.db.execute("SELECT status, refresh_count FROM watchlist_links WHERE tweet_id = ?",
                         (t_flaky,)).fetchone()[:] == ("ok", 1),
           "the flaky one recovered to ok on its next attempt")
        ok(st.db.execute("SELECT COUNT(*) c FROM tweet_hits WHERE stream_id = ? AND tweet_id = ?",
                         (sid, t_ok)).fetchone()["c"] == 1, "the hit edge is not duplicated")

        # A read that cannot see the author's follower count must not blank
        # the number we have: null is "unknown", not zero (R5).
        eng.script[t_ok] = detail_payload(t_ok, likes=30, views="9500", followers=1300)

        def _blank_followers(det):
            det.tweet.user.followersCount = None
        eng.mutate = _blank_followers
        batch = [l for l in await st.links_due(now + 2 * 86_400_001) if l["tweet_id"] == t_ok]
        ok(len(batch) == 1, "the ok link is due again another day later")
        await col.refresh_links(batch)
        eng.mutate = None
        row = st.db.execute("SELECT * FROM tweets WHERE tweet_id = ?", (t_ok,)).fetchone()
        ok(row["like_count"] == 30 and row["author_followers"] == 1200,
           "a null followers read keeps the last known number (COALESCE), counters still move")

        # The rate guard must not read a links pass as the search budget.
        import guard as _guard
        st.db.execute("INSERT INTO streams(label, query, tab, watermarked, created_at) "
                      "VALUES('s1', 'from:x', 'Latest', 1, 'x')")
        s1 = st.db.execute("SELECT stream_id FROM streams WHERE label = 's1'").fetchone()[0]
        pid_s = await st.begin_poll(s1, kind="poll")
        await st.finish_poll(pid_s, pages=1, rl_limit=50, rl_remaining=40, rl_reset=int(time.time()) + 600)
        class _Cfg:
            db_results = pathlib.Path(tmp) / "results.db"
        b = _guard._budget(_Cfg(), "search")
        ok(b.get("limit") == 50 and b.get("remaining") == 40,
           f"guard reads the search poll's budget ({b})")
        # (the links passes above recorded rl 50/47 on kind='links' AFTER it —
        # the guard must still answer with the search poll)
        pid_l = await st.begin_poll(sid, kind="links")
        await st.finish_poll(pid_l, pages=20, rl_limit=150, rl_remaining=12, rl_reset=int(time.time()) + 600)
        b = _guard._budget(_Cfg(), "search")
        ok(b.get("limit") == 50 and b.get("remaining") == 40 and b.get("recent_requests") == 1,
           f"…and ignores a later links pass on the TweetDetail bucket ({b})")

        # An engine exception is transient for that link and does not abort the pass.
        eng.script[t_gone] = RuntimeError("boom")
        eng.script[t_flaky] = detail_payload(t_flaky, likes=2, views="3")
        st.db.execute("UPDATE watchlist_links SET force = 1 WHERE watchlist_id = ?", (wid,))
        summ = await col.refresh_links(await st.links_due(now))
        r = st.db.execute("SELECT status, status_note FROM watchlist_links WHERE tweet_id = ?",
                          (t_gone,)).fetchone()
        ok(summ["fetched"] == 3 and r["status"] == "unavailable" and "boom" in r["status_note"],
           "an exception on one link is recorded as transient on that row; the pass goes on")

        # The global pause stops the trickle mid-batch.
        await st.set_collection_paused(True)
        eng.calls.clear()
        st.db.execute("UPDATE watchlist_links SET force = 1 WHERE watchlist_id = ?", (wid,))
        summ = await col.refresh_links(await st.links_due(now))
        ok(summ["fetched"] == 0 and eng.calls == [], "the global pause stops a pass before any fetch")
        await st.set_collection_paused(False)

        print("== collector: one fetch per post per pass, across lists ==")
        made2 = await st.create_watchlist(1, "Master", "links")
        await st.add_links(made2["watchlist_id"], [(t_ok, "u1"), (t_flaky, "u3")])
        eng.calls.clear()
        eng.script[t_ok] = detail_payload(t_ok, likes=40, views="9600")
        eng.script[t_flaky] = detail_payload(t_flaky, likes=3, views="4")
        st.db.execute("UPDATE watchlist_links SET force = 1 WHERE tweet_id IN (?, ?)", (t_ok, t_flaky))
        batch = [l for l in await st.links_due(now, 50) if l["tweet_id"] in (t_ok, t_flaky)]
        summ = await col.refresh_links(batch)
        ok(summ["fetched"] == 4 and summ["reused"] == 2 and sorted(eng.calls) == sorted([t_ok, t_flaky]),
           f"two lists sharing two posts -> two TweetDetail calls, four rows updated ({summ}, {eng.calls})")
        ok(all(r[0] == "ok" for r in st.db.execute(
            "SELECT status FROM watchlist_links WHERE tweet_id = ? ", (t_ok,))),
           "both lists' rows record the outcome")
        sid2 = st.db.execute("SELECT stream_id FROM streams WHERE label = ?",
                             (f"wl:{made2['watchlist_id']}:0",)).fetchone()[0]
        ok(st.db.execute("SELECT 1 FROM tweet_hits WHERE stream_id = ? AND tweet_id = ?",
                         (sid2, t_ok)).fetchone() is not None,
           "the reused answer still gives the second list its own hit edge")
        await st.delete_watchlist(made2["watchlist_id"])

        print("== collector: the third clock in run_forever ==")
        eng.calls.clear()
        st.db.execute("UPDATE watchlist_links SET force = 1 WHERE watchlist_id = ?", (wid,))
        C.LINKS_TICK_S = 0.2
        col2 = C.Collector(eng, st, [], max_concurrency=2, log=lambda m: None)
        await col2.run_forever(duration=2.0)
        ok(len(eng.calls) == 3, f"run_forever with no poll streams still drove the links pass ({eng.calls})")
        ok(col2._http is None, "no sheet was bound, so no HTTP client was ever opened")
    finally:
        C.LINKS_GAP_S = 7.0
        C.LINKS_TICK_S = 30.0
        await st.close()


# --------------------------------------------------------------------------
# 5. links.sync_sheet against a fake Sheets API
# --------------------------------------------------------------------------

class _Rep:
    def __init__(self, status, payload, url="https://x.com/i/web/status/0"):
        self.status_code = status
        self._p = payload
        self.url = url
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class FakeSheetsClient:
    """Answers the two Sheets API GETs and t.co HEADs from a dict of tabs."""

    def __init__(self, title, tabs, tco=None, fail=None):
        self.title, self.tabs, self.tco, self.fail = title, tabs, tco or {}, fail
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None, follow_redirects=False):
        self.calls.append(url)
        if self.fail:
            return _Rep(self.fail, {"error": {"message": "The caller does not have permission"}})
        if "/values/" in url:
            import urllib.parse as _up
            rng = _up.unquote(url.rsplit("/values/", 1)[-1])
            ok_encoded = "%" in url.rsplit("/values/", 1)[-1] or "'" not in url
            if not ok_encoded:
                return _Rep(400, {"error": {"message": f"Unable to parse range: {rng}"}})
            name = rng.split("!")[0].strip("'").replace("''", "'")
            for gid, (title, hidden, values) in self.tabs.items():
                if title == name:
                    return _Rep(200, {"values": values})
            return _Rep(400, {"error": {"message": f"Unable to parse range: {rng}"}})
        if "t.co" in url:
            return _Rep(200, {}, url=self.tco.get(url.replace("https://", ""), url))
        return _Rep(200, {"properties": {"title": self.title},
                          "sheets": [{"properties": {"sheetId": gid, "title": t, "hidden": h}}
                                     for gid, (t, h, _) in self.tabs.items()]})

    async def head(self, url, follow_redirects=False, timeout=None):
        self.calls.append("HEAD " + url)
        return _Rep(200, {}, url=self.tco.get(url.replace("https://", ""), url))


async def run_sync(tmp, ok):
    print("== links: sync_sheet ==")
    import sheets as _sheets

    st = await _store(tmp)
    orig_creds, orig_token = _sheets.load_creds, _sheets.access_token
    _sheets.load_creds = lambda: {"client_email": "svc@example.iam", "private_key": "x"}

    async def _tok(client, creds):
        return "tok", ""
    _sheets.access_token = _tok
    try:
        a, b, c = id_at(-1000), id_at(-2000), id_at(-3000)
        tabs = {
            66: ("6/9/26", False, [["Posts", "views", "Likes"], ["National X Influencers"],
                                    [f"https://x.com/n/status/{c + 7}", "30,000", "60,000"]]),
            55: ("Q3/Q4 — what? #1 100%", False, [[f"https://x.com/q/status/{c + 99}"]]),
            11: ("Campaign A", False, [["Link", "Note"], [f"https://x.com/a/status/{a}", "d1"],
                                       ["https://t.co/abc12", "short"], ["https://instagram.com/p/1", ""]]),
            22: ("Campaign B", False, [[f"x.com/b/status/{b}"], [f"https://x.com/b/status/{b}"]]),
            33: ("Hidden", True, [[f"https://x.com/h/status/{c + 50}"]]),
            44: ("Empty", False, []),
        }
        client = FakeSheetsClient("Reach tracker", tabs, tco={"t.co/abc12": f"https://x.com/c/status/{c}"})
        bound = await st.bind_link_sheet(1, "1AbCdEfGhIjKlMnOpQrSt")
        res = await L.sync_sheet(st, client, bound, now_ms=1_000)
        ok(res["error"] == "" and res["tabs"] == 5, f"every non-hidden tab is read, hidden skipped ({res['tabs']}: {res['error']})")
        ok(res["found"] == 5 and res["added"] == 5 and res["skipped"] == 1,
           f"5 links found across tabs (one via t.co), the Instagram cell skipped ({res})")
        names = {w["tab"]: w for w in res["watchlists"]}
        ok(set(names) == {"Campaign A", "Campaign B", "Empty", "Q3/Q4 — what? #1 100%", "6/9/26"},
           f"one watchlist per tab, a title with ? / # % included — the range is URL-encoded ({sorted(names)})")
        ok(names["6/9/26"]["day"] == "2026-09-06" and names["Campaign A"]["day"] is None,
           "a date-named tab carries its day; a named tab does not")
        drow = st.db.execute("SELECT section FROM watchlist_links WHERE watchlist_id = ?",
                             (names["6/9/26"]["watchlist_id"],)).fetchone()
        ok(drow and drow["section"] == "National X Influencers", "the link under a heading carries the section")
        ok(names["Campaign A"]["found"] == 2 and names["Campaign B"]["found"] == 1,
           "the duplicate in Campaign B collapsed; t.co resolved into Campaign A")
        sheet = await st.link_sheet(bound["link_sheet_id"])
        ok(sheet["title"] == "Reach tracker" and sheet["last_sync_ms"] == 1_000 and sheet["last_error"] is None,
           "the sheet row records title, sync time, no error")
        wa = names["Campaign A"]["watchlist_id"]
        rows = {r["tweet_id"]: dict(r) for r in st.db.execute(
            "SELECT * FROM watchlist_links WHERE watchlist_id = ?", (wa,))}
        ok(rows[a]["sheet_row"] == 2 and rows[a]["added_via"] == "sheet" and rows[c]["url"].endswith(f"/status/{c}"),
           "sheet links carry their row and the resolved URL")
        ok(any(x.startswith("HEAD") for x in client.calls), "t.co was resolved with a HEAD")

        # Second sync: a row left, a row arrived, a manual add is untouched.
        await st.add_links(wa, [(a + 7, "https://x.com/m/status/x")], via="manual")
        tabs[11] = ("Campaign A", False, [[f"https://x.com/a/status/{a + 1}"], ["https://t.co/abc12"]])
        res = await L.sync_sheet(st, client, bound, now_ms=2_000)
        rows = {r["tweet_id"]: r["status"] for r in st.db.execute(
            "SELECT tweet_id, status FROM watchlist_links WHERE watchlist_id = ?", (wa,))}
        ok(res["added"] == 1 and res["removed"] == 1 and rows[a] == "removed" and rows[a + 1] == "pending"
           and rows[c] == "pending" and rows[a + 7] == "pending",
           f"second sync: the row that left is marked removed, the new one added, manual kept ({rows})")
        # A renamed tab keeps its watchlist.
        tabs[22] = ("Campaign B (Sept)", False, [[f"https://x.com/b/status/{b}"]])
        res = await L.sync_sheet(st, client, bound, now_ms=3_000)
        wb = names["Campaign B"]["watchlist_id"]
        ok(st.db.execute("SELECT name FROM watchlists WHERE watchlist_id = ?", (wb,)).fetchone()[0]
           == "Campaign B (Sept)", "a renamed tab renames its watchlist and keeps its links")

        # Permission failure: recorded on the sheet, nothing changes.
        client.fail = 403
        res = await L.sync_sheet(st, client, bound, now_ms=4_000)
        sheet = await st.link_sheet(bound["link_sheet_id"])
        ok("permission" in res["error"] and "share the sheet with svc@example.iam" in res["error"]
           and sheet["last_error"] == res["error"] and sheet["last_sync_ms"] == 4_000,
           f"a 403 names the service account to share with, and is recorded ({res['error'][:60]}…)")
        client.fail = None
        # No credentials: same, with the .env hint.
        _sheets.load_creds = lambda: None
        res = await L.sync_sheet(st, client, bound, now_ms=5_000)
        ok(_sheets.CREDS_ENV in res["error"], "missing credentials are reported by env var name")
    finally:
        _sheets.load_creds, _sheets.access_token = orig_creds, orig_token
        await st.close()


# --------------------------------------------------------------------------
# 6. web.py — routing, allowlist, project-locked keys
# --------------------------------------------------------------------------

class _FakeHandler:
    """Enough of BaseHTTPRequestHandler for Handler._require_auth."""

    def __init__(self, method, path, key):
        self.command = method
        self.path = path
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.sent = None
        self._via_api_key = False

    def _send(self, status, body):
        self.sent = (status, body)

    def _authed(self):
        return False


def test_web(ok):
    print("== web: /api/links and project-locked keys ==")
    import web

    ok("/api/links" in web.API_KEY_READ_PATHS and "/api/links" not in web.API_KEY_WRITE_PATHS,
       "/api/links is key-readable and never key-writable")
    for p in ("/api/watchlists/links", "/api/watchlists/links/refresh",
              "/api/watchlists/links/interval", "/api/links/sheets",
              "/api/links/sheets/sync", "/api/links/sheets/remove"):
        ok(p not in web.API_KEY_READ_PATHS and p not in web.API_KEY_WRITE_PATHS,
           f"{p} is closed to an UNSCOPED key")
    # …but binding a sheet is open to a PROJECT-LOCKED key, deliberately
    # (REPORT_TOOL_PLAN.md Part 2.2): the report tool hands us the sheet an
    # operator already pasted there, for its own project only.
    ok(web.API_KEY_SCOPED_WRITE_PATHS == {"/api/links/sheets"},
       "the scoped write set is exactly one path, and it is the sheet binding")
    ok("/api/projects" not in web.API_KEY_SCOPED_WRITE_PATHS,
       "a machine key still cannot CREATE a project — a duplicate project would "
       "silently split one campaign's history in two")
    ok("/api/project" in web.API_KEY_SCOPED_PATHS and "/api/project" in web.API_KEY_READ_PATHS,
       "the handshake is readable by both key kinds")
    src = pathlib.Path(web.__file__).read_text()
    ok('u.path == "/api/links"' in src and 'u.path == "/api/watchlists/links"' in src,
       "the new paths are routed")

    K1, K2 = "k_" + "a" * 40, "k_" + "b" * 40
    os.environ["API_KEYS"] = K1
    os.environ["API_KEYS_SCOPED"] = f"{K2}=7, junk, short=3, {K1}=notanumber"
    try:
        ok(web._scoped_keys() == {K2: 7}, f"API_KEYS_SCOPED parses key=project and skips junk ({web._scoped_keys()})")
        ok(web._valid_api_key(K2) and web._valid_api_key(K1) and not web._valid_api_key("k_" + "c" * 40),
           "a scoped key is a valid key; an unknown one is not")
        ok(web._key_scope(K2) == 7 and web._key_scope(K1) is None, "scope is 7 for the locked key, None for Watch-Tower's")

        def go(method, path, key):
            h = _FakeHandler(method, path, key)
            allowed = web.Handler._require_auth(h)
            return allowed, h.sent

        ok(go("GET", "/api/links?project=7", K2) == (True, None), "scoped key: GET /api/links?project=7 passes")
        ok(go("GET", "/api/tweets?project=7&limit=5", K2)[0], "…and /api/tweets for its own project")
        a, sent = go("GET", "/api/links?project=8", K2)
        ok(not a and sent[0] == 403 and "locked to project 7" in sent[1]["error"],
           "scoped key: another project is refused, naming its own")
        a, sent = go("GET", "/api/links", K2)
        ok(not a and sent[0] == 403, "scoped key: no ?project= is refused")
        a, sent = go("GET", "/api/projects", K2)
        ok(not a and sent[0] == 403 and sent[1]["allowed_get"] == sorted(web.API_KEY_SCOPED_PATHS),
           "scoped key: /api/projects is refused and the reply lists the four paths")
        a, sent = go("POST", "/api/fetch?project=7", K2)
        ok(not a and sent[0] == 403, "scoped key: POST /api/fetch is refused (no budget spend)")
        a, sent = go("POST", "/api/watchlists/links?project=7", K2)
        ok(not a and sent[0] == 403, "scoped key: cannot add links")
        h = _FakeHandler("POST", "/api/links/sheets", K2)
        ok(web.Handler._require_auth(h) and h.sent is None and h._key_project == 7,
           "scoped key: POST /api/links/sheets passes the door, carrying its project")
        ok(web._links_sheet_post({"project": 8, "sheet": "x"}, 7)[0] == 403,
           "…and the handler refuses another project named in the BODY, which the "
           "path allowlist cannot see")
        h2 = _FakeHandler("GET", "/api/links?project=7", K1)
        web.Handler._require_auth(h2)
        ok(h2._key_project is None,
           "an unscoped key carries no project, so the body check never binds it")
        a, sent = go("POST", "/api/links/sheets/sync", K2)
        ok(not a and sent[0] == 403,
           "scoped key: the other sheet routes stay shut (only the bind is granted)")
        ok(go("GET", "/api/links?project=8", K1) == (True, None), "an unscoped key still reads any project")
        ok(go("GET", "/api/projects", K1) == (True, None), "…and the whole read allowlist")
        a, sent = go("GET", "/api/links?project=7", "k_" + "z" * 40)
        ok(not a and sent[0] == 401, "an unknown key is 401")
    finally:
        os.environ.pop("API_KEYS", None)
        os.environ.pop("API_KEYS_SCOPED", None)

    # The snapshot handler's validation, without a database on disk.
    class Cfg:
        db_results = pathlib.Path("/nonexistent/results.db")
    orig = web._CFG
    web._CFG = Cfg()
    try:
        # These handlers answer (http_status, body): a consumer that cannot tell
        # "no such project" from "no links yet" from "your key is wrong" retries
        # the same broken URL forever.
        code, body = web._links_json({})
        ok(code == 400 and "error" in body, "/api/links without ?project= is 400, not 200")
        code, body = web._links_json({"project": "3"})
        ok(code == 200 and body == {"total": 0, "rows": [], "items": [],
                                    "limit": 500, "offset": 0},
           f"/api/links on a fresh install is an empty snapshot, not a 500 ({body})")
        ok(body["items"] == body["rows"],
           "…and even the empty envelope carries `items`, so their normalize_many() "
           "parses it instead of raising")
        code, body = web._links_json({"project": "3", "status": "lost"})
        ok(code == 400 and "error" in body, "an unknown status is refused with 400")
        code, body = web._project_json({})
        ok(code == 400 and "error" in body, "/api/project without ?project= is 400")
        ok("error" in web._links_post({}) and "error" in web._links_post({"watchlist_id": 1})
           and "error" in web._links_refresh({}),
           "the write validators refuse empty bodies")
        ok(web._links_sheet_post({"project": 1})[0] == 400,
           "…and the sheet binding refuses a body with no sheet URL")
    finally:
        web._CFG = orig


# --------------------------------------------------------------------------

def run(tmp, ok=_ok):
    """Every links test, in plan order. `tmp` is a scratch directory."""
    tmp = pathlib.Path(tmp)
    test_parsing(ok)
    test_planning(ok)
    test_classify(ok)
    test_parse_detail(ok)
    d = tmp / "store"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_store(d, ok))
    d = tmp / "collector"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_collector(d, ok))
    d = tmp / "sync"; d.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_sync(d, ok))
    test_web(ok)


if __name__ == "__main__":
    import shutil
    import tempfile
    root = pathlib.Path(tempfile.mkdtemp(prefix="links-"))
    try:
        run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All links checks passed.")
