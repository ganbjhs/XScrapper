# Rulebook

The rules every change must respect. Each one was paid for in a bug, a ban, or
a confused operator — breaking one re-buys that lesson. `BLUEPRINT.md` is the
map (what each file does); this is the law (what you may not do).

## 0. The rulebook is part of every change (the living-rulebook rule)

**A change that alters behavior updates THIS FILE in the same commit.** New
rule learned → written here; old rule invalidated → corrected here, with the
reason. A rulebook that lags the code is worse than none — it tells the next
person (or the next AI session) to rebuild a dead end with confidence. The
pre-commit hook enforces it: a commit touching engine/collector/server code
without touching `RULEBOOK.md` is refused (`RULE_OK=1 git commit …` is the
explicit opt-out for a change that truly alters no behavior, e.g. a typo).
When a rule here and the code disagree, STOP and reconcile — do not pick one
silently. (This section exists because mbasic survived in the engine after
testing had already killed it: the code and the book disagreed, and the stale
rule won.)

## 1. Three prime directives

1. **The browser is only for logging in and for rendering.** Collection is
   HTTP (X via twscrape, Instagram via instagrapi). A headless browser is used
   only where there is no other way in: the one-time X sign-in, and Facebook's
   rendered page. Never drive a browser to "click through" a feed — it is slow,
   fragile, and a ban magnet.
2. **Extract here; analyse in Watch-Tower.** This tool collects, normalizes,
   stores, and delivers. Sentiment, scoring, AI, entity work — none of it lives
   here. The Collector's job is to make clean, complete, timely data available;
   the intelligence is Watch-Tower's job. Do not grow an analysis layer here.
3. **Media travels as URLs, never bytes.** Photos and videos are stored as
   their CDN URLs (with a thumbnail URL). We never download media. Watch-Tower
   and viewers fetch it from the platform directly. This keeps storage tiny and
   bandwidth honest across all three platforms.

## 2. The one post shape (the platform contract)

Every platform's engine normalizes to the SAME record before anything
downstream sees it:

```
{ platform, tweet_id, url, text, created_at, collected_at,
  author_username, author_display_name,
  media: [{ type, url, thumb }], metrics }
```

New platform = new engine + collector + store that emit this shape (see X / IG /
FB as the three worked examples). Everything downstream — projects, the feed,
delivery, the UI card — already generalizes over it, so a fourth platform
touches no downstream code. `tweet_id` is always a **string** (JS loses integer
precision past 2^53; snowflake ids are well past it).

## 3. Collection rules

- **Watermark, newest-first, stop at the first already-seen post.** Every poll
  walks newest→older and stops the moment it reaches something already stored
  (X uses a composite `(collected_ms, tweet_id)` cursor; FB stops on posted
  time with id-dedup as backstop). Never re-walk a whole feed.
