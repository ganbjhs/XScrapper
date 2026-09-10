# Collector ⇄ report.vedictech.in — the Collector's answer, and the sheet-driven plan

Reply to `REPORT_TOOL_ANSWERS.md` (VedicReport v3.3.0, git `493b51b`).
Written from the Collector side at git head `1d6b6ca`, against the live campaign
sheet `1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0`
("Varansi Day Wise Data"), read 10 Sep 2026.

Everything here is quoted from code or measured from that sheet. Where a thing
is absent it says **does not exist**.

---

## Part 0 — The verdict, up front

**Your analysis is correct and we accept it.** The tool pulls, we serve. We are
not asking you to build a push. Of the eight gaps you listed on your side, we
agree with all eight and have added nothing to them.

Three things you should know before you start:

1. **One line on our side is currently breaking the whole integration.** Our
   `/api/links` returns the array under `"rows"`. Your `normalize_many()`
   accepts `posts | data | items | results | records` and nothing else, so the
   sync throws *"Expected a JSON array of posts"* on the very first call, every
   time. We are adding `"items"` as a second key pointing at the same list.
   We are **not** renaming `rows` — Watch-Tower already consumes it, and
   `LINKS_CONSUMER_HANDOVER.md` promises fields are added, never renamed.
   Everything below follows that rule: `group` beside `section`,
   `thumbnail_url` beside `thumb`, never instead of.

2. **The sheet is not shaped the way either of us assumed.** Part 1 below. The
   short version: it is 53 tabs, 50 of them day tabs (good — that is exactly
   your `tab_mode: dated`, and it already works), but **41% of the links on a
   typical day tab are Facebook, Instagram or YouTube**, and our links
   watchlist is X-only. This is the largest open problem in the whole
   integration and it is not on your list or ours.

3. **The history the client is paying for is built entirely on your side.**
   The Collector keeps no counter history and never will — it overwrites in
   place, by design (`X_LINKS_PLAN.md` §4). Your Part 4.2 `post_metric_days`
   table and your Part 4.3 fix to `_fold_scraper` back-updating are the whole
   product. If those two do not land, every daily pull rewrites yesterday and
   the Growth chart is a flat line forever. Please treat them as P0, above the
   cosmetic field mapping.

---

## Part 1 — What the real sheet actually looks like

Read on 10 Sep 2026 from
`docs.google.com/spreadsheets/d/1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0`.
The numbers below are that sheet's, not a sample's.

### 1.1 Fifty-three tabs, of two different kinds

| Kind | Count | Names | Our `day` |
|---|---|---|---|
| **Day tabs** | 50 | `8/9/26`, `7/9/26`, … `21/7/26` | ✅ 49 of 50 parse; see 1.4 |
| **Archive tabs** | 3 | `Tweet LInks`, `Counter Links`, `3rd Party Posting` | ❌ `null` |

The day tabs are `D/M/YY` (Indian order). We ran the real names through
`links.parse_tab_day()` — every one resolves correctly:

```
parse OK   : 49/53
day = null : 3   -> ['Tweet LInks', 'Counter Links', '3rd Party Posting']
wrong year : 1   -> ('24/7/25' -> '2025-07-24')          ← see 1.4

'8/9/26'  -> 2026-09-08     '31/8/26' -> 2026-08-31     '21/7/26' -> 2026-07-21
```

**The tab naming needs no change.** `D/M/YY` is read correctly, and so are
`D-M-YY`, `DD/MM/YYYY`, `2026-09-08`, `8 Sep`, `Sep 8, 2026`. Keep writing them
exactly as they are written today.

**So `tab_mode: dated` is the right mode for this sheet, not `fixed`.** Your
§2.2 asked for `fixed` (one designated tab). Do not build the client
configuration around `fixed` — this sheet, which is the real one, is `dated`,
and our sheet binder already treats every non-hidden tab as one watchlist named
after the tab, with the day parsed from its title
(`links.sync_sheet` → `store.links_watchlist_for_tab(…, day=parse_tab_day(tab.title))`).
Tomorrow's new tab becomes tomorrow's watchlist by itself, within
`DEFAULT_SYNC_S = 600` seconds. **This part of the design is already built and
already matches what was asked for.**

### 1.2 The layout inside a day tab

```
| Posts                              | views  | Likes  |
| [merged] 3 Party Pages Posting     |        |        |   ← section heading
| https://www.facebook.com/reel/278… | 30,000 | 60,000 |
| https://www.instagram.com/reel/Dc8…| 30,000 | 60,000 |
| [merged] Hyper Local Pages Posting |        |        |   ← section heading
| https://youtube.com/shorts/3V405Q… | 30,000 | 60,000 |
| [merged] National X Influencers    |        |        |
| https://x.com/anujakapurindia/sta… | 30,000 | 60,000 |
| [merged] Counter Comments Links    |        |        |
```

