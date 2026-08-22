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
  project_id    INTEGER NOT NULL DEFAULT 0, -- WHOSE post: copied from the source
                                          -- that collected it. 0 = unassigned,
                                          -- which no scoped read ever returns.
  collected_at  INTEGER NOT NULL          -- unix seconds, when we saved it
);
CREATE INDEX IF NOT EXISTS ix_posts_taken   ON posts(taken_at);
CREATE INDEX IF NOT EXISTS ix_posts_user    ON posts(username);
CREATE INDEX IF NOT EXISTS ix_posts_source  ON posts(source_label);
-- NOTE: the project_id indexes are created in Store._migrate, NOT here.
-- executescript runs this whole block before any ALTER TABLE, and
-- CREATE TABLE IF NOT EXISTS is a no-op on a database that already has the
-- table — so an index naming a column the migration has not added yet raises
-- "no such column" on open, for every database written before scoping.

CREATE TABLE IF NOT EXISTS sources (
  label        TEXT PRIMARY KEY,          -- IDENTITY: the person, e.g. 'Narendra Modi'.
                                          -- This is the cross-platform join key. It is
                                          -- never touched by the collector.
  type         TEXT NOT NULL,             -- 'following' | 'user' | 'hashtag'
  value        TEXT NOT NULL DEFAULT '',  -- HANDLE: the human-readable platform name
                                          -- ('narendramodi'), or the hashtag.
                                          -- '' for a following source.
  platform_id  TEXT NOT NULL DEFAULT '',  -- MACHINE ID: Instagram's numeric pk for that
                                          -- handle. Resolved once, cached forever, and
                                          -- used for every fetch. Never displayed.
                                          -- See the note on the three-column split below.
  project_id   INTEGER NOT NULL DEFAULT 0, -- WHICH PROJECT owns this source.
                                          -- 0 = unassigned; a scoped read never
                                          -- returns it, so an unassigned source
                                          -- is invisible rather than shared.
  account      TEXT NOT NULL DEFAULT '',  -- which IG login collects it ('' = the active one)
  enabled      INTEGER NOT NULL DEFAULT 1,
  watermark_pk INTEGER,                   -- newest pk seen; stop the walk here
  last_run     INTEGER,
  created_at   INTEGER NOT NULL
);

-- WHY label / value / platform_id ARE THREE COLUMNS AND NOT ONE
--
-- They are three different kinds of fact with three different lifetimes:
--
--   label        the PERSON. Stable across every platform. "Narendra Modi" is
--                @narendramodi on Instagram and @modinarendra on Facebook, and
--                the label is what ties those rows to one profile — one avatar
--                fetch, one identity, three feeds. Nothing here ever rewrites it.
--   value        the HANDLE on THIS platform. Human-readable, verifiable by eye,
--                what the dashboard shows. Changes only when the person renames.
--   platform_id  the numeric pk Instagram actually accepts on its media endpoint.
--                Opaque, permanent, meaningless to a human.
--
-- Before this split, `value` had to be all three at once, which forced an
-- impossible trade: store the handle and the fetch breaks on any restricted
-- session (Instagram gates name lookup separately from media reads — see
-- engine_ig.resolve_user), or store the numeric id and lose the handle that made
-- the cross-platform mapping legible. Splitting the columns retires the trade.
--
-- Invariant: platform_id is a CACHE derived from value. If value changes, the
-- cached id is stale and add_source drops it (see the ON CONFLICT clause).

