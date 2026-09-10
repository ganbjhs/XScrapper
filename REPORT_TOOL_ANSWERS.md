# report.vedictech.in ⇄ scraper.vedictech.in — the integration contract

Written for the Collector agent. Answers the 21 questions that were asked
(Part 5, at the end) and then specifies the thing that was not asked but is
needed: **how the two systems connect, how the report tool fetches, and what
link-wise (watchlist) scraping for X has to look like on both sides.**

Read from the code at `VedicReport` v3.3.0 (`VERSION`), git head `493b51b`.
Every path, table, column and route is quoted as it exists today. Where a
thing is absent it says **does not exist**.

**The verdict, up front: the report tool PULLS. Do not build a push.**
The pull machinery already exists and runs on a scheduler (`portal/scraper.py`
+ `webapp/portal_publish.py: sync_client`). There is no inbound endpoint that
writes metrics, and the tool's bearer API (`/v1`) demands an `X-Actor` header
tied to a human account, so it cannot serve a server-to-server push without
new scopes. Building a push means building it from nothing; making the pull
work means closing eight named gaps, listed in Part 4.

**Two apps, two SQLite files, one host.**

| | Report Maker ("the tool") | Client Portal ("the dashboard") |
|---|---|---|
| code | `webapp/` | `portal/` |
| container | `web:8000` | `portal:8020` |
| public URL | `https://report.vedictech.in/` | `https://report.vedictech.in/portal/` |
| database | `data/app.db` (`webapp/jobs/store.py`) | `data/portal.db` (`portal/schema.py`) |
| writes `post_metrics` | yes, via `webapp/portal_publish.py` | never — opens the file `mode=ro` |

---

# Part 1 — How the connection is established

## 1.1 The shape of it

```
Google Sheet  ──read by──▶  COLLECTOR project P  ──served at──▶  GET /api/links?project=P
 (day tabs,                  (X watchlist:                              │
  headings)                   only links from                           │ Bearer <project-locked key>
      │                       the designated tab)                       │ once a day
      │                                                                 ▼
      └────────read by──────────────────────────────────▶  REPORT TOOL  client_sources row
                (independently, for the report PDFs)        └──▶ post_metrics + daily snapshots
                                                                  └──▶ Client Portal dashboard
```

Both systems read the same Google Sheet, for different reasons, and neither
tells the other about it. The Collector reads it to know **which links to
scrape**. The tool reads it to know **which links to screenshot for the
report**. They meet on the link itself.

## 1.2 What a staff member does, in order

1. **In the report tool** — `https://report.vedictech.in/` → the project already
   exists and has its Google Sheet attached as a `sources` row
   (`purpose = dashboard` or `both`). Nothing to change.
2. **In the Collector** — create project `P`, point its **X watchlist** at the
   same Google Sheet and name the tab that holds the links (Part 2), and issue
   a **project-locked, read-only API key** for it.
3. **Back in the report tool** — `/admin/clients` → pick the client → **Data
   source — the scraper** → fill in:
   - *Signature name*: e.g. `varanasi-collector` — sent as `X-Signature-Name`, so
     the Collector can log which consumer called.
   - *API URL*: `https://scraper.vedictech.in/api/links?project=7`
   - *Auth*: `Authorization: Bearer`
   - *API key*: the project-locked key. Stored sealed
     (`client_sources.api_key_enc`, `portal/secretbox.py`, under
     `PORTAL_KEY_SECRET`) and never echoed back by any route.
4. **Test connection** → the tool calls the Collector's handshake endpoint
   (§1.4) and shows *"Connected to project 7 'Varanasi Campaign' · watchlist
   tab 'X Links' · 224 links · last refresh 06:12 IST"*. That is the moment the
   staff member knows they wired the right sheet to the right client.
   **This endpoint does not exist yet — build it. It is the single highest-value
   thing on the Collector side**, because today a wrong key or wrong project
   surfaces only as an opaque `HTTP 401` hours later, inside a scheduled sync
   nobody is watching.
5. **Sync now** → first import. From then on the scheduler runs it daily.

## 1.3 Exactly what the tool sends (this is not negotiable — it is existing code)

`portal/scraper.py: fetch_raw()`, verbatim behaviour:

```
GET <base_url>                       ← {from} {to} {date} are substituted if present; nothing else is
Accept: application/json
User-Agent: VedicReport-portal/1.0
Authorization: Bearer <key>          ← when auth_style = bearer
X-API-Key: <key>                     ← ALSO sent when auth_style = bearer. Both headers arrive together.
X-Signature-Name: <signature_name>
```

- **GET only. No body. No cookies.**
- **Both `Authorization` and `X-API-Key` carry the same key** when auth style is
  `bearer`. Do not reject a request for presenting two credentials — read
  `Authorization` and ignore the other.
- Alternative styles the admin UI offers: `x-api-key` (only that header), or
  `query:api_key` / `query:key` / `query:token` (appended to the URL).
  `routes_clients.AUTH_STYLES` is the closed list.
- **Timeout 20 s** (`_TIMEOUT`). A page that takes longer fails the whole sync.
- **Response cap 25 MB** (`_MAX_BYTES`). Over that: `"The scraper's answer is
  larger than 25 MB — narrow the date range"`.
- **TLS is verified** (`ssl.create_default_context()`). A valid certificate is
  required — Let's Encrypt is fine, self-signed is not.
- **No retry, no backoff.** One failed request fails that source's sync; the
  next scheduled run tries again. On `429` the tool does **not** honour
  `Retry-After`. Tell us your rate limit and we will stay under it rather than
  discover it.
- The tool has **no way to send a date range to your endpoint** unless the URL
  contains `{from}`/`{to}`/`{date}`. Your endpoint takes no dates, which is
  correct — see §3.3 on why a range would be wrong here anyway.

## 1.4 Handshake endpoint — **please build this**

```
GET https://scraper.vedictech.in/api/project?project=7
Authorization: Bearer <project-locked key>
```

`200`:

```json
{
  "project":   { "id": 7, "name": "Varanasi Campaign", "platform": "x" },
  "watchlist": {
    "sheet_url": "https://docs.google.com/spreadsheets/d/1AbC.../edit",
    "sheet_title": "Varanasi Daily Links",
    "tab": "X Links",
    "tab_mode": "fixed",
    "tab_gid": "0",
    "links": 224,
    "last_sheet_read_ms": 1757400000000
  },
  "counters": { "ok": 210, "pending": 6, "unavailable": 5, "removed": 3, "total": 224 },
  "last_refresh_ms": 1757486400000,
  "key": { "name": "report-tool", "scope": "read", "project_locked": true },
  "limits": { "max_limit": 500, "requests_per_minute": 60 }
}
```

Errors — a JSON body with a human sentence, please, because the tool puts the
text straight in front of an admin (`client_sources.last_error`, rendered on
`/admin/clients`):

| Code | When | Body |
|---|---|---|
| `401` | key unknown or revoked | `{"error": "Unknown or revoked key."}` |
| `403` | key is valid but not locked to that project | `{"error": "This key is locked to project 4, not 7."}` |
| `404` | no such project | `{"error": "Project 7 does not exist."}` |
| `409` | project has no watchlist configured yet | `{"error": "Project 7 has no X watchlist tab set."}` |
| `429` | rate limited | `{"error": "…"}` + `Retry-After` |

`403` and `409` are the two that save real time. A key silently returning
another client's links is the failure mode that matters most here — the tool
files whatever comes back under whichever client asked, with no cross-check.