- The merged one-cell rows are the headings. Our `links.scan_values()` reads
  them as `section` and carries each onto every link beneath it, which is
  exactly your `group` → `category_raw`. The four headings in this sheet are
  **3 Party Pages Posting**, **Hyper Local Pages Posting**, **National X
  Influencers**, **Counter Comments Links**. Carry them verbatim; they are
  what `clients.category_map` will be keyed on.
- **Columns B and C (`views`, `Likes`) are typed by hand by the operator.**
  Right now every row reads `30,000 / 60,000`, i.e. placeholder. See Part 5.3 —
  these are the "hidden metrics" and they must never collide with scraped
  numbers.
- The three archive tabs use a **different layout**: dates as row labels in
  column A (`Date- 4-7-26`, `5-7-26`, …) with links in column B. Our scanner
  will read those date labels as *section headings*, not as days, and every
  link in those tabs will land with `day = null` and a nonsense `group`.

### 1.3 The platform split — the biggest problem in this integration

Measured across the whole sheet:

| Host | Links |
|---|---|
| `x.com` | 2,295 (**1,964 unique** post ids) |
| `facebook.com` | 530 |
| `instagram.com` | 355 |
| `youtube.com` | 44 |

And on the **day tabs specifically** — the ones that feed the dashboard:

```
day tabs:   1,394 X links     935 non-X links     (40% non-X)
tab 8/9/26:    21 X links      27 non-X links     (56% non-X)
```

**On a typical day tab there are more Facebook/Instagram links than X links.**

Our links watchlist is X-only. `links.STATUS_RE` matches only
`x.com` / `twitter.com` (and the fixer mirrors); everything else is counted in
`scan.skipped` and never fetched. So if we bind this sheet today:

- 1,964 X posts get real, refreshed counters ✅
- ~930 Facebook, Instagram and YouTube links are **silently skipped** ❌

We do have Facebook and Instagram engines (`engine_fb.py`, `engine_ig.py`,
`collect_fb.py`, `collect_ig.py`, `/api/fb/posts`, `/api/ig/posts`) — but they
collect **from accounts and pages**, on a cadence. There is **no per-URL fetch**
for a given Facebook reel or Instagram post anywhere in the codebase; we
grepped for one. A links watchlist for FB/IG is new engine work, not a mapping
change. YouTube does not exist on our side at all.

Note this also trips the hazard you already flagged in your §7:
`portal/util.py: platform_of()` ends `return "x"`, so those 44 YouTube links —
if they ever reach you — will be filed and charted as X.

**Decision required (Part 8, Q1).** Until it is made, the honest statement to
the client is "X is live, other platforms are on the sheet but not yet
measured" — not a dashboard that quietly shows a third of the campaign.

### 1.4 One real bug found in the sheet

Tab **`24/7/25`** parses to **2025-07-24**. Every other tab in that run is
`/26`. It is a typo in the tab name and it will file a day of posts a year
into the past, where no report will ever look for it. Ask the operator to
rename it to `24/7/26`. We will not "fix" it in code — guessing that a year is
wrong is exactly the kind of silent correction that hides the next real one.

---

## Part 2 — The provisioning flow you asked about

Tilak's ask: *add the sheet in the report tool once, have it sync to the
Collector over the API, and let the Collector either create a project or attach
to one I choose; the Collector then makes one watchlist per tab.*

The last half of that already exists. The first half is one new endpoint, and
one deliberate refusal.

### 2.1 Agreed sequence

```
1. Human, in the Collector, once per campaign:
     create project P  ──▶  issue a project-locked key for P
                            (API_KEYS_SCOPED=<key>=<P>)

2. Human, in the report tool, once per client:
     Admin → Clients → Data source — the scraper
        API URL   https://scraper.vedictech.in/api/links?project=P
        API key   <the project-locked key>
        Auth      Authorization: Bearer

3. Report tool  ──▶  GET /api/project?project=P        (handshake, new)
     shows "Connected to project P 'Varanasi' · 53 tabs · 1,964 links"
     THIS is the moment a wrong key or wrong project is caught.

4. Human pastes the Google Sheet URL in the report tool and hits "Send to
   Collector".
     Report tool  ──▶  POST /api/links/sheets  {project: P, sheet: <url>}   (new grant)
     Collector binds the sheet, reads it now, creates one watchlist per tab,
     and re-reads it every 600 s forever after.

5. Daily, 03:30 IST:
     Report tool  ──▶  GET /api/links?project=P&limit=500&offset=0 …
     → post_metrics + post_metric_days   ← the history
```

