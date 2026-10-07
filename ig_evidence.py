"""
ig_evidence.py — what actually happened at a sign-in, written down.

WHY THIS EXISTS (2026-10-07). Six Instagram accounts were failing to sign in
and nobody could say why. The streamed browser showed "403" to the operator's
eyes and the activity log recorded `POST /api/login/start -> 200`. The
background login failed with urllib3's "too many 429 error responses" — the
library's words, after it had quietly knocked three more times. Every theory
after that (the proxy range, headless, the phone's country) was a guess,
because the one thing that would have settled it — WHAT answered, with WHICH
status, to WHICH client, through WHICH address — was never kept.

So, three things, all in activity.db beside the log the dashboard already reads:

  record()    one row per sign-in attempt, per door (browser window, background
              login, pasted cookies), success or failure: the HTTP status, who
              answered, the exit address, the browser that was really launched,
              the phone. And one plain line in the activity log, under the
              account's name.

  diagnose()  reads those rows ACROSS accounts and says what the failures have
              in common and what they do not — "four accounts, three addresses,
              two countries, one browser" — so a shared cause is named by the
              tool instead of noticed by a person on the fifth attempt.

  probe_*     a measurement, on request: the same login page, through one
              account's proxy, asked by different clients (plain HTTP, HTTP
              dressed as the phone, the browser dressed as the phone, the
              browser as itself). Which of them Instagram refuses is the answer
              to "is it the address or the browser" — measured, not argued.

Nothing here holds a cookie, a password or a proxy credential.
"""

import json
import re
import sqlite3
import threading
import time

DEFAULT_DB = "activity.db"
_LOCK = threading.Lock()
_KEEP = 5000

LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Doors, as the operator knows them.
DOORS = {"browser": "browser window", "background": "background login",
         "cookie": "pasted session", "probe": "probe"}

# `why` — a small closed vocabulary, because diagnose() groups on it.
WHY_TEXT = {
    "ok": "worked",
    "http_403": "Instagram refused the request (HTTP 403)",
    "http_429": "Instagram is throttling this (HTTP 429, too many requests)",
    "http_401": "Instagram wants a login it did not get (HTTP 401)",
    "proxy_refused": "the proxy refused or dropped the connection",
    "network": "the connection failed before any answer",
    "no_form": "the page loaded but never showed a login form",
    "challenge": "Instagram raised a checkpoint / challenge",
    "two_factor": "Instagram asked for a two-factor code",
    "bad_password": "Instagram says the password is wrong",
    "app_out_of_date": "Instagram says the app build is out of date",
    "session_rejected": "Instagram did not accept the session",
    "no_browser": "no browser could be started on this server",
    "unknown": "failed without a recognisable reason",
}

# Response headers that only Meta's edge writes. Kept as supporting evidence;
# see answered_by() for why an https status needs none of them.
_META_HEADERS = ("x-fb-debug", "x-fb-trip-id", "x-fb-connection-quality",
                 "x-stack", "x-ig-origin-region", "x-ig-request-elapsed-time-ms",
                 "ig-set-password-encryption-web-key-id")


