# Prompt for the Watch-Tower agent — the Collector panel, all three platforms

*Paste the fenced block below to the Watch-Tower side verbatim. It is written
for their coding agent. Rationale for us is above the fence; the earlier
`WATCH_TOWER_PROMPT.md` (X cursor pull) and `WATCH_TOWER_INSTAGRAM_PROMPT.md`
(IG pull) still stand — this one is about the Connections → Collector PANEL
and the binding model, and it adds Facebook.*

## Context for us, not for them (2026-09-18)

What their panel does today (read off app.watch-tower.in → Connections →
Collector): one card per upstream project, showing ONE X stream line
(`list:<id>` or the keyword query), "handles seen (24h)", posts/24h, the mirror
count, the bound Watch-Tower project, and Fetch now / Backfill / Bind / Unbind.
Nothing for Instagram, nothing for Facebook, and a project with several X
watchlists shows only one of them.

Two defects, found by comparing their cards with our `/api/streams`:

1. **They derive the project from the stream label.** Our labels are
   `wl:<watchlist_id>:<n>`; they read the number as a PROJECT id. Their card
   "BhajanLal Ji Watchlist Media 2 · upstream #17" shows
   `list:2093579659413979567 · 2,440 collected` — that is **watchlist 17**,
   "Rajasthan WatchList (ज़िलाध्यक्ष)", which lives in **project 14**. Project
   17's real list is watchlist 81, `list:2100208316253352362`, 1,087 posts,
   and it appears nowhere on their panel. "X Influencer - CAT C · #15" shows
   `list:2093642904740954190` (our stale, paused `wl:15:0`), not project 15's
   live watchlist 19. The ids only line up for 13 and 14 by coincidence.
   Whatever their per-list Fetch now / Backfill does, it is aimed at the wrong
   list on those cards.
2. **One stream per project.** Project 14 has three X lists; project 16 has a
   dozen `links` watchlists; the card shows one line.

Instagram: they already got the IG pull prompt (2026-09-02). The panel never
grew an Instagram section, and the "BhajanLal Ji Watchlist Media 2" project
with 25 IG sources shows none of them.

Agreed model (operator decision, 2026-09-18): the card points at the upstream
PROJECT and lists every X / Instagram / Facebook list under it; Watch-Tower
binds the project and then chooses all-or-selected lists per platform, and
can unbind any one list. Their mirror stays as it is. Our API already filters
per list for X (`stream=`) and Instagram (`source=`); Facebook needs `page=`
from us (open items below).

---

