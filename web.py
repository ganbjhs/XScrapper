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
import datetime
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
from pathlib import Path
from urllib.parse import unquote

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


def _day_ms(value: str):
    """Midnight UTC for a yyyy-mm-dd date input, or None if it is not one.

    Returns None rather than raising: a half-typed date in a live-filtering box
    should narrow nothing, not blank the page with an error.
    """
    try:
        d = datetime.datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return int(d.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _query_tweets(p):
    """Filter the collected tweets. All of this is local; nothing touches X."""
    where, params = ["t.source = 'result'"], []
    joins = ""

    if p.get("stream"):
        joins = "JOIN tweet_hits h USING(tweet_id) JOIN streams s USING(stream_id)"
        where.append("s.label = ?")
        params.append(p["stream"])

    if p.get("project"):
        # Everything any of this project's streams collected. EXISTS rather
        # than a join so a tweet hit by two of the project's streams appears
        # once, not twice.
        try:
            pid = int(p["project"])
        except (TypeError, ValueError):
            pid = 0
        if pid:
            where.append(
                "EXISTS (SELECT 1 FROM tweet_hits ph JOIN project_streams ps "
                "        ON ps.stream_id = ph.stream_id "
                "        WHERE ph.tweet_id = t.tweet_id AND ps.project_id = ?)")
            params.append(pid)

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

    if p.get("min_views"):
        where.append("t.view_count >= ?")
        params.append(int(p["min_views"]))

    if p.get("min_followers"):
        where.append("t.author_followers >= ?")
        params.append(int(p["min_followers"]))

    # Explicit date bounds, as dd/mm/yyyy-free ISO from the date inputs.
    # `to` is inclusive of the whole day, which is what someone picking a date
    # means — a bare midnight bound silently drops everything posted that day.
    if p.get("from_date"):
        ms = _day_ms(p["from_date"])
        if ms is not None:
            where.append("t.created_ms >= ?")
            params.append(ms)
    if p.get("to_date"):
        ms = _day_ms(p["to_date"])
        if ms is not None:
            where.append("t.created_ms < ?")
            params.append(ms + 86_400_000)

    # Verification and account category come out of the stored raw tweet, not
    # a column: twscrape carries user.blue / user.blueType and we keep the full
    # JSON, so these are REAL values rather than a guess. `verified` is the old
    # legacy flag and is False on essentially every modern account — `blue` is
    # the checkmark people actually mean, so that is what this filters on.
    if p.get("verified"):
        where.append(
            "json_extract(COALESCE(r.raw_json, t.raw_json), '$.user.blue') = 1")

    if p.get("category"):
        where.append(
            "json_extract(COALESCE(r.raw_json, t.raw_json), '$.user.blueType') = ?")
        params.append(p["category"])

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

    # raw_json lives in tweet_raw (8c416bf) — LEFT JOIN it and extract ONLY
    # the avatar field in SQL, so the feed keeps its slim rows but the
    # profile picture still arrives (RULEBOOK §6 X).
    sql = (f"SELECT t.*, "
           f"  json_extract(COALESCE(r.raw_json, t.raw_json), "
           f"               '$.user.profileImageUrl') AS author_avatar "
           f"FROM tweets t LEFT JOIN tweet_raw r USING(tweet_id) "
           f"{joins} WHERE {' AND '.join(where)}")
    # A cursor walk has to run oldest-first, or "the last row I got" is not a
    # position you can resume from.
    order = "ASC" if (p.get("order") == "asc" or cursoring) else "DESC"
    order_by = "t.collected_ms, t.tweet_id" if p.get("since_collected_ms") else "t.tweet_id"
    # Engagement sorts, whitelisted — never while cursoring: a cursor position
    # only means something on a stable time order.
    _SORTS = {"likes": "t.like_count DESC, t.tweet_id DESC",
              "views": "t.view_count DESC, t.tweet_id DESC"}
    if not cursoring and p.get("sort") in _SORTS:
        order_by, order = _SORTS[p["sort"]], ""
    limit = min(int(p.get("limit") or 50), 500)
    offset = int(p.get("offset") or 0)

    with _connect() as con:
        total = con.execute(
            f"SELECT COUNT(*) c FROM tweets t "
            f"LEFT JOIN tweet_raw r USING(tweet_id) "
            f"{joins} WHERE {' AND '.join(where)}", params
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
    # The author's profile picture. The payload lives in tweet_raw since
    # 8c416bf, so queries feeding this function extract it in SQL
    # (json_extract over the LEFT JOIN — see _query_tweets / _live_rows /
    # store.collection_rows). Only FALL BACK to parsing an inline raw_json
    # here, and never clobber an avatar the query already provided.
    if not d.get("author_avatar"):
        try:
            d["author_avatar"] = (json.loads(d.get("raw_json") or "{}")
                                  .get("user", {}).get("profileImageUrl"))
        except (TypeError, ValueError, AttributeError):
            d["author_avatar"] = None
    # JS loses integer precision above 2^53, and tweet ids are well past it.
    d["tweet_id"] = str(d["tweet_id"])
    d.pop("raw_json", None)
    d.pop("raw_entry_json", None)
    return d


def _x_avatars_for(handles):
    """
    The latest known X profile picture per handle (case-insensitive map:
    handle → URL). Public figures use the SAME photo on every platform, and X
    avatars arrive free with every collected tweet — so X is the canonical
    avatar source, and a Facebook/Instagram post whose handle matches an X
    account we collect is shown with that picture. No extra fetching anywhere:
    this reads only what the X collector already stored.
    """
    out = {}
    wanted = {str(h).lower() for h in handles if h}
    if not wanted or not _CFG.db_results.exists():
        return out
    try:
        with _connect() as con:
            for h in wanted:
                row = con.execute(
                    "SELECT json_extract(COALESCE(r.raw_json, t.raw_json), "
                    "       '$.user.profileImageUrl') AS av "
                    "FROM tweets t LEFT JOIN tweet_raw r USING(tweet_id) "
                    "WHERE t.author_username = ? COLLATE NOCASE "
                    "ORDER BY t.created_ms DESC LIMIT 1", (h,)).fetchone()
                if row and row["av"]:
                    out[h] = row["av"]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Shared identity: one editable "display name" per source links handles ACROSS
# platforms. Two handles with the SAME name are the same person, so a Facebook
# or Instagram post with no picture borrows the X avatar of the X author who
# carries that display name. X avatars arrive free with every tweet, so this
# fixes cross-handle profile pictures with no extra fetching and no migration.
# ---------------------------------------------------------------------------

def _names_con():
    """Writable results.db connection that ensures the handle_names table."""
    con = sqlite3.connect(_CFG.db_results, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS handle_names ("
                "platform TEXT NOT NULL, handle TEXT NOT NULL, "
                "display_name TEXT NOT NULL, updated_ms INTEGER NOT NULL, "
                "PRIMARY KEY (platform, handle))")
    return con


def _set_handle_name(platform, handle, display_name):
    con = _names_con()
    try:
        h = str(handle or "").strip().lower().lstrip("@")
        nm = str(display_name or "").strip()
        if not h:
            return
        if nm:
            con.execute(
                "INSERT INTO handle_names(platform, handle, display_name, updated_ms) "
                "VALUES(?,?,?,?) ON CONFLICT(platform, handle) DO UPDATE SET "
                "display_name = excluded.display_name, updated_ms = excluded.updated_ms",
                (platform, h, nm, int(time.time() * 1000)))
        else:
            con.execute("DELETE FROM handle_names WHERE platform=? AND handle=?",
                        (platform, h))
        con.commit()
    finally:
        con.close()


def _handle_names_map(platform):
    """{handle_lower: display_name} for a platform."""
    try:
        con = _names_con()
        try:
            return {r["handle"]: r["display_name"] for r in con.execute(
                "SELECT handle, display_name FROM handle_names WHERE platform = ?",
                (platform,))}
        finally:
            con.close()
    except Exception:
        return {}


def _x_avatars_by_name(names):
    """{display_name_lower: X avatar URL} — latest avatar of the X author whose
    DISPLAY NAME matches. Reads only what the X collector already stored."""
    out = {}
    wanted = {str(n).strip().lower() for n in names if n and str(n).strip()}
    if not wanted or not _CFG.db_results.exists():
        return out
    try:
        with _connect() as con:
            for nm in wanted:
                row = con.execute(
                    "SELECT json_extract(COALESCE(r.raw_json, t.raw_json), "
                    "       '$.user.profileImageUrl') AS av "
                    "FROM tweets t LEFT JOIN tweet_raw r USING(tweet_id) "
                    "WHERE t.author_display_name = ? COLLATE NOCASE "
                    "ORDER BY t.created_ms DESC LIMIT 1", (nm,)).fetchone()
                if row and row["av"]:
                    out[nm] = row["av"]
    except Exception:
        pass
    return out


def _fill_avatars_by_name(posts, platform, handle_of):
    """For posts still missing an avatar, resolve it via the source's display
    name → the X author with that name. `handle_of(post)` returns the post's
    source handle, lowercased."""
    names = _handle_names_map(platform)
    if not names:
        return
    want = {names[h] for h in
            (handle_of(p) for p in posts if not p.get("author_avatar"))
            if h in names}
    if not want:
        return
    avby = _x_avatars_by_name(want)
    for p in posts:
        if p.get("author_avatar"):
            continue
        nm = names.get(handle_of(p))
        if nm and avby.get(nm.lower()):
            p["author_avatar"] = avby[nm.lower()]


def _status():
    """Accounts, streams, budget, totals — everything the sidebar shows."""
    import auth

    out = {"accounts": [], "streams": [], "totals": {}, "db": str(_CFG.db_results)}

    # Is anything actually collecting? The dashboard only reads the database;
    # without a live watcher the whole page is a photograph, not a feed — and
    # that difference must be shouted, not inferred. (This is exactly the
    # confusion a real user hit: settings tuned, dashboard open, watcher never
    # started, and nothing said so.)
    try:
        out["watcher_pid"] = auth.read_watcher_pid(_CFG.root)
    except Exception:
        out["watcher_pid"] = None

    # The Start/Stop toggle's state.
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'collection_paused'").fetchone()
            out["collection_paused"] = bool(row and row["value"] == "1")
    except Exception:
        out["collection_paused"] = False

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
    for acct in _CFG.accounts_for("x"):
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

    # Instagram, from its own store. Kept in a separate list rather than mixed
    # into out["accounts"]: they authenticate differently, fail differently and
    # are read by different code, so flattening them would only invite one to
    # be treated as the other.
    out["instagram"] = []
    try:
        import ig
        declared = {a.label: a for a in _CFG.accounts_for("instagram")}
        path = _CFG.root / "ig_accounts.db"
        rows = []
        if path.exists():
            with ig.Store(path) as st:
                rows = st.all()
        for r in rows:
            out["instagram"].append({
                "username": r["username"], "label": r["label"],
                "active": bool(r["active"]), "error": r["error_msg"],
                "requests": r["total_req"], "last_used": r["last_used"],
                "proxy": bool(r["proxy"]),
                "status": "live" if r["active"] else "dead",
                "reasons": [] if r["active"] else [r["error_msg"] or "not signed in"],
                "action": "" if r["active"] else "Press Sign in to Instagram.",
            })
        # Declared but never signed in — shown for the same reason as on the X
        # side: "I added it" and "it is collecting" must not look identical.
        seen = {r["label"] for r in rows}
        for label, acct in declared.items():
            if label not in seen:
                out["instagram"].append({
                    "username": acct.username or label, "label": label,
                    "active": False, "status": "unknown",
                    "reasons": ["added, but not signed in to Instagram yet"],
                    "action": "Press Sign in to Instagram.",
                    "requests": 0, "proxy": bool(acct.proxy),
                })
    except Exception as e:
        out["instagram_error"] = f"{type(e).__name__}: {e}"

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
                    # Has the COLLECTOR ever polled this, as opposed to it being
                    # a one-off search the dashboard recorded? The sidebar is
                    # titled "what we are watching" and used to show both kinds
                    # identically, so an ad-hoc search looked like a live
                    # subscription — you could switch Telegram on for it and
                    # wait forever for a tweet that was never going to be
                    # collected. Surfacing the difference is the fix.
                    "watched": watched,
                    "paused": bool(_col("paused", 0)),
                    "speed": speed,
                    "pages": _col("max_pages_per_poll"),
                    "tg_enabled": bool(_col("tg_enabled", 0)),
                    "tg_chat_id": _col("tg_chat_id", ""),
                    "tg_min_likes": _col("tg_min_likes", 0),
                    "tg_skip_retweets": bool(_col("tg_skip_retweets", 0)),
                    "tg_skip_replies": bool(_col("tg_skip_replies", 0)),
                    "tg_max_age_h": _col("tg_max_age_h", 0),
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


def _project_fetch(body):
    """
    The Refresh button's real work: poll each of the project's live streams
    ONCE, one page each, right now — then the watcher's normal cadence
    carries on. Same guard, same lock, same engine as /api/fetch, so a click
    can never spend budget the guard would have refused.
    """
    import auth
    import guard
    from collector import poll_once
    from config import StreamCfg
    from engine import Engine

    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    if not pid:
        return {"error": "which project?"}

    if not _CFG.db_results.exists():
        return {"error": "nothing has been collected yet — create a watchlist first"}
    with _connect() as con:
        rows = con.execute(
            "SELECT s.* FROM streams s JOIN project_streams ps USING(stream_id) "
            "WHERE ps.project_id = ? AND s.paused = 0 "
            "AND (s.query != '' OR s.list_id IS NOT NULL) "
            "ORDER BY s.stream_id LIMIT 8", (pid,)).fetchall()
    if not rows:
        return {"error": "this project has no live streams — create a watchlist first"}

    v = guard.assess(_CFG, action="fetch", cost=len(rows))
    if v.blocked:
        return {"error": v.summary(), "blocked": True, "guard": v.to_json()}
    if v.warnings and not bool(body.get("ack")):
        return {"error": "Warnings not acknowledged: "
                         + "; ".join(w.title for w in v.warnings),
                "blocked": True, "needs_ack": True, "guard": v.to_json()}

    async def run():
        api = auth.open_api(_CFG.db_accounts)
        names = await auth.active_usernames(api)
        if not names:
            return {"error": "No active X account is signed in — see Accounts & Sessions."}
        st = store_mod.Store(_CFG.db_results, _CFG.defaults.keep_entry_json)
        await st.open()
        try:
            eng = Engine(api)
            total_new = total_pages = 0
            polled = []
            for r in rows:
                s = StreamCfg(label=r["label"], query=r["query"] or "",
                              list_id=r["list_id"] or "", tab=r["tab"] or "Latest")
                s.max_pages_per_poll = 1
                res = await poll_once(eng, st, s, r["stream_id"])
                total_new += res.new
                total_pages += res.pages
                polled.append({"stream": r["label"], "new": res.new,
                               "error": res.error})
            return {"new": total_new, "streams": len(rows),
                    "pages": total_pages, "polled": polled}
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

    platform = (body.get("platform") or "x").strip().lower()
    if platform not in ("x", "instagram"):
        return {"error": f"unknown platform {platform!r}"}

    block = (
        f'\n[[accounts]]\n'
        f'label              = "{label}"\n'
        f'platform           = "{platform}"\n'
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

    return {"ok": True, "label": label, "platform": platform}


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
SPEEDS = {"fastest": 5, "fast": 15, "normal": 60, "slow": 300,
          "quarter": 900, "hourly": 1800}


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
    if "tg_skip_replies" in body:
        vals["tg_skip_replies"] = int(bool(body["tg_skip_replies"]))
    if "tg_max_age_h" in body:
        try:
            vals["tg_max_age_h"] = max(0, int(body.get("tg_max_age_h") or 0))
        except (TypeError, ValueError):
            return {"error": "hours must be a whole number (0 = no limit)"}

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


# --------------------------------------------------------------------------
# the live stream (SSE)
# --------------------------------------------------------------------------

# How often the stream re-checks the database. The collector's own floor is
# 5s between polls, so 1.5s here means a post is on screen within a couple of
# seconds of being stored while costing one cheap local read per tick.
LIVE_TICK_S = 1.5


def _live_rows(con, last_ms: int, last_tweet_id: int, project: int = 0,
               limit: int = 50) -> list:
    """
    Everything collected past the cursor, oldest first — the identical
    composite-cursor walk the webhook sender does (see store.tweets_after),
    so the live feed can never disagree with delivery about what "new" means.
    Factored out of the SSE loop so the test suite can exercise it without
    holding a streaming connection open.
    """
    where = ["t.source = 'result'",
             "(t.collected_ms > ? OR (t.collected_ms = ? AND t.tweet_id > ?))"]
    params = [last_ms, last_ms, last_tweet_id]
    if project:
        where.append(
            "EXISTS (SELECT 1 FROM tweet_hits ph JOIN project_streams ps "
            "        ON ps.stream_id = ph.stream_id "
            "        WHERE ph.tweet_id = t.tweet_id AND ps.project_id = ?)")
        params.append(project)
    rows = con.execute(
        f"SELECT t.*, "
        f"  json_extract(COALESCE(r.raw_json, t.raw_json), "
        f"               '$.user.profileImageUrl') AS author_avatar "
        f"FROM tweets t LEFT JOIN tweet_raw r USING(tweet_id) "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY t.collected_ms, t.tweet_id LIMIT ?", [*params, limit]).fetchall()
    return [_row_to_json(r) for r in rows]


# --------------------------------------------------------------------------
# read-only views the SPA needs: delivery, activity, metrics
# --------------------------------------------------------------------------

def _delivery_json(q=None):
    """
    Where every delivery target stands. The number that matters is `behind`:
    how many collected tweets sit past the target's cursor — the same
    composite-cursor arithmetic the sender itself uses, so the dashboard and
    the sender can never disagree about what "caught up" means.

    ?project=N narrows to that project's own targets plus the global ones
    (config.toml webhooks and per-stream Telegram), which are marked as such.
    """
    out = {"targets": []}
    if not _CFG.db_results.exists():
        return out
    try:
        pid = int((q or {}).get("project") or 0)
    except (TypeError, ValueError):
        pid = 0

    targets = []

    with _connect() as con:
        # Which projects each stream feeds — a stream-scoped target belongs to
        # exactly those projects' Delivery pages, not to everyone's.
        stream_projects: dict = {}
        try:
            for r in con.execute(
                    "SELECT s.label, ps.project_id FROM streams s "
                    "JOIN project_streams ps USING(stream_id)"):
                stream_projects.setdefault(r["label"], set()).add(r["project_id"])
        except sqlite3.Error:
            pass

        def _projects_of(labels):
            got = set()
            for lbl in labels:
                got |= stream_projects.get(lbl, set())
            return got

        try:
            for h in _CFG.enabled_webhooks():
                projs = _projects_of(h.streams or [])
                if pid and h.streams and projs and pid not in projs:
                    continue      # scoped to streams in OTHER projects
                targets.append({"label": h.label, "name": h.label, "kind": "webhook",
                                "url": h.url, "streams": list(h.streams or []),
                                "scope": "global" if not h.streams else sorted(projs) or "global",
                                "enabled": True})
        except Exception:
            pass

        try:
            for r in con.execute(
                    "SELECT label FROM streams WHERE tg_enabled = 1 ORDER BY label"):
                projs = stream_projects.get(r["label"], set())
                if pid and projs and pid not in projs:
                    continue      # this stream's Telegram belongs elsewhere
                targets.append({"label": f"tg:{r['label']}", "name": r["label"],
                                "kind": "telegram", "url": "Telegram",
                                "streams": [r["label"]],
                                "scope": sorted(projs) or "global",
                                "enabled": True})
        except sqlite3.Error:
            pass
        try:
            for r in con.execute(
                    "SELECT * FROM delivery_targets "
                    + ("WHERE project_id = ? " if pid else "")
                    + "ORDER BY target_id", ([pid] if pid else [])):
                targets.append({
                    "label": f"dt:{r['target_id']}", "name": r["name"],
                    "target_id": r["target_id"], "kind": r["kind"],
                    "url": r["url"] or (r["chat_id"] and f"Telegram {r['chat_id']}") or "",
                    "streams": [], "scope": r["project_id"],
                    "project_id": r["project_id"], "enabled": bool(r["enabled"]),
                    "secret_env": r["secret_env"], "chat_id": r["chat_id"],
                    "secret_ready": bool(
                        r["kind"] != "webhook"
                        or os.getenv(r["secret_env"] or "", "").strip()),
                })
        except sqlite3.Error:
            pass

        for t in targets:
            row = con.execute("SELECT * FROM webhook_state WHERE label = ?",
                              (t["label"],)).fetchone()
            cur = dict(row) if row else {
                "last_ms": 0, "last_tweet_id": 0, "sent": 0, "failures": 0,
                "next_attempt_ms": 0, "last_error": None, "last_ok_ms": None}

            where = ["t.source = 'result'",
                     "(t.collected_ms > ? OR (t.collected_ms = ? AND t.tweet_id > ?))"]
            params = [cur["last_ms"], cur["last_ms"], cur["last_tweet_id"]]
            if t["streams"]:
                where.append(
                    "EXISTS (SELECT 1 FROM tweet_hits h JOIN streams s USING(stream_id) "
                    "        WHERE h.tweet_id = t.tweet_id AND s.label IN "
                    f"       ({','.join('?' * len(t['streams']))}))")
                params += t["streams"]
            if t.get("project_id"):
                where.append(
                    "EXISTS (SELECT 1 FROM tweet_hits ph JOIN project_streams ps "
                    "        ON ps.stream_id = ph.stream_id "
                    "        WHERE ph.tweet_id = t.tweet_id AND ps.project_id = ?)")
                params.append(t["project_id"])
            behind = 0
            if cur["last_ms"] or cur["sent"]:
                behind = con.execute(
                    f"SELECT COUNT(*) c FROM tweets t WHERE {' AND '.join(where)}",
                    params).fetchone()["c"]

            out["targets"].append({
                **t, "sent": cur["sent"], "failures": cur["failures"],
                "behind": behind, "last_ok_ms": cur["last_ok_ms"],
                "last_error": cur["last_error"],
                "started": bool(cur["last_ms"] or cur["sent"]),
            })
    return out


def _activity_json(q):
    """The recent poll history — what the collector actually did, and when."""
    if not _CFG.db_results.exists():
        return {"polls": []}
    limit = min(int(q.get("limit") or 100), 500)
    with _connect() as con:
        rows = con.execute(
            "SELECT p.poll_id, s.label, p.kind, p.started_ms, p.finished_ms, "
            "       p.account, p.pages, p.results, p.new_tweets, p.stop_reason, "
            "       p.lag_p50_ms, p.error "
            "FROM polls p JOIN streams s USING(stream_id) "
            "ORDER BY p.poll_id DESC LIMIT ?", (limit,)).fetchall()
    return {"polls": [dict(r) for r in rows]}


def _activity_logs_json(q):
    """
    The raw account-activity log: every line the collectors and engines write
    while acting as the burner accounts — session reuse, login attempts,
    logged-out walls, fetches, avatar captures, errors. This is the "what are
    the accounts actually doing" view; filters: ?platform=facebook|instagram,
    ?level=info|warn|error, ?limit=N.
    """
    import activity_log
    try:
        limit = min(int(q.get("limit") or 300), 1000)
    except (TypeError, ValueError):
        limit = 300
    platform = (q.get("platform") or "").strip().lower() or None
    level = (q.get("level") or "").strip().lower() or None
    events = activity_log.recent(limit=limit, platform=platform, level=level,
                                 db=str(_CFG.root / "activity.db"))
    return {"count": len(events), "events": events}


def _metrics_json(q=None):
    """
    The stat strip and the 7-day chart, from what is actually stored. Days are
    UTC to match every other timestamp in the store.

    ?project=N scopes the X and Facebook numbers to that project (X via
    project_streams, Facebook via posts.project_id). Instagram is a GLOBAL pool
    with no project mapping, so its line is the same global flow in every view
    rather than reading zero.
    """
    try:
        pid = int((q or {}).get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    out = {"today": {"collected": 0, "photos": 0, "videos": 0,
                     "median_lag_ms": None, "p95_lag_ms": None},
           "per_day": [], "totals": {"tweets": 0}}
    day_ms = 86_400_000
    now_ms = int(time.time() * 1000)
    midnight = (now_ms // day_ms) * day_ms
    week_ago = midnight - 6 * day_ms

    scope = ""
    scope_params: list = []
    if pid:
        scope = (" AND EXISTS (SELECT 1 FROM tweet_hits ph "
                 "   JOIN project_streams ps ON ps.stream_id = ph.stream_id "
                 "   WHERE ph.tweet_id = t.tweet_id AND ps.project_id = ?)")
        scope_params = [pid]

    if _CFG.db_results.exists():
        with _connect() as con:
            out["totals"]["tweets"] = con.execute(
                f"SELECT COUNT(*) c FROM tweets t WHERE t.source = 'result'{scope}",
                scope_params).fetchone()["c"]
            r = con.execute(
                "SELECT COUNT(*) c, "
                "  SUM(CASE WHEN t.media_json LIKE '%\"photo\"%' THEN 1 ELSE 0 END) p, "
                "  SUM(CASE WHEN t.media_json LIKE '%\"video\"%' "
                "        OR t.media_json LIKE '%\"gif\"%' THEN 1 ELSE 0 END) v "
                f"FROM tweets t WHERE t.source = 'result' AND t.collected_ms >= ?{scope}",
                [midnight, *scope_params]).fetchone()
            out["today"].update(collected=r["c"], photos=r["p"] or 0,
                                videos=r["v"] or 0)
            lags = [x["lag_ms"] for x in con.execute(
                "SELECT t.lag_ms FROM tweets t WHERE t.source = 'result' "
                f"AND t.collected_ms >= ? AND t.lag_ms IS NOT NULL{scope}",
                [midnight, *scope_params])]
            if lags:
                out["today"]["median_lag_ms"] = store_mod._percentile(lags, 50)
                out["today"]["p95_lag_ms"] = store_mod._percentile(lags, 95)
            by_day = {int(r["d"]): r["c"] for r in con.execute(
                "SELECT t.collected_ms / ? AS d, COUNT(*) c FROM tweets t "
                f"WHERE t.source = 'result' AND t.collected_ms >= ?{scope} GROUP BY d",
                [day_ms, week_ago, *scope_params])}
    else:
        by_day = {}

    # Instagram is a GLOBAL pool (no project mapping), so its line is the same
    # in every project view — show it rather than zeroing it under a project.
    ig_by_day = {}
    rp = _CFG.root / "ig_results.db"
    if rp.exists():
        try:
            con = sqlite3.connect(f"file:{rp}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            # store_ig keys taken_at in unix SECONDS.
            ig_by_day = {int(r["d"]): r["c"] for r in con.execute(
                "SELECT (taken_at * 1000) / ? AS d, COUNT(*) c FROM posts "
                "WHERE taken_at * 1000 >= ? GROUP BY d", (day_ms, week_ago))}
            con.close()
        except Exception:
            pass

    # Facebook IS project-scoped (posts carry project_id) — scope the line the
    # same way X is, so it describes THIS project. Uses collected_ms to match X.
    fb_by_day = {}
    fp = _CFG.root / "fb_results.db"
    if fp.exists():
        try:
            con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            fb_where, fb_params = "collected_ms >= ?", [week_ago]
            if pid:
                fb_where += " AND project_id = ?"
                fb_params.append(pid)
            fb_by_day = {int(r["d"]): r["c"] for r in con.execute(
                f"SELECT collected_ms / ? AS d, COUNT(*) c FROM posts "
                f"WHERE {fb_where} GROUP BY d", (day_ms, *fb_params))}
            con.close()
        except Exception:
            pass

    for i in range(7):
        ms = week_ago + i * day_ms
        d = ms // day_ms
        out["per_day"].append({
            "day": datetime.datetime.fromtimestamp(
                ms / 1000, tz=datetime.timezone.utc).strftime("%d %b"),
            "x": by_day.get(d, 0), "ig": ig_by_day.get(d, 0),
            "fb": fb_by_day.get(d, 0)})
    return out


# --------------------------------------------------------------------------
# projects & watchlists — thin validators over the Store methods
# --------------------------------------------------------------------------
#
# All writes AND reads go through _with_store rather than the read-only
# _connect(): opening the Store runs the migration, so the first dashboard
# that touches these endpoints creates the tables and the Default project on
# a pre-projects database instead of erroring on a missing table.

def _projects_json():
    return {"projects": _with_store(
        lambda st: st.projects(include_archived=True))}


def _project_post(body):
    if "archived" in body:
        try:
            pid = int(body.get("project_id") or 0)
        except (TypeError, ValueError):
            return {"error": "project_id must be a number"}
        ok = _with_store(lambda st: st.set_project_archived(pid, body["archived"]))
        return {"ok": True} if ok else {"error": f"no project {pid}"}
    return _with_store(lambda st: st.create_project(body.get("name") or ""))


def _watchlists_json(q):
    try:
        pid = int(q.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return {"error": "which project? pass ?project=<id>"}
    return {"watchlists": _with_store(lambda st: st.watchlists(pid))}


def _watchlist_post(body):
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    if not pid:
        return {"error": "which project?"}
    kind = (body.get("kind") or "query").strip()
    raw_list = str(body.get("list_id") or "").strip()
    if kind == "xlist" and raw_list:
        # Accept the whole URL, exactly like config.toml and /api/fetch do.
        from config import ConfigError, _parse_list_id
        try:
            raw_list = _parse_list_id(raw_list, "watchlist")
        except ConfigError as e:
            return {"error": str(e)}

    async def go(st):
        made = await st.create_watchlist(pid, body.get("name") or "", kind, raw_list)
        if "error" in made:
            return made
        handles = body.get("handles") or []
        if handles and kind in ("query", "keywords"):
            upd = await st.set_watchlist_members(made["watchlist_id"], add=handles)
            if "error" in upd:
                # The name is taken but the handles were bad: report both facts
                # rather than leaving a silently empty watchlist.
                made["warning"] = upd["error"]
            else:
                made.update(upd)
        return made
    return _with_store(go)


def _watchlist_members(body):
    try:
        wid = int(body.get("watchlist_id") or 0)
    except (TypeError, ValueError):
        return {"error": "watchlist_id must be a number"}
    return _with_store(lambda st: st.set_watchlist_members(
        wid, add=body.get("add") or [], remove=body.get("remove") or []))


def _watchlist_interval(body):
    try:
        wid = int(body.get("watchlist_id") or 0)
    except (TypeError, ValueError):
        return {"error": "watchlist_id must be a number"}
    return _with_store(lambda st: st.set_watchlist_interval(
        wid, body.get("seconds")))


def _watchlist_filters(body):
    try:
        wid = int(body.get("watchlist_id") or 0)
    except (TypeError, ValueError):
        return {"error": "watchlist_id must be a number"}
    return _with_store(lambda st: st.set_watchlist_filters(
        wid, body.get("filters") or {}))


def _watchlist_remove(body):
    try:
        wid = int(body.get("watchlist_id") or 0)
    except (TypeError, ValueError):
        return {"error": "watchlist_id must be a number"}
    return _with_store(lambda st: st.delete_watchlist(wid))


# --------------------------------------------------------------------------
# stream assignments + delivery targets — thin validators
# --------------------------------------------------------------------------

def _stream_assignments_json():
    return {"streams": _with_store(lambda st: st.streams_with_projects())}


def _stream_assign(body, attach: bool):
    try:
        pid = int(body.get("project") or 0)
        sid = int(body.get("stream_id") or 0)
    except (TypeError, ValueError):
        return {"error": "project and stream_id must be numbers"}
    if not pid or not sid:
        return {"error": "project and stream_id are required"}
    if attach:
        _with_store(lambda st: st.attach_stream(pid, sid))
        return {"ok": True}
    ok_ = _with_store(lambda st: st.detach_stream(pid, sid))
    return {"ok": True} if ok_ else {"error": "that stream was not in this project"}


def _delivery_target_post(body):
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    return _with_store(lambda st: st.create_delivery_target(
        pid, (body.get("kind") or "").strip(), body.get("name") or "",
        body.get("url") or "", body.get("secret_env") or "",
        body.get("chat_id") or "", body.get("batch_size") or 50))


def _delivery_target_update(body):
    try:
        tid = int(body.get("target_id") or 0)
    except (TypeError, ValueError):
        return {"error": "target_id must be a number"}
    vals = {}
    if "enabled" in body:
        vals["enabled"] = int(bool(body["enabled"]))
    ok_ = _with_store(lambda st: st.update_delivery_target(tid, vals))
    return {"ok": True} if ok_ else {"error": f"no target {tid}"}


def _date_window(body):
    """
    (lo_ms, hi_ms, error) for a history send: either exact dates —
    from_date/to_date as yyyy-mm-dd, BOTH days inclusive, so July is
    2026-07-01 → 2026-07-31 — or a rolling `since` window ('24h', '7d').
    Exact dates win when both are supplied.
    """
    fd, td = body.get("from_date"), body.get("to_date")
    if fd or td:
        lo = _day_ms(fd) if fd else None
        if lo is None:
            return 0, 0, "from_date must be a real date (yyyy-mm-dd)"
        if td:
            hi = _day_ms(td)
            if hi is None:
                return 0, 0, "to_date must be a real date (yyyy-mm-dd)"
            hi += 86_400_000          # inclusive of the whole 'to' day
        else:
            hi = int(time.time() * 1000)
        if hi <= lo:
            return 0, 0, "to_date is before from_date"
        return lo, hi, None
    try:
        lo = store_mod.parse_window(body.get("since") or "24h") or 0
    except ValueError as e:
        return 0, 0, str(e)
    return lo, int(time.time() * 1000) + 86_400_000, None


def _delivery_backfill(body):
    """
    One-shot: send PAST posts (by posted time, within a window) to a Telegram
    target. Exists because normal delivery deliberately starts from NOW — the
    moment you add a target for a handle you just started watching, the
    history that was collected before the target existed would otherwise
    never reach the channel.

    Sends oldest-first so the channel reads chronologically, paced to
    Telegram's per-group rate limit, capped, and NEVER moves the delivery
    cursor — live delivery continues exactly as before, with no duplicates
    (everything sent here sits behind the cursor already).
    """
    import webhook as wh

    try:
        tid = int(body.get("target_id") or 0)
    except (TypeError, ValueError):
        return {"error": "target_id must be a number"}
    with _connect() as con:
        row = con.execute("SELECT * FROM delivery_targets WHERE target_id = ?",
                          (tid,)).fetchone()
    if not row:
        return {"error": f"no target {tid}"}
    if row["kind"] != "telegram":
        return {"error": "history send is for Telegram targets (a webhook "
                         "receiver should read your API instead)"}
    token = wh.telegram_token()
    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN is not set in .env"}
    lo, hi, err = _date_window(body)
    if err:
        return {"error": err}
    try:
        limit = min(50, max(1, int(body.get("limit") or 20)))
    except (TypeError, ValueError):
        return {"error": "limit must be a whole number"}

    with _connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT t.* FROM tweets t WHERE t.source = 'result' "
            "AND t.created_ms >= ? AND t.created_ms < ? "
            "AND EXISTS (SELECT 1 FROM tweet_hits ph JOIN project_streams ps "
            "            ON ps.stream_id = ph.stream_id "
            "            WHERE ph.tweet_id = t.tweet_id AND ps.project_id = ?) "
            "ORDER BY t.created_ms ASC LIMIT ?",
            (lo, hi, row["project_id"], limit))]
    if not rows:
        return {"sent": 0, "note": "nothing collected in that window for this project"}

    async def go():
        import httpx
        sent = 0
        async with httpx.AsyncClient() as client:
            for i, msg in enumerate(wh.tg_format(rows)):
                if i:
                    await asyncio.sleep(wh.TG_GAP_S)
                ok_, err = await wh.tg_send(client, token, row["chat_id"], msg)
                if not ok_:
                    return {"sent": sent, "error": err}
                sent += 1
        return {"sent": sent}
    # 50 messages at Telegram pace is ~160s; the request waits it out.
    return _run(go(), timeout=280)


def _delivery_target_remove(body):
    try:
        tid = int(body.get("target_id") or 0)
    except (TypeError, ValueError):
        return {"error": "target_id must be a number"}
    ok_ = _with_store(lambda st: st.delete_delivery_target(tid))
    return {"ok": True} if ok_ else {"error": f"no target {tid}"}


# --------------------------------------------------------------------------
# alerts — thin validators over the Store methods
# --------------------------------------------------------------------------

def _alerts_json(q):
    try:
        pid = int(q.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return {"error": "which project? pass ?project=<id>"}
    return {"alerts": _with_store(lambda st: st.alerts(pid)),
            "telegram_ready": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
            "default_chat": bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())}


def _alert_post(body):
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    wid = body.get("watchlist_id") or None
    chat = str(body.get("tg_chat_id") or "").strip()
    if chat and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{4,32}", chat):
        return {"error": "a chat id is a number like -1001234567890, "
                         "or a public channel like @mychannel"}
    return _with_store(lambda st: st.create_alert(
        pid, wid, body.get("threshold", 3.0), body.get("min_posts", 10), chat))


def _alert_update(body):
    try:
        aid = int(body.get("alert_id") or 0)
    except (TypeError, ValueError):
        return {"error": "alert_id must be a number"}
    vals = {}
    if "enabled" in body:
        vals["enabled"] = int(bool(body["enabled"]))
    if "threshold" in body:
        try:
            vals["threshold"] = max(1.1, float(body["threshold"]))
        except (TypeError, ValueError):
            return {"error": "threshold must be a number"}
    if "min_posts" in body:
        try:
            vals["min_posts"] = max(1, int(body["min_posts"]))
        except (TypeError, ValueError):
            return {"error": "min_posts must be a whole number"}
    ok_ = _with_store(lambda st: st.update_alert(aid, vals))
    return {"ok": True} if ok_ else {"error": f"no alert {aid} (or nothing to change)"}


def _alert_remove(body):
    try:
        aid = int(body.get("alert_id") or 0)
    except (TypeError, ValueError):
        return {"error": "alert_id must be a number"}
    ok_ = _with_store(lambda st: st.delete_alert(aid))
    return {"ok": True} if ok_ else {"error": f"no alert {aid}"}


# --------------------------------------------------------------------------
# collections — thin validators over the Store methods
# --------------------------------------------------------------------------

def _collections_json(q):
    try:
        pid = int(q.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return {"error": "which project? pass ?project=<id>"}
    return {"collections": _with_store(lambda st: st.collections(pid))}


def _collection_post(body):
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    return _with_store(lambda st: st.create_collection(pid, body.get("name") or ""))


def _collection_remove(body):
    try:
        cid = int(body.get("collection_id") or 0)
    except (TypeError, ValueError):
        return {"error": "collection_id must be a number"}
    return _with_store(lambda st: st.delete_collection(cid))


def _collection_pin(body):
    try:
        cid = int(body.get("collection_id") or 0)
    except (TypeError, ValueError):
        return {"error": "collection_id must be a number"}
    return _with_store(lambda st: st.collection_pin(
        cid, add=body.get("add") or [], remove=body.get("remove") or []))


def _collection_items_json(q):
    try:
        cid = int(q.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        return {"error": "which collection? pass ?id=<id>"}
    rows = _with_store(lambda st: st.collection_rows(cid))
    return {"count": len(rows), "rows": [_row_to_json(r) for r in rows]}


def _collection_export_csv(q):
    """The board as CSV — the same frozen column set the exporter uses."""
    try:
        cid = int(q.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    rows = _with_store(lambda st: st.collection_rows(cid)) if cid else []
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=store_mod.FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        rec = dict(r)
        for k in ("hashtags", "mentions", "urls", "media_urls"):
            try:
                rec[k] = json.loads(rec.get(k) or "[]")
            except (TypeError, ValueError):
                rec[k] = []
        w.writerow(store_mod.to_csv_row(rec, store_mod.FIELDS))
    return buf.getvalue()


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


# How many real tweets "Send a test" puts in the channel. Three is enough to
# see the format, the pacing and the links without filling the group.
TG_TEST_COUNT = 3


def _test_telegram(body):
    """
    Send a few REAL tweets, so 'saved' and 'working' are not confused.

    This used to post one fixed line — "Connected. Tweets will arrive here." —
    which proved the bot token and the chat id and nothing else. Every failure
    this project actually had lived in the part that line skipped: the stream
    wiring, the delivery loop, the formatter. A test that cannot fail the way
    the system fails is not a test.

    So it now sends the newest few collected tweets through the SAME formatter
    the collector uses. What lands in the group is exactly what a real delivery
    looks like, one tweet per message.

    The delivery cursor is deliberately untouched: a test that consumed tweets
    would make normal delivery skip them, so proving it works would cost you the
    very messages you were proving it with.
    """
    import webhook as wh

    token = wh.telegram_token()
    chat = (body.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token:
        return {"error": "No bot token saved yet."}
    if not chat:
        return {"error": "No chat id — tell me where to send it."}

    # Prefer the stream the test was launched from, so the sample is the one you
    # are configuring rather than whatever happens to be newest overall.
    p = {"limit": TG_TEST_COUNT, "order": "desc"}
    if body.get("stream"):
        p["stream"] = body["stream"]
    try:
        rows = _query_tweets(p).get("rows") or []
    except Exception:
        rows = []

    msgs = wh.tg_format(rows) if rows else [
        "<b>X Collector</b>\nConnected. No tweets collected yet, so this is a "
        "plain check of the bot token and chat id."]

    async def go():
        import httpx
        async with httpx.AsyncClient() as client:
            for i, msg in enumerate(msgs):
                if i:
                    await asyncio.sleep(wh.TG_GAP_S)
                ok, err = await wh.tg_send(client, token, chat, msg)
                if not ok:
                    return False, err
            return True, ""

    # The timeout must cover the pacing: TG_GAP_S between each send, plus room
    # for Telegram to be slow on the last one.
    budget = int(30 + wh.TG_GAP_S * len(msgs)) + 1
    ok, err = _run(go(), timeout=budget)
    return {"ok": ok, "sent": len(msgs), "real": bool(rows)} if ok else {"error": err}


# --------------------------------------------------------------------------
# interactive login
# --------------------------------------------------------------------------
#
# One at a time, deliberately. Each session holds a Chrome process open against
# an account's profile directory, and two Chromes on one profile corrupt it.
_LOGIN = {"session": None, "label": None, "platform": "x"}
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


def _login_module(platform: str):
    """
    The module that knows how to sign into this platform.

    Both expose the same surface — InteractiveLogin with frame/click/type/
    scroll, LOGIN_VIEWPORT, and the state constants — so everything below drives
    either without caring which it got. That is the whole reason the sign-in
    window did not have to be written twice.
    """
    if platform == "instagram":
        import ig
        return ig
    import auth
    return auth


def _login_start(label):
    _login_reap()
    with _LOGIN_LOCK:
        if _LOGIN["session"] is not None:
            return {"error": f"A sign-in window is already open for "
                             f"'{_LOGIN['label']}'. Finish or close that one first."}
        try:
            acct = _CFG.account(label)
        except Exception as e:
            return {"error": str(e)}

        mod = _login_module(acct.platform)

        # CAPTURE the launch diagnostics. _launch reports which browser it
        # tried, which failed and why, and every one of those lines used to go
        # into a default `lambda m: None` — so "it works on my laptop but the
        # server shows nothing" arrived with the one explanation discarded.
        trace = []

        def log(m):
            trace.append(str(m).strip())
            print(f"[signin:{label}] {m}", flush=True)   # and into journalctl

        started = time.time()
        try:
            sess = _run(mod.InteractiveLogin(acct).start(log=log), timeout=180)
        except ImportError:
            return {"error": "The browser this needs is not installed on this "
                             "server. Run:  bash deploy/setup.sh",
                    "trace": trace}
        except TimeoutError:
            return {"error": "The browser did not start within 3 minutes. On a "
                             "small server this is almost always memory — "
                             "headless Chrome needs roughly 1 GB free. Check "
                             "with:  python3 main.py doctor --browser",
                    "trace": trace}
        except Exception as e:
            return {"error": f"Could not open a browser: {type(e).__name__}: {e}",
                    "trace": trace}
        took = time.time() - started

        _LOGIN["session"] = sess
        _LOGIN["label"] = label
        _LOGIN["platform"] = acct.platform
        return {"ok": True, "label": label, "platform": acct.platform,
                "state": sess.state, "screen_name": sess.screen_name,
                "url": sess.url(), "took_s": round(took, 1), "trace": trace,
                "warning": getattr(sess, "error", "") or "",
                "width": mod.LOGIN_VIEWPORT["width"],
                "height": mod.LOGIN_VIEWPORT["height"]}


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
        elif act == "reload":
            # Instagram's login sometimes wedges on a half-rendered page. A
            # reload is what a person would do, and it costs nothing.
            _run(s.reload(), timeout=60)
        state = _run(s.refresh_state(), timeout=30)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out = {"ok": True, "state": state, "screen_name": s.screen_name,
           "url": s.url()}
    if state == "logged_in":
        out.update(_login_capture())
    return out


def _ig_store_path():
    """Instagram sessions live beside the X ones, not inside them."""
    return _CFG.root / "ig_accounts.db"


def _login_capture():
    """
    Signed in — copy the session out of the browser, then close the browser.

    The browser is disposable; the session is the asset. Everything a later
    HTTP client needs (cookies plus the REAL user-agent) is taken here and
    written down; the browser is then shut down cleanly, which is also what
    flushes the profile that keeps this device trusted next time.
    """
    s, label = _LOGIN["session"], _LOGIN["label"]
    platform = _LOGIN.get("platform", "x")
    if s is None:
        return {"error": "The sign-in window is closed."}

    try:
        harvest = _run(s.harvest(), timeout=60)
        if not harvest.has_required:
            # Do NOT close the browser: both sites set these a moment after the
            # redirect, so the next poll usually succeeds. Tearing the window
            # down would make someone start over for a timing blip.
            return {"error": "Signed in, but the session cookies are not set "
                             "yet. Give it a moment and it will finish."}

        if platform == "instagram":
            import ig
            with ig.Store(_ig_store_path()) as st:
                active, detail = ig.capture(st, harvest, _CFG.account(label))
            username = harvest.username
        else:
            import auth
            async def save():
                api = auth.open_api(_CFG.db_accounts)
                return await auth.upsert_session(api, harvest, _CFG.account(label))

            username, res = _run(save(), timeout=120)
            active, detail = res.ok, ("" if res.ok else res.error)
            if res.ok:
                auth.write_identity(_CFG.account(label), username)
    except Exception as e:
        _login_drop()
        return {"error": f"Could not save the session: {type(e).__name__}: {e}"}

    _login_drop()
    return {"captured": True, "username": username, "active": active,
            "platform": platform, "detail": "" if active else detail}


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


def _fb_status(q=None):
    """Facebook sources + totals for a project (or all)."""
    try:
        pid = int((q or {}).get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    out = {"sources": [], "totals": {"posts": 0}, "enabled": False}
    rp = _CFG.root / "fb_results.db"
    # Configured if ANY login path exists: raw cookies, email+password, or a
    # session already saved on disk from an earlier successful login.
    out["enabled"] = bool(
        (os.getenv("FB_C_USER", "").strip() and os.getenv("FB_XS", "").strip())
        or (os.getenv("FB_EMAIL", "").strip() and os.getenv("FB_PASSWORD", ""))
        or (_CFG.root / os.getenv("FB_STATE_PATH", "fb_state.json")).exists())
    # The session itself, so the Accounts panel can show Facebook next to X
    # and Instagram. One burner login — not a pool — so this is a single
    # identity plus how it authenticates; never a password or cookie value.
    email = os.getenv("FB_EMAIL", "").strip()
    c_user = os.getenv("FB_C_USER", "").strip()
    out["session"] = {
        "identity": email or (f"c_user {c_user}" if c_user else "") or None,
        "method": ("cookies" if c_user and os.getenv("FB_XS", "").strip()
                   else "password" if email and os.getenv("FB_PASSWORD", "")
                   else "saved state" if out["enabled"] else None),
        "state_saved": (_CFG.root / os.getenv("FB_STATE_PATH",
                                              "fb_state.json")).exists(),
    }
    # Login health — the circuit breaker's state, so the dashboard can show
    # the ACTUAL cause of a dead login and offer the human actions.
    try:
        hp = _CFG.root / os.getenv("FB_HEALTH_PATH", "fb_health.json")
        out["health"] = json.loads(hp.read_text()) if hp.exists() else {}
    except Exception:
        out["health"] = {}
    settings = {}
    if rp.exists():
        try:
            import store_fb
            with store_fb.Store(rp) as st:
                srcs = st.sources(project_id=pid or None)
                # Map each page's stored interval back to the named speed the
                # panel offers, so re-opening shows what is actually set.
                inv = {v: k for k, v in FB_SPEEDS.items()}
                for s in srcs:
                    s["speed"] = inv.get(s.get("interval_s"), "")
                out["sources"] = srcs
                out["totals"] = {"posts": st.total(pid or None)}
                settings = st.settings_all()
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
    # The whole operating configuration, dashboard-settings first, env as the
    # fallback — what the service loop actually uses (it re-reads per cycle).
    out["paused"] = settings.get("fb_paused") == "1"
    out["config"] = {
        "mode": (settings.get("fb_mode")
                 or os.getenv("FB_MODE", "pages")).lower(),
        "default_interval_s": int(settings.get("fb_interval_s")
                                  or os.getenv("FB_INTERVAL_S", "21600")),
        "fav_interval_s": int(settings.get("fb_fav_interval_s")
                              or os.getenv("FB_FAV_INTERVAL_S", "3600")),
        "monthly_cap_gb": float(os.getenv("FB_MONTHLY_CAP_GB", "200")),
        "use_proxy": os.getenv("FB_USE_PROXY", "0") == "1",
    }
    return out


def _fb_posts(q):
    """Collected Facebook posts, newest first, in the shared feed shape."""
    rp = _CFG.root / "fb_results.db"
    if not rp.exists():
        return {"count": 0, "posts": []}
    import store_fb
    try:
        pid = int(q.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    since_ms = None
    if q.get("since"):
        try:
            since_ms = store_mod.parse_window(q["since"])
        except Exception:
            since_ms = None
    try:
        limit = min(int(q.get("limit") or 50), 200)
    except (TypeError, ValueError):
        limit = 50
    with store_fb.Store(rp) as st:
        rows = st.recent(project_id=pid or None, limit=limit, since_ms=since_ms)
        # TRUE count for the window (project-scoped, since FB carries project_id).
        fw, fa = [], []
        if pid:
            fw.append("project_id = ?"); fa.append(pid)
        if since_ms:
            fw.append("collected_ms >= ?"); fa.append(since_ms)
        try:
            total = st.db.execute(
                "SELECT COUNT(*) c FROM posts"
                + ((" WHERE " + " AND ".join(fw)) if fw else ""), fa).fetchone()["c"]
        except Exception:
            total = len(rows)
    posts = [store_fb.to_feed(r) for r in rows]
    # A post still without a picture gets the X avatar for the same handle —
    # public figures use one photo everywhere, and X is the canonical source.
    missing = {p["author_username"] for p in posts if not p.get("author_avatar")}
    if missing:
        xmap = _x_avatars_for(missing)
        for p in posts:
            if not p.get("author_avatar"):
                p["author_avatar"] = xmap.get(
                    str(p.get("author_username") or "").lower())
    # Cross-handle: a display name set on the page links it to the X avatar.
    _fill_avatars_by_name(posts, "fb",
                          lambda p: str(p.get("author_username") or "").lower())
    return {"count": len(rows), "total": total, "posts": posts}


# The named check-cadences a Facebook page can be set to, seconds. Hours, not
# minutes: pages post a few times a day and the browser render is heavy, so a
# tighter cadence would spend bandwidth for nothing.
FB_SPEEDS = {"1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400}


def _fb_source_post(body):
    """Add, remove, re-time, or pause/resume a Facebook page source."""
    import store_fb
    action = body.get("action") or "add"
    raw = str(body.get("label") or "").strip()
    # Be forgiving about pasted URLs, but never let one become a "page":
    #   - a feed/favorites URL is not a page at all -> reject with guidance
    #   - a real page URL -> pull the handle out of it
    low = raw.lower()
    if "facebook.com" in low:
        if any(t in low for t in ("filter=", "sk=", "/feed", "?filter", "favorites")):
            return {"error": "That looks like the Favorites/feed URL, not a page. "
                             "Use the “Fetch Favorites feed” button for that. Here, "
                             "add just a page handle, e.g. narendramodi."}
        m = re.search(r"facebook\.com/([^/?#]+)", raw)
        if m:
            raw = m.group(1)
    if raw.lower() in ("profile.php", "people", "pages", "watch", "groups"):
        return {"error": "Paste the page's handle (the part after facebook.com/), "
                         "e.g. narendramodi."}
    label = re.sub(r"[^A-Za-z0-9_.-]", "", raw).strip(".")
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        return {"error": "project must be a number"}
    if not label:
        return {"error": "a Facebook page name is required"}
    rp = _CFG.root / "fb_results.db"
    with store_fb.Store(rp) as st:
        if action == "remove":
            st.remove_source(label)
            return {"ok": True, "removed": label}
        if action == "interval":
            speed = str(body.get("speed") or "")
            if speed and speed not in FB_SPEEDS:
                return {"error": f"speed must be one of: {', '.join(FB_SPEEDS)}"}
            st.set_interval(label, FB_SPEEDS.get(speed))   # "" clears → default
            return {"ok": True, "label": label, "speed": speed}
        if action == "enable":
            st.set_enabled(label, bool(body.get("enabled")))
            return {"ok": True, "label": label, "enabled": bool(body.get("enabled"))}
        st.add_source(label, project_id=pid)
    return {"ok": True, "label": label}


# Only one Facebook fetch at a time — a second would launch a second headless
# browser on the same burner session and invite a checkpoint.
_FB_FETCH_LOCK = threading.Lock()


def _fb_locked_run(coro, timeout=300):
    """
    Run a Facebook coroutine single-flight. The lock is released when the
    coroutine ACTUALLY finishes (a done-callback on the loop), NOT when this
    function returns. That matters: if the HTTP request times out, the browser
    is still driving Facebook — releasing the lock here would let a second click
    launch a second headless browser on the same session. Returns (result, err).
    """
    import concurrent.futures as _cf
    if _LOOP is None:
        return None, {"error": "server not ready"}
    if not _FB_FETCH_LOCK.acquire(blocking=False):
        return None, {"error": "A Facebook fetch is already running — give it a moment."}
    fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    fut.add_done_callback(lambda f: _FB_FETCH_LOCK.release())
    try:
        return fut.result(timeout), None
    except _cf.TimeoutError:
        # The coroutine keeps running; the callback releases the lock when it
        # finishes, so no second browser can start meanwhile.
        return None, {"error": "Facebook fetch is taking a while — it's still "
                               "running in the background; check the Live Feed "
                               "in a minute."}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}


def _fb_fetch(body):
    """
    The Facebook "Fetch now" button: run ONE collection pass immediately, from
    the dashboard, so nobody needs a terminal. Same shared loop as the X fetch;
    reports how many new posts it saved plus the raw run log so a bad login or
    checkpoint is visible in the UI rather than silent.
    """
    rp = _CFG.root / "fb_results.db"
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0

    import store_fb
    srcs = []
    if rp.exists():
        try:
            with store_fb.Store(rp) as st:
                srcs = st.sources(project_id=pid or None, enabled_only=True)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    if not srcs:
        return {"error": "Add a Facebook page first (the Facebook pages panel), "
                         "then press Fetch."}

    from collect_fb import _can_log_in, run_once
    if not _can_log_in():
        return {"error": "Facebook login isn't set up on the server yet — add "
                         "FB_EMAIL/FB_PASSWORD (or FB_C_USER/FB_XS) to .env and "
                         "restart the dashboard, then try again."}

    logs: list = []
    # Echo into the response (the button shows it) AND persist to the account
    # activity log, so button-triggered runs appear in the Log section too.
    import activity_log
    _lg = activity_log.logger(
        "facebook", account=os.getenv("FB_EMAIL", "").strip() or None,
        echo=lambda m: logs.append(str(m)), db=str(_CFG.root / "activity.db"))
    n, err = _fb_locked_run(run_once(str(rp), project_id=pid or None,
                                     log=_lg), timeout=300)
    if err:
        return {**err, "log": logs}
    diag = None
    try:
        dp = _CFG.root / os.getenv("FB_DIAG_PATH", "fb_diag.json")
        if dp.exists():
            diag = json.loads(dp.read_text())
    except Exception:
        diag = None
    return {"ok": True, "new": n, "sources": len(srcs), "log": logs, "diag": diag}


def _fb_favorites(body):
    """
    Read the account's Favorites feed once and attribute posts to tracked pages
    (across all projects). Global by nature — one burner account has one
    favorites list — so it is not project-scoped.
    """
    rp = _CFG.root / "fb_results.db"
    try:
        pid = int(body.get("project") or 0)
    except (TypeError, ValueError):
        pid = 0
    from collect_fb import _can_log_in, run_favorites
    if not _can_log_in():
        return {"error": "Facebook login isn't set up on the server yet — add "
                         "FB_EMAIL/FB_PASSWORD to .env and restart the dashboard."}
    logs: list = []
    import activity_log
    _lg = activity_log.logger(
        "facebook", account=os.getenv("FB_EMAIL", "").strip() or None,
        echo=lambda m: logs.append(str(m)), db=str(_CFG.root / "activity.db"))
    # pid lets favorited pages auto-register under THIS project, so the feed
    # just flows in without hand-adding each page.
    n, err = _fb_locked_run(run_favorites(str(rp), project_id=pid or None,
                                          log=_lg), timeout=300)
    if err:
        return {**err, "log": logs}
    return {"ok": True, "new": n, "log": logs}


def _fb_control(body):
    """Pause / resume Facebook collection globally — the dashboard's master
    switch. The service loop re-reads it every cycle, so no restart needed."""
    action = (body.get("action") or "").lower()
    if action not in ("pause", "resume"):
        return {"error": "action must be pause or resume"}
    import activity_log
    import store_fb
    with store_fb.Store(_CFG.root / "fb_results.db") as st:
        st.set_setting("fb_paused", "1" if action == "pause" else "")
    activity_log.log_event(
        "facebook", f"[fb] collection {action.upper()}D by operator from the "
        f"dashboard", db=str(_CFG.root / "activity.db"))
    return {"ok": True, "paused": action == "pause"}


def _fb_health_post(body):
    """
    The human side of the login circuit breaker.
      clear          — "I fixed it": forget the block; the NEXT run may attempt
                       one login again.
      reset_session  — also delete fb_state.json, forcing a completely fresh
                       login (use after clearing a checkpoint in a browser).
    """
    action = (body.get("action") or "").lower()
    if action not in ("clear", "reset_session"):
        return {"error": "action must be clear or reset_session"}
    import activity_log
    hp = _CFG.root / os.getenv("FB_HEALTH_PATH", "fb_health.json")
    sp = _CFG.root / os.getenv("FB_STATE_PATH", "fb_state.json")
    targets = [hp] if action == "clear" else [hp, sp]
    for pth in targets:
        try:
            pth.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            return {"error": f"could not remove {pth.name}: {e}"}
    activity_log.log_event(
        "facebook",
        "[fb] login block CLEARED by operator — next run may attempt one login"
        if action == "clear" else
        "[fb] session RESET by operator — fb_state.json deleted; next run "
        "logs in fresh", db=str(_CFG.root / "activity.db"))
    return {"ok": True}


def _fb_settings_post(body):
    """Edit the Facebook operating configuration from the dashboard. Values
    land in the settings table; empty string clears back to the env default."""
    import activity_log
    import store_fb
    changes = {}
    if "mode" in body:
        mode = str(body.get("mode") or "").lower()
        if mode not in ("", "pages", "favorites"):
            return {"error": "mode must be pages or favorites"}
        changes["fb_mode"] = mode
    for body_key, store_key in (("default_interval_s", "fb_interval_s"),
                                ("fav_interval_s", "fb_fav_interval_s")):
        if body_key not in body:
            continue
        v = body.get(body_key)
        if v in (None, ""):
            changes[store_key] = ""
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return {"error": f"{body_key} must be seconds (a number)"}
        if iv < 900:
            # Refuse, don't clamp (RULEBOOK §3): sub-15-minute Facebook
            # cadence is a ban risk nobody meant to ask for.
            return {"error": f"{body_key}: minimum is 900 seconds (15 min) — "
                             f"tighter cadence is how accounts get flagged"}
        changes[store_key] = str(iv)
    if not changes:
        return {"error": "nothing to change"}
    with store_fb.Store(_CFG.root / "fb_results.db") as st:
        for k, v in changes.items():
            st.set_setting(k, v)
    activity_log.log_event(
        "facebook", "[fb] settings changed from the dashboard: "
        + ", ".join(f"{k}={v or '(default)'}" for k, v in changes.items()),
        db=str(_CFG.root / "activity.db"))
    return {"ok": True}


def _ig_status():
    """Instagram accounts + sources + totals for the dashboard's Instagram view."""
    import sqlite3
    root = _CFG.root
    out = {"accounts": [], "sources": [], "totals": {"posts": 0}}
    ap = root / "ig_accounts.db"
    if ap.exists():
        try:
            con = sqlite3.connect(f"file:{ap}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            for r in con.execute("SELECT username, active, proxy, error_msg FROM accounts"):
                acct = {"username": r["username"], "active": bool(r["active"]),
                        "proxy": bool(r["proxy"]), "error": r["error_msg"]}
                # The checkpoint tombstone lives in the account's session
                # sidecar — surface it so the dashboard can say "a human must
                # act" instead of the collector failing quietly (RULEBOOK §6).
                try:
                    sc = root / "profiles" / f"ig_{r['username']}.json"
                    if sc.exists():
                        meta = (json.loads(sc.read_text()) or {}).get("meta", {})
                        acct["checkpoint_at"] = meta.get("checkpoint_at") or None
                except Exception:
                    pass
                out["accounts"].append(acct)
            con.close()
        except Exception as e:
            out["accounts_error"] = f"{type(e).__name__}: {e}"
    rp = root / "ig_results.db"
    settings = {}
    if rp.exists():
        try:
            import store_ig
            with store_ig.Store(rp) as st:
                # Read enabled straight off the table — store_ig.sources() drops
                # it, and the dashboard's pause toggle needs the real value.
                out["sources"] = [dict(r) for r in st.db.execute(
                    "SELECT label, type, value, account, enabled "
                    "FROM sources ORDER BY label")]
                out["totals"] = st.stats()
                settings = {r["key"]: r["value"] for r in st.db.execute(
                    "SELECT key, value FROM settings")}
        except Exception as e:
            out["sources_error"] = f"{type(e).__name__}: {e}"
    out["paused"] = settings.get("ig_paused") == "1"
    out["config"] = {
        "interval_s": int(settings.get("ig_interval_s")
                          or os.getenv("IG_INTERVAL_S", "120")),
    }
    return out


def _ig_control(body):
    """Pause / resume Instagram collection globally. The service loop re-reads
    the flag every cycle, so it applies without a restart."""
    action = (body.get("action") or "").lower()
    if action not in ("pause", "resume"):
        return {"error": "action must be pause or resume"}
    import activity_log
    import store_ig
    with store_ig.Store(_CFG.root / "ig_results.db") as st:
        st.set_setting("ig_paused", "1" if action == "pause" else "")
    activity_log.log_event(
        "instagram", f"collection {action.upper()}D by operator from the "
        f"dashboard", db=str(_CFG.root / "activity.db"))
    return {"ok": True, "paused": action == "pause"}


def _ig_settings_post(body):
    """Edit the Instagram cadence from the dashboard (seconds between passes).
    Gentle floor: Instagram punishes tight polling with rate limits."""
    import activity_log
    import store_ig
    v = body.get("interval_s")
    if v in (None, ""):
        val = ""
    else:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return {"error": "interval_s must be seconds (a number)"}
        if iv < 60:
            # Refuse, don't clamp (RULEBOOK §3).
            return {"error": "interval_s: minimum is 60 seconds — tighter "
                             "polling is how Instagram sessions get limited"}
        val = str(iv)
    with store_ig.Store(_CFG.root / "ig_results.db") as st:
        st.set_setting("ig_interval_s", val)
    activity_log.log_event(
        "instagram", f"cadence changed from the dashboard: "
        f"{val or '(default)'}s", db=str(_CFG.root / "activity.db"))
    return {"ok": True}


_IG_FETCH_LOCK = threading.Lock()


def _ig_locked_run(coro, timeout=180):
    """Run an Instagram coroutine single-flight (mirrors _fb_locked_run) — a
    second click must not launch a second concurrent poll on the same session."""
    import concurrent.futures as _cf
    if _LOOP is None:
        return None, {"error": "server not ready"}
    if not _IG_FETCH_LOCK.acquire(blocking=False):
        return None, {"error": "An Instagram fetch is already running — give it a moment."}
    fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    fut.add_done_callback(lambda f: _IG_FETCH_LOCK.release())
    try:
        return fut.result(timeout), None
    except _cf.TimeoutError:
        return None, {"error": "Instagram fetch is still running in the "
                               "background — check the Live Feed in a minute."}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}


def _ig_fetch(body):
    """
    The Instagram "Fetch now" button: run ONE collection pass immediately from
    the dashboard, so collection doesn't depend only on the background service
    being up. Reports new-post count + the run log, so a checkpoint or a
    missing source is visible in the UI instead of silent.
    """
    rp = _CFG.root / "ig_results.db"
    import store_ig
    srcs = []
    if rp.exists():
        try:
            with store_ig.Store(rp) as st:
                srcs = [r for r in st.db.execute(
                    "SELECT label FROM sources WHERE enabled = 1")]
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    if not srcs:
        return {"error": "Add an Instagram source first (Watchlists → + New "
                         "watchlist → Instagram), then press Fetch."}
    try:
        from collect_ig import run_once
    except Exception as e:
        return {"error": f"Instagram collector not available: {e}"}
    logs: list = []
    import activity_log
    _lg = activity_log.logger("instagram",
                              echo=lambda m: logs.append(str(m)),
                              db=str(_CFG.root / "activity.db"))
    n, err = _ig_locked_run(run_once(str(rp), log=_lg), timeout=180)
    if err:
        return {**err, "log": logs}
    return {"ok": True, "new": n, "sources": len(srcs), "log": logs}


def _ig_source_post(body):
    """
    Add, remove, or pause/resume an Instagram source from the dashboard —
    the same unified watchlist flow X and Facebook already have, so all three
    platforms are managed in ONE place instead of the IG CLI.
    """
    import store_ig
    action = (body.get("action") or "add").lower()
    label = str(body.get("label") or "").strip()
    if not label:
        return {"error": "a source label is required"}
    rp = _CFG.root / "ig_results.db"
    with store_ig.Store(rp) as st:
        if action == "add":
            typ = (body.get("type") or "user").lower()
            value = str(body.get("value") or "").strip()
            if typ in ("user", "hashtag") and not value:
                return {"error": f"a {typ} source needs a value "
                                 f"({'numeric id or username' if typ == 'user' else 'the hashtag'})"}
            try:
                st.add_source(label, typ, value, str(body.get("account") or ""))
            except ValueError as e:
                return {"error": str(e)}
        elif action == "remove":
            st.db.execute("DELETE FROM sources WHERE label = ?", (label,))
            st.db.commit()
        elif action == "enable":
            st.set_enabled(label, bool(body.get("enabled")))
        else:
            return {"error": "action must be add, remove, or enable"}
    return {"ok": True}


def _ig_posts(q):
    """Collected Instagram posts, newest first, in the shared API shape."""
    rp = _CFG.root / "ig_results.db"
    if not rp.exists():
        return {"count": 0, "posts": [], "next_cursor": None}
    import store_ig

    # `since` accepts the same windows the X side uses ("24h", "7d", …) so one
    # habit works on both tabs. store_ig.query has always supported the bound;
    # only this endpoint never passed it through.
    since = None
    if q.get("since"):
        ms = store_mod.parse_window(q["since"])
        if ms:
            since = int(ms / 1000)      # store_ig keys on taken_at, unix SECONDS

    with store_ig.Store(rp) as st:
        rows = st.query(limit=int(q.get("limit") or 30),
                        since=since,
                        source=q.get("source") or None,
                        username=q.get("username") or None,
                        before_pk=int(q["cursor"]) if q.get("cursor") else None)
        posts = [store_ig.to_api(r) for r in rows]
        # TRUE count for the window (not the page) so the UI number is real,
        # independent of how many rows the load-more has pulled so far.
        tw, ta = [], []
        if since is not None:
            tw.append("taken_at >= ?"); ta.append(since)
        if q.get("source"):
            tw.append("source_label = ?"); ta.append(q["source"])
        if q.get("username"):
            tw.append("username = ?"); ta.append(str(q["username"]).lstrip("@"))
        try:
            total = st.db.execute(
                "SELECT COUNT(*) c FROM posts"
                + ((" WHERE " + " AND ".join(tw)) if tw else ""), ta).fetchone()["c"]
        except Exception:
            total = len(posts)
    # Same rule as Facebook: the X avatar for the same handle is the profile
    # picture (one photo everywhere; X is the canonical source).
    missing = {(p.get("author") or {}).get("username") for p in posts
               if not p.get("author_avatar")}
    missing.discard(None)
    if missing:
        xmap = _x_avatars_for(missing)
        for p in posts:
            if not p.get("author_avatar"):
                p["author_avatar"] = xmap.get(
                    str((p.get("author") or {}).get("username") or "").lower())
    # Cross-handle: a display name set on the source links it to the X avatar.
    _fill_avatars_by_name(posts, "ig",
                          lambda p: str((p.get("author") or {}).get("username") or "").lower())
    return {"count": len(posts), "total": total, "posts": posts,
            "next_cursor": posts[-1]["id"] if posts else None}


def _identities_json(q):
    platform = (q.get("platform") or "").strip().lower()
    if platform not in ("x", "ig", "fb"):
        return {"error": "platform must be x | ig | fb"}
    return {"names": _handle_names_map(platform)}


def _identity_post(body):
    platform = (body.get("platform") or "").strip().lower()
    handle = (body.get("handle") or "").strip()
    if platform not in ("x", "ig", "fb"):
        return {"error": "platform must be x | ig | fb"}
    if not handle:
        return {"error": "handle is required"}
    _set_handle_name(platform, handle, body.get("display_name") or "")
    return {"ok": True}


# ---------------------------------------------------------------------------
# X List members. A list collects as ONE stream (fast rate limit), but the
# accounts inside it live on x.com. This caches those member accounts so the
# dashboard can show them individually — like Facebook pages / Instagram
# sources — instead of an opaque "managed on x.com". Fetching spends a little
# X budget (guard-checked), so it is cached and refreshed on demand.
# ---------------------------------------------------------------------------

def _xlist_con():
    con = sqlite3.connect(_CFG.db_results, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS xlist_members ("
                "list_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                "username TEXT, display_name TEXT, avatar TEXT, "
                "fetched_ms INTEGER NOT NULL, "
                "PRIMARY KEY (list_id, user_id))")
    return con


def _xlist_members_save(list_id, members):
    con = _xlist_con()
    try:
        now = int(time.time() * 1000)
        con.execute("DELETE FROM xlist_members WHERE list_id = ?", (list_id,))
        con.executemany(
            "INSERT OR REPLACE INTO xlist_members(list_id, user_id, username, "
            "display_name, avatar, fetched_ms) VALUES(?,?,?,?,?,?)",
            [(list_id, m["user_id"], m.get("username", ""), m.get("display_name", ""),
              m.get("avatar", ""), now) for m in members if m.get("user_id")])
        con.commit()
    finally:
        con.close()


def _xlist_members_json(q):
    list_id = str(q.get("list_id") or "").strip()
    if not list_id:
        return {"error": "list_id is required"}
    con = _xlist_con()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT user_id, username, display_name, avatar, fetched_ms "
            "FROM xlist_members WHERE list_id = ? ORDER BY LOWER(display_name)",
            (list_id,))]
    finally:
        con.close()
    return {"members": rows, "count": len(rows),
            "fetched_ms": max((r["fetched_ms"] for r in rows), default=None)}


def _xlist_refresh(body):
    list_id = str(body.get("list_id") or "").strip()
    if not list_id.isdigit():
        return {"error": "a numeric X List id is required"}
    import guard
    v = guard.assess(_CFG, action="fetch", cost=3, queue="list")
    if v.blocked:
        b = v.blocks[0]
        return {"error": f"{b.title} — {b.remedy}", "blocked": True, "guard": v.to_json()}
    if v.warnings and not body.get("ack"):
        return {"error": "Warnings not acknowledged: "
                + "; ".join(w.title for w in v.warnings),
                "blocked": True, "needs_ack": True, "guard": v.to_json()}

    async def run():
        api = auth.open_api(_CFG.db_accounts)
        names = await auth.active_usernames(api)
        if not names:
            return {"error": "No active X account is signed in."}
        out = []
        try:
            async for u in api.list_members(int(list_id),
                                            limit=int(body.get("limit") or 300)):
                out.append({
                    "user_id": str(getattr(u, "id_str", "") or getattr(u, "id", "") or ""),
                    "username": u.username or "",
                    "display_name": u.displayname or "",
                    "avatar": getattr(u, "profileImageUrl", "") or "",
                })
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return {"members": out}

    with _FETCH_LOCK:
        res = _run(run())
    if res.get("error"):
        return res
    _xlist_members_save(list_id, res["members"])
    return {"ok": True, "count": len(res["members"]), "members": res["members"],
            "fetched_ms": int(time.time() * 1000)}


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
        self.send_header("Location", "/app/")
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

    def _redirect(self, where: str):
        """303 See Other — used to send the old page URLs into the React app."""
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _sse_live(self, q):
        """
        GET /api/live — a Server-Sent Events stream of newly collected posts.

        One long-lived response per viewer; each tick is a read-only local
        query, so a browser tab costs the collector nothing. Runs on this
        request's own thread (ThreadingHTTPServer), streams until the client
        goes away, and sends a comment ping on quiet ticks so a dead
        connection is noticed within seconds rather than held forever.

        `Connection: close` on purpose: without a Content-Length the browser
        reads until EOF, which is exactly what a stream wants — and it stops
        this socket being reused for a second request it could never serve.
        """
        try:
            project = int(q.get("project") or 0)
        except (TypeError, ValueError):
            project = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        # Start from NOW: the page already loaded its backlog over /api/tweets;
        # replaying history here would double every post on screen.
        last_ms = int(time.time() * 1000)
        last_id = 0
        quiet = 0
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                if _CFG.db_results.exists():
                    try:
                        with _connect() as con:
                            rows = _live_rows(con, last_ms, last_id, project)
                    except sqlite3.Error:
                        rows = []
                    for r in rows:
                        last_ms = r["collected_ms"]
                        last_id = int(r["tweet_id"])
                        body = json.dumps(r, ensure_ascii=False)
                        self.wfile.write(f"event: post\ndata: {body}\n\n".encode())
                    if rows:
                        quiet = 0
                        self.wfile.flush()
                quiet += 1
                if quiet >= 10:      # ~15s of nothing: prove the pipe is alive
                    quiet = 0
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(LIVE_TICK_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the viewer left; nothing to clean up

    def _serve_app(self, path: str):
        """
        Serve the built SPA (frontend/dist) under /app, if it has been built.

        Same traversal guard as /static/. Any /app path that is not a real
        file falls back to index.html — that is what client-side routing
        needs: /app/watchlists is a React route, not a file on disk. In
        production nginx serves dist/ directly and this route goes unused;
        its point is that `python3 main.py serve` alone gives the full UI.
        """
        dist = APP_DIST_DIR.resolve()
        index = dist / "index.html"
        if not index.is_file():
            return self._send(404, {
                "error": "the new dashboard is not built",
                "detail": "run: cd frontend && npm install && npm run build"})
        rel = unquote(path[len("/app"):]).lstrip("/")
        target = (dist / rel).resolve() if rel else index
        if not target.is_relative_to(dist) or not target.is_file():
            target = index
        ctype = _STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)

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
                    return self._redirect("/app/")
                return self._send(200, self._login_html(), "text/html; charset=utf-8")
            if u.path == "/logout":
                return self._do_logout()
            if not self._require_auth():
                return
            # The React app (frontend/dist, served under /app) IS the dashboard
            # now. The old server-rendered pages at / and /accounts are retired;
            # redirects keep every bookmark and muscle-memory URL working.
            if u.path == "/":
                return self._redirect("/app/")
            if u.path in ("/accounts", "/accounts/"):
                return self._redirect("/app/accounts")
            if u.path == "/app" or u.path.startswith("/app/"):
                return self._serve_app(u.path)
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
            if u.path == "/api/projects":
                return self._send(200, _projects_json())
            if u.path == "/api/watchlists":
                return self._send(200, _watchlists_json(q))
            if u.path == "/api/delivery":
                return self._send(200, _delivery_json(q))
            if u.path == "/api/streams/assignments":
                return self._send(200, _stream_assignments_json())
            if u.path == "/api/live":
                return self._sse_live(q)
            if u.path == "/api/alerts":
                return self._send(200, _alerts_json(q))
            if u.path == "/api/collections":
                return self._send(200, _collections_json(q))
            if u.path == "/api/collections/items":
                return self._send(200, _collection_items_json(q))
            if u.path == "/api/collections/export":
                name = (q.get("name") or "collection").strip() or "collection"
                safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60]
                return self._send(200, _collection_export_csv(q),
                                  "text/csv; charset=utf-8",
                                  {"Content-Disposition":
                                   f'attachment; filename="{safe}.csv"'})
            if u.path == "/api/activity":
                return self._send(200, _activity_json(q))
            if u.path == "/api/activity/logs":
                return self._send(200, _activity_logs_json(q))
            if u.path == "/api/metrics":
                return self._send(200, _metrics_json(q))
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
            if u.path == "/api/identities":
                return self._send(200, _identities_json(q))
            if u.path == "/api/watchlist/xmembers":
                return self._send(200, _xlist_members_json(q))
            if u.path == "/api/ig/status":
                return self._send(200, _ig_status())
            if u.path == "/api/ig/posts":
                return self._send(200, _ig_posts(q))
            if u.path == "/api/fb/status":
                return self._send(200, _fb_status(q))
            if u.path == "/api/fb/posts":
                return self._send(200, _fb_posts(q))
            if u.path == "/api/pool" or u.path.startswith("/api/pool/"):
                # Account Control Panel (store_accounts). Dashboard-only: it is
                # behind _require_auth above, and /api/pool* is NOT in
                # API_KEY_PATHS, so a machine key gets 403 — managing accounts is
                # never a "read". See accounts_api.py / ACCOUNTS.md.
                import accounts_api
                return self._send(200, accounts_api.handle(
                    "GET", u.path[len("/api/pool"):], None, q))
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
            if u.path == "/api/projects":
                return self._send(200, _project_post(body))
            if u.path == "/api/watchlists":
                return self._send(200, _watchlist_post(body))
            if u.path == "/api/watchlists/members":
                return self._send(200, _watchlist_members(body))
            if u.path == "/api/watchlists/filters":
                return self._send(200, _watchlist_filters(body))
            if u.path == "/api/watchlists/interval":
                return self._send(200, _watchlist_interval(body))
            if u.path == "/api/watchlists/remove":
                return self._send(200, _watchlist_remove(body))
            if u.path == "/api/streams/attach":
                return self._send(200, _stream_assign(body, True))
            if u.path == "/api/streams/detach":
                return self._send(200, _stream_assign(body, False))
            if u.path == "/api/delivery/targets":
                return self._send(200, _delivery_target_post(body))
            if u.path == "/api/delivery/targets/update":
                return self._send(200, _delivery_target_update(body))
            if u.path == "/api/delivery/targets/remove":
                return self._send(200, _delivery_target_remove(body))
            if u.path == "/api/delivery/backfill":
                return self._send(200, _delivery_backfill(body))
            if u.path == "/api/alerts":
                return self._send(200, _alert_post(body))
            if u.path == "/api/alerts/update":
                return self._send(200, _alert_update(body))
            if u.path == "/api/alerts/remove":
                return self._send(200, _alert_remove(body))
            if u.path == "/api/collections":
                return self._send(200, _collection_post(body))
            if u.path == "/api/collections/pin":
                return self._send(200, _collection_pin(body))
            if u.path == "/api/collections/remove":
                return self._send(200, _collection_remove(body))
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
            if u.path == "/api/collection":
                paused = bool(body.get("paused"))
                _with_store(lambda st: st.set_collection_paused(paused))
                return self._send(200, {"collection_paused": paused})
            if u.path == "/api/fb/source":
                return self._send(200, _fb_source_post(body))
            if u.path == "/api/fb/fetch":
                return self._send(200, _fb_fetch(body))
            if u.path == "/api/fb/favorites":
                return self._send(200, _fb_favorites(body))
            if u.path == "/api/fb/control":
                return self._send(200, _fb_control(body))
            if u.path == "/api/fb/health":
                return self._send(200, _fb_health_post(body))
            if u.path == "/api/fb/settings":
                return self._send(200, _fb_settings_post(body))
            if u.path == "/api/identity":
                return self._send(200, _identity_post(body))
            if u.path == "/api/watchlist/xmembers/refresh":
                return self._send(200, _xlist_refresh(body))
            if u.path == "/api/ig/source":
                return self._send(200, _ig_source_post(body))
            if u.path == "/api/ig/fetch":
                return self._send(200, _ig_fetch(body))
            if u.path == "/api/ig/control":
                return self._send(200, _ig_control(body))
            if u.path == "/api/ig/settings":
                return self._send(200, _ig_settings_post(body))
            if u.path == "/api/project/fetch":
                return self._send(200, _project_fetch(body))
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
            if u.path == "/api/pool" or u.path.startswith("/api/pool/"):
                import accounts_api
                return self._send(200, accounts_api.handle(
                    "POST", u.path[len("/api/pool"):], body, {}))
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


APP_DIST_DIR = Path(__file__).resolve().parent / "frontend" / "dist"

_STATIC_TYPES = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".ico": "image/x-icon"}
