"""
collect_fb.py — the Facebook poll loop (the twin of collect_ig.py).

For each enabled Facebook source: fetch the page's newest posts with the warm
browser engine, keep those newer than the watermark (id-dedup as backstop),
save to fb_results.db, advance the watermark. Gentle by design — Facebook pages
post a few times a day, so the default cadence is hours, not minutes, and the
engine's monthly byte cap is the hard ceiling.

Runs on the SERVER. CLI mirrors collect_ig:

  python3 collect_fb.py add-source narendramodi --project 2
  python3 collect_fb.py list
  python3 collect_fb.py run                 # one pass over all enabled sources
  python3 collect_fb.py run --loop --every 21600   # every 6h, as a service
"""

import argparse
import asyncio
import os
import random
import time

import store_fb


async def collect_source(engine, store, source, *, max_scroll=4, log=print) -> int:
    """One pass over a source. Returns how many new posts were saved."""
    posts = await engine.fetch_page(source["label"], max_scroll=max_scroll)
    if not posts:
        return 0
    wm = store.watermark(source["label"])
    new = 0
    newest_ms = wm or 0
    for p in posts:
        p["project_id"] = source.get("project_id")
        p["source_label"] = source["label"]
        # Watermark is best-effort on FB (created_ms often unknown from mobile):
        # id-dedup in store.upsert is the real guarantee that nothing doubles.
        if store.upsert(p):
            new += 1
        if p.get("created_ms"):
            newest_ms = max(newest_ms, p["created_ms"])
    store.db.commit()
    if newest_ms:
        store.set_watermark(source["label"], newest_ms)
    else:
        # No timestamps this run — still stamp last_run so the UI shows activity.
        store.db.execute("UPDATE sources SET last_run=? WHERE label=?",
                         (int(time.time()), source["label"]))
        store.db.commit()
    log(f"[fb] {source['label']}: +{new} new")
    return new


def _can_log_in() -> bool:
    """Any usable way in: saved session, raw cookies, or email+password."""
    if os.path.exists(os.getenv("FB_STATE_PATH", "fb_state.json")):
        return True
    if os.getenv("FB_C_USER") and os.getenv("FB_XS"):
        return True
    if os.getenv("FB_EMAIL") and os.getenv("FB_PASSWORD"):
        return True
    return False


async def run_once(store_path="fb_results.db", *, max_scroll=4,
                   project_id=None, log=print) -> int:
    from engine_fb import FacebookEngine

    st = store_fb.Store(store_path).open()
    try:
        sources = st.sources(enabled_only=True)
        if project_id:
            sources = [s for s in sources if s.get("project_id") == project_id]
        if not sources:
            log("[fb] no enabled sources — add a page first")
            return 0
        if not _can_log_in():
            log("[fb] no Facebook login available — set FB_C_USER/FB_XS or "
                "FB_EMAIL/FB_PASSWORD in .env")
            return 0
        total = 0
        async with FacebookEngine(log=log) as eng:
            for s in sources:
                try:
                    total += await collect_source(eng, st, s, max_scroll=max_scroll, log=log)
                except Exception as e:
                    log(f"[fb] {s['label']} error: {type(e).__name__}: {e}")
        return total
    finally:
        st.close()


async def run_due(store_path="fb_results.db", *, default_interval=21600,
                  max_scroll=4, log=print) -> int:
    """
    One scheduler tick: collect only the pages whose own interval has elapsed
    since their last run (falling back to the default). This is what the --loop
    service calls, so each page keeps its OWN cadence — the Facebook twin of the
    per-watchlist "check every" on the X side. Opens the browser only when at
    least one page is actually due, so idle ticks cost nothing.
    """
    from engine_fb import FacebookEngine

    st = store_fb.Store(store_path).open()
    try:
        now = int(time.time())
        due = [s for s in st.sources(enabled_only=True)
               if now - (s.get("last_run") or 0) >= (s.get("interval_s") or default_interval)]
        if not due:
            return 0
        if not _can_log_in():
            log("[fb] no Facebook login available — set FB_C_USER/FB_XS or "
                "FB_EMAIL/FB_PASSWORD in .env")
            return 0
        log(f"[fb] {len(due)} page(s) due")
        total = 0
        async with FacebookEngine(log=log) as eng:
            for s in due:
                try:
                    total += await collect_source(eng, st, s, max_scroll=max_scroll, log=log)
                except Exception as e:
                    log(f"[fb] {s['label']} error: {type(e).__name__}: {e}")
        return total
    finally:
        st.close()


