# Links snapshot — handover for the reach tracker

*What the Collector gives a tool that needs the daily reach of specific X
posts. Written 9 Sep 2026 with the first version of the feature
(`X_LINKS_PLAN.md`). Everything here is additive-only from now on: fields
are added, never renamed, removed or retyped.*

## What you get

A **snapshot**, once a day (or as often as you like — reads are local and
free). Every post link the operator has put on a *links watchlist* in your
project, joined with the post's **current** engagement counters. The
Collector re-fetches each post on its own cadence (24 h by default; 12 h /
48 h per list) and **overwrites** the counters. It keeps **no history** —
that is your job. `view_count` is the reach figure.

## The call

```
GET https://scraper.vedictech.in/api/links?project=<P>&limit=500&offset=0
Authorization: Bearer <your key>
```

Your key is **locked to project `<P>`**. It can read only
`/api/links`, `/api/tweets`, `/api/watchlists` and `/api/export`, only with
`?project=<P>`; any other path, project or method returns a 403 that says
so. Use `/api/links` — the others exist for debugging.

Parameters: `project` (required), `watchlist` (one list's id), `status`
(`ok` | `pending` | `unavailable` | `removed`), `sort` (`day` | `views` |
`likes` | `posted` | `refreshed` | `added`; the default is a stable
`watchlist, sheet row, added, id` order — the sheet's own order), `limit`
(≤ 500), `offset`.

**Paging:** page with `offset` until `offset + rows.length >= total`. There
is no cursor, on purpose: a refreshed post's `collected_ms` never changes,
so a cursor would never re-deliver it. The order is stable between
refreshes, so offset paging is safe.

## The row

```json
{
  "watchlist_id": 15, "watchlist": "6/9/26", "tab": "6/9/26", "day": "2026-09-06",
  "section": "National X Influencers", "sheet_row": 31,
  "refresh_every_s": 86400,
  "url": "https://x.com/nasa/status/1789…",   "tweet_id": "1789…",
  "post_url": "https://x.com/NASA/status/1789…",
  "status": "ok", "status_note": null,
  "added_at": "2026-09-08T10:00:00.000000+00:00", "added_via": "sheet", "sheet_row": 12,
  "last_refresh_ms": 1788440000000, "last_attempt_ms": 1788440000000,
  "refresh_count": 12, "fail_streak": 0, "force": false, "fetched": true,
  "created_at": "2026-09-01T08:12:00+00:00", "created_ms": 1787000000000,
  "text": "…", "lang": "en",
  "author_username": "nasa", "author_display_name": "NASA", "author_id": "11348282",
  "author_followers": 87000000, "author_avatar": "https://pbs.twimg.com/…",
  "like_count": 1200, "retweet_count": 300, "reply_count": 80,
  "quote_count": 25, "view_count": 410000, "bookmark_count": 90,
  "is_retweet": false, "is_reply": false, "is_quote": false,
  "media": [ { "type": "photo", "url": "https://pbs.twimg.com/…" } ],
  "collected_ms": 1788363997000, "last_seen_at": "2026-09-09T02:00:00.000000+00:00"
}
```

Rules you can rely on:

- `tweet_id` is a **string** (ids exceed 2^53). De-duplicate on it.
- `url` is the link as the operator wrote it (always present); `post_url`
  is X's canonical URL for the post, `null` until the first successful read.
- Counters are **numbers or `null`**. `null` means the Collector has not
  read this post yet (`fetched: false`) or X did not give the number —
  never zero. `author_followers` keeps its last known value if a later read
  cannot see it.
- `last_refresh_ms` is when the numbers were last read (server clock, ms).
  `last_seen_at` on the post says the same in ISO. **That is your
  timestamp for a history row**, not `collected_ms` (frozen at first sight).
- `status`:
  - `ok` — the last fetch returned the post; counters are current.
  - `pending` — listed but not read yet, or retrying after a transient
    miss (`fail_streak` > 0, `status_note` says why).
  - `unavailable` — deleted, protected or suspended. `status_note` carries
    X's own sentence. **The counters are the last ones read**; keep them.
  - `removed` — taken off the sheet or removed by hand. No more refreshes;
    the last counters stay. Filter with `status=ok` if you do not want them.
- `tab` is the Google Sheet tab the link came from (`watchlist` is the same
  unless the operator renamed the list). **`day`** is that tab's title
  parsed as an ISO date (`6/9/26` → `2026-09-06`, day-first) and `null` for
  a tab that is not a date (`Tweet Links`, `Counter Links`, …). **`section`**
  is the heading the link sits under in its tab (`National X Influencers`,
  `Counter Comments Links`, …), `null` if none. Group on `day` then
  `section` to rebuild the sheet's structure. A post listed on several tabs
  appears once per tab, with the same counters.

## Cadence and freshness

Posts are refreshed one at a time through the day, not in a burst, so at
any moment some rows are a few hours fresher than others. Pull once a day
at a fixed time and record `last_refresh_ms` per row; a row whose
`last_refresh_ms` did not move since your last pull has not been re-read
(the Collector was paused, or the post is `unavailable` and now on a weekly
retry).

## What it is not

Not a feed, not a stream, not Watch-Tower's mirror. No webhook, no cursor,
no history. If you need a post's history, you build it from daily
snapshots — that was the agreement.
