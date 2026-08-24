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
2. **Extract here; analyse in Watch-Tower — with ONE named exception.** This
   tool collects, normalizes, stores, and delivers. Sentiment, scoring, entity
   work, topic modelling — none of it lives here, and the Collector must not
   grow a general analysis layer. The single exception, added 2026-08-22 at the
   operator's explicit request, is **content labelling**: one
   operator-triggered category per post, produced by an external model
   (`classify.py` → Grok), stored as a stamped fact beside the post.

   Why this one and nothing else. The dashboard is where the operator triages,
   and triage without a label means reading every post; a client watching four
   political beats cannot do that by hand. The old rule's real worry —
   *"duplicating analysis here would only create two answers that can
   disagree"* (`store_ig.py`) — is answered by the label travelling WITH the
   post in delivery, so Watch-Tower reads this answer rather than forming a
   second one.

   The boundary, which is the part that must not erode: labelling is never
   automatic, never on a keystroke, never a side effect of collection, and
   never feeds back into WHAT gets collected. It is a button, it says what it
   spent, and a human's correction always outranks the model's. (The spend cap
   and the per-run ceiling were removed on 2026-08-24 — the boundary was never
   the ceiling, it was the button.) Anything beyond one category per post — scores, summaries,
   sentiment, entity extraction, a second model — is still Watch-Tower's, and
   adding it is a new operator decision, not an extension of this one.
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

### Instagram and Facebook data is PROJECT-SCOPED, and the default is closed

A request that names no project returns NOTHING, and says so. It does not
return everything.

Both platforms used to accept `?project=N` and do `pid or None` at every call
site, and `None` reached the store as "no WHERE clause". Scoping therefore
worked exactly as long as the caller remembered to ask for it, and silently did
not when they forgot. **A filter that defaults open is not a boundary, it is a
suggestion** — one dropped query parameter (a bookmarked URL, a refactored
fetch, a curl during debugging) and one project's operator is reading another
project's collection. Instagram was worse: it had no project mapping at all,
and `_metrics_json` drew the same global chart under every project.

The rules:

- **Every `/api/ig/*` and `/api/fb/*` DATA endpoint requires a project** —
  status, posts, source, fetch, favorites. No project, no data, with the reason
  in the response (`web.py::_NO_PROJECT`).
- **Enforcement is at the BOUNDARY, not in the store.** `store_ig`/`store_fb`
  still accept `project_id=None` for "every project", because the collector has
  to walk every source in one pass and the migration has to see rows that
  belong to nobody. Capability underneath, policy at the edge — one place to
  audit, and no chance of a collector silently collecting nothing because a
  filter defaulted closed.
- **A post inherits its project from the source that collected it**, stamped at
  write time (`store_ig.upsert_posts`). Reassigning a source does NOT rewrite
  history: a post genuinely was gathered for whoever was watching at the time.
- **The external API key CARRIES its scope** (`api_keys.project_id`). Not a
  query parameter — a third party must not be able to ask for another project
  by editing a number in a URL. A key issued before scoping has project 0 and
  is REFUSED with an explanation, because its old behaviour was "all Instagram
  data" and quietly preserving that is the exact leak this closes.
- **Project 0 means UNASSIGNED, never "all".** A parked source still collects;
  it is invisible, not disabled. `collect_ig.py list-sources` names parked
  sources explicitly so they cannot be forgotten.
- **Operational switches are NOT scoped.** Pause/resume, cadence, login health
  describe the one collector, not anyone's data. Pausing Instagram pauses
  Instagram. Accounts are likewise global — a login belongs to the server, and
  the Accounts panel shows it in every project view.
- **An endpoint that returns BOTH server facts and project data SPLITS; it does
  not refuse wholesale.** `_ig_status` and `_fb_status` each carry two kinds of
  thing: the login, its circuit-breaker health and the collector config (the
  server's) alongside sources and totals (the project's). The server half must
  answer whatever the project, because the Accounts & Sessions panel calls both
  with no project at all — it is describing the machine. Only the project half
  is gated. (Paid for the same day the gate landed: `_fb_status` returned the
  bare refusal object and blanked the Facebook account card, which had been
  correct to ask unscoped. **Closing a default means finding every caller that
  was relying on it being open** — grep the frontend for the endpoint before
  assuming a refusal is safe.)

