"""
web.py — local dashboard.

    python3 main.py serve          # then open http://127.0.0.1:8765

Zero new dependencies: stdlib http.server plus one self-contained HTML page.
No npm, no build step, nothing fetched from a CDN.

The design point that matters is the split between two very different actions:

  * "Search collected" reads results.db. Free, instant, unlimited. This is the
    default, and it is what you should be doing 99% of the time.
  * "Fetch from X" goes out to X's GraphQL endpoint. That costs one request per
    page from a budget of ~50 per 15 minutes PER ACCOUNT — the same budget the
    watcher needs to keep streams fresh. So it is never automatic, never fires
    on keystroke, and the remaining budget is shown before you spend it.

Binds to 127.0.0.1 by default. This serves your collected data and can spend
your rate-limit budget; it has no authentication, so do not expose it.
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


def _auth_configured() -> bool:
    return bool(os.getenv("DASH_USER") and os.getenv("DASH_PASSWORD"))


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

    sql = f"SELECT t.* FROM tweets t {joins} WHERE {' AND '.join(where)}"
    order = "ASC" if p.get("order") == "asc" else "DESC"
    limit = min(int(p.get("limit") or 50), 500)
    offset = int(p.get("offset") or 0)

    with _connect() as con:
        total = con.execute(
            f"SELECT COUNT(*) c FROM tweets t {joins} WHERE {' AND '.join(where)}", params
        ).fetchone()["c"]
        rows = con.execute(
            f"{sql} ORDER BY t.tweet_id {order} LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()

    return {"total": total, "rows": [_row_to_json(r) for r in rows]}


def _row_to_json(r):
    d = dict(r)
    for k in ("hashtags", "mentions", "urls", "media_urls"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (TypeError, ValueError):
            d[k] = []
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

    known = {a["username"].lower() for a in out["accounts"]}
    by_label = {a.get("label") for a in out["accounts"]}
    for acct in _CFG.accounts:
        if acct.label not in by_label:
            # In config.toml but never logged in. Showing it is the point:
            # otherwise "I added an account" and "it is collecting" look the same.
            out["accounts"].append({
                "username": acct.username or acct.label, "label": acct.label,
                "active": False, "status": "unknown",
                "reasons": ["configured but never logged in"],
                "action": f"python3 main.py login --account {acct.label}",
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
                lag = con.execute(
                    "SELECT t.lag_ms FROM tweet_hits h JOIN tweets t USING(tweet_id) "
                    "WHERE h.stream_id = ? AND t.created_ms >= COALESCE(?, 0) "
                    "ORDER BY h.first_seen_ms DESC LIMIT 200",
                    (s["stream_id"], s["first_poll_ms"]),
                ).fetchall()
                lags = sorted(x["lag_ms"] for x in lag)
                out["streams"].append({
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

        st = store_mod.Store(_CFG.db_results)
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


def _add_account(body):
    """
    Append an account to config.toml (and its password to .env).

    Writes secrets to disk, so it is localhost-only by contract — see the
    banner in serve(). It deliberately does NOT log in: that needs a real
    browser window a human can see, which a server process cannot assume it
    has. It writes the config and hands back the exact command to run.
    """
    import re as _re

    label = (body.get("label") or "").strip()
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", label):
        return {"error": "label must be 1-32 chars: letters, digits, _ or -"}

    username = (body.get("username") or "").strip().lstrip("@")
    password = body.get("password") or ""
    proxy = (body.get("proxy") or "").strip()

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
        return {"error": f"an account labelled {label!r} is already in config.toml"}

    env_key = f"X_PASSWORD_{label.upper().replace('-', '_')}"
    block = (
        f'\n[[accounts]]\n'
        f'label              = "{label}"\n'
        f'username           = "{username}"\n'
        f'password_env       = "{env_key}"\n'
        f'profile_dir        = "profiles/{label}"\n'
        f'proxy              = "{proxy}"\n'
        f'enabled            = true\n'
    )
    cfg_path.write_text(text.rstrip() + "\n" + block)

    wrote_pw = False
    if password:
        env_path = _CFG.root / ".env"
        cur = env_path.read_text() if env_path.exists() else ""
        # Only ACTIVE assignments count. .env ships with commented templates
        # like `# X_PASSWORD_ACCT_B=`, and a naive substring check treats those
        # as "already set" — the password is then silently not written and the
        # login fails later with no clue why.
        already = any(
            ln.strip().startswith(f"{env_key}=")
            for ln in cur.splitlines()
        )
        if not already:
            env_path.write_text(cur.rstrip() + f"\n{env_key}={password}\n")
            wrote_pw = True

    return {
        "ok": True, "label": label, "env_key": env_key, "wrote_password": wrote_pw,
        "next": f"python3 main.py login --account {label}",
        "note": "A Chrome window opens so you can clear the captcha / 2FA once. "
                "Restart `serve` afterwards to pick up the new config.",
    }


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
  <p class="sub">Sign in to continue.</p>
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
        if self._authed():
            return True
        if self.path.startswith("/api/"):
            self._send(401, {"error": "not signed in", "login": "/login"})
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
                f"Too many attempts. Try again in {wait // 60 + 1} minute(s)."),
                "text/html; charset=utf-8")

        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        user = (form.get("username") or [""])[0]
        pwd = (form.get("password") or [""])[0]

        if not _check_credentials(user, pwd):
            _record_failure(ip)
            left = MAX_ATTEMPTS - len(_attempts.get(ip, []))
            return self._send(401, self._login_html(
                "Wrong username or password."
                + (f" {left} attempt(s) left." if left <= 3 else "")),
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

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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
        log("[serve]   but DASH_USER / DASH_PASSWORD are not set, so there would be no login.")
        log("[serve]   Add to .env:")
        log("[serve]       DASH_USER=you")
        log("[serve]       DASH_PASSWORD=<a long random string>")
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
  .streambtn{display:block;width:100%;text-align:left;margin-bottom:5px;font-size:13px}
  .streambtn.active{border-color:var(--accent);background:rgba(29,155,240,.1)}
  code{background:var(--bg);padding:1px 5px;border-radius:5px;font-size:12px;
       border:1px solid var(--line)}
</style></head><body>

<header>
  <h1>X Collector</h1>
  <select id="src" title="Where 'Fetch from X' pulls from">
    <option value="search">Search</option>
    <option value="list">List</option>
  </select>
  <input id="q" placeholder="Search collected tweets — text, @author, keywords…" autofocus>
  <button class="primary" id="go">Search collected</button>
  <select id="pages" title="How many pages to pull from X. 1 request each.">
    <option value="1">1 page · ~20 tweets · 1 req</option>
    <option value="3">3 pages · ~60 tweets · 3 req</option>
    <option value="5" selected>5 pages · ~100 tweets · 5 req</option>
    <option value="10">10 pages · ~200 tweets · 10 req</option>
    <option value="25">25 pages · ~500 tweets · 25 req</option>
  </select>
  <button id="live" title="Spends rate-limit budget">Fetch from X</button>
  <button id="csv">Export CSV</button>
  <a href="/logout" class="muted" style="font-size:13px;margin-left:auto">Sign out</a>
</header>

<div class="filters">
  <input id="author" placeholder="@author" size="12">
  <select id="since">
    <option value="">any time</option><option value="1h">last hour</option>
    <option value="6h">last 6h</option><option value="24h">last 24h</option>
    <option value="7d">last 7 days</option>
  </select>
  <input id="minlikes" type="number" placeholder="min likes" size="8" style="width:110px">
  <input id="lang" placeholder="lang" size="4" style="width:70px">
  <label class="muted"><input type="checkbox" id="media"> media only</label>
  <select id="order"><option value="desc">newest first</option><option value="asc">oldest first</option></select>
  <span class="muted" id="count"></span>
</div>

<div id="banner"></div>

<div class="wrap">
  <main id="results"><p class="muted">Loading…</p></main>
  <aside>
    <div class="box" id="riskbox" hidden>
      <h2>Risks</h2>
      <div id="risks"></div>
    </div>
    <div class="box">
      <h2>Streams</h2>
      <div id="streams"><span class="muted">—</span></div>
    </div>
    <div class="box">
      <h2>Accounts</h2>
      <div id="accounts"><span class="muted">—</span></div>
      <button id="acctnew" style="width:100%;margin-top:8px;padding:5px;font-size:13px">
        + Add account</button>
      <div id="acctform" hidden style="margin-top:8px;display:grid;gap:5px">
        <input id="a_label" placeholder="label (e.g. acct_b)">
        <input id="a_user"  placeholder="@username">
        <input id="a_pass"  type="password" placeholder="password (stored in .env)">
        <input id="a_proxy" placeholder="proxy (optional)">
        <div style="display:flex;gap:6px">
          <button id="a_save" class="primary" style="flex:1;padding:5px;font-size:13px">Save</button>
          <button id="a_cancel" style="padding:5px 10px;font-size:13px">Cancel</button>
        </div>
      </div>
    </div>
    <div class="box">
      <h2>Collected</h2>
      <div id="totals"><span class="muted">—</span></div>
    </div>
    <div class="box">
      <h2>Query syntax</h2>
      <p class="muted" style="margin:0 0 6px">Fetching from X accepts its advanced-search operators:</p>
      <p class="muted" style="margin:0"><code>from:user</code> <code>min_faves:50</code>
      <code>lang:en</code> <code>-filter:replies</code> <code>filter:images</code>
      <code>since:2026-07-01</code></p>
    </div>
  </aside>
</div>

<script>
const $ = s => document.querySelector(s);
let activeStream = "";

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
    throw new Error(
      "Cannot reach the server. Is it still running?\n" +
      "Start it with:  python3 main.py serve");
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
    ? `showing ${loaded} of ${lastTotal}` : "0 matching";

  if (!d.rows || !d.rows.length){
    if (!append) $("#results").innerHTML = `<p class="muted">Nothing in the local database matches.
      ${$("#q").value.trim() ? 'Use <b>Fetch from X</b> to pull this query live.' : ''}</p>`;
    return;
  }
  const html = d.rows.map(card).join("");
  if (append) $("#more")?.remove(), $("#results").insertAdjacentHTML("beforeend", html);
  else $("#results").innerHTML = html;

  if (loaded < lastTotal){
    $("#results").insertAdjacentHTML("beforeend",
      `<button id="more" style="width:100%;padding:10px">Load ${
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
  const urls = t.media_urls || [];
  const bits = [];

  for (const u of urls){
    if (/\.(jpg|jpeg|png|webp)(\?|$)/i.test(u))
      bits.push(`<img src="${esc(u)}" loading="lazy" alt="">`);
    else if (/\.mp4(\?|$)/i.test(u))
      bits.push(`<video src="${esc(u)}" controls preload="metadata"
                        playsinline muted loop></video>`);
    else if (/\.m3u8(\?|$)/i.test(u))
      bits.push(`<a class="medialink" href="${esc(u)}" target="_blank" rel="noopener">
                   <b>HLS stream</b><span>not playable inline — open externally</span></a>`);
  }

  for (const u of (t.urls || [])){
    const yt = u.match(YT);
    if (yt){
      bits.push(`<iframe class="yt" src="https://www.youtube-nocookie.com/embed/${esc(yt[1])}"
                  loading="lazy" allowfullscreen
                  referrerpolicy="strict-origin-when-cross-origin"></iframe>`);
    } else if (/x\.com\/i\/broadcasts\//.test(u) || /pscp\.tv/.test(u)){
      bits.push(`<a class="medialink live" href="${esc(u)}" target="_blank" rel="noopener">
                   <b>● LIVE broadcast</b>
                   <span>X blocks embedding these — opens on x.com</span></a>`);
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
      ${t.lag_ms != null ? `<span title="time from posted to collected">⏱ ${(t.lag_ms/1000).toFixed(1)}s</span>` : ""}
      <a href="${esc(t.url)}" target="_blank" rel="noopener">open ↗</a>
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

  $("#streams").innerHTML = (d.streams||[]).length
    ? `<button class="streambtn ${activeStream?'':'active'}" data-s="">All streams</button>` +
      d.streams.map(s => `<button class="streambtn ${activeStream===s.label?'active':''}"
        data-s="${esc(s.label)}" title="${esc(s.query)}">
        ${esc(s.label)} <span class="muted">· ${s.count}</span>
        ${s.lag_p50!=null ? `<span class="muted">· p50 ${s.lag_p50.toFixed(0)}s</span>`:''}
        </button>`).join("")
    : '<span class="muted">none yet</span>';
  document.querySelectorAll(".streambtn").forEach(b =>
    b.onclick = () => { activeStream = b.dataset.s; status(); search(); });

  $("#accounts").innerHTML = (d.accounts||[]).length
    ? d.accounts.map(a => {
        const st = a.status || (a.active ? "live" : "dead");
        const label = {live:"LIVE", warning:"WARN", dead:"DEAD", unknown:"?"}[st] || st;
        const reasons = (a.reasons||[]).length
          ? `<div class="muted" style="margin:1px 0 5px 2px">`
            + a.reasons.map(r => `• ${esc(r)}`).join("<br>")
            + (a.action ? `<br><span style="opacity:.85">→ ${esc(a.action)}</span>` : "")
            + `</div>`
          : "";
        return `<div class="row">
            <span class="k">@${esc(a.username)}${a.proxy?' <span title="has a proxy">⛓</span>':''}</span>
            <span class="flag ${st}">${label}</span>
          </div>${reasons}`;
      }).join("")
    : '<span class="muted">no accounts — run <code>login</code></span>';

  const s0 = (d.streams||[]).find(s => s.rl_limit);
  $("#totals").innerHTML =
    `<div class="row"><span class="k">tweets</span><span>${d.totals.tweets ?? 0}</span></div>` +
    (s0 ? `<div class="row"><span class="k">rate limit left</span>
           <span>${s0.rl_remaining}/${s0.rl_limit}</span></div>` : "") +
    (d.totals.note ? `<div class="muted">${esc(d.totals.note)}</div>` : "");
}

$("#src").onchange = () => {
  const isList = $("#src").value === "list";
  $("#q").placeholder = isList
    ? "Paste an X List URL or id — https://x.com/i/lists/1234567890"
    : "Search collected tweets — text, @author, keywords…";
  /* Lists get 500 requests per 15 min against search's 50, so the same page
     count is a far smaller share of the budget. Say so where it is decided. */
  [...$("#pages").options].forEach(o => {
    const n = parseInt(o.value, 10);
    o.textContent = `${n} page${n>1?'s':''} · ~${n*20} tweets · ${n} req`
      + (isList ? " (of 500)" : " (of 50)");
  });
};
$("#src").onchange();

$("#go").onclick = search;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
["author","since","minlikes","lang","order","media"].forEach(id =>
  $("#"+id).addEventListener("change", search));

$("#csv").onclick = () => { location = "/api/export?" + params(); };

$("#live").onclick = async () => {
  const raw = $("#q").value.trim();
  const isList = $("#src").value === "list";
  if (!raw) return banner(isList
      ? "Paste an X List URL or id first."
      : "Type a query first. Fetching from X uses X's advanced-search syntax.", "err");
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
      `<b>Blocked — ${esc(blocks[0].title)}</b><br>${esc(blocks[0].detail)}` +
      (blocks[0].remedy ? `<br><b>Fix:</b> ${esc(blocks[0].remedy)}` : ""), "err");
  }

  let msg = `Fetch from X — ${pages} page${pages>1?'s':''} (~${pages*20} tweets), ` +
            `${pages} request${pages>1?'s':''}.\n\n${query}\n`;
  if (warns.length){
    msg += "\n⚠  " + warns.map(w => w.title + (w.remedy ? `\n   → ${w.remedy}` : "")).join("\n⚠  ") + "\n";
  }
  msg += "\nResults are saved into your database. Proceed?";
  if (!confirm(msg)) return;

  $("#live").disabled = true;
  banner(`Fetching ${pages} page${pages>1?'s':''} from X…`);
  try {
    const d = await api("/api/fetch", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({query, list_id:listId, tab:"Latest", pages, ack:true})
    });

    if (d.error) banner("X returned an error: " + esc(d.error), "err");
    else if (d.hint) banner(esc(d.hint), "err");
    else if (d.stop === "no_account_or_abort")
      banner("No account was available — the pool is starved, not the stream. " +
             "Check Accounts, or wait for the rate-limit window to reset.", "err");
    else {
      let msg = `Fetched ${d.results} results over ${d.pages} page(s) — ` +
                `${d.new} new, ${d.dup} already had. ` +
                `Rate limit now ${d.rl_remaining}/${d.rl_limit}.`;
      if (d.stop === "exhausted" && d.pages < pages)
        msg += ` X ran out of results after ${d.pages} page(s) — that is everything it has for this query.`;
      banner(msg, "ok");
      activeStream = d.stream;
      $("#q").value = "";        // the query now lives as a stream filter
    }
    await status(); await search();
  } catch (e) {
    banner(esc(e.message).replace(/\n/g,"<br>"), "err");
  } finally { $("#live").disabled = false; }
};

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
        ${f.level==='block'?'BLOCK':'WARN'}</span>
      <div style="margin-top:3px">${esc(f.title)}</div>
      ${f.remedy?`<div class="muted" style="margin-top:2px">→ ${esc(f.remedy)}</div>`:''}
    </div>`).join("");
}

$("#acctnew").onclick = () => { $("#acctform").hidden = false; $("#a_label").focus(); };
$("#a_cancel").onclick = () => { $("#acctform").hidden = true; };
$("#a_save").onclick = async () => {
  const body = {
    label: $("#a_label").value.trim(), username: $("#a_user").value.trim(),
    password: $("#a_pass").value, proxy: $("#a_proxy").value.trim(),
  };
  if (!body.label) return banner("A label is required.", "err");
  try {
    const d = await api("/api/account", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
    if (d.error) return banner(esc(d.error), "err");
    banner(`Added <b>${esc(d.label)}</b> to config.toml`
      + (d.wrote_password ? ` (password saved as ${esc(d.env_key)})` : "")
      + `.<br>Now run: <code>${esc(d.next)}</code><br>${esc(d.note)}`, "ok");
    ["#a_label","#a_user","#a_pass","#a_proxy"].forEach(s => $(s).value = "");
    $("#acctform").hidden = true;
    await status();
  } catch (e) { banner(esc(e.message), "err"); }
};

status(); search(); risks();
setInterval(() => { status(); risks(); }, 15000);
</script>
</body></html>
"""