---

# Part 2 — Link-wise X scraping (the watchlist)

The requirement: **the Collector scrapes only the posts that are in the Google
Sheet — nothing else.** X only for now; Facebook and Instagram later, along the
same contract.

## 2.1 Who owns what

| Concern | Owner | Why |
|---|---|---|
| Which links exist | **Collector** | It reads the designated sheet tab and holds the watchlist |
| Turning a link into an X post id | **Collector** | It must dedupe its own queue anyway |
| Scraping counters, author, media, text | **Collector** | It has the X session |
| Which day a link belongs to | **Collector**, from the tab name → `day` | The tool must not re-derive it |
| Which heading a link sits under | **Collector**, from the sheet → `group` | Becomes the dashboard's category |
| Whether a client may see a row yet | **Report tool** (`visible_from = day + lag_days`) | Client-specific, 2 days by default |
| Day-over-day history | **Report tool** | The Collector explicitly keeps none |
| Screenshots and the PDF/PPTX report | **Report tool** | Unchanged, separate path |

## 2.2 Watchlist configuration on the Collector

Per project, stored on your side, surfaced through the handshake:

- `sheet_url` — the Google Sheet
- `tab_mode`:
  - `fixed` — one named tab is the watchlist (what was asked for)
  - `dated` — every tab whose name parses as a date is a watchlist, and each
    link's `day` is that tab's date
- `tab` / `tab_gid` — the tab, when `tab_mode = fixed`
- `platform` — `x` for now

**On `tab_mode = fixed`, `day` still has to be a real date per link.** The
dashboard is day-wise and groups on it (Part 5 §5). If one flat tab holds every
link, put a date column in the sheet and parse `day` from that; if there is no
date at all, fall back to the IST date the link was first seen and say so in
`status_note`. Do not send the scrape date as `day` — that would make every
historical post pile onto today.

## 2.3 Reading the sheet

- Take X links only (`x.com` / `twitter.com`, including `/i/web/status/…`).
  Ignore other platforms silently for now; when FB/IG arrive they will be
  separate rows with their own `platform` field, not a different endpoint.
- **The heading a link sits under becomes `group`.** In these sheets the
  headings are rows like *National X Influencers*, *Counter Comments*,
  *Hyper-local pages*, *3rd-party pages*. Carry the heading text through
  verbatim — do not tidy, translate or title-case it. The tool stores it raw in
  `post_metrics.category_raw` and lets each client relabel it in the admin UI
  (`clients.category_map`), so a "cleaned up" string breaks an existing mapping.
- A link removed from the sheet keeps its row and gets `status = "removed"` —
  it must not simply vanish, or the tool cannot tell "deleted from the sheet"
  from "the pull failed".
- Re-reading the sheet must be idempotent: same link, same `tweet_id`, same row.

## 2.4 `tweet_id` is the join key — please always send it

The tool currently dedupes on a **normalised URL**
(`post_metrics.post_url_norm`, `portal/util.py: norm_url`) and has no post id
column at all. That normalisation is lossy in a way that will bite us:

- all query parameters are dropped (for non-Facebook hosts)
- `twitter.com` → `x.com`, `www./m./mobile./mbasic.` stripped, trailing slash and
  fragment dropped
- **the path keeps its case** — so `x.com/User/status/123` and
  `x.com/user/status/123` are two different rows

Sheets are typed by hand, so both spellings will occur. We are adding
`post_metrics.tweet_id TEXT` and matching on it first, URL second (Part 4).

**Send `tweet_id` as a JSON string of decimal digits.** X ids exceed 2^53; the
dashboard parses responses with plain `JSON.parse`, so a bare number would be
silently rounded. Server-side Python and SQLite handle it fine either way — the
browser is the constraint.

---

# Part 3 — The API contract

## 3.1 The snapshot endpoint

```
GET https://scraper.vedictech.in/api/links?project=7&limit=500&offset=0
Authorization: Bearer <project-locked key>
```

`200`:

```json
{
  "project": 7,
  "total": 224,
  "limit": 500,
  "offset": 0,
  "items": [ { … row … } ]
}
```

**The array key must be one of `items`, `data`, `results`, `posts`, `records`.**
`portal/scraper.py: normalize_many()` looks for exactly those and raises
`"Expected a JSON array of posts (or {\"posts\": [...]})"` otherwise. If the
current build returns `{"links": [...]}` **the sync fails outright today** —
either rename to `items` (preferred, it is already accepted) or wait for the
tool-side change in Part 4 that adds `links`/`rows` to the list. A bare
top-level array also works.

## 3.2 The row

`R` = required, `W` = wanted (the tool will store and show it), `–` = accepted
and ignored for now.

| Field | | Type | Notes |
|---|---|---|---|
| `tweet_id` | **R** | string of digits | The join key. String, never a number. |
| `url` | **R** | string | The link as it appears in the sheet |
| `post_url` | **R** | string | Canonical `https://x.com/<user>/status/<id>` |
| `day` | **R** | `YYYY-MM-DD` | **IST date** the link belongs to (§2.2). Drives every day-wise view. |
| `group` | **R** | string | The sheet heading, verbatim → `category_raw` |
| `status` | **R** | `ok`\|`pending`\|`unavailable`\|`removed` | See §3.4 |
| `platform` | **R** | `"x"` | Explicit, so FB/IG can join later without a guess |
| `status_note` | W | string | Shown to staff when a row is skipped |
| `tab` | W | string | Which tab it came from — for debugging a wrong-sheet wiring |
| `created_at` | **R** | ISO 8601 UTC, `Z` | The post's own publish time |
| `text` | W | string | → `caption` |
| `lang` | W | string | New column |
| `author_username` | W | string | → `handle`, stored with a leading `@` |
| `author_display_name` | W | string | → `display_name` |
| `author_avatar` | W | url | → `avatar_url` |
| `author_followers` | W | number\|null | New column |
| `like_count` | **R** | number\|null | → `likes` |
| `retweet_count` | **R** | number\|null | → `shares` (shown as "Reposts" for X) |
| `reply_count` | **R** | number\|null | → `comments` (shown as "Replies" for X) |
| `view_count` | **R** | number\|null | → `views` |
| `quote_count` | W | number\|null | New column |
| `bookmark_count` | W | number\|null | New column |
| `media[]` | W | `[{type, url, thumbnail_url}]` | → `media_type` + `thumb_url`, first element |
| `last_refresh_ms` | **R** | number | Epoch ms of the last successful read. Drives §3.5 |
| `refresh_count` | W | number | |
| `added_at` | – | ISO 8601 | |

**Keep your names.** Six of them (`author_avatar`, `group`, `day`, `status`,
`tweet_id`, `quote_count`/`bookmark_count`) do not match the tool's current
`FIELD_MAP` and are being added to it — see Part 4.1. Do not rename anything on
your side to fit the old map; it is the map that is wrong.

**Counters: number or `null`, never `0` for unknown.** This is already right and
it matters more than it looks. `portal/util.py: to_int()` maps `null` → `None` →
a NULL column → the dashboard prints `—`. A `0` would be charted as a real zero
and would drag every average down. X shows nothing at all for a zero count, so
`null` is the honest value.

## 3.3 Paging

`offset` / `limit`, `total` in the envelope, **stable order across pages**
(order by `tweet_id`, or by `added_at, tweet_id` — anything deterministic;
"newest first" over a mutating table will duplicate and drop rows between
pages).

