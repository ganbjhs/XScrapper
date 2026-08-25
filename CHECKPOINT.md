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
