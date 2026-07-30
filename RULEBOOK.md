# Rulebook

Everything about this project: what it is for, what it must never do, what has
already gone wrong and why, and how it would be rebuilt if you started again.

`README.md` tells you how to *use* it. This file tells you how to *reason* about
it — read it before changing anything structural.

---

## 1. Goal

**Get tweets matching a query into a local database as fast as possible after
they are posted, without an X API, without getting the account banned.**

The single metric is **lag**: seconds between a tweet being posted and it
landing in `results.db`. Not coverage, not volume, not history.

Measured on one account: **p50 15.4s, p95 18.2s.**

### Non-goals

Naming these matters as much as the goal, because each one would pull the
design somewhere else:

| Not a goal | Why |
|---|---|
| Historical archives | X's Latest index only reaches back ~7 days. Building for backfill would optimise for something the source cannot provide. |
| Guaranteed completeness | X samples high-volume queries server-side. Promising completeness would be a lie told in code. |
| Maximum throughput | Throughput trades directly against ban risk. A dead account collects nothing, which is worse than a slow one. |
| Being a general X client | No posting, following, DMs. Read-only, search only. Every extra capability is extra ban surface. |

---

## 2. The rules

Numbered so they can be cited in review. Ordered by how expensive breaking them is.

### R1 — Auth is never a side effect

`search` and `watch` never log in. They report a missing session and exit 6.
Only `login` authenticates.

*Why:* the original prototype logged in from inside its search path. An expired
cookie surfaced three steps later as an unrelated-looking search error. Failures
must appear where their cause is.

### R2 — Nothing is trusted until a real request proves it

An account is not `active` because it has cookies. It is `active` because an
authenticated HTTP request came back 200.

*Why:* twscrape marks a cookie account active with **no network call at all**
(`account.py:16-17` + `accounts_pool.py:114-115`). Dead cookies then report a
successful login and fail much later, somewhere else.

### R3 — Every page generator is wrapped in `aclosing`

```python
async with aclosing(engine.search_pages(q)) as gen:
    async for page in gen: ...
```

*Why:* acquiring an account locks it for **15 minutes**. The lock releases when
the generator is *closed*, not when you stop iterating. Measured: breaking out
while any reference survives holds the lock the full 15 minutes, and
`gc.collect()` does not help. The watermark poller breaks early on nearly every
poll.

### R4 — The watermark moves only on real search results

Never on tweets found elsewhere in the payload.

*Why:* a page also contains quoted tweets and reply parents. An embedded 2015
quote has a tiny snowflake ID. Anything looking at *all* parsed tweets stops on
page one, forever. The result set comes from timeline `entryId`s only.

### R5 — Zero pages means starvation, never silence

`pages == 0` is reported as `no_account_or_abort`, and the poller **holds** its
interval instead of backing off.

*Why:* when no account is free, twscrape's generator simply yields nothing —
identical to a quiet stream from the outside. Backing off is exactly wrong: the
problem is capacity, not lack of data.

### R6 — Errors are never swallowed into a default

A failed read must surface, not silently become an empty list or a `False`.

*Why:* broken twice in this project. The guard's account check swallowed an
exception into `[]` and reported a confident **"No accounts"** for a healthy
account. Then `_budget()` swallowed a missing-column error and silently
disabled the rate-limit rule — the single most important check — while still
reporting "no risks".

**A confidently wrong answer is worse than an error.**

### R7 — Client-side checks are suggestions; enforce on the server

The dashboard asks the guard before offering a button, and `/api/fetch`
**re-checks independently**. Warnings must be acknowledged (`ack: true`), not
merely displayed.

*Why:* anything can POST to the endpoint. A check that only lives in the browser
is decoration.

### R8 — Refuse, never silently do something smaller

Asking for 60 pages when the cap is 25 is an error, not a request for 25.

*Why:* silently clamping still spends 25 requests the caller never agreed to.
That is precisely the surprise cost the guard exists to prevent. Found by
testing — the clamp ran *before* the guard and quietly spent half a budget.

### R9 — Keep the raw payload forever

Every tweet stores its complete `Tweet.json()`.