def _con(db=None):
    con = sqlite3.connect(db or DEFAULT_DB, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    con.execute(
        "CREATE TABLE IF NOT EXISTS signin_attempts ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_ms INTEGER NOT NULL,"
        "  platform TEXT, account TEXT,"
        "  door TEXT,"          # browser | background | cookie | probe
        "  stage TEXT,"         # open | login | adopt | probe
        "  ok INTEGER,"
        "  why TEXT,"           # WHY_TEXT key
        "  http_status INTEGER,"
        "  answered_by TEXT,"   # instagram | proxy | network | ''
        "  exit_ip TEXT, country TEXT,"
        "  browser TEXT,"       # what was really launched, and how
        "  phone TEXT,"
        "  detail TEXT,"
        "  extra TEXT)")        # JSON: the navigation chain, headers seen, ...
    con.execute("CREATE INDEX IF NOT EXISTS ix_sa_ts ON signin_attempts(ts_ms)")
    return con


# ------------------------------------------------------------------ reading
# the wire


def why_from_status(status: int) -> str:
    status = int(status or 0)
    if 200 <= status < 400:
        return "ok"
    if status in (401, 403, 429):
        return f"http_{status}"
    if status == 407:
        return "proxy_refused"
    return "unknown" if not status else f"http_{status}"


def answered_by(url: str, status: int, failure: str = "") -> str:
    """Who produced this outcome.

    An HTTP status on an https:// URL can only have been written by the site:
    the proxy carries an encrypted tunnel (CONNECT) and cannot put a status
    page inside it without breaking the certificate, which the client checks.
    A proxy that refuses does so BEFORE the tunnel exists, and the client sees
    that as a failed connection (Chrome: ERR_TUNNEL_CONNECTION_FAILED,
    ERR_PROXY_*; requests: ProxyError) — never as a 403 page."""
    f = (failure or "").upper()
    if "TUNNEL" in f or "PROXY" in f or int(status or 0) == 407:
        return "proxy"
    if failure and not status:
        return "network"
    if status and str(url or "").lower().startswith("https://"):
        return "instagram"
    return ""


def why_from_exception(exc) -> str:
    """A sign-in exception → the vocabulary above. Reads instagrapi's names
    and urllib3's, and the message when the name says nothing."""
    return _why(type(exc).__name__, str(exc))


def why_from_outcome(o, wire_: dict | None = None) -> str:
    """signin.Outcome → the vocabulary. The status Instagram actually sent
    (wire) outranks the words of whichever exception carried it."""
    if getattr(o, "ok", False):
        return "ok"
    needs = getattr(o, "needs", "")
    if needs == "totp":
        return "two_factor"
    if needs == "browser":
        return "challenge"
    detail = str(getattr(o, "detail", "") or "")
    m = re.search(r"\b([A-Z][A-Za-z]+(?:Error|Required|Minutes|Password|Suspended))\b",
                  detail)
    why = _why(m.group(1) if m else "", detail)
    status = int((wire_ or {}).get("status") or 0)
    if why == "unknown" and status >= 400:
        return why_from_status(status)
    return why


def _why(name: str, text: str) -> str:
    text = str(text).lower()
    if "out of date" in text or "upgrade your app" in text:
        return "app_out_of_date"
    if name == "TwoFactorRequired":
        return "two_factor"
    if name in ("ChallengeRequired", "ChallengeError", "CheckpointRequired",
                "AccountSuspended") or "challenge_required" in text \
            or "checkpoint" in text:
        return "challenge"
    if name == "BadPassword" or "bad_password" in text:
        return "bad_password"
    if name in ("PleaseWaitFewMinutes", "ClientThrottledError", "RateLimitError") \
            or "429" in text or "please wait" in text:
        return "http_429"
    if name == "ProxyError" or "tunnel connection failed" in text \
            or "proxyerror" in text or "407" in text:
        return "proxy_refused"
    if name in ("ClientForbiddenError",) or " 403" in text:
        return "http_403"
    if name in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "SSLError",
                "ClientConnectionError", "TimeoutError"):
        return "network"
    return "unknown"


def wire(cl) -> dict:
    """The last thing Instagram said to this app client, without secrets:
    status, the path asked, Retry-After, Instagram's own message. Empty when
    the client never got an answer."""
    out = {}
    try:
        r = getattr(cl, "last_response", None)
        if r is not None:
            out["status"] = int(getattr(r, "status_code", 0) or 0)
            try:
                from urllib.parse import urlsplit
                out["path"] = urlsplit(r.url).path
            except Exception:
                pass
            h = getattr(r, "headers", {}) or {}
            if h.get("Retry-After"):
                out["retry_after"] = str(h.get("Retry-After"))
            seen = [k for k in _META_HEADERS if k in {x.lower() for x in h}]
            if seen:
                out["meta_headers"] = seen
        j = getattr(cl, "last_json", None) or {}
        if isinstance(j, dict):
            for k in ("message", "error_type", "status"):
                if j.get(k):
                    out[k if k != "status" else "ig_status"] = str(j[k])[:200]
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ writing


def describe(row: dict) -> str:
    """One line a person can read in the activity log."""
    bits = [f"[signin] @{row.get('account') or '?'} · "
            f"{DOORS.get(row.get('door'), row.get('door') or '?')}"
            f"{' (' + row['stage'] + ')' if row.get('stage') not in ('', None, 'login') else ''}: "
            + ("OK" if row.get("ok") else "FAILED")]
    why = row.get("why") or ""
    if why and why != "ok":
        bits.append(WHY_TEXT.get(why, why))
    if row.get("http_status") and not str(why).startswith("http_"):
        bits.append(f"HTTP {row['http_status']}")
    if row.get("answered_by") and not row.get("ok"):
        bits.append(f"answered by {row['answered_by']}")
    if row.get("exit_ip"):
        bits.append(f"via {row['exit_ip']}"
                    + (f" ({row['country']})" if row.get("country") else ""))
    if row.get("browser"):
        bits.append(row["browser"])
    if row.get("phone"):
        bits.append(row["phone"])
    if row.get("detail"):
        bits.append(str(row["detail"])[:300])
    return " — ".join(bits[:2]) + ("; " + "; ".join(bits[2:]) if bits[2:] else "")


