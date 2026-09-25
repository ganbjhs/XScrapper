# Watch-Tower ← Collector: what is here and how to fetch it (2026-09-18, shared watchlists added 2026-09-25)

*The one document to hand the Watch-Tower agent. It is written for their
coding agent, is self-contained, and supersedes the endpoint sections of the
earlier notes (`WATCH_TOWER_PROMPT.md`, `WATCH_TOWER_INSTAGRAM_PROMPT.md`,
`WATCH_TOWER_INSTAGRAM_PANEL_PROMPT.md`). The rules in those notes still
hold; this is the current map. Paste the fenced block verbatim.*

Ours, above the fence: this describes the API as of commit `2f8e02b`
("Instagram becomes a stream"). It is live only once that commit is deployed
on the VPS (`cd /opt/xscraper/app && git pull --ff-only && bash
deploy/update.sh`) — the GitHub deploy workflow skips when its secrets are
unset, which it did for this push. Check `GET /api/ig/diag` → `git.rev`
starts with `2f8e02b` before sending this.

---

```
COLLECTOR ("xscraper") — WHAT IS HERE AND HOW YOU FETCH IT
Host https://scraper.vedictech.in · one Bearer key · read-only except one POST

Send the key on every request:  Authorization: Bearer <key>
(or X-API-Key: <key>). A 401 means no/invalid key. A 403 means a path your
key cannot use; its body lists `allowed_get` and `allowed_post` — that body
is the API surface, there is no other discovery document. Do not probe.

======================================================================
1. THE SHAPE OF WHAT WE COLLECT
======================================================================

  PROJECT   the unit of everything. A client or a topic. Has an integer
            project_id (P). Every post, list, source and label belongs to
            exactly one project. GET /api/projects lists them.

  Inside a project, per platform:

  X         WATCHLISTS. Kinds:
              xlist     an X List (list_id, owner_handle = the X account it
                        was made on, the only account that can edit members)
              query     a saved search
              keywords  a keyword set
              handles   a handle set
              links     specific post URLs from a Google Sheet — a different
                        consumer's data; visible, never mirror, never bind
            Each watchlist compiles to one or more STREAMS labelled
            wl:<watchlist_id>:<n> (n is a chunk, almost always 0).
            IMPORTANT: the number in wl:N:0 is a WATCHLIST id, not a
            project id. Never parse labels; read the join from
            /api/watchlists?project=P (each watchlist carries its streams[]).

  INSTAGRAM SOURCES: handles (type user), hashtags, or an account's home
            feed (type following), each with a numeric platform id once we
            have resolved it. Exposed to you as ONE pseudo-stream per
            project, labelled ig:<project_id>:0 (here the number IS the
            project id), and as one synthetic watchlist of kind "instagram"
            whose members[] are the handles.

  FACEBOOK  PAGES (handle per page), or "favorites" mode where the account's
            own feed is collected and the project has zero pages by design.

  Every post on every platform is served in ONE row shape (§4).

======================================================================
2. STRUCTURE ENDPOINTS — what exists (build your cards from these)
======================================================================

  GET /api/projects
    -> { projects:[{project_id, name, archived, watchlists, streams}] }
    Skip archived. Re-read on every refresh: we add projects, lists and
    sources without telling you.

  GET /api/watchlists?project=P          (project= required)
    -> { watchlists:[ …X rows…, …one instagram row when P has sources… ] }
    X row:        { watchlist_id, project_id, name, kind, list_id,
                    owner_handle, created_at, filters, paused, interval_s,
                    members:[{handle,display_name,user_id,added_at}]   (for
                              query/keywords/handles kinds — the terms),
                    streams:[{stream_id, label:"wl:<id>:<n>", paused,
                              tweets, …}], backfill:{…} }
    Instagram row:{ watchlist_id:-P, project_id:P, kind:"instagram",
                    platform:"instagram", name:"Instagram sources",
                    list_id:null, owner_handle:null, paused,
                    members:[{handle, display_name, user_id (numeric
                              Instagram id or null while we resolve it),
                              resolved, collector, type}],
                    streams:[{stream_id:-P, label:"ig:P:0", paused, tweets}] }
    Negative ids are synthetic on purpose: integers you can key on, never
    colliding with a real row, refused by every POST.

  GET /api/watchlist/xmembers?list_id=<list_id>      (keyed by LIST id)
    -> { members:[{user_id, username, display_name, avatar, fetched_ms}],
         count }
    The accounts inside an X List. Populated for every live list today
    (News 92, Rajasthan 115, ज़िलाध्यक्ष 40, MP 13, BhajanLal Media 2 28, …).
    Display only — do NOT turn it into a per-handle poll (§3, the August
    bug). For Instagram there is no members call: the watchlist row's
    members[] IS the account list.

  GET /api/streams
    -> { streams:[{label, source:"list:<id>"|<query>, paused, tweets},
                  …, {label:"ig:P:0", source:"instagram:project:P", paused,
                      tweets, platform:"instagram"}] }
  GET /api/streams/assignments
    -> { streams:[{stream_id, label, paused, list_id, tweets, projects:[P…]},
                  …, {stream_id:-P, label:"ig:P:0", paused, list_id:null,
                      tweets, projects:[P], platform:"instagram"}] }
    Either one maps every stream to its project. Prefer /api/watchlists.

  GET /api/ig/status?project=P
    -> { sources:[{label,type,value,platform_id,project_id,account,
                   assigned_account,enabled,collector}],
         totals:{posts,sources_enabled,newest_taken_at,sources_unresolved},
         paused, config:{interval_s}, accounts:[…], unassigned }
    The Instagram detail behind the pseudo-stream. Use `collector`; ignore
    accounts[], account, assigned_account, unassigned (our pool internals).

  GET /api/fb/status?project=P
    -> { sources:[{label,project_id,enabled,interval_s,last_run,posts,speed}],
         totals:{posts}, enabled, paused,
         config:{mode,default_interval_s,fav_interval_s,monthly_cap_gb,
                 use_proxy}, session, health }
    mode "favorites" = zero sources and posts > 0 is normal.

  Both status calls REQUIRE project=. Without it: {"error":"no project
  selected"} with HTTP 200 — check for an `error` key.

  Today (18 Sept): projects with Instagram sources are 13 "Varanasi" (10),
  14 "Rajasthan WatchList" (118, three X lists too), 17 "BhajanLal Ji
  Watchlist Media 2" (25 — all still resolving, so 0 posts for now; that is
  expected, not a fault). Project 9 "fb_pages" is Facebook favorites mode.

======================================================================
3. FETCHING POSTS — one loop, three platforms
======================================================================

  The loop (you already run it for X):

    cursor = 0                        # cold load; else your stored cursor
    loop:
      GET /api/tweets?project=P&since_collected_ms=<cursor>&limit=500
          [&platform=instagram]       # for Instagram; omit for X
      upsert rows by (platform, tweet_id)
      cursor = the response's cursor.since_collected_ms (= max collected_ms
               you COMMITTED)
      stop when the page is shorter than limit; sleep; resume from cursor

  X          GET /api/tweets?project=P&since_collected_ms=<c>&limit=500
  Instagram  GET /api/tweets?platform=instagram&project=P&since_collected_ms=<c>&limit=500
             (stream=ig:P:0 is accepted instead of platform=instagram and
              implies the project)
  Facebook   GET /api/fb/posts?project=P&limit=200[&since=72h|7d|ISO]
             -> { count, total, posts:[…] }   — no cursor yet, newest-first,
             200 cap. Cold load since=30d then narrower day windows while
             total > count; incremental since=72h every 30 min, upsert by
             tweet_id. Facebook posts land HOURS after posting (a page is
             rendered every 1–24h) so the overlap is required.

  Rules of the cursor (X and Instagram alike):
  - since_collected_ms is GAPLESS: it walks the order rows reached OUR
    database. Use it. Do not use since_id for mirroring — X indexes some
    tweets late, and Instagram posts are collected hours after posting, so
    an id cursor steps over rows. (since_id exists and works for "what is
    newer than this id"; that is a different question.)
  - While cursoring, rows come OLDEST-FIRST so the last row is a resumable
    position. The response's `cursor` object hands it back in both
    currencies; `has_more` says whether to page again. `total` is bounded
    to ~10 pages beyond the current one — page until short, do not trust it
    as an exact remainder.
  - Do not send `sort` while cursoring (ignored). `limit` clamps at 500.
  - Persist the cursor only after you COMMIT the rows.
  - Filters mean the same on both platforms: q=, author= (prefix match —
    author=ani also matches ani_digital; scope by project, not by author),
    min_likes=, min_views=, since=24h|7d, from_date=/to_date=YYYY-MM-DD,
    has_media=1.
  - Instagram without project= (and without stream=): an EMPTY page with a
    `note` and HTTP 200 — no `error` key.

  Per-list pulls, if you ever bind a single list rather than a project:
    X          add &stream=wl:<watchlist_id>:0 (one pull per stream label in
               that watchlist's streams[])
    Instagram  &stream=ig:P:0 is the whole project — Instagram has no
               per-list granularity beyond the project; &username=<handle>
               exists on /api/ig/posts for debugging, not for ingestion
  Project scoping is what tracks membership for you: handles come and go on
  an X List, we add and remove Instagram sources, and a bound project keeps
  flowing with its current members with no change on your side. NEVER
  enumerate a handle list and poll per author — that was the August bug
  that lost half of the News feed.

  What NOT to call for posts:
  - POST /api/fetch — the ONE write your key has. It triggers a LIVE fetch
    against X and spends our real rate-limit budget. Steady-state ingest
    calls it zero times. If you keep a "Fetch now" button for X, set the
    client timeout well above a live fetch — 5,064 abandoned calls in
    August cost us quota and returned you nothing.
  - /api/ig/fetch, /api/ig/source, /api/ig/control, /api/ig/settings,
    /api/fb/* POST — dashboard-only, 403 by design. No Fetch now for
    Instagram or Facebook; our collectors pace themselves like a person.
  - /api/live (SSE) — X only, starts from NOW, never replays backlog. Optional
    for latency on top of the cursor, never the source of truth.
  - /api/ig/posts — still works (newest-first on id, 72h-window loop from
    the 2 Sept note). Superseded by the Instagram stream above; do not run
    both.

======================================================================
4. THE ROW SHAPE (identical across platforms)
======================================================================

  {
    platform:            "x" | "instagram" | "facebook"   (X rows may omit it
                         on /api/tweets — absent means X)
    tweet_id:            STRING. The post id. Exceeds JS safe integer range —
                         never parse as Number. Dedupe on (platform, tweet_id).
    url, text,
    created_at:          post time, ISO UTC     created_ms:   same, epoch ms
    collected_at:        when WE saved it        collected_ms: the cursor field
    author_username, author_display_name (= handle on Instagram),
    author_id:           numeric platform user id ("" while unresolved on IG)
    author_avatar:       url or null
    media:               [{type: photo|video|album|other, url, thumb}]
                         X and Facebook media do not expire (Facebook's are
                         re-hosted on OUR host, /media/fb/…). Instagram media
                         are CDN links that EXPIRE (oe= param): render
                         promptly, mirror the thumb bytes at ingest if you
                         need them beyond a few days.
    like_count, reply_count (comments), retweet_count (shares; null on IG),
    view_count (plays on IG; null on FB),
    source:              our cross-platform person label
    streams:             ["wl:<id>:0", …] on X, ["ig:P:0"] on Instagram
    label, label_source, label_ms:   our content label (key only; null = not
                         classified yet). Look the name up in your own copy
                         of the vocabulary.
    …plus, on X rows only, the raw tweet columns you already store.
  }
  Instagram and Facebook metrics are frozen at first collection.

======================================================================
5. BINDING — how a Watch-Tower project takes data
======================================================================

  Bind your project to our project P. That is the relation. Then per
  platform you either take everything in P (mode "all" — resolved on every
  run, never a snapshot of ids, so a list we add next week flows without
  anyone touching the binding) or a selected set of streams (X: watchlist
  ids → their stream labels; Instagram: the one ig:P:0; Facebook:
  whole-project only until we add a page filter). Unbind stops the pull for
  that (project, platform). A project bound for X and Instagram receives
  both; bound for one, receives one. Treat ig:P:0 exactly as one more
  stream of P in whatever you do today for an X list: bind, unbind,
  mirror, backfill from your own copy.

  SHARED WATCHLISTS (2026-09-25). One X watchlist can now be used by
  several of our projects: it is created in one project (its owner) and
  any other project can add it. Nothing is copied — one list, one set of
  wl:<id>:<n> streams, fetched ONCE, and its posts show in every project
  that added it. What that means for you:

    * /api/watchlists?project=P keeps its shape. A shared list is simply
      listed under EVERY project that uses it, with the SAME watchlist_id
      and the same streams[]. `project_id` on the row stays the OWNER's id
      (where it was created), so under P it may not equal P.
    * Key your cards on (project_id_you_asked_for, watchlist_id) — never
      on watchlist_id alone, or the same list bound in two of your
      projects collides. This is the one thing to check in your code.
    * Additive keys on the row, ignore if you like:
        owner_project_id  int   the project that created it (= project_id)
        owner_project     str   its name
        projects          [{project_id, name, owner}]  everyone using it
        shared            bool  used by more than one project
    * /api/projects → `watchlists` counts the lists a project USES,
      including added ones. /api/streams/assignments → `projects[]` on a
      shared list's streams names every project, which it always could.
    * A post collected through a shared list is one row in /api/tweets;
      `streams` on it names the wl:<id>:<n> stream as before, and the
      post appears under ?project=P for each project using the list. If
      you mirror two of our projects that share a list, you will receive
      the same tweet_id once per project — dedupe on tweet_id per your
      project, exactly as a tweet matched by two lists already required.
    * Nothing changes for Instagram or Facebook: sources there are still
      per-project.
  Until the first list is actually shared, none of this is observable;
  we will tell you before that happens.

  REQUEST — SEVERAL OF OUR LISTS PER WATCH-TOWER PROJECT (2026-09-25).
  Today a Watch-Tower project shows ONE of our watchlists for a bound
  project. We would like it to show ALL of them, with the choice on your
  side. Please build this — and, agent: before you start, get Ankit sir's
  go-ahead on the shape below, since it changes what his projects show.

    What we want:
      * When a Watch-Tower project is bound to our project P, list EVERY
        row of GET /api/watchlists?project=P — every X list P created or
        added, plus the Instagram row — not just one.
      * Ankit sir picks which of them the Watch-Tower project follows:
        add / remove from your side, any time, any number. Nothing to ask
        us for; nothing changes on our side when he does.
      * Store the choice as (our project_id, our watchlist_id) pairs.
        Never store handles or stream labels as the binding: we add and
        remove handles inside a list, and a list re-chunks its wl:<id>:<n>
        streams on its own — read streams[] fresh from the row each run.
      * Pull per selected list exactly as today: for each label in that
        row's streams[], GET /api/tweets?project=P&stream=<label>&
        since_collected_ms=… . A list selected in two of your projects is
        one list on our side; pull it once per your project and dedupe on
        tweet_id within each.
      * Re-read /api/watchlists?project=P on every refresh so a list we add
        to P shows up as selectable without anyone re-binding; a list we
        delete disappears from the picker and its selection can be shown
        as "no longer in the collector" rather than erroring.
      * Cards: one per selected list, keyed on (project_id, watchlist_id),
        with `upstream posts` = sum of streams[].tweets for that list.

    What limits it: the picker can only ever offer lists that exist on
    OUR side under that project — created there, or shared into it from
    another project with the new "Add existing…" control in our
    dashboard. If a list Ankit sir wants is missing from the picker, it
    has to be added to project P in the collector first; that is a
    request to us, not something to work around by parsing labels.

======================================================================
6. NUMBERS ON YOUR CARDS
======================================================================

  upstream posts        X: sum of streams[].tweets on the watchlist;
                        IG: `tweets` on ig:P:0; FB: totals.posts
  handles seen (24h)    distinct author_username in YOUR mirror, last 24h
  posts / 24h           count in YOUR mirror, last 24h (stop sampling our
                        pages for this — you have the rows)
  in our mirror         your total for (P, platform)
  Show upstream vs mirror side by side so a gap is visible. Instagram
  counts lag X: our Instagram collector runs on a human pace, daytime IST.

======================================================================
7. VERIFY, THEN REPORT BACK
======================================================================

  1. GET /api/status → 200 with the key.
  2. GET /api/streams → ig:13:0, ig:14:0, ig:17:0 present with
     platform:"instagram".
  3. GET /api/watchlists?project=14 → three X lists PLUS one kind:"instagram"
     row with ~118 members. project=17 → one X list (watchlist 81,
     list 2100208316253352362) plus an instagram row, 25 members,
     resolved:false. Your card for project 17 must show watchlist 81 — if it
     shows list 2093579659413979567 you are still parsing labels (that is
     watchlist 17, in project 14).
  4. GET /api/tweets?platform=instagram&project=14&since_collected_ms=0&limit=5
     → 5 rows, oldest-collected first, streams:["ig:14:0"], tweet_id strings,
     a thumbnail that opens.
  5. Cold-load project 14 on Instagram until a short page: your row count
     equals `tweets` on ig:14:0 (± what arrived during the walk).
  6. Run the incremental loop 30 min in Indian daytime: new Instagram rows
     within ~5 min of our newest collected_ms moving.
  7. Live Feed → Instagram filter on the bound project renders thumbs and
     captions.
  8. Your /api/fetch call count in steady state is zero; your 499 count is
     zero.
  Report per bound project and platform: your row count vs ours, and the
  distinct `source` values you hold.
```

---

## Open items on our side

- **Deploy is manual until the workflow secrets exist.** `Deploy to VPS`
  runs green in 4 s because it skips (DEPLOY_HOST / DEPLOY_SSH_KEY unset).
  Runs 15 and 16 both skipped. Either set the secrets or keep pulling by
  hand; the CHECKPOINT line saying a push deploys is wrong today.
- Facebook cursor + page filter (`fb:P:0`, `platform=facebook`) — same
  treatment as Instagram when they ask; the table already has `collected_ms`.
- `wl:15:0` is an orphan paused stream in `/api/streams`; cosmetic.
- `accounts[]` / `session` / `health` on the status endpoints are collector
  telemetry a consumer has no use for; consider trimming from the keyed view.
