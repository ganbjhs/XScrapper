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

**Still open**

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