*Why:* X changes its schema. With raw payloads, a parser fix can be replayed
over history. Without them, a schema change means the data is simply lost.

### R10 — Private API coupling must fail loudly

Every twscrape internal this project depends on is asserted at startup
(`engine.check()`, 20 assertions), and twscrape is pinned exactly.

*Why:* the alternative is a silent behaviour change producing wrong results
weeks later.

### R11 — Ban risk beats throughput, always

Jitter polls. Keep 25% of the rate budget unspent. Prefer a long-lived process
over frequent restarts.

*Why:* sustained 429s are themselves a ban signal. A banned account collects
nothing.

### R12 — Account status has four states, and unknown is never green

| flag | colour | meaning | what to do |
|---|---|---|---|
| `LIVE` | green | Active, no complaints | nothing |
| `WARN` | amber | Works, but something raises ban risk or will bite later | act before it becomes red |
| `DEAD` | red | X rejected it — collects nothing right now | re-login, or replace the account |
| `?` | grey | Present but unclassifiable, usually a failed read | investigate; **never assume healthy** |

Amber is **not** a weaker red. Red means "this account is not collecting".
Amber means "it works, and here is what will hurt you": no proxy, no `kdt`
cookie, a placeholder user-agent, locked queues, or an unusually heavy request
count. They demand different responses, so they get different colours.

Red is only ever set by X's own verdict — codes `(32)` session expired, `(326)`
locked, `(88)` rate-limit ban heuristic, `(64)` suspended. Never by our guess.

Grey exists because of R6. An account we cannot classify must not render as
green; a confident wrong "healthy" is exactly the failure this taxonomy is for.
An account in `config.toml` that has never logged in is grey too — otherwise
"I added it" and "it is collecting" look identical.

Every non-green flag carries its reasons and a remedy. A coloured dot with no
explanation just moves the debugging problem somewhere else.

### R13 — Preview what can be previewed; link what cannot, and say why

Images and `.mp4` play inline — the URL in `media_urls` is the direct file.

**X broadcasts cannot be embedded.** Measured: `x.com` sends
`x-frame-options: SAMEORIGIN` and `frame-ancestors 'self' https://x.com`, so an
iframe from the dashboard is refused by the browser. They render as a labelled
link that says embedding is blocked, rather than an embed that would silently
be a blank box. A broken player is worse than an honest link.

### R14 — Endpoints that write secrets are loopback-only

`/api/account` writes a password into `.env` and an account into `config.toml`.
It refuses outright unless the server is bound to `127.0.0.1`, checked
server-side, not by hiding the button.

It also does **not** attempt the login itself: that needs a browser window a
human can see, which a server process cannot assume exists. It writes the
config and returns the exact command to run.

### R15 — Local reads are free; going to X is not

The dashboard defaults to querying the local database. Fetching from X is an
explicit, confirmed, budgeted action that never fires on a keystroke.

---

## 3. Problems and solutions

Everything that has actually gone wrong, with the fix. This is the highest-value
section — each entry cost real debugging time.

### Auth

| Problem | Root cause | Solution |
|---|---|---|
| `accounts.db` stayed empty; login never succeeded | HTTP password login is X's most captcha-gated path | Real browser (Playwright) for login only; harvest cookies for HTTP collection |
| Expired cookies reported success, failed at search | twscrape sets `active=True` on non-empty cookie strings, no network call | Validate with a real request before activating (R2) |
| Editing cookies in `.env` had no effect | `add_account` early-returns when the row exists (`accounts_pool.py:93-97`) | Use `pool.save()` — a full upsert |
| One failed login excluded an account forever | `login_all` filters `WHERE error_msg IS NULL` | Clear `error_msg` on every successful re-auth |
| Login detection hung on a logged-in browser | v1.1 REST endpoints **all return 404/code 34** — measured on `api.x.com`, `x.com/i/api`, `api.twitter.com` | Read identity from the DOM; validate via GraphQL Bookmarks |
| Headless login saw "unknown" state | `no_viewport=True` gave a narrow window; X hides the left nav below ~1000px | Pin a 1440×900 viewport when headless |

