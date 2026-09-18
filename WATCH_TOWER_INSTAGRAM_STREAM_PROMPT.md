# Prompt for the Watch-Tower agent — Instagram is now a stream

*Paste the fenced block below to the Watch-Tower side verbatim. It is written
for their coding agent. Supersedes `WATCH_TOWER_INSTAGRAM_PANEL_PROMPT.md`
(the separate-panel ask) and, for ingestion, the 72h-window loop in
`WATCH_TOWER_INSTAGRAM_PROMPT.md` — that loop keeps working, but this one is
what they should run.*

## Context for us, not for them (2026-09-18)

They can pull X, so they can pull Instagram the same way — the only thing in
the way was our own endpoint split. Shipped in `web.py` (CHECKPOINT
2026-09-18 II): `/api/tweets?platform=instagram` serves Instagram on the X
contract, and the structure endpoints list one `ig:P:0` pseudo-stream per
project. Their change is now one query parameter on a loop they already run,
plus letting a `kind: "instagram"` row render on their cards. Nothing about
their X cards, mirror or bindings changes.

---

```
Instagram now comes to you exactly the way X does. Same host
(https://scraper.vedictech.in), same Bearer key, same endpoints you already
call, same cursor, same row shape. Your X code does not change; you run it
once more with one extra parameter. Nothing new was opened on the key.

## 1. Pull — the X loop, with platform=instagram

    GET /api/tweets?platform=instagram&project=P&since_collected_ms=<cursor>&limit=500
    Authorization: Bearer <key>
    → upsert by (platform, tweet_id); cursor = max(collected_ms) committed;
      page until a page comes back short. Cold load starts at 0.

  - Identical to your X pull in every respect: `since_collected_ms` is the
    gapless collection cursor, rows come OLDEST-FIRST while cursoring, the
    response carries `total` (bounded), `rows[]`, `cursor{since_id,
    since_collected_ms}` and `has_more`. `limit` clamps at 500.
  - `stream=ig:P:0` is accepted instead of `platform=instagram` (it implies
    the project) — use whichever your per-stream code path already sends.
  - `project` is required, as on every Instagram read. Without it you get an
    EMPTY page with a `note` field and HTTP 200 — no `error` key, nothing to
    special-case beyond "no rows".
  - Do not send `sort` while cursoring (ignored, as on X). `since=72h`,
    `q=`, `author=`, `min_likes=`, `min_views=`, `has_media=1`,
    `from_date`/`to_date` all mean what they mean on X.
  - /api/ig/posts and its 72h-window loop still work. Switch to this one
    and delete the window logic; there is no reason to keep two models.

## 2. Row shape

  Every row is the shared shape you already store for X, with two things to
  notice:

    platform            "instagram"            (X rows say "x" / carry none)
    tweet_id            STRING — the Instagram media id; dedupe on
                        (platform, tweet_id). Never parse it as a Number.
    streams             ["ig:P:0"]             the pseudo-stream (§3)
    author_username     the handle; author_display_name = the handle too
    author_id           the numeric Instagram user id, "" while unresolved
    created_at / created_ms      post time
    collected_at / collected_ms  when WE saved it (the cursor field)
    media               [{type: photo|video|album|other, url, thumb}]
                        Instagram CDN links — they EXPIRE (oe= param).
                        Render promptly; mirror the thumb bytes at ingest if
                        you need them beyond a few days. (Facebook media
                        does not expire; X media does not expire.)
    like_count, reply_count(=comments), view_count(=plays); retweet_count null
    source              our cross-platform person label
    label, label_source, label_ms   same as X

  Metrics are frozen at first collection and never change upstream.

## 3. Where Instagram appears on the structure endpoints

  Every place you read X streams from now also lists Instagram, one
  pseudo-stream per upstream project that has Instagram sources:

    GET /api/streams
      + { label:"ig:P:0", source:"instagram:project:P", paused, tweets,
          platform:"instagram" }
    GET /api/streams/assignments
      + { stream_id:-P, label:"ig:P:0", paused, list_id:null, tweets,
          projects:[P], platform:"instagram" }
    GET /api/watchlists?project=P
      + { watchlist_id:-P, project_id:P, kind:"instagram",
          platform:"instagram", name:"Instagram sources", list_id:null,
          owner_handle:null, paused,
          members:[{handle, display_name, user_id, resolved, collector, type}],
          streams:[{stream_id:-P, label:"ig:P:0", paused, tweets}] }

  Read them like any X stream: `tweets` is the project's Instagram post
  count, `paused` our collector's switch. `members[]` IS the account list —
  the handles you asked to see, with the numeric id (`user_id`, null while
  we are still resolving it — `resolved:false`) and which of our accounts
  reads it. There is no separate members call for Instagram.

  Ids are NEGATIVE on purpose: an integer your code can key on, impossible
  to collide with a real watchlist or stream, and a signal that the row is
  synthetic — no POST action accepts it. The label is `ig:<project>:0`; the
  number in it is the PROJECT id (unlike `wl:N:0`, where N is a watchlist
  id, not a project — your card "BhajanLal Ji Watchlist Media 2 · #17"
  currently shows list 2093579659413979567, which is watchlist 17 in
  project 14; project 17's list is watchlist 81. Build cards from
  /api/watchlists?project=P, never from a label.)

  Today: ig:13:0 (Varanasi, 10 sources), ig:14:0 (Rajasthan WatchList, 118
  sources), ig:17:0 (BhajanLal Ji Watchlist Media 2, 25 sources — all still
  resolving, so 0 posts for now; expected, not a fault). Re-read on every
  refresh; we add projects and sources without telling you.

## 4. Binding

  Treat `ig:P:0` as one more stream of project P. Whatever you do today to
  bind an X list to a Watch-Tower project, do for it: bind, unbind, mirror,
  backfill from your own copy. A project bound for X and Instagram receives
  both; bound for one, receives one. No Fetch now for Instagram — there is
  no live-fetch endpoint on your key and our collector paces itself.

## 5. Verify, then report back

  1. GET /api/streams → ig:13:0, ig:14:0, ig:17:0 present with
     platform:"instagram".
  2. GET /api/watchlists?project=14 → the X lists PLUS one kind:"instagram"
     row with ~118 members; project 17's row has 25 members, resolved:false.
  3. GET /api/tweets?platform=instagram&project=14&since_collected_ms=0&limit=5
     → 5 rows, oldest-collected first, streams:["ig:14:0"], tweet_id
     strings, thumbnail opens.
  4. Cold-load project 14 with the loop in §1 until a short page; your row
     count equals `tweets` on ig:14:0 from /api/streams (± what arrived
     during the walk).
  5. Run the incremental loop 30 min in Indian daytime: new Instagram rows
     within ~5 min of our newest collected_ms moving.
  6. Live Feed → Instagram filter on the bound project renders thumbs and
     captions.
  Report per project: your Instagram row count vs our `tweets`, and the
  distinct `source` values you hold.
```

---

## Open items on our side

- `/api/export` and `/api/live` (SSE) remain X-only. Fine — they pull.
- Facebook can get the same treatment (`fb:P:0`, `platform=facebook`) when
  they ask; `store_fb.to_feed` already emits the shape and `collected_ms` is
  in the table.
- `WATCH_TOWER_INSTAGRAM_PANEL_PROMPT.md` (separate panel) and the
  bind-all-or-selected design in `WATCH_TOWER_COLLECTOR_PANEL_PROMPT.md` are
  superseded for ingestion by this note; the per-list selection idea still
  applies as a later UI refinement on their side.