### The three-column source: a PERSON, a HANDLE, an ID — never one string

A source row holds three different facts with three different lifetimes, and
they may never share a column:

| column | is | example | written by |
|---|---|---|---|
| `label` | the **person** | `Narendra Modi` | **a human, only** |
| `value` | the **handle** on this platform | `narendramodi` | a human |
| `platform_id` | the platform's **numeric id** | `1550693326` | the collector |

- **`label` is the cross-platform identity key, and nothing automated may write
  it.** The same person is `@narendramodi` on Instagram and `@modinarendra` on
  Facebook; `label` is what makes those one profile — one avatar (§6, X is the
  canonical avatar source), one display name, one `WHERE label = ?` instead of
  three hand-maintained handle lists. It is also the only one of the three that
  cannot be re-derived if it is lost.
- **`platform_id` is a CACHE derived from `value`.** Resolve once, store it,
  fetch by it forever. If `value` changes, the cached id is stale and MUST be
  dropped — an id pointing at a name the row no longer claims collects the
  wrong person's posts under the right person's identity, silently, which is
  the worst failure available here.
- **An unresolved row still fetches by name** (`user_id` returns
  `platform_id or value`). This is what makes the model safe to deploy: nothing
  breaks on the first pass, rows simply stop failing as they fill in.

Paid for on 2026-08-20 (see §6, Instagram). Implemented for Instagram;
`IDENTITY_MODEL.md` carries the design and the migration order for Facebook
(whose `sources.label` is still the page handle, so it has no identity column
yet) and X (whose `watchlist_members` already has all three, under other
names).

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
  budget the caller never agreed to. This covers backfill grants too: an absurd
  page count is refused with the ceiling named, never trimmed to fit.
- **A compiled query states its own precedence.** Watchlist rules are OR-joined
  into one X query, so every rule that is more than one token is parenthesised
  before joining — `compile_term`, both for the AND form and for a bare
  multi-word rule, which is an implicit AND to X. Ungrouped, `Devendra Fadnavis
  OR Deva Bhau` asks X's parser to decide whether that is two people or one
  word shared between them. It binds OR loosest and gives the right answer
  today; that is a detail we do not own and cannot test against, and inheriting
  it would mean a change at X quietly altering what every keyword watchlist
  collects. Quoted phrases and single tokens are already atomic and stay bare —
  parens there only spend query length against the ~512 cap that chunking has
  to respect.
- **AND over an OR group DISTRIBUTES; it never nests.** `(a OR b) AND c`
  compiles to `(a c) OR (b c)`, flattened into the watchlist's single OR list —
  not to `((a OR b) c)`. X does not honour a group three parens deep: it
  flattens it and starts matching the alternatives on their own. Observed
  2026-08-23, a rule reading `(सीएम OR मुख्यमंत्री) AND महाराष्ट्र` collected a post
  containing मुख्यमंत्री twice and महाराष्ट्र not at all. Nothing in a compiled query
  may exceed two parens deep, and `expand_term` is where that is enforced.
  Distribution costs query length, so it is capped (`MAX_ALTERNATIVES`) and a
  rule that multiplies past it is refused with the reason — separate rules OR
  together anyway.
- **Labelling is a fetch too, and spends money instead of budget.** Everything
  the two rules above say about going out to a platform applies to going out to
  Grok: explicit (the Classify button, never a timer), serialized behind ONE
  lock (`_CLASSIFY_LOCK` — two concurrent runs would read the same "unlabelled"
  set and pay twice for the same posts), and it reports what it cost in dollars
  when it finishes. Month-to-date spend is still metered in `label_runs` and
  still shown, but it is a METER and not a limit: as of 2026-08-24 one press
  covers every unlabelled post in the project, and the only things that end a
  run early are a provider auth or quota failure, each reported by name.

  The cap and the per-run ceiling are gone on purpose. A ceiling that silently
  leaves half the archive unlabelled is the same sin as a trimmed page request
  — the operator asked for the job and got a fraction of it, with the shortfall
  visible only as a number nobody reads. If a budget ever needs enforcing
  again, it must refuse the whole run and name the figure, never shorten it.

  Because a whole-archive run outlives any sane HTTP timeout, `/api/classify`
  starts the job and answers at once; `/api/labels/status` carries a `run`
  block with total/done/failed/cost, and the dashboard shows a bar. Batches go
  out `CLASSIFY_FANOUT` at a time; the writes stay serialized on the one store
  connection.