### Collection

| Problem | Root cause | Solution |
|---|---|---|
| Tweets silently missing | twscrape's parser drops any tweet whose ID is in `retweeted_ids` — a tweet that is both a hit and retweeted on the same page vanishes | Parse with `to_old_rep` + `Tweet.parse`, bypassing `_parse_items` |
| Watermark logic would stop on page 1 | `api.search()` yields dict-insertion order with embedded old quotes mixed in | Key off timeline `entryId`s, which are true timeline order (R4) |
| Tweets indexed late were lost forever | Stopping exactly at the watermark leaves a blind spot | Stop 60s *below* the watermark (overlap window) |
| Accounts vanished for 15 minutes | Generator not closed on early break | `aclosing` everywhere (R3) |

### Runtime

| Problem | Root cause | Solution |
|---|---|---|
| `RuntimeError: Lock is bound to a different event loop` | twscrape holds a module-level `asyncio.Lock` (`db.py:12`); the dashboard called `asyncio.run()` per request | One persistent loop on a background thread; handlers submit via `run_coroutine_threadsafe` |
| Dashboard froze permanently | Same cause. Concurrent calls across dead loops **deadlock** rather than erroring — reproduction had to be killed at 2 minutes | Same fix. Regression test asserts 6 concurrent threads all complete |
| "Failed to fetch" with no explanation | Server not running | Error text now names the cause and the command that fixes it |
| Guard reported "No accounts" for a healthy account | Swallowed exception (R6) | Read `accounts.db` directly with sqlite3 — no event loop, no nesting |
| Rate-limit rule silently disabled | `rl_reset` column missing on older databases; error swallowed (R6) | Column-aware query + explicit migration + surface read failures |

### Known and unfixed

| Limitation | Status |
|---|---|
| No global rate governor | **Biggest gap.** Each stream self-paces; nothing enforces a total ceiling across streams. |
| Gaps detected but not backfilled | Recorded in `gaps`, reported by `doctor`. Nothing fills them. |
| Proxies untested | Config supports `proxy` per account; never exercised. |
| TLS fingerprint mismatch | httpx sends Python TLS with a Chrome UA. `curl_cffi` fixes it but strips our real UA — a real trade, not a free win. |
| `xclid` UA inconsistency | twscrape fetches `x.com/tesla` with a random UA. Cached per process, so long-lived processes minimise it. |
| Latest index ~7-day horizon | X-side. Unfixable. |
| High-volume queries sampled | X-side. Narrower queries are more complete. |

---

## 4. If you rebuilt it

What would stay, what would change.

### Keep

- **The six-module split.** Each has one reason to exist and dependencies run
  one way (`config` leaf → `main` root). No cycles.
- **The `engine` seam.** Only `engine.py` knows X's wire format — which is what
  lets the tests swap in a replay engine and exercise the whole poll loop
  offline, with no network and no rate-limit budget.
- **The `auth` seam.** Only `auth.py` imports Playwright, so collection never
  pulls in a browser.
- **Snowflake IDs as the ordering key.** `ORDER BY tweet_id` is chronological
  for free; millisecond precision where `created_at` gives seconds.
- **Raw payload retention** (R9).
- **Offline-first tests.** 140+ assertions, no network. This is why the
  freshness logic is verifiable rather than hopefully-correct.

### Change

1. **Build the rate governor first, not last.** A global token bucket sized from
   measured `rl_limit`, shared across streams and accounts. Currently each
   stream self-paces and nothing enforces the total. This is the one structural
   gap that actually raises ban risk.

2. **Make the guard a hard gate from day one.** It was added last; it should
   have been the second thing built, right after auth. Every action that spends
   budget or touches an account should have gone through it from the start.

3. **Do not depend on any REST endpoint.** All four v1.1 endpoints died. Assume
   GraphQL only, and assume every `/i/api` request needs
   `x-client-transaction-id` — a 404 means a signing problem, not a missing
   endpoint.

