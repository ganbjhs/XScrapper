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