def record(account, door, ok, *, stage="login", why="", http_status=0,
           answered="", exit_ip="", country="", browser="", phone="",
           detail="", extra=None, platform="instagram", db=None,
           now_ms=None, log_line=True) -> dict:
    """Keep one attempt. Never raises — evidence must not break a sign-in."""
    row = {
        "ts_ms": int(time.time() * 1000) if now_ms is None else int(now_ms),
        "platform": platform,
        "account": str(account or "").strip().lstrip("@").lower(),
        "door": door, "stage": stage, "ok": 1 if ok else 0,
        "why": why or ("ok" if ok else "unknown"),
        "http_status": int(http_status or 0), "answered_by": answered or "",
        "exit_ip": exit_ip or "", "country": (country or "").upper(),
        "browser": browser or "", "phone": phone or "",
        "detail": str(detail or "")[:600],
        "extra": json.dumps(extra or {}, default=str)[:4000],
    }
    try:
        with _LOCK:
            con = _con(db)
            cols = list(row)
            con.execute(
                f"INSERT INTO signin_attempts({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", [row[c] for c in cols])
            con.execute("DELETE FROM signin_attempts WHERE id <= "
                        "(SELECT MAX(id) FROM signin_attempts) - ?", (_KEEP,))
            con.commit()
            con.close()
    except Exception:
        pass
    if log_line:
        try:
            import activity_log
            activity_log.log_event(platform, describe(row), account=row["account"],
                                   level="ok" if ok else "error", db=db)
        except Exception:
            pass
    return row


