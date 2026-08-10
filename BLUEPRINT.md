# Collector — Blueprint

The one document that maps this project. Read this first; `RULEBOOK.md` holds
the deep design rules for the X collection engine; `README.md` is the
one-page front door. There are no other docs — if something about the
project's shape is worth writing down, it belongs in here.

> One line: a self-hosted tool for a media house that watches **X (Twitter)**
> and **Instagram** for chosen accounts/topics, collects matching posts within
> seconds — **no paid API** — organizes them into projects and watchlists,
> shows them in a live dashboard, and delivers everything (media included) to
> **Watch-Tower** for analysis.

---

## 1. The three principles everything follows

1. **Browser for login, plain HTTP for collection.** A real browser is used
   only to obtain a session (captcha/OTP is a human step). All polling is
   cheap authenticated HTTP reusing that session.
2. **Freshness-first watermark polling.** Each poll walks a timeline from the
   newest item and stops at the first already-seen one, so a normal poll
   costs one request. Small overlap catches late-indexed posts.
3. **Extract here, analyse in Watch-Tower.** No sentiment, topics or AI in
   this tool — two analysers would give two disagreeing answers. Everything
   this tool computes is a count or a timestamp.

## 2. Architecture

```
                       ┌──────────  DASHBOARD  ──────────┐
                       │  /app  React SPA (frontend/)    │
                       │  /     classic page (web.py)    │
                       └───────────────┬─────────────────┘
                                       │ JSON + SSE (web.py)
  AUTH             ENGINE                COLLECTOR          STORE
  ────             ──────                ─────────          ─────
X: auth.py    ->   engine.py        ->   collector.py  ->   store.py  -> results.db
   accounts.db     (twscrape)            (watermark)        (+ projects, watchlists,
                                                             collections, alerts)
IG: ig_*.py   ->   engine_ig.py     ->   collect_ig.py ->   store_ig.py -> ig_results.db
                   (instagrapi)

  DELIVERY (webhook.py): push loop -> Watch-Tower webhook + Telegram + alerts
  GUARD (guard.py):      advisory risk checks, never mutates
  API (api.py):          API-key JSON service for Instagram pull
```

The organizing layer on top (all in `store.py` + `web.py`):

- **Project** — one client/beat ("Elections 2026"). Groups watchlists, feeds
  and delivery scoping. Scraper accounts stay global: one pool serves all
  projects (safer for account health).
- **Watchlist** — handles managed in the dashboard. Compiles into ordinary
  streams the collector already knows: `kind='query'` → `(from:a OR from:b)`
  search streams chunked ≤20 handles (`wl:<id>:<n>`, sorted, deterministic);
  `kind='xlist'` → one X-List stream. Compiled streams carry `watched=1`, the
  flag that makes the watcher poll them with no config.toml entry (same
  mechanism as `tg_enabled`). Shrinking retires chunks by pausing — never
  deleting collected history.
- **Collection** — a curation board. Pins are references into `tweets`, never
  copies; deleting a board destroys nothing. CSV export per board.
- **Alert** — "this scope is posting ≥N× its usual hourly pace" → Telegram
  ping. Baseline = trailing 24h ending an hour ago (the surge must not sit in
  its own yardstick). `min_posts` floor, 30-min cooldown. Evaluated once a
  minute inside the delivery loop (`alerts.py`).

## 3. File map (every file, one line)

| File | Job |
|---|---|
| `main.py` | CLI: `serve`, `watch --all`, `login`, `doctor`, `guard`, `export`, `webhook`, `telegram` |
| `auth.py` | X sessions via real Chromium + persistent profile; validates before marking active |
| `engine.py` | Transport/parse seam over pinned `twscrape`; yields `Page` objects |
| `collector.py` | The poll loop: watermark stop, dedup, stop-reasons, adaptive intervals |
| `store.py` | `results.db`: tweets, streams, watermarks, polls, gaps + projects/watchlists/collections/alerts + snowflake helpers |
| `webhook.py` | Delivery loop: signed webhook push (media included), Telegram, alert ticking; cursor-based, loses nothing |
| `alerts.py` | Velocity-alert decision + tick (pure logic, testable) |
| `guard.py` | Advisory risk findings (budget, shared IP, dead accounts); never changes state |
| `config.py` / `config.toml` | What to watch + accounts; passwords live in `.env` by name |
| `web.py` | HTTP server: classic dashboard, JSON API, SSE live stream, serves `frontend/dist` at `/app` |
| `api.py` | API-key service (hash-stored keys) serving Instagram posts to Watch-Tower |
| `ig.py`, `ig_import.py`, `ig_login.py`, `ig_session.py` | Instagram session acquisition/persistence (cookie or password path; device pinned) |
| `engine_ig.py`, `collect_ig.py`, `store_ig.py` | Instagram engine / poll loop / store (`ig_results.db`) |
| `engine_fb.py`, `collect_fb.py`, `store_fb.py` | Facebook engine (desktop-UA headless render, password session, byte cap) / per-page scheduler / store (`fb_results.db`) |
| `fb_debug.py` | Facebook page-structure diagnostic (standalone; also built into the Fetch-now log) |
| `frontend/` | Vite + React SPA (source in `src/`, built `dist/` committed — the server never runs Node) |
| `static/` | The classic dashboard's assets |
| `deploy/` | VPS install: systemd units (`xscraper-web`, `xscraper-watch`, `xscraper-fb`), nginx, `setup.sh` (re-runnable) |
| `tests/test_all.py` | The whole offline suite — no network, no budget spent. Run it after every change |
| `tools/ig_probe.py` | Instagram session diagnostic |

