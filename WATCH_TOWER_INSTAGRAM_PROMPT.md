# Prompt for the Watch-Tower agent — Instagram

*Paste the fenced block below to the Watch-Tower side verbatim. It is
self-contained; the long version with rationale is
`WATCH_TOWER_INSTAGRAM_HANDOVER.md`.*

---

```
Add Instagram to your Collector mirror. X already works; Instagram uses the
same host, the same API key, the same project ids, a DIFFERENT endpoint, a
DIFFERENT post shape and a DIFFERENT paging model. Nothing is pushed for
Instagram (the signed webhook is X-only) — you pull, exactly like X.

## 1. Endpoints (same Bearer key you use for /api/tweets)

  GET https://scraper.vedictech.in/api/ig/status?project=<P>
      -> { sources:[{label,type,value,platform_id,project_id,account,enabled}],
           totals:{posts,sources_enabled,newest_taken_at,sources_unresolved},
           paused, config:{interval_s}, accounts:[...] }

  GET https://scraper.vedictech.in/api/ig/posts?project=<P>&limit=200
        [&cursor=<id>] [&since=72h|7d|ISO] [&source=<label>] [&username=<handle>]
      -> { count, total, next_cursor, posts:[...] }

  `project` is REQUIRED. Without it you get {"error":"no project selected",...}
  WITH HTTP 200 — always check for an `error` key.
  `limit` is clamped to 200. Rows are NEWEST FIRST by `id`. `cursor` walks
  OLDER (pass back `next_cursor`). `since` is a window on POST time
  (created_at), not collection time. There is NO since_collected_ms and NO
  collected_at on Instagram — see §3.

  Do not call POST /api/ig/fetch, /api/ig/source, /api/ig/control,
  /api/ig/settings — dashboard-only, 403 by design. Do not probe other paths.

## 2. Post shape

  {
    "id": "3456789012345678901",           // STRING. Dedupe on this.
    "platform": "instagram",
    "url": "https://www.instagram.com/p/<shortcode>/",
    "shortcode": "…",
    "created_at": "2026-09-02T09:14:33Z",   // post time, UTC
    "author": {"username": "narendramodi", "id": "1234567890"},
    "text": "caption …",
    "media": {"type": "photo|video|album|other",
              "thumbnail": "<cdninstagram url>", "video": "<mp4 url or null>"},
    "metrics": {"likes": 15230, "comments": 412, "views": 98000},
    "source": "Narendra Modi",             // our cross-platform person label
    "author_avatar": "<url or null>",
    "label": null, "label_source": null, "label_ms": null   // same as X
  }

  Map to your X-shaped row:
    tweet_id=id  text=text  created_at=created_at
    author_username=author.username  author_display_name=author.username
    author_id=author.id  like_count=metrics.likes  reply_count=metrics.comments
    view_count=metrics.views  retweet/quote/bookmark=null  is_*=false
    media=[{type:media.type, url:media.video??media.thumbnail, thumb:media.thumbnail}]
    streams=[source]
  media URLs are Instagram CDN links and EXPIRE (oe= param): render promptly,
  mirror the thumbnail bytes at ingest if you need it beyond a few days.
  metrics are frozen at first collection and never change upstream.

## 3. Mirror loop (gapless without a collection cursor)

  Cold load / Backfill, per upstream project P:
    cursor=None; loop: GET /api/ig/posts?project=P&limit=200[&cursor]
      upsert by id (ON CONFLICT DO NOTHING); stop when count<200 or next_cursor null

  Incremental, every 5 minutes (our collector runs every ~300s, daytime IST):
    cursor=None; loop: GET /api/ig/posts?project=P&limit=200&since=72h[&cursor]
      upsert by id; stop when count<200 or next_cursor null
    store your own last-run time as the Instagram "cursor" — there is no
    upstream value to store.

  Why the 72h window: the collector can pick up a post hours or days after
  it was posted; a "newest id seen" watermark would skip it forever. The
  overlap catches it. Volume is small (tens–hundreds/project/day), so the
  re-read costs nothing.

## 4. Cards and binding

  Same cards as X. Per upstream project add: Instagram sources (from
  /api/ig/status sources[] where enabled), totals.posts, newest_taken_at,
  paused, your mirrored count and last run. A bound Watch-Tower project
  receives BOTH X and Instagram posts from that upstream project. Zero IG
  sources on a project is normal, not a failure.

## 5. Verify and report back

  1. GET /api/status -> 200 with the Bearer header.
  2. GET /api/projects; pick a P where /api/ig/status?project=P has sources.
  3. GET /api/ig/posts?project=P&limit=5 -> ids are strings, thumbnail opens.
  4. Cold-load P; your row count must equal `total` from the first page.
  5. Run the incremental loop 30 min during Indian daytime; new rows within
     ~5 min of newest_taken_at moving.
  6. Live Feed -> Source: Instagram -> bound project renders thumb + caption.
  Report per project: your Instagram row count vs our `total`, and the
  distinct `source` values seen.

  If you want a `since_collected_ms` cursor on /api/ig/posts (so the loop is
  identical to X), say so — it is a small change on our side.
```
