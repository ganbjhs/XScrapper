"""
Offline tests for the Facebook media path — the store, the URL guard, the
delivery rewrite, and the DOM-derived fields the extractor now returns.

    python3 tests/test_fb_media.py

Runs on Python 3.10 (deliberately: it imports neither `config` nor playwright),
so it can be run anywhere, including the remote session that wrote it. Nothing
here touches the network or Facebook.

Why this file exists: every failure it checks for has already happened once.
Media links expired in the database and the feed went blank; an emoji sprite
was stored as a photograph; posts arrived with no time and no counts because
the DOM path stored neither.
"""

import importlib.util
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fb_media


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


efb = _load("engine_fb")

PASS = []
FAIL = []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    if got != want:
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def section(name):
    print(f"\n-- {name}")


# A real (expired) URL from the collected corpus, and its shapes.
PHOTO = ("https://scontent-bom2-4.xx.fbcdn.net/v/t15.5256-10/774508831_n.jpg"
         "?stp=dst-jpg_tt6&cstp=mx1080x1920&ctp=s960x960&_nc_cat=107"
         "&oh=00_AQHVrReQ&oe=6A842163")
EMOJI = "https://static.xx.fbcdn.net/images/emoji.php/v9/t72/1/16/1f538.png"
AVATAR = ("https://scontent-bom2-4.xx.fbcdn.net/v/t1.6435-1/a.jpg"
          "?stp=cp0_dst-jpg_p32x32&_nc_cat=1")
ORIGINAL = "https://scontent-bom2-4.xx.fbcdn.net/v/t39.30808-6/o_n.jpg?_nc_cat=100"


def test_is_post_image():
    section("post media vs page chrome")
    check("post photo kept", efb._is_post_image(PHOTO), True)
    check("full-size original kept", efb._is_post_image(ORIGINAL), True)
    check("emoji sprite rejected", efb._is_post_image(EMOJI), False)
    check("avatar rejected", efb._is_post_image(AVATAR), False)
    check("foreign host rejected", efb._is_post_image("https://pbs.twimg.com/x.jpg"), False)
    check("empty rejected", efb._is_post_image(""), False)


def test_clean_media():
    section("media normalization")
    got = efb._clean_media([PHOTO, EMOJI, AVATAR, PHOTO],
                           "https://www.facebook.com/p/posts/1")
    check("junk dropped and deduped", len(got), 1)
    check("typed photo", got[0]["type"], "photo")
    reel = efb._clean_media([PHOTO], "https://www.facebook.com/reel/17992786/")
    check("reel is video, not a still", reel[0]["type"], "video")
    check("avatar excluded by identity", efb._clean_media([PHOTO], "u", PHOTO), [])
    check("dict form survives",
          len(efb._clean_media([{"type": "video",
                                 "url": "https://video.xx.fbcdn.net/v.mp4",
                                 "thumb": PHOTO}], "u")), 1)
    check("cap at six", len(efb._clean_media(
        [PHOTO + f"&n={i}" for i in range(9)], "u")), 6)


def test_counts_and_time():
    section("what the DOM shows, as numbers")
    check("aria label", efb._num("1.2K reactions; see who reacted"), 1200)
    check("comment text", efb._num("83 comments"), 83)
    check("thousands separator", efb._num("3,456"), 3456)
    check("millions", efb._num("2.1M"), 2100000)
    check("zero is not none", efb._num("0"), 0)
    check("unreadable is none", efb._num("Shared with Public"), None)

    now = 1_788_000_000_000
    check("relative hours", efb._time_ms("5h", None, now), now - 5 * 3_600_000)
    check("relative days", efb._time_ms("3 days", None, now), now - 3 * 86_400_000)
    check("exact utime", efb._time_ms(None, 1_787_000_000, now), 1_787_000_000_000)
    check("unreadable stays none", efb._time_ms("Shared with Public", None, now), None)
    check("missing stays none", efb._time_ms(None, None, now), None)
    # An absolute date with no year that reads as future belongs to last year,
    # and nothing may ever be dated after collection (it would poison lag_ms).
    check("never in the future",
          efb._time_ms("30 December at 23:59", None, now) <= now, True)


def test_store(tmp):
    section("the byte store")
    st = fb_media.MediaStore(tmp)
    a = st.put(b"jpeg-bytes-a", "image/jpeg", PHOTO)
    b = st.put(b"jpeg-bytes-a", "image/jpeg", PHOTO + "&again=1")
    c = st.put(b"png-bytes-c", "image/png", None)
    check("stored", bool(a), True)
    check("same bytes, same name", a, b)
    check("different bytes, different name", a != c, True)
    check("html refused", st.put(b"<html>", "text/html", None), None)
    check("oversize refused",
          st.put(b"x" * (fb_media.MAX_BYTES + 1), "image/jpeg", None), None)

    section("the media route's URL guard")
    rel = a[len(fb_media.URL_PREFIX):]
    check("mints what it resolves", st.resolve(rel) is not None, True)
    for bad in ("../../etc/passwd",
                "aa/" + "f" * 64 + ".jpg",          # not stored
                "ab/" + rel.split("/")[-1],          # shard does not match hash
                "aa/short.jpg",
                rel.replace(".jpg", ".php"),
                rel + "/..",
                ""):
        check(f"refuses {bad[:24]!r}", st.resolve(bad), None)

    section("eviction (implemented, unset by default)")
    check("no cap set = no work", st.sweep(0), 0)
    check("cap 0 from env", fb_media.cap_bytes(), 0)
    before = st.total_bytes()
    check("holds bytes", before > 0, True)
    check("cap evicts oldest first", st.sweep(1), 2)
    check("nothing left", st.total_bytes(), 0)


