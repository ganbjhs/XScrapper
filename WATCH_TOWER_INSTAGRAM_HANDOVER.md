# Instagram → Watch-Tower — integration handover

*Hand this to the Watch-Tower developer / agent. It is everything their side
needs to add Instagram next to the X (Twitter) ingest that already works.
Written 2 Sep 2026 from the current source (`web.py`, `store_ig.py`,
`api.py`, `webhook.py`) and from what the Watch-Tower Collector page shows
today.*

---

## 0. One-paragraph summary

Watch-Tower already **pulls** X posts from the Collector with an API key,
project by project, on a `since_collected_ms` cursor, into its own mirror
("Collector" page → one card per upstream project → bind to a Watch-Tower
project). That works. Instagram is served by the **same host, same key, same
project ids**, from a **separate endpoint** (`/api/ig/posts`) with a
**different post shape** (nested `author`, `media`, `metrics`; the id key is
`id`, not `tweet_id`) and a **different paging model** (newest-first keyset
cursor on `id`, no collection-time cursor). Nothing is pushed for Instagram —
the signed webhook is X-only. To make Instagram "just work", Watch-Tower adds
an Instagram mirror loop per upstream project using the algorithm in §5, maps
the shape as in §4, and shows the rows under its existing "Instagram" source.

---

## 1. How Watch-Tower gets data today (X — the working reference)

Observed on `https://app.watch-tower.in/` → Connections → **Collector**
(`GET /api/collector` on the Watch-Tower side):

- One card per **upstream project** (`scraper_project_id` 2, 3, 7, 8, 9, 11,
  12, 13, 14, 15 …). Each card carries `mirror_enabled`, `mirror_cursor`
  (e.g. `"1788363997000"` — that is our `collected_ms`), `mirror_last_run`,
  `mirrored`, `mirrored_24h`, `lists[]` (our stream labels like `wl:15:0`),
  `handles`, `posts_24h`, `top_handles[]`, `role`, `binding`.
- "Every list is pulled once into **our own mirror**; projects are then
  filled from that copy, so a backfill never touches the collector again."
- Buttons: **Mirror now** (`POST /api/collector/mirror`), **Refresh stats**,
  per-card **Fetch now / Backfill… / Unbind / Bind** to a Watch-Tower project.
  `news_scraper_project` = 8 (our "News" project feeds Media Monitoring).

So the X pipeline is:

```
Collector (scraper.vedictech.in)             Watch-Tower
  GET /api/projects                     ─▶  list of upstream projects (cards)
  GET /api/watchlists?project=P         ─▶  lists / stream labels on the card
  GET /api/tweets?project=P
      &since_collected_ms=<cursor>&limit=500 ─▶ mirror table, cursor advanced
                                            ─▶ bound Watch-Tower project(s)
```

Auth on every call: `Authorization: Bearer <key>` (or `X-API-Key: <key>`).
The full list of key-readable GET paths (23) is in `WATCH_TOWER_PROMPT.md`;
`/api/ig/posts` and `/api/ig/status` are already on it. **The key you have
for X works for Instagram unchanged.**

Instagram must follow the same pattern: same cards, one extra mirror loop.

---

## 2. What exists on our side for Instagram

| Thing | Where | Notes |
|---|---|---|
| Collector service | `collect_ig.py` as `xscraper-ig.service`, `--loop --every 300` | Human-paced (`ig_human.py`): active hours 07:00–24:00 IST, randomised gaps, per-account daily budget, warm-up. Expect posts to land in **bursts every few minutes during the day, almost nothing at night**. |
| Storage | `ig_results.db` → table `posts` (one row per media, PK = Instagram `pk`) and `sources` | `INSERT OR IGNORE`: a post is stored **once**; like/comment/view counts are **frozen at first collection** and never refreshed. |
| Sources | `sources` table: `label` (the person, cross-platform identity), `type` (`user` / `hashtag` / `following`), `value` (IG handle), `project_id` | Each source belongs to exactly one project. A project with no IG sources returns zero posts — that is normal, not an error. |
| Read API | `GET /api/ig/posts`, `GET /api/ig/status` in `web.py` | Key-readable. Reads local SQLite — cheap, call it as often as you like. |
| Push (webhook) | `webhook.py` | **X only.** `store.tweets_after` reads the `tweets` table. Instagram is never pushed. Do not wait for it. |
| Content labels | `_stamp_labels` | Same Grok category labels as X, keyed `(project, 'instagram', id)`; travel on each post as `label`, `label_source`, `label_ms`. |

Project ids are shared across platforms: project 8 on X is project 8 on
Instagram. `GET /api/projects` is the single list.

---

## 3. The Instagram API contract (as it is today)

Base: `https://scraper.vedictech.in`

### 3.1 `GET /api/ig/status?project=<id>`

