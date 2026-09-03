# Collector — Blueprint

> **PROTECTED DOCUMENT.** This blueprint may be UPDATED — it must be, in the
> same commit as any change that alters the project's shape — but it may never
> be deleted (pre-commit enforces this, see RULEBOOK §8). Together with
> `RULEBOOK.md` (the law: what you may not do) this file is sufficient to
> rebuild the whole tool from scratch.

The one document that maps this project. Read this first; `RULEBOOK.md` holds
the design rules and the protected-feature registry; `README.md` is the
one-page front door; `FACEBOOK_LESSONS.md` is the graveyard of Facebook dead
ends (read before touching the FB engine).

> One line: a self-hosted tool for a media house that watches **X (Twitter)**,
> **Instagram** and **Facebook** for chosen accounts/topics, collects matching
> posts within seconds — **no paid API** — organizes them into projects and
> watchlists, shows them in a live dashboard, and delivers everything (media
> as URLs) to **Watch-Tower** for analysis.

---

## 1. The principles everything follows

1. **Browser for login and rendering only; HTTP for collection.** A real
   browser obtains sessions (captcha/OTP is a human step). X and IG polling is
   cheap authenticated HTTP. Facebook is the one rendered surface (its data is
   only reachable through a logged-in page), and even there the browser reads
   structured GraphQL data, never pixels.
2. **Freshness-first watermark polling.** Each poll walks a timeline from the
   newest item and stops at the first already-seen one, so a normal poll costs
   one request. Dedup is the backstop, never the mechanism.
3. **Extract here, analyse in Watch-Tower — one exception.** No sentiment, no
   topics, no scoring. Everything this tool computes is a count or a timestamp,
   except **content labels**: one operator-triggered category per post from an
   external model (`classify.py` → Grok), stored beside the post and delivered
   with it. Manual, metered, capped, and overridable by hand. RULEBOOK §1
   directive 2 holds the full boundary; the short version is that this is a
   triage label, not an analysis layer.
4. **Humans clear walls, scripts never retry into them.** A login wall gets
   ONE scripted attempt; then the cause is recorded and a human acts from the
   dashboard. (The FB circuit breaker, `fb_health.json`.)
5. **Every operational switch lives in the dashboard.** Service loops re-read
   their settings every cycle — no SSH, no restarts to steer collection.
   Secrets are the one exception (`.env` only).

## 2. Architecture

```
                       ┌────────────  DASHBOARD  ────────────┐
                       │  /app  React SPA (frontend/)        │
                       │  /     redirects to /app (web.py)   │
                       └────────────────┬────────────────────┘
                                        │ JSON + SSE (web.py)
  AUTH             ENGINE                COLLECTOR          STORE
  ────             ──────                ─────────          ─────
X: auth.py    ->   engine.py        ->   collector.py  ->   store.py    -> results.db
   accounts.db     (twscrape)            (watermark)        (+ tweet_raw split, projects,
                                                             watchlists, collections, alerts)
IG: ig_*.py   ->   engine_ig.py     ->   collect_ig.py ->   store_ig.py -> ig_results.db
                   (instagrapi)
FB: fb_state  ->   engine_fb.py     ->   collect_fb.py ->   store_fb.py -> fb_results.db
   .json           (headless render,     (per-page cadence   (+ settings, page_profiles,
                   gql capture,          or favorites mode,   removed_pages tombstones)
                   login breaker)        paused/blocked-aware)

  LABELS (classify.py):  Classify button -> Grok -> one category per post,
                         auto-pinned to that category's board (manual,
                         background, whole-project, uncapped)
  DELIVERY (webhook.py): push loop -> webhook + Telegram + Google Sheet + alerts
  SHEETS (sheets.py):    Apps Script web app (default) or service-account JWT;
                         date|link|text|media, header once, append only
  GUARD (guard.py):      advisory risk checks, never mutates
  API (api.py):          API-key JSON service for Instagram pull
  LOG (activity_log.py): every collector/engine line -> activity.db -> dashboard
  POOL (store_accounts.py + accounts_api.py): managed account pool, failover, TOTP
```