The tool does **one GET and ignores `total`** today — at `limit=500` everything
past page one is silently dropped. The offset loop is Part 4.1. Once it lands
it will: start at `offset=0`, keep going while it has fewer rows than `total`
and the last page was full, stop at 50 pages / 25 000 rows, and abandon the
sync if `total` changes mid-walk rather than stitch a torn read.

Each page must return **inside 20 seconds**. If 500 rows cannot, say so and we
will configure `limit=200`.

There is deliberately **no date range**. Your endpoint serves the whole
watchlist, which is correct — but note that `sync_client()` currently discards
scraper-only rows outside `[today-3, today]` (`PORTAL_SYNC_DAYS_BACK = 4`), so
on first import **every post older than four days would be dropped**. That
window is removed for watchlist sources in Part 4.3.

## 3.4 What each `status` means to the tool

| `status` | What the tool writes | What the client sees |
|---|---|---|
| `ok` | `post_metrics.status = 'ok'`, counters written | The post, with its numbers |
| `pending` | `status = 'ok'`, counters stay NULL | The post, with `—` for every number |
| `unavailable` | `status = 'skipped'`, `skip_reason = status_note` | Nothing — hidden by the `VISIBLE` clause |
| `removed` | `status = 'skipped'`, `skip_reason = 'removed from sheet'` | Nothing |

Today `_fold_scraper` writes `status = 'ok'` **unconditionally** and ignores the
field entirely — a deleted or protected post renders as a live post with stale
numbers. Fixed in Part 4.3.

## 3.5 `last_refresh_ms` and what "unchanged" should do

Once the snapshot table exists, a pull where `last_refresh_ms` has not moved
since the previous pull will:

- **still write the day's snapshot row** — a flat day is data, and a gap is not
  the same as "no change";
- carry `last_refresh_ms` and `refresh_count` onto that row, so a genuinely flat
  count is distinguishable from a stale read weeks later;
- **skip the content update** (`caption`, `avatar_url`, `thumb_url`,
  `posted_at`) — no point rewriting identical strings.

So: keep `last_refresh_ms` accurate even when the numbers happen to be
identical. It is the only signal that separates "we looked and nothing changed"
from "we could not look".

## 3.6 Cadence

- **Once a day, 03:30 IST**, after the previous IST day has closed. The tool's
  loop is hourly today (`PORTAL_SYNC_MINUTES = 60`) and fires on a fixed
  minute-of-epoch in UTC, unrelated to any timezone — Part 4.6 changes it to a
  local hour.
- One full walk of the watchlist per run.
- Please have the Collector's own sheet read + scrape finish **before** 03:00
  IST so the tool never pulls mid-scrape. If that is not possible, expose
  `refresh_in_progress: true` on the handshake and the tool will skip that day
  rather than snapshot a half-scraped watchlist.

---

# Part 4 — What changes inside the report tool

Our side, in build order. Listed so the Collector agent knows what is already
handled and does not work around it.

**4.1 `portal/scraper.py`**
- `FIELD_MAP`: add `author_avatar` to `avatar`; add `group` to `category`;
  add `quote_count`, `bookmark_count`, `author_followers`, `lang`, `tweet_id`,
  `status`, `status_note`, `day`, `last_refresh_ms`, `refresh_count`.
- `normalize()`: prefer a supplied `day` over the one derived from `posted_at`;
  carry `status` through instead of dropping it.
- `normalize_many()`: accept `items` (already), plus `links` and `rows`.
- `fetch()`: offset paging with the caps in §3.3.

**4.2 `portal/schema.py`** — bump `SCHEMA_VERSION` to `2`.
- `post_metrics` gains `tweet_id TEXT DEFAULT ''`, `quotes INTEGER`,
  `bookmarks INTEGER`, `author_followers INTEGER`, `lang TEXT DEFAULT ''`.
- New index `pm_client_tweet ON post_metrics (client_id, tweet_id)`.
- New table for history, which does not exist today:
  `post_metric_days (client_id, tweet_id, post_url_norm, day, likes, comments,
  shares, views, quotes, bookmarks, author_followers, last_refresh_ms,
  refresh_count, pulled_at, PRIMARY KEY (client_id, tweet_id, day))`.
- Both must go in the `DDL` string **and** in the ad-hoc `ALTER TABLE` block of
  `ensure_schema()` — `CREATE TABLE IF NOT EXISTS` is a no-op on the deployed
  database, so DDL alone would leave production without the columns.

**4.3 `webapp/portal_publish.py: _fold_scraper()`** — three bugs, all live:
- match on `tweet_id` first, `post_url_norm` second;
- **stop back-updating every past `sheet_date` for a URL.** It currently loops
  every matching row and overwrites them all, so a daily pull rewrites history
  instead of building it and the Growth chart flattens to zero;
- honour `status` per §3.4;
- write the `post_metric_days` snapshot;
- drop the `[today-3, today]` acceptance window for watchlist sources (§3.3).

**4.4 Handshake** — new admin route `POST /api/clients/{cid}/sources/{sid}/test`
calling `GET /api/project`, rendering the result in the Data source card on
`/admin/clients`.

**4.5 Dashboard** — surface `quotes` and `bookmarks` for X in
`portal/queries.py: _post_public` / `trend` and in `portal/static/portal.js`
(`METRIC_NAME`, `mLabel`). Note `reach` and `impressions` are already stored and
already invisible; do not add a third.

**4.6 Scheduler** — `portal_publish._loop()` fires on
`int(time.time() // 60) % SYNC_MINUTES == 0`, i.e. a fixed minute of the Unix
epoch in UTC. Change to a local-hour comparison against `clients.tz` so "03:30
IST" means 03:30 IST.

**4.7 Alerting** — a failed scheduled pull is **silent** today. It lands in
`client_sources.last_error` and container stdout, and nobody is told. At minimum:
show a stale `last_ok_at` as a warning on `/admin/clients`.

---

# Part 5 — Answers to your 21 questions

*The evidence behind everything above. Quoted from the code as it stands.*

---

## 1. Clients and how they map to the outside

### 1. What is a client in the data model?

Table `clients` in `data/portal.db`. DDL: `portal/schema.py`.

```sql
CREATE TABLE IF NOT EXISTS clients (
    id              TEXT PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    display_name    TEXT DEFAULT '',
    logo_path       TEXT DEFAULT '',
    accent_hex      TEXT DEFAULT '',
    lag_days        INTEGER NOT NULL DEFAULT 2,
    tz              TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    category_map    TEXT NOT NULL DEFAULT '{}',   -- {raw heading: {label, order, hidden}}
    show_screenshots INTEGER NOT NULL DEFAULT 0,
    show_reports    INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    archived        INTEGER NOT NULL DEFAULT 0
);
```

Primary key type: `TEXT`, generated as `uuid.uuid4().hex[:12]`
(`webapp/routes_clients.py: create_client`). No ORM — raw `sqlite3`.

**External-id field: does not exist.** There is no `external_id`, no
`collector_project_id`, no free-form settings blob on `clients`.
`category_map` is JSON but it is the category relabel map and is validated
key-by-key on write (`update_client`), so it is not usable as a settings bag.

**Per-client API key and endpoint: already exists**, in a separate table —
this is the integration point that is already built:

