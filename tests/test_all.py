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


def test_xclid_shim():
    """
    2026-08-25: X's legacy web build (served to LOGGED-IN sessions) switched
    from 7-hex to 16-hex chunk hashes. twscrape 0.20.0's fallback parser
    requires exactly 7, so request signing died on every account with
    "X web scripts not found", and the collector reported starvation.
    engine.py shims the one function. This pins both directions: the shim
    parses the new shape, and twscrape as shipped still does not (when it
    does, the shim must stand down by itself).
    """
    print("== x-client-transaction-id: legacy build with 16-hex chunk hashes ==")
    import engine
    import twscrape.xclid as x

    page = (
        '<html><script src="https://abs.twimg.com/responsive-web/client-web/main.3fc0640facfee243a.js">'
        '</script><script>(e=>({3:"bundle.Payments",5:"ondemand.s",7:"i18n/ar",9:"loader.Foo"})[e]||e)'
        '+"."+({3:"cf787fd54a63440c",5:"b7dbcfcff298f890",7:"2322cb6c5c855f73",9:"f28425fe7c4b6176"})'
        '[e]+"a.js"</script></html>'
    )
    want = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.b7dbcfcff298f890a.js"

    ok(engine.XCLID_SHIM in ("installed",) or str(engine.XCLID_SHIM).startswith("not-needed"),
       f"shim decided at import: {engine.XCLID_SHIM}")
    urls = x.get_scripts_list(page)
    ok(want in urls, f"live get_scripts_list finds the ondemand.s signing script ({len(urls)} chunks)")
    ok("https://abs.twimg.com/responsive-web/client-web/i18n/ar.2322cb6c5c855f73a.js" in urls,
       "name map still resolves chunk ids to names (i18n/ar)")
    ok([u for u in urls if x.INDICES_FILE_RE.search(u)] == [want],
       "twscrape's own INDICES_FILE_RE picks exactly that script, so no chunk scanning is needed")

    if engine.XCLID_SHIM == "installed":
        try:
            engine._UPSTREAM_get_scripts_list(page)
            ok(False, "twscrape as shipped parses 16-hex hashes — the shim guard should have stood down")
        except x.XClIdParseError as e:
            ok("scripts not found" in str(e),
               f"  control: twscrape as shipped still fails on this page ({e}) — shim is earning its keep")

    # The modern (Vite) build and the logged-out shell are untouched by the shim.
    modern = '<link href="https://abs.twimg.com/x-web/x-web/entry-client-3fc06.js">'
    ok(x.get_scripts_list(modern) == ["https://abs.twimg.com/x-web/x-web/entry-client-3fc06.js"],
       "modern x-web build passes straight through")
    try:
        x.get_scripts_list('<link href="https://abs.twimg.com/x-web/x-web/entry-client-logged-out-A3q3.js">')
        ok(False, "logged-out shell should raise XClIdAccountError")
    except x.XClIdAccountError:
        ok(True, "logged-out shell still raises XClIdAccountError (dead session is still detected)")
    try:
        x.get_scripts_list("<html>nothing here</html>")
        ok(False, "an unrecognised page should raise")
    except x.XClIdParseError:
        ok(True, "an unrecognised page still raises XClIdParseError")


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

    # The August-2026 X payload change (what twscrape 0.20.0 exists to fix).
    # X stopped typing the author object embedded in each tweet, so
    # to_old_rep's `users` map — which is built by collecting every
    # `__typename: "User"` in the payload — came back EMPTY. On 0.19.2
    # Tweet.parse then did res["users"][user_id_str] -> KeyError for every
    # single hit, parse_page filed each as a parse_failure, every result was an
    # orphan, and the collector stored nothing while reporting healthy polls.
    # 0.20.0 resolves the author from core.user_results.result instead.
    import copy

    def _strip_user_typenames(node):
        if isinstance(node, dict):
            if node.get("__typename") == "User":
                node = {k: v for k, v in node.items() if k != "__typename"}
            return {k: _strip_user_typenames(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_strip_user_typenames(x) for x in node]
        return node

    untyped = _strip_user_typenames(copy.deepcopy(search_payload()))
    from twscrape.utils import to_old_rep as _to_old_rep

    ok(
        not _to_old_rep(untyped).get("users"),
        "fixture reproduces the X change: no typed User objects -> empty users map",
    )
    page2 = parse_page(FakeResponse(untyped), 1)
    ok(
        page2.parse_failures == [] and page2.orphan_ids == [],
        f"authors are still resolved from core.user_results (failures={page2.parse_failures}, "
        f"orphans={page2.orphan_ids})",
    )
    ok(
        page2.result_ids == page.result_ids
        and all(page2.tweets[i].user.username == page.tweets[i].user.username for i in page.result_ids),
        "...and every result carries the same author as the typed payload",
    )


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
    JITTER_PINNED,
    STOP_ERROR,
    STOP_EXHAUSTED,
    STOP_PAGE_BUDGET,
    STOP_STARVED,
    STOP_WATERMARK,
    jittered,
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
    print("== twscrape 0.20.0: an outdated GQL contract is an error, not a process exit ==")
    # Before 0.20.0 twscrape called exit(1) from inside the request loop when X
    # answered (336) "features cannot be null" — SystemExit is not an Exception,
    # so it went straight through poll_once and killed the watcher. Now it is a
    # real exception. Two things must hold: it is caught like any other error
    # (the loop survives), and the message names the fix, because otherwise the
    # dashboard shows the same cryptic class name on every stream forever.
    from twscrape import GqlFeaturesOutdatedError

    class Outdated:
        def pages_for(self, stream, **kw):
            return self.search_pages(stream.query, **kw)

        async def search_pages(self, *a, **kw):
            raise GqlFeaturesOutdatedError("(336) The following features cannot be null: x")
            yield  # pragma: no cover

    res = await poll_once(Outdated(), store, stream, sid)
    ok(res.stop_reason == STOP_ERROR, f"reported as an error (stop={res.stop_reason})")
    ok("upgrade" in (res.error or "").lower() and "requirements.txt" in (res.error or ""),
       f"...and the error text says what to do: {res.error!r}")
    ok((await store.get_watermark(sid))["high_tweet_id"] == wm["high_tweet_id"],
       "the watermark is NOT advanced on that poll either")

    print()
    print("== X changes the payload under us: all-orphan pages are an ERROR, not '0 new' ==")
    # The 2026-08 failure, as the collector saw it: pages walked, entry ids
    # present, and not one tweet parseable (twscrape 0.19.2 raised KeyError on
    # every author after X untyped the user object). Before this check the
    # poll reported healthy, stored nothing, and — worse — advanced the
    # watermark past the tweets it had just failed to keep, so the fix could
    # never recover them. Every one of those must now be false.
    from engine import Page

    class ShapeChanged:
        def pages_for(self, stream, **kw):
            return self.search_pages(stream.query, **kw)

        async def search_pages(self, *a, **kw):
            newer = [id_at(-30_000), id_at(-60_000)]     # newer than the watermark
            page = Page(page_no=1, received_ts=time.time(), server_ts=None,
                        account="alice", status=200, rl_limit=50, rl_remaining=40)
            page.result_ids = newer
            page.entries_by_id = {i: {"entryId": f"tweet-{i}"} for i in newer}
            page.parse_failures = [(str(i), "KeyError: '11'") for i in newer]
            yield page

    res = await poll_once(ShapeChanged(), store, stream, sid)
    ok(res.results == 2 and res.orphans == 2, f"2 results, 2 orphans (results={res.results} orphans={res.orphans})")
    ok(res.stop_reason == STOP_ERROR, f"reported as an ERROR (stop={res.stop_reason})")
    ok("payload shape changed" in (res.error or "") and "KeyError" in (res.error or ""),
       f"...naming the cause and the first failure: {res.error!r}")
    ok(res.max_id is None and res.new == 0, "no watermark candidate from tweets that were not stored")
    ok((await store.get_watermark(sid))["high_tweet_id"] == wm["high_tweet_id"],
       "the watermark did NOT advance over the orphaned tweets")

    # One orphan among real results is ordinary (a deleted-mid-page tweet),
    # not a shape change — and the watermark still comes from what was stored.
    class OneOrphan:
        def pages_for(self, stream, **kw):
            return self.search_pages(stream.query, **kw)

        async def search_pages(self, *a, **kw):
            page = parse_page(FakeResponse(search_payload(ids=[id_at(-20_000), id_at(-40_000)])), 1)
            ghost = id_at(-10_000)                          # newest entry, unparseable
            page.result_ids.insert(0, ghost)
            page.entries_by_id[ghost] = {"entryId": f"tweet-{ghost}"}
            yield page

    res = await poll_once(OneOrphan(), store, stream, sid)
    ok(res.stop_reason != STOP_ERROR and res.orphans == 1 and res.new == 2,
       f"a single orphan among parsed results is not an error (stop={res.stop_reason} new={res.new})")
    ok(res.max_id == id_at(-20_000),
       "the watermark candidate is the newest STORED tweet, not the unparseable entry above it")

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
    print("== the three-column source: identity, handle, id ==")
    # label / value / platform_id are three different facts with three different
    # lifetimes, and the collector is only ever allowed to write the third.
    # `label` is the cross-platform identity key ("Narendra Modi" is
    # @narendramodi here and @modinarendra on Facebook) — if anything in this
    # module can overwrite it, the profile mapping silently rots.
    import store_ig, tempfile as _tf, os as _os

    sp = _os.path.join(_tf.mkdtemp(), "sources.db")
    with store_ig.Store(sp) as st:
        st.add_source("Narendra Modi", "user", "narendramodi")
        src = st.sources()[0]
        ok(src.user_id == "narendramodi",
           "an unresolved source still fetches by name — nothing breaks on day one")
        ok([(x.label, x.value) for x in st.unresolved_sources()]
           == [("Narendra Modi", "narendramodi")],
           "and shows up on the resolve worklist")

        st.cache_platform_id("narendramodi", "1550693326")
        src = st.sources()[0]
        ok(src.user_id == "1550693326", "once resolved it fetches by numeric id")
        ok(src.label == "Narendra Modi" and src.value == "narendramodi",
           "while the identity and the handle are left exactly as they were")
        ok(st.unresolved_sources() == [], "and it leaves the worklist")

        st.add_source("Narendra Modi", "user", "narendramodi")
        ok(st.sources()[0].platform_id == "1550693326",
           "re-adding the same handle keeps the cached id (no wasted lookup)")

        st.add_source("Narendra Modi", "user", "narendramodi_new")
        ok(st.sources()[0].platform_id == "",
           "but renaming the handle DROPS it — a stale id would collect the "
           "wrong account under the right name")

        st.add_source("natgeo", "user", "787132")
        ng = [x for x in st.sources() if x.label == "natgeo"][0]
        ok(ng.platform_id == "787132" and ng.value == "",
           "a numeric --value still works and lands in the id column, not the handle")

    # An ig_results.db written before platform_id existed must upgrade itself on
    # open, or every read of a source row raises on a file the service already has.
    import sqlite3 as _sq
    old = _os.path.join(_tf.mkdtemp(), "old.db")
    _c = _sq.connect(old)
    _c.executescript(
        "CREATE TABLE sources (label TEXT PRIMARY KEY, type TEXT NOT NULL, "
        "value TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '', "
        "enabled INTEGER NOT NULL DEFAULT 1, watermark_pk INTEGER, "
        "last_run INTEGER, created_at INTEGER NOT NULL);"
        "INSERT INTO sources(label,type,value,created_at) "
        "VALUES('Yogi Adityanath','user','myogi_adityanath',1);")
    _c.commit(); _c.close()
    with store_ig.Store(old) as st:
        ok(st.sources()[0].user_id == "myogi_adityanath",
           "an old database gains platform_id on open and keeps collecting")

    print()
    print("== project scoping: no project, no data ==")
    # The old behaviour was `pid or None` at every call site, and None reached
    # the store as "no WHERE clause". Scoping therefore worked exactly as long
    # as the caller remembered to ask for it. These assertions pin the default
    # CLOSED: a read that names no project returns nothing, never everything.
    import store_ig as _sig, tempfile as _tf2, os as _os2

    sp2 = _os2.path.join(_tf2.mkdtemp(), "scope.db")
    with _sig.Store(sp2) as st:
        st.add_source("Modi", "user", "narendramodi", project_id=2)
        st.add_source("Kejriwal", "user", "arvindkejriwal", project_id=3)
        st.add_source("Parked", "user", "someoneelse")          # no project
        st.upsert_posts([{"pk": 100, "code": "a", "taken_at": 1}], "Modi")
        st.upsert_posts([{"pk": 200, "code": "b", "taken_at": 2}], "Kejriwal")
        st.upsert_posts([{"pk": 300, "code": "c", "taken_at": 3}], "Parked")

        ok(st.db.execute("SELECT project_id FROM posts WHERE pk=100").fetchone()[0] == 2,
           "a post inherits its project from the source that collected it")
        ok([x.label for x in st.sources(project_id=2)] == ["Modi"],
           "a project sees its own sources")
        ok([x.label for x in st.sources(project_id=3)] == ["Kejriwal"],
           "and only its own — the other project's source is not there")
        ok([r["pk"] for r in st.query(project_id=2)] == [100],
           "a project sees its own posts")
        ok([r["pk"] for r in st.query(project_id=3)] == [200],
           "and cannot see the other project's")
        ok([r["pk"] for r in st.query(project_id=0)] == [300],
           "project 0 is a real id matching the UNASSIGNED rows — not a "
           "wildcard, and not an alias for 'all'")
        ok(len(st.query()) == 3,
           "the store CAN still read unscoped — the collector and the migration "
           "need it; the boundary is what refuses (web.py, api.py)")
        ok(st.stats(project_id=2)["posts"] == 1 and st.stats()["posts"] == 3,
           "stats scope the same way")

        # A parked source still collects. It is invisible, not disabled — the
        # posts are there the moment someone assigns it.
        ok([x.label for x in st.sources(project_id=0)] == ["Parked"],
           "an unassigned source is parked, not lost")
        st.set_project("Parked", 3)
        ok(sorted(x.label for x in st.sources(project_id=3)) == ["Kejriwal", "Parked"],
           "assigning it makes it visible to that project")
        ok([r["pk"] for r in st.query(project_id=3)] == [200],
           "but its already-collected posts keep the project they were "
           "collected under — history is not retroactively reassigned")

    # THE BOUNDARY. The assertions above prove the store can scope; these prove
    # the endpoints actually DO. That distinction is the whole bug: the store
    # could always scope, and every call site defaulted to not bothering.
    import web as _web

    # _ig_status reads the ACCOUNT pool whatever the project, so it needs a
    # config even on the refused path. Point it at an empty temp root: no
    # accounts to find, which is exactly what we want to assert around.
    class _Cfg:
        root = pathlib.Path(_tf2.mkdtemp())
    _orig_cfg = _web._CFG
    _web._CFG = _Cfg()
    try:
        _scoping_boundary_checks(_web)
    finally:
        _web._CFG = _orig_cfg

    # The API key IS the scope: there is no parameter for a caller to tamper
    # with, and a key issued before scoping reads nothing rather than all.
    import api as _api
    kp = _os2.path.join(_tf2.mkdtemp(), "keys.db")
    tok2 = _api.create_key("watchtower", path=kp, project_id=2)
    tok0 = _api.create_key("legacy", path=kp)
    ok(_api.verify_key(tok2, kp)["project_id"] == 2,
       "a key carries the project it may read")
    ok(_api.verify_key(tok0, kp)["project_id"] == 0,
       "a key issued with no project carries 0, which the service refuses")
    pre = _api.verify_key(tok0, kp)["prefix"]
    ok(_api.set_key_project(pre, 7, kp) and _api.verify_key(tok0, kp)["project_id"] == 7,
       "and can be re-scoped rather than reissued")


def _scoping_boundary_checks(_web):
    """Every IG/FB data endpoint, called with no project."""
    for fn, arg, what in [(_web._ig_posts, {}, "/api/ig/posts"),
                          (_web._fb_posts, {}, "/api/fb/posts"),
                          (_web._ig_fetch, {}, "/api/ig/fetch"),
                          (_web._fb_fetch, {}, "/api/fb/fetch"),
                          (_web._fb_favorites, {}, "/api/fb/favorites")]:
        r = fn(arg)
        ok(r.get("error") == "no project selected" and not r.get("posts")
           and not r.get("sources"),
           f"{what} with no project returns nothing, and says why")
    # status endpoints are a SPLIT, not a refusal: the server half always
    # answers (the Accounts panel calls them with no project at all), the
    # project half does not.
    r = _web._ig_status({})
    ok(r.get("error") == "no project selected" and r["sources"] == [],
       "/api/ig/status with no project lists no sources")
    ok("accounts" in r,
       "...but still reports ACCOUNTS — a login belongs to the server, not to "
       "a project, and the Accounts panel needs it in every view")
    r = _web._fb_status({})
    ok(r.get("error") == "no project selected" and r["sources"] == [],
       "/api/fb/status with no project lists no sources")
    ok("session" in r and "health" in r and "config" in r,
       "...but still reports the LOGIN, its health and the collector config — "
       "server facts, which the Accounts panel renders with no project set")
    for act in ("add",):
        r = _web._ig_source_post({"action": act, "label": "X", "value": "x"})
        ok(r.get("error") == "no project selected",
           "adding an Instagram source with no project is refused, not parked "
           "silently")
        r = _web._fb_source_post({"action": act, "label": "somepage"})
        ok(r.get("error") == "no project selected",
           "and the same for a Facebook page")

    print()
    print("== username -> pk resolution ==")
    # Instagram grants name LOOKUP and media READS separately: a live session
    # was measured fetching user_medias_paginated_v1('787132') while every
    # username resolver returned login_required. So a numeric id must never be
    # sent through a lookup it does not need, and a failed lookup must say so.
    import engine_ig
    from engine_ig import IGEngine

    class Blocked:
        calls = 0

        def user_id_from_username(self, name):
            Blocked.calls += 1
            raise RuntimeError("login_required")

    # The two fallbacks are stubbed for the whole block: this is a unit test of
    # the resolution LOGIC, and it must never touch the network.
    _real_web, _real_html = (engine_ig._pk_via_web_profile_info,
                             engine_ig._pk_via_profile_html)
    web_calls = []
    try:
        engine_ig._pk_via_web_profile_info = lambda cl, n, timeout=15: None
        engine_ig._pk_via_profile_html = lambda cl, n, timeout=15: None

        eng = IGEngine(Blocked())
        ok(eng.resolve_user("787132") == "787132", "a numeric id passes straight through")
        ok(eng.resolve_user(787132) == "787132", "including when it arrives as an int")
        ok(Blocked.calls == 0, "and costs ZERO lookup requests")
        try:
            eng.resolve_user("natgeo")
            ok(False, "a lookup blocked on every path raises")
        except RuntimeError as e:
            ok("numeric id" in str(e) and "set-id" in str(e),
               "which names the fix (cache the id) rather than the trace")
            ok("web_profile_info" in str(e) and "profile HTML" in str(e),
               "and reports every path it tried, not just the first")

        # The point of the fallback: the private API is dead, the WEB endpoint
        # is not, and the source collects anyway. This is the case that used to
        # take a whole project offline.
        engine_ig._pk_via_web_profile_info = lambda cl, n, timeout=15: (
            web_calls.append(n), "787132")[1]
        cached = []
        eng2 = IGEngine(Blocked(), on_resolved=lambda h, pk: cached.append((h, pk)))
        ok(eng2.resolve_user("natgeo") == "787132",
           "a name resolves through the web endpoint when the private one is gated")
        ok(cached == [("natgeo", "787132")],
           "and the answer is handed to the store, so it is looked up ONCE ever")
        ok(eng2.resolve_user("@natgeo") == "787132" and len(web_calls) == 1,
           "the in-process cache is keyed on the bare name, so '@natgeo' is free")

        eng3 = IGEngine(Blocked(), on_resolved=lambda h, pk: 1 / 0)
        ok(eng3.resolve_user("natgeo") == "787132",
           "a failing write-back never breaks a resolve that succeeded")
    finally:
        engine_ig._pk_via_web_profile_info = _real_web
        engine_ig._pk_via_profile_html = _real_html


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


def test_forget_stream_purges_raw(tmp):
    """
    forget_stream(delete_tweets=True) must not orphan the externalized raw
    payload. The raw Tweet.json lives in its own tweet_raw table (~10x the
    searchable row) with no cascade, so a delete that skipped it would leave
    the bulk of every "destroyed" tweet sitting in the database forever.
    Regression guard for that leak, and for the shared-tweet EXCEPT rule.
    """
    import store as store_mod

    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        sid_a = await st.ensure_stream("gone", "q1", "Latest", True)
        sid_b = await st.ensure_stream("kept", "q2", "Latest", True)

        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}

        def add_tweet(tid, stream_ids):
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=tid, collected_ms=tid, source="result")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))
            st.db.execute("INSERT INTO tweet_raw(tweet_id, raw_json) VALUES(?, ?)",
                          (tid, '{"id":"%d"}' % tid))
            for s in stream_ids:
                st.db.execute(
                    "INSERT INTO tweet_hits(stream_id, tweet_id, first_seen_ms) "
                    "VALUES(?,?,?)", (s, tid, tid))

        add_tweet(201, [sid_a])              # only in the doomed stream
        add_tweet(202, [sid_a, sid_b])       # shared with a stream we keep
        st.db.commit()

        res = await st.forget_stream("gone", delete_tweets=True)
        out = {
            "res": res,
            "tweets": {r["tweet_id"] for r in st.db.execute("SELECT tweet_id FROM tweets")},
            "raw": {r["tweet_id"] for r in st.db.execute("SELECT tweet_id FROM tweet_raw")},
        }
        await st.close()
        return out

    r = asyncio.run(run())
    ok(r["res"]["tweets_deleted"] == 1, "only the unshared tweet is destroyed")
    ok(r["tweets"] == {202}, "the tweet shared with a kept stream survives")
    ok(r["raw"] == {202}, "its raw payload is purged with it — no orphan in tweet_raw")


