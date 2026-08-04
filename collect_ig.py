"""
collect_ig.py — the piece that was missing: run engine_ig over the configured
sources and SAVE what it finds into store_ig. This is what turns "engine_ig can
fetch" into "there is Instagram data to serve".

It reuses the exact freshness idea collector.py uses for X: walk newest-first
and STOP at the watermark (the newest post already stored), so a normal poll
reads one page and no post is fetched twice. IG media pks are time-monotonic, so
the same numeric "have I reached known ground" test works — no snowflake math.

CLI (the working model — simple on purpose):

  python3 collect_ig.py add-source --label natgeo --type user --value natgeo
  python3 collect_ig.py add-source --label home   --type following
  python3 collect_ig.py list-sources
  python3 collect_ig.py run                 # one pass over every enabled source
  python3 collect_ig.py run --loop --every 120

Sources are stored in ig_results.db (store_ig), so the API and a future
dashboard read the same list. The account that collects is the active Instagram
login unless a source names its own (--account).
"""

import argparse
import asyncio
import time

import ig
import ig_session
import store_ig
from engine_ig import IGEngine


def _active_account() -> str:
    """The Instagram handle to collect with: the first active one in the store."""
    with ig.Store("ig_accounts.db") as st:
        rows = [r for r in st.all() if r.get("active")]
    if not rows:
        raise RuntimeError("no active Instagram account — onboard one with "
                           "ig_login.py or ig_import.py first")
    return rows[0]["username"]


async def collect_source(engine, store, source, *, page_size=12, max_pages=5, log=print) -> int:
    """One pass over a source: collect posts newer than the watermark, save them."""
    wm = store.watermark(source.label)
    collected, newest, stop = [], None, False

    async for page in engine.pages_for(source, page_size=page_size, max_pages=max_pages):
        if newest is None and page.result_ids:
            newest = max(page.result_ids)
        for pk in page.result_ids:
            if wm and pk <= wm:          # reached known ground
                stop = True
                break
            rec = page.entries_by_id.get(pk)
            if rec:
                collected.append(rec)
        if stop:
            break

    new = store.upsert_posts(collected, source.label)
    if newest and (not wm or newest > wm):
        store.set_watermark(source.label, newest)
    log(f"  [{source.label}] type={source.type} value={source.value or '-'} "
        f"new={new} (had watermark={'yes' if wm else 'no'})")
    return new


async def run_once(store_path="ig_results.db", account_override="", log=print) -> int:
    with store_ig.Store(store_path) as store:
        sources = store.sources(only_enabled=True)
        if not sources:
            log("no enabled sources — add one with `collect_ig.py add-source`")
            return 0

        # Group sources by the account that collects them, so one client serves
        # all of that account's sources.
        by_account = {}
        for s in sources:
            acct = s.account or account_override or _active_account()
            by_account.setdefault(acct, []).append(s)

        total = 0
        for acct, group in by_account.items():
            log(f"account @{acct}: {len(group)} source(s)")
            try:
                cl = ig_session.load_client(acct, log=log)
            except Exception as e:
                log(f"  could not load @{acct}: {e}")
                continue
            engine = IGEngine(cl, account=acct)
            for s in group:
                try:
                    total += await collect_source(engine, store, s, log=log)
                except Exception as e:
                    log(f"  [{s.label}] error: {type(e).__name__}: {e}")
        log(f"done: {total} new post(s) stored")
        return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect Instagram posts into store_ig")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-source")
    a.add_argument("--label", required=True)
    a.add_argument("--type", required=True, choices=["following", "user", "hashtag"])
    a.add_argument("--value", default="")
    a.add_argument("--account", default="")

    sub.add_parser("list-sources")

    d = sub.add_parser("disable"); d.add_argument("label")
    e = sub.add_parser("enable");  e.add_argument("label")

    r = sub.add_parser("run")
    r.add_argument("--account", default="", help="override which IG login collects")
    r.add_argument("--loop", action="store_true")
    r.add_argument("--every", type=int, default=120, help="seconds between passes with --loop")

    args = ap.parse_args()
    store_path = "ig_results.db"

    if args.cmd == "add-source":
        if args.type == "user" and not args.value:
            print("--value is the username for a user source"); return 1
        if args.type == "hashtag" and not args.value:
            print("--value is the hashtag for a hashtag source"); return 1
        with store_ig.Store(store_path) as st:
            st.add_source(args.label, args.type, args.value, args.account)
        print(f"added source '{args.label}' ({args.type} {args.value})")
        return 0

    if args.cmd in ("enable", "disable"):
        with store_ig.Store(store_path) as st:
            st.set_enabled(args.label, args.cmd == "enable")
        print(f"{args.label}: {args.cmd}d")
        return 0

    if args.cmd == "list-sources":
        with store_ig.Store(store_path) as st:
            rows = st.sources(only_enabled=False)
            if not rows:
                print("no sources yet")
            for s in rows:
                print(f"  {s.label:16} {s.type:10} {s.value or '-':20} "
                      f"account={s.account or '(active)'}")
            print("stats:", st.stats())
        return 0

    if args.cmd == "run":
        if not args.loop:
            asyncio.run(run_once(store_path, args.account))
            return 0

        async def loop():
            while True:
                started = time.time()
                try:
                    await run_once(store_path, args.account)
                except Exception as e:
                    print(f"pass error: {type(e).__name__}: {e}")
                await asyncio.sleep(max(5, args.every - (time.time() - started)))
        try:
            asyncio.run(loop())
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
