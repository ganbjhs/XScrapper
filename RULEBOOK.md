# Rulebook

The whole system in one file: what it is for, how it is built, what it must
never do, what has already gone wrong, and how you would rebuild it from
nothing.

If you read only one file, read this one. `README.md` is a quickstart and
nothing more; everything it says is repeated here in more detail.

Last reviewed: 2026-07-30.

---

## 1. Goal

**Get tweets matching what you care about into a local database as fast as
possible after they are posted, without X's official API, and without getting
the account banned.**

The single metric is **lag**: seconds between a tweet being posted and it
landing in `results.db`. Not coverage, not volume, not history.

**Measured: p50 15.4s, p95 18.2s** — on one account, one watched stream,
`watch` running continuously. That last condition matters and is the most
common way to misread this number: the figure only means anything for a stream
being polled on a timer. A one-off "get new tweets" sweep pulls in tweets that
are already hours or days old, and its `lag_ms` column measures how old they
were when first seen, not how fresh the system is.

> As of this writing `results.db` contains only sweeps — no watcher has run
> against it yet — so its lag column is backfill age. Start `watch` and let it
> run before quoting a freshness number from it.

### Non-goals

Naming these matters as much as the goal, because each would pull the design
somewhere else:

| Not a goal | Why |
|---|---|
| Historical archives | X's Latest index only reaches back ~7 days. Building for backfill would optimise for something the source cannot provide. |
| Guaranteed completeness | X samples high-volume queries server-side. Promising completeness would be a lie told in code. |
| Maximum throughput | Throughput trades directly against ban risk. A dead account collects nothing, which is worse than a slow one. |
| Being a general X client | No posting, following, DMs. Read-only, search only. Every extra capability is extra ban surface. |

---

## 2. The rules

Numbered so they can be cited in review. Ordered by how expensive breaking them
is.

### R1 — Auth is never a side effect

`search` and `watch` never sign in. They report a missing session and exit 6.
Only an explicit sign-in authenticates: `main.py login`, or the sign-in window
in the dashboard.

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

