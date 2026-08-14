"""
accounts_api.py — the dashboard API for the Account Control Panel.

Thin request layer over store_accounts.AccountStore: it validates input and
shapes JSON, nothing more. web.py delegates the `/api/pool*` routes here so the
146 KB server file grows by a few lines, not a few hundred. Everything is
dashboard-only (cookie-authed); an API key must never reach these — managing
accounts is not a read (RULEBOOK §5).

Contract with web.py, matching how the rest of the API already behaves:
  * a handler returns a plain dict;
  * a validation problem is returned as {"error": "..."} with HTTP 200 (the
    frontend's client.js turns {error} into a thrown Error either way);
  * ids come back as ints, which is fine — these are small AUTOINCREMENT ids,
    not snowflakes, so no string-id rule applies.

The pool DB path and encryption key come from the environment so nothing secret
is hard-coded:
  ACCOUNTS_DB          default 'pool.db'
  ACCOUNTS_SECRET_KEY  required to store/read secrets (see store_accounts)
"""

from __future__ import annotations

import os

from store_accounts import AccountStore, PLATFORMS, STATUSES, SecretError


def _open() -> AccountStore:
    return AccountStore(os.getenv("ACCOUNTS_DB", "pool.db")).open()


def _acct_json(a) -> dict:
    """The safe wire shape of one account — never a decrypted secret."""
    return {
        "account_id": a.account_id,
        "platform": a.platform,
        "label": a.label,
        "login": a.login,
        "proxy_id": a.proxy_id,
        "status": a.status,
        "health": a.health,
        "last_success_at": a.last_success_at,
        "has_totp": a.has_totp,
        "has_proxy": a.has_proxy,
        "backup_codes_left": a.backup_codes_left,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def _pool_summary(st: AccountStore, platform: str) -> dict:
    active = st.active(platform)
    backups = st.backups_left(platform)
    return {
        "active": active.label if active else None,
        "active_id": active.account_id if active else None,
        "backups": backups,
        # "low" drives the amber warning in the panel: one bad day from empty.
        "low": backups <= 1,
    }


def _as_codes(raw) -> list[str]:
    """Accept a list, or a blob pasted with newlines/commas/spaces."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(c).strip() for c in raw if str(c).strip()]
    parts = str(raw).replace(",", "\n").split()
    return [p.strip() for p in parts if p.strip()]


def _need_int(body: dict, key: str) -> int:
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"{key} is required")
    return int(v)


def handle(method: str, subpath: str, body: dict | None, query: dict | None) -> dict:
    """
    Dispatch one `/api/pool*` request. `subpath` is the part AFTER '/api/pool'
    (so '' is the list, '/add' adds, etc.). Returns a dict for web.py to send.
    """
    body = body or {}
    query = query or {}
    sub = subpath.rstrip("/")

    try:
        st = _open()
    except SecretError as e:
        return {"error": str(e)}

    try:
        # ---- reads ----
        if method == "GET" and sub in ("", "/list"):
            accounts = [_acct_json(a) for a in st.list()]
            platforms = {p: _pool_summary(st, p) for p in PLATFORMS}
            return {"platforms": platforms, "accounts": accounts,
                    "cipher_ready": st._cipher.enabled}

        if method == "GET" and sub == "/totp":
            # Preview the current code — handy to confirm an account's TOTP
            # secret was pasted correctly. Never logged, only shown on request.
            aid = int(query.get("account_id"))
            code = st.totp_now(aid)
            return {"account_id": aid, "code": code}

        # ---- writes ----
        if method == "POST" and sub == "/add":
            platform = (body.get("platform") or "").strip()
            label = (body.get("label") or "").strip()
            login = (body.get("login") or "").strip()
            if platform not in PLATFORMS:
                return {"error": f"platform must be one of {', '.join(PLATFORMS)}"}
            if not label:
                return {"error": "label is required"}
            if not login:
                return {"error": "login (username / email) is required"}
            try:
                aid = st.add(
                    platform, label, login,
                    password=body.get("password") or "",
                    totp_secret=body.get("totp_secret") or "",
                    backup_codes=_as_codes(body.get("backup_codes")),
                    proxy_id=(body.get("proxy_id") or None),
                    proxy_url=body.get("proxy_url") or "",
                    notes=body.get("notes") or "",
                )
            except SecretError as e:
                return {"error": str(e)}
            return {"ok": True, "account_id": aid}

        if method == "POST" and sub == "/update":
            aid = _need_int(body, "account_id")
            # Only pass fields that were actually supplied (None = leave alone).
            kw = {}
            for k in ("label", "login", "password", "totp_secret", "proxy_id",
                      "proxy_url", "notes"):
                if k in body and body[k] is not None:
                    kw[k] = body[k]
            try:
                st.update(aid, **kw)
            except SecretError as e:
                return {"error": str(e)}
            return {"ok": True}

        if method == "POST" and sub == "/remove":
            st.remove(_need_int(body, "account_id"))
            return {"ok": True}

        if method == "POST" and sub == "/status":
            aid = _need_int(body, "account_id")
            status = (body.get("status") or "").strip()
            if status not in STATUSES:
                return {"error": f"status must be one of {', '.join(STATUSES)}"}
            st.set_status(aid, status, body.get("health") or "")
            return {"ok": True}

        if method == "POST" and sub == "/promote":
            st.promote(_need_int(body, "account_id"))
            return {"ok": True}

        if method == "POST" and sub == "/failover":
            platform = (body.get("platform") or "").strip()
            if platform not in PLATFORMS:
                return {"error": f"platform must be one of {', '.join(PLATFORMS)}"}
            promoted = st.failover(
                platform,
                rotate_proxy=(body.get("rotate_proxy") or None),
                reason=body.get("reason") or "manual failover from panel",
            )
            return {"ok": True, "promoted": promoted.label if promoted else None}

        if method == "POST" and sub == "/backup_codes":
            aid = _need_int(body, "account_id")
            codes = _as_codes(body.get("codes"))
            if not codes:
                return {"error": "paste at least one backup code"}
            st.set_backup_codes(aid, codes)
            return {"ok": True, "remaining": st.backup_codes_remaining(aid)}

        if method == "POST" and sub == "/login":
            # Signing an account in FROM the dashboard is not wired yet. Rather
            # than pretend, return the ACCURATE per-platform way to sign this
            # account in on the server — Instagram never used a streamed
            # browser (that path is dead; cookie/password is the way), so the
            # old X-centric text was wrong on IG/FB cards.
            aid = _need_int(body, "account_id")
            plat = st.get(aid).platform
            todo = {
                "ig": "Instagram signs in on the SERVER, not from here. As the "
                      "xscraper user run: `.venv/bin/python3 ig_login.py "
                      "<username>` (password in .env) — or import a fresh "
                      "cookie with `ig_import.py \"<sessionid>\"`. The account's "
                      "residential proxy is used automatically.",
                "fb": "Facebook signs in on the SERVER via FB_EMAIL/FB_PASSWORD "
                      "in .env (it persists fb_state.json on first run). There "
                      "is no dashboard login button for Facebook.",
                "x": "X sign-in uses the streamed-browser flow on the server "
                     "(see ACCOUNTS.md §5). Dashboard-triggered login is not "
                     "wired yet.",
            }.get(plat, "Dashboard-triggered login is not wired for this "
                        "platform yet.")
            return {"ok": False, "todo": todo}

        return {"error": f"unknown pool route: {method} {subpath}"}

    except KeyError as e:
        return {"error": f"no such account: {e}"}
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    finally:
        st.close()
