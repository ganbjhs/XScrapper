"""
auth.py — getting and keeping a usable X session.

Two halves of one job:

  * browser login   — Playwright drives a real Chrome against a per-account
                      profile. Login is the one step where a browser earns its
                      cost: X gates it behind captcha, device verification and
                      2FA that no scripted HTTP replay clears.
  * session store   — the accounts.db adapter. twscrape owns that schema, but
                      every one of its obvious entry points is wrong for us.

Why not just use twscrape's own account handling:

  * add_account() logs "already exists" and RETURNS EARLY when the row is
    present (accounts_pool.py:99-104) — which is why editing cookies in .env
    never took effect in the prototype.
  * add_account_cookies() does upsert since twscrape 0.20.0, but it activates
    the row with no network call, keeps user_agent at the "@chrome"
    placeholder and never writes the proxy — three of the four things the
    harvest exists to record. Still not the primitive we want.
  * relogin() wipes cookies AND resets user_agent to "@chrome"
    (accounts_pool.py:228-248), destroying exactly what the browser harvested.
    Since 0.20.0 it skips rows whose password is "_" (our placeholder), so it
    would now silently do nothing to our accounts rather than damage them —
    either way it is not a refresh path for us.
  * login_all() selects WHERE error_msg IS NULL (accounts_pool.py:211), so
    one failed login excludes an account permanently; since 0.20.0 it also
    skips password="_" rows entirely.
  * cookie accounts are marked active with NO network call at all
    (accounts_pool.py:128-143), so expired cookies report success and fail
    much later as an unrelated-looking search error.

The correct primitive is pool.save() plus a real authenticated request before
anything is trusted.

The asset this module builds is not really the cookie — it is the persistent
Chrome profile in profiles/<label>/. X remembers that profile as a trusted
device, which is what makes later refreshes silent and headless. Guard it: one
user_data_dir per account forever, never two Chromes on one dir, always the
same channel/args/proxy/locale/timezone, always close the context so Chrome
flushes to disk.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from twscrape import API
from twscrape.account import Account
from twscrape.utils import utc

from config import PY


# ==========================================================================
# browser login (Playwright)
# ==========================================================================

# Cookie domains worth carrying over to the HTTP client. auth_token and ct0 are
# the ones that authenticate; the rest matter because dropping them makes the
# HTTP client look like a different device than the browser that logged in.
# `kdt` in particular is X's known-device token.
COOKIE_DOMAINS = ("x.com", ".x.com", "twitter.com", ".twitter.com")

HOME_URL = "https://x.com/home"
SETTINGS_PATH = "/i/api/1.1/account/settings.json"

# X's public web bearer token — the same constant the x.com app itself ships
# and that twscrape uses (account.py:13). Not a secret; every request to the
# internal API must carry it.
_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Chrome leaves these behind when it is killed rather than closed. A stale one
# makes the next launch fail with "profile appears to be in use".
SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

LOGGED_IN = "logged_in"
NEEDS_LOGIN = "needs_login"
CHALLENGE = "challenge"
UNKNOWN = "unknown"


class LoginError(Exception):
    pass


@dataclass
class Harvest:
    """Everything the HTTP client needs to impersonate this browser session."""
    screen_name: str
    user_agent: str
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def has_required(self) -> bool:
        return bool(self.cookies.get("auth_token") and self.cookies.get("ct0"))


# --------------------------------------------------------------------------
# state detection
# --------------------------------------------------------------------------

# Authoritative check. Runs same-origin from the x.com page, so it is
# indistinguishable from the app's own traffic, and it returns the canonical
# screen_name — which matters because accounts.db keys on username COLLATE
# NOCASE and a guessed-wrong value creates an orphan row.
#
# The headers are NOT optional. X's internal /i/api endpoints reject a
# cookies-only request: they require the public web bearer token plus
# x-csrf-token echoing the ct0 cookie. A bare fetch(..., {credentials:'include'})
# returns 401 no matter how logged in the browser is, which makes a perfectly
# good session look dead. twscrape's own client sets the same headers on every
# request (account.py:66-72).
_SETTINGS_PROBE = """
async () => {
  try {
    const ct0 = (document.cookie.match(/(?:^|;\\s*)ct0=([^;]+)/) || [])[1] || '';
    const r = await fetch('%s', {
      credentials: 'include',
      headers: {
        'authorization': '%s',
        'x-csrf-token': ct0,
        'x-twitter-auth-type': 'OAuth2Session',
        'x-twitter-active-user': 'yes',
      },
    });
    let body = '';
    try { body = (await r.clone().text()).slice(0, 200); } catch (e) {}
    if (!r.ok) return {ok: false, status: r.status, body: body};
    const j = await r.json();
    return {ok: true, status: r.status, screen_name: j.screen_name || '', body: body};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}
""" % (SETTINGS_PATH, _BEARER)

# Fallback identity sources, in the rendered app. This is not a nicety: X's
# v1.1 REST endpoints now answer 404 (code 34) for every path and host tried,
# so as of today the DOM is the ONLY thing that yields a screen_name during
# login.
#
# Two independent sources, because they fail differently:
#   * the left-nav profile link href — absent on narrow viewports
#   * the avatar container testid, which embeds the screen name and is present
#     in the timeline too, so it survives a hidden sidebar
_DOM_PROBE = """
() => {
  const out = {screen_name: '', source: ''};

  const link = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  const m = (link ? link.getAttribute('href') || '' : '').match(/^\\/([A-Za-z0-9_]+)$/);
  if (m) { out.screen_name = m[1]; out.source = 'nav-link'; }

  if (!out.screen_name) {
    const av = document.querySelector('[data-testid^="UserAvatar-Container-"]');
    const id = av ? av.getAttribute('data-testid').replace('UserAvatar-Container-', '') : '';
    if (/^[A-Za-z0-9_]+$/.test(id)) { out.screen_name = id; out.source = 'avatar-testid'; }
  }

  out.switcher   = !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  out.primaryCol = !!document.querySelector('[data-testid="primaryColumn"]');
  out.loginForm  = !!document.querySelector('input[autocomplete="username"], input[name="password"]');
  out.width      = window.innerWidth;
  return out;
}
"""

# Any of these means the SPA has finished booting one way or the other, so we
# can stop waiting. A fixed sleep is not enough: X's app boot time varies a
# lot, and reading the DOM too early looks exactly like "not logged in".
_READY_SELECTOR = ",".join([
    'a[data-testid="AppTabBar_Profile_Link"]',
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    '[data-testid^="UserAvatar-Container-"]',
    '[data-testid="primaryColumn"]',
    'input[autocomplete="username"]',
    'input[name="password"]',
])


async def _wait_for_app(page, timeout=12000) -> None:
    """Wait for X's SPA to render something we can read a verdict from."""
    try:
        await page.wait_for_selector(_READY_SELECTOR, timeout=timeout, state="attached")
    except Exception:
        pass  # fall through; detect_state will report what it can see


async def _cookies_dict(ctx) -> dict[str, str]:
    """All X cookies as a flat name->value dict."""
    out: dict[str, str] = {}
    for c in await ctx.cookies():
        if c.get("domain") in COOKIE_DOMAINS and c.get("value"):
            # x.com wins over twitter.com if both are present.
            if c["name"] not in out or c.get("domain", "").endswith("x.com"):
                out[c["name"]] = c["value"]
    return out


async def detect_state(page, ctx, diag=None) -> tuple[str, str]:
    """
    Return (state, screen_name).

    Cheap checks first, then the authenticated API probe, then a DOM fallback.
    The DOM fallback exists because a false negative here is expensive: it makes
    a perfectly good session look dead and leaves the operator staring at a
    logged-in browser while the script waits for something that already
    happened.

    `diag` optionally receives a dict explaining the verdict, which is what
    `login --debug-detect` prints.
    """
    note = {} if diag is None else diag

    cookies = await _cookies_dict(ctx)
    note["cookies"] = sorted(cookies)
    if not (cookies.get("auth_token") and cookies.get("ct0")):
        note["why"] = "auth_token and/or ct0 missing from the cookie jar"
        return NEEDS_LOGIN, ""

    url = page.url or ""
    note["url"] = url
    low = url.lower()
    if "/i/flow/login" in low or low.rstrip("/").endswith("x.com/login"):
        note["why"] = "still on the login flow"
        return NEEDS_LOGIN, ""
    if "/i/flow/" in low or "account/access" in low:
        note["why"] = "on a challenge/verification page"
        return CHALLENGE, ""

    # Authoritative: an authenticated request, from the page, with the headers
    # X actually requires.
    try:
        res = await page.evaluate(_SETTINGS_PROBE)
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    note["api"] = res

    if res.get("ok") and res.get("screen_name"):
        note["why"] = f"settings.json returned @{res['screen_name']}"
        return LOGGED_IN, res["screen_name"]

    # Fallback: the app itself is plainly rendered and logged in.
    try:
        dom = await page.evaluate(_DOM_PROBE)
    except Exception as e:
        dom = {"error": f"{type(e).__name__}: {e}"}
    note["dom"] = dom

    if dom.get("screen_name"):
        note["why"] = (
            f"API probe failed ({res.get('status')}) but the app is rendered "
            f"and logged in as @{dom['screen_name']}"
        )
        return LOGGED_IN, dom["screen_name"]

    if res.get("status") in (401, 403) and not dom.get("switcher"):
        note["why"] = f"settings.json returned {res.get('status')} and the app is not logged in"
        return NEEDS_LOGIN, ""

    note["why"] = (
        f"inconclusive — api={res.get('status') or res.get('error')} "
        f"dom_switcher={dom.get('switcher')}"
    )
    return UNKNOWN, ""


# --------------------------------------------------------------------------
# form driving
# --------------------------------------------------------------------------

async def _try_fill(page, selectors, value, timeout=8000) -> bool:
    """Fill the first selector that shows up. Never raises."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.fill(value)
            return True
        except Exception:
            continue
    return False


async def _try_click(page, selectors, timeout=8000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
            return True
        except Exception:
            continue
    return False


async def _drive_login_form(page, acct, log) -> None:
    """
    Best-effort automation of the login form.

    Deliberately never raises on a missing selector. X reshuffles this flow
    constantly and inserts challenge steps unpredictably, so every failure here
    simply falls through to the human wait — which is the real fallback for
    everything anyway. Automating it is a convenience, not a dependency.
    """
    try:
        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
    except Exception as e:
        log(f"  could not open the login page: {e}")
        return

    if not acct.username:
        log("  no username configured — complete the login by hand in the window")
        return

    filled = await _try_fill(
        page,
        ['input[autocomplete="username"]', 'input[name="text"]', 'input[data-testid="ocfEnterTextTextInput"]'],
        acct.username,
    )
    if not filled:
        log("  username field not found — continue by hand in the window")
        return
    log(f"  entered username {acct.username!r}")
    await _try_click(page, ['button:has-text("Next")', 'div[role="button"]:has-text("Next")'])
    await asyncio.sleep(1.5)

    # X sometimes interjects "enter your phone number or username" here. If the
    # email is configured, answering keeps the flow moving; otherwise the human
    # takes over.
    if acct.email:
        try:
            probe = page.locator('input[data-testid="ocfEnterTextTextInput"]').first
            await probe.wait_for(state="visible", timeout=3000)
            await probe.fill(acct.email)
            log("  answered the identity challenge with the configured email")
            await _try_click(page, ['button:has-text("Next")', 'div[role="button"]:has-text("Next")'])
            await asyncio.sleep(1.5)
        except Exception:
            pass

    password = acct.password
    if not password:
        log("  no password in .env — enter it by hand in the window")
        return
    if await _try_fill(page, ['input[name="password"]', 'input[type="password"]'], password):
        log("  entered password")
        await _try_click(page, ['button[data-testid="LoginForm_Login_Button"]', 'button:has-text("Log in")'])
    else:
        log("  password field not found — continue by hand in the window")


# --------------------------------------------------------------------------
# profile hygiene
# --------------------------------------------------------------------------

def clear_stale_locks(profile_dir: Path, log) -> None:
    """
    Remove Chrome's singleton lock files left by a killed process.

    Only safe when no Chrome is actually running against this directory; the
    caller is responsible for that (one account is logged in at a time, and
    `login` refuses to run while a watcher holds the pool).
    """
    for name in SINGLETON_FILES:
        p = profile_dir / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
                log(f"  cleared stale {name} from a previous crash")
        except OSError:
            pass


def _proxy_kwargs(url: str) -> dict:
    """
    Split a proxy URL into what Playwright actually honours.

    Chromium does NOT read credentials out of `--proxy-server`, so a residential
    URL of the shape http://user:pass@gateway:port passed whole as `server`
    authenticates as nobody and the first request comes back 407 — which
    surfaces as an inscrutable browser-launch or blank-page failure rather than
    "your proxy password was ignored". Playwright takes them as separate
    `username` / `password` fields; this hands them over that way.

    A URL with no credentials is passed through untouched.
    """
    import urllib.parse as _up

    try:
        u = _up.urlsplit(url)
    except Exception:
        return {"server": url}
    if not u.username:
        return {"server": url}
    hostport = u.hostname or ""
    if u.port:
        hostport = f"{hostport}:{u.port}"
    out = {"server": _up.urlunsplit((u.scheme or "http", hostport, u.path or "", "", "")),
           "username": _up.unquote(u.username)}
    if u.password:
        out["password"] = _up.unquote(u.password)
    return out


async def _launch(pw, acct, headless: bool, log):
    """Launch the persistent context, preferring real Chrome over bundled Chromium."""
    profile_dir = Path(acct.profile_path)
    profile_dir.mkdir(parents=True, exist_ok=True)
    clear_stale_locks(profile_dir, log)

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        # Chrome puts its shared-memory files in /dev/shm, which is 64 MB on a
        # default container or a systemd unit with PrivateTmp. Loading x.com
        # overruns that and the tab dies with no useful message.
        args.append("--disable-dev-shm-usage")

    kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
        locale=acct.locale,
        timezone_id=acct.timezone,
    )

    if headless:
        # X's layout is responsive, and the left nav — which is where the
        # profile link and account switcher live — is hidden below ~1000px.
        # Letting headless Chrome pick its own (narrow) window size makes the
        # logged-in DOM signals disappear and a perfectly good session read as
        # "unknown". Pin a desktop-width viewport instead.
        kwargs["viewport"] = {"width": 1440, "height": 900}
    else:
        kwargs["no_viewport"] = True   # real window size for the human

    if acct.proxy_or_none:
        kwargs["proxy"] = _proxy_kwargs(acct.proxy_or_none)

    # Tried in order, most faithful first.
    #
    # channel="chromium" MATTERS AND IS NOT COSMETIC. With headless=True and no
    # channel, Playwright runs `chrome-headless-shell` — a stripped-down binary
    # meant for scraping static pages. It loads Instagram's login perfectly
    # happily and then paints NOTHING: the tab showed a plain white rectangle,
    # the URL was right, the title was right, and the screenshot came back at
    # 8 KB instead of the ~70 KB a real render produces. Nothing errors, so
    # every check upstream passes.
    #
    # channel="chromium" asks for the FULL browser in new-headless mode, which
    # renders like the real thing. `playwright install chromium` puts both on
    # disk, so this costs nothing extra.
    #
    # The no-sandbox variants exist because Chromium's sandbox needs
    # unprivileged user namespaces, and a hardened systemd unit
    # (NoNewPrivileges) or Ubuntu's AppArmor policy denies them. The failure is
    # a launch error with no hint of the cause, and the whole browser is there
    # to visit one site, so falling back beats stopping. Announced, never
    # silent, and only ever reached after the sandboxed attempt failed.
    nosandbox = [*args, "--no-sandbox"]
    attempts = (
        ("Google Chrome", dict(kwargs, channel="chrome")),
        ("bundled Chromium", dict(kwargs, channel="chromium")),
        ("bundled Chromium without its sandbox",
         dict(kwargs, channel="chromium", args=nosandbox)),
        # Last resorts. These render badly on app-heavy sites; reaching them at
        # all is worth saying out loud.
        ("the headless shell (renders poorly)", kwargs),
        ("the headless shell without its sandbox (renders poorly)",
         dict(kwargs, args=nosandbox)),
    )
    last = None
    for i, (what, kw) in enumerate(attempts):
        try:
            ctx = await pw.chromium.launch_persistent_context(**kw)
            if i:
                log(f"  using {what}")
            return ctx
        except Exception as e:
            last = e
            log(f"  {what} did not start ({type(e).__name__})")
    raise LoginError(
        f"No browser could be started. Last error: {type(last).__name__}: {last}\n"
        f"  On a server, run: deploy/setup.sh   (it installs headless Chromium)"
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# interactive login, driven from the dashboard
# --------------------------------------------------------------------------

# The screen the operator sees. Fixed, because click coordinates are scaled
# against it — a viewport that changed size would land clicks in the wrong place.
LOGIN_VIEWPORT = {"width": 1100, "height": 780}

# Abandoned sessions hold a Chrome process and the account's profile directory
# open. Two Chromes on one profile corrupts it, so an idle one must not linger.
LOGIN_IDLE_TIMEOUT_S = 300


class InteractiveLogin:
    """
    A real browser, running on the server, that the operator drives remotely.

    Why this exists: X gates login behind captcha and device verification that
    no scripted HTTP replay clears, so a human has to see the page and click.
    On a server there is no screen to show them. So the browser runs headless
    here, its screen is streamed out as PNG frames, and clicks and keystrokes
    are forwarded back. It is a browser, just with the glass somewhere else.

    The session is captured the moment X reports the account as logged in;
    nothing is scraped from the rendered page, and the password is typed by the
    operator into the real x.com form, never handled by this code.
    """

    def __init__(self, acct):
        self.acct = acct
        self.pw = self.ctx = self.page = None
        self.started = time.time()
        self.touched = time.time()
        self.state = UNKNOWN
        self.screen_name = ""
        self.error = ""

    async def start(self, log=lambda m: None):
        from playwright.async_api import async_playwright

        self.pw = await async_playwright().start()
        # Headless: nobody is at the server's console. The viewport is pinned so
        # the operator's clicks map onto the same pixels we render.
        self.ctx = await _launch(self.pw, self.acct, True, log)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self.page.set_viewport_size(LOGIN_VIEWPORT)
        try:
            await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            await _wait_for_app(self.page)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        await self.refresh_state()
        # Already signed in on this profile? Then there is nothing to drive.
        if self.state != LOGGED_IN:
            try:
                await self.page.goto("https://x.com/i/flow/login",
                                     wait_until="domcontentloaded", timeout=45000)
                await _wait_for_app(self.page)
            except Exception:
                pass
        return self

    def _alive(self):
        if self.page is None:
            raise RuntimeError("login session is closed")
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
        return await self.page.screenshot(type="jpeg", quality=68)

    def url(self) -> str:
        """Where the remote browser actually is. Shown so it is not a black box."""
        return (self.page.url or "") if self.page else ""

    async def click(self, x: int, y: int):
        self._alive()
        await self.page.mouse.click(x, y)

    async def type_text(self, text: str):
        self._alive()
        # delay: X's login form is a React app that ignores instantaneous
        # programmatic input on some fields.
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

    async def harvest(self) -> "Harvest":
        """Capture the session. Only valid once state is LOGGED_IN."""
        self._alive()
        cookies = await _cookies_dict(self.ctx)
        ua = await self.page.evaluate("() => navigator.userAgent")
        return Harvest(screen_name=self.screen_name, user_agent=ua, cookies=cookies)

    async def close(self):
        # Closing the CONTEXT is what makes Chrome flush the profile to disk.
        # Killing the process instead loses the trusted-device state that makes
        # the next login silent.
        for closer in (
            lambda: self.ctx.close() if self.ctx else None,
            lambda: self.pw.stop() if self.pw else None,
        ):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception:
                pass
        self.pw = self.ctx = self.page = None


async def harvest_session(
    acct,
    *,
    headless: bool = False,
    refresh_only: bool = False,
    timeout: float = 300.0,
    poll_every: float = 2.0,
    log=print,
) -> Harvest:
    """
    Bring the account's browser profile to a logged-in state and harvest it.

    headless=True + refresh_only=True is the routine path: relaunch the trusted
    profile, let x.com boot (which rotates ct0), re-harvest, no human involved.
    Escalate to headed only when that fails.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise LoginError(
            "playwright is not installed. Run:\n"
            f"  {PY} -m pip install -r requirements.txt\n"
            f"  {PY} -m playwright install chromium"
        ) from e

    async with async_playwright() as pw:
        ctx = await _launch(pw, acct, headless, log)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
            except Exception as e:
                raise LoginError(f"could not reach x.com: {e}") from e
            # X boots its app and rotates ct0 shortly after load.
            await asyncio.sleep(2.0)

            state, screen_name = await detect_state(page, ctx)
            log(f"  profile state: {state}" + (f" (@{screen_name})" if screen_name else ""))

            if state != LOGGED_IN:
                if refresh_only:
                    raise LoginError(
                        f"profile is not logged in (state={state}) and --refresh-only was set.\n"
                        f"  Re-run without --refresh-only (and without --headless) to log in "
                        f"interactively."
                    )
                if headless:
                    raise LoginError(
                        f"profile is not logged in (state={state}) and the browser is headless, "
                        f"so nobody can clear the captcha / 2FA challenge.\n"
                        f"  Re-run without --headless."
                    )

                if state == NEEDS_LOGIN:
                    await _drive_login_form(page, acct, log)

                state, screen_name = await _wait_for_human(
                    page, ctx, acct, timeout, poll_every, log
                )

            if state != LOGGED_IN:
                raise LoginError(f"gave up waiting for a logged-in session (state={state})")

            cookies = await _cookies_dict(ctx)
            user_agent = await page.evaluate("() => navigator.userAgent")

            harvest = Harvest(screen_name=screen_name, user_agent=user_agent, cookies=cookies)
            if not harvest.has_required:
                raise LoginError(
                    "logged in, but auth_token/ct0 were not present in the cookie jar"
                )
            log(f"  harvested {len(cookies)} cookies for @{screen_name}"
                + (" (incl. known-device token)" if "kdt" in cookies else ""))
            return harvest
        finally:
            # Always close: Chrome only flushes the profile on a clean exit, and
            # the profile is the thing that keeps this device trusted.
            await ctx.close()


async def _wait_for_human(page, ctx, acct, timeout, poll_every, log):
    """Poll until the session goes live or we run out of patience."""
    log("")
    log(f"  >> Finish signing in to '{acct.label}' in the Chrome window.")
    log("  >> Solve any captcha / 2FA / device-verification prompt there.")
    log(f"  >> Waiting up to {int(timeout)}s...")
    log("")

    deadline = time.monotonic() + timeout
    last = None
    last_diag = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_every)
        diag = {}
        try:
            state, screen_name = await detect_state(page, ctx, diag)
        except Exception:
            continue
        last_diag = diag
        if state == LOGGED_IN:
            log(f"  signed in as @{screen_name}")
            return state, screen_name
        if state != last:
            remaining = int(deadline - time.monotonic())
            # Say WHY, not just what. A silent "needs_login" while the operator
            # is staring at a logged-in browser is the worst possible output.
            log(f"  ...{state}: {diag.get('why', '?')} ({remaining}s left)")
            last = state

    log("")
    log("  Timed out. Last detection detail:")
    for key in ("url", "why", "api", "dom"):
        if key in last_diag:
            log(f"    {key}: {last_diag[key]}")
    log("  If the browser clearly shows you logged in, this is a detection bug —")
    log(f"  re-run with: {PY} main.py login --account {acct.label} --debug-detect")
    return UNKNOWN, ""