*Why:* broken three times in this project. The guard's account check swallowed
an exception into `[]` and reported a confident **"No accounts"** for a healthy
account. `_budget()` swallowed a missing-column error and silently disabled the
rate-limit rule — the single most important check — while still reporting "no
risks". And `load_config` invented a whole account out of stray environment
variables when `config.toml` was missing, which is how the only real account on
this machine ended up collecting under a label nobody had chosen (§9, "the
phantom account").

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

| shown as | colour | meaning | what to do |
|---|---|---|---|
| `Working` | green | Active, no complaints | nothing |
| `Check this` | amber | Works, but something raises ban risk or will bite later | act before it becomes red |
| `Signed out` | red | X rejected it — collects nothing right now | sign in again, or replace the account |
| `Not set up` | grey | Present but unclassifiable, usually a failed read | investigate; **never assume healthy** |

Amber is **not** a weaker red. Red means "this account is not collecting".
Amber means "it works, and here is what will hurt you": no proxy, no
known-device cookie, a placeholder user-agent, locked queues, or an unusually
heavy request count. They demand different responses, so they get different
colours.

Red is only ever set by X's own verdict — codes `(32)` session expired, `(326)`
locked, `(88)` rate-limit ban heuristic, `(64)` suspended. Never by our guess.

Grey exists because of R6. An account we cannot classify must not render as
green; a confident wrong "healthy" is exactly the failure this taxonomy is for.
An account in `config.toml` that has never signed in is grey too — otherwise
"I added it" and "it is collecting" look identical.

Every non-green state carries its reasons and a remedy. A coloured dot with no
explanation just moves the debugging problem somewhere else.

### R13 — Preview what can be previewed; link what cannot, and say why

Images and `.mp4` play inline — the URL in `media_urls` is the direct file.

**X broadcasts cannot be embedded.** Measured: `x.com` sends
`x-frame-options: SAMEORIGIN` and `frame-ancestors 'self' https://x.com`, so an
iframe from the dashboard is refused by the browser. They render as a labelled
link that says so, rather than an embed that would silently be a blank box. A
broken player is worse than an honest link.

### R14 — Adding an account and signing it in are one action

`/api/account` writes `config.toml`, **reloads the config into the running
process**, and the dashboard goes straight into the sign-in window. It never
ends by printing a command to run.

*Why:* it used to do exactly that — write the config, then hand back
`python3 main.py login --account X` and a note saying to restart the server.
That is a dead end for anyone using the dashboard over the internet, which is
the only way it is used in production. It also could not work: `_CFG` was read
once at startup, so the account existed on disk and nowhere in memory, and the
login would have failed with "No account labelled ...".

The endpoint is gated on the dashboard login being configured, not on the bind
address — behind nginx every connection appears to come from `127.0.0.1`, so a
loopback check would have passed on a public box.

**No password is ever sent to this server.** It is typed into the real x.com
form inside the sign-in window. `config.toml` still names an env var for one,
used only by the command-line `login` path.

### R15 — Local reads are free; going to X is not

The dashboard defaults to querying the local database. Getting new tweets from
X is an explicit, confirmed, budgeted action that never fires on a keystroke.
Auto-update re-reads the local database only.

### R16 — Deleting data is possible, never convenient, and never the default

Removing a stream offers two separate actions, and they are not variations of
one another:

| | what happens | reversible |
|---|---|---|
| **Stop watching** | the stream stops being collected and leaves the sidebar; every tweet stays, still searchable, still served by the API | yes |
| **Delete this and its tweets** | the tweets go too, permanently | **no** |

The first is one click. The second requires typing the stream's name, says how
many tweets will go, and reports exactly what went.

*Why:* X's Latest index only reaches back ~7 days, so anything destroyed more
than a week after collection can never be collected again. Storage is cheap and
the data is not.

**A tweet matched by more than one stream survives.** Deleting a junk stream
must not punch holes in a list you kept, so only tweets that no other stream
also matched are removed — and the count reported is the number actually
deleted, not the number the stream contained.

*This rule replaced an earlier one that said deletion did not exist at all.*
That version shipped "hide" instead, which turned out to be the wrong shape:
hiding left a growing list of hidden things to manage, and the operator wanted
the entry gone. The safety it was protecting is real, so it moved into the
confirmation rather than into a refusal.

### R17 — HTTPS is not a manual step, and re-deploying can never turn it off

`deploy/setup.sh` obtains the certificate itself, and nothing it does on a
later run can remove one.

*Why, part one:* it used to end by printing "next, run certbot", and the result
was a dashboard sitting on plain HTTP with a password field on it, which every
browser labels **Not secure** — while the setup script reported success. A
manual step after a script that says "done" is a step that does not happen.

*Why, part two:* the first fix introduced a worse bug. `certbot --nginx` edits
the vhost **in place** to add the TLS block, and setup.sh copied its own
template over that file on every run — deleting HTTPS. The check that should
have caught it asked the wrong question ("does a certificate exist?" — yes, in
`/etc/letsencrypt`, untouched) instead of the right one ("is nginx serving this
name on 443?" — no). So each re-run silently reverted the site to plain HTTP
and reported success.

The structure that prevents it: the vhost is **written once and then owned by
certbot**, and everything that legitimately changes between deploys — proxy
target, port, headers — lives in an `include`d snippet with no `server` or
`listen` line in it, rewritten freely. See §9, "the neighbour's certificate".

### R18 — On a shared machine, claim only names scoped to this app

**This server also runs `namo.vedictech.in` and `report.vedictech.in`.** They
are not ours. Nothing here may touch their vhosts, certificates, units or
ports — see §6.

Every resource this deployment creates is named for the app or for its own
domain: `sites-available/scraper.vedictech.in`, `snippets/xscraper-app.conf`,
`xscraper-web`, `/opt/xscraper`, and a port confirmed free before use. It never
takes a port, never edits another vhost, never removes `sites-enabled/default`.

*Why:* a generic name is a collision waiting to happen. The vhost was once just
`sites-available/scraper`, which says nothing about which host it serves and
invites a second app to overwrite it.

The failure this guards against is quiet in the worst way: nothing errors, the
site keeps answering on port 80, and only HTTPS breaks — because nginx, finding
no server block for the requested name, falls through to another app's block
and serves **that app's certificate**. See §9, "the neighbour's certificate".

When our site and a neighbour's cannot both be right, **ours loses**. setup.sh
disables the scraper rather than fight for a contested hostname. A scraper
that is down is a nuisance we can fix; a neighbour that is down is someone
else's outage caused by our deploy.

### R19 — The dashboard speaks plain language

No jargon in anything a user sees: no "rate-limit budget", no "watermark", no
"stream", no error codes without an explanation, no shell commands as remedies.
Say "requests left this 15 minutes", "what we are watching", "press Sign in
to X".

*Why:* the person operating this is not the person who wrote it. A message they
cannot act on is the same as no message. The code and comments stay precise —
this rule is about the screen.

### R20 — A key is scoped by an allowlist, never by good manners

An API key may read collected data and call `/api/fetch`. It may not add an
account, open a sign-in browser, or change the view. The list lives in
`API_KEY_PATHS` and is checked before anything else runs.

*Why:* the alternative is "a key can do whatever a signed-in human can", which
means a leaked key rewrites `config.toml`, writes secrets to disk and launches
browser processes. Scoping by endpoint makes a leak cost data exposure instead
of the account.

Delivery to other systems follows the same shape: signed so the receiver can
prove it was us, at-least-once so nothing is dropped, and on its own task so a
receiver's outage never becomes our lag. See §6b.

---

## 3. Architecture

### Shape

```
                    config.toml + .env
                            │
                            ▼
                      ┌──────────┐
                      │  config  │  what you declared
                      └────┬─────┘
                           │
        ┌──────────┬───────┼────────┬──────────┐
        ▼          ▼       ▼        ▼          ▼
   ┌────────┐ ┌────────┐ ┌─────┐ ┌───────┐ ┌───────┐
   │  auth  │ │ engine │ │store│ │ guard │ │  web  │
   │session │ │ X wire │ │ db  │ │ risk  │ │  UI   │
   └───┬────┘ └───┬────┘ └──┬──┘ └───┬───┘ └───┬───┘
       │          │         │        │         │
       │          └────┬────┘        │         │
       │               ▼             │         │
       │        ┌─────────────┐      │         │
       │        │  collector  │      │         │
       │        │ when to poll│      │         │
       │        └──────┬──────┘      │         │
       └───────────────┼─────────────┴─────────┘
                       ▼
                   ┌───────┐
                   │ main  │  the CLI
                   └───────┘
```

Dependencies run one way, `config` (leaf) to `main` (root). No cycles.

### The two seams that matter

**`engine.py` is the only module that knows X's wire format.** That is what
lets the tests swap in a replay engine and exercise the whole poll loop
offline, with no network and no rate-limit budget spent. 140+ assertions run
that way.

**`auth.py` is the only module that imports Playwright.** Collection therefore
never pulls in a browser. A collector process has no Chrome in it at all.

### What each file owns

| file | owns | key surface |
|---|---|---|
| `config.py` | what you declared | `load_config()`, `Config.account()`, `Config.stream()` |
| `auth.py` | getting and keeping a session | `harvest_session()`, `InteractiveLogin`, `upsert_session()`, `health()` |
| `engine.py` | talking to X | `Engine.search_pages()`, `parse_page()`, `check()` |
| `collector.py` | when to poll, and when to stop | `poll_once()`, `next_interval()`, `Collector` |
| `store.py` | what is kept, and its shape | snowflake helpers, `normalize_tweet()`, `Store`, export writers |
| `guard.py` | what not to do | `assess()`, `classify_account()` |
| `web.py` | the dashboard and the read API | `serve()`, the single-page `PAGE` |
| `webhook.py` | pushing tweets to other systems | `run()`, `pump()`, `sign()`, `verify()` |
| `main.py` | the CLI | `login doctor guard serve search export watch` |

### Data flow, one poll

```
collector.poll_once(engine, store, stream, stream_id)
  │
  ├─ store.watermark(stream_id)          → the highest tweet id already seen
  ├─ stop_below = id_minus_ms(hw, 60s)   → overlap window (R4, and §9)
  │
  ├─ async with aclosing(engine.search_pages(...)) as gen:      ← R3
  │    for each page:
  │      engine.parse_page()  → results (from timeline entryIds)  ← R4
  │                           + embedded (quotes, reply parents)
  │      store.upsert_tweets(results, source='result')
  │      store.upsert_tweets(embedded, source='embedded')
  │      store.link_hits(stream_id, result ids)
  │      stop when: any result id <= stop_below  → 'watermark'
  │                 no cursor                    → 'exhausted'
  │                 page count hit the cap       → 'page_budget'
  │
  ├─ store.set_watermark(max result id)          ← never an embedded id
  └─ store.record_poll(...)  → the audit row: pages, results, new, dup,
                                stop_reason, rate-limit headers, lag p50/p95
```

`stop_reason` is the whole point of the audit row. "Nothing new" and "the
account pool was starved" look identical from the outside and demand opposite
responses (R5).

---

## 4. Data model

Two SQLite databases, deliberately separate.

### `accounts.db` — twscrape owns this schema

Cookies, user-agent, per-queue locks, request stats, `active`, `error_msg`. We
never create it ourselves; we drive it through `pool.save()` (§9, "editing
cookies had no effect").

**It holds a live `auth_token`, which is full control of the X account.**
Treat the file as the credential it is: `chmod 600`, never in git, never on a
machine you do not control.

### `results.db` — ours

```sql
tweets              -- one row per tweet ever seen
  tweet_id            INTEGER PRIMARY KEY   -- snowflake: ORDER BY == chronological
  created_ms          INTEGER NOT NULL      -- decoded from the id, ms precision
  collected_at/_ms    -- frozen at FIRST sight, never updated
  last_seen_at        -- updated on every re-observation
  lag_ms              -- frozen at first sight: collected_ms - created_ms
  url text lang author_* reply_count retweet_count like_count quote_count
  view_count bookmark_count is_retweet is_reply is_quote
  hashtags mentions urls media_urls   -- JSON arrays
  in_reply_to conversation_id
  source              TEXT   -- 'result' (a real hit) | 'embedded' (quote/parent)
  raw_json            TEXT   -- complete Tweet.json()          ← R9
  raw_entry_json      TEXT   -- the timeline wrapper; off by default, ~60% of size

streams             -- one row per thing being watched
  stream_id label query list_id tab watermarked first_poll_ms created_at

tweet_hits          -- which stream matched which tweet, and when we first saw it
  (stream_id, tweet_id) PRIMARY KEY, poll_id, first_seen_ms   -- WITHOUT ROWID

watermarks          -- per stream: how far we have got, and the adaptive interval
  stream_id high_tweet_id high_created_ms
  interval_s ewma_rate consecutive_empty next_poll_ms

polls               -- the audit trail: one row per poll, why it stopped
  poll_id stream_id kind started_ms finished_ms account
  pages results new_tweets dup_tweets orphans max_id min_id
  stop_reason rl_limit rl_remaining rl_reset lag_p50_ms lag_p95_ms error

gaps                -- detected discontinuities. Recorded, never yet backfilled.
  gap_id stream_id lo_tweet_id hi_tweet_id lo_ms hi_ms resume_cursor status

webhook_state       -- how far each endpoint has been delivered (see 6b)
  label last_ms last_tweet_id      -- the cursor, in COLLECTION order
  sent failures next_attempt_ms last_error last_ok_ms
```

Three decisions worth defending:

**`tweet_id` is an INTEGER primary key.** Snowflake ids encode their creation
time in the high bits, so `ORDER BY tweet_id` is chronological for free — no
date parsing, no index on a text column, and millisecond precision where
`created_at` gives seconds.

**`tweets` and `tweet_hits` are separate.** A tweet can match several streams;
storing it once and linking N times keeps one row of truth and makes
"everything" a query over `tweets` rather than a union.

**`collected_at` and `lag_ms` are frozen at first sight.** Re-observing a tweet
updates its engagement counts and `last_seen_at`, never its freshness. A lag
that drifted on re-observation would make the one metric this project exists
for meaningless.

**JS loses integer precision above 2^53 and tweet ids are well past it**, so
every id crossing into JSON is a string. Comparisons in the browser use
`BigInt`.

---

## 5. The dashboard

`python3 main.py serve` → one stdlib `http.server`, one self-contained HTML
page. No npm, no build step, nothing fetched from a CDN.

Three actions, costing wildly different amounts, and the UI's job is to make
that obvious:

| action | cost | when |
|---|---|---|
| Search saved tweets | free, local | nearly always |
| Auto-update | free, local | on by default, every 15s |
| Get new tweets from X | 1 request per 20 tweets | explicit, confirmed, guarded (R15) |
| Sign in to X | a browser, briefly | when an account is added or drops out |

### The sign-in window

X gates login behind captcha and device verification that no scripted HTTP
replay clears, so a human has to see the page and click. On a server there is
no screen to show them.

So the browser runs headless **on the server**, its screen is streamed out as
JPEG frames roughly once a second, and clicks, keystrokes, scrolls and pastes
are forwarded back:

```
browser                          server
  │  POST /api/login/start  ───▶  launch Chrome on profiles/<label>/, goto x.com
  │  ◀───  {state, width, height}
  │  GET  /api/login/frame  ───▶  page.screenshot(jpeg)     (repeats ~1/s)
  │  POST /api/login/act    ───▶  mouse.click / keyboard.type / wheel
  │  ◀───  {state}                 …and detect_state() every time
  │
  │                    state == logged_in
  │  ◀───  {captured, username}   harvest cookies + real UA
  │                               validate with ONE real request  ← R2
  │                               write accounts.db, close Chrome  ← disposed
```

Click coordinates are scaled from the displayed image size back to the real
viewport, so `LOGIN_VIEWPORT` is fixed at 1100×780 — a viewport that changed
size would land clicks in the wrong place.

**The browser is disposable; the session is the asset.** It is closed the moment
the session is captured, on cancel, on close, and after
`LOGIN_IDLE_TIMEOUT_S` (5 min) if someone walks away. Closing the *context*
rather than killing the process is what makes Chrome flush the profile to disk,
and the profile is what keeps the device trusted.

One session at a time, under a lock: two Chromes on one profile directory
corrupt it.

### Session and login

The dashboard's own login is a signed cookie — HMAC over an expiry stamp with a
secret generated at startup. No session table, nothing to leak, and a restart
signs everyone out. `Secure` is set only behind a TLS-terminating proxy;
setting it on a plain-HTTP localhost run would make the browser drop the cookie
and produce an unexplainable login loop.

**The server refuses to bind anywhere but loopback unless `DASH_USER` and
`DASH_PASSWORD` are both set** to something that is not a placeholder and is at
least 12 characters. Refusing to start is the only reliable prevention; a
warning would scroll past.

---

## 6. Setting it up

### On your own machine

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium

cp config.toml.example config.toml     # required — there is no fallback (R6)
cp .env.example .env                   # set DASH_USER / DASH_PASSWORD

python3 main.py serve                  # open http://127.0.0.1:8765
```

Then add an account in the dashboard and press **Save and sign in**.

### On a server

Target here is `scraper.vedictech.in` on `200.97.175.12` — **a shared box that
also serves `namo.vedictech.in` and `report.vedictech.in`.** Read "sharing a
server with other sites" below before changing anything in `deploy/`.

```
                                     ┌─▶ namo.vedictech.in     (not ours)
internet ──HTTPS──▶ nginx :443 ──────┼─▶ report.vedictech.in   (not ours)
                    (TLS, certbot)   │
                                     └─▶ 127.0.0.1:8765  scraper.vedictech.in
                                         (or the next free port)
                                         login required
```

One nginx, one certbot, three certificates. nginx is the only thing exposed;
the Python server binds to loopback, so even a misconfigured firewall cannot
reach it directly.

**1. Point DNS.** An A record for `scraper` → the server's IP. Verify with
`dig +short scraper.vedictech.in` before continuing — `setup.sh` skips HTTPS if
the name does not resolve to that machine, because a failed certbot attempt
counts against Let's Encrypt's rate limit.

**2. Run the script.**

```bash
ssh root@200.97.175.12
apt update && apt install -y git
git clone https://github.com/ganbjhs/XScrapper.git /opt/xscraper/app
bash /opt/xscraper/app/deploy/setup.sh
```

It is idempotent — re-run it after any `git pull`, or after a partial failure.
It installs packages, creates the `xscraper` system user, builds the venv,
downloads headless Chromium, generates a dashboard password if the placeholder
is still there, picks a free port, renders and installs both systemd units and
the nginx config, **obtains the TLS certificate** (R17), allows 80/443 through
ufw if ufw is already active, and finally checks that the domain serves *its
own* certificate.

Useful overrides:

```bash
DOMAIN=other.example.com    bash deploy/setup.sh   # a different hostname
CERT_EMAIL=you@example.com  bash deploy/setup.sh   # where expiry warnings go
XS_PORT=8790                bash deploy/setup.sh   # pin the loopback port
SKIP_BROWSER=1              bash deploy/setup.sh   # skip the ~400 MB Chromium
```

### Sharing a server with other sites

**`200.97.175.12` is not ours alone.** As of 2026-07-31 it serves:

| hostname | whose | notes |
|---|---|---|
| `namo.vedictech.in` | another app | own certificate, own vhost |
| `report.vedictech.in` | another app | own certificate, own vhost |
| `scraper.vedictech.in` | **this project** | own certificate, own vhost |

All three share one nginx and one certbot. That is the entire reason §9's
"neighbour's certificate" failure was possible, and why the rules below are not
optional politeness — they are what keeps three apps on one box from taking
each other down.

**Never touch a hostname that is not `scraper.vedictech.in`.** Not its vhost,
not its certificate, not its systemd unit, not its port. If a change seems to
require it, the change is wrong.

Everything this deployment claims is namespaced so it cannot collide (R18):

| resource | this app takes | never touches |
|---|---|---|
| nginx vhost | `sites-available/scraper.vedictech.in` | any file it did not create |
| nginx include | `snippets/xscraper-app.conf` | — |
| systemd | `xscraper-web`, `xscraper-watch` | any other unit |
| user / files | `xscraper`, `/opt/xscraper` | — |
| certificate | one, for its own domain only | the other two certificates |
| port | first free from 8765 up | any port already in use |

Note the vhost filename. It was once just `sites-available/scraper`, which
names no host at all and invites exactly one question — "is this the scraper
app, or the scraper subdomain, or something else?" — at the moment someone is
deciding whether it is safe to overwrite. Naming it for the host it serves
removes the question.

Port selection is stable and non-invasive: `XS_PORT` if you set it, otherwise
whatever this deployment already uses, otherwise the first free port. If
something else holds 8765 it takes 8766 and writes that into both the systemd
unit and the nginx snippet, so the two can never disagree.

Before enabling its site it asks `nginx -T` whether another block already
claims the hostname, and after reloading it fails loudly if nginx reports a
conflicting server name — disabling **our** site rather than fighting for the
name. Losing our own site is recoverable; breaking `namo` or `report` is
someone else's outage.

`sites-enabled/default` is left alone. Removing it on a shared server is how
you take down someone else's site by accident.

### What a healthy deployment looks like

Confirmed working 2026-07-31. If you are ever unsure whether the site is
actually fine, this is the target state — all four must hold:

```bash
# 1. our vhost is the one enabled, named for its host
ls /etc/nginx/sites-enabled/scraper.vedictech.in

# 2. certbot's TLS lines are in it, and the :80 redirect block exists
nginx -T | grep -n scraper.vedictech.in
#   ...sites-enabled/scraper.vedictech.in:
#   server_name scraper.vedictech.in;
#   ssl_certificate     /etc/letsencrypt/live/scraper.vedictech.in/fullchain.pem;
#   ssl_certificate_key /etc/letsencrypt/live/scraper.vedictech.in/privkey.pem;
#   if ($host = scraper.vedictech.in) {        <- the redirect certbot added
#   server_name scraper.vedictech.in;

# 3. the host serves ITS OWN certificate      <- the check that matters
echo | openssl s_client -servername scraper.vedictech.in \
       -connect scraper.vedictech.in:443 2>/dev/null | openssl x509 -noout -subject
#   subject=CN = scraper.vedictech.in          <- NOT namo, NOT report

# 4. and it answers
curl -sI https://scraper.vedictech.in/ | head -1     #   HTTP/1.1 200 OK
```

Check 3 is the one people skip, and it is the only one that would have caught
the outage. Checks 1, 2 and 4 all passed while the site was serving
`namo.vedictech.in`'s certificate to every visitor.

### If HTTPS breaks again

The recovery that worked, in order. Stop at the first step that fixes it.

```bash
# Is the certificate still there? (it usually is — the loss is nginx's use of
# it, not the certificate itself, so do NOT start by re-issuing)
ls /etc/letsencrypt/live/scraper.vedictech.in/

# Re-install it into nginx. --reinstall, not a fresh issue: re-issuing burns a
# Let's Encrypt rate-limit slot to solve a problem that is not about expiry.
certbot --nginx -d scraper.vedictech.in --redirect --reinstall

# Then re-run the deploy, which is idempotent and will verify the result
cd /opt/xscraper/app && git pull && bash deploy/setup.sh
```

`setup.sh` does all of this itself now, including the `--reinstall` branch and
the certificate-identity check. Running it is the normal fix; the manual
commands are for when you want to see each step.

**Before running `git pull` on the server, confirm the commit is actually on
`origin`.** A pull that reports "Already up to date" and then runs the *old*
script looks identical to a successful deploy — same output, same success
message, nothing fixed. This cost a full round trip once. From the machine you
committed on:

```bash
git log --oneline -1 origin/main     # must be the commit you expect
```

**3. Add an account in the dashboard.** Not on the server's command line —
there is no screen there. The sign-in window is exactly for this.

**4. Start collecting.**

```bash
systemctl start xscraper-watch
journalctl -u xscraper-watch -f
```

### Two risks the deployment does not remove

1. **`accounts.db` holds a live `auth_token`.** Anyone with root on that box
   owns the X account — posting, DMs, settings. Use a throwaway you can afford
   to lose, never a personal account.
2. **A datacenter IP is a ban signal.** X treats VPS ranges with more suspicion
   than residential ones. Expect a shorter account life than on a laptop.

Neither blocks deployment. Both are worth knowing before filing the account
under "working".

### Updating

```bash
cd /opt/xscraper/app && bash deploy/setup.sh    # pulls, reinstalls, restarts
```

### Backups

`results.db` is the only thing that is not reproducible, and `profiles/` is the
only thing that keeps X treating this machine as a known device.

```bash
sudo -u xscraper sqlite3 /opt/xscraper/app/results.db ".backup /tmp/results-$(date +%F).db"
cp -r profiles profiles.backup          # with the browser closed
```

Use `.backup`, never `cp`, for the databases: both run in WAL mode and a plain
copy can land mid-transaction.

---

### When the site is wrong but the app is fine

Work outside in. Most "the site is broken" reports are nginx, not Python.

```bash
# 1. Is the app itself up?
curl -sI http://127.0.0.1:$(sed -n 's/.*--port \([0-9]*\).*/\1/p' \
     /etc/systemd/system/xscraper-web.service)/ | head -1

# 2. Does the hostname serve ITS OWN certificate?  <- the one people skip
echo | openssl s_client -servername scraper.vedictech.in \
       -connect scraper.vedictech.in:443 2>/dev/null \
     | openssl x509 -noout -subject

# 3. Who else claims this name?
nginx -T | grep -n 'scraper.vedictech.in'
```

| symptom | almost always |
|---|---|
| HTTP works, HTTPS shows the certificate for **`namo` or `report`** | our vhost has no 443 block; nginx fell through to a neighbour. `certbot --nginx -d DOMAIN --redirect --reinstall` |
| Browser says "Not secure", no certificate error | no certificate at all yet — DNS was wrong when setup.sh ran. Fix DNS, re-run it |
| 502 Bad Gateway | nginx and systemd disagree about the port. Re-run setup.sh; it writes both from one value |
| 404 / someone else's app | two server blocks claim the name. `nginx -T \| grep` for it |
| Requests hang, sign-in window never paints | `proxy_buffering off` missing from the snippet |

Re-running `bash deploy/setup.sh` is the fix for most of these and is always
safe. It cannot remove a certificate (R17) or touch another site (R18).

## 6b. Feeding other systems

The dashboard is one consumer of `results.db`. Other systems get the same
tweets two ways, and they compose: push for speed, pull to catch up or
backfill.

### Push — we POST to you

Declare an endpoint in `config.toml`; the sender runs inside `watch`.

```toml
[[webhooks]]
label      = "main"
url        = "https://your-system.example.com/hooks/tweets"
secret_env = "WEBHOOK_SECRET_MAIN"     # names a variable in .env
streams    = []                        # e.g. ["politicians"]; empty = all
batch_size = 50
enabled    = true
```

```http
POST /hooks/tweets
Content-Type: application/json
X-XS-Timestamp: 1785414135
X-XS-Signature: sha256=<hmac>
X-XS-Webhook: main

{"version":1,"webhook":"main","sent_at":1785414135,"count":2,
 "tweets":[{"tweet_id":"2082803616151437632","text":"…","author_username":"…",
            "created_at":"…","lag_ms":15400,"streams":["politicians"], …}]}
```

**Verify the signature.** HMAC-SHA256 over `"<timestamp>.<raw body>"` with the
shared secret. The timestamp is inside the signed material so a captured
delivery cannot be replayed forever; reject anything older than a few minutes.
`webhook.verify()` is a working reference implementation, and it is what the
test suite exercises — port it rather than reinventing it.

**De-duplicate on `tweet_id`.** Delivery is *at-least-once*: the cursor only
advances after a 2xx, so a receiver that answers 200 and then dies before
committing will see that batch again. `tweet_id` is stable forever.

**`tweet_id` is a string, always.** Tweet ids passed 2⁵³ years ago, so any
consumer parsing JSON numbers — every JavaScript one — would silently round it
and corrupt the id.

Three properties worth relying on:

| property | why it holds |
|---|---|
| Nothing is lost | Delivery position is a cursor in the database, not a queue in memory. A receiver down for a day catches up by itself; a restart here changes nothing. |
| Your outage is not our lag | The sender is its own task with its own HTTP client. A receiver that hangs for its full timeout costs one background task; polling X keeps its schedule. |
| Failures back off | 5s doubling to a 15-minute cap, cursor untouched. Recovery needs no manual step. |

The cursor is `(collected_ms, tweet_id)`, **not** `tweet_id`. Tweets do not
arrive in posting order — X indexes some late, so a tweet collected now can
carry an older snowflake than one collected a minute ago. A `tweet_id` cursor
steps over those permanently and the gap is invisible.

A new endpoint starts from **now**, not from the whole archive.

```bash
python3 main.py webhook --test      # one real tweet to every endpoint, consuming nothing
python3 main.py webhook --status    # how far each has got, and what is stuck
python3 main.py watch --all --no-webhooks   # collect without delivering
```

### Push — Telegram

Same machinery as webhooks: same cursor, same back-off, same "never block
collection". Only the formatting and the transport differ, which is why it
lives beside them rather than as its own subsystem.

Set it up in the dashboard under **What we are watching → ⚙ Telegram &
settings**: make a bot by messaging `@BotFather`, paste the token and a chat
id, press *Send a test*. Then switch it on per stream with the ⚙ beside that
stream, where you can also set a minimum like count and skip retweets.

Unlike webhooks, Telegram is **not** declared in `config.toml`. It is switched
on per stream from the dashboard, so its settings live where the dashboard can
write them and the running watcher can re-read them — the `streams` table. The
bot token is the exception: it is a credential, so it goes in `.env` with the
others.

Two things Telegram forces:

- **Messages are batched.** Their limit is about 20 messages a minute into one
  group, so a busy list sending one message per tweet would start collecting
  429s within a minute. Tweets are packed up to 3500 characters per message
  with a pause between sends.
- **HTML, not Markdown.** Tweet text is full of unbalanced underscores,
  asterisks and brackets, and Telegram rejects a whole message whose Markdown
  does not parse. Escaping three characters for HTML always works.

A tweet too long for one message is trimmed, not dropped — and the *raw* text
is trimmed before escaping, never after. Cutting an escaped string can slice
through the middle of an entity (`&amp;` → `&am`), which Telegram rejects as
malformed, taking the whole batch down with it.

### Per-stream settings

The ⚙ beside each stream sets how often it is checked, how many tweets per
check, whether it is paused, and where it goes.

**These live in the database, not `config.toml`.** `config.toml` declares WHAT
to watch; the `streams` table says how it is tuned right now. The split is what
lets a change in the dashboard reach the running watcher on its next cycle —
editing `config.toml` would need a restart, and rewriting a file a human
hand-edited would eat their comments.

`NULL` means "no override, use config.toml", which is deliberately not the same
as `0`: a `min_interval_s` of 0 would poll as fast as the loop can turn.

### Pull — you ask us

Set `API_KEYS` in `.env` (comma-separated, so one consumer can be revoked
alone). Programs send a bearer token; browsers keep using the session cookie.

```bash
curl -H "Authorization: Bearer $KEY" \
     "https://scraper.vedictech.in/api/tweets?since_collected_ms=0&limit=200"
```

| endpoint | gives you |
|---|---|
| `GET /api/tweets` | the tweets, with all the dashboard's filters |
| `GET /api/streams` | what exists and how much of it |
| `GET /api/export` | the same as CSV |
| `GET /api/status` | account health, rate-limit budget |
| `POST /api/fetch` | go to X now. Spends budget — see below |

Filters: `stream` `q` `author` `lang` `min_likes` `since` `has_media` `order`
`limit` (max 500) `offset`.

**Two cursors, and the difference matters:**

- `since_collected_ms` — everything *we saw* after this point. Gapless. Use
  this to mirror the database.
- `since_id` — everything *posted* after this tweet. What people expect, and
  fine for "show me what's new", but it silently skips late-indexed tweets for
  the same reason the webhook cursor does not use it.

Every response carries `cursor` with both, so a consumer never has to work out
which field to remember:

```json
{"total": 384, "rows": [...],
 "cursor": {"since_id": "2082803115678687574", "since_collected_ms": 1785414135000},
 "has_more": true}
```

### What a key may not do

An allowlist, enforced server-side — not a convention:

```
allowed   /api/tweets  /api/streams  /api/export  /api/status  /api/guard  /api/fetch
refused   /api/account      writes secrets to disk
          /api/login/*      launches a browser process
          /api/stream/*     changes what is collected, or destroys data
          /api/settings/*   writes credentials to disk
```

A leaked key then costs you data exposure, not a banned X account or a
rewritten `config.toml`.

`POST /api/fetch` still goes through the guard (R7): it re-checks the budget
independently of any client, and refuses unless warnings are acknowledged with
`"ack": true`. **A client that loops on it will spend the same ~50 requests per
15 minutes the collector needs** and can get the account restricted (R11). Push
is the right shape for staying current; `/api/fetch` is for going and getting
something specific.

## 7. Operating

**Daily**

```bash
python3 main.py guard      # what is risky right now, and why
python3 main.py doctor     # accounts, streams, lag
```

**Account hygiene**

1. Throwaways only, never a personal account.
2. Run 3–5. One account is a single point of failure and has the smallest budget.
3. Warm new accounts up — sign in, browse, follow a few things over a couple of
   days. A day-old account making 200 requests/hour is the obvious pattern.
4. Give each its own proxy. Residential beats datacenter.
5. **Back up `profiles/`** with Chrome closed. That directory *is* the
   trusted-device asset; losing it means facing new-device challenges everywhere.

**When an account dies**

twscrape auto-deactivates on `(32)` session expired, `(326)` access denied,
`(88)` rate limit with budget remaining. The pool skips it and keeps going.

Press **Sign in to X** in the dashboard. If X has genuinely suspended the
account, it is gone — swap in another. **The design assumes accounts die.**

**When X changes**

- *Sudden 404 on every search* → the GraphQL doc_id rotated.
  `pip3 install -U twscrape`, or set `X_SEARCH_DOC_ID` in `.env` from DevTools
  to unblock immediately.
- *Fields going null* → schema drift. Fix the parser, then **replay** from the
  raw payloads (R9).
- *`doctor --selftest` failing* → twscrape internals moved. Do not ignore it.

---

## 8. Choosing what to watch

**Prefer a list.** Measured on this database: `ListLatestTweetsTimeline` is
allowed **500 requests per 15 minutes**, `SearchTimeline` **50**. They are
separate budgets, so adding a list raises total capacity instead of dividing
it. A list is the lowest-lag source available.

The trade-off: X does no server-side filtering on a list timeline — no
`min_faves`, no `lang` — so narrowing happens locally, after the tweets are
already paid for.

**Be specific with keyword searches.** This is not a style note; it is the
single biggest source of junk in this database. A stream defined as
`(RBI OR "repo rate" OR "monetary policy")` — intended to catch India's central
bank — collected almost entirely baseball, because RBI is also "runs batted
in". A `world_news` stream on `(breaking OR urgent) news` collected wildfires,
crypto spam and a TV-serial murder case.

Every ad-hoc "Get new tweets" search in the dashboard also becomes a permanent
stream row named `ui:<what you typed>`. That is how you end up with eighteen
streams, fourteen of them junk. Watch deliberately; search ad-hoc sparingly.

---

## 8b. Instagram — planned, not built

**Nothing in this section exists yet.** It is the design agreed before writing
any of it, recorded here so the decisions are visible and can be argued with.
Rules are numbered `IG*` so they cannot be mistaken for the `R*` rules, which
describe code that actually runs.

### The target is completeness, not speed

Decided 2026-08-01, and it inverts the priority the X side was built around.
Apify was tried first and **missed posts** — returned an incomplete set. So for
Instagram the metric is not lag, it is: *did we get everything?*

That single choice drives most of what follows. On X, freshness justified
stopping a poll early at the watermark. Here, stopping early is the thing most
likely to reproduce the failure being fixed.

### Why the scraping route, knowing the official one is free

§6b's arithmetic stands: Meta charges nothing, and `business_discovery` sits on
a flat 200-calls-per-hour limit. It was rejected because it only reaches public
Business/Creator accounts and because its freshness is measured in tens of
minutes.

**Run `tools/ig_probe.py` anyway**, and not to reconsider. For every target that
*is* a Business/Creator account, the official API becomes a **correctness
oracle**: the same handle fetched two ways, and any post the official API knows
about that the scraper missed is a proven gap, with a name and a timestamp.
That is the difference between believing the collector is complete and being
able to show it. Nothing else available gives a ground truth to check against.

### Three paths, doing different jobs

A merged feed is fast and cheap but is a *merge* — Instagram decides what
enters it and how far back it goes. Trusting it alone is how you end up with
Apify's problem.

| path | cost | job |
|---|---|---|
| **Following / Favourites feed** | 1 request, many accounts | the fast path. Notices new posts. |
| **Per-profile sweep** | 1 request per handle | the authoritative path. Slow cycle, proves what a handle actually posted. |
| **Official API** | free, where reachable | the oracle. Independent confirmation. |

Reconcile them: anything a sweep or the oracle finds that the feed never
delivered is recorded in `gaps` exactly as X's missed ranges are. **A gap that
is detected is a bug report; a gap that is not detected is data loss.**

The ranked home feed is never used, for the same reason as R4 — it silently
omits, so an early stop on it means nothing.

### The rules

**IG1 — One identity per account, invented once, never changed.**
Device id, user agent, app and OS version, locale, timezone. The X equivalent
is the persistent Chrome profile (§7). Changing any of these mid-life is a
stronger signal than the request volume ever is: it reads as a stolen session.

**IG2 — One residential IP per account, for that account's whole life.**
Datacenter ranges are penalised heavily, and an account that moves address
looks compromised. This is not optional the way it is on X.

**IG3 — The budget is ours to impose, because Instagram publishes none.**
X returns `x-rate-limit-*` on every response and `guard.assess` is built on it.
Instagram returns nothing equivalent, so the ceiling is a number *we choose and
enforce*, using the self-count already in `guard._budget`. Treat every figure
as a guess until §8b's ramp has measured it.

**IG4 — Stop at the first challenge. Never retry through one.**
A checkpoint means Instagram wants a human. Retrying turns a recoverable
challenge into a dead account. Mark it, stop that account, and say so.

**IG5 — The collector never writes. Follows are done by hand.**
The approach needs the collector account to follow its targets — do that
manually, once, from a phone. Automated following, liking or posting is what
Instagram's spam systems are actually built to catch, and it would put the
account's own writes in the same session as its reads.

**IG6 — No account collects until it is warmed.**
Age plus real human use first. A day-old account making structured requests is
the most obvious pattern there is.

**IG7 — Accounts sleep.**
Quiet hours, per account, in its own configured timezone. Continuous
round-the-clock polling is not a rate problem, it is a *shape* problem — no
person is awake at 04:00 every night.

**IG8 — One request at a time per account, always jittered.**
No concurrency within an account, and no fixed interval. §7's jitter reasoning,
applied harder.

**IG9 — A chronological source, or none.**
Following or Favourites. Never the ranked feed (R4).

**IG10 — The first anomaly stops that account and is recorded.**
Not just hard blocks — a truncated feed, a latency jump, or an empty response
where posts were expected. These are the *leading* indicators, and they are the
only warning Instagram gives before it acts.

### Finding the safe rate

**You cannot find Instagram's limit the way you find X's.** On X a 429 is
harmless, recoverable, and the headers tell you where you stand — the stress
test in §7 can safely walk right up to the line. On Instagram the first hard
signal *is* the punishment: an action-block, or a checkpoint on the account.

So the ramp never targets the wall. It finds the highest rate at which nothing
looks wrong, and stops one step below.

```
day 1    1 poll / 30 min     record: latency, posts returned, error shapes
day 2    1 poll / 20 min     compare against day 1
day 3    1 poll / 15 min     ...
day 4    1 poll / 10 min
```

Advance a step only after a clean 24 hours. Watch the three leading indicators
from IG10 rather than waiting for a block. **At the first anomaly: drop back
two steps, hold for 48 hours, and treat the step below as the ceiling.**

Run it on one account against one target. The number found is that account's
number — limits appear to vary with account age and history, so it is a
starting point for others, not a constant.

### Build order

1. **Recon, collecting nothing.** One warmed account, by hand, recording the
   real shapes: what the feed returns, what a challenge looks like, what a
   truncated response looks like. Every number in this section is a guess until
   this is done.
2. **`engine_ig.py`** behind the same interface as `engine.py`, so `collector`,
   `store`, `guard`, `web`, the API and delivery all carry over untouched. This
   is the payoff from the engine seam (§3).
3. **One account, one target, slow, for a week.** Stability before scale.
4. **The ramp above.**
5. **Reconciliation** — profile sweeps and the official oracle, writing `gaps`.
6. **More accounts,** each with its own identity and IP (IG1, IG2).

Do not start at 2. Step 1 is what makes the rest not fiction.

## 9. Problems and solutions

Everything that has actually gone wrong, with the fix. This is the
highest-value section — each entry cost real debugging time.

### Auth

| Problem | Root cause | Solution |
|---|---|---|
| `accounts.db` stayed empty; login never succeeded | HTTP password login is X's most captcha-gated path | Real browser (Playwright) for login only; harvest cookies for HTTP collection |
| Expired cookies reported success, failed at search | twscrape sets `active=True` on non-empty cookie strings, no network call | Validate with a real request before activating (R2) |
| Editing cookies in `.env` had no effect | `add_account` early-returns when the row exists (`accounts_pool.py:93-97`) | Use `pool.save()` — a full upsert |
| One failed login excluded an account forever | `login_all` filters `WHERE error_msg IS NULL` | Clear `error_msg` on every successful re-auth |
| Login detection hung on a logged-in browser | v1.1 REST endpoints **all return 404/code 34** — measured on `api.x.com`, `x.com/i/api`, `api.twitter.com` | Read identity from the DOM; validate via GraphQL Bookmarks |
| Headless login saw "unknown" state | `no_viewport=True` gave a narrow window; X hides the left nav below ~1000px | Pin a 1440×900 viewport when headless |
| Adding an account in the dashboard ended in "now run this command" | `/api/account` deliberately did not log in, and `_CFG` was read once at startup so the new account was invisible in-process anyway | Reload the config on write and open the sign-in window immediately (R14) |
| "Sign in to X" button did nothing | The button was rendered but no click handler was ever bound — and `status()` redraws the whole panel every 15s, so anything bound once would have been discarded anyway | Bind the handler in the same pass that draws the buttons |

### Collection

| Problem | Root cause | Solution |
|---|---|---|
| Tweets silently missing | twscrape's parser drops any tweet whose ID is in `retweeted_ids` — a tweet that is both a hit and retweeted on the same page vanishes | Parse with `to_old_rep` + `Tweet.parse`, bypassing `_parse_items` |
| Watermark logic would stop on page 1 | `api.search()` yields dict-insertion order with embedded old quotes mixed in | Key off timeline `entryId`s, which are true timeline order (R4) |
| Tweets indexed late were lost forever | Stopping exactly at the watermark leaves a blind spot | Stop 60s *below* the watermark (overlap window) |
| Accounts vanished for 15 minutes | Generator not closed on early break | `aclosing` everywhere (R3) |
| A database of 1,853 tweets, ~1,455 of them junk | Broad keyword streams (§8) plus every ad-hoc dashboard search becoming a permanent stream | One-time manual purge against a backup, keeping four political streams; config narrowed to a single curated list |

### Runtime

| Problem | Root cause | Solution |
|---|---|---|
| `RuntimeError: Lock is bound to a different event loop` | twscrape holds a module-level `asyncio.Lock` (`db.py:12`); the dashboard called `asyncio.run()` per request | One persistent loop on a background thread; handlers submit via `run_coroutine_threadsafe` |
| Dashboard froze permanently | Same cause. Concurrent calls across dead loops **deadlock** rather than erroring — reproduction had to be killed at 2 minutes | Same fix. Regression test asserts 6 concurrent threads all complete |
| Guard reported "No accounts" for a healthy account | Swallowed exception (R6) | Read `accounts.db` directly with sqlite3 — no event loop, no nesting |
| Rate-limit rule silently disabled | `rl_reset` column missing on older databases; error swallowed (R6) | Column-aware query + explicit migration + surface read failures |
| Auto-update never refreshed, but its indicator pulsed | The "Get new tweets" button and the auto-update checkbox both had `id="live"`. `$("#live")` returns the first match — the button — so `.checked` was `undefined` and `tick()` returned on its first line, every time | Renamed to `#getnew` and `#autorefresh`. A duplicate-id scan is worth running on any page this size |
| `curl -I https://…` reported the site broken while it served fine | `BaseHTTPRequestHandler` answers 501 to any method with no `do_*` method, and HEAD had none — the exact check the deploy docs told you to run | `do_HEAD` delegates to `do_GET` and suppresses the body |
| Browser fails to start under systemd | Chromium's sandbox needs to gain privileges, which `NoNewPrivileges=true` forbids; `/dev/shm` is also too small under `PrivateTmp` | Dropped `NoNewPrivileges` from the web unit, added `--disable-dev-shm-usage`, and `_launch` falls back to an unsandboxed browser — announced, never silent |

### The neighbour's certificate

The most instructive failure so far, because every individual piece looked
healthy.

This server also hosts `namo.vedictech.in`. One afternoon `scraper.vedictech.in`
started showing a certificate error in the browser. What was actually true:

| checked | result |
|---|---|
| DNS | correct, → the right server |
| the Python app | running, healthy |
| `http://scraper.vedictech.in/` | **200, serving our login page** |
| `/etc/letsencrypt/live/scraper.vedictech.in/` | present, valid, not expired |
| `https://scraper.vedictech.in/` | served the certificate for **`namo.vedictech.in`** |

Chain of causes:

1. `setup.sh` did `cp deploy/nginx-scraper.conf /etc/nginx/sites-available/scraper`
   on **every** run.
2. `certbot --nginx` had earlier edited that same file in place to add
   `listen 443 ssl` and the certificate paths.
3. So a routine re-deploy deleted the TLS block. Our vhost went back to
   HTTP-only — which is why port 80 kept working perfectly.
4. A browser asking for `scraper.vedictech.in` over HTTPS now matched **no**
   server block. nginx falls back to the first SSL block it loaded, which was
   the neighbouring app's, and served that app's certificate.
5. The guard against this — "if the certificate already exists, skip" — asked
   the wrong question. The certificate did exist. It was nginx that was not
   using it. So every subsequent run reported success and changed nothing.

Three fixes, because one would not have been enough:

- **Split the config.** The vhost (`sites-available/<domain>`) is written once
  and then belongs to certbot. Everything that changes between deploys lives in
  `snippets/xscraper-app.conf`, which contains no `server` or `listen`
  directive and is rewritten freely. Re-running can no longer reach the TLS
  block. (R17)
- **Ask the right question.** The condition is now "does the vhost actually
  contain a 443/ssl_certificate line", not "does a certificate exist". Cert
  present but nginx not using it → `certbot --reinstall`, which re-installs
  into nginx without burning a rate-limit slot on a needless re-issue.
- **Verify who answered.** setup.sh ends by opening a TLS connection with SNI
  and comparing the served certificate's CN to the domain. A 200 over HTTP
  proves nothing about HTTPS; this is the check that would have caught it on
  day one.

**The lesson generalises: when a tool edits your config file, that file is no
longer yours to overwrite.** Own an include, not the whole thing.

**Resolved 2026-07-31.** The deploy detected the certificate present but unused,
ran `certbot --reinstall`, and the identity check then confirmed
`subject=CN = scraper.vedictech.in`. The vhost is now
`sites-enabled/scraper.vedictech.in` with certbot's TLS block intact and its
`:80` redirect alongside. See §6, "what a healthy deployment looks like", for
the exact state to compare against next time.

Two smaller things this cost, both worth remembering:

- **The old vhost had to be migrated, not replaced.** A file still called
  `sites-available/scraper` and a new one called `scraper.vedictech.in` would
  both have declared `server_name scraper.vedictech.in` — a second conflict on
  top of the first. setup.sh moves the old file into the new name so certbot's
  work travels with it, rather than deleting it and re-issuing.
- **The fix was described before it was pushed.** The server was told to
  `git pull`, correctly reported "Already up to date", and ran the old script —
  whose output is a plausible success. Nothing distinguishes "deployed" from
  "pulled nothing" except checking `origin` first.

### The phantom account

Worth its own entry, because it is R6 doing real damage.

`load_config()` used to synthesize a one-account config from bare
`X_USERNAME` / `X_PASSWORD` / `X_COOKIES` environment variables whenever
`config.toml` was absent, labelling it `legacy`. Nothing ever failed. The result:

- `config.toml` declared `acct_a` → `profiles/acct_a`, a directory that did not exist;
- the account actually collecting was `@HanaMal93`, under `profiles/legacy`;
- the dashboard showed both — one grey "never signed in", one green — and
  neither line was wrong, which is what made it hard to see;
- `auth.health()` could not join the real account back to any configured label.

The fallback is gone. A missing `config.toml` is now an error that says to copy
the template. **A config guessed from stray environment variables is how an
account ends up collecting under a label nobody chose.**

### Known and unfixed

| Limitation | Status |
|---|---|
| No global rate governor | **Biggest gap.** Each stream self-paces; nothing enforces a total ceiling across streams. |
| Gaps detected but not backfilled | Recorded in `gaps`, reported by `doctor`. Nothing fills them. |
| Proxies untested | Config supports `proxy` per account; never exercised. |
| TLS fingerprint mismatch | httpx sends Python TLS with a Chrome UA. `curl_cffi` fixes it but strips our real UA — a real trade, not a free win. |
| `xclid` UA inconsistency | twscrape fetches `x.com/tesla` with a random UA. Cached per process, so long-lived processes minimise it. |
| A long fetch blocks the dashboard | `_FETCH_LOCK` is held for the whole fetch. A job queue would fix it. |
| Latest index ~7-day horizon | X-side. Unfixable. |
| High-volume queries sampled | X-side. Narrower queries are more complete. |

---

## 10. If you rebuilt it

### Keep

- **The module split.** Each has one reason to exist and dependencies run one
  way. No cycles.
- **The `engine` seam.** Only `engine.py` knows X's wire format — which is what
  lets the tests exercise the whole poll loop offline.
- **The `auth` seam.** Only `auth.py` imports Playwright, so collection never
  pulls in a browser.
- **Snowflake IDs as the ordering key.**
- **Raw payload retention** (R9).
- **Offline-first tests.** 140+ assertions, no network. This is why the
  freshness logic is verifiable rather than hopefully-correct.
- **The sign-in window.** It is the difference between a system an operator can
  run and one that needs its author.

### Change

1. **Build the rate governor first, not last.** A global token bucket sized from
   measured `rl_limit`, shared across streams and accounts. This is the one
   structural gap that actually raises ban risk.

2. **Make the guard a hard gate from day one.** It was added last; it should
   have been second, right after auth.

3. **Do not depend on any REST endpoint.** All four v1.1 endpoints died. Assume
   GraphQL only, and assume every `/i/api` request needs
   `x-client-transaction-id` — a 404 means a signing problem, not a missing
   endpoint.

4. **Consider owning the GraphQL client.** twscrape's value is almost entirely
   `xclid.py` (the transaction-id generator) and doc_id maintenance. Everything
   else — the pool, the parser — this project already works around.

5. **Reach for lists before more accounts.** An extra account multiplies the
   search budget by 1. Moving a query to an X List multiplies it by 10, on the
   same account, with no extra ban surface. This was found by measuring, and it
   inverts the obvious scaling instinct.

6. **Separate ad-hoc searches from watched streams in the schema.** Every
   dashboard search becoming a permanent `ui:` stream is what turned this
   database into eighteen streams of mostly junk (§8).

7. **Store engagement history, not just latest.** A
   `tweet_metrics(tweet_id, observed_ms, likes, …)` table would make virality
   analysis possible for one extra row per sighting.

8. **Separate the fetch worker from the HTTP handler.** A job queue with
   progress streaming would fix the UI freeze.

### Do not

- **Do not scrape the DOM for tweets.** Counts come back truncated (`1.2K`),
  selectors break constantly, and it is slower than every alternative. The
  browser is for signing in only.
- **Do not run the browser during collection.** It is the single biggest
  resource cost and buys nothing once you hold the cookies.
- **Do not add per-keystroke live search.** It would drain the budget the
  watcher needs.
- **Do not make deleting data easy** (R16). Stopping is one click; destroying
  needs the name typed.
- **Do not unpin twscrape** without running `doctor --selftest`.
- **Do not set `TWS_PROXY`.** It silently overrides every per-account proxy,
  collapsing the pool onto one IP. Config rejects it.

---

## 11. Reference

**Rate budget is per GraphQL operation, not per account.** Confirmed against
this database:

| queue | limit / 15 min | floor, 1 account |
|---|---|---|
| `SearchTimeline` | 50 | ~24 s |
| `ListLatestTweetsTimeline` | 500 | ~2.5 s |

Minus the 25% reserve (R11), search sustains one poll every ~24s per account;
lists sustain one every ~2.5s.

**Costs:** local search 0 · steady-state poll 1 · cold poll ≤ `max_pages_per_poll`
· dashboard fetch 1 per page (cap 25, refused above — R8).

**Exit codes:** 0 ok · 2 auth · 3 search · 4 config · 6 no account.

**Guard levels:** `BLOCK` will damage something, refuse · `WARN` proceed only
knowingly, needs `ack` over HTTP · `note` informational.

**Commands**

```bash
python3 main.py login   --account LABEL [--refresh-only] [--force] [--debug-detect]
python3 main.py doctor  [--accounts] [--streams] [--selftest] [--lag] [--since 24h]
python3 main.py guard   [--action fetch --cost 5] [--json]
python3 main.py serve   [--host H] [--port P] [--behind-proxy]
python3 main.py watch   [--stream LABEL | --all] [--once] [--no-webhooks]
python3 main.py webhook [--status] [--test]
python3 main.py search  --query '...' | --list URL  [--limit N]
python3 main.py export  [--stream LABEL] [--since 6h] [--format csv|json|jsonl|raw]
python3 tests/test_all.py
```

**Files:** `config` what you declared · `auth` how you get a session · `engine`
how we talk to X · `collector` when to poll · `store` what we keep · `guard`
what not to do · `web` how you look at it · `webhook` how other systems get it ·
`main` how you drive it.