```sql
CREATE TABLE IF NOT EXISTS client_sources (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    signature_name  TEXT NOT NULL,                 -- the label for this key
    base_url        TEXT NOT NULL,                 -- may contain {from} {to} {date}
    api_key_enc     TEXT NOT NULL DEFAULT '',      -- portal/secretbox.py, never plain
    auth_style      TEXT NOT NULL DEFAULT 'bearer', -- bearer | x-api-key | query:<param>
    enabled         INTEGER NOT NULL DEFAULT 1,
    added_by        TEXT DEFAULT '',
    added_at        REAL NOT NULL,
    last_ok_at      REAL,
    last_error      TEXT DEFAULT '',
    last_count      INTEGER DEFAULT 0
);
```

A Collector `project_id` (integer) has **no column of its own**. Today it can
only ride inside `base_url` as a literal query parameter — e.g.
`https://scraper.vedictech.in/api/links?project=7&limit=500` — because
`build_url()` only substitutes `{from}`, `{to}` and `{date}`
(`portal/scraper.py: build_url`). That works with zero schema change; a real
`project_id INTEGER` column would need a migration (see 21).

### 2. How is a client created, and can integration settings be edited there?

Page: `GET /admin/clients` → `webapp/main.py:392` (`clients_page`), admin-only
(`Depends(auth.require_admin)`), template `webapp/templates/clients.html`.

API behind it: `webapp/routes_clients.py`, `APIRouter(prefix="/api/clients")`.
Auth is the **staff session cookie + CSRF token** (`_csrf` from
`routes_extras`), not a bearer token.

- `POST /api/clients` — create. Body `{name, slug?, display_name?, accent_hex?, lag_days?, tz?, show_screenshots?, show_reports?}`.
- `PATCH /api/clients/{cid}` — edit name, display_name, accent_hex, **tz**, **lag_days** (re-derives `visible_from` for every existing row), show_screenshots, show_reports, archived, category_map.
- `POST /api/clients/{cid}/projects` — link/unlink projects (optionally backfills).
- `POST /api/clients/{cid}/backfill`
- `POST /api/clients/{cid}/users`, `/users/set-credentials`, `/users/{uid}/reinvite`, `PATCH /users/{uid}`
- `POST /api/clients/{cid}/sources` — **add an integration source** (signature name, API URL, auth style, API key). Refuses if `PORTAL_KEY_SECRET` is unset.
- `DELETE /api/clients/{cid}/sources/{sid}`
- `POST /api/clients/{cid}/sync` — pull that client's sources now, body `{from, to, source_id?}`.
- `POST /api/clients/{cid}/sync-sheet` — re-read the client's Google Sheets now.

So yes: an admin can add the Collector as a source, paste the key, and hit
"Sync all sources" from the browser today. The key is never echoed back —
`_public_client` returns only `has_key`.

### 3. One client = one campaign = one sheet?

**Not true — a client can have several.** The chain is:

```
clients (portal.db)
  └── client_projects (client_id, project_id)        ← many projects per client
        └── projects (app.db)                         ← the grouping entity
              └── sources (app.db, project_id)        ← many Google Sheets per project
```

`client_projects` is `PRIMARY KEY (client_id, project_id)` with no uniqueness on
`project_id`, so a project can also feed several clients
(`portal_publish.clients_for_project`).

The **grouping entity is `projects`** (`webapp/jobs/store.py`):

```sql
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    client TEXT DEFAULT '', emoji TEXT DEFAULT '', owner TEXT NOT NULL,
    settings TEXT DEFAULT '{}', archived INTEGER DEFAULT 0, created_at REAL NOT NULL
);
```

Note `projects.settings TEXT DEFAULT '{}'` — an unused JSON blob on the
*project*, and `projects.client` is a free-text label, unrelated to
`clients.id`.

Sheets hang off a project as `sources` rows, each with
`url`, `mode` (`latest|tab|all`), `gid`, and
`purpose` (`report | dashboard | both`, added via `_ADDED_COLUMNS`).
Only `purpose in ('dashboard','both')` feeds the dashboard
(`portal_publish._dashboard_sheet_sources`).

Separately, a client can have several `client_sources` (scraper endpoints) —
the docstring says "One active source per client is the normal case; several
are allowed (name them)."

---

## 2. What the client dashboard shows for post links

### 4. Pages/widgets, routes, and what they read

All four views live in one page. Route `GET /` (i.e. `/portal/`) →
`portal/routes.py: dashboard` → `portal/templates/dashboard.html` +
`portal/static/portal.js`. Client id comes from the session cookie only —
there is no client id in any URL.

| View (element id) | Widgets | Data route | Query behind it |
|---|---|---|---|
| **Today** `#today` | KPI tiles `#kpis` (with 7-day sparklines), share bar `#sharebar`, per-platform rows `#platrows`, **Top posts of the day** `#topgrid`, **Categories on this day** `#catgrid` | `GET /api/daily?day=` and `GET /api/trend` | `portal/queries.py: daily()` → `db.visible_posts()` → `SELECT * FROM post_metrics WHERE client_id=? AND visible_from<=? AND status='ok' AND sheet_date=? ORDER BY id` |
| **Categories** `#categories` | per-category panels, one chart per platform, duration segment (7/14/30/custom), metric selector | `GET /api/trend?from&to` | `queries.trend()` (aggregate below) |
| **Growth** `#growth` | multi-line chart `#gChart`, delta headline `#gBig`/`#gDelta`, table `#gTable`, split **By platform / By category** | `GET /api/trend?from&to` | same |
| **Feed** `#feed` | post cards `#cards`, filters by category/platform, sort by engagement/views/time, search, pager | `GET /api/daily?day=` | `queries.daily()` |

Supporting routes: `GET /api/meta` (`queries.meta`), `GET /api/reports` +
`GET /report/{report_id}` (`client_reports`), `GET /api/export.xlsx`
(`portal/export.py`), `GET /media/{post_id}` (screenshots, gated on
`clients.show_screenshots`).

The trend aggregate, verbatim (`portal/queries.py: trend`):

```sql
SELECT sheet_date AS day, category_raw AS category, platform, COUNT(*) AS posts,
       SUM(COALESCE(likes,0))    AS likes,
       SUM(COALESCE(comments,0)) AS comments,
       SUM(COALESCE(shares,0))   AS shares,
       SUM(COALESCE(views,0))    AS views,
       SUM(COALESCE(likes,0)+COALESCE(comments,0)+COALESCE(shares,0)) AS engagement
FROM post_metrics
WHERE client_id = ? AND visible_from <= ? AND status = 'ok'
  AND sheet_date BETWEEN ? AND ?
GROUP BY sheet_date, category_raw, platform ORDER BY sheet_date
```

**Which metrics exist:** `likes`, `comments`, `shares`, `views`, and
`engagement = likes + comments + shares` (computed, `portal/static/portal.js: eng`).

The X vocabulary is only a **relabel in the browser** —
`portal/static/portal.js: mLabel` renders `comments` as "Replies" and `shares`
as "Reposts" when the platform is `x`. There is one storage column each.

- **reposts/retweets** → `shares` ✅
- **replies** → `comments` ✅
- **views** → `views` ✅
- **likes** → `likes` ✅
- **quotes** → **does not exist** (no column, no map entry, not rendered)
- **bookmarks** → **does not exist** in `post_metrics`. (The capture engine
  does read them — `metrics/x_metrics.py`, `metrics/shot_metrics.py` — and the
  XLSX run export has a "Bookmarks" column in `webapp/jobs/runner.py`, but that
  value is never carried into `post_metrics` and never reaches the dashboard.)