def test_absolutize():
    section("delivery: what leaves the building")
    stored = [{"type": "photo", "url": "/media/fb/aa/deadbeef.jpg",
               "thumb": "/media/fb/aa/deadbeef.jpg", "src": PHOTO}]
    out = fb_media.absolutize(stored, base="https://scraper.example.in/")
    check("absolute for the receiver", out[0]["url"],
          "https://scraper.example.in/media/fb/aa/deadbeef.jpg")
    check("thumb too", out[0]["thumb"],
          "https://scraper.example.in/media/fb/aa/deadbeef.jpg")
    check("input not mutated", stored[0]["url"], "/media/fb/aa/deadbeef.jpg")
    # No base configured is a misconfiguration, not a mode: fall back to the
    # original link (expiring, but fetchable) rather than send a bare path.
    check("no base falls back to src",
          fb_media.absolutize(stored, base="")[0]["url"], PHOTO)
    x = [{"type": "photo", "url": "https://pbs.twimg.com/x.jpg"}]
    check("other platforms untouched",
          fb_media.absolutize(x, base="https://s.example")[0]["url"],
          "https://pbs.twimg.com/x.jpg")
    check("garbage tolerated", fb_media.absolutize(["nope", None]), [])


def test_refresh(tmp):
    """A post seen twice must LEARN, not freeze.

    Dedup keeps a post to one row; it was never meant to keep that row
    ignorant. 160 posts sat with "posted --" and four dashes because the second
    sighting was refused outright."""
    section("a second sighting of a post already held")
    store_fb = _load("store_fb")
    st = store_fb.Store(pathlib.Path(tmp) / "refresh.db")
    st.open()

    FBCDN = "https://scontent-x.xx.fbcdn.net/v/a.jpg?ctp=s960x960&oe=6A842163"
    OURS = "/media/fb/aa/" + "b" * 64 + ".jpg"
    poor = {"post_id": "p:1", "page": "p",
            "url": "https://www.facebook.com/p/posts/1",
            "created_ms": None, "author_name": None, "author_avatar": None,
            "text": "a caption cut at... See more",
            "like_count": None, "comment_count": None, "share_count": None,
            "media": [{"type": "photo", "url": FBCDN, "thumb": FBCDN}],
            "project_id": 9}
    rich = dict(poor, created_ms=1786637743000, author_name="Page Name",
                author_avatar="https://scontent-x.xx.fbcdn.net/av.jpg?p32x32",
                text="a caption cut at nothing, whole and untruncated",
                like_count=1300, comment_count=256, share_count=24,
                media=[{"type": "photo", "url": OURS, "thumb": OURS,
                        "src": FBCDN}])

    check("first sighting inserts", st.upsert(poor), True)
    check("second sighting does not duplicate", st.upsert(rich), False)
    row = dict(st.db.execute(
        "SELECT * FROM posts WHERE post_id='p:1'").fetchone())
    check("time filled", row["created_ms"], 1786637743000)
    check("counts filled",
          (row["like_count"], row["comment_count"], row["share_count"]),
          (1300, 256, 24))
    check("author filled", row["author_name"], "Page Name")
    check("truncated text replaced", "untruncated" in row["text"], True)
    check("expiring media replaced by ours", "media/fb" in row["media_json"], True)

    # Engagement moves and must follow; a fact already recorded must not be
    # overwritten by a later, poorer sighting.
    st.upsert(dict(rich, like_count=1450, comment_count=260,
                   created_ms=1, author_name="WRONG"))
    row = dict(st.db.execute(
        "SELECT * FROM posts WHERE post_id='p:1'").fetchone())
    check("counts follow engagement",
          (row["like_count"], row["comment_count"]), (1450, 260))
    check("recorded time not overwritten", row["created_ms"], 1786637743000)
    check("recorded author not overwritten", row["author_name"], "Page Name")

    st.upsert(dict(rich, media=[{"type": "photo", "url": FBCDN, "thumb": FBCDN}]))
    row = dict(st.db.execute(
        "SELECT * FROM posts WHERE post_id='p:1'").fetchone())
    check("never downgrades to an expiring link",
          "media/fb" in row["media_json"], True)
    check("still exactly one row",
          st.db.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"], 1)


def main():
    tmp = tempfile.mkdtemp(prefix="fbmedia-test-")
    test_is_post_image()
    test_clean_media()
    test_counts_and_time()
    test_store(tmp)
    test_absolutize()
    test_refresh(tmp)
    print("\n" + "=" * 62)
    if FAIL:
        print(f"FAILED: {len(FAIL)} of {len(PASS) + len(FAIL)}")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print(f"All {len(PASS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
