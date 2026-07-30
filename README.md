# X (Twitter) Freshness-First Scraper

> **[RULEBOOK.md](RULEBOOK.md)** — goals, the 12 rules, every problem hit so far
> with its cause and fix, and how it would be rebuilt. Read that before
> changing anything structural.

Monitors X advanced-search queries and gets new tweets into a local database
**fast**. No official API, no paid tier.

Two moving parts, each doing what it is good at:

- **A real browser handles login.** Playwright opens Chrome against a
  per-account profile, you clear the captcha / 2FA once, and it harvests the
  session cookies. This is the one step where a browser is worth its cost — X
  gates password login behind challenges no scripted HTTP replay can pass.
- **Plain HTTP handles collection.** Once authenticated, tweets come from X's
  internal GraphQL endpoints via [`twscrape`](https://github.com/vladkens/twscrape):
  fast, exact integer counts, full media variants. No browser is running while
  collecting.

The optimisation target is **lag** — the time between a tweet being posted and
it landing in your database — not exhaustive coverage of a date range.

---

## How it collects

Each stream keeps a **watermark**: the highest tweet id it has ever seen. A
poll walks the Latest timeline from the top and stops the moment it reaches
known ground, so a steady-state poll costs exactly one request.

Tweet ids are Twitter snowflakes, which encode their own creation time. That
one fact does most of the work: `ORDER BY tweet_id` is chronological for free,
post times are millisecond-precise (the API's `created_at` is only
second-precise), and "stop when we reach what we already have" is an integer
comparison.

Three details that are easy to get wrong, and are handled:

- **The overlap window.** Polls stop 60s *below* the watermark, not at it. X's
  index is not perfectly ordered at the edge; without this, tweets indexed a
  few seconds late fall permanently into the blind spot between polls.
- **Embedded tweets never move the watermark.** A search page also contains
  quoted tweets and reply parents. An embedded 2015 quote has a tiny snowflake
  id, so anything that looked at *all* parsed tweets would stop on page one
  forever. The result set comes from timeline entry ids only.
- **Starvation is not silence.** When no account is free, the underlying
  generator simply yields nothing — identical to a quiet stream unless you
  check the page count. Zero pages is reported as `no_account_or_abort`, and
  the poller holds its interval instead of backing off.

---

## Setup

```bash
pip3 install -r requirements.txt
python3 -m playwright install chromium

cp .env.example .env                # per-account secrets
cp config.toml.example config.toml  # accounts + queries
```

Then edit `config.toml` (which accounts and which queries) and `.env` (the
passwords those accounts reference). Passwords are only needed for the *first*
login of each account; afterwards the Chrome profile carries the session.

**Use throwaway accounts.** Automating a personal account violates X's ToS and
accounts do get locked. The design assumes accounts die and get replaced.

---

## Use

```bash
# 1. Log in. A Chrome window opens; clear any captcha/2FA by hand.
python3 main.py login --all

# 2. Check everything is healthy.
python3 main.py doctor

# 3. Poll your streams. Ctrl-C to stop.
python3 main.py watch --all

# 4. Browse it.
python3 main.py serve            # http://127.0.0.1:8765

# ...or get it out as a file.
python3 main.py export --format csv --since 24h --out today
```

`watch --once` does a single poll per stream and exits — the fastest way to see
whether a query works.

### Commands

| Command | What it does |
|---|---|
| `login` | Browser login; harvests and validates the session. `--refresh-only` refreshes a trusted profile headlessly, no human needed. |
| `watch` | Polls streams continuously on adaptive intervals. `--once` for a single pass. |
| `serve` | Local web dashboard: search, filter and export collected tweets, or fetch a new query from X on demand. |
| `search` | One-shot advanced search (`--query`) or list pull (`--list`), writes `results.{json,csv,raw.jsonl}`. |
| `export` | Writes collected tweets from the store. `--format json\|csv\|jsonl\|raw`. |
| `guard` | Risk check: what is unsafe right now and why. Run before anything expensive. |
| `doctor` | Account health, streams, lag percentiles, twscrape compatibility. |

### The dashboard

`python3 main.py serve` opens a local UI over `results.db`. It draws a hard line
between two very different actions:

- **Search collected** reads your database. Free, instant, unlimited — free-text
  across tweet and author, plus filters for stream, author, time window, minimum
  likes, language and media-only. This is the default and what you want almost
  always.
- **Fetch from X** makes a real request. It asks for confirmation, states the
  cost up front, runs one at a time, and reports the remaining rate-limit budget
  afterwards. It never fires on a keystroke, because that budget is the same one
  `watch` needs to keep streams fresh.

Binds to localhost only. It has no authentication and can spend your rate-limit
budget, so don't expose it.

The prototype's original invocation still works:

```bash
python3 main.py --query 'from:nasa since:2026-01-01 min_faves:500' --limit 100
```

### Measuring freshness

```bash
python3 main.py doctor --lag --since 24h
```

Reports p50/p95 lag per stream, plus a breakdown of *why* polls stopped.
`page_budget` means a stream is outrunning the poller; `no_account_or_abort`
means the account pool is dry. Tweets that already existed when a stream
started watching are counted separately as backlog — their "lag" is just how
long they predated you, and including them would make p95 meaningless.

### What lag to expect

```
sustained rate per account = rate_limit / 900s      (measured: 50 per 15 min)
interval floor  = streams x requests_per_poll / (accounts x rate x 0.75)
p50 lag         ~ interval / 2 + X's own index lag (~1-10s, irreducible)
```

| accounts | streams | interval floor | expected p50 lag |
|---|---|---|---|
| 1 | 1 | ~24 s | ~12 s + index |
| 5 | 1 | ~5 s | ~2.5 s + index |
| 5 | 5 | ~24 s | ~12 s + index |

**Rate limits are PER GRAPHQL OPERATION, not per account.** Measured 2026-07-29:

| queue | limit / 15 min | interval floor, 1 account |
|---|---|---|
| `SearchTimeline` | **50** | ~24 s |
| `ListLatestTweetsTimeline` | **500** | ~2.5 s |

A list stream and a search stream never draw from the same pool, so adding
lists raises total capacity rather than dividing it. If you need the lowest
possible lag, put the accounts you care about in an X List and watch that —
it is a 10x larger budget.

**Measured on one account, one search stream:** p50 **15.4s**, p95 **18.2s**,
and steady-state polls cost 1 request each:

```
poll 1 (cold)   new=100  dup=0   pages=5   stop=page_budget
poll 2          new=4    dup=16  pages=1   stop=watermark
poll 3          new=0    dup=20  pages=1   stop=watermark
```

Accounts buy freshness roughly linearly until the interval approaches X's own
index lag. Past ~5-10 accounts *per stream*, more accounts buy more **streams**
rather than lower lag.

---

## Files

Eight modules, each with one reason to exist.

| File | Role |
|---|---|
| `config.py` | What you declare: `config.toml` loading and validation |
| `auth.py` | How you get a session: Playwright login + the `accounts.db` adapter |
| `engine.py` | How we talk to X: page fetch/parse, plus twscrape compatibility asserts |
| `collector.py` | When to poll: watermark loop, adaptive intervals, scheduler |
| `store.py` | What we keep: snowflake helpers, normalize, results DB, export writers |
| `web.py` | How you look at it: local dashboard (stdlib HTTP server + one HTML page) |
| `guard.py` | What not to do: risk rules that block or warn before an action |
| `main.py` | How you drive it: CLI and every subcommand |

Dependencies run one way — `config` is a leaf, `main` is the root — so there
are no import cycles.

Two seams are deliberate rather than cosmetic. `auth.py` is the only module
that touches Playwright, so collection never imports a browser. `engine.py` is
the only module that knows X's wire format, which is what lets the test suite
swap in a replay engine and exercise the whole poll loop offline.

```
tests/
  fixtures.py   canned X payloads (incl. the two shapes that break naive parsers)
  test_all.py   every test — units, session, engine, collector, CLI
```

### Databases

- `accounts.db` — twscrape's session store: cookies, user-agent, per-account
  rate-limit state. **Contains live credentials.**
- `results.db` — collected tweets, watermarks, poll history. Every tweet keeps
  its full raw JSON, so a future X schema change can be reparsed from history
  instead of re-scraped.
- `profiles/<label>/` — Chrome profiles. **These are the real asset**: X
  remembers them as trusted devices, which is what makes re-login silent.
  Back them up; never share one between two accounts or run two Chromes
  against one.

`.gitignore` covers all three. Back up live databases with
`sqlite3 results.db ".backup out.db"` rather than copying files, since both run
in WAL mode.

---

## Tests

```bash
python3 tests/test_all.py
```

Everything runs offline against canned payloads — no network, no accounts, no
rate-limit budget spent. The suites cover snowflake arithmetic, config
validation, the three auth bug fixes, page parsing, account-lock release,
watermark stopping, dedup, gap detection, adaptive intervals, and the CLI's
exit codes.

---

## Maintenance and limits

**Everything goes through GraphQL, and every `/i/api` request needs an
`x-client-transaction-id` header** that X computes in-page. twscrape
reverse-engineers that generator, which is the main reason this project depends
on it. Without the header X answers **404**, not 401 — so a 404 usually means a
signing problem, not a missing endpoint.

That bit us once already: the session-validation check originally used the v1.1
REST endpoints (`account/settings.json`, `verify_credentials.json`). Measured
2026-07-29 against a live logged-in session, **all of them return 404/code 34**
on every host — `api.x.com`, `x.com/i/api`, `api.twitter.com` — from inside the
browser and out. Validation now uses a GraphQL Bookmarks call instead: auth-only
by construction, and on its own rate-limit queue so it never spends the search
budget. The trade-off is that it proves the session authenticates but not whose
it is; identity comes from the browser DOM of the profile the cookies were taken
from.

**X rotates the GraphQL `doc_id` every few weeks.** Also a sudden 404 on every
search. Fix with `pip3 install -U twscrape`, or unblock yourself immediately by
grabbing the current id from DevTools and setting `X_SEARCH_DOC_ID` in `.env`.

**`twscrape` is pinned exactly** because this project uses its private
internals — cursor injection, per-page rate-limit headers, and a parse path
that avoids a bug where twscrape silently drops any tweet that is both a search
hit and retweeted by another hit on the same page. `python3 main.py doctor
--selftest` asserts every one of those assumptions, so an upgrade fails loudly
instead of quietly corrupting results.

**Completeness has a floor set by X, not by this code.** The Latest index has a
rolling horizon of roughly a week, and X does not guarantee that Latest returns
every matching tweet on high-volume queries. Narrower queries are more
complete. When a poll cannot reach the previous watermark, the missed window is
recorded as a gap and surfaced by `doctor` rather than being silently dropped;
automatic backfill of those gaps is not yet implemented.

**Detection surface.** The HTTP client presents a Python TLS fingerprint that
does not match the Chrome user-agent it sends. Several accounts polling the
same queries from one IP look like one operator, because they are — per-account
proxies (set in `config.toml`) reduce that correlation. Never set `TWS_PROXY`:
twscrape gives it precedence over every per-account proxy, collapsing the whole
pool onto one exit IP. Polls are jittered by ±15% for the same reason.

**Not yet built** (deliberately deferred until the above proves out): automatic
gap backfill, a global token-bucket rate governor sized from measured limits,
and service/supervisor files for unattended operation.