- **Going out to the platform is never automatic on a keystroke.** X fetches
  spend a shared rate-limit budget, so they are explicit, serialized behind one
  lock, guard-checked, and report what they cost. The dashboard *reads* the DB
  for free; *fetching* is a deliberate act (the Refresh / Fetch-now buttons, or
  the watcher's own timer).
- **Refuse, don't silently clamp.** A request for more pages than the cap is
  rejected with the reason, not quietly shrunk — a silent clamp still spends
  budget the caller never agreed to.
- **Per-source cadence is the source's own.** X watchlists and FB pages each
  carry their own check interval; a scheduler collects only what is *due*. Idle
  ticks cost nothing (no browser opened when nothing is due).

## 4. Delivery rules

- **Delivery is a durable cursor, at-least-once.** The webhook sender walks the
  same composite cursor the feed does, so the dashboard and the sender can never
  disagree about what "new" or "behind" means. A failed delivery leaves the
  cursor untouched — nothing is skipped; a post may repeat, never vanish.
- **Per-project scoping is real, not cosmetic.** A project's numbers, feed, and
  delivery describe THAT project's streams only. Webhooks are HMAC-signed.
- **Facebook/Instagram delivery is pull.** Watch-Tower pulls IG/FB via
  `/api/fb/posts` and `/api/instagram/posts`; X is pushed via webhook. Both are
  the same normalized shape.
- **A new delivery target is a transport, never a second pipeline.** Webhook,
  Telegram and Google Sheet share one cursor, one back-off, one filter set and
  one loop; a target kind adds a `deliver_*` function and a row shape and
  nothing else. If a target needs its own scheduling, it is being designed
  wrong.
- **Google Sheet targets append, never insert.** Four columns —
  `date | link | text | media` — with the header written only into a tab that
  is empty, so a sheet a human has already shaped is left alone. Appending
  means a row that lands is never moved again, which is what makes it safe to
  run under someone who is reading and filtering the same sheet. Cells are
  parsed on write (real dates, clickable links), so any cell starting
  `= + - @` is apostrophe-escaped — scraped text must never become a formula.
- **A sheet has two routes in, and the credential-free one is the default.**
  Google requires a credential for every write, so `sheet_mode='script'` puts
  the receiver INSIDE the sheet (an Apps Script web app running as its owner)
  and we hold only a URL and a token; `sheet_mode='service_account'` is the
  REST API with a key from `.env`. A NULL mode reads as `script`. Both build
  rows with the same `sheets.sheet_rows`, so the sheet never reveals which was
  used — if the two ever produce different columns, that is the bug.
- **"How long ago" has a shelf life.** A post's PUBLISHED time reads as
  relative for its first 30 days and as a real date after that (`fmtPosted`),
  because "97d ago" makes a reader do arithmetic to arrive at "mid-May".
  `fmtAgo` is untouched and still measures OUR freshness — last collected,
  last delivered, last polled — where a large number IS the alarm and a date
  would bury it. Do not merge the two.
- **A per-deployment secret is NAMED, never stored.** The Apps Script token
  follows the webhook-secret rule (`secret_env` holds the variable's name),
  not the Telegram-token rule (one global value), because there is one token
  per sheet. `/exec` URLs are addresses and are stored; tokens are not.

## 5. Security rules

- **Secrets live in `.env` / `config.toml` and are git-ignored. Never commit a
  cookie, password, session, or token.** `fb_state.json`, `*.db`, `profiles/`
  are ignored for the same reason.
- **The dashboard refuses to bind anywhere but localhost unless `DASH_USER` +
  `DASH_PASSWORD` are set** (and not placeholders, min length enforced). Making
  the unsafe state impossible beats documenting it.
- **An API key may only read data and spend fetch budget** — never add an
  account, open a sign-in browser, or change what a human sees. Enforced by an
  allowlist server-side, not by the caller being polite. Keys are compared in
  constant time.
- **One account, one steady IP.** Hopping IPs, or hammering a fresh account, is
  what gets accounts checkpointed. FB runs from the server IP; IG uses the
  residential pool; don't cross them.

## 6. Per-platform hard rules

**Sign-in is IMPORT-FIRST, on every platform, and it is never a browser.**
Instagram reached this wall first — "the streamed-browser IG login is dead" —
but the reason generalises: the login form is the most-scrutinised page these
sites have, and it is where every automation signal is checked at once (the
WebDriver flag, CDP artefacts, headless quirks, a profile with no history, a
datacenter IP that has never seen the account). Solving the captcha cannot
help, because the distrust is about the browser, not the answer. So the server
does not log in. `signin.py` has exactly two mechanisms, in this order:

1. **Import** (all three platforms). The operator pastes cookies from a
   browser they are already signed into, on their own IP, on a device the
   platform has trusted for months. No login event reaches the platform from
   this server at all — there is nothing to fingerprint and no captcha to
   lose. `parse_cookies` accepts whatever DevTools produced, because a parser
   that only takes one shape sends the operator back to the browser login.
2. **Background login** (Instagram only). instagrapi's app API with a pinned
   device — not a browser, which is why it works where the window never did.
   It costs one real login event, so it is second, but it is the only path
   that can re-login by itself when a session dies. X has none (twscrape's
   HTTP password login is unusable for the four reasons in `auth.py`'s
   docstring); Facebook's only non-cookie path is a headless browser at the
   login form, i.e. the thing this rule forbids.

**Carry the WHOLE cookie jar, and the operator's real user-agent.** `kdt` (X)
and `datr` (Facebook) are device-trust tokens — the browser saying "this is
the machine you already know". Importing only the two strictly-required
cookies throws that away and turns a trusted device into a new one. An empty
or `@`-prefixed user-agent makes twscrape invent a RANDOM one seeded by the
username (`http.py:25-33`), a fingerprint never associated with those cookies;
the panel sends `navigator.userAgent` with every paste for exactly this reason.

**One IP per session, consistently — a wrong IP beats two IPs.** An imported
cookie was minted on the operator's home IP and is then used from the server:
a real signal. Instagram's answer is its residential proxy, and both IG paths
REFUSE to run without one rather than fall back to the server IP. X and
Facebook run on the stable server IP (proxy bandwidth is the constraint) and
are mitigated by cookie completeness instead. Do NOT "fix" that by logging in
through a residential proxy and then collecting from the server IP — hopping
exits mid-session is worse than a single unfamiliar one.


**X (Twitter).** List watchlists poll ~10× faster than query watchlists (500 vs
50 requests / 15 min) — big permanent watchlists belong on real X Lists. One
long-lived asyncio loop for the whole process (twscrape's module lock binds to
the first loop that awaits it). Verification/category come from the stored raw
tweet JSON at read time, not columns. **The raw payload lives in `tweet_raw`,
not in the `tweets` row** (8c416bf moved it; `tweets.raw_json` is NULL on
every new row). Any reader that needs a payload field — the avatar, `user.blue`,
`blueType` — must `LEFT JOIN tweet_raw` and read
`COALESCE(r.raw_json, t.raw_json)`, extracting ONLY the needed field in SQL so
feed pages stay slim. This rule was paid for: `web.py` kept reading
`t.raw_json` after the move and every X profile picture silently vanished from
the Live Feed (2026-08-14).

**Instagram.** Fights automation hard: checkpoints only a human clears (then
import a fresh `sessionid` from that browser), `LoginRequired` on fingerprint
drift (pin the device file), `PleaseWaitFewMinutes` punishes retries — back off
hours. `user_medias` wants the numeric pk; validate sessions against the *feed*
endpoint. The streamed-browser IG login is dead; cookie/password paths work.
- **The IG circuit breaker is the sidecar's `checkpoint_at`** — once recorded,
  `ig_session.refresh()` refuses every automatic relogin until a human imports
  a fresh sessionid (which clears it). Relogin is attempted at most ONCE per
  collection pass, and only after a real call returned login_required. The
  dashboard surfaces the checkpoint (Network & settings) with the human steps.
- **Dashboard switches, same contract as Facebook:** global pause
  (`ig_paused`) and cadence (`ig_interval_s`) live in the settings table; the
  service loop re-reads them every cycle — no restart to apply. Cadence floor
  60s, refused not clamped.
- **The live smoke test is the gate.** `engine_ig.py` was verified by
  introspection, not against a live account — before trusting collection, run
  `python3 engine_ig.py <username>` once on the server with a real session (the
  built-in smoke test), and after any instagrapi bump run it again.

### Instagram is the STRICT platform — treat every rule here as non-negotiable

Instagram's bot detection watches rhythm, volume, IP and device as much as any
single request. These are hard rules, not tuning:

- **Human rhythm is mandatory, via `ig_human.py`.** The collector must move
  like a person: humanized per-page and per-source gaps, an active-hours
  window (mostly quiet overnight), an occasional long break, a per-account
  daily request budget, and a warm-up ramp for young accounts. The loop may
  never fire at a fixed machine tick. Removing or bypassing `ig_human` pacing
  is a rule violation (it is on the §8 protected list).
- **One account : one steady residential IP, forever.** IG runs through the
  account's own residential proxy (`http://user:pass@host:port`, stored
  encrypted in the pool), never the datacenter server IP, never a
  rapidly-rotating exit. Prefer sticky sessions; hopping IPs is itself a flag.
- **Cold starts stay small.** `max_pages` default 2; raise only once an
  account is warm. A fresh session opening with five back-to-back requests
  earns a `PleaseWaitFewMinutes`.
- **One relogin attempt per pass, never into a checkpoint.** The `checkpoint_at`
  breaker is absolute — retrying a locked account is the fastest way to kill
  it permanently.
- **The numeric pk beats the @name** for user sources; validate sessions
  against the feed endpoint, not `account_info`.
- **Test on throwaways first.** A hot account stays hot; never debug against
  the account you depend on.

**Facebook.** (Full history of dead ends in `FACEBOOK_LESSONS.md` — read it
before changing the engine; nearly every "obvious" idea has been tried.)
- **Read the data, not the layout. Three paths, best-first.** (1) Captured
  **GraphQL** responses — when logged in, Facebook fetches the feed over
  background `/graphql` requests rather than embedding it, so the engine
  captures those response bodies and pulls posts out of them. (2) On-page
  `<script type="application/json">` blobs (the logged-out case). (3) The old
  `role="article"` **DOM** scrape. All the JSON paths key on objects whose
  `__typename === "Story"` — a stable discriminator that survives Facebook's
  CSS/DOM reshuffles (visible-card class names rotate every few weeks; the
  story schema does not), and they carry the profile picture, exact time, and
  reaction/comment/share counts the DOM can't give. A Facebook change degrades
  us down the ladder, never to zero. The Fetch-now log's `via www:gql|json|dom`
  says which path fed a run.
- **mbasic.facebook.com is DEAD. Never re-add it.** Tested and removed
  (2026-08): it serves the WebLite/Bloks shell — no post JSON, no
  `role="article"`, no permalinks — so it can never meet the requirements, and
  as a "fallback" it only spent a request and polluted the diagnostics. Zero
  posts from the desktop site means *diagnose with the diag log*, not degrade
  to a surface that is known to return nothing.
- **Login gets ONE automatic attempt, then a human. Never loop.** A login
  wall triggers at most one scripted re-login. If that fails, the engine
  writes the ACTUAL cause to `fb_health.json` (a verification **checkpoint** —
  which Facebook throws even with 2FA off, and which no script may answer — vs
  **bad credentials / IP block**) and every further attempt is refused until an
  operator presses "Clear & retry" on the dashboard. Retrying a checkpoint in
  a loop is how the burner account gets locked permanently; a blocked login is
  a task for a human, and both the account log and the Facebook panel say so
  in plain words.
- **Profile pictures: X is the canonical source; never make a request just
  for a picture, and NEVER scrape one off a rendered page.** A public figure
  uses the same photo on every platform, and X avatars arrive free inside
  every collected tweet — so the dashboard shows a Facebook/Instagram post's
  avatar by handle-matching it to the X data at READ time
  (`web.py _x_avatars_for`). On the Facebook side the only avatar ever stored
  is one embedded in a post's own GraphQL Story (`actor.profile_picture`) —
  structured data, not screen contents. Render-harvesting was tried and
  REMOVED (2026-08-14): it ran against a login-walled render once and cached
  Facebook's login-page artwork as the "profile picture" of real pages. A
  heuristic that reads pixels off whatever page happens to be showing will
  eventually cache garbage forever; structured data or nothing.
- **The collector only READS the account. It never changes account settings** —
  no enabling 2FA, no answering verification flows, no profile edits, nothing
  under Settings. Anything Facebook asks that is not "show me the feed" is a
  human's job, by definition.
- **Every operational switch lives on the dashboard, not in SSH.** Pause /
  resume, collection mode (pages vs favorites), cadences, login retry and
  session reset are all in the Facebook panel; the background service re-reads
  them EVERY cycle, so no change requires a restart, and a background loop
  must always honor them (a paused or login-blocked collector idles for
  pennies instead of spinning). Secrets are the one exception: credentials
  stay in `.env` and are never shown or edited in the UI.
- **Dedup on TWO keys.** A post is refused if its `post_id` was seen OR its
  content signature (page + normalized caption) was — so the same post can't
  slip in twice just because a different path handed it to us under a different
  id scheme.
- **Removing a page is FINAL against auto-register, and label matching is
  case-insensitive.** Facebook injects "Suggested for you" posts into the
  Favorites feed, so a page appearing there is NOT consent to track it —
  `remove_source` writes a tombstone (`removed_pages`) that the favorites
  auto-register must honor; only a deliberate re-add lifts it. Labels are
  canonical lowercase; every WHERE on a label goes through `LOWER(label)`
  (paid for: pre-canonicalization mixed-case rows like `MohitBeniwalBJP`
  could not be removed from the dashboard at all, 2026-08-14).
- **A page profile fires no feed calls; a real FEED does.** Visiting
  `facebook.com/<page>` server-renders only the newest few posts and triggers no
  graphql — so per-page collection is DOM-only and shallow. The account's
  **Favorites feed** (`FB_MODE=favorites`) is a genuine infinite-scroll feed:
  it fires the graphql, returns many posts with the rich fields, and each post
  is attributed back to its own author page (mapped to whichever project tracks
  that page). This is the Facebook analogue of an X List, and it works only
  because we hold the account. It needs the pages added to the account's
  Favorites once (max 30).
- **Use a DESKTOP user-agent. Never switch it to mobile.** A mobile UA makes
  Facebook serve the "WebLite/Bloks" shell — post text and images render, but
  there is no post JSON and no `role="article"`, so BOTH extraction paths get
  nothing. The desktop site ships the JSON and the article DOM.
- **Hold the session, don't replay borrowed cookies.** Log in with
  `FB_EMAIL`/`FB_PASSWORD` so the browser owns its own `datr`, and persist the
  whole session to `fb_state.json` for reuse. Replaying `xs` without a matching
  `datr` gets logged out in a request or two. Delete `fb_state.json` to force a
  fresh login (e.g. after clearing a checkpoint in a normal browser).
- **Server IP, no proxy, hard byte cap.** FB uses the VPS's own bandwidth (not
  the residential pool); every response is metered in `fb_meter.db` and refused
  past `FB_MONTHLY_CAP_GB`, so it can never run away.
