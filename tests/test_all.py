"""
The whole offline test suite.

    python3 tests/test_all.py

No network, no accounts, no rate-limit budget spent. Everything runs against
canned X payloads in fixtures.py, which is what makes the freshness logic
actually testable rather than hopefully-correct.

Sections:
  units      snowflake arithmetic, normalize, config validation
  session    the three auth bugs, each shown against twscrape's own behaviour
  engine     page parsing, and the account-lock release regression
  collector  watermark stopping, dedup, gaps, adaptive intervals
  cli        argv routing, exit codes, search -> store -> export
"""

import asyncio
import gc
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from contextlib import aclosing
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import auth as ss
import config as C
import store as nz          # normalize helpers now live in store
import store as sf          # snowflake helpers too
from auth import Harvest
from config import AccountCfg
from engine import parse_page
from fixtures import (
    DATE_HEADER_TS, FakeResponse, ID_NEWEST, ID_OLD_QUOTE, ID_ORIGINAL,
    ID_RETWEET, id_at, search_payload,
)
from store import Store

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = None   # set by main() once the scratch directory exists
HERE = pathlib.Path(__file__).resolve().parent
FAILURES = []


def ok(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


def section(name):
    print(flush=True)
    print("=" * 70, flush=True)
    print(f"  {name}", flush=True)
    print("=" * 70, flush=True)


# ==========================================================================
# units
# ==========================================================================

def test_snowflake():
    print("== snowflake ==")
    epoch = datetime.fromtimestamp(sf.TWITTER_EPOCH_MS / 1000, tz=timezone.utc)
    ok(
        epoch.isoformat() == "2010-11-04T01:42:54.657000+00:00",
        f"epoch constant is the documented Twitter epoch ({epoch.isoformat()})",
    )
    for ms in (sf.TWITTER_EPOCH_MS, 1700000000000, 1900000000000):
        ok(sf.id_to_ms(sf.ms_to_id(ms)) == ms, f"ms -> id -> ms round-trips ({ms})")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    ok(sf.id_to_dt(sf.dt_to_id(now)) == now, "datetime round-trips through id space")

    day = sf.dt_to_id(datetime(2026, 1, 2, tzinfo=timezone.utc)) - sf.dt_to_id(
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    ok(day >> 22 == 86_400_000, "24h of id space is exactly 86400000ms")

    ids = sorted(sf.ms_to_id(1_800_000_000_000 + i * 997) for i in range(500))
    ok(
        all(sf.id_to_ms(a) <= sf.id_to_ms(b) for a, b in zip(ids, ids[1:])),
        "sorting by id is sorting by time (why tweet_id is the store's INTEGER pk)",
    )

    wm = sf.dt_to_id(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
    ok(sf.id_to_ms(wm) - sf.id_to_ms(sf.id_minus_ms(wm, 60_000)) == 60_000,
       "id_minus_ms rolls back by exactly the overlap window")
    ok(not sf.is_snowflake(20), "@jack's tweet id 20 is rejected as pre-snowflake")
    ok(sf.is_snowflake(sf.MIN_SNOWFLAKE_ID), "the cutover id itself is accepted")
    ok(sf.id_to_dt(sf.MIN_SNOWFLAKE_ID).year == 2010, "and decodes to 2010")
    ok(sf.lag_ms(wm, sf.id_to_ms(wm) - 5000) == 0, "negative lag clamps to 0 (clock skew)")
    ok(sf.ms_to_id(0) == 0, "pre-epoch ms clamps to 0")


def test_normalize():
    print()
    print("== normalize ==")

    class V:
        def __init__(s, url, br, ct):
            s.url, s.bitrate, s.contentType = url, br, ct

    class Vid:
        def __init__(s, variants):
            s.variants, s.thumbnailUrl = variants, "thumb.jpg"

    class M:
        def __init__(s, videos):
            s.photos, s.videos, s.animated = [], videos, []

    tie = [V("a.m3u8", 900, "application/x-mpegURL"), V("b.mp4", 900, "video/mp4")]
    r1 = nz._media_urls(M([Vid(list(tie))]))
    r2 = nz._media_urls(M([Vid(list(reversed(tie)))]))
    ok(r1 == r2 == ["b.mp4"], f"mp4 wins an equal-bitrate tie in BOTH input orders ({r1}/{r2})")
    ok(
        nz._media_urls(M([Vid([V("lo.mp4", 100, "video/mp4"), V("hi.mp4", 9000, "video/mp4")])]))
        == ["hi.mp4"],
        "highest bitrate wins",
    )
    same = [V("first.mp4", 500, "video/mp4"), V("second.mp4", 500, "video/mp4")]
    ok(
        nz._media_urls(M([Vid(same)])) == ["first.mp4"],
        "identical variants resolve to the FIRST deterministically (was last, order-dependent)",
    )
    ok(nz._media_urls(M([Vid([])])) == ["thumb.jpg"], "no variants falls back to the thumbnail")
    ok(nz._media_urls(None) == [], "no media is not an error")

    row = {
        "tweet_id": 1750000000000000000, "url": "u", "created_at": "2024-01-24T03:38:08+00:00",
        "text": "hi", "lang": "en", "author_username": "a", "author_display_name": "A",
        "author_id": 42, "author_followers": 10, "reply_count": 1, "retweet_count": 2,
        "like_count": 3, "quote_count": 4, "view_count": 5, "is_retweet": 0, "is_reply": 1,
        "is_quote": 0, "hashtags": json.dumps(["x", "y"]), "mentions": json.dumps([]),
        "urls": json.dumps(["http://z"]), "media_urls": "not-json", "in_reply_to": 7,
        "conversation_id": None,
    }
    rec = nz.from_store_row(row)
    ok(rec["tweet_id"] == "1750000000000000000", "tweet_id comes back as a string (2^53 safety)")
    ok(rec["hashtags"] == ["x", "y"], "JSON array columns parse back to lists")
    ok(rec["media_urls"] == [], "malformed JSON degrades to [] rather than crashing an export")
    ok(rec["is_reply"] is True and rec["is_quote"] is False, "int flags become bools")
    ok(set(rec) == set(nz.FIELDS), "the record has exactly FIELDS keys")
    ok(nz.to_csv_row(rec)["hashtags"] == "x|y", "to_csv_row pipe-joins list fields")
    ok("media" not in nz.to_csv_row({**rec, "media": [{"type": "video"}]}),
       "to_csv_row drops keys outside the frozen column list (media is JSON-only)")
    wide = nz.to_csv_row({**rec, "lag_ms": 42, "source": "result"}, nz.FIELDS_EXT)
    ok(wide.get("lag_ms") == 42 and wide.get("source") == "result",
       "but keeps them when the caller asks for the wider profile")

    print()
    print("== media ==")

    class _V:
        def __init__(s, **k): s.__dict__.update(k)

    vid = _V(variants=[_V(url="hi.mp4", bitrate=900, contentType="video/mp4")],
             thumbnailUrl="thumb.jpg", duration=333040)
    m = nz._media(_V(photos=[_V(url="p.jpg")], videos=[vid], animated=[]))
    ok([x["type"] for x in m] == ["photo", "video"], "media keeps photos and videos apart")
    ok(m[1]["thumb"] == "thumb.jpg" and m[1]["url"] == "hi.mp4",
       "a video carries BOTH its thumbnail and its file, so it can be shown without being fetched")
    ok(nz._media_urls(_V(photos=[_V(url="p.jpg")], videos=[vid], animated=[]))
       == ["p.jpg", "hi.mp4"],
       "the flat media_urls shape is unchanged (frozen CSV column)")

    raw = {"media": {"photos": [], "videos": [{"thumbnailUrl": "t.jpg",
            "variants": [{"url": "v.mp4", "bitrate": 1, "contentType": "video/mp4"}]}]}}
    back = nz._media(nz._Attr(raw["media"]))
    ok(back and back[0]["thumb"] == "t.jpg",
       "the same extractor runs over stored raw_json, so a parser fix replays over history (R9)")
    ok(
        nz.FIELDS_EXT[: len(nz.FIELDS)] == nz.FIELDS,
        "FIELDS_EXT extends FIELDS in place, so existing CSV columns never shift",
    )


def _cfg(text, name):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(text)
    return C.load_config(root=d)


def _err(text, name, needle):
    try:
        _cfg(text, name)
        return False, "no error raised"
    except C.ConfigError as e:
        return needle in str(e), str(e).splitlines()[0]


def test_config():
    print()
    print("== config ==")
    base = '[[accounts]]\nlabel="a"\nprofile_dir="profiles/a"\n[[streams]]\nlabel="s"\nquery="hello"\n'

    cfg = _cfg(base, "c1")
    ok(cfg.streams[0].watermark is True, "a Latest stream is watermarked by default")
    ok(cfg.streams[0].page_size == 20, "streams inherit [defaults]")
    ok(cfg.db_results.is_absolute(), "relative paths resolve against the config directory")

    good, msg = _err(base + 'tab="Top"\n', "c2", "choose the collection mode explicitly")
    ok(good, f"a ranked tab must declare its mode rather than silently downgrading: {msg}")
    ok(_err(base + 'tab="Top"\nwatermark=true\n', "c3", 'requires tab = "Latest"')[0],
       "watermark=true on a ranked tab is rejected outright")
    ok(_cfg(base + 'tab="Top"\nwatermark=false\n', "c4").streams[0].watermark is False,
       "an explicit sweep on a ranked tab is allowed")

    ok(_err(base + '\n[[accounts]]\nlabel="b"\nprofile_dir="profiles/a"\n', "c5",
            "share profile_dir")[0],
       "two accounts may not share one Chrome profile directory")
    ok(_err('[[streams]]\nlabel="s"\nquery="q"\n', "c6", "declares no [[accounts]]")[0],
       "at least one account is required")
    ok(_err(base + '\n[bogus]\nx=1\n', "c7", "unknown top-level")[0],
       "typo'd top-level keys are rejected, not ignored")
    ok(_err(base.replace('query="hello"', 'query="hello"\nmin_interval_s=100\nmax_interval_s=10'),
            "c8", "exceeds max_interval_s")[0],
       "an inverted interval range is rejected")

    os.environ["TWS_PROXY"] = "http://x:1"
    try:
        ok(_err(base, "c9", "TWS_PROXY is set")[0],
           "a global TWS_PROXY is refused (it silently overrides every per-account proxy)")
    finally:
        del os.environ["TWS_PROXY"]

    example = (pathlib.Path(__file__).resolve().parent.parent / "config.toml.example").read_text()
    cfg = _cfg(example, "c10")
    ok(len(cfg.accounts) == 1 and len(cfg.streams) >= 1, "the shipped config.toml.example loads")


# ==========================================================================
# session (the three auth bugs)
# ==========================================================================

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


async def fake_validate(acc, proxy=None):
    """Stubbed probe: cookies starting 'good' are live, everything else is dead."""
    token = (acc.cookies or {}).get("auth_token", "")
    if token.startswith("good"):
        return ss.ValidationResult(True, acc.username, 200, "settings")
    return ss.ValidationResult(
        False, "", 401, "bookmarks", 'HTTP 401: {"errors":[{"code":32,"message":"Could not authenticate you."}]}'
    )


async def run_session(tmp):
    ss.validate_http = fake_validate
    api = ss.open_api(tmp / "accounts.db")
    acct = AccountCfg(label="acct_a", email="a@b.c", profile_dir=str(tmp / "p"))
    acct.profile_path = tmp / "p"

    print("== pool policy ==")
    ok(api.pool._order_by == "last_used ASC",
       "accounts are picked least-recently-used, not alphabetically "
       "(twscrape's default hammers account #1)")

    print()
    print("== BUG 1: cookies were trusted with no network call at all ==")
    from twscrape.account import has_required_cookies

    ok(has_required_cookies({"auth_token": "dead", "ct0": "c"}) is True,
       "twscrape's has_required_cookies calls obviously-dead cookies valid "
       "(it only checks for non-empty strings)")
    _, res = await ss.upsert_session(api, Harvest("bob", "UA", {"auth_token": "dead", "ct0": "c"}), acct)
    acc = await api.pool.get_account("bob")
    ok(not res.ok and not acc.active, "FIXED: we validate with a real request before activating")
    ok("401" in (acc.error_msg or ""), f"and record why: {(acc.error_msg or '')[:60]}")

    print()
    print("== BUG 2: rotating cookies on an existing account did nothing ==")
    await ss.upsert_session(api, Harvest("alice", "UA1", {"auth_token": "good1", "ct0": "c1", "kdt": "k"}), acct)
    rows = await ss.health(api)
    ok(
        any(r.has_known_device for r in rows),
        "the known-device token (kdt) is carried over, not just auth_token/ct0 — "
        "it is what keeps X treating the HTTP client as the same trusted device",
    )
    before = (await api.pool.get_account("alice")).cookies["auth_token"]
    await api.pool.add_account("alice", "p", "e", "ep", cookies="auth_token=STALE; ct0=STALE")
    unchanged = (await api.pool.get_account("alice")).cookies["auth_token"]
    ok(unchanged == before,
       "twscrape's add_account silently no-ops on an existing row (this is why editing "
       ".env never took effect)")
    await ss.upsert_session(api, Harvest("alice", "UA2", {"auth_token": "good2", "ct0": "c2"}), acct)
    after = (await api.pool.get_account("alice")).cookies["auth_token"]
    ok(after == "good2", f"FIXED: pool.save() overwrites unconditionally ({before} -> {after})")

    print()
    print("== BUG 3: one failed login excluded an account forever ==")
    import twscrape.db as tdb

    rows = await tdb.fetchall(
        str(tmp / "accounts.db"), "SELECT username FROM accounts WHERE active = false AND error_msg IS NULL"
    )
    ok("bob" not in [r["username"] for r in rows],
       "login_all's own query skips any account carrying an error_msg — permanently")
    _, res = await ss.upsert_session(api, Harvest("bob", "UA", {"auth_token": "good3", "ct0": "c3"}), acct)
    acc = await api.pool.get_account("bob")
    ok(res.ok and acc.active and acc.error_msg is None,
       "FIXED: a successful re-login clears error_msg and reactivates")

    print()
    print("== fingerprint consistency ==")
    acc = await api.pool.get_account("alice")
    ok(acc.user_agent == "UA2",
       "the REAL browser user-agent is stored (the '@chrome' placeholder makes twscrape "
       "invent a random UA unrelated to these cookies)")
    ok(not acc.user_agent.startswith("@"), "so make_client sends the browser's own UA verbatim")
    a2 = await api.pool.get_account("alice")
    ok(a2.headers == {}, "headers are cleared so the bearer/x-csrf-token rebuild from the new ct0")

    print()
    print("== what validation does and does not prove ==")
    # There used to be an identity cross-check here: validation returned a
    # screen_name and upsert_session refused to activate if it disagreed with
    # the harvested one. That check is gone, and deliberately so — measured
    # 2026-07-29, every v1.1 endpoint that could return a screen_name answers
    # 404/code 34. The GraphQL probe that replaced them proves the session
    # AUTHENTICATES but not whose it is.
    #
    # That is safe because `username` comes from the DOM of the very browser
    # profile the cookies were harvested from, so the two cannot disagree. What
    # still must hold is the activation rule itself.
    await ss.upsert_session(api, Harvest("carol", "UA", {"auth_token": "dead2", "ct0": "c"}), acct)
    acc = await api.pool.get_account("carol")
    ok(not acc.active, "a session that fails the probe is never activated")
    ok(bool(acc.error_msg), f"and the reason is recorded: {(acc.error_msg or '')[:50]}")

    print()
    print("== identity sidecar ==")
    ss.write_identity(acct, "alice")
    ok(ss.read_identity(acct) == "alice",
       "the label -> username mapping lives beside the profile, so config.toml is never rewritten")

    print()
    print("== require_active ==")
    names = await ss.require_active(api)
    ok(sorted(names) == ["alice", "bob"], f"only validated accounts are reported live: {sorted(names)}")
    for n in ("alice", "bob"):
        await api.pool.mark_inactive(n, "simulated")
    try:
        await ss.require_active(api)
        ok(False, "require_active should raise when nothing is live")
    except RuntimeError as e:
        ok("login --all" in str(e), "and it names the exact command that fixes it")

    rows = await ss.health(api)
    ok(len(rows) == 3 and all(not r.active for r in rows), "health() reports every account")


# ==========================================================================
# engine (parsing + account-lock release)
# ==========================================================================

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_parse():

    print("== page parsing ==")
    rep = FakeResponse(search_payload())
    page = parse_page(rep, 1)

    ok(
        page.result_ids == [ID_NEWEST, ID_RETWEET, ID_ORIGINAL],
        f"result_ids are in TIMELINE order from entry ids: {page.result_ids}",
    )
    ok(page.cursor == "CURSOR_PAGE2", f"bottom cursor extracted: {page.cursor}")
    ok(
        page.account == "alice" and page.rl_limit == 50 and page.rl_remaining == 47,
        "serving account and rate-limit budget travel with the page",
    )
    ok(
        abs(page.collected_ms / 1000 - DATE_HEADER_TS) < 2,
        "the server Date header is the lag clock (immune to host clock skew)",
    )
    ok(
        not any(str(e).startswith("who-to-follow") for e in page.entries_by_id),
        "non-result entries (who-to-follow, cursors) are excluded",
    )

    # The retweet-original collision.
    ok(ID_ORIGINAL in page.tweets, "the retweeted ORIGINAL survives our parse")
    from twscrape.models import parse_tweets

    tws = {t.id for t in parse_tweets(search_payload(), -1)}
    ok(
        ID_ORIGINAL not in tws,
        f"  confirmed: twscrape's own parse_tweets silently loses it (got {sorted(tws)})",
    )

    # The embedded ancient quote.
    ok(
        ID_OLD_QUOTE in page.tweets,
        "the embedded decade-old quoted tweet is still captured as context",
    )
    ok(
        ID_OLD_QUOTE not in page.result_ids and ID_OLD_QUOTE in page.embedded_ids,
        "  but it is NOT a result, so it cannot drag the watermark backwards",
    )
    ok(
        page.min_result_id() > ID_OLD_QUOTE,
        f"  min_result_id ({page.min_result_id()}) ignores it entirely",
    )
    ok(page.orphan_ids == [], f"no orphan results: {page.orphan_ids}")
    ok(page.parse_failures == [], f"no parse failures: {page.parse_failures}")


async def test_lock_release(tmp):
    """F5: breaking out early must release the account lock immediately."""
    import auth as ss
    from auth import Harvest
    from config import AccountCfg
    from engine import Engine
    from twscrape.utils import utc

    print()
    print("== F5: early break must release the 15-minute account lock ==")

    db = tmp / "accounts.db"
    api = ss.open_api(db)

    async def fake_validate(acc, proxy=None):
        return ss.ValidationResult(True, acc.username, 200, "settings")

    ss.validate_http = fake_validate
    acct = AccountCfg(label="x", profile_dir=str(tmp / "p"))
    acct.profile_path = tmp / "p"
    await ss.upsert_session(api, Harvest("alice", "UA", {"auth_token": "t", "ct0": "c"}), acct)

    rep = FakeResponse(search_payload())

    # Drive the REAL QueueClient + pool locking, but serve a canned response so
    # the test stays offline. This is the machinery that actually holds locks.
    from twscrape import api as twapi
    from twscrape.queue_client import QueueClient

    async def fake_gql_items(self, op, kv, ft=None, limit=-1, cursor_type="Bottom"):
        queue = op.split("/")[-1]
        async with QueueClient(self.pool, queue, False, proxy=None):
            for _ in range(50):  # effectively endless pagination
                yield rep

    twapi.API._gql_items = fake_gql_items

    async def held_locks():
        now = utc.now()
        return [
            q
            for a in await api.pool.get_all()
            for q, until in (a.locks or {}).items()
            if until and until > now
        ]

    eng = Engine(api)

    # --- contrast 1: break, then the generator goes out of scope ---
    # CPython refcounting finalizes it here, so the lock happens to be
    # released. This is why the bug is easy to miss in simple code.
    await api.pool.reset_locks()

    async def scoped():
        async for _ in eng.search_pages("q"):
            break

    await scoped()
    gc.collect()
    await asyncio.sleep(0.1)
    ok(
        await held_locks() == [],
        "  (context: break + immediate scope exit happens to release, via refcounting)",
    )

    # --- contrast 2: the real failure mode ---
    # Any surviving reference means there is nothing to collect, so the
    # QueueClient never exits and the account is gone for 15 minutes.
    await api.pool.reset_locks()
    leaked_gen = eng.search_pages("q")
    async for _ in leaked_gen:
        break
    gc.collect()
    await asyncio.sleep(0.1)
    stuck = await held_locks()
    ok(
        stuck == ["SearchTimeline"],
        f"  (context: break with a live reference LEAKS the lock: held={stuck}) "
        f"-- this is what aclosing prevents",
    )
    await leaked_gen.aclose()
    ok(await held_locks() == [], "  (and closing it explicitly releases the lock)")

    # --- the right way ---
    await api.pool.reset_locks()
    async with aclosing(eng.search_pages("q")) as g:
        async for _ in g:
            break
    ok(await held_locks() == [], "aclosing + break releases the lock immediately")

    await api.pool.reset_locks()
    try:
        async with aclosing(eng.search_pages("q")) as g:
            async for _ in g:
                raise ValueError("boom")
    except ValueError:
        pass
    ok(await held_locks() == [], "the lock is released when the loop body raises")

    await api.pool.reset_locks()
    async with aclosing(eng.search_pages("q", max_pages=2)) as g:
        pages = [p async for p in g]
    ok(len(pages) == 2, f"max_pages stops pagination at the budget ({len(pages)} pages)")
    ok(await held_locks() == [], "the lock is released on the max_pages path too")

    # Cursor resume must reach twscrape's variables intact.
    seen = {}

    async def capture(self, op, kv, ft=None, limit=-1, cursor_type="Bottom"):
        seen.update(kv)
        async with QueueClient(self.pool, op.split("/")[-1], False, proxy=None):
            yield rep

    twapi.API._gql_items = capture
    await api.pool.reset_locks()
    async with aclosing(eng.search_pages("q", cursor="RESUME_HERE", page_size=40, tab="Latest")) as g:
        async for _ in g:
            pass
    ok(seen.get("cursor") == "RESUME_HERE", f"resume cursor is passed through: {seen.get('cursor')}")
    ok(seen.get("count") == 40 and seen.get("product") == "Latest", "page_size and tab reach the request")


# ==========================================================================
# collector (watermark, dedup, gaps, intervals)
# ==========================================================================

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from collector import (  # noqa: E402
    STOP_EXHAUSTED,
    STOP_PAGE_BUDGET,
    STOP_STARVED,
    STOP_WATERMARK,
    next_interval,
    poll_once,
)


class ReplayEngine:

    def pages_for(self, stream, **kw):
        """Mirror Engine.pages_for so tests exercise the real dispatch seam."""
        if getattr(stream, "list_id", None):
            kw.pop("tab", None)
            return self.list_pages(stream.list_id, **kw)
        return self.search_pages(stream.query, **kw)

    async def list_pages(self, list_id, **kw):
        async with aclosing(self.search_pages("", **kw)) as g:
            async for page in g:
                yield page
    """Serves canned pages. `pages` is a list of lists of tweet ids."""

    def __init__(self, pages, cursor_end=True):
        self.pages = pages
        self.cursor_end = cursor_end
        self.calls = 0
        self.requested = []

    async def search_pages(self, query, *, tab="Latest", page_size=20,
                           max_pages=0, limit=-1, cursor=None):
        self.calls += 1
        self.requested.append({"query": query, "cursor": cursor, "max_pages": max_pages})
        for i, ids in enumerate(self.pages):
            is_last = i == len(self.pages) - 1
            payload = search_payload(
                ids=ids,
                cursor=None if (is_last and self.cursor_end) else f"CUR{i}",
                # A page of `None` means "use the trap payload" — the one with
                # the retweet collision and the ancient embedded quote.
                include_traps=ids is None,
            )
            yield parse_page(FakeResponse(payload), i + 1)
            if max_pages and i + 1 >= max_pages:
                return


class Stream:
    def __init__(self, **kw):
        self.label = kw.get("label", "s")
        self.query = kw.get("query", "test query")
        self.tab = "Latest"
        self.watermark = kw.get("watermark", True)
        self.page_size = kw.get("page_size", 20)
        self.max_pages_per_poll = kw.get("max_pages_per_poll", 5)
        self.min_interval_s = kw.get("min_interval_s", 5.0)
        self.max_interval_s = kw.get("max_interval_s", 900.0)
        self.overlap_ms = kw.get("overlap_ms", 60_000)


MIN = 60_000


def ids_at(*offsets_min):
    """
    Tweet ids at given MINUTE offsets before now, newest first.

    Spacing matters: the poller re-reads a 60s overlap window past the
    watermark on every poll, so fixtures spaced seconds apart would sit
    entirely inside that window and never exercise the stop condition.
    """

    return [id_at(-int(off * MIN)) for off in offsets_min]


async def run_collector(tmp):
    db = tmp / "results.db"
    store = Store(db)
    await store.open()
    stream = Stream()
    sid = await store.ensure_stream(stream.label, stream.query, "Latest", True)

    print("== first poll: no watermark yet ==")
    p1 = ids_at(10, 15, 20)  # 10, 15 and 20 minutes old
    eng = ReplayEngine([p1])
    res = await poll_once(eng, store, stream, sid)
    ok(res.new == 3 and res.dup == 0, f"3 new tweets on a cold start (new={res.new} dup={res.dup})")
    ok(res.stop_reason == STOP_EXHAUSTED, f"stop={res.stop_reason} (no watermark to stop at)")
    ok(res.max_id == max(p1), "watermark candidate is the newest result id")
    ok(len(res.lags) == 3 and all(x >= 0 for x in res.lags), "lag recorded per tweet")
    await store.set_watermark(sid, res.max_id)

    print()
    print("== second poll: same data -> must stop at the watermark, 1 page ==")
    eng = ReplayEngine([p1, ids_at(25, 30)])
    res = await poll_once(eng, store, stream, sid)
    ok(res.stop_reason == STOP_WATERMARK, f"stopped at the watermark (stop={res.stop_reason})")
    ok(res.pages == 1, f"only ONE page fetched, not the whole timeline (pages={res.pages})")
    ok(res.new == 0 and res.dup == 3, f"cross-run dedup: new={res.new} dup={res.dup}")

    print()
    print("== third poll: 2 genuinely new tweets on top ==")
    fresh = ids_at(1, 5)
    eng = ReplayEngine([fresh + p1])
    res = await poll_once(eng, store, stream, sid)
    ok(res.new == 2 and res.dup == 3, f"only the new ones count as new (new={res.new} dup={res.dup})")
    ok(res.stop_reason == STOP_WATERMARK, "still stops at the watermark")
    await store.set_watermark(sid, res.max_id)

    print()
    print("== the overlap window ==")
    wm = await store.get_watermark(sid)
    stop_id = sf.id_minus_ms(wm["high_tweet_id"], stream.overlap_ms)
    ok(
        sf.id_to_ms(wm["high_tweet_id"]) - sf.id_to_ms(stop_id) == 60_000,
        "the poller stops 60s BELOW the watermark, so late-indexed tweets are still caught",
    )
    # Posted 30s after the watermark tweet but indexed late, so it appears
    # BELOW the watermark on a later poll. Without the overlap it is lost.

    late = [id_at(-int(1 * MIN) + 30_000)]
    eng = ReplayEngine([ids_at(0.2) + late + ids_at(1)])
    res = await poll_once(eng, store, stream, sid)
    ok(res.new == 2, f"the late-indexed tweet below the watermark is still collected (new={res.new})")

    print()
    print("== page budget: the stream outran us ==")
    busy = Stream(label="busy", query="busy q", max_pages_per_poll=2)
    bsid = await store.ensure_stream(busy.label, busy.query, "Latest", True)
    eng = ReplayEngine([ids_at(120)], cursor_end=False)
    r0 = await poll_once(eng, store, busy, bsid)
    await store.set_watermark(bsid, r0.max_id)
    eng = ReplayEngine([ids_at(1, 2), ids_at(3, 4), ids_at(5)], cursor_end=False)
    res = await poll_once(eng, store, busy, bsid)
    ok(res.stop_reason == STOP_PAGE_BUDGET, f"stop={res.stop_reason} at the page ceiling")
    ok(res.pages == 2, f"honoured max_pages_per_poll=2 (pages={res.pages})")
    ok(res.gap_opened, "a GAP was recorded for the window we never reached")
    gaps = await store.open_gaps(bsid)
    ok(len(gaps) == 1 and gaps[0]["hi_tweet_id"] == res.min_id,
       "the gap spans (old watermark, oldest tweet actually seen)")

    print()
    print("== starvation must not look like a quiet stream ==")

    class Starved:
        def pages_for(self, stream, **kw):
            return self.search_pages(stream.query, **kw)

        async def search_pages(self, *a, **kw):
            return
            yield  # pragma: no cover

    res = await poll_once(Starved(), store, stream, sid)
    ok(res.stop_reason == STOP_STARVED, f"zero pages is reported as starvation (stop={res.stop_reason})")
    ok(res.pages == 0 and res.new == 0, "no data attributed")
    wm_after = await store.get_watermark(sid)
    ok(wm_after["high_tweet_id"] == wm["high_tweet_id"],
       "the watermark is NOT advanced on a starved poll")

    print()
    print("== embedded context never moves the watermark ==")
    eng = ReplayEngine([None])  # None -> the trap payload with the ancient quote
    eng.pages = [None]
    res = await poll_once(eng, store, Stream(label="trap", query="q"),
                          await store.ensure_stream("trap", "q", "Latest", True))

    ok(res.max_id == ID_NEWEST, f"watermark uses the newest RESULT ({res.max_id})")
    ok(res.min_id > ID_OLD_QUOTE,
       "the decade-old embedded quote is excluded from result ids entirely")
    ok(res.embedded >= 1, f"...but it IS stored as context (embedded={res.embedded})")

    print()
    print("== lag report ==")
    lines = await store.lag_report("24h")
    for x in lines:
        print(f"       {x}")
    ok(
        any("predate this stream's first poll" in x for x in lines),
        "backlog-only streams say so plainly instead of reporting nothing",
    )
    ok(any("page_budget" in x for x in lines), "poll outcomes broken down by stop reason")
    ok(any("starvation" in x.lower() for x in lines), "starvation is called out explicitly")

    # Now collect a tweet created AFTER the stream started, which is what a
    # steady-state stream looks like, and confirm real percentiles appear.

    eng = ReplayEngine([[id_at(+2_000)]])
    await poll_once(eng, store, stream, sid)
    lines = await store.lag_report("24h")
    ok(any("p50=" in x for x in lines), f"real percentiles once a fresh tweet lands: "
       f"{[x for x in lines if 'p50=' in x]}")
    ok(any("backlog, excluded" in x for x in lines), "backlog is counted separately, not hidden")

    print()
    print("== export round-trip ==")
    import store as exporter

    rows = list(store.iter_export(limit=5))
    ok(len(rows) == 5, f"export query returns rows ({len(rows)})")
    path, n = exporter.export(rows, str(tmp / "out"), "csv")
    ok(n == 5 and pathlib.Path(path).exists(), f"csv written: {path} ({n} rows)")
    import csv as _csv

    with open(path) as f:
        header = next(_csv.reader(f))
    from store import FIELDS

    ok(header == FIELDS, "exported CSV header is byte-identical to the prototype's FIELDS")

    rows = list(store.iter_export(limit=3))
    path, n = exporter.export(rows, str(tmp / "out_all"), "json", "all")
    import json as _json

    recs = _json.loads(pathlib.Path(path).read_text())
    ok("lag_ms" in recs[0] and "collected_at" in recs[0],
       "--fields all adds collection metadata")
    ok(isinstance(recs[0]["tweet_id"], str),
       "tweet_id exports as a string (JSON loses precision above 2^53)")

    await store.close()


def test_interval():
    print()
    print("== adaptive interval ==")
    s = Stream(min_interval_s=5, max_interval_s=900, page_size=20)

    class R:
        def __init__(self, new, stop):
            self.new, self.stop_reason = new, stop

        @property
        def starved(self):
            return self.stop_reason == STOP_STARVED

    i, e, c = next_interval(s, 60, 60, R(0, STOP_WATERMARK), 0.0, 0)
    ok(i > 60, f"a quiet stream backs off (60s -> {i:.0f}s)")

    i2, e2, c2 = next_interval(s, i, 60, R(0, STOP_WATERMARK), e, c)
    ok(i2 > i, f"and keeps backing off while empty ({i:.0f}s -> {i2:.0f}s)")

    i3, _, _ = next_interval(s, 300, 60, R(40, STOP_PAGE_BUDGET), 0.0, 0)
    ok(i3 == 150, f"hitting the page budget halves the interval (300s -> {i3:.0f}s)")

    i4, _, _ = next_interval(s, 100, 100, R(0, STOP_STARVED), 0.0, 3)
    ok(i4 == 100, f"starvation HOLDS the interval, never backs off ({i4:.0f}s)")

    i5, _, _ = next_interval(s, 60, 60, R(12, STOP_WATERMARK), 0.0, 0)
    ok(abs(i5 - 60) < 1, f"a stream hitting the target holds steady ({i5:.0f}s)")

    i6, _, _ = next_interval(s, 60, 60, R(120, STOP_WATERMARK), 0.0, 0)
    ok(i6 < 60 and i6 >= s.min_interval_s, f"a busy stream speeds up ({i6:.0f}s)")

    i7, _, _ = next_interval(s, 800, 60, R(0, STOP_WATERMARK), 0.0, 9)
    ok(i7 <= s.max_interval_s, f"never exceeds max_interval_s ({i7:.0f}s)")

    i8, _, _ = next_interval(s, 6, 1, R(9999, STOP_PAGE_BUDGET), 0.0, 0)
    ok(i8 >= s.min_interval_s, f"never drops below min_interval_s ({i8:.0f}s)")


# ==========================================================================
# cli (routing, exit codes, end to end)
# ==========================================================================

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))



def run_cli(args, cwd, env=None):
    e = {**os.environ, "TWS_TELEMETRY": "0", "DO_NOT_TRACK": "1", **(env or {})}
    p = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=str(cwd), capture_output=True, text=True, env=e, timeout=120,
    )
    return p.returncode, p.stdout + p.stderr