async def debug_detect(acct, *, headless: bool = False, log=print) -> dict:
    """
    Open the account's profile and dump exactly what detection sees.

    For when the browser plainly shows a logged-in session but the script
    disagrees.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise LoginError("playwright is not installed") from e

    async with async_playwright() as pw:
        ctx = await _launch(pw, acct, headless, log)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)
            diag = {}
            state, screen_name = await detect_state(page, ctx, diag)
            log(f"  state       : {state}")
            log(f"  screen_name : {screen_name or '(none)'}")
            log(f"  url         : {diag.get('url')}")
            log(f"  why         : {diag.get('why')}")
            log(f"  cookies     : {', '.join(diag.get('cookies', [])) or '(none)'}")
            log(f"  api probe   : {diag.get('api')}")
            log(f"  dom probe   : {diag.get('dom')}")
            ua = await page.evaluate("() => navigator.userAgent")
            log(f"  user agent  : {ua}")
            return diag
        finally:
            await ctx.close()

# ==========================================================================
# session store (accounts.db)
# ==========================================================================

# How a session is validated outside the browser.
#
# This used to hit the v1.1 REST endpoints (account/settings.json and
# verify_credentials.json). Measured 2026-07-29 against a live logged-in
# session, every one of them answers 404 with error code 34, "Sorry, that page
# does not exist" — on api.x.com, x.com/i/api, and api.twitter.com alike, both
# from inside the browser and out. They are not usable.
#
# The working path is GraphQL, which requires an `x-client-transaction-id`
# header that X computes in-page. twscrape reverse-engineers that generator, so
# we borrow it. Bookmarks is the probe of choice because it is auth-only by
# construction (a logged-out session cannot have any) and it lives on its own
# rate-limit queue, so validating never spends the SearchTimeline budget that
# determines freshness.
#
# Deliberately checks the RAW response rather than iterating results: an
# account with zero bookmarks returns a valid, empty timeline, which would look
# identical to a failure through twscrape's item generator.
VALIDATION_PROBES = (("bookmarks", "graphql"),)

# Written by `watch`, read by `login`. Clearing an account's locks while a
# collector holds it would let two pollers use it at once.
WATCHER_LOCKFILE = ".watcher.pid"


@dataclass
class ValidationResult:
    ok: bool
    screen_name: str = ""
    status: int | None = None
    probe: str = ""
    error: str = ""


# Where a label's real X username is recorded after login.
#
# accounts.db keys on `username COLLATE NOCASE`, but config.toml only knows a
# label — and the authoritative username is not known until X reports it. Rather
# than rewrite the operator's config file, the mapping is stored next to the
# Chrome profile that produced it, which is where it belongs: that profile IS
# that account.
IDENTITY_FILE = ".xs_account.json"


@dataclass
class AccountHealth:
    label: str
    username: str
    active: bool
    has_cookies: bool
    has_known_device: bool
    real_user_agent: bool
    proxy: str | None
    total_req: int
    last_used: str | None
    locked_queues: list[str]
    error_msg: str | None


def read_identity(acct) -> str:
    """The X username this account's profile last logged in as ("" if never)."""
    try:
        data = json.loads((Path(acct.profile_path) / IDENTITY_FILE).read_text())
        return str(data.get("username") or "")
    except Exception:
        return acct.username or ""