```json
{
  "accounts": [ {"username": "…", "label": "…", "active": true, "proxy": false,
                 "error": null, "last_used": 1756800000, "requests": 1234,
                 "checkpoint_at": null} ],
  "sources":  [ {"label": "Narendra Modi", "type": "user", "value": "narendramodi",
                 "platform_id": "…", "project_id": 8, "account": "", "enabled": 1} ],
  "totals":   {"posts": 412, "sources_enabled": 9, "newest_taken_at": 1756790000,
               "sources_unresolved": 0},
  "paused": false,
  "config": {"interval_s": 300}
}
```

Use it for the card: number of sources (= "handles"), `totals.posts`,
`newest_taken_at`, and `paused` (global pause switch for the IG collector).
`accounts` is global (server logins), everything else is per project.

### 3.2 `GET /api/ig/posts?project=<id>&limit=<n>&cursor=<id>&since=<window>&source=<label>&username=<handle>`

| Param | Meaning |
|---|---|
| `project` | **required.** Without it you get an error object (see below). |
| `limit` | default 30, **clamped to 200** (`store_ig.query`). |
| `cursor` | keyset cursor: pass the previous response's `next_cursor` to get **older** rows. |
| `since` | window on **post time** (`taken_at`), not collection time: `24h`, `7d`, `30m`, or an ISO timestamp. |
| `source` | filter by source label (the person). |
| `username` | exact IG handle. |

Ordering is **newest first by `id`** (`ORDER BY pk DESC`). Instagram media
pks are time-monotonic on *post creation*, so `id` order == post-time order.

Response:

```json
{
  "count": 30,
  "total": 412,
  "next_cursor": "3456789012345678901",
  "posts": [
    {
      "id": "3456789012345678901",
      "platform": "instagram",
      "url": "https://www.instagram.com/p/C1a2B3c4D5e/",
      "shortcode": "C1a2B3c4D5e",
      "created_at": "2026-09-02T09:14:33Z",
      "author": {"username": "narendramodi", "id": "1234567890"},
      "text": "caption text …",
      "media": {
        "type": "video",
        "thumbnail": "https://scontent….cdninstagram.com/….jpg?…&oe=…",
        "video": "https://scontent….cdninstagram.com/….mp4?…"
      },
      "metrics": {"likes": 15230, "comments": 412, "views": 98000},
      "source": "Narendra Modi",
      "author_avatar": "https://…",
      "label": null,
      "label_source": null,
      "label_ms": null
    }
  ]
}
```

Field notes:

- `id` is a **STRING** (same reason as `tweet_id`: it exceeds JS safe
  integer range). De-duplicate on `id`.
- `media.type` ∈ `photo | video | album | other`. `thumbnail` is always
  the still; `video` is set only for videos/reels. For an `album` you get
  the cover only (one row per media pk, not per carousel item).
- `media.*` URLs are **Instagram CDN URLs, which are signed and expire**
  (the `oe=` parameter). Render them promptly and, if you need them
  beyond a few days, mirror the thumbnail bytes at ingest — we do **not**
  re-host Instagram media (only Facebook media is re-hosted by us).
- `created_at` is post time (UTC, `Z`). **There is no `collected_at` on
  Instagram posts** and no `since_collected_ms` parameter — see §5 for why
  this matters and what to do.
- `author.username` is the only author identity; Instagram gives no
  display name on a media row. `source` is our cross-platform person
  label — use it to join with the same person's X handle if you want.
- `metrics` are the numbers at first collection and never change.
- `label / label_source / label_ms` — same vocabulary as X; `null` =
  not classified yet.
- `total` is the true count for the window/filter, not the page size.

**Error on missing project — note the status code is 200:**

```json
{"error": "no project selected", "detail": "Instagram and Facebook data is scoped to a project …",
 "sources": [], "posts": [], "count": 0, "totals": {"posts": 0}, "next_cursor": null}
```

Always check for an `error` key; do not rely on HTTP status alone.

### 3.3 What you must NOT call

- `POST /api/ig/fetch`, `/api/ig/source`, `/api/ig/control`,
  `/api/ig/settings` — dashboard-only, 403 for a key by design. A key can
  only `POST /api/fetch` (X live fetch), and steady-state ingest should call
  that zero times.
- Do not probe for other endpoints; a 403 body lists `allowed_get` /
  `allowed_post`.

---

## 4. Shape mapping (Instagram → your existing X-shaped post)

| Watch-Tower field | Instagram source |
|---|---|
| id / tweet_id (string) | `id` |
| platform / source filter | `"instagram"` |
| url | `url` |
| text | `text` |
| created_at | `created_at` |
| author_username | `author.username` |
| author_display_name | `author.username` (no better value exists) |
| author_id | `author.id` |
| author_avatar | `author_avatar` |
| like_count | `metrics.likes` |
| reply_count | `metrics.comments` |
| view_count | `metrics.views` (null for photos) |
| retweet_count / quote_count / bookmark_count | `null` — Instagram has no equivalent |
| is_retweet / is_reply / is_quote | `false` |
| media[] | `[{type: media.type, url: media.video ?? media.thumbnail, thumb: media.thumbnail}]` |
| streams | `[source]` (the person label) |
| label / label_source / label_ms | as-is |