```
This is about your Connections → Collector panel and how a Watch-Tower project
binds to our collector. Same host (https://scraper.vedictech.in), same Bearer
key, same 23 GET paths you already have. Nothing new was opened; everything
below is readable today. This does not replace the X cursor note or the
Instagram note, and it keeps your mirror exactly as it is — it fixes how the
panel is built, adds Facebook, and changes binding from "one list" to "a
project, then all or some of its lists on each platform".

## 0. Two bugs to fix first

a) You read our stream label `wl:N:0` and treat N as the upstream PROJECT id.
   N is the WATCHLIST id. They are different tables. Proof, from our API right
   now:

     your card "BhajanLal Ji Watchlist Media 2 · upstream #17"
       shows  list:2093579659413979567 · 2,440 collected
       that is watchlist_id 17 = "Rajasthan WatchList (ज़िलाध्यक्ष)", project 14
     project 17's actual X watchlist is watchlist_id 81,
       list:2100208316253352362, 1,087 posts — missing from your panel
     your card "X Influencer - CAT C · #15" shows list:2093642904740954190,
       our stale paused stream wl:15:0; project 15's live list is watchlist 19,
       list:2094784419513221358

   Never parse a stream label. The project → watchlist → stream join is
   served to you:

     GET /api/watchlists?project=<P>
       -> { watchlists:[ { watchlist_id, project_id, name, kind, list_id,
                           owner_handle, paused, streams:[{stream_id,label,
                           paused,tweets,…}], … } ] }
     GET /api/streams/assignments
       -> { streams:[ {stream_id,label,paused,list_id,tweets,projects:[P,…]} ] }

   Either one gives you the project for every stream. Use the first for the
   card; the second only if you need the reverse lookup.

b) A project can hold MANY X watchlists (project 14 has three lists; 16 has
   a dozen). Show all of them. One line per watchlist, not one per project.

## 1. The unit of the panel is the upstream PROJECT

One card per row of GET /api/projects (id, name, archived, watchlists,
streams). Skip archived. Under each card, three sections, always rendered,
even when empty ("0 sources" is a normal state, not an error):

  X / Twitter   from GET /api/watchlists?project=P
    one row per watchlist:
      name · kind · what it watches · paused · posts
      kind = xlist     -> "List <list_id>" (+ owner_handle if not null)
             query     -> the query text (filters.q or name)
             keywords  -> the keyword set
             handles   -> the handle set
             links     -> "post links (sheet-fed, daily refresh)" — these are a
                          separate consumer's lists; show them, never mirror
                          them, never bind on them
      posts = sum of streams[].tweets ; paused = every stream paused
    card total = sum over watchlists

    Every field you need is on the watchlist row: watchlist_id, project_id,
    name, kind, list_id, owner_handle (the X account the List was made on
    — the only account that can edit its members), created_at, filters,
    interval_s, streams[], backfill, and for query/keywords/handles kinds
    a members[] array holding the terms or handles themselves.

    MEMBERS of an X List — show them, expandable, per list:
      GET /api/watchlist/xmembers?list_id=<list_id>
        -> { members:[{user_id,username,display_name,avatar,fetched_ms}],
             count }
      keyed by list_id (NOT watchlist id, NOT project). This cache is now
      populated for every live list (News 92, Rajasthan 115, ज़िलाध्यक्ष 40,
      MP 13, BhajanLal Media 2: 28 …) and refreshed from our dashboard;
      the note from August that said it was empty is superseded. It is
      still display-only for you: membership is tracked by project/stream
      scoping, so never turn this list into a per-handle poll.

  Instagram     from GET /api/ig/status?project=P
    { sources:[{label,type,value,platform_id,project_id,account,
                assigned_account,enabled,collector}],
      totals:{posts,sources_enabled,newest_taken_at,sources_unresolved},
      paused, config:{interval_s}, accounts:[…] }
    one row per source where enabled:
      label · type (user/hashtag/following) · @value ·
      "id pending" when platform_id is "" (nothing collected for that
      source yet — we are still resolving it; it is not your problem) ·
      via @collector
    section header: totals.posts collected · newest_taken_at ·
      sources_unresolved pending · paused ·
      "checked every {config.interval_s}s"
    ignore accounts[] — that is our pool, not project data

  Facebook      from GET /api/fb/status?project=P
    { sources:[{label,project_id,enabled,interval_s,last_run,posts,speed}],
      totals:{posts}, enabled, paused, config:{mode,default_interval_s,
      fav_interval_s,monthly_cap_gb,use_proxy}, session, health }
    one row per source where enabled: page handle · posts · last_run ·
      interval
    section header: totals.posts · config.mode ("pages" or "favorites" —
      favorites mode collects from the account's feed and has zero sources
      by design, so "0 pages · N posts" is normal) · paused
    ignore session/health — collector internals

  All three status calls REQUIRE ?project=P. Without it you get
  {"error":"no project selected"} with HTTP 200. Check for the `error` key.

## 2. Binding: point at the upstream PROJECT, then choose all or some of
##    its lists, per platform

The anchor of a binding is (your project) ↔ (our project_id P). Under that
anchor, each platform has its own selection:

    X          mode = all | selected   selected = set of watchlist_id
    Instagram  mode = all | selected   selected = set of source label
    Facebook   mode = all | selected   selected = set of page label
                                        (whole-project only until §2c lands)

Every list stays visible on the card whether it is bound or not, with a
per-row Bind / Unbind. "Unbind" on the last row of a platform leaves that
platform unbound; "Unbind" on the card drops the whole anchor. The mirror
stays exactly what it is today: one pull per bound thing into your own
copy, projects filled from the copy, backfill never touches us.

2a. "all" is a MODE, not a snapshot. It means "every list in P on this
    platform, including ones we add next week". Do not expand "all" into
    the ids that exist at bind time and store those — a list added later
    would then never flow and nobody would notice for a month. Store the
    mode; resolve it against /api/watchlists (or ig/fb status) on every
    mirror run. "selected" is the explicit set and does not grow by itself;
    the card shows new unbound lists so a human can add them.

2b. Selection lives INSIDE one upstream project. A Watch-Tower project must
    not stitch list 81 from P=17 with list 14 from P=14. If a client needs
    two upstream projects, bind two anchors. Project scoping is the thing
    that keeps a leak like the 14 political handles in News (Aug) from
    happening again; per-list selection narrows it, never widens it.

2c. The pulls — one cursor/watermark on your side per (your project, P,
    platform, list). All filters combine with the cursor:

  X, mode=all       GET /api/tweets?project=P&since_collected_ms=<c>&limit=500
  X, selected       GET /api/tweets?project=P&stream=<label>
                        &since_collected_ms=<c>&limit=500
                    one pull per selected watchlist, per stream label in its
                    streams[] (almost always exactly one, wl:<id>:0). Dedupe
                    on tweet_id — a post hit by two selected lists is one row.
  Instagram, all    GET /api/ig/posts?project=P&limit=200[&cursor][&since=72h]
  Instagram, sel.   GET /api/ig/posts?project=P&source=<label>&limit=200
                        [&cursor][&since=72h]
                    one pull per selected source. Cold load walks cursor;
                    incremental = since=72h every 5 min, upsert on `id` —
                    as in the Instagram note.
  Facebook, all     GET /api/fb/posts?project=P&limit=200[&since=72h|7d|ISO]
                    -> { count, total, posts:[…] }. No cursor today. Rows
                    newest-first, 200 cap: cold load = since=30d, then
                    narrower day windows if total > count. Incremental =
                    since=72h every 30 min, upsert on tweet_id. Facebook
                    posts land HOURS after posting (a page is rendered once
                    every 1–24h) so the 72h overlap is not optional.
  Facebook, sel.    not filterable yet. We are adding `page=<label>` and a
                    `since_collected_ms` cursor to /api/fb/posts; until you
                    see them in the 403 body's parameter list / hear from
                    us, Facebook binds whole-project only. Build the
                    per-page checkboxes now, behind that flag.

  Facebook post shape (already X-shaped, no mapping needed):
    { platform:"facebook", tweet_id:"<string>", url, text, created_at,
      collected_at, author_username:<page handle>, author_display_name,
      author_avatar, media:[{type,url,thumb}], like_count,
      reply_count(=comments), retweet_count(=shares), view_count:null,
      source:<our person label>, label, label_source, label_ms }
    media[].url for Facebook points at OUR host (/media/fb/…) and does not
    expire — unlike Instagram's CDN links. Keep it as-is.

2d. Instagram "lists" are individual handles, and a project can hold 100+
    (P=14 has 118). Do not render 118 checkboxes as the primary control:
    default to the platform toggle (all), and offer "selected" as a picker
    grouped by our `source` label (the cross-platform person name your feed
    already shows) with per-handle exclusion underneath. `links` watchlists
    (kind=links, P=16) are shown and never bindable.

2e. Membership INSIDE a list is ours to track. Handles come and go on an X
    List, we add and remove Instagram sources; a bound list keeps flowing
    with its current members and you change nothing. That is what the
    project/stream/source filters buy you over a handle list.

  Keep your existing "Also send to project…" for News (upstream 8) as-is —
  that is a second consumer of the same P, fine.

## 3. Buttons

  Fetch now   X only. POST /api/fetch is the ONE write you may call and it
              spends our live X budget; keep your client timeout above a
              live fetch (you were told — 5,064 abandoned calls in August).
              Do NOT show Fetch now for Instagram or Facebook: /api/ig/fetch
              and /api/fb/* POST are dashboard-only, 403 by design, and our
              collectors run on their own human-paced cadence. Do not probe.
  Backfill    from your own mirror, all three platforms, never from us.
  Mirror now  run the three incremental pulls for every bound P.
  Refresh     re-read /api/projects + the three status calls per P. Cache
  stats       for 60s; the status calls are cheap but not free.

## 4. Numbers on the card, per platform

  per LIST row          bound / unbound · upstream posts (X: that
                        watchlist's streams[].tweets; IG: n/a per source —
                        show "id pending" or last post time; FB: sources[].posts)
                        · in your mirror for that list · last pull time
  "handles seen (24h)"  distinct author_username in YOUR mirror, last 24h,
                        for that P and platform
  "posts / 24h"         count in your mirror, last 24h (stop sampling our
                        pages for this — you have the rows)
  "in our mirror"       your total for (P, platform)
  upstream total        X: sum streams[].tweets; IG: totals.posts;
                        FB: totals.posts — show upstream vs mirror side by
                        side so a gap is visible; with mode=selected the
                        gap is expected and the row-level numbers explain it

## 5. Verify, then report back

  1. GET /api/projects → one card per non-archived row; no card is built
     from a stream label anywhere in the code (grep for "wl:").
  2. Card 17 ("BhajanLal Ji Watchlist Media 2") shows X: watchlist 81,
     list 2100208316253352362; Instagram: 25 sources, all "id pending",
     0 posts; Facebook: 0 pages. Card 14 shows THREE X lists and ~118
     Instagram sources.
  3. Bind one project to P=14, X mode=all, Instagram mode=all. After the
     first Mirror now: your X count matches the sum of the three lists'
     streams[].tweets within the 24h drift, your Instagram count equals
     /api/ig/status?project=14 totals.posts, and Instagram rows render in
     your Live Feed with the Instagram filter.
  4. Switch that binding to X mode=selected with ONLY watchlist 18
     ("Rajasthan Watchlist (MP)"). Backfill from your mirror. Your X count
     for that project must now equal watchlist 18's streams[].tweets, and
     GET /api/tweets?project=14&stream=wl:18:0&limit=5 must return the same
     newest rows you hold. Unbind list 18 → the platform shows unbound and
     the incremental pull for X stops; Instagram keeps flowing.
  5. Add a test list on our side (we will tell you when) to P=14 while your
     binding is mode=all: it must appear on the card AND start flowing
     without anyone touching the binding. Repeat with mode=selected: it
     must appear on the card as unbound and NOT flow.
  6. Bind a project to P=9 ("fb_pages", favorites mode) and confirm 160
     Facebook rows arrive with media URLs on our host.
  7. Report per bound P: your count vs our total, per platform and per
     bound list, and the distinct `source` values — same reconciliation as
     before.
```

