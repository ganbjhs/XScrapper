"""
store_fb.py — the Facebook results store (fb_results.db).

The Facebook twin of store_ig: posts deduped by their Facebook id, sources with
a per-source watermark, and to_feed() mapping a stored row to the SAME post
shape the dashboard and Watch-Tower already use for X and Instagram. Sources
are project-scoped from the start (unlike the older IG sources), so a project's
feed and delivery see only its Facebook pages.

Watermark by POSTED TIME, not by id: Facebook post ids are not time-monotonic
the way X snowflakes are, so a poll walks newest-first and stops once it reaches
a post older than the newest one we already had — with id dedup as the backstop.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  post_id       TEXT PRIMARY KEY,           -- Facebook story/post id (stable)
  page          TEXT NOT NULL,              -- the page/source handle
  url           TEXT,
  created_ms    INTEGER,                    -- when it was POSTED (best-effort)
  collected_ms  INTEGER NOT NULL,           -- when we saved it
  author_name   TEXT,
  text          TEXT,
  like_count    INTEGER,
  comment_count INTEGER,
  share_count   INTEGER,
  media_json    TEXT,                       -- [{type,url,thumb}] — same as X/IG
  author_avatar TEXT,                        -- the page's profile picture URL
  sig           TEXT,                        -- content signature (2nd dedup key)
  source_label  TEXT,
  project_id    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fb_created ON posts(created_ms);
CREATE INDEX IF NOT EXISTS ix_fb_page    ON posts(page);
CREATE INDEX IF NOT EXISTS ix_fb_project ON posts(project_id);

CREATE TABLE IF NOT EXISTS sources (
  label          TEXT PRIMARY KEY,          -- the page handle (e.g. 'narendramodi')
  project_id     INTEGER,
  enabled        INTEGER NOT NULL DEFAULT 1,
  watermark_ms   INTEGER,                   -- newest posted-time seen; stop here
  last_run       INTEGER,
  interval_s     INTEGER,                   -- per-page check cadence; NULL = default
  created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,                  -- e.g. 'fb_mode', 'fb_paused'
  value  TEXT
);

CREATE TABLE IF NOT EXISTS page_profiles (
  handle        TEXT PRIMARY KEY,           -- lowercase page handle
  avatar_url    TEXT,                       -- the page's profile picture URL
  display_name  TEXT,
  updated_ms    INTEGER NOT NULL            -- when the avatar was last confirmed
);
"""

# Columns added after the first release. Applied on open() so an existing
# fb_results.db upgrades in place instead of erroring on a missing column —
# same additive-migration rule the X store follows.
_MIGRATIONS = [
    ("sources", "interval_s", "ALTER TABLE sources ADD COLUMN interval_s INTEGER"),
    ("posts", "author_avatar", "ALTER TABLE posts ADD COLUMN author_avatar TEXT"),
    ("posts", "sig", "ALTER TABLE posts ADD COLUMN sig TEXT"),
]