CONFIG = """
[defaults]
db_accounts = "accounts.db"
db_results  = "results.db"

[[accounts]]
label = "acct_a"
profile_dir = "profiles/acct_a"

[[streams]]
label = "test_stream"
query = "hello world lang:en"
tab   = "Latest"
"""


def test_routing_and_exit_codes(tmp):
    print("== argv routing ==")
    from main import normalize_argv

    ok(
        normalize_argv(["--query", "x", "--limit", "5"]) == ["search", "--query", "x", "--limit", "5"],
        "the prototype's flag-first invocation still routes to `search`",
    )
    ok(normalize_argv(["login", "--all"]) == ["login", "--all"], "subcommands pass through")
    ok(normalize_argv(["--help"]) == ["--help"], "--help is not swallowed")

    print()
    print("== exit codes ==")
    (tmp / "config.toml").write_text(CONFIG)

    rc, out = run_cli(["doctor", "--selftest"], tmp)
    ok(rc == 0, f"doctor --selftest passes on the pinned twscrape (rc={rc})")

    rc, out = run_cli(["doctor", "--accounts"], tmp)
    ok(rc == 6, f"doctor --accounts exits 6 (no account) when the store is empty (rc={rc})")
    ok("login --all" in out, "and names the command that fixes it")

    rc, out = run_cli(["search", "--query", "test"], tmp)
    ok(rc == 6, f"search exits 6 rather than pretending to work without a session (rc={rc})")
    ok("login" in out, "and points at login")

    rc, out = run_cli(["watch", "--once"], tmp)
    ok(rc == 6, f"watch exits 6 without a session (rc={rc})")

    rc, out = run_cli(["export", "--format", "csv"], tmp)
    ok(rc == 4, f"export exits 4 when there is no results db yet (rc={rc})")

    bad = tmp / "bad"
    bad.mkdir(exist_ok=True)
    (bad / "config.toml").write_text(
        '[[accounts]]\nlabel="a"\n[[streams]]\nlabel="s"\nquery="q"\ntab="Top"\n'
    )
    rc, out = run_cli(["doctor"], bad)
    ok(rc == 4, f"a config error exits 4 (rc={rc})")
    ok("collection mode explicitly" in out, "and explains the ranked-tab problem")