---

## Open items on our side

- `wl:15:0` (list 2093642904740954190) is a paused stream with no live
  watchlist behind it; it still appears in `/api/streams`. Harmless, but it is
  what their #15 card latched onto. Worth a cleanup.
- **Promised in §2c, ours to ship:** `page=<label>` filter and a
  `since_collected_ms` cursor on `/api/fb/posts` (`web.py:_fb_posts`,
  `store_fb.recent`). Without them Facebook can only bind whole-project.
  Same shape as the `source=` filter `/api/ig/posts` already has; add the
  IG `since_collected_ms` cursor in the same commit so all three pulls are
  identical. Tell them when it is live (the 403 body's parameter list is
  where they were told to look).
- Per-list X pulls use `stream=<label>`; a watchlist normally has exactly one
  stream (`wl:<id>:0`). If a watchlist ever grows a second stream they must
  pull both — `streams[]` on `/api/watchlists` is the source of truth, and the
  prompt says so.
- Verify step 5 needs us to add (and later remove) a throwaway list on P=14
  on request. Cheap; do it when they ask.
- `/api/ig/status` and `/api/fb/status` expose `accounts[]` / `session` /
  `health` to the API key. Nothing secret (labels, exit IPs, identity text),
  but it is collector telemetry a consumer has no use for; consider trimming
  them from the keyed view.