The organizing layer (in `store.py` + `web.py`):

- **Project** — one client/beat. Groups watchlists, feeds, delivery scoping.
  Scraper accounts stay global (one pool serves all projects).
- **Watchlist** — compiled into ordinary streams (`kind='query'` →
  `(from:a OR from:b)` chunks ≤20 handles; `kind='keywords'` → rule streams;
  `kind='xlist'` → one X-List stream). Compiled streams carry `watched=1` so
  the watcher polls them with no config entry. Shrinking pauses, never deletes.
- **Collection** — a curation board; pins are references, never copies, keyed
  `(platform, post_id)` so one board holds X, IG and FB posts together. A board
  marked `auto` belongs to a label category and is filled by classify runs.
- **Label** — one category per post per project, from `classify.py`. Manual
  only, spend metered but not capped, one press covers every unlabelled post in
  the project, and a hand correction outranks the model's answer forever.
- **Alert** — pace-surge → Telegram; baseline = trailing 24h ending an hour
  ago; cooldown 30 min; evaluated in the delivery loop.

## 3. File map (every file, one line)

| File | Job |
|---|---|
| `main.py` | CLI: `serve`, `watch --all`, `login`, `doctor`, `guard`, `export`, `webhook`, `telegram` |
| `auth.py` | X sessions via real Chromium + persistent profile; validates before marking active |
| `engine.py` | Transport/parse seam over pinned `twscrape`; yields `Page` objects |
| `collector.py` | X poll loop: watermark stop, dedup, stop-reasons, adaptive intervals |
| `store.py` | `results.db`: tweets (hot row) + `tweet_raw` (payloads, LEFT JOIN when needed), streams, polls, projects/watchlists/collections/alerts, cross-platform pins (`collection_posts`), content labels (`post_labels`, `label_categories`, `label_runs`), settings, FTS5 search, retention |
| `webhook.py` | Delivery loop: signed webhook push, Telegram, Google Sheet, alert ticking; cursor-based |
| `sheets.py` | Google Sheets transport, two modes: Apps Script web app (no cloud project) or service-account JWT (no new deps); header-once, append-only `date\|link\|text\|media` |
| `alerts.py` | Velocity-alert decision + tick (pure logic, testable) |
| `classify.py` | Content labelling: builds the prompt from the project's categories, calls Grok over injectable async httpx, parses and validates the answer, prices it. Pure — no DB, no globals, never raises |
| `xlsx_min.py` | A .xlsx writer in one file, no dependencies: the Collections export is one workbook with a summary sheet and a sheet per board. Text and numbers, bold frozen header, column widths — nothing more |
| `guard.py` | Advisory risk findings; never changes state |
| `activity_log.py` | Persistent account-activity log (`activity.db`); `logger()` wraps every collector's log= |
| `decider.py` | The decider: every collector condition (no sources, paused, session missing/rejected, checkpoint, rate limit, budget, pass error) → ONE rule-based decision (idle / backoff / relogin / quarantine / rest + wait), logged once, operator paged once via Telegram WITH a link to the Fix panel (`/app/accounts?fix=<id>`) and a snooze link, recovery announced. `open_conditions` / `snooze` / `resolve` back the panel (`/api/decider/conditions`, `POST /api/decider`). State in `activity.db` (`decider_state`). IG wired (incl. checkpoint failover, `collect_ig.ig_failover`); FB/X next |
| `store_accounts.py` / `accounts_api.py` | Managed account pool (encrypted secrets, TOTP, backup codes, failover) + its API |
| `config.py` / `config.toml` | What to watch + accounts; passwords live in `.env` by name |
| `web.py` | THE server: JSON API, SSE live stream, auth, serves `frontend/dist` at `/app` |
| `api.py` | API-key service (hash-stored keys) serving Instagram posts to Watch-Tower. A key carries its `project_id` — the key IS the scope; an unscoped key reads nothing |
| `ig.py`, `ig_import.py`, `ig_login.py`, `ig_session.py` | Instagram session acquisition/persistence (cookie or password; device pinned) |
| `engine_ig.py`, `collect_ig.py`, `store_ig.py` | Instagram engine / poll loop (+ human pacing) / store (`ig_results.db`, settings). Sources are three columns: `label` person, `value` handle, `platform_id` numeric id |
| `migrate_ig_sources.py` | One-time, manual, idempotent: moves an old `ig_results.db` onto the three-column source model. Backs up first; never writes `label` |
| `IDENTITY_MODEL.md` | The label/handle/id contract — why it exists, how IG implements it, the migration order for FB and X |
| `ig_human.py` | Human-behavior pacing: active-hours, humanized gaps, long breaks, daily budget, warm-up — makes IG move like a person (pure, testable) |
| `engine_fb.py` | FB engine: desktop-UA headless render, GraphQL capture (primary), on-page JSON, DOM fallback; login circuit breaker; byte meter |
| `collect_fb.py` | FB scheduler: per-page cadence or favorites mode; honors pause/block; avatar cache from post data only |
| `store_fb.py` | `fb_results.db`: posts (two-key dedup), sources (lowercase labels), settings, page_profiles, removed_pages tombstones |
| `fb_debug.py` / `fb_probe.py` / `fb_data_probe.py` | FB page-structure diagnostics (standalone) |
| `frontend/` | Vite + React SPA (source `src/`, built `dist/` committed — the server never runs Node). Shell is a 2-column grid: `nav.side` is `position: sticky` and therefore its own stacking context, so overlays portal to `<body>` rather than trusting z-index |
| `deploy/` | VPS install: systemd units (`xscraper-web`, `xscraper-watch` X, `xscraper-fb`, `xscraper-ig`), nginx, re-runnable `setup.sh`, pre-commit hook. FB/IG units are enabled but start STOPPED (need a signed-in session; Fetch-now runs a pass on demand meanwhile) |
| `CHECKPOINT.md` | Running history of what changed, newest first — the evidence behind each rule's current wording. Protected: append every change, never delete |
| `tests/` | The offline suite — no network, no budget spent. Run after every change |
| `tools/ig_probe.py` | Instagram session diagnostic |
| `tools/diag_project.py` | "Why did this project stop collecting?" — read-only: prints the exact query each stream sends to X, recent polls with stop_reason/rate-limit, and checks every active collection filter against what the stream has actually collected |

