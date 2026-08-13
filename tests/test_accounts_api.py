"""Offline tests for accounts_api.handle — no server, no network."""
import os
import tempfile

os.environ["ACCOUNTS_DB"] = os.path.join(tempfile.mkdtemp(), "pool.db")
os.environ["ACCOUNTS_SECRET_KEY"] = "test-key"

import accounts_api as A


def _add(**kw):
    body = {"platform": "fb", "label": "l", "login": "u@x.com"}
    body.update(kw)
    return A.handle("POST", "/add", body, {})


def test_add_list_summary():
    r = _add(label="fb_a", totp_secret="JBSWY3DPEHPK3PXP", backup_codes="AAA\nBBB")
    assert r["ok"] and r["account_id"] > 0
    lst = A.handle("GET", "", {}, {})
    assert lst["cipher_ready"] is True
    a = [x for x in lst["accounts"] if x["label"] == "fb_a"][0]
    assert a["status"] == "backup" and a["has_totp"] and a["backup_codes_left"] == 2
    assert lst["platforms"]["fb"]["backups"] == 1
    assert lst["platforms"]["fb"]["low"] is True         # <=1 backup
    print("ok  add + list + pool summary")


def test_validation():
    assert "error" in A.handle("POST", "/add", {"platform": "nope", "label": "x", "login": "u"}, {})
    assert "error" in A.handle("POST", "/add", {"platform": "x", "label": "", "login": "u"}, {})
    assert "error" in A.handle("POST", "/add", {"platform": "x", "label": "x", "login": ""}, {})
    assert "error" in A.handle("POST", "/status", {"account_id": 1, "status": "banana"}, {})
    print("ok  rejects bad platform / label / login / status")


def test_promote_and_failover():
    a1 = _add(platform="x", label="x1", proxy_id="ip1")["account_id"]
    a2 = _add(platform="x", label="x2", proxy_id="ip2")["account_id"]
    assert A.handle("POST", "/promote", {"account_id": a1}, {})["ok"]
    s = A.handle("GET", "", {}, {})["platforms"]["x"]
    assert s["active"] == "x1" and s["backups"] == 1
    r = A.handle("POST", "/failover", {"platform": "x", "rotate_proxy": "ip-new"}, {})
    assert r["promoted"] == "x2"
    lst = A.handle("GET", "", {}, {})["accounts"]
    x2 = [x for x in lst if x["label"] == "x2"][0]
    assert x2["status"] == "active" and x2["proxy_id"] == "ip-new"
    print("ok  promote + failover (with IP rotation) via API")


def test_update_remove_backupcodes_totp():
    aid = _add(platform="ig", label="ig1", password="pw")["account_id"]
    assert A.handle("POST", "/update", {"account_id": aid, "proxy_id": "p9"}, {})["ok"]
    got = A.handle("GET", "", {}, {})
    row = [x for x in got["accounts"] if x["label"] == "ig1"][0]
    assert row["proxy_id"] == "p9"
    r = A.handle("POST", "/backup_codes", {"account_id": aid, "codes": "Z1, Z2, Z3"}, {})
    assert r["remaining"] == 3
    tot = _add(platform="ig", label="ig_totp", totp_secret="JBSWY3DPEHPK3PXP")["account_id"]
    code = A.handle("GET", "/totp", {}, {"account_id": tot})["code"]
    assert code and len(code) == 6 and code.isdigit()
    assert A.handle("POST", "/remove", {"account_id": aid}, {})["ok"]
    print("ok  update / backup codes / totp preview / remove")


def test_login_is_stubbed():
    aid = _add(platform="fb", label="fb_login")["account_id"]
    r = A.handle("POST", "/login", {"account_id": aid}, {})
    assert r["ok"] is False and "todo" in r
    print("ok  login endpoint honestly reports 'next step'")


def main():
    for name in sorted(k for k in globals() if k.startswith("test_")):
        globals()[name]()
    print("\nAll accounts_api tests passed.")


if __name__ == "__main__":
    main()
