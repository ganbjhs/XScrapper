# Facebook — what we tried, what works, what doesn't

The hard-won map of the Facebook integration. Facebook has no free API and
fights automation harder than any platform here, so this took many dead ends.
**Read this before changing anything in `engine_fb.py` — most "obvious" ideas
here have already been tried and failed.** Companion to `RULEBOOK.md` §6.

---

## The setup that WORKS (and why)

| Piece | What we do | Why |
|---|---|---|
| Browser identity | **Desktop** Chrome user-agent | A mobile UA gets the useless "WebLite/Bloks" shell (see fails). Desktop gets the real site. |
| Session | **Cookies copied from a real logged-in browser** (`c_user` + `xs` + `datr`), saved to `fb_state.json` and reused | The browser already holds a session Facebook trusts (right `datr`, checkpoint cleared). Moving all three cookies moves that trust. |
| IP | The **server's own IP**, no proxy | FB uses the 4 TB VPS bandwidth, not the 1 GB residential pool. One steady IP = fewer checkpoints. |
| What we read | The account's **Favorites feed** (or home feed), NOT page profiles | A real feed infinite-scrolls and fires the data calls; a page profile does not. |
| Data source | **Captured GraphQL responses** (`/graphql` XHR bodies), matched by `__typename == "Story"` | When logged in, FB loads the feed over background graphql, not in the page HTML. This is where post_id, exact time, profile picture, and reaction counts live. Layout-proof: the JSON schema is stable while CSS classes rotate weekly. |
| Fallbacks | on-page `<script type="application/json">` → `role="article"` DOM → `mbasic.facebook.com` | So a Facebook change degrades us down the ladder, never to zero. |
| Anti-detection | Human-like scroll (real wheel events, varied pauses, occasional scroll-up), stealth flags (`navigator.webdriver` erased, `--disable-blink-features=AutomationControlled`), jittered cadence | Makes the logged-in session survive far longer. |
| Cadence | One **Favorites** read per hour covers ALL pages (jittered). `FB_MODE=favorites`, `FB_FAV_INTERVAL_S` | One feed read ≈ a person opening Facebook once. No need for per-page hits. |
| Dedup | On `post_id` AND a content signature (`page + posted-day + caption`) | Same post via different paths/ids can't double; different-day same-caption posts are kept. |
| Safety | Monthly byte cap (`fb_meter.db`), single-flight lock, "only auto-save when confirmed on the real Favorites feed" | Can't run away on bandwidth, can't launch two browsers, can't pull in non-favorited accounts. |

**The winning realization:** read a real *feed* (not page profiles), capture the
*graphql* (not the DOM), from a *desktop* browser holding a *browser-exported*
session on *one steady IP*.

---

## What we TRIED and it FAILED (do not repeat)

1. **Logged-out scraping.** → Facebook shows a logged-out scraper almost nothing;
   pages hit a login wall fast. Must be logged in.
2. **Mobile user-agent.** → Facebook serves the "WebLite/Bloks" shell: post text
   and images render, but every post is a tap-to-open JS button with **no
   permalink, no `role="article"`, and no post JSON**. Nothing to extract. This
   cost a whole session. **Never switch back to mobile UA.**
3. **Replaying `c_user` + `xs` cookies WITHOUT `datr`.** → Facebook ties the
   session to the browser's `datr` fingerprint; a session replayed on a browser
   whose `datr` differs is treated as hijacked and logged out within a request
   or two. Always include `datr`.
4. **Automated password login (`FB_EMAIL`/`FB_PASSWORD`) on a flagged/new
   account.** → Trips a checkpoint / "Was this you?" / 2FA and fails. Works only
   on a rested, trusted account. The reliable fix is copying cookies from a
   browser where a human already cleared the checkpoint.
5. **Residential proxy for Facebook.** → Abandoned. Full page renders are
   20–120 MB; the residential pool is only ~1 GB, and the server already has
   4 TB. Server IP is the right call for FB. (Residential stays for Instagram.)
6. **IP hopping** (UK proxy → India proxy → server IP within an hour). →
   Checkpointed the account immediately. **One account, one steady IP.**
7. **Loading the favorites URL directly** (`facebook.com/?filter=favorites`). →
   A cold URL load renders an empty page; Facebook only builds that feed through
   in-app navigation. Must open home and **click** the "Favourites" sidebar link.
