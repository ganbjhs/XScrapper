# Facebook Collection — Plan

## FINDINGS (2026-08-08) — logged-out is dead; going logged-in

Tested real fetches through the Webshare residential proxy against
`/narendramodi`:

- `mbasic.facebook.com` → **"Content not found"** (mbasic is retired).
- `m.facebook.com` / `www.facebook.com` → HTTP 200 but the body is only
  Facebook's **JavaScript app shell** (WebLite/Bloks bootstrap +
  "This browser is not supported"). Marked `LOGGED_OUT_EXCLUDE_PERMALINK…`.
  **No post content in the HTML** — posts load after JS over a WebSocket.

So static logged-out scraping cannot collect posts. **Decision: logged-in
accounts** (Tilak, 2026-08-08). The account grants access; the transport is
still to be settled by one test (below). Everything about the standalone-then-
merge shape and the bandwidth cap (`proxy_pool.py`) stays.

### Revised approach — logged-in

1. **Onboard a BURNER Facebook account** (never a personal one). Log in once
   in a normal browser, capture its session cookies (`c_user`, `xs`) — the
   same "import a session" path Instagram uses. We never store the password.
2. **One transport test on the server:** fetch a Page while sending those
   cookies. Two possible outcomes decide the build:
   - the logged-in mobile page returns real post HTML/JSON → light,
     ~tens of KB/fetch, we parse it directly (best case);
   - it still returns a JS shell → we drive it with a headless browser
     carrying the session (heavier; the bandwidth cap earns its keep).
3. Build `engine_fb` around whichever transport wins, behind `proxy_pool`
   (rotation + monthly byte cap), watermark polling, same record shape.
4. Profiles → groups → hashtag search, each behind an honest capability check.
5. Standalone review, then the one-flag merge.

### Honest risks for the logged-in route

- **Accounts get checkpointed/banned** — worse than Instagram. Burner
  accounts only; one steady residential IP per account; go slow. A locked
  account is expected attrition, not a bug.
- **Session cookies are IP/device-sensitive.** A cookie captured on one IP
  and used from the proxy may trip a checkpoint. We may need to log in from
  (or pin) an IP close to where it's used.
- Facebook changes the GraphQL/Bloks surface often — expect maintenance,
  same as the pinned-library discipline elsewhere.

---


How we add Facebook without risking the working X/Instagram system: build it
**standalone and provable first**, shaped to drop into the existing seam, then
merge when it's trusted. Same layering that let Instagram be added by touching
only two layers (BLUEPRINT §9).

---

## 1. The honest feasibility picture (read first)

Facebook is the hardest of the three, and unlike X (`twscrape`) and Instagram
(`instagrapi`) there is **no reliable free library** — the FB equivalents die
constantly. So we don't lean on one; we fetch **`mbasic.facebook.com`** — the
stripped, no-JavaScript mobile site — and parse its HTML. It's plain, light,
and the most stable surface Facebook has. Logged-out, using your residential
IPs to spread requests.

Tiered by how well each target you asked for actually works:

| Target | Logged-out feasibility | Notes |
|---|---|---|
| **Public Pages** | ✅ Solid | The newsroom core. A Page's `/pagename` timeline renders on mbasic without login. This is where we start and where 90% of value is. |
| **Public profiles** | 🟡 Partial | Public individual profiles often render on mbasic, but Facebook shows less without login and rate-limits harder. Treat as best-effort. |
| **Public Groups** | 🟡 Limited | Many public groups render logged-out; many now require login even to read. Where it works, it works; where FB demands login, we say so, not silently fail. |
| **Keyword / hashtag search** | 🔴 Fragile | FB heavily restricts logged-out search. Hashtag pages (`/hashtag/word`) sometimes render; keyword *search* usually needs login and breaks often. Lowest priority — build last, promise least. |

**No `filter:` operators.** Facebook has nothing like X's advanced search, so
the checkbox filter panel we built for X won't map. Filtering FB content (by
media, by recency) happens **our side, after fetch**, on the parsed fields.

## 2. The bandwidth reality — the real design constraint

Your pool is **80M IPs but 1 GB bandwidth/month**. The IPs are plentiful; the
**bandwidth is the ceiling**, and it drives every design choice:

- An `mbasic` Page timeline page is small — roughly **30–80 KB**. The full
  `m.facebook.com` (or the real site) is **1–3 MB**. So: **mbasic only** —
  full site would burn the whole month in a few hundred fetches.
- Budget math: 1 GB ≈ **13,000–30,000 mbasic fetches/month** ≈ **~18–40 an
  hour**. That is enough to watch **a few hundred Pages on a gentle cycle**,
  not to crawl all of Facebook.
- Therefore the same **watermark rule** we use everywhere applies doubly:
  one fetch per poll, stop at the first already-seen post. And we **never
  download the media file** — we store Facebook's image/video URL and let
  Watch-Tower (or the viewer) fetch it directly, exactly as we do for X. Media
  bytes would dwarf the HTML budget.
- The dashboard gets a **bandwidth meter** for the FB source so you watch the
  1 GB drain in real time and it throttles before it runs dry, rather than
  going dark mid-month.

Rotate a fresh residential IP per fetch (or per Page) so no single IP shows a
pattern — this is exactly what the pool is good for, and it's what keeps
logged-out scraping alive.

## 3. Shape — a standalone trio that mirrors the others

Three new files, built and tested on their own, matching the existing
platform pattern so the merge is mechanical:

```
engine_fb.py     fetch mbasic over a rotating residential proxy; parse HTML
                 into the SAME record shape X/IG use (id, url, author, text,
                 created_at, media[], metrics). Yields FBPage objects
                 duck-compatible with engine.py's Page.
collect_fb.py    the poll loop — watermark stop, dedup, adaptive interval,
                 per-source bandwidth accounting. Twin of collect_ig.py.
store_fb.py      SQLite store (fb_results.db): posts + sources. to_api()
                 maps to the stable external JSON shape (same as IG/X).
proxy_pool.py    the residential-IP rotator + a hard monthly bandwidth cap
                 (counts bytes per response; refuses when the budget's spent).
```

A **source** on Facebook mirrors a watchlist member: `page:pagename`,
`profile:username`, `group:<id>`, `hashtag:<word>`. Your point about groups
capping at 30 and wanting X-List-style multi-profile storage → on our side a
**watchlist just holds many FB sources**, no platform limit; we poll each. So
"store many at once" is solved by our watchlist, independent of Facebook's own
list limits.

## 4. Build order (each step independently testable, offline where possible)

1. **`engine_fb` against saved HTML.** Fetch a dozen real public Page pages
   once, save the HTML, write the parser against those fixtures — so parsing
   is testable with **zero network** (same discipline as the X/IG test suite).
   Deliverable: HTML → clean records, proven on fixtures.
2. **`proxy_pool` + live fetch.** Wire the residential proxy, fetch one real
   Page live, confirm the parser holds on fresh HTML, measure bytes/fetch to
   validate the bandwidth math.
3. **`collect_fb` loop.** Watermark polling over a handful of Pages, into
   `fb_results.db`, with the bandwidth cap enforced. Run it a day; confirm it
   stays under budget and collects steadily.
4. **Profiles, then groups, then hashtag search** — in that order, each behind
   an honest capability check that reports "needs login / not available
   logged-out" instead of failing silently.
5. **Standalone dashboard page** (or a CLI) to eyeball collected FB posts
   before any merge.

## 5. The merge (only after step 4 is trusted)

Because the trio speaks the same record shape and the organizing layer
(projects, watchlists, delivery, alerts) is already platform-neutral, merging
is small and reversible:

- Watchlists gain a **Facebook source type** (the Source dropdown already
  lists "Facebook — soon"; this flips it on).
- The live feed already renders any platform; FB posts appear with an **FB
  badge**, same as X/IG.
- Delivery, collections, alerts work unchanged — they operate on stored posts,
  which now include Facebook's.
- One unified `metrics`/feed read gains the FB series.

Nothing about X or Instagram changes. If Facebook proves too flaky in
production, it's disabled by one flag with zero effect on the rest.

## 6. Honest risks (so nobody's surprised later)

- **Facebook fights logged-out scraping** and changes mbasic's HTML without
  notice — expect periodic parser maintenance. Pinning fixtures + a parser
  self-test makes breakage loud, not silent (same as the pinned-library
  asserts elsewhere).
- **mbasic could be retired** by Facebook someday. If it is, logged-out
  Pages get much harder and the honest answer may become "Pages via the
  official Graph API for Pages you're authorized on" — a different, paid,
  approval-gated path. We design so that swap touches only `engine_fb`.
- **Groups / keyword search may simply require login.** We won't pretend
  otherwise; those tiers degrade to a clear "not available logged-out"
  rather than half-working.
- **Legality/ToS:** this collects **public** content, like the rest of the
  tool. Groups/profiles that aren't public are out of scope by design — we
  only fetch what a logged-out browser can see.

## 7. What I need from you to start step 1

- **Proxy pool access details** — endpoint, auth, and how a fresh IP is
  requested (rotating gateway URL, or username-per-IP). This lives in `.env`,
  never in git.
- **5–10 real Facebook Page names** you actually want (the Bhajanlal / cabinet
  / party Pages), so the parser is built against real newsroom targets, not
  toy ones.

Give me those two and I build step 1 (the fixture-based parser) in a separate
module — your live system untouched the whole time.