def write_identity(acct, username: str) -> None:
    p = Path(acct.profile_path)
    p.mkdir(parents=True, exist_ok=True)
    (p / IDENTITY_FILE).write_text(
        json.dumps(
            {
                "label": acct.label,
                "username": username,
                "last_login": utc.now().isoformat(),
            },
            indent=2,
        )
    )


def open_api(db_path) -> API:
    """
    Build the twscrape API with our pool policy applied.

    _order_by defaults to "username" (accounts_pool.py:37), which makes the
    alphabetically-first account serve every request until it rate-limits —
    concentrating both wear and ban exposure on one account. LRU spreads it.
    SQLite sorts NULLs first, so never-used accounts are picked up first.
    """
    api = API(str(db_path))
    api.pool._order_by = "last_used ASC"
    return api


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

async def validate_http(acc: Account, proxy: str | None = None) -> ValidationResult:
    """
    Prove the harvested cookies authenticate OUTSIDE the browser.

    Uses acc.make_client() (account.py:73-85) — the exact client every search
    will later use: same user-agent, same cookie jar, same proxy, with the
    bearer token and x-csrf-token already injected. Validating through any
    other client would prove the wrong thing.

    Targets this specific account on purpose. Going through twscrape's
    QueueClient would be less code, but it leases whichever active account the
    pool picks, so with more than one account configured it could report a
    different account's health than the one just harvested.

    Note this proves the session AUTHENTICATES, not who it belongs to — the
    GraphQL bookmarks response carries no screen_name. Identity comes from the
    browser DOM of the very profile these cookies were taken from, which is a
    sound source: it is the logged-in session itself.
    """
    from urllib.parse import urlparse

    from twscrape.api import GQL_FEATURES, GQL_URL, OP_Bookmarks
    from twscrape.utils import encode_params
    from twscrape.xclid import XClIdGen

    url = f"{GQL_URL}/{OP_Bookmarks}"
    params = encode_params({
        "variables": {"count": 1, "includePromotedContent": False},
        "features": {**GQL_FEATURES, "graphql_timeline_v2_bookmark_timeline": True},
    })

    client = acc.make_client(proxy=proxy)
    try:
        # X requires this header on every /i/api request. Without it the server
        # answers 404 regardless of how valid the cookies are — which is
        # exactly what made the old v1.1 probes look like dead endpoints.
        try:
            gen = await XClIdGen.create(proxy=acc.resolve_proxy(proxy), cookies=acc.cookies)
        except Exception as e:
            # XClIdGen fetches an x.com page with these cookies; it raises
            # XClIdAccountError when X serves the logged-out bundle, which is
            # itself a definitive "this session is dead".
            return ValidationResult(
                False, "", None, "bookmarks",
                f"could not build x-client-transaction-id ({type(e).__name__}: {e}) — "
                f"usually means X served the logged-out page for these cookies",
            )

        hdr = {"x-client-transaction-id": gen.calc("GET", urlparse(url).path)}
        rep = await client.get(url, params=params, headers=hdr)
        status = rep.status_code

        try:
            body = rep.json() or {}
        except Exception:
            body = {}
        errors = body.get("errors") or []
        codes = {e.get("code") for e in errors if isinstance(e, dict)}

        if status == 200 and not (codes & {32, 34, 63, 64, 326}):
            return ValidationResult(True, acc.username, status, "bookmarks")

        msg = "; ".join(
            f"({e.get('code')}) {e.get('message')}" for e in errors if isinstance(e, dict)
        )
        return ValidationResult(
            False, "", status, "bookmarks",
            msg or f"HTTP {status}: {(rep.text or '')[:160]}",
        )
    except Exception as e:
        return ValidationResult(False, "", None, "bookmarks", f"{type(e).__name__}: {e}")
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