### 2.2 What we will NOT let the report tool do, and why

**The report tool may not create projects.** `POST /api/projects` stays
cookie-only.

A project is the Collector's top-level grouping and its name is how a human
finds a campaign. If the report tool can create one, then a retry, a double
click, or a client re-added under a slightly different name produces a second
project — and a second project silently splits one campaign's link history in
two, with no error anywhere. The cost of preventing that is a human spending
ten seconds creating the project once per campaign. Take the ten seconds.

The same reasoning, stated as the rule we are adding to `RULEBOOK.md`: *a
machine key may point an existing project at more data; it may not bring a
project into existence.*

**What the report tool may do** is exactly one new write:
`POST /api/links/sheets {project, sheet}` — and only for the project its key is
already locked to. `_require_auth` already enforces that match for scoped keys;
we are extending the check to cover this one POST rather than writing a new
door. Binding the same sheet twice is harmless (`bind_link_sheet` is
idempotent, adds never delete), so a retry is safe.

### 2.3 The zero-code alternative, stated honestly

You can have all of this today with no new endpoint by pasting the sheet URL in
both places instead of one. The whole benefit of step 4 is saving one paste per
campaign, ever. We think it is still worth building — operators wire these up
under time pressure and a sheet pasted into only one of two systems is a
failure that shows up days later as missing data — but if you would rather not
open a write path on a boundary that is currently read-only, say so and we will
drop step 4 and lose nothing else.

---

## Part 3 — What the Collector has built  ✅ DONE 2026-09-10

Our side. **All of it is built, tested and merged** — verified over real HTTP
with the exact headers `portal/scraper.py: fetch_raw()` sends (both
`Authorization: Bearer` and `X-API-Key`, `X-Signature-Name`, `Accept:
application/json`, `User-Agent: VedicReport-portal/1.0`). Tests:
`tests/test_report_contract.py`, registered in `tests/test_all.py`
(1,359 checks, all passing).

Nothing here needs anything from you first. **You can point a source at us
today.**

**3.1 `web.py: _links_json()` — the envelope and the row.** Additive only.

- Add `"items"` beside `"rows"` (same list object, so no extra memory).
  **This is the one that unblocks you.**
- Add `"platform": "x"` to every row. Explicit, so FB/IG can join later
  without your `platform_of()` guessing.
- Add `"group"` beside `"section"` (same value) → your `category_raw`.
- Add `"thumbnail_url"` beside `"thumb"` in each `media[]` element, so
  `media.0.thumbnail_url` resolves for you.
- Guarantee `day` is never `null`: when the tab title is not a date, fall back
  to the IST date the link was first seen and say so in `status_note`
  (`"day inferred: tab 'Tweet LInks' is not a date"`). Per your §2.2 — we will
  not send the scrape date as `day`.

**3.2 Real HTTP status codes on `/api/links`.** Today every answer is
`_send(200, …)`, so an unknown project returns **HTTP 200** with
`{"error": "…"}` — and your `normalize_many` then reports the confusing
*"Expected a JSON array of posts"* instead of the actual problem. We will send
`400` for a missing/ bad `project`, `404` for a project that does not exist,
`409` for a project with no links watchlist yet, each with the human sentence
you asked for in the body so it lands readably in `client_sources.last_error`.
`401` and `403` are already correct and already carry a sentence.

**3.3 Handshake `GET /api/project?project=P`.** New, and added to
`API_KEY_SCOPED_PATHS`. The body follows your §1.4 exactly, with our real
numbers:

```json
{
  "project":   { "id": 7, "name": "Varanasi Campaign", "platform": "x" },
  "watchlist": {
    "sheet_url": "https://docs.google.com/spreadsheets/d/1xTDykt5z6x9oEs0_…/edit",
    "sheet_title": "Varansi Day Wise Data",
    "tab_mode": "dated",
    "tabs": 53, "dated_tabs": 50,
    "links": 1964,
    "last_sheet_read_ms": 1757400000000
  },
  "counters": { "ok": 1897, "pending": 41, "unavailable": 19, "removed": 7, "total": 1964 },
  "skipped_non_x": 930,
  "refresh_in_progress": false,
  "last_refresh_ms": 1757486400000,
  "key": { "name": "report-tool", "scope": "read", "project_locked": true },
  "limits": { "max_limit": 500, "requests_per_minute": 60 }
}
```

