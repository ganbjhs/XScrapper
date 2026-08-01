"""
web.py — the dashboard.

    python3 main.py serve          # then open http://127.0.0.1:8765

Zero new dependencies: stdlib http.server plus one self-contained HTML page.
No npm, no build step, nothing fetched from a CDN.

Three things happen here, and they cost very different amounts:

  * Searching saved tweets reads results.db. Free, instant, unlimited. It is
    the default and what you should be doing nearly all the time.
  * Getting new tweets goes out to X. One request per page from a budget of
    ~50 per 15 minutes per account — the same budget the watcher needs to stay
    fresh. So it is never automatic, never fires on a keystroke, and the
    remaining budget is shown before you spend it.
  * Signing an account in runs a real browser on the server and streams its
    screen into the page. The browser exists only long enough to get a session;
    it is thrown away the moment X reports the account as signed in.

Binds to 127.0.0.1 by default. It serves your collected data, can spend your
rate-limit budget, and can add accounts — so it refuses to bind anywhere else
until DASH_USER and DASH_PASSWORD are set.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store as store_mod

# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------
#
# Credentials come from .env (DASH_USER / DASH_PASSWORD) so they are never in
# the repo. The design rule that matters:
#
#   THE SERVER REFUSES TO BIND ANYWHERE BUT LOCALHOST UNLESS BOTH ARE SET.
#
# Anything else relies on the operator remembering, and the failure mode is an
# unauthenticated dashboard on a public IP that can spend rate-limit budget and
# read every tweet collected. Making it impossible beats documenting it.
#
# Sessions are a signed cookie, not server state: HMAC over an expiry stamp
# with a secret generated at startup. That means a restart logs everyone out,
# which is a fair trade for holding no session table and having nothing to leak.
EXIT_REFUSED = 4

SESSION_COOKIE = "xs_session"
SESSION_TTL_S = 12 * 3600

# Brute-force damping. Small numbers on purpose: this guards one operator's
# dashboard, not a consumer signup form.
MAX_ATTEMPTS = 8
LOCKOUT_S = 300

_SECRET = secrets.token_bytes(32)
_attempts: dict[str, list] = {}
_attempts_lock = threading.Lock()


# .env ships with these filled in so the keys are visible and uncommented.
# They must never count as real credentials: a placeholder that satisfies the
# "is auth configured" check is worse than a blank, because it lets the server
# bind publicly with a password that is written down in the repo.
PLACEHOLDERS = {
    "changeme", "change_me", "CHANGE_ME_TO_A_LONG_RANDOM_STRING",
    "password", "admin", "your-password-here", "xxx", "todo",
}
MIN_PASSWORD_LEN = 12


def _auth_problem() -> str:
    """Return why the configured credentials are unusable, or '' if they are fine."""
    user = os.getenv("DASH_USER", "").strip()
    pwd = os.getenv("DASH_PASSWORD", "")
    if not user or not pwd:
        return "DASH_USER / DASH_PASSWORD are not set"
    if user.lower() in {p.lower() for p in PLACEHOLDERS}:
        return f"DASH_USER is still the placeholder {user!r}"
    if pwd in PLACEHOLDERS or pwd.lower() in {p.lower() for p in PLACEHOLDERS}:
        return "DASH_PASSWORD is still the placeholder from .env.example"
    if len(pwd) < MIN_PASSWORD_LEN:
        return (f"DASH_PASSWORD is {len(pwd)} characters; "
                f"at least {MIN_PASSWORD_LEN} is required to expose the dashboard")
    return ""


def _auth_configured() -> bool:
    return _auth_problem() == ""


# --------------------------------------------------------------------------
# API keys — how other systems authenticate
# --------------------------------------------------------------------------
#
# A browser gets a session cookie. A machine gets a bearer token, because a
# program cannot reasonably be asked to POST a login form and keep a cookie
# jar. Keys live in .env as API_KEYS, comma-separated:
#
#     API_KEYS=k_live_abc...,k_live_def...
#
# Several so a consumer can be revoked on its own, by deleting its key, without
# re-issuing to everyone else.
#
# WHAT A KEY MAY NOT DO. It reads collected data and may spend rate-limit
# budget through /api/fetch, and that is the whole list. It cannot add an
# account, open a sign-in browser, or hide a stream — those write secrets to
# disk, launch a process, or change what a human sees, and none of them is
# something a remote integration should reach. The rule is enforced by an
# allowlist in _require_auth, not by the caller being polite.
MIN_API_KEY_LEN = 24

# Endpoints a key may call. Everything else is cookie-only, deliberately.
API_KEY_PATHS = {
    "/api/tweets", "/api/status", "/api/streams", "/api/export", "/api/guard",
    "/api/fetch",
}


def _api_keys() -> set:
    raw = os.getenv("API_KEYS", "")
    return {k.strip() for k in raw.split(",")
            if len(k.strip()) >= MIN_API_KEY_LEN}


def _presented_key(headers) -> str:
    """Bearer token, or the X-API-Key header some clients find easier."""
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("X-API-Key") or "").strip()


def _valid_api_key(presented: str) -> bool:
    if not presented:
        return False
    # compare_digest against every key, and never break early: returning as
    # soon as one matches leaks, through timing, roughly where in the list a
    # guessed key would sit.
    hit = False
    for k in _api_keys():
        if hmac.compare_digest(presented, k):
            hit = True
    return hit


def _check_credentials(user: str, password: str) -> bool:
    """Constant-time compare, so response timing cannot leak the credentials."""
    want_u = os.getenv("DASH_USER", "")
    want_p = os.getenv("DASH_PASSWORD", "")
    if not (want_u and want_p):
        return False
    # Compare BOTH every time — short-circuiting on a wrong username would make
    # a valid username measurably slower to reject.
    u_ok = hmac.compare_digest(user.encode(), want_u.encode())
    p_ok = hmac.compare_digest(password.encode(), want_p.encode())
    return u_ok and p_ok


def _issue_token() -> str:
    exp = str(int(time.time() + SESSION_TTL_S)).encode()
    sig = hmac.new(_SECRET, exp, hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(exp + b"." + sig).decode()


def _token_valid(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        exp, sig = raw.split(b".", 1)
        expect = hmac.new(_SECRET, exp, hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(sig, expect):
            return False
        return int(exp) > time.time()
    except Exception:
        return False


def _locked_out(ip: str) -> int:
    """Seconds remaining in a lockout, or 0."""
    with _attempts_lock:
        hits = [t for t in _attempts.get(ip, []) if time.time() - t < LOCKOUT_S]
        _attempts[ip] = hits
        if len(hits) >= MAX_ATTEMPTS:
            return int(LOCKOUT_S - (time.time() - hits[0]))
    return 0


def _record_failure(ip: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(ip, []).append(time.time())


def _clear_failures(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)

# Filled in by serve().
_CFG = None

# Only one live fetch at a time: concurrent ones would race for the same
# account and just trip twscrape's 15-minute lock.
_FETCH_LOCK = threading.Lock()

# Hard ceiling on one fetch: ~500 tweets for 25 of ~50 requests. Exceeding it
# is refused outright, never silently reduced.
MAX_FETCH_PAGES = 25

# ONE event loop for the whole process, living on its own thread.
#
# This is not a style choice. twscrape keeps a module-level `asyncio.Lock()`
# (db.py:12) that binds to the first event loop which awaits it. Calling
# asyncio.run() per request — as this server originally did — creates a fresh
# loop each time, so the second request onward died with:
#
#   RuntimeError: <asyncio.locks.Lock ...> is bound to a different event loop
#
# A single long-lived loop keeps that lock valid, and also lets twscrape reuse
# its per-account transaction-id generator instead of rebuilding it per call.
_LOOP = None


def _start_loop():
    global _LOOP
    if _LOOP is not None:
        return
    _LOOP = asyncio.new_event_loop()
    threading.Thread(
        target=_LOOP.run_forever, name="xs-asyncio", daemon=True
    ).start()


def _run(coro, timeout=300):
    """Run a coroutine on the server's shared loop, from a handler thread."""
    if _LOOP is None:
        raise RuntimeError("event loop not started")
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


# --------------------------------------------------------------------------
# data access — plain sqlite, read-only, one connection per request
# --------------------------------------------------------------------------

