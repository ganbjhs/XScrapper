"""
engine_ig.py — the Instagram scraping seam. Everything instagrapi-specific
lives here, exactly as engine.py holds everything twscrape-specific.

WHAT THIS IS, IN ONE LINE. It is to Instagram what engine.py is to X: a thin
wrapper over a free library that yields PAGES in timeline order, so that
collector.py's watermark poller, the adaptive interval, and the dashboard can
run over Instagram without knowing it is Instagram.

WHY instagrapi AND NOT HAND-ROLLED HTTP. The same reasoning that put twscrape
behind engine.py. instagrapi (MIT, free, actively maintained) already speaks
Instagram's private web API: it loads a harvested session, echoes the
x-ig-www-claim header Instagram demands, carries the device fingerprint, and
centralises 429/challenge/relogin handling. Re-implementing that by hand is how
you turn a small task into a big one. We wrap it and pin it, precisely as
engine.py pins twscrape==0.20.0 and asserts its internals.

HONEST LIMITS — READ BEFORE TRUSTING THIS.
  * NOT YET VERIFIED AGAINST A LIVE ACCOUNT. The call shapes are from
    instagrapi 2.18.12 and were introspected, not exercised against Instagram.
    The `check()` self-test confirms the library still exposes what we call;
    the `__main__` smoke test is what you run once with a real session to prove
    pagination actually works. Same discipline as ig.py.
  * NO SCRAPER IS BAN-PROOF. instagrapi's own docs say it plainly: "account
    trust, proxies, device state, challenges and rate limits can change
    independently of the library." A private-API tool CAN break the way
    snscrape did when the platform shifts, and accounts CAN be limited. A
    stable device (profiles/, IG1) + one proxy per account + a gentle interval
    is mitigation, never immunity. guard.py is where that risk is surfaced.

WHAT collector.py STILL NEEDS to drive this (deliberately NOT done here, and
written down rather than left to be discovered — the R6 rule):
  1. A watermark keyed on `taken_at` (a unix second), not on X's snowflake
     epoch. IG media `pk`s ARE time-monotonic, so the numeric "am I below the
     watermark" compare in collector.poll_once still holds; only the LAG clock
     changes. Every IGPage carries `taken_at` per result for exactly this.
  2. An Instagram results store. store.py is tweet-shaped (results.db). This
     module hands back normalised dicts (see `Media.record()`) ready for such a
     store; wiring one is the last step and is intentionally out of scope so
     the working X store is never put at risk.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# instagrapi is synchronous (requests under the hood). Every call it makes is
# wrapped in asyncio.to_thread below so the single-loop server that runs the
# collector is never blocked while a page is in flight.
PINNED_VERSION = "2.18.12"

# Instagram's public web app id — the same constant ig.py already sends. Not a
# secret; instagrapi sets its own, this is only documented here for parity.
WEB_APP_ID = "936619743392459"


# ==========================================================================
# compatibility — assert the instagrapi surface we depend on
# ==========================================================================
#
# Mirrors engine.check(): an accidental instagrapi upgrade that renames or
# drops a method we call should fail LOUDLY here, not silently return nothing.

@dataclass
class Report:
    ok: bool = True
    lines: list = field(default_factory=list)

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        self.lines.append(("OK   " if cond else "BROKEN ") + label
                           + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            self.ok = False


def check() -> Report:
    r = Report()
    try:
        import instagrapi
        from instagrapi import Client
        from instagrapi.extractors import extract_media_v1
    except Exception as e:
        r.check("instagrapi is importable", False, f"{type(e).__name__}: {e}")
        return r

    try:
        from importlib.metadata import version as _v
        found = _v("instagrapi")
    except Exception:
        found = "?"
    r.check(
        f"instagrapi is the pinned {PINNED_VERSION} (found {found})",
        found == PINNED_VERSION,
        "re-run this after every upgrade; the checks below are what to verify",
    )

    # The session bootstrap and the fingerprint setters.
    for name in ("login_by_sessionid", "set_proxy", "set_user_agent",
                 "set_settings", "get_timeline_feed",
                 "user_medias_paginated_v1", "hashtag_medias_recent_v1",
                 "user_id_from_username", "search_users_v1",
                 "user_following_v1"):
        r.check(f"Client.{name}() exists", callable(getattr(Client, name, None)))

    r.check("extract_media_v1() is importable (our timeline parser needs it)",
            callable(extract_media_v1))
    return r


# ==========================================================================
# the Page — duck-identical to engine.Page, on purpose
# ==========================================================================
#
# NOT imported from engine.py, and that is deliberate: importing engine pulls
# twscrape into the Instagram collection path for no reason (import weight on a
# small box is exactly what this project trims). This carries the same fields
# collector.py reads off a page, so the poller cannot tell the two apart.

@dataclass
class IGPage:
    page_no: int
    received_ts: float
    server_ts: float | None = None
    account: str | None = None
    status: int = 200

    # Instagram publishes no per-request budget header (unlike X's
    # x-rate-limit-*). These stay None; guard.py budgets IG from OUR OWN
    # request count in the window instead — see guard._budget's recent_requests,
    # which becomes the primary signal here rather than a cross-check.
    rl_limit: int | None = None
    rl_remaining: int | None = None
    rl_reset: int | None = None

    # The authoritative, ordered result set: media pks, newest first.
    result_ids: list = field(default_factory=list)
    entries_by_id: dict = field(default_factory=dict)   # pk -> normalised record
    tweets: dict = field(default_factory=dict)          # pk -> normalised record (name kept for parity)
    taken_at: dict = field(default_factory=dict)        # pk -> unix seconds (the IG lag clock)

    cursor: str | None = None
    raw: dict = field(default_factory=dict)
    parse_failures: list = field(default_factory=list)

    @property
    def collected_ms(self) -> int:
        ts = self.server_ts if self.server_ts is not None else self.received_ts
        return int(ts * 1000)

    @property
    def orphan_ids(self) -> list:
        return [i for i in self.result_ids if i not in self.tweets]

    @property
    def embedded_ids(self) -> list:
        # Instagram feeds have no quoted-tweet analogue; there is no embedded
        # context to keep separate. Present so collector.py's loop is happy.
        return []

    def min_result_id(self):
        return min(self.result_ids) if self.result_ids else None

    def max_result_id(self):
        return max(self.result_ids) if self.result_ids else None

    def lag_ms(self, pk: int) -> int | None:
        """Freshness for one post: now (server clock) minus when it was posted.

        This REPLACES store.lag_ms for Instagram. IG media carry taken_at
        directly, so there is no snowflake epoch to decode — and running an IG
        pk through X's snowflake math would produce nonsense.
        """
        t = self.taken_at.get(pk)
        return None if t is None else max(0, self.collected_ms - int(t) * 1000)


# ==========================================================================
# normalising one media object
# ==========================================================================

def _epoch(taken_at) -> int:
    """instagrapi gives taken_at as a datetime; the store wants a unix second."""
    if isinstance(taken_at, datetime):
        return int(taken_at.replace(tzinfo=taken_at.tzinfo or timezone.utc).timestamp())
    try:
        return int(taken_at)
    except (TypeError, ValueError):
        return 0


def record(media) -> dict:
    """
    Flatten one instagrapi Media into the small, self-contained row a future
    Instagram results store would keep. Only the fields worth persisting — the
    raw Media carries image-candidate ladders and nested user objects that are
    ~all of its size and that nothing reads, the IG echo of config.toml's
    keep_entry_json = false.
    """
    u = getattr(media, "user", None)
    pk = int(getattr(media, "pk", 0) or 0)
    code = getattr(media, "code", "") or ""
    return {
        "pk": pk,
        "code": code,
        "url": f"https://www.instagram.com/p/{code}/" if code else "",
        "taken_at": _epoch(getattr(media, "taken_at", 0)),
        "media_type": getattr(media, "media_type", None),
        "product_type": getattr(media, "product_type", "") or "",
        "caption": getattr(media, "caption_text", "") or "",
        "like_count": getattr(media, "like_count", None),
        "comment_count": getattr(media, "comment_count", None),
        "play_count": getattr(media, "play_count", None),
        "video_url": str(getattr(media, "video_url", "") or ""),
        "thumbnail_url": str(getattr(media, "thumbnail_url", "") or ""),
        "user_pk": int(getattr(u, "pk", 0) or 0) if u else 0,
        "username": getattr(u, "username", "") or "" if u else "",
        # The author's profile picture, exactly as the media row carried it.
        # instagrapi's UserShort hands it over with every post, so keeping it
        # costs no request (RULEBOOK §6: structured data or nothing — never a
        # fetch made just for a picture, never a pixel read off a rendered
        # page). Dropped here until 2026-09-04, which is why every Instagram
        # post sat with a blank circle unless its handle matched an X account.
        "author_avatar": (str(getattr(u, "profile_pic_url", "") or "")
                          if u else ""),
    }


def _page_from_media(media_list, page_no, cursor, account, raw=None) -> IGPage:
    page = IGPage(page_no=page_no, received_ts=time.time(),
                  server_ts=time.time(), account=account,
                  cursor=cursor or None, raw=raw or {})
    for m in media_list:
        try:
            rec = record(m)
        except Exception as e:
            page.parse_failures.append((str(getattr(m, "pk", "?")),
                                        f"{type(e).__name__}: {e}"))
            continue
        pk = rec["pk"]
        if not pk or pk in page.entries_by_id:
            continue
        page.result_ids.append(pk)
        page.entries_by_id[pk] = rec
        page.tweets[pk] = rec
        page.taken_at[pk] = rec["taken_at"]
    return page


# ==========================================================================
# session -> authenticated client
# ==========================================================================

def build_client(cookies: dict, user_agent: str, proxy: str = "", log=lambda m: None,
                 *, label: str = "ig_a", root="."):
    """
    Turn a harvested Instagram session (ig.Session.cookies + the real browser
    user-agent) into an authenticated instagrapi Client.

    The DEVICE, user-agent and proxy are pinned to what the account has always
    used, for the same reason auth.validate_http reuses acc.make_client: the
    HTTP client that collects must look like the one that logged in, or
    Instagram reads the change of device as a stolen session (IG1).

    The pinning is not done here — ig_session.new_client owns it, so there is a
    single place where a device is chosen. See "THE DEVICE SEED" in that module
    for why a fresh Client() per login was invalidating these sessions.
    """
    import ig_session

    sessionid = cookies.get("sessionid", "")
    if not sessionid:
        raise RuntimeError("no sessionid in the stored session")

    cl = ig_session.new_client(label, proxy=proxy, root=root, log=log)

    # Only fall back to the stored user-agent when no seed has supplied one. A
    # seeded client's user-agent is DERIVED from its device_settings, and
    # overriding one without the other is precisely the mismatch (a Pixel 8 Pro
    # claiming to be some other handset) that gets a session flagged.
    if user_agent and not user_agent.startswith("@") and not cl.settings.get("user_agent"):
        cl.set_user_agent(user_agent)

    # Canonical, minimal login. login_by_sessionid replaces settings["cookies"]
    # and re-runs init(), which re-reads settings["uuids"] — so the seeded
    # device SURVIVES this call. (An earlier version pre-loaded a partial
    # settings dict of cookies + authorization_data with no uuids, which made
    # instagrapi redirect-loop into the login page; a full device seed with no
    # cookies is the opposite case and is what login_by_sessionid expects.)
    cl.login_by_sessionid(sessionid)
    return cl


def client_from_store(store, username: str, proxy: str = "", log=lambda m: None):
    """Load a stored ig session by username and build its client.

    If the exact username is not present, the error names what IS in the store
    (or that it is empty) — R6, say what you saw. The commonest cause is that
    the account was never signed in through the dashboard, or that Instagram
    stored a different handle than the one typed.
    """
    row = store.get(username)
    if row is None:
        have = [a.get("username") or "(no handle)" for a in store.all()]
        if not have:
            raise RuntimeError(
                "the Instagram session store is empty — no account has been "
                "signed in yet. Open the dashboard, Instagram accounts panel, "
                "press 'Sign in to Instagram' and finish the login.")
        raise RuntimeError(
            f"no stored session for @{username}. Stored handles: {', '.join(have)}. "
            "Re-run with one of those (Instagram keys the row by the handle it "
            "detects at sign-in, which may differ from the label you typed).")
    import json
    cookies = json.loads(row["cookies"] or "{}")
    return build_client(cookies, row["user_agent"], proxy or (row.get("proxy") or ""), log,
                        label=row["label"] or "ig_a")


# ==========================================================================
# username -> numeric id, the two fallbacks
# ==========================================================================
#
# Both of these deliberately reuse the LOGGED-IN client's cookies, proxy and
# user-agent rather than opening a clean connection. A lookup that leaves from
# a different IP, or carries a different fingerprint, than the session it claims
# to belong to is a louder signal than the lookup itself — same reasoning as
# build_client pinning the device (IG1).

def _browser_session(cl):
    """A requests.Session that looks like this account's browser tab.

    Cookies and proxy come from the live client, so the request exits the same
    way every other request from this account does.
    """
    import requests

    sess = requests.Session()
    for src in ("private", "public"):
        jar = getattr(getattr(cl, src, None), "cookies", None)
        if jar:
            sess.cookies.update(jar)
    try:
        sess.cookies.update(cl.cookie_dict or {})
    except Exception:
        pass
    proxies = (getattr(getattr(cl, "private", None), "proxies", None)
               or getattr(getattr(cl, "public", None), "proxies", None))
    if proxies:
        sess.proxies.update(proxies)

    # The web endpoint wants a WEB user-agent — and it must be THIS PHONE's
    # browser, not a Mac's. A seeded client carries the identity block
    # (ig_identity, spliced in by ig_session.new_client): the same mobile
    # Chrome string and Client Hints the streamed sign-in window presented.
    # Before 2026-09-04 these calls left as desktop Chrome on a Mac from an
    # Android app session, and instagram.com bounced them to the login page
    # (TooManyRedirects) every time.
    try:
        settings = cl.settings or {}
    except Exception:
        settings = {}
    dev = {k: settings.get(k) for k in ("web_user_agent", "identity", "device_settings")}
    if dev.get("identity"):
        import ig_identity
        headers = ig_identity.web_headers(dev)
    else:
        ua = settings.get("web_user_agent") or ""
        if "Mozilla" not in ua:
            ua = getattr(cl, "user_agent", "") or ""
        if "Mozilla" not in ua:
            ua = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36")
        headers = {"User-Agent": ua, "Accept-Language": "en-IN,en;q=0.9"}
    sess.headers.update({
        **headers,
        "Accept": "*/*",
        "X-IG-App-ID": WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
    })
    csrf = sess.cookies.get("csrftoken")
    if csrf:
        sess.headers["X-CSRFToken"] = csrf
    return sess


class ResolveError(RuntimeError):
    """A username could not be turned into a numeric id. `why` says what kind
    of refusal it was, so the collector can decide how long to leave it:

        rate_limited   429 / "please wait" — Instagram is throttling LOOKUPS
                       from this session. Asking again sooner makes it last
                       longer. Leave it for hours, not minutes.
        blocked        the web side bounced us to the login page or answered
                       401/403 — the signature of a datacenter IP or a session
                       instagram.com does not accept. A proxy problem, not a
                       handle problem.
        not_found      404 — the handle does not exist (typo, renamed, banned).
                       No retry will ever help; a human fixes the handle.
        tls_intercepted
                       the TLS handshake to instagram.com failed certificate
                       verification — "unable to get local issuer certificate".
                       Instagram never answered; something BETWEEN us and it
                       (a Sophos/ISP firewall on the proxy's residential exit)
                       is re-signing HTTPS. The PROXY EXIT is unusable, for
                       every request from this account, not just lookups.
        network        the connection died before any HTTP reply (proxy
                       refused / unreachable / reset / connect timeout). Same
                       conclusion: nothing from this session reaches
                       Instagram, so no handle can ever resolve until the
                       proxy is fixed.
        unknown        everything else.

    tls_intercepted and network are ACCOUNT conditions wearing a per-handle
    error: the collector folds them into one 'proxy_broken' card instead of
    N "handle needs its numeric id" cards that all say the same thing.
    """

    def __init__(self, msg: str, why: str = "unknown", attempts=()):
        super().__init__(msg)
        self.why = why
        self.attempts = list(attempts)


# Exception class names that mean "the request never reached Instagram" —
# requests', urllib3's and instagrapi's names for a dead pipe. Matched by NAME
# so decider.py (which has the same list) never needs these libraries
# importable.
_NETWORK_EXC = frozenset((
    "ClientConnectionError",     # instagrapi's wrapper around requests.ConnectionError
    "ConnectionError", "ProxyError", "ConnectTimeout", "ConnectTimeoutError",
    "NewConnectionError", "MaxRetryError", "ProtocolError",
    "RemoteDisconnected", "ConnectionResetError", "ConnectionRefusedError",
))
_TLS_EXC = frozenset(("SSLError", "SSLCertVerificationError", "CertificateError"))


def network_why(e) -> str:
    """'tls_intercepted', 'network', or '' when `e` is an actual HTTP answer.

    Text first, then the class name: instagrapi wraps a certificate failure
    as ClientConnectionError("SSLError HTTPSConnectionPool(...) certificate
    verify failed: unable to get local issuer certificate"), so the class
    alone would file a MITM'd proxy under plain 'network' and lose the one
    detail that tells the operator what to do."""
    text = str(e).lower()
    if ("certificate verify failed" in text or "unable to get local issuer" in text
            or "self signed certificate" in text or "self-signed certificate" in text
            or "certificate_verify_failed" in text
            or "hostname mismatch" in text):
        return "tls_intercepted"
    name = type(e).__name__
    if name in _TLS_EXC or "sslerror" in text or "ssl: " in text:
        return "tls_intercepted"
    if name in _NETWORK_EXC:
        return "network"
    # Text, for wrapped exceptions (instagrapi's ClientConnectionError carries
    # the urllib3 cause in its message). NOT "max retries exceeded" on its own:
    # requests.RetryError says that too, and that one is three 429s — an
    # answer from Instagram, not a dead pipe.
    if ("caused by newconnectionerror" in text or "caused by proxyerror" in text
            or "caused by connecttimeouterror" in text or "caused by protocolerror" in text
            or "connection refused" in text or "connection reset" in text
            or "tunnel connection failed" in text or "cannot connect to proxy" in text
            or "name or service not known" in text or "connection aborted" in text
            or "remote end closed connection" in text):
        return "network"
    return ""


# The advice the operator sees for each network `why` — on the Fix panel, in
# the Telegram ping, and in the exception text. One place, so all three agree.
NETWORK_ADVICE = {
    "tls_intercepted": (
        "Nothing from this account reaches Instagram: the HTTPS handshake "
        "fails certificate verification (\"unable to get local issuer "
        "certificate\"). That is the proxy EXIT intercepting TLS — a Sophos / "
        "ISP firewall on that residential IP re-signs every connection, and "
        "the server rightly refuses the forged certificate. Not a handle "
        "problem, not a throttle: change the proxy exit (Accounts & Sessions "
        "→ Edit → proxy, another session number) and verify with "
        "`curl -x '<proxy url>' -sS -o /dev/null -w '%{http_code}\\n' "
        "https://www.instagram.com/` — it must print 200 or 302 without -k."),
    "network": (
        "Nothing from this account reaches Instagram: the connection dies "
        "before any reply (proxy refused / unreachable / reset). Not a handle "
        "problem — check the proxy URL, its credentials and that the provider "
        "still lists that endpoint (Accounts & Sessions → Edit → proxy), then "
        "verify with `curl -x '<proxy url>' -sS -o /dev/null -w "
        "'%{http_code}\\n' https://www.instagram.com/`."),
}


def _status_of(e) -> int:
    """HTTP status behind a requests/instagrapi exception, or 0."""
    import re
    for attr in ("response", "last_response"):
        r = getattr(e, attr, None)
        code = getattr(r, "status_code", None)
        if code:
            return int(code)
    # requests: "429 Client Error: Too Many Requests for url"; instagrapi
    # often carries "status_code=429" / "status 429" in the text.
    m = re.search(r"\b(4\d\d|5\d\d) (?:Client|Server) Error"
                  r"|status(?:_code| code)?\D{0,3}(4\d\d|5\d\d)", str(e))
    return int(m.group(1) or m.group(2)) if m else 0


def _why(e) -> str:
    name = type(e).__name__
    text = str(e).lower()
    code = _status_of(e)
    # A dead pipe first: no status code, no Instagram answer, so none of the
    # HTTP-shaped tests below can apply. (A verify-failed TLS handshake
    # inside a RetryError/ConnectionError must not be read as a 429.)
    net = network_why(e)
    if net and not code:
        return net
    if name == "RetryError" or code == 429 or "please wait" in text \
            or "too many requests" in text:
        # requests.RetryError is urllib3 giving up after instagrapi's adapter
        # re-sent a 429 three times — i.e. three 429s in a row.
        return "rate_limited"
    if name == "TooManyRedirects" or code in (401, 403) or "login_required" in text:
        return "blocked"
    if code == 404 or name == "UserNotFound" or "user not found" in text:
        return "not_found"
    return "unknown"


class _no_retries:
    """instagrapi mounts a urllib3 Retry on the private session that re-sends
    a request up to three times on 429 (0s, 2s, 4s apart). For a media read
    that is arguably fine; for a NAME LOOKUP it is the worst possible reply
    to "please wait" — three more knocks in six seconds, from a session that
    was just told to stop. Lookups run with retries off and the setting is
    restored afterwards, whatever happens."""

    def __init__(self, cl):
        self.cl = cl
        self.saved = None

    def __enter__(self):
        try:
            self.saved = self.cl.session_retry_total
            self.cl.session_retry_total = 0
            self.cl._configure_private_session_retry()
        except Exception:
            self.saved = None
        return self

    def __exit__(self, *a):
        if self.saved is not None:
            try:
                self.cl.session_retry_total = self.saved
                self.cl._configure_private_session_retry()
            except Exception:
                pass


def _pk_via_search(cl, name: str):
    """users/search/ — a different private endpoint from usernameinfo, gated
    separately, and it answers with pk + username for every match. Exact
    match on the username only; a search is not a lookup."""
    for u in cl.search_users_v1(name, 10) or []:
        if str(getattr(u, "username", "")).lower() == name.lower():
            return str(getattr(u, "pk", "") or "")
    return None


def _pk_via_web_profile_info(cl, name: str, timeout: int = 15):
    """The endpoint instagram.com hits when you open a profile in a tab.

    Gated separately from the app API — this is the path that usually still
    answers on a session where user_id_from_username returns login_required.
    Returns the numeric pk as a string, or None if the payload has no id.
    """
    sess = _browser_session(cl)
    r = sess.get("https://www.instagram.com/api/v1/users/web_profile_info/",
                 params={"username": name},
                 headers={"Referer": f"https://www.instagram.com/{name}/"},
                 timeout=timeout)
    r.raise_for_status()
    user = ((r.json() or {}).get("data") or {}).get("user") or {}
    pk = user.get("id") or user.get("pk")
    return str(pk) if pk else None


def _pk_via_profile_html(cl, name: str, timeout: int = 15):
    """Last resort: load the profile page and read the id out of the markup.

    Instagram does not always inline it any more, so this is genuinely a
    fallback — but when it IS there it costs one ordinary page load and no
    API surface at all.
    """
    import re

    sess = _browser_session(cl)
    sess.headers["Accept"] = ("text/html,application/xhtml+xml,application/xml;"
                              "q=0.9,*/*;q=0.8")
    sess.headers.pop("X-Requested-With", None)
    r = sess.get(f"https://www.instagram.com/{name}/", timeout=timeout)
    r.raise_for_status()
    for pat in (r'"profile_id"\s*:\s*"(\d+)"',
                r'"owner"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"',
                r'"user_id"\s*:\s*"(\d+)"'):
        m = re.search(pat, r.text)
        if m:
            return m.group(1)
    return None


# ==========================================================================
# the engine
# ==========================================================================

class IGEngine:
    """
    Thin wrapper over an authenticated instagrapi Client. Swap this to change
    the transport, exactly as engine.Engine wraps twscrape's API.

    Every generator yields IGPage objects and MUST be consumed the way
    collector.py already consumes engine.py's — the caller iterates and may
    break early. There is no 15-minute account lock here (that was a twscrape
    behaviour), so aclosing is not load-bearing the way it is on X; it remains
    good hygiene and costs nothing.
    """

    def __init__(self, client, account: str | None = None, on_resolved=None):
        self.cl = client
        self.account = account
        self._pk_cache: dict = {}
        self._following: dict = {}      # username.lower() -> pk, see below
        # Called as on_resolved(handle, numeric_pk) the moment a name is
        # resolved. The collector points this at store_ig.cache_platform_id so
        # the lookup is paid ONCE, ever — not once per process. Without it the
        # cache below dies with the engine, which is why a restarting service
        # re-resolves (and re-fails) the same names on every single pass.
        self.on_resolved = on_resolved

    def resolve_user(self, who) -> str:
        """
        Turn 'natgeo' into '787132'. Numeric input is returned untouched.

        WHY THIS IS ITS OWN STEP, AND WHY IT CAN FAIL WHERE FETCHING DOES NOT.
        The media endpoint takes a numeric pk; only lookup takes a name. Those
        are different permissions, and measured on a live restricted session
        they came apart completely: user_medias_paginated_v1('787132') returned
        posts while user_id_from_username('natgeo'), user_info_by_username_v1
        and search_users ALL returned login_required or 400. So a source
        configured by name can be unfetchable while the very same source
        configured by id collects perfectly.

        THE ANSWER IS NOT "STOP USING NAMES". A name is the only part of this
        that a human can read, and in this system the label above it is the
        cross-platform identity key. So: keep the name, resolve it ONCE, and
        persist the answer next to it (store_ig.sources.platform_id). After the
        first success this method never touches the network again for that
        handle, on this process or any future one.

        Three ways in, cheapest and most likely first. They fail independently,
        which is the entire reason there is more than one:

          1. instagrapi's private user_id_from_username — the app-flavoured
             endpoint. First choice on a healthy session; the first thing
             withdrawn under a checkpoint.
          2. the WEB endpoint api/v1/users/web_profile_info, with the browser
             X-IG-App-ID. This is what instagram.com itself calls when you open
             a profile in a tab, and it is gated separately from the app API —
             it routinely answers on exactly the sessions where (1) returns
             login_required. This is usually the one that saves a restricted
             account.
          3. the profile HTML, scraped for "profile_id". Least reliable (the id
             is not always inlined any more) and last for that reason, but it
             costs one ordinary page load and sometimes it is simply there.

        If all three refuse, the error says so and points at `set-id`, because
        at that point the fix is one number typed by a human, not a new login.
        """
        s = str(who).strip()
        if s.isdigit():
            return s
        # Key the cache on the NORMALISED name, so 'natgeo' and '@natgeo' are
        # one entry and not two lookups.
        name = s.lstrip("@")
        if name in self._pk_cache:
            return self._pk_cache[name]

        # The following list first: if this account follows the target, one
        # request that was probably already paid for this pass answers with
        # no lookup endpoint touched at all (see resolve_from_following).
        if name.lower() in self._following:
            pk = self._following[name.lower()]
            self._pk_cache[name] = pk
            if self.on_resolved:
                try:
                    self.on_resolved(name, pk)
                except Exception:
                    pass
            return pk

        attempts, pk, whys = [], None, []
        with _no_retries(self.cl):
            for how, fn in (("private usernameinfo",
                             lambda: self.cl.user_id_from_username(name)),
                            ("private users/search",
                             lambda: _pk_via_search(self.cl, name)),
                            ("web_profile_info",
                             lambda: _pk_via_web_profile_info(self.cl, name)),
                            ("profile HTML",
                             lambda: _pk_via_profile_html(self.cl, name))):
                try:
                    got = fn()
                except Exception as e:
                    why = _why(e)
                    code = _status_of(e)
                    attempts.append(f"{how}: {type(e).__name__}"
                                    + (f" {code}" if code else ""))
                    whys.append(why)
                    if why in ("rate_limited", "not_found",
                               "tls_intercepted", "network"):
                        # A 429 on one lookup endpoint is a 429 on the next —
                        # they share the limiter. A 404 is an answer, not an
                        # outage. And a pipe that cannot complete a TLS
                        # handshake (or connect at all) is the same pipe for
                        # the next three endpoints. Either way: stop knocking.
                        break
                    continue
                if got and str(got).isdigit():
                    pk = str(got)
                    break
                attempts.append(f"{how}: no id in the response")
                whys.append("unknown")

        if pk is None:
            why = ("tls_intercepted" if "tls_intercepted" in whys
                   else "network" if "network" in whys
                   else "rate_limited" if "rate_limited" in whys
                   else "not_found" if "not_found" in whys
                   else "blocked" if whys and all(w == "blocked" for w in whys)
                   else "unknown")
            advice = {
                **NETWORK_ADVICE,
                "rate_limited": (
                    "Instagram is throttling name lookups from this session "
                    "(429). It clears by itself if left alone for hours; asking "
                    "again sooner extends it. The other sources still collect."),
                "blocked": (
                    "instagram.com refused this session's web requests (login "
                    "bounce / 401 / 403). That is what a datacenter IP looks "
                    "like to Instagram — give this account its residential "
                    "proxy (Accounts & Sessions → Edit)."),
                "not_found": (
                    f"Instagram says there is no user '{name}' (404). Check the "
                    f"spelling in Watchlists — a retry cannot fix a typo."),
                "unknown": (
                    "Instagram gates name lookup separately from media reads, "
                    "so this can fail on a session that fetches fine."),
            }[why]
            if why in NETWORK_ADVICE:
                # Pasting an id would not help: the media fetch leaves through
                # the same dead pipe. Say so instead of sending the operator
                # to view-source for eight handles.
                tail = ("  A pasted id would NOT help — post reads go through "
                        "the same proxy. Fix the proxy, then Retry.")
            else:
                tail = (f"  Fix by hand, ONCE (the id never changes): open "
                        f"https://www.instagram.com/{name}/ , view source, search for "
                        f"\"profile_id\", paste it on the Fix panel or run "
                        f"`python3 collect_ig.py set-id --label <label> --id <numeric_id>`. "
                        f"Or follow @{name} from the collecting account — the next pass "
                        f"reads the id off the following list, no lookup needed.")
            raise ResolveError(
                f"could not resolve the username '{name}' to a numeric id. "
                f"{advice}\n"
                f"  tried — {'; '.join(attempts)}\n"
                f"{tail}",
                why=why, attempts=attempts)

        self._pk_cache[name] = pk
        if self.on_resolved:
            # Never let a caching failure break a fetch that just succeeded.
            try:
                self.on_resolved(name, pk)
            except Exception:
                pass
        return pk

    def resolve_from_following(self, names) -> dict:
        """Fill ids for every `names` entry this account FOLLOWS, in one
        request, touching no lookup endpoint.

        This is the permanent answer to "why does it fail to resolve every
        time". The lookup endpoints (usernameinfo, users/search, the web
        profile call) are the most tightly limited calls Instagram has, and a
        session that gets a 429 on them and asks again a few minutes later
        keeps the 429 alive indefinitely. The following list is a normal feed
        call, pays one request for every followed account at once, and is
        exactly what a human account looks like: it follows the people it
        reads. So: follow the sources from the collecting account, and ids
        resolve themselves without ever hitting the limiter.

        Returns {username: pk} for the names it could fill. Never raises —
        a failure here just means the per-name paths run as before.
        """
        wanted = {str(n).lstrip("@").lower() for n in names if not str(n).isdigit()}
        if not wanted:
            return {}
        try:
            me = str(getattr(self.cl, "user_id", "") or "")
            rows = self.cl.user_following_v1(me, amount=0) if me else []
        except Exception:
            return {}
        found = {}
        for u in rows or []:
            un = str(getattr(u, "username", "") or "").lower()
            pk = str(getattr(u, "pk", "") or "")
            if un and pk.isdigit():
                self._following[un] = pk
                if un in wanted:
                    found[un] = pk
                    self._pk_cache[un] = pk
                    if self.on_resolved:
                        try:
                            self.on_resolved(un, pk)
                        except Exception:
                            pass
        return found

    # -- one target account -------------------------------------------------
    def _user_feed_page(self, user_id: str, count: int, max_id: str = ""):
        """One page of feed/user/<pk>/ — what instagrapi's
        user_medias_paginated_v1 does, minus its `except Exception: return
        [], None`.

        That swallow is why a dead proxy used to look like a quiet account:
        every pass logged `new=0 (had watermark=no)`, the pool stamped "last
        success" on an account that had not stored a post in days, and the
        only visible symptom was eight per-handle "needs its numeric id"
        cards. A connection that never reaches Instagram is an ERROR and is
        raised as one (ClientConnectionError), so the collector can name the
        proxy and stop the pass. Instagram's own refusals (PrivateError
        subclasses: login_required, checkpoint, please wait) came through
        before and still do.

        A client without private_request (the test fakes) falls back to the
        library call unchanged.
        """
        pr = getattr(self.cl, "private_request", None)
        if not callable(pr):
            return self.cl.user_medias_paginated_v1(str(user_id), count, max_id)
        from instagrapi.extractors import extract_media_v1
        data = pr(f"feed/user/{int(user_id)}/",
                  params={"max_id": max_id or "", "count": int(count),
                          "min_timestamp": None,
                          "rank_token": self.cl.rank_token,
                          "ranked_content": "true"})
        items = (data or {}).get("items") or []
        next_max_id = (getattr(self.cl, "last_json", None) or {}).get("next_max_id", "") \
            or (data or {}).get("next_max_id", "")
        return [extract_media_v1(m) for m in items], next_max_id

    async def user_pages(self, user_id, *, page_size: int = 12,
                         max_pages: int = 0, cursor: str | None = None):
        """
        Yield IGPages for ONE user's feed, newest first — the natural fit for
        the watermark poll. Uses the paginated v1 endpoint, which hands back a
        real end_cursor so a walk can resume mid-stream.

        Accepts a numeric pk or a username; see resolve_user for why the two are
        not interchangeable as far as Instagram is concerned.
        """
        user_id = await asyncio.to_thread(self.resolve_user, user_id)
        page_no = 0
        end_cursor = cursor or ""
        while True:
            page_no += 1
            medias, end_cursor = await asyncio.to_thread(
                self._user_feed_page, str(user_id), page_size, end_cursor)
            yield _page_from_media(medias, page_no, end_cursor, self.account)
            if not medias or not end_cursor:
                return
            if max_pages and page_no >= max_pages:
                return

    # -- everyone the account follows, in one request -----------------------
    async def timeline_pages(self, *, page_size: int = 12,
                            max_pages: int = 0, cursor: str | None = None):
        """
        Yield IGPages from the HOME feed — posts from every account this login
        follows, in a single request per page.

        This is the Instagram twin of config.toml's "watch an X List, not N
        searches" decision: follow the accounts you want and one poll covers
        all of them, instead of one request per target. It is the biggest
        single load win available here.

        Caveat, stated because it is easy to forget: the home feed is RANKED,
        not strictly reverse-chronological. The numeric watermark still stops
        the walk correctly (older pk => below the mark), but a re-ranked older
        post can surface above the mark, so dedup by pk in the store is what
        keeps the result set clean — the poller already dedups, so this is
        handled, not ignored.
        """
        page_no = 0
        max_id = cursor or None
        reason = "cold_start_fetch"
        while True:
            page_no += 1
            data = await asyncio.to_thread(
                self.cl.get_timeline_feed, reason, max_id)
            reason = "pagination"
            medias = _timeline_media(data)
            max_id = data.get("next_max_id")
            page = _page_from_media(medias, page_no, max_id, self.account, raw={})
            page.status = 200
            yield page
            if not medias or not max_id:
                return
            if max_pages and page_no >= max_pages:
                return

    # -- a hashtag ----------------------------------------------------------
    async def hashtag_pages(self, name: str, *, page_size: int = 27,
                           max_pages: int = 0, cursor: str | None = None):
        """
        Yield IGPages for a hashtag's recent media.

        WEAKEST source, flagged like list_pages flags its trade-off. Instagram
        ranks and truncates hashtag results and the v1 recent endpoint exposes
        no stable cursor, so this returns a single page and cannot page deep.
        Treat it as a periodic sweep, not a freshness stream; the store's dedup
        is what makes repeated sweeps cheap.
        """
        medias = await asyncio.to_thread(
            self.cl.hashtag_medias_recent_v1, name.lstrip("#"), page_size)
        yield _page_from_media(medias, 1, None, self.account)

    # -- dispatch: keep the source choice in ONE place ----------------------
    def pages_for(self, stream, **kw):
        """
        Route a stream to the right generator, mirroring engine.pages_for.

        A stream selects its source by which attribute it sets:
          * following = true  -> the home feed (one request, many accounts)
          * user_id / user    -> that account's feed
          * hashtag / tag     -> a hashtag sweep
        """
        kw.pop("tab", None)   # Instagram has no product tab
        if getattr(stream, "following", False):
            return self.timeline_pages(**kw)
        uid = getattr(stream, "user_id", None) or getattr(stream, "user", None)
        if uid:
            return self.user_pages(uid, **kw)
        tag = getattr(stream, "hashtag", None) or getattr(stream, "tag", None)
        if tag:
            return self.hashtag_pages(tag, **kw)
        raise ValueError(
            f"stream {getattr(stream, 'label', '?')} names no Instagram source "
            "(set following=true, or a user_id, or a hashtag)")


def _timeline_media(data: dict) -> list:
    """Pull media objects out of a get_timeline_feed payload, in feed order.

    Home-feed items wrap the post in `media_or_ad`; ads and suggested-content
    rows have no media and are skipped. extract_media_v1 is instagrapi's own
    normaliser, so the resulting objects match every other path here.
    """
    from instagrapi.extractors import extract_media_v1

    out = []
    for item in (data.get("feed_items") or []):
        raw = item.get("media_or_ad")
        if not isinstance(raw, dict):
            continue
        try:
            out.append(extract_media_v1(raw))
        except Exception:
            continue
    return out


# ==========================================================================
# smoke test — the first thing to run with a REAL session
# ==========================================================================

def _smoke():
    """
    python3 engine_ig.py <username> [user_id_to_fetch]

    Loads the stored session for <username>, builds a client, and pulls one
    page from the home feed (and one user feed if a user id is given). This is
    where "not yet verified against live Instagram" stops being true.
    """
    import sys
    import ig

    rep = check()
    for line in rep.lines:
        print(" ", line)
    if not rep.ok:
        print("compat check failed — fix the above before trusting a run")
        return 2
    if len(sys.argv) < 2:
        print("\nusage: python3 engine_ig.py <username> [user_id]")
        return 1

    username = sys.argv[1]
    store_path = "ig_accounts.db"
    with ig.Store(store_path) as st:
        rows = st.all()
        print(f"\n{len(rows)} account(s) in {store_path}:")
        for a in rows:
            print(f"  @{a.get('username') or '(no handle)':20} "
                  f"active={a.get('active')}  label={a.get('label')}")

    # What is actually on disk, before any network call — the first question to
    # answer when a session misbehaves is "which device and which session am I
    # even using?", and guessing at it wastes more time than printing it.
    import ig_session
    label = next((a.get("label") for a in rows
                  if a.get("username") == username), None) or "ig_a"
    dev_path = ig_session.device_path(label)
    device = ig_session.load_device(label)
    side = ig_session.sidecar_path(username)
    print(f"\ndevice seed  {dev_path}: "
          + (f"pinned, uuid={(device.get('uuids') or {}).get('uuid')}, "
             f"{(device.get('device_settings') or {}).get('model')}"
             if device else "NONE YET (one will be minted on first use)"))
    print(f"sidecar      {side}: {'present' if side.exists() else 'none'}")

    # The shared session module reuses that sidecar and, if the session has
    # died, relogins automatically from the password in .env. A failure here is
    # the answer, not something to paper over with a second attempt: the
    # fallback path would only re-run the same dead cookie and bury the real
    # message under a redirect-loop traceback.
    try:
        cl = ig_session.load_client(username, store_path=store_path, log=print)
    except Exception as e:
        print(f"\ncould not get a working session for @{username}:\n  {e}")
        return 2
    eng = IGEngine(cl, account=username)

    async def run():
        # Each source is tried SEPARATELY and a failure in one does not end the
        # run. Instagram grants these endpoints independently — a session can be
        # barred from the home feed (an account-scoped read) while serving user
        # feeds perfectly — so "the home feed failed" is a fact about one source,
        # not a verdict on the session. Reporting it as the latter is what had
        # this project throwing away a session that could collect.
        ok_any = False

        print(f"\n-- home feed for @{username} --")
        try:
            async for page in eng.timeline_pages(max_pages=1):
                for pk in page.result_ids[:10]:
                    rec = page.entries_by_id[pk]
                    lag = page.lag_ms(pk)
                    lag_s = f"{lag/1000:.0f}s" if lag is not None else "?"
                    print(f"  @{rec['username']:20} {rec['url']}  lag={lag_s}")
                print(f"  ({len(page.result_ids)} posts, next_cursor={bool(page.cursor)})")
            ok_any = True
        except Exception as e:
            print(f"  unavailable: {type(e).__name__}: {str(e)[:160]}")
            print("  (account-scoped; commonly barred while a checkpoint is open. "
                  "'following' sources need this endpoint — user/hashtag sources do not.)")

        uid = sys.argv[2] if len(sys.argv) >= 3 else "787132"   # natgeo
        print(f"\n-- user feed for id {uid} --")
        try:
            async for page in eng.user_pages(uid, max_pages=1):
                for pk in page.result_ids[:10]:
                    rec = page.entries_by_id[pk]
                    print(f"  @{rec['username']:20} {rec['url']}")
                print(f"  ({len(page.result_ids)} posts)")
            ok_any = True
        except Exception as e:
            print(f"  unavailable: {type(e).__name__}: {str(e)[:160]}")

        print()
        print("session can collect: YES — at least one source works"
              if ok_any else
              "session can collect: NO — every source was refused")
        return 0 if ok_any else 2

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(_smoke())