def attempts(hours=72, *, db=None, now_ms=None, platform="instagram") -> list:
    """Attempts in the window, newest first, `extra` decoded."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        con = _con(db)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM signin_attempts WHERE ts_ms >= ? AND platform = ? "
            "ORDER BY id DESC", (now - int(hours * 3600 * 1000), platform)).fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except Exception:
            d["extra"] = {}
        d["ok"] = bool(d["ok"])
        out.append(d)
    return out


# ------------------------------------------------------------------ diagnosis


def _net(ip: str) -> str:
    p = (ip or "").split(".")
    return ".".join(p[:2]) + ".x.x" if len(p) == 4 else ""


def _names(accts) -> str:
    return ", ".join("@" + a for a in sorted(accts))


def _ago(ms, now) -> str:
    s = max(0, (now - ms) / 1000)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)}m ago"
    if s < 172800:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def diagnose(rows=None, conditions=None, *, collecting=None, db=None,
             now_ms=None, hours=72) -> dict:
    """What the accounts' failures have in common, in plain words.

    `rows`        attempts() output (read from `db` when None)
    `conditions`  decider.open_conditions("instagram") — the collectors' side
    `collecting`  usernames that own sources right now

    The reasoning is deliberately simple and is printed with its evidence:
    a failure seen on several accounts is compared along the things an account
    does NOT share with the others (its address, its country) and the things it
    DOES (this server's browser, the door). What every failure has in common
    and no success has is the suspect. When there is no success to compare
    with, it says exactly that instead of picking one."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rows = attempts(hours, db=db, now_ms=now) if rows is None else rows
    findings = []

    # The latest word on each (account, door); probes are read separately.
    latest = {}
    for r in sorted((r for r in rows if r.get("door") != "probe"),
                    key=lambda r: r["ts_ms"]):
        latest[(r["account"], r["door"])] = r
    fails = [r for r in latest.values() if not r["ok"]]
    wins = [r for r in latest.values() if r["ok"]]

    groups = {}
    for r in fails:
        groups.setdefault((r["door"], r["why"]), []).append(r)

    for (door, why), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        accts = {r["account"] for r in g}
        what = WHY_TEXT.get(why, why)
        door_name = DOORS.get(door, door)
        nets = {_net(r["exit_ip"]) for r in g} - {""}
        ccs = {r["country"] for r in g} - {""}
        browsers = {r["browser"] for r in g} - {""}
        same_door_wins = [w for w in wins if w["door"] == door
                          and w["account"] not in accts]
        ev = [f"@{r['account']}: {_ago(r['ts_ms'], now)}"
              + (f", HTTP {r['http_status']}" if r["http_status"] else "")
              + (f", via {r['exit_ip']}" + (f" ({r['country']})" if r["country"] else "")
                 if r["exit_ip"] else "")
              for r in sorted(g, key=lambda r: -r["ts_ms"])]
        if len(accts) == 1:
            r = g[0]
            findings.append({
                "level": "single", "door": door, "why": why,
                "accounts": sorted(accts),
                "title": f"@{r['account']}: {door_name} — {what}",
                "detail": ("Only this account shows it"
                           + (f"; {_names(w['account'] for w in same_door_wins)} "
                              f"got through the same door"
                              if same_door_wins else
                              "; no other account has tried this door in the "
                              "window, so there is nothing to compare it with")
                           + "."),
                "evidence": ev})
            continue
        lines = [f"{len(accts)} accounts fail the same way through the "
                 f"{door_name}: {what}."]
        suspect = "undetermined"
        if same_door_wins:
            win_nets = {_net(w["exit_ip"]) for w in same_door_wins} - {""}
            if nets and win_nets and not (nets & win_nets):
                suspect = "address"
                lines.append(
                    f"{_names(w['account'] for w in same_door_wins)} got through "
                    f"the same door on {', '.join(sorted(win_nets))}, and every "
                    f"failure is on {', '.join(sorted(nets))}. What differs is "
                    f"the address range.")
            else:
                suspect = "account"
                lines.append(
                    f"{_names(w['account'] for w in same_door_wins)} got through "
                    f"the same door on an address in the same range, so the door "
                    f"and the range both work. What differs is the accounts "
                    f"themselves (or their phones).")
        elif len(nets) >= 2:
            suspect = "shared"
            lines.append(
                f"They are on {len(nets)} different address ranges"
                + (f" in {len(ccs)} countries ({', '.join(sorted(ccs))})"
                   if len(ccs) >= 2 else "")
                + ", so it is not one account and not one address. What they "
                  "share is this server and how it knocks"
                + (f": {next(iter(browsers))}" if len(browsers) == 1 else "")
                + ". No account has got through this door in the window.")
        else:
            lines.append(
                "All of them are on the same address range"
                + (f" ({next(iter(nets))})" if nets else "")
                + " and nothing has got through this door in the window, so "
                  "the range and a shared cause cannot be told apart from "
                  "these attempts alone. Run a probe on one of them.")
        findings.append({"level": "shared", "door": door, "why": why,
                         "suspect": suspect, "accounts": sorted(accts),
                         "title": f"{len(accts)} accounts: {door_name} — {what}",
                         "detail": " ".join(lines), "evidence": ev})

    # The collectors' side: one kind of condition open on several accounts.
    kinds = {}
    for c in conditions or []:
        if c.get("account"):
            kinds.setdefault(c["kind"], set()).add(c["account"].lower())
    coll = {c.lower() for c in (collecting or [])}
    for kind, accts in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        if len(accts) < 2:
            continue
        line = f"{len(accts)} accounts have '{kind}' open while collecting."
        if coll and coll <= accts:
            line += (f" That is every account that is collecting right now "
                     f"({len(coll)}), so nothing is being collected.")
        findings.append({"level": "shared", "door": "collector", "why": kind,
                         "accounts": sorted(accts),
                         "title": f"{len(accts)} accounts: collector — {kind}",
                         "detail": line, "evidence": []})

    # Probes: the measurement outranks the inference. Newest per account.
    seen = set()
    for r in sorted((r for r in rows if r.get("door") == "probe"
                     and r.get("stage") == "verdict"), key=lambda r: -r["ts_ms"]):
        if r["account"] in seen:
            continue
        seen.add(r["account"])
        findings.insert(0, {
            "level": "measured", "door": "probe", "why": r["why"],
            "accounts": [r["account"]],
            "title": f"Probe on @{r['account']} ({_ago(r['ts_ms'], now)}): "
                     f"{PROBE_TITLES.get(r['why'], r['why'])}",
            "detail": r.get("detail") or "",
            "evidence": [f"{c.get('client')}: "
                         + (f"HTTP {c.get('status')}" if c.get("status")
                            else (c.get("error") or "no answer"))
                         for c in (r.get("extra") or {}).get("clients", [])]})

    per = {}
    for (acct, door), r in latest.items():
        per.setdefault(acct, []).append({
            k: r[k] for k in ("door", "stage", "ok", "why", "http_status",
                              "answered_by", "exit_ip", "country", "browser",
                              "phone", "detail", "ts_ms")})
    for v in per.values():
        v.sort(key=lambda r: -r["ts_ms"])
    return {"generated_ms": now, "window_hours": hours,
            "attempts": len(rows), "findings": findings, "accounts": per}


# ------------------------------------------------------------------ the probe

PROBE_TITLES = {
    "address": "Instagram refuses this address, whoever asks",
    "browser": "Instagram answers this address but refuses the browser",
    "disguise": "Instagram accepts the browser as itself and refuses it "
                "dressed as the phone",
    "claims_chrome": "Instagram refuses anything that says it is Chrome from "
                     "this address",
    "open": "Instagram answers every client — the login page is reachable",
    "proxy": "the proxy did not carry the requests",
    "mixed": "the clients disagree in a way that names no single cause",
}


def probe_http(proxy: str, device: dict | None = None, *, get=None,
               timeout: int = 25) -> list:
    """Ask for the login page through `proxy` as two plain-HTTP clients: curl,
    and the same library carrying this phone's browser headers. No cookies, no
    account. `get(url, headers, proxies, timeout)` is injectable for tests."""
    if get is None:
        import requests

        def get(url, headers, proxies, timeout):
            return requests.get(url, headers=headers, proxies=proxies,
                                timeout=timeout, allow_redirects=False)
    clients = [("plain HTTP (curl)", {"User-Agent": "curl/8.4.0"})]
    if device:
        import ig_identity
        h = dict(ig_identity.web_headers(device))
        h.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;"
                               "q=0.9,*/*;q=0.8")
        clients.append(("plain HTTP with the phone's browser headers", h))
    out = []
    for name, headers in clients:
        row = {"client": name, "status": 0, "error": "", "ms": 0}
        t0 = time.time()
        try:
            r = get(LOGIN_URL, headers, {"http": proxy, "https": proxy}, timeout)
            row["status"] = int(getattr(r, "status_code", 0) or 0)
            loc = (getattr(r, "headers", {}) or {}).get("Location")
            if loc:
                row["location"] = str(loc)[:120]
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            row["why"] = why_from_exception(e)
        row["ms"] = int((time.time() - t0) * 1000)
        out.append(row)
    return out