def _connect():
    """Read-only connection, so the dashboard can never corrupt a live watcher."""
    con = sqlite3.connect(f"file:{_CFG.db_results}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _query_tweets(p):
    """Filter the collected tweets. All of this is local; nothing touches X."""
    where, params = ["t.source = 'result'"], []
    joins = ""

    if p.get("stream"):
        joins = "JOIN tweet_hits h USING(tweet_id) JOIN streams s USING(stream_id)"
        where.append("s.label = ?")
        params.append(p["stream"])

    if p.get("q"):
        # Free text across the tweet and its author.
        where.append("(t.text LIKE ? OR t.author_username LIKE ? OR t.author_display_name LIKE ?)")
        params += [f"%{p['q']}%"] * 3

    if p.get("author"):
        where.append("t.author_username LIKE ?")
        params.append(p["author"].lstrip("@") + "%")

    if p.get("lang"):
        where.append("t.lang = ?")
        params.append(p["lang"])

    if p.get("min_likes"):
        where.append("t.like_count >= ?")
        params.append(int(p["min_likes"]))

    if p.get("since"):
        ms = store_mod.parse_window(p["since"])
        if ms:
            where.append("t.created_ms >= ?")
            params.append(ms)

    if p.get("has_media"):
        where.append("t.media_urls NOT IN ('[]', '')")

    if p.get("no_retweets"):
        where.append("t.is_retweet = 0")

    # Cursors, for programs syncing rather than people browsing.
    #
    # Two, because they answer different questions and one of them has a trap:
    #
    #   since_id           everything POSTED after this tweet. What people
    #                      expect, and fine for "show me what's new". But X
    #                      indexes some tweets late, so a tweet collected now
    #                      can carry an older id than one collected a minute
    #                      ago — and this cursor steps straight over those.
    #   since_collected_ms everything WE SAW after this point. Gapless, because
    #                      collection order is the order rows actually appear.
    #                      Use this one to mirror the database.
    #
    # Saying so here rather than only in the docs: the difference is invisible
    # until a consumer notices it is quietly missing tweets.
    cursoring = False
    if p.get("since_id"):
        where.append("t.tweet_id > ?")
        params.append(int(p["since_id"]))
        cursoring = True
    if p.get("since_collected_ms"):
        where.append("t.collected_ms > ?")
        params.append(int(p["since_collected_ms"]))
        cursoring = True

    sql = f"SELECT t.* FROM tweets t {joins} WHERE {' AND '.join(where)}"
    # A cursor walk has to run oldest-first, or "the last row I got" is not a
    # position you can resume from.
    order = "ASC" if (p.get("order") == "asc" or cursoring) else "DESC"
    order_by = "t.collected_ms, t.tweet_id" if p.get("since_collected_ms") else "t.tweet_id"
    limit = min(int(p.get("limit") or 50), 500)
    offset = int(p.get("offset") or 0)

    with _connect() as con:
        total = con.execute(
            f"SELECT COUNT(*) c FROM tweets t {joins} WHERE {' AND '.join(where)}", params
        ).fetchone()["c"]
        rows = con.execute(
            f"{sql} ORDER BY {order_by} {order} LIMIT ? OFFSET ?",
            [*params, limit, offset]
        ).fetchall()

    out = {"total": total, "rows": [_row_to_json(r) for r in rows]}
    if rows:
        # Hand back the position to resume from, so a consumer never has to
        # work out which field to remember.
        last = rows[-1]
        out["cursor"] = {"since_id": str(last["tweet_id"]),
                         "since_collected_ms": last["collected_ms"]}
        out["has_more"] = len(rows) == limit and total > limit
    return out


def _row_to_json(r):
    d = dict(r)
    for k in ("hashtags", "mentions", "urls", "media_urls"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (TypeError, ValueError):
            d[k] = []
    try:
        d["media"] = json.loads(d.pop("media_json", None) or "[]")
    except (TypeError, ValueError):
        d["media"] = []
    # JS loses integer precision above 2^53, and tweet ids are well past it.
    d["tweet_id"] = str(d["tweet_id"])
    d.pop("raw_json", None)
    d.pop("raw_entry_json", None)
    return d


def _status():
    """Accounts, streams, budget, totals — everything the sidebar shows."""
    import auth

    out = {"accounts": [], "streams": [], "totals": {}, "db": str(_CFG.db_results)}

    async def _accounts():
        api = auth.open_api(_CFG.db_accounts)
        return await auth.health(api, _CFG)

    try:
        import guard as _g
        for r in _run(_accounts()):
            view = _g.AccountView(
                username=r.username, active=r.active, proxy=r.proxy,
                error_msg=r.error_msg, has_known_device=r.has_known_device,
                real_user_agent=r.real_user_agent, requests=r.total_req or 0)
            cls = _g.classify_account(view)
            out["accounts"].append({
                "username": r.username, "label": r.label, "active": r.active,
                "requests": r.total_req, "last_used": r.last_used,
                "locked": r.locked_queues, "error": r.error_msg,
                "status": cls["status"], "reasons": cls["reasons"],
                "action": cls["action"], "proxy": bool(r.proxy),
            })
    except Exception as e:
        out["accounts_error"] = f"{type(e).__name__}: {e}"

    by_label = {a.get("label") for a in out["accounts"]}
    for acct in _CFG.accounts:
        if acct.label not in by_label:
            # Added but never signed in. Showing it is the point: otherwise
            # "I added an account" and "it is collecting" look the same.
            out["accounts"].append({
                "username": acct.username or acct.label, "label": acct.label,
                "active": False, "status": "unknown",
                "reasons": ["added, but not signed in to X yet"],
                "action": "Use the Sign in to X button below.",
                "requests": 0, "proxy": bool(acct.proxy), "never_logged_in": True,
            })

    if not _CFG.db_results.exists():
        out["totals"] = {"tweets": 0, "note": "nothing collected yet"}
        return out

    try:
        with _connect() as con:
            out["totals"]["tweets"] = con.execute("SELECT COUNT(*) c FROM tweets").fetchone()["c"]
            for s in con.execute("SELECT * FROM streams ORDER BY label").fetchall():
                hits = con.execute(
                    "SELECT COUNT(*) c FROM tweet_hits WHERE stream_id = ?", (s["stream_id"],)
                ).fetchone()["c"]
                last = con.execute(
                    "SELECT * FROM polls WHERE stream_id = ? ORDER BY started_ms DESC LIMIT 1",
                    (s["stream_id"],),
                ).fetchone()
                # Lag only means "how fresh are we" for a stream a watcher has
                # actually polled on a timer. A one-off sweep pulls in tweets
                # that are already hours or days old, so its lag_ms is the AGE
                # of what it scooped up — reporting that as freshness reads as
                # "usually saved 8240s after posting", which is both alarming
                # and meaningless.
                watched = con.execute(
                    "SELECT 1 FROM polls WHERE stream_id = ? AND kind IN ('poll','seed') LIMIT 1",
                    (s["stream_id"],),
                ).fetchone() is not None
                lag = con.execute(
                    "SELECT t.lag_ms FROM tweet_hits h JOIN tweets t USING(tweet_id) "
                    "WHERE h.stream_id = ? AND t.created_ms >= COALESCE(?, 0) "
                    "ORDER BY h.first_seen_ms DESC LIMIT 200",
                    (s["stream_id"], s["first_poll_ms"]),
                ).fetchall() if watched else []
                lags = sorted(x["lag_ms"] for x in lag)
                cols = s.keys()

                def _col(name, default=None):
                    return s[name] if name in cols and s[name] is not None else default

                # Map the stored interval back to the named speed the panel
                # offers, so re-opening it shows what is actually set rather
                # than defaulting to blank and inviting an accidental change.
                speed = ""
                mi = _col("min_interval_s")
                if mi is not None:
                    speed = next((k for k, v in SPEEDS.items() if abs(v - mi) < 0.5), "")

                out["streams"].append({
                    "paused": bool(_col("paused", 0)),
                    "speed": speed,
                    "pages": _col("max_pages_per_poll"),
                    "tg_enabled": bool(_col("tg_enabled", 0)),
                    "tg_chat_id": _col("tg_chat_id", ""),
                    "tg_min_likes": _col("tg_min_likes", 0),
                    "tg_skip_retweets": bool(_col("tg_skip_retweets", 0)),
                    "label": s["label"], "query": s["query"], "tab": s["tab"],
                    "count": hits,
                    "lag_p50": lags[len(lags) // 2] / 1000 if lags else None,
                    "lag_p95": lags[int(len(lags) * 0.95)] / 1000 if lags else None,
                    "last_stop": last["stop_reason"] if last else None,
                    "last_new": last["new_tweets"] if last else None,
                    "rl_remaining": last["rl_remaining"] if last else None,
                    "rl_limit": last["rl_limit"] if last else None,
                })
    except Exception as e:
        out["totals"]["error"] = f"{type(e).__name__}: {e}"

    # Budget per QUEUE, not per stream.
    #
    # This used to take whichever stream happened to sort first and show its
    # last recorded header — so with both kinds configured you saw a search's
    # "35/50" while about to spend from a list's 500, or the reverse. Worse, it
    # was a frozen snapshot: X resets the window every 15 minutes, and a value
    # recorded before a reset reads as spent budget that has already come back.
    #
    # guard._budget already answers both properly, including the reset, so ask
    # it rather than keeping a second, worse copy of the same logic here.
    try:
        import guard as _g
        out["budget"] = {}
        for queue in ("search", "list"):
            b = _g._budget(_CFG, queue)
            if b.get("known"):
                out["budget"][queue] = {
                    "remaining": b["remaining"], "limit": b["limit"],
                    "resets_in": max(0, int(b["reset"] - time.time())) if b.get("reset") else None,
                    "rolled": b.get("window_rolled", False),
                    "stale_s": b.get("age_s"),
                }
    except Exception as e:
        out["budget_error"] = f"{type(e).__name__}: {e}"

    return out


def _fetch_live(query, tab="Latest", pages=1, ack=False, list_id=""):
    """
    Go out to X. This is the only action here that spends rate-limit budget,
    so it is explicit, serialised, and reports exactly what it cost.
    """
    import auth
    from collector import poll_once
    from engine import Engine

    # Built OUTSIDE the class body on purpose. A class body that assigns a name
    # makes that name class-local for the whole body, so an earlier line
    # referencing the enclosing function's `list_id` raised NameError rather
    # than reading the parameter. Class bodies do not close over enclosing
    # function scope for names they bind.
    _label = (f"ui:list:{list_id}" if list_id else f"ui:{query}")[:120]

    class _Stream:
        label = _label
        watermark = False        # ad-hoc query: sweep, never move a watermark
        tab = "Latest"
        page_size = 20
        max_pages_per_poll = 1
        overlap_ms = 0
        min_interval_s = 5
        max_interval_s = 900

    # REJECT rather than clamp. Silently turning a 60-page request into a
    # 25-page one still spends 25 requests the caller never agreed to — which
    # is precisely the surprise cost the guard exists to prevent. Refusing
    # makes the ceiling visible instead of absorbing the mistake.
    pages = int(pages)
    if pages > MAX_FETCH_PAGES:
        return {
            "error": f"Requested {pages} pages, but a single fetch is capped at "
                     f"{MAX_FETCH_PAGES} (~{MAX_FETCH_PAGES * 20} tweets, "
                     f"{MAX_FETCH_PAGES} of ~50 requests per 15 min). "
                     f"Ask for {MAX_FETCH_PAGES} or fewer.",
            "blocked": True,
        }
    pages = max(1, pages)

    s = _Stream()
    s.query, s.tab, s.max_pages_per_poll = query, tab, pages
    s.list_id = list_id

    # Re-check server-side. The UI asks the guard before offering the button,
    # but a check that only lives in the browser is a suggestion, not a control:
    # anything can POST here directly.
    import guard

    v = guard.assess(_CFG, action="fetch", cost=pages,
                     queue="list" if list_id else "search")
    if v.blocked:
        b = v.blocks[0]
        return {"error": f"{b.title} — {b.remedy}", "blocked": True,
                "guard": v.to_json()}

    # Warnings must be acknowledged, not merely displayed. The dashboard shows
    # them and sets ack=true once you confirm; a direct POST that skips that
    # step gets refused rather than quietly proceeding — otherwise the guard is
    # advisory over HTTP and enforcing only in the browser, which is backwards.
    if v.warnings and not ack:
        return {
            "error": "Warnings not acknowledged: "
                     + "; ".join(w.title for w in v.warnings),
            "blocked": True, "needs_ack": True, "guard": v.to_json(),
        }

    async def run():
        api = auth.open_api(_CFG.db_accounts)
        names = await auth.active_usernames(api)
        if not names:
            return {"error": "No active account. Run: python3 main.py login --all"}

        st = store_mod.Store(_CFG.db_results, _CFG.defaults.keep_entry_json)
        await st.open()
        try:
            sid = await st.ensure_stream(s.label, s.query, s.tab,
                                         watermarked=False, list_id=list_id)
            res = await poll_once(Engine(api), st, s, sid, kind="sweep")

            # Zero pages normally means the account pool was starved. For a
            # LIST it much more often means the list itself yielded nothing —
            # wrong id, private, or genuinely empty — and telling someone their
            # accounts are broken when they mistyped a list id sends them to
            # debug the wrong thing entirely.
            hint = ""
            if res.pages == 0 and list_id:
                hint = (f"List {list_id} returned nothing. Most likely it is "
                        f"private, empty, or the id is wrong — open "
                        f"https://x.com/i/lists/{list_id} to check. "
                        f"(A starved account pool looks identical here; "
                        f"the Accounts panel will say if that is the cause.)")

            return {
                "new": res.new, "dup": res.dup, "pages": res.pages,
                "results": res.results, "stop": res.stop_reason,
                "account": res.account, "rl_remaining": res.rl_remaining,
                "rl_limit": res.rl_limit, "error": res.error,
                "stream": s.label, "hint": hint, "is_list": bool(list_id),
            }
        finally:
            await st.close()

    with _FETCH_LOCK:
        return _run(run())


def _reload_config():
    """
    Re-read config.toml into the process.

    Adding an account writes config.toml, and everything downstream — the login
    session, the account panel, the guard — looks the account up through _CFG.
    Without this the account exists on disk and nowhere in memory, so opening
    the sign-in window straight after saving failed with "No account labelled
    ...". That is what used to force the operator back to a terminal.

    Failure is deliberately not swallowed: a config the server cannot parse must
    surface here, at the write that caused it, not three actions later.
    """
    global _CFG
    import config as config_mod

    fresh = config_mod.load_config(root=_CFG.root)
    fresh._behind_proxy = getattr(_CFG, "_behind_proxy", False)
    _CFG = fresh
    return _CFG


def _add_account(body):
    """
    Append an account to config.toml (and its password to .env), then make it
    live in this process so the sign-in window can open immediately.

    Writes secrets to disk, so the endpoint is behind the dashboard login.
    """
    import re as _re

    label = (body.get("label") or "").strip()
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", label):
        return {"error": "label must be 1-32 chars: letters, digits, _ or -"}

    # Every value below is written verbatim into a quoted TOML string, so a
    # stray quote would break out of it. Reject rather than escape: these
    # fields have narrow legal shapes, and an escaper is just a second place
    # for a bug to live.
    #
    # No password is accepted here. The operator types it into the real x.com
    # form in the sign-in window, so this server never receives, stores or logs
    # one. config.toml still names an env var for it, which is only used by the
    # command-line `login` path.
    username = (body.get("username") or "").strip().lstrip("@")
    proxy = (body.get("proxy") or "").strip()

    if username and not _re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        return {"error": "An X username is 1-15 letters, digits or underscores."}
    if proxy and not _re.fullmatch(r"[A-Za-z0-9+.\-]+://[^\s\"'\\\x00-\x1f]{1,200}", proxy):
        return {"error": "A proxy looks like scheme://host:port, with no spaces or quotes."}

    cfg_path = _CFG.root / "config.toml"
    if not cfg_path.exists():
        return {"error": f"{cfg_path} does not exist yet"}
    text = cfg_path.read_text()

    # PARSE the file rather than pattern-match it. The regex this replaced
    # required end-of-line right after the closing quote, so a trailing comment
    #     label = "legacy"   # matches profiles/legacy
    # slipped straight past and a duplicate account was appended. Duplicate
    # labels then make config.account(label) ambiguous and give two entries the
    # same Chrome profile — two browsers on one profile dir corrupts it.
    import tomllib
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {"error": f"config.toml is not valid TOML: {e}"}
    existing = {str(a.get("label", "")).strip()
                for a in parsed.get("accounts", []) if isinstance(a, dict)}
    if label in existing:
        return {"error": f"There is already an account called {label!r}."}

    block = (
        f'\n[[accounts]]\n'
        f'label              = "{label}"\n'
        f'username           = "{username}"\n'
        f'password_env       = "X_PASSWORD_{label.upper().replace("-", "_")}"\n'
        f'profile_dir        = "profiles/{label}"\n'
        f'proxy              = "{proxy}"\n'
        f'enabled            = true\n'
    )
    cfg_path.write_text(text.rstrip() + "\n" + block)

    # Make it real in this process. If the file we just wrote does not parse,
    # say so here rather than letting the sign-in window fail with a confusing
    # "no such account".
    try:
        _reload_config()
    except Exception as e:
        return {"error": f"Saved to config.toml, but the file no longer loads: "
                         f"{type(e).__name__}: {e}"}

    return {"ok": True, "label": label}


# --------------------------------------------------------------------------
# stream settings, removal, and Telegram
# --------------------------------------------------------------------------

def _with_store(fn):
    """Run one coroutine against a writable Store. The read path stays read-only."""
    async def go():
        st = store_mod.Store(_CFG.db_results, _CFG.defaults.keep_entry_json)
        await st.open()
        try:
            return await fn(st)
        finally:
            await st.close()
    return _run(go())


# Update speeds, named rather than numeric. "Every 30 seconds" is a promise the
# system cannot keep — the interval is adaptive and X's budget is the real
# ceiling — so these set the FLOOR and let the controller settle above it.
SPEEDS = {"fastest": 5, "fast": 15, "normal": 60, "slow": 300, "hourly": 1800}


def _stream_settings(body):
    label = (body.get("label") or "").strip()
    if not label:
        return {"error": "which stream?"}

    vals = {}
    if "paused" in body:
        vals["paused"] = int(bool(body["paused"]))
    if "speed" in body:
        speed = str(body.get("speed") or "")
        if speed and speed not in SPEEDS:
            return {"error": f"speed must be one of: {', '.join(SPEEDS)}"}
        # "" clears the override and goes back to whatever config.toml says.
        vals["min_interval_s"] = SPEEDS.get(speed) if speed else None
    if "pages" in body:
        pages = body.get("pages")
        if pages in (None, "", 0):
            vals["max_pages_per_poll"] = None
        else:
            try:
                n = int(pages)
            except (TypeError, ValueError):
                return {"error": "pages must be a whole number"}
            if not 1 <= n <= 25:
                return {"error": "pages must be between 1 and 25"}
            vals["max_pages_per_poll"] = n
    if "tg_enabled" in body:
        vals["tg_enabled"] = int(bool(body["tg_enabled"]))
    if "tg_chat_id" in body:
        chat = str(body.get("tg_chat_id") or "").strip()
        if chat and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{4,32}", chat):
            return {"error": "a chat id is a number like -1001234567890, "
                             "or a public channel like @mychannel"}
        vals["tg_chat_id"] = chat or None
    if "tg_min_likes" in body:
        try:
            vals["tg_min_likes"] = max(0, int(body.get("tg_min_likes") or 0))
        except (TypeError, ValueError):
            return {"error": "minimum likes must be a whole number"}
    if "tg_skip_retweets" in body:
        vals["tg_skip_retweets"] = int(bool(body["tg_skip_retweets"]))

    if not vals:
        return {"error": "nothing to change"}
    if not _with_store(lambda st: st.set_stream_settings(label, vals)):
        return {"error": f"no stream called {label!r}"}
    return {"ok": True, "label": label, "applied": vals}


def _stream_remove(body):
    """
    Two very different actions, and the caller must say which.

    `delete_tweets` is not a flag with a safe default — it is the difference
    between "stop watching this" and "destroy what it collected", so it is
    required to be explicit and the reply says exactly what went.
    """
    label = (body.get("label") or "").strip()
    if not label:
        return {"error": "which stream?"}
    hard = bool(body.get("delete_tweets"))

    # Destroying data needs the operator to have typed the name. A misclick
    # cannot produce this, and X's ~7-day window means it cannot be undone.
    if hard and (body.get("confirm") or "").strip() != label:
        return {"error": f"To delete the data, type the name exactly: {label}"}

    res = _with_store(lambda st: st.forget_stream(label, delete_tweets=hard))
    if not res.get("found"):
        return {"error": f"no stream called {label!r}"}

    # A stream declared in config.toml would simply be recreated on the next
    # poll, so removing it there too is the only way "stop watching" sticks.
    removed_from_config = _drop_stream_from_config(label)
    res["removed_from_config"] = removed_from_config
    if removed_from_config:
        try:
            _reload_config()
        except Exception as e:
            res["warning"] = f"config.toml no longer loads: {e}"
    return res


def _drop_stream_from_config(label: str) -> bool:
    """Remove a [[streams]] block by label, leaving the rest of the file alone."""
    path = _CFG.root / "config.toml"
    if not path.exists():
        return False
    text = path.read_text()
    # Match the block from its [[streams]] header to the next top-level table
    # or end of file, and only when THIS block's label is the one asked for.
    pattern = re.compile(
        r'\n\[\[streams\]\]\s*\n(?:(?!\n\[\[|\n\[).)*?'
        r'label\s*=\s*["\']' + re.escape(label) + r'["\'](?:(?!\n\[\[|\n\[).)*',
        re.S)
    new, n = pattern.subn("\n", text)
    if not n:
        return False
    path.write_text(new)
    return True


def _save_telegram(body):
    """Store the bot token and default chat in .env, where secrets live."""
    token = (body.get("token") or "").strip()
    chat = (body.get("chat_id") or "").strip()

    if token and not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,50}", token):
        return {"error": "That does not look like a bot token. BotFather gives "
                         "you something like 123456789:AAH... — paste the whole line."}
    if chat and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{4,32}", chat):
        return {"error": "a chat id is a number like -1001234567890, "
                         "or a public channel like @mychannel"}

    env_path = _CFG.root / ".env"
    cur = env_path.read_text() if env_path.exists() else ""
    for key, val in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)):
        if not val:
            continue
        lines, done = [], False
        for ln in cur.splitlines():
            if ln.strip().startswith(f"{key}="):
                lines.append(f"{key}={val}")
                done = True
            else:
                lines.append(ln)
        if not done:
            lines.append(f"{key}={val}")
        cur = "\n".join(lines) + "\n"
        os.environ[key] = val          # live now, without a restart
    env_path.write_text(cur)
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    return {"ok": True, "has_token": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "chat_id": os.getenv("TELEGRAM_CHAT_ID", "")}