- **A post is paid for once.** A run only ever sends posts with no label. A
  re-classification is a separate, deliberate act — never something a changed
  category or a new prompt version triggers on its own, or editing a word in a
  description would silently re-bill the whole archive.
- **A human's label outranks the model's, permanently.** `post_labels.source`
  is `'human'` or `'grok'`, and a `'grok'` write refuses to touch a `'human'`
  row (one WHERE clause in `store.set_post_labels`). That is the whole reason
  re-running is safe. It is also why nothing automatic may ever write a label
  onto a SOURCE: a category attached to a person is an identity fact, and
  `IDENTITY_MODEL.md` reserves those for humans. Labels attach to POSTS.
- **The category vocabulary lives in the database, and the prompt is built from
  it.** Categories are per project, edited in the dashboard, and their
  `description` is handed to the model verbatim. An editor whose text never
  reached the prompt would be theatre. The catch-all (`other`) may be renamed
  but not archived: without somewhere to put a post about cricket, the model is
  forced to file it under a political category, and the Hate Speech board fills
  with noise.
- **Per-source cadence is the source's own.** X watchlists and FB pages each
  carry their own check interval; a scheduler collects only what is *due*. Idle
  ticks cost nothing (no browser opened when nothing is due).
- **The watermark walk is FORWARD-ONLY, and history is a separate, budgeted
  act.** Stopping at the first already-seen post is what makes a poll cost one
  request — and it also means a poll can never reach further back than
  `page_size x max_pages_per_poll`. On a live account that ceiling is invisible.
  On an archival query (`from:someone until:2025-02-20`), a dormant account, or
  anything whose newest post is already stored, it is permanent: the stream sits
  at exactly that number forever, polling on schedule, finding nothing, and
  looking from the dashboard as though collection died. Reaching older posts is
  `collector.backfill_once` — a separate pass that resumes from a stored cursor,
  spends an operator-granted page budget a few pages at a time, and NEVER moves
  the watermark (the watermark answers "how far forward are we"; an old post
  must not be allowed to answer it). It is off until switched on, so the
  forward poller's behaviour is unchanged by its existence.
- **History is a CADENCE, not a quantity.** The first version of the backwards
  sweep was a page grant: spend N pages, go idle, wait to be asked again. That
  makes the operator the scheduler — returning every few minutes to top up a
  background job so it stays alive — and it is the wrong shape for the ordinary
  case, which is "empty this archive". `backfill_auto` + `backfill_every_s` is
  the standing form: it digs on its own interval, needs no budget, and RETIRES
  ITSELF by setting `backfill_done` when X stops returning results. Anything
  that runs in the background must be able to end without being told to.
- **Exhausted and starved are different, and the sweep must not confuse them.**
  Fewer pages than asked for means X has no more history (`backfill_done`).
  ZERO pages means no account was free. Treat starvation as exhaustion and an
  empty pool permanently retires a sweep with history left; treat exhaustion as
  starvation and the sweep asks a dry query every cycle forever. The test that
  pins both directions is `run_backfill`.
- **Both directions belong on the same card.** "Fetch now" (forward) and "dig
  older" (backwards) answer the two halves of "why is this number not moving",
  and for a while only the backwards one was on the Watchlists page while the
  forward one lived on the Live Feed. That is what made a working schedule read
  as a broken one: the operator refreshed the Live Feed, saw nothing new, and
  concluded collection had stopped. Controls that diagnose the same symptom go
  in the same place.
- **Starvation spends nothing.** Zero pages means no account was free, not that
  the archive ended. A backfill pass that walks zero pages must not decrement
  the operator's budget, advance the cursor, or mark the sweep exhausted —
  otherwise an empty pool silently eats a grant and reports success.
