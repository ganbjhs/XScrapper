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