Data stores (all git-ignored, never commit): `accounts.db` (X sessions),
`results.db` (tweets + organizing layer), `ig_accounts.db` / `ig_results.db`,
`fb_results.db` (Facebook posts + project-scoped pages), `fb_state.json` (FB
logged-in session), `fb_meter.db` (FB monthly bandwidth), `api_keys.db` (hashes
only), `profiles/` (browser profiles + IG sidecars), `.env` (all secrets).

## 4. How data flows

1. **Watch** — `python3 main.py watch --all` polls: config.toml streams +
   every DB stream with `watched=1` (watchlists) or `tg_enabled=1`. New tweets
   land in `results.db` with full media (`media_json`: photos, video files
   AND video thumbnails).
2. **See** — the SPA's Live Feed loads backlog over `/api/tweets?project=` and
   then receives each new post over the SSE stream `/api/live` within ~2s of
   storage. New arrivals batch behind a "N new posts" pill.
3. **Deliver** — `webhook.py` (runs inside `watch`) pushes signed batches to
   Watch-Tower. Position is a cursor: a receiver down for a day catches up by
   itself. Payload includes `media` (with thumbnails) since 2026-08-05 —
   receivers must de-dup on `tweet_id` (a string on the wire).
4. **Curate / get pinged** — editors pin into Collections, export CSV; alert
   rules ping Telegram on pace surges.

The dashboard NEVER collects. Without a running watcher the page is a
photograph — the UI shows a red "Collection is OFF" banner exactly then
(`/api/status.watcher_pid` is the signal).

## 5. External interfaces

**Webhook push (X → Watch-Tower).** Configured in `[[webhooks]]`. HMAC-SHA256
signature over `"<timestamp>.<body>"` in `X-XS-Signature`; reject stale
timestamps. Per-tweet shape: ids as strings, `media_urls` (flat, frozen) and
`media` `[{type: photo|video|gif, url, thumb, duration?}]`.

**Cookie-authed JSON (the dashboards).** `/api/status`, `/api/tweets` (rich
filters + `project=` + two cursors — `since_id` vs gapless
`since_collected_ms`), `/api/metrics`, `/api/delivery`, `/api/activity`,
`/api/guard`, `/api/live` (SSE), CRUD for projects / watchlists / collections
/ alerts, `/api/fetch` (explicit budget spend), `/api/ig/*`.

**API-key pull.** `web.py` allows keys on a fixed read allowlist;
`api.py` serves `/v1/instagram/posts`. Keys are currently all-or-nothing
across projects — fine while Watch-Tower is the only consumer. **Before any
second consumer exists, build project-locked keys** (key ↔ allowed projects,
enforced server-side).

## 6. Going live (the VPS runbook)

One-time: push to GitHub → on the VPS
`git clone … /opt/xscraper/app && cd deploy && bash setup.sh` → edit
`/opt/xscraper/app/.env` (`DASH_USER`/`DASH_PASSWORD`, webhook secret,
Telegram token, account passwords) → sign the scraper account in at
`https://<domain>/accounts` (streamed browser; the session must live on the
server, never copied from a laptop — one account, one place, one IP) →
`systemctl start xscraper-watch`.

Every update: push, then re-run `setup.sh` (idempotent: pulls, re-owns,
restarts). The collector service auto-restarts on crash and reboot —
deliberately slowly (60s), because rapid restarts are themselves a ban
signal. Health check: `/app` shows a green **Live** chip and no red banner.
`frontend/dist` is committed to git on purpose: the server runs no Node.

## 7. Invariants (each one was paid for — keep them)

1. Browser only for login; HTTP for collection. The collector never opens a
   browser.
2. Watermark stop = one request per poll. Never fetch date ranges.
3. Pin the scraping libs (`twscrape==0.19.2`, `instagrapi==2.18.12`) and
   assert their internals at startup (`engine.check()` / `engine_ig.check()`).