- **A cadence shown in the interface is a promise, and the scheduler keeps it.**
  The Watchlists dropdown wrote only `min_interval_s`, so it was a floor: the
  adaptive controller multiplied a quiet stream's interval by `GROW` on every
  empty poll up to `max_interval_s`, and "empty" is the permanent, correct
  answer for an archival query. The panel said 5 minutes, the collector ran 15,
  and nothing anywhere reported the disagreement. An explicit choice now pins
  BOTH ends (`min_interval_s == max_interval_s`) and jitter drops to
  `JITTER_PINNED`, so the number displayed is the number that runs. `auto`
  clears both and hands the cadence back to the adaptive controller, which is
  still the right default for a busy live stream. General form: if a control
  states a number, either honour that number or do not state it.

## 4. Delivery rules

- **The delivery cursor keys on `collected_ms`, NEVER on `created_ms`.** This is
  what lets a backfilled post reach its destination at all: a 2024 tweet stored
  today is AHEAD of the cursor, because the cursor asks "what have we not sent
  yet", not "what was posted recently". Switching it to posting time would look
  like an obvious fix for ordering and would silently strand every post the
  backwards sweep ever collects behind the cursor, undelivered and unreported.
  Age is filtered separately, in `_wanted`, on `created_ms` — that is the right
  place for it, and note that a Telegram target with `tg_max_age_h` set WILL
  drop backfilled posts while the cursor advances past them. Sheet targets set
  no age filter, which is why "dig older" and a Google Sheet compose without
  further wiring. Test: `test_backfilled_posts_reach_delivery`.
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
  is empty, so a sheet a human has already shaped is left alone. Cells are
  parsed on write (real dates, clickable links), so any cell starting
  `= + - @` is apostrophe-escaped — scraped text must never become a formula.
- **The sheet is ordered by POST date, newest first — and that is a reversal,
  with a cost.** Rows arrive in COLLECTION order, which nobody wants to read:
  a backwards sweep collects 2024 while live polling adds today, so a pure
  append interleaves decades. The Apps Script therefore sorts the range below
  the header descending on arrival (`SORT_NEWEST_FIRST`), which means rows DO
  move under a reader — the exact property append-only was chosen to protect.
  The trade is deliberate and reversible per sheet by flipping the flag in the
  script. NOTE the asymmetry: the `service_account` route still appends in
  collection order, because the REST path would need a separate `sortRange`
  batchUpdate keyed on the numeric gid. Closing that gap is real work, not an
  oversight — until it is done, the two routes do NOT produce the same sheet.
- **The sheet endpoint is idempotent, keyed on the link column.** "Send past
  posts" deliberately does not move the delivery cursor, so any window that
  overlaps what was already delivered would duplicate every post in it — and
  the natural way to fill a gap is to ask for a generous window. The script
  reads column B and drops rows it already has (`SKIP_DUPLICATES`), so a range
  can be re-sent as often as the operator likes. Without this the honest advice
  for a gap would have been "compute the exact untouched window yourself",
  which nobody can do from the dashboard.
- **"Delivered" and "landed in the sheet" are different claims — surface the
  script's receipt.** The Apps Script replies with `appended`/`skipped` and the
  sender used to discard them, so a backfill of 120 duplicates read as
  "✓ Sent 120" while the sheet visibly did not change — indistinguishable,
  from the dashboard, from rows landing in the wrong spreadsheet entirely.
  `sheets.deliver(..., info=)` now carries the reply through and the backfill
  result states what was written and what was skipped, and to which tab. The
  general rule: when a transport may decline rows ON PURPOSE (dedup), its
  receipt must say so, or every deliberate decline reads as data loss.
- **A project-scoped page fetches nothing until the project id is known.**
  `/api/delivery` without `?project` answers with EVERY project's targets —
  the right shape for the nav badge, the wrong one to paint a project page
  from. The Delivery view used to fire its first request while the projects
  list was still loading, so every reload flashed other projects' data until
  the refetch landed. A scoped view holds its request until `pid` exists
  (`Delivery.jsx`); an unscoped call from a scoped page is a bug even when a
  later refetch papers over it.
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
- **A setup screen shows the credential the SERVER holds — it never mints one
  on view.** The sheet form used to generate a fresh token every time it was
  opened, so deploying a script, closing the form and reopening it silently
  replaced the token of a working deployment and made a correct `.env` look
  wrong. `.env` is the source of truth; a new value appears only when the
  operator asks to rotate. Anything that puts a secret on screen must read it
  from where it is used, or it becomes a second, competing source of truth.
