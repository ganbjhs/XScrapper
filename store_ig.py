"""
store_ig.py — where collected Instagram posts live, and the sources that find
them. The Instagram twin of results.db, kept deliberately small and separate.

Two tables, one job each:

  * posts   — one row per media, deduped by its pk. This IS the extract that
              the API serves and that Watch-Tower pulls. Only fields worth
              keeping (the IG payload's image-candidate ladders are dropped, the
              same call config.toml makes with keep_entry_json = false).
  * sources — what to collect (a followed feed / a user / a hashtag), each with
              a watermark: the newest pk already seen, so a poll stops the
              moment it reaches known ground and normally costs one request.

No analysis is stored here on purpose. This tool extracts and serves; sentiment
and topics are Watch-Tower's job, so duplicating them here would only create two
answers that can disagree. The engagement numbers Instagram already returns
(likes, comments, views) are kept, because they arrive free with the post.
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  pk            INTEGER PRIMARY KEY,      -- media pk (time-monotonic)
  code          TEXT,
  url           TEXT,
  taken_at      INTEGER,                  -- unix seconds, from the post
  username      TEXT,
  user_pk       INTEGER,
  caption       TEXT,
  media_type    INTEGER,
  product_type  TEXT,
  like_count    INTEGER,
  comment_count INTEGER,
  play_count    INTEGER,
  video_url     TEXT,
  thumbnail_url TEXT,
  source_label  TEXT,                     -- which source surfaced it
  collected_at  INTEGER NOT NULL          -- unix seconds, when we saved it
);
CREATE INDEX IF NOT EXISTS ix_posts_taken   ON posts(taken_at);
CREATE INDEX IF NOT EXISTS ix_posts_user    ON posts(username);
CREATE INDEX IF NOT EXISTS ix_posts_source  ON posts(source_label);

CREATE TABLE IF NOT EXISTS sources (
  label        TEXT PRIMARY KEY,          -- short name, unique
  type         TEXT NOT NULL,             -- 'following' | 'user' | 'hashtag'
  value        TEXT NOT NULL DEFAULT '',  -- username / hashtag ('' for following)
  account      TEXT NOT NULL DEFAULT '',  -- which IG login collects it ('' = the active one)
  enabled      INTEGER NOT NULL DEFAULT 1,
  watermark_pk INTEGER,                   -- newest pk seen; stop the walk here
  last_run     INTEGER,
  created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,                -- e.g. 'ig_paused', 'ig_interval_s'
  value  TEXT
);
"""


@dataclass
class Source:
    """Duck-typed for engine_ig.pages_for: it reads .following/.user_id/.hashtag/.label."""
    label: str
    type: str
    value: str = ""
    account: str = ""

    @property
    def following(self) -> bool:
        return self.type == "following"

    @property
    def user_id(self):
        return self.value if self.type == "user" else None

    @property
    def hashtag(self):
        return self.value if self.type == "hashtag" else None