async def test_search_to_export(tmp):
    """search -> store -> export, with the network stubbed at the Engine seam."""
    print()
    print("== search -> store -> export (engine stubbed) ==")
    os.chdir(tmp)
    sys.path.insert(0, str(ROOT))

    import main as commands
    import auth as ss
    from auth import Harvest
    from config import AccountCfg, load_config

    cfg = load_config(root=tmp)

    # A validated account, so require_active passes.
    api = ss.open_api(cfg.db_accounts)

    async def fake_validate(acc, proxy=None):
        return ss.ValidationResult(True, acc.username, 200, "settings")

    ss.validate_http = fake_validate
    acct = AccountCfg(label="acct_a", profile_dir=str(tmp / "profiles/acct_a"))
    acct.profile_path = tmp / "profiles/acct_a"
    await ss.upsert_session(api, Harvest("alice", "UA", {"auth_token": "t", "ct0": "c"}), acct)

    ids = [id_at(-60_000 * i) for i in range(1, 6)]

    class StubEngine:
        def __init__(self, api):
            self.api = api

        def pages_for(self, stream, **kw):
            if getattr(stream, "list_id", None):
                kw.pop("tab", None)
                return self.list_pages(stream.list_id, **kw)
            return self.search_pages(stream.query, **kw)

        async def list_pages(self, list_id, **kw):
            async with aclosing(self.search_pages("", **kw)) as g:
                async for page in g:
                    yield page

        async def search_pages(self, query, *, tab="Latest", page_size=20,
                               max_pages=0, limit=-1, cursor=None):
            yield parse_page(
                FakeResponse(search_payload(ids=ids, cursor=None, include_traps=False)), 1
            )

    commands.Engine = StubEngine

    class A:
        config = None
        query = "hello world lang:en"
        list_id = ""
        limit = 50
        tab = "Latest"
        out = str(tmp / "results")
        db = None
        store = True
        debug_pages = True
        cursor = None

    rc = await commands.cmd_search(A())
    ok(rc == 0, f"search succeeds (rc={rc})")
    for suffix in (".json", ".csv", ".raw.jsonl"):
        p = tmp / f"results{suffix}"
        ok(p.exists() and p.stat().st_size > 0, f"legacy output written: results{suffix}")

    recs = json.loads((tmp / "results.json").read_text())
    ok(len(recs) == 5, f"5 tweets in results.json ({len(recs)})")
    ok(isinstance(recs[0]["tweet_id"], str), "tweet_id is a string in JSON output")

    # Cross-run dedup: the same search again must report zero new.
    rc = await commands.cmd_search(A())
    import store as store_mod

    st = store_mod.Store(cfg.db_results)
    await st.open()
    total = await st.count_tweets()
    await st.close()
    ok(total == 5, f"re-running the identical search added nothing new (total={total})")

    class E:
        config = None
        stream = None
        since = None
        until = None
        format = "csv"
        out = str(tmp / "exp")
        fields = "all"
        include_embedded = False
        limit = None
        order = "desc"

    rc = await commands.cmd_export(E())
    ok(rc == 0 and (tmp / "exp.csv").exists(), "export writes a CSV from the store")

    # Assert VALUES, not just headers. Checking the header alone let a real
    # regression through: to_csv_row briefly filtered to FIELDS while the writer
    # used FIELDS_EXT, so every extended column was dropped — and DictWriter
    # fills a missing key with '' instead of complaining, so the file still had
    # all the right headers with nothing underneath them.
    import csv as _csv
    rows = list(_csv.DictReader((tmp / "exp.csv").open()))
    header = (tmp / "exp.csv").read_text().splitlines()[0]
    ok("lag_ms" in header and "collected_at" in header, "--fields all includes lag metadata")
    empty = [k for k in ("collected_at", "lag_ms", "stream_label", "source")
             if not (rows and rows[0].get(k))]
    ok(not empty, f"--fields all POPULATES the extended columns, not just their headers "
                  f"({'all present' if not empty else 'empty: ' + ', '.join(empty)})")
    ok(all(r.get("tweet_id") for r in rows), "every exported row has its tweet_id")

    E.format = "raw"
    await commands.cmd_export(E())
    raw_lines = (tmp / "exp.raw.jsonl").read_text().strip().splitlines()
    ok(len(raw_lines) == 5, f"raw export preserves one full Tweet.json per line ({len(raw_lines)})")
    obj = json.loads(raw_lines[0])
    ok("bookmarkedCount" in obj or "id" in obj, "raw rows retain fields the flat schema drops")