def _fine(c) -> bool:
    return 200 <= int(c.get("status") or 0) < 400


def probe_verdict(clients: list) -> tuple:
    """(why, sentence) from the rows of probe_http() and the browser probes.
    Browser rows carry kind='browser' and variant 'phone' | 'plain'."""
    http = [c for c in clients if c.get("kind") != "browser"]
    br = [c for c in clients if c.get("kind") == "browser"]
    answered = [c for c in clients if c.get("status")]
    if not answered:
        return "proxy", ("No client got any answer through this proxy: "
                         + "; ".join(f"{c['client']}: {c.get('error') or 'nothing'}"
                                     for c in clients))
    curl = http[0] if http else None
    dressed = http[1] if len(http) > 1 else None
    phone = next((c for c in br if c.get("variant") == "phone"), None)
    plain = next((c for c in br if c.get("variant") == "plain"), None)

    def st(c):
        return f"HTTP {c['status']}" if c.get("status") else (c.get("error") or "no answer")

    if all(not _fine(c) for c in clients):
        return "address", ("Every client was refused, including plain curl ("
                           + ", ".join(st(c) for c in clients) + "). Nothing "
                           "about the browser can fix this; the address needs "
                           "to rest or be replaced.")
    if all(_fine(c) for c in clients):
        return "open", ("Every client got the login page ("
                        + ", ".join(st(c) for c in clients) + "). A sign-in "
                        "that fails now is failing after this point.")
    if curl and _fine(curl) and br and all(not _fine(c) for c in br):
        if dressed and not _fine(dressed):
            return "claims_chrome", (
                f"curl got {st(curl)}, but the same request carrying the "
                f"phone's Chrome headers got {st(dressed)}, and so did the real "
                f"browser ({', '.join(st(c) for c in br)}). From this address "
                f"Instagram turns away what presents as Chrome — a logged-out "
                f"browser visit — while a bare client passes. The browser "
                f"build is not what it objects to.")
        return "browser", (
            f"curl got {st(curl)}"
            + (f" and so did plain HTTP with the phone's headers ({st(dressed)})"
               if dressed else "")
            + f", but the real browser got {', '.join(st(c) for c in br)}. The "
              f"address is accepted; what is refused is this server's browser.")
    if phone and plain and _fine(plain) and not _fine(phone):
        return "disguise", (
            f"The browser as itself got {st(plain)}; dressed as the phone it "
            f"got {st(phone)}. The phone disguise is what is refused.")
    return "mixed", "; ".join(f"{c['client']}: {st(c)}" for c in clients)
