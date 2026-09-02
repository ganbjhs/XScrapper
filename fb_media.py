"""
fb_media.py — the Facebook media store: bytes we hold, not links we hope in.

Facebook signs every fbcdn URL and writes the expiry INTO the URL as
`oe=<hex epoch>`, about five days out. A stored link is therefore a perishable
good, and we had been treating it as an asset: the Live Feed went blank on
every post older than a week, and both delivery paths — the Watch-Tower webhook
(`webhook.py`) and the Sheets export (`sheets.py`) — ship `media[].url`
verbatim, so receivers were handed links that rot in their hands a few days
after arrival. Nothing on the render side could have fixed that; only holding
the bytes can.

So the collector keeps the bytes. Each image is fetched ONCE, while its
signature is still alive, through the collector's own logged-in browser
context (so it costs no new session and is metered like every other Facebook
byte), stored under the sha256 of its content, and served from our own host at
`/media/fb/<aa>/<hash>.<ext>` — a URL that never expires, needs no session, and
travels downstream unchanged. That last property is the point: Watch-Tower
needs no change at all, because the shape it receives is identical.

Content-addressed on purpose: the same picture posted by four pages is stored
once, and a re-fetch of a post already held is a no-op rather than a duplicate.

Sizing. `FB_MEDIA_CAP_GB` is implemented and deliberately UNSET by default —
the operator's call (2026-09-02): the disk is ample, so no limit runs until one
is wanted, and turning it on later is a variable, not a code change. When it IS
set, `sweep()` evicts oldest-first, and a post whose bytes were evicted falls
back to the embed frame in the dashboard, which is the same fallback the
pre-2026-09-02 posts use.
"""

import hashlib
import os
import sqlite3
import time
from pathlib import Path

# Only these. A content-type outside this set is not stored: the store exists
# to hold pictures, and anything else arriving from a CDN is a surprise, not a
# feature.
EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
}
MAX_BYTES = 12 * 1024 * 1024      # one image; a post's worth is 6 of these
URL_PREFIX = "/media/fb/"


def cap_bytes() -> int:
    """0 means NO limit — the configured default. See the module docstring."""
    try:
        return int(float(os.getenv("FB_MEDIA_CAP_GB", "0")) * 1_000_000_000)
    except ValueError:
        return 0


class MediaStore:
    """Files on disk plus an index. Both are cheap to rebuild from the other:
    the index is the only thing that knows fetch order (for eviction), and the
    files are the only thing that matters to a reader."""

    def __init__(self, root):
        self.root = Path(root)
        self.dir = Path(os.getenv("FB_MEDIA_DIR") or (self.root / "media" / "fb"))
        self.dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the dashboard is a threading HTTP
        # server: the store is opened once per process and the media route can
        # be answered on any worker thread. Writes are single-statement and
        # WAL-journalled, so concurrent readers never block.
        self.db = sqlite3.connect(str(self.root / "fb_media.db"),
                                  isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files("
            "  hash TEXT PRIMARY KEY,"
            "  ext TEXT NOT NULL,"
            "  bytes INTEGER NOT NULL,"
            "  fetched_ms INTEGER NOT NULL,"
            "  src TEXT)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS files_age ON files(fetched_ms)")

    # -- paths -------------------------------------------------------------
    #
    # Sharded by the first two hex characters: 256 directories instead of one
    # with a million entries in it, which is the difference between `ls` and a
    # coffee break.

    def _path(self, h: str, ext: str) -> Path:
        return self.dir / h[:2] / f"{h}.{ext}"

    @staticmethod
    def url_for(h: str, ext: str) -> str:
        return f"{URL_PREFIX}{h[:2]}/{h}.{ext}"

    def resolve(self, rel: str):
        """A '/media/fb/aa/<hash>.<ext>' request -> the file, or None.

        Refuses anything that is not exactly the shape we mint: the name must
        be 64 hex characters and a known extension, and the shard must match
        the hash. That is a whitelist, not a traversal check — '..' cannot
        survive it."""
        parts = [p for p in rel.strip("/").split("/") if p]
        if len(parts) != 2:
            return None
        shard, name = parts
        stem, _, ext = name.rpartition(".")
        if (len(shard) != 2 or len(stem) != 64
                or ext not in set(EXT_BY_TYPE.values())
                or not all(c in "0123456789abcdef" for c in stem + shard)
                or stem[:2] != shard):
            return None
        p = self._path(stem, ext)
        return p if p.is_file() else None

    # -- writing -----------------------------------------------------------

    def put(self, data: bytes, content_type: str, src: str = None):
        """Store one image; return its public path, or None if unstorable.

        Returns the SAME path for identical bytes, without a second write."""
        ext = EXT_BY_TYPE.get((content_type or "").split(";")[0].strip().lower())
        if not ext or not data or len(data) > MAX_BYTES:
            return None
        h = hashlib.sha256(data).hexdigest()
        p = self._path(h, ext)
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(p)            # atomic: a reader never sees half a file
        self.db.execute(
            "INSERT INTO files(hash, ext, bytes, fetched_ms, src) "
            "VALUES(?,?,?,?,?) ON CONFLICT(hash) DO NOTHING",
            (h, ext, len(data), int(time.time() * 1000), src))
        return self.url_for(h, ext)

    def total_bytes(self) -> int:
        r = self.db.execute("SELECT COALESCE(SUM(bytes), 0) t FROM files").fetchone()
        return int(r["t"])

    def sweep(self, cap=None) -> int:
        """Evict oldest-first until under the cap. 0/None = no limit, no work.

        A file whose row is gone is gone: the dashboard falls back to the embed
        frame for that post, exactly as it does for posts collected before this
        store existed. Returns the number of files removed."""
        cap = cap_bytes() if cap is None else cap
        if not cap or cap <= 0:
            return 0
        total = self.total_bytes()
        if total <= cap:
            return 0
        removed = 0
        for r in self.db.execute(
                "SELECT hash, ext, bytes FROM files ORDER BY fetched_ms ASC"):
            if total <= cap:
                break
            try:
                self._path(r["hash"], r["ext"]).unlink(missing_ok=True)
            except OSError:
                continue
            self.db.execute("DELETE FROM files WHERE hash = ?", (r["hash"],))
            total -= int(r["bytes"])
            removed += 1
        return removed


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
#
# The dashboard is same-origin with the media route, so it renders the stored
# path directly. A DELIVERY is not: Watch-Tower's webhook body and the Sheets
# export are read on other machines, where "/media/fb/aa/….jpg" resolves to
# nothing. So outbound copies are absolutized against PUBLIC_BASE_URL.
#
# With no base configured we fall back to `src` — the original Facebook link.
# It expires within the week, which is exactly the problem this module exists
# to solve, so an unset PUBLIC_BASE_URL is a misconfiguration and not a mode:
# it degrades delivery to what it was before, rather than sending a receiver a
# path they cannot fetch.

def public_base() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


def absolutize(media, base=None):
    """Outbound copy of a media list with our own paths made absolute.

    Anything that is not ours (X's CDN, Instagram's) passes through untouched,
    so this is safe to apply to every platform's rows."""
    base = public_base() if base is None else base.rstrip("/")
    out = []
    for m in (media or []):
        if not isinstance(m, dict):
            continue
        m = dict(m)
        for key in ("url", "thumb"):
            v = m.get(key) or ""
            if isinstance(v, str) and v.startswith(URL_PREFIX):
                m[key] = (base + v) if base else (m.get("src") or v)
        out.append(m)
    return out