- **When 0 posts parse, the "Fetch now" log prints `all_links=` /
  `containers=`** — the real DOM shape to retune the extractor against. That is
  the diagnostic path; use it before guessing.

## 7. Change rules

- **Update this rulebook in the same commit as the change** (§0). The
  pre-commit hook blocks engine/collector/server commits that don't touch
  `RULEBOOK.md`; `RULE_OK=1 git commit …` is the explicit opt-out for
  behavior-neutral edits.
- **Migrations are additive and self-applying.** Add a column via a guarded
  `ALTER TABLE` on `open()` so an existing DB upgrades in place; never require a
  wipe. (See `store_fb._MIGRATIONS` and the X store's `watched`/interval
  columns as the pattern.)
- **`python3 tests/test_all.py` stays green, offline, and grows a test for the
  new behavior.** The suite is the contract; it needs no accounts and spends no
  budget. Run it as a script, not under pytest.
- **Watchlists are ONE structure across platforms.** The Watchlists page is
  master-detail (compact list left, one detail panel right — long member lists
  scroll inside their own box, never the page) with two tabs: "Watchlists"
  for daily use and "Network & settings" for configuration, login health and
  streams wiring. Adding is one platform-first flow (X: handles/keywords/
  X List; FB: pages/favorites; IG: user/hashtag/following). A new platform
  adds one entry to PLATFORM_KINDS, one detail component, and a `/api/<p>/source`
  endpoint — it must NOT invent its own page structure or scatter controls
  back onto the main surface.
