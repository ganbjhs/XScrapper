"""
engine_fb.py — fetch a Facebook page's newest posts via ONE warm headless
browser, logged in with a burner session.

Design settled this session (see FACEBOOK_PLAN.md):
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

# Extract candidate posts from the rendered mobile page. Facebook wraps each
# post in role="article"; we read author, permalink, timestamp text, body, and
# media image URLs (URLs only — bytes are blocked). Comments are dropped later
# by keeping only articles that carry a post permalink.
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
  const out = [];
  for (const a of document.querySelectorAll('[role="article"]')) {
    if (a.closest('[role="dialog"]')) continue;   // skip comment/reel popovers
    const links = [...a.querySelectorAll('a[href]')].map(l => l.href);
    const perma = links.find(isPerma) || null;
    const head = a.querySelector('h2 a, h3 a, h4 a, strong a, a[aria-label]');
    const author = head ? head.innerText.trim() : null;
    const bodies = [...a.querySelectorAll('div[dir="auto"]')]
      .map(d => d.innerText.trim()).filter(Boolean);
    const text = bodies.sort((x, y) => y.length - x.length)[0] || "";
    const imgs = [...a.querySelectorAll('img')].map(i => i.src)
      .filter(s => /scontent|fbcdn/.test(s) && !/s32x32|s40x40|p32x32|p24x24/.test(s));
    out.push({ id: idFrom(perma), author, permalink: perma,
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
  return { posts: out, diag };
}
"""


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
            args=["--no-sandbox", "--disable-dev-shm-usage"])

        # Prefer a saved session (it carries the browser's own datr, so Facebook
        # keeps it logged in). Fall back to the raw cookies from .env on first
        # run; the login-with-password path below rebuilds the session if both
        # are stale.
        ctx_kw = dict(user_agent=DESKTOP_UA,
                      viewport={"width": 1366, "height": 2600}, locale="en-US")
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

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await self._ctx.route("**/*", route)

        self._bytes = 0

        async def on_resp(resp):
            try:
                cl = resp.headers.get("content-length")
                if cl:
                    self._bytes += int(cl)
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

    def _build_posts(self, handle, raw):
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
                "created_ms": None,          # exact time filled later if available
                "author_name": r.get("author"),
                "text": r.get("text") or "",
                "like_count": None, "comment_count": None, "share_count": None,
                "media": [{"type": "photo", "url": u, "thumb": u} for u in r.get("media", [])],
            })
        return posts

    async def _attempt(self, url, handle, max_scroll, allow_login):
        """One navigate+extract against a given URL. Returns (posts, diag)."""
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
                return [], None
        try:
            await self._page.wait_for_selector('[role="article"]', timeout=20000)
        except Exception:
            pass
        await self._page.wait_for_timeout(2000)
        for _ in range(max(0, max_scroll)):
            await self._page.evaluate("window.scrollBy(0, 2500)")
            await self._page.wait_for_timeout(2500)
        res = await self._page.evaluate(_EXTRACT_JS)
        raw = res.get("posts", []) if isinstance(res, dict) else (res or [])
        diag = res.get("diag") if isinstance(res, dict) else None
        return self._build_posts(handle, raw), diag

    async def fetch_page(self, handle: str, max_scroll: int = 4) -> list:
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
            posts, diag = await self._attempt(
                f"https://www.facebook.com/{handle}", handle, max_scroll,
                allow_login=True)
        except Exception as e:
            self.log(f"[fb] fetch {handle} (www) failed: {type(e).__name__}: {e}")

        if not posts:
            # mbasic: no JavaScript, posts are plain <article> with story links.
            source = "mbasic"
            try:
                p2, d2 = await self._attempt(
                    f"https://mbasic.facebook.com/{handle}", handle,
                    max_scroll=2, allow_login=False)
                if p2:
                    posts, diag = p2, d2
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
                     f"articles={diag.get('articles')} feed={diag.get('feed')} "
                     f"permalinks={diag.get('permalinks')} "
                     f"roles={json.dumps(diag.get('roles'))}")
            if len(posts) == 0:
                self.log("[fb] all_links=" +
                         json.dumps(diag.get("all_links"))[:1600])
                self.log("[fb] containers=" +
                         json.dumps(diag.get("containers"))[:1800])
        return posts


def _fallback_id(permalink: str) -> str | None:
    if not permalink:
        return None
    m = re.search(r"pfbid[0-9A-Za-z]+", permalink)
    if m:
        return m.group(0)
    m = re.search(r"(\d{6,})", permalink)
    return m.group(1) if m else None