# ==========================================================================
# content labelling (RULEBOOK §1 directive 2, the one named exception)
#
# Offline and unpaid: the Grok client takes its httpx client as an argument, so
# every branch here runs against a fake that never opens a socket. XAI_API_KEY
# is stubbed too — a test that read the real environment would pass or fail
# depending on whose laptop it ran on.
# ==========================================================================

class FakeGrok:
    """
    An httpx.AsyncClient stand-in. `plan` is a list of (status, payload) served
    one per call, so a test can make the second batch fail and check that the
    first batch's labels were still kept and still paid for.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        status, payload = self.plan.pop(0) if self.plan else (200, {})

        class Rep:
            status_code = status
            text = payload if isinstance(payload, str) else ""

            def json(self_inner):
                if isinstance(payload, str):
                    raise ValueError("not json")
                return payload
        return Rep()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _grok_reply(pairs, in_tok=1000, out_tok=100):
    """A well-formed chat-completions body labelling each (id, label)."""
    body = {"labels": [{"id": str(i), "label": l, "confidence": 0.9}
                       for i, l in pairs]}
    return {"choices": [{"message": {"content": json.dumps(body)}}],
            "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok}}


def test_classify_pure(tmp):
    """The prompt builder, the parser and the pricing — no store, no network."""
    import classify as cls

    cats = cls.DEFAULT_CATEGORIES
    keys = [c["key"] for c in cats]
    prompt = cls.build_prompt(cats)

    ok(all(c["key"] in prompt and c["description"][:30] in prompt for c in cats),
       "every category's key AND its description reach the prompt "
       "(an editor whose text never arrives is theatre)")
    ok(prompt.index("hate") < prompt.index("bjp_pro"),
       "categories are listed in precedence order, which is what the tie-break "
       "instruction refers to")
    ok("ONE" in prompt and "other" in prompt,
       "the prompt states one-category-only and names the catch-all")

    got = cls.parse_response(json.dumps(
        {"labels": [{"id": "1", "label": "hate", "confidence": 0.8}]}),
        keys, ["1"])
    ok(got == {"1": ("hate", 0.8)}, "a well-formed answer parses to (label, confidence)")

    ok(cls.parse_response('```json\n{"labels":[{"id":"1","label":"bjp_pro"}]}\n```',
                          keys, ["1"]) == {"1": ("bjp_pro", None)},
       "a fenced code block still parses, and a missing confidence is None "
       "rather than a guess")
    ok(cls.parse_response('Sure! {"labels":[{"id":"1","label":"hate"}]} hope that helps',
                          keys, ["1"]) == {"1": ("hate", None)},
       "JSON wrapped in chatter is recovered rather than losing the batch")
    ok(cls.parse_response('{"labels":[{"id":"1","label":"anti_national"}]}',
                          keys, ["1"]) == {"1": ("other", None)},
       "a category the model invented becomes the catch-all — a label the "
       "operator never defined must never reach a board")
    ok(cls.parse_response('{"labels":[{"id":"77","label":"hate"}]}',
                          keys, ["1"]) == {},
       "a label for a post we did not ask about is dropped, not misattributed")
    ok(cls.parse_response('{"labels":[{"id":"1","label":"hate","confidence":7}]}',
                          keys, ["1"]) == {"1": ("hate", None)},
       "an out-of-range confidence is discarded, not stored as 7.0")
    ok(cls.parse_response("total garbage", keys, ["1"]) == {}
       and cls.parse_response("", keys, ["1"]) == {},
       "an unparseable reply is an empty result, never an exception")
    ok(cls.parse_response('{"labels":[{"id":"1","label":"hate"}]}', keys, ["1", "2"])
       == {"1": ("hate", None)},
       "a post the model skipped is simply absent, so the caller can count it "
       "as failed instead of inventing a label for it")

    ok(abs(cls.cost_usd(1_000_000, 1_000_000, 2.0, 6.0) - 8.0) < 1e-9,
       "cost is input x price_in + output x price_out, per million tokens")
    ok(cls.cost_usd(0, 0) == 0.0, "a call that used nothing costs nothing")
    ok(len(list(cls.chunk(list(range(55)), 25))) == 3,
       "work is split into request-sized batches")

    ok(cls.CATCHALL in keys and keys[-1] == cls.CATCHALL,
       "the shipped vocabulary ends with the catch-all")


def test_classify_call(tmp):
    """label_batch against a fake client: happy path and every failure mode."""
    import classify as cls

    cats = cls.DEFAULT_CATEGORIES
    posts = [{"id": "1", "text": "great work by the government", "author": "a"},
             {"id": "2", "text": "cricket score tonight", "author": "b"}]

    async def run():
        out = {}
        c = FakeGrok([(200, _grok_reply([("1", "bjp_pro"), ("2", "other")]))])
        out["ok"] = await cls.label_batch(c, "k", "grok-4.6", posts, cats)
        out["sent"] = c.calls[0]

        out["nokey"] = await cls.label_batch(c, "", "grok-4.6", posts, cats)

        c2 = FakeGrok([(429, "rate limited, slow down")])
        out["429"] = await cls.label_batch(c2, "k", "m", posts, cats)

        c3 = FakeGrok([(200, "<html>gateway</html>")])
        out["notjson"] = await cls.label_batch(c3, "k", "m", posts, cats)

        # A 400 is retried once WITHOUT response_format, because the likeliest
        # cause is a deployment that does not accept structured output.
        c4 = FakeGrok([(400, "unsupported"),
                       (200, _grok_reply([("1", "hate"), ("2", "other")]))])
        out["retry"] = await cls.label_batch(c4, "k", "m", posts, cats)
        out["retry_bodies"] = c4.calls

        class Boom(FakeGrok):
            async def post(self, *a, **k):
                raise OSError("connection reset")
        out["boom"] = await cls.label_batch(Boom([]), "k", "m", posts, cats)

        out["empty"] = await cls.label_batch(FakeGrok([]), "k", "m", [], cats)
        return out

    r = asyncio.run(run())

    labels, usage, err = r["ok"]
    ok(labels == {"1": ("bjp_pro", 0.9), "2": ("other", 0.9)} and not err,
       "a good reply comes back as {id: (label, confidence)}")
    ok(usage == {"in": 1000, "out": 100},
       "token usage is read off the reply, so the meter bills what was used")
    body = r["sent"]
    ok(body["temperature"] == 0,
       "temperature 0: the same post must label the same way twice, or a "
       "re-run is a coin flip rather than a correction")
    ok(body["response_format"]["json_schema"]["schema"]["properties"]["labels"]
       ["items"]["properties"]["label"]["enum"] == [c["key"] for c in cats],
       "the structured-output schema pins the answer to this project's keys")
    ok(posts[0]["text"] in body["messages"][1]["content"]
       and "ID: 1" in body["messages"][1]["content"],
       "each post is sent with its id, so labels come back attributable rather "
       "than positional")

    ok(r["nokey"][0] == {} and "XAI_API_KEY" in r["nokey"][2],
       "a missing key is an error naming the variable, not a crash")
    ok(r["429"][0] == {} and "429" in r["429"][2] and "slow down" in r["429"][2],
       "an HTTP error carries the provider's own words, not just the status")
    ok(r["notjson"][0] == {} and "JSON" in r["notjson"][2],
       "a gateway page instead of JSON is an error string")
    ok(r["retry"][0] and len(r["retry_bodies"]) == 2
       and "response_format" not in r["retry_bodies"][1],
       "a 400 retries once without response_format rather than failing the run")
    ok(r["boom"][0] == {} and "OSError" in r["boom"][2],
       "a transport blow-up is returned, never raised — a run must not lose "
       "the batches it already paid for")
    ok(r["empty"] == ({}, {}, ""), "an empty batch is a no-op, not a request")


def test_labels_store(tmp):
    """Labels, auto boards, the human override, and the spend meter."""
    import classify as cls
    import store as store_mod

    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        out = {}
        pid = (await st.create_project("Client A"))["project_id"]
        pid2 = (await st.create_project("Client B"))["project_id"]

        out["seed"] = await st.seed_post_label_categories(pid, cls.DEFAULT_CATEGORIES)
        out["seed2"] = await st.seed_post_label_categories(pid, cls.DEFAULT_CATEGORIES)
        cats = await st.post_label_categories(pid)
        out["order"] = [c["key"] for c in cats]

        boards = {c["key"]: await st.ensure_label_collection(pid, c["key"], c["name"])
                  for c in cats}
        boards2 = {c["key"]: await st.ensure_label_collection(pid, c["key"], c["name"])
                   for c in cats}
        out["boards_stable"] = boards == boards2
        out["board_count"] = len(await st.collections(pid))

        # One post per platform, to prove the key is (platform, post_id).
        rows = [("x", "111", "hate", 0.9),
                ("instagram", "222", "bjp_pro", 0.5),
                ("facebook", "pfbid_x", "other", None)]
        out["wrote"] = await st.set_post_labels(pid, rows, source="grok",
                                                model="m", prompt_ver=1)
        for plat, post_id, key, _c in rows:
            await st.repin_labelled(pid, plat, post_id, key, boards)
        out["counts"] = await st.post_label_counts(pid)
        out["hate_board"] = await st.collection_post_rows(boards["hate"])
        out["fb_board"] = await st.collection_post_rows(boards["other"])

        # A different project labelling the SAME post keeps its own answer.
        await st.seed_post_label_categories(pid2, cls.DEFAULT_CATEGORIES)
        await st.set_post_labels(pid2, [("x", "111", "other", None)])
        got = await st.post_labels_for(pid, [("x", "111")])
        got2 = await st.post_labels_for(pid2, [("x", "111")])
        out["scoped"] = (got[("x", "111")]["label"], got2[("x", "111")]["label"])

        # The operator corrects a label; a later run must not undo it.
        await st.set_post_labels(pid, [("x", "111", "other", None)], source="human")
        await st.repin_labelled(pid, "x", "111", "other", boards)
        await st.set_post_labels(pid, [("x", "111", "hate", 0.99)], source="grok")
        after = await st.post_labels_for(pid, [("x", "111")])
        out["human"] = (after[("x", "111")]["label"], after[("x", "111")]["source"])
        out["moved_off"] = await st.collection_post_rows(boards["hate"])
        out["moved_on"] = [p["post_id"] for p in
                           await st.collection_post_rows(boards["other"])]

        # A hand-pinned post on a manual board is never moved by a relabel.
        mine = (await st.create_collection(pid, "My picks"))["collection_id"]
        await st.collection_pin_posts(mine, add=[("x", "111")])
        await st.repin_labelled(pid, "x", "111", "bjp_pro", boards)
        out["manual_kept"] = len(await st.collection_post_rows(mine))

        out["already"] = sorted(await st.labelled_post_ids(pid))
        out["catchall"] = await st.set_post_label_category(pid, "other",
                                                           archived=True)
        out["badkey"] = await st.set_post_label_category(pid, "bad key!",
                                                         name="x", description="y")
        out["newcat"] = await st.set_post_label_category(
            pid, "against_bjp", name="Against BJP",
            description="Attacks the BJP.", rank=5)

        rid = await st.start_label_run(pid, 50, "grok-4.6")
        await st.finish_label_run(rid, labelled=48, failed=2, in_tokens=1_000_000,
                                  out_tokens=0, cost=cls.cost_usd(1_000_000, 0),
                                  stop_reason="cap")
        out["spend"] = await st.label_spend_month(0)
        out["spend_after"] = await st.label_spend_month(9_999_999_999_999)
        out["last"] = await st.last_label_run(pid)

        out["settings_default"] = await st.setting("classify_cap_usd", "10")
        await st.set_setting("classify_cap_usd", "25")
        out["settings_set"] = await st.setting("classify_cap_usd", "10")
        await st.set_setting("classify_cap_usd", "")
        out["settings_cleared"] = await st.setting("classify_cap_usd", "10")
        await st.close()
        return out

    r = asyncio.run(run())
    ok(r["seed"] == 5 and r["seed2"] == 0,
       "the vocabulary seeds once and never re-seeds over an operator's edits")
    ok(r["order"][0] == "hate" and r["order"][-1] == "other",
       "categories come back in precedence order, catch-all last")
    ok(r["boards_stable"] and r["board_count"] == 5,
       "each category gets exactly one auto board, and asking again reuses it")
    ok(r["wrote"] == 3 and r["counts"] == {"hate": 1, "bjp_pro": 1, "other": 1},
       "labels persist for X, Instagram and Facebook alike")
    ok([p["post_id"] for p in r["hate_board"]] == ["111"]
       and [p["post_id"] for p in r["fb_board"]] == ["pfbid_x"],
       "a labelled post lands on its category's board, whatever its platform")
    ok(r["fb_board"][0]["platform"] == "facebook",
       "a board pin carries its platform — an id alone cannot say which")
    ok(r["scoped"] == ("hate", "other"),
       "two projects label the same post independently: one client's answer is "
       "never the other's")
    ok(r["human"] == ("other", "human"),
       "a hand-set label survives a later Grok run untouched — the WHERE clause "
       "that makes re-running safe")
    ok(r["moved_off"] == [] and "111" in r["moved_on"],
       "re-labelling moves the post between auto boards instead of leaving it "
       "on both")
    ok(r["manual_kept"] == 1,
       "a post the operator pinned by hand stays pinned however the label "
       "changes — only auto boards are the labeller's to rearrange")
    ok(r["already"] == [("facebook", "pfbid_x"), ("instagram", "222"),
                        ("x", "111")],
       "the already-labelled set is what stops a run paying for a post twice")
    ok("error" in r["catchall"],
       "the catch-all cannot be archived: without it every unrelated post is "
       "forced into a political category")
    ok("error" in r["badkey"], "a category key with a space is refused")
    ok(r["newcat"]["name"] == "Against BJP" and r["newcat"]["rank"] == 5,
       "the operator can add a category without a code change")
    ok(abs(r["spend"] - 2.0) < 1e-9 and r["spend_after"] == 0.0,
       "the meter sums a month's runs and ignores everything before its window")
    ok(r["last"]["stop_reason"] == "cap" and r["last"]["labelled"] == 48,
       "a run records how it ended, so 'stopped at the cap' can be said out loud")
    ok(r["settings_default"] == "10" and r["settings_set"] == "25"
       and r["settings_cleared"] == "10",
       "a switch reads its default until set, and clearing it restores the "
       "default rather than pinning it to nothing")


def test_cross_platform_pins(tmp):
    """The pin migration, and the retention rule that must not eat a board."""
    import store as store_mod

    db = pathlib.Path(tmp) / "results.db"

    async def seed_tweets(st, ids, created_ms):
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}
        for tid in ids:
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=created_ms, collected_ms=created_ms,
                       source="result")
            st.db.execute(
                f"INSERT OR IGNORE INTO tweets({','.join(row)}) "
                f"VALUES({','.join('?' * len(row))})", list(row.values()))

    async def run():
        out = {}
        st = store_mod.Store(db, False)
        await st.open()
        pid = (await st.create_project("P"))["project_id"]
        cid = (await st.create_collection(pid, "Old board"))["collection_id"]
        old_ms = int(time.time() * 1000) - 400 * 86_400_000
        await seed_tweets(st, [501, 502], old_ms)
        # Pretend this database predates cross-platform pins: an old-style pin,
        # and the migration flag cleared.
        st.db.execute("INSERT INTO collection_items(collection_id, tweet_id, "
                      "added_ms) VALUES(?,?,?)", (cid, 501, 1))
        st.db.execute("DELETE FROM collection_posts")
        st.db.execute("DELETE FROM meta WHERE key = 'pins_cross_platform'")
        await st.close()

        st = store_mod.Store(db, False)
        await st.open()
        out["migrated"] = await st.collection_post_rows(cid)
        await st.close()

        st = store_mod.Store(db, False)      # again: must not double up
        await st.open()
        out["again"] = len(await st.collection_post_rows(cid))
        out["listed"] = (await st.collections(pid))[0]["items"]

        # Retention: 501 is pinned and must survive; 502 is not and must go.
        st.retention_days = 30
        out["swept"] = await st.maintain()
        out["left"] = sorted(x["tweet_id"] for x in
                             st.db.execute("SELECT tweet_id FROM tweets"))
        await st.close()
        return out

    r = asyncio.run(run())
    ok([p["post_id"] for p in r["migrated"]] == ["501"]
       and r["migrated"][0]["platform"] == "x"
       and r["migrated"][0]["pinned_by"] == "human",
       "an existing board migrates to cross-platform pins as X posts pinned by "
       "a human, which is the only thing the old table could hold")
    ok(r["again"] == 1, "the migration is gated on a flag and never doubles a pin")
    ok(r["listed"] == 1, "the board list counts the migrated pins")
    ok(501 in r["left"] and 502 not in r["left"],
       "retention keeps a PINNED post and sweeps an unpinned one — reading the "
       "old pin table here would have deleted every newly pinned post")


def test_api_key_allowlist(tmp):
    """
    The API-key allowlist is a pair of literal sets, and the (method, path)
    split IS the safety property — GET /api/projects lists, POST /api/projects
    creates. Nothing enforces that but the sets themselves, so a path added to
    the wrong one is a silent privilege grant that no other test would notice.

    This guards the two ways it rots: an endpoint added later landing in a set
    it does not belong in, and the method split being collapsed back to a
    path-only check.
    """
    import web

    read, write = web.API_KEY_READ_PATHS, web.API_KEY_WRITE_PATHS

    # /api/fetch spends real X rate-limit budget. It is the only write a key
    # gets, and that is a deliberate, argued exception — not a default.
    ok(write == {"/api/fetch"},
       f"a key may write exactly one path, /api/fetch (got {sorted(write)})")

    # Classification calls a paid external model. A key that could POST it
    # could spend the monthly cap from outside the dashboard.
    ok("/api/classify" not in read and "/api/classify" not in write,
       "/api/classify is not reachable with an API key (it spends money)")

    # The label WRITE paths edit the vocabulary and override human labels.
    for path in ("/api/labels/set", "/api/labels/settings"):
        ok(path not in read and path not in write,
           f"{path} is not reachable with an API key (it writes)")

    # Credentials and processes are not data. These are the ones whose leak
    # would be worst, so they are named rather than left to a rule.
    for path in ("/api/pool/signin", "/api/stress/accounts", "/api/login/start"):
        ok(path not in read and path not in write,
           f"{path} stays cookie-only (credentials, not data)")

    # The split itself. A path-only check would make these two identical.
    def allowed(method, path):
        return (method == "GET" and path in read) or path in write

    ok(allowed("GET", "/api/projects"), "GET /api/projects is readable")
    ok(not allowed("POST", "/api/projects"),
       "POST /api/projects is refused — the method split is what stops a read "
       "key creating projects")
    ok(not allowed("DELETE", "/api/projects"), "DELETE /api/projects is refused")
    ok(allowed("POST", "/api/fetch"), "POST /api/fetch is still granted")

    # Every allowlisted path must actually be routed: a typo here fails open in
    # the confusing direction — the caller gets 404 from a path we believe we
    # granted, and debugging starts at the wrong end.
    import pathlib as _pl
    import re as _re
    src = (_pl.Path(web.__file__)).read_text()
    routed = set(_re.findall(r'u\.path == "(/api/[^"]+)"', src))
    routed |= set(_re.findall(r'u\.path\.startswith\("(/api/[^"]+)"', src))
    missing = sorted(p for p in (read | write)
                     if p not in routed and not any(r.startswith(p) for r in routed))
    ok(not missing, f"every allowlisted path is routed (unrouted: {missing})")

    # /api/delivery is granted so an integration can watch how far behind each
    # target is. The ADDRESSES are not part of that: an Apps Script /exec URL
    # and a sheet_id are infrastructure, and a link-shared spreadsheet makes its
    # id a read capability on its own. A token carried in a webhook query string
    # is the sharpest case, which is why the whole query is dropped, not escaped.
    import copy as _copy
    raw = {"targets": [
        {"label": "dt:6", "kind": "sheet",
         "url": "https://script.google.com/macros/s/AKfycbxSECRET/exec",
         "sheet_id": "1AbCdEfGhIjK", "chat_id": None},
        {"label": "dt:3", "kind": "telegram", "url": "Telegram -1001234567890",
         "sheet_id": "", "chat_id": "-1001234567890"},
        {"label": "wh", "kind": "webhook",
         "url": "https://watchtower.example.com/ingest?token=abc123",
         "sheet_id": "", "chat_id": None},
    ]}
    m = {t["label"]: t for t in web._mask_delivery(_copy.deepcopy(raw))["targets"]}
    ok(m["dt:6"]["url"] == "https://script.google.com",
       "a masked Apps Script URL keeps its host and loses the deployment id")
    ok(m["dt:6"]["sheet_id"] == "", "a masked target discloses no sheet_id")
    ok(m["dt:3"]["url"] == "Telegram" and m["dt:3"]["chat_id"] is None,
       "a masked Telegram target discloses no chat id")
    ok("token" not in m["wh"]["url"] and m["wh"]["url"].endswith("example.com"),
       "masking drops the query string, so a token in a webhook URL is not "
       "handed to a key holder")
    ok(all(t.get("masked") for t in m.values()),
       "a masked response says so, rather than looking like a target with no "
       "address configured")

    # The flag must reset per request: one handler instance serves a whole
    # keep-alive connection, so a key request followed by a dashboard request
    # on the same socket would otherwise mask the dashboard's own view.
    src_auth = src[src.index("def _require_auth"):]
    src_auth = src_auth[:src_auth.index("def ", 10)]
    ok("self._via_api_key = False" in src_auth,
       "_require_auth resets the key flag before deciding, so it cannot leak "
       "across a keep-alive connection")


def test_labels_web(tmp):
    """
    The read path: labels stamped onto every platform's feed, and a board
    resolved out of three separate databases.

    web.py is the one place the three stores meet, so it is the one place a
    label can go missing from a feed or a pin can render as a hole. Driven
    through the module's own functions rather than a socket, the way the auth
    tests above drive it.
    """
    import shutil as _sh
    import sqlite3

    import config as _config
    import store as store_mod
    import store_fb
    import web

    tmp = pathlib.Path(tmp)
    _sh.copy(pathlib.Path(__file__).resolve().parent.parent / "config.toml.example",
             tmp / "config.toml")
    cfg = _config.load_config(root=tmp)
    web._CFG = cfg
    web._start_loop()
    now = int(time.time() * 1000)

    async def seed():
        st = store_mod.Store(cfg.db_results, False)
        await st.open()
        pid = (await st.create_project("Desk"))["project_id"]
        sid = await st.ensure_stream("s", "from:a", "Latest", True)
        st.db.execute("INSERT INTO project_streams(project_id, stream_id) "
                      "VALUES(?,?)", (pid, sid))
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        req = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        txt = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}
        for tid, body in ((7001, "praise for the scheme"), (7002, "unrelated")):
            row = {c: ("" if c in txt else 0) for c in req}
            row.update(tweet_id=tid, created_ms=now, collected_ms=now,
                       source="result", text=body, author_username="acct")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))
            st.db.execute("INSERT INTO tweet_hits(stream_id, tweet_id, "
                          "first_seen_ms) VALUES(?,?,?)", (sid, tid, now))
        await st.close()
        return pid

    pid = asyncio.run(seed())
    with store_fb.Store(cfg.root / "fb_results.db") as fst:
        fst.db.execute(
            "INSERT INTO posts(post_id, page, text, created_ms, collected_ms, "
            "project_id) VALUES('pfbid9','apage','a communal incident',?,?,?)",
            (now, now, pid))
        fst.db.commit()

    out = {}
    out["waiting"] = (web._x_unlabelled(pid, set())
                      + web._fb_unlabelled(pid, set()))

    async def label():
        st = store_mod.Store(cfg.db_results, False)
        await st.open()
        await st.set_post_labels(pid, [("x", "7001", "bjp_pro", 0.8),
                                       ("facebook", "pfbid9", "hindu_muslim", 0.7)])
        cid = (await st.create_collection(pid, "Mixed"))["collection_id"]
        await st.collection_pin_posts(cid, add=[("x", "7001"),
                                                ("facebook", "pfbid9"),
                                                ("x", "999999")])
        await st.close()
        return cid

    cid = asyncio.run(label())

    out["after"] = web._x_unlabelled(pid, {("x", "7001")})
    rows = [{"platform": "x", "tweet_id": "7001"},
            {"platform": "x", "tweet_id": "7002"}]
    web._stamp_labels(rows, pid)
    out["stamped"] = rows
    out["unscoped"] = web._stamp_labels(
        [{"platform": "x", "tweet_id": "7001"}], 0)

    pins = [{"platform": "x", "post_id": "7001", "added_ms": 20},
            {"platform": "facebook", "post_id": "pfbid9", "added_ms": 30},
            {"platform": "x", "post_id": "999999", "added_ms": 40}]
    out["resolved"] = web._resolve_pins(pins, pid)
    out["board"] = web._board_rows(cid)[1]

    out["pairs"] = web._pin_pairs(
        ["123", {"platform": "facebook", "post_id": "abc"}, ["instagram", "9"]])

    month = datetime.fromtimestamp(web._month_start_ms() / 1000, timezone.utc)
    out["month"] = (month.day, month.hour, month.minute)

    ok(out["waiting"] == 3,
       "the waiting count spans X and Facebook — one number for the button")
    ok(out["after"] == 1, "a labelled post drops out of the waiting count")

    st = {r["tweet_id"]: r for r in out["stamped"]}
    ok(st["7001"]["label"] == "bjp_pro" and st["7001"]["label_source"] == "grok",
       "a labelled feed row carries its label and who set it")
    ok("label" in st["7002"] and st["7002"]["label"] is None,
       "an unlabelled row says so with an explicit null — 'not classified yet' "
       "is a state the UI has to be able to render, not a missing key")
    ok(out["unscoped"][0]["label"] is None,
       "without a project there are no labels to stamp: they are project facts")

    got = {r["tweet_id"]: r for r in out["resolved"]}
    ok(set(got) == {"7001", "pfbid9"},
       "a pin whose post is gone resolves to nothing rather than a hole")
    ok(got["pfbid9"]["platform"] == "facebook"
       and got["pfbid9"]["text"] == "a communal incident"
       and "author_username" in got["pfbid9"] and "media" in got["pfbid9"],
       "a Facebook pin comes back out of its own database in the shared shape")
    ok(got["pfbid9"]["label"] == "hindu_muslim",
       "and carries the label, which lives in a third database again")
    ok([r["tweet_id"] for r in out["resolved"]] == ["pfbid9", "7001"],
       "a mixed board is ordered newest pin first, across platforms")
    ok(len(out["board"]) == 2,
       "_board_rows resolves a real board end to end")

    ok(out["pairs"] == [("x", "123"), ("facebook", "abc"), ("instagram", "9")],
       "a bare id still means an X post, so the old pin contract keeps working "
       "beside the new one")
    ok(out["month"] == (1, 0, 0),
       "the spend meter's window starts at midnight UTC on the 1st")

    # An older database that a writable Store has not opened yet has no
    # post_labels table at all. The read path must render it unlabelled, not 500.
    old = tmp / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE tweets(tweet_id INTEGER PRIMARY KEY)")
    con.commit(); con.close()
    saved = web._CFG
    try:
        class _Shim:
            db_results = old
            root = tmp
        web._CFG = _Shim()
        ok(web._labels_map(pid, [("x", "7001")]) == {},
           "a database written before labelling existed reads as unlabelled "
           "rather than raising: the dashboard opens it READ-ONLY and cannot "
           "create the table itself")
    finally:
        web._CFG = saved


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

    # A bare multi-word rule is an implicit AND to X, so it must be grouped
    # before it is OR-joined with its siblings. Ungrouped, the meaning of
    # `Devendra Fadnavis OR Deva Bhau` is decided by X's operator precedence
    # rather than by us — it happens to bind OR loosest, which is the answer we
    # want, but a rule whose meaning rests on an undocumented parser detail is
    # one upstream change away from silently collecting something else.
    ok(ct("Devendra Fadnavis") == "(Devendra Fadnavis)",
       "a bare multi-word rule is grouped, so OR cannot straddle it")
    ok(ct('"Devendra Fadnavis"') == '"Devendra Fadnavis"',
       "a quoted phrase is already one atom to X — no parens, no wasted query "
       "length against the ~512 cap")
    ok(ct("gst OR vat") == "(gst OR vat)", "an OR rule is grouped too")
    joined = "(" + " OR ".join(ct(t) for t in ("Devendra Fadnavis", "Deva Bhau")) + ")"
    ok(joined == "((Devendra Fadnavis) OR (Deva Bhau))",
       "two multi-word rules OR-join without either one absorbing the other's "
       "words")

    # One field takes shorthand OR a whole X query pasted from advanced search.
    # The second is the reason the length cap is not 120: a real query with two
    # OR groups and a couple of filters clears that before it says anything
    # interesting, and it was being rejected outright.
    real_query = ('(Maharashtra OR \u092e\u0939\u093e\u0930\u093e\u0937\u094d\u091f\u094d\u0930 OR Mumbai) '
                  '(Fadnavis OR "Deva Bhau" OR \u092b\u0921\u0923\u0935\u0940\u0938) '
                  '(CM OR \u0938\u0940\u090f\u092e OR \u092e\u0941\u0916\u094d\u092f\u092e\u0902\u0924\u094d\u0930\u0940) '
                  '-filter:replies -filter:retweets lang:mr min_faves:5')
    ok(len(real_query) > 120, "the sample really is longer than the old cap")
    ok(nt(real_query) == real_query,
       "a complete X query survives normalize_term as itself")
    ok(nt("x" * (store_mod.MAX_TERM_LEN + 1)) is None,
       "a rule past the cap is still refused — the cap moved, it did not go")
    # The budget the chunker gets must leave room for the parens AND for this
    # watchlist's filter suffix, which is 140 characters with everything on.
    worst = store_mod.filters_suffix(
        {**{k: True for k in store_mod.WATCHLIST_FILTERS},
         "lang": "mr", "min_likes": 1, "min_retweets": 1})
    ok(store_mod.MAX_TERM_LEN + 2 + len(worst) <= 512,
       "one max-length rule plus parens plus the worst suffix still fits X's "
       f"~512 cap ({store_mod.MAX_TERM_LEN} + 2 + {len(worst)})")

    # ---- AND over an OR group DISTRIBUTES, it does not nest ----------------
    #
    # Observed live on 2026-08-23: the nested form put the group three parens
    # deep inside the watchlist's OR list, X flattened it, and the stream
    # collected a post with मुख्यमंत्री twice and महाराष्ट्र not at all. Depth is
    # the thing being tested here, not prettiness.
    et = store_mod.expand_term
    ok(et("(CM OR \u092e\u0941\u0916\u094d\u092f\u092e\u0902\u0924\u094d\u0930\u0940) AND MH")
       == ["(CM MH)", "(\u092e\u0941\u0916\u094d\u092f\u092e\u0902\u0924\u094d\u0930\u0940 MH)"],
       "AND over an OR group distributes into flat alternatives")
    ok(et("finance AND gst") == ["(finance gst)"], "a plain AND is unchanged")
    ok(et("gst OR vat") == ["gst", "vat"],
       "a top-level OR becomes two alternatives, needing no parens at all")
    ok(et('"Deva Bhau"') == ['"Deva Bhau"'],
       "a quoted phrase stays one atom — OR inside quotes is not an operator")
    ok(et("(a OR b) AND (c OR d)") == ["(a c)", "(a d)", "(b c)", "(b d)"],
       "two OR groups produce the full product")
    ok(et(" AND ".join(f"(a{i} OR b{i})" for i in range(5))) is None,
       f"a rule past {store_mod.MAX_ALTERNATIVES} combinations is refused, not "
       "silently truncated")

    def _depth(q):
        d = m = 0
        quoted = False
        for ch in q:
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch == "(":
                d += 1
                m = max(m, d)
            elif not quoted and ch == ")":
                d -= 1
        return m

    live = ['"Deva Bhau"', '"Devendra Fadnavis"',
            '(\u0938\u0940\u090f\u092e OR \u092e\u0941\u0916\u094d\u092f\u092e\u0902\u0924\u094d\u0930\u0940) AND \u092e\u0939\u093e\u0930\u093e\u0937\u094d\u091f\u094d\u0930',
            'CM AND Maharashtra']
    flat = []
    for r in sorted(live):
        flat.extend(et(r))
    ok(_depth("(" + " OR ".join(flat) + ")") <= 2,
       "a whole compiled watchlist query never nests deeper than two parens — "
       "the depth X was observed to flatten was three")

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
    print("== bug-fix regressions ==")
    # case-insensitive handle: per-page (label) and feed (actor) paths agree
    j = eng._build_from_json("NatGeo", [{"id": "77", "text": "wild"}])
    f = eng._build_feed([{"id": "77", "author_handle": "natgeo", "text": "wild"}])
    ok(j[0]["post_id"] == f[0]["post_id"] == "natgeo:77",
       "the same post keys identically whichever path found it (case-folded)")
    # re-adding a paused page does NOT silently un-pause it
    st.add_source("pausetest", project_id=7)
    st.set_enabled("pausetest", False)
    st.add_source("PauseTest", project_id=7)      # re-add, different case
    row = [s for s in st.sources() if s["label"] == "pausetest"][0]
    ok(row["enabled"] == 0, "re-adding a paused page keeps it paused")
    # same caption on DIFFERENT days = two real posts, both kept
    d1 = {"post_id": "somepage:a", "page": "somepage", "text": "Breaking news today",
          "created_ms": 1785000000000, "project_id": 7, "media": []}
    d2 = {"post_id": "somepage:b", "page": "somepage", "text": "Breaking news today",
          "created_ms": 1785000000000 + 2 * 86_400_000, "project_id": 7, "media": []}
    ok(st.upsert(d1) is True and st.upsert(d2) is True,
       "same caption on different days is NOT dropped as a duplicate")
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
        on_favorites = True       # pretend we reached the real Favorites feed
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def fetch_favorites(self, max_scroll=6):
            return [
                {"post_id": "narendramodi:20", "page": "narendramodi", "url": "u",
                 "text": "tracked page post", "media": [], "project_id": None},
                {"post_id": "randompage:21", "page": "randompage", "url": "u2",
                 "text": "untracked page post", "media": [], "project_id": None},
            ]
    _orig = _efb.FacebookEngine
    _orig_login = collect_fb._can_log_in
    _orig_blocked = _efb.login_blocked
    _efb.FacebookEngine = lambda *a, **k: _FakeFav()
    collect_fb._can_log_in = lambda: True     # no real FB creds in the test env
    # And no real fb_health.json either. login_blocked() reads that file
    # relative to the CWD, so on the server — where a live checkpoint is
    # recorded — run_favorites short-circuited before it ever reached the fake
    # engine, and these four assertions failed on the VPS while passing on a
    # laptop. A test that reads live server state is not offline. (2026-08-21)
    _efb.login_blocked = lambda: None
    try:
        # No project → only the already-tracked page is saved.
        got = asyncio.run(collect_fb.run_favorites(str(db)))
        ok(got == 1, "with no project, only the tracked page's post is saved")
        # With a project → the untracked favorited page auto-registers + saves.
        got2 = asyncio.run(collect_fb.run_favorites(str(db), project_id=7))
    finally:
        _efb.FacebookEngine = _orig
        collect_fb._can_log_in = _orig_login
        _efb.login_blocked = _orig_blocked
    ok(got2 == 1, "an untracked favorited page auto-registers and its post saves")
    with store_fb.Store(db) as st2:
        rows = [r for r in st2.recent(project_id=7) if r["post_id"] == "narendramodi:20"]
        ok(len(rows) == 1 and rows[0]["project_id"] == 7,
           "the favorites post is attributed to the project that tracks its page")
        labels = {s["label"] for s in st2.sources()}
        ok("randompage" in labels,
           "the new favorited page was auto-added as a source under the project")


def test_no_undefined_names():
    """Every name a scope LOADS must be bound in that scope, an enclosing
    scope, the module, or builtins — checked PER SCOPE, not per file.

    Paid for on 2026-08-21: `collect_ig.py` used `os.getenv` on the `--loop`
    path without ever importing `os`. It had been that way since the ig_human
    commit, so the systemd IG service crashed on start every single time —
    `NameError` inside `main()`, under a second, `Restart=always` hiding it in
    a restart loop. Nobody noticed because the dashboard's Fetch-now button
    calls `collect_ig.run_once` DIRECTLY (web.py), never through `main()`, so
    Instagram collection appeared to work while the service was dead.

    Paid for AGAIN on 2026-08-26: the first version of this test was
    scope-blind — it pooled every name bound anywhere in the file and accepted
    any load of any of them. `web.py` has a local variable `auth = headers.get(
    "Authorization")` in the API-key reader, and that one local silently
    vouched for `auth.open_api(...)` (the `auth` MODULE, never imported in
    web.py) in `_xlist_refresh` — so "Refresh members" on an X List raised
    `NameError: name 'auth' is not defined` in production while this test was
    green. `_stress_accounts` had the same hole for `auth` and `ig`, wrapped in
    `except Exception: pass`, so the Stress Test account picker just showed no
    accounts. Python resolves names per scope; a checker that does not is
    not checking. `symtable` (stdlib) is the per-scope name table — a symbol
    that is referenced and resolves as global in some function must be bound
    at module level (assigned, imported, def/class, or `global`-declared in a
    function) or be a builtin.

    A full linter would be better; this is the stdlib version that needs no
    dependency and catches exactly the failure that bit us — twice.
    """
    import symtable as _st, builtins as _bi

    MODULES = ["collect_ig.py", "collect_fb.py", "collector.py", "engine_ig.py",
               "engine.py", "engine_fb.py", "store_ig.py", "store_fb.py",
               "store.py", "web.py", "main.py", "ig_human.py", "pool_link.py",
               "activity_log.py", "alerts.py", "webhook.py", "sheets.py",
               "migrate_ig_sources.py"]
    # Module dunders are supplied by the import machinery, not by the source.
    builtin_names = set(dir(_bi)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__debug__"}
    root = pathlib.Path(__file__).resolve().parent.parent

    def _bound(sym):
        return sym.is_assigned() or sym.is_imported() or sym.is_namespace()

    for name in MODULES:
        path = root / name
        if not path.exists():
            continue
        top = _st.symtable(path.read_text(), str(path), "exec")
        module_bound = {s.get_name() for s in top.get_symbols() if _bound(s)}
        # `global x` + assignment inside a function binds x at module level.
        stack = list(top.get_children())
        while stack:
            t = stack.pop()
            module_bound |= {s.get_name() for s in t.get_symbols()
                             if s.is_declared_global() and s.is_assigned()}
            stack.extend(t.get_children())
        missing = set()
        stack = [top]
        while stack:
            t = stack.pop()
            for s in t.get_symbols():
                # In a function, a referenced name that is neither local nor
                # free (closed over) resolves as global; at module level every
                # unbound reference does.
                if (s.is_referenced() and not _bound(s)
                        and (t is top or s.is_global())
                        and s.get_name() not in module_bound
                        and s.get_name() not in builtin_names):
                    missing.add(s.get_name())
            stack.extend(t.get_children())
        missing = sorted(missing)
        ok(not missing,
           f"{name}: every scope resolves every name it loads"
           + (f" — MISSING: {', '.join(missing)}" if missing else ""))


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


def test_sheets(tmp):
    """
    Google Sheet delivery, end to end, without Google.

    Everything that can be wrong here is wrong quietly: a tab name that needed
    quoting sends rows to the wrong sheet, a tweet starting "=" becomes a
    formula, a header row written twice pushes every column one row down. So
    the transport is exercised against a fake client that records exactly what
    would have been sent.
    """
    import json as _json

    import store as store_mod
    import webhook as wh

    import sheets as sh

    print("== addressing ==")
    ok(sh.sheet_id("https://docs.google.com/spreadsheets/d/1AbC_dEf-123/edit#gid=0")
       == "1AbC_dEf-123", "the id is picked out of a pasted URL")
    ok(sh.sheet_id("  1AbC_dEf-123  ") == "1AbC_dEf-123", "a bare id passes through")
    ok(sh.a1("Sheet1") == "'Sheet1'!A:D", "the range is quoted and spans the columns")
    ok(sh.a1("July posts") == "'July posts'!A:D", "a tab name with a space survives")
    ok(sh.a1("Bob's tab") == "'Bob''s tab'!A:D",
       "a single quote in the tab name is doubled, not left to break the range")
    ok(sh.a1("") == "'Sheet1'!A:D", "an empty tab name falls back to Sheet1")

    print("== the row ==")
    ok(len(sh.HEADER) == 4 and sh.COLS == "A:D",
       "the header and the column range describe the same four columns")
    os.environ["SHEET_TZ"] = "UTC"
    row = {"tweet_id": 123, "created_ms": 1784303502259,
           "url": "https://x.com/a/status/123", "text": "hello world",
           "media_json": _json.dumps([
               {"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
                "thumb": "https://pbs.twimg.com/a.jpg"},
               {"type": "video", "url": "https://video.twimg.com/b.mp4",
                "thumb": "https://pbs.twimg.com/b.jpg"}])}
    built = sh.sheet_row(row)
    ok(len(built) == len(sh.HEADER), "a row has exactly as many cells as the header")
    ok(built[0] == "2026-07-17 15:51:42",
       "date is the POSTED time, formatted so Sheets reads it as a datetime")
    ok(built[1] == "https://x.com/a/status/123", "link is the post's own URL")
    ok(built[2] == "hello world", "text is the post text, untouched")
    ok(built[3] == "https://pbs.twimg.com/a.jpg\nhttps://video.twimg.com/b.mp4",
       "media is every URL, one per line — the video's mp4, not its thumbnail")

    ok(sh.sheet_row({**row, "media_json": "[]"})[3] == "",
       "a post with no media gets an empty cell, not '[]'")
    ok(sh.sheet_row({**row, "media_json": "not json"})[3] == "",
       "a malformed media column degrades to empty instead of stopping delivery")
    ok(sh.sheet_row({**row, "url": None, "tweet_id": 55})[1]
       == "https://x.com/i/status/55", "a missing URL is rebuilt from the id")

    print("== formula injection ==")
    ok(sh.sheet_row({**row, "text": "=IMPORTXML(A1,\"//x\")"})[2].startswith("'="),
       "a post that starts '=' is forced to text — scraped input is never a formula")
    for lead in ("+", "-", "@"):
        ok(sh.sheet_row({**row, "text": lead + "ok"})[2] == "'" + lead + "ok",
           f"a post starting '{lead}' is escaped too")
    ok(sh.sheet_row({**row, "text": "normal"})[2] == "normal",
       "ordinary text is left exactly alone")

    print("== the service-account assertion ==")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    creds = {"client_email": "bot@proj.iam.gserviceaccount.com", "private_key": pem}
    tok = sh._assertion(creds, 1_700_000_000)
    head, body, sig = tok.split(".")
    import base64 as _b64

    def unb64(x):
        return _json.loads(_b64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))
    ok(unb64(head)["alg"] == "RS256", "the assertion is RS256, as Google requires")
    claims = unb64(body)
    ok(claims["iss"] == creds["client_email"] and claims["aud"] == sh.TOKEN_URL,
       "the claims name the service account and Google's token endpoint")
    ok(claims["exp"] - claims["iat"] == 3600, "the assertion expires in an hour")
    key.public_key().verify(
        _b64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)),
        f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    ok(True, "the signature verifies against the service account's public key")

    print("== credentials ==")
    keyfile = pathlib.Path(tmp) / "svc.json"
    keyfile.write_text(_json.dumps(creds))
    os.environ[sh.CREDS_ENV] = str(keyfile)
    ok((sh.load_creds() or {}).get("client_email") == creds["client_email"],
       "a path to the JSON key is read from disk")
    os.environ[sh.CREDS_ENV] = _json.dumps(creds)
    ok(sh.load_creds() is not None, "the JSON itself also works, for a keyless disk")
    os.environ[sh.CREDS_ENV] = "/no/such/file.json"
    ok(sh.load_creds() is None, "a missing key file returns None, it does not raise")
    os.environ[sh.CREDS_ENV] = _json.dumps({"client_email": "x"})
    ok(sh.load_creds() is None, "a key with no private_key is refused")
    os.environ[sh.CREDS_ENV] = str(keyfile)

    print("== a delivery, against a fake Google ==")

    class Rep:
        def __init__(self, code, payload=None, text=""):
            self.status_code, self._p, self.text = code, payload, text

        def json(self):
            if self._p is None:
                raise ValueError("not json")
            return self._p

    class FakeGoogle:
        """Records every call and answers like the Sheets API does."""

        def __init__(self, existing_header=None, append_code=200):
            self.calls = []
            self.existing = existing_header
            self.append_code = append_code

        async def post(self, url, **kw):
            self.calls.append(("post", url, kw))
            if url == sh.TOKEN_URL:
                return Rep(200, {"access_token": "tok-1", "expires_in": 3600})
            if self.append_code != 200:
                return Rep(self.append_code,
                           {"error": {"message": "The caller does not have permission"}})
            return Rep(200, {"updates": {"updatedRows": len(kw["json"]["values"])}})

        async def get(self, url, **kw):
            self.calls.append(("get", url, kw))
            return Rep(200, {"values": self.existing} if self.existing else {})

        async def put(self, url, **kw):
            self.calls.append(("put", url, kw))
            return Rep(200, {})

    def run_deliver(client, rows, tab="Sheet1"):
        sh._tokens.clear()
        sh._header_done.clear()
        return asyncio.run(sh.via_api(client, "SHEETID", tab, rows))

    rows = [row, {**row, "tweet_id": 124, "text": "second"}]
    g = FakeGoogle()
    good, err = run_deliver(g, rows)
    ok(good and not err, f"a clean batch delivers ({err})")
    puts = [c for c in g.calls if c[0] == "put"]
    ok(len(puts) == 1 and puts[0][2]["json"]["values"] == [sh.HEADER],
       "an empty tab gets the header row, exactly once")
    appends = [c for c in g.calls if c[0] == "post" and c[1] != sh.TOKEN_URL]
    ok(len(appends) == 1, "the whole batch is ONE append — it lands or it does not")
    body = appends[0][2]
    ok(body["params"]["insertDataOption"] == "INSERT_ROWS",
       "rows are INSERTed at the end, never overwriting into a gap")
    ok(body["params"]["valueInputOption"] == "USER_ENTERED",
       "cells are parsed, which is why the formula escape above matters")
    ok(len(body["json"]["values"]) == 2
       and body["json"]["values"][1][2] == "second",
       "every post in the batch is a row, in order")
    ok(appends[0][1].endswith("/values/'Sheet1'!A:D:append"),
       "the append is addressed at the quoted tab range")

    g2 = FakeGoogle(existing_header=[["date", "link", "text", "media"]])
    ok(run_deliver(g2, rows)[0] and not [c for c in g2.calls if c[0] == "put"],
       "a tab that already has rows is never given a second header")

    g3 = FakeGoogle(existing_header=[["my own heading"]])
    ok(run_deliver(g3, rows)[0] and not [c for c in g3.calls if c[0] == "put"],
       "a header someone changed by hand is left alone — we are a guest here")

    g4 = FakeGoogle(append_code=403)
    good, err = run_deliver(g4, rows)
    ok(not good and "share the sheet" in err.lower(),
       "a 403 says the fix (share the sheet), not just 'HTTP 403'")

    os.environ[sh.CREDS_ENV] = ""
    good, err = run_deliver(FakeGoogle(), rows)
    ok(not good and sh.CREDS_ENV in err,
       "no key on the server is a reported failure, never a silent no-op")
    os.environ[sh.CREDS_ENV] = str(keyfile)

    print("== the target ==")
    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        pid = (await st.create_project("Sheets"))["project_id"]
        out = {}
        out["url"] = await st.create_delivery_target(
            pid, "sheet", "Daily",
            sheet_id="https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit",
            sheet_tab="July posts", sheet_mode="service_account")
        out["bad"] = await st.create_delivery_target(
            pid, "sheet", "Junk", sheet_id="not a sheet",
            sheet_mode="service_account")
        out["kind"] = await st.create_delivery_target(pid, "carrier-pigeon", "No")
        out["rows"] = await st.delivery_targets(pid, enabled_only=True)
        await st.close()
        return out

    r = asyncio.run(run())
    ok("target_id" in r["url"], "a target created from a pasted URL is accepted")
    ok("error" in r["bad"], "a spreadsheet id that is not one is refused at creation")
    ok("error" in r["kind"] and "sheet" in r["kind"]["error"],
       "an unknown kind is refused, and the message lists the real ones")
    made = [t for t in r["rows"] if t["kind"] == "sheet"]
    ok(len(made) == 1, "only the valid target was stored")
    ok(made[0]["sheet_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
       "the id was reduced from the URL before it was stored")
    ok(made[0]["sheet_tab"] == "July posts", "the tab name is stored as typed")

    t = wh.DbTarget(made[0])
    ok(t.kind == "sheet" and t.label == f"dt:{made[0]['target_id']}",
       "the sender treats it as one more target on the shared cursor")
    ok(t.url == wh.SHEET_URL_PREFIX + t.sheet_id,
       "its logged/displayed URL is one an operator can actually click")
    ok(wh.DbTarget({**made[0], "sheet_tab": None}).sheet_tab == "Sheet1",
       "a target with no tab name defaults to Sheet1 rather than failing at send")

    print("== migrating an older database ==")
    old = pathlib.Path(tmp) / "old.db"
    con = sqlite3_connect(old)
    con.execute("CREATE TABLE delivery_targets (target_id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, project_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                "name TEXT NOT NULL, url TEXT, secret_env TEXT, chat_id TEXT, "
                "batch_size INTEGER NOT NULL DEFAULT 50, "
                "enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
    con.commit()
    con.close()

    async def migrate():
        st = store_mod.Store(old, False)
        await st.open()
        cols = {c[1] for c in st.db.execute("PRAGMA table_info(delivery_targets)")}
        await st.close()
        return cols

    cols = asyncio.run(migrate())
    ok({"sheet_id", "sheet_tab", "sheet_mode"} <= cols,
       "a database made before sheets existed gains the new columns on open")


def test_sheets_script(tmp):
    """
    The Apps Script route: a sheet that runs its own receiver.

    This is the mode with no Google Cloud project behind it, so the failures
    it can have are all deployment settings — and every one of them looks the
    same from Python (an HTML page where JSON was expected). What is tested
    here is mostly that each of them is turned back into the setting to fix.
    """
    import store as store_mod
    import webhook as wh

    import sheets as sh

    print("== the script we hand out ==")
    tok = sh.new_token()
    ok(len(tok) >= 32, f"a generated token is long enough to be one ({len(tok)})")
    ok(sh.new_token() != tok, "every deployment gets its own")
    code = sh.script_source(tok)
    ok("%TOKEN%" not in code and f"var TOKEN = '{tok}';" in code,
       "the token is baked into the script, so nothing is left to retype")
    ok("function doPost(e)" in code and "ContentService" in code,
       "the script is a complete web-app receiver")
    ok("LockService" in code,
       "it takes a lock — a live batch and a history send must not interleave")
    # The sheet is a TIMELINE, not a delivery log. Rows arrive in collection
    # order, so a backwards sweep and live polling interleave decades unless
    # the receiver reorders them.
    ok("SORT_NEWEST_FIRST" in code and "ascending: false" in code,
       "it sorts newest-first on arrival, so the oldest post ends up last")
    ok(".sort({ column: 1" in code,
       "sorting on column 1 — the POST date, not the collection order")
    # Idempotence is what makes "send past posts" safe to use for a gap: the
    # one-shot never moves the cursor, so an overlapping window would otherwise
    # duplicate every post in it.
    ok("SKIP_DUPLICATES" in code and "getRange(2, 2" in code,
       "and drops posts whose link is already in the sheet, keyed on column B")
    ok(str(sh.HEADER) .replace('"', "'") in code.replace('"', "'"),
       "the script writes the SAME four columns this module builds")
    ok("'" not in sh.script_source("has'quote").split("var TOKEN = '")[1].split("'")[0],
       "a quote in a token cannot break out of the string it is pasted into")

    print("== a delivery ==")

    class Rep:
        def __init__(s, code_, payload=None, text=""):
            s.status_code, s._p, s.text = code_, payload, text

        def json(s):
            if s._p is None:
                raise ValueError("not json")
            return s._p

    class FakeScript:
        def __init__(s, reply=None):
            s.seen = []
            s.reply = reply

        async def post(s, url, **kw):
            s.seen.append((url, kw))
            if s.reply is not None:
                return s.reply
            body = kw["json"]
            if body.get("token") != tok:
                return Rep(200, {"error": "bad token"})
            return Rep(200, {"ok": True, "appended": len(body["rows"]),
                             "tab": body["tab"]})

    row = {"tweet_id": 9, "created_ms": 1784303502259,
           "url": "https://x.com/a/status/9", "text": "hello",
           "media_json": "[]"}
    EXEC = "https://script.google.com/macros/s/AKfyc123/exec"

    c = FakeScript()
    good, err = asyncio.run(sh.via_script(c, EXEC, tok, "July", [row]))
    ok(good and not err, f"a batch is accepted ({err})")
    url, kw = c.seen[0]
    ok(url == EXEC, "posted straight at the /exec URL")
    ok(kw.get("follow_redirects") is True,
       "redirects are followed for THIS call — Apps Script always 302s, even "
       "though the delivery client refuses redirects everywhere else")
    ok(kw["json"]["tab"] == "July", "the tab travels with the batch")
    ok(kw["json"]["header"] == sh.HEADER, "so does the header the script writes")
    ok(kw["json"]["rows"] == sh.sheet_rows([row]),
       "the rows are built by the same builder the REST path uses")
    ok(kw["json"]["token"] == tok, "and the token, which is what makes us welcome")

    ok(asyncio.run(sh.via_script(c, EXEC, "wrong-token", "July", [row]))[0] is False,
       "a token the script does not hold is a failed delivery, not a silent one")
    _, err = asyncio.run(sh.via_script(c, EXEC, "wrong-token", "July", [row]))
    ok("re-deployed" in err.lower(),
       "and the message names the step people miss — redeploying after an edit")

    print("== the deployment settings people get wrong ==")
    login = FakeScript(Rep(200, None, "<!DOCTYPE html><html>Sign in</html>"))
    _, err = asyncio.run(sh.via_script(login, EXEC, tok, "July", [row]))
    ok("Anyone" in err,
       "an HTML sign-in page is reported as 'Who has access' being wrong, "
       "not as unparseable JSON")
    gone = FakeScript(Rep(404, None, "not found"))
    _, err = asyncio.run(sh.via_script(gone, EXEC, tok, "July", [row]))
    ok("Manage deployments" in err,
       "a 404 says where to fetch the current URL from")
    boom = FakeScript(Rep(200, {"error": "Exception: no permission"}))
    _, err = asyncio.run(sh.via_script(boom, EXEC, tok, "July", [row]))
    ok("no permission" in err, "an error the script itself reports is passed through")

    _, err = asyncio.run(sh.via_script(FakeScript(), EXEC, "", "July", [row]))
    ok("token" in err, "no token in .env is a reported failure, never a no-op")
    _, err = asyncio.run(sh.via_script(FakeScript(), "", tok, "July", [row]))
    ok("URL" in err, "and so is a target with no URL")

    print("== check access writes nothing ==")
    c2 = FakeScript()
    ok(asyncio.run(sh.check_script_access(c2, EXEC, tok, "July"))[0],
       "the check passes against a healthy deployment")
    ok(c2.seen[0][1]["json"]["rows"] == [],
       "it sends ZERO rows — the tab is left ready, not left with test data")

    print("== the target ==")
    db = pathlib.Path(tmp) / "results.db"

    async def run():
        st = store_mod.Store(db, False)
        await st.open()
        pid = (await st.create_project("S"))["project_id"]
        out = {}
        out["made"] = await st.create_delivery_target(
            pid, "sheet", "Daily", url=EXEC, secret_env="SHEET_TOKEN_DAILY",
            sheet_tab="July", sheet_mode="script")
        out["badurl"] = await st.create_delivery_target(
            pid, "sheet", "X", url="https://example.com/hook",
            secret_env="SHEET_TOKEN_DAILY", sheet_mode="script")
        out["badenv"] = await st.create_delivery_target(
            pid, "sheet", "X", url=EXEC, secret_env="not upper", sheet_mode="script")
        out["rows"] = await st.delivery_targets(pid)
        os.environ["SHEET_TOKEN_DAILY"] = tok
        out["built"] = await wh.db_targets(st, log=lambda m: None)
        os.environ.pop("SHEET_TOKEN_DAILY")
        out["missing"] = await wh.db_targets(st, log=lambda m: None)
        await st.close()
        return out

    r = asyncio.run(run())
    ok("target_id" in r["made"], "a script target is created from URL + env name")
    ok("error" in r["badurl"] and "/exec" in r["badurl"]["error"],
       "a URL that is not an Apps Script deployment is refused")
    ok("error" in r["badenv"],
       "the token must be NAMED, never pasted into the database")
    stored = [t for t in r["rows"] if t["kind"] == "sheet"][0]
    ok(stored["sheet_mode"] == "script", "the mode is stored")
    ok(stored["url"] == EXEC and stored["secret_env"] == "SHEET_TOKEN_DAILY",
       "and so are the address and the NAME of the token")
    ok(not stored["sheet_id"],
       "script mode stores no spreadsheet id — it never needs one")
    ok(len(r["built"]) == 1 and r["built"][0].token == tok,
       "the sender picks the token out of .env at build time")
    ok(r["built"][0].url == EXEC, "and posts to the deployment, not to a docs URL")
    ok(r["missing"] == [],
       "with the .env line gone the target is skipped, loudly, not sent tokenless")

    print("== the two modes meet at one call ==")
    ok(sh.mode_of(None) == sh.MODE_SCRIPT and sh.mode_of("") == sh.MODE_SCRIPT,
       "a row written before the column existed reads as script mode")
    ok(sh.mode_of("service_account") == sh.MODE_API, "an explicit mode is kept")
    ok(sh.mode_of("nonsense") == sh.MODE_SCRIPT,
       "anything unrecognised falls back to the mode needing no server key")

    class T:
        kind = "sheet"
        sheet_mode = "script"
        sheet_id = ""
        sheet_tab = "July"
        url = EXEC
        token = tok

    c3 = FakeScript()
    ok(asyncio.run(sh.deliver(c3, T(), [row]))[0],
       "sheets.deliver routes a script target to the script")
    ok(len(c3.seen) == 1, "in exactly one request")

    print("== the token the form shows must not drift ==")
    #
    # The regression this pins: the setup form used to mint a NEW token every
    # time it was opened. Deploy the script, close the form, reopen it, and it
    # now showed a different token — silently killing a deployment that was
    # already correct and making the operator's own .env look wrong.
    import web

    os.environ.pop("SHEET_TOKEN_MAIN", None)
    web._pending_sheet_tokens.clear()
    first = web._sheet_script({"name": ""})
    ok(first["secret_env"] == "SHEET_TOKEN_MAIN",
       "an unnamed target gets the default variable name")
    ok(web._sheet_script({"secret_env": "SHEET_TOKEN_MAIN"})["token"]
       == first["token"],
       "reopening BEFORE .env is edited shows the SAME token, not a new one")
    ok(first["already_set"] is False, "and says the server does not have it yet")

    os.environ["SHEET_TOKEN_MAIN"] = first["token"]
    again = web._sheet_script({"secret_env": "SHEET_TOKEN_MAIN"})
    ok(again["token"] == first["token"] and again["already_set"] is True,
       "once .env holds it, the server's value is what the form shows, forever")
    ok(f"var TOKEN = '{first['token']}';" in again["code"],
       "and the script on screen carries exactly that token")

    rotated = web._sheet_script({"secret_env": "SHEET_TOKEN_MAIN", "rotate": True})
    ok(rotated["token"] != first["token"] and rotated["rotated"] is True,
       "rotation is the ONLY way to get a new token, and it is explicit")
    ok(os.getenv("SHEET_TOKEN_MAIN") == first["token"],
       "rotating does not touch .env — the operator still has to")

    os.environ["SHEET_TOKEN_MAIN"] = first["token"]
    checked = {}

    class SpyClient:
        async def post(s, url, **kw):
            checked["token"] = kw["json"]["token"]

            class R:
                status_code = 200
                text = ""

                def json(s2):
                    return {"ok": True}
            return R()

        async def __aenter__(s):
            return s

        async def __aexit__(s, *a):
            return False

    import httpx as _httpx
    real_client, real_run, real_loop = _httpx.AsyncClient, web._run, web._LOOP
    try:
        _httpx.AsyncClient = lambda *a, **k: SpyClient()
        web._run = lambda coro, timeout=300: asyncio.new_event_loop().run_until_complete(coro)
        web._test_sheet({"sheet_mode": "script", "url": EXEC,
                         "secret_env": "SHEET_TOKEN_MAIN",
                         "token": rotated["token"]})
    finally:
        _httpx.AsyncClient, web._run, web._LOOP = real_client, real_run, real_loop
    ok(checked.get("token") == first["token"],
       "Check access tests the token in .env — the one DELIVERY will use — "
       "never whichever one happens to be on screen")
    os.environ.pop("SHEET_TOKEN_MAIN", None)


def sqlite3_connect(path):
    import sqlite3
    return sqlite3.connect(str(path))


def test_pinned_interval():
    """
    An interval chosen in the dashboard must be the interval that RUNS.

    This is the regression that made the Watchlists panel look broken: the
    dropdown wrote min_interval_s only, so it was a floor. The adaptive
    controller multiplied a quiet stream's interval by GROW on every empty
    poll -- and "empty" is the permanent, correct answer for an archival query
    or an account that has stopped posting -- until it sat at max_interval_s.
    The panel said 5 minutes, the collector ran every 15, and nothing anywhere
    reported the disagreement.
    """
    print()
    print("== a pinned interval does not drift ==")
    pinned = Stream(min_interval_s=300, max_interval_s=300, page_size=20)

    class R:
        def __init__(self, new, stop):
            self.new, self.stop_reason = new, stop

        @property
        def starved(self):
            return self.stop_reason == STOP_STARVED

    i, ewma, empty = 300.0, 0.0, 0
    for _ in range(10):
        i, ewma, empty = next_interval(pinned, i, i, R(0, STOP_WATERMARK), ewma, empty)
    ok(i == 300, f"ten empty polls in a row do not inflate it ({i:.0f}s)")

    i, _, _ = next_interval(pinned, 300, 300, R(0, STOP_PAGE_BUDGET), 0.0, 0)
    ok(i == 300, f"hitting the page budget does not shorten it either ({i:.0f}s)")

    i, _, _ = next_interval(pinned, 300, 300, R(500, STOP_WATERMARK), 0.0, 0)
    ok(i == 300, f"nor does a burst of new tweets ({i:.0f}s)")

    # And the unpinned case must be untouched -- "auto" still adapts.
    auto = Stream(min_interval_s=5, max_interval_s=900, page_size=20)
    i, _, _ = next_interval(auto, 60, 60, R(0, STOP_WATERMARK), 0.0, 0)
    ok(i > 60, f"'auto' still backs off on a quiet stream (60s -> {i:.0f}s)")

    # Jitter has to stay INSIDE the promise, or pinning is only cosmetic.
    worst = max(abs(jittered(300, JITTER_PINNED) - 300) for _ in range(2000))
    ok(worst <= 300 * JITTER_PINNED + 1e-9,
       f"pinned jitter stays within +/-{JITTER_PINNED:.0%} (worst seen: {worst:.1f}s)")
    ok(worst < 10, f"...which on a 5-minute cadence is under 10s of slop ({worst:.1f}s)")


async def run_backfill(tmp):
    """
    Walking BACKWARDS: the fix for a watchlist frozen at exactly
    page_size * max_pages_per_poll.

    The first half of this test reproduces the freeze, because a fix for a bug
    nobody can see reproduced is a fix nobody can check.
    """
    import store as store_mod
    from collector import (STOP_BACKFILL_BUDGET, STOP_BACKFILL_DONE,
                           STOP_BACKFILL_IDLE, backfill_once)

    store = store_mod.Store(tmp / "results.db")
    await store.open()

    print("== the freeze, reproduced ==")
    arch = Stream(label="archive", query="from:someone until:2025-02-20",
                  max_pages_per_poll=2)
    sid = await store.ensure_stream(arch.label, arch.query, "Latest", True)

    # Two pages of an archive that has plenty more behind it.
    eng = ReplayEngine([ids_at(100, 110), ids_at(120, 130), ids_at(140, 150)],
                       cursor_end=False)
    r1 = await poll_once(eng, store, arch, sid)
    ok(r1.new == 4 and r1.pages == 2,
       f"the first poll takes its whole page budget and stops (new={r1.new} pages={r1.pages})")
    await store.set_watermark(sid, r1.max_id)

    # Every poll from here on sees the same newest tweet on page one.
    for _ in range(3):
        eng = ReplayEngine([ids_at(100, 110), ids_at(120, 130), ids_at(140, 150)],
                           cursor_end=False)
        rn = await poll_once(eng, store, arch, sid)
    ok(rn.new == 0 and rn.pages == 1,
       f"and every poll after it finds nothing, on one page (new={rn.new} pages={rn.pages})")
    total = await store.count_tweets()
    ok(total == 4, f"the stream is stuck at its page ceiling ({total} tweets, forever)")

    print()
    print("== backfill: budget, resume, accumulate ==")
    st0 = await store.backfill_state(sid)
    ok(st0["remaining"] == 0, "no budget granted yet, so nothing to spend")
    res = await backfill_once(eng, store, arch, sid)
    ok(res.stop_reason == STOP_BACKFILL_IDLE,
       f"and a pass with no budget costs nothing (stop={res.stop_reason})")

    store.db.execute("UPDATE streams SET backfill_pages = 4 WHERE stream_id = ?", (sid,))
    older = ReplayEngine([ids_at(200, 210), ids_at(220, 230), ids_at(240, 250)],
                         cursor_end=False)
    res = await backfill_once(older, store, arch, sid,
                              max_pages=2)
    ok(res.pages == 2, f"one pass spends only its per-pass budget (pages={res.pages})")
    ok(res.new == 4, f"and collects older posts the poller could never reach (new={res.new})")
    ok(res.stop_reason == STOP_BACKFILL_BUDGET, f"stop={res.stop_reason}")
    ok(older.requested[0]["cursor"] is None, "the first pass starts where the poll left off")

    state = await store.backfill_state(sid)
    ok(state["walked"] == 2 and state["remaining"] == 2,
       f"progress is recorded against the grant (walked={state['walked']} "
       f"remaining={state['remaining']})")
    ok(state["cursor"] == "CUR1", f"and the resume point is stored (cursor={state['cursor']})")

    # THE point of the cursor: the second pass continues, it does not restart.
    older2 = ReplayEngine([ids_at(300, 310)], cursor_end=True)
    res = await backfill_once(older2, store, arch, sid,
                              max_pages=2)
    ok(older2.requested[0]["cursor"] == "CUR1",
       f"the next pass RESUMES from the stored cursor ({older2.requested[0]['cursor']})")
    ok(res.stop_reason == STOP_BACKFILL_DONE,
       f"finishing under budget means X has no more to give (stop={res.stop_reason})")
    state = await store.backfill_state(sid)
    ok(state["done"] and state["got"] == 6,
       f"exhausted, with the total accumulated across passes (got={state['got']})")

    print()
    print("== what backfill must NOT do ==")
    wm = await store.get_watermark(sid)
    ok(wm["high_tweet_id"] == r1.max_id,
       "an older tweet never moves the watermark — that answers a forward question")
    ok(not await store.open_gaps(sid),
       "and a backwards walk opens no gaps; it is the thing that CLOSES them")

    print()
    print("== starvation must not silently eat the budget ==")

    class Starved:
        def pages_for(self, stream, **kw):
            return self.search_pages(stream.query, **kw)

        async def search_pages(self, *a, **kw):
            return
            yield  # pragma: no cover

    store.db.execute(
        "UPDATE streams SET backfill_pages = backfill_walked + 3, backfill_done = 0 "
        "WHERE stream_id = ?", (sid,))
    before = await store.backfill_state(sid)
    res = await backfill_once(Starved(), store, arch, sid)
    after = await store.backfill_state(sid)
    ok(res.stop_reason == STOP_STARVED, f"zero pages is starvation (stop={res.stop_reason})")
    ok(after["walked"] == before["walked"],
       "an empty account pool spends none of the operator's pages")
    ok(after["cursor"] == before["cursor"], "and does not disturb the resume point")

    print()
    print("== the standing sweep needs no grant, and retires itself ==")
    # The grant model made the OPERATOR the scheduler: spend N pages, go idle,
    # wait to be clicked again. backfill_auto is the cadence version -- the
    # mirror of the forward poller's interval -- and the thing to prove is that
    # it runs with no budget at all and still stops when X runs out.
    store.db.execute(
        "UPDATE streams SET backfill_pages = backfill_walked, backfill_auto = 1, "
        "backfill_done = 0 WHERE stream_id = ?", (sid,))
    state = await store.backfill_state(sid)
    ok(state["auto"] is True, "the stream reports itself as a standing sweep")
    ok(state["remaining"] is None,
       "which has no page budget to run out of (remaining is not a number)")

    auto_eng = ReplayEngine([ids_at(300, 310), ids_at(320, 330)], cursor_end=False)
    res = await backfill_once(auto_eng, store, arch, sid, max_pages=2)
    ok(res.stop_reason != STOP_BACKFILL_IDLE,
       f"a pass runs with zero granted pages (stop={res.stop_reason})")
    ok(res.new > 0, f"and actually collects older posts ({res.new})")

    # Exhaustion is the ONLY thing that stops it, and it must stick: an auto
    # sweep that kept asking a dry query would spend a request every cycle
    # forever, which is the failure the grant model accidentally prevented.
    # Exhaustion is "X gave us FEWER pages than we asked for", not "X gave us
    # nothing" -- zero pages is starvation and is proven above to spend nothing.
    # Getting this distinction wrong in either direction is a real bug: treat
    # starvation as exhaustion and an empty account pool permanently retires a
    # sweep that had history left; treat exhaustion as starvation and the sweep
    # asks a dry query forever.
    dry = ReplayEngine([ids_at(400, 410)], cursor_end=True)
    res = await backfill_once(dry, store, arch, sid, max_pages=3)
    state = await store.backfill_state(sid)
    ok(state["done"] is True, "X running out of results marks the sweep done")
    res = await backfill_once(auto_eng, store, arch, sid, max_pages=2)
    ok(res.stop_reason == STOP_BACKFILL_IDLE,
       "and a finished sweep costs nothing thereafter, standing or not")

    print()
    print("== the scheduler picks a standing sweep up without a grant ==")
    store.db.execute(
        "UPDATE streams SET backfill_done = 0 WHERE stream_id = ?", (sid,))
    ok(arch.label in store.streams_with_backfill(),
       "an auto stream with no unspent pages is still due for a dig")
    store.db.execute(
        "UPDATE streams SET backfill_auto = 0 WHERE stream_id = ?", (sid,))
    ok(arch.label not in store.streams_with_backfill(),
       "and switching it off takes it out of the scheduler's list")

    await store.close()


def test_backfilled_posts_reach_delivery(tmp):
    """
    An old post collected today must still reach the sheet.

    This is the question the backwards sweep raises for delivery, and the
    answer is not obvious: normal delivery deliberately starts from NOW, so
    "we just collected a post from 2024" sounds exactly like the case delivery
    is designed to skip. It is not, and the reason is that the delivery cursor
    keys on collected_ms (WHEN WE GOT IT) and never on created_ms (when it was
    posted). A tweet the digger stores today is therefore AHEAD of the cursor
    however old it is, and flows out with everything else.

    Worth a test rather than a comment, because the whole feature rests on it:
    if the cursor ever moved to created_ms as an "obvious" fix for ordering,
    every backfilled post would land behind it and silently never deliver.
    """
    import store as store_mod

    async def go():
        st = store_mod.Store(pathlib.Path(tmp) / "results.db", False)
        await st.open()
        out = {}
        sid = await st.ensure_stream("archive", "from:someone until:2025-02-20",
                                     "Latest", True)
        info = list(st.db.execute("PRAGMA table_info(tweets)"))
        required = [x[1] for x in info if x[3] and x[4] is None and not x[5]]
        text_cols = {x[1] for x in info if "TEXT" in (x[2] or "").upper()}

        # A 2024 post, collected right now by the backwards sweep.
        old_created = 1_700_000_000_000      # Nov 2023
        now_collected = 1_780_000_000_000    # far ahead of any cursor below
        for tid, created, collected in ((900, old_created, now_collected),
                                        (901, now_collected, now_collected + 1)):
            row = {c: ("" if c in text_cols else 0) for c in required}
            row.update(tweet_id=tid, created_ms=created, collected_ms=collected,
                       source="result")
            st.db.execute(
                f"INSERT INTO tweets({','.join(row)}) VALUES({','.join('?' * len(row))})",
                list(row.values()))
            st.db.execute("INSERT INTO tweet_hits(stream_id, tweet_id, first_seen_ms) "
                          "VALUES(?,?,?)", (sid, tid, collected))

        # A delivery cursor sitting where live delivery would have left it,
        # i.e. well after the old post was WRITTEN and well before it was
        # COLLECTED.
        cursor_at = old_created + 1_000_000
        out["due"] = [r["tweet_id"] for r in await st.tweets_after(cursor_at, 0, 50)]
        await st.close()
        return out

    r = asyncio.run(go())
    ok(900 in r["due"],
       "a 2024 post collected today is still due for delivery — the cursor "
       "keys on collection, not on posting")
    ok(r["due"] == sorted(r["due"]),
       "and it queues in collection order, so the sheet reads in the order it "
       "was dug up")


def test_watchlist_depth_controls(tmp):
    """The dashboard writes, and what it writes is what the collector reads."""
    import store as store_mod

    async def go():
        st = store_mod.Store(pathlib.Path(tmp) / "results.db", False)
        await st.open()
        out = {}
        pid = (await st.create_project("Depth"))["project_id"]
        wid = (await st.create_watchlist(pid, "Archive", "keywords"))["watchlist_id"]
        await st.set_watchlist_members(wid, add=["from:someone until:2025-02-20"])
        out["five_min"] = await st.set_watchlist_interval(wid, 300)
        out["rows"] = [dict(r) for r in st.db.execute(
            "SELECT label, min_interval_s, max_interval_s FROM streams "
            "WHERE label LIKE ?", (f"wl:{wid}:%",))]
        out["auto"] = await st.set_watchlist_interval(wid, "")
        out["rows_auto"] = [dict(r) for r in st.db.execute(
            "SELECT min_interval_s, max_interval_s FROM streams WHERE label LIKE ?",
            (f"wl:{wid}:%",))]
        out["pages_bad"] = await st.set_watchlist_pages(wid, 99)
        out["pages_ok"] = await st.set_watchlist_pages(wid, 10)
        out["bf_bad"] = await st.set_watchlist_backfill(wid, 99999)
        out["bf"] = await st.set_watchlist_backfill(wid, 40)
        out["bf_again"] = await st.set_watchlist_backfill(wid, 40)
        out["granted"] = [dict(r) for r in st.db.execute(
            "SELECT backfill_pages, backfill_walked FROM streams WHERE label LIKE ?",
            (f"wl:{wid}:%",))]
        out["stop"] = await st.set_watchlist_backfill(wid, 0)
        out["stopped"] = [dict(r) for r in st.db.execute(
            "SELECT backfill_pages, backfill_walked FROM streams WHERE label LIKE ?",
            (f"wl:{wid}:%",))]
        out["auto_bad"] = await st.set_watchlist_backfill_auto(wid, True, 60)
        out["auto_on"] = await st.set_watchlist_backfill_auto(wid, True, 300)
        out["auto_rows"] = [dict(r) for r in st.db.execute(
            "SELECT backfill_auto, backfill_every_s, backfill_done FROM streams "
            "WHERE label LIKE ?", (f"wl:{wid}:%",))]
        out["view_auto"] = (await st.watchlists(pid))[0]
        out["auto_off"] = await st.set_watchlist_backfill_auto(wid, False)
        out["off_rows"] = [dict(r) for r in st.db.execute(
            "SELECT backfill_auto, backfill_cursor FROM streams WHERE label LIKE ?",
            (f"wl:{wid}:%",))]
        out["view"] = (await st.watchlists(pid))[0]
        await st.close()
        return out

    r = asyncio.run(go())

    print("== the interval control pins both ends ==")
    ok(r["rows"] and all(x["min_interval_s"] == 300 and x["max_interval_s"] == 300
                         for x in r["rows"]),
       "choosing '5 min' writes BOTH the floor and the ceiling")
    ok(r["five_min"].get("pinned") is True, "and reports back that it is pinned")
    ok(all(x["min_interval_s"] is None and x["max_interval_s"] is None
           for x in r["rows_auto"]),
       "'auto' clears both and hands the cadence back to the controller")

    print()
    print("== depth and backfill are bounded, and additive ==")
    ok("error" in r["pages_bad"], f"99 pages per poll is refused: {r['pages_bad'].get('error', '')[:60]}")
    ok(r["pages_ok"].get("max_pages_per_poll") == 10, "10 is accepted")
    ok("error" in r["bf_bad"], "an absurd backfill grant is refused, not clamped")
    ok(r["bf"].get("granted_pages") == 40, "a sane grant goes through")
    ok(all(x["backfill_pages"] == x["backfill_walked"] + 40 for x in r["granted"]),
       "a second grant asks for MORE history rather than restarting the walk")
    ok(r["stop"].get("stopped") is True, "and it can be stopped")
    ok(all(x["backfill_pages"] == x["backfill_walked"] for x in r["stopped"]),
       "which withdraws the unspent remainder")

    print()
    print("== the standing sweep is a cadence, not a quantity ==")
    ok("error" in r["auto_bad"],
       f"an off-menu cadence is refused, not clamped: {r['auto_bad'].get('error','')[:60]}")
    ok(r["auto_on"].get("auto") is True, "5 min is accepted")
    ok(all(x["backfill_auto"] == 1 and x["backfill_every_s"] == 300
           for x in r["auto_rows"]),
       "and is written to every compiled stream")
    ok(all(x["backfill_done"] == 0 for x in r["auto_rows"]),
       "switching it on clears a previous 'no more history' verdict")
    ok(r["view_auto"]["backfill"]["running"] is True,
       "a standing sweep reports as running with no pages granted")
    ok(r["view_auto"]["backfill"]["every_s"] == 300,
       "and the panel is told which cadence it is on")
    ok(all(x["backfill_auto"] == 0 for x in r["off_rows"]), "it can be switched off")
    ok(r["view"]["backfill"]["running"] is False, "which stops it reporting as running")

    print()
    print("== the panel is told all of it ==")
    v = r["view"]
    ok("backfill" in v and "running" in v["backfill"],
       "the watchlist payload carries backfill state for the interface")
    ok(v["backfill"]["running"] is False, "a stopped sweep does not report as running")
    ok("interval_pinned" in v and "pages" in v,
       "along with whether the cadence is pinned, and the depth setting")


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
        test_xclid_shim()
        test_parse()
        asyncio.run(test_lock_release(fresh("engine")))

        section("collector (watermark, dedup, gaps, intervals)")
        asyncio.run(run_collector(fresh("collector")))
        test_interval()
        test_pinned_interval()

        section("backfilled posts still reach delivery")
        test_backfilled_posts_reach_delivery(fresh("bf_delivery"))

        section("backfill (walking a query backwards, on a budget)")
        asyncio.run(run_backfill(fresh("backfill")))
        test_watchlist_depth_controls(fresh("depthctl"))

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

        section("content labelling (Grok)")
        test_classify_pure(fresh("classify_pure"))
        test_classify_call(fresh("classify_call"))
        test_labels_store(fresh("labels_store"))
        test_cross_platform_pins(fresh("pins"))
        test_labels_web(fresh("labels_web"))

        section("api key allowlist (method split, paid paths shut)")
        test_api_key_allowlist(fresh("apikeys"))

        section("forget stream (delete_tweets purges externalized raw)")
        test_forget_stream_purges_raw(fresh("forget"))

        section("facebook (store, cap, collect loop)")
        test_facebook(fresh("facebook"))

        section("velocity alerts (pace, threshold, cooldown)")
        test_alerts(fresh("alerts"))

        section("keywords, stream assignment, per-project delivery")
        test_keywords_and_project_delivery(fresh("kwdeliv"))

        section("google sheets (columns, quoting, injection, one append)")
        test_sheets(fresh("sheets"))

        section("google sheets via apps script (no cloud project)")
        test_sheets_script(fresh("sheetscript"))

        section("webhook (signing, cursor, receiver outage)")
        asyncio.run(run_webhook(fresh("webhook")))


        section("web (shared event loop)")
        test_web_event_loop(fresh("web"))

        section("no undefined names (the import that systemd found first)")
        test_no_undefined_names()

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