8. **Parsing the on-page `<script type="application/json">` blobs while logged
   in.** → `json_stories=0`. Logged-in Facebook does NOT embed the feed in the
   page; it fetches it over background `/graphql` XHR. Capture the responses
   instead. (The embedded-JSON path only helps logged-*out* public pages.)
9. **`window.scrollTo` / `window.scrollBy` to trigger "load more".** → Does
   nothing on a page profile: Facebook's feed scrolls an inner container, not
   the window, so no load-more fired and no graphql appeared (byte count was
   byte-for-byte identical across runs — the tell). Use **real mouse-wheel
   events** at the cursor instead.
10. **Visiting each page profile** (`facebook.com/<page>`). → Server-renders
    only the newest ~4 posts, fires no graphql, so it's DOM-only and shallow.
    Fine as a last resort, but the Favorites feed is the real answer.
11. **Running the diagnostic as a SECOND `page.evaluate` after extraction.** →
    It silently failed / didn't run, so we saw "0 posts" with no explanation.
    Fold the diagnostic into the SAME extraction call so it can't be skipped.
12. **Adding the Favorites/feed URL as a "Facebook page" in the dashboard.** →
    The page-handle box stripped the URL's punctuation into a garbage handle
    (`httpswww.facebook.comfilterfavoritesskh_chr`) and fetched a page that
    doesn't exist. The box now rejects pasted URLs. Favorites is the button, not
    a page.
13. **Auto-saving every post from the home feed.** → Pulled in friends and
    everyone-you-follow, not just favorites. Now we auto-save only when we
    *confirm* we reached the real Favorites feed; otherwise only already-tracked
    pages are saved.

---

## Bugs we hit in our OWN code (fixed — don't reintroduce)

- **Handle case mismatch made duplicates.** The per-page path keyed on the
  user's label (`NatGeo`), the feed path on the actor username (`natgeo`) —
  different `post_id`, saved twice. Handles are now lower-cased everywhere.
- **The fetch lock leaked.** On an HTTP timeout the lock was released while the
  headless browser was still running, so a second click launched a second
  browser on the same session. The lock now releases only when the work truly
  finishes.
- **Re-adding a paused page un-paused it** (`ON CONFLICT ... SET enabled`). Now
  a re-add keeps the existing enabled/paused state.
- **Same caption on different days was dropped as a duplicate.** The content
  signature now includes the posted day.
- **Byte meter undercounted graphql** (chunked responses carry no
  `content-length`). Now the captured graphql body length is metered.
- **The 24 h feed filter hid just-collected FB posts** whose original post time
  was older. Now a post shows if EITHER its post time OR its collection time is
  in the window.

---

## Rules / gotchas to remember

- **The Favorites feed is Facebook's version of a Twitter List** — but it's an
  account feature, so it only works because we're logged in. The account must
  have the pages added to Favorites (max 30). Whatever's favorited is what we
  can collect.
- **When the session dies (`logged_in=False`)**, the recovery is always the
  same: in a browser logged into the account, F12 → Application → Cookies →
  copy `c_user`/`xs`/`datr` → put them in `.env` → `rm fb_state.json` → restart.
- **Facebook mixes friends into the home feed.** Keep the account's Favorites to
  only the pages you actually track, and Pause/Remove any stray auto-added pages
  in the dashboard.
- **Don't spam "Fetch now."** Every extra automated hit on a flagged account
  risks another checkpoint. Let the hourly service do the work.

---

## Known open limitation (not yet solved)

- **Datacenter-IP session durability.** Even with a browser-exported session,
  Facebook *may* invalidate it after a while because the server is on a
  datacenter IP and the cookies were minted on a home IP. If `logged_in` keeps
  flipping to False quickly, the only permanent cures are: (a) a residential
  proxy dedicated to Facebook, or (b) accepting the shallow per-page DOM
  collection. This is a platform limit, not a code bug — don't burn time trying
  to "fix" it in the extractor.
- **The "click Favourites" step can still fail** on some layouts; when it does,
  the code safely falls back to the home feed and saves only already-tracked
  pages. If it fails consistently, capture what the sidebar link looks like
  before rewriting the selector blindly.