4. **Consider owning the GraphQL client.** twscrape's value is almost entirely
   `xclid.py` (the transaction-id generator) and doc_id maintenance. Everything
   else — the pool, the parser — this project already works around. A thin
   client plus a vendored signer would remove the private-API coupling that
   `compat.check()` exists to police.

5. **Reach for lists before more accounts.** An extra account multiplies the
   search budget by 1. Moving a query to an X List multiplies it by 10, on the
   same account, with no extra ban surface. This was found by measuring rather
   than assumed, and it inverts the obvious scaling instinct.

6. **Store engagement history, not just latest.** Counts are overwritten on
   re-observation. A `tweet_metrics(tweet_id, observed_ms, likes, …)` table
   would make virality analysis possible for one extra row per sighting.

7. **Separate the fetch worker from the HTTP handler.** A long fetch currently
   blocks the dashboard because it holds `_FETCH_LOCK` for its duration. A job
   queue with progress streaming would fix the UI freeze.

### Do not

- **Do not scrape the DOM for tweets.** Counts come back truncated (`1.2K`),
  selectors break constantly, and it is slower than every alternative. The
  browser is for login only.
- **Do not run the browser during collection.** It is the single biggest
  resource cost and buys nothing once you hold the cookies.
- **Do not add per-keystroke live search.** It would drain the budget the
  watcher needs (R12).
- **Do not unpin twscrape** without running `doctor --selftest`.
- **Do not set `TWS_PROXY`.** It silently overrides every per-account proxy,
  collapsing the pool onto one IP. Config rejects it.

---

## 5. Operating rules

**Daily**

```bash
python3 main.py guard      # what is risky right now, and why
python3 main.py doctor     # accounts, streams, lag
```

**Account hygiene**

1. Throwaways only, never a personal account.
2. Run 3–5. One account is a single point of failure and has the smallest budget.
3. Warm new accounts up — log in, browse, follow a few things over a couple of
   days. A day-old account making 200 requests/hour is the obvious pattern.
4. Give each its own proxy. Residential beats datacenter.
5. **Back up `profiles/`** with Chrome closed. That directory *is* the
   trusted-device asset; losing it means redoing headed logins and facing
   new-device challenges everywhere.

**When an account dies**

twscrape auto-deactivates on `(32)` session expired, `(326)` access denied,
`(88)` rate limit with budget remaining. The pool skips it and keeps going.

```bash
python3 main.py doctor --accounts             # which and why
python3 main.py login --account X --force     # recover if it was just expiry
```

If genuinely suspended, that account is gone. Swap in another — **the design
assumes accounts die.**

**When X changes**

- *Sudden 404 on every search* → doc_id rotated. `pip3 install -U twscrape`, or
  set `X_SEARCH_DOC_ID` in `.env` from DevTools to unblock immediately.
- *Fields going null* → schema drift. Fix the parser, then **replay** from the
  raw payloads (R9).
- *`doctor --selftest` failing* → twscrape internals moved. Do not ignore it.

---

## 6. Reference

**Budget is per GraphQL operation, not per account.** Measured 2026-07-29:

| queue | limit / 15 min | floor, 1 account |
|---|---|---|
| `SearchTimeline` | 50 | ~24 s |
| `ListLatestTweetsTimeline` | 500 | ~2.5 s |

Search and list streams never draw from the same pool, so adding lists raises
total capacity instead of dividing it. Minus a 25% reserve, search sustains one
poll every ~24s per account; lists sustain one every ~2.5s. **A list is the
lowest-lag source available** — the trade-off is no server-side filtering, so
narrowing happens locally after the tweets are already paid for.

**Costs:** local search 0 · steady-state poll 1 · cold poll ≤ `max_pages_per_poll`
· dashboard fetch 1 per page (cap 25).

**Exit codes:** 0 ok · 2 auth · 3 search · 4 config · 6 no account.

**Guard levels:** `BLOCK` will damage something, refuse · `WARN` proceed only
knowingly, needs `ack` over HTTP · `note` informational.

**Files:** `config` what you declare · `auth` how you get a session · `engine`
how we talk to X · `collector` when to poll · `store` what we keep · `guard`
what not to do · `web` how you look at it · `main` how you drive it.
