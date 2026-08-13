"""
Offline tests for store_accounts.py — no network, no accounts, no budget.
Run as a script (the project's convention), not under pytest:

    python3 test_accounts.py

Exits non-zero on the first failed assertion.
"""

import os
import tempfile
import time

import pyotp

import store_accounts as sa
from store_accounts import AccountStore, SecretError

KEY = "test-secret-key-please-change"


def _fresh():
    path = os.path.join(tempfile.mkdtemp(), "pool.db")
    return AccountStore(path, secret_key=KEY).open(), path


def test_open_idempotent():
    st, path = _fresh()
    st.close()
    # Re-opening the same file must not error or wipe (additive migration).
    st2 = AccountStore(path, secret_key=KEY).open()
    assert st2.list() == []
    st2.close()
    print("ok  open/migrate idempotent")


def test_add_and_metadata():
    st, _ = _fresh()
    aid = st.add("fb", "fb_main", "main@example.com",
                 password="pw", totp_secret="JBSWY3DPEHPK3PXP",
                 backup_codes=["11112222", "33334444"], proxy_id="proxy-1")
    a = st.get(aid)
    assert a.platform == "fb" and a.label == "fb_main"
    assert a.status == sa.BACKUP           # enters as backup
    assert a.has_totp is True
    assert a.backup_codes_left == 2
    assert a.proxy_id == "proxy-1"
    # The metadata view must never carry a decrypted secret.
    assert not hasattr(a, "password") and not hasattr(a, "totp_secret")
    st.close()
    print("ok  add + safe metadata view")


def test_encryption_required_and_roundtrip():
    # No key => storing a non-empty secret is refused (no silent plaintext).
    path = os.path.join(tempfile.mkdtemp(), "pool.db")
    nokey = AccountStore(path, secret_key="").open()
    try:
        nokey.add("x", "nope", "u", password="secret")
        assert False, "expected SecretError with no key"
    except SecretError:
        pass
    # An account with NO secrets is fine without a key.
    nokey.add("x", "plain", "u")
    nokey.close()

    # With a key, password round-trips exactly.
    st, _ = _fresh()
    aid = st.add("x", "acct", "u", password="hunter2")
    assert st.get_password(aid) == "hunter2"
    st.close()
    print("ok  encryption required + round-trip")


def test_wrong_key_cannot_read():
    st, path = _fresh()
    aid = st.add("ig", "ig1", "u", password="pw")
    st.close()
    other = AccountStore(path, secret_key="a-different-key").open()
    try:
        other.get_password(aid)
        assert False, "a different key must not decrypt"
    except Exception:
        pass
    other.close()
    print("ok  wrong key cannot decrypt")


def test_update_partial():
    st, _ = _fresh()
    aid = st.add("fb", "f", "u", password="pw", proxy_id="p1")
    st.update(aid, proxy_id="p2")          # rotate proxy only
    assert st.get(aid).proxy_id == "p2"
    assert st.get_password(aid) == "pw"    # password untouched
    st.update(aid, password="new")
    assert st.get_password(aid) == "new"
    st.close()
    print("ok  partial update leaves other fields alone")


def test_one_active_invariant():
    st, _ = _fresh()
    a1 = st.add("fb", "a1", "u")
    a2 = st.add("fb", "a2", "u")
    st.promote(a1)
    assert st.active("fb").account_id == a1
    st.promote(a2)                         # promoting a2 must demote a1
    act = st.active("fb")
    assert act.account_id == a2
    assert st.get(a1).status == sa.BACKUP
    # exactly one active
    actives = [a for a in st.list("fb") if a.status == sa.ACTIVE]
    assert len(actives) == 1
    st.close()
    print("ok  one-active invariant holds across promotes")


def test_failover_rotates_and_promotes():
    st, _ = _fresh()
    a1 = st.add("fb", "a1", "u", proxy_id="ip1")
    a2 = st.add("fb", "a2", "u", proxy_id="ip2")
    st.promote(a1)
    assert st.backups_left("fb") == 1
    new = st.failover("fb", rotate_proxy="ip-fresh", reason="test ban")
    assert new is not None and new.account_id == a2
    assert new.status == sa.ACTIVE
    assert new.proxy_id == "ip-fresh"      # IP rotated on failover
    assert st.get(a1).status == sa.QUARANTINED
    # Pool now has no backups: another failover finds nobody.
    assert st.failover("fb") is None
    st.close()
    print("ok  failover quarantines, promotes next, rotates IP")


def test_record_success():
    st, _ = _fresh()
    aid = st.add("x", "a", "u")
    st.set_status(aid, sa.ACTIVE, health="warming")
    st.record_success(aid)
    a = st.get(aid)
    assert a.last_success_at is not None
    assert a.health == ""
    st.close()
    print("ok  record_success stamps time, clears health")


def test_totp_matches_pyotp():
    st, _ = _fresh()
    secret = pyotp.random_base32()
    aid = st.add("fb", "t", "u", totp_secret=secret)
    # Compare against pyotp directly; retry once across a 30s boundary.
    for _ in range(3):
        got = st.totp_now(aid)
        want = pyotp.TOTP(secret).now()
        if got == want:
            break
        time.sleep(0.5)
    assert got == want, (got, want)
    # An account with no TOTP returns None.
    aid2 = st.add("fb", "t2", "u")
    assert st.totp_now(aid2) is None
    st.close()
    print("ok  TOTP matches pyotp, None when unset")


def test_totp_normalizes_pasted_secret():
    st, _ = _fresh()
    base = pyotp.random_base32()
    spaced = " ".join(base[i:i+4] for i in range(0, len(base), 4)).lower()
    aid = st.add("fb", "n", "u", totp_secret=spaced)     # pasted with spaces + lowercase
    for _ in range(3):
        got, want = st.totp_now(aid), pyotp.TOTP(base).now()
        if got == want:
            break
        time.sleep(0.5)
    assert got == want
    st.close()
    print("ok  TOTP normalizes a space/case-mangled paste")


def test_backup_codes_single_use():
    st, _ = _fresh()
    aid = st.add("fb", "b", "u", backup_codes=["AAA", "BBB", "CCC"])
    assert st.backup_codes_remaining(aid) == 3
    seen = set()
    for expect_left in (2, 1, 0):
        code = st.take_backup_code(aid)
        assert code is not None and code not in seen
        seen.add(code)
        assert st.backup_codes_remaining(aid) == expect_left
    assert st.take_backup_code(aid) is None      # exhausted
    assert seen == {"AAA", "BBB", "CCC"}
    # Refresh replaces the set.
    st.set_backup_codes(aid, ["ZZZ"])
    assert st.backup_codes_remaining(aid) == 1
    assert st.take_backup_code(aid) == "ZZZ"
    st.close()
    print("ok  backup codes are single-use, refreshable")


def test_remove():
    st, _ = _fresh()
    aid = st.add("x", "gone", "u")
    st.remove(aid)
    try:
        st.get(aid)
        assert False, "expected KeyError after remove"
    except KeyError:
        pass
    st.close()
    print("ok  remove deletes the row")


def test_validation():
    st, _ = _fresh()
    try:
        st.add("tiktok", "x", "u")
        assert False
    except ValueError:
        pass
    aid = st.add("x", "v", "u")
    try:
        st.set_status(aid, "banana")
        assert False
    except ValueError:
        pass
    st.close()
    print("ok  rejects bad platform / status")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} account-store tests passed.")


if __name__ == "__main__":
    main()