4. One stable device + one steady IP per account, forever. Persist sessions;
   re-login only when dead.
5. Extract here, analyse in Watch-Tower. Alerts are counts, not judgments.
6. Secrets never touch git; API keys stored as hashes; every `sessionid` is a
   password.
7. Guard is advisory, never destructive.
8. Migrations are additive (`CREATE IF NOT EXISTS` + `ALTER ADD COLUMN`);
   an old database upgrades in place, nothing vanishes (the Default-project
   backfill + orphan-stream adoption in `store._migrate` are the pattern).
9. New-post walks use the composite cursor `(collected_ms, tweet_id)` —
   delivery, the SSE stream and the dashboard must never disagree about what
   "new" means. One implementation, reused.
10. Removing an organizing object (watchlist, collection, board item) pauses
    or unlinks — it never deletes collected tweets. Destroying data stays an
    explicit, name-typed act.
11. tweet ids cross every wire as strings (JS numbers corrupt them).
12. The dashboard tells the truth about the pipe: watcher off → red banner;
    delivery behind → the number; a stream that cannot be polled → said out
    loud. Silent states are bugs.

## 8. Hard realities (do not underestimate)

- **Instagram fights automation.** Checkpoints only a human can clear (then
  import a fresh `sessionid` from that same browser); `LoginRequired` when
  fingerprints drift (pin the device file); `PleaseWaitFewMinutes` rate
  limits that punish retries — back off hours. A "hot" account stays hot;
  test on throwaways. The streamed-browser IG login is effectively dead;
  cookie/password paths work.
- **X List watchlists poll ~10× faster than query watchlists** (500 vs 50
  requests / 15 min). Big permanent watchlists belong on real X Lists; the
  planned "promote to X List" browser automation is the intended bridge.
- **No scraper is ban-proof.** Accounts get locked; libraries break when
  platforms shift. `doctor`, `guard`, and the pinned-version asserts turn
  breakage loud instead of silent.
- **`user_medias` wants the numeric pk, not the @name.** Validate IG sessions
  against the *feed* endpoint (`account_info` 403s on valid sessions).
- **Facebook serves a mobile UA the useless "WebLite/Bloks" shell.** With a
  mobile user-agent, a logged-in page renders post text + images but every post
  is a JS button with NO permalink and NO `role="article"` — unextractable. The
  engine therefore uses a **desktop UA** (real `role="article"` + permalinks),
  with an `mbasic.facebook.com` fallback. Don't switch it back to mobile.
- **Facebook logs out replayed cookies without `datr`.** `xs` replayed on a
  browser whose `datr` differs is treated as a hijacked session and killed in
  a request or two. The durable fix (and default): log in with
  `FB_EMAIL`/`FB_PASSWORD` so the browser owns its `datr`, and persist the whole
  session to `fb_state.json` for reuse. One steady IP (the server), no proxy.
  When 0 posts parse, the "Fetch now" log prints `all_links=`/`containers=` —
  the DOM shape to retune the extractor against.

## 9. Redesigning / extending

- Read this file, then the seam you're touching: engines (transport),
  collector (loop), store (persistence), webhook (delivery), web (surface),
  frontend/src (views).
- New dashboard capability = store method → thin `web.py` validator → small
  view in `frontend/src/views/` (tokens in `styles.css`, honest
  loading/empty/error states, dark mode from the same tokens). Rebuild with
  `cd frontend && npm run build` and commit `dist/`.
- New platform = new engine + collector + store trio behind the same Page
  shape; everything downstream (projects, delivery, UI) already generalizes.
- Every change: `python3 tests/test_all.py` stays green, offline, and grows a
  test for the new behavior. The suite is the contract.

**Done since first blueprint:** Facebook as a full third platform (desktop-UA
render, password session, per-page intervals, server-IP + byte cap); keyword
watchlists with AND; keyword-match highlighting in the feed; per-source Start/
Pause and check intervals; the Refresh button fetching X and Facebook together.

**Parked roadmap** (build when asked, in this order of value): media archiving
(local copies of media so deleted posts keep their evidence — cheap for
thumbnails, opt-in per watchlist); DOCX rundown export for collections;
promote-to-X-List browser automation; project-locked API keys (mandatory before
a second API consumer); keyword alerts (same alert loop, LIKE-match on recent
text); users & roles (editor vs viewer); Facebook groups & keyword search
(pages work today; groups/search are the next FB surfaces); pull-parity
delivery view for IG/FB in the dashboard.

*History note: git history contains the retired planning docs this file and the
rulebook replaced (PROJECT_CONTEXT.md, UI_REBUILD_PROMPT.md, UPGRADE_PLAN.md,
DEPLOY_LIVE.md, FACEBOOK_PLAN.md).*
