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

import activity_log
import store_fb


def _persist_log(log=None):
    """
    The collectors' default logger: still prints (so journalctl keeps working)
    AND persists every line to the account-activity log, which is what the
    dashboard's Account Log section reads. A caller that passes its own log
    keeps it untouched.
    """
    if log is not None and log is not print:
        return log
    return activity_log.logger(
        "facebook", account=(os.getenv("FB_EMAIL", "").strip() or None))


def _cache_avatar(engine, store, handle, posts, log=print):
    """
    Keep the page's profile picture in the page_profiles cache. FREE sources
    only — never a dedicated visit just for a picture (RULEBOOK §6; X is the
    canonical avatar source and the dashboard backfills from it at read time):
      1. a collected post that carried the avatar (also refreshes the cache),
      2. the avatar harvested from the page render we already did.
    Returns the avatar URL (cached or fresh) so posts can be backfilled.
    """
    handle = str(handle).lower()
    fresh = next((p.get("author_avatar") for p in posts
                  if p.get("author_avatar")), None)
    fresh = fresh or getattr(engine, "page_avatars", {}).get(handle)
    if fresh:
        store.set_profile(handle, avatar_url=fresh)
        return fresh
    cached = store.profile_avatar(handle)
    if cached:
        return cached
    return None


async def collect_source(engine, store, source, *, max_scroll=4, log=print) -> int:
    """One pass over a source. Returns how many new posts were saved."""
    posts = await engine.fetch_page(source["label"], max_scroll=max_scroll)
    # Avatar cache: posts / this render / DB — and because fetch_page just
    # rendered the profile itself, a dedicated visit is never needed here.
    avatar = _cache_avatar(engine, store, source["label"], posts, log=log)
    if not posts:
        return 0
    wm = store.watermark(source["label"])
    new = 0
    newest_ms = wm or 0
    for p in posts:
        p["project_id"] = source.get("project_id")
        p["source_label"] = source["label"]
        if avatar and not p.get("author_avatar"):
            p["author_avatar"] = avatar
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
    from engine_fb import FacebookEngine, login_blocked

    log = _persist_log(log)

    st = store_fb.Store(store_path).open()
    try:
        if st.setting("fb_paused") == "1":
            log("[fb] collection is PAUSED from the dashboard — run skipped "
                "(resume it in Watchlists → Facebook pages)")
            return 0
        blk = login_blocked()
        if blk:
            log(f"[fb] run skipped — login blocked ({blk.get('reason')}), "
                f"waiting for a human. Fix it, then press \"Clear & retry\" "
                f"in the dashboard.")
            return 0
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
    from engine_fb import FacebookEngine, login_blocked

    log = _persist_log(log)
    st = store_fb.Store(store_path).open()
    try:
        # Paused or login-blocked: skip WITHOUT logging on every tick — a
        # 5-minute heartbeat of "still skipped" is noise, and the state that
        # explains the silence is on the dashboard already.
        if st.setting("fb_paused") == "1":
            return 0
        now = int(time.time())
        due = [s for s in st.sources(enabled_only=True)
               if now - (s.get("last_run") or 0) >= (s.get("interval_s") or default_interval)]
        if due and login_blocked():
            return 0
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