Data stores (all git-ignored): `accounts.db`, `results.db` (+ WAL),
`ig_accounts.db`, `ig_results.db`, `fb_results.db`, `fb_state.json` (FB
session), `fb_health.json` (login breaker state), `fb_meter.db` (monthly
bytes), `activity.db` (account log), `api_keys.db` (hashes only),
`profiles/`, `.env` (all secrets).

## 4. How data flows

1. **Watch** — `main.py watch --all` (X), `collect_ig.py run --loop` (IG),
   `collect_fb.py run --loop` (FB). New posts land in the per-platform DB.
   FB re-reads its dashboard settings (mode/cadence/pause) every cycle.
2. **See** — the SPA's Live Feed loads backlog per platform
   (`/api/tweets`, `/api/ig/posts`, `/api/fb/posts` — one shared post shape)
   and receives new X posts over SSE within ~2s. Profile pictures: X is the
   canonical avatar source; FB/IG posts are handle-matched to it at read time.
3. **Deliver** — `webhook.py` pushes signed batches to Watch-Tower
   (cursor-based, at-least-once, ids as strings). IG/FB are pull
   (`/api/fb/posts`, `/v1/instagram/posts`).
4. **Watch the watchers** — every collector line persists to `activity.db`;
   the Activity Log page shows logins, fetches, walls, errors per platform.

## 5. External interfaces

**Webhook push (X → Watch-Tower).** HMAC-SHA256 over `"<ts>.<body>"` in
`X-XS-Signature`; per-tweet: string ids, `media` `[{type,url,thumb,duration?}]`.

