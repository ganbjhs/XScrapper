"""
Offline tests for pool_link.py — the collectors' write-path into the account
pool — plus the /login contract the Account Control Panel depends on.

Run as a script (the project's convention), not under pytest:

    python3 tests/test_pool_link.py

What is actually being pinned here is the bug these tests were written for: the
pool had `record_success` from day one and NOTHING called it, so every card in
the panel read `last success —` forever regardless of how much the account was
collecting. A test that only checked store_accounts would have stayed green
through all of it, because the store was never the broken half.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEY = "test-secret-key-please-change"
os.environ["ACCOUNTS_SECRET_KEY"] = KEY

import accounts_api as A          # noqa: E402
import pool_link                  # noqa: E402
from store_accounts import AccountStore  # noqa: E402


def _fresh_pool():
    """A pool.db nothing else has touched, wired up as the ambient one."""
    path = os.path.join(tempfile.mkdtemp(), "pool.db")
    os.environ["ACCOUNTS_DB"] = path
    return path


def _add(platform, label, login, status=None):
    with AccountStore(os.environ["ACCOUNTS_DB"], secret_key=KEY) as st:
        aid = st.add(platform, label, login)
        if status:
            st.set_status(aid, status)
        return aid


def _get(aid):
    with AccountStore(os.environ["ACCOUNTS_DB"], secret_key=KEY) as st:
        return st.get(aid)


# ---------------------------------------------------------------------------

def test_record_success_stamps_the_card():
    _fresh_pool()
    aid = _add("ig", "Sana Akhtar", "sanaakhtar221")
    assert _get(aid).last_success_at is None, "starts blank — this is the bug's symptom"
    assert pool_link.record_success("ig", "sanaakhtar221") is True
    assert _get(aid).last_success_at, "a successful pass must stamp last_success_at"
    print("ok  record_success stamps the card the panel renders")


def test_matching_is_forgiving():
    _fresh_pool()
    aid = _add("ig", "Sana Akhtar", "sanaakhtar221")
    # A collector knows the handle; a person typed the label. Both must land.
    assert pool_link.find("ig", "SANAAKHTAR221").account_id == aid
    assert pool_link.find("ig", "@sanaakhtar221").account_id == aid
    assert pool_link.find("ig", "  Sana Akhtar ").account_id == aid
    # config.py says 'instagram', the pool says 'ig'.
    assert pool_link.find("instagram", "sanaakhtar221").account_id == aid
    assert pool_link.find("ig", "someone-else") is None
    print("ok  matching tolerates case, @, whitespace and the platform alias")


def test_login_required_does_not_demote_the_active_account():
    _fresh_pool()
    aid = _add("ig", "Shoaib", "shoaibakhtar4915", status="active")
    assert pool_link.note_needs_login("ig", "shoaibakhtar4915", "login_required") is True
    a = _get(aid)
    assert a.status == "active", "one bad source must not hand collection to a backup"
    assert "login_required" in a.health, "but the reason has to reach the card"

    bid = _add("ig", "Youssef", "youssefnasser168")           # a warm backup
    pool_link.note_needs_login("ig", "youssefnasser168", "cookie expired")
    b = _get(bid)
    assert b.status == "needs_login" and "cookie expired" in b.health
    print("ok  needs_login marks a backup, only annotates the active account")


def test_quarantine_pulls_out_of_rotation():
    _fresh_pool()
    aid = _add("ig", "Omar", "omarfarooq724", status="active")
    assert pool_link.quarantine("ig", "omarfarooq724", "checkpoint_required") is True
    a = _get(aid)
    assert a.status == "quarantined" and "checkpoint_required" in a.health
    print("ok  quarantine pulls a checkpointed account out of rotation")


def test_no_pool_is_never_an_error():
    """A collector's job is to collect. A missing/unreadable pool must not raise."""
    os.environ["ACCOUNTS_DB"] = os.path.join(tempfile.mkdtemp(), "nope.db")
    assert pool_link.record_success("ig", "whoever") is False
    assert pool_link.note_needs_login("ig", "whoever", "x") is False
    assert pool_link.quarantine("ig", "whoever") is False
    assert pool_link.find("ig", "whoever") is None
    print("ok  a missing pool degrades to False, never an exception")


def test_works_without_the_secret_key():
    """The collector units set no EnvironmentFile, so ACCOUNTS_SECRET_KEY is
    usually absent in their environment. Nothing here needs a secret — and it
    must not accidentally ask for one, because AccountStore.list() decrypts
    backup codes just to build an Account, and the resulting SecretError would
    be swallowed as "no pool" and leave the cards blank forever."""
    _fresh_pool()
    with AccountStore(os.environ["ACCOUNTS_DB"], secret_key=KEY) as st:
        aid = st.add("ig", "Sana Akhtar", "sanaakhtar221",
                     password="p", backup_codes=["AAA", "BBB"])
    saved = os.environ.pop("ACCOUNTS_SECRET_KEY")
    try:
        assert pool_link.find("ig", "sanaakhtar221").account_id == aid
        assert pool_link.record_success("ig", "sanaakhtar221") is True
    finally:
        os.environ["ACCOUNTS_SECRET_KEY"] = saved
    assert _get(aid).last_success_at
    print("ok  writes the card with no ACCOUNTS_SECRET_KEY in the environment")


def test_login_route_advertises_the_streamed_window():
    _fresh_pool()
    for plat in ("x", "ig"):
        aid = A.handle("POST", "/add",
                       {"platform": plat, "label": f"{plat}_a", "login": "u"}, {})["account_id"]
        r = A.handle("POST", "/login", {"account_id": aid}, {})
        assert r["ok"] is False, "the route must not silently open a browser"
        assert r["signin"]["ready"] is True
        assert r["signin"]["account_id"] == aid
        assert r["signin"]["start"] == "/api/login/start"
        assert r["signin"]["body"] == {"account_id": aid}
        assert f"profiles/pool_{aid}" in r["todo"]

    fid = A.handle("POST", "/add",
                   {"platform": "fb", "label": "fb_a", "login": "u"}, {})["account_id"]
    r = A.handle("POST", "/login", {"account_id": fid}, {})
    assert "signin" not in r, "Facebook has no streamed window — don't claim one"
    assert "FB_EMAIL" in r["todo"]
    print("ok  /login advertises the streamed window for X + IG, not for FB")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"\nAll {n} pool-link tests passed.")