async def run_favorites(store_path="fb_results.db", *, project_id=None,
                        max_scroll=6, log=print) -> int:
    """
    Collect the account's FAVORITES feed in ONE pass. Each post is attributed to
    its own author page. If that page is already tracked, its project is used;
    otherwise, when a project_id is given (the dashboard button passes the one
    you clicked from), the page is auto-registered under that project and the
    post is saved. So whatever you favorite just flows in — no hand-matching. A
    page you don't want is a Pause/Remove away in the dashboard.
    """
    from engine_fb import FacebookEngine, login_blocked

    log = _persist_log(log)
    st = store_fb.Store(store_path).open()
    try:
        if st.setting("fb_paused") == "1":
            log("[fb] collection is PAUSED from the dashboard — favorites "
                "pass skipped")
            return 0
        if login_blocked():
            log("[fb] favorites pass skipped — login blocked, waiting for a "
                "human (see the dashboard's Facebook panel)")
            return 0
        if not _can_log_in():
            log("[fb] no Facebook login available — set FB_EMAIL/FB_PASSWORD in .env")
            return 0
        by_handle = {s["label"].lower(): s for s in st.sources()}
        total = matched = added = 0
        async with FacebookEngine(log=log) as eng:
            posts = await eng.fetch_favorites(max_scroll=max_scroll)
            on_fav = getattr(eng, "on_favorites", False)
            # Avatar cache upkeep — FREE sources only: posts that carried the
            # avatar. NO dedicated profile visits for pictures (RULEBOOK §6):
            # X is the canonical avatar source — a public figure uses one
            # photo everywhere — and the dashboard backfills FB/IG posts from
            # the X avatar of the same handle at read time (web.py).
            for p in posts:
                h = str(p.get("page") or "").lower()
                if h and p.get("author_avatar"):
                    st.set_profile(h, avatar_url=p["author_avatar"])
        avatars = st.profiles()   # handle → cached avatar, for backfill
        # Auto-register a page ONLY when we confirmed we're on the real Favorites
        # feed. If we fell back to the home feed, we must not treat everyone the
        # account follows as a favorite — store only already-tracked pages.
        if not on_fav:
            log("[fb] favorites: could NOT open the Favorites feed — reading the "
                "home feed instead; saving only pages you already track (no "
                "auto-add, to avoid pulling in non-favorites)")
        for p in posts:
            handle = str(p.get("page") or "")
            s = by_handle.get(handle.lower())
            if s:
                pid = s["project_id"]
            elif project_id and on_fav:
                st.add_source(handle, project_id=project_id)   # auto-register
                by_handle[handle.lower()] = {"label": handle, "project_id": project_id}
                pid = project_id
                added += 1
            else:
                continue
            matched += 1
            p["project_id"] = pid
            p["source_label"] = handle
            if not p.get("author_avatar"):
                p["author_avatar"] = avatars.get(handle.lower())
            if st.upsert(p):
                total += 1
        st.db.commit()
        log(f"[fb] favorites: on_favorites={on_fav}, {matched} posts attributed "
            f"({added} new page(s) auto-added), +{total} new")
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
    # FB_FAV_PROJECT lets the CLI/service auto-register favorited pages under a
    # project (the dashboard button passes this per-click).
    fav_project = int(os.getenv("FB_FAV_PROJECT", "0")) or None

    if args.cmd == "favorites":
        n = asyncio.run(run_favorites(args.store, project_id=fav_project))
        print(f"[fb] favorites pass complete: {n} new")
        return 0
    if args.cmd == "run":
        # Mode and cadence come from the DASHBOARD settings first (the settings
        # table in fb_results.db), falling back to the environment. The loop
        # re-reads them EVERY cycle, so flipping a switch on the dashboard
        # takes effect without restarting the service (RULEBOOK §6).
        def _live_settings():
            with store_fb.Store(args.store) as st:
                return {
                    "paused": st.setting("fb_paused") == "1",
                    "mode": (st.setting("fb_mode")
                             or os.getenv("FB_MODE", "pages")).lower(),
                    "every": int(st.setting("fb_interval_s") or args.every),
                    "fav_every": int(st.setting("fb_fav_interval_s")
                                     or os.getenv("FB_FAV_INTERVAL_S", "3600")),
                }

        if not args.loop:
            cfg = _live_settings()
            if cfg["mode"] == "favorites":
                n = asyncio.run(run_favorites(args.store, project_id=fav_project))
            else:
                # One-shot: collect everything enabled now (ignores per-page
                # cadence) — what the dashboard "Fetch now" button wants too.
                n = asyncio.run(run_once(args.store, max_scroll=args.scroll))
            print(f"[fb] pass complete: {n} new")
            return 0

        async def loop():
            while True:
                cfg = _live_settings()
                if cfg["paused"]:
                    # Cheap idle tick: no browser, no log spam — the pause
                    # state is visible on the dashboard.
                    base = max(60, args.tick)
                elif cfg["mode"] == "favorites":
                    await run_favorites(args.store, project_id=fav_project)
                    base = cfg["fav_every"]
                else:
                    await run_due(args.store, default_interval=cfg["every"],
                                  max_scroll=args.scroll)
                    base = max(60, args.tick)
                # Jitter the wait ±25% so the rhythm isn't a robotic fixed clock.
                await asyncio.sleep(int(base * random.uniform(0.75, 1.25)))
        asyncio.run(loop())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
