# Checkpoint

The running history of what actually changed, newest first.

`RULEBOOK.md` is the law, `BLUEPRINT.md` is the map, and this file is the
record of how the two arrived at their current wording. A rule reads as an
assertion; the entry that produced it carries the evidence — what broke, how it
was proved, what was verified before it was called done. Six months on, the
rule tells you what to do and this file tells you whether the reasoning still
applies.

**Every change appends an entry in the same commit** (RULEBOOK §8). This file
is a protected document: update it, never delete it — the pre-commit hook
blocks the deletion.

Entry format: date, one-line summary, then what changed / why / how it was
verified / what is still open. Keep entries short. Detail that a future change
must respect belongs in the rulebook, not here.

---

## 2026-09-12 (III) — Settings gets a home; Accounts stops shouting

**Changed**

- `frontend/src/views/Settings.jsx` (new) — `/settings`, under GLOBAL in the
  nav. Three panels: **Pager** (moved off Accounts, now a panel rather than a
  permanent banner, and it says that a ping which reads "Browser: no" means
  there is nothing to clear), **Secret storage** (`ACCOUNTS_SECRET_KEY` state),
  and **System** — build rev, instagrapi version, systemd units, whether
  collection is paused, whether a pass is running, the loop heartbeat. That
  last panel is `/api/ig/diag`, which has existed since 2026-09-04 with no UI
  at all: the only way to read it was curl.
- `frontend/src/views/Accounts.jsx` — `PagerBox` deleted (it lives in Settings).
  `FixPanel` splits `needs_human` from self-healing: the first gets the "Needs
  attention" heading and the cards, the second gets one muted line with a "Show
  anyway" toggle. The sessions tile stops swapping its own label and going red.
  "Pool low" drops from `banner-crit` to a muted line. The "nobody is paged"
  hint points at Settings instead of "above".
- `frontend/src/App.jsx`, `frontend/src/components/ui.jsx` — the route, the nav
  item, a gear icon.
- `frontend/dist` — rebuilt (`index-B65t0QUP.js`).
- `RULEBOOK.md` §4 — a new "The dashboard is a working surface, not a status
  board" subsection: five rules, including what a banner is for and where admin
  configuration lives.

**Why**

The operator: *"remove the unnecessary popups like needs attention without
having any critical issues"*, *"move the pager system ... to a new section
called settings where all admin settings live"*, *"make accounts section
clean"*.

Reading /app/accounts, three things were shouting with nothing wrong: the
pager's amber banner (amber is its resting state until a dedicated admin bot
is configured), a "Needs attention" heading raised by any open condition at
all including the self-healing ones, and a red "Pool low" bar for Facebook,
which is not in use this week. The stats tile compounded it by relabelling
itself and turning red for any needs_login / quarantined / **dead** account —
so a burner retired weeks ago kept the page looking alarmed.

This is the same thread as (II): a surface that shouts constantly gets
ignored, and then the operator goes poking at production to find out what is
really happening — which is how a second live session got opened on an
Instagram account.

**Verified**

`npm run build` clean (vite 5, 51 modules) and `dist/index.html` names exactly
the two assets in `dist/assets`. Python suite untouched by this commit and
still green at 1,447.

**NOT verified — say so plainly**

The rendered page was not loaded. The Chrome extension was not connected this
session and the dashboard needs its own login, so this is a build-and-review
change, not a tested-in-browser one. What to look at on first load:
`/settings` renders all three panels; Accounts shows no amber pager bar; with a
`proxy_flaky` open, Accounts shows the muted "1 condition is clearing itself"
line and NO "Needs attention" heading.

**Still open**

- The `st-good` / `st-warn` / `st-crit` classes exist; there is no `st-ok`
  (Settings originally used it and was corrected). Worth a pass to make the
  status-colour names consistent.
- Deletion inside `frontend/` is refused over the desktop bridge even with the
  session grant, so vite cannot empty `dist/`. Built to a path outside the
  mount and copied in; the stale asset was moved to `_to_delete/`. A local
  `npm run build` does this cleanly.

---

## 2026-09-12 (II) — the pager stops crying wolf, and the browser door stops making a second session

**Changed**

- `decider.py` — `Rule` gains `tell_recovered` (page when this CLOSES?) and
  `browser` (does a human need to open this account's browser?). New condition
  `proxy_flaky`: the connection died before Instagram answered, which on a
  rotating residential pool heals itself — backs off identically to
  `proxy_broken` but escalates only after 2h and never announces its recovery.
  `proxy_broken` keeps the TLS-interception half and still pages at once.
  `rate_limited` also stops announcing recoveries. `_worth_telling` honours
  `tell_recovered`; `_ping_text` adds the Browser line to every ping.
  `Decision.stop_account` lists BOTH proxy kinds.
- `collect_ig.py` — `_proxy_broken` picks the kind from `why`
  (`tls_intercepted` → `proxy_broken`, `network` → `proxy_flaky`).
- `web.py` — `_login_start` refuses to open an Instagram browser door while a
  collection pass holds the PassLock, and says how long the pass has been
  running and what to do instead. A lock that cannot be read never blocks a
  sign-in.
- `ig.py` — `InteractiveLogin._prove_exit`: the window asks an IP echo before
  it touches Instagram and logs `WINDOW EXIT <ip>` — through the proxy, a
  different exit from the collector's last, or (no proxy on file) the server's
  own address.
- `tests/test_all.py` — updated for the split and the silence; new checks that
  a human-needed condition says `Browser: YES` and a self-healing one says
  `Browser: no`.
- `RULEBOOK.md` §6 — three rules (never two sessions on one account; the window
  proves its exit; a ping only when a human must act).

**Why**

The operator's report, in their words: *"it pings me 100 times in a day so I
have to check it once is everything okay or not — I didn't understand what it
pings me again and again, that's the issue, nothing else"*, and *"I opened
[the browser] to check what's happening, is there any challenge which I have
to remove"*.

That is one causal chain, not two complaints. The pager paged on every proxy
flap and again on every recovery; none of it said whether a human was needed;
so the only way to find out was to open the account's browser window — and
that window is a SECOND live session on the account. Instagram then said so
in its own words on @sanaakhtar221's Account Status page: "You couldn't create
multiple sessions", ended 11 Sep.

So the fix runs the whole chain: fewer pings (the self-healing half of
`proxy_broken` now backs off in silence), every remaining ping answers the
browser question in one line, and the door refuses to make the second session
even if someone opens it anyway.

**Verified**

`tests/test_all.py` — **All checks passed (1,447)**, up from 1,442. The split
caught a genuine regression on the way: `Decision.stop_account` had a hardcoded
kind list, so the new `proxy_flaky` let a pass carry on through a dead exit,
source after source. `test_ig_stop_stands` failed within the hour and it is
fixed.

**Still open**

- Unmeasured against live traffic. The honest test is the operator's phone
  over a day: the count should fall to a handful, and each survivor should end
  in an action.
- `_prove_exit` costs one extra page load per window open (~2s) and reaches a
  third party (api.ipify.org) from the account's proxy. Cheap, but it is a
  request the account did not make before.
- The browser-vs-pass guard is machine-wide, not per-account, because
  `PassLock` is. Correct but slightly blunt: a pass on OTHER accounts also
  blocks the door. If that turns out to be annoying in practice, the lock
  would need to carry which accounts a pass is touching.
- `@shoaibakhtar4915` is still quarantined and the Facebook pool still shows
  0 backups.

---

## 2026-09-12 — Instagram: the session ages instead of being re-enacted; the proxy's rotation becomes a number

**Changed**

- `IG_DETECTION_ANALYSIS.md` (new) — the deep-dive behind everything below:
  what Instagram actually checks, graded DOCUMENTED / CORROBORATED / FOLKLORE
  with sources, and why most of what is written publicly on this subject is
  proxy-vendor marketing. Read §3 before changing anything here.
- `ig_session.py` — `touch()`: the SESSION half of the sidecar, written at the
  end of every pass. Fenced: never `save_device()`, never `ig.Store.save()`,
  and it refuses a settings dict with no `sessionid`. `_read_sidecar` /
  `_write_sidecar` (atomic: temp file + rename, then chmod).
  `refresh_browser_version()` — the derived Chrome major, updated in place with
  no reseed. `exit_due` / `sample_exit` / `exit_summary` + `EXIT_HISTORY`,
  `EXIT_SAMPLE_H`. `persist()` now writes through `_write_sidecar` and keeps any
  exit history a pass has accumulated.
- `ig_identity.py` — `refresh_web_browser()`: rewrites `identity.chrome_major`
  and the web UA built from it, and NOTHING else; refuses a legacy seed (that
  wants a reseed, not a bump).
- `collect_ig.py` — `_collect_account` ends with the write-back: `sample_exit`
  in a thread (it is blocking), `touch(cl, acct, exit=chk)`, and a log line when
  `exit_summary()["distinct"] > 1`. Never fatal — the posts are already stored.
- `signin.py` — `_fresh_phone_if_legacy` refreshes the browser version when the
  seed is NOT legacy (a legacy one is reseeded and a fresh mint already reads
  the real major). `_carry_jar` makes the app's `X-MID` agree with the browser's
  `mid` cookie, or says so when it cannot. `ig_browser_adopt` CHECKS the seed is
  not legacy and warns — it must not reseed, which would adopt the session onto
  a phone Instagram has never seen.
- `ig.py` — `InteractiveLogin._phone` refreshes the browser version on a seed it
  is not reseeding.
- `auth.py` — `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` and
  the older spelling, in the launch args.
- `fb_probe.py`, `fb_data_probe.py` — a real Webshare username and password were
  sitting in the usage docstrings of two TRACKED files. Replaced with
  placeholders and a note. **The credential is in git history; treat it as
  burned and rotate it.**
- `requirements.txt` — the `pyotp`/`cryptography` pair had been pasted three
  times.
- `tests/test_all.py` — `test_ig_writeback` (26 checks): the claim advances and
  the advance is kept; the handset cannot change in a pass whatever the client
  claims to be; the roster row is untouched; a client with no sessionid cannot
  blank a good sidecar; the exit history counts distinct IPs; a re-sign-in keeps
  the history; the browser version moves without a reseed and the Client Hints
  follow it.
- `PLATFORM_INSTAGRAM.md`, `PLATFORM_FACEBOOK.md`, `PLATFORM_X.md` (new,
  **gitignored**) — one recovery file per platform: architecture, the data path,
  the log line by line, every dead end we have hit and why, what works, the
  checkpoint timeline, a failure playbook and a rebuild-from-nothing order.
- `RULEBOOK.md` §6 — five new Instagram rules (write-back and its fences;
  derived version vs identifier; `mid` on two surfaces; WebRTC; "one steady IP"
  measured rather than asserted).

**Why**

The operator reported two symptoms: `proxy_broken` messages that sometimes
cleared themselves and sometimes needed a browser, and Instagram still finding
automated behaviour despite the rhythm layer. They are two different problems.

`proxy_broken` is not Instagram — it is the pipe, and the classifier was already
right about that: a connect-level exception means TLS never completed, so
Instagram never saw the request. The cause was already in this file, filed under
"Still open" on 2026-09-06: webshare's session-pinned exits are not steady, two
sign-ins two minutes apart left through two different IPs. The self-healing IS
the disease. That cannot be fixed in code — it needs a static residential / ISP
proxy per account — so the code now MEASURES it instead of asserting the rule.

The detection half was not a missing fingerprint. `ig_identity` is sound, and
the collection path runs no JavaScript at all, so canvas/WebGL/audio are
surfaces Instagram never sees from it. What was missing was CONTINUITY:
`persist()` ran at a sign-in and nowhere else, so every pass restored the
`x-ig-www-claim` minted at the last login and re-presented it — a value frozen
at a login timestamp while a real client's advances on every round trip. Nothing
raised; the session was simply coherent on day one and less so every day after.
The same shape produced the other two: a `chrome_major` of 140 in front of a
Chromium 151 render (recorded on 2026-09-06 and filed as "leave it", on the
mistaken belief that fixing it needed a reseed — it is a derived field), and an
app `X-MID` that never agreed with the browser `mid` cookie it inherited.

**Verified**

`tests/test_all.py` — **All checks passed (1,442)**, up from 1,416, on the
pinned deps. The full suite cannot run over the desktop bridge (the FUSE mount
gives aiosqlite `disk I/O error`); it was run from a copy on a real filesystem.

**Still open**

- **The proxies still rotate.** Everything above is maintenance; this is the
  decision that changes the outcome. Static residential / ISP, one per account,
  roughly $0.25–$3.50/IP/month.
- **None of this has run against live Instagram.** All 1,442 checks are offline.
  On the next deploy watch three things: `distinct == 1` in the exit summary, no
  `proxy_broken`, and no `recovered … after 0s`.
- The WebRTC flags are set but unmeasured — nobody has loaded a leak-test page
  through an account's own proxy using the account's own `_launch` path. The
  same run would say whether `--disable-blink-features=AutomationControlled`
  still does anything on Chromium 151.
- Production device seeds have not been audited for `ig_identity.is_legacy()`.
  The seeds in this checkout are still the August US Pixel with no `.bak`.
- The structural gap is untouched and unfixable by configuration: the browser
  mints the session (real Chrome TLS, JS, telemetry) and instagrapi replays it
  (python TLS, no JS, no telemetry) claiming to be the Android app.
  `IG_DETECTION_ANALYSIS.md` §3.3 sets out the fork.
- Instagram's Graph API + `business_discovery` has still not been costed. It is
  the only option that ends this class of problem rather than managing it.

---

## 2026-09-10 — The report tool's pull contract, reading a sheet without the Sheets API, and naming a link on every platform

**Changed**

- `store.py` — `links_snapshot()` serves the array under `items` AS WELL AS
  `rows`; every row states `platform`, carries `group` beside `section` and
  `thumbnail_url` beside `thumb`, and `day` is never null (the tab's date, else
  the IST date the link was first seen, with `status_note` saying it was
  inferred). New `_ist_day_of()` (fixed +05:30), `Store.project()`,
  `Store.links_handshake()`. `link_sheets` gains `script_url` +
  `script_token_env`, in the DDL **and** the migrate-on-open block.
- `web.py` — `/api/links` answers real HTTP codes (400/404/409) instead of 200
  with an `{"error": …}` body; new `GET /api/project` handshake carrying the
  counts, `skipped_non_x` and `refresh_in_progress`;
  `API_KEY_SCOPED_WRITE_PATHS = {"/api/links/sheets"}` with the body's project
  checked against the key's scope inside the handler; a per-key rate limit
  (60/min, hashed keys, `429` + `Retry-After`).
- `sheets.py` — `SCRIPT_SOURCE` gains `{"action":"tabs"}` and
  `{"action":"values"}`; `var VERSION = 2`, mirrored as `SCRIPT_VERSION` /
  `SCRIPT_READ_VERSION`. Reads take no lock; an action-less POST still appends.
- `links.py` — `read_sheet_via_script()`; `sync_sheet()` chooses its door from
  the bound row. `parse_post_url()` / `find_post_links()` /
  `find_short_links()` / `WATCHED_PLATFORMS` name a post on X, Instagram,
  Facebook and YouTube. `MAX_LIMIT`, `RATE_PER_MIN`.
- `tests/` — `test_report_contract.py`, `test_script_read.py` +
  `appsscript_harness.js`, `test_platform_urls.py`, all registered in
  `test_all.py`. `test_links.py` updated: its `/api/links/sheets` assertion had
  started passing for the wrong reason after the grant changed.
- `RULEBOOK.md` §2 and §5 — six rules. `LINKS_CONSUMER_HANDOVER.md`,
  `REPORT_TOOL_PLAN.md` (new, the reply to `REPORT_TOOL_ANSWERS.md`).

**Why**

`/api/links` returned its array under `rows`, and the consumer's
`normalize_many()` accepts `posts|data|items|results|records` and nothing else
— so its sync failed on the first call, every time, with "Expected a JSON array
of posts". Everything else in Phase 1 followed from reading their code against
ours: a `403` that names the project is useless if it arrives as an opaque
`200`, and a wrong key or wrong sheet surfaced only hours later inside a
scheduled sync nobody watches, which is what the handshake is for.

Reading a sheet needed a door that is not the Sheets REST API, because the
service-account route wants a cloud project, a JSON key, a sharing step and a
credential of ours that reaches every sheet that account was ever given. The
sheet's own Apps Script runs as its owner and needs none of them, and the sheet
stays private. The published-CSV route was tested and rejected: it works, but
it requires the sheet to be world-readable forever, cannot see hidden tabs, and
would rest tab discovery on a regex over Google's minified markup.

40% of the links on a day tab of the live sheet are Facebook, Instagram or
YouTube, and none of them could be named at all.

**Verified**

- `tests/test_all.py` — **All checks passed**, 1,403 checks, on Python 3.11.15
  against a staged copy (the local VM is still 3.10; `config.py` needs
  `tomllib`).
- The pull contract was exercised over real HTTP against the real handler, with
  the exact headers `portal/scraper.py: fetch_raw()` sends — both
  `Authorization: Bearer` and `X-API-Key` together, `X-Signature-Name`,
  `User-Agent: VedicReport-portal/1.0`. `tweet_id` was checked to be quoted
  **on the wire**, not merely a Python `str`. The 70th request in a minute came
  back `429` with `Retry-After`.
- The Apps Script is tested by RUNNING IT: `appsscript_harness.js` executes the
  real `SCRIPT_SOURCE` in node behind an HTTP server, and the real
  `read_sheet_via_script` talks to it over httpx. A Python re-implementation
  would have proved nothing about the source operators paste.
- **That harness caught a live bug in this change.** A version-1 deployment does
  not know `action`, so it read a read request as an append and answered
  `{ok:true, appended:0}` with no tab list — and `read_sheet_via_script` built
  an EMPTY snapshot with no error. Since a link absent from the sheet is marked
  `removed`, one such read would have emptied every watchlist in the project;
  the same POST would also have inserted a stray "Sheet1" tab into the
  operator's spreadsheet. Fixed with a GET version probe before any POST (doGet
  touches nothing) plus refusing a reply that carries no tab list.