# ==========================================================================
# lists
# ==========================================================================

def test_lists():
    print()
    print("== lists ==")
    base = '[[accounts]]\nlabel="a"\nprofile_dir="profiles/a"\n'

    cfg = _cfg(base + '[[streams]]\nlabel="l"\nlist_id="1234567890"\n', "l1")
    ok(cfg.streams[0].list_id == "1234567890" and not cfg.streams[0].query,
       "a list stream parses with no query")
    ok(cfg.streams[0].watermark is True,
       "list timelines are chronological, so they watermark like a Latest search")

    # URL forms, because people paste from the address bar
    for raw, want in [
        ("https://x.com/i/lists/999", "999"),
        ("https://twitter.com/i/lists/888?ref_src=twsrc%5Etfw", "888"),
        ("  777  ", "777"),
    ]:
        got = _cfg(base + f'[[streams]]\nlabel="l"\nlist_id="{raw}"\n',
                   f"lu{want}").streams[0].list_id
        ok(got == want, f"list_id {raw.strip()[:38]!r} -> {got!r}")

    ok(_err(base + '[[streams]]\nlabel="l"\nlist_id="abc"\n', "l2", "must be numeric")[0],
       "a non-numeric list id is rejected with a fixable message")
    ok(_err(base + '[[streams]]\nlabel="l"\nlist_id="https://x.com/foo"\n', "l3",
            "could not find a list id")[0],
       "a URL with no list id in it is rejected")
    ok(_err(base + '[[streams]]\nlabel="l"\nquery="hi"\nlist_id="12"\n', "l4",
            "one source")[0],
       "query + list_id together is refused rather than one silently winning")
    ok(_err(base + '[[streams]]\nlabel="l"\n', "l5", "either a `query` or a `list_id`")[0],
       "a stream with no source at all is refused")
    ok(_err(base + '[[streams]]\nlabel="l"\nlist_id="12"\ntab="Top"\n', "l6",
            "no 'Top' tab")[0],
       "a list stream cannot ask for a product tab")

    # dispatch: one place decides search vs list
    from engine import Engine

    calls = []

    class FakeAPI:
        async def search_raw(self, q, **kw):
            calls.append(("search", q, kw))
            return
            yield

        async def list_timeline_raw(self, lid, **kw):
            calls.append(("list", lid, kw))
            return
            yield

    eng = Engine(FakeAPI())

    class S:
        query, list_id = "hello", ""

    async def drain(gen):
        async with aclosing(gen) as g:
            async for _ in g:
                pass

    asyncio.run(drain(eng.pages_for(S(), tab="Latest", page_size=20)))
    ok(calls and calls[-1][0] == "search", "a query stream dispatches to search_raw")

    S.query, S.list_id = "", "42"
    asyncio.run(drain(eng.pages_for(S(), tab="Latest", page_size=20)))
    ok(calls[-1][0] == "list" and calls[-1][1] == 42,
       f"a list stream dispatches to list_timeline_raw with an int id ({calls[-1][1]!r})")
    ok("tab" not in calls[-1][2] and "product" not in calls[-1][2].get("kv", {}),
       "and the product tab is dropped — a list timeline has no tabs")


# ==========================================================================
# account status flags
# ==========================================================================

def test_account_flags():
    print()
    print("== account status taxonomy ==")
    import guard

    def V(**kw):
        base = dict(username="u", active=True, proxy="http://p", error_msg=None,
                    has_known_device=True, real_user_agent=True, requests=1)
        base.update(kw)
        return guard.AccountView(**base)

    ok(guard.classify_account(V())["status"] == guard.STATUS_LIVE,
       "a clean active account is LIVE")

    for code, why in [("32", "session"), ("326", "locked"), ("88", "ban"), ("64", "suspend")]:
        c = guard.classify_account(V(active=False, error_msg=f"({code}) whatever X said"))
        ok(c["status"] == guard.STATUS_DEAD, f"X code ({code}) -> DEAD, not a warning")
        ok(bool(c["reasons"][0]), f"  and ({code}) explains itself: {c['reasons'][0][:44]}")

    c = guard.classify_account(V(active=False, error_msg=None))
    ok(c["status"] == guard.STATUS_DEAD and c["reasons"],
       "inactive with no recorded reason is still DEAD, with a placeholder reason")
    # It must say how to fix it, and say it in terms of the dashboard: the
    # whole point of the sign-in window is that recovering an account no longer
    # means finding a terminal.
    ok("sign in" in c["action"].lower(), "DEAD says how to fix it, without a CLI command")

    for kw, label in [({"proxy": ""}, "no proxy"),
                      ({"has_known_device": False}, "no kdt"),
                      ({"real_user_agent": False}, "placeholder UA"),
                      ({"requests": guard.HEAVY_REQUESTS + 1}, "heavy usage")]:
        c = guard.classify_account(V(**kw))
        ok(c["status"] == guard.STATUS_WARN, f"{label} -> WARN (still usable)")
        ok(len(c["reasons"]) >= 1, f"  and says why: {c['reasons'][0][:48]}")

    c = guard.classify_account(V(proxy="", has_known_device=False))
    ok(len(c["reasons"]) == 2, f"multiple problems all get reported ({len(c['reasons'])})")

    # The rule that matters most: a warning must never read as healthy, and a
    # dead account must never read as merely warning.
    ok(guard.classify_account(V(proxy=""))["status"] != guard.STATUS_LIVE,
       "a risky-but-working account is never LIVE")
    ok(guard.classify_account(V(active=False, proxy=""))["status"] == guard.STATUS_DEAD,
       "inactive outranks every warning — DEAD wins")


# ==========================================================================
# dashboard authentication
# ==========================================================================

def test_auth():
    print()
    print("== dashboard auth ==")
    import web

    old = {k: os.environ.get(k) for k in ("DASH_USER", "DASH_PASSWORD")}
    try:
        os.environ.pop("DASH_USER", None)
        os.environ.pop("DASH_PASSWORD", None)
        ok(not web._auth_configured(), "no credentials -> auth not configured")
        ok(not web._check_credentials("", ""), "and empty credentials never authenticate")
        ok(not web._check_credentials("admin", "anything"),
           "an unset password does not become a wildcard")

        # Placeholder/weak credentials must not count as configured, or the
        # server would bind publicly with a password that is in the repo.
        os.environ["DASH_USER"] = "changeme"
        os.environ["DASH_PASSWORD"] = "a-perfectly-long-password"
        ok(not web._auth_configured(), "a placeholder USERNAME is not 'configured'")
        os.environ["DASH_USER"] = "tilak"
        os.environ["DASH_PASSWORD"] = "CHANGE_ME_TO_A_LONG_RANDOM_STRING"
        ok(not web._auth_configured(), "the .env.example PASSWORD is not 'configured'")
        os.environ["DASH_PASSWORD"] = "short"
        ok(not web._auth_configured(),
           f"a password under {web.MIN_PASSWORD_LEN} chars is not 'configured'")
        ok("characters" in web._auth_problem(), f"and it says why: {web._auth_problem()[:52]}")

        os.environ["DASH_USER"] = "tilak"
        os.environ["DASH_PASSWORD"] = "s3cret-long-value"
        ok(web._auth_configured(), "a real username and long password -> configured")
        ok(web._check_credentials("tilak", "s3cret-long-value"), "correct pair accepted")
        ok(not web._check_credentials("tilak", "s3cret-long-valu"), "near-miss password rejected")
        ok(not web._check_credentials("Tilak", "s3cret-long-value"), "username is case-sensitive")
        ok(not web._check_credentials("", "s3cret-long-value"), "blank username rejected")

        tok = web._issue_token()
        ok(web._token_valid(tok), "a freshly issued session token validates")
        ok(not web._token_valid(tok[:-4] + "AAAA"), "a tampered token is rejected")
        ok(not web._token_valid("garbage"), "garbage is rejected, not crashed on")
        ok(not web._token_valid(""), "an empty token is rejected")

        # Expiry is inside the signature, so it cannot be edited without
        # invalidating the HMAC.
        import base64, hmac, hashlib, time as _t
        past = str(int(_t.time() - 10)).encode()
        sig = hmac.new(web._SECRET, past, hashlib.sha256).digest()[:18]
        expired = base64.urlsafe_b64encode(past + b"." + sig).decode()
        ok(not web._token_valid(expired), "a correctly-signed but EXPIRED token is rejected")

        # A token signed with a different secret must not pass — this is what
        # stops a restarted process honouring an attacker-minted cookie.
        other = hmac.new(b"x" * 32, past, hashlib.sha256).digest()[:18]
        forged = base64.urlsafe_b64encode(past + b"." + other).decode()
        ok(not web._token_valid(forged), "a token signed with the wrong secret is rejected")

        ip = "203.0.113.9"
        web._clear_failures(ip)
        ok(web._locked_out(ip) == 0, "a fresh IP is not locked out")
        for _ in range(web.MAX_ATTEMPTS):
            web._record_failure(ip)
        ok(web._locked_out(ip) > 0, f"{web.MAX_ATTEMPTS} failures triggers lockout")
        web._clear_failures(ip)
        ok(web._locked_out(ip) == 0, "a successful login clears the lockout")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ==========================================================================
# web (the shared event loop)
# ==========================================================================

async def _touch_db(path):
    """Any twscrape DB call — these go through its module-level asyncio.Lock."""
    api = ss.open_api(path)
    return await api.pool.get_all()


def test_web_event_loop(tmp):
    """
    Regression: the dashboard must use ONE event loop for its whole life.

    twscrape holds a module-level asyncio.Lock (db.py:12) that binds to the
    first loop which awaits it. The dashboard originally called asyncio.run()
    per request, creating a fresh loop each time, so the SECOND request onward
    died with "Lock is bound to a different event loop" — and, because that
    surfaced as a failed fetch, it looked to the user like the account had been
    logged out.
    """
    print("== web: shared event loop ==")
    import web

    db = tmp / "accounts.db"

    # Deliberately NOT reproducing the bug here. Measured behaviour of the
    # broken pattern (asyncio.run per call):
    #   * sequential calls happen to succeed — the lock binds lazily on first
    #     await, and an uncontended lock survives its loop being closed;
    #   * CONCURRENT calls from several threads either raise
    #     "bound to a different event loop" or HANG FOREVER, because a waiter
    #     gets queued on a loop that then dies and the lock is never released.
    # A test that can hang the suite is worse than no test, so this asserts the
    # invariant that prevents it instead of provoking the failure.
    web._start_loop()
    try:
        results = [web._run(_touch_db(db)) for _ in range(4)]
        ok(len(results) == 4, "four consecutive calls on the shared loop all succeed")
        ok(all(isinstance(r, list) for r in results),
           "and each returns real data, not an exception")
    except RuntimeError as e:
        ok(False, f"shared loop still broke: {e}")

    # The browser fires /api/status and /api/tweets alongside a fetch, on
    # separate handler threads. That overlap is exactly what killed the
    # original design, so it has to work.
    errs, out = [], []

    def worker():
        try:
            out.append(web._run(_touch_db(db), timeout=30))
        except BaseException as e:
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=45)

    ok(not any(t.is_alive() for t in threads),
       "6 concurrent handler threads all finish — no deadlock on twscrape's lock")
    ok(errs == [], f"and none of them error ({errs[:2]})")
    ok(len(out) == 6, f"all 6 got results ({len(out)}/6)")

    ok(web._LOOP is not None and web._LOOP.is_running(),
       "one loop runs on its own thread for the server's lifetime")
    ok(all(web._LOOP is web._LOOP for _ in range(3)) and web._start_loop() is None,
       "_start_loop is idempotent — never replaces a live loop")


# ==========================================================================
# instagram device pinning
# ==========================================================================

