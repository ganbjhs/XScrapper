"""
signin.py — ONE way to put a working session on the server, for all three
platforms, without looking like a suspicious login.

--------------------------------------------------------------------------
WHY THIS EXISTS, AND WHY IT IS NOT A BROWSER
--------------------------------------------------------------------------
The server used to sign in by driving a headless Chrome at the platform's
login form. That is the single most-scrutinised page on any of these sites,
and it is where every automation signal is checked at once: the WebDriver
flag, the CDP artefacts, the headless quirks, a profile with no history, and
a datacenter IP that has never seen this account. Instagram's verdict on that
approach is already written into RULEBOOK 6 — "the streamed-browser IG login
is dead" — because the captcha it serves cannot be passed by solving it. The
distrust is about the browser, not about the answer.

The same reasoning applies to X and Facebook; Instagram just reached the wall
first, being the strictest of the three.

So this module removes the login from the server entirely. There are exactly
two mechanisms, and they are ranked by how much suspicion they create:

  1. IMPORT  (works on all three, always preferred)
     You are already signed in, in your own browser, on your own IP, on a
     device the platform has trusted for months. Paste that session's cookies
     here. No login event ever happens from the server: there is no form to
     fingerprint, no captcha to lose, and nothing for a risk engine to score,
     because from the platform's side no authentication took place at all —
     an existing, already-trusted session simply made a request.

  2. BACKGROUND LOGIN  (Instagram only, and only because it is not a browser)
     instagrapi speaks Instagram's mobile-app API with a pinned device
     fingerprint. Instagram trusts that far more than an automated browser,
     which is why `ig_login.py` works where the streamed window never did. It
     costs one real login event, so it is the second choice, not the first —
     but it is the only path that can RE-login by itself when a session dies,
     which is what keeps collection alive without a human.

     X has no equivalent: twscrape's HTTP password login is unusable here for
     four separate reasons documented in auth.py's module docstring. Facebook's
     only non-cookie path is a headless browser at the login form, i.e. exactly
     what this module exists to avoid. For both, IMPORT is the path.

--------------------------------------------------------------------------
THE ONE HONEST RISK, STATED PLAINLY
--------------------------------------------------------------------------
An imported cookie was minted on your home IP and is then used from a
datacenter IP. That is a real signal — a session that crosses continents
between one request and the next. Two things follow, and they are not
optional:

  * INSTAGRAM MUST GO THROUGH ITS RESIDENTIAL PROXY. That is what the proxy
    was always for, and it is why `ig_password` and `ig_cookie` below REFUSE
    to run without one rather than quietly falling back to the server IP.

  * X AND FACEBOOK RUN ON THE SERVER IP, SO CARRY THE WHOLE COOKIE JAR.
    `kdt` (X) and `datr` (Facebook) are device-trust tokens: they are the
    browser saying "this is the machine you already know". Importing only the
    two strictly-required cookies throws them away and turns a known device
    into a new one. `parse_cookies` therefore takes the ENTIRE cookie header
    and keeps everything, and the panel sends the operator's real user-agent
    alongside it, so the fingerprint arriving at the platform is coherent.

    Do NOT "fix" the IP mismatch for X/FB by routing the login through a
    residential proxy and then collecting from the server IP. Two IPs is worse
    than one wrong IP: consistency is what these risk engines actually score.

--------------------------------------------------------------------------
CONTRACT
--------------------------------------------------------------------------
Every function returns an `Outcome`. Nothing here raises for an expected
failure — a rejected cookie, a challenge, a missing proxy are all results, not
exceptions, because every one of them has to reach the operator as a sentence
they can act on (RULEBOOK 6: login walls surface in plain words, never
silently).

`Outcome.needs` is the machine-readable version of "what a human must do now":
    ""          nothing, it worked
    "paste"     the automated path is exhausted; import a cookie instead
    "totp"      a 2FA code is required and none was on file
    "proxy"     refused: this platform may not run on the server IP
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Outcome", "parse_cookies", "ig_password", "ig_cookie",
           "x_cookie", "fb_cookie", "PLATFORM_HELP"]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    ok: bool = False
    identity: str = ""          # the username the PLATFORM reported back
    detail: str = ""            # plain words, shown to the operator verbatim
    needs: str = ""             # "" | "paste" | "totp" | "proxy"
    lines: list = field(default_factory=list)   # the running log

    def as_json(self) -> dict:
        return {"ok": self.ok, "identity": self.identity, "detail": self.detail,
                "needs": self.needs, "lines": list(self.lines)}


class _Log:
    """Collects progress into the Outcome and still prints it to journalctl."""

    def __init__(self, outcome: Outcome, prefix: str):
        self.o, self.prefix = outcome, prefix

    def __call__(self, m):
        line = str(m).strip()
        if line:
            self.o.lines.append(line)
            print(f"[signin:{self.prefix}] {line}", flush=True)


# ---------------------------------------------------------------------------
# Cookie parsing — take whatever the browser gave them
# ---------------------------------------------------------------------------

# The operator is copying from DevTools under time pressure. Every one of these
# is a shape a real person actually ends up with, so accept all of them rather
# than teaching a format:
#   * the Request Headers "cookie:" line          a=1; b=2; c=3
#   * "Copy all as JSON" from the Cookies pane    [{"name":"a","value":"1"},…]
#   * a hand-picked line or two                   auth_token=abc
#   * the Cookies TABLE, pasted                   name<TAB>value<TAB>domain…
_KV = re.compile(r"^\s*([A-Za-z0-9_\-.]+)\s*[=:\t]\s*(.*?)\s*$")


def parse_cookies(blob: str) -> dict:
    """
    Turn any of the above into {name: value}. Never raises; an unparseable blob
    returns {} and the caller reports it as "that does not look like cookies".

    Values are NOT url-decoded: `ct0`, `sessionid` and `xs` all legitimately
    contain % and : and must reach the platform byte-for-byte as the browser
    held them.
    """
    text = (blob or "").strip()
    if not text:
        return {}

    # 1. DevTools "Copy all as JSON"
    if text[0] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if all(isinstance(v, str) for v in data.values()):
                    return {str(k): v for k, v in data.items() if str(k)}
                data = data.get("cookies") or []
            out = {}
            for c in data:
                if isinstance(c, dict) and c.get("name"):
                    out[str(c["name"])] = str(c.get("value", ""))
            if out:
                return out
        except Exception:
            pass

    out: dict[str, str] = {}

    # 2. The `cookie:` header line — one line, semicolon separated.
    line = text
    if line.lower().startswith("cookie:"):
        line = line.split(":", 1)[1]
    if ";" in line and "\n" not in line.strip():
        for part in line.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
        if out:
            return out

    # 3. Line-oriented: `k=v`, `k: v`, or a tab-separated DevTools table row.
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            cells = [c.strip() for c in raw.split("\t")]
            if len(cells) >= 2 and cells[0] and cells[0].lower() != "name":
                out[cells[0]] = cells[1]
                continue
        m = _KV.match(raw)
        if m and m.group(1).lower() != "name":
            out[m.group(1)] = m.group(2)

    # A stray "; " inside one line of a multi-line paste still splits out.
    for k in list(out):
        if ";" in out[k]:
            head, _, rest = out[k].partition(";")
            out[k] = head.strip()
            for part in rest.split(";"):
                if "=" in part:
                    kk, vv = part.split("=", 1)
                    if kk.strip():
                        out[kk.strip()] = vv.strip()
    return {k: v for k, v in out.items() if k and v}


# What each platform needs, and how to get it. Shown in the panel so nobody has
# to remember which cookie is which.
PLATFORM_HELP = {
    "ig": {
        "required": ["sessionid"],
        "valuable": ["ds_user_id", "csrftoken", "mid", "ig_did", "rur"],
        "where": "instagram.com",
        "how": "Open instagram.com in the browser you are already signed into, "
               "DevTools (Cmd+Opt+I) -> Application -> Cookies -> "
               "https://www.instagram.com. Right-click the list, 'Copy all as "
               "JSON', and paste the whole thing. (A single sessionid value "
               "works too, but the full set carries the device tokens.)",
    },
    "x": {
        "required": ["auth_token", "ct0"],
        "valuable": ["kdt", "twid", "guest_id", "personalization_id"],
        "where": "x.com",
        "how": "Open x.com signed in, DevTools -> Application -> Cookies -> "
               "https://x.com. 'Copy all as JSON' and paste it all. `kdt` is "
               "the known-device token — pasting only auth_token and ct0 "
               "throws away the proof that this is a machine X already trusts.",
    },
    "fb": {
        "required": ["c_user", "xs"],
        "valuable": ["datr", "sb", "fr", "wd"],
        "where": "facebook.com",
        "how": "Open facebook.com signed in, DevTools -> Application -> "
               "Cookies -> https://www.facebook.com. 'Copy all as JSON' and "
               "paste it all. `datr` is the device token Facebook uses to "
               "recognise the browser; without it this looks like a new "
               "machine on an unfamiliar IP.",
    },
}


def _missing(platform: str, cookies: dict) -> list:
    return [c for c in PLATFORM_HELP[platform]["required"] if not cookies.get(c)]


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def _ig_guard_proxy(proxy: str, o: Outcome) -> bool:
    """RULEBOOK 6: Instagram never touches the datacenter server IP. Refuse
    rather than fall back — a silent fallback here costs the account."""
    if proxy:
        return True
    o.detail = ("This Instagram account has no residential proxy. Instagram "
                "must never run from the server IP — signing in from it is the "
                "fastest way to lose the account. Add the proxy URL on the "
                "card (Edit -> residential proxy URL), then sign in.")
    o.needs = "proxy"
    return False


def ig_password(login: str, password: str, *, totp_secret: str = "",
                proxy: str = "", label: str = "ig_a", root=".",
                store: str = "ig_accounts.db") -> Outcome:
    """
    Background login through instagrapi's app API. NO BROWSER.

    One attempt, never a loop (RULEBOOK 6). A challenge is NOT auto-answered:
    instagrapi's own handler calls input(), which on a server would hang the
    request forever, so it is replaced with one that declines. A challenge
    therefore ends the attempt and asks for a cookie import instead — which is
    the correct answer anyway, because a challenged account wants to see a
    browser it recognises, and we have one: yours.
    """
    o = Outcome()
    log = _Log(o, login)
    if not _ig_guard_proxy(proxy, o):
        return o
    if not password:
        o.detail = ("No password stored for this account. Add one on the card "
                    "(Edit -> password), or import a cookie instead.")
        o.needs = "paste"
        return o

    import ig_session
    from instagrapi.exceptions import (ChallengeRequired, ClientError,
                                       TwoFactorRequired)

    log(f"signing in as @{login} through {_redact(proxy)}")
    cl = ig_session.new_client(label, proxy=proxy, username=login, log=log)

    # The interactive handler would block a request thread on input() forever.
    cl.challenge_code_handler = lambda username, choice: None

    code = ""
    if totp_secret:
        try:
            code = cl.totp_generate_code(totp_secret)
            log("generated a 2FA code from the stored secret")
        except Exception as e:
            log(f"could not generate a 2FA code: {type(e).__name__}: {e}")

    try:
        ok = cl.login(login, password, verification_code=code)
    except TwoFactorRequired:
        o.detail = ("Instagram asked for a two-factor code and none is on file. "
                    "Add the account's TOTP secret on the card (Edit -> TOTP "
                    "secret) and sign in again, or import a cookie.")
        o.needs = "totp"
        return o
    except ChallengeRequired as e:
        # Write the tombstone so nothing auto-relogins into a locked account.
        try:
            ig_session._note_checkpoint(login, root, log=log)
        except Exception:
            pass
        o.detail = ("Instagram raised a checkpoint — it wants to see a browser "
                    "it recognises, which this server is not. Sign in normally "
                    "in your own browser, clear whatever it asks for, then "
                    f"import the cookie here. ({type(e).__name__})")
        o.needs = "paste"
        return o
    except ClientError as e:
        o.detail = f"Instagram rejected the login: {type(e).__name__}: {e}"
        o.needs = "paste"
        return o
    except Exception as e:
        o.detail = f"Login failed: {type(e).__name__}: {e}"
        o.needs = "paste"
        return o

    if not ok:
        o.detail = ("Instagram did not accept the login, without saying why. "
                    "Import a cookie instead.")
        o.needs = "paste"
        return o

    return _ig_persist(cl, login, label=label, proxy=proxy, root=root,
                       store=store, o=o, log=log,
                       password_env=f"IG_PASSWORD_{label.upper()}")


def ig_cookie(blob: str, *, proxy: str = "", label: str = "ig_a", root=".",
              store: str = "ig_accounts.db") -> Outcome:
    """Adopt a session cookie minted by a browser Instagram already trusts."""
    o = Outcome()
    log = _Log(o, label)
    if not _ig_guard_proxy(proxy, o):
        return o

    cookies = parse_cookies(blob)
    sessionid = cookies.get("sessionid") or (blob or "").strip().strip('"\'')
    if not sessionid or not re.match(r"^\d+(:|%3A)", sessionid):
        o.detail = ("That does not look like an Instagram session. Paste the "
                    "cookies from instagram.com — the `sessionid` value starts "
                    "with digits then ':' or '%3A'.")
        o.needs = "paste"
        return o

    import ig_session
    from instagrapi.exceptions import ClientError

    # Through new_client so the cookie is adopted BY THE ACCOUNT'S PINNED
    # DEVICE. Importing on one device and collecting on another is what was
    # invalidating these sessions.
    cl = ig_session.new_client(label, proxy=proxy, log=log)
    log(f"validating the pasted session through {_redact(proxy)}")
    try:
        if not cl.login_by_sessionid(sessionid):
            o.detail = ("Instagram rejected that sessionid. It has probably "
                        "expired — copy a fresh one from your browser.")
            o.needs = "paste"
            return o
    except ClientError as e:
        o.detail = (f"Instagram rejected the session ({type(e).__name__}). Most "
                    f"often it is stale — re-copy it from a browser where you "
                    f"are signed in right now.")
        o.needs = "paste"
        return o
    except Exception as e:
        o.detail = f"Could not validate the session: {type(e).__name__}: {e}"
        o.needs = "paste"
        return o

    username = getattr(cl, "username", "") or ""
    if not username:
        try:
            username = cl.account_info().username or ""
        except Exception:
            username = ""
    if not username:
        username = "user_" + str(getattr(cl, "user_id", "") or "")

    # No password_env: an imported cookie cannot auto-relogin when it dies.
    return _ig_persist(cl, username, label=label, proxy=proxy, root=root,
                       store=store, o=o, log=log, password_env="")


def _ig_persist(cl, username, *, label, proxy, root, store, o, log,
                password_env) -> Outcome:
    try:
        import ig_session
        ig_session.persist(cl, username, label=label, proxy=proxy,
                           password_env=password_env, store_path=store,
                           root=root, log=log)
    except Exception as e:
        o.detail = f"Signed in, but could not save the session: {type(e).__name__}: {e}"
        return o
    o.ok, o.identity = True, username
    o.detail = (f"@{username} is signed in and saved."
                + (" Auto-relogin is armed." if password_env else
                   " Note: no password on file, so this cannot re-login by "
                   "itself when the cookie expires."))
    log(o.detail)
    return o


# ---------------------------------------------------------------------------
# X
# ---------------------------------------------------------------------------

def x_cookie(blob: str, *, screen_name: str, user_agent: str = "",
             acct_cfg=None, db_accounts="accounts.db") -> Outcome:
    """
    Adopt an X session from pasted cookies.

    This is the ONLY browser-free way into accounts.db, and it reuses
    auth.upsert_session unchanged — which means the safety property that
    matters survives intact: nothing is marked active until validate_http has
    made one real authenticated GraphQL request with these exact cookies
    through this exact proxy. A cookie that looks fine but is dead fails here,
    loudly, instead of much later as an unrelated-looking search error.
    """
    import asyncio

    import auth

    o = Outcome()
    log = _Log(o, screen_name or "x")

    cookies = parse_cookies(blob)
    miss = _missing("x", cookies)
    if miss:
        o.detail = (f"Missing {', '.join(miss)}. " + PLATFORM_HELP["x"]["how"])
        o.needs = "paste"
        return o

    name = (screen_name or "").strip().lstrip("@")
    if not name:
        o.detail = ("The account's login must be the exact @handle — X keys its "
                    "sessions on the username, and nothing downstream can check "
                    "it for you (the validation probe proves the cookies work, "
                    "not whose they are).")
        o.needs = "paste"
        return o

    # An empty or '@'-prefixed UA makes twscrape invent a RANDOM one seeded by
    # the username (http.py:25-33) — a fingerprint that has never been near
    # these cookies. The panel sends the operator's real navigator.userAgent.
    ua = (user_agent or "").strip()
    if not ua or ua.startswith("@"):
        o.detail = ("The browser's user-agent did not arrive with the cookies. "
                    "It has to, or the session collects under a fingerprint "
                    "that has never been associated with it.")
        o.needs = "paste"
        return o

    log(f"kept {len(cookies)} cookies"
        + (" including kdt (known device)" if cookies.get("kdt")
           else " — no kdt, so X will treat this as a new device"))

    h = auth.Harvest(screen_name=name, user_agent=ua, cookies=dict(cookies))
    if not h.has_required:
        o.detail = "auth_token and ct0 are both required and one of them is empty."
        o.needs = "paste"
        return o

    async def save():
        api = auth.open_api(db_accounts)
        return await auth.upsert_session(api, h, acct_cfg)

    try:
        log("proving the cookies authenticate with one real request…")
        username, res = asyncio.run(save())
    except Exception as e:
        o.detail = f"Could not save the session: {type(e).__name__}: {e}"
        return o

    if not res.ok:
        o.detail = (f"X did not accept the session: "
                    f"{res.error or 'validation failed'}. If you copied it "
                    f"minutes ago it may already have been rotated — re-copy "
                    f"and try again.")
        o.needs = "paste"
        return o

    try:
        auth.write_identity(acct_cfg, username)
    except Exception:
        pass
    o.ok, o.identity = True, username
    o.detail = f"@{username} is signed in and validated."
    log(o.detail)
    return o


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

def fb_cookie(blob: str, *, state_path="fb_state.json", root=".") -> Outcome:
    """
    Adopt a Facebook session from pasted cookies.

    Facebook's engine opens its context from a Playwright `storage_state` file
    and applies no schema check of its own (engine_fb.__aenter__), so writing a
    well-formed one is a complete, supported way in — and it takes precedence
    over FB_C_USER/FB_XS, which are only ever read when no state file exists.

    Not validated here: proving it would mean launching the browser, and the
    engine will do that on its next pass anyway and report through the health
    breaker it already has. What IS done is clearing that breaker, because a
    fresh session is exactly the condition its block was waiting on.
    """
    o = Outcome()
    log = _Log(o, "facebook")

    cookies = parse_cookies(blob)
    miss = _missing("fb", cookies)
    if miss:
        o.detail = f"Missing {', '.join(miss)}. " + PLATFORM_HELP["fb"]["how"]
        o.needs = "paste"
        return o

    # Playwright's storage_state shape, exactly. Domain-wide and path '/' so it
    # covers www/m/mbasic alike; far-future expiry because Facebook rotates `xs`
    # itself and a short expiry would drop the session mid-pass.
    expires = int(time.time()) + 365 * 24 * 3600
    state = {"cookies": [{"name": k, "value": v, "domain": ".facebook.com",
                          "path": "/", "expires": expires, "httpOnly": False,
                          "secure": True, "sameSite": "None"}
                         for k, v in cookies.items()],
             "origins": []}

    p = Path(root) / state_path
    try:
        p.write_text(json.dumps(state, indent=2))
        try:
            p.chmod(0o600)      # holds a live session
        except OSError:
            pass
    except Exception as e:
        o.detail = f"Could not write {p}: {type(e).__name__}: {e}"
        return o
    log(f"wrote {len(cookies)} cookies -> {p}")
    if not cookies.get("datr"):
        log("no datr — Facebook will treat this as a browser it has not seen")

    try:
        import engine_fb
        engine_fb.clear_login_block()
        log("cleared the login circuit breaker")
    except Exception as e:
        log(f"could not clear the login breaker: {type(e).__name__}: {e}")

    o.ok, o.identity = True, cookies.get("c_user", "")
    o.detail = ("Facebook session saved. It is proved on the next collection "
                "pass — the engine reports through the health breaker if it "
                "was not accepted.")
    return o


def _redact(proxy: str) -> str:
    """A proxy URL is a credential. Show the host, never the password."""
    if not proxy:
        return "the server IP"
    try:
        import urllib.parse as up
        u = up.urlsplit(proxy)
        host = u.hostname or "?"
        return f"{host}:{u.port}" if u.port else host
    except Exception:
        return "the configured proxy"
