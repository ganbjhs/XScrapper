# Rulebook

The rules every change must respect. Each one was paid for in a bug, a ban, or
a confused operator — breaking one re-buys that lesson. `BLUEPRINT.md` is the
map (what each file does); this is the law (what you may not do).

## 1. Three prime directives

1. **The browser is only for logging in and for rendering.** Collection is
   HTTP (X via twscrape, Instagram via instagrapi). A headless browser is used
   only where there is no other way in: the one-time X sign-in, and Facebook's
   rendered page. Never drive a browser to "click through" a feed — it is slow,
   fragile, and a ban magnet.
2. **Extract here; analyse in Watch-Tower.** This tool collects, normalizes,
   stores, and delivers. Sentiment, scoring, AI, entity work — none of it lives
   here. The Collector's job is to make clean, complete, timely data available;
   the intelligence is Watch-Tower's job. Do not grow an analysis layer here.
3. **Media travels as URLs, never bytes.** Photos and videos are stored as
   their CDN URLs (with a thumbnail URL). We never download media. Watch-Tower
   and viewers fetch it from the platform directly. This keeps storage tiny and
   bandwidth honest across all three platforms.

## 2. The one post shape (the platform contract)

Every platform's engine normalizes to the SAME record before anything
downstream sees it:

```
{ platform, tweet_id, url, text, created_at, collected_at,
  author_username, author_display_name,
  media: [{ type, url, thumb }], metrics }
```

New platform = new engine + collector + store that emit this shape (see X / IG /
FB as the three worked examples). Everything downstream — projects, the feed,
delivery, the UI card — already generalizes over it, so a fourth platform
touches no downstream code. `tweet_id` is always a **string** (JS loses integer
precision past 2^53; snowflake ids are well past it).

## 3. Collection rules

- **Watermark, newest-first, stop at the first already-seen post.** Every poll
  walks newest→older and stops the moment it reaches something already stored
  (X uses a composite `(collected_ms, tweet_id)` cursor; FB stops on posted
  time with id-dedup as backstop). Never re-walk a whole feed.
- **Going out to the platform is never automatic on a keystroke.** X fetches
  spend a shared rate-limit budget, so they are explicit, serialized behind one
  lock, guard-checked, and report what they cost. The dashboard *reads* the DB
  for free; *fetching* is a deliberate act (the Refresh / Fetch-now buttons, or
  the watcher's own timer).
- **Refuse, don't silently clamp.** A request for more pages than the cap is
  rejected with the reason, not quietly shrunk — a silent clamp still spends
  budget the caller never agreed to.
- **Per-source cadence is the source's own.** X watchlists and FB pages each
  carry their own check interval; a scheduler collects only what is *due*. Idle
  ticks cost nothing (no browser opened when nothing is due).

## 4. Delivery rules

- **Delivery is a durable cursor, at-least-once.** The webhook sender walks the
  same composite cursor the feed does, so the dashboard and the sender can never
  disagree about what "new" or "behind" means. A failed delivery leaves the
  cursor untouched — nothing is skipped; a post may repeat, never vanish.
- **Per-project scoping is real, not cosmetic.** A project's numbers, feed, and
  delivery describe THAT project's streams only. Webhooks are HMAC-signed.
- **Facebook/Instagram delivery is pull.** Watch-Tower pulls IG/FB via
  `/api/fb/posts` and `/api/instagram/posts`; X is pushed via webhook. Both are
  the same normalized shape.

## 5. Security rules

- **Secrets live in `.env` / `config.toml` and are git-ignored. Never commit a
  cookie, password, session, or token.** `fb_state.json`, `*.db`, `profiles/`
  are ignored for the same reason.
- **The dashboard refuses to bind anywhere but localhost unless `DASH_USER` +
  `DASH_PASSWORD` are set** (and not placeholders, min length enforced). Making
  the unsafe state impossible beats documenting it.
- **An API key may only read data and spend fetch budget** — never add an
  account, open a sign-in browser, or change what a human sees. Enforced by an
  allowlist server-side, not by the caller being polite. Keys are compared in
  constant time.
- **One account, one steady IP.** Hopping IPs, or hammering a fresh account, is
  what gets accounts checkpointed. FB runs from the server IP; IG uses the
  residential pool; don't cross them.

## 6. Per-platform hard rules

**X (Twitter).** List watchlists poll ~10× faster than query watchlists (500 vs
50 requests / 15 min) — big permanent watchlists belong on real X Lists. One
long-lived asyncio loop for the whole process (twscrape's module lock binds to
the first loop that awaits it). Verification/category come from the stored raw
tweet JSON at read time, not columns.

**Instagram.** Fights automation hard: checkpoints only a human clears (then
import a fresh `sessionid` from that browser), `LoginRequired` on fingerprint
drift (pin the device file), `PleaseWaitFewMinutes` punishes retries — back off
hours. `user_medias` wants the numeric pk; validate sessions against the *feed*
endpoint. The streamed-browser IG login is dead; cookie/password paths work.

**Facebook.**
- **Use a DESKTOP user-agent. Never switch it to mobile.** A mobile UA makes
  Facebook serve the "WebLite/Bloks" shell — post text and images render, but
  every post is a tap-to-open JS button with NO permalink and NO
  `role="article"`, which is impossible to extract. The desktop site renders
  real `role="article"` posts with real permalinks. There is an
  `mbasic.facebook.com` fallback when the desktop render yields nothing.
- **Hold the session, don't replay borrowed cookies.** Log in with
  `FB_EMAIL`/`FB_PASSWORD` so the browser owns its own `datr`, and persist the
  whole session to `fb_state.json` for reuse. Replaying `xs` without a matching
  `datr` gets logged out in a request or two. Delete `fb_state.json` to force a
  fresh login (e.g. after clearing a checkpoint in a normal browser).
- **Server IP, no proxy, hard byte cap.** FB uses the VPS's own bandwidth (not
  the residential pool); every response is metered in `fb_meter.db` and refused
  past `FB_MONTHLY_CAP_GB`, so it can never run away.
- **When 0 posts parse, the "Fetch now" log prints `all_links=` /
  `containers=`** — the real DOM shape to retune the extractor against. That is
  the diagnostic path; use it before guessing.

## 7. Change rules

- **Migrations are additive and self-applying.** Add a column via a guarded
  `ALTER TABLE` on `open()` so an existing DB upgrades in place; never require a
  wipe. (See `store_fb._MIGRATIONS` and the X store's `watched`/interval
  columns as the pattern.)
- **`python3 tests/test_all.py` stays green, offline, and grows a test for the
  new behavior.** The suite is the contract; it needs no accounts and spends no
  budget. Run it as a script, not under pytest.
- **The dashboard ships built.** The VPS runs no Node — `frontend/dist/` is
  committed on purpose. After any UI change: `cd frontend && npm run build` and
  commit `dist/`. New capability = store method → thin `web.py` validator →
  small view in `frontend/src/views/`, with honest loading/empty/error states
  and dark mode from the same tokens.
- **Keep the pinned scraper versions and the `doctor`/`guard` asserts.** They
  turn "the platform changed under us" into a loud failure instead of silent
  data loss.
