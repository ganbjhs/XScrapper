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

import os
import re
import sqlite3
import time

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
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
  return out;
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
        ctx_kw = dict(user_agent=MOBILE_UA,
                      viewport={"width": 412, "height": 2400}, locale="en-US")
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
            await p.goto("https://m.facebook.com/login.php",
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

    async def fetch_page(self, handle: str, max_scroll: int = 4) -> list:
        """
        Newest posts of one Facebook page, as normalized records. Refuses if the
        monthly byte cap is spent (returns [] and logs), so it can never overrun.
        """
        ok, used = _bandwidth_ok(self.meter_db, self.cap_bytes)
        if not ok:
            self.log(f"[fb] monthly bandwidth cap reached ({used/1e9:.1f} GB) — "
                     f"skipping {handle}")
            return []

        self._bytes = 0
        url = f"https://www.facebook.com/{handle}"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self._page.wait_for_timeout(2500)
            # If Facebook bounced us to a login/checkpoint wall, the session is
            # dead. Try to log back in with the password (fresh datr) and retry
            # once — that is the whole point of holding email+password.
            if self._is_login_wall(self._page.url):
                self.log(f"[fb] {handle}: session logged out — attempting re-login")
                if await self._login():
                    await self._page.goto(url, wait_until="domcontentloaded",
                                          timeout=60000)
                    await self._page.wait_for_timeout(2500)
                if self._is_login_wall(self._page.url):
                    self.log(f"[fb] {handle}: NOT LOGGED IN — session expired and "
                             f"re-login failed. url={self._page.url}")
                    _record_bytes(self.meter_db, self._bytes)
                    return []
            # Wait for the feed to actually render its post blocks.
            try:
                await self._page.wait_for_selector('[role="article"]', timeout=20000)
            except Exception:
                pass
            await self._page.wait_for_timeout(2000)
            # Scroll with JS, not the mouse wheel — a wheel event over a reel
            # thumbnail hover-opens a reel dialog and hijacks the extraction.
            for _ in range(max(0, max_scroll)):
                await self._page.evaluate("window.scrollBy(0, 2500)")
                await self._page.wait_for_timeout(2500)
            raw = await self._page.evaluate(_EXTRACT_JS)
        except Exception as e:
            self.log(f"[fb] fetch {handle} failed: {type(e).__name__}: {e}")
            _record_bytes(self.meter_db, self._bytes)
            return []
        _record_bytes(self.meter_db, self._bytes)
        # We reached the page logged in — refresh the saved session so its
        # cookies (and any rotated datr) carry to the next run.
        await self._save_state()

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
                "created_ms": None,          # FB mobile hides exact time; fill later
                "author_name": r.get("author"),
                "text": r.get("text") or "",
                "like_count": None, "comment_count": None, "share_count": None,
                "media": [{"type": "photo", "url": u, "thumb": u} for u in r.get("media", [])],
            })
        self.log(f"[fb] {handle}: {len(posts)} posts, {self._bytes//1024} KB")
        return posts


def _fallback_id(permalink: str) -> str | None:
    if not permalink:
        return None
    m = re.search(r"pfbid[0-9A-Za-z]+", permalink)
    if m:
        return m.group(0)
    m = re.search(r"(\d{6,})", permalink)
    return m.group(1) if m else None
