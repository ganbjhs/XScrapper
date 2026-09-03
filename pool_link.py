"""
pool_link.py — the one-way bridge from the COLLECTORS to the account pool.

Why this file exists
--------------------
`store_accounts.py` has always known how to say "this account collected
successfully" (`record_success`) and "this account needs a human"
(`quarantine` / `set_status`). Nothing ever called them. That is why every
card in the Account Control Panel showed `last success —` forever, on every
platform, no matter how much data the account was actually pulling: the panel
was reading a column no writer wrote.

This module is that writer. It is deliberately tiny and deliberately
FORGIVING — a collector's job is to collect, so a missing pool.db, a missing
ACCOUNTS_SECRET_KEY, or an account that simply is not in the pool must never
raise into a collection pass. Every function here returns a bool and swallows
its own errors.

It is one-way on purpose: the pool learns from the collectors, the collectors
never take instructions from the pool. Failover stays an operator action in the
panel (see ACCOUNTS.md §2), not something a scraper triggers mid-pass.

Platform keys are the POOL's vocabulary ('x' | 'ig' | 'fb'), not config.py's
('x' | 'instagram') — `platform_key()` converts when a caller has the other one.
"""

from __future__ import annotations

import os

__all__ = [
    "platform_key", "find", "record_success", "note_needs_login",
    "note_health", "quarantine",
]


def platform_key(platform: str) -> str:
    """config.py says 'instagram'; the pool says 'ig'. Accept either."""
    p = (platform or "").strip().lower()
    return {"instagram": "ig", "facebook": "fb", "twitter": "x"}.get(p, p)


def _open():
    """An open AccountStore, or None if the pool is not usable here."""
    try:
        from store_accounts import AccountStore
    except Exception:
        return None
    path = os.getenv("ACCOUNTS_DB", "pool.db")
    if not os.path.exists(path):
        # No pool configured on this host — that is a normal deployment, not an
        # error. A collector must not care.
        return None
    try:
        return AccountStore(path).open()
    except Exception:
        return None


def _norm(s) -> str:
    return (s or "").strip().lower().lstrip("@")


# The columns needed to identify an account and report on it. Read as RAW SQL,
# on purpose: AccountStore.list() builds full Account objects, and building one
# DECRYPTS the account's backup codes. That would make every function in this
# module depend on ACCOUNTS_SECRET_KEY being present in the collector's
# environment — and the systemd units (deploy/xscraper-ig.service and friends)
# set no EnvironmentFile, so on the box it usually is not. The failure would
# have been silent: SecretError, caught below, "no pool", cards blank forever.
# Nothing here ever needs a secret, so nothing here ever asks for one.
_COLS = ("account_id", "platform", "label", "login", "status", "health",
         "last_success_at")


class _Row:
    """Just enough of an account to identify it and report on it. No secrets."""

    __slots__ = _COLS

    def __init__(self, r):
        for c in _COLS:
            setattr(self, c, r[c])

    def __repr__(self):
        return f"<pool {self.platform}:{self.label} #{self.account_id} {self.status}>"


def _lookup(st, platform: str, who: str):
    target = _norm(who)
    if not target:
        return None
    rows = [_Row(r) for r in st.db.execute(
        f"SELECT {', '.join(_COLS)} FROM managed_accounts WHERE platform = ?",
        (platform_key(platform),))]
    # Login first, label second: the login is what a collector actually holds,
    # and two accounts can share a display name long before they share a handle.
    return (next((a for a in rows if _norm(a.login) == target), None)
            or next((a for a in rows if _norm(a.label) == target), None))


def find(platform: str, who: str):
    """
    The pool account for `who` on `platform`, or None.

    `who` may be the login (what a collector knows — '@handle' tolerated) or the
    human label (what the panel shows). Matching is case-insensitive because the
    two halves of this system are typed by different people at different times:
    the panel's "Sana Akhtar" and the collector's "sanaakhtar221" both have to
    land on the same row.
    """
    st = _open()
    if st is None:
        return None
    try:
        return _lookup(st, platform, who)
    except Exception:
        return None
    finally:
        try:
            st.close()
        except Exception:
            pass


def _with_account(platform: str, who: str, fn) -> bool:
    st = _open()
    if st is None:
        return False
    try:
        hit = _lookup(st, platform, who)
        if hit is None:
            return False
        fn(st, hit)
        return True
    except Exception:
        return False
    finally:
        try:
            st.close()
        except Exception:
            pass


def record_success(platform: str, who: str) -> bool:
    """A pass worked. Stamps last_success_at and clears any health note.

    Call this ONCE per account per pass, after the pass, not per item — the
    column means "this account was working at this time", and a per-item write
    would be one sqlite round trip per post for no extra information.
    """
    return _with_account(platform, who, lambda st, a: st.record_success(a.account_id))


def note_needs_login(platform: str, who: str, reason: str = "") -> bool:
    """The session was rejected. Marks the account `needs_login` with the reason.

    NOT `quarantined`: a rejected cookie is usually fixable by signing in again,
    and quarantine means "do not retry in a loop" (ACCOUNTS.md §2). Reserve that
    for a checkpoint or a ban, which `quarantine()` below is for.
    """
    def go(st, a):
        # 'active' is not overwritten blindly: an account can serve some sources
        # and fail others while a checkpoint is half-open, and demoting it
        # mid-pass would hand collection to a backup for a blip. Record the
        # health note either way; change status only if it was not the active one.
        if a.status == "active":
            st.set_status(a.account_id, "active", (reason or "session rejected")[:400])
        else:
            st.set_status(a.account_id, "needs_login", (reason or "session rejected")[:400])
    return _with_account(platform, who, go)


def note_health(platform: str, who: str, health: str) -> bool:
    """Leave a health note without changing status (a warning, not a verdict)."""
    return _with_account(
        platform, who, lambda st, a: st.set_status(a.account_id, a.status, (health or "")[:400]))


def promote(platform: str, who: str) -> bool:
    """Make `who` the pool's active account for its platform (failover's
    other half). The one-active invariant is store_accounts.promote's."""
    return _with_account(platform, who, lambda st, a: st.promote(a.account_id))


def quarantine(platform: str, who: str, reason: str = "") -> bool:
    """Checkpoint / ban / anything a human must clear. Pulls it out of rotation."""
    return _with_account(
        platform, who, lambda st, a: st.quarantine(a.account_id, reason))