Two additions to your spec, both deliberate:

- **`skipped_non_x`** — the count of links in the sheet we are *not* measuring
  (Part 1.3). Put it on the Data source card. An operator looking at
  "1,964 links" has no way to know ~930 more were dropped on the floor, and that
  is precisely the number that will be asked about in a client meeting.
- **`refresh_in_progress`** — as you offered in §3.6. When `true`, skip that
  day's pull rather than snapshot a half-scraped watchlist.

**3.4 A rate limit, declared.** There is none today (grep: no `429` on any API
path). We will set **60 requests/minute per key** and return `429` with
`Retry-After`. At `limit=500` a full walk of 1,964 links is 4 requests, so this
constrains nothing you do and stops a retry loop from costing us a night.

**3.5 The scoped write grant.** `POST /api/links/sheets` becomes callable by a
project-locked key, for its own project only (Part 2.2). New constant
`API_KEY_SCOPED_WRITE_PATHS = {"/api/links/sheets"}`, checked the same way the
read set is.

**3.6 Page timing.** You require each page inside 20 s. `links_snapshot` is a
single indexed join with `LIMIT/OFFSET` on a local SQLite file; 500 rows is
comfortably under a second today. We will measure at 1,964 links and tell you
if that stops being true. If it ever does, we will say so rather than let you
discover it as a timeout.

**Not building:** `tab_mode: fixed`. This sheet is `dated` and `dated` already
works (Part 1.1). We will build `fixed` when a sheet that needs it exists.

---

## Part 4 — What we need from you, in priority order

**P0 — the history. Nothing else matters if this is missing.**

Your Part 4.2 (`post_metric_days`) and Part 4.3 (stop `_fold_scraper`
back-updating every past `sheet_date`). We keep no history; we overwrite
counters in place on every refresh, by design. Your daily pull is the only
thing that can turn our current-state snapshot into a time series. Until that
table exists, pulling daily is strictly worse than pulling once — it rewrites
yesterday's numbers with today's and flattens Growth to zero.

**P0 — `normalize_many` accepting our envelope.** We are adding `items`, which
you already accept, so this is belt-and-braces: adding `links` and `rows` to
your list too means neither of us can break the other with a rename.

**P1 — the field map.** `author_avatar`, `group`, `day`, `status`, `tweet_id`,
`quote_count`, `bookmark_count`, `author_followers`, `lang`, `last_refresh_ms`,
`refresh_count`. Your Part 4.1. We are sending all of them.

**P1 — honour `status`.** Your §3.4. `_fold_scraper` writes `status='ok'`
unconditionally today, so a post that X has deleted, or that the operator
removed from the sheet, renders to the client as a live post with stale
numbers. We send `removed` and `unavailable` accurately and have done since
day one; they are currently being thrown away.

**P1 — drop the `[today-3, today]` window for watchlist sources.** Your §3.3.
This sheet has 50 day tabs going back to 21 July. With
`PORTAL_SYNC_DAYS_BACK = 4`, first import keeps four days and **silently drops
the other 45**.

**P2 — offset paging.** 1,964 links at `limit=500` is 4 pages. Today you make
one GET and ignore `total`, so you would import 500 and lose 1,369 of them
without an error.

**P2 — the handshake button** (your 4.4), **the local-hour scheduler**
(your 4.6), **the stale-`last_ok_at` warning** (your 4.7).

**P3 — surface `quotes` and `bookmarks`** (your 4.5).

---

## Part 5 — Three design points

### 5.1 "Real-time" is bounded by our refresh cadence, not your pull frequency

Tilak asked for real-time data. It is worth being precise about what that can
mean, because pulling more often does not produce it.

We re-fetch each link's counters on the watchlist's own cadence — **24 h by
default**, 12 h / 48 h configurable, **1 h hard floor**
(`links.MIN_REFRESH_S = 3600`) — and overwrite in place. If you pull hourly you
receive the same 24-hour-old numbers twenty-four times. The freshness ceiling
is ours, and lowering it costs X rate-limit budget linearly: 1,964 links at
24 h is 1,964 TweetDetail calls a day; at 6 h it is 7,856.

Our recommendation: **keep your daily 03:30 IST pull.** A day-wise campaign
dashboard with a 2-day client lag (`clients.lag_days = 2`) cannot show a
difference that a fresher number would make. If a specific client genuinely
needs same-day movement, move that one project's watchlist to 12 h and pull
twice a day — do not raise the cadence globally.

