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
import time

import store_fb


async def collect_source(engine, store, source, *, max_scroll=1, log=print) -> int:
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


async def run_once(store_path="fb_results.db", *, max_scroll=1, log=print) -> int:
    from engine_fb import FacebookEngine

    st = store_fb.Store(store_path).open()
    try:
        sources = st.sources(enabled_only=True)
        if not sources:
            log("[fb] no enabled sources — add one with `collect_fb.py add-source`")
            return 0
        if not os.getenv("FB_C_USER") or not os.getenv("FB_XS"):
            log("[fb] FB_C_USER / FB_XS not set in the environment — cannot log in")
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
    rn.add_argument("--every", type=int, default=int(os.getenv("FB_INTERVAL_S", "21600")))
    rn.add_argument("--scroll", type=int, default=1)

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
    if args.cmd == "run":
        async def loop():
            while True:
                n = await run_once(args.store, max_scroll=args.scroll)
                print(f"[fb] pass complete: {n} new")
                if not args.loop:
                    return
                await asyncio.sleep(max(60, args.every))
        asyncio.run(loop())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