-- WHERE PROJECT SCOPING IS ENFORCED, AND WHY NOT HERE
--
-- Every read below takes project_id=None to mean "no filter". That is NOT a
-- loophole, it is the collector's requirement: collect_ig must walk every
-- source across every project in one pass, and the migration must see rows
-- that belong to nobody yet. A store that cannot express "all rows" cannot be
-- maintained.
--
-- The POLICY — that a caller may not read across projects — belongs at the
-- boundary, and lives in exactly two places:
--
--   web.py   every /api/ig/* and /api/fb/* data endpoint refuses a request
--            that names no project, instead of quietly serving all of them
--   api.py   an API key carries its own project_id; the key IS the scope, so
--            an external consumer cannot ask for someone else's data at all
--
-- Putting it there rather than here means one place to audit, and no chance of
-- a collector silently collecting nothing because a filter defaulted closed.

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
    platform_id: str = ""
    project_id: int = 0

    @property
    def following(self) -> bool:
        return self.type == "following"

    @property
    def user_id(self):
        """What the fetcher hands Instagram: the numeric pk when we have one,
        otherwise the handle (which the engine will try to resolve, and cache).

        This is the whole point of the split. A row that has been resolved once
        never asks Instagram to resolve a name again, and a row that has not
        behaves exactly as it did before — so nothing breaks on the first run
        after the migration; rows simply stop failing as they fill in."""
        if self.type != "user":
            return None
        return self.platform_id or self.value

    @property
    def handle(self):
        """The human-readable name, always — never the numeric id. Use this for
        display and for matching this row to the same person on another platform."""
        return self.value if self.type == "user" else None

    @property
    def resolved(self) -> bool:
        return bool(self.platform_id)

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
        self._migrate()
        self.db.commit()
        return self

    def _migrate(self) -> None:
        """Bring an older ig_results.db up to the current schema, in place.

        CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists, so
        a database written before platform_id existed keeps the old five-column
        sources table and every read of r["platform_id"] would raise. Adding the
        column here means the upgrade happens the first time the service opens
        the file — no separate migration step is required for the column itself.
        (migrate_ig_sources.py exists for the data move, which is a judgement
        call and deliberately not automatic.)"""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(sources)")}
        if "platform_id" not in have:
            self.db.execute(
                "ALTER TABLE sources ADD COLUMN platform_id TEXT NOT NULL DEFAULT ''")
        if "project_id" not in have:
            self.db.execute(
                "ALTER TABLE sources ADD COLUMN project_id INTEGER NOT NULL DEFAULT 0")
        havep = {r["name"] for r in self.db.execute("PRAGMA table_info(posts)")}
        if "project_id" not in havep:
            self.db.execute(
                "ALTER TABLE posts ADD COLUMN project_id INTEGER NOT NULL DEFAULT 0")
        # Created here, unconditionally, for BOTH paths: a fresh database (the
        # column came from SCHEMA) and an upgraded one (the column came from
        # the ALTER above). IF NOT EXISTS makes the repeat free.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS ix_posts_project ON posts(project_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS ix_sources_project ON sources(project_id)")

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
    def add_source(self, label, type_, value="", account="", platform_id="",
                   project_id=0) -> None:
        """Register (or update) a source.

        label is the person; value is the handle. platform_id is optional and
        almost never passed by hand — the collector fills it in on first resolve.

        A numeric `value` is accepted and routed to platform_id, so the old
        `add-source --value 787132` calling convention still does the right
        thing: the id lands in the id column instead of masquerading as a handle.

        On re-add, a cached id survives only if the handle is unchanged. Rename
        the handle and the id is dropped, because it now points at a name this
        row no longer claims — better one wasted lookup than a silently wrong
        account collected under someone else's identity."""
        if type_ not in ("following", "user", "hashtag"):
            raise ValueError("type must be following | user | hashtag")
        value = str(value or "").strip().lstrip("@")
        platform_id = str(platform_id or "").strip()
        if type_ == "user" and value.isdigit() and not platform_id:
            value, platform_id = "", value
        self.db.execute(
            "INSERT INTO sources(label,type,value,platform_id,project_id,account,"
            "enabled,created_at) VALUES(?,?,?,?,?,?,1,?) "
            "ON CONFLICT(label) DO UPDATE SET "
            "type=excluded.type, value=excluded.value, account=excluded.account, "
            "platform_id = CASE "
            "  WHEN excluded.platform_id != '' THEN excluded.platform_id "
            "  WHEN sources.value = excluded.value THEN sources.platform_id "
            "  ELSE '' END, "
            # A re-add that names no project keeps the one it already has —
            # re-adding a source is not a request to un-assign it.
            "project_id = CASE WHEN excluded.project_id != 0 "
            "  THEN excluded.project_id ELSE sources.project_id END",
            (label, type_, value, platform_id, int(project_id or 0), account, _now()))
        self.db.commit()

    def set_project(self, label, project_id) -> None:
        """Move one source to a project (0 un-assigns it, hiding it from every
        scoped read). Posts already collected keep the project they were
        collected under — history is not retroactively reassigned, because a
        post genuinely was gathered for whoever was watching at the time."""
        self.db.execute("UPDATE sources SET project_id=? WHERE label=?",
                        (int(project_id or 0), label))
        self.db.commit()

    def set_platform_id(self, label, platform_id) -> None:
        """Cache the numeric id for one source, by label."""
        self.db.execute("UPDATE sources SET platform_id=? WHERE label=?",
                        (str(platform_id), label))
        self.db.commit()

    def cache_platform_id(self, handle, platform_id) -> int:
        """Cache a resolved id against every user source carrying that handle.

        Keyed by handle, not label, because the engine only ever learns the name
        it was asked to resolve — it has no idea which identity row sent it. Two
        projects watching the same account both benefit from the one lookup.
        Returns how many rows were filled."""
        h = str(handle or "").strip().lstrip("@")
        if not h or str(platform_id or "").strip() == "":
            return 0
        cur = self.db.execute(
            "UPDATE sources SET platform_id=? "
            "WHERE type='user' AND platform_id='' AND lower(value)=lower(?)",
            (str(platform_id), h))
        self.db.commit()
        return cur.rowcount

    def unresolved_sources(self) -> list:
        """User sources that still have a handle but no cached id — the rows
        that will cost a name lookup on the next pass, and the exact worklist
        for `collect_ig.py resolve-ids`."""
        return [Source(r["label"], r["type"], r["value"], r["account"],
                       r["platform_id"], r["project_id"])
                for r in self.db.execute(
                    "SELECT * FROM sources WHERE type='user' AND platform_id='' "
                    "AND value != '' ORDER BY label")]

    def set_enabled(self, label, enabled: bool) -> None:
        self.db.execute("UPDATE sources SET enabled=? WHERE label=?",
                        (int(enabled), label))
        self.db.commit()

    def sources(self, only_enabled=True, project_id=None) -> list:
        """project_id=None means EVERY project — the collector's view. Callers
        serving a user must pass a real id; see the note above the schema for
        why that policy is enforced at the boundary and not here."""
        where, args = [], []
        if only_enabled:
            where.append("enabled=1")
        if project_id is not None:
            where.append("project_id=?"); args.append(int(project_id))
        q = "SELECT * FROM sources"
        if where:
            q += " WHERE " + " AND ".join(where)
        return [Source(r["label"], r["type"], r["value"], r["account"],
                       r["platform_id"], r["project_id"])
                for r in self.db.execute(q + " ORDER BY label", args)]

    def project_for(self, label) -> int:
        r = self.db.execute("SELECT project_id FROM sources WHERE label=?",
                            (label,)).fetchone()
        return int(r["project_id"]) if r else 0

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
    def upsert_posts(self, records, source_label, project_id=None) -> int:
        """Insert engine_ig.record() dicts; ignore ones already stored. Returns new count.

        Each post is stamped with the project of the source that surfaced it.
        project_id=None looks it up from the source, so a caller cannot forget:
        the owner is a property of the source, not something the collector has
        to remember to pass.
        """
        now = _now()
        if project_id is None:
            project_id = self.project_for(source_label)
        project_id = int(project_id or 0)
        new = 0
        for r in records:
            pk = int(r.get("pk") or 0)
            if not pk:
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO posts(pk,code,url,taken_at,username,user_pk,"
                "caption,media_type,product_type,like_count,comment_count,play_count,"
                "video_url,thumbnail_url,source_label,project_id,collected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pk, r.get("code"), r.get("url"), r.get("taken_at"),
                 r.get("username"), r.get("user_pk"), r.get("caption"),
                 r.get("media_type"), r.get("product_type"), r.get("like_count"),
                 r.get("comment_count"), r.get("play_count"), r.get("video_url"),
                 r.get("thumbnail_url"), source_label, project_id, now))
            new += cur.rowcount
        self.db.commit()
        return new

    def query(self, *, since=None, until=None, source=None, username=None,
              limit=50, before_pk=None, project_id=None) -> list:
        """
        Return posts newest-first as plain dicts — the shape the API serves.

        since/until are unix seconds on taken_at; before_pk is the keyset cursor
        (pass the last pk from the previous page to get the next page).

        project_id=None returns EVERY project's posts. Every caller that serves
        a user or an API key must pass a real id — see the note above the
        schema. The only legitimate unscoped readers are the collector, the
        migration and the tests.
        """
        where, args = [], []
        if project_id is not None:
            where.append("project_id = ?"); args.append(int(project_id))
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

    def stats(self, project_id=None) -> dict:
        """Counts. project_id=None counts every project; pass an id to describe
        one. The external API always passes one (the key's own project)."""
        c = self.db.execute
        pw, pa = "", []
        if project_id is not None:
            pw, pa = " WHERE project_id = ?", [int(project_id)]
        total = c(f"SELECT COUNT(*) n FROM posts{pw}", pa).fetchone()["n"]
        newest = c(f"SELECT MAX(taken_at) t FROM posts{pw}", pa).fetchone()["t"]
        sw, sa = "enabled=1", []
        if project_id is not None:
            sw += " AND project_id = ?"; sa = [int(project_id)]
        srcs = c(f"SELECT COUNT(*) n FROM sources WHERE {sw}", sa).fetchone()["n"]
        unres = c(f"SELECT COUNT(*) n FROM sources WHERE {sw} AND type='user' "
                  f"AND platform_id='' AND value != ''", sa).fetchone()["n"]
        return {"posts": total, "sources_enabled": srcs, "newest_taken_at": newest,
                "sources_unresolved": unres}


def _now() -> int:
    return int(time.time())


# -- external JSON shape: the stable contract Watch-Tower consumes -----------

def to_feed(row: dict) -> dict:
    """
    Map a stored row to the SHARED post shape — the same keys store_fb.to_feed
    emits and the same ones the X read path produces (RULEBOOK §2, the one post
    shape).

    to_api above is a different contract and stays as it is: it is what
    Watch-Tower pulls, nested and versioned. This one is for surfaces that mix
    platforms in a single list — a collection board holding X, Instagram and
    Facebook posts side by side cannot ask the reader to handle three shapes.
    """
    import datetime as _dt
    ta = row.get("taken_at") or 0
    created = _dt.datetime.utcfromtimestamp(ta).isoformat() + "Z" if ta else None
    ca = row.get("collected_at") or 0
    collected = (_dt.datetime.utcfromtimestamp(ca).isoformat() + "Z"
                 if ca else created)
    kind = {1: "photo", 2: "video", 8: "album"}.get(row.get("media_type"), "other")
    thumb, video = row.get("thumbnail_url") or None, row.get("video_url") or None
    media = ([{"type": kind, "url": video or thumb, "thumb": thumb}]
             if (thumb or video) else [])
    return {
        "platform": "instagram",
        "tweet_id": str(row["pk"]),
        "url": row.get("url"),
        "text": row.get("caption") or "",
        "created_at": created,
        "collected_at": collected,
        "author_username": row.get("username"),
        # Instagram gives no display name on a media row, so the handle stands
        # in. Saying the handle twice is honest; inventing a name is not.
        "author_display_name": row.get("username"),
        "author_avatar": row.get("author_avatar"),
        "media": media,
        "like_count": row.get("like_count"),
        "reply_count": row.get("comment_count"),
        "retweet_count": None,
        "view_count": row.get("play_count"),
        "source": row.get("source_label"),
    }


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