def _test_telegram(body):
    """Send one real message, so 'saved' and 'working' are not confused."""
    import webhook as wh

    token = wh.telegram_token()
    chat = (body.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token:
        return {"error": "No bot token saved yet."}
    if not chat:
        return {"error": "No chat id — tell me where to send it."}

    async def go():
        import httpx
        async with httpx.AsyncClient() as client:
            return await wh.tg_send(
                client, token, chat,
                "<b>X Collector</b>\nConnected. Tweets will arrive here.")

    ok, err = _run(go(), timeout=40)
    return {"ok": ok} if ok else {"error": err}


# --------------------------------------------------------------------------
# interactive login
# --------------------------------------------------------------------------
#
# One at a time, deliberately. Each session holds a Chrome process open against
# an account's profile directory, and two Chromes on one profile corrupt it.
_LOGIN = {"session": None, "label": None}
_LOGIN_LOCK = threading.RLock()


def _login_drop():
    """Shut the browser down and forget the session. Safe to call twice."""
    with _LOGIN_LOCK:
        s = _LOGIN["session"]
        _LOGIN["session"] = _LOGIN["label"] = None
    if s is not None:
        try:
            _run(s.close(), timeout=30)
        except Exception:
            pass


def _login_reap():
    """Close a window the operator opened and then walked away from."""
    import auth

    with _LOGIN_LOCK:
        s = _LOGIN["session"]
        if s is None or s.idle_s <= auth.LOGIN_IDLE_TIMEOUT_S:
            return
    _login_drop()


def _login_start(label):
    import auth

    _login_reap()
    with _LOGIN_LOCK:
        if _LOGIN["session"] is not None:
            return {"error": f"A sign-in window is already open for "
                             f"'{_LOGIN['label']}'. Finish or close that one first."}
        try:
            acct = _CFG.account(label)
        except Exception as e:
            return {"error": str(e)}

        try:
            sess = _run(auth.InteractiveLogin(acct).start(), timeout=180)
        except ImportError:
            return {"error": "The browser this needs is not installed on the "
                             "server. Ask whoever set it up to run: "
                             "deploy/setup.sh"}
        except Exception as e:
            return {"error": f"Could not open a browser: {type(e).__name__}: {e}"}

        _LOGIN["session"], _LOGIN["label"] = sess, label
        return {"ok": True, "label": label, "state": sess.state,
                "screen_name": sess.screen_name,
                "width": auth.LOGIN_VIEWPORT["width"],
                "height": auth.LOGIN_VIEWPORT["height"]}


def _login_act(body):
    """Forward one interaction, then report the current state."""
    s = _LOGIN["session"]
    if s is None:
        return {"error": "The sign-in window is closed.", "closed": True}
    act = body.get("act")
    try:
        if act == "click":
            _run(s.click(int(body["x"]), int(body["y"])), timeout=30)
        elif act == "type":
            _run(s.type_text(str(body.get("text") or "")), timeout=60)
        elif act == "key":
            _run(s.press(str(body.get("key") or "Enter")), timeout=30)
        elif act == "scroll":
            _run(s.scroll(int(body.get("dy") or 0)), timeout=30)
        state = _run(s.refresh_state(), timeout=30)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out = {"ok": True, "state": state, "screen_name": s.screen_name}
    if state == "logged_in":
        out.update(_login_capture())
    return out


def _login_capture():
    """
    Signed in — copy the session out of the browser, then close the browser.

    The browser is disposable; the session is the asset. Everything the HTTP
    collector needs (cookies plus the real user-agent) is taken here, checked
    against X with one real request, and written to accounts.db. Chrome is then
    shut down cleanly, which is also what flushes the profile that keeps this
    device trusted for next time.
    """
    import auth

    s, label = _LOGIN["session"], _LOGIN["label"]
    if s is None:
        return {"error": "The sign-in window is closed."}
    try:
        harvest = _run(s.harvest(), timeout=60)
        if not harvest.has_required:
            # Do NOT close the browser here: X sets these a moment after the
            # redirect, so the next poll usually succeeds. Tearing the window
            # down would make the operator start over for a timing blip.
            return {"error": "Signed in, but X has not finished setting up the "
                             "session yet. Give it a moment."}

        async def save():
            api = auth.open_api(_CFG.db_accounts)
            return await auth.upsert_session(api, harvest, _CFG.account(label))

        username, res = _run(save(), timeout=120)
        if res.ok:
            auth.write_identity(_CFG.account(label), username)
    except Exception as e:
        _login_drop()
        return {"error": f"Could not save the session: {type(e).__name__}: {e}"}

    _login_drop()
    return {"captured": True, "username": username, "active": res.ok,
            "detail": "" if res.ok else res.error}


def _login_cancel():
    _login_drop()
    return {"ok": True}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — X Collector</title>
<style>
  :root{--bg:#fff;--panel:#f7f8fa;--line:#e3e6ea;--fg:#14171a;--dim:#5b7083;--accent:#1d9bf0;--warn:#c0392b}
  @media (prefers-color-scheme:dark){
    :root{--bg:#15181c;--panel:#1e2126;--line:#2f3336;--fg:#e7e9ea;--dim:#8b98a5}}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
       color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  form{width:min(92vw,340px);border:1px solid var(--line);border-radius:14px;
       padding:26px;background:var(--panel)}
  h1{margin:0 0 4px;font-size:17px}
  p.sub{margin:0 0 20px;color:var(--dim);font-size:13px}
  label{display:block;font-size:12px;color:var(--dim);margin:12px 0 4px;
        text-transform:uppercase;letter-spacing:.05em}
  input{width:100%;padding:9px 11px;border-radius:9px;border:1px solid var(--line);
        background:var(--bg);color:var(--fg);font:inherit}
  input:focus{outline:2px solid var(--accent);outline-offset:-1px}
  button{width:100%;margin-top:18px;padding:10px;border:0;border-radius:9px;
         background:var(--accent);color:#fff;font:inherit;font-weight:650;cursor:pointer}
  button:hover{filter:brightness(1.06)}
  .err{margin-top:14px;padding:8px 11px;border-radius:8px;font-size:13px;
       border:1px solid var(--warn);color:var(--warn)}
</style></head><body>
<form method="POST" action="/login">
  <h1>X Collector</h1>
  <p class="sub">Sign in to see your collected tweets.</p>
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
  __ERROR__
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the access log is noise here

    # ---------------- auth plumbing ----------------

    def _client_ip(self) -> str:
        # Behind nginx every connection appears to come from 127.0.0.1, so the
        # forwarded header is what distinguishes real clients for lockout
        # purposes. Only trusted when we are actually behind a proxy.
        if _CFG and getattr(_CFG, "_behind_proxy", False):
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _authed(self) -> bool:
        if not _auth_configured():
            return True  # localhost-only mode; serve() guarantees the binding
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE and _token_valid(v):
                return True
        return False

    def _require_auth(self) -> bool:
        """True if the request may proceed; otherwise responds and returns False."""
        path = urllib.parse.urlparse(self.path).path

        # A machine key gets an explicit allowlist of endpoints, never "whatever
        # a signed-in human can do". Checked before the cookie so an API client
        # that also happens to carry a stale cookie still gets key semantics.
        presented = _presented_key(self.headers)
        if presented:
            if not _valid_api_key(presented):
                self._send(401, {"error": "invalid API key"})
                return False
            if path not in API_KEY_PATHS:
                self._send(403, {
                    "error": f"an API key cannot use {path}",
                    "allowed": sorted(API_KEY_PATHS),
                    "detail": "Adding accounts, signing in and changing the view "
                              "are dashboard-only. Sign in with a browser for those.",
                })
                return False
            return True

        if self._authed():
            return True
        if path.startswith("/api/"):
            self._send(401, {
                "error": "not signed in",
                "detail": "Browsers sign in at /login. Programs send "
                          "'Authorization: Bearer <key>' with a key from API_KEYS.",
            })
        else:
            self._send(200, self._login_html(), "text/html; charset=utf-8")
        return False

    def _login_html(self, error: str = "") -> str:
        block = f'<div class="err">{error}</div>' if error else ""
        return LOGIN_PAGE.replace("__ERROR__", block)

    def _do_login(self):
        ip = self._client_ip()
        wait = _locked_out(ip)
        if wait:
            return self._send(429, self._login_html(
                f"Too many tries. Wait {wait // 60 + 1} minute(s) and try again."),
                "text/html; charset=utf-8")

        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        user = (form.get("username") or [""])[0]
        pwd = (form.get("password") or [""])[0]

        if not _check_credentials(user, pwd):
            _record_failure(ip)
            left = MAX_ATTEMPTS - len(_attempts.get(ip, []))
            return self._send(401, self._login_html(
                "That username or password is not right."
                + (f" {left} more tr{'y' if left == 1 else 'ies'} before this "
                   f"page locks for a few minutes." if left <= 3 else "")),
                "text/html; charset=utf-8")

        _clear_failures(ip)
        # Secure is set only behind a proxy terminating TLS; setting it on a
        # plain-HTTP localhost run would make the browser drop the cookie and
        # produce an unexplainable login loop.
        secure = "; Secure" if getattr(_CFG, "_behind_proxy", False) else ""
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={_issue_token()}; HttpOnly; SameSite=Strict; "
            f"Path=/; Max-Age={SESSION_TTL_S}{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _do_logout(self):
        self.send_response(303)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    _head_only = False

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # Content-Length still describes the body a GET would return; that is
        # what HEAD is for. Only the bytes are withheld.
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not self._head_only:
            self.wfile.write(data)

    def do_HEAD(self):
        """
        Same headers as GET, no body.

        BaseHTTPRequestHandler answers 501 for anything it has no do_* method
        for, so `curl -I https://…` reported the dashboard as broken while it
        was serving fine — and that is the first check anyone runs against a
        deployment. Uptime monitors do the same thing.
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/login":
                if self._authed():
                    self.send_response(303); self.send_header("Location", "/")
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                return self._send(200, self._login_html(), "text/html; charset=utf-8")
            if u.path == "/logout":
                return self._do_logout()
            if not self._require_auth():
                return
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, _status())
            if u.path == "/api/streams":
                # /api/status carries account health and rate-limit internals.
                # An integration wants to know what exists and how much of it
                # there is, so this is that and nothing else.
                if not _CFG.db_results.exists():
                    return self._send(200, {"streams": []})
                with _connect() as con:
                    rows = con.execute(
                        "SELECT s.label, s.query, s.list_id, s.paused, "
                        "       COUNT(h.tweet_id) AS tweets "
                        "FROM streams s LEFT JOIN tweet_hits h USING(stream_id) "
                        "GROUP BY s.stream_id ORDER BY s.label").fetchall()
                return self._send(200, {"streams": [
                    {"label": r["label"],
                     "source": ("list:" + r["list_id"]) if r["list_id"] else r["query"],
                     "paused": bool(r["paused"]), "tweets": r["tweets"]}
                    for r in rows]})
            if u.path == "/api/guard":
                import guard
                return self._send(200, guard.assess(
                    _CFG, action=q.get("action", ""), cost=int(q.get("cost") or 0),
                    host=self.server.server_address[0],
                    queue=q.get("queue", "search"),
                ).to_json())
            if u.path == "/api/tweets":
                if not _CFG.db_results.exists():
                    return self._send(200, {"total": 0, "rows": []})
                return self._send(200, _query_tweets(q))
            if u.path == "/api/login/frame":
                s = _LOGIN["session"]
                if s is None:
                    return self._send(409, {"error": "The sign-in window is closed."})
                try:
                    png = _run(s.frame(), timeout=30)
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return self._send(200, png, "image/jpeg")
            if u.path == "/api/export":
                return self._export(q)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/login":
                return self._do_login()
            if not self._require_auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or "{}")
            if u.path == "/api/login/start":
                return self._send(200, _login_start((body.get("label") or "").strip()))
            if u.path == "/api/login/act":
                return self._send(200, _login_act(body))
            if u.path == "/api/login/cancel":
                return self._send(200, _login_cancel())
            if u.path == "/api/stream/settings":
                return self._send(200, _stream_settings(body))
            if u.path == "/api/stream/remove":
                return self._send(200, _stream_remove(body))
            if u.path == "/api/settings/telegram":
                if not _auth_configured():
                    return self._send(403, {"error":
                        "Saving the Telegram token needs DASH_USER/DASH_PASSWORD "
                        "set in .env, so the endpoint is behind a login."})
                return self._send(200, _save_telegram(body))
            if u.path == "/api/settings/telegram/test":
                return self._send(200, _test_telegram(body))
            if u.path == "/api/account":
                # Writes an X password into .env, so it needs a real gate.
                # The old check was "is the socket on 127.0.0.1", which is
                # ALWAYS true behind nginx — it would have passed on a public
                # box. Authentication is the correct gate; the bind address is
                # not evidence of anything once a proxy is in front.
                if not _auth_configured():
                    return self._send(403, {"error":
                        "Adding accounts needs DASH_USER/DASH_PASSWORD set in .env, "
                        "so the endpoint is behind a login."})
                return self._send(200, _add_account(body))
            if u.path == "/api/fetch":
                query = (body.get("query") or "").strip()
                raw_list = str(body.get("list_id") or "").strip()
                if query and raw_list:
                    return self._send(400, {"error": "pass query or list_id, not both"})
                if not query and not raw_list:
                    return self._send(400, {"error": "query or list_id is required"})
                if raw_list:
                    from config import ConfigError, _parse_list_id
                    try:
                        body["list_id"] = _parse_list_id(raw_list, "fetch")
                    except ConfigError as e:
                        return self._send(400, {"error": str(e)})
                return self._send(200, _fetch_live(
                    query, body.get("tab") or "Latest",
                    body.get("pages") or 1, bool(body.get("ack")),
                    str(body.get("list_id") or "")))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _export(self, q):
        import csv
        import io

        from store import FIELDS, from_store_row, to_csv_row

        q = {**q, "limit": q.get("limit") or 5000}
        rows = _query_tweets(q)["rows"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(to_csv_row(from_store_row(r)))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="x-export-{stamp}.csv"'})


def serve(cfg, host="127.0.0.1", port=8765, log=print, behind_proxy=False):
    global _CFG
    _CFG = cfg
    _CFG._behind_proxy = behind_proxy

    loopback = host in ("127.0.0.1", "localhost", "::1")

    # The one hard safety rule. An unauthenticated dashboard reachable off this
    # machine can spend rate-limit budget, read every collected tweet, and add
    # accounts. Refusing to start is the only reliable way to prevent it — a
    # warning would just scroll past.
    if not loopback and not _auth_configured():
        log("[serve] REFUSING TO START")
        log(f"[serve]   You asked to bind {host}, which is reachable from other machines,")
        log(f"[serve]   but: {_auth_problem()}")
        log("[serve]   Set real values in .env:")
        log("[serve]       DASH_USER=you")
        log("[serve]       DASH_PASSWORD=$(python3 -c \"import secrets;print(secrets.token_urlsafe(24))\")")
        log("[serve]   Or bind --host 127.0.0.1 and reach it over an SSH tunnel.")
        return EXIT_REFUSED

    _start_loop()
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        log(f"[serve] cannot bind {host}:{port} — {e}")
        return EXIT_REFUSED

    log(f"[serve] http://{host}:{port}")
    log(f"[serve] reading {cfg.db_results}")
    if _auth_configured():
        log(f"[serve] login required (user: {os.getenv('DASH_USER')})")
    else:
        log("[serve] NO LOGIN — localhost only. Set DASH_USER/DASH_PASSWORD to expose it.")
    if behind_proxy:
        log("[serve] trusting X-Forwarded-For (behind a reverse proxy)")
    log("[serve] Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\n[serve] stopped")
    finally:
        srv.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>X Collector</title>
<style>
  :root{
    --bg:#fff; --panel:#f7f8fa; --line:#e3e6ea; --fg:#14171a; --dim:#5b7083;
    --accent:#1d9bf0; --warn:#c0392b; --ok:#17a673; --radius:10px;
  }
  @media (prefers-color-scheme:dark){
    :root{ --bg:#15181c; --panel:#1e2126; --line:#2f3336; --fg:#e7e9ea; --dim:#8b98a5; }
  }
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--fg)}
  header{position:sticky;top:0;z-index:9;background:var(--bg);border-bottom:1px solid var(--line);
         padding:12px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  h1{font-size:15px;margin:0 12px 0 0;font-weight:650;white-space:nowrap}
  input,select,button{font:inherit;color:var(--fg);background:var(--panel);
        border:1px solid var(--line);border-radius:8px;padding:7px 10px}
  input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
  #q{flex:1;min-width:240px}
  button{cursor:pointer;background:var(--panel)}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
  button.danger{border-color:var(--warn);color:var(--warn)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .wrap{display:grid;grid-template-columns:1fr 300px;gap:18px;padding:18px;align-items:start}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .filters{display:flex;gap:8px;flex-wrap:wrap;padding:0 18px 4px;align-items:center}
  .filters input,.filters select{padding:5px 8px;font-size:13px}
  .card{border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;margin-bottom:10px;
        background:var(--panel)}
  .card .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:5px}
  .name{font-weight:650}
  .handle,.when{color:var(--dim);font-size:13px}
  .text{white-space:pre-wrap;word-wrap:break-word;margin:2px 0 8px}
  .metrics{display:flex;gap:14px;color:var(--dim);font-size:13px;flex-wrap:wrap;align-items:center}
  .media{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px}
  .media img,.media video{max-height:220px;max-width:100%;border-radius:8px;
       border:1px solid var(--line);background:#000}
  .media video{width:min(100%,380px)}
  /* A video shows as its still until clicked. The button IS the thumbnail. */
  .playwrap{position:relative;padding:0;border:0;background:none;cursor:pointer;
            line-height:0;border-radius:8px}
  .playwrap img{display:block;max-height:220px}
  .playbtn{position:absolute;inset:0;margin:auto;width:52px;height:52px;
           border-radius:50%;background:rgba(0,0,0,.62);color:#fff;font-size:20px;
           display:grid;place-items:center;pointer-events:none;line-height:1;
           padding-left:4px}
  .playwrap:hover .playbtn{background:rgba(0,0,0,.82)}
  .dur{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.72);color:#fff;
       font-size:11px;padding:1px 6px;border-radius:4px;pointer-events:none;line-height:1.6}
  .media .yt{width:min(100%,380px);height:214px;border:1px solid var(--line);
       border-radius:8px;background:#000}
  .medialink{display:flex;flex-direction:column;gap:2px;padding:9px 12px;
       border:1px solid var(--line);border-radius:8px;text-decoration:none;
       background:var(--bg);min-width:220px}
  .medialink b{color:var(--fg);font-size:13px}
  .medialink span{color:var(--dim);font-size:12px}
  .medialink.live b{color:var(--warn)}
  aside .box{border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;
             margin-bottom:12px;background:var(--panel)}
  aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
           margin:0 0 8px;font-weight:650}
  .row{display:flex;justify-content:space-between;gap:8px;font-size:13px;padding:3px 0}
  .row .k{color:var(--dim)}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:650}
  .pill.ok{background:rgba(23,166,115,.15);color:var(--ok)}
  /* Status flags. Colour carries the meaning, the word carries it again for
     anyone who cannot rely on colour alone. */
  .flag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
        font-weight:700;letter-spacing:.03em}
  .flag.live   {background:rgba(23,166,115,.16); color:#17a673}
  .flag.warning{background:rgba(224,168,0,.20);  color:#b8860b}
  .flag.dead   {background:rgba(192,57,43,.18);  color:#e0483a}
  .flag.unknown{background:rgba(120,130,140,.20);color:var(--dim)}
  .pill.bad{background:rgba(192,57,43,.15);color:var(--warn)}
  .muted{color:var(--dim);font-size:13px}
  .banner{margin:0 18px 10px;padding:9px 12px;border-radius:8px;font-size:13px;
          border:1px solid var(--line);background:var(--panel)}
  .banner.err{border-color:var(--warn);color:var(--warn)}
  .banner.ok{border-color:var(--ok);color:var(--ok)}
  a{color:var(--accent)}
  /* The remote browser. The image IS the page: clicks and keys are forwarded
     to a real Chrome running on the server, so this behaves like a window. */
  #loginwrap{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;
             display:none;place-items:center}
  #loginwrap.on{display:grid}
  /* A DEFINITE height, not max-height. A flex column sized by its content
     gives its children an indefinite height, and `max-height:100%` on the
     image inside then resolves to nothing at all — the image stayed 780px tall
     in a 700px window and the fix silently did nothing. Pinning the height is
     what makes the percentage below mean something.
     The cap is the natural size (780px of page + 92px of chrome), so on a tall
     screen the picture is 1:1 with no letterboxing, and on a short one the box
     is the window and the picture scales down to fit it. */
  #loginbox{background:var(--bg);border:1px solid var(--line);border-radius:12px;
            overflow:hidden;width:min(96vw,1100px);height:min(96vh,872px);
            display:flex;flex-direction:column}
  #loginhead{display:flex;align-items:center;gap:12px;padding:10px 14px;
             border-bottom:1px solid var(--line);flex:0 0 auto}
  #loginhead b{font-size:14px}
  #loginmsg{color:var(--dim);font-size:13px;flex:1}
  /* The remote page must ALWAYS fit. It is a fixed 1100x780 screenshot, and it
     used to be dropped in at full size inside a scrolling box — so on a laptop
     the bottom of X's login card, Continue button included, was simply below
     the fold. You cannot press a button you cannot see, and it is not obvious
     the window scrolls.
     flex:1 + min-height:0 lets the stage take the leftover height and shrink;
     max-height:100% on the image scales it down to match. Both max-* on a
     replaced element preserve the aspect ratio, so the element's box stays
     exactly the drawn image — which is what keeps click mapping honest. */
  /* flex, NOT grid. A grid row sized `auto` grows to fit its item, so the
     image's `max-height:100%` resolved against the image's own height — a
     circular constraint that clamps to nothing. A flex item's percentage
     cross-size resolves against the container's definite height instead. */
  #loginstage{flex:1;min-height:0;display:flex;align-items:center;
              justify-content:center;overflow:hidden;background:#000}
  #loginimg{display:block;max-width:100%;max-height:100%;cursor:crosshair}
  #loginhint{padding:7px 14px;font-size:12px;color:var(--dim);flex:0 0 auto;
             border-top:1px solid var(--line);background:var(--panel)}
  .livetog{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--dim);
           border:1px solid var(--line);border-radius:8px;padding:4px 9px;cursor:pointer}
  .livetog select{padding:2px 4px;font-size:12px;border:0;background:transparent}
  #livedot{width:7px;height:7px;border-radius:50%;background:var(--dim);display:inline-block}
  #livedot.on{background:var(--ok);animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  /* A brief tint on arrival, then it settles into the list. Permanent
     highlighting would just accumulate into noise. */
  @keyframes arrive{from{background:rgba(29,155,240,.16)}to{background:var(--panel)}}
  .card.new{animation:arrive 2.5s ease-out}
  #newbar{position:sticky;top:52px;z-index:8;display:none;margin-bottom:10px}
  #newbar button{width:100%;padding:7px;background:var(--accent);color:#fff;
                 border-color:var(--accent);font-weight:600}
  .streamrow{display:flex;gap:4px;margin-bottom:5px}
  /* "Everything" is a direct child with no row wrapper, so it carries its own
     spacing; the ones inside a row get theirs from .streamrow. */
  #streams > .streambtn{margin-bottom:5px}
  .streambtn{display:block;width:100%;text-align:left;font-size:13px}
  .streambtn.active{border-color:var(--accent);background:rgba(29,155,240,.1)}
  .streambtn.off{opacity:.5}
  .streamx{flex:0 0 auto;padding:4px 8px;font-size:12px;color:var(--dim)}
  .streamx:hover{border-color:var(--accent);color:var(--accent)}
  .streamcfg{border:1px solid var(--line);border-radius:8px;padding:8px 10px;
             margin:-2px 0 8px;background:var(--bg);display:grid;gap:6px}
  .cfgrow{display:flex;align-items:center;justify-content:space-between;gap:6px;
          font-size:12px;color:var(--dim)}
  .cfgrow select,.cfgrow input{font-size:12px;padding:3px 6px;max-width:58%}
  .cfgchk{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
  .cfghead{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
           color:var(--dim);margin-top:4px;border-top:1px solid var(--line);padding-top:6px}
  .cfgbtn{width:100%;padding:4px;font-size:12px}
  .cfgbtn.danger{border-color:var(--warn);color:var(--warn)}
  .linky{width:100%;border:0;background:none;color:var(--dim);font-size:12px;
         text-align:left;padding:2px 0;text-decoration:underline;cursor:pointer}
  code{background:var(--bg);padding:1px 5px;border-radius:5px;font-size:12px;
       border:1px solid var(--line)}
</style></head><body>

<header>
  <h1>X Collector</h1>
  <select id="src" title="What the Get new tweets button should fetch">
    <option value="search">words</option>
    <option value="list">a list</option>
  </select>
  <input id="q" placeholder="Type words to find in the tweets you have saved…" autofocus>
  <button class="primary" id="go">Search</button>
  <select id="pages" title="How many tweets to ask X for">
    <option value="1">about 20 tweets</option>
    <option value="3">about 60 tweets</option>
    <option value="5" selected>about 100 tweets</option>
    <option value="10">about 200 tweets</option>
    <option value="25">about 500 tweets</option>
  </select>
  <button id="getnew" title="Asks X for tweets you do not have yet">Get new tweets</button>
  <button id="csv">Download</button>
  <label class="livetog" title="Checks for tweets the collector has saved since you loaded this page.
It does not contact X, so it is free.">
    <input type="checkbox" id="autorefresh" checked>
    <span id="livedot"></span>Auto-update
    <select id="everysec">
      <option value="15" selected>every 15s</option>
      <option value="30">every 30s</option>
      <option value="60">every 60s</option>
    </select>
  </label>
  <a href="/logout" class="muted" style="font-size:13px;margin-left:auto">Sign out</a>
</header>

<div class="filters">
  <input id="author" placeholder="from @someone" size="14">
  <select id="since">
    <option value="">any time</option><option value="1h">past hour</option>
    <option value="6h">past 6 hours</option><option value="24h">past day</option>
    <option value="7d">past week</option>
  </select>
  <input id="minlikes" type="number" placeholder="at least ? likes" style="width:140px">
  <input id="lang" placeholder="language" style="width:90px" title="Two-letter code, e.g. en or hi">
  <label class="muted"><input type="checkbox" id="media"> only with pictures or video</label>
  <label class="muted" title="A retweet is someone resharing another account's post.
Tick this to see only original posts."><input type="checkbox" id="noretweets"> hide retweets</label>
  <select id="order"><option value="desc">newest first</option><option value="asc">oldest first</option></select>
  <span class="muted" id="count"></span>
</div>

<div id="banner"></div>

<div class="wrap">
  <main>
    <div id="newbar"><button id="newbtn"></button></div>
    <div id="results"><p class="muted">Loading…</p></div>
  </main>
  <aside>
    <div class="box" id="riskbox" hidden>
      <h2>Needs your attention</h2>
      <div id="risks"></div>
    </div>
    <div class="box">
      <h2>What we are watching</h2>
      <div id="streams"><span class="muted">—</span></div>
      <button id="tgtoggle" class="linky" style="margin-top:6px">⚙ Telegram &amp; settings</button>
      <div id="tgbox" hidden style="margin-top:6px;display:grid;gap:5px">
        <p class="muted" style="margin:0">
          Send new tweets straight to Telegram. Make a bot by messaging
          <b>@BotFather</b> on Telegram, then paste what it gives you here.
          Switch it on per list with the ⚙ next to that list.</p>
        <input id="tg_token" placeholder="bot token from @BotFather" autocomplete="off">
        <input id="tg_chat" placeholder="chat id, e.g. -1001234567890">
        <p class="muted" style="margin:0;font-size:11px">
          Not sure of the chat id? Add the bot to your group, send it a message,
          then open api.telegram.org/bot&lt;token&gt;/getUpdates — the id is in there.</p>
        <div style="display:flex;gap:6px">
          <button id="tg_save" class="primary" style="flex:1;padding:5px;font-size:13px">Save</button>
          <button id="tg_test" style="padding:5px 10px;font-size:13px">Send a test</button>
        </div>
      </div>
    </div>
    <div class="box">
      <h2>X accounts</h2>
      <div id="accounts"><span class="muted">—</span></div>
      <button id="acctnew" style="width:100%;margin-top:8px;padding:5px;font-size:13px">
        + Add an account</button>
      <div id="acctform" hidden style="margin-top:8px;display:grid;gap:5px">
        <p class="muted" style="margin:0 0 2px">
          Give it a short name, then sign in. A window opens where you type your
          X password directly into x.com.</p>
        <input id="a_label" placeholder="short name, e.g. acct_b">
        <input id="a_user"  placeholder="X username (optional)">
        <input id="a_proxy" placeholder="proxy (leave blank)">
        <div style="display:flex;gap:6px">
          <button id="a_save" class="primary" style="flex:1;padding:5px;font-size:13px">
            Save and sign in</button>
          <button id="a_cancel" style="padding:5px 10px;font-size:13px">Cancel</button>
        </div>
      </div>
    </div>
    <div class="box">
      <h2>Saved so far</h2>
      <div id="totals"><span class="muted">—</span></div>
    </div>
    <div class="box">
      <h2>Search tips</h2>
      <p class="muted" style="margin:0 0 6px">Searching what you have saved takes plain
      words. When you press <b>Get new tweets</b>, X also understands:</p>
      <p class="muted" style="margin:0"><code>from:someone</code> — only their posts<br>
      <code>-filter:replies</code> — skip replies<br>
      <code>lang:en</code> — English only<br>
      <code>min_faves:50</code> — at least 50 likes</p>
    </div>
  </aside>
</div>

<div id="loginwrap">
  <div id="loginbox">
    <div id="loginhead">
      <b>Sign in to X</b>
      <span id="loginmsg">Starting…</span>
      <button id="logindone" hidden>Done</button>
      <button id="loginx">Close</button>
    </div>
    <div id="loginstage">
      <img id="loginimg" alt="The X sign-in page">
    </div>
    <div id="loginhint">This is a real browser. Click and type in it as you normally
      would. Your password goes straight to x.com — this page never sees it.
      Scrolling works too.</div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let activeStream = "";
let openCfg = null;      // which stream has its settings open

const esc = s => (s||"").replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function ago(iso){
  if(!iso) return "";
  const s = (Date.now() - new Date(iso).getTime())/1000;
  if (s < 60) return Math.max(0,Math.round(s))+"s ago";
  if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return Math.round(s/3600)+"h ago";
  return Math.round(s/86400)+"d ago";
}
const num = n => n == null ? "0" : n >= 1000 ? (n/1000).toFixed(1)+"K" : ""+n;

// A duration in seconds, said the way a person would say it.
function secs(s){
  if (s < 90)    return Math.round(s) + " seconds";
  if (s < 5400)  return Math.round(s/60) + " minutes";
  if (s < 86400) return Math.round(s/3600) + " hours";
  return Math.round(s/86400) + " days";
}

// A video's length, as a player shows it. X reports duration in MILLISECONDS —
// feeding it to secs() above turns a 5-minute clip into "3 days".
function clock(ms){
  const t = Math.round(ms/1000), m = Math.floor(t/60), s = t % 60;
  if (m < 60) return `${m}:${String(s).padStart(2,"0")}`;
  return `${Math.floor(m/60)}:${String(m%60).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function banner(msg, kind){
  $("#banner").innerHTML = msg ? `<div class="banner ${kind||''}">${msg}</div>` : "";
}

const PAGE_SIZE = 100;
let offset = 0, loaded = 0, lastTotal = 0;

function params(){
  const p = new URLSearchParams();
  const add = (k,v) => { if(v) p.set(k,v); };
  add("q", $("#q").value.trim());
  add("author", $("#author").value.trim());
  add("since", $("#since").value);
  add("min_likes", $("#minlikes").value);
  add("lang", $("#lang").value.trim());
  add("order", $("#order").value);
  add("stream", activeStream);
  if ($("#media").checked) p.set("has_media","1");
  if ($("#noretweets").checked) p.set("no_retweets","1");
  return p;
}

/* Distinguishes "the server is not running" from "the server said no".
   A bare "Failed to fetch" is useless — it is what the browser says when the
   backend is simply gone, which is the single most likely cause. */
async function api(url, opts){
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new Error("Cannot reach the collector. It may have stopped — " +
                    "reload the page in a moment.");
  }
  const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

async function search(append){
  if (!append){ offset = 0; loaded = 0; }
  const p = params();
  p.set("limit", PAGE_SIZE);
  p.set("offset", offset);

  let d;
  try { d = await api("/api/tweets?" + p); }
  catch (e) { return banner(esc(e.message).replace(/\n/g,"<br>"), "err"); }
  banner("");

  lastTotal = d.total || 0;
  loaded += (d.rows || []).length;
  $("#count").textContent = lastTotal
    ? `showing ${loaded} of ${lastTotal}` : "nothing found";

  if (!d.rows || !d.rows.length){
    if (!append) $("#results").innerHTML = `<p class="muted">No saved tweets match that.
      ${$("#q").value.trim() ? 'Press <b>Get new tweets</b> to ask X for them.' : ''}</p>`;
    return;
  }
  const html = d.rows.map(card).join("");
  if (append) $("#more")?.remove(), $("#results").insertAdjacentHTML("beforeend", html);
  else {
    // A fresh search resets the high-water mark, otherwise changing filters
    // would suppress everything older than whatever the previous filter showed.
    $("#results").innerHTML = html;
    topId = null; pending = []; $("#newbar").style.display = "none";
  }
  noteTop(d.rows);

  if (loaded < lastTotal){
    $("#results").insertAdjacentHTML("beforeend",
      `<button id="more" style="width:100%;padding:10px">Show ${
        Math.min(PAGE_SIZE, lastTotal - loaded)} more (${lastTotal - loaded} left)</button>`);
    $("#more").onclick = () => { offset += PAGE_SIZE; search(true); };
  }
}

/* Media rendering.
   mp4s from video.twimg.com play inline — the URL in media_urls is the direct
   file (verified: 200 video/mp4, byte-range capable), so a plain <video> works.
   X BROADCASTS CANNOT BE PREVIEWED: x.com sends
   `frame-ancestors 'self' https://x.com`, so any iframe from this page is
   refused by the browser. They get an honest labelled link instead of an
   embed that would render as a blank box. */
const YT = /(?:youtube\.com\/watch\?v=|youtu\.be\/)([A-Za-z0-9_-]{11})/;

function mediaHtml(t){
  const bits = [];

  /* Videos show their THUMBNAIL until you click.

     Every card used to mount a real <video> pointing at video.twimg.com.
     Even with preload="metadata" that is a request per clip, so scrolling a
     page of 100 tweets pulled dozens of videos nobody watched — and X's media
     URLs are signed and expire, so most of them came back 403 and rendered as
     black boxes. A still is a few tens of KB against several MB, and it cannot
     expire into a broken player.

     media[] carries {type,url,thumb}. Older rows have only the flat
     media_urls, so fall back to the previous behaviour for those rather than
     showing them nothing. */
  const media = (t.media && t.media.length)
    ? t.media
    : (t.media_urls || []).map(u => ({
        type: /\.(jpg|jpeg|png|webp)(\?|$)/i.test(u) ? "photo"
            : /\.mp4(\?|$)/i.test(u) ? "video" : "other",
        url: u, thumb: null }));

  for (const m of media){
    if (m.type === "photo"){
      bits.push(`<img src="${esc(m.thumb || m.url)}" loading="lazy" alt="">`);
    } else if (m.type === "video" || m.type === "gif"){
      const dur = m.duration ? `<span class="dur">${clock(m.duration)}</span>` : "";
      bits.push(m.thumb
        ? `<button class="playwrap" data-play="${esc(m.url)}" data-kind="${esc(m.type)}"
                   title="Play">
             <img src="${esc(m.thumb)}" loading="lazy" alt="">
             <span class="playbtn">▶</span>${dur}
           </button>`
        : `<video src="${esc(m.url)}" controls preload="none"
                  playsinline muted loop></video>`);
    } else if (/\.m3u8(\?|$)/i.test(m.url)){
      bits.push(`<a class="medialink" href="${esc(m.url)}" target="_blank" rel="noopener">
                   <b>Video stream</b><span>cannot play here — opens in a new tab</span></a>`);
    }
  }

  for (const u of (t.urls || [])){
    const yt = u.match(YT);
    if (yt){
      bits.push(`<iframe class="yt" src="https://www.youtube-nocookie.com/embed/${esc(yt[1])}"
                  loading="lazy" allowfullscreen
                  referrerpolicy="strict-origin-when-cross-origin"></iframe>`);
    } else if (/x\.com\/i\/broadcasts\//.test(u) || /pscp\.tv/.test(u)){
      bits.push(`<a class="medialink live" href="${esc(u)}" target="_blank" rel="noopener">
                   <b>● Live broadcast</b>
                   <span>X does not allow these to play here — opens on x.com</span></a>`);
    }
  }
  return bits.length ? `<div class="media">${bits.join("")}</div>` : "";
}

function card(t){
  const media = mediaHtml(t);
  return `<article class="card">
    <div class="top">
      <span class="name">${esc(t.author_display_name || t.author_username)}</span>
      <span class="handle">@${esc(t.author_username)}</span>
      <span class="when">· ${ago(t.created_at)}</span>
      ${t.lang ? `<span class="handle">· ${esc(t.lang)}</span>` : ""}
    </div>
    <div class="text">${esc(t.text)}</div>
    ${media}
    <div class="metrics">
      <span>♥ ${num(t.like_count)}</span>
      <span>⟳ ${num(t.retweet_count)}</span>
      <span>↩ ${num(t.reply_count)}</span>
      ${t.view_count ? `<span>👁 ${num(t.view_count)}</span>` : ""}
      ${t.lag_ms != null ? `<span title="how long after it was posted we saved it">⏱ ${(t.lag_ms/1000).toFixed(1)}s</span>` : ""}
      <a href="${esc(t.url)}" target="_blank" rel="noopener">see on X ↗</a>
    </div>
  </article>`;
}

async function status(){
  let d;
  try { d = await api("/api/status"); }
  catch (e) {
    $("#accounts").innerHTML = '<span class="muted">server unreachable</span>';
    return banner(esc(e.message).replace(/\n/g,"<br>"), "err");
  }

  const all = d.streams || [];

  $("#streams").innerHTML = all.length
    ? `<button class="streambtn ${activeStream?'':'active'}" data-s="">Everything</button>` +
      all.map(s => `<div class="streamrow">
        <button class="streambtn ${activeStream===s.label?'active':''} ${s.paused?'off':''}"
          data-s="${esc(s.label)}" title="${esc(s.query || s.label)}">
          ${esc(s.label)} <span class="muted">· ${s.count} tweets</span>
          ${s.paused ? '<span class="muted">· paused</span>' : ''}
          ${s.tg_enabled ? '<span class="muted">· → Telegram</span>' : ''}
          ${s.lag_p50!=null ? `<span class="muted">· usually saved ${secs(s.lag_p50)} after posting</span>`:''}
        </button>
        <button class="streamx" data-gear="${esc(s.label)}" title="Settings">⚙</button>
      </div>
      <div class="streamcfg" data-cfg="${esc(s.label)}" hidden></div>`).join("")
    : '<span class="muted">nothing yet</span>';

  document.querySelectorAll(".streambtn").forEach(b =>
    b.onclick = () => { activeStream = b.dataset.s; status(); search(); });

  // Bind in the same pass that draws them: status() replaces this whole panel
  // every 15s, so anything bound earlier belongs to nodes that no longer exist.
  document.querySelectorAll("[data-gear]").forEach(b => b.onclick = () => {
    const label = b.dataset.gear;
    openCfg = (openCfg === label) ? null : label;
    drawCfg(all);
  });
  drawCfg(all);

  $("#accounts").innerHTML = (d.accounts||[]).length
    ? d.accounts.map(a => {
        const st = a.status || (a.active ? "live" : "dead");
        // The word repeats what the colour says, for anyone who cannot rely on
        // colour — and says it in words rather than status codes.
        const label = {live:"Working", warning:"Check this", dead:"Signed out",
                       unknown:"Not set up"}[st] || st;
        const reasons = (a.reasons||[]).length
          ? `<div class="muted" style="margin:1px 0 5px 2px">`
            + a.reasons.map(r => `• ${esc(r)}`).join("<br>")
            + (a.action ? `<br><span style="opacity:.85">→ ${esc(a.action)}</span>` : "")
            + `</div>`
          : "";
        const needs = st === "dead" || st === "unknown";
        return `<div class="row">
            <span class="k">@${esc(a.username)}${a.proxy?' <span title="uses a proxy">⛓</span>':''}</span>
            <span class="flag ${st}">${label}</span>
          </div>${reasons}
          ${needs ? `<button data-signin="${esc(a.label||a.username)}"
             style="width:100%;margin:2px 0 8px;padding:4px;font-size:12px">
             Sign in to X</button>` : ""}`;
      }).join("")
    : '<span class="muted">none yet — add one below</span>';

  // Wire the sign-in buttons this pass just drew. status() replaces the whole
  // panel every 15s, so anything bound before now is gone with the old nodes.
  document.querySelectorAll("[data-signin]").forEach(b =>
    b.onclick = () => loginOpen(b.dataset.signin));

  // Both queues, named. They are separate allowances that do not share, so
  // showing one number invites spending the wrong budget.
  const QNAME = {search: "word searches", list: "lists"};
  const budget = Object.entries(d.budget || {}).map(([q, b]) =>
    `<div class="row"><span class="k">${QNAME[q] || q}</span>
       <span>${b.remaining} of ${b.limit}</span></div>` +
    (b.resets_in != null
      ? `<div class="muted" style="margin:-2px 0 4px 2px;font-size:11px">
           ${b.rolled ? "window reset — full again"
                      : `resets in ${secs(b.resets_in)}`}</div>` : "")
  ).join("");

  $("#totals").innerHTML =
    `<div class="row"><span class="k">tweets</span><span>${d.totals.tweets ?? 0}</span></div>` +
    (budget ? `<div class="cfghead" style="margin-top:6px">Requests left</div>` + budget : "") +
    (d.totals.note ? `<div class="muted">${esc(d.totals.note)}</div>` : "");
}

/* ------------------------------------------------------------------
   Per-stream settings, behind the gear.

   Kept collapsed and rendered on demand: the sidebar redraws every 15s, and
   an always-open form would fight whatever you were typing into it. Only the
   one you opened is built, and it survives the redraw because openCfg is
   module state rather than DOM state.
   ------------------------------------------------------------------ */
const SPEED_LABELS = {"":"leave as configured", fastest:"as fast as allowed (~5s)",
  fast:"every 15s or so", normal:"every minute or so", slow:"every 5 minutes or so",
  hourly:"every half hour or so"};

function drawCfg(streams){
  document.querySelectorAll("[data-cfg]").forEach(box => {
    const label = box.dataset.cfg;
    if (label !== openCfg){ box.hidden = true; box.innerHTML = ""; return; }
    const s = streams.find(x => x.label === label) || {};
    box.hidden = false;
    box.innerHTML = `
      <label class="cfgrow">How often to check
        <select data-k="speed">${Object.entries(SPEED_LABELS).map(([v,t]) =>
          `<option value="${v}" ${s.speed===v?"selected":""}>${t}</option>`).join("")}</select>
      </label>
      <label class="cfgrow">Tweets per check
        <select data-k="pages">
          <option value="">leave as configured</option>
          <option value="1"  ${s.pages===1?"selected":""}>about 20</option>
          <option value="5"  ${s.pages===5?"selected":""}>about 100</option>
          <option value="10" ${s.pages===10?"selected":""}>about 200</option>
        </select>
      </label>
      <label class="cfgchk"><input type="checkbox" data-k="paused" ${s.paused?"checked":""}>
        Pause — stop checking this for now</label>

      <div class="cfghead">Send to Telegram</div>
      <label class="cfgchk"><input type="checkbox" data-k="tg_enabled" ${s.tg_enabled?"checked":""}>
        Send these tweets to Telegram</label>
      <label class="cfgrow">Send where
        <input data-k="tg_chat_id" placeholder="default chat" value="${esc(s.tg_chat_id||"")}"></label>
      <label class="cfgrow">Only if it has at least
        <input data-k="tg_min_likes" type="number" min="0" style="width:70px"
               value="${s.tg_min_likes||0}"> likes</label>
      <label class="cfgchk"><input type="checkbox" data-k="tg_skip_retweets"
        ${s.tg_skip_retweets?"checked":""}> Skip retweets</label>

      <div class="cfghead">Remove</div>
      <button class="cfgbtn" data-act="stop">Stop watching — keep the tweets</button>
      <button class="cfgbtn danger" data-act="wipe">Delete this and its tweets</button>
      <div class="muted" style="margin-top:4px;font-size:11px">
        Stopping is reversible. Deleting is not — X only lets you look back
        about 7 days.</div>`;

    // Every control saves itself. A Save button here would be one more thing
    // to forget to press.
    box.querySelectorAll("[data-k]").forEach(el => el.onchange = async () => {
      const k = el.dataset.k;
      const v = el.type === "checkbox" ? el.checked : el.value;
      try {
        const r = await api("/api/stream/settings", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({label, [k]: v})
        });
        if (r.error) return banner(esc(r.error), "err");
        banner(`Saved for <b>${esc(label)}</b>.`, "ok");
      } catch (e) { return banner(esc(e.message), "err"); }
      await status();
    });

    box.querySelectorAll("[data-act]").forEach(b => b.onclick = () =>
      removeStream(label, b.dataset.act === "wipe", s.count || 0));
  });
}

async function removeStream(label, wipe, count){
  let body = {label};
  if (wipe){
    const typed = prompt(
      `Delete "${label}" AND its ${count} tweets?\n\n` +
      `This cannot be undone. X only lets you look back about 7 days, so ` +
      `anything older than that can never be collected again.\n\n` +
      `Tweets also matched by another list are kept.\n\n` +
      `Type the name to confirm:`);
    if (typed === null) return;
    body = {label, delete_tweets: true, confirm: typed.trim()};
  } else if (!confirm(`Stop watching "${label}"?\n\n` +
                      `Its ${count} tweets stay and are still searchable.`)) {
    return;
  }
  let d;
  try {
    d = await api("/api/stream/remove", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
  } catch (e) { return banner(esc(e.message), "err"); }
  if (d.error) return banner(esc(d.error), "err");

  if (activeStream === label) activeStream = "";
  openCfg = null;
  banner(wipe
    ? `Deleted <b>${esc(label)}</b> and ${d.tweets_deleted} tweet(s).`
    : `Stopped watching <b>${esc(label)}</b>. Its tweets are still here.`, "ok");
  await status(); await search();
}

$("#src").onchange = () => {
  const isList = $("#src").value === "list";
  $("#q").placeholder = isList
    ? "Paste an X list link — https://x.com/i/lists/1234567890"
    : "Type words to find in the tweets you have saved…";
  /* Lists are a far better deal than searches on BOTH counts, and the numbers
     are not close.
       requests : 500 per 15 min against 50
       per request : X returns exactly the 20 asked for on a search, and
                     ignores the count on a list — measured 78 to 92, so ~90.
     Quoting 20 per request for a list understated it by four and a half times:
     "5 pages, about 100 tweets" actually fetched 460. */
  const per = isList ? 90 : 20;
  [...$("#pages").options].forEach(o => {
    const n = parseInt(o.value, 10);
    o.textContent = `about ${n*per} tweets · uses ${n} of your `
      + (isList ? "500" : "50") + " requests";
  });
};
$("#src").onchange();

$("#go").onclick = search;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
["author","since","minlikes","lang","order","media","noretweets"].forEach(id =>
  $("#"+id).addEventListener("change", search));

$("#csv").onclick = () => { location = "/api/export?" + params(); };

$("#getnew").onclick = async () => {
  const raw = $("#q").value.trim();
  const isList = $("#src").value === "list";
  if (!raw) return banner(isList
      ? "Paste an X list link first."
      : "Type what to look for first.", "err");
  const query = isList ? "" : raw, listId = isList ? raw : "";
  const pages = parseInt($("#pages").value, 10);

  /* Ask the guard BEFORE spending anything. This is the whole point: the
     dangerous click is the one you make without knowing the cost. */
  let g;
  try { g = await api(`/api/guard?action=fetch&cost=${pages}&queue=${isList?"list":"search"}`); }
  catch (e) { return banner(esc(e.message).replace(/\n/g,"<br>"), "err"); }

  const blocks = g.findings.filter(f => f.level === "block");
  const warns  = g.findings.filter(f => f.level === "warn");

  if (blocks.length){
    return banner(
      `<b>Cannot do that right now — ${esc(blocks[0].title)}</b><br>${esc(blocks[0].detail)}` +
      (blocks[0].remedy ? `<br><b>What to do:</b> ${esc(blocks[0].remedy)}` : ""), "err");
  }

  let msg = `Ask X for about ${pages*20} tweets matching:\n\n${raw}\n\n` +
            `This uses ${pages} of your ${isList ? 500 : 50} requests ` +
            `for the next 15 minutes.\n`;
  if (warns.length){
    msg += "\nWorth knowing first:\n  • "
         + warns.map(w => w.title + (w.remedy ? `\n    ${w.remedy}` : "")).join("\n  • ") + "\n";
  }
  msg += "\nAnything found is saved. Go ahead?";
  if (!confirm(msg)) return;

  $("#getnew").disabled = true;
  banner("Asking X…");
  try {
    const d = await api("/api/fetch", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({query, list_id:listId, tab:"Latest", pages, ack:true})
    });

    if (d.error) banner("X said: " + esc(d.error), "err");
    else if (d.hint) banner(esc(d.hint), "err");
    else if (d.stop === "no_account_or_abort")
      banner("No X account was free to do this. Check the accounts panel, or " +
             "wait a few minutes for the request allowance to reset.", "err");
    else {
      let msg = `Found ${d.results} tweets — ${d.new} new, ${d.dup} you already had. ` +
                `${d.rl_remaining} of ${d.rl_limit} requests left for the next 15 minutes.`;
      if (d.stop === "exhausted" && d.pages < pages)
        msg += ` That is everything X has for this.`;
      banner(msg, "ok");
      activeStream = d.stream;
      $("#q").value = "";        // the search now lives as a filter on the left
    }
    await status(); await search();
  } catch (e) {
    banner(esc(e.message).replace(/\n/g,"<br>"), "err");
  } finally { $("#getnew").disabled = false; }
};

/* Click a thumbnail to fetch and play that one video.

   Delegated from #results rather than bound per card: the list is re-rendered
   on every search, every auto-update tick and every "show more", so handlers
   attached to individual cards would belong to nodes that no longer exist. */
$("#results").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-play]");
  if (!btn) return;
  const gif = btn.dataset.kind === "gif";
  const v = document.createElement("video");
  v.src = btn.dataset.play;
  v.controls = !gif; v.autoplay = true; v.playsInline = true;
  v.muted = gif; v.loop = gif;
  // If the URL has expired — X signs them and they do expire — say so instead
  // of leaving a black rectangle where the video was.
  v.onerror = () => {
    const a = document.createElement("a");
    a.className = "medialink"; a.target = "_blank"; a.rel = "noopener";
    a.href = btn.dataset.play;
    a.innerHTML = "<b>Video unavailable</b><span>X's link has expired — opens on x.com</span>";
    v.replaceWith(a);
  };
  btn.replaceWith(v);
});

/* Standing risk panel. The costly mistakes here are the silent ones, so the
   state that makes an action dangerous is always on screen — not only at the
   moment you click. */
async function risks(){
  let g;
  try { g = await api("/api/guard"); } catch { return; }
  const items = g.findings.filter(f => f.level !== "info");
  const box = $("#riskbox");
  if (!items.length){ box.hidden = true; return; }
  box.hidden = false;
  $("#risks").innerHTML = items.map(f => `
    <div style="margin-bottom:9px">
      <span class="pill ${f.level==='block'?'bad':''}"
            style="${f.level==='warn'?'background:rgba(224,168,0,.18);color:#b8860b':''}">
        ${f.level==='block'?'Stops work':'Worth fixing'}</span>
      <div style="margin-top:3px">${esc(f.title)}</div>
      ${f.remedy?`<div class="muted" style="margin-top:2px">→ ${esc(f.remedy)}</div>`:''}
    </div>`).join("");
}

$("#tgtoggle").onclick = () => {
  const box = $("#tgbox");
  box.hidden = !box.hidden;
  if (!box.hidden) $("#tg_token").focus();
};
$("#tg_save").onclick = async () => {
  $("#tg_save").disabled = true;
  try {
    const d = await api("/api/settings/telegram", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({token: $("#tg_token").value.trim(),
                            chat_id: $("#tg_chat").value.trim()})
    });
    if (d.error) return banner(esc(d.error), "err");
    // Never echo the token back into the page once it is stored.
    $("#tg_token").value = "";
    $("#tg_token").placeholder = d.has_token ? "saved — paste again to replace"
                                             : "bot token from @BotFather";
    banner("Telegram saved. Switch it on for a list with the ⚙ beside it.", "ok");
  } catch (e) { banner(esc(e.message), "err"); }
  finally { $("#tg_save").disabled = false; }
};
$("#tg_test").onclick = async () => {
  $("#tg_test").disabled = true;
  banner("Sending a test message…");
  try {
    const d = await api("/api/settings/telegram/test", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({chat_id: $("#tg_chat").value.trim()})
    });
    banner(d.error ? "Telegram said: " + esc(d.error)
                   : "Sent. Check Telegram — if nothing arrived, the chat id is wrong.",
           d.error ? "err" : "ok");
  } catch (e) { banner(esc(e.message), "err"); }
  finally { $("#tg_test").disabled = false; }
};

$("#acctnew").onclick = () => { $("#acctform").hidden = false; $("#a_label").focus(); };
$("#a_cancel").onclick = () => { $("#acctform").hidden = true; };
$("#a_save").onclick = async () => {
  const body = {
    label: $("#a_label").value.trim(), username: $("#a_user").value.trim(),
    proxy: $("#a_proxy").value.trim(),
  };
  if (!body.label) return banner("Give the account a short name first.", "err");
  $("#a_save").disabled = true;
  try {
    const d = await api("/api/account", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
    if (d.error) return banner(esc(d.error), "err");
    ["#a_label","#a_user","#a_proxy"].forEach(s => $(s).value = "");
    $("#acctform").hidden = true;
    banner("");
    await status();
    // Straight into signing in. An account that is saved but not signed in
    // collects nothing, so there is no reason to make that a second step —
    // and no reason to send anyone to a terminal for it.
    await loginOpen(d.label);
  } catch (e) { banner(esc(e.message), "err"); }
  finally { $("#a_save").disabled = false; }
};

/* ------------------------------------------------------------------
   Auto-update.

   This re-reads the LOCAL DATABASE on a timer. It never calls X — the
   watcher service already polls X continuously on its own adaptive
   interval, and new tweets land in results.db as it goes. Auto-fetching
   from the browser every 15s would be 240 requests/hour against a ceiling
   of ~200, so it would rate-limit the watcher out of existence within the
   hour. Reading the database is free and shows the same tweets.

   New rows are PREPENDED rather than replacing the list, so scroll
   position, "Show more" pages and reading position all survive a refresh.

   The checkbox is #autorefresh, NOT #live. It used to share the id "live"
   with the fetch button, so $("#live") returned the button, .checked was
   undefined, and tick() bailed on its first line every single time — the
   dot pulsed and nothing ever refreshed.
   ------------------------------------------------------------------ */
let liveTimer = null, pending = [], topId = null;

const bigger = (a, b) => {           // tweet ids exceed 2^53; compare as BigInt
  try { return BigInt(a) > BigInt(b); } catch { return false; }
};

function noteTop(rows){
  for (const r of rows) if (!topId || bigger(r.tweet_id, topId)) topId = r.tweet_id;
}

function showPending(){
  if (!pending.length) return;
  const html = pending.map(t => card(t).replace('class="card"', 'class="card new"')).join("");
  $("#results").insertAdjacentHTML("afterbegin", html);
  noteTop(pending);
  loaded += pending.length; lastTotal += pending.length; offset += pending.length;
  $("#count").textContent = `showing ${loaded} of ${lastTotal}`;
  pending = [];
  $("#newbar").style.display = "none";
}

async function tick(){
  if (!$("#autorefresh").checked || document.hidden) return;
  let d;
  try {
    const p = params(); p.set("limit", PAGE_SIZE); p.set("offset", 0);
    d = await api("/api/tweets?" + p);
  } catch { return; }            // a blip should not spam the banner

  const fresh = (d.rows || []).filter(r => !topId || bigger(r.tweet_id, topId));
  if (!fresh.length) return;

  // At the top of the page, drop them straight in; otherwise offer a button so
  // the page never jumps under someone who is reading.
  pending = fresh.concat(pending);
  if (window.scrollY < 80) {
    showPending();
  } else {
    $("#newbtn").textContent = `${pending.length} new tweet${pending.length>1?'s':''} — show them`;
    $("#newbar").style.display = "block";
  }
}

function setLive(on){
  $("#livedot").classList.toggle("on", on);
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = on ? setInterval(tick, parseInt($("#everysec").value, 10) * 1000) : null;
}
$("#autorefresh").onchange = () => setLive($("#autorefresh").checked);
$("#everysec").onchange    = () => setLive($("#autorefresh").checked);
$("#newbtn").onclick    = () => { showPending(); window.scrollTo({top: 0, behavior: "smooth"}); };
// A hidden tab should not keep querying; catch up the moment it is visible.
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

/* ------------------------------------------------------------------
   The sign-in window.

   The <img> is a live picture of a real Chrome running on the server.
   Clicks are scaled from the displayed size back to the real window size
   and forwarded; keystrokes go straight through. The password is typed
   into the genuine x.com form — this page never reads or stores it.

   As soon as X reports the account as signed in, the session is copied
   out and the browser is shut down. It exists only to get past the
   captcha and device checks that a plain script cannot clear.
   ------------------------------------------------------------------ */
let loginTimer = null, loginW = 1100, loginH = 780, loginDone = false;

function loginMsg(text, bad){
  $("#loginmsg").textContent = text;
  $("#loginmsg").style.color = bad ? "var(--warn)" : "var(--dim)";
}

function loginStopFrames(){
  if (loginTimer) { clearInterval(loginTimer); loginTimer = null; }
}

async function loginOpen(label){
  $("#loginwrap").classList.add("on");
  loginMsg("Starting a browser…");
  $("#logindone").hidden = true;
  loginDone = false;
  let d;
  try {
    d = await api("/api/login/start", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({label})
    });
  } catch (e) { return loginMsg(e.message, true); }
  if (d.error){ loginMsg(d.error, true); return; }

  loginW = d.width; loginH = d.height;
  loginMsg("Type your X username and password in the window below.");
  loginFrame();
  loginTimer = setInterval(loginFrame, 900);

  // The profile may already be signed in, in which case there is nothing for
  // anyone to do — capture it and close.
  if (d.state === "logged_in") loginAct({act:"none"});
}

function loginFrame(){
  if (loginDone) return;   // the browser is gone; asking again just 409s
  // Cache-buster: the URL is constant but the picture changes every frame.
  $("#loginimg").src = "/api/login/frame?t=" + Date.now();
}

async function loginAct(payload){
  if (loginDone) return;
  let d;
  try {
    d = await api("/api/login/act", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
  } catch (e) { return loginMsg(e.message, true); }

  if (d.error)  return loginMsg(d.error, true);
  if (d.closed) return loginClose();

  if (d.state === "challenge")
    loginMsg("X wants a code or a puzzle solved — do it in the window below.");
  else if (d.state === "needs_login")
    loginMsg("Type your X username and password in the window below.");

  if (d.captured){
    // The browser has already been shut down server-side at this point.
    loginDone = true;
    loginStopFrames();
    $("#logindone").hidden = false;
    if (d.active){
      loginMsg(`Signed in as @${d.username}. This account is ready to collect.`);
      banner(`@${esc(d.username)} is connected and collecting.`, "ok");
      await status();
    } else {
      loginMsg(`Signed in as @${d.username}, but the session could not be saved: `
               + d.detail, true);
    }
    return;
  }
  setTimeout(loginFrame, 250);
}

$("#loginimg").onclick = (e) => {
  const r = e.target.getBoundingClientRect();
  // The image is displayed scaled; clicks must land on the real pixels.
  const x = Math.round((e.clientX - r.left) * (loginW / r.width));
  const y = Math.round((e.clientY - r.top)  * (loginH / r.height));
  loginAct({act:"click", x, y});
};
$("#loginimg").onwheel = (e) => { e.preventDefault(); loginAct({act:"scroll", dy: e.deltaY}); };

document.addEventListener("keydown", (e) => {
  if (!$("#loginwrap").classList.contains("on")) return;
  if (e.key === "Escape") { e.preventDefault(); return loginClose(); }
  e.preventDefault();
  if (e.key.length === 1)        loginAct({act:"type", text:e.key});
  else if (["Enter","Backspace","Tab","ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Delete"].includes(e.key))
    loginAct({act:"key", key:e.key});
});
// Pasting a password is normal and safer than typing it.
document.addEventListener("paste", (e) => {
  if (!$("#loginwrap").classList.contains("on")) return;
  const text = (e.clipboardData || window.clipboardData).getData("text");
  if (text) { e.preventDefault(); loginAct({act:"type", text}); }
});

async function loginClose(){
  loginStopFrames();
  $("#loginwrap").classList.remove("on");
  // Always tell the server, even after a successful capture: the call is
  // idempotent, and a browser left running holds the account's profile
  // directory open, which blocks the next sign-in.
  try { await api("/api/login/cancel", {method:"POST",
        headers:{"Content-Type":"application/json"}, body:"{}"}); } catch {}
  loginDone = false;
  await status();
}
$("#loginx").onclick = loginClose;
$("#logindone").onclick = loginClose;

status(); search().then(() => setLive(true)); risks();
setInterval(() => { status(); risks(); }, 15000);
</script>
</body></html>
"""