def _signature(page, text, created_ms=None):
    """
    A content signature that is the SAME for one real post no matter which path
    (GraphQL / DOM / mbasic) or id scheme found it — so a post cannot appear
    twice just because Facebook handed it to us with a different id. Built from
    the page + POSTED DAY + (normalized) caption; None for captionless posts,
    which fall back to id-only dedup.

    The posted-day component matters: a page that reuses a caption ("Breaking:",
    "Good morning") on different days makes genuinely different posts — without
    the day they would collide on signature and the second would be dropped as a
    false duplicate. Same page + same day + same caption is a real duplicate.
    """
    import hashlib
    norm = " ".join(str(text or "").split())[:160].lower()
    if len(norm) < 8:
        return None
    day = int(created_ms // 86_400_000) if created_ms else "na"
    return hashlib.sha1(
        f"{str(page).lower()}\n{day}\n{norm}".encode("utf-8", "ignore")).hexdigest()


@dataclass
class FBSource:
    label: str
    project_id: int = 0
    enabled: bool = True


class Store:
    def __init__(self, path="fb_results.db"):
        self.path = Path(path)
        self.db = None

    def open(self):
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Additive migrations for databases created before a column existed.
        for table, col, ddl in _MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(ddl)
        # Index on the signature column — created after the migration so it
        # exists whether the DB is new or upgraded.
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_fb_sig ON posts(sig)")
        self.db.commit()
        return self

    def close(self):
        if self.db:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    # ---- sources ----

    def add_source(self, label, project_id=0, enabled=True):
        # Canonicalize to lowercase — FB handles are case-insensitive, and this
        # keeps source labels in step with the lowercased page on each post.
        label = str(label).lower()
        now = int(time.time())
        # On re-add, update the project but KEEP the existing enabled flag — a
        # page deliberately paused must not silently resume when it is re-added
        # (or auto-registered from the favorites feed).
        self.db.execute(
            "INSERT INTO sources(label, project_id, enabled, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(label) DO UPDATE SET "
            "  project_id = excluded.project_id",
            (label, int(project_id), int(bool(enabled)), now))
        self.db.commit()

    def remove_source(self, label):
        self.db.execute("DELETE FROM sources WHERE label = ?", (str(label).lower(),))
        self.db.commit()

    def set_interval(self, label, seconds):
        """Per-page check cadence in seconds; None clears it (use the default)."""
        self.db.execute("UPDATE sources SET interval_s = ? WHERE label = ?",
                        (int(seconds) if seconds else None, str(label).lower()))
        self.db.commit()

    def set_enabled(self, label, enabled):
        """Pause (0) or resume (1) a page without losing what it collected."""
        self.db.execute("UPDATE sources SET enabled = ? WHERE label = ?",
                        (int(bool(enabled)), str(label).lower()))
        self.db.commit()

    def sources(self, project_id=None, enabled_only=False):
        where, params = [], []
        if project_id is not None:
            where.append("project_id = ?"); params.append(int(project_id))
        if enabled_only:
            where.append("enabled = 1")
        rows = self.db.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM posts p "
            "             WHERE LOWER(p.page) = LOWER(s.label)) AS posts "
            "FROM sources s "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "ORDER BY s.label", params).fetchall()
        return [dict(r) for r in rows]

    def watermark(self, label):
        row = self.db.execute(
            "SELECT watermark_ms FROM sources WHERE label = ?", (label,)).fetchone()
        return row["watermark_ms"] if row else None

    def set_watermark(self, label, ms):
        self.db.execute(
            "UPDATE sources SET watermark_ms = ?, last_run = ? WHERE label = ?",
            (int(ms), int(time.time()), label))
        self.db.commit()

    # ---- settings (the dashboard's control surface) ----
    #
    # Operational switches live HERE, not only in .env — so the dashboard can
    # show and change them, and the service loop re-reads them every cycle
    # (RULEBOOK §6: no SSH needed to steer collection; a setting's empty value
    # means "fall back to the environment default").

    def setting(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] not in (None, "") else default

    def set_setting(self, key, value):
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, "" if value is None else str(value)))
        self.db.commit()

    def settings_all(self):
        return {r["key"]: r["value"]
                for r in self.db.execute("SELECT key, value FROM settings")}

    # ---- page profiles (avatar cache) ----
    #
    # The profile picture of a page changes rarely, so it is fetched ONCE and
    # cached here — the collector checks this table before ever visiting a
    # page's profile just for its picture. Rows are refreshed for free whenever
    # a collected post happens to carry the avatar URL anyway.

    def profile(self, handle):
        """The cached profile row for a handle, or None."""
        row = self.db.execute(
            "SELECT * FROM page_profiles WHERE handle = ?",
            (str(handle).lower(),)).fetchone()
        return dict(row) if row else None

    def profile_avatar(self, handle):
        """Just the cached avatar URL (None when never captured)."""
        p = self.profile(handle)
        return p["avatar_url"] if p else None

    def set_profile(self, handle, avatar_url=None, display_name=None):
        """
        Upsert the cached profile. A None field never clobbers a stored value —
        so a run that only learned the display name can't erase the avatar.
        """
        self.db.execute(
            "INSERT INTO page_profiles(handle, avatar_url, display_name, updated_ms) "
            "VALUES(?,?,?,?) ON CONFLICT(handle) DO UPDATE SET "
            "  avatar_url   = COALESCE(excluded.avatar_url, page_profiles.avatar_url), "
            "  display_name = COALESCE(excluded.display_name, page_profiles.display_name), "
            "  updated_ms   = excluded.updated_ms",
            (str(handle).lower(), avatar_url, display_name, int(time.time() * 1000)))
        self.db.commit()

    def profiles(self):
        """{handle: avatar_url} for every cached page (used for backfill)."""
        return {r["handle"]: r["avatar_url"] for r in self.db.execute(
            "SELECT handle, avatar_url FROM page_profiles "
            "WHERE avatar_url IS NOT NULL")}

    # ---- posts ----

    def upsert(self, post: dict) -> bool:
        """
        Insert one post; returns True if it was new. Dedup on TWO keys: the
        post_id (exact), and a content signature (so the same caption on the
        same page can't slip in twice under a different id scheme).
        """
        sig = _signature(post.get("page"), post.get("text"), post.get("created_ms"))
        if sig:
            exists = self.db.execute(
                "SELECT 1 FROM posts WHERE post_id = ? OR sig = ?",
                (post["post_id"], sig)).fetchone()
        else:
            exists = self.db.execute(
                "SELECT 1 FROM posts WHERE post_id = ?", (post["post_id"],)).fetchone()
        if exists:
            return False
        self.db.execute(
            "INSERT INTO posts(post_id, page, url, created_ms, collected_ms, "
            " author_name, text, like_count, comment_count, share_count, "
            " media_json, author_avatar, sig, source_label, project_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post["post_id"], post.get("page"), post.get("url"),
             post.get("created_ms"), int(time.time() * 1000),
             post.get("author_name"), post.get("text"),
             post.get("like_count"), post.get("comment_count"),
             post.get("share_count"),
             json.dumps(post.get("media") or []),
             post.get("author_avatar"), sig,
             post.get("source_label") or post.get("page"),
             post.get("project_id")))
        return True

    def recent(self, project_id=None, limit=50, since_ms=None):
        where, params = [], []
        if project_id is not None:
            where.append("p.project_id = ?"); params.append(int(project_id))
        if since_ms:
            where.append("p.collected_ms >= ?"); params.append(int(since_ms))
        # LEFT JOIN the avatar cache so every post shows its page's profile
        # picture — including posts collected before the avatar was known.
        rows = self.db.execute(
            "SELECT p.*, "
            "  COALESCE(p.author_avatar, pp.avatar_url) AS _avatar_resolved "
            "FROM posts p "
            "LEFT JOIN page_profiles pp ON pp.handle = LOWER(p.page) "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "ORDER BY p.collected_ms DESC, p.post_id DESC LIMIT ?",
            [*params, int(limit)]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["author_avatar"] = d.pop("_avatar_resolved")
            out.append(d)
        return out

    def total(self, project_id=None) -> int:
        if project_id is None:
            return self.db.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        return self.db.execute(
            "SELECT COUNT(*) c FROM posts WHERE project_id = ?",
            (int(project_id),)).fetchone()["c"]


def to_feed(row: dict) -> dict:
    """Map a stored FB row to the shared post shape (same keys as X/IG)."""
    try:
        media = json.loads(row.get("media_json") or "[]")
    except (TypeError, ValueError):
        media = []
    import datetime as _dt
    cms = row.get("created_ms") or 0
    created = (_dt.datetime.utcfromtimestamp(cms / 1000).isoformat() + "Z"
               if cms else None)
    collms = row.get("collected_ms") or 0
    collected = (_dt.datetime.utcfromtimestamp(collms / 1000).isoformat() + "Z"
                 if collms else created)
    return {
        "platform": "facebook",
        "tweet_id": str(row["post_id"]),
        "url": row.get("url"),
        "text": row.get("text") or "",
        "created_at": created,
        "collected_at": collected,
        "author_username": row.get("page"),
        "author_display_name": row.get("author_name") or row.get("page"),
        "author_avatar": row.get("author_avatar"),
        "media": media,
        "like_count": row.get("like_count"),
        "reply_count": row.get("comment_count"),
        "retweet_count": row.get("share_count"),
        "view_count": None,
        "source": row.get("source_label"),
    }