async def upsert_session(api: API, harvest, acct_cfg, *, clear_locks: bool = True):
    """
    Write a freshly harvested browser session into accounts.db and validate it.

    Returns (username, ValidationResult).
    """
    pool = api.pool
    username = harvest.screen_name
    acc = await pool.get_account(username)

    if acc is None:
        acc = Account(
            username=username,
            # Placeholders: we never use twscrape's HTTP password login. The
            # columns are NOT NULL, so they need something.
            password=acct_cfg.password or "_",
            email=acct_cfg.email or "_",
            email_password=acct_cfg.email_password or "_",
            user_agent="@chrome",
            active=False,
            locks={},
            stats={},
            headers={},
            cookies={},
        )

    # FIX (stale cookies): unconditional overwrite. add_account would have
    # silently kept the old ones.
    acc.cookies = dict(harvest.cookies)

    # The real browser UA, not twscrape's "@chrome" placeholder. That
    # placeholder makes make_client pick a RANDOM fake-useragent string seeded
    # by sha256(username) (account.py:74, http.py:30-33) — a UA that has never
    # been associated with these cookies. Storing the real one keeps the HTTP
    # client's fingerprint consistent with the browser that logged in.
    acc.user_agent = harvest.user_agent

    acc.proxy = acct_cfg.proxy_or_none

    # FIX (permanent lockout): clearing error_msg puts the account back in play.
    acc.error_msg = None

    # Force make_client to rebuild the bearer and x-csrf-token from the new ct0.
    acc.headers = {}

    if clear_locks:
        acc.locks = {}

    # FIX (unvalidated activation): never trust cookies before a real request.
    acc.active = False

    await pool.save(acc)

    # The identity cross-check that used to live here is gone with the v1.1
    # endpoints: the GraphQL probe proves the session authenticates but says
    # nothing about whose it is. That is acceptable because `username` comes
    # from the DOM of the very browser profile these cookies were harvested
    # from — the logged-in session itself — so the two cannot disagree.
    result = await validate_http(acc, proxy=acct_cfg.proxy_or_none)
    if result.ok:
        await pool.set_active(username, True)
    else:
        detail = result.error or f"validation failed via {result.probe}"
        await pool.mark_inactive(username, detail[:400])
        result = ValidationResult(False, "", result.status, result.probe, detail)
    return username, result


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

