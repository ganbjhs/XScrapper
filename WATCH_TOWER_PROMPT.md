# Prompt for the Watch-Tower agent

*Paste the fenced block below to the Watch-Tower side verbatim. It is written
to be read by their coding agent, not by a person.*

Context for us, not for them: Watch-Tower reported being "blocked". It was not.
Its agent probed six endpoint names that have never existed, got 403 from the
API-key allowlist, and concluded it was locked out. Meanwhile its real defect —
a hardcoded 45-handle list standing in for a 91-member watchlist — had been
costing it half the News feed for weeks. Both are covered below, along with the
widened key access (23 GET paths, up from 6).

Supersedes the ad-hoc note; if an earlier version was already sent, this one
replaces it — the earlier one said only six endpoints exist, which is now wrong.

---

```
Our upstream collector ("xscraper") has just widened your API key's access, and
your ingestion has a long-standing correctness bug. Both are covered below.
This supersedes any earlier integration note.

## 1. You are not blocked — and access has just been widened

You reported being blocked. You were not. On 22 Aug 12:06 UTC you probed:

  12:06:10  /api/streams /api/status /api/authors /api/handles /api/sources
            /api/stats                                              -> 401
  12:06:50  /api/authors /api/handles /api/sources /api/stats
            /api/accounts /api/list                                 -> 403

The 401s were sent without a valid key — send it on every request:

      Authorization: Bearer <your key>          (or  X-API-Key: <your key>)

The 403s were a valid key hitting paths that DO NOT EXIST and never have:
/api/authors, /api/handles, /api/sources, /api/stats, /api/accounts,
/api/list, /docs, /openapi.json. No ban, no rate limit, no revoked key —
39,834 of your /api/tweets calls returned 200, continuously, including today.

Your key may now GET all of these (23, up from 6):

  Posts     /api/tweets  /api/export  /api/ig/posts  /api/fb/posts
  Structure /api/projects  /api/watchlists  /api/streams
            /api/streams/assignments  /api/identities  /api/watchlist/xmembers
  Curated   /api/collections  /api/collections/items  /api/collections/export
  Telemetry /api/status  /api/guard  /api/metrics  /api/alerts  /api/activity
            /api/activity/logs  /api/delivery  /api/ig/status  /api/fb/status
  Live      /api/live   (Server-Sent Events)

POST is allowed on /api/fetch ONLY. Everything else is 403 by design.

Do not probe for other endpoints. If you get a 403, read the response body —
it now returns `allowed_get` and `allowed_post` arrays, and distinguishes
"this path is closed" from "this path is readable with GET, you used POST".
That body is the API surface; there is no other discovery document.

## 2. The bug: you enumerate a stale hardcoded handle list

You poll one handle at a time:

      GET /api/tweets?author=<handle>&limit=40

against 45 hardcoded handles, ~68x/day each. That list is wrong three ways:

  a) COVERAGE. The News watchlist has 91 members (87 actively posting). You
     request 45, and only 31 of those are even in the News list.
     56 collected handles are NEVER requested. Over the last 7 days,
     33,876 of 67,655 posts — 50.1% — never reached you.
     Largest omissions (7d counts): zeerajasthan_ 2751, TNNavbharat 2586,
     EconomicTimes 2442, htTweets 1549, IndiaNewsUP_UK 1344, PrimeNewsInd
     1255, DeccanChronicle 1174, ZeeBiharNews 1145, the_hindu 1120,
     ndtvindia 1094, ETNOWlive 1016, plus timesofindia, DainikBhaskar,
     livemint, ZeeBusiness, DDNewslive, News18Bihar and ~40 more.

  b) PREFIX MATCH. `author=` is `LIKE 'value%'`, not equality. author=ani
     also returns ani_digital, ANINewsUP, ANI_MP_CG_RJ, AnilPatil397538,
     anirajani. author=republic also returns every Republic_Bharat post. You
     are ingesting accounts you never asked for and double-counting
     republic/republic_bharat.

  c) NO SCOPE. The author filter queries the entire corpus across all
     projects. 14 of your 45 handles (narendramodi, amitshah, myogiadityanath,
     nsitharaman, dev_fadnavis, pushkardhami, suvenduwb, gupta_rekha,
     nayabsainibjp, joshipralhad, samrat4bjp, bhajanlalbjp, ibc24news,
     vistaarnews) are not in the News list at all — they belong to other
     projects and are leaking into your News ingest.

Do NOT fix this by extending the handle list. It will drift again on the next
membership change. Delete the list entirely.

## 3. Required change: one cursor-driven pull per project

      GET /api/tweets?project=8&since_collected_ms=<cursor>&limit=500
      Authorization: Bearer <your key>

News is project_id 8. Discover the rest yourself with GET /api/projects —
it returns all 12 with ids and names. GET /api/watchlists?project=8 returns
the watchlist, its stream labels and current post totals.

- `project=8` returns everything every stream in that project collected,
  deduplicated across streams, and tracks membership automatically: a member
  added on X flows through with zero change on your side. This is what the
  internal dashboard uses. It currently matches 105,334 posts.
- `since_collected_ms` is the GAPLESS cursor. Persist the largest
  `collected_ms` you have COMMITTED, and pass it back next call. While
  cursoring, rows come back OLDEST-FIRST so the last row you got is a
  resumable position. Start a cold load at 0.
- Use `since_collected_ms`, NOT `since_id`. `since_id` filters on tweet id,
  and X indexes some tweets late — a tweet collected now can carry an older id
  than one collected a minute ago, and an id cursor steps straight over those.
  It silently drops posts. The upstream source comments say so explicitly.
- `limit` is clamped to 500. Page until a response returns fewer rows than the
  limit, then sleep and resume from the new cursor.
- Do not send `sort` while cursoring; engagement sorts are ignored, because a
  cursor position only means something on a stable time order.

`tweet_id` is a STRING — it exceeds JavaScript's safe-integer range and
parsing it as a Number silently corrupts it. De-duplicate on `tweet_id`;
delivery is at-least-once by design.

Two endpoints you might reach for, and should not rely on:
- /api/watchlist/xmembers currently returns {"members":[],"count":0} — that
  cache has never been populated. It is NOT a membership source. You do not
  need membership at all; project scoping replaces it.
- /api/live (SSE) starts from NOW and never replays backlog, so it cannot
  recover a gap. Optional for low latency ONLY, layered on top of the cursor
  pull — never as the source of truth.

## 4. Stop abandoning /api/fetch requests

nginx recorded 5,064 requests from your IP where you closed the connection
before we answered (status 499), essentially all POST /api/fetch, concentrated
15-20 Aug. /api/fetch triggers a LIVE fetch against X and spends our real X
rate-limit budget — every abandoned call costs us quota and returns you
nothing.

Raise your client timeout well above a live fetch's duration, or stop calling
it. For ingesting already-collected posts you do not need it at all:
/api/tweets reads local storage and is fast. Steady-state ingestion should
call /api/fetch zero times.

## 5. Verify

1. GET /api/status with the Bearer header -> 200.
2. GET /api/projects -> confirm you see project_id 8 "News".
3. Cold load project=8 from since_collected_ms=0, paging to the end. Count
   distinct author_username: expect ~87, not 45. Confirm zeerajasthan_,
   TNNavbharat, EconomicTimes, htTweets and the_hindu are present.
4. Confirm none of the 14 political handles in 2(c) appear in project=8.
5. Steady state: poll on the stored cursor. Your 24h count should now match
   ours instead of running ~50% low.
6. Confirm your /api/fetch call count is zero and your 499 count is zero.

Report back the distinct-author count and 24h post count after the change so
we can reconcile against our side.
```

---

## Open items on our side

- `xlist_members` holds 0 rows, so `/api/watchlist/xmembers` answers empty for
  every list. It is filled by `POST /api/watchlist/xmembers/refresh`
  (dashboard-only), which has apparently never been run for the News list. The
  project-scoped pull does not need it, but our own UI cannot show membership
  either until it is refreshed.
- `/api/delivery` is now key-readable and returns raw webhook URLs, including
  the Google Apps Script `/exec` URLs, which are effectively capability URLs.
  No secret values leak (`secret_env` is a variable NAME, plus a `secret_ready`
  boolean). Mask the URL to its host if that is not wanted.
- `author=` matching is a prefix `LIKE`, which is a correctness bug for every
  caller, not just this one. Left as-is here because changing it would break
  any consumer relying on the current behaviour; worth an explicit decision.
