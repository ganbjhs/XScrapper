"""
engine_fb.py — fetch a Facebook page's newest posts via ONE warm headless
browser, logged in with a burner session.

Design settled (see RULEBOOK.md §6 Facebook, and FACEBOOK_RUNBOOK.md):
  * runs from the SERVER's own IP by default (FB_USE_PROXY=0) — the VPS has
    4 TB, so Facebook does not touch the small residential pool;
  * a mobile user-agent — the mobile layout is lighter and its posts sit in
    clean role="article" blocks (this is the variant that actually parsed);
  * ONE browser/context reused across pages so Facebook's app caches and later
    pages are cheaper;
  * image/video/font bytes blocked (URLs kept), and a hard monthly byte cap so
    it can never run away.

Session + settings come from the environment, never git:
  FB_C_USER, FB_XS            the burner account cookies
  FB_USE_PROXY=0|1            0 = server IP (default); 1 = Webshare residential
  WEBSHARE_USER/PASS/GATEWAY  used only when FB_USE_PROXY=1
  FB_MONTHLY_CAP_GB=200       the runaway guard (bytes counted in fb_meter.db)

Runs on the SERVER (needs Playwright chromium). It cannot reach Facebook from
the build sandbox, so live behaviour is confirmed by `collect_fb.py run` there.
The extractor is one function (`_EXTRACT_JS`) — the piece most likely to need a
small tune after the first real run; everything else is stable.
"""

import json
import os
import random
import re
import sqlite3
import time

# A DESKTOP user-agent, deliberately. A mobile UA makes Facebook serve the
# "WebLite/Bloks" shell — content renders, but every post is a tap-to-open
# JavaScript button with NO permalink and NO role="article", which is
# impossible to extract from (this cost us a long debugging session, see
# BLUEPRINT). The desktop site renders each post as a real role="article" with
# a real permalink link, which is what the extractor needs.
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BLOCK = {"image", "media", "font"}