- `parse_post_url` was run over **all 3,159 distinct URLs in the live sheet**
  (`1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0`): x 2230, facebook 502,
  instagram 355, youtube 44, fb-short 28, **unparsed 0**. Two real shapes were
  missing on the first pass — `instagram.com/<author>/reel/<code>` (the
  browser's own share sheet) and `facebook.com/share/p/<code>` (a short link
  whose code never joins to a post).
- The 50 dated tab names of the live sheet were run through `parse_tab_day`:
  49 correct, 3 archive tabs null by design, and one typo (`24/7/25`, since
  renamed by the operator).

**Still open**

- Phase 2 proper: a watchlist row for a non-X post.
  `watchlist_links` is `PRIMARY KEY (watchlist_id, tweet_id INTEGER)` and
  `WITHOUT ROWID`, so a string post id needs a widened key or a second table.
  The pattern to follow is `collection_posts` + `web._resolve_pins` — pins
  keyed `(platform, post_id)` and three lookups merged in Python, because X, IG
  and FB posts live in three separate SQLite files and nothing here joins
  across them.
- Resolution (IG shortcode → media pk, FB reel id → page), once per post and
  cached; then refresh BY AUTHOR, paced by `ig_human` — 335 IG posts and 339 FB
  reels come from only 15 distinct FB pages, so the author feed is ~20 reads a
  day against 335.
- YouTube (44 links) parses but is outside `WATCHED_PLATFORMS`: there is no
  engine for it on either side.
- The report tool's own P0 is unbuilt on their side: `post_metric_days` and
  stopping `_fold_scraper` back-updating past `sheet_date`s. Until both land, a
  daily pull rewrites yesterday instead of building history.
- The live sheet is owned by `nishantsoron@gmail.com` and its only other
  permission is `anyone: reader`. We hold no named grant and open it view-only,
  so the Apps Script door — which needs edit access to deploy — cannot be
  opened by us on THIS sheet, and removing the public link would cut every
  route to it at once. The service-account door is the smaller ask here (one
  share, Viewer); the script stays the better door for sheets the agency owns.
  Both are built and selectable per sheet.
- **First production bind, 10 Sep 2026, project 16.** 54 tabs, 2,288 X links
  added, 929 non-X skipped (the estimate from the offline parse was ~930), no
  errors, `email: ""` — the whole read went through the Apps Script and never
  touched a service account. All 51 dated tabs took their own day, including
  the renamed `24/7/26`.
- **`/api/links` served paused watchlists.** `links_due` honoured the pause and
  stopped fetching them; the snapshot did not, so a list the operator had
  visibly switched off kept reaching the consumer. Now both use the same
  predicate, the handshake counts only what is served, and `paused:
  {watchlists, links}` states what is withheld. This — not skipping tabs at
  read time — is the mechanism for the archive tabs: the operator's call, and
  the right one, because a per-watchlist control that is visible in the
  dashboard beats a per-sheet rule that would be wrong for the next sheet.
- **That bind also found a real bug**, which `tabs_mode` can fix (kept, but not
  the default answer): the sheet's
  two archive tabs were bound as watchlists (674 duplicate links), their rows
  took an inferred `day` (a 4 July post filed on 10 September) and their
  `section` became the column-A date label `"Date- 4-7-26"`. The decision to
  skip those tabs had been recorded in REPORT_TOOL_PLAN.md and never
  implemented; the handshake meanwhile reported `tab_mode: "dated"` as though
  it had been.
- The Apps Script is deployed and answers `{"ok":true,"v":2}` on GET, and
  `{"error":"bad token"}` to an anonymous POST — reachable exactly as the
  collector will call it. It was deployed from the sheet OWNER's Google
  account, because the operator's own account returned "This app is blocked"
  (an account-level restriction, not a script problem). **The deployment
  therefore lives in the owner's account**: if it is revoked, or the owner
  leaves, the read stops and the fix is a re-deploy by whoever owns it then.
- The dashboard cannot yet bind a sheet to a script: the "+ New watchlist"
  modal posts only `{project, sheet}`, so `script_url` / `script_token_env`
  are API-only. The first bind is a curl (`_secrets/bind_sheet.sh`).
- `origin` carries a GitHub personal access token in plain text in
  `.git/config`.

---

## 2026-09-09 — Links watchlists: post URLs re-fetched daily, counters overwritten, no history (X_LINKS_PLAN.md, Phase 1: X)

**Changed**

- `links.py` (new) — URL parsing (`x.com` / `twitter.com` / the fixer
  mirrors, `/status/` and `/statuses/`, `i/web`, photo/video tails,
  scheme-less), `scan_values` (every cell of a tab, first occurrence wins,
  non-X links counted as skipped, t.co reported), `is_due` / `order_due`
  (forced → never-fetched → longest-waiting; transient back-off 30 min ×
  streak capped 6 h; unavailable on cadence ×3 then weekly), `read_sheet`
  (two Sheets API GETs, service account) + `sync_sheet` (tab → watchlist by
  gid, adds never delete, t.co resolved by HEAD), `classify_detail`
  (found / "No status found" / tombstone / TweetUnavailable / else transient).
- `engine.py` — `Detail`, `parse_detail` (through `parse_page`, result set
  pinned to the focal id), `Engine.tweet_detail` over
  `api.tweet_details_raw`; `check()` asserts the op exists, rides
  `_gql_item`, and that twscrape passes the deleted-post error through.
- `store.py` — `link_sheets` + `watchlist_links` tables; `watchlists` gains
  `link_sheet_id` / `sheet_gid` / `sheet_tab` / `refresh_every_s` /
  `last_refresh_ms`; `kind='links'` in `create_watchlist` (compiles to
  `wl:<id>:0`, empty query, `watched=0`), `compile_watchlist`,
  `watchlists()` (`links` summary, `paused`, `sheet`), `delete_watchlist`
  (drops link rows, keeps tweets); the links section (`bind_link_sheet`,
  `links_watchlist_for_tab`, `add_links[_text]`, `sync_removed_links`,
  `remove_link`, `set_links_refresh`, `request_links_refresh`, `links_due`,
  `link_refreshed`, `links_summary`, `links_snapshot`, …).
  `upsert_tweets`: `author_followers = COALESCE(excluded, existing)`.
- `collector.py` — the third clock: `refresh_links` (one poll row per
  watchlist per pass, kind `links`; `LINKS_BATCH` links `LINKS_GAP_S`
  apart under `self.sem` and the global pause) and `sync_sheets`, driven
  from `run_forever` every `LINKS_TICK_S` / `LINKS_SYNC_TICK_S`;
  `links_enabled` for tests; the HTTP client opens on first sheet use.
- `main.py` — `watch` starts with zero poll streams when links watchlists
  exist (`_links_watchlist_count`).
- `web.py` — `GET /api/links` (+ `/api/links/sheets`), `POST
  /api/watchlists/links` (add / remove), `/links/refresh`, `/links/interval`,
  `/api/links/sheets` (bind + first sync on the request), `/sync`, `/remove`;
  `_watchlist_post` accepts `kind=links` with `sheet` or `links`;
  `API_KEYS_SCOPED` + `API_KEY_SCOPED_PATHS` + the scoped branch in
  `_require_auth`; `/api/links` in `API_KEY_READ_PATHS`.
