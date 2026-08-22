# Collector

Self-hosted social-media collector for a media house. Watches chosen **X
(Twitter)**, **Instagram**, and **Facebook** accounts/pages, stores matching
posts within seconds — no paid API — organizes them into projects and
watchlists, shows a live dashboard, and delivers everything (media included) to
Watch-Tower.

Highlights: projects + watchlists (handles, keyword searches with AND, or X
Lists); a real-time Live Feed merging all three platforms with keyword-match
highlighting; per-source check intervals and Start/Pause; velocity alerts and
collections; per-project delivery to Watch-Tower with a durable, at-least-once
cursor.

One thing it does that is NOT collection: **content labelling**. Press Classify
and the unlabelled posts in a project go to Grok once, come back with one
category each (the categories are yours, edited in the dashboard), and land on
a board per category. It is manual, it is metered against a monthly dollar cap,
and a label you correct by hand is never overwritten. That is the only analysis
in here — everything else is still a count or a timestamp, and the deeper
intelligence is Watch-Tower's job. See RULEBOOK §1 directive 2 for why the
exception exists and where its edges are.

**Docs — there are exactly three, on purpose:**

| Doc | What it is |
|---|---|
| `README.md` | This page: run it |
| `BLUEPRINT.md` | The whole map: architecture, every file's job, data flow, deploy runbook, invariants, roadmap |
| `RULEBOOK.md` | The design rules every change must respect (the invariants, and why each was paid for) |

Facebook has two side-docs: `FACEBOOK_RUNBOOK.md` (how to turn it on) and
`FACEBOOK_LESSONS.md` (**everything already tried and why it failed** — read it
before touching the Facebook engine, it will save you days).

## Run it

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium        # for the one-time X sign-in
cp .env.example .env                          # dashboard login + secrets
cp config.toml.example config.toml            # accounts (streams optional —
                                              #   watchlists are made in the UI)
python3 main.py serve                         # dashboard: 127.0.0.1:8765/app
python3 main.py watch --all                   # the collector — keep it running
```

Sign an account in from the dashboard (`/accounts`), create a project and a
watchlist in the UI, and posts start flowing into the Live Feed.

The dashboard only *shows*; the `watch` process is what *collects*. If the
Live Feed shows a red "Collection is OFF" banner, the watcher is not running.

Instagram and Facebook are optional and independent. Facebook needs no browser
sign-in dance — put an account's `FB_EMAIL`/`FB_PASSWORD` in `.env`, add pages
in the dashboard, and it collects from the server's own IP. See
`FACEBOOK_RUNBOOK.md`.

## Go live (24/7, no laptop)

`BLUEPRINT.md` §6 — one script on a Linux VPS sets up the always-on collector,
the dashboard behind nginx + HTTPS, and auto-restart. Instagram and Facebook
each run as their own always-on service (`xscraper-ig`, `xscraper-fb`)
alongside the X watcher (`xscraper-watch`).

## Tests

```bash
python3 tests/test_all.py     # offline, no accounts, no rate budget
```

Keep it green; every change grows a test.