async def health(api: API, cfg=None) -> list[AccountHealth]:
    """One row per account in accounts.db, joined back to config labels."""
    by_username: dict[str, str] = {}
    if cfg is not None:
        for a in cfg.accounts:
            known = read_identity(a)
            if known:
                by_username[known.lower()] = a.label

    now = utc.now()
    rows: list[AccountHealth] = []
    for acc in await api.pool.get_all():
        locks = acc.locks or {}
        now_locked = [q for q, until in locks.items() if until and until > now]
        rows.append(
            AccountHealth(
                label=by_username.get(acc.username.lower(), "-"),
                username=acc.username,
                active=bool(acc.active),
                has_cookies=bool(
                    (acc.cookies or {}).get("auth_token") and (acc.cookies or {}).get("ct0")
                ),
                has_known_device="kdt" in (acc.cookies or {}),
                real_user_agent=not (acc.user_agent or "").startswith("@"),
                proxy=acc.proxy,
                total_req=sum((acc.stats or {}).values()),
                last_used=acc.last_used.isoformat() if acc.last_used else None,
                locked_queues=sorted(now_locked),
                error_msg=acc.error_msg,
            )
        )
    return sorted(rows, key=lambda r: (not r.active, r.username.lower()))


async def active_usernames(api: API) -> list[str]:
    return [a.username for a in await api.pool.get_all() if a.active]


async def require_active(api: API) -> list[str]:
    """
    Assert the pool can actually serve requests.

    This replaces the prototype's ensure_login(), which tried to log in as a
    side effect of searching. Auth is now an explicit command: this only
    reports, it never authenticates.
    """
    names = await active_usernames(api)
    if names:
        return names

    from config import CLI

    rows = await health(api)
    if not rows:
        raise RuntimeError(
            "No accounts in the session store.\n"
            f"  Fix: {CLI} login --all"
        )
    detail = "\n".join(
        f"    @{r.username}: " + (r.error_msg or "inactive, no error recorded")
        for r in rows
    )
    raise RuntimeError(
        "No active account in the session store.\n"
        f"{detail}\n"
        f"  Fix: {CLI} login --all --refresh-only   (silent, headless)\n"
        f"       {CLI} login --all                  (opens a browser)"
    )


def read_watcher_pid(root) -> int | None:
    """Return the PID of a running watcher, if one holds this project."""
    p = Path(root) / WATCHER_LOCKFILE
    try:
        pid = int(json.loads(p.read_text())["pid"])
    except Exception:
        return None
    try:
        os.kill(pid, 0)  # signal 0 just tests for existence
    except OSError:
        return None
    return pid