# X Links Tracker — plan (Phase 1: X only)

*Written 8 Sep 2026 against the code as it stands (store.py, collector.py,
engine.py, sheets.py, web.py, frontend/src/views/Watchlists.jsx). Every
"already exists" claim below names the function it rests on. Build it in the
order of §9; each step keeps `tests/test_all.py` green offline.*

> **Status (9 Sep 2026): built, steps 1–6.** Where the code differs from
> the text below, the code is right and the difference is deliberate:
> the links stream is `watched=0` (not merely an empty query) so the
> watcher's startup scan never logs it; a transient miss backs off on a
> separate `last_attempt_ms` clock so `last_refresh_ms` keeps meaning "the
> numbers were read"; `/api/links` filters by `status` and sorts by
> `views|likes|posted|refreshed`; the panel table omits the quotes column
> (it is in the API and the row's tooltip); the first sync of a bound sheet
> runs on the dashboard request so the Add modal can report what it found.
> Still to do on the server: `tools/links_probe.py <url>` once, then bind
> the real sheet. `CHECKPOINT.md` 2026-09-09 has the full list.

---

## 0. What this is, in one paragraph

A fourth kind of X watchlist, **`links`**. Instead of handles or keywords it
holds **specific post URLs**. The URLs arrive from a Google Sheet (every tab
of the sheet, automatically, new rows picked up without anyone touching the
dashboard) or are pasted in. The collector fetches each post **once a day**
(12h / 24h / 48h, per watchlist) and **overwrites** its engagement counters in
place — likes, retweets, replies, quotes, **views (reach)**, bookmarks. It
keeps **no history**: the consumer that reads this data keeps its own. That
consumer is **not Watch-Tower**; it is a second tool, pulling a snapshot from
a new key-readable endpoint.

**What it is not:** it is not a stream, it does not search, it does not
follow the author, it does not deliver to Watch-Tower, it does not snapshot.

---

## 1. Decision: a links panel, not "add the author to the handles watchlist"

**Build the links panel.** Adding the post's author to a `query` (handles)
watchlist does not do the job, for three reasons that are about mechanics,
not taste:

1. **A handles watchlist never re-fetches an old post.** It polls
   `from:handle` search forward from a watermark (`collector.poll_once`,
   "walk pages until we reach known ground"). A post from last week is below
   the watermark and is never seen again, so its counters never refresh. The
   whole feature is "re-fetch *these* posts"; only a per-post fetch does that.
2. **Wrong data, wrong budget.** A handle stream collects *everything* the
   author posts from now on, every few minutes, on the SearchTimeline rate
   bucket the watchlists already compete for. A links list needs one
   `TweetDetail` call per post per day, on a *separate* bucket.
3. **The consumer asked for these posts, not this author.** Their tool wants
   reach for a known set of URLs. Mixing in the author's other posts makes
   them filter on our side of the fence.

What the handle idea was really asking for — "see it the way I see handles"
— the panel gives for free: every link row shows the author's avatar and
`@handle` (the fetch returns them), and the panel has a **group-by-author**
toggle. If an operator later wants to *also* follow an author, the row gets
a one-click "watch this handle" that adds it to a handles watchlist — a
complement, never the mechanism.

---

## 2. Data model (all additive; migrate-on-open like the existing tables)

```sql
-- One sheet bound to one project. A project may bind several sheets.
CREATE TABLE IF NOT EXISTS link_sheets (
  link_sheet_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id     INTEGER NOT NULL,
  sheet_id       TEXT NOT NULL,            -- the /d/<id>/ part; sheets.sheet_id() parses it
  title          TEXT,                     -- spreadsheet title, for the panel
  sync_every_s   INTEGER NOT NULL DEFAULT 600,
  last_sync_ms   INTEGER,
  last_error     TEXT,
  paused         INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  UNIQUE(project_id, sheet_id)
);

-- watchlists: kind gains 'links'. New nullable columns:
--   link_sheet_id  INTEGER   -- NULL = a pasted-only links watchlist
--   sheet_gid      INTEGER   -- the tab's numeric sheetId (survives a rename)
--   sheet_tab      TEXT      -- the tab's title at last sync (display only)
--   refresh_every_s INTEGER NOT NULL DEFAULT 86400
--   last_refresh_ms INTEGER

CREATE TABLE IF NOT EXISTS watchlist_links (
  watchlist_id    INTEGER NOT NULL,
  tweet_id        INTEGER NOT NULL,        -- parsed from the URL at insert
  url             TEXT NOT NULL,           -- as given (kept for the panel + API)
  added_at        TEXT NOT NULL,
  added_via       TEXT NOT NULL,           -- 'sheet' | 'manual'
  sheet_row       INTEGER,                 -- 1-based, when added_via='sheet'
  status          TEXT NOT NULL DEFAULT 'pending',
                  -- 'pending'     never fetched, or last attempt was a transient error
                  -- 'ok'          last fetch returned the post
                  -- 'unavailable' deleted / protected / suspended — keep the row + last counters
                  -- 'removed'     gone from the sheet or removed by hand — stop refreshing
  status_note     TEXT,
  fail_streak     INTEGER NOT NULL DEFAULT 0,
  last_refresh_ms INTEGER,
  refresh_count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (watchlist_id, tweet_id)
) WITHOUT ROWID;
```

**The post itself stays one row in `tweets`** (RULEBOOK §2 "one post shape").
A links watchlist compiles to exactly one stream, `wl:<id>:0`, with
`query=''` and no `list_id`. That empty query is what keeps the poll
scheduler off it: `Collector.discover_new_streams` only picks up streams
`WHERE query != '' OR list_id IS NOT NULL`. The stream still gets a
`project_streams` row, and every refresh writes a `tweet_hits` row for it —
so the post shows in the project's Live Feed and in `/api/tweets?project=P`
through the same join everything else uses, no special case.

**Counters overwrite; nothing else moves.** `store.upsert_tweets` already
does precisely this on `ON CONFLICT(tweet_id)`: `reply_count`,
`retweet_count`, `like_count`, `quote_count`, `view_count`, `bookmark_count`
and `last_seen_at` update; `collected_ms` / `collected_at` / `lag_ms` are
frozen at first sight. One addition, guarded so a bad read can never blank a
good value:

```sql
author_followers = COALESCE(excluded.author_followers, tweets.author_followers)
```

Followers drift too, and reach math on the consumer's side wants the current
number. The COALESCE keeps R5 ("null is unknown, not zero") true.

**No history table.** Deliberately. The consumer owns history; we own the
latest true value. `refresh_count` and `last_refresh_ms` on the link are
operational telemetry, not history.

---

## 3. Reading the sheet — every tab, automatically

**Route: the service-account mode `sheets.py` already has** (`MODE_API`,
`GOOGLE_SHEETS_CREDENTIALS`, `access_token()`, `_get_values()`). Reading is a
subset of the scope we already request (`spreadsheets`). The operator shares
the sheet with the service-account email — **Viewer is enough** — exactly the
step `sheets.check_api_access()` already diagnoses with a 403 hint. Two calls
per sync:

```
GET  /v4/spreadsheets/{id}?fields=properties.title,sheets.properties(sheetId,title,hidden)
GET  /v4/spreadsheets/{id}/values/'<tab>'!A:Z         (one per non-hidden tab)
```

**Tab = watchlist.** Each non-hidden tab becomes one `links` watchlist named
after the tab, keyed on the tab's numeric `sheetId` (`sheet_gid`) so a rename
keeps the watchlist and its links; a new tab appears as a new watchlist on
the next sync without anyone clicking anything. The tab name flows through
to the consumer (`tab` field in §6), so their groupings survive the trip.

**Any cell, any column.** Every cell of the tab is scanned for X status URLs
with the parser in §4; the operator's sheet layout is not our business. Cells
with no X link are ignored; a per-sync count of "skipped (not an X post
link)" is reported so an Instagram link pasted by mistake is visible, not
silent. (Those become the later IG/FB phases.)