def test_ig_device(tmp):
    """
    The Instagram device fingerprint must be minted ONCE and reused forever.

    This is a regression guard on a bug that cost real sessions: instagrapi's
    Client() mints fresh random uuids when handed no settings, so every login
    looked like a NEW handset to Instagram — which reads a session hopping
    devices as a stolen cookie, invalidates it, and raises a checkpoint. If
    anyone reintroduces a bare Client() on a login path, these checks fail.

    Entirely offline: no call here touches Instagram.
    """
    import ig_session

    print("== one device, minted once ==")
    a = ig_session.new_client("ig_t", root=tmp)
    b = ig_session.new_client("ig_t", root=tmp)
    seed = ig_session.load_device("ig_t", root=tmp)
    ua, ub = a.get_settings()["uuids"], b.get_settings()["uuids"]
    ok(ua == ub, "two separately-built clients are the SAME device")
    ok(ua == seed["uuids"], "and both match the seed file on disk")
    ok(a.user_agent == b.user_agent, "the user-agent is stable across clients")
    ok(all(ua.values()), "every uuid is populated (not silently blank)")

    print()
    print("== the seed survives what used to reroll it ==")
    before = dict(a.get_settings()["uuids"])
    a.settings["cookies"] = {"sessionid": "1%3Anot_a_real_session"}
    a.init()      # precisely what login_by_sessionid() does internally
    ok(a.get_settings()["uuids"] == before,
       "login_by_sessionid's re-init does not mint a new device")

    ig_session.save_device({"uuids": {"phone_id": "second-thoughts"}}, "ig_t",
                           root=tmp)
    ok(ig_session.load_device("ig_t", root=tmp)["uuids"] == before,
       "save_device REFUSES to overwrite an existing seed")

    print()
    print("== a stale sidecar cannot drag an old device back ==")
    stale = {"uuids": {k: "stale" for k in before},
             "user_agent": "Instagram 1.0 Android (a different phone)",
             "cookies": {"sessionid": "carried_through"}}
    c = ig_session.new_client("ig_t", settings=stale, root=tmp)
    ok(c.get_settings()["uuids"] == before, "the seed wins over the sidecar")
    ok(c.user_agent == a.user_agent, "including the user-agent")
    ok(c.get_settings()["cookies"].get("sessionid") == "carried_through",
       "while the session itself is still carried through")

    print()
    print("== gentleness (IG4) ==")
    ok(list(a.delay_range or []) == list(ig_session.DELAY_RANGE),
       f"clients are built with a delay_range of {ig_session.DELAY_RANGE}")

    print()
    print("== username -> pk resolution ==")
    # Instagram grants name LOOKUP and media READS separately: a live session
    # was measured fetching user_medias_paginated_v1('787132') while every
    # username resolver returned login_required. So a numeric id must never be
    # sent through a lookup it does not need, and a failed lookup must say so.
    from engine_ig import IGEngine

    class Blocked:
        calls = 0

        def user_id_from_username(self, name):
            Blocked.calls += 1
            raise RuntimeError("login_required")

    eng = IGEngine(Blocked())
    ok(eng.resolve_user("787132") == "787132", "a numeric id passes straight through")
    ok(eng.resolve_user(787132) == "787132", "including when it arrives as an int")
    ok(Blocked.calls == 0, "and costs ZERO lookup requests")
    try:
        eng.resolve_user("natgeo")
        ok(False, "a blocked lookup raises")
    except RuntimeError as e:
        ok("numeric id" in str(e) and "add-source" in str(e),
           "a blocked lookup names the fix (use the numeric id) rather than the trace")


# ==========================================================================
# dashboard filters
# ==========================================================================

def test_filters(tmp):
    """
    The Filters panel must narrow on REAL stored values, not guesses.

    Verified and Category have no column: they are read out of the raw tweet
    JSON (user.blue / user.blueType), which twscrape carries and we keep. If a
    schema change ever drops raw_json, these filters would silently match
    nothing — which looks exactly like "the collector found nothing". This
    pins them to actual rows so that failure is loud instead.
    """
    import json as _json
    import sqlite3

    import web

    db = pathlib.Path(tmp) / "results.db"

    async def build():
        st = Store(db, True)
        await st.open()
        await st.close()
    asyncio.run(build())

    def raw(blue, kind):
        return _json.dumps({"user": {"blue": blue, "blueType": kind,
                                     "verified": False}})

    con = sqlite3.connect(db)

    # Fill every NOT NULL column from the schema rather than naming them by
    # hand: this test is about the WHERE clause, and a row that fails to insert
    # because the table grew a column tells us nothing about filtering.
    info = list(con.execute("PRAGMA table_info(tweets)"))
    required = [r[1] for r in info if r[3] and r[4] is None and not r[5]]
    text_cols = {r[1] for r in info if "CHAR" in (r[2] or "").upper()
                 or "TEXT" in (r[2] or "").upper()}

    # created_ms: 2026-08-01, 2026-08-03, 2026-08-03
    rows = [
        (1, 1785542400000, 5000,  900000,  "en", raw(True,  "Government")),
        (2, 1785715200000, 100,   50,      "hi", raw(False, None)),
        (3, 1785715200000, 90000, 2000000, "en", raw(True,  "Business")),
    ]
    for tid, cms, views, followers, lang, rj in rows:
        row = {c: ("" if c in text_cols else 0) for c in required}
        row.update(tweet_id=tid, created_ms=cms, collected_ms=cms, source="result",
                   text="t", url="u", lang=lang, author_username="a",
                   author_followers=followers, view_count=views, like_count=0,
                   is_retweet=0, media_urls="[]", raw_json=rj)
        cols = ",".join(row)
        con.execute(f"INSERT INTO tweets({cols}) "
                    f"VALUES({','.join('?' * len(row))})", list(row.values()))
    con.commit()
    con.close()

    class Cfg:
        db_results = db
    web._CFG = Cfg()

    def n(**p):
        return web._query_tweets(p)["total"]

    print("== dates ==")
    ok(web._day_ms("2026-08-01") == 1785542400000, "a yyyy-mm-dd date becomes midnight UTC")
    ok(web._day_ms("nonsense") is None, "an unparseable date is ignored, not an error")
    ok(n() == 3, "no filters shows everything")
    ok(n(from_date="2026-08-03") == 2, "From date keeps that day and after")
    ok(n(to_date="2026-08-01") == 1, "To date INCLUDES the whole day chosen")

    print()
    print("== values that live in the raw tweet ==")
    ok(n(verified="1") == 2, "Verified only matches user.blue, not the legacy flag")
    ok(n(category="Government") == 1, "Category reads user.blueType")
    ok(n(category="Business") == 1, "and distinguishes between types")
    ok(n(category="Government", verified="1") == 1, "filters combine as AND")

    print()
    print("== numeric thresholds ==")
    ok(n(min_views="1000") == 2, "Min views is a floor, not an exact match")
    got = [r["tweet_id"] for r in web._query_tweets({"sort": "views"})["rows"]]
    ok(got == ["3", "1", "2"], f"sort=views orders by engagement, not by time ({got})")

    print()
    print("== the history-send date window ==")
    lo, hi, err = web._date_window({"from_date": "2026-07-01", "to_date": "2026-07-31"})
    ok(err is None and (hi - lo) == 31 * 86_400_000,
       "July 1–31 covers exactly 31 whole days, both ends inclusive")
    ok(web._date_window({"from_date": "2026-07-31", "to_date": "2026-07-01"})[2],
       "a backwards range is refused, not silently empty")
    ok(web._date_window({"from_date": "not-a-date"})[2],
       "an unparseable date is an error, not 'everything ever'")
    lo2, hi2, err2 = web._date_window({"since": "24h"})
    ok(err2 is None and lo2 > 0, "the rolling presets still work")
    ok(n(min_followers="1000000") == 1, "Min followers likewise")
    ok(n(lang="hi") == 1, "Language still narrows")
    ok(n(min_views="99999999") == 0, "a threshold nothing meets returns nothing, cleanly")


# ==========================================================================
# telegram: switching it on is what makes a stream watched
# ==========================================================================

def test_telegram_watch_rule(tmp):
    """
    A stream with Telegram switched on must be POLLED, not just forwarded.

    The bug this guards: the dashboard writes a search into the streams table,
    the sidebar lists it under "what we are watching", you switch Telegram on —
    and nothing polls it, because the watcher built its list from config.toml
    alone. Everything looked configured and no tweet could ever arrive.
    """
    import sqlite3

    import main
    import store as store_mod

    db = pathlib.Path(tmp) / "results.db"

    async def schema():
        st = store_mod.Store(db, False)
        await st.open()
        await st.close()
    asyncio.run(schema())

    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO streams(label,query,list_id,tab,tg_enabled,paused,created_at) "
        "VALUES(?,?,?,?,?,?,'x')",
        [("wanted",     "from:someone", None,      "Latest", 1, 0),
         ("no_telegram","from:other",   None,      "Latest", 0, 0),
         ("paused",     "from:third",   None,      "Latest", 1, 1),
         ("nothing",    "",             None,      "Latest", 1, 0),
         ("by_list",    "",             "12345",   "Latest", 1, 0),
         ("politicians","",             "999",     "Latest", 1, 0)])
    con.commit()
    con.close()

    class Cfg:
        db_results = db

    # 'politicians' stands in for a stream config.toml already declares.
    got = main._telegram_streams(Cfg(), {"politicians"}, log=lambda m: None)
    labels = sorted(s.label for s in got)

    ok(labels == ["by_list", "wanted"], f"only pollable Telegram streams are added ({labels})")
    ok("no_telegram" not in labels, "Telegram off means not watched by this rule")
    ok("paused" not in labels, "a paused stream is not woken up by Telegram being on")
    ok("nothing" not in labels, "a row with no query and no list is skipped, not polled")
    ok("politicians" not in labels,
       "a stream config.toml already declares is not added twice")
    by_list = next(s for s in got if s.label == "by_list")
    ok(by_list.list_id == "12345", "a list-backed stream keeps its list_id")


# ==========================================================================
# projects & watchlists: dashboard state that compiles into streams
# ==========================================================================

def test_projects_watchlists(tmp):
    """
    The projects layer: a watchlist is handles in the database, compiled into
    ordinary '(from:…)' streams the collector already knows how to poll.
    """
    import sqlite3

    import main
    import store as store_mod
    import web

    db = pathlib.Path(tmp) / "results.db"

    print("== handle normalization ==")
    nh = store_mod.normalize_handle
    ok(nh("@NatGeo") == "natgeo", "leading @ is stripped, case folded")
    ok(nh("https://x.com/NatGeo?s=20") == "natgeo", "a pasted profile URL works")
    ok(nh("twitter.com/NatGeo/status/1") == "natgeo", "so does the old domain")
    ok(nh("not a handle") is None, "spaces are rejected")
    ok(nh("abcdefghijklmnop") is None, "16 chars is longer than X allows")
    ok(nh("") is None and nh(None) is None, "empty input is rejected, not crashed on")

    print()
    print("== migration: the Default project ==")

    async def seed():
        st = store_mod.Store(db, False)
        await st.open()
        await st.ensure_stream("politicians", "", "Latest", True, list_id="999")
        await st.close()
        # Second open must not create a second Default or re-link anything.
        st = store_mod.Store(db, False)
        await st.open()
        projs = await st.projects()
        await st.close()
        return projs

    projs = asyncio.run(seed())
    ok(len(projs) == 1 and projs[0]["name"] == "Default",
       "an upgraded database gets exactly one Default project")
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    linked = con.execute(
        "SELECT COUNT(*) c FROM project_streams ps JOIN streams s USING(stream_id) "
        "WHERE s.label = 'politicians'").fetchone()["c"]
    con.close()
    ok(linked == 1, "existing streams are attached to Default, so nothing vanishes")

    print()
    print("== watchlist -> compiled streams ==")

    async def lifecycle():
        st = store_mod.Store(db, False)
        await st.open()
        out = {}
        out["proj"] = await st.create_project("Elections 2026")
        out["dup"] = await st.create_project("Elections 2026")
        pid = out["proj"]["project_id"]
        out["wl"] = await st.create_watchlist(pid, "Cabinet")
        wid = out["wl"]["watchlist_id"]
        # 45 handles -> 3 chunks of 20/20/5
        handles = [f"user{i:03d}" for i in range(45)]
        out["set"] = await st.set_watchlist_members(wid, add=handles)
        # Snapshot chunk 0 NOW — later steps (shrink, delete) rewrite it.
        out["q0"] = dict(st.db.execute(
            "SELECT query, watched, paused FROM streams WHERE label = ?",
            (f"wl:{wid}:0",)).fetchone())
        out["wls"] = await st.watchlists(pid)
        # One bad handle rejects the WHOLE request — nothing half-applies.
        out["bad"] = await st.set_watchlist_members(
            wid, add=["fine_handle", "not a handle"])
        out["after_bad"] = await st.watchlists(pid)
        # Shrink to 5 members -> 1 live chunk, the other two retired.
        out["shrunk"] = await st.set_watchlist_members(
            wid, remove=[f"user{i:03d}" for i in range(40)])
        # An xlist watchlist compiles to a single list-backed stream.
        out["xl"] = await st.create_watchlist(pid, "Big permanent", "xlist", "777")
        out["wls2"] = await st.watchlists(pid)
        out["gone"] = await st.delete_watchlist(wid)
        out["wls3"] = await st.watchlists(pid)
        out["pid"], out["wid"] = pid, wid
        await st.close()
        return out

    r = asyncio.run(lifecycle())
    ok("error" in r["dup"], "a duplicate project name is refused")
    ok(r["set"]["streams"] == [f"wl:{r['wid']}:0", f"wl:{r['wid']}:1", f"wl:{r['wid']}:2"],
       f"45 handles compile into 3 chunked streams ({r['set']['streams']})")
    wl = r["wls"][0]
    ok(len(wl["members"]) == 45, "every handle is stored")
    ok(len(wl["streams"]) == 3, "the watchlist reports its compiled streams")

    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    q0 = r["q0"]
    ok(q0["query"].startswith("(from:user000 OR from:user001"),
       "a chunk is an OR of from: terms, in sorted order")
    ok(q0["query"].count("from:") == 20, "a chunk holds at most 20 handles")
    ok(q0["watched"] == 1 and q0["paused"] == 0,
       "compiled streams are watched — the watcher polls them with no config entry")
    plinked = con.execute(
        "SELECT COUNT(*) c FROM project_streams ps JOIN streams s USING(stream_id) "
        "WHERE ps.project_id = ? AND s.label LIKE ?",
        (r["pid"], f"wl:{r['wid']}:%")).fetchone()["c"]
    ok(plinked == 3, "compiled streams belong to the watchlist's project")

    ok("error" in r["bad"], "one invalid handle rejects the whole request")
    ok(len(r["after_bad"][0]["members"]) == 45,
       "and the valid handle in the same request was NOT half-applied")

    retired = con.execute(
        "SELECT label, watched, paused FROM streams WHERE label IN (?,?)",
        (f"wl:{r['wid']}:1", f"wl:{r['wid']}:2")).fetchall()
    ok(all(x["paused"] == 1 and x["watched"] == 0 for x in retired),
       "shrinking retires surplus chunks: paused, not deleted")
    ok(r["shrunk"]["streams"] == [f"wl:{r['wid']}:0"],
       "5 members need exactly one live chunk")

    xl_label = r["xl"]["watchlist_id"]
    xl = con.execute("SELECT list_id, watched FROM streams WHERE label = ?",
                     (f"wl:{xl_label}:0",)).fetchone()
    ok(xl["list_id"] == "777" and xl["watched"] == 1,
       "an xlist watchlist compiles to one watched list-backed stream")

    ok(r["gone"]["removed"] and not any(w["watchlist_id"] == r["wid"] for w in r["wls3"]),
       "deleting a watchlist removes it from the dashboard")
    dead = con.execute(
        "SELECT watched, paused FROM streams WHERE label = ?",
        (f"wl:{r['wid']}:0",)).fetchone()
    ok(dead["paused"] == 1 and dead["watched"] == 0,
       "…and stops its collection without destroying what it collected")
    con.close()

    print()
    print("== the watcher picks compiled streams up ==")

    class Cfg:
        db_results = db

    got = main._telegram_streams(Cfg(), set(), log=lambda m: None)
    labels = sorted(s.label for s in got)
    ok(f"wl:{xl_label}:0" in labels,
       f"a live compiled stream is watched with no config.toml entry ({labels})")
    ok(f"wl:{r['wid']}:0" not in labels,
       "a retired one is not")

    print()
    print("== the project filter on /api/tweets ==")

    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    info = list(con.execute("PRAGMA table_info(tweets)"))
    required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
    text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}
    sid_in = con.execute("SELECT stream_id FROM streams WHERE label = ?",
                         (f"wl:{xl_label}:0",)).fetchone()["stream_id"]
    sid_out = con.execute("SELECT stream_id FROM streams WHERE label = 'politicians'"
                          ).fetchone()["stream_id"]
    for tid, sid in ((11, sid_in), (12, sid_in), (13, sid_out)):
        row = {c: ("" if c in text_cols else 0) for c in required}
        row.update(tweet_id=tid, created_ms=1, collected_ms=1, source="result")
        cols = ",".join(row)
        con.execute(f"INSERT INTO tweets({cols}) VALUES({','.join('?' * len(row))})",
                    list(row.values()))
        con.execute("INSERT INTO tweet_hits(stream_id, tweet_id, first_seen_ms) "
                    "VALUES(?,?,1)", (sid, tid))
    con.commit()
    con.close()

    web._CFG = Cfg()
    all_n = web._query_tweets({})["total"]
    proj_n = web._query_tweets({"project": str(r["pid"])})["total"]
    ok(all_n == 3, f"without the filter, every stored tweet shows ({all_n})")
    ok(proj_n == 2, f"project= narrows to what that project's streams collected ({proj_n})")
    ok(web._query_tweets({"project": "nonsense"})["total"] == 3,
       "an unparseable project id narrows nothing rather than erroring")

    print()
    print("== the live stream cursor ==")
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows_all = web._live_rows(con, 0, 0)
    ok(len(rows_all) == 3, "from cursor zero, every stored tweet replays, oldest first")
    ok([int(x["tweet_id"]) for x in rows_all] == sorted(int(x["tweet_id"]) for x in rows_all),
       "in collection order — the same walk delivery uses")
    ok(len(web._live_rows(con, 0, 0, project=r["pid"])) == 2,
       "the project scope holds on the live stream too")
    first = rows_all[0]
    resumed = web._live_rows(con, first["collected_ms"], int(first["tweet_id"]))
    ok(len(resumed) == 2 and int(resumed[0]["tweet_id"]) != int(first["tweet_id"]),
       "the composite cursor resumes without repeating or skipping")
    con.close()


# ==========================================================================
# collections: curation boards over collected tweets
# ==========================================================================

def test_collections(tmp):
    """Boards reference tweets, never copy them; unpinning destroys nothing."""
    import sqlite3

    import store as store_mod

    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        out = {}
        pid = (await st.create_project("Newsroom"))["project_id"]

        # Two minimal tweets to pin.
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}
        for tid in (101, 102):
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=tid, collected_ms=tid, source="result")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))

        out["made"] = await st.create_collection(pid, "Floods day 2")
        cid = out["made"]["collection_id"]
        out["dup"] = await st.create_collection(pid, "Floods day 2")
        out["pin"] = await st.collection_pin(cid, add=[101, 102, 999])
        out["repin"] = await st.collection_pin(cid, add=[101])
        out["rows"] = await st.collection_rows(cid)
        out["unpin"] = await st.collection_pin(cid, remove=[101])
        out["rows2"] = await st.collection_rows(cid)
        out["lists"] = await st.collections(pid)
        out["gone"] = await st.delete_collection(cid)
        out["tweets_left"] = st.db.execute(
            "SELECT COUNT(*) c FROM tweets").fetchone()["c"]
        await st.close()
        return out

    r = asyncio.run(run())
    ok("error" in r["dup"], "a duplicate board name in one project is refused")
    ok(r["pin"]["pinned"] == 2 and r["pin"].get("not_found") == ["999"],
       "real posts pin; a stale id is reported, never stored as a hole")
    ok(len(r["rows"]) == 2, "the board holds exactly the pinned posts")
    ok(r["repin"]["pinned"] == 1 and len(r["rows"]) == 2,
       "pinning the same post twice stays one entry")
    ok(r["unpin"]["removed"] == 1 and len(r["rows2"]) == 1,
       "unpinning removes from the board only")
    ok(r["lists"][0]["items"] == 1, "the board list reports its live count")
    ok(r["gone"]["removed"] and r["tweets_left"] == 2,
       "deleting a whole board leaves every collected tweet in place")


# ==========================================================================
# keywords, stream assignment, per-project delivery
# ==========================================================================

def test_keywords_and_project_delivery(tmp):
    import sqlite3

    import store as store_mod
    import webhook as wh

    db = pathlib.Path(tmp) / "results.db"

    print("== keyword terms ==")
    nt, ct = store_mod.normalize_term, store_mod.compile_term
    ok(nt("  finance   AND gst ") == "finance AND gst", "whitespace collapses")
    ok(nt('"vishnu deo sai"') == '"vishnu deo sai"', "phrases pass through")
    ok(nt('bad "quote') is None, "an unbalanced quote is rejected, not sent to X")
    ok(nt("AND") is None, "a bare operator is rejected")
    ok(ct("finance AND gst") == "(finance gst)",
       "AND compiles to X's implicit-and (a space), grouped")
    ok(ct("finance and GST") == "(finance GST)", "AND is case-insensitive")
    ok(ct("#Chhattisgarh") == "#Chhattisgarh", "plain terms pass through")

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        out = {}
        pid_a = (await st.create_project("A"))["project_id"]
        pid_b = (await st.create_project("B"))["project_id"]

        wid = (await st.create_watchlist(pid_a, "Topics", "keywords"))["watchlist_id"]
        out["set"] = await st.set_watchlist_members(
            wid, add=["finance AND gst", '"exact phrase"', "#tag"])
        out["q"] = st.db.execute("SELECT query FROM streams WHERE label = ?",
                                 (f"wl:{wid}:0",)).fetchone()["query"]
        out["badd"] = await st.set_watchlist_members(wid, add=['broken "quote'])
        # Filters recompile every stream with the operator suffix.
        out["setf"] = await st.set_watchlist_filters(
            wid, {"skip_retweets": True, "skip_quotes": True})
        out["qf"] = st.db.execute("SELECT query FROM streams WHERE label = ?",
                                  (f"wl:{wid}:0",)).fetchone()["query"]
        out["clearf"] = await st.set_watchlist_filters(wid, {})
        out["qc"] = st.db.execute("SELECT query FROM streams WHERE label = ?",
                                  (f"wl:{wid}:0",)).fetchone()["query"]

        # Stream assignment: B takes over a stream, A lets it go.
        sid = st.db.execute("SELECT stream_id FROM streams WHERE label = ?",
                            (f"wl:{wid}:0",)).fetchone()["stream_id"]
        await st.attach_stream(pid_b, sid)
        out["both"] = [s for s in await st.streams_with_projects()
                       if s["stream_id"] == sid][0]["projects"]
        await st.detach_stream(pid_a, sid)
        out["after"] = [s for s in await st.streams_with_projects()
                        if s["stream_id"] == sid][0]["projects"]

        # Per-project delivery: a target scoped to B sees only B's tweets.
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}
        other = st.db.execute(
            "INSERT INTO streams(label, query, tab, created_at) "
            "VALUES('elsewhere', 'q', 'Latest', 'x')").lastrowid
        await st.attach_stream(pid_a, other)
        for tid, s in ((11, sid), (12, sid), (13, other)):
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=tid, collected_ms=tid, source="result")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))
            st.db.execute("INSERT INTO tweet_hits(stream_id, tweet_id, first_seen_ms) "
                          "VALUES(?,?,?)", (s, tid, tid))
        out["b_rows"] = [r["tweet_id"] for r in
                         await st.tweets_after(0, 0, 50, project_id=pid_b)]
        out["all_rows"] = [r["tweet_id"] for r in await st.tweets_after(0, 0, 50)]

        # Target CRUD + the sender's view of it.
        out["bad_secret"] = await st.create_delivery_target(
            pid_b, "webhook", "wt", "https://x.example/h", "not lowercase ok?")
        out["made"] = await st.create_delivery_target(
            pid_b, "webhook", "wt", "https://x.example/h", "WH_TEST_SECRET")
        os.environ["WH_TEST_SECRET"] = "s3cret"
        built = await wh.db_targets(st, log=lambda m: None)
        out["built"] = [(t.label, t.kind, t.project_id, t.secret) for t in built]
        os.environ.pop("WH_TEST_SECRET")
        out["built_missing"] = await wh.db_targets(st, log=lambda m: None)
        out["gone"] = await st.delete_delivery_target(out["made"]["target_id"])
        out["pids"] = (pid_a, pid_b)
        await st.close()
        return out

    r = asyncio.run(run())
    print()
    print("== keyword watchlists compile ==")
    ok(r["q"] == '("exact phrase" OR #tag OR (finance gst))',
       f"terms OR-combine into one X query ({r['q']})")
    ok("error" in r["badd"], "a bad term rejects the request, nothing half-applies")
    ok(r["qf"].endswith(" -filter:retweets -filter:quote"),
       f"saving filters recompiles the stream with the suffix ({r['qf']!r})")
    ok(r["qc"] == r["q"], "clearing filters restores the bare query")

    print()
    print("== a new watchlist is picked up without a restart ==")
    from collector import Collector

    async def discovery():
        st = store_mod.Store(db, False)
        await st.open()
        # Collector starts with an empty stream set (nothing existed yet).
        col = Collector(engine=None, store=st, streams=[], log=lambda *a: None)
        await col.prepare()
        n0 = await col.discover_new_streams()
        # Now a watchlist is created AFTER the collector started.
        pid = (await st.create_project("Live"))["project_id"]
        wid = (await st.create_watchlist(pid, "New"))["watchlist_id"]
        await st.set_watchlist_members(wid, add=["someone", "another"])
        n1 = await col.discover_new_streams()
        n2 = await col.discover_new_streams()   # idempotent — no double-add
        labels = [s.label for s in col.streams]
        # Interval control writes through to the stream.
        await st.set_watchlist_interval(wid, 900)
        iv = st.db.execute("SELECT min_interval_s FROM streams WHERE label = ?",
                           (f"wl:{wid}:0",)).fetchone()["min_interval_s"]
        await st.close()
        return n0, n1, n2, labels, wid, iv

    # The global Start/Stop flag the collector honors.
    async def pauseflag():
        st = store_mod.Store(db, False)
        await st.open()
        col = Collector(engine=None, store=st, streams=[], log=lambda *a: None)
        a = col._paused()
        await st.set_collection_paused(True)
        b = col._paused()
        await st.set_collection_paused(False)
        c = col._paused()
        await st.close()
        return a, b, c
    pa, pb, pc = asyncio.run(pauseflag())
    ok(pa is False and pb is True and pc is False,
       "the Start/Stop flag round-trips and the collector reads it")

    d0, d1, d2, dlabels, dwid, div = asyncio.run(discovery())
    ok(d1 == 1 and f"wl:{dwid}:0" in dlabels,
       f"a watchlist created after startup is discovered and polled — the exact "
       f"'new project not collecting' fix ({dlabels})")
    ok(d2 == 0, "re-scanning does not add the same stream twice")
    ok(div == 900, "the per-watchlist 'check every' interval writes to its streams")

    print()
    print("== collection filters ==")
    nf, fs = store_mod.normalize_filters, store_mod.filters_suffix
    clean, e = nf({"skip_retweets": True, "skip_quotes": True, "lang": "HI",
                   "min_likes": "50", "skip_replies": False})
    ok(e is None and clean == {"skip_retweets": True, "skip_quotes": True,
                               "lang": "hi", "min_likes": 50},
       "checkboxes normalize; off-boxes and empties drop out")
    ok(fs(clean) == " -filter:retweets -filter:quote lang:hi min_faves:50",
       f"filters compile to X advanced-search operators ({fs(clean)!r})")
    ok(nf({"nonsense": True})[1], "an unknown filter key is an error, never ignored")
    ok(nf({"lang": "hindi"})[1], "a wrong language code is refused with guidance")
    ok(fs({}) == "", "no filters means an untouched query")

    print()
    print("== stream assignment ==")
    pa, pb = r["pids"]
    ok(sorted(r["both"]) == sorted([pa, pb]), "a stream can feed two projects at once")
    ok(r["after"] == [pb], "detaching from one leaves the other untouched")

    print()
    print("== per-project delivery ==")
    ok(r["b_rows"] == [11, 12], f"a project-scoped target sees only its posts ({r['b_rows']})")
    ok(r["all_rows"] == [11, 12, 13], "an unscoped target still sees everything")
    ok("error" in r["bad_secret"], "secret_env must be an ENV NAME, never a secret")
    ok(r["built"] == [("dt:1", "webhook", pb, "s3cret")],
       "a target builds into a sender with its secret from .env")
    ok(r["built_missing"] == [],
       "with the secret missing the target is skipped loudly, never sent unsigned")
    ok(r["gone"], "targets delete cleanly (cursor row included)")


# ==========================================================================
# velocity alerts: counts, thresholds, cooldown
# ==========================================================================

def test_alerts(tmp):
    """The whole pipeline: rule -> pace measurement -> fire -> cooldown."""
    import alerts as al
    import store as store_mod

    print("== the decision, on its own ==")
    ok(al.decide(30, 5.0, 3.0, 10) == (True, 6.0),
       "30/hour against a usual 5/hour is 6x - fires at threshold 3")
    ok(al.decide(12, 5.0, 3.0, 10)[0] is False,
       "2.4x does not clear a 3x threshold")
    ok(al.decide(8, 0.5, 3.0, 10)[0] is False,
       "a big ratio still never fires under min_posts - quiet scopes stay quiet")
    ok(al.decide(40, 0.0, 3.0, 10) == (True, None),
       "no history yet + a real burst fires, with no ratio to report")

    print()
    print("== end to end against a store ==")
    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        out = {}
        pid = (await st.create_project("Desk"))["project_id"]
        wid = (await st.create_watchlist(pid, "Cabinet"))["watchlist_id"]
        await st.set_watchlist_members(wid, add=["someone"])
        sid = st.db.execute("SELECT stream_id FROM streams WHERE label = ?",
                            (f"wl:{wid}:0",)).fetchone()["stream_id"]

        now = 2_000_000_000_000  # fixed clock: the test owns time
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}

        def put(tid, ms):
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=ms, collected_ms=ms, source="result")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))
            st.db.execute(
                "INSERT INTO tweet_hits(stream_id, tweet_id, first_seen_ms) VALUES(?,?,?)",
                (sid, tid, ms))

        # Usual pace: 24 posts spread over the prior day = 1/hour.
        for i in range(24):
            put(1000 + i, now - 3_600_000 - (i + 1) * 3_500_000 // 1)
        # The surge: 12 posts in the last hour = 12x usual.
        for i in range(12):
            put(2000 + i, now - i * 60_000)

        out["made"] = await st.create_alert(pid, wid, threshold=3.0, min_posts=10)
        aid = out["made"]["alert_id"]

        sent = []
        async def record(alert, msg):
            sent.append(msg)
            return True, ""

        out["fired1"] = await al.tick(st, None, log=lambda m: None,
                                      send=record, now_ms=now)
        out["fired2"] = await al.tick(st, None, log=lambda m: None,
                                      send=record, now_ms=now + 60_000)
        out["fired3"] = await al.tick(st, None, log=lambda m: None,
                                      send=record, now_ms=now + al.COOLDOWN_MS + 60_000)
        out["sent"] = sent
        out["disabled"] = await st.update_alert(aid, {"enabled": 0})
        out["fired4"] = await al.tick(st, None, log=lambda m: None,
                                      send=record, now_ms=now + al.COOLDOWN_MS * 3)
        await st.close()
        return out

    r = asyncio.run(run())
    ok(r["fired1"] == 1, "a 12x surge over a 3x rule fires once")
    ok("Cabinet" in r["sent"][0] and "12 posts" in r["sent"][0],
       f"the ping names the scope and the count: {r['sent'][0][:70]}")
    ok(r["fired2"] == 0, "a minute later the cooldown holds - no spam")
    ok(r["fired3"] == 1, "after the cooldown a continuing surge pings again")
    ok(r["disabled"] and r["fired4"] == 0, "a paused rule never fires")


def test_facebook(tmp):
    """The Facebook store + collect loop (offline; engine is server-only)."""
    import store_fb
    import collect_fb
    import engine_fb

    db = pathlib.Path(tmp) / "fb_results.db"
    st = store_fb.Store(db).open()
    st.add_source("narendramodi", project_id=7)

    print("== bandwidth cap ==")
    meter = str(pathlib.Path(tmp) / "m.db")
    ok(engine_fb._bandwidth_ok(meter, 1000)[0], "under cap: fetch allowed")
    engine_fb._record_bytes(meter, 1200)
    ok(not engine_fb._bandwidth_ok(meter, 1000)[0],
       "over the monthly cap: fetching refuses (no runaway)")

    print()
    print("== store + collect ==")

    class FakeEng:
        async def fetch_page(self, handle, max_scroll=1):
            return [
                {"post_id": f"{handle}:1", "page": handle, "url": "u1",
                 "created_ms": 1785000000000, "author_name": "Modi", "text": "a",
                 "media": [{"type": "video", "url": "v", "thumb": "t"}]},
                {"post_id": f"{handle}:2", "page": handle, "url": "u2",
                 "created_ms": 1785000600000, "author_name": "Modi", "text": "b",
                 "media": []},
            ]

    src = st.sources(enabled_only=True)[0]
    n1 = asyncio.run(collect_fb.collect_source(FakeEng(), st, src))
    n2 = asyncio.run(collect_fb.collect_source(FakeEng(), st, src))
    ok(n1 == 2, "first pass saves both posts")
    ok(n2 == 0, "second pass dedups on post id — nothing doubles")
    ok(st.total(project_id=7) == 2, "project-scoped total is right")
    ok(st.watermark("narendramodi") == 1785000600000, "watermark advances to newest")

    feed = store_fb.to_feed(st.recent(project_id=7)[0])
    ok(feed["platform"] == "facebook", "posts map to the shared feed shape")
    vid = [r for r in st.recent(project_id=7) if store_fb.to_feed(r)["media"]]
    ok(any(store_fb.to_feed(r)["media"] and store_fb.to_feed(r)["media"][0].get("thumb")
           for r in st.recent(project_id=7)),
       "a Facebook video carries its thumbnail through to the feed/delivery shape")

    print()
    print("== JSON extraction path (layout-proof) ==")
    eng = engine_fb.FacebookEngine()
    items = [{
        "id": "999", "author": "Narendra Modi",
        "author_avatar": "https://scontent/pic.jpg", "permalink": None,
        "text": "quit india", "created_ms": 1785000700000,
        "media": [{"type": "photo", "url": "p.jpg", "thumb": "p.jpg"}],
        "like_count": 33000, "comment_count": 2600, "share_count": 2500,
    }]
    built = eng._build_from_json("narendramodi", items)
    ok(built[0]["post_id"] == "narendramodi:999", "a JSON post keys on its story id")
    ok(built[0]["url"].endswith("/narendramodi/posts/999"),
       "a missing permalink is synthesized from handle + id")
    ok(built[0]["author_avatar"] == "https://scontent/pic.jpg",
       "profile picture is carried from the JSON")
    built[0]["project_id"] = 7
    st.upsert(built[0]); st.db.commit()
    row = [r for r in st.recent(project_id=7) if r["post_id"] == "narendramodi:999"][0]
    got = store_fb.to_feed(row)
    ok(got["author_avatar"] == "https://scontent/pic.jpg",
       "profile picture survives store → feed")
    ok(got["like_count"] == 33000 and got["created_at"],
       "reaction count and exact post time survive store → feed")

    print()
    print("== graphql parsing (logged-in feed data) ==")
    sample = {"data": {"node": {"timeline_list_feed_units": {"edges": [{"node": {
        "__typename": "Story", "post_id": "555", "creation_time": 1785000000,
        "wwwURL": "https://www.facebook.com/narendramodi/posts/pfbidZZ",
        "message": {"text": "Namaste India", "ranges": []},
        "actors": [{"__typename": "Page", "name": "Narendra Modi",
                    "profile_picture": {"uri": "https://cdn/modi.jpg"}}],
        "attachments": [{"media": {"__typename": "Photo",
                                   "image": {"uri": "https://cdn/pic1.jpg"}}}],
        "feedback": {"reaction_count": {"count": 41000},
                     "comment_rendering_instance": {"comments": {"total_count": 900}},
                     "share_count": {"count": 700}},
    }}]}}}}
    gp = engine_fb._stories_from_graphql([json.dumps(sample)])
    ok(len(gp) == 1 and gp[0]["id"] == "555", "a Story is pulled out of a graphql response")
    ok(gp[0]["author"] == "Narendra Modi" and gp[0]["author_avatar"] == "https://cdn/modi.jpg",
       "author name + profile picture come from the graphql payload")
    ok(gp[0]["like_count"] == 41000 and gp[0]["media"][0]["url"] == "https://cdn/pic1.jpg",
       "reaction count and media come from the graphql payload")

    print()
    print("== duplicate guard across id schemes ==")
    a = {"post_id": "narendramodi:pfbidAAA", "page": "narendramodi",
         "text": "One and the same caption here", "project_id": 7, "media": []}
    b = {"post_id": "narendramodi:888888", "page": "narendramodi",
         "text": "One and the same caption here", "project_id": 7, "media": []}
    ok(st.upsert(a) is True, "first copy of a post saves")
    ok(st.upsert(b) is False,
       "the SAME post arriving with a different id is refused (content signature)")
    st.db.commit()

    print()
    print("== favorites feed: per-author attribution ==")
    feed_items = [
        {"id": "10", "author": "Narendra Modi", "author_handle": "narendramodi",
         "author_avatar": "a.jpg", "permalink": None, "text": "one",
         "created_ms": 1785000000000, "media": [{"type": "photo", "url": "p", "thumb": "p"}]},
        {"id": "11", "author": "Amit Shah", "author_handle": None,
         "author_url": "https://www.facebook.com/amitshahofficial",
         "permalink": None, "text": "two", "media": ["img.jpg"]},
        {"id": "12", "author": "No Handle", "author_handle": None,
         "author_url": None, "text": "three", "media": []},
    ]
    fed = eng._build_feed(feed_items)
    ok(len(fed) == 2, "a post with no resolvable author page is dropped")
    ok(fed[0]["post_id"] == "narendramodi:10", "post keyed on its OWN author page")
    ok(fed[1]["page"] == "amitshahofficial",
       "author page is recovered from the profile URL when no handle field")
    ok(fed[1]["media"][0]["url"] == "img.jpg",
       "bare-string media (DOM path) is normalized to the media shape")
    st.close()

    print()
    print("== favorites collection attributes to tracked pages only ==")
    import engine_fb as _efb

    class _FakeFav:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def fetch_favorites(self, max_scroll=12):
            return [
                {"post_id": "narendramodi:20", "page": "narendramodi", "url": "u",
                 "text": "tracked page post", "media": [], "project_id": None},
                {"post_id": "randompage:21", "page": "randompage", "url": "u2",
                 "text": "untracked page post", "media": [], "project_id": None},
            ]
    _orig = _efb.FacebookEngine
    _orig_login = collect_fb._can_log_in
    _efb.FacebookEngine = lambda *a, **k: _FakeFav()
    collect_fb._can_log_in = lambda: True     # no real FB creds in the test env
    try:
        got = asyncio.run(collect_fb.run_favorites(str(db)))
    finally:
        _efb.FacebookEngine = _orig
        collect_fb._can_log_in = _orig_login
    ok(got == 1, "only the post from a tracked page is saved (untracked page ignored)")
    with store_fb.Store(db) as st2:
        rows = [r for r in st2.recent(project_id=7) if r["post_id"] == "narendramodi:20"]
        ok(len(rows) == 1 and rows[0]["project_id"] == 7,
           "the favorites post is attributed to the project that tracks its page")


# ==========================================================================
# runner
# ==========================================================================

async def run_webhook(tmp):
    """
    Delivery: signing, the cursor, and what happens when the receiver is down.

    Offline — the receiver is a real HTTP server on localhost, so the signing
    and the status handling are exercised for real rather than mocked.
    """
    import json as _json
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import config as _C
    import webhook as wh

    print("== signing ==")
    secret, body = "s3cret_shared_value", b'{"tweets":[]}'
    ts = str(int(time.time()))
    sig = wh.sign(secret, ts, body)
    ok(wh.verify(secret, ts, body, sig), "a genuine delivery verifies")
    ok(not wh.verify("other", ts, body, sig), "a different secret does not")
    ok(not wh.verify(secret, ts, body + b"x", sig), "a tampered body does not")
    old = str(int(time.time()) - 3600)
    ok(not wh.verify(secret, old, body, wh.sign(secret, old, body)),
       "an hour-old delivery is refused (replay)")
    ok(not wh.verify(secret, ts, body, ""), "a missing signature is refused")

    print()
    print("== payload media ==")
    row = {"tweet_id": 5, "text": "clip",
           "media_json": _json.dumps([{"type": "video",
                                       "url": "https://video.twimg.com/v.mp4",
                                       "thumb": "https://pbs.twimg.com/t.jpg",
                                       "duration": 12.3}]),
           "media_urls": _json.dumps(["https://video.twimg.com/v.mp4"])}
    j = wh._tweet_json(row, [])
    ok(j["media"] == [{"type": "video", "url": "https://video.twimg.com/v.mp4",
                       "thumb": "https://pbs.twimg.com/t.jpg", "duration": 12.3}],
       "the payload carries structured media — type, url and the video THUMBNAIL — "
       "not just the flat mp4 list (a receiver cannot show a still it never got)")
    ok(j["media_urls"] == ["https://video.twimg.com/v.mp4"],
       "and the frozen flat media_urls shape is untouched")
    ok(wh._tweet_json({"tweet_id": 5, "media_json": "not-json"}, [])["media"] == [],
       "malformed media_json degrades to [] rather than blocking delivery")

    print()
    print("== delivery ==")
    got, fail = [], {"on": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if fail["on"]:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            got.append((wh.verify(secret, self.headers.get("X-XS-Timestamp"),
                                  raw, self.headers.get("X-XS-Signature")),
                        _json.loads(raw)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["WH_SECRET"] = secret

    st = Store(tmp / "results.db", False)
    await st.open()
    try:
        # Populate through the real collector path, so what gets delivered is
        # exactly what a real poll stores — not a hand-built row that might
        # differ in some field the payload depends on.
        stream = Stream()
        sid = await st.ensure_stream(stream.label, stream.query, "Latest", True)
        await poll_once(ReplayEngine([ids_at(10, 15, 20, 25, 30)]), st, stream, sid)

        hook = _C.WebhookCfg(label="t", url=f"http://127.0.0.1:{srv.server_address[1]}/h",
                             secret_env="WH_SECRET", batch_size=2)
        import httpx
        async with httpx.AsyncClient() as client:
            sent = await wh.pump(hook, st, client, log=lambda m: None)
            ok(sent == 5, f"every collected tweet is delivered ({sent})")
            ok(all(sig for sig, _ in got), "every batch is signed")
            ids = [x["tweet_id"] for _, p in got for x in p["tweets"]]
            ok(len(ids) == len(set(ids)) == 5, "batching produces no duplicates")
            ok(all(isinstance(i, str) for i in ids),
               "tweet_id crosses the wire as a string (2^53 safety)")
            ok(all("media" in x and isinstance(x["media"], list)
                   for _, p in got for x in p["tweets"]),
               "every delivered tweet carries the structured media list, so "
               "images and video thumbnails reach the receiver")
            ok(await wh.pump(hook, st, client, log=lambda m: None) == 0,
               "nothing new means nothing sent")

            # Receiver falls over. The cursor must NOT move.
            fail["on"] = True
            await poll_once(ReplayEngine([ids_at(1)]), st, stream, sid)
            before = await st.webhook_cursor("t")
            await wh.pump(hook, st, client, log=lambda m: None)
            after = await st.webhook_cursor("t")
            ok(after["last_tweet_id"] == before["last_tweet_id"],
               "a failed delivery leaves the cursor alone, so nothing is skipped")
            ok(after["failures"] == 1 and after["last_error"],
               f"the failure is recorded with its reason: {(after['last_error'] or '')[:24]}")
            ok(after["next_attempt_ms"] > int(time.time() * 1000),
               "and backs off rather than hammering a receiver that is down")

            # It comes back. Catching up must need no manual step.
            fail["on"] = False
            await st.webhook_advance("t", before["last_ms"], before["last_tweet_id"], 0)
            sent = await wh.pump(hook, st, client, log=lambda m: None)
            ok(sent == 1, "the receiver recovers and the missed tweet arrives")
            ok((await st.webhook_cursor("t"))["failures"] == 0,
               "and the failure count resets")

        ok(wh.backoff_ms(1) < wh.backoff_ms(3) <= wh.BACKOFF_MAX_S * 1000,
           "back-off grows but stays capped")

        print()
        print("== filters ==")

        class _F:
            skip_retweets = True
            min_likes = 10

        ok(not wh._wanted(_F(), {"is_retweet": 1, "like_count": 999}),
           "skip_retweets drops a retweet")
        ok(not wh._wanted(_F(), {"is_retweet": 0, "like_count": 9}),
           "min_likes drops a quiet tweet")
        ok(wh._wanted(_F(), {"is_retweet": 0, "like_count": 10}),
           "and keeps one that clears the bar")

        # The cursor MUST move past rows we chose not to send. If it did not, a
        # stream whose every tweet is filtered out would re-read the same rows
        # forever, deliver nothing, and never advance.
        hook.min_likes = 10 ** 9
        before = await st.webhook_cursor("t")
        async with httpx.AsyncClient() as client:
            await poll_once(ReplayEngine([ids_at(0)]), st, stream, sid)
            await wh.pump(hook, st, client, log=lambda m: None)
        after = await st.webhook_cursor("t")
        ok(after["last_tweet_id"] > before["last_tweet_id"],
           "the cursor advances past filtered-out tweets, so a fully filtered "
           "stream cannot wedge delivery")
        hook.min_likes = 0

        print()
        print("== telegram ==")
        msgs = wh.tg_format(
            [{"tweet_id": 1, "author_display_name": "A & B", "author_username": "ab",
              "text": "R & D <script>x</script>", "url": "https://t.co/abc", "like_count": 3}],
            {1: ["ui:from:RajeshGupta5766 -filter:replies -filter:retweets"]})
        ok(len(msgs) == 1 and "&amp;" in msgs[0] and "&lt;script&gt;" in msgs[0],
           "telegram escapes the three characters its HTML mode cares about")
        ok("<a href" not in msgs[0],
           "no hyperlink markup — the tweet's own t.co link is left as written")
        ok("ui:from:" not in msgs[0], "no stream label")
        ok("@ab" not in msgs[0] and "A &amp; B" not in msgs[0],
           "no handle and no display name — the message is the tweet, nothing else")
        ok("♥" not in msgs[0] and "3" not in msgs[0], "no like count")

        plain = wh.tg_format([{
            "tweet_id": 5, "author_display_name": "Someone", "author_username": "someone",
            "text": "just the words https://t.co/keepThis",
            "url": "https://x.com/someone/status/5", "like_count": 99}])[0]
        ok(plain == "just the words https://t.co/keepThis",
           "the message is EXACTLY the tweet text, byte for byte")

        media_only = wh.tg_format([{
            "tweet_id": 6, "author_display_name": "S", "author_username": "s",
            "text": "", "url": "https://x.com/s/status/6", "like_count": 0}])[0]
        ok(media_only == "https://x.com/s/status/6",
           "a post with no text falls back to its link — Telegram rejects an "
           "empty message, so the post would otherwise be lost")

        # One message per tweet is the whole point: several posts packed into one
        # block cannot be forwarded, replied to or deleted individually.
        many = [{"tweet_id": i, "author_display_name": "n", "author_username": "n",
                 "text": "short", "url": "https://x.com/", "like_count": 0}
                for i in range(12)]
        ok(len(wh.tg_format(many)) == 12,
           "12 tweets produce 12 separate messages, never packed together")

        big = [{"tweet_id": i, "author_display_name": "n", "author_username": "n",
                "text": "y" * 900, "url": "https://x.com/", "like_count": 0}
               for i in range(12)]
        packed = wh.tg_format(big)
        ok(len(packed) == 12 and all(len(m) <= wh.TG_MAX_CHARS for m in packed),
           "long tweets stay one-per-message and each is under Telegram's limit")

        # ONE link per message, and it is the tweet's own. We add none: X already
        # ends the text with a t.co pointing at the post, so a permalink of ours
        # underneath printed the same destination twice.
        onelink = wh.tg_format([{
            "tweet_id": 9, "author_display_name": "R", "author_username": "r",
            "text": "look at this https://t.co/selfLink1",
            "urls": "[]", "media_urls": '["https://pbs.twimg.com/media/x.jpg"]',
            "url": "https://x.com/r/status/9", "like_count": 1}])[0]
        ok(onelink.count("http") == 1,
           "exactly one link in the message — the tweet's own t.co")
        ok("t.co/selfLink1" in onelink, "the text is passed through untouched")
        ok("x.com/r/status/9" not in onelink,
           "and no permalink of ours is appended")

        # "-filter:replies" is a hint to X's SEARCH. It is honoured imperfectly
        # and does nothing at all for a stream collected another way, so replies
        # still reached the channel. is_reply is our own parsed field.
        class _H:
            skip_retweets = False
            skip_replies = True
            min_likes = 0
        rr = [{"tweet_id": 1, "is_reply": 1, "like_count": 0},
              {"tweet_id": 2, "is_reply": 0, "like_count": 0}]
        ok([r["tweet_id"] for r in rr if wh._wanted(_H(), r)] == [2],
           "skip_replies drops replies at delivery, whatever the search query said")
        _H.skip_replies = False
        ok([r["tweet_id"] for r in rr if wh._wanted(_H(), r)] == [1, 2],
           "and leaves them alone when it is off")

        # Delivery keys on when a tweet was COLLECTED, so a stream that has just
        # started pushes its whole backlog. max_age_h bounds it by when the
        # tweet was actually POSTED.
        now_ms = int(time.time() * 1000)
        class _A:
            skip_retweets = False
            skip_replies = False
            min_likes = 0
            max_age_h = 24
        aged = [{"tweet_id": 1, "created_ms": now_ms - 2 * 3600_000, "like_count": 0},
                {"tweet_id": 2, "created_ms": now_ms - 6 * 86_400_000, "like_count": 0}]
        ok([r["tweet_id"] for r in aged if wh._wanted(_A(), r)] == [1],
           "max_age_h drops a six-day-old post collected today, keeps a fresh one")
        _A.max_age_h = 0
        ok([r["tweet_id"] for r in aged if wh._wanted(_A(), r)] == [1, 2],
           "and 0 means no age limit at all")

        ok(wh.TG_GAP_S >= 3.0,
           f"the gap between sends respects ~20 messages/minute per group "
           f"({wh.TG_GAP_S}s) — required now that every tweet is its own message")

        huge = wh.tg_format(
            [{"tweet_id": 1, "author_display_name": "n", "author_username": "n",
              "text": "z" * 9000, "url": "https://x.com/1", "like_count": 0}])
        ok(len(huge) == 1 and len(huge[0]) <= wh.TG_MAX_CHARS,
           "one oversized tweet is trimmed rather than dropped or rejected")

        print()
        print("== stream settings and removal ==")
        # A second stream matching some of the same tweets, so the "keep what
        # another list also has" rule gets exercised rather than assumed.
        s2 = await st.ensure_stream("other", "q2")
        shared = await st.tweets_after(0, 0, 3)
        for r in shared:
            st.db.execute(
                "INSERT OR IGNORE INTO tweet_hits(stream_id, tweet_id, first_seen_ms) "
                "VALUES(?,?,?)", (s2, r["tweet_id"], r["collected_ms"]))
        total_before = await st.count_tweets()

        ok(await st.set_stream_settings("s", {"paused": 1, "min_interval_s": 300}),
           "settings save against a real stream")
        cfg_row = await st.stream_settings("s")
        ok(cfg_row["paused"] == 1 and cfg_row["min_interval_s"] == 300,
           "and read back")
        await st.set_stream_settings("s", {"min_interval_s": None})
        ok((await st.stream_settings("s"))["min_interval_s"] is None,
           "clearing an override stores NULL, not 0 (0 would poll flat out)")
        ok(not await st.set_stream_settings("nope", {"paused": 1}),
           "settings for a stream that does not exist are refused, not invented")

        r = await st.forget_stream("s", delete_tweets=False)
        ok(r["found"] and r["tweets_deleted"] == 0, "stop watching removes no tweets")
        ok(await st.count_tweets() == total_before,
           "and every tweet is still there afterwards")
        ok(not (await st.stream_settings("s")), "while the stream itself is gone")

        # 's' is gone, so these three are now 'other' alone and go with it.
        r = await st.forget_stream("other", delete_tweets=True)
        ok(r["tweets_deleted"] == len(shared),
           f"deleting data removes that stream's tweets ({r['tweets_deleted']})")
        ok(await st.count_tweets() == total_before - len(shared),
           "and only those")
        ok(not (await st.forget_stream("other"))["found"],
           "and removing it twice is not an error, just found=False")
    finally:
        await st.close()
        srv.shutdown()


def main():
    root = HERE / ".tmp"
    cwd = os.getcwd()
    shutil.rmtree(root, ignore_errors=True)

    def fresh(name):
        # A separate directory per section, NOT a wiped-and-reused one.
        # twscrape memoises "this db path is already migrated" in a class-level
        # dict keyed by path (db.py:109), so a recreated file at the same path
        # would skip migration and fail with "no such table: accounts".
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    try:
        section("units")
        globals()["TMP"] = fresh("units")
        test_snowflake()
        test_normalize()
        test_config()
        test_lists()
        test_account_flags()
        test_auth()

        section("session (the three auth bugs)")
        asyncio.run(run_session(fresh("session")))

        section("engine (parsing + account-lock release)")
        test_parse()
        asyncio.run(test_lock_release(fresh("engine")))

        section("collector (watermark, dedup, gaps, intervals)")
        asyncio.run(run_collector(fresh("collector")))
        test_interval()

        section("instagram (one stable device per account)")
        test_ig_device(fresh("ig"))

        section("dashboard filters (category, verified, views, followers, dates)")
        test_filters(fresh("filters"))

        section("telegram (switching it on is what makes a stream watched)")
        test_telegram_watch_rule(fresh("tgwatch"))

        section("projects & watchlists (dashboard state -> compiled streams)")
        test_projects_watchlists(fresh("projects"))

        section("collections (curation boards)")
        test_collections(fresh("collections"))

        section("facebook (store, cap, collect loop)")
        test_facebook(fresh("facebook"))

        section("velocity alerts (pace, threshold, cooldown)")
        test_alerts(fresh("alerts"))

        section("keywords, stream assignment, per-project delivery")
        test_keywords_and_project_delivery(fresh("kwdeliv"))

        section("webhook (signing, cursor, receiver outage)")
        asyncio.run(run_webhook(fresh("webhook")))


        section("web (shared event loop)")
        test_web_event_loop(fresh("web"))

        section("cli (routing, exit codes, end to end)")
        d = fresh("cli")
        (d / "config.toml").write_text(CONFIG)
        test_routing_and_exit_codes(d)
        asyncio.run(test_search_to_export(d))
    finally:
        os.chdir(cwd)
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("=" * 70)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
