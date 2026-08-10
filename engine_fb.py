"""
engine_fb.py — fetch a Facebook page's newest posts via ONE warm headless
browser, logged in with a burner session.

Design settled this session (see FACEBOOK_PLAN.md):
  * runs from the SERVER's own IP by default (FB_USE_PROXY=0) — the VPS has
    4 TB, so Facebook does not touch the small residential pool;
  * a DESKTOP user-agent on www.facebook.com — this is the exact combination
    fb_probe.py PROVED renders real posts in role="article" blocks. (An earlier
    revision used a mobile Android UA; live runs showed Facebook redirects that
    to the m.facebook.com login wall — the session cookie is UA-sensitive, so
    the replay UA must match the desktop browser the cookies came from.)
  * ONE browser/context reused across pages so Facebook's app caches and later
    pages are cheaper;
  * image/video/font bytes blocked (URLs kept), and a hard monthly byte cap so
    it can never run away.

Session + settings come from the environment, never git:
  FB_C_USER, FB_XS            the burner account cookies (REQUIRED)
  FB_DATR, FB_SB, FB_FR       optional but STRONGLY recommended — Facebook's
                              device cookies; a session replayed without datr
                              is far more likely to be rejected/checkpointed.
                              Copy them from the same browser as c_user/xs.
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

# The UA fb_probe.py used when it PROVED the logged-in render works. Keep this
# in sync with the browser the burner's cookies are captured from — Facebook
# invalidates sessions whose device fingerprint flips (desktop cookie replayed
# as an Android phone is exactly that flip).
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
BLOCK = {"image", "media", "font"}

# Extract candidate posts from the rendered desktop page. Facebook wraps each
# post in role="article"; we read author, permalink, timestamp text, body, and
# media image URLs (URLs only — bytes are blocked). Comments are dropped later
# by keeping only articles that carry a post permalink.
_EXTRACT_JS = r"""
() => {
  const out = [];
  for (const a of document.querySelectorAll('[role="article"]')) {
    const links = [...a.querySelectorAll('a[href]')].map(l => l.href);
    const perma = links.find(h =>
      /\/posts\/|\/story|story_fbid=|\/videos\/|\/photos\/|\/permalink\/|\/reel\//.test(h)) || null;
    const head = a.querySelector('h2 a, h3 a, h4 a, strong a, a[aria-label]');
    const author = head ? head.innerText.trim() : null;
    const bodies = [...a.querySelectorAll('div[dir="auto"]')]
      .map(d => d.innerText.trim()).filter(Boolean);
    const text = bodies.sort((x, y) => y.length - x.length)[0] || "";
    const imgs = [...a.querySelectorAll('img')].map(i => i.src)
      .filter(s => /scontent|fbcdn/.test(s) && !/s32x32|s40x40|p32x32/.test(s));
    // best-effort id from the permalink
    let id = null;
    if (perma) {
      const m = perma.match(/story_fbid=([0-9]+)|\/posts\/([0-9]+)|\/videos\/([0-9]+)|\/reel\/([0-9]+)|fbid=([0-9]+)/);
      id = m ? (m[1] || m[2] || m[3] || m[4] || m[5]) : null;
    }
    out.push({ id, author, permalink: perma, text: text.slice(0, 2000),
               media: [...new Set(imgs)].slice(0, 6) });
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


def _session_cookies():
    """The burner session as Playwright cookie dicts. c_user/xs are required;
    datr/sb/fr ride along when provided (they mark the 'device' Facebook
    expects to see with this session — sending them makes rejection and
    checkpoints much less likely)."""
    cookies = []
    for name, env, http_only in (
            ("c_user", "FB_C_USER", False),
            ("xs",     "FB_XS",     True),
            ("datr",   "FB_DATR",   True),
            ("sb",     "FB_SB",     True),
            ("fr",     "FB_FR",     True)):
        v = os.getenv(env, "").strip()
        if v:
            cookies.append({"name": name, "value": v,
                            "domain": ".facebook.com", "path": "/",
                            "secure": True, "httpOnly": http_only})
    return cookies


class FacebookEngine:
    """One warm browser for the whole run. Use as an async context manager."""

    def __init__(self, meter_db="fb_meter.db", log=print):
        self.log = log
        self.meter_db = meter_db
        self.cap_bytes = int(float(os.getenv("FB_MONTHLY_CAP_GB", "200")) * 1e9)
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
        self._ctx = await self._browser.new_context(
            user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 2200},
            locale="en-US")
        await self._ctx.add_cookies(_session_cookies())

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

    async def _login_walled(self) -> bool:
        """True if Facebook bounced us to the login page — i.e. the session
        cookies were rejected. This must be LOUD, never a silent 0-posts."""
        url = self._page.url or ""
        if "login" in url or "/?next=" in url or "checkpoint" in url:
            return True
        try:
            title = (await self._page.title()) or ""
        except Exception:
            title = ""
        return "log in" in title.lower() or "log into" in title.lower()

    async def fetch_page(self, handle: str, max_scroll: int = 1) -> list:
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
        try:
            await self._page.goto(f"https://www.facebook.com/{handle}",
                                  wait_until="domcontentloaded", timeout=60000)
            # Let the feed render; posts appear when role=article shows up.
            try:
                await self._page.wait_for_selector('[role="article"]', timeout=15000)
            except Exception:
                pass                      # judged below — maybe login wall
            await self._page.wait_for_timeout(2000)

            if await self._login_walled():
                self.log(f"[fb] {handle}: SESSION REJECTED — Facebook bounced to "
                         f"the login page (landed on {self._page.url}). The "
                         f"FB_XS session is dead or the cookies are incomplete. "
                         f"Log into the burner in a normal desktop browser, "
                         f"clear any checkpoint, and refresh FB_C_USER / FB_XS "
                         f"(+ FB_DATR / FB_SB) in .env.")
                _record_bytes(self.meter_db, self._bytes)
                return []

            for _ in range(max(0, max_scroll)):
                await self._page.mouse.wheel(0, 3000)
                await self._page.wait_for_timeout(2500)
            raw = await self._page.evaluate(_EXTRACT_JS)
        except Exception as e:
            self.log(f"[fb] fetch {handle} failed: {type(e).__name__}: {e}")
            _record_bytes(self.meter_db, self._bytes)
            return []
        _record_bytes(self.meter_db, self._bytes)

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
                "created_ms": None,          # FB hides exact time in DOM; fill later
                "author_name": r.get("author"),
                "text": r.get("text") or "",
                "like_count": None, "comment_count": None, "share_count": None,
                "media": [{"type": "photo", "url": u, "thumb": u} for u in r.get("media", [])],
            })
        if not posts and raw:
            self.log(f"[fb] {handle}: {len(raw)} articles rendered but none had a "
                     f"post permalink — extractor likely needs a tune (run "
                     f"fb_debug.py {handle} and check permalink_samples)")
        self.log(f"[fb] {handle}: {len(posts)} posts, {self._bytes//1024} KB")
        return posts


def _fallback_id(permalink: str) -> str | None:
    m = re.search(r"(\d{6,})", permalink or "")
    return m.group(1) if m else None
