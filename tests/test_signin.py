"""
Offline tests for signin.py — the one session-import path for all three
platforms. No network, no accounts, no browser.

    python3 tests/test_signin.py

The cookie parser is the part that gets the most abuse: an operator copies
whatever DevTools happened to give them, under time pressure, and a parser that
only accepts one shape means a working session gets rejected as "invalid" and
the operator goes back to the browser login this module exists to replace.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signin  # noqa: E402


def test_parses_a_cookie_header():
    c = signin.parse_cookies(
        "cookie: auth_token=abc123; ct0=deadbeef; kdt=KDTVALUE; lang=en")
    assert c == {"auth_token": "abc123", "ct0": "deadbeef",
                 "kdt": "KDTVALUE", "lang": "en"}, c
    # …and without the header prefix
    assert signin.parse_cookies("a=1; b=2") == {"a": "1", "b": "2"}
    print("ok  parses a `cookie:` request-header line")


def test_parses_devtools_copy_all_as_json():
    blob = """[
      {"name":"sessionid","value":"123%3Aabc%3A6","domain":".instagram.com"},
      {"name":"ds_user_id","value":"123"},
      {"name":"csrftoken","value":"tok"}
    ]"""
    c = signin.parse_cookies(blob)
    assert c["sessionid"] == "123%3Aabc%3A6"
    assert c["ds_user_id"] == "123" and c["csrftoken"] == "tok"
    # a plain {name: value} object works too
    assert signin.parse_cookies('{"c_user":"1","xs":"2"}') == {"c_user": "1", "xs": "2"}
    print("ok  parses DevTools 'Copy all as JSON' and a flat object")


def test_parses_hand_picked_lines_and_a_pasted_table():
    assert signin.parse_cookies("auth_token=abc\nct0=def") == {
        "auth_token": "abc", "ct0": "def"}
    table = "Name\tValue\tDomain\nc_user\t100081\t.facebook.com\nxs\t42%3Aabc\t.facebook.com"
    c = signin.parse_cookies(table)
    assert c == {"c_user": "100081", "xs": "42%3Aabc"}, c
    print("ok  parses hand-picked lines and a pasted cookie table")


def test_values_are_never_url_decoded():
    """ct0, sessionid and xs legitimately contain % and : — decoding them
    silently produces a session the platform will reject."""
    raw = "sessionid=7788%3AtD8XoZ%3A6%3AAYj"
    assert signin.parse_cookies(raw)["sessionid"] == "7788%3AtD8XoZ%3A6%3AAYj"
    print("ok  values reach the platform byte-for-byte")


def test_garbage_is_empty_not_an_exception():
    for junk in ("", "   ", "hello world", None):
        assert signin.parse_cookies(junk) == {}
    print("ok  an unparseable blob is {} — never a crash")


def test_instagram_refuses_the_server_ip():
    """RULEBOOK 6: Instagram never runs from the datacenter IP. A silent
    fallback here costs the account, so both IG paths must REFUSE."""
    o = signin.ig_password("someone", "pw", proxy="")
    assert o.ok is False and o.needs == "proxy"
    assert "residential proxy" in o.detail
    o = signin.ig_cookie("sessionid=123%3Aabc", proxy="")
    assert o.ok is False and o.needs == "proxy"
    print("ok  Instagram refuses to sign in over the server IP")


def test_instagram_rejects_a_non_session_blob_before_any_network():
    o = signin.ig_cookie("not a cookie at all", proxy="http://u:p@gw:1")
    assert o.ok is False and o.needs == "paste"
    assert "sessionid" in o.detail
    print("ok  a blob that is not a sessionid fails before touching Instagram")


def test_x_requires_both_cookies_a_handle_and_a_real_ua():
    o = signin.x_cookie("auth_token=abc", screen_name="alice", user_agent="Mozilla/5.0")
    assert o.ok is False and "ct0" in o.detail

    o = signin.x_cookie("auth_token=a; ct0=b", screen_name="", user_agent="Mozilla/5.0")
    assert o.ok is False and "@handle" in o.detail

    # An empty or '@'-prefixed UA makes twscrape invent a RANDOM one seeded by
    # the username — a fingerprint never associated with these cookies.
    for bad in ("", "@chrome"):
        o = signin.x_cookie("auth_token=a; ct0=b", screen_name="alice", user_agent=bad)
        assert o.ok is False and "user-agent" in o.detail, bad
    print("ok  X refuses without both cookies, a handle, and a real user-agent")


def test_facebook_writes_a_playwright_storage_state():
    root = tempfile.mkdtemp()
    o = signin.fb_cookie("c_user=100081; xs=42%3Aabc; datr=DATRVAL", root=root)
    assert o.ok is True, o.detail
    assert o.identity == "100081"

    import json
    state = json.loads(open(os.path.join(root, "fb_state.json")).read())
    assert set(state) == {"cookies", "origins"}
    by = {c["name"]: c for c in state["cookies"]}
    assert by["xs"]["value"] == "42%3Aabc", "must not be url-decoded"
    assert by["c_user"]["domain"] == ".facebook.com" and by["c_user"]["path"] == "/"
    assert by["datr"]["expires"] > 0
    print("ok  Facebook import writes a valid Playwright storage_state")


def test_facebook_refuses_without_the_required_pair():
    root = tempfile.mkdtemp()
    o = signin.fb_cookie("datr=only", root=root)
    assert o.ok is False and o.needs == "paste"
    assert "c_user" in o.detail and "xs" in o.detail
    assert not os.path.exists(os.path.join(root, "fb_state.json")), \
        "a refused import must not leave a half-written session behind"
    print("ok  Facebook refuses without c_user + xs, writing nothing")


def test_a_proxy_url_is_never_echoed_with_its_password():
    assert signin._redact("http://user:sekret@gw.host.io:8080") == "gw.host.io:8080"
    assert "sekret" not in signin._redact("http://user:sekret@gw.host.io:8080")
    assert signin._redact("") == "the server IP"
    print("ok  a proxy password is never echoed back to the operator")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"\nAll {n} signin tests passed.")