- `reach` and `impressions` are columns on `post_metrics` but are **not**
  returned by `queries._post_public` and **not** aggregated in `trend`, so they
  are stored and invisible.

**Per-day growth is computed in the browser, not stored.** `portal.js`
`renderToday()` fetches today and yesterday and diffs them; the Growth view
diffs a period against the preceding period of equal length (`deltaHtml`,
`pct`). Nothing persists a delta.

### 5. What "day-wise" means

The grouping column is **`post_metrics.sheet_date` (TEXT, `YYYY-MM-DD`)** — the
day tab, not the read date. But what fills it depends on which writer ran:

| Writer | `sheet_date` is |
|---|---|
| `portal_publish.publish_run()` (after a capture run) | `jobs.sheet_date` — the sheet's day tab (`_sheet_date_of`); falls back to the job's creation date in **server local time** if the job has none |
| `portal_publish.publish_from_sheet()` (read the sheet directly) | the date parsed out of the tab name (`smartsheet.list_tabs` → `t["date"]`), else the sheet's newest date block |
| `portal_publish._fold_scraper()`, post the sheet already listed | unchanged — the existing row keeps its `sheet_date` |
| `portal_publish._fold_scraper()`, post the sheet never listed | **`p["day"]` = the post's own publish date**, i.e. `util.day_str(parse_when(posted_at))`; if that is empty, the last day of the fetch window |

So: sheet-sourced rows are grouped by the sheet tab day; scraper-only rows are
grouped by the post's publish date. Never by the date the metrics were read.

Visibility on top of that: `visible_from = sheet_date + clients.lag_days`
(default 2) and a row is shown when `visible_from <= today in clients.tz`
(`portal/db.py: VISIBLE`, `visible_posts`).

### 6. Category / group dimension