- **A connectivity check tests what DELIVERY will use, or it is theatre.** The
  same bug had "Check access" verifying the token the form had just invented
  rather than the one in `.env` that the sender reads — a check that could
  pass while every real send failed. Same rule as the Telegram test sending
  real tweets through the real formatter: a check that cannot fail the way the
  system fails is not a check.
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
- **The API-key allowlist is keyed on (METHOD, path), never path alone.** Most
  endpoints mean opposite things under the two verbs — `GET /api/projects`
  lists, `POST /api/projects` creates; likewise watchlists, collections and
  alerts. A path-only allowlist cannot express "read this", only "reach this",
  so every path granted for reading silently grants its write half too. Reads
  go in `API_KEY_READ_PATHS` (GET only); `API_KEY_WRITE_PATHS` stays a short,
  deliberate list. (Written when the key was widened from 6 paths to 23 for
  Watch-Tower: under the old path-only check that same edit would have handed a
  read-only analytics consumer the power to delete every project.)
- **Accounts are not data.** `/api/pool*`, `/api/stress/accounts` and
  `/api/login/*` stay cookie-only no matter how wide read access gets — they
  expose the burner pool and launch sign-in processes. "Give the integration
  everything" means every post and every metric, never the credentials that
  collect them.
- **An auth denial says which denial it is.** "That path does not exist here",
  "that path is readable but you used POST" and "your key is invalid" send an
  integrator down three different paths, and a caller that cannot tell them
  apart retries URL variants until it concludes it has been blocked — which is
  exactly what Watch-Tower did, six 403s deep, while nothing was blocking it.
  A 403 body carries `allowed_get` and `allowed_post`; a 401 says the key is
  missing or invalid, not that the endpoint is forbidden.
- **Granting a read does not grant its addresses.** `/api/delivery` is readable
  with a key so an integration can see how far behind each target is; the target
  URLs and `sheet_id`s are masked to their host for machine callers and shown in
  full only on a cookie. An Apps Script `/exec` deployment id, a Telegram chat
  id and a link-shared spreadsheet id are all capability-ish — they are
  infrastructure, not the collected data the key was issued for, and the whole
  query string goes with them because a webhook URL may carry its token there.
  The operator needs the address; the integration needs the number.
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
- **The numeric pk beats the @name for FETCHING — but the source still stores
  the name.** (CORRECTED 2026-08-21. The old rule said "use the numeric pk for
  user sources", which was read as *put the number in the source*, and that is
  what took a whole project offline: every source was keyed by a number or by a
  name that could not be looked up, and `value` could not be both.) Name lookup
  and media reads are SEPARATE permissions — measured on a live restricted
  session, `user_medias_paginated_v1('787132')` returned posts while
  `user_id_from_username('natgeo')`, `user_info_by_username_v1` and
  `search_users` all returned `login_required`. So: keep the handle in `value`,
  resolve it ONCE into `sources.platform_id`, and fetch by the id forever after
  (§2, the three-column source). Validate sessions against the feed endpoint,
  not `account_info`.
- **A username lookup has three ways in, and a failed one is CACHED as a
  question, never retried in a loop.** `engine_ig.resolve_user` tries the
  private API, then the web `web_profile_info` endpoint (browser `X-IG-App-ID`
  — a different gate, commonly open when the private one is shut), then the
  profile HTML. Every path reuses the logged-in client's cookies, proxy and
  user-agent, because a lookup leaving from a different IP than the session it
  claims is a louder signal than the lookup itself (IG1). If all three refuse,
  the error names a human fix (`collect_ig.py set-id`) and the source waits —
  it does not re-ask on the next pass and the one after that.
