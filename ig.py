"""
ig.py — getting and keeping a usable Instagram session.

The Instagram counterpart to auth.py, and deliberately the same shape: a real
browser gets the session, plain HTTP will later use it. Only this module and
auth.py know how a session is obtained; nothing that collects imports either.

WHY A BROWSER, EMPHATICALLY. Instagram gates login behind exactly the things a
script cannot pass: image captchas, "we detected unusual activity", email and
SMS codes, and device-approval prompts on a phone you are holding. Those are
not obstacles to route around — they are the point. A human sitting in front of
the page clears every one of them in seconds. So the browser runs on the
server, its screen is streamed to whoever is signing in, and their clicks go
back. The captcha is solved by a person, because it is meant to be.

The asset this builds is the Chrome profile in profiles/<label>/, exactly as on
the X side, and it matters MORE here (RULEBOOK IG1): Instagram treats a stable
device as strong evidence the session is genuine, and a session whose
fingerprint changes reads as a stolen one. One profile per account, forever.

NOT YET VERIFIED AGAINST LIVE INSTAGRAM. The cookie names and the probe
endpoint are from Instagram's current web client, but nobody has run this
against a real account. Every failure path here is written to say what it saw
rather than to guess, because the first run is where that gets discovered.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import auth   # _launch, and the state constants; the browser plumbing is shared

LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"

# Instagram's public web client id. Not a secret — every browser sends it, and
# the private endpoints answer 400 without it.
WEB_APP_ID = "936619743392459"

# `sessionid` is the one that authenticates. The rest are carried because
# dropping them makes the HTTP client look like a different device than the
# browser that logged in — the same reasoning as auth.COOKIE_DOMAINS.
COOKIE_NAMES = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur",
                "datr", "ig_nrcb", "ps_l", "ps_n")
REQUIRED_COOKIES = ("sessionid", "ds_user_id")

LOGGED_IN = auth.LOGGED_IN
NEEDS_LOGIN = auth.NEEDS_LOGIN
CHALLENGE = auth.CHALLENGE
UNKNOWN = auth.UNKNOWN

# The screen the operator drives. Fixed, because click coordinates are scaled
# against it (see web.py). Instagram's login is a narrow column, so this is
# taller and narrower than X's.
LOGIN_VIEWPORT = {"width": 1000, "height": 820}
LOGIN_IDLE_TIMEOUT_S = 300


@dataclass
class Session:
    """Everything an HTTP client needs to be this browser."""
    username: str
    user_id: str
    user_agent: str
    cookies: dict = field(default_factory=dict)

    @property
    def has_required(self) -> bool:
        return all(self.cookies.get(k) for k in REQUIRED_COOKIES)


# ==========================================================================
# session store
# ==========================================================================
#
# Its own database, not twscrape's accounts.db. That schema belongs to twscrape
# and is X-shaped; bolting Instagram into it would mean either lying about what
# a row means or patching a library's table. A separate file is smaller, honest
# and independently backed up.

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  username     TEXT PRIMARY KEY,      -- as Instagram spells it
  user_id      TEXT,                  -- ds_user_id, stable across renames
  label        TEXT,                  -- the config.toml block that owns it
  cookies      TEXT NOT NULL,         -- JSON
  user_agent   TEXT NOT NULL,         -- the REAL browser's, never invented
  proxy        TEXT,
  active       INTEGER NOT NULL DEFAULT 0,
  error_msg    TEXT,
  last_used    TEXT,
  total_req    INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""


class Store:
    """The Instagram session store. Small, synchronous, one file."""

    def __init__(self, path):
        self.path = Path(path)
        self.db = None

    def open(self):
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        try:
            self.path.chmod(0o600)   # holds live session cookies
        except OSError:
            pass
        return self

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def save(self, sess: Session, label: str, proxy: str = "",
             active: bool = False, error: str = "") -> None:
        """
        Full upsert. Never a partial one.

        auth.py's docstring records what twscrape's add_account() cost this
        project: it early-returns when the row exists, so re-logging in
        silently kept stale cookies. Nothing here gets to do that.
        """
        now = _now()
        self.db.execute(
            "INSERT INTO accounts(username, user_id, label, cookies, user_agent,"
            "                     proxy, active, error_msg, last_used, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,?,?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "  user_id=excluded.user_id, label=excluded.label,"
            "  cookies=excluded.cookies, user_agent=excluded.user_agent,"
            "  proxy=excluded.proxy, active=excluded.active,"
            "  error_msg=excluded.error_msg, updated_at=excluded.updated_at",
            (sess.username, sess.user_id, label, json.dumps(sess.cookies),
             sess.user_agent, proxy or None, int(active), error or None, now, now))
        self.db.commit()

    # `active` now means "one of the phones that collects", not "THE phone".
    #
    # From 2026-08-31 to 2026-09-04 this store enforced exactly one active
    # row on write (_demote_others), because with a single collector the
    # alternative was WHICH account collects being settled by alphabetical
    # order. That reasoning still holds — and it is answered differently now:
    # every source is OWNED by exactly one account (store_ig.assign_sources,
    # sticky, balanced, logged), so N accounts collect in parallel and no
    # sort order ever chooses. Activating a row no longer demotes the others;
    # deactivating one moves its sources on the next pass (collect_ig).

    def set_active(self, username: str, active: bool, error: str = "") -> None:
        self.db.execute(
            "UPDATE accounts SET active=?, error_msg=?, updated_at=? WHERE username=?",
            (int(active), error or None, _now(), username))
        self.db.commit()

    def active_accounts(self) -> list:
        """Usernames of every row that participates in collection."""
        return [r["username"] for r in self.all() if r.get("active")]

    def all(self) -> list:
        if self.db is None:
            return []
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM accounts ORDER BY active DESC, username")]

    def get(self, username: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
        return dict(r) if r else None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# ==========================================================================
# state detection
# ==========================================================================

# Authoritative check: an authenticated request, made from the page, with the
# header Instagram's web client always sends. Same reasoning as auth.py's
# settings probe — a bare fetch without x-ig-app-id comes back 400 no matter
# how logged in the browser is, which would make a good session look dead.
_WHOAMI = """
async () => {
  try {
    const r = await fetch('/api/v1/accounts/edit/web_form_data/', {
      credentials: 'include',
      headers: {'x-ig-app-id': '%s', 'x-requested-with': 'XMLHttpRequest'},
    });
    if (!r.ok) return {ok: false, status: r.status};
    const j = await r.json();
    return {ok: true, username: (j.form_data || {}).username || ''};
  } catch (e) { return {ok: false, error: String(e)}; }
}
""" % WEB_APP_ID

# Fallback, read from the rendered app. Instagram renders a link to your own
# profile in the nav once you are signed in.
_DOM = """
() => {
  const out = {username: '', loginForm: false};
  const nav = document.querySelector('a[href^="/"][role="link"] img[alt*="profile picture"]');
  if (nav) {
    const a = nav.closest('a');
    const m = (a && a.getAttribute('href') || '').match(/^\\/([A-Za-z0-9._]+)\\/$/);
    if (m) out.username = m[1];
  }
  out.loginForm = !!document.querySelector('input[name="username"], input[name="password"]');
  return out;
}
"""


async def detect_state(page, ctx, diag=None) -> tuple:
    """
    Return (state, username). Cheap checks first, then the API, then the DOM.

    A false negative here is expensive in exactly the way auth.py describes:
    it leaves someone staring at a signed-in browser while the code insists
    nothing happened.
    """
    note = {} if diag is None else diag
    cookies = await cookies_dict(ctx)
    note["cookies"] = sorted(cookies)

    if not cookies.get("sessionid"):
        note["why"] = "no sessionid cookie"
        return NEEDS_LOGIN, ""

    url = (page.url or "").lower()
    note["url"] = url
    # Instagram parks you on these when it wants something from a human.
    if "/challenge" in url or "/accounts/suspended" in url:
        note["why"] = "on a challenge or suspension page"
        return CHALLENGE, ""
    if "/accounts/login" in url:
        note["why"] = "still on the login page"
        return NEEDS_LOGIN, ""

    try:
        res = await page.evaluate(_WHOAMI)
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    note["api"] = res
    if res.get("ok") and res.get("username"):
        note["why"] = f"web_form_data returned @{res['username']}"
        return LOGGED_IN, res["username"]

    try:
        dom = await page.evaluate(_DOM)
    except Exception as e:
        dom = {"error": f"{type(e).__name__}: {e}"}
    note["dom"] = dom
    if dom.get("username"):
        note["why"] = f"app is rendered and signed in as @{dom['username']}"
        return LOGGED_IN, dom["username"]

    # A sessionid with no identity is genuinely ambiguous, and R6 says say so
    # rather than guessing a colour.
    note["why"] = (f"sessionid present but identity unreadable "
                   f"(api={res.get('status') or res.get('error')})")
    return UNKNOWN, ""


async def cookies_dict(ctx) -> dict:
    out = {}
    for c in await ctx.cookies():
        if "instagram.com" in (c.get("domain") or "") and c.get("name") in COOKIE_NAMES:
            out[c["name"]] = c.get("value") or ""
    return out


# ==========================================================================
# the sign-in window
# ==========================================================================

class InteractiveLogin:
    """
    A real Instagram, running on the server, driven from the dashboard.

    Identical in shape to auth.InteractiveLogin — same frame/click/type/scroll
    surface, so web.py drives either without caring which it has. The browser
    plumbing itself is auth._launch, shared rather than copied: the sandbox
    fallbacks and the /dev/shm fix were paid for once already.

    The captcha and the OTP are not problems this code solves. They are shown
    to a person, who solves them. That is the whole design.
    """

    def __init__(self, acct):
        self.acct = acct
        self.pw = self.ctx = self.page = None
        self.started = self.touched = time.time()
        self.state = UNKNOWN
        self.screen_name = ""
        self.error = ""
        self.viewport = dict(LOGIN_VIEWPORT)
        self.device = {}
        self.ig_label = getattr(acct, "ig_label", "") or acct.label

    @property
    def root(self):
        # profile_path is <root>/profiles/<dir>; the device files sit beside it.
        return Path(self.acct.profile_path).parent.parent

    def _phone(self, log):
        """The window must BE this account's phone: same handset, same locale,
        same time zone, a mobile Chrome of the same major — the identity the
        app client will present afterwards (ig_identity). A legacy (library
        default) seed is replaced here, because this IS a sign-in."""
        import ig_identity
        import ig_session
        dev = ig_session.ensure_device(self.ig_label, self.root,
                                       username=self.acct.username, log=log)
        if ig_identity.is_legacy(dev):
            dev = ig_session.reseed(self.ig_label, self.root,
                                    why="legacy default phone; the browser "
                                        "sign-in mints the real one", log=log)
        self.device = dev
        kw = ig_identity.playwright_kwargs(dev)
        self.viewport = dict(kw["viewport"])
        log(f"window is {ig_identity.describe(dev)}")
        return kw

    async def _client_hints(self, log):
        """Playwright sets only the UA string; Chromium's own Client Hints
        would still say platform=Linux under an Android UA. CDP fixes the
        hints to match. Best effort — a browser without CDP keeps the UA."""
        import ig_identity
        try:
            cdp = await self.ctx.new_cdp_session(self.page)
            await cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": ig_identity.playwright_kwargs(self.device)["user_agent"],
                "acceptLanguage": (self.device.get("identity") or {}).get(
                    "accept_language", "en-IN,en;q=0.9"),
                "platform": "Linux armv8l",
                "userAgentMetadata": ig_identity.cdp_user_agent_metadata(self.device),
            })
        except Exception as e:
            log(f"client hints not set ({type(e).__name__}); the UA alone is in force")

    async def start(self, log=lambda m: None):
        from playwright.async_api import async_playwright

        extra = self._phone(log)
        self.pw = await async_playwright().start()
        self.ctx = await auth._launch(self.pw, self.acct, True, log, extra=extra)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self._client_hints(log)
        await self.page.set_viewport_size(self.viewport)
        try:
            await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(2000)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        await self.refresh_state()
        if self.state != LOGGED_IN:
            try:
                await self.page.goto(LOGIN_URL, wait_until="domcontentloaded",
                                     timeout=45000)
                # Wait for the FORM, not for a number of milliseconds.
                # Instagram is a React app: domcontentloaded fires long before
                # anything is painted, and a fixed sleep that is generous on a
                # laptop is a guess on a server. If it never appears, say so —
                # a blank window with no explanation is the worst outcome here.
                try:
                    await self.page.wait_for_selector(
                        'input[name="username"], input[name="password"]',
                        timeout=20000, state="visible")
                except Exception:
                    self.error = ("Instagram loaded but never showed a login "
                                  "form. It may be blocking this server's "
                                  "address, or the page did not render.")
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
        return self

    def _alive(self):
        if self.page is None:
            raise RuntimeError("sign-in window is closed")
        self.touched = time.time()

    @property
    def idle_s(self):
        return time.time() - self.touched

    async def refresh_state(self):
        try:
            self.state, name = await detect_state(self.page, self.ctx)
            if name:
                self.screen_name = name
        except Exception as e:
            self.state, self.error = UNKNOWN, f"{type(e).__name__}: {e}"
        return self.state

    async def frame(self) -> bytes:
        self._alive()
        # scale="css": one image pixel per CSS pixel, whatever the phone's
        # device_scale_factor, so a click on the frame maps 1:1 onto the
        # viewport and a 2.6× screen does not ship a 1071×2379 JPEG per poll.
        return await self.page.screenshot(type="jpeg", quality=68, scale="css")

    def url(self) -> str:
        return (self.page.url or "") if self.page else ""

    async def click(self, x: int, y: int):
        self._alive()
        await self.page.mouse.click(x, y)

    async def type_text(self, text: str):
        self._alive()
        # Instagram's login is a React form that ignores instantaneous input on
        # some fields, exactly as X's does.
        await self.page.keyboard.type(text, delay=25)

    async def press(self, key: str):
        self._alive()
        await self.page.keyboard.press(key)

    async def scroll(self, dy: int):
        self._alive()
        await self.page.mouse.wheel(0, dy)

    async def reload(self):
        self._alive()
        await self.page.reload(wait_until="domcontentloaded", timeout=45000)

    async def harvest(self) -> Session:
        """Copy the session out. Only meaningful once state is LOGGED_IN."""
        self._alive()
        cookies = await cookies_dict(self.ctx)
        ua = await self.page.evaluate("() => navigator.userAgent")
        return Session(username=self.screen_name,
                       user_id=cookies.get("ds_user_id", ""),
                       user_agent=ua, cookies=cookies)

    async def close(self):
        # Close the CONTEXT, never kill the process: Chrome only flushes the
        # profile on a clean exit, and that profile is the trusted device
        # (IG1). Losing it means facing a fresh device check every time.
        for closer in (lambda: self.ctx.close() if self.ctx else None,
                       lambda: self.pw.stop() if self.pw else None):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass
        self.pw = self.ctx = self.page = None


def capture(sess_store: Store, session: Session, acct) -> tuple:
    """
    Store a freshly harvested session.

    NOTE the difference from R2, and it is deliberate. On X, an account is only
    marked active after an authenticated HTTP request proves the cookies work.
    There is no equivalent probe here yet, because nothing collects from
    Instagram — so the session is stored as active on the strength of the
    browser having been signed in, and that is a weaker claim.

    When engine_ig.py exists, validate here before activating, exactly as
    auth.upsert_session does. Until then this is knowingly optimistic, which is
    why it is written down rather than left to be discovered.
    """
    if not session.has_required:
        return False, ("signed in, but Instagram has not set the session "
                       "cookies yet — give it a moment")
    sess_store.save(session, label=acct.label, proxy=acct.proxy or "", active=True)
    return True, ""