- **The dashboard ships built.** The VPS runs no Node — `frontend/dist/` is
  committed on purpose. After any UI change: `cd frontend && npm run build` and
  commit `dist/`. New capability = store method → thin `web.py` validator →
  small view in `frontend/src/views/`, with honest loading/empty/error states
  and dark mode from the same tokens.
- **Every state the dashboard renders must have a writer, and a test that
  proves it writes.** `store_accounts.record_success()` existed from day one
  and nothing ever called it, so every card in the Account Control Panel read
  `last success —` forever no matter how much the account was collecting. The
  store's own tests were green the whole time, because the store was never the
  broken half. A getter without a caller is a lie the UI tells on your behalf:
  when you add a status column, wire the writer in the same commit
  (`pool_link.py` is that path for the pool) and test the wiring, not just the
  setter.
- **A missing state IS a state — never render nothing.** The panel's session
  row was `{live && (…)}`, so an account that had never been signed in showed
  no row at all: "collecting fine" and "we have never seen this account" were
  both blank. Blank reads as "nothing to report", which is exactly backwards —
  the absent case is usually the one needing action. Render every case by name
  ("never signed in on this server"), never by omission. Same reason
  `_status()` already lists declared-but-never-signed-in accounts.
- **Proxy credentials go in Playwright's `username` / `password`, never inline
  in `server`.** Chromium ignores userinfo in `--proxy-server`, so passing a
  whole `http://user:pass@host:port` authenticates as nobody and the first
  request returns 407 — surfacing as an inscrutable browser-launch failure, not
  as "your proxy password was dropped". `auth._proxy_kwargs()` splits it.