**Cookie-authed JSON (the SPA).** Status/metrics/delivery/activity (+
`/api/activity/logs`), tweets with rich filters + two cursors, SSE `/api/live`,
CRUD for projects/watchlists/collections/alerts, `/api/fb/*` (status, posts,
source, fetch, favorites, control, health, settings), `/api/ig/*` (status,
posts, source), account pool `/api/pool*`, and labelling `/api/classify` +
`/api/labels/{status,categories,set,settings}`. `/api/classify` is deliberately
NOT in `API_KEY_PATHS`: it spends money, and a machine key may only read.

**API-key pull.** Method-split allowlist: `API_KEY_READ_PATHS` covers every
collected-data and telemetry endpoint on GET, `API_KEY_WRITE_PATHS` is
`/api/fetch` alone. The split is the safety property — most read paths also
exist as a POST that writes, so a path-only check would grant the write half
with the read. Keys never reach `/api/pool*`, `/api/stress/accounts` or
`/api/login/*`: accounts and sessions are credentials, not data. Before any
second consumer: project-locked keys.

## 6. Going live (VPS runbook)

One-time: push to GitHub → on the VPS
`git clone … /opt/xscraper/app && cd deploy && bash setup.sh` → fill
`/opt/xscraper/app/.env` (`DASH_USER`/`DASH_PASSWORD`, `FB_EMAIL`/`FB_PASSWORD`,
webhook secret, Telegram token, `ACCOUNTS_SECRET_KEY`) → sign accounts in →
`systemctl start xscraper-watch xscraper-fb`.

Every update: push, `git pull --ff-only`, `chown -R xscraper:xscraper .`,
restart the touched services. `frontend/dist` is committed on purpose (no Node
on the server). Install the pre-commit hook:
`ln -sf ../../deploy/pre-commit .git/hooks/pre-commit`.

## 7. Invariants (each one was paid for — keep them)

1. Browser only for login/rendering; the X/IG collectors never open one.
2. Watermark stop = one request per quiet poll. Never fetch date ranges.
3. Pin the scraping libs and assert their internals at startup.
4. One stable device + one steady IP per account, forever.
5. Extract here, analyse in Watch-Tower — except the one content label per
   post, which is manual, metered and travels with the post so there is one
   answer rather than two.
6. Secrets never touch git; API keys stored as hashes.
7. Guard is advisory, never destructive.
8. Migrations are additive and self-applying; old DBs upgrade in place.
9. One composite cursor `(collected_ms, tweet_id)` shared by feed, SSE and
   delivery — they can never disagree about "new".
10. Removing an organizing object pauses/unlinks; it never deletes collected
    posts. FB page removal writes a tombstone that beats auto-register.
11. Ids cross every wire as strings.
12. The dashboard tells the truth: watcher off, delivery behind, login
    blocked — all said out loud, never silent.
13. Login walls get ONE scripted attempt, then a human (the FB breaker).
14. X raw payloads live in `tweet_raw` — payload fields are read via
    LEFT JOIN, extracting only what's needed in SQL.
15. Profile pictures come from structured data or X handle-match — never
    scraped off a rendered page.

## 8. Hard realities (do not underestimate)

- **Instagram fights automation.** Checkpoints only a human clears; pin the
  device file; `PleaseWaitFewMinutes` punishes retries — back off hours.
  `user_medias` wants the numeric pk; validate sessions against the feed.
- **X Lists poll ~10× faster than query watchlists** (500 vs 50 req/15min).
- **No scraper is ban-proof.** `doctor`, `guard`, pinned versions make
  breakage loud.