**Adds, never deletes.** A link that vanished from the sheet gets
`status='removed'` and stops refreshing; its `tweets` row stays. Same
invariant as "shrinking a watchlist pauses, never deletes."

**Cadence.** Every 10 minutes per sheet (two cheap reads), plus **Sync now**
in the panel. New links are `pending`, and pending links are fetched on the
next links tick (§5) — so a row pasted into the sheet has its first numbers
within a couple of minutes. That is the "automatically adds and starts
scraping" in the request.

**Apps-Script mode.** Not in Phase 1. If the deployment only has the pasted
script and no service-account key, `doGet` grows an
`?action=links&token=…` branch and the operator re-pastes the script — about
a day, because it is a second transport with its own error surface. Say so
before starting if that is the case.

---

## 4. The fetch: one `TweetDetail` per link

**URL parser** (`links.py`, pure, offline-testable):
`x.com | twitter.com | mobile.twitter.com | vxtwitter.com | fxtwitter.com`
`/<anything>/status/<id>[/photo/n][?…]` → `tweet_id`. `t.co/…` short links are
resolved with one `HEAD` (follow redirects, no auth) at insert time; a link
that does not resolve to a status URL is skipped with a note.

**Transport**: `Engine.tweet_details(tweet_id)` in `engine.py`, a thin wrapper
over twscrape's `api.tweet_details()` (GraphQL `TweetDetail`). twscrape has
no batch-by-ids op, so it is one call per link. Step 3 of §9 verifies the
call still works through the project's pinned `twscrape==0.20.0` and its
`install_xclid_shim()` — `tweet_details` rides the same `_gql_item` path as
the search ops the shim already covers, so expect yes, but prove it with one
real link before writing the loop.

**Budget.** `TweetDetail` is its own rate bucket (~150 requests / 15 min per
account), so it never slows watchlist polling. Refreshed once a day and
trickled (§5), 1,000 links is ~1,000 requests spread over hours across the
pool — noise.

**Outcomes → link status:**

| Fetch result | `status` | `fail_streak` | next attempt |
|---|---|---|---|
| post returned | `ok` | 0 | `refresh_every_s` |
| tombstone / not found / protected / suspended | `unavailable` | +1 | next cycle; after 3 in a row, weekly |
| rate-limited / account trouble / network | unchanged (`pending` or `ok`) | +1 | next cycle |
| link gone from sheet or removed by hand | `removed` | — | never |

An `unavailable` link **keeps its last counters** — a deleted post's final
numbers are the evidence the consumer will want.

---

## 5. The refresh loop — a third clock in the X watcher

**Where:** inside `Collector.run_forever` (`collector.py`), beside the two
clocks it already runs — forward polls (`tasks`) and backwards backfill
(`bf_tasks`). Add `links_tasks` on the same pattern: tracked separately so a
slow trickle can never take a forward poll's slot, sharing `self.sem` (the
resource that actually needs protecting) and gated by the same global pause
(`self._paused()` — "stop collecting" means stop, not "stop the half you can
see").

**Why not its own service:** it would open a second twscrape session over
the same `accounts.db` and fight the pool for the same accounts. The X
watcher already owns the engine, the account pool, the rate awareness and the
activity log. One process, one pool.

**Tick (every 30 s):**
1. Re-read links watchlists (`kind='links'`, not paused) — same
   "read the dashboard's settings every cycle, never once at startup"
   discipline as `apply_settings`.
2. Due = every `pending` link, plus every `ok`/`unavailable` link whose
   `last_refresh_ms + refresh_every_s <= now` (weekly for retired
   unavailables).
3. Take the oldest-due first, **one link at a time, 6–10 s apart with
   jitter** — a trickle, never a burst. 1,000 links finish in ~2 h, well
   inside a 24 h window.
4. Each fetch: `begin_poll(stream_id, kind='links')` → `tweet_details` →
   `upsert_tweets([(tweet, page, 'result', None)], stream_id, poll_id)` →
   `finish_poll(...)` → `mark_link(...)`. Batch the poll row per tick, not
   per link, so the `polls` table does not grow by 1,000 rows a day.

**Refresh now** (panel button) sets a per-watchlist grant the tick honours
within 30 s — the same shape as backfill grants
(`store.streams_with_backfill`), so it needs no new plumbing.

**What the Activity Log sees:** one line per tick with counts
(`[links] wl:15:0  fetched 40  ok 38  unavailable 2  next in 24h`), and the
existing `/api/streams` telemetry shows the links stream's last poll like
any other.

---

## 6. The consumer's API — a snapshot, not a cursor

The other tool wants "the latest numbers for every link, once a day". It does
not want a cursor: the cursor contract (`since_collected_ms`) is for
*gapless mirroring of new rows*, and a refreshed post's `collected_ms` never
changes (R3), so a cursor would never re-deliver it. This endpoint is a
**full snapshot by design**, paged by offset:

```
GET /api/links?project=P[&watchlist=W][&status=ok][&limit=500][&offset=0]
Authorization: Bearer <key>
```

One object per link, the link's own fields joined with the post's current
row:

```json
{ "watchlist_id": 15, "watchlist": "Campaign A", "tab": "Campaign A",
  "url": "https://x.com/nasa/status/1789…", "tweet_id": "1789…",
  "status": "ok", "status_note": null,
  "added_at": "2026-09-08T10:00:00Z", "added_via": "sheet",
  "last_refresh_ms": 1788440000000, "refresh_count": 12,
  "created_at": "2026-09-01T08:12:00Z",
  "author_username": "nasa", "author_display_name": "NASA",
  "author_followers": 87000000, "author_avatar": "https://…",
  "like_count": 1200, "retweet_count": 300, "reply_count": 80,
  "quote_count": 25, "view_count": 410000, "bookmark_count": 90,
  "text": "…", "lang": "en", "media": [ { "type": "photo", "url": "…" } ] }
```

Ids are strings (R2). Counters are numbers or `null` for unknown (R5).
Shape is additive from day one (R4). Sort is `watchlist_id, added_at,
tweet_id` — stable, so offset paging is safe between refreshes. Total in a
`meta.total` so the consumer knows when it has everything.

`/api/links` goes into `API_KEY_READ_PATHS` (R7: the allowlist is the whole
surface). `/api/tweets?project=P` keeps working unchanged and also carries
these posts (through `tweet_hits`), but without `status` / `tab`, so the
consumer should use `/api/links`.

**Project-locked key — do this, the rulebook already said so.** BLUEPRINT
§9 parks "project-locked API keys (**mandatory before a second consumer**)".
This tool is the second consumer. Minimal shape, ~40 lines at the key gate
in `web.py` (around the `allowed = …` check):

```
API_KEYS_SCOPED="<key>=<project_id>[,<key>=<project_id>…]"    # .env
```

A scoped key is refused (403, with the reason in the body) on any request
whose `?project=` is not its own, on any path that is not project-scoped, and
on `/api/fetch`. Watch-Tower's keys in `API_KEYS` are untouched. Constant-time
compare like `_valid_api_key`.

**Keep it in its own project.** Watch-Tower mirrors *whole* projects
(`/api/tweets?project=P` with a cursor). A links watchlist created inside a
project that is bound on their side would flow to them. Make a dedicated
project (e.g. "Reach Tracker") for links; leave it `unused` on their
Collector page. Nothing in the code needs to enforce this — but the panel
should say it in the Add modal, once.

---

## 7. The panel (`frontend/src/views/Watchlists.jsx`)

Master-detail, like everything else in that view; new controls go in the
platform's detail panel, never on the main surface (BLUEPRINT §9).

**Add modal** — `PLATFORM_KINDS.x` gains
`["links", "Post links (from a Google Sheet, or pasted)"]`. Form: sheet URL
*or* a paste box (either, both allowed). With a sheet: on save it syncs at
once and reports *"3 tabs → 3 watchlists · 128 links found · 4 skipped (not
X post links)"*. One line of guidance: *"Use a project that is not bound to
Watch-Tower."*

**Sidebar card sub-line:** `128 links · every 24h · sheet tab "Campaign A"`.

**`LinksDetail`** (beside `XDetail`):
- Header: sheet title + tab, last sync, next refresh, **Sync now**,
  **Refresh now**, cadence `12h / 24h / 48h`, Pause. Sync/refresh errors in
  an honest banner, the same way `FbHealthBanner` does it.
- **The links panel** — one row per link: avatar + `@handle` · first ~80
  chars of text · posted date · ❤ 🔁 💬 🔗 👁 current counters · status pill
  (`pending` grey / `ok` green / `unavailable` amber / `removed` muted) ·
  "refreshed 2h ago". Sort by views / likes / added; filter by status;
  **group by author** toggle (the "see it like handles" view). A row's
  overflow menu: open on X · watch this handle (adds to a handles watchlist)
  · remove (soft).
- Paste-more-links box at the bottom (manual adds live alongside sheet adds;
  `added_via` tells them apart in the row).

**Live Feed:** nothing new. The posts arrive through `tweet_hits` with
stream label `wl:<id>:0`; the existing per-project filters already cover
them.

---

## 8. Rules touched, and what to write into RULEBOOK

Respected as-is: one post shape (§2) · `collected_ms` frozen (R3) ·
additive shape (R4) · numbers-or-null (R5) · allowlist is the surface (R7) ·
reads are local and cheap (R8) · shrink pauses never deletes · settings
re-read every cycle · global pause means stop · pre-commit: RULEBOOK +
CHECKPOINT + BLUEPRINT in the same commit.