class Store:
    def __init__(self, path="ig_results.db"):
        self.path = Path(path)
        self.db = None

    def open(self):
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        return self

    def close(self):
        if self.db:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self.open()

    # ---- settings (the dashboard's control surface) ----
    #
    # Same contract as store_fb: operational switches live HERE so the
    # dashboard can show and change them, and the service loop re-reads them
    # every cycle (RULEBOOK §6). Empty value = fall back to the env/CLI default.

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

    def __exit__(self, *a):
        self.close()

    # -- sources ------------------------------------------------------------
    def add_source(self, label, type_, value="", account="") -> None:
        if type_ not in ("following", "user", "hashtag"):
            raise ValueError("type must be following | user | hashtag")
        self.db.execute(
            "INSERT INTO sources(label,type,value,account,enabled,created_at) "
            "VALUES(?,?,?,?,1,?) ON CONFLICT(label) DO UPDATE SET "
            "type=excluded.type, value=excluded.value, account=excluded.account",
            (label, type_, value, account, _now()))
        self.db.commit()

    def set_enabled(self, label, enabled: bool) -> None:
        self.db.execute("UPDATE sources SET enabled=? WHERE label=?",
                        (int(enabled), label))
        self.db.commit()

    def sources(self, only_enabled=True) -> list:
        q = "SELECT * FROM sources"
        if only_enabled:
            q += " WHERE enabled=1"
        return [Source(r["label"], r["type"], r["value"], r["account"])
                for r in self.db.execute(q + " ORDER BY label")]

    def watermark(self, label):
        r = self.db.execute("SELECT watermark_pk FROM sources WHERE label=?",
                            (label,)).fetchone()
        return r["watermark_pk"] if r else None

    def set_watermark(self, label, pk) -> None:
        self.db.execute(
            "UPDATE sources SET watermark_pk=?, last_run=? WHERE label=?",
            (pk, _now(), label))
        self.db.commit()

    # -- posts --------------------------------------------------------------
    def upsert_posts(self, records, source_label) -> int:
        """Insert engine_ig.record() dicts; ignore ones already stored. Returns new count."""
        now = _now()
        new = 0
        for r in records:
            pk = int(r.get("pk") or 0)
            if not pk:
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO posts(pk,code,url,taken_at,username,user_pk,"
                "caption,media_type,product_type,like_count,comment_count,play_count,"
                "video_url,thumbnail_url,source_label,collected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pk, r.get("code"), r.get("url"), r.get("taken_at"),
                 r.get("username"), r.get("user_pk"), r.get("caption"),
                 r.get("media_type"), r.get("product_type"), r.get("like_count"),
                 r.get("comment_count"), r.get("play_count"), r.get("video_url"),
                 r.get("thumbnail_url"), source_label, now))
            new += cur.rowcount
        self.db.commit()
        return new

    def query(self, *, since=None, until=None, source=None, username=None,
              limit=50, before_pk=None) -> list:
        """
        Return posts newest-first as plain dicts — the shape the API serves.

        since/until are unix seconds on taken_at; before_pk is the keyset cursor
        (pass the last pk from the previous page to get the next page).
        """
        where, args = [], []
        if since is not None:
            where.append("taken_at >= ?"); args.append(int(since))
        if until is not None:
            where.append("taken_at <= ?"); args.append(int(until))
        if source:
            where.append("source_label = ?"); args.append(source)
        if username:
            where.append("username = ?"); args.append(username.lstrip("@"))
        if before_pk:
            where.append("pk < ?"); args.append(int(before_pk))
        sql = "SELECT * FROM posts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pk DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        return [dict(r) for r in self.db.execute(sql, args)]

    def stats(self) -> dict:
        c = self.db.execute
        total = c("SELECT COUNT(*) n FROM posts").fetchone()["n"]
        srcs = c("SELECT COUNT(*) n FROM sources WHERE enabled=1").fetchone()["n"]
        newest = c("SELECT MAX(taken_at) t FROM posts").fetchone()["t"]
        return {"posts": total, "sources_enabled": srcs, "newest_taken_at": newest}


def _now() -> int:
    return int(time.time())


# -- external JSON shape: the stable contract Watch-Tower consumes -----------

def to_api(row: dict) -> dict:
    """
    Map a stored row to the clean, stable object the API returns. Kept separate
    from the table so the DB can change without breaking the external contract.
    """
    import datetime as _dt
    ta = row.get("taken_at") or 0
    iso = _dt.datetime.utcfromtimestamp(ta).isoformat() + "Z" if ta else None
    return {
        "id": str(row["pk"]),
        "platform": "instagram",
        "url": row.get("url"),
        "shortcode": row.get("code"),
        "created_at": iso,
        "author": {"username": row.get("username"), "id": str(row.get("user_pk") or "")},
        "text": row.get("caption") or "",
        "media": {
            "type": {1: "photo", 2: "video", 8: "album"}.get(row.get("media_type"), "other"),
            "thumbnail": row.get("thumbnail_url") or None,
            "video": row.get("video_url") or None,
        },
        "metrics": {
            "likes": row.get("like_count"),
            "comments": row.get("comment_count"),
            "views": row.get("play_count"),
        },
        "source": row.get("source_label"),
    }


if __name__ == "__main__":
    # Tiny self-test: no network, no accounts. Proves the store round-trips.
    import tempfile, os
    p = os.path.join(tempfile.mkdtemp(), "t.db")
    with Store(p) as st:
        st.add_source("natgeo", "user", "natgeo")
        st.add_source("home", "following")
        fake = [{"pk": 17900000000000010 + i, "code": f"C{i}", "url": f"https://instagram.com/p/C{i}/",
                 "taken_at": 1720000000 + i*3600, "username": "natgeo", "user_pk": 787132,
                 "caption": f"post {i}", "media_type": 1, "like_count": 100+i,
                 "comment_count": i, "play_count": None, "video_url": "", "thumbnail_url": ""}
                for i in range(5)]
        added = st.upsert_posts(fake, "natgeo")
        again = st.upsert_posts(fake, "natgeo")   # dedup: should add 0
        rows = st.query(limit=3)
        print("added:", added, "second pass (dedup):", again)
        print("sources:", [s.label for s in st.sources()])
        print("stats:", st.stats())
        print("sample api object:", to_api(rows[0]))