- **Facebook, the short version of FACEBOOK_LESSONS.md:** desktop UA only
  (mobile serves the unextractable WebLite shell); mbasic is DEAD (tested,
  removed — returns nothing extractable); logged-in feed data arrives over
  background GraphQL — capture those responses, key on
  `__typename === "Story"`; counts often arrive in SEPARATE UFI payloads —
  stitch by `subscription_target_id`; replayed cookies without the browser's
  own `datr` get logged out — log in with email/password and persist
  `fb_state.json`; a page profile fires no feed GraphQL, the Favorites feed
  does (but Facebook injects "Suggested for you" into it — presence in the
  feed is NOT consent to track, hence tombstones); checkpoints appear even
  with 2FA off and only a human can pass them (hence the circuit breaker);
  server IP, no proxy, hard monthly byte cap in `fb_meter.db`.
- **Bandwidth per FB fetch** (after the 2026-08-14 slimming: no mbasic
  second navigation, no avatar visits): one page fetch = one desktop render
  with image/media/font bytes blocked + captured GraphQL, typically ~2–8 MB
  warm (the browser and its caches are reused across pages in a run); the
  favorites pass covers ALL pages in one render. Every fetch logs its exact
  KB in the Account Log; the month total lives in `fb_meter.db` and the cap
  refuses overruns.

## 9. Redesigning / extending

- Read this file, then the seam you're touching: engines (transport),
  collectors (loop), stores (persistence), webhook (delivery), web (surface),
  frontend/src (views).
- New dashboard capability = store method → thin `web.py` validator → view in
  `frontend/src/views/` (tokens from `styles.css`, honest states, dark mode).
  `cd frontend && npm run build`, commit `dist/`.
- New platform = engine + collector + store trio emitting the shared post
  shape, one entry in the Watchlists `PLATFORM_KINDS`, one detail component,
  one `/api/<p>/source` endpoint. Everything downstream generalizes.
- Watchlists UI is master-detail with a "Network & settings" tab — new
  controls go to their platform's detail panel or settings section, never
  scattered on the main surface.
- Every change: tests stay green offline and grow one for the new behavior;
  `RULEBOOK.md` and `CHECKPOINT.md` (and this file, if the shape changed)
  update in the SAME commit — the pre-commit hook enforces it. The hook is a
  DENY-list: everything counts as behavior except `tests/`, `tools/`, the FB
  probes and generated `frontend/dist/`. A UI change under `frontend/src/` is
  a behavior change.

**Done since first blueprint:** Facebook as a full third platform (GraphQL
capture, favorites mode, per-page cadence, byte cap); keyword watchlists;
account pool with failover/TOTP; `tweet_raw` split + FTS5; persistent account
activity log + Activity Log page; FB login circuit breaker with dashboard
human-handoff; dashboard-editable FB settings (mode/cadence/pause, applied
without restart); X-canonical profile pictures across platforms; watchlists
master-detail UI with unified platform-first add (X/FB/IG); IG sources
manageable from the dashboard; label canonicalization + removal tombstones;
living rulebook + protected documents enforced by pre-commit; IG structural
parity — dashboard pause/cadence re-read per cycle, checkpoint state surfaced
in the UI with the human recovery steps (the sidecar `checkpoint_at` breaker
predates FB's and stays); dashboard view state that the operator chose now
survives leaving the view (Live Feed filters persisted per project) and
overlays portal to `<body>` so no ancestor stacking context can bury them.

Content labelling as the one named exception to "analyse in Watch-Tower":
per-project category vocabulary edited in the dashboard, an
operator-triggered Grok pass over every unlabelled post across all three
platforms (background, progress-reported, no cap and no per-run ceiling as of
2026-08-24), a spend meter that reports rather than limits, sentiment counts at
the top of Collections, chips and a category filter on the Live Feed, an auto
board per category, one Excel export of every board (`xlsx_min.py`, no new
dependency), and hand corrections the model can never overwrite; collection pins made cross-platform
(`collection_posts`, migrated from `collection_items` on open).

**Parked roadmap** (build when asked): media archiving (local copies so
deleted posts keep evidence); DOCX rundown export; promote-to-X-List
automation; project-locked API keys (mandatory before a second consumer);
keyword alerts; users & roles; FB groups & keyword search; pull-parity
delivery view for IG/FB.

*History note: git history holds the retired planning docs this file and the
rulebook replaced.*