This is exactly the flattening our own dashboard does (`store_ig.to_feed`),
so the Live Feed on both sides renders the same thing.

---

## 5. Recommended mirror algorithm (this is the part that makes it gapless)

Why not copy the X loop verbatim: X uses `since_collected_ms`, a cursor on
**collection** time, which is gapless because our collector can pick up a
post hours after it was posted. Instagram has no such cursor today — its
only cursor walks **backwards** by `id` (post time). If you kept a "newest
id seen" watermark and stopped there, a post the collector found late
(older pk, collected today) would be skipped forever.

So use an **overlap window + upsert**, which is cheap because Instagram
volume is small (tens to low hundreds of posts per project per day):

```
for each upstream project P with mirror_enabled:

  COLD LOAD (once, or on Backfill):
    cursor = None
    loop:
      r = GET /api/ig/posts?project=P&limit=200[&cursor=cursor]
      if r.error: log and stop
      upsert every post by id
      if r.count < 200 or r.next_cursor is None: break
      cursor = r.next_cursor

  INCREMENTAL (every 5–10 minutes):
    cursor = None
    loop:
      r = GET /api/ig/posts?project=P&limit=200&since=72h[&cursor=cursor]
      upsert every post by id            # INSERT … ON CONFLICT DO NOTHING
      if r.count < 200 or r.next_cursor is None: break
      cursor = r.next_cursor
    record mirror_last_run = now, mirrored = count(*) where platform='instagram' and project=P
```

- `since=72h` is the overlap: any post *posted* in the last 3 days that the
  collector picks up late still lands. Posts older than 72h that are
  collected late (rare: a source added today with old posts) are caught by
  the cold load / Backfill, which pages the whole project.
- Polling every 5 minutes matches the collector's own cadence
  (`--every 300`). Faster buys nothing.
- Keep `mirror_cursor` for Instagram as **"last run time"** in your own
  table, not an upstream value — there is none to store.
- Upsert, never update: our counts never change, so a re-seen id is a no-op.

**Optional upgrade on our side (say the word and we ship it):** add
`collected_at` to the post and a `since_collected_ms` parameter to
`/api/ig/posts` with oldest-first ordering when cursoring — then the
Instagram loop becomes byte-for-byte the X loop. Until then, §5 above is
correct and complete.

---

## 6. Card / binding model

Reuse the existing cards. Per upstream project add an Instagram block:

- sources (from `/api/ig/status?project=P` → `sources[]` where `enabled`),
- `totals.posts`, `newest_taken_at`, `paused`,
- mirrored Instagram count and last run.

Binding is the same as X: an upstream project bound to a Watch-Tower project
should deliver **both** its X and its Instagram posts. A project with zero IG
sources simply shows "no Instagram sources" — do not treat it as a failure.

---

## 7. Verify (10 minutes)

1. `GET /api/status` with the Bearer header → 200 (proves the key).
2. `GET /api/projects` → pick a project id `P` that has Instagram sources
   (`GET /api/ig/status?project=P` → `sources.length > 0`).
3. `GET /api/ig/posts?project=P&limit=5` → 5 posts, `id` is a string,
   `media.thumbnail` opens in a browser.
4. Cold-load `P` (page with `cursor` to the end); compare your row count to
   `total` from the first response — they must match.
5. Run the incremental loop for 30 minutes during Indian daytime; new rows
   should appear within ~5 minutes of `newest_taken_at` moving on our side.
6. Open Watch-Tower Live Feed → Source: **Instagram** → bound project →
   posts render with thumbnail and caption; sentiment/topics run on `text`
   exactly as for X.
7. Report back: per project, your Instagram row count vs our `total`, and the
   distinct `source` values you see, so we can reconcile.

---

## 8. Open items on our side (for us, not for them)

- No `collected_at` / `since_collected_ms` on `/api/ig/posts` — the §5
  overlap window covers it, but a collection-time cursor is the cleaner
  contract and is a ~20-line change in `store_ig.query` + `to_api` +
  `_ig_posts`. Ship it if Watch-Tower asks, or pre-emptively.
- `/api/ig/posts` returns the "no project selected" error with HTTP **200**.
  Consider 400 for parity with `/api/collections/export`.
- Instagram media URLs expire; if a lasting thumbnail matters downstream,
  extend the Facebook-style local media store (`fb_media.py`) to Instagram.
- Metrics are frozen at first collection (`INSERT OR IGNORE`). Fine for
  monitoring latency; wrong if anyone downstream wants engagement growth.