async def run_favorites(store_path="fb_results.db", *, max_scroll=6, log=print) -> int:
    """
    Collect from the account's FAVORITES feed in ONE pass, then attribute each
    post to whichever tracked page (across all projects) it came from. Pages the
    account has favorited but that no project tracks are ignored; a post whose
    page two projects both track is stored once under the first match. This is
    the efficient, rich-data path — one feed read instead of N page visits.
    """
    from engine_fb import FacebookEngine

    st = store_fb.Store(store_path).open()
    try:
        srcs = st.sources(enabled_only=True)
        if not srcs:
            log("[fb] no enabled pages to attribute favorites to — add pages first")
            return 0
        if not _can_log_in():
            log("[fb] no Facebook login available — set FB_EMAIL/FB_PASSWORD in .env")
            return 0
        by_handle = {s["label"].lower(): s for s in srcs}
        total = matched = 0
        async with FacebookEngine(log=log) as eng:
            posts = await eng.fetch_favorites(max_scroll=max_scroll)
        for p in posts:
            s = by_handle.get(str(p.get("page") or "").lower())
            if not s:
                continue                       # a favorited page no project tracks
            matched += 1
            p["project_id"] = s["project_id"]
            p["source_label"] = s["label"]
            if st.upsert(p):
                total += 1
        st.db.commit()
        log(f"[fb] favorites: {matched} posts matched tracked pages, +{total} new")
        return total
    finally:
        st.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect Facebook page posts")
    ap.add_argument("--store", default="fb_results.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-source")
    a.add_argument("label", help="the page handle, e.g. narendramodi")
    a.add_argument("--project", type=int, default=0)

    sub.add_parser("list")
    r = sub.add_parser("remove"); r.add_argument("label")

    rn = sub.add_parser("run")
    rn.add_argument("--loop", action="store_true")
    rn.add_argument("--every", type=int, default=int(os.getenv("FB_INTERVAL_S", "21600")),
                    help="default cadence for pages with no per-page interval")
    rn.add_argument("--tick", type=int, default=int(os.getenv("FB_TICK_S", "300")),
                    help="how often the loop re-checks which pages are due")
    rn.add_argument("--scroll", type=int, default=4)

    i = sub.add_parser("interval")
    i.add_argument("label")
    i.add_argument("seconds", type=int, help="0 clears the per-page override")

    sub.add_parser("favorites")   # one pass over the account's Favorites feed

    args = ap.parse_args()

    if args.cmd == "add-source":
        with store_fb.Store(args.store) as st:
            st.add_source(args.label, project_id=args.project)
        print(f"added source {args.label} (project {args.project})")
        return 0
    if args.cmd == "list":
        with store_fb.Store(args.store) as st:
            for s in st.sources():
                print(f"  {s['label']:24s} project={s['project_id']} "
                      f"enabled={s['enabled']} posts={s['posts']}")
        return 0
    if args.cmd == "remove":
        with store_fb.Store(args.store) as st:
            st.remove_source(args.label)
        print(f"removed {args.label}")
        return 0
    if args.cmd == "interval":
        with store_fb.Store(args.store) as st:
            st.set_interval(args.label, args.seconds)
        print(f"{args.label}: check every "
              f"{args.seconds}s" if args.seconds else f"{args.label}: interval cleared")
        return 0
    if args.cmd == "favorites":
        n = asyncio.run(run_favorites(args.store))
        print(f"[fb] favorites pass complete: {n} new")
        return 0
    if args.cmd == "run":
        # FB_MODE=favorites → read the account's one Favorites feed each cycle
        # instead of visiting each page (efficient + richer data).
        favorites_mode = os.getenv("FB_MODE", "pages").lower() == "favorites"
        if not args.loop:
            if favorites_mode:
                n = asyncio.run(run_favorites(args.store))
            else:
                # One-shot: collect everything enabled now (ignores per-page
                # cadence) — what the dashboard "Fetch now" button wants too.
                n = asyncio.run(run_once(args.store, max_scroll=args.scroll))
            print(f"[fb] pass complete: {n} new")
            return 0

        # Favorites mode reads the one feed on its own interval (default hourly);
        # per-page mode wakes on the short tick and collects only what's due.
        fav_every = int(os.getenv("FB_FAV_INTERVAL_S", "3600"))

        async def loop():
            while True:
                if favorites_mode:
                    await run_favorites(args.store)
                    base = fav_every
                else:
                    await run_due(args.store, default_interval=args.every,
                                  max_scroll=args.scroll)
                    base = max(60, args.tick)
                # Jitter the wait ±25% so the rhythm isn't a robotic fixed clock.
                await asyncio.sleep(int(base * random.uniform(0.75, 1.25)))
        asyncio.run(loop())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
