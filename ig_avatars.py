"""
ig_avatars.py — Instagram profile pictures we HOLD, not links we hope in.

Instagram hands us a profile-picture URL with every collected post
(store_ig.profiles.avatar_url). Two things are wrong with showing that URL
in a browser: it is signed and expires (`oe=` in the query, days out), and
the CDN refuses hot-linked loads from another origin even while it is
valid — measured 2026-09-29: 23 of 25 sources had a URL, 0 of 4 tested
rendered, expired or not. So every Instagram account in the dashboard sat
behind its initial, and Watch-Tower got the same dead links.

Same cure as Facebook (fb_media.py): fetch the bytes ONCE while the
signature is alive, keep them content-addressed in the media store, and
serve them from our own host at a URL that never expires and needs no
session — `/media/fb/<aa>/<hash>.<ext>` (the prefix is historical; the
store is shared, and renaming it would only make Watch-Tower's links
change). `profiles.avatar_local` remembers our path and `avatar_src` the
CDN path it came from (query string stripped — the signature changes,
the picture does not), so a picture is re-fetched only when the account
actually changes it.

Who fetches: the Instagram collector after each pass (up to a few dozen,
paced), and the dashboard lazily in small batches when it draws a row that
has a URL but no bytes yet — so a freshly deployed server fills in without
waiting for the next post from each account. Plain HTTP, no session, no
proxy: the CDN serves a signed URL to anyone, and this is a picture from a
public profile.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request

TIMEOUT_S = 8
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_LOCK = threading.Lock()
_LAST_LAZY = 0.0
LAZY_EVERY_S = 45        # the dashboard's on-demand batch runs at most this often


def src_key(url: str) -> str:
    """The URL without its signature: same picture, same key."""
    try:
        u = urllib.parse.urlsplit(str(url or ""))
        return f"{u.netloc}{u.path}"
    except ValueError:
        return str(url or "")


def fetch(url: str, timeout: float = TIMEOUT_S):
    """(bytes, content_type) or (None, reason)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(12 * 1024 * 1024 + 1)
            if len(data) > 12 * 1024 * 1024:
                return None, "too large"
            return data, ctype
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def ensure(root, ig_store, media_store, limit: int = 30, log=None) -> dict:
    """
    Cache up to `limit` profile pictures that have a URL but no bytes (or a
    changed URL). Returns counts. Safe to call from anywhere; the store's
    writes are small and serialised by its own lock.
    """
    log = log or (lambda m: None)
    done = failed = 0
    rows = ig_store.profiles_needing_avatar(limit)
    for r in rows:
        url = r["avatar_url"]
        data, ctype = fetch(url)
        if data is None:
            failed += 1
            ig_store.note_avatar_failure(r["user_pk"])
            continue
        rel = media_store.put(data, ctype, src=url)
        if not rel:
            failed += 1
            ig_store.note_avatar_failure(r["user_pk"])
            continue
        ig_store.set_avatar_local(r["user_pk"], rel, src_key(url))
        done += 1
    if done or failed:
        log(f"profile pictures: {done} cached, {failed} failed, {len(rows)} tried")
    return {"cached": done, "failed": failed, "tried": len(rows)}


def ensure_lazy(root, ig_store_or_path, media_store, limit: int = 8):
    """The dashboard's version: at most once per LAZY_EVERY_S per process,
    and in a background thread with its own store connection — a status
    call must never wait on Instagram's CDN. Returns the thread, or None
    when it is too soon."""
    global _LAST_LAZY
    with _LOCK:
        now = time.time()
        if now - _LAST_LAZY < LAZY_EVERY_S:
            return None
        _LAST_LAZY = now
    path = getattr(ig_store_or_path, "path", ig_store_or_path)

    def run():
        try:
            import store_ig
            with store_ig.Store(path) as st:
                ensure(root, st, media_store, limit=limit)
        except Exception:
            pass

    t = threading.Thread(target=run, name="ig-avatars", daemon=True)
    t.start()
    return t
