"""
test_platform_urls.py — naming a post link on every platform the sheet uses.

Phase 2, step 1 (REPORT_TOOL_PLAN.md Part 8). 40% of the links on a day tab of
the live sheet are Facebook, Instagram or YouTube. Before any of them can be
fetched they have to be NAMED — a link we can name is one we can show as
`pending` and count; a link we cannot name vanishes silently, which is the
failure this module exists to prevent.

Every case below is a real URL shape taken from the live sheet
(1xTDykt5z6x9oEs0_46g353CM75oQ5zgZNtAiXstBfP0), not an invented one.

Offline. Run: python3 tests/test_platform_urls.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import links as L

FAILED = []


def _ok(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


CASES = [
    # (url, expected platform, expected reference)
    ("https://x.com/anujakapurindia/status/2073389948066214090", "x", "2073389948066214090"),
    ("https://x.com/i/status/2073396756172472359", "x", "2073396756172472359"),
    ("https://twitter.com/User/status/2073389948066214090?s=20", "x", "2073389948066214090"),
    ("https://www.instagram.com/p/Dc8SV1DmK2V/", "instagram", "Dc8SV1DmK2V"),
    ("https://www.instagram.com/reel/Dc8iUCeR4Ri/?stkn=MWxueTFtdnNkaTNpdg==",
     "instagram", "Dc8iUCeR4Ri"),
    # the browser's own share sheet puts the author in the path
    ("https://www.instagram.com/political_chashma/reel/Dc00Zc6tgq4",
     "instagram", "Dc00Zc6tgq4"),
    ("https://www.facebook.com/reel/27814387954926960/", "facebook", "27814387954926960"),
    ("https://www.facebook.com/61592759962787/posts/pfbid02v8eYbcGGmZ3Aie/",
     "facebook", "61592759962787/pfbid02v8eYbcGGmZ3Aie"),
    ("https://youtube.com/shorts/3V405QJjcLw?si=MD3OJwrzRRcHQAHn", "youtube", "3V405QJjcLw"),
]


def test_parse(ok):
    print("== every link shape in the live sheet gets a name ==")
    for url, plat, ref in CASES:
        got = L.parse_post_url(url)
        ok(got == (plat, ref), f"{plat:<10} {url[:58]:<58} -> {got}")


def test_boundaries(ok):
    print("== and nothing else does ==")
    ok(L.parse_post_url("https://example.com/p/abcdef") is None,
       "a non-platform host is not a post")
    ok(L.parse_post_url("https://notinstagram.com/p/Dc8SV1DmK2V") is None,
       "a host that merely ENDS in the right name is not that platform")
    ok(L.parse_post_url("") is None and L.parse_post_url(None) is None,
       "an empty cell is not a post")
    ok(L.parse_post_url("https://www.instagram.com/political_chashma/") is None,
       "a profile link is not a post — it has no shortcode")
    # /p/ must not be read as a username
    ok(L.parse_post_url("https://www.instagram.com/p/Dc8SV1DmK2V")[1] == "Dc8SV1DmK2V",
       "the optional author segment never swallows the /p/ keyword")


def test_x_unchanged(ok):
    print("== X still parses exactly as it did ==")
    for url, _p, ref in CASES[:3]:
        ok(str(L.parse_status_url(url)) == ref,
           f"parse_status_url is untouched for {url[:52]}")
    ok(L.parse_post_url("https://spacex.com/updates/status/12345678901") is None,
       "the left-boundary guard still rejects spacex.com/…/status/… "
       "(it used to burn a fetch per cycle)")


def test_short_links(ok):
    print("== Facebook short links are followed, never guessed ==")
    for u in ("https://www.facebook.com/share/p/1BPGprbNgR",
              "https://fb.watch/aB3dEf/"):
        ok(L.find_short_links(u) and L.parse_post_url(u) is None,
           f"{u[:48]} is a link to FOLLOW, not a post id — a /share/ code "
           f"never joins to a post, so minting a row from it would look "
           f"tracked and be silently dead")


def test_platform_sets(ok):
    print("== YouTube is named but not watched ==")
    ok(L.parse_post_url("https://youtube.com/shorts/3V405QJjcLw")[0] == "youtube",
       "a YouTube link is recognised…")
    ok("youtube" not in L.WATCHED_PLATFORMS,
       "…and deliberately absent from WATCHED_PLATFORMS: we cannot collect it, "
       "and a link filed under a platform we do not collect is honest, while "
       "one guessed as X is not")
    ok(L.WATCHED_PLATFORMS == ("x", "instagram", "facebook"),
       f"the watched set is exactly the three we can fetch: {L.WATCHED_PLATFORMS}")


def test_multiple_in_a_cell(ok):
    print("== a cell holding several links ==")
    cell = ("https://x.com/a/status/2073389948066214090 "
            "https://www.instagram.com/reel/Dc8iUCeR4Ri/ "
            "https://x.com/a/status/2073389948066214090")
    got = L.find_post_links(cell)
    ok(len(got) == 2, f"duplicates within one cell collapse ({len(got)} of 3)")
    ok([g[0] for g in got] == ["x", "instagram"],
       f"…in first-seen order, across platforms: {[g[0] for g in got]}")


def run(tmp=None, ok=_ok):
    test_parse(ok)
    test_boundaries(ok)
    test_x_unchanged(ok)
    test_short_links(ok)
    test_platform_sets(ok)
    test_multiple_in_a_cell(ok)


def main():
    print("naming a post link on every platform\n")
    run(None, _ok)
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