### 5.2 Hidden metrics belong to you, not to us

The ask: some metrics visible only to the owner, the rest fetched from the
Collector.

**The Collector will send every metric it has, to every key, always.** Hiding
belongs on your side, and you already have every mechanism for it:
`queries._post_public` decides which columns reach a client, `clients.category_map`
already hides whole categories, and `lag_days` already hides recent days.

If we filtered instead, we would need two API shapes and two key classes, and
the owner's own dashboard could not see the owner's own data without a third.
One API at full fidelity, one place that decides who sees what — that place is
the report tool.

### 5.3 The hand-typed `views` and `Likes` columns must not collide with ours

Columns B and C of every day tab are typed by the operator (Part 1.2). Those
numbers arrive in your system through `publish_from_sheet()`; ours arrive
through `_fold_scraper()`. **They are two different measurements of the same
post and one must never overwrite the other.**

You already have the field that solves this: `post_metrics.metric_source`
(`sheet | page | ocr | mixed | scraper | none`). Please decide the precedence
explicitly and write it down — our suggestion is that a scraped number always
wins for X (we read it from X itself), a hand-typed number always wins for
Facebook/Instagram/YouTube (it is the only number that exists), and the loser
is kept, not discarded, so an operator's typo can be caught by comparison
rather than by someone noticing a chart looks wrong.

This is also the honest answer to "hidden metrics": the operator's typed
numbers are the owner's view, the scraped numbers are the measured view, and a
client should probably see one of them, labelled.

---

## Part 6 — What the client actually gets on day one

Worth setting expectations before the first demo, because one part of this
looks like history and is not.

When we first bind this sheet, we fetch each of the 1,964 posts **once, now**.
A post from the 21 July tab gets **today's** like count, not its 21 July like
count. X does not serve historical counters and neither of us can invent them.

So on switch-on day the client gets:

- **50 days of structure** — every post, correctly filed by day and category ✅
- **one snapshot of totals**, as of today, for all 50 days ✅
- **zero days of growth** — every "change over time" chart is flat ⚠️

Real day-over-day history starts accumulating from the first nightly pull after
your `post_metric_days` table exists, and grows one row per post per day from
there. After a fortnight the Growth view is genuine. Before then it is honest
to show totals and categories and to leave the trend charts out rather than
show a flat line and let someone conclude the campaign is dead.

---

## Part 7 — Open decisions

**Q1 — ANSWERED: build FB/IG link watchlists.** See Part 8 for the design.
YouTube (44 links) stays out of scope; those rows will keep coming back as
`skipped_non_x` in the handshake so nobody forgets they exist.

*(original question kept for the record)*
**Q1 — Facebook, Instagram and YouTube (Part 1.3). The big one.**
40% of the day-tab links are not X and we cannot measure them today. Which:

- **(a) X only, stated plainly.** Ship now. The dashboard shows the X third and
  says so; the sheet's own typed numbers carry the rest. No new engine work.
- **(b) Build FB/IG link watchlists.** Per-URL fetch for a given reel or post —
  real engine work on our side, weeks not days, and both platforms fight it.
  YouTube is separate again and has no code on either side.
- **(c) Hand-typed numbers for non-X, scraped for X**, joined in your
  `post_metrics` and distinguished by `metric_source` (Part 5.3). Cheapest
  route to a complete dashboard, and honest as long as the labelling is.

We recommend **(c) now, (b) later if the client pays for it.**

**Q2 — ANSWERED: skip them.** `Tweet LInks`, `Counter Links` and `3rd Party Posting` will not be bound as watchlists. Please confirm with the operator that nothing lives only there.

*(original question kept for the record)*
**Q2 — the three archive tabs.** `Tweet LInks`, `Counter Links` and
`3rd Party Posting` hold 605 X links with dates as row labels, not tab names.
Skip them (they look like duplicates of the day tabs), or teach the scanner to
read a date out of column A? We suggest **skip**, and confirm with the operator
that nothing lives only there.

**Q3 — ANSWERED: built.** A project-locked key may `POST /api/links/sheets` for its own project. Creating projects stays barred. Pasting the URL in both places still works and remains the fallback.

*(original question kept for the record)*
**Q3 — the sheet-binding write** (Part 2.2/2.3). Build it, or paste the URL
twice? Our recommendation: build it, with project creation still barred.

**Q4 — ANSWERED: 24 h**, switchable per watchlist (12 h / 48 h) from the panel without touching either codebase. Keep your pull daily.

*(original question kept for the record)*
**Q4 — refresh cadence** (Part 5.1). 24 h (free, today), 12 h (2×), or 6 h (4×)?
Our recommendation: **24 h**, revisited per project if a client asks.

**Q5a — the key you will be given is NEW, and is not Watch-Tower's.**
Watch-Tower's key lives in `API_KEYS` and carries the whole read allowlist
across **every** project, plus `/api/fetch`, which spends X rate-limit budget.
Handing that to the report tool would mean: it could read every client's links,
not just its own; neither consumer could be revoked without breaking the other;
and the `403 "this key is locked to project N"` check — the one that catches a
mis-wired client before it files one client's posts under another — could never
fire, because an unscoped key has no project to check against. Your own §1.4
names that as the failure mode that matters most here, and we agree.

So the report tool gets its own entry in `API_KEYS_SCOPED` (`<key>=<project>`).
Rotating or revoking it touches nothing else. This is `RULEBOOK.md` §5, "a
second consumer gets a PROJECT-LOCKED key, never a Watch-Tower key".

**Q5b — `PORTAL_KEY_SECRET`.** Your §19 notes that with it unset, `sync_client`
returns immediately and the scheduler skips the scraper leg entirely — silently.
Please confirm it is set on the server **before** we issue a key, so we are not
both debugging a working integration that never ran.

---

## Appendix — the row we will serve

Additions to `LINKS_CONSUMER_HANDOVER.md` marked `NEW`. Everything else is
already served today.

```json
{
  "platform": "x",                                       // NEW
  "watchlist_id": 15, "watchlist": "7/9/26", "tab": "7/9/26",
  "day": "2026-09-07",
  "section": "National X Influencers",
  "group":   "National X Influencers",                   // NEW (= section)
  "sheet_row": 31, "refresh_every_s": 86400,
  "url": "https://x.com/anujakapurindia/status/2073389948066214090",
  "tweet_id": "2073389948066214090",
  "post_url": "https://x.com/anujakapurindia/status/2073389948066214090",
  "status": "ok", "status_note": null,
  "added_at": "2026-09-08T10:00:00.000000+00:00", "added_via": "sheet",
  "last_refresh_ms": 1788440000000, "refresh_count": 12,
  "fail_streak": 0, "force": false, "fetched": true,
  "created_at": "2026-09-07T08:12:00+00:00",
  "text": "…", "lang": "hi",
  "author_username": "anujakapurindia", "author_display_name": "Anuja Kapur",
  "author_id": "11348282", "author_followers": 87000,
  "author_avatar": "https://pbs.twimg.com/…",
  "like_count": 1200, "retweet_count": 300, "reply_count": 80,
  "quote_count": 25, "view_count": 8658, "bookmark_count": 90,
  "media": [ { "type": "photo",
               "url": "https://pbs.twimg.com/…",
               "thumb": "https://pbs.twimg.com/…",
               "thumbnail_url": "https://pbs.twimg.com/…" } ]   // NEW
}
```

Envelope:

```json
{ "total": 1869, "limit": 500, "offset": 0,
  "rows":  [ … ],
  "items": [ … ] }        // NEW — same list. Read "items".
```

Rules that have not changed and that you can keep relying on: `tweet_id` is a
**string**; counters are **numbers or `null`, never 0** for unknown;
`last_refresh_ms` is your history timestamp, not `collected_ms` (which is
frozen at first sight); order is stable across pages, so offset paging is safe.

---

## Part 8 — Facebook and Instagram: the decision, and how it will work

Q1 is answered: **build link watchlists for Facebook and Instagram** (Part 1.3
— 41% of the day-tab links). YouTube stays out of scope for now.

This section is the design, written down before the code, because the
Instagram half has a failure mode that is expensive and permanent: a banned
account.

### 8.1 The rule that makes Instagram possible: refresh by AUTHOR, never by post

The naive shape — "walk the watchlist, fetch each post" — is what gets an
account banned within minutes. Instagram treats a sequence of individual media
lookups from one session as exactly what it is.

The sheet makes the better shape obvious. Counting the real links:

```
335 distinct Instagram posts   ← the watchlist
339 distinct Facebook reels
 15 distinct Facebook pages    ← behind ALL of those FB reels
```

Fifteen pages. The Instagram side is the same story: a few hyper-local and
third-party accounts posting many reels each. **So the posts are many but the
authors are few.**

Therefore:

1. **Resolve once.** A post's author never changes, so a shortcode → author
   lookup is done exactly once per post, ever, and cached
   (`watchlist_links.author_id`). This is the only per-post network call in
   the whole design.
2. **Refresh by author.** Every day after that, we pull each author's own media
   feed — `engine_ig.py: _user_feed_page()`, which already exists and already
   pages — and harvest counters for **every watched post of theirs in one
   call**. ~20 feed reads a day instead of 335 post reads.
3. **A post whose author's feed no longer carries it** (deleted, archived,
   made private) is marked `unavailable` with a note, exactly as X links are.

That is a ~15× reduction in requests and, more importantly, it is a *shape*
Instagram sees constantly: an account looking at profiles it follows.

### 8.2 The pacing already exists — we are plugging into it, not inventing it

`ig_human.py` is the rhythm layer and its defaults are already conservative. It
answers four questions and the new code will ask all four rather than route
around any of them:

- `active_now(now)` / `session_now(acct)` — is it a plausible hour, and is the
  phone in hand right now? People sleep and use the app in bursts. **Outside
  the plan: zero requests.** A watchlist refresh that is "due" at 04:00 waits.
- `request_gap(rng)` — a log-normal pause between two page reads.
- `source_gap(rng)` — the longer pause when switching from one author to the
  next. This is the one that matters most here: fifteen authors in a row at a
  machine's tick is the signature we are avoiding.
- daily budget + warm-up ramp — a per-account ceiling, and a young account is
  never run at full volume on day one.

**The refresh loop gets no cadence of its own.** It proposes work; `ig_human`
decides when any of it happens. A links watchlist that wants a 24 h refresh and
an account that is out of budget resolve in the account's favour, always.

### 8.3 The backfill is the risky part, and it is paced deliberately

Steady state is cheap (~20 feed reads/day). Getting there is not: 335 posts
need one resolution each, and those are the per-post calls we just said are
dangerous.

So the backfill is spread, not run: a bounded number of resolutions per account
per session, drawn from the same budget as everything else, over as many days
as it takes. On the current pool that is a **two-to-three week** ramp for the
Instagram backlog, and it gets faster as accounts are added.

Two consequences to plan around:

- **`status: "pending"` is normal for weeks, not minutes.** Per your §3.4 a
  `pending` row is shown with `—` for every number. That is the honest display
  and it is why `pending` exists — please make sure it does not read to a
  client as "broken".
- **Every new day tab adds ~24 new links**, of which ~15 are non-X and need
  resolving. That is inside the daily budget, so from switch-on the *new* days
  keep up; it is only the 50-tab backlog that ramps.

### 8.4 Facebook is a smaller problem than it looks, and a different one

Tilak's read is right — Facebook is the less reliable engine. But the sheet
helps twice over: only 15 pages are involved, and a third of the FB links
already carry their page id in the URL (`facebook.com/<page_id>/posts/…`), so
those need no resolution at all. `facebook.com/reel/<id>` links do.

FB will follow the same author-first design, behind the same watchlist, and it
is **staged second** — after Instagram is proven — precisely because the engine
is the weaker one. If it turns out FB cannot hold a page feed reliably, the
fallback is the hand-typed sheet numbers for FB only (Part 5.3), which is a
data-quality decision rather than an engineering one.

### 8.5 What changes in the contract

Nothing, for you. The row already carries `platform`, and every field a
Facebook or Instagram post needs is already in the shape you mapped:

- `platform` becomes `"facebook"` / `"instagram"` on those rows — which is why
  it went in now, explicitly, rather than being inferred (§1.3: your
  `platform_of()` files anything unrecognised as X).
- `tweet_id` stays the join key and carries the **platform's own post id** —
  an IG media pk, an FB post id. It is already a string, and already documented
  as "de-duplicate on it", so no consumer change is needed. If you would prefer
  a neutral name we will add `post_id` beside it; we will not rename it.
- `retweet_count` / `reply_count` keep their names and carry shares / comments.
  Your `mLabel` already relabels per platform in the browser, so nothing on the
  wire needs to change.
- Counters stay **number or `null`, never 0** — and this matters more on
  Instagram, where view counts are simply absent for many post types. `—` is
  the honest cell.

**One thing we will need from you:** your `PLATFORMS` tuple and `platform_of()`
are the only places that decide what a row is. Once we send
`platform: "instagram"`, please read the field instead of parsing the URL.

---

## Part 9 — Reading the sheet without the Sheets API  ✅ DONE 2026-09-10

The Collector now reads a bound sheet through **either** door, chosen per
sheet. Nothing about the contract in Parts 1–3 changes; this is only how the
links get in.

### 9.1 The two doors

| | Service account (`MODE_API`) | **The sheet's own Apps Script** |
|---|---|---|
| Credential | a JSON key in `.env`, reaching every sheet that account was ever given | a token for **one** sheet |
| Setup | cloud project → key → share the sheet as Viewer | paste a script → deploy → one `.env` line |
| Sheet visibility | private | **private** |
| Scales to many sheets | ✅ share and go | one paste per sheet |
| Runs as | the service account | **the sheet's owner** |

The mode is derived from `link_sheets.script_url` rather than stored beside
it — a mode column and a URL column can disagree, and then the row says one
thing and does another. `script_token_env` holds the **name** of the `.env`
variable, never the token itself, exactly as `delivery_targets.secret_env`
does for webhooks.

Binding a sheet through the script:

```
POST /api/links/sheets
{ "project": 7,
  "sheet": "https://docs.google.com/spreadsheets/d/1xTDykt5z6x9…/edit",
  "script_url": "https://script.google.com/macros/s/…/exec",
  "script_token_env": "VARANASI_SHEET_TOKEN" }
```

### 9.2 The route we rejected, and why it matters to you

A published sheet can be read with **no credentials at all**: `/export?format=csv&gid=…`
for the cells, and the tab list scraped out of `/htmlview`. We tested both
against the live sheet and both work today.

We are not using it, and one of the reasons is yours as much as ours: tab
discovery would rest on a regex over Google's minified markup. When Google
changes that shape — undocumented, unannounced — the read returns **zero tabs,
with no error**. A link that is no longer in the sheet is marked `removed`, so
one silent bad read empties every watchlist in the project, and your next pull
faithfully records that the entire campaign was deleted.

That failure mode is the reason for a rule now in `RULEBOOK.md`: **an empty
snapshot is never the honest reading of a failed read.** It is worth holding on
your side too — if a scheduled pull ever returns a fraction of the rows it
returned yesterday, that is likelier to be a broken read than a deleted
campaign, and `post_metric_days` is the thing that makes the difference
recoverable.

### 9.3 One thing that bit us, in case it bites you

An older deployment of the script does not know about the `action` field, so it
reads a *read* request as an *append*: it answers `{ok:true, appended:0}` with
no tab list — and, taking the tab name from `body.tab`, inserts a sheet called
"Sheet1" into the operator's spreadsheet on the way. The fix is a `GET` probe
for the version before any POST, plus refusing a reply that carries no tab
list. Generalised: **a compatible-looking success from an older peer is more
dangerous than an error**, because nothing reports it.

---

## Part 10 — Phase 2, step 1: every link now has a name  ✅ DONE 2026-09-10

Before a Facebook or Instagram post can be fetched it has to be identified.
`links.parse_post_url()` now returns `(platform, reference)` for X, Instagram,
Facebook and YouTube.

Run against **all 3,159 distinct URLs in the live sheet**:

```
  x                            2230
  facebook                      502
  instagram                     355
  youtube                        44
  fb-short (needs a redirect)    28
  UNPARSED                        0
```

Two shapes we would otherwise have dropped, both real and both from your sheet:

- `instagram.com/<author>/reel/<code>` — what the browser's own share sheet
  produces. The first pattern only accepted `/reel/<code>`.
- `facebook.com/share/p/<code>` and `fb.watch/<code>` — **short links**. The
  code in them is not a post id and never joins to a post, so they are followed
  like a `t.co` rather than parsed. Minting a row from one would look tracked
  and be silently dead.

**YouTube is named but not watched.** It parses, so those 44 links can be
counted and reported as unmeasured, but it is deliberately outside
`WATCHED_PLATFORMS`. A link filed under a platform we do not collect is honest;
one guessed as X is not — and guessing as X is precisely what your
`platform_of()` does with a row that does not state its platform. Every row we
send states it.

### What is left in Phase 2

- **The watchlist row for a non-X post.** `watchlist_links` is keyed
  `(watchlist_id, tweet_id INTEGER)` and is `WITHOUT ROWID`, so a string post id
  needs either a widened key or a second table. The pattern we will follow is
  `collection_posts` + `web._resolve_pins`: pins keyed `(platform, post_id)` in
  `results.db`, and **three lookups merged in Python**, because X, Instagram and
  Facebook posts live in three separate SQLite files and nothing in this
  codebase joins across them.
- **Resolution**, once per post, cached forever: an Instagram shortcode → media
  pk, a Facebook reel id → its page.
- **Refresh by author** (Part 8.1), paced by `ig_human`.

None of that changes anything in Parts 1–3. When those rows appear they will
arrive through the same `/api/links`, in the same shape, with `platform` set to
`instagram` or `facebook` and counters `null` until first fetched.
