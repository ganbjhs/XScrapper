"""
main.py — entry point and every subcommand.

    python3 main.py login   --all               # browser login, once per account
    python3 main.py watch   --all               # poll streams for new tweets
    python3 main.py search  --query '...'       # one-shot advanced search
    python3 main.py export  --format csv        # write collected tweets out
    python3 main.py doctor                      # accounts, streams, health

The prototype's original invocation still works unchanged:

    python3 main.py --query 'from:nasa min_faves:500' --limit 100 --tab Latest

A leading flag is routed to the `search` subcommand, so existing scripts keep
working.

Auth is deliberately NOT automatic. The prototype logged in as a side effect of
searching, which is how an expired cookie surfaced as a confusing search error
several steps later. Run `login` explicitly; `search` and `watch` only ever
report that a session is missing.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from contextlib import aclosing

# twscrape accumulates PostHog telemetry in-process and calls machineid to
# fingerprint the host, both at import time. These have to be set BEFORE
# anything pulls twscrape in, which is why the project imports sit below rather
# than at the top of the file.
os.environ.setdefault("TWS_TELEMETRY", "0")
os.environ.setdefault("DO_NOT_TRACK", "1")

import auth  # noqa: E402
import store as store_mod  # noqa: E402
from collector import Collector  # noqa: E402
from config import CLI, ConfigError, load_config  # noqa: E402
# `check` is the twscrape-compatibility self-test; aliased because `engine` is
# used as a local variable name below.
from engine import Engine, check as twscrape_compat_check  # noqa: E402
from store import normalize_tweet  # noqa: E402


# ==========================================================================
# commands: login, doctor
# ==========================================================================

EXIT_OK = 0
EXIT_AUTH = 2
EXIT_SEARCH = 3
EXIT_CONFIG = 4
EXIT_STORE = 5
EXIT_NO_ACCOUNT = 6


def _log(msg=""):
    print(msg, flush=True)


def _resolve_accounts(cfg, labels, want_all):
    if want_all or not labels:
        accounts = cfg.enabled_accounts()
        if not accounts:
            raise ConfigError("No enabled accounts in config.")
        return accounts
    return [cfg.account(x) for x in labels]


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------

async def cmd_login(args) -> int:
    cfg = load_config(args.config)
    accounts = _resolve_accounts(cfg, args.account, args.all)

    if args.debug_detect:
        for acct in accounts:
            _log(f"[{acct.label}] profile={acct.profile_path}")
            await auth.debug_detect(acct, headless=args.headless, log=_log)
            _log("")
        return EXIT_OK

    api = auth.open_api(cfg.db_accounts)

    # Clearing an account's locks while a collector holds it would let two
    # pollers share one account.
    watcher = auth.read_watcher_pid(cfg.root)
    if watcher:
        _log(f"[warn] a watcher is running (pid {watcher}); leaving account locks alone")

    ok_n = fail_n = skip_n = 0
    for acct in accounts:
        _log(f"[{acct.label}] profile={acct.profile_path}")

        known = auth.read_identity(acct)
        if known and not args.force:
            existing = await api.pool.get_account(known)
            if existing is not None and existing.active:
                res = await auth.validate_http(existing, proxy=acct.proxy_or_none)
                if res.ok:
                    _log(f"  already live as @{known} (validated via {res.probe}) — skipping")
                    skip_n += 1
                    continue
                _log(f"  stored session for @{known} is stale ({res.error[:80]}); refreshing")

        # Headless first when the profile is already trusted: X remembers the
        # device, so the refresh is silent and no human is needed.
        headless = args.headless or (args.refresh_only and not args.headed)
        try:
            harvest = await auth.harvest_session(
                acct,
                headless=headless,
                refresh_only=args.refresh_only,
                timeout=args.timeout,
                log=_log,
            )
        except auth.LoginError as e:
            if headless and not args.refresh_only and not args.headless:
                _log(f"  headless refresh failed ({e}); retrying with a visible window")
                try:
                    harvest = await auth.harvest_session(
                        acct, headless=False, refresh_only=False,
                        timeout=args.timeout, log=_log,
                    )
                except auth.LoginError as e2:
                    _log(f"  [FAIL] {e2}")
                    fail_n += 1
                    continue
            else:
                _log(f"  [FAIL] {e}")
                fail_n += 1
                continue

        username, res = await auth.upsert_session(
            api, harvest, acct, clear_locks=(watcher is None)
        )
        if res.ok:
            auth.write_identity(acct, username)
            _log(f"  [OK] @{username} active (validated out-of-browser via {res.probe})")
            ok_n += 1
        else:
            _log(f"  [FAIL] @{username} not activated: {res.error[:200]}")
            fail_n += 1

    _log("-" * 60)
    _log(f"[login] ok={ok_n} skipped={skip_n} failed={fail_n}")

    if args.verify_search and ok_n + skip_n:
        _log("[verify] running one real search to exercise the full request path...")
        rc = await _verify_search(cfg, api)
        if rc:
            return rc
    return EXIT_OK if fail_n == 0 else EXIT_AUTH


async def _verify_search(cfg, api) -> int:
    """Tier-2 validation: one real SearchTimeline page. Costs 1 of ~50."""
    eng = Engine(api)
    try:
        async with aclosing(eng.search_pages("the", tab="Latest", max_pages=1)) as gen:
            async for page in gen:
                _log(
                    f"  [verify] {len(page.result_ids)} results via @{page.account} "
                    f"| rate-limit {page.rl_remaining}/{page.rl_limit}"
                )
                return EXIT_OK
        _log("  [verify][FAIL] no page returned — pool starved or request aborted")
        return EXIT_NO_ACCOUNT
    except Exception as e:
        _log(f"  [verify][FAIL] {type(e).__name__}: {e}")
        return EXIT_SEARCH


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

async def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    show_all = not any([args.accounts, args.selftest, args.lag, args.streams])

    rc = EXIT_OK

    if args.selftest or show_all:
        _log("== twscrape compatibility ==")
        report = twscrape_compat_check()
        for line in report.lines:
            _log(f"  {line}")
        if not report.ok:
            _log("  -> A twscrape upgrade changed something this project depends on.")
            rc = EXIT_CONFIG
        _log("")

    if args.accounts or show_all:
        api = auth.open_api(cfg.db_accounts)
        rows = await auth.health(api, cfg)
        _log(f"== accounts ({cfg.db_accounts}) ==")
        if not rows:
            _log(f"  (none) — run: {CLI} login --all")
            rc = rc or EXIT_NO_ACCOUNT
        for r in rows:
            flags = []
            if not r.has_cookies:
                flags.append("NO-COOKIES")
            if not r.real_user_agent:
                flags.append("PLACEHOLDER-UA")
            if not r.has_known_device:
                flags.append("no-kdt")
            if r.locked_queues:
                flags.append("locked:" + ",".join(r.locked_queues))
            if r.proxy:
                flags.append("proxy")
            _log(
                f"  [{'ACTIVE' if r.active else '  DEAD'}] @{r.username} "
                f"({r.label})  req={r.total_req}  last={r.last_used or 'never'}"
                + (f"  {' '.join(flags)}" if flags else "")
            )
            if r.error_msg:
                _log(f"           error: {r.error_msg[:150]}")
        if rows and not any(r.active for r in rows):
            _log(f"  -> No active account. Run: {CLI} login --all --refresh-only")
            rc = rc or EXIT_NO_ACCOUNT
        _log("")

    if args.probe:
        api = auth.open_api(cfg.db_accounts)
        _log("== validation probe ==")
        accs = await api.pool.get_all()
        if not accs:
            _log("  (no accounts to probe with)")
        for acc in accs:
            res = await auth.validate_http(acc)
            verdict = "OK" if res.ok else "FAIL"
            _log(f"  [{verdict}] @{acc.username} via {res.probe} (HTTP {res.status})")
            if not res.ok:
                _log(f"         {res.error[:180]}")
        _log("")

    if args.streams or show_all:
        _log("== streams ==")
        if not cfg.streams:
            _log("  (none declared in config.toml)")
        for s in cfg.streams:
            mode = "watermark" if s.watermark else "sweep"
            _log(
                f"  [{'on ' if s.enabled else 'off'}] {s.label}  tab={s.tab} {mode}  "
                f"interval={s.min_interval_s:g}-{s.max_interval_s:g}s  "
                f"pages<={s.max_pages_per_poll}"
            )
            if s.list_id:
                _log(f"        list:  https://x.com/i/lists/{s.list_id}")
            else:
                _log(f"        query: {s.query}")
        _log("")

    if args.lag:
        st = store_mod.Store(cfg.db_results)
        await st.open()
        try:
            _log(f"== lag ({args.since}) ==")
            for line in await st.lag_report(args.since):
                _log("  " + line)
        finally:
            await st.close()
        _log("")

    return rc


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

SUBCOMMANDS = ("login", "doctor", "search", "export", "watch")


def build_parser():
    p = argparse.ArgumentParser(
        prog="main.py",
        description="X (Twitter) freshness-first scraper — no official API.",
    )
    p.add_argument("--config", help="Path to config.toml (default: ./config.toml)")
    sub = p.add_subparsers(dest="cmd")

    # --- login ---
    lg = sub.add_parser("login", help="Log accounts in via a real browser and store the session.")
    lg.add_argument("--account", action="append", default=[], metavar="LABEL",
                    help="Account label from config.toml (repeatable). Default: all enabled.")
    lg.add_argument("--all", action="store_true", help="All enabled accounts.")
    lg.add_argument("--headless", action="store_true",
                    help="Never show a window. Fails if a human is needed.")
    lg.add_argument("--headed", action="store_true",
                    help="Always show a window, even when refreshing.")
    lg.add_argument("--refresh-only", action="store_true",
                    help="Refresh an already-trusted profile; never attempt an interactive login.")
    lg.add_argument("--force", action="store_true",
                    help="Re-harvest even if the stored session already validates.")
    lg.add_argument("--timeout", type=float, default=300.0,
                    help="Seconds to wait for a human to clear captcha/2FA (default 300).")
    lg.add_argument("--verify-search", action="store_true",
                    help="After login, run one real search to exercise the whole path.")
    lg.add_argument("--debug-detect", action="store_true",
                    help="Open the profile and dump exactly what login detection sees, "
                         "then exit. Use when the browser shows you logged in but the "
                         "script disagrees.")
    lg.set_defaults(func=cmd_login)

    # --- doctor ---
    dr = sub.add_parser("doctor", help="Health of accounts, streams, and twscrape compatibility.")
    dr.add_argument("--accounts", action="store_true", help="Account/session status only.")
    dr.add_argument("--streams", action="store_true", help="Configured streams only.")
    dr.add_argument("--selftest", action="store_true",
                    help="Assert the twscrape internals this project depends on.")
    dr.add_argument("--probe", action="store_true",
                    help="Hit each validation endpoint and report what it returns today.")
    dr.add_argument("--lag", action="store_true", help="Lag percentiles from the results store.")
    dr.add_argument("--since", default="24h", help="Window for --lag (e.g. 6h, 24h, 7d).")
    dr.set_defaults(func=cmd_doctor)

    # --- guard ---
    gd = sub.add_parser("guard", help="Risk check: what is unsafe right now, and why.")
    gd.add_argument("--action", default="", choices=["", "fetch", "watch", "login", "search"],
                    help="Check against a specific action you are about to take.")
    gd.add_argument("--cost", type=int, default=0,
                    help="Requests that action will spend (1 per search page).")
    gd.add_argument("--queue", default="search", choices=["search", "list"],
                    help="Which rate-limit budget to check. Lists have their own, "
                         "much larger one (500/15min vs 50).")
    gd.add_argument("--json", action="store_true", help="Machine-readable output.")
    gd.set_defaults(func=cmd_guard)

    # --- serve ---
    sv = sub.add_parser("serve", help="Local web dashboard for the collected data.")
    sv.add_argument("--port", type=int, default=8765, help="Port (default 8765).")
    sv.add_argument("--behind-proxy", action="store_true",
                    help="Trust X-Forwarded-For. Set this ONLY when nginx/Caddy "
                         "sits in front, otherwise clients can spoof their IP "
                         "and defeat login lockout.")
    sv.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Defaults to localhost — this UI has no auth "
                         "and can spend rate-limit budget, so do not expose it.")
    sv.set_defaults(func=cmd_serve)

    _add_search(sub)
    _add_export(sub)
    _add_watch(sub)
    return p


async def cmd_guard(args) -> int:
    import guard

    cfg = load_config(args.config)
    if args.json:
        _log(json.dumps(guard.assess(cfg, args.action, args.cost,
                                     queue=args.queue).to_json(), indent=2))
        return EXIT_OK
    return guard.report(cfg, args.action, args.cost, log=_log, queue=args.queue)


async def cmd_serve(args) -> int:
    import web

    cfg = load_config(args.config)
    if not cfg.db_results.exists():
        _log(f"[serve] note: {cfg.db_results} does not exist yet — the dashboard will be "
             f"empty until you run `{CLI} watch --once`.")
    return web.serve(cfg, host=args.host, port=args.port, log=_log,
                     behind_proxy=args.behind_proxy)


def _add_search(sub):
    sp = sub.add_parser("search", help="One-shot advanced search.")
    sp.add_argument("--list", dest="list_id", default="",
                    help="Poll an X List instead of a search query. Accepts a bare "
                         "id or the full https://x.com/i/lists/... URL. Runs on a "
                         "SEPARATE rate-limit queue from search.")
    sp.add_argument("--query", default="",
                    help="X advanced-search query, e.g. 'from:nasa since:2026-01-01 min_faves:500'")
    sp.add_argument("--limit", type=int, default=50, help="Max tweets to fetch (default 50).")
    sp.add_argument("--tab", default="Latest", choices=["Latest", "Top", "Media"],
                    help="Search product tab (default Latest).")
    sp.add_argument("--out", default="results", help="Output file prefix (default 'results').")
    sp.add_argument("--db", help="Override the session/account db path.")
    sp.add_argument("--store", dest="store", action="store_true", default=True,
                    help="Also upsert into the results store (default).")
    sp.add_argument("--no-store", dest="store", action="store_false",
                    help="Write files only, exactly like the original prototype.")
    sp.add_argument("--debug-pages", action="store_true",
                    help="Print per-page diagnostics: ids, cursor, account, rate limit.")
    sp.add_argument("--cursor", help="Resume pagination from a saved cursor.")
    sp.set_defaults(func=cmd_search)


def _add_export(sub):
    ep = sub.add_parser("export", help="Write collected tweets out.")
    ep.add_argument("--stream", help="Limit to one stream label.")
    ep.add_argument("--since", help="ISO timestamp or relative window (e.g. 6h, 3d).")
    ep.add_argument("--until", help="ISO timestamp.")
    ep.add_argument("--format", default="csv", choices=["json", "csv", "jsonl", "raw"],
                    help="Output format (default csv).")
    ep.add_argument("--out", default="export", help="Output path prefix (default 'export').")
    ep.add_argument("--fields", default="default", choices=["default", "all"],
                    help="'all' adds collection metadata (lag, source, stream).")
    ep.add_argument("--include-embedded", action="store_true",
                    help="Include quoted/parent tweets that were not themselves search hits.")
    ep.add_argument("--limit", type=int, help="Max rows.")
    ep.add_argument("--order", default="desc", choices=["asc", "desc"])
    ep.set_defaults(func=cmd_export)


def _add_watch(sub):
    wp = sub.add_parser("watch", help="Poll streams continuously for new tweets.")
    wp.add_argument("--stream", action="append", default=[], metavar="LABEL",
                    help="Stream label from config.toml (repeatable). Default: all enabled.")
    wp.add_argument("--all", action="store_true", help="All enabled streams.")
    wp.add_argument("--once", action="store_true", help="One poll per stream, then exit.")
    wp.add_argument("--duration", type=float, help="Stop after N seconds.")
    wp.add_argument("--max-concurrency", type=int, default=0,
                    help="Max concurrent polls (default: number of active accounts).")
    wp.add_argument("--page-size", type=int, help="Override tweets per page.")
    wp.add_argument("--max-pages", type=int, help="Override max pages per poll.")
    wp.add_argument("--min-interval", type=float, help="Override the interval floor (seconds).")
    wp.add_argument("--max-interval", type=float, help="Override the interval ceiling (seconds).")
    wp.add_argument("--overlap-ms", type=int,
                    help="How far past the watermark to re-read each poll.")
    wp.set_defaults(func=cmd_watch)


def normalize_argv(argv):
    """
    Preserve the prototype's invocation.

    `python3 main.py --query '...' --limit 100` predates subcommands, so a
    leading flag is rewritten to the `search` subcommand rather than erroring.
    """
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        return ["search"] + list(argv)
    return list(argv)


def main(argv=None) -> int:
    argv = normalize_argv(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_CONFIG

    try:
        return asyncio.run(args.func(args))
    except ConfigError as e:
        print(f"[config][ERROR] {e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130

# ==========================================================================
# commands: search, export, watch
# ==========================================================================

async def _ready(cfg, db_override=None):
    """Open the pool and assert something can actually serve requests."""
    api = auth.open_api(db_override or cfg.db_accounts)
    await auth.require_active(api)
    return api


# --------------------------------------------------------------------------
# search — one-shot, the prototype's original behaviour
# --------------------------------------------------------------------------

async def cmd_search(args) -> int:
    from config import _parse_list_id

    cfg = load_config(args.config)

    # One source, chosen explicitly. Same rule as config.toml streams.
    # getattr, not attribute access: the legacy `main.py --query ...` shim and
    # the test harness both build args objects that predate --list.
    raw_list = getattr(args, "list_id", "") or ""
    query_arg = getattr(args, "query", "") or ""
    if raw_list and query_arg:
        print("[search][ERROR] pass --list or --query, not both — a search has one source.")
        return EXIT_CONFIG
    if not raw_list and not query_arg:
        print("[search][ERROR] need --query 'from:nasa ...' or --list <id|url>.")
        return EXIT_CONFIG
    try:
        list_id_arg = _parse_list_id(raw_list, "search")
    except ConfigError as e:
        print(f"[search][ERROR] {e}")
        return EXIT_CONFIG

    try:
        api = await _ready(cfg, args.db)
    except RuntimeError as e:
        print(f"[auth][ERROR] {e}")
        return EXIT_NO_ACCOUNT

    engine = Engine(api)

    # A one-shot search reuses the collector's stream shape so that pages_for
    # makes the search-vs-list choice in exactly one place.
    class _Src:
        query = args.query
        list_id = list_id_arg

    if list_id_arg:
        _log(f"[search] list={list_id_arg}  limit={args.limit}  "
             f"(ListLatestTweetsTimeline — separate rate-limit queue)")
    else:
        _log(f"[search] query={args.query!r}  tab={args.tab}  limit={args.limit}")

    started = time.time()
    tweets, pages_seen = [], 0
    seen: set[int] = set()
    raw_rows = []

    try:
        async with aclosing(
            engine.pages_for(_Src(), tab=args.tab, limit=args.limit, cursor=args.cursor)
        ) as pages:
            async for page in pages:
                pages_seen += 1
                if args.debug_pages:
                    _log(
                        f"  page {page.page_no}: results={len(page.result_ids)} "
                        f"embedded={len(page.embedded_ids)} orphans={len(page.orphan_ids)} "
                        f"ids={page.min_result_id()}..{page.max_result_id()} "
                        f"acct=@{page.account} rl={page.rl_remaining}/{page.rl_limit} "
                        f"cursor={(page.cursor or '')[:24]}..."
                    )
                    if page.parse_failures:
                        _log(f"    parse failures: {page.parse_failures[:3]}")

                for tid in page.result_ids:
                    tweet = page.tweets.get(tid)
                    if tweet is None or tid in seen:
                        continue
                    seen.add(tid)
                    tweets.append(tweet)
                    raw_rows.append((tweet, page, "result", page.entries_by_id.get(tid)))
                for tid in page.embedded_ids:
                    raw_rows.append((page.tweets[tid], page, "embedded", None))

                if 0 < args.limit <= len(tweets):
                    break
    except Exception as e:
        print(f"[search][ERROR] {type(e).__name__}: {e}")
        print(f"  If this looks like rate-limiting or auth, run: {CLI} doctor --accounts")
        return EXIT_SEARCH

    elapsed = time.time() - started
    tweets = tweets[: args.limit] if args.limit > 0 else tweets

    if pages_seen == 0:
        # Same trap as in the poller: twscrape's generator ends silently when
        # no account can be acquired, which looks exactly like "no results".
        print("[search][ERROR] no pages returned — the account pool was starved or the request aborted.")
        print(f"  Run: {CLI} doctor --accounts")
        return EXIT_NO_ACCOUNT

    stored = None
    if args.store:
        st = store_mod.Store(cfg.db_results)
        await st.open()
        try:
            sid = await st.ensure_stream(
                f"oneshot:{list_id_arg or args.query}"[:120], args.query,
                args.tab, watermarked=False, list_id=list_id_arg
            )
            poll_id = await st.begin_poll(sid, kind="oneshot")
            stored = await st.upsert_tweets(raw_rows, sid, poll_id)
            await st.finish_poll(
                poll_id, pages=pages_seen, results=len(tweets),
                new_tweets=stored.new, dup_tweets=stored.dup, stop_reason="oneshot",
            )
        finally:
            await st.close()

    records = [normalize_tweet(t) for t in tweets]
    json_path, csv_path, raw_path = store_mod.write_legacy_outputs(records, tweets, args.out)

    _log("-" * 60)
    _log(f"[done] tweets={len(tweets)}  pages={pages_seen}  time={elapsed:.1f}s")
    if stored is not None:
        _log(f"[store] new={stored.new}  already-had={stored.dup}  context={stored.embedded}"
             f"  -> {cfg.db_results}")
    for p in (json_path, csv_path, raw_path):
        _log(f"[out]  {p}")
    if not tweets:
        _log("[note] 0 results — check the query, the tab, or account health (doctor --accounts).")
    return EXIT_OK


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

async def cmd_export(args) -> int:
    cfg = load_config(args.config)
    if not cfg.db_results.exists():
        print(f"[export][ERROR] no results database at {cfg.db_results}")
        print(f"  Collect something first: {CLI} watch --once")
        return EXIT_CONFIG

    st = store_mod.Store(cfg.db_results)
    await st.open()
    try:
        since = store_mod.parse_window(args.since)
        until = store_mod.parse_window(args.until)
        rows = st.iter_export(
            stream=args.stream, since=since, until=until, limit=args.limit,
            order=args.order, include_embedded=args.include_embedded,
        )
        path, n = store_mod.export(rows, args.out, args.format, args.fields)
    except ValueError as e:
        print(f"[export][ERROR] {e}")
        return EXIT_CONFIG
    finally:
        await st.close()

    _log(f"[export] {n} rows -> {path}")
    if n == 0:
        _log("[note] nothing matched. Check --stream/--since, or run `doctor --lag`.")
    return EXIT_OK


# --------------------------------------------------------------------------
# watch
# --------------------------------------------------------------------------

def _apply_overrides(stream, args):
    for attr, val in (
        ("page_size", args.page_size),
        ("max_pages_per_poll", args.max_pages),
        ("min_interval_s", args.min_interval),
        ("max_interval_s", args.max_interval),
        ("overlap_ms", args.overlap_ms),
    ):
        if val is not None:
            setattr(stream, attr, val)
    return stream


async def cmd_watch(args) -> int:
    cfg = load_config(args.config)

    streams = (
        cfg.enabled_streams()
        if (args.all or not args.stream)
        else [cfg.stream(x) for x in args.stream]
    )
    if not streams:
        raise ConfigError(
            "No streams to watch. Declare [[streams]] in config.toml "
            "(see config.toml.example)."
        )
    streams = [_apply_overrides(s, args) for s in streams]

    try:
        api = await _ready(cfg)
    except RuntimeError as e:
        print(f"[auth][ERROR] {e}")
        return EXIT_NO_ACCOUNT

    # A long-running collector is exactly where a silent risk compounds, so
    # surface everything once at startup rather than letting it accumulate.
    import guard

    v = guard.assess(cfg, action="watch")
    if v.blocked:
        _log("[guard] refusing to start:")
        for f in v.blocks:
            _log(f"  {f.line()}\n         {f.detail}\n         -> {f.remedy}")
        return EXIT_CONFIG
    for f in v.warnings:
        _log(f"[guard] {f.title}")
        if f.remedy:
            _log(f"        -> {f.remedy}")
    if v.warnings:
        _log("")

    active = await auth.active_usernames(api)
    concurrency = args.max_concurrency or max(1, len(active))

    st = store_mod.Store(cfg.db_results)
    await st.open()
    lock = cfg.root / auth.WATCHER_LOCKFILE
    lock.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))

    collector = Collector(
        Engine(api), st, streams, max_concurrency=concurrency, log=_log
    )
    await collector.prepare()

    _log(f"[watch] {len(streams)} stream(s), {len(active)} account(s), "
         f"concurrency={concurrency}")
    for s in streams:
        mode = "watermark" if s.watermark else "sweep"
        src = f"list:{s.list_id}" if s.list_id else s.query
        _log(f"  - {s.label} ({s.tab}, {mode}): {src}")
    _log("")

    stop = asyncio.Event()

    def _request_stop(*_):
        if not stop.is_set():
            _log("\n[watch] stopping — letting in-flight polls finish so accounts unlock cleanly")
            stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            signal.signal(sig, _request_stop)  # Windows

    try:
        if args.once:
            results = await collector.run_once()
            starved = [r for r in results if r.starved]
            if starved:
                _log("")
                _log("[watch] some polls got no account at all. That is pool starvation, "
                     "not an empty stream.")
                return EXIT_NO_ACCOUNT
            return EXIT_OK

        runner = asyncio.create_task(collector.run_forever(duration=args.duration))
        stopper = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if runner in done and runner.exception():
            raise runner.exception()
        return EXIT_OK
    finally:
        await st.close()
        lock.unlink(missing_ok=True)
        total = "?"
        try:
            st2 = store_mod.Store(cfg.db_results)
            await st2.open()
            total = await st2.count_tweets()
            await st2.close()
        except Exception:
            pass
        _log(f"[watch] stopped. {total} tweets in {cfg.db_results}")

if __name__ == "__main__":
    sys.exit(main())
