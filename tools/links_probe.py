"""
links_probe.py — fetch ONE post by link the way a `links` watchlist does.

    .venv/bin/python3 tools/links_probe.py https://x.com/nasa/status/1789...
    .venv/bin/python3 tools/links_probe.py 1789000000000000001 --store

Spends exactly one TweetDetail request on the account pool (its own rate
bucket, not search's). Prints what the collector would record — outcome,
X's note if the post is gone, the counters, the serving account and the
remaining budget — and with --store writes the post through the real
upsert so you can see the row (and its frozen collected_ms) in results.db.

This is the live check X_LINKS_PLAN.md §9 step 3 asks for: run it once on
the server, on a post you can see in a browser, before trusting the loop.
"""

import argparse
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import auth
import links as L
import store as store_mod
from config import load_config
from engine import Engine


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("link", help="an x.com/twitter.com post URL, or the bare tweet id")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--store", action="store_true",
                    help="write the post into results.db through upsert_tweets "
                         "(one row, no watchlist, no hit edge)")
    args = ap.parse_args()

    tid = L.parse_status_url(args.link) or (int(args.link) if args.link.isdigit() else None)
    if not tid:
        print(f"[links] not an X post link or id: {args.link!r}")
        return 2

    cfg = load_config(args.config)
    api = auth.open_api(cfg.db_accounts)
    await auth.require_active(api)
    eng = Engine(api)

    t0 = time.time()
    det = await eng.tweet_detail(tid)
    took = time.time() - t0
    pg = det.page
    print(f"[links] {L.canonical_url(tid)}")
    print(f"        outcome   {det.outcome}" + (f"  — {det.note}" if det.note else ""))
    if pg is not None:
        print(f"        account   {pg.account}   status {pg.status}   {took:.1f}s")
        print(f"        budget    {pg.rl_remaining}/{pg.rl_limit} TweetDetail left, resets {pg.rl_reset}")
        print(f"        collected {pg.collected_ms}  (server clock)")
        if pg.parse_failures:
            print(f"        parse failures: {pg.parse_failures[:3]}")
    if det.tweet is not None:
        t = det.tweet
        rec = store_mod.normalize_tweet(t)
        print(f"        author    @{rec['author_username']}  followers {rec['author_followers']}")
        print(f"        posted    {rec['created_at']}")
        print(f"        counters  likes {rec['like_count']}  rt {rec['retweet_count']}  "
              f"replies {rec['reply_count']}  quotes {rec['quote_count']}  "
              f"views {rec['view_count']}  bookmarks {getattr(t, 'bookmarkedCount', None)}")
        print(f"        text      {(rec['text'] or '')[:120]!r}")
        print(f"        context   {len(pg.tweets) - 1} other tweet(s) in the payload, not stored")
        if args.store:
            st = store_mod.Store(cfg.db_results, cfg.defaults.keep_entry_json)
            await st.open()
            try:
                counts = await st.upsert_tweets([(t, pg, "result", det.entry)], None, None)
                row = st.db.execute("SELECT like_count, view_count, collected_ms, last_seen_at "
                                    "FROM tweets WHERE tweet_id = ?", (tid,)).fetchone()
                print(f"        stored    new={counts.new} dup={counts.dup}  row: {dict(row)}")
            finally:
                await st.close()
    return 0 if det.outcome != L.OUTCOME_TRANSIENT else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