- `frontend/src/views/Watchlists.jsx` — `PLATFORM_KINDS.x` += links; the
  Add modal's links form (sheet URL or name + pasted links) and the
  post-sync report; `LinksDetail` (header chips, cadence, Refresh now, Sync
  sheet, Pause, Delete; sheet line + error banner; search / sort / state /
  group-by-author; the rows table; paste box; help). `client.js` +9 calls.
- `tools/links_probe.py` (new) — the one-link live check.
- `tests/test_links.py` (new, 170+ checks) run from `test_all.py`.
- `guard.py` — `_budget` ignores `kind='links'` polls: a links pass reports
  the TweetDetail bucket (~150/15 min), which must never be read as the
  search budget.
- `.env.example` — `API_KEYS_SCOPED`. Docs: RULEBOOK §3 / §5 / §8,
  BLUEPRINT §2 / §3 / §5 / §9, WATCH_TOWER §7, README,
  `LINKS_CONSUMER_HANDOVER.md` (new).

**Day + section, same day.** The real sheet ("Varansi Day Wise Data
Testing") names its tabs by date (`7/9/26`, `6/9/26`, … `16/8/26`) beside
three master tabs, and splits each day into headed sections. `links.py`
gained `parse_tab_day` (day-first slash/dash, ISO, month names) and heading
detection in `scan_values` (a one-cell text row); `watchlists.sheet_day` and
`watchlist_links.section` (both migrated on open); `/api/links` rows carry
`day` and `section`, sort `day`, and default to the sheet's row order;
the panel shows the day in the sidebar and header, a section badge per row,
and "group by section" beside "group by author"; `refresh_links` fetches a
post once per pass however many lists carry it (`reused` in the summary).
Tests added for each.

**Sidebar (2026-09-10).** With a day-wise sheet the X group is thirty rows
long: it now scrolls in its own box past five rows (`.wl-rows.scroll`),
shows its count, and gets a filter box past eight. And X has a row even
when the project has no X watchlist yet — "X (Twitter) watchlists · none
yet" with an empty panel that offers "+ New watchlist" — matching the
Facebook and Instagram rows, which were always there.

**Review fixes, same day** (an independent review of the diff found eight;
all fixed and each has a test): `/api/links` returned the post's URL — null
before a fetch — instead of the link's own (`LINK_COLS` no longer carries
`url`; the post's is `post_url`); a tab title with `? # / %` broke the whole
sheet's sync forever (the A1 range is now URL-encoded); a fetch landing
after a removal resurrected the link (`link_refreshed` is guarded on
`status != 'removed'`); `delete_project` left the bound sheet behind, to be
re-read and fail every 10 min (it now drops `link_sheets` and
`watchlist_links`); the guard read a links pass's headers as the search
budget; `STATUS_RE` had no left boundary (`notx.com/…/status/…`,
`spacex.com/updates/status/…`, `example.com/x.com/…` all matched);
retention would have pruned tracked posts and let the next refresh re-insert
them with a new `collected_ms` (tracked links are excluded from `_prune`);
and a ms-wide race between the dashboard's first sync and the watcher's
tick could make two watchlists for one tab (`bind_link_sheet(claim_sync=
True)` + a gid re-check on name collision).

**Why**

A second tool needs the daily reach of specific posts, growing over time,
and keeps its own history. Adding the post's author to a handles watchlist
would not do it: a handle stream polls forward from a watermark and never
re-reads an old post, so its counters never refresh; it also collects every
future post on the search bucket. The plan chose a per-post fetch on its own
bucket, one row per post, counters overwritten, nothing snapshotted here.

**Verified**

`python3 tests/test_all.py` green at 1288 (links section: parsing, planning,
classify, parse_detail, store lifecycle, collector pass — counters move and
`collected_ms` does not, unavailable keeps numbers, transient backs off,
exception on one link does not abort the pass, global pause stops it,
`run_forever` drives it with no poll streams — sync_sheet against a fake
Sheets API incl. rename / removal / 403 / no creds, scoped-key gate).
`engine.check()` OK on twscrape 0.20.0. Panel rendered against a seeded
database (list, grouped, Add modal) at 1440 px. **Not yet verified live:**
`tools/links_probe.py <url>` on the server with a signed-in account is the
remaining step.

**Still open**

- Run the probe on the server; then bind the real sheet in a dedicated
  project (not one Watch-Tower has bound) and watch the first pass in the
  Activity Log.
- Give the consumer a scoped key (`API_KEYS_SCOPED`) and the handover.
- Instagram and Facebook links (phases 2/3): today they are counted as
  "skipped" by the sheet scan.
- Script-mode deployments cannot read a sheet; service account only.

---

## 2026-09-06 (III) — IG: a STOP decision stands for the whole pass; every shared store opens in WAL

**Changed**

- `decider.py` / `collect_ig.py` — a BACKOFF (or any stop) decided
  mid-pass is not closed by the end-of-pass `ok()`; the condition stays
  open, the admin is paged once, the back-off runs.
- `store_ig.py`, `store_fb.py`, `ig.py`, `activity_log.py` — every shared
  SQLite store sets `journal_mode = WAL` on open, as `results.db` always did.
- `tests/test_all.py` — `test_ig_stop_stands`.

**Why**

Live server, 2026-09-06 12:52:30: four sources read, the fifth died on the
proxy (502), BACKOFF 30m + page — then one second later "recovered from
'proxy_broken' after 0s", a second page, and the back-off cancelled. And at
14:42 a "database is locked" pass_error while the dashboard read the same
store the collector was writing.

**Verified**

`python3 tests/test_all.py` green (`test_ig_stop_stands`: every store
reports `wal`; a pass whose fifth source raises the proxy error ends with the
BACKOFF condition still open and exactly one page).

---

## 2026-09-06 (II) — Watch-Tower "silent Collector list": a slow-request log, and a bounded mirror count

**Changed**

- `web.py` — `Handler._note_slow`: any request slower than `SLOW_REQUEST_S`
  (3 s) prints one line to journalctl — method, path, the query KEYS
  (never values), status, seconds, caller. `do_GET` / `do_POST` stamp
  `_t0`; `_send` reports. The access log stays off otherwise.
- `web.py` — `_query_tweets`: on a cursor walk (`since_id` /
  `since_collected_ms`) the `COUNT(*)` is bounded to ten pages beyond the
  current one (`LIMIT limit*10+1`), so an old or reset cursor no longer
  walks the whole project's rows on every call. `has_more` is unchanged
  in meaning. Nothing in the dashboard uses the cursor parameters; only
  the mirror does.

**Why**

Watch-Tower's WhatsApp alert, 2026-09-06 13:55–14:20 IST: four projects
"have a silent Collector list — the mirror failed: timeout of 60000ms
exceeded" (Varanasi, BJP Rajasthan, Bihar, Devendra Fadnavis). Their
mirror calls our `GET /api/tweets?project=P&since_collected_ms=…&limit=500`
and gives it 60 s; the alert opens when their newest mirrored post is
older than 6 h, so the stall may have run for hours before it, not just
the 25 minutes the alert was open. On our side there was nothing to read:
the access log is disabled. The two changes are the minimum that makes
the next one diagnosable and removes the one known way the call itself
gets slow. The cause of this one is not established — see below.

**Verified**

`python3 tests/test_all.py` green at 1113.

**Still open**

- The cause. On the server, for the window 07:30–14:30 IST:
  `journalctl -u xscraper-web -u xscraper-watch --since "2026-09-06 07:30"
  --until "2026-09-06 14:30"`, `free -m`, `uptime`, `ls -la
  /opt/xscraper/app/results.db*`, and whether a streamed browser window
  (Chromium — ~1 GB) was open. The candidates, in order: the server
  starved (a Chromium from the browser door, or swap); the X collector
  itself producing nothing for 6 h (Watch-Tower's threshold is on OUR
  newest post — the timeout is only the error it last recorded); a mirror
  with an old cursor hitting the unbounded count (now bounded).
- Facebook and X collectors still print-and-continue instead of going
  through the decider, so a stalled X collector pages nobody on our side.
  Watch-Tower noticed first. That is the next thing to route.

---

## 2026-09-06 — the pager, phase 2: short pings, rare pings, its own bot

**Changed**

- `decider.py` — `Rule.human` is gone; `Rule.title` (the ping's first line)
  and `Rule.short` (the "Do:" line) replace it on every paging rule.
  `_ping_text` composes `🔴 IG @acct — title / Do: … / Now: <meta.note> /
  Fix → … / Snooze 6h → …`; `_still_open_text`; `_recovered_text` is one
  line (`🟢 IG @acct — recovered from 'kind' by … after …`). New table
  `decider_pinged(scope, kind, ms)` (kept after a condition closes) with
  `_State.pinged_ms` / `mark_pinged`; `REPING_COOLDOWN_S` 24h,
  `STILL_OPEN_AFTER_S` 24h, `RECOVERED_MIN_OPEN_S` 10m,
  `RECOVERED_DEDUPE_S` 1h; `_worth_telling`. `decide()`: escalation checks
  the cooldown (suppressed → `meta.not_paged_ms` + a log line), one
  still-open ping (`meta.still_open_ms`), no "recovered (now X)" on a
  change of kind. `resolve(quiet=)`. `Decider(quiet=)` logs once instead
  of paging. The channel: `admin_token()` (`ADMIN_TELEGRAM_BOT_TOKEN`, then
  `TELEGRAM_BOT_TOKEN`), `send_admin` (the text as composed — the
  "Admin —" prefix is gone), `discover_chat` (the last private chat in
  `getUpdates`), `_tg`; `notify_ready` uses the admin token. A CLI:
  `python3 decider.py test | chat`.
- `collect_ig.py` — the one-shot decider in `run_once` is `quiet=True`; the
  failover notes are one line each ("Collection failed over to @x; sources
  pinned to @y wait." / "Collection is STOPPED — no other account with a
  working session.").
- `web.py` — `_decider_after_signin` resolves with `quiet=True`; `_env_set`
  (factored out of `_save_telegram`); `POST /api/pager/telegram` (token,
  chat id, name → `.env`; a blank chat id is read off the bot's updates),
  `POST /api/pager/test` (one real ping; finds and saves the chat id if it
  is missing); `/api/decider/conditions` carries `pager` (ready, own_bot,
  token_hint — the id before the colon, never the token — admin, chat).
- `frontend/src/views/Accounts.jsx` (+ `client.js`, rebuilt `dist`) —
  `PagerBox` above "Needs attention": one line saying who is paged through
  which bot, Send test, and a Set up / Change form.
- `.env.example` — `ADMIN_TELEGRAM_BOT_TOKEN`, and the dashboard/CLI ways
  to set it.
- `tests/test_all.py` — `test_pager_quiet` (32 checks); two string
  expectations in `test_pager` / `test_decider` updated to the new text.
- `RULEBOOK.md` §6 (the pager bullet rewritten; a new "short and rare"
  bullet).

**Why**

The operator's three complaints, all fair. The pings were paragraphs
("60 words, a phone shows 20") — the fix line was somewhere in the middle.
One checkpoint produced a stream of them: the Fetch-now button runs a
one-shot decider with no memory, so every click paged again; a sign-in
closed the condition with a "recovered" and the adopted session re-opened
it with a fresh page — two messages per attempt; and a change of kind sent
"recovered (now X)" before X's own ping. And the pings came from the
delivery bot, whose voice belongs to collected content, not to "sign this
account in". If an account is quarantined and the operator has not been
able to return it to the pool in a day, something bigger is wrong — a
fourth reminder does not fix it; one "still open, this is the last" says
that honestly.

**Verified**

`python3 tests/test_all.py` green at 1113, offline (the admin-bot checks
patch `decider._tg`, so no message leaves the test). `npm run build`.
`python3 decider.py -h` / `chat` without a token say so and exit non-zero.

**Still open**

- The admin bot token must be set on the SERVER: Accounts & Sessions →
  Pager → Set up (or `ADMIN_TELEGRAM_BOT_TOKEN=` in the server's `.env`),
  then press Start on the bot in Telegram and Send test. The token is a
  secret and is in neither the repo nor this file.
- Facebook and X conditions do not go through the decider yet (their
  collectors print-and-continue); when they do, they inherit all of this.

---

## 2026-09-05 (II) — Accounts page: the Instagram session rows refresh like everything else

**Changed**

- `frontend/src/views/Accounts.jsx` (+ rebuilt `dist`) — `liveIg` is fetched
  every 30 s, as `pool`, `liveX`, `liveFb` and `conds` already were.

**Why**

Seen live: all three Instagram cards said "never signed in on this server"
while their pool half said "last success 1m ago". The session rows were
loaded once when the page opened; three sign-ins later on that same page
they had never been refetched, so the card contradicted itself and the
operator asked whether the accounts were working at all. (They were.)

**Verified**

`npm run build`; no Python change, suite untouched (`RULE_OK=1`).

---

## 2026-09-05 — the browser door: no capture off a warning page; Save Info answered for you

**Changed**

- `ig.py` — `INTERSTITIALS`: `/accounts/scraping_warning`, `/challenge`,
  `/accounts/suspended`, each with the sentence the panel shows.
  `detect_state` checks them before the whoami and returns CHALLENGE with
  `note["hint"]`; UNKNOWN carries a hint too ("give it a moment, Check
  state, or Reload"). `InteractiveLogin.hint`, set by `refresh_state`.
  `InteractiveLogin.settle(log, wait_ms=4000)`: waits up to 4 s for a
  button named "Save Info" / "Save info" / "Save your login info" (role +
  regex `SAVE_INFO`, never "Not now"), clicks it once, gives the page 2.5 s,
  logs it, returns `"save_info"`; anything else returns `""` and the
  capture proceeds as before.
- `web.py` — `_login_start` / `_login_act` return `hint`; `_login_capture`
  calls `settle` (when the window has one — the X window does not) before
  `harvest`, and returns `answered`.
- `frontend/src/views/Accounts.jsx` (+ rebuilt `dist`) — the browser modal
  shows the hint under the page URL (red for a challenge, amber otherwise),
  the done banner says "Login info saved on this phone" when it was, and
  the help text says the window answers the prompt itself.
- `tests/test_all.py` — `test_ig_browser_door` (14 checks): each
  interstitial is CHALLENGE with its hint even when whoami says signed in;
  `/accounts/onetap/` is signed in; UNKNOWN / NEEDS_LOGIN hints; two-factor
  is still the login page; `settle` clicks once, matches both
  capitalisations and never "Not now", waits, logs; no prompt → "" with no
  error; `refresh_state` carries and clears the hint.
- `RULEBOOK.md` §6 (sign-in door 3, two new rules).

**Why**

The operator opened @shoaibakhtar4915's browser on 2026-09-05 10:52 and
saw "Save your login info?" over `/accounts/scraping_warning/` with the
panel saying `unknown`; when they clicked Save Info the window closed.
Two defects. The panel refreshes state only on a click, and the capture
fires the instant the state reads signed in — so the click that answered
the prompt was the click that closed the window, and the prompt was never
answered. And the URL check knew `/challenge` and `/accounts/suspended`
but not the automated-behaviour warning, so the jar was copied with the
warning open; the app client inherited it, and the account was quarantined
within the hour.

**Verified**

`python3 tests/test_all.py` green at 1083, offline. `npm run build` in
`frontend/` (dist committed).

**Still open**

- @shoaibakhtar4915 still has the warning pending on Instagram's side. The
  fix is the same door, once this deploys: open the browser, the panel
  now shows the warning in red with what to do, dismiss it in the frame,
  the window answers Save Info and adopts the session; then Return to pool
  and Promote.
- "Turn on notifications?" (the dialog after Save Info) is not answered;
  the window closes on it, which is what "Not now" amounts to.

---

## 2026-09-04 (III) — Instagram rhythm: phone-time sessions, and one visit at a time

**Changed**

- `ig_human.py` — the day plan. `day_plan(account, now, tz_offset_s)` draws,
  from a seed of (account, local day), 2–4 sessions of 20–120 min spread
  across the active hours (one slot per session, a random start inside it,
  so they never overlap and the morning / afternoon / evening each get a
  turn) plus 0–2 glances of 2–6 min at any hour (a night one survives only
  `NIGHT_POLL_CHANCE` of the time). `session_now` → (in hand, seconds until
  that changes — into tomorrow's first window after the last).
  `planned_seconds`, `local_day`. `visit_gap(budget, planned_s)` = the human
  switch gap + an occasional break, floored at planned ÷ budget. Knobs:
  `IG_SESSIONS_MIN/MAX`, `IG_SESSION_MIN_S/MAX_S`, `IG_GLANCES_MAX`,
  `IG_GLANCE_MIN_S/MAX_S`. `active_now` is kept (the day's bounds).
- `collect_ig.py` — `run_once(max_sources=, due_after=)` makes a pass a
  VISIT: `_collect_account` decides the lookup hold-offs first (they are
  the decider's memory, no session needed), then `_due_sources` picks the N
  most overdue sources not seen within `due_after` (never-seen first, then
  oldest `last_run`, ties by label), reads only those, resolves only their
  names off the following list, and stamps `store.touch_source` on the
  ATTEMPT. A visit says `@acct visits <label> (most overdue of N)` and
  nothing at all when nothing is due; the `plan:` and `done:` lines belong
  to full passes. The decider always sees the account's FULL group. The
  `--loop`: per account, `session_now` with the stable per-phone offset;
  nobody in hand → sleep to the earliest next window (≤ 30 min, so a
  dashboard change still applies); otherwise one visit (`max_sources=1,
  due_after=<cadence>`), then `visit_gap` (floored by `platform_wait_s`).
  `--every` / the dashboard cadence now means "a source seen within this is
  not due". Fetch-now and `run` without `--loop` are unchanged full passes.
- `store_ig.py` — `touch_source(label, at)` and `last_runs()`. `last_run`
  was only ever written by `set_watermark`, i.e. when a source had something
  NEW — so the Watchlists page said "checked 3 days ago" about a quiet
  source checked an hour ago. Now every look stamps it.
- `tests/test_all.py` — `test_ig_rhythm` (33 checks): the plan is seeded,
  per-account, per-day, inside local active hours, non-overlapping, bounded;
  night glances rare over 300 account-days; `session_now` inside / before /
  after the last window / a day with no windows; `visit_gap` floor and
  human minimum; `_due_sources` ordering and cadence; a visit reads one
  source, stamps it, narrates only that, is silent when nothing is due,
  moves on after a failure; a full pass is unchanged.
- `RULEBOOK.md` §6 (new bullet under the Instagram strict rules),
  `BLUEPRINT.md` (the `ig_human.py` row).

**Why**

The operator's plan (2026-09-04): "in a day every account gets a random
time to be on Instagram; while it is, the scraper reads what it needs one
handle at a time, the way a person who opened the app would; the rest of
the time it is quiet." The collector already had human gaps, active hours,
a budget and a warm-up, but the SHAPE of a day was still a machine's: a
thin, even smear from 07:00 to midnight with a burst of every owned source
at the top of each cycle, and the day's budget spent in the first hour then
silence. No person's usage looks like that. Sessions fix the day; the visit
fixes the burst; the floor in `visit_gap` fixes the budget dying early.

Nothing here is a claim about Meta's weights — the precise signals are
theirs. Volume, device/IP consistency and account age are the established
levers; the shape of a day is the plausible next one, and the counters that
would prove it (requests per endpoint per account per day, checkpoints per
thousand requests) are the next step, alongside the app-mix reads
(timeline, reels tray, notifications) and follow-based collection.

**Verified**

`python3 tests/test_all.py` green at 1069, offline. A sample plan for
"sana" on one IST day: 07:50–09:34, 12:25–13:42, glance 15:53, glance
17:01, 17:23–18:15, 21:36–23:24 (5.8 h); "omar" the same day: 11:54–12:38,
14:44–16:14, 22:46–23:53 (3.3 h). Same call, same answer, every time.

**Still open**

- The loop body (`main()` → `loop()`) is exercised by the service only; its
  decisions are pure functions that ARE tested (`session_now`, `visit_gap`,
  `_due_sources`), and the symtable check guards its names.
- The between-visit floor uses the LARGEST plan among the phones in hand,
  because the loop has one clock for all of them. A per-account clock would
  let a phone with a short plan read a little faster; not worth a second
  loop yet.
- Steps 3–4 of the plan (the app-mix reads; follow the shard, read the home
  feed, `feed/user` as gap-fill) and the dynamic headers
  (`X-IG-Connection-Type`, `X-IG-Nav-Chain` are constants in instagrapi) are
  not started.

---

## 2026-09-04 (II) — Instagram profile pictures: stored on the first sighting, from the post itself

**Changed**

- `engine_ig.py` — `record()` keeps `author_avatar`: the `profile_pic_url`
  on the media row's own `UserShort`. It was being dropped with the rest of
  the nested user object. No request is made for it; it is in the payload
  every post already arrives in.
- `store_ig.py` — `posts.author_avatar` (guarded `ALTER TABLE` on open, so
  every existing `ig_results.db` upgrades itself; NULL, never `''`, when a
  sighting had none). `upsert_posts` writes it on the FIRST insert; a post
  seen again fills an empty avatar and never overwrites one, and still
  counts as no new post. New `profiles` table — the per-author cache
  (`user_pk` → newest `avatar_url`, written only by `upsert_posts`) — and
  `set_profile` / `profile_avatar`. `query()` and the new `by_pks()` resolve
  every row's `author_avatar` through the cache (cache first, row second)
  via `_with_avatars`, which joins AFTER the page is selected so
  `_post_filter`'s unqualified clauses stay unambiguous. `to_api` now carries
  top-level `author_avatar` (the shape `WATCH_TOWER_INSTAGRAM_HANDOVER.md`
  has documented all along); `to_feed` serves `''` as `None`.
- `web.py` — the mixed-platform board reads Instagram rows through
  `Store.by_pks` instead of a bare `SELECT`, so it gets the cache too. The
  Instagram feed itself needed no change: `_ig_posts` already backfilled
  only the posts still missing a picture, and now most are not.
- `migrate_ig_sources.py` — `COLUMNS` back in step with `Store._migrate`
  (`posts.author_avatar`, and `sources.assigned_account`, which had been
  missed).
- `tests/test_all.py` — `test_ig_avatar`: record keeps it, first insert
  stores it, re-sighting fills a hole and never overwrites, the cache
  backfills older posts and keeps the newest URL, `by_pks`, both external
  shapes, `''` → null, and an old database upgrading on open.
- `RULEBOOK.md` §6 (the profile-picture rule gains its Instagram clause),
  `BLUEPRINT.md` §5/§7.

**Why**

Every Instagram post in the dashboard sat with a blank circle unless its
handle happened to match an X account we collect. The read-time X
handle-match (`web._x_avatars_for`) was the ONLY path a picture had, because
the collector had been throwing the picture away: `record()` dropped it,
the table could not hold it, and `to_api` did not emit it. The operator's
reading of the rule was the right one — "if the profile picture is empty,
fill it, and on the first time too" — and it is satisfied without a single
extra request, because Instagram puts the picture inside every post.

Two design choices worth keeping. NULL, not `''`, for "no picture": `''` is
a value to `COALESCE` and to the fill-a-hole test, so a blank that counts as
known would be a hole nothing could ever fill. And the cache keeps the NEWEST
URL while the row keeps its first: Instagram signs its CDN links with an
expiry (`oe=`), so the freshest signature is the one most likely to still
open, and a pass over a live source renews it for free — the reader is
served the cache first for exactly that reason.

**Verified**

`python3 tests/test_all.py` green offline, including the new section.

**Still open**

- Instagram `thumbnail_url` / `video_url` carry the same signed expiry and
  are still stored as bare links (RULEBOOK §1.3: media travels as URLs).
  Facebook moved to stored bytes for this reason on 2026-09-02
  (`fb_media.py`); whether Instagram follows is an operator decision, not
  an extension of this change.
- Posts older than this change by an author who has NOT been collected
  since still show the X fallback or nothing; the next pass over that
  source fills the cache and lights them up. No backfill script is needed.

---

## 2026-09-04 — Instagram in parallel: one coherent phone per account, three sign-in doors, N collectors

**Changed**

- `ig_identity.py` (new) — the phone catalogue and the minting rule. An
  identity is an Indian-market handset (Samsung / Redmi / POCO / realme /
  OnePlus / vivo / OPPO with real resolution, dpi, Android release), an app
  build read from instagrapi's own `APP_SETTINGS` (version_code and bloks
  id agree — nothing invented), `en_IN` / IN / +91 / Asia/Kolkata, fresh
  UUIDs, and a matching mobile-Chrome web UA on the Chrome major this
  server's Chromium really is (`chrome_major`, probed in a child process so
  it works from inside the web server's loop). `is_legacy` recognises the
  old library default; `summary`/`describe` feed the card; `web_headers`,
  `playwright_kwargs`, `cdp_user_agent_metadata` make the requests session
  and the streamed window BE the same phone; `stable_offset` shifts each
  account's waking hours.
- `ig_session.py` — `ensure_device` mints through `ig_identity`
  (never `Client().get_settings()`); `reseed` (only ever from a sign-in;
  keeps the old seed as `.bak`); `taken_models`; `DEVICE_KEYS` carries
  `web_user_agent` / `identity`; `persist` splices them back into the
  sidecar and records `meta.exit`; `proxy_check` (exit IP via ipify,
  country via ipapi, instagram.com status, TLS) + `redact_proxy`.
- `signin.py` — `CodeRelay` (the handler BLOCKS the sign-in thread until
  `POST /api/login/code`, five minutes; status via `relay_status`);
  `_check_exit` before any login; `_fresh_phone_if_legacy`; `_needs_browser`
  → `Outcome.needs == "browser"`; `IG_JAR` + `_carry_jar` (the whole browser
  jar into the app client); `ig_browser_adopt` (the browser door's last
  step). A code that never arrives is NOT a checkpoint (no tombstone).
- `ig.py` — `InteractiveLogin` is the account's phone: `_phone()` mints /
  reseeds via `ig_session`, `auth._launch(extra=…)` gets Playwright's
  mobile emulation, `_client_hints()` sets UA metadata over CDP, frames are
  `scale="css"` so clicks map 1:1; `COOKIE_NAMES` gains `datr` and friends.
  `ig.Store`: `active` is a roster — `_demote_others` removed,
  `active_accounts()` added.
- `auth.py` — `_launch(..., extra=None)`; `config.AccountCfg.ig_label`.
- `store_ig.py` — `sources.assigned_account` (additive migration),
  `Source.assigned_account` / `.collector`, `assign_sources` (pin wins and
  waits; sticky; least-loaded + stable hash for the rest; every move
  logged), `assignment_counts`; the connection is `check_same_thread=False`
  with a write lock, because `on_resolved` runs inside `asyncio.to_thread`
  and the id write was raising `ProgrammingError` — swallowed by
  `resolve_user` — so resolved ids were NOT reaching the DB from the live
  path.
- `collect_ig.py` — `collectors()` (owners + benched with reasons),
  `PassLock` (fcntl, `profiles/.ig_pass.lock`), `heartbeat`
  (`profiles/ig_loop.json`), `_collect_account` (the per-account body),
  `run_once` runs the accounts as parallel tasks with `STAGGER_S`, honours
  `dec.account_wait` (resting keeps its sources) and an `awake` set (the
  loop's per-account active-hours draw); the loop sleeps on
  `dec.platform_wait_s()`; `ig_failover` moves sources to the remaining
  collectors and wakes a warm backup only when nobody is left.
- `decider.py` — `account_wait(account)`, `platform_wait_s()`.
- `engine_ig.py` — `_browser_session` sends the identity's mobile Chrome UA
  and Client Hints (was: desktop Chrome on a Mac).
- `accounts_api.py` — promote ADDS a collector ("alongside @…").
- `web.py` — `_pool_account_cfg` sets `ig_label`; `_login_start` returns the
  phone's viewport; `_login_capture` adopts through `signin.ig_browser_adopt`
  and runs `_decider_after_signin`; `_signin_status` carries `waiting_for`;
  `POST /api/login/code`; `GET /api/ig/diag` (git rev, services, heartbeat,
  pass lock, per-account phone / sidecar facts / pool / open conditions —
  cookie NAMES only, no URLs, no UUIDs); `POST /api/ig/account` (bench /
  collect); `POST /api/ig/reseed` (new phone); `/api/ig/status` accounts
  gain `identity`, `exit`, `owns`, sources gain `collector`; Fetch-now
  passes `root` / `who` and reports "a pass is already running".
- `frontend/src/views/Accounts.jsx` — `BrowserLoginModal` (the streamed
  window: frame polling, scaled clicks, type / keys / scroll / reload,
  adoption result); `SignInModal` gets the one-time-code box and the
  "Open this account's browser" door; the IG card shows the phone, "owns
  N", the sign-in exit, and Bench / Collect / New phone. `Watchlists.jsx`
  shows each source's collector and the checkpoint banner points at the
  browser door. `src/api/client.js`: `loginCode`, `loginStart`, `loginAct`,
  `loginCancel`, `igDiag`, `igAccount`, `igReseed`. `frontend/dist` →
  `index-BW98rivS.js`.
- `tests/test_all.py` — `test_ig_identity` (coherence, uniqueness, legacy
  detection, reseed, the client and the web session ARE the phone),
  `test_ig_signin` (the relay end to end, `proxy_check` on six exits, exit
  refusal before any login, legacy reseed only at a sign-in, `needs=browser`,
  `ig_browser_adopt` carrying the jar and the exit), `test_ig_parallel`
  (assignment rules, `collectors()`, two phones interleaving in one pass,
  resting vs benched, the awake set, a checkpoint moving sources, the
  dashboard endpoints against a temp root, `PassLock` across callers); the
  undefined-names sweep covers `ig_identity`, `ig_session`, `signin`, `ig`;
  `test_pager` / `test_accounts_api` reworded for the roster. 1,010 checks.
- `RULEBOOK.md` §6 (the three doors; one coherent phone; the exit check;
  N collectors + one owner per source; one pass per machine; "no sort
  order decides"), §8; `BLUEPRINT.md`; `README.md`; `.env.example`.

**Why**

Instagram had stored zero posts, ever. The 2026-09-03 log, IST: @youssef —
`usernameinfo` 429 three times on every pass, then `PleaseWaitFewMinutes`
on the one feed read it could make; the web fallbacks bounced to the login
page. @sana — `ChallengeRequired` at 23:49, two minutes after a Fetch-now
started a second pass on top of the loop's. @shoaib — `ClientConnectionError`
on every lookup through `resi-in-35`. And the device seeds on disk: `ig_a`
and `ig_b`, minted a day apart, byte-for-byte instagrapi's default Pixel 8
Pro, en_US, US Eastern — three accounts, one phone, three Indian exits, born
from a Mac's cookie. The server-side sign-in could not finish by design:
`challenge_code_handler = lambda: None`. The operator asked for accounts
that look like people, sign in from the server, and run in parallel.

**Verified**

- `tests/test_all.py` — **All checks passed** (1,010) on Python 3.11.15 with
  the pinned deps, in the cloud workspace; `test_signin` 11, `test_pool_link`
  7, `test_accounts_api`, `test_accounts` 13, `test_fb_media` 60 all green.
- `ig_identity.chrome_major()` reads 141 off the workspace's Chromium in
  0.5 s from a child process.
- `frontend` builds clean (vite 5, 50 modules).
- NOT yet run against live Instagram: the three doors, the exit check and
  the parallel pass go live with the deploy that follows this commit; each
  existing account needs ONE fresh sign-in (its legacy phone is replaced
  then). @sanaakhtar221's checkpoint must be cleared through her browser
  door or on a trusted phone first.

- First live run (same night, IST 01:50–02:15): the deploy workflow had
  skipped (no `DEPLOY_HOST` / `DEPLOY_SSH_KEY` secrets), so the pull +
  `update.sh` ran by hand from the VPS terminal. The server's Playwright
  was 1.62 with browsers from an older build (`chromium-1228`; the door
  failed "Executable doesn't exist … 1234") — `playwright install
  chromium` + `install-deps` fixed it (Chrome 151). Background sign-in on
  @youssefnasser168: exit check OK (a Jio exit, and a DIFFERENT one two
  minutes later — `resi-in-31` rotates), legacy Pixel reseeded to a Samsung
  Galaxy A15 · en_IN · Asia/Kolkata, then Instagram answered with a native
  checkpoint → `needs=browser`. The browser door rendered Instagram's
  mobile login through the proxy as that phone, but mouse clicks never
  focused a field while Tab + type did: `ig.InteractiveLogin.click` now
  TAPS (`page.touchscreen.tap`) on a phone-shaped context. The frame is
  height-bound in the modal so the whole screen is visible.

- Morning after (IST 09:55–10:05): @youssefnasser168 signed in through
  his browser door — the operator typed the password into the streamed
  window, Instagram let him in, `ig_browser_adopt` carried the WHOLE jar
  (csrftoken, datr, ds_user_id, ig_did, mid, ps_l, ps_n, rur, sessionid)
  onto the Samsung Galaxy A15, the checkpoint tombstone cleared. The next
  pass (Fetch-now) assigned all ten sources to him, resolved EIGHT handles
  in one request off his following list (the lookups that had 429'd for
  days), and stored the first Instagram posts this project has ever kept
  (`awaj.news new=24`). Then it took a 388 s human break, as designed.
  One bug seen in the night's loop and fixed here: `run_once` called the
  platform-level `dec.ok()` BEFORE checking for collectors, so an idle
  server "recovered from session_missing" and re-opened it every pass —
  two Telegram pings per 30 minutes for nothing. `dec.ok()` now runs only
  once a pass actually has owners.

**Still open**

- The identities minted before Chromium 151 was installed carry
  `chrome_major: 140` (the fallback); the real browser is 151. Coherent
  with each other, not with the binary — a reseed would fix it, at the
  cost of a sign-in. Leave it.
- Webshare's session-pinned exits are not steady: two sign-ins two minutes
  apart left through two different IPs. Instagram will see the account
  moving within one ISP (which real phones on CGNAT also do), but "one IP
  forever" needs a static residential / ISP proxy per account.
- `resolve-ids` (CLI) still uses one login (`_active_account`) unless
  `--account` is given.
- The pool's "one active per platform" summary line is X/FB's model; for
  Instagram the card's "collecting · owns N" is the truth.
- Facebook and X conditions still do not route through the decider.

---

## 2026-09-03 (III) — collection filters now hold on X List watchlists

**Changed**

- `store.py` — `tweet_passes_filters(tweet, filters)`: the same checkbox set
  (`WATCHLIST_FILTERS` + `lang` / `min_likes` / `min_retweets`) applied to a
  PARSED tweet, reading the exact fields `normalize_tweet` stores
  (`retweetedTweet`, `quotedTweet`, `inReplyToTweetId`, media, links,
  `user.blue`/`verified`, `lang`, `likeCount`, `retweetCount`). New
  `streams.filters` TEXT column; `compile_watchlist` copies the watchlist's
  filter JSON onto every compiled stream. `set_watchlist_filters` no longer
  refuses `kind='xlist'`.
- `collector.py` — `apply_settings` re-reads `streams.filters` every poll
  (so a dashboard save takes effect on the next check, no restart);
  `poll_once` / `backfill_once` drop a result that fails the filters BEFORE
  it is committed, count it in `PollResult.filtered`, and still let it bound
  the watermark. Log lines show `filtered=N` when non-zero. `config.StreamCfg`
  gains `filters: dict | None` (never set from config.toml).
- `frontend/src/views/Watchlists.jsx` — the Collection filters panel now
  renders for X List watchlists too, with hint text that says the honest
  thing: the List timeline is read as usual and filtered posts are dropped
  before storage.

**Why**

Filters compiled only into the search query (`-filter:retweets` …). A List
timeline is fetched, not searched, so there was nowhere for the operator to
put them and the panel was hidden for X Lists — "untick RT" had no way to
stop retweets from a List. Post-fetch filtering is the only mechanism a List
allows; it costs no extra requests (same pages) and, because it reads our own
parsed columns, it is the check that actually holds (RULEBOOK's
`-filter:replies` lesson). Search streams go through the same predicate as
belt-and-braces to X's hints.

**Verified**

- New suite section "X List collection filters (post-fetch)" in
  `tests/test_all.py` (18 checks): the trap payload served as a LIST page
  with `skip_retweets` stores the two originals, drops the retweet
  (`filtered=1 new=2`), never writes it to `results.db`, and still sets
  `max_id`/`min_id` from the full result set; unticking collects it on the
  next poll; each checkbox exercised against parsed tweets; an xlist
  watchlist accepts `set_watchlist_filters`, compiles the JSON onto
  `wl:<id>:0`, and `Collector.apply_settings` hands it to the stream (and
  clears it after the box is unticked).
- `tests/test_all.py` — **All checks passed** (849) on Python 3.12 against
  a copy of the tree in the local VM (the suite's sqlite scratch under
  `tests/.tmp` hits "disk I/O error" on the mounted folder; the stray
  `tests/.tmp` from that attempt was moved to `_to_delete/tests-tmp-scratch`).
- `frontend/dist/` rebuilt (`--emptyOutDir false`); bundle moved to
  `index-BL-ILiLj.js`, the stale `index-BgcKJ9uU.js` moved to
  `_to_delete/old-dist-assets/`.

**Still open**

- Already-collected retweets stay (by decision): the filter affects new
  collection only. Hide them in the Live Feed if needed.
- `only_media` / `skip_links` on a List rely on our media/link parsing; a
  media type `_media_urls` does not know would be dropped under
  `only_media`.

---

## 2026-09-03 (III) — one refusal ends the asking: the lookup breaker

**Changed**

- `decider.py` — rule `lookup_throttled` (scope `account/lookups`, action
  HOLD 6h→12h→24h, admin told on the 2nd consecutive refusal, actions
  `retry` / `resolve`); `Decider.fold()` closes per-handle cards a broader
  condition now covers.
- `collect_ig.py` — `_decide_exc` maps a `ResolveError` with `why` in
  (`rate_limited`, `blocked`) to the account condition (pending handles in
  meta); the pass stops asking for the remaining handles; while the hold is
  open no lookup is made from that account (following list included);
  a lookup that works again (`resolve_from_following` hit, or a previously
  unresolved source collecting) closes it.
- `web.py` — `POST /api/decider {action: retry}` (resolve; next pass probes
  once). `Accounts.jsx` — "Name lookups refused — held" card, pending list,
  "Retry lookups now". `frontend/dist` → `index-DzUuAiCd.js`.
- `tests/test_all.py` — 12 checks in `test_pager` (861 pass; `test_facebook`
  still the pre-existing red). `RULEBOOK.md` §6.

**Why**

The Fix panel showed eight identical "Handle needs its numeric id" cards on
@youssefnasser168, opened one to two minutes apart — one per handle, each
"seen 1×", each a 429. The refusal was the session's, not the handle's, so
every ask was wasted and prolonged the throttle. The operator's rule, now
the decider's: the same refusal twice with no move left means stop and
wait for a human, not knock again.

---

## 2026-09-03 (II) — the pager: ping with a link, otherwise fix it yourself

**Changed**

- `decider.py` — condition ids (`platform:account:kind`), `fix_url()` from
  `PUBLIC_BASE_URL`; every ping ends with "Fix it → …?fix=<id>" and
  "Snooze 6h → …&snooze=6". Rules carry `fix` (steps), `actions` (what the
  panel may offer) and `needs_human`. `open_conditions()`, `snooze()`,
  `resolve()` for the panel; `decider_state` gains `snoozed_until_ms` and
  `meta` (additive migration). A paged condition that closes — by itself,
  by a switch to another kind, by sign-in or by "Mark fixed" — pings once
  more ("recovered"). While snoozed: no lines, no ping, still counting.
- `collect_ig.py` — `_decide_exc` is the one path for every account
  exception; a checkpoint quarantines, records `checkpoint_at` in the
  sidecar, and `ig_failover` promotes the first other `ig_accounts.db` row
  with a session, no error and no checkpoint (pool mirrored via the new
  `pool_link.promote`). The decision's meta records the account's sources
  and whether failover happened; the ping says so. `.env` is loaded at
  service start (the units set no EnvironmentFile).
- `web.py` — `GET /api/decider/conditions`, `POST /api/decider` (snooze /
  resolve / reenable_sources / resume); dashboard-only, not in
  `API_KEY_PATHS`. `_decider_after_signin`: a successful IG sign-in closes
  that account's session condition, re-enables its recorded sources, and
  clears the row's error.
- `frontend/src/views/Accounts.jsx` — the Fix panel ("Needs attention") at
  the top of Accounts & Sessions: every open condition, the linked one
  expanded, steps from the rule, buttons from the rule's actions (Sign in
  opens the existing `SignInModal` for the pooled account, or "Add to pool"
  first). `?snooze=` is applied once and dropped from the URL.
  `src/api/client.js` — `deciderConditions`, `deciderAction`.
  `frontend/dist` rebuilt → `index-zAzpVXsY.js`.
- `tests/test_all.py` — `test_pager` (32 checks); one `test_decider` check
  reworded for the transition ping. `RULEBOOK.md` §6 (two rules),
  `BLUEPRINT.md`, `.env.example`.

**Why**

"If the tool needs me, ping me with a link where I fix it; otherwise fix it
itself." The first decider paged, but a page without a destination is a
notification, not a task, and the Sep 2 checkpoint on @sanaakhtar221 took
collection down for a day although two other accounts with sessions were
sitting in `ig_accounts.db`.

**Verified**

- Suite on a staged copy, Python 3.11.15, pinned deps: 785 checks pass
  (`test_facebook` still skipped — the pre-existing graphql media failure).
- `web._decider_conditions` / `_decider_post` exercised against a temp root:
  re-enable restores only the condition's recorded sources; snooze and
  resolve round-trip; unknown action refused.
- Frontend built on the local VM (vite writes to `$HOME/distbuild`, copied
  into `dist/` — the bridge cannot delete the old hashed assets, so stale
  `index-*.js` files remain beside the new one; harmless, `index.html`
  points at the new bundle). Bundle contains the panel.

- Admin contact (same day): `ADMIN_TELEGRAM_CHAT_ID` / `ADMIN_NAME` in
  `.env` — the decider pages the admin (fallback `TELEGRAM_CHAT_ID`);
  `decider.admin_chat()` / `notify_ready()`; panel hint and `.env.example`
  updated; 9 checks in `test_pager`. Watch-Tower is unaffected: no change to
  the post shape, delivery, scoping or the key allow-list — nothing to resend.

- Promote moves collection (same evening): the operator promoted a backup
  in Accounts & Sessions and the log still read `account @sanaakhtar221`
  — the pool and `ig_accounts.db` were never joined.
  `accounts_api._activate_collector` (on `/promote` and `/failover`) now
  sets the active row in `ig_accounts.db` when the account has a session,
  and returns a `note` the card shows either way. `frontend/dist` →
  `index-BdnaKueQ.js`. `tests/test_accounts_api.py` +1.

- `unresolved_source` (first live pass): a resolve failure on one source
  had been filed as `pass_error` and backed the account off. New condition,
  scoped `account/label` (`Event.source`, `_who`/`_split_who`; ids like
  `instagram:sana/Bhajanlal Sharma:unresolved_source`), decision `SKIP`,
  admin paged after 3 in a row, Fix panel "Save id" → `POST /api/decider
  {action: set_id}` → `store_ig.set_platform_id` + resolve. `collect_ig`
  passes the source into `_decide_exc` and calls `dec.ok(acct,
  source=label)` after each clean collect. `frontend/dist` →
  `index-BgcKJ9uU.js`. `test_decider`/`test_pager` +11 (805 pass).

- Resolution, permanently (evening): `engine_ig.resolve_user` runs with
  transport retries off, stops at the first 429/404, tries `users/search/`
  as a second private door, records status codes, and raises
  `ResolveError(why=rate_limited|blocked|not_found|unknown)` with advice per
  kind (throttled → leave it; blocked → residential proxy; 404 → spelling).
  New `IGEngine.resolve_from_following`: one `user_following_v1` call
  fills every followed handle; `collect_ig` runs it per account before the
  source loop and `resolve-ids` runs it first. `decider`: `Rule.loop_wait`,
  `_wait_for`, `Decider.holdoff(account, source)`; `unresolved_source` now
  holds the SOURCE off 1h→24h without delaying the loop, and `collect_ig`
  skips held sources before spending a request ("leaving N unresolved
  source(s) alone this pass"). `engine_ig.check` asserts the three Client
  methods now called. `test_resolve` (27 checks); 831 pass.

**Still open**

- The old hashed bundles in `frontend/dist/assets` should be deleted
  locally before commit (`git status` shows the new one untracked).
- `PUBLIC_BASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` must be set
  in the server's `.env`; the panel says so when Telegram is not configured.
- Facebook's conditions (logged-out session seen Sep 2) are not routed
  through the decider yet.

---

## 2026-09-03 — the decider: one decision per condition, said once

**Changed**

- `decider.py` (new) — a rule-based policy: `Event(kind, platform, account)`
  → `Decision(action, wait_s, say, notify)`. Rules for `paused`,
  `no_sources`, `session_missing`, `session_rejected`, `checkpoint`,
  `rate_limited`, `budget_spent`, `pass_error`, `ok`. One open condition per
  (platform, account) scope, persisted in `activity.db` → `decider_state`;
  announced on open, reminded every 6h, escalated to Telegram once
  (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, the `alerts.py` channel),
  "recovered after …" on close. `classify_exception` maps instagrapi
  exceptions by NAME (no import) and `ig_session`'s `RuntimeError` wrappers
  by text, so a wrapped checkpoint is still a checkpoint.
- `collect_ig.py` — `run_once(..., dec=None)`: every condition routes through
  the decider. A checkpoint quarantines the account in the pool and stops
  the pass for it; a rate limit stops the pass for it; a failed refresh stops
  it (was: `continue`, which knocked once per remaining source). No active
  account is a `session_missing` condition, not a crash. The `--loop` holds
  ONE persistent decider and sleeps `max(ig_human.next_interval, dec.wait_s())`
  — the decision's wait is a floor under the human rhythm. A pass that raises
  is `pass_error` (third in a row pages the operator).
- `activity_log.py` — level regexes know the decider's lines (QUARANTINE /
  session / pass_error → error; a decision or reminder → warn; recovered →
  info).
- `tests/test_all.py` — `test_decider` (30 checks) + `decider.py` in the
  undefined-names sweep. `RULEBOOK.md` §6 + §8, `BLUEPRINT.md` file map.

**Why**

The Activity Log on the server showed "no enabled sources — add one with
`collect_ig.py add-source`" every 20–40 minutes for two days: the IG
service was running against a store with no enabled source. Not an error —
but nothing about it was a decision either. The cadence did not change,
nobody was told, and any real error would have scrolled off behind it. The
same print-and-continue shape sat under every other condition, including the
one that kills accounts: a checkpoint inside `collect_source` was caught by
`except Exception`, logged, and the next source of the same account was
fetched anyway.

**Verified**

- `tests/test_all.py` on a staged copy under Python 3.11.15 with the pinned
  deps: `test_decider` 30/30; the undefined-names sweep covers `decider.py`;
  756 checks pass. `test_facebook` was skipped in that run — it fails on
  `_stories_from_graphql` media (`gp[0]["media"]` empty) BEFORE this change,
  from the 2026-09-02 FB feed-structure work; not touched here.
- Dry run of `collect_ig.run_once` ×3 on an empty `ig_results.db` with one
  persistent decider: one Activity Log row (`decision: IDLE on 'no_sources'
  … next try in 30m; operator will be told if still open in 2h`), then
  silence; `dec.wait_s()` = 1800. Two one-shot runs (Fetch-now path) both
  speak.

**Still open**

- The server still has no enabled Instagram source for any project; add one
  (Watchlists → + New watchlist → Instagram) and the open condition closes
  with a "recovered" line. `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` must be
  set in the server's `.env` for the escalation to reach a phone; without
  them the log says "operator NOT told" once.
- Facebook and X still print-and-continue; their conditions should route
  through the same policy table (`collect_fb.py` has the identical
  `no enabled sources` line).
- `test_facebook` (graphql media) is red independently of this change.

---

## 2026-09-02 (III) — a post seen twice now learns; posted-time recovered from the embed

**Changed**

- `store_fb.py` — `upsert()` reads the existing row instead of a bare `SELECT
  1`, and calls the new `_refresh()`: counts always take the newer number;
  `created_ms` / `author_name` / `author_avatar` fill a hole but never
  overwrite; text is replaced only when the stored copy was shorter (truncated
  at "See more"); `media_json` is replaced only to swap Facebook's expiring
  links for our stored copies, never the reverse. Still returns `False`, so
  "new posts" counts are unchanged.
- `tools/fb_media_backfill.py` — harvests with `show_text=true` and reads
  `<abbr data-utime>` from the embed header, writing `created_ms` where the row
  had none. Reports `posted-time recovered` and states plainly that counts are
  not recoverable this way. Its row selection is now media-OR-time: selecting on
  media alone meant a row the FIRST backfill had already fixed could never come
  back for its timestamp, which is precisely the state that run left behind.
- `RULEBOOK.md` §6, `tests/test_fb_media.py`.

**Why**

The media backfill fixed the pictures and left the metrics dashed, because it
only ever rewrote `media_json` — and the rows themselves were collected by the
DOM path back when it stored neither time nor counts. Worse, the collector
could not heal them either: `upsert` refused a second sighting outright, so a
post already held could never gain a fact it lacked, no matter how many richer
passes saw it. That is dedup doing more than dedup was for.

Two facts are recoverable and one is not. The public embed carries the post's
exact creation epoch in `<abbr data-utime>` (verified: `1786637743` =
2026-08-13 17:35 UTC on the Fadnavis post, consistent with its 17:53 collection
time), so the backfill writes it. It renders Like / Comment / Share as buttons
with NO numbers, so reaction counts are not publicly available for old posts;
they stay null rather than becoming a number we invented. Reels embed as a
nested video plugin with no `data-utime` at all, so they keep a null time.

**Verified**

- `tests/test_fb_media.py` — **60/60** (12 new): a second sighting fills time,
  counts, author and truncated text, replaces an expiring link with ours,
  follows engagement upward, refuses to overwrite a recorded time or author
  with a poorer later value, never downgrades our media back to fbcdn, and
  leaves exactly one row.
- The `data-utime` claim read off the live embed for both a photo post (present,
  exact) and a reel (absent) before the code was written.

**Still open**

- Counts for the 160 backfilled posts stay null unless those posts appear in a
  future Favorites pass, which `_refresh` will now pick up. Posts older than the
  feed's reach keep dashes permanently.
- The extractor's own count/time reading is still unverified against live
  Facebook; one `Fetch Favorites feed` on the server settles it.

---

## 2026-09-02 (later) — Facebook media is stored, not linked; delivery stops rotting

**Changed**

- `fb_media.py` (new) — a content-addressed byte store: `put()` dedupes on
  sha256 and returns `/media/fb/<aa>/<hash>.<ext>`, `resolve()` accepts only
  that exact shape, `sweep()` evicts oldest-first under `FB_MEDIA_CAP_GB`
  (unset = no limit), `absolutize()` rewrites our paths for delivery.
- `engine_fb.py` — `_cache_media()` / `_fetch_image()`, called at the end of
  both `fetch_page` and `fetch_favorites`: each picture is downloaded through
  `self._ctx.request` (browser cookies, bypasses the image-blocking route),
  counted into `self._bytes`, stored, and the item rewritten to our path with
  the Facebook URL kept as `src`. Failure never fails the run.
- `web.py` — `/media/fb/...` served BEFORE the auth gate, immutable cache
  headers; `_send` no longer forces `no-store` when a caller names its own
  `Cache-Control`.
- `webhook.py`, `sheets.py` — media absolutized against `PUBLIC_BASE_URL` on
  the way out.
- `tools/fb_media_backfill.py` (new) — re-harvests posts collected before the
  store existed through Facebook's PUBLIC embed page, which mints fresh image
  URLs, and rewrites their rows.
- `tests/test_fb_media.py` (new), `.env.example`, `RULEBOOK.md` §6.

**Why**

The embed frame shipped earlier the same day made the dashboard show pictures
again, but the operator named its two real faults: it is Facebook's card
inside ours (it breaks the one post shape, §2), and it carries nothing to
Watch-Tower — an iframe has no URL to deliver. Checking the delivery path
confirmed the second point was already a live defect rather than a new one:
`webhook.py` sends `media[].url` verbatim and `sheets.py` writes the same URLs
into the media column, so every Facebook delivery had been handing receivers
links that expire in about five days. Only holding the bytes fixes both.

**Verified**

- `tests/test_fb_media.py` — **48/48**: chrome vs post media (emoji sprite,
  avatar, full-size original), reel typing, dedupe and the six-item cap, count
  and time parsing, store dedupe/refusals, seven malformed media paths refused
  by `resolve` (traversal, wrong shard, unknown extension, `.php`), eviction
  order, and delivery absolutization including the no-base fallback and other
  platforms passing through untouched.
- The backfill's premise was tested against the live embed before the tool was
  written: the public `plugins/post.php` page for the Fadnavis post (whose
  stored links are dead) carries 7 fbcdn images, 6 freshly signed, the post
  photos at 500-850px — big enough for the card and a lightbox.
- `embed_href` on real stored URLs strips `__cft__`/`__tn__` from post, reel
  and `story.php` shapes while keeping `story_fbid`/`id`.
- `ast.parse` on all five changed modules; `python3 tests/test_fb_media.py`
  runs on 3.10 (imports neither `config` nor playwright).

**Still open**

- Nothing here has run against live Facebook: `_cache_media` needs one fetch
  on the server, and the backfill needs `--dry-run` then a small `--limit`
  before the full pass.
- `PUBLIC_BASE_URL` was set in `.env` to `https://scraper.vedictech.in`.
  If the dashboard ever moves, delivered media URLs move with it — old
  deliveries keep pointing at the old host.
- nginx proxies `/media/fb/` to the app like everything else. Serving it from
  disk with an `alias` would be faster; not done, because `nginx-app.conf` is
  rewritten by `setup.sh` and the install path is not knowable from here.
- Videos store the still only; playback stays on the permalink.

---

## 2026-09-02 — Facebook: expired media, a DOM path that dropped the facts, and a dead Favorites button

**Changed**

- `frontend/src/components/PostCard.jsx` — reads the `oe=` expiry out of a
  stored fbcdn URL and, when it has passed (or the image errors), renders
  Facebook's own post embed for that post's permalink instead of a dead
  thumbnail. Frames mount lazily via `IntersectionObserver`; the permalink is
  stripped of `__cft__`/`__tn__`; "Download media" is hidden on framed cards.
- `frontend/src/styles.css` — `.card.fbframe` widens the media track to 348px
  for framed cards; `.fb-embed` (420px, 620px for reels/videos), both collapsed
  to one column on narrow screens.
- `frontend/src/views/Watchlists.jsx` — "Fetch Favorites feed" is no longer
  disabled when the watchlist holds zero pages.
- `engine_fb.py` — the DOM fallback now returns `author_avatar`, `time_text`,
  `utime` and `counts_raw`, and expands every "See more" before reading the
  body. New pure helpers `_time_ms`, `_is_post_image`, `_clean_media`; the
  existing `_num` gained a loose fallback so it can read a count out of a label
  ("1.2K reactions"). All three build paths run media through `_clean_media`,
  and `_build_feed` attributes a post from its permalink when the byline link
  is missing rather than dropping it.
- `RULEBOOK.md` §6 — four Facebook rules: favorites needs no page rows, the
  fbcdn link is perishable while the permalink is durable, the DOM path carries
  the facts, and post media is what the post shows.

**Why**

Images had stopped rendering on saved Facebook posts. The cause is not our
markup: Facebook signs every fbcdn URL with an expiry inside the URL, and the
sampled post from `fb_pages` (collected 13 Aug) carried `oe=6A842163` =
18 Aug 09:09 UTC. Opening it returns the plain text "URL signature expired".
So the links in the database were already dead and nothing on the render side
could have fixed them — the only durable handle we hold on a post's pictures is
its permalink, which is what the embed frame renders. We store no bytes by
choice (the engine blocks image/media/font under the byte cap), so framing is
the whole answer rather than a fallback.

Reading the same API response showed the rest: `created_at`, `like_count`,
`reply_count` and `retweet_count` were null on every one of the four sampled
posts, `author_display_name` was the handle, text ended at "… See more", and
one "photo" was a Facebook emoji sprite. That is the DOM fallback's output —
the Favorites feed had been landing there — and it had been written as a
last-resort shape that stored almost nothing.

And the operator could not re-fetch to test any of it: both fetch buttons were
disabled because the watchlist holds 0 pages, though favorites mode reads the
account's own feed and needs none.

**Verified**

- The expiry claim, from the source: the stored URL opened in a browser returns
  "URL signature expired", and `fbExpiryMs` decodes its `oe` to
  2026-08-18T09:09:55Z — 5 days after collection.
- The embed renders what the dead link cannot: `plugins/post.php` on the
  Fadnavis permalink (collected 13 Aug) shows the byline, the text and all five
  photographs; the Amit Shah `/reel/` permalink plays.
- `_num` / `_time_ms` / `_is_post_image` / `_clean_media` — 21/21 against
  labels, relative and absolute times, `data-utime`, junk input, avatars, the
  emoji sprite, a full-size original, reel typing and dedupe. `_time_ms` was
  checked to never return a future time.
- `fbExpiryMs` / `fbLinkDead` / `fbEmbedHref` — 10/10 from the shipped
  PostCard.jsx source, including the real expired URL, an unsigned URL (NOT
  dead), a garbage `oe`, and `__cft__` stripping on post, reel and story URLs.
- `node --check` on the extractor JS; `ast.parse` on `engine_fb.py`;
  `frontend/dist` rebuilt (`index-TTHc9DVQ.js`, `index-CyR8QxWa.css`).
- The frame was measured in place, not assumed: a mock card at the real grid
  (`minmax(0,1fr) 348px`) with the live embed showed the plugin renders the
  byline and caption above the pictures — it ignores `show_text=false` for
  photo posts — so 420px cut a five-photo grid in half. `.fb-embed` is 540px
  because that is what fit.

**Still open**

- Not run against live Facebook: the extractor changes need one `Fetch
  Favorites feed` on the server to confirm time/counts/author actually land.
  Until then the DOM path's new fields are verified only offline.
- The 160 posts already collected keep their dead links; they render through
  the frame, but their stored `created_ms`/counts stay null — a re-fetch does
  not revisit them (dedup refuses them by id and signature).
- `frontend/dist/assets/` still holds the two previous bundle files: the remote
  bridge can create and overwrite but not delete. Remove
  `index-BlhS2xKy.css`, `index-DQop_0GD.js`, `index-BN-YsL7z.js` and
  `index-De-hU1zQ.css` locally when convenient; `index.html` points at the
  current pair.

---

## 2026-08-31 (later) — all three Instagram accounts were active at once

**Changed**

- `ig.py` — new `Store._demote_others()`; `save()` and `set_active()` call it
  whenever they activate a row, so the table holds exactly one active account.
  Deactivating still promotes nobody, and `save(active=False)` leaves the
  current active row alone.
- `RULEBOOK.md` §6 — "exactly one active row, enforced on write".

**Why**

On the server, `SELECT username,active FROM accounts` returned `active=1` for
all three of sanaakhtar221, shoaibakhtar4915 and youssefnasser168. `save()` has
always written `active=excluded.active` and demoted nobody, so every import
after the first simply added another active row.

Nothing was collecting twice: `collect_ig._active_account()` filters to active
rows and takes `rows[0]`, and `Store.all()` orders by `active DESC, username`.
The bug is subtler and worse — the collecting account was being chosen by
ALPHABETICAL ORDER. `sanaakhtar221` sorts first, so it collected; remove or
rename it and collection moves to `shoaibakhtar4915` with no decision taken,
nothing logged, and the residential proxy still labelled for the account that
is no longer running. The panel compounded it: `web.py` reports every
`active=1` row as a live session, so the board showed three.

**Verified**

- Store exercised directly against a temp DB: three consecutive
  `save(active=True)` leave one active row (the last writer); `set_active(x,
  True)` moves it; `set_active(x, False)` leaves zero active rather than
  auto-promoting; `save(other, active=False)` does not disturb the active row.

**Open**

- The existing rows still need fixing by hand — the invariant is enforced on
  write, and nothing rewrites history. One `UPDATE` on the server.
- `ig_accounts.db.last_used` is written NULL on insert and never updated by
  anything; the column is dead and the panel cannot show a real per-account
  last-use for Instagram.
- Still unaddressed: the pool cannot bench an IG account. `pool.db` status and
  `ig_accounts.db.active` are independent, so the panel's Active badge and the
  collecting account can disagree — as they did here (panel: Youssef active,
  reality: Sana collecting).

---

## 2026-08-31 — the account Edit form could not set a proxy

**Changed**

- `frontend/src/views/Accounts.jsx` — `EditModal` gains **Residential proxy
  URL** and **Notes**. The proxy box is write-only, mirroring password/TOTP:
  blank keeps what is on file, a new URL replaces it, and a separate
  "Remove the stored proxy" checkbox (shown only when one exists) is the only
  way to clear it. The helper line says whether a proxy is on file, and that it
  lands on the account's next sign-in.
- `accounts_api.py` — `_acct_json` now returns `notes`. The column existed on
  `Account` and `/update` already accepted it; nothing ever sent it to the
  panel, so a Notes box would have been editing blind.
- `RULEBOOK.md` §5 — "a write-only secret's blank box means KEEP, never CLEAR".

**Why**

`AddModal` has taken `proxy_url` since the pool landed; `EditModal` only ever
took `proxy_id` — a human label like `resi-in-01`, which nothing dials. So the
three X accounts already in the pool could not be given a proxy at all without
deleting and re-adding them, while `guard` was warning that two of them share
one IP. The store (`update(proxy_url=...)`, `enc_proxy`) and the API route
already did the work; only the form was missing.

**Verified**

- `_Cipher.encrypt("")` returns `""`, so `proxy_url: ""` really clears
  `enc_proxy` and flips `has_proxy` to false — the remove checkbox works rather
  than storing an encrypted empty string.
- Chain confirmed end to end for X: `enc_proxy` -> `get_proxy()` ->
  `_pool_account_cfg` -> `AccountCfg.proxy` -> `auth` login -> `pool.save(acc)`
  -> the `proxy` column in `accounts.db` -> twscrape collects through it.
  Setting a proxy therefore requires a re-sign-in to take effect.

**Open**

- The panel still cannot bench an account for real: promote/demote writes
  `pool.db`, while the watcher reads `accounts.db` and `collect_ig` reads
  `ig_accounts.db`, and nothing syncs downward (`pool_link` is one-way by
  design). Until that exists, benching is a manual `UPDATE ... SET active=0`.
- Nothing calls `failover()` automatically; the X watcher never reports health
  into the pool at all, so there is no signal to trigger it on.

---

## 2026-08-25 (later) — request signing: X's legacy build grew 16-hex chunk hashes

**Changed**

- `engine.py` — `install_xclid_shim()`: replaces
  `twscrape.xclid.get_scripts_list` with the same logic and a chunk-hash
  width of 7..64 instead of exactly 7. Installs at import, only while the
  upstream source still carries `[0-9a-f]{7}`; `XCLID_SHIM` records
  "installed" / "not-needed" / "skipped". Two new `check()` lines (26 now).
- `auth.py` — `import engine` at top, so `open_api()` — every process's
  route to twscrape — has the shim in place.
- `tools/xclid_probe.py` — new. Fetches https://x.com/tesla the way the
  signer does and reports what came back; ends with an end-to-end
  `XClIdGen.create()` as shipped and with the shim.
- `tests/test_all.py::test_xclid_shim` — the 16-hex shape parses, upstream
  still fails on it (control), modern build and logged-out detection are
  untouched.
- `RULEBOOK.md` §6 — "POOL STARVED on every stream is a signing failure
  until proven otherwise".

**Why**

After the 0.20.0 deploy the watcher logged, for both accounts and every
queue, `XClIdParseError: X web scripts not found` followed by
`stop=no_account_or_abort << POOL STARVED`. The probe showed the cause:
anonymous fetches get X's modern build (`/x-web/entry-client-logged-out`),
but logged-in sessions get the legacy webpack build, whose chunk map now
has 1,025 entries with 16-hex values (`main.3fc0640facfee243a.js`).
twscrape's legacy fallback regex `(\d+):"([0-9a-f]{7})"` matches none of
them. Everything else the signer needs (the `ondemand.s` chunk name, the
`twitter-site-verification` key, the four `loading-x-anim` SVGs) is still in
the page. Not an IP block: HTTP 200, no challenge title, no logged-out entry.

**Verified**

- Against the real HTML saved from the VPS: upstream 0.20.0 raises the
  production error; the shim returns 1,023 chunk URLs including
  `ondemand.s.b7dbcfcff298f890a.js`, and reproduces the page's actual
  `i18n/en.c085…a.js` link (so the `a` suffix convention still holds).
- `doctor --selftest` 26/26; suite 731/731; side suites green.
- Live signing (`XClIdGen.create` on the VPS) is the probe's last line and
  is what to confirm before restarting the services.

**Open**

- Report upstream (vladkens/twscrape) so the shim can stand down; the
  guard makes that automatic once their regex changes.
- The `_to_delete/xclid_*.html` capture on the Mac can go.

---

## 2026-08-25 — twscrape 0.19.2 → 0.20.0 (X changed its search payload)

**Changed**

- `requirements.txt` — pin bumped to `twscrape==0.20.0`, with the reason
  recorded next to it.
- `engine.py` — `PINNED_VERSION = "0.20.0"`; four new `check()` assertions
  for the 0.20.0 behaviours we now rely on: `_is_stalled` pagination stop,
  `GqlFeaturesOutdatedError` replacing `exit(1)`, transport errors cooling
  and rotating instead of raising, and `Tweet.parse` resolving an author
  from `core.user_results` when the `users` map is empty.
- `collector.py` — three things:
    * `orphaned_payload_error()`: a poll or backfill pass whose results ALL
      failed to parse is `STOP_ERROR`, with the first parse failure and the
      next step in the message. Backfill additionally returns before
      `save_backfill`, so no cursor advance and no budget spent.
    * `min_id`/`max_id` now come from results that parsed, in both
      `poll_once` and `backfill_once`. Before, they came from entry ids, so
      the watermark advanced over orphaned tweets — see Why.
    * `describe_error()`: a `GqlFeaturesOutdatedError` is reported as
      "twscrape is out of date for X's current API … bump the pin, run
      doctor --selftest" instead of a bare class name. Starvation comment
      updated (a dead proxy now also lands there).
- `RULEBOOK.md` — §6 "pinned versions turn 'the platform changed' into a
  loud failure" corrected: they catch a changed library, not a changed
  platform. §3 new rule: all-orphans is an error and the watermark only
  moves over stored tweets. §7 new rule: how to upgrade a pinned scraper.
- `auth.py`, `store.py`, `engine_ig.py` — docstrings that cited 0.19.2 line
  numbers or behaviours corrected; no code change.
- `tests/test_all.py` — four regression tests: the untyped-author payload
  parses with zero orphans; an outdated-features error is an actionable
  `STOP_ERROR` rather than a process exit; an all-orphan poll is
  `STOP_ERROR` and leaves the watermark alone; a single orphan among parsed
  results is not an error and the watermark candidate is the newest STORED
  tweet.

**Why**

Collection went quiet on the pinned 0.19.2. twscrape 0.20.0 (released
2026-08-07) says why: X rotated every GraphQL operation id and stopped
tagging the author object inside each tweet as `__typename: "User"`. Our
parse path builds the `users` map from exactly those tags, so it came back
empty and `Tweet.parse` raised `KeyError` for every hit. In `parse_page` that
is a `parse_failure` per tweet: every result an orphan, nothing stored, and
the poll itself reported healthy. The collector was designed to see
starvation, not "the payload changed shape under us": the `orphans` count
went into the polls table on every poll and nothing read it.

Worse, found while writing the rule: `res.max_id` was the newest ENTRY id,
parsed or not, so every one of those broken polls advanced the watermark
past tweets that were never stored. Upgrading the library alone would have
left the next poll stopping at that watermark, with the days of orphaned
tweets permanently behind it. Hence the two collector changes above rather
than a bare pin bump; a `backfill` grant is the recovery for what the old
code already lost.

Also in 0.20.0, and relevant to us: pagination now stops when X echoes the
previous cursor or page (saves the 3 empty-page retries on backfill tails;
the collector reads it as `STOP_EXHAUSTED`, which is right); a (336)
"features cannot be null" answer raises instead of calling `exit(1)` from
inside the request loop (that used to take the whole watcher down — now it
is one stream's `STOP_ERROR`, with a message that names the fix); transport
errors retry 3× with backoff, then cool the account for 60s and rotate
instead of raising `ConnectError` (a dead proxy therefore reads as
`STOP_STARVED` and the twscrape log line "cooling account for 60s" is the
tell); `(-1) LoadShed` is retried on the same account; `add_account_cookies`
became an upsert and `relogin`/`login_all` skip `password="_"` rows — none
of which we call, `pool.save()` remains our only write path.

**Verified**

- `python3 main.py doctor --selftest` → all 24 checks OK on 0.20.0.
- `python3 tests/test_all.py` → 723/723; `test_accounts`, `test_accounts_api`,
  `test_signin`, `test_pool_link` all pass.
- Control: the new untyped-author fixture run against 0.19.2 in a scratch
  venv gives 3 results, 3 orphans, `KeyError` on every tweet — the exact
  live symptom — and 0 orphans on 0.20.0.

**Open**

- Deploy: `deploy/setup.sh` installs from `requirements.txt`, so a redeploy
  picks the pin up; the running services must be restarted after it.
- Not verified against a live account from this checkpoint; the first live
  poll after deploy should show `orphans=0` in `doctor`.
- Every X stream's watermark currently sits past tweets the broken parser
  never stored (however many days 0.19.2 ran against the new payload). They
  will not come back on their own: grant each affected stream a `backfill`
  covering that window.

---

## 2026-08-25 — diag_project.py, for silent collection stalls

**Changed**

- `tools/diag_project.py` — new, read-only. Diagnostic only; no behavior
  changed, so no rule was added.
- `BLUEPRINT.md` — file-map row.

**Why**

The iSupportNamo project stopped collecting and the dashboard could not say
why: Refresh reported "0 new from X", which is the SAME message for "the
account posted nothing", "every filter excluded it", and "the poll errored".
Nothing in the UI distinguishes them, so the operator has no next step.

The likely trap this tool is built to expose: a collection filter that is
individually reasonable and fatal for the account it is applied to.
`min_faves:N` against a handle whose posts sit at 0 likes, or `lang:en`
against a handle that posts in Hindi, excludes 100% of its output — and the
dashboard shows a healthy green "Collecting" the whole time. So the tool
prints the compiled query beside the language and like distribution of what
that stream has already collected, which makes the mismatch self-evident.

**Verified**

- Exercised against a seeded database covering both traps: a `min_likes=5`
  filter on posts that top out at 1 like, and `lang=en` on a stream holding
  4 Hindi posts to 1 English. Both flagged.
- `ast.parse` clean, and the f-strings avoid nested same-quotes so it runs on
  Python 3.10+ (the VPS is not guaranteed to be 3.12).
- Opens the DB `mode=ro` — safe to run while the collector is writing.

**Still open**

- `_project_fetch` returns a per-stream `polled[]` carrying each stream's
  `error`, and `refreshNow` in `LiveFeed.jsx` throws it away, printing only
  `r.new`. A rate-limited or dead account is therefore reported to the
  operator as "0 new from X". Not fixed yet — it needs a rule (RULEBOOK
  already says "a missing state IS a state") and an operator decision on how
  loudly to surface it.

---

## 2026-08-25 — Live Feed filter memory, modal stacking fix, hook coverage

**Changed**

- `frontend/src/views/LiveFeed.jsx` — the filter bar (Source / Sort / Duration
  / Category) persists to `localStorage` under `collector.feed.filters`, keyed
  by project. Read-back is validated against the options that currently exist;
  both read and write tolerate storage being unavailable.
- `frontend/src/components/ui.jsx` — `Modal` renders through
  `createPortal(…, document.body)` instead of in place.
- `deploy/pre-commit` — the living-rulebook check became a deny-list.
- `RULEBOOK.md` §7 — three rules added (overlay portals, persisted view state,
  deny-list guards). §8 — `CHECKPOINT.md` registered as a protected document.
- `BLUEPRINT.md` — file map and the done-since list.

**Why**

The Live Feed is a route, so leaving it unmounted the component and every
`useState` returned to its default. An operator who set Source=X / Last 7 days,
stepped over to Watchlists and came back was shown "everything" while believing
they were still looking at one platform and one week.

The "New project" modal rendered UNDERNEATH the post media thumbnails, which
covered the name field it exists to collect. Cause: `nav.side` is
`position: sticky`, which creates a stacking context even at `z-index: auto`,
so the modal's `z-index: 50` was scoped to the inside of the navbar;
`main.content` follows it in the DOM, so the feed's positioned descendants
(`.thumb` is `position: relative`) painted over the dialog and its scrim.

Both landed with no rulebook entry because the pre-commit hook never checked
`frontend/src/`. Looking into that turned up the larger hole: the hook's
allow-list of behavior files used globs that excluded the files they appeared
to name — `engine_*.py` misses `engine.py`, `store_*.py` misses `store.py`,
`ig_*.py` misses `ig.py`. The X engine and the X store had been exempt from the
living-rulebook rule since the hook was written.

**Verified**

- Storage helpers extracted and run against a stub `localStorage`: 10/10 —
  round-trip, per-project isolation, unknown values falling back, corrupt JSON,
  and `dur: "toString"` rejected by the `hasOwnProperty` guard.
- Stacking diagnosis proved from the screenshot's pixels, not by eye: the scrim
  IS painting (everything behind reads `rgb(153,153,153)` = white under
  `rgba(0,0,0,0.4)`) while the thumbnails stay at full brightness and the modal
  body is pure white — i.e. the thumbnails paint above BOTH the scrim and the
  dialog, which is the signature of a trapped stacking context rather than a
  missing overlay.
- Hook rewrite exercised against 30 paths: the eleven previously-uncovered
  modules now register as behavior; `tests/`, `tools/`, the FB probes and
  `frontend/dist/` stay exempt. `bash -n` clean.
- `tests/test_all.py` — **All checks passed.** Note: the suite needs Python
  3.11+ (`config.py` imports `tomllib`) and the local Linux VM only has 3.10,
  so it was run against a staged copy on 3.11.15 with the pinned deps
  installed. Nothing Python changed in this commit, so this is a baseline
  confirmation rather than a test of the change.
- `frontend/dist/` rebuilt; bundle hash moved to `index-DUN_udqY.js`, which is
  the proof the `ui.jsx` portal actually shipped (only that file changed in
  this build). `bash -n deploy/pre-commit` clean.

**Still open**

- The Live Feed's load-more depth (`pageN`) and scroll position still reset on
  return. Deliberate: restoring them means refetching every loaded page.
- `git` operations from the remote session leave a stale `.git/index.lock`
  (the bridge can create files but cannot delete them). Run `rm -f
  .git/index.lock` locally if a commit refuses to start.