- **A resolved id is written to the DB, not just to memory.** An in-process
  cache dies with the process, so a restarting service re-resolves — and
  re-fails — every name on every pass. That is exactly what one checkpointed
  account did on 2026-08-20: the same `RetryError` on every source, all night,
  each one spending pacing budget to accomplish nothing. `on_resolved` →
  `store_ig.cache_platform_id` makes the lookup a one-time cost. Any future
  name→id resolution on any platform carries the same obligation.
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
- **A service must be OBSERVABLE and its restart limiter must actually apply.**
  Two unit-file defects, found together on 2026-08-22, and between them they
  are why a service that had been dead for weeks looked alive:
    * `Environment=PYTHONUNBUFFERED=1` on every Python unit. Python
      block-buffers stdout when it is not a TTY, so `print()` sits in an 8KB
      buffer and never reaches journald — `journalctl -f` on a healthy,
      working collector showed nothing but systemd's own lines.
    * `StartLimitIntervalSec` / `StartLimitBurst` go in **`[Unit]`**, not
      `[Service]`. systemd ignores them in `[Service]` and logs "Unknown key
      name … ignoring". With `Restart=always` and an inert limiter, a service
      that crashes on start restarts forever instead of hitting
      `start-limit-hit` and going `failed`. Nothing ever goes red, so nothing
      is ever noticed — which is exactly how `collect_ig.py` survived a
      missing `import os`.
  A service you cannot see and cannot catch failing is not deployed, it is
  merely installed. Check both on any new unit.
- **A path only systemd runs is a path nothing tests — `main()` is code.**
  `collect_ig.py` called `os.getenv` on its `--loop` branch without importing
  `os`, from the ig_human commit onward. The IG service therefore crashed on
  start EVERY time, in under a second, with `Restart=always` hiding it in a
  restart loop — while Instagram collection still appeared to work, because the
  dashboard's Fetch-now calls `collect_ig.run_once` directly and never goes
  through `main()`. A green suite plus a working dashboard said "fine" for
  weeks. `tests/test_all.py::test_no_undefined_names` now walks every module's
  name table, which is the cheap half; the expensive half is remembering that
  the service entrypoint is not covered just because the library under it is
  (2026-08-21).
- **Offline also means isolated from the WORKING DIRECTORY.** A test may not
  read whatever live state happens to sit next to it. The FB favorites tests
  stubbed `_can_log_in` but not `login_blocked`, which reads `fb_health.json`
  relative to the CWD — so on the VPS, where a real checkpoint is recorded,
  `run_favorites` returned 0 before reaching the fake engine and four
  assertions failed on the server while passing on every laptop. A suite that
  disagrees with itself by machine cannot be a contract. When a code path
  consults a file, an env var or a clock, the test stubs it (2026-08-21).
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
- Project scoping on IG and FB, defaulting CLOSED (§2). No endpoint may be
  changed back to serving every project when the caller names none, and the
  external API key may not gain a project query parameter.
- The three-column source contract (§2): `label` = person, `value` = handle,
  `platform_id` = numeric id. No code path may write `label`, and no path may
  collapse the three back into one column — that collapse is the bug the model
  exists to prevent.
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
- IG username→id resolution: three independent paths, the answer persisted to
  `platform_id`, and a human escape hatch (`collect_ig.py set-id`) when a
  restricted session can resolve nothing. Removing a fallback or the write-back
  returns the collector to re-failing every name on every pass.
- Login walls / bans surface in the UI in plain words — never silent.

**Dashboard**
- Content labelling (`classify.py`, `/api/classify`): the Classify button on
  the Collections page (and the Live Feed), the sentiment counts above the
  tabs, the per-project category editor, the category filter and the card
  chips, the auto board per category, the human-correction override, and the
  spend meter. Added 2026-08-22 as the one named
  exception to prime directive 2 — a refactor that "tidies" it away, or that
  quietly makes it automatic, is a rule violation either way.
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
- Collections (cross-platform pins keyed (platform, post_id), CSV export), Alerts (velocity → Telegram), Delivery
  (targets, backfill, behind-count), Search, Guard views.
- Every operational switch editable in the dashboard; service loops re-read
  settings each cycle (no restart to apply).

**Delivery & interfaces**
- HMAC-signed webhook push to Watch-Tower (cursor-based, at-least-once,
  media as URLs), Telegram sends, velocity alerts.
- API-key read-only pull on a fixed allowlist; IG/FB pull endpoints in the
  shared post shape.