# Extract posts from the rendered page — two ways, best-first:
#
#   1. JSON (primary, layout-proof). Facebook server-renders every post's data
#      into <script type="application/json"> blobs (its GraphQL/Relay store).
#      We walk those blobs and pick objects whose __typename is "Story" — a
#      STABLE discriminator that survives Facebook's constant CSS/DOM reshuffles
#      (which is what breaks visible-DOM scrapers). From each Story we read id,
#      permalink, exact time, text, author + PROFILE PICTURE, media, and the
#      reaction/comment/share counts — none of which the visible DOM gives us
#      reliably. This is how the commercial scrapers (Apify/Scrapfly) do it.
#   2. DOM (fallback). role="article" blocks, kept for the case where the JSON
#      shape shifts — so a Facebook change degrades us to the old behaviour
#      instead of to zero.
_EXTRACT_JS = r"""
() => {
  // A stable id from a permalink. FB uses several shapes:
  //   /reel/<num>, /posts/pfbid<...>, story_fbid=<num|pfbid>, /videos/<num>,
  //   /permalink/<num>, fbid=<num>. pfbid tokens are letters+digits.
  const idFrom = (h) => {
    if (!h) return null;
    let m;
    if (m = h.match(/\/reel\/(\d+)/)) return 'reel_' + m[1];
    if (m = h.match(/(pfbid[0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/story_fbid=([0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/\/posts\/([0-9A-Za-z]+)/)) return m[1];
    if (m = h.match(/\/videos\/(\d+)/)) return 'vid_' + m[1];
    if (m = h.match(/\/permalink\/(\d+)/)) return m[1];
    if (m = h.match(/fbid=([0-9A-Za-z]+)/)) return m[1];
    return null;
  };
  const isPerma = (h) =>
    /\/posts\/|\/story|story_fbid=|\/videos\/|\/photos\/|\/permalink\/|\/reel\//.test(h);

  // ---- generic JSON walkers (depth-capped so a huge blob can't hang) ----
  const deepFind = (o, pred, d) => {
    d = d || 0;
    if (!o || typeof o !== 'object' || d > 14) return null;
    if (pred(o)) return o;
    if (Array.isArray(o)) { for (const x of o) { const r = deepFind(x, pred, d + 1); if (r) return r; } return null; }
    for (const k in o) { const r = deepFind(o[k], pred, d + 1); if (r) return r; }
    return null;
  };
  const deepAll = (o, pred, acc, d) => {
    acc = acc || []; d = d || 0;
    if (!o || typeof o !== 'object' || d > 14 || acc.length > 40) return acc;
    if (pred(o)) acc.push(o);
    if (Array.isArray(o)) { for (const x of o) deepAll(x, pred, acc, d + 1); return acc; }
    for (const k in o) deepAll(o[k], pred, acc, d + 1);
    return acc;
  };

  // ---- 1) JSON extraction ----
  const stories = [];
  const seen = new Set();
  const collect = (o, d) => {
    d = d || 0;
    if (!o || typeof o !== 'object' || d > 16) return;
    if (Array.isArray(o)) { for (const x of o) collect(x, d + 1); return; }
    if (o.__typename === 'Story' && (o.post_id || o.creation_time || o.wwwURL || o.url)) {
      const key = o.post_id || o.id || o.wwwURL || o.url;
      if (!seen.has(key)) { seen.add(key); stories.push(o); }
    }
    for (const k in o) { const v = o[k]; if (v && typeof v === 'object') collect(v, d + 1); }
  };
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    let data; try { data = JSON.parse(s.textContent); } catch (e) { continue; }
    collect(data, 0);
  }

  const jsonPosts = [];
  for (const st of stories) {
    const id = st.post_id || st.id || null;
    let perma = st.wwwURL || st.url || null;
    if (!perma) { const u = deepFind(st, (o) => typeof o.url === 'string' && isPerma(o.url)); perma = u ? u.url : null; }
    // message text: a node with a string .text and formatting ranges is the body
    const msg = (st.message && typeof st.message.text === 'string')
      ? st.message
      : deepFind(st, (o) => o && typeof o.text === 'string' && Array.isArray(o.ranges));
    const text = msg && msg.text ? msg.text : '';
    // author + profile picture
    const actor = (Array.isArray(st.actors) && st.actors[0])
      ? st.actors[0]
      : deepFind(st, (o) => o && (o.__typename === 'User' || o.__typename === 'Page') && o.name);
    const author = actor ? (actor.name || null) : null;
    const author_url = actor ? (actor.url || null) : null;
    const avatar = actor
      ? ((actor.profile_picture && actor.profile_picture.uri) || actor.profile_picture_url || null)
      : null;
    const created_ms = st.creation_time ? st.creation_time * 1000 : null;
    // media (URLs only)
    const media = [];
    for (const m of deepAll(st, (o) => o && (o.__typename === 'Photo' || o.__typename === 'Video' || o.__typename === 'GenericAttachmentMedia'))) {
      if (m.__typename === 'Video' || m.playable_url || m.playable_url_quality_hd) {
        const thumb = (m.preferred_thumbnail && m.preferred_thumbnail.image && m.preferred_thumbnail.image.uri) || (m.image && m.image.uri) || '';
        const url = m.playable_url_quality_hd || m.playable_url || thumb;
        if (url || thumb) media.push({ type: 'video', url: url || thumb, thumb: thumb || url });
      } else {
        const uri = (m.image && m.image.uri) || (m.photo_image && m.photo_image.uri) || (m.viewer_image && m.viewer_image.uri) || '';
        if (uri) media.push({ type: 'photo', url: uri, thumb: uri });
      }
      if (media.length >= 6) break;
    }
    // reaction / comment / share counts
    const fbk = deepFind(st, (o) => o && (o.reaction_count || o.i18n_reaction_count || o.comment_rendering_instance || o.share_count));
    const likes = fbk && fbk.reaction_count ? fbk.reaction_count.count : null;
    const comments = fbk
      ? ((fbk.comment_rendering_instance && fbk.comment_rendering_instance.comments && fbk.comment_rendering_instance.comments.total_count) || fbk.total_comment_count || null)
      : null;
    const shares = fbk && fbk.share_count ? fbk.share_count.count : null;
    if (!id && !perma) continue;
    jsonPosts.push({
      id: id ? String(id) : idFrom(perma),
      author, author_url, author_avatar: avatar, permalink: perma,
      text: (text || '').slice(0, 2000), created_ms, media,
      like_count: likes, comment_count: comments, share_count: shares,
    });
  }

  // ---- 2) DOM fallback ----
  const out = [];
  for (const a of document.querySelectorAll('[role="article"]')) {
    if (a.closest('[role="dialog"]')) continue;   // skip comment/reel popovers
    const links = [...a.querySelectorAll('a[href]')].map(l => l.href);
    const perma = links.find(isPerma) || null;
    const head = a.querySelector('h2 a, h3 a, h4 a, strong a, a[aria-label]');
    const author = head ? head.innerText.trim() : null;
    const author_url = head ? head.href : null;
    const bodies = [...a.querySelectorAll('div[dir="auto"]')]
      .map(d => d.innerText.trim()).filter(Boolean);
    const text = bodies.sort((x, y) => y.length - x.length)[0] || "";
    const imgs = [...a.querySelectorAll('img')].map(i => i.src)
      .filter(s => /scontent|fbcdn/.test(s) && !/s32x32|s40x40|p32x32|p24x24/.test(s));
    out.push({ id: idFrom(perma), author, author_url, permalink: perma,
               text: text.slice(0, 2000), media: [...new Set(imgs)].slice(0, 6) });
  }
  // Diagnostics gathered from the SAME render, so if extraction returns nothing
  // we still learn what the page held — surfaced in the "Fetch now" log, no
  // terminal needed. Returned together so it can never be skipped.
  const permaAll = [...document.querySelectorAll('a[href]')].map(a => a.href)
    .filter(isPerma);
  const roles = {};
  for (const e of document.querySelectorAll('[role]')) {
    const r = e.getAttribute('role'); roles[r] = (roles[r] || 0) + 1;
  }
  // Every link on the page (href + its short text), so the real permalink
  // shape is visible even when it does not match the patterns above.
  const allLinks = [...new Set([...document.querySelectorAll('a[href]')]
    .map(a => a.href))].slice(0, 45);
  // Climb from each long text block to the first ancestor that also holds an
  // image and links — that ancestor IS the post container on layouts without
  // role="article". Report its tag/attrs/links so the selector can be written.
  const longs = [...document.querySelectorAll('div[dir="auto"]')]
    .filter(d => (d.innerText || "").trim().length > 60).slice(0, 5);
  const containers = longs.map(d => {
    let el = d;
    for (let i = 0; i < 9 && el; i++) {
      const hasImg = el.querySelector('img[src*="fbcdn"], img[src*="scontent"]');
      const links = [...new Set([...el.querySelectorAll('a[href]')].map(a => a.href))];
      if (hasImg && links.length) {
        return { tag: el.tagName, role: el.getAttribute('role'),
                 attrs: el.getAttributeNames().slice(0, 12),
                 dataft: el.getAttribute('data-ft'),
                 links: links.slice(0, 8),
                 text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80) };
      }
      el = el.parentElement;
    }
    return { none: (d.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60) };
  });
  const diag = {
    url: location.href,
    title: document.title,
    json_stories: stories.length,
    json_posts: jsonPosts.length,
    articles: document.querySelectorAll('[role="article"]').length,
    feed: document.querySelectorAll('[role="feed"]').length,
    permalinks: permaAll.length,
    roles: roles,
    sample: [...new Set(permaAll)].slice(0, 5),
    all_links: allLinks,
    containers: containers,
    body_head: (document.body ? document.body.innerText : "")
      .replace(/\s+/g, " ").trim().slice(0, 240),
  };
  return { json_posts: jsonPosts, dom_posts: out, diag };
}
"""


