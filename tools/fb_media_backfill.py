#!/usr/bin/env python3
"""
fb_media_backfill.py — recover pictures for Facebook posts collected before the
media store existed.

    python3 tools/fb_media_backfill.py --limit 20 --dry-run
    python3 tools/fb_media_backfill.py            # the rest, slowly

The problem it solves. Every fbcdn URL Facebook mints is signed and expires in
about five days (`oe=<hex epoch>` in the URL). Posts collected before
2026-09-02 stored those links and nothing else, so their pictures are gone from
our side: the link in the row returns "URL signature expired" and no amount of
re-requesting it will help.

The pictures themselves are not gone, though — they are still on the post. So
this walks the affected rows and re-harvests each one through Facebook's PUBLIC
post embed (`plugins/post.php?href=<permalink>`), which renders the post live
and therefore mints FRESH image URLs, downloads those bytes into the media
store, and rewrites the row to point at our own copy. After it runs, an old
post renders in the Collector's own card like any new one, and its media is
deliverable to Watch-Tower.

Why the embed page and not the post page:
  * it is public — no session, so the collector's burner account is never
    spent on a backfill, and a rate-limit here cannot cost us collection;
  * it is one small page per post rather than a full logged-in render;
  * it is the same surface the dashboard already frames, so if a post renders
    in the feed today, this can read it.

It is deliberately slow (a pause between posts) and resumable: a row is only
rewritten once its bytes are stored, so an interrupted run loses nothing and
re-running picks up where it stopped.
"""

import argparse
import asyncio
import json
import pathlib
import random
import re
import sqlite3
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fb_media                      # noqa: E402
from engine_fb import _is_post_image  # noqa: E402

PLUGIN = "https://www.facebook.com/plugins/post.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def embed_href(url: str) -> str:
    """The bare permalink. Facebook's stored URLs carry a click-tracking
    payload (`__cft__[0]=…`, `__tn__=…`) that the plugin does not want; only
    the story/video ids identify the post."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return url
    keep = {k: v for k, v in urllib.parse.parse_qsl(u.query)
            if k in ("story_fbid", "id", "v")}
    q = urllib.parse.urlencode(keep)
    return f"{u.scheme}://{u.netloc}{u.path}" + (f"?{q}" if q else "")


def needs_backfill(media_json: str) -> bool:
    """A row is stale when any item still points at Facebook's CDN — that is
    precisely a link that has died or will."""
    try:
        media = json.loads(media_json or "[]")
    except (TypeError, ValueError):
        return False
    return any(isinstance(m, dict)
               and re.search(r"fbcdn|scontent", str(m.get("url") or ""))
               for m in media)


async def harvest(page, url: str):
    """Fresh image URLs for one post, newest signature, from the embed."""
    src = f"{PLUGIN}?href={urllib.parse.quote(embed_href(url), safe='')}" \
          f"&show_text=false&width=750"
    await page.goto(src, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    return await page.evaluate("""() =>
        [...document.querySelectorAll('img, image')]
          .map(e => e.tagName === 'IMG' ? e.src
                : (e.getAttribute('xlink:href') || e.getAttribute('href') || ''))
          .filter(Boolean)""")


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "fb_results.db"))
    ap.add_argument("--limit", type=int, default=0, help="0 = every stale row")
    ap.add_argument("--pause", type=float, default=4.0,
                    help="seconds between posts (jittered)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be fetched; write nothing")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = [r for r in db.execute(
        "SELECT post_id, url, media_json FROM posts "
        "WHERE media_json IS NOT NULL AND media_json != '[]' "
        "ORDER BY collected_ms DESC")
        if needs_backfill(r["media_json"]) and r["url"]]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} posts still point at Facebook's CDN")
    if not rows or args.dry_run:
        for r in rows[:10]:
            print("  would fetch", r["post_id"], embed_href(r["url"])[:90])
        return 0

    store = fb_media.MediaStore(ROOT)
    from playwright.async_api import async_playwright
    done = empty = failed = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA,
                                        viewport={"width": 900, "height": 1200})
        page = await ctx.new_page()
        try:
            for i, r in enumerate(rows, 1):
                try:
                    found = await harvest(page, r["url"])
                except Exception as e:
                    failed += 1
                    print(f"  [{i}/{len(rows)}] {r['post_id']}: "
                          f"{type(e).__name__}: {e}")
                    continue
                # The same rule the collector uses, so a backfilled post holds
                # exactly what a freshly collected one would: no avatars, no
                # reaction glyphs, no emoji sprites.
                urls, seen = [], set()
                for u in found:
                    if _is_post_image(u) and u not in seen:
                        seen.add(u)
                        urls.append(u)
                if not urls:
                    empty += 1
                    print(f"  [{i}/{len(rows)}] {r['post_id']}: embed showed no "
                          f"post images (deleted, or not public)")
                    continue
                try:
                    old = json.loads(r["media_json"])
                except (TypeError, ValueError):
                    old = []
                kind = "video" if re.search(r"/reel/|/videos/", r["url"]) else "photo"
                media = []
                for u in urls[:6]:
                    resp = await ctx.request.get(u, timeout=30000)
                    if not resp.ok:
                        continue
                    local = store.put(await resp.body(),
                                      resp.headers.get("content-type", ""), u)
                    if local:
                        media.append({"type": kind, "url": local,
                                      "thumb": local, "src": u})
                if not media:
                    failed += 1
                    continue
                db.execute("UPDATE posts SET media_json = ? WHERE post_id = ?",
                           (json.dumps(media), r["post_id"]))
                db.commit()
                done += 1
                print(f"  [{i}/{len(rows)}] {r['post_id']}: {len(media)} stored "
                      f"(was {len(old)})")
                await asyncio.sleep(args.pause * random.uniform(0.6, 1.6))
        finally:
            await browser.close()
    swept = store.sweep()
    print(f"\nrewritten {done}, no images {empty}, failed {failed}; "
          f"store holds {store.total_bytes()//1024} KB"
          + (f", evicted {swept}" if swept else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