- **Match accounts across subsystems on the login, case-insensitively.** The
  pool, `ig_accounts.db`, `accounts.db` and `config.toml` each name the same
  account differently (label vs handle, typed by different people at different
  times). Match on the login first and the label second, normalising case, `@`
  and whitespace — an exact `===` on a display name silently un-matches an
  account and the UI degrades to blank.
- **Keep the pinned scraper versions and the `doctor`/`guard` asserts.** They
  turn "the platform changed under us" into a loud failure instead of silent
  data loss.

## 8. Protected features (removal requires the operator's explicit permission)

This registry is STRICT: nothing on it may be removed, disabled by default, or
quietly degraded without the operator saying so, in their own words, first. A
refactor that "simplifies away" a listed feature is a rule violation even if
every test stays green. Adding to this list is normal work; removing from it
is an operator decision recorded in the commit message. The two documents
`RULEBOOK.md` and `BLUEPRINT.md` are themselves protected: they may be
updated, never deleted (the pre-commit hook blocks the deletion).

**Collection**
- X watchlists: handles / keywords (AND, quoted phrases) / X Lists, with
  per-watchlist check intervals and collection-time filters.
- Instagram sources: user / hashtag / home-feed, managed from the dashboard
  (`/api/ig/source`), collected by the IG service.