New rule to add (one paragraph): **A `links` watchlist re-fetches the post
itself, on its own `TweetDetail` budget, and overwrites counters in place.
The collector keeps no counter history — the consumer does. A link that
leaves the sheet is marked, never deleted; an unavailable post keeps its last
counters.** Plus the `author_followers` COALESCE carve-out under the
upsert's "counters update, first-sight fields never" note.

`WATCH_TOWER.md` needs one sentence in §7/§9: links projects exist, are not
for them, and a bound project must not host one.

---

## 9. Build order (each step: offline tests green, one new test)

| # | Step | Files | Size |
|---|---|---|---|
| 1 | `links.py`: URL parser, t.co resolver, sheet reader (tabs + cells), `plan_refresh(links, now)` — all pure, fixture-tested | `links.py`, `tests/test_links.py` | ½ day |
| 2 | Store: `kind='links'`, tables + migration, `bind_sheet / sync_sheet_result / add_links / links_due / mark_link / links_snapshot`; compile to `wl:<id>:0` with empty query | `store.py` | ½ day |
| 3 | `Engine.tweet_details`; **live check with one real link** through the pinned twscrape + shim; then the links clock in `Collector.run_forever` | `engine.py`, `collector.py` | ½ day |
| 4 | API: `/api/links` GET; `/api/watchlists` accepts `kind=links` + `sheet`; `/api/watchlists/links` POST (paste / remove); `/api/watchlists/sync`, `/api/watchlists/refresh`, `/api/watchlists/interval` for links; scoped keys | `web.py`, `.env.example` | ½ day |
| 5 | Panel: `PLATFORM_KINDS`, Add modal branch, `LinksDetail`, sidebar sub-line; `npm run build`, commit `dist/` | `Watchlists.jsx`, `api/` | 1 day |
| 6 | Docs in the same commits: RULEBOOK rule, BLUEPRINT §2/§3/§9, CHECKPOINT, WATCH_TOWER one line, README one line; `LINKS_CONSUMER_HANDOVER.md` for the other tool (endpoint, fields, "no history, pull daily", the scoped key) | docs | ½ day |

≈ 3½–4 days. Steps 1–4 are usable from the API before the panel exists,
which is the order to ship if the consumer is waiting.

---

## 10. Defaults I am taking (say so if any is wrong)

- **Tab = watchlist**, keyed on the tab's gid, named after the tab.
- **Sheet access = service account, shared as Viewer** (the mode sheet
  delivery already uses). Script-only deployments: +1 day, see §3.
- **Every cell of every non-hidden tab** is scanned; non-X links are counted
  as skipped, not errors.
- **Refresh 24h by default**; new links fetched within ~1 min of sync; sheet
  re-read every 10 min.
- **Dedicated project for links**, so Watch-Tower never mirrors it.
- **Counters overwrite in place, no history.** `view_count` is the reach
  figure; `author_followers` is refreshed with a null-guard.
- **Consumer pulls a full snapshot from `/api/links` with a project-locked
  key**; no push, no webhook, no cursor.