# --------------------------------------------------------------------------
# GraphQL response parsing (the reliable path for a LOGGED-IN session)
# --------------------------------------------------------------------------
#
# When logged in, Facebook does NOT embed the feed in the page — it fetches it
# over background /graphql requests after load. So the most reliable extraction
# is to capture those responses and pull the posts out of THEM. Each post is a
# "Story" object; we read the same fields the on-page JSON would have carried
# (id, permalink, time, text, author + profile picture, media, counts). Pure
# functions so the whole thing is unit-tested offline.

_PERMA_RE = re.compile(
    r"/posts/|/story|story_fbid=|/videos/|/photos/|/permalink/|/reel/")


def _is_perma(h) -> bool:
    return bool(h and _PERMA_RE.search(h))


def _handle_from_url(u):
    """The page handle from a profile URL — 'narendramodi' from
    facebook.com/narendramodi, or the numeric id from a profile.php?id= link.
    This is how a post in a mixed feed is attributed back to its page."""
    if not u:
        return None
    m = re.search(r"facebook\.com/([^/?#]+)", u)
    if not m:
        return None
    seg = m.group(1)
    if seg in ("profile.php", "people", "pages", "groups", "watch", "reel"):
        m2 = re.search(r"[?&]id=(\d+)", u)
        return m2.group(1) if m2 else None
    return seg or None


def _iter_json_objects(blob: str):
    """Facebook streams graphql as one JSON object, or several concatenated /
    newline-delimited. Yield every object we can parse out of the blob."""
    if not blob:
        return
    b = blob.strip()
    if b.startswith("for (;;);"):
        b = b[len("for (;;);"):]
    try:
        yield json.loads(b)
        return
    except Exception:
        pass
    for line in b.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _deep_find(o, pred, depth=0):
    if depth > 16 or not isinstance(o, (dict, list)):
        return None
    if isinstance(o, dict):
        if pred(o):
            return o
        for v in o.values():
            r = _deep_find(v, pred, depth + 1)
            if r is not None:
                return r
    else:
        for x in o:
            r = _deep_find(x, pred, depth + 1)
            if r is not None:
                return r
    return None