**Yes.** Column `post_metrics.category_raw TEXT NOT NULL DEFAULT ''` — "heading
exactly as in the sheet". Indexed: `pm_client_day ON post_metrics (client_id,
sheet_date, category_raw, platform)`.

Display is remapped per client through `clients.category_map`, JSON of
`{raw heading: {label, order, hidden}}`, edited in Admin → Clients →
"Categories (the sheet's headings, as published)". A `hidden` category is
stripped from every answer, not just the menu (`queries._hidden_raw`).

Where it is filled from:
- capture run: `results` row `category` (`portal_publish._publish_rows`)
- sheet read: `row["category"]` or `row["section"]` (`_publish_sheet_rows`)
- scraper: `portal/scraper.py FIELD_MAP["category"] = ["category", "classification", "section", "label", "bucket"]`

"National X Influencers", "Counter Comments", "Hyper-local pages", "3rd-party
pages" are exactly the shape this expects — pass them as the string.
**Note: `"group"` is not in that FIELD_MAP list**, so the Collector's planned
`group` field would be dropped unless it is sent as `category` (or the list is
extended).

### 7. Platforms modelled

Three: **`x`, `facebook`, `instagram`**. Nothing else.

- Column: `post_metrics.platform TEXT NOT NULL` — `-- x | facebook | instagram`
- Constants: `portal/queries.py: PLATFORMS = ("facebook", "instagram", "x")`,
  `PLATFORM_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "x": "X (Twitter)"}`
- Also `portal/util.py: PLATFORMS = ("facebook", "instagram", "x")`

**YouTube does not exist.** Worse than absent: `portal/util.py: platform_of()`
ends with `return "x"` — anything it does not recognise as Instagram or
Facebook is filed as X. A YouTube link would appear in the X column.

Instagram and Facebook metrics **are** displayed today, out of the same
`post_metrics` table via the same three routes. There is no platform-specific
query, table or widget — only the label swap in `mLabel`. The values arrive
from whichever writer ran (sheet columns, screenshot/page read, or a
`client_sources` scraper); `metric_source` records which
(`sheet | page | ocr | mixed | scraper | none`).

---

## 3. How data gets in today

### 8. Existing ingest paths

**a) Capture run → publish (the original path).**
`webapp/jobs/runner.py` calls `portal_publish.publish_run(job_id)` when a job is
marked done. Writes `post_metrics` for every client whose project is linked.
Auth: internal, no HTTP.

**b) Google Sheet read, no capture.**
`portal_publish.publish_from_sheet(client_id)` → `webapp/smartsheet.py:
list_tabs/read` → `webapp/uploads.py: analyse()` → `_publish_sheet_rows()`.
Route: `POST /api/clients/{cid}/sync-sheet`, admin session + CSRF. Also runs on
the scheduler (see 10). Reads up to `max_days=90` dated tabs.

**c) Outbound HTTP pull from a per-client scraper — this is the one you want.**
`portal/scraper.py` (`fetch_raw` / `fetch` / `normalize_many` / `normalize`) +
`portal_publish.sync_client()` → `_fold_scraper()`.
Route: `POST /api/clients/{cid}/sync`, body `{from, to, source_id?}`, admin
session + CSRF. Also runs on the scheduler.
Auth *outbound*: `auth_style` on the source —
`bearer` sends **both** `Authorization: Bearer <key>` **and** `X-API-Key: <key>`;
`x-api-key` sends only `X-API-Key`; `query:<param>` appends `?<param>=<key>`
(allowed values: `query:api_key`, `query:key`, `query:token` —
`routes_clients.AUTH_STYLES`). Plus `X-Signature-Name: <signature_name>` always.
Timeout 20 s, response cap 25 MB, `Accept: application/json`,
`User-Agent: VedicReport-portal/1.0`.
Writes: `post_metrics` (upsert) and `scraper_cache` (verbatim body per day).

**d) File/sheet upload into a run.** `webapp/uploads.py`, `POST /v1/preview/file`,
`POST /v1/run/file` (bearer bot token). Feeds a capture run, not `post_metrics`.

**There is no inbound HTTP endpoint that writes `post_metrics`. Does not exist.**
Confirmed by grep — the only writers are `webapp/portal_publish.py` (3 sites),
one `UPDATE ... SET visible_from` in `webapp/routes_clients.py:198`, plus the
test and dev-seed scripts.

### 9. Existing Collector / Watch-Tower integration

**None.** `scraper.vedictech.in`, "Watch-Tower" and "watchtower" appear nowhere
in the repository (grep across `*.py *.js *.html *.md *.yml`). No
`client_sources` row is committed — sources live only in `data/portal.db` on the
server, so check the live DB before assuming, but there is no code that knows
the Collector.

### 10. Can the tool run a scheduled job?

**Yes — plain Python `threading` daemon threads. No cron, no Celery, no
systemd timer, no Laravel scheduler.**

Started in `webapp/main.py` lifespan (lines 56–58):

```python
cleanup.start_scheduler()
sources.start_scheduler()          # v3: watch every project's sheet sources
portal_publish.start_scheduler()   # Client Portal: scraper sync (needs PORTAL_KEY_SECRET)
```

The portal sync loop (`webapp/portal_publish.py`):

```python
def _loop():
    while not _STOP.wait(60):
        if int(time.time() // 60) % max(1, SYNC_MINUTES) != 0:
            continue
        ... sync_sheets_all() ...
        if KEY_SECRET:
            ... sync_all() ...     # sync_client() for every client with an enabled source
```

- thread name `portal-sync`, daemon, wakes every 60 s
- `SYNC_MINUTES = PORTAL_SYNC_MINUTES` (default **60**, floor 5 in `webapp/config.py`)
- `SYNC_DAYS_BACK = PORTAL_SYNC_DAYS_BACK` (default **4**) — each run asks for `[today-3 … today]` in the client's tz
- scraper leg is skipped entirely unless `PORTAL_KEY_SECRET` is set

Other examples of the same mechanism: `webapp/sources.py` (Google Sheet
fingerprint watcher, auto-starts runs) and `webapp/jobs/cleanup.py` (retention
sweep at boot then every 24 h).

**Conclusion: the tool can pull, and the pull machinery already exists.** Push
would be new code; pull is configuration plus the gaps listed in 17/18.

---

## 4. History and identity

### 11. Where a daily snapshot would live

**A per-day snapshot table does not exist.** `post_metrics` is a *current state*
table, one row per (client, post, sheet day):

```sql
UNIQUE (client_id, post_url_norm, sheet_date)
```

Columns (full DDL in `portal/schema.py`): `id, client_id, project_id, run_id,
sheet_date, visible_from, captured_at, platform, category_raw, handle,
display_name, avatar_url, post_url, post_url_norm, post_type, caption,
media_type, thumb_url, posted_at, collected_at, likes, comments, shares, views,
reach, impressions, metric_source, raw_metrics, status, skip_reason,
screenshot_path, first_published_at, published_at`.

Indexes: `pm_client_visible (client_id, visible_from)`,
`pm_client_day (client_id, sheet_date, category_raw, platform)`,
`pm_client_url (client_id, post_url_norm)`.

**Two facts that matter a great deal for daily pulls:**

1. A re-sync **overwrites counts in place**. `_fold_scraper` does
   `UPDATE post_metrics SET likes=?, comments=? … WHERE id = ?`.
2. It updates **every row for that URL, across every `sheet_date`**:

```python
rows = conn.execute("SELECT id, sheet_date FROM post_metrics WHERE client_id = ? AND post_url_norm = ? "
                    "ORDER BY sheet_date DESC", (client["id"], p["post_url_norm"])).fetchall()
...
for r in rows:
    ...
    conn.execute(f"UPDATE post_metrics SET {', '.join(sets)} WHERE id = ?", vals)
```

So a daily pull today does not build history — it **rewrites** it. Yesterday's
"likes on 8 Sep" becomes today's live like count. The Growth view would flatten
to zero movement.

The nearest thing to raw history is `scraper_cache`:

```sql
CREATE TABLE scraper_cache (
    client_id TEXT, source_id TEXT, day TEXT, fetched_at REAL,
    post_count INTEGER, body TEXT, PRIMARY KEY (client_id, source_id, day));
```

but its `day` is the *post's publish day*, written `INSERT OR REPLACE`, so
successive pulls overwrite each other too.

**What a snapshot needs:** a new table keyed on `(client_id, post_url_norm,
day)` (or `(client_id, post_id, day)` once a post id exists), written
append-only, with the day being the *pull* day in the client's timezone — plus a
change to `_fold_scraper` so it stops back-updating rows whose `sheet_date` is
older than today. Respect the existing `UNIQUE (client_id, post_url_norm,
sheet_date)` on `post_metrics`; do not widen it.

### 12. Dedupe key for a post

**The normalised URL: `post_metrics.post_url_norm`.** There is **no post id
column anywhere** — `tweet_id`, `post_id`, `platform_post_id` do not exist in
`post_metrics` or any other portal table.

Normalisation, `portal/util.py: norm_url()` — worth reading before you send
URLs:
- scheme forced to `https`, fragment dropped, trailing slashes stripped
- host lowercased, `www. / m. / mobile. / mbasic.` prefixes stripped
- `twitter.com` → `x.com`, `fb.com` → `facebook.com`
- **for every host except facebook.com, all query parameters are dropped**
- for facebook.com only `story_fbid, id, v, fbid, set` are kept

So `https://x.com/user/status/1234567890123456789` and
`https://twitter.com/User/status/1234567890123456789?s=20` collapse to the same
key. Case in the path is preserved — `/User/` and `/user/` are different rows.

**Bigint safety:** nothing to break today, because ids are not stored or sent.
Python has arbitrary-precision ints and SQLite INTEGER is 8 bytes, so
`1234567890123456789` is safe server-side; the browser never sees it. If you
add a `tweet_id` column, make it **`TEXT`** and keep it a JSON string on the
wire — `portal.js` parses responses with plain `JSON.parse`, which would round
any X id above 2^53.

### 13. Timezone for "a day"

**Per client**, then a portal-wide default:

- `clients.tz TEXT NOT NULL DEFAULT 'Asia/Kolkata'`, editable in Admin →
  Clients ("Timezone" field). Used by `portal/db.py: today_for()` →
  `portal/util.py: today_in()` (`zoneinfo.ZoneInfo`) for the visibility rule,
  and by `portal_publish.sync_client()` to choose the fetch window.
- `PORTAL_TZ` in `.env` / `portal/config.py`, default `Asia/Kolkata` — used when
  a client row has no tz.
- Containers run UTC. The only place server local time leaks in is
  `portal_publish._sheet_date_of()`, which falls back to
  `datetime.fromtimestamp(created)` for a job with no `sheet_date`.

`sheet_date` and `visible_from` are stored as naive `YYYY-MM-DD` strings and
compared as strings.

### 14. Retention and roll-ups

`post_metrics`, `client_reports`, `publish_log`, `scraper_cache` and
`portal_audit` are **never deleted by any job**. History is kept indefinitely.

The only retention that runs is on job working directories —
`webapp/jobs/cleanup.py`, boot then every 24 h:
- finished jobs older than `RETENTION_DAYS` (default **7**, `.env`) are removed
- while `data/jobs` exceeds `MAX_DATA_GB` (default **5**) the oldest finished
  job is removed, repeatedly

Deleting a job removes its screenshots and outputs; the `post_metrics` rows it
published survive (they hold a copy of the screenshot under
`PORTAL_MEDIA_DIR`).

**No roll-ups.** Aggregation is computed per request in SQL (`queries.trend`),
bounded by `PORTAL_MAX_TREND_DAYS` (default **120**) and by
`MAX_TREND_DAYS` clamping in `trend()`.

---

## 5. If the Collector should push

### 15. Ingest endpoint

**Does not exist.** Nothing to specify from code. What exists nearby, so you can
judge the cost:

- The tool has a bearer-token API: `webapp/api_v1.py`,
  `APIRouter(prefix="/v1")`, `require_token()` reads
  `Authorization: Bearer vr_…`, looks the token up in the `bots` table
  (`webapp/jobs/store.py`), and intersects the token's scopes with the linked
  actor's role ceiling.
- **It would not work as-is for a server-to-server push**: `require_token`
  demands an `X-Actor` header resolving to a linked human account
  (`effective_scopes` returns the empty set when `username` is falsy), and the
  scope list has nothing for writing metrics:
  `project.read, report.preview, report.run, report.download, report.cancel,
  source.run, send.compose, send.dispatch, send.other_chat, style.write,
  schedule.write, users.link`.
- The admin routes (`/api/clients/*`) are session-cookie + CSRF, not usable by a
  machine.

Hard facts that would constrain any push design:
- **Max body size: 10 MB**, enforced at the edge by Caddy
  (`request_body { max_size 10MB }` on `report.vedictech.in`; 1 MB on
  `clients.vedictech.in`). `MAX_UPLOAD_MB` in `.env` caps file uploads inside
  the app only.
- **Idempotency as it stands**: any writer upserts on
  `(client_id, post_url_norm, sheet_date)`, so posting the same day twice
  overwrites rather than duplicating — and, per 11, also overwrites *other*
  days for the same URL if it goes through `_fold_scraper`.
- No HMAC verification, no IP allowlist and no request-signing code exists
  anywhere in the repo.

### 16. Staging / UAT instance

**Does not exist.** One `docker-compose.yml` (services: `web`, `portal`, `wa`,
`tg`, `tg-splitter`, `caddy`), one `Caddyfile` with one hostname, one
`deploy.sh`. No `staging`, `uat` or `sandbox` string in the compose file, the
deploy script, `.env.example` or `docs/SERVER-DEPLOY-RUNBOOK.md`.

Closest available:
- `scripts/portal_dev_seed.py` — seeds a demo client with fake posts into a
  local `portal.db`.
- `portal/tests/test_portal.py` — inserts `post_metrics` rows directly.
- Running the stack locally: `python -m uvicorn portal.main:app --port 8020`.

A throwaway client on production (`archived=1` when idle, no `client_users`) is
the only way to test against the real instance today.

---

## 6. If the report tool should pull

### 17. Field mapping — Collector → tool

Mapping happens in `portal/scraper.py: normalize()` via `FIELD_MAP`, which tries
each candidate key in order and takes the first non-empty value. Dotted paths
dig into nested objects.

| Collector field | Tool field / column | Status |
|---|---|---|
| `tweet_id` | — | **unused.** No id column exists (see 12) |
| `url` | `post_url` + `post_url_norm` | ✅ `FIELD_MAP["url"] = [url, permalink, link, post_url, href]` |
| `post_url` | same | ✅ (fallback; `url` wins) |
| `tab` | — | **missing.** No mapping; see the `day` note below |
| `status` | — | **missing and dangerous.** `_fold_scraper` writes `status = 'ok'` unconditionally, so `unavailable` / `removed` rows would show as live posts |
| `status_note` | — | **missing** |
| `added_at` | — | **missing** |
| `last_refresh_ms` | — | **missing.** `collected_at` is looked for under `collected_at, scraped_at, fetched_at, crawled_at` — the name will not match |
| `refresh_count` | — | **missing** |
| `created_at` (post time) | `posted_at`, and it **drives `sheet_date`** for new rows | ✅ `FIELD_MAP["posted_at"] = [posted_at, created_at, timestamp, taken_at, date, published_at]`; parsed by `util.parse_when` (ISO, epoch s, epoch ms) |
| `text` | `caption` | ✅ (`text` is first in the list) |
| `lang` | — | **missing.** No column |
| `author_username` | `handle` (stored with a leading `@`) | ✅ via `username` |
| `author_display_name` | `display_name` | ✅ via `display_name` |
| `author_followers` | — | **missing.** No column on `post_metrics` |
| `author_avatar` | `avatar_url` | ❌ **will not match.** The list is `[avatar, avatar_url, profile_pic_url, profile_image_url, user.profile_pic_url, user.avatar, author.avatar, owner.profile_pic_url]` — `author_avatar` is not in it |
| `like_count` | `likes` | ✅ |
| `retweet_count` | `shares` | ✅ (`retweet_count` is in the list) |
| `reply_count` | `comments` | ✅ |
| `quote_count` | — | **missing.** No column, no map entry, not displayed |
| `view_count` | `views` | ✅ |
| `bookmark_count` | — | **missing.** No column in `post_metrics` |
| `media[]` | `media_type` + `thumb_url` | ✅ via `media.0.type` / `media.0.thumbnail_url` / `media.0.url` |
| `day` (planned) | should become `sheet_date` | ❌ **not read.** `normalize()` computes `day` itself from `posted_at`; a supplied `day` key is ignored. Needs a code change to prefer it |
| `group` (planned) | `category_raw` | ❌ **not in the list.** `FIELD_MAP["category"] = [category, classification, section, label, bucket]` — send it as `category`, or add `group` |

Two things the Collector already gets right for this code:

- **Counters are numbers or `null`, never zero for unknown.** That is exactly
  `portal/util.py: to_int()`, whose docstring says "a blank is the honest cell
  and a 0 would be a lie". `null` → `None` → the column stays NULL → the
  dashboard prints `—`. Do not switch to 0.
- Epoch-ms timestamps are handled (`parse_when` divides by 1000 above 2e10).

**Response envelope — check this first.** `normalize_many()` accepts a bare JSON
array, or an object holding the array under one of
`posts | data | items | results | records`:

```python
if isinstance(body, dict):
    for k in ("posts", "data", "items", "results", "records"):
        if isinstance(body.get(k), list): arr = body[k]; break
if not isinstance(arr, list):
    raise ScraperError('Expected a JSON array of posts (or {"posts": [...]}).')
```

If `GET /api/links` returns `{"links": [...], "total": n}` or
`{"rows": [...], "total": n}` the sync **fails outright** with that error.
Either return the array under `data`/`items`/`results`, or add the key here.

### 18. Cadence, and a row whose `last_refresh_ms` did not change

**Cadence today: hourly, not daily**, and a 4-day sliding window
(`PORTAL_SYNC_MINUTES=60`, `PORTAL_SYNC_DAYS_BACK=4`; both in `.env`, read in
`webapp/config.py`).

`PORTAL_SYNC_MINUTES=1440` would make it daily, but **not at a chosen local
hour** — the loop's condition is
`int(time.time() // 60) % SYNC_MINUTES == 0`, i.e. a fixed minute of the Unix
epoch, in UTC, unrelated to `clients.tz`. Firing at, say, 03:00 IST needs a
small change to `_loop()` (compare `util.today_in(tz)` / local hour instead of
the modulo).

Recommendation, if you want IST day boundaries: run the pull at **03:00–04:00
Asia/Kolkata**, after the previous IST day has closed, and stamp the snapshot
with the IST date of the *previous* day.

**Unchanged `last_refresh_ms`:** today nothing reads it, so the tool would blindly
rewrite the same numbers over every historical row for that URL (see 11). Once a
snapshot table exists, the correct behaviour is: still write the day's row
(a flat day is data), carry `last_refresh_ms` and `refresh_count` onto it so a
genuinely flat count is distinguishable from a stale read, and skip the content
update (`caption`, `avatar_url`, `thumb_url`, `posted_at`) entirely.

### What is missing on the tool side for the pull to work

Ordered by how much it breaks:

1. **No paging.** `portal/scraper.py: fetch()` does exactly one GET and hands
   the body to `normalize_many`. There is no `offset` loop and `total` is
   ignored. With `limit=500` everything past the first page is silently
   dropped. **Required change.**
2. **No `project_id` field.** Must be embedded literally in
   `client_sources.base_url` today, since `build_url` only substitutes
   `{from}`, `{to}`, `{date}`.
3. **`{from}`/`{to}` are not what the Collector takes.** The Collector's
   endpoint has no date range — it returns the whole watchlist. So `base_url`
   would be a plain URL with no placeholders, and `SYNC_DAYS_BACK` becomes
   meaningless for it; the window logic in `sync_client` only affects which
   scraper-only rows are accepted (`if day < a or day > b: continue`), which
   would **silently drop every post older than 4 days** on first import.
4. **No snapshot table** (11) — the pull overwrites history instead of building it.
5. **`status` is ignored** — removed/unavailable posts would render as live.
6. **Field-name gaps** (17): `author_avatar`, `group`, `day`, and possibly the
   response envelope key.
7. **No `quote_count` / `bookmark_count` / `author_followers` / `lang` columns.**
8. **`platform_of()` defaults to `x`**, which happens to be right for the
   Collector's X-only feed, but will misfile anything else later.

The smallest honest change set: add `links`/`rows` to `normalize_many`; add
`author_avatar`, `group`, `day`, `status`, `tweet_id`, `quote_count`,
`bookmark_count` to `FIELD_MAP` and `normalize()`; add an offset loop to
`fetch()`; add the columns and a snapshot table via the migration mechanism in
21; stop `_fold_scraper` back-updating past days.

---

## 7. Operational

### 19. Secrets and config

`.env` at the repository root, loaded by docker compose `env_file:` and, when
run by hand, by `python-dotenv` (`portal/config.py`). **No settings table for
secrets, no vault.**

Variables present in the live `.env`: `APP_USERS, APP_ADMINS, SESSION_SECRET,
COOKIE_SECURE, SESSION_HOURS, X_USERNAME, X_PASSWORD, X_EMAIL, X_TOTP_SECRET,
WORKERS, MAX_CONCURRENT_JOBS, INFLUENCER_WORKERS, JOB_TIMEOUT_MINUTES,
MAX_UPLOAD_MB, MAX_LINKS, RETENTION_DAYS, MAX_DATA_GB, EXECUTION_MODE, DATA_DIR,
SESSIONS_DIR, PORT, PORTAL_PUBLIC_URL, PORTAL_SESSION_SECRET, PORTAL_KEY_SECRET,
PORTAL_COOKIE_SECURE, PORTAL_TZ, PORTAL_SYNC_MINUTES, PORTAL_SYNC_DAYS_BACK,
PORTAL_AGENCY_NAME`. Also read but not currently set: `PORTAL_DB`,
`PORTAL_MEDIA_DIR`, `PORTAL_HOST`, `PORTAL_BASE_PATH`, `PORTAL_LAG_DAYS_DEFAULT`,
`PORTAL_SESSION_HOURS`, `PORTAL_MAX_TREND_DAYS`.

**A per-client API key can be stored safely, and that mechanism already
exists.** `client_sources.api_key_enc` holds the key sealed by
`portal/secretbox.py` (`seal` / `open_`) under `PORTAL_KEY_SECRET`. It is
written on `POST /api/clients/{cid}/sources` and opened only inside
`sync_client`. No route returns it — `_public_client` exposes `has_key: bool`
only. The add-source route refuses outright if `PORTAL_KEY_SECRET` is unset:

> "PORTAL_KEY_SECRET is not set in .env — set it (and PORTAL_SESSION_SECRET) before saving an API key, so it can be stored sealed."

Verify `PORTAL_KEY_SECRET` is actually set on the server before handing over a
key: with it empty, `sync_client` returns immediately with
`["PORTAL_KEY_SECRET is not set, so the saved API key cannot be opened."]` and
the scheduler skips the scraper leg entirely.

### 20. Error reporting

Four places, all pull-based. **No e-mail, no Slack, no Telegram alerting exists
for failures** (the `tg/` bot is a report-delivery bot, not an alert channel).

1. **`client_sources.last_error` / `last_ok_at` / `last_count`** — set on every
   sync attempt, rendered in the "Data source — the scraper" table on
   `/admin/clients`. Errors are plain-English from `ScraperError`, e.g.
   "The scraper answered HTTP 401 for …", "The scraper did not answer within
   20s", "The scraper did not return JSON".
2. **`publish_log`** (`client_id, run_id, kind, sheet_date, posts, visible_from,
   note, by_user, at`), `kind` ∈ `run | backfill | scraper | sheet`. Last 40
   rows shown as "Publish log" on `/admin/clients`. A scraper sync logs
   `"<signature>: <matched> matched, <added> added"`.
3. **Inline in the response** — `POST /api/clients/{cid}/sync` returns
   `{"ok": false, "result": {"errors": [...]}}`.
4. **stdout** — `print(f"[portal] sync {cid} failed: {e}", flush=True)` and
   friends, i.e. `docker compose logs web`. Scheduled failures land only here
   and in `last_error`; nobody is notified.

Practical consequence: **a broken nightly pull is silent** until someone opens
`/admin/clients`. If that matters, ask for it explicitly — it is new work.

### 21. Stack facts for writing a correct integration

- **Language/framework:** Python, FastAPI `>=0.110,<1.0` + `uvicorn[standard]`,
  Jinja2 server-rendered templates. Portal image `python:3.12-slim`
  (`Dockerfile.portal`); web image
  `mcr.microsoft.com/playwright/python:v1.61.0-noble` (`Dockerfile`).
  App version `3.3.0` (`VERSION`).
- **ORM: none.** Raw `sqlite3` with `conn.row_factory = sqlite3.Row`, WAL mode,
  `PRAGMA foreign_keys=ON`, 10–15 s busy timeout. Two files under `DATA_DIR`:
  `app.db` and `portal.db`.
- **Outbound HTTP: the standard library**, `urllib.request` in
  `portal/scraper.py`. No `requests`, no `httpx` on this path — deliberate:
  `portal/scraper.py`, `portal/util.py`, `portal/secretbox.py` and
  `portal/schema.py` are stdlib-only so `webapp/portal_publish.py` can import
  them without dragging FastAPI into the internal app. **Keep any new fetch code
  stdlib-only** or it breaks that boundary. Timeout 20 s, cap 25 MB, default
  SSL context.
- **Frontend:** hand-written vanilla JS (`portal/static/portal.js`), SVG charts
  drawn inline, no framework and no build step.
- **JSON number handling:** server side Python `int` (arbitrary precision) into
  SQLite `INTEGER` (8 bytes) — safe for X ids. Browser side is plain
  `JSON.parse` → IEEE double, so **any id must travel as a string** and be
  stored `TEXT`. `util.to_int()` also parses `"1.1K"`, `"63,900"`,
  `"2.1 lakh"`, `"1.2 cr"` and Devanagari digits, and returns `None` — never 0
  — for `"—"`, `"hidden"`, `""`.
- **Migrations: no Alembic, no migration files.** Two idempotent mechanisms,
  both run at every boot:
  1. `portal/schema.py: ensure_schema(path)` — executes the whole `DDL` script
     (`CREATE TABLE IF NOT EXISTS` throughout), then a small hand-written block
     of `ALTER TABLE … ADD COLUMN` for columns added after release, then
     records `SCHEMA_VERSION = 1` in the `meta` table. Called by both apps.
  2. `webapp/jobs/store.py: init()` — `executescript(_SCHEMA)` plus the
     `_ADDED_COLUMNS` dict, `{table: ((name, decl), …)}`, ALTERed in when
     `PRAGMA table_info` says they are absent, tolerating a concurrent add.

  **To add a column to `post_metrics`: put it in the `DDL` string *and* in the
  ad-hoc ALTER block of `ensure_schema`** — `CREATE TABLE IF NOT EXISTS` is a
  no-op on the deployed database, so DDL alone would leave production without
  the column and every query would fail. Bump `SCHEMA_VERSION` while you are
  there. To add a whole table, the `DDL` string alone is enough.
- **Deploy:** `./deploy.sh` → `docker compose up -d --build`; the script fails
  the deploy if the portal container is not serving.