- Facebook pages with per-page cadence, pause/resume per page and globally,
  plus favorites mode (one richer feed pass, posts attributed per page).
- Watermark polling everywhere (one cheap request per quiet poll), FB two-key
  dedup (post id + content signature), removal tombstones beating favorites
  auto-register.
- FB monthly byte cap + meter (`fb_meter.db`) — collection refuses past cap.

**Reliability & safety**
- Instagram human-behavior pacing (`ig_human.py`): active-hours window,
  humanized request/source gaps, long breaks, per-account daily budget,
  new-account warm-up. Mandatory whenever IG collects.
- Per-account residential proxy, stored encrypted in the pool, applied at
  login; the datacenter server IP is never used for IG.
- FB login circuit breaker: ONE automatic attempt, cause recorded
  (checkpoint vs credentials), human clears it from the dashboard. Never loop.
- IG checkpoint breaker (`checkpoint_at`): no auto-relogin into a locked
  account; human imports a fresh sessionid to clear it.
- Account activity log (`activity.db`): every collector/engine line,
  timestamped, browsable per platform in the dashboard.
- Guard (advisory only), `doctor`, pinned scraper versions with startup
  asserts, additive self-applying migrations.
- Login walls / bans surface in the UI in plain words — never silent.

**Dashboard**
- Live Feed: X + IG + FB in one stream, SSE real-time, keyword highlighting,
  profile pictures with X as the canonical avatar source.
- Watchlists page: master-detail with tabs ("Watchlists" /
  "Network & settings"), unified platform-first add flow.
- Accounts & Sessions: account pool (add / edit / promote / failover /
  quarantine / TOTP preview / backup codes), live sessions incl. orphans.
- Every pool card states its session in words, always — "signed in ·
  collecting", "never signed in on this server", "checkpoint — Instagram wants
  a human", or the session error. The row is unconditional; it may never go
  back to being hidden when there is no live record.
- Collectors write back to the pool (`pool_link.py`): a clean pass stamps
  `last_success_at`, a rejected session sets `needs_login` with the reason, a
  checkpoint quarantines. This works with no `ACCOUNTS_SECRET_KEY` in the
  collector's environment (the systemd units set no `EnvironmentFile`), so the
  lookup reads raw columns and never decrypts.
- One sign-in path for all three platforms (`signin.py`, `/api/pool/signin`):
  paste-a-session on every card, plus background login on Instagram. It runs
  in a thread and streams its commentary back, because "Instagram wants a code
  sent to your email" and "that cookie expired" are different problems and one
  red X cannot tell them apart. The Instagram device label is REUSED from any
  existing `ig_accounts.db` row — minting a new label hands the account a new
  handset, which is what gets a re-login challenged.
- The streamed sign-in window (`/api/login/*`) is offered for **X only** —
  the panel must never present it for Instagram (6: the IG streamed-browser
  login is dead, captcha loop; `ig_login.py` / `ig_import.py` are the paths).
  It takes a pool `account_id`, not just a `config.toml` label: it signs in through that account's own encrypted
  residential proxy and its own stable profile directory
  (`profiles/pool_<id>`), and writes the outcome back onto the card. The
  profile directory is the trusted-device state — it must stay derived from the
  immutable account id, never from a renameable label.
- Activity Log page: structured X poll history + raw account log with
  platform/level filters.
- Collections (pins, CSV export), Alerts (velocity → Telegram), Delivery
  (targets, backfill, behind-count), Search, Guard views.
- Every operational switch editable in the dashboard; service loops re-read
  settings each cycle (no restart to apply).

**Delivery & interfaces**
- HMAC-signed webhook push to Watch-Tower (cursor-based, at-least-once,
  media as URLs), Telegram sends, velocity alerts.
- API-key read-only pull on a fixed allowlist; IG/FB pull endpoints in the
  shared post shape.