def _deep_all(o, pred, acc=None, depth=0):
    if acc is None:
        acc = []
    if depth > 16 or len(acc) > 40 or not isinstance(o, (dict, list)):
        return acc
    if isinstance(o, dict):
        if pred(o):
            acc.append(o)
        for v in o.values():
            _deep_all(v, pred, acc, depth + 1)
    else:
        for x in o:
            _deep_all(x, pred, acc, depth + 1)
    return acc


def _walk_stories(o, out, seen, depth=0):
    if depth > 18 or not isinstance(o, (dict, list)):
        return
    if isinstance(o, list):
        for x in o:
            _walk_stories(x, out, seen, depth + 1)
        return
    if o.get("__typename") == "Story" and (
            o.get("post_id") or o.get("creation_time") or o.get("wwwURL")):
        key = o.get("post_id") or o.get("id") or o.get("wwwURL")
        if key not in seen:
            seen.add(key)
            out.append(o)
    for v in o.values():
        if isinstance(v, (dict, list)):
            _walk_stories(v, out, seen, depth + 1)


def _story_to_post(st: dict):
    pid = st.get("post_id") or st.get("id")
    perma = st.get("wwwURL") or st.get("url")
    if not perma:
        u = _deep_find(st, lambda o: isinstance(o.get("url"), str) and _is_perma(o["url"]))
        perma = u["url"] if u else None
    if not pid and not perma:
        return None
    msg = st.get("message") if isinstance(st.get("message"), dict) and st["message"].get("text") \
        else _deep_find(st, lambda o: isinstance(o.get("text"), str) and isinstance(o.get("ranges"), list))
    text = (msg or {}).get("text") or ""
    actors = st.get("actors")
    actor = actors[0] if isinstance(actors, list) and actors else \
        _deep_find(st, lambda o: o.get("__typename") in ("User", "Page") and o.get("name"))
    author = actor.get("name") if actor else None
    avatar = None
    author_handle = None
    if actor:
        pp = actor.get("profile_picture")
        avatar = (pp.get("uri") if isinstance(pp, dict) else None) or actor.get("profile_picture_url")
        author_handle = actor.get("username") or _handle_from_url(actor.get("url")) \
            or (str(actor.get("id")) if actor.get("id") else None)
    created_ms = st["creation_time"] * 1000 if st.get("creation_time") else None
    media = []
    for m in _deep_all(st, lambda o: o.get("__typename") in ("Photo", "Video", "GenericAttachmentMedia")):
        if m.get("__typename") == "Video" or m.get("playable_url") or m.get("playable_url_quality_hd"):
            thumb = ((m.get("preferred_thumbnail") or {}).get("image") or {}).get("uri") \
                or (m.get("image") or {}).get("uri") or ""
            url = m.get("playable_url_quality_hd") or m.get("playable_url") or thumb
            if url or thumb:
                media.append({"type": "video", "url": url or thumb, "thumb": thumb or url})
        else:
            uri = (m.get("image") or {}).get("uri") or (m.get("photo_image") or {}).get("uri") or ""
            if uri:
                media.append({"type": "photo", "url": uri, "thumb": uri})
        if len(media) >= 6:
            break
    fbk = _deep_find(st, lambda o: o.get("reaction_count") or o.get("i18n_reaction_count")
                     or o.get("comment_rendering_instance") or o.get("share_count"))
    likes = (fbk.get("reaction_count") or {}).get("count") if fbk else None
    comments = None
    if fbk:
        cri = fbk.get("comment_rendering_instance") or {}
        comments = (cri.get("comments") or {}).get("total_count") or fbk.get("total_comment_count")
    shares = (fbk.get("share_count") or {}).get("count") if fbk else None
    return {"id": str(pid) if pid else _fallback_id(perma), "author": author,
            "author_handle": author_handle, "author_avatar": avatar,
            "permalink": perma, "text": text[:2000],
            "created_ms": created_ms, "media": media,
            "like_count": likes, "comment_count": comments, "share_count": shares}


def _stories_from_graphql(blobs) -> list:
    out, seen = [], set()
    for b in blobs or []:
        for obj in _iter_json_objects(b):
            _walk_stories(obj, out, seen)
    posts = []
    for s in out:
        p = _story_to_post(s)
        if p:
            posts.append(p)
    return posts


def _bandwidth_ok(meter_db, cap_bytes):
    month = time.strftime("%Y-%m", time.gmtime())
    con = sqlite3.connect(meter_db)
    con.execute("CREATE TABLE IF NOT EXISTS bw(month TEXT PRIMARY KEY, bytes INTEGER)")
    row = con.execute("SELECT bytes FROM bw WHERE month=?", (month,)).fetchone()
    con.close()
    used = row[0] if row else 0
    return used < cap_bytes, used


def _record_bytes(meter_db, n):
    month = time.strftime("%Y-%m", time.gmtime())
    con = sqlite3.connect(meter_db)
    con.execute("CREATE TABLE IF NOT EXISTS bw(month TEXT PRIMARY KEY, bytes INTEGER)")
    con.execute("INSERT INTO bw(month,bytes) VALUES(?,?) "
                "ON CONFLICT(month) DO UPDATE SET bytes=bytes+excluded.bytes",
                (month, int(n)))
    con.commit(); con.close()


class FacebookEngine:
    """One warm browser for the whole run. Use as an async context manager."""

    def __init__(self, meter_db="fb_meter.db", log=print):
        self.log = log
        self.meter_db = meter_db
        self.cap_bytes = int(float(os.getenv("FB_MONTHLY_CAP_GB", "200")) * 1e9)
        # Where the whole logged-in session (cookies incl. the browser's OWN
        # datr) is saved between runs, so the session is reused instead of
        # replayed cold every time — the reuse is what stops the logouts.
        self.state_path = os.getenv("FB_STATE_PATH", "fb_state.json")
        self.email = os.getenv("FB_EMAIL", "").strip()
        self.password = os.getenv("FB_PASSWORD", "")
        self._pw = self._browser = self._ctx = self._page = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        use_proxy = os.getenv("FB_USE_PROXY", "0") == "1"
        proxy = None
        if use_proxy:
            gw = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
            proxy = {"server": f"http://{gw}",
                     "username": os.getenv("WEBSHARE_USER", ""),
                     "password": os.getenv("WEBSHARE_PASS", "")}
        self._browser = await self._pw.chromium.launch(
            headless=True, proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  # Strip the biggest "I'm automated" tell — the Automation
                  # Controlled blink feature that sets navigator.webdriver.
                  "--disable-blink-features=AutomationControlled"])

        # Prefer a saved session (it carries the browser's own datr, so Facebook
        # keeps it logged in). Fall back to the raw cookies from .env on first
        # run; the login-with-password path below rebuilds the session if both
        # are stale.
        ctx_kw = dict(user_agent=DESKTOP_UA,
                      viewport={"width": 1366, "height": 2600}, locale="en-US",
                      timezone_id=os.getenv("FB_TIMEZONE", "Asia/Kolkata"))
        if os.path.exists(self.state_path):
            ctx_kw["storage_state"] = self.state_path
            self._ctx = await self._browser.new_context(**ctx_kw)
            self.log(f"[fb] reusing saved session {self.state_path}")
        else:
            self._ctx = await self._browser.new_context(**ctx_kw)
            cookies = []
            if os.getenv("FB_C_USER") and os.getenv("FB_XS"):
                cookies = [
                    {"name": "c_user", "value": os.getenv("FB_C_USER", ""),
                     "domain": ".facebook.com", "path": "/"},
                    {"name": "xs", "value": os.getenv("FB_XS", ""),
                     "domain": ".facebook.com", "path": "/"}]
                for name, env in (("datr", "FB_DATR"), ("sb", "FB_SB")):
                    val = os.getenv(env, "")
                    if val:
                        cookies.append({"name": name, "value": val,
                                        "domain": ".facebook.com", "path": "/"})
            if cookies:
                await self._ctx.add_cookies(cookies)

        # Stealth: erase the leftover automation fingerprints a headless Chrome
        # still carries, so the browser reads as an ordinary logged-in Chrome.
        await self._ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = window.chrome || { runtime: {} };
        """)

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await self._ctx.route("**/*", route)

        self._bytes = 0
        self._gql = []          # captured graphql response bodies (per attempt)
        self._gql_bytes = 0

        async def on_resp(resp):
            try:
                cl = resp.headers.get("content-length")
                if cl:
                    self._bytes += int(cl)
            except Exception:
                pass
            # Capture the graphql responses that actually carry posts — this is
            # where a logged-in session's feed data lives (not in the page).
            try:
                if "/graphql" in resp.url and self._gql_bytes < 14_000_000:
                    body = await resp.text()
                    if ("post_id" in body or "Story" in body
                            or "creation_time" in body):
                        self._gql.append(body)
                        self._gql_bytes += len(body)
            except Exception:
                pass
        self._ctx.on("response", on_resp)
        self._page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *exc):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _human_scroll(self, rounds):
        """
        Scroll like a person, not a robot: real wheel events at the cursor
        (which scroll Facebook's inner feed container — window.scrollBy does
        not), varied distances, uneven pauses, and the occasional scroll back
        up. This both looks human (fewer logouts) and actually drives the feed's
        "load more" that fires the graphql we capture.
        """
        try:
            await self._page.mouse.move(683, 500)
        except Exception:
            pass
        for _ in range(max(0, rounds)):
            try:
                await self._page.mouse.wheel(0, random.randint(500, 1500))
            except Exception:
                await self._page.evaluate("window.scrollBy(0, 900)")
            await self._page.wait_for_timeout(random.randint(1300, 3800))
            if random.random() < 0.18:      # glance back up, like a human
                try:
                    await self._page.mouse.wheel(0, -random.randint(150, 450))
                except Exception:
                    pass
                await self._page.wait_for_timeout(random.randint(500, 1400))

    async def _save_state(self):
        """Persist the whole logged-in session so the next run reuses it."""
        try:
            await self._ctx.storage_state(path=self.state_path)
        except Exception as e:
            self.log(f"[fb] could not save session: {type(e).__name__}: {e}")

    @staticmethod
    def _is_login_wall(url: str) -> bool:
        u = url or ""
        return ("login" in u or "/?next=" in u or "checkpoint" in u
                or "/recover/" in u)

    async def _login(self) -> bool:
        """
        Log in with FB_EMAIL / FB_PASSWORD in the real browser, so Facebook
        issues a fresh session bound to THIS browser's datr, and save it. This
        is the durable fix for the session getting logged out: instead of
        replaying borrowed cookies, we hold a session the browser itself owns.
        """
        if not (self.email and self.password):
            self.log("[fb] logged out and no FB_EMAIL / FB_PASSWORD set — "
                     "cannot re-login")
            return False
        p = self._page
        try:
            await p.goto("https://www.facebook.com/login.php",
                         wait_until="domcontentloaded", timeout=60000)
            await p.wait_for_timeout(2500)
            await p.fill('input[name="email"]', self.email)
            await p.fill('input[name="pass"]', self.password)
            # The button's name/shape varies; try the common ones in order.
            for sel in ('button[name="login"]', 'input[name="login"]',
                        'button[type="submit"]', '[data-testid="royal_login_button"]'):
                el = await p.query_selector(sel)
                if el:
                    await el.click()
                    break
            await p.wait_for_timeout(7000)
        except Exception as e:
            self.log(f"[fb] login attempt failed: {type(e).__name__}: {e}")
            return False
        if self._is_login_wall(p.url):
            self.log(f"[fb] login did not complete — wrong password, or Facebook "
                     f"is asking for a checkpoint/2FA. url={p.url}")
            return False
        await self._save_state()
        self.log("[fb] logged in with password; session saved")
        return True

    def _build_from_json(self, handle, items):
        """Records from the JSON path — a real post_id is enough (no permalink
        needed); synthesize a URL if Facebook didn't give one."""
        posts = []
        for r in items:
            pid = r.get("id") or (r.get("permalink") and _fallback_id(r["permalink"]))
            if not pid:
                continue
            url = r.get("permalink") or f"https://www.facebook.com/{handle}/posts/{pid}"
            posts.append({
                "post_id": f"{handle}:{pid}",
                "page": handle,
                "url": url,
                "created_ms": r.get("created_ms"),
                "author_name": r.get("author"),
                "author_avatar": r.get("author_avatar"),
                "text": r.get("text") or "",
                "like_count": r.get("like_count"),
                "comment_count": r.get("comment_count"),
                "share_count": r.get("share_count"),
                "media": r.get("media") or [],
            })
        return posts

    def _build_feed(self, items):
        """Records from a MIXED feed (Favorites) — each post is attributed to
        its OWN author page, not a single handle. Media is normalized whether it
        arrived as {type,url,thumb} dicts (JSON paths) or bare url strings (DOM)."""
        posts = []
        for r in items:
            h = r.get("author_handle") or _handle_from_url(r.get("author_url"))
            if not h:
                continue
            pid = r.get("id") or (r.get("permalink") and _fallback_id(r["permalink"]))
            if not pid:
                continue
            media = []
            for m in (r.get("media") or []):
                if isinstance(m, str):
                    media.append({"type": "photo", "url": m, "thumb": m})
                elif isinstance(m, dict):
                    media.append(m)
            url = r.get("permalink") or f"https://www.facebook.com/{h}/posts/{pid}"
            posts.append({
                "post_id": f"{h}:{pid}",
                "page": h,
                "url": url,
                "created_ms": r.get("created_ms"),
                "author_name": r.get("author"),
                "author_avatar": r.get("author_avatar"),
                "text": r.get("text") or "",
                "like_count": r.get("like_count"),
                "comment_count": r.get("comment_count"),
                "share_count": r.get("share_count"),
                "media": media[:6],
            })
        return posts

    def _build_posts(self, handle, raw):
        """Records from the DOM fallback — permalink required as the id source."""
        posts = []
        for r in raw:
            if not r.get("permalink"):      # drop comments / non-post articles
                continue
            pid = r.get("id") or _fallback_id(r["permalink"])
            if not pid:
                continue
            posts.append({
                "post_id": f"{handle}:{pid}",
                "page": handle,
                "url": r["permalink"],
                "created_ms": None,          # DOM hides exact time
                "author_name": r.get("author"),
                "author_avatar": None,
                "text": r.get("text") or "",
                "like_count": None, "comment_count": None, "share_count": None,
                "media": [{"type": "photo", "url": u, "thumb": u} for u in r.get("media", [])],
            })
        return posts

    async def _attempt(self, url, handle, max_scroll, allow_login):
        """One navigate+extract. Returns (posts, diag, method). Order of trust:
        captured graphql (logged-in feed data) > on-page JSON > DOM."""
        self._gql = []          # only THIS attempt's graphql responses
        self._gql_bytes = 0
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2500)
        if allow_login and self._is_login_wall(self._page.url):
            self.log(f"[fb] {handle}: session logged out — attempting re-login")
            if await self._login():
                await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await self._page.wait_for_timeout(2500)
            if self._is_login_wall(self._page.url):
                self.log(f"[fb] {handle}: NOT LOGGED IN — re-login failed. "
                         f"url={self._page.url}")
                return [], None, "none"
        try:
            await self._page.wait_for_selector('[role="article"]', timeout=20000)
        except Exception:
            pass
        await self._page.wait_for_timeout(random.randint(1500, 3000))
        await self._human_scroll(max_scroll)
        res = await self._page.evaluate(_EXTRACT_JS)
        if not isinstance(res, dict):
            res = {}
        diag = res.get("diag") or {}
        # 1) captured graphql (most reliable when logged in)
        gql = _stories_from_graphql(self._gql)
        diag["gql_responses"] = len(self._gql)
        diag["gql_posts"] = len(gql)
        if gql:
            return self._build_from_json(handle, gql), diag, "gql"
        # 2) on-page JSON
        jsonp = res.get("json_posts") or []
        if jsonp:
            return self._build_from_json(handle, jsonp), diag, "json"
        # 3) DOM fallback
        return self._build_posts(handle, res.get("dom_posts") or []), diag, "dom"

    async def fetch_page(self, handle: str, max_scroll: int = 8) -> list:
        """
        Newest posts of one Facebook page, as normalized records. Refuses if the
        monthly byte cap is spent (returns [] and logs), so it can never overrun.

        Tries the desktop site first (real role="article" posts), then falls
        back to mbasic (server-rendered HTML, cleanest of all) if the desktop
        render yields nothing — Facebook A/B tests its layouts, so having two
        surfaces to try makes collection resilient.
        """
        ok, used = _bandwidth_ok(self.meter_db, self.cap_bytes)
        if not ok:
            self.log(f"[fb] monthly bandwidth cap reached ({used/1e9:.1f} GB) — "
                     f"skipping {handle}")
            return []

        self._bytes = 0
        posts, diag, source = [], None, "www"
        try:
            posts, diag, method = await self._attempt(
                f"https://www.facebook.com/{handle}", handle, max_scroll,
                allow_login=True)
            source = f"www:{method}"
        except Exception as e:
            self.log(f"[fb] fetch {handle} (www) failed: {type(e).__name__}: {e}")

        if not posts:
            # mbasic: no JavaScript, posts are plain <article> with story links.
            try:
                p2, d2, m2 = await self._attempt(
                    f"https://mbasic.facebook.com/{handle}", handle,
                    max_scroll=2, allow_login=False)
                if p2:
                    posts, diag, source = p2, d2, f"mbasic:{m2}"
                elif d2 is not None:
                    diag = d2
            except Exception as e:
                self.log(f"[fb] fetch {handle} (mbasic) failed: {type(e).__name__}: {e}")

        _record_bytes(self.meter_db, self._bytes)
        await self._save_state()

        self.log(f"[fb] {handle}: {len(posts)} posts via {source}, "
                 f"{self._bytes//1024} KB")
        if diag is not None:
            diag["handle"] = handle
            diag["parsed"] = len(posts)
            try:
                with open(os.getenv("FB_DIAG_PATH", "fb_diag.json"), "w") as f:
                    json.dump(diag, f)
            except Exception:
                pass
            self.log(f"[fb] {handle}: diag — title={diag.get('title')!r} "
                     f"gql_responses={diag.get('gql_responses')} "
                     f"gql_posts={diag.get('gql_posts')} "
                     f"json_stories={diag.get('json_stories')} "
                     f"json_posts={diag.get('json_posts')} "
                     f"articles={diag.get('articles')} feed={diag.get('feed')} "
                     f"permalinks={diag.get('permalinks')} "
                     f"roles={json.dumps(diag.get('roles'))}")
            if len(posts) == 0:
                self.log("[fb] all_links=" +
                         json.dumps(diag.get("all_links"))[:1600])
                self.log("[fb] containers=" +
                         json.dumps(diag.get("containers"))[:1800])
        return posts

    async def fetch_favorites(self, max_scroll: int = 12) -> list:
        """
        Read the account's FAVORITES feed — a single real news feed of every
        page the account has favorited. Because it is a genuine feed (not a page
        profile), it infinite-scrolls and fires the graphql calls we capture, so
        this is the path that yields the rich data. Each post is attributed back
        to its own author page, so downstream can scope it to whichever project
        tracks that page. Returns records for ALL authors in the feed.
        """
        ok, used = _bandwidth_ok(self.meter_db, self.cap_bytes)
        if not ok:
            self.log(f"[fb] monthly bandwidth cap reached ({used/1e9:.1f} GB) — "
                     f"skipping favorites")
            return []

        self._bytes = 0
        self._gql = []
        self._gql_bytes = 0
        # Facebook only builds the Favorites feed through in-app navigation — a
        # direct URL load renders an empty page. So open home, then CLICK the
        # Favourites entry in the Feeds sidebar, exactly like a person does.
        home = os.getenv("FB_HOME_URL", "https://www.facebook.com/")
        diag = {}
        posts = []
        source = "none"
        try:
            await self._page.goto(home, wait_until="domcontentloaded", timeout=60000)
            await self._page.wait_for_timeout(3000)
            if self._is_login_wall(self._page.url):
                self.log("[fb] favorites: session logged out — attempting re-login")
                if await self._login():
                    await self._page.goto(home, wait_until="domcontentloaded", timeout=60000)
                    await self._page.wait_for_timeout(3000)
                if self._is_login_wall(self._page.url):
                    self.log(f"[fb] favorites: NOT LOGGED IN. url={self._page.url}")
                    _record_bytes(self.meter_db, self._bytes)
                    return []
            # Click "Favourites" (UK spelling) / "Favorites" — the sidebar link's
            # href carries filter=favorites regardless of the displayed spelling.
            clicked = False
            for sel in ('a[href*="filter=favorites"]',
                        'a[aria-label="Favourites"]', 'a[aria-label="Favorites"]'):
                try:
                    el = await self._page.query_selector(sel)
                    if el:
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                for name in ("Favourites", "Favorites"):
                    try:
                        await self._page.get_by_role(
                            "link", name=name, exact=True).first.click(timeout=4000)
                        clicked = True
                        break
                    except Exception:
                        pass
            self.log(f"[fb] favorites: clicked Favourites link = {clicked}")
            await self._page.wait_for_timeout(random.randint(3000, 5000))
            try:
                await self._page.wait_for_selector('[role="article"]', timeout=20000)
            except Exception:
                pass
            await self._page.wait_for_timeout(random.randint(1500, 3000))
            await self._human_scroll(max_scroll)
            res = await self._page.evaluate(_EXTRACT_JS)
            if isinstance(res, dict):
                diag = res.get("diag") or {}
            gql = _stories_from_graphql(self._gql)
            diag["gql_responses"] = len(self._gql)
            diag["gql_posts"] = len(gql)
            if gql:
                posts, source = self._build_feed(gql), "gql"
            elif res.get("json_posts"):
                posts, source = self._build_feed(res["json_posts"]), "json"
            else:
                posts, source = self._build_feed(res.get("dom_posts") or []), "dom"
        except Exception as e:
            self.log(f"[fb] favorites failed: {type(e).__name__}: {e}")

        _record_bytes(self.meter_db, self._bytes)
        await self._save_state()
        authors = sorted({p["page"] for p in posts})
        self.log(f"[fb] favorites: {len(posts)} posts from {len(authors)} pages "
                 f"via {source}, {self._bytes//1024} KB")
        try:
            diag["handle"] = "favorites"
            diag["parsed"] = len(posts)
            diag["authors"] = authors[:40]
            with open(os.getenv("FB_DIAG_PATH", "fb_diag.json"), "w") as f:
                json.dump(diag, f)
        except Exception:
            pass
        self.log(f"[fb] favorites: diag — title={diag.get('title')!r} "
                 f"gql_responses={diag.get('gql_responses')} "
                 f"gql_posts={diag.get('gql_posts')} "
                 f"articles={diag.get('articles')} authors={authors[:20]}")
        return posts


def _fallback_id(permalink: str) -> str | None:
    if not permalink:
        return None
    m = re.search(r"pfbid[0-9A-Za-z]+", permalink)
    if m:
        return m.group(0)
    m = re.search(r"(\d{6,})", permalink)
    return m.group(1) if m else None
