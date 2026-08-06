"""
store.py — data shape and persistence.

Four concerns that all answer "what do we keep, and in what form":

  * snowflake helpers   — tweet ids encode their own creation time
  * normalize           — a Tweet object becomes a flat record
  * Store               — the results database
  * export writers      — JSON / CSV / JSONL / raw output

The store's job beyond plain storage is cross-run dedup (the prototype deduped
in memory, per run, so re-running rewrote the same tweets), watermarks (so a
poll can stop at known ground), and an audit trail recording WHY each poll
stopped — "nothing new" and "the account pool was starved" look identical from
the outside and demand opposite responses.

tweet_id is an INTEGER primary key on purpose: snowflake ids are time-ordered,
so `ORDER BY tweet_id` is chronological with no date parsing and no index on a
text column.
"""

import csv
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone


# ==========================================================================
# snowflake — tweet id <-> time
# ==========================================================================

# 2010-11-04T01:42:54.657Z — the epoch Twitter's snowflake generator counts from.
TWITTER_EPOCH_MS = 1288834974657

# Bits reserved for worker id + sequence. The timestamp lives above these.
_TIMESTAMP_SHIFT = 22

# Tweets posted before the Nov 2010 cutover use sequential ids, not snowflakes,
# so their creation time cannot be recovered from the id. This is the first
# snowflake id Twitter issued; anything below it is sequential. Search results
# are always modern, so this only guards against embedded quotes of very old
# tweets, whose ids would otherwise decode to a bogus 2010 timestamp.
MIN_SNOWFLAKE_ID = 29_700_859_247


def is_snowflake(tweet_id: int) -> bool:
    """False for pre-cutover sequential ids, whose time cannot be recovered."""
    return tweet_id >= MIN_SNOWFLAKE_ID


def id_to_ms(tweet_id: int) -> int:
    """Tweet id -> creation time in epoch milliseconds."""
    return (tweet_id >> _TIMESTAMP_SHIFT) + TWITTER_EPOCH_MS


def ms_to_id(ms: int) -> int:
    """
    Epoch milliseconds -> the smallest tweet id at that millisecond.

    Useful as an exclusive-lower-bound sentinel: every tweet created at or
    after `ms` has an id >= ms_to_id(ms).
    """
    return max(0, (ms - TWITTER_EPOCH_MS) << _TIMESTAMP_SHIFT)


def id_to_dt(tweet_id: int) -> datetime:
    """Tweet id -> timezone-aware UTC datetime."""
    return datetime.fromtimestamp(id_to_ms(tweet_id) / 1000.0, tz=timezone.utc)


def dt_to_id(dt: datetime) -> int:
    """Datetime -> the smallest tweet id at that instant. Naive input is UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return ms_to_id(int(dt.timestamp() * 1000))


def id_minus_ms(tweet_id: int, delta_ms: int) -> int:
    """
    Roll an id backwards in time by delta_ms.

    This is the overlap window: the poller stops at id_minus_ms(watermark, 60s)
    rather than at the watermark itself, so every poll re-reads a minute past
    the boundary and picks up tweets X indexed late.
    """
    return ms_to_id(id_to_ms(tweet_id) - delta_ms)


def lag_ms(tweet_id: int, collected_ms: int) -> int:
    """
    Milliseconds between a tweet being posted and us collecting it.

    Clamped at 0: X's clock and ours are not the same clock, and a small
    negative would otherwise poison the percentiles.
    """
    return max(0, collected_ms - id_to_ms(tweet_id))

# ==========================================================================
# normalize — Tweet object -> flat record
# ==========================================================================

# Column order used for the CSV output. Frozen — the exporter and any
# downstream consumer depend on it. New fields go in FIELDS_EXT.
FIELDS = [
    "tweet_id", "url", "created_at", "text", "lang",
    "author_username", "author_display_name", "author_id", "author_followers",
    "reply_count", "retweet_count", "like_count", "quote_count", "view_count",
    "is_retweet", "is_reply", "is_quote",
    "hashtags", "mentions", "urls", "media_urls",
    "in_reply_to", "conversation_id",
]

# Opt-in export profile (`export --fields all`). Adds the collection metadata
# that only exists once a tweet has been through the store.
FIELDS_EXT = FIELDS + [
    "collected_at", "lag_ms", "stream_label", "bookmark_count", "source",
]

# Columns the store persists as JSON arrays.
LIST_FIELDS = ("hashtags", "mentions", "urls", "media_urls")


def _g(obj, name, default=None):
    """Safe getattr."""
    return getattr(obj, name, default) if obj is not None else default


class _Attr:
    """
    Read a plain dict as if it were an object, recursively.

    Lets the same extraction code run over a live twscrape Tweet and over the
    stored `raw_json` of one, so replaying a parser fix across history is the
    same code path as parsing it fresh — not a second implementation that can
    drift from the first.
    """

    __slots__ = ("_d",)

    def __init__(self, d):
        self._d = d if isinstance(d, dict) else {}

    def __getattr__(self, name):
        v = self._d.get(name)
        if isinstance(v, dict):
            return _Attr(v)
        if isinstance(v, list):
            return [_Attr(x) if isinstance(x, dict) else x for x in v]
        return v


def _iso(dt):
    """datetime -> ISO string, tolerant of already-string or None."""
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except AttributeError:
        return str(dt)


def _best_variant(variants):
    """
    Pick one video variant deterministically: prefer mp4, then highest bitrate.

    Ranking on the tuple (is_mp4, bitrate) with a STRICT `>` means the first
    maximum wins, so the same payload always yields the same URL. The previous
    `>=` comparison picked the last of any tie, which made output depend on
    dict ordering.

    The mp4 preference matters because HLS `.m3u8` playlist variants carry no
    bitrate. twscrape 0.19.2 happens to filter those out upstream
    (models.py:384), but this module is engine-agnostic by design and another
    engine will not.
    """
    best, best_key = None, None
    for var in variants:
        url = _g(var, "url")
        if not url:
            continue
        ctype = (_g(var, "contentType", "") or "").lower()
        key = (ctype == "video/mp4", _g(var, "bitrate", 0) or 0)
        if best_key is None or key > best_key:
            best_key, best = key, url
    return best


def _media_urls(media):
    """
    Flat list of media URLs. Kept because FIELDS is frozen and both the CSV
    export and the API publish this shape; `_media(media)` is the richer view.
    """
    return [m["url"] for m in _media(media) if m.get("url")]


def _media(media) -> list:
    """
    Media as {type, url, thumb}, so a video can be shown without fetching it.

    X gives every video a `thumbnailUrl` — a small still on pbs.twimg.com. We
    used to throw it away and keep only the .mp4, which forced anything
    displaying a tweet to pull the whole video just to show that one exists.
    A 30-second clip is several megabytes; its thumbnail is a few tens of KB.

    Nothing is downloaded here either way. These are X's own URLs, and whoever
    renders them fetches from X directly — so the saving is in what a viewer
    (and X) has to move, not in this database.
    """
    out = []
    if media is None:
        return out

    for p in _g(media, "photos", []) or []:
        if (u := _g(p, "url")):
            out.append({"type": "photo", "url": u, "thumb": u})

    for v in _g(media, "videos", []) or []:
        url = _best_variant(_g(v, "variants", []) or [])
        thumb = _g(v, "thumbnailUrl")
        if url or thumb:
            out.append({"type": "video", "url": url or thumb, "thumb": thumb,
                        "duration": _g(v, "duration")})

    # Animated GIFs are mp4s on X, and always silent and looping.
    for a in _g(media, "animated", []) or []:
        url, thumb = _g(a, "videoUrl"), _g(a, "thumbnailUrl")
        if url or thumb:
            out.append({"type": "gif", "url": url or thumb, "thumb": thumb})

    return out


def normalize_tweet(t) -> dict:
    user = _g(t, "user")
    return {
        "tweet_id": str(_g(t, "id", "")) or None,
        "url": _g(t, "url"),
        "created_at": _iso(_g(t, "date")),
        "text": _g(t, "rawContent"),
        "lang": _g(t, "lang"),

        "author_username": _g(user, "username"),
        "author_display_name": _g(user, "displayname"),
        "author_id": str(_g(user, "id", "")) or None,
        "author_followers": _g(user, "followersCount"),

        "reply_count": _g(t, "replyCount"),
        "retweet_count": _g(t, "retweetCount"),
        "like_count": _g(t, "likeCount"),
        "quote_count": _g(t, "quoteCount"),
        "view_count": _g(t, "viewCount"),

        "is_retweet": _g(t, "retweetedTweet") is not None,
        "is_reply": _g(t, "inReplyToTweetId") is not None,
        "is_quote": _g(t, "quotedTweet") is not None,

        "hashtags": list(_g(t, "hashtags", []) or []),
        "mentions": [_g(u, "username") for u in (_g(t, "mentionedUsers", []) or [])],
        "urls": [_g(l, "url") for l in (_g(t, "links", []) or [])],
        "media_urls": _media_urls(_g(t, "media")),
        "media": _media(_g(t, "media")),

        "in_reply_to": str(_g(t, "inReplyToTweetId")) if _g(t, "inReplyToTweetId") else None,
        "conversation_id": str(_g(t, "conversationId")) if _g(t, "conversationId") else None,
    }


def to_csv_row(rec: dict, fields=None) -> dict:
    """
    Flatten list fields to pipe-joined strings, keeping only `fields`.

    Restricted deliberately: a new key appearing in normalize_tweet must not
    silently become a new column, and must not blow DictWriter up either.
    `media` is exactly that case — structured, useful over JSON and the API,
    meaningless as a CSV cell.

    `fields` MUST be the same list the DictWriter was built with. Hardcoding
    FIELDS here instead cost a round of silent data loss: `--fields all` writes
    FIELDS_EXT headers, every extended column fell outside the hardcoded list
    and was dropped, and DictWriter fills a missing key with '' rather than
    complaining — so the export still had collected_at, lag_ms, stream_label,
    bookmark_count and source as headers, with nothing under them. The test
    that should have caught it only checked the header row.
    """
    keep = FIELDS if fields is None else fields
    row = {k: rec.get(k) for k in keep if k in rec}
    for k in LIST_FIELDS:
        if k in row:
            vals = [str(v) for v in (row.get(k) or []) if v is not None]
            row[k] = "|".join(vals)
    return row


def _loads_list(val):
    """Parse a stored JSON array back to a list, tolerating junk."""
    if isinstance(val, list):
        return val
    if not val:
        return []
    try:
        parsed = json.loads(val)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def from_store_row(row, fields=None) -> dict:
    """
    Inverse of the store's write path: a sqlite row -> a record shaped like
    normalize_tweet() output, so to_csv_row() can be reused verbatim.

    tweet_id is stored as INTEGER (so ORDER BY is chronological) and comes back
    out as a string, because JSON consumers lose precision above 2^53.
    """
    src = dict(row)
    out = {}
    for k in (fields or FIELDS):
        val = src.get(k)
        if k in LIST_FIELDS:
            out[k] = _loads_list(val)
        elif k in ("tweet_id", "author_id", "in_reply_to", "conversation_id"):
            out[k] = str(val) if val not in (None, "") else None
        elif k in ("is_retweet", "is_reply", "is_quote"):
            out[k] = bool(val)
        else:
            out[k] = val
    return out

# ==========================================================================
# Store — the results database
# ==========================================================================

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
  stream_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  label         TEXT NOT NULL UNIQUE,
  query         TEXT NOT NULL,
  tab           TEXT NOT NULL DEFAULT 'Latest',
  watermarked   INTEGER NOT NULL DEFAULT 1,
  first_poll_ms INTEGER,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tweets (
  tweet_id            INTEGER PRIMARY KEY,   -- snowflake: ORDER BY == chronological
  created_at          TEXT,
  created_ms          INTEGER NOT NULL,      -- from the snowflake (ms precision)
  collected_at        TEXT NOT NULL,         -- frozen at first sight
  collected_ms        INTEGER NOT NULL,
  last_seen_at        TEXT NOT NULL,
  lag_ms              INTEGER NOT NULL,      -- frozen at first sight
  url                 TEXT,
  text                TEXT,
  lang                TEXT,
  author_username     TEXT,
  author_display_name TEXT,
  author_id           TEXT,
  author_followers    INTEGER,
  reply_count         INTEGER,
  retweet_count       INTEGER,
  like_count          INTEGER,
  quote_count         INTEGER,
  view_count          INTEGER,
  bookmark_count      INTEGER,
  is_retweet          INTEGER,
  is_reply            INTEGER,
  is_quote            INTEGER,
  hashtags            TEXT,                  -- JSON array
  mentions            TEXT,
  urls                TEXT,
  media_urls          TEXT,
  in_reply_to         TEXT,
  conversation_id     TEXT,
  source              TEXT NOT NULL DEFAULT 'result',  -- result | embedded
  media_json          TEXT,                  -- [{type,url,thumb}] so a video
                                             -- can be shown without fetching it
  raw_json            TEXT,                  -- Tweet.json(): nothing is lost
  raw_entry_json      TEXT
);
CREATE INDEX IF NOT EXISTS ix_tweets_created   ON tweets(created_ms DESC);
CREATE INDEX IF NOT EXISTS ix_tweets_collected ON tweets(collected_ms DESC);
CREATE INDEX IF NOT EXISTS ix_tweets_author    ON tweets(author_username, created_ms DESC);

-- A tweet can match several streams; this is the many-to-many edge.
CREATE TABLE IF NOT EXISTS tweet_hits (
  stream_id     INTEGER NOT NULL,
  tweet_id      INTEGER NOT NULL,
  poll_id       INTEGER,
  first_seen_ms INTEGER NOT NULL,
  PRIMARY KEY (stream_id, tweet_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hits_stream ON tweet_hits(stream_id, tweet_id DESC);

CREATE TABLE IF NOT EXISTS watermarks (
  stream_id         INTEGER PRIMARY KEY,
  high_tweet_id     INTEGER NOT NULL,
  high_created_ms   INTEGER NOT NULL,
  interval_s        REAL NOT NULL DEFAULT 30,
  ewma_rate         REAL NOT NULL DEFAULT 0,
  consecutive_empty INTEGER NOT NULL DEFAULT 0,
  next_poll_ms      INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
  poll_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  stream_id     INTEGER NOT NULL,
  kind          TEXT NOT NULL,          -- poll | seed | oneshot | sweep
  started_ms    INTEGER NOT NULL,
  finished_ms   INTEGER,
  account       TEXT,
  pages         INTEGER NOT NULL DEFAULT 0,
  results       INTEGER NOT NULL DEFAULT 0,
  new_tweets    INTEGER NOT NULL DEFAULT 0,
  dup_tweets    INTEGER NOT NULL DEFAULT 0,
  orphans       INTEGER NOT NULL DEFAULT 0,
  max_id        INTEGER,
  min_id        INTEGER,
  stop_reason   TEXT,                   -- watermark | exhausted | page_budget
                                        -- | no_account_or_abort | error
  rl_limit      INTEGER,
  rl_remaining  INTEGER,
  rl_reset      INTEGER,                -- unix ts the budget refills; the guard
                                        -- needs it to tell "spent" from "stale"
  lag_p50_ms    INTEGER,
  lag_p95_ms    INTEGER,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_polls_stream ON polls(stream_id, started_ms DESC);

-- Recorded, never silently dropped: the window between the old watermark and
-- the oldest tweet a poll actually reached. Backfilling these is Phase 6; for
-- now they are surfaced by `doctor` so under-collection is visible.
CREATE TABLE IF NOT EXISTS gaps (
  gap_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  stream_id     INTEGER NOT NULL,
  lo_tweet_id   INTEGER NOT NULL,       -- open interval (lo, hi)
  hi_tweet_id   INTEGER NOT NULL,
  lo_ms         INTEGER NOT NULL,
  hi_ms         INTEGER NOT NULL,
  resume_cursor TEXT,
  cursor_ms     INTEGER,
  status        TEXT NOT NULL DEFAULT 'open',
  detected_poll INTEGER,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gaps_open ON gaps(status, stream_id, hi_tweet_id DESC);

-- How far each webhook has been delivered. One row per endpoint; see the
-- webhook section below for why this is a cursor and not a queue.
CREATE TABLE IF NOT EXISTS webhook_state (
  label           TEXT PRIMARY KEY,
  last_ms         INTEGER NOT NULL DEFAULT 0,   -- cursor: collected_ms
  last_tweet_id   INTEGER NOT NULL DEFAULT 0,   -- cursor: tie-break within a ms
  sent            INTEGER NOT NULL DEFAULT 0,   -- lifetime tweets delivered
  failures        INTEGER NOT NULL DEFAULT 0,   -- consecutive, resets on success
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,   -- back-off gate
  last_error      TEXT,
  last_ok_ms      INTEGER
);

-- Delivery walks tweets in collection order, which is not the primary key.
CREATE INDEX IF NOT EXISTS ix_tweets_delivery ON tweets(collected_ms, tweet_id);

-- ---------------------------------------------------------------------------
-- Projects and watchlists (the dashboard's organizing layer).
--
-- A PROJECT groups watchlists and feeds for one client/beat. Scraper accounts
-- and the collector stay global — a project is a VIEW over streams, never its
-- own collection machinery. A WATCHLIST is a list of handles the user manages
-- in the dashboard; it COMPILES into ordinary streams (the unit the collector
-- already understands), so nothing downstream of here changes:
--
--   kind='query'  ->  one '(from:a OR from:b ...)' search stream per chunk of
--                     handles (X's query length caps a chunk at ~20)
--   kind='xlist'  ->  one list_id stream (an X List, external or promoted)
--
-- Compiled streams are named 'wl:<watchlist_id>:<n>' and carry watched=1 so
-- the watcher picks them up without a config.toml entry (same mechanism as
-- tg_enabled). project_streams maps EVERY stream a project sees — compiled
-- ones and manually attached ones alike — so "this project's tweets" is one
-- join, not special cases.
CREATE TABLE IF NOT EXISTS projects (
  project_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlists (
  watchlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'query',   -- 'query' | 'xlist'
  list_id      TEXT,                            -- when kind='xlist'
  created_at   TEXT NOT NULL,
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS watchlist_members (
  watchlist_id INTEGER NOT NULL,
  handle       TEXT NOT NULL,                   -- lowercase, no '@'
  display_name TEXT,
  user_id      TEXT,                            -- filled when resolved
  added_at     TEXT NOT NULL,
  PRIMARY KEY (watchlist_id, handle)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS project_streams (
  project_id INTEGER NOT NULL,
  stream_id  INTEGER NOT NULL,
  PRIMARY KEY (project_id, stream_id)
) WITHOUT ROWID;

-- Collections: curation boards. An editor pins posts from the feed or search
-- into a named board ("Floods — day 2") and hands the board off (CSV today;
-- other shapes can follow). Pinning is a REFERENCE to the tweets table, never
-- a copy — the post stays exactly one row, and unpinning deletes nothing.
CREATE TABLE IF NOT EXISTS collections (
  collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    INTEGER NOT NULL,
  name          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS collection_items (
  collection_id INTEGER NOT NULL,
  tweet_id      INTEGER NOT NULL,
  added_ms      INTEGER NOT NULL,
  PRIMARY KEY (collection_id, tweet_id)
) WITHOUT ROWID;

-- Velocity alerts: "this scope is posting far above its usual pace." Counts
-- only — no sentiment, no AI (analysis stays in Watch-Tower). A rule scopes
-- to one watchlist or a whole project, compares the last hour against the
-- trailing day's hourly average, and pings Telegram through the same
-- machinery streams already use. last_fired_ms is the cooldown anchor: a
-- surge that lasts three hours should ping a few times, not sixty.
CREATE TABLE IF NOT EXISTS alerts (
  alert_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    INTEGER NOT NULL,
  watchlist_id  INTEGER,                       -- NULL = the whole project
  threshold     REAL NOT NULL DEFAULT 3.0,     -- × the usual hourly pace
  min_posts     INTEGER NOT NULL DEFAULT 10,   -- floor: quiet scopes never fire
  tg_chat_id    TEXT,                          -- NULL = TELEGRAM_CHAT_ID env
  enabled       INTEGER NOT NULL DEFAULT 1,
  last_fired_ms INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);
"""


@dataclass
class UpsertCounts:
    new: int = 0
    dup: int = 0
    embedded: int = 0


def _iso_ms(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).isoformat()


def parse_window(spec: str | None) -> int | None:
    """'6h' / '3d' / '30m' / an ISO timestamp -> epoch ms. None passes through."""
    if not spec:
        return None
    s = str(spec).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(s) > 1 and s[-1].lower() in units and s[:-1].replace(".", "", 1).isdigit():
        return int((time.time() - float(s[:-1]) * units[s[-1].lower()]) * 1000)
    import datetime as _dt

    try:
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError as e:
        raise ValueError(f"Cannot parse time {spec!r}. Use ISO, or 30m / 6h / 3d.") from e


# An X handle: 1–15 word characters, with or without a leading '@'. Also
# accepts a pasted profile URL ('x.com/NatGeo' or 'https://twitter.com/NatGeo').
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_handle(raw) -> str | None:
    """'@NatGeo' / 'natgeo' / 'https://x.com/NatGeo' -> 'natgeo'; None if invalid."""
    s = str(raw or "").strip()
    if "/" in s:
        for host in ("x.com/", "twitter.com/"):
            if host in s:
                s = s.split(host, 1)[1]
                break
        s = s.split("/", 1)[0].split("?", 1)[0]
    s = s.lstrip("@").strip()
    return s.lower() if _HANDLE_RE.match(s) else None


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


class Store:
    """
    Synchronous sqlite3 behind an async-shaped facade.

    Deliberately not aiosqlite: every write here is a sub-millisecond local
    operation, and the collector is I/O-bound on X's API by orders of
    magnitude. WAL keeps `export` and `doctor` readable while a watcher writes.
    """

    def __init__(self, path, keep_entry_json: bool = False):
        # See config.Defaults.keep_entry_json: off by default because the
        # entry wrapper is ~60% of the database and nothing reads it.
        self.keep_entry_json = keep_entry_json
        self.path = str(path)
        self.db: sqlite3.Connection | None = None

    async def open(self):
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        return self

    def _migrate(self):
        """
        Add columns that appeared after a database was first created.

        CREATE TABLE IF NOT EXISTS silently does nothing on an existing table,
        so new columns need an explicit ALTER or older databases keep working
        but quietly lack the field.
        """
        # Per-stream tuning lives in the DATABASE, not config.toml.
        #
        # config.toml declares WHAT to watch; these say how it is tuned right
        # now. Splitting them that way is what lets the dashboard change a
        # setting and have the running watcher pick it up on its next cycle —
        # editing config.toml would need a restart, and rewriting a file a
        # human hand-edited would eat their comments. NULL means "inherit
        # whatever config.toml says", so nothing here has to duplicate a
        # default in order to leave it alone.
        wanted = {"polls": {"rl_reset": "INTEGER"},
                  "tweets": {"media_json": "TEXT"},
                  "streams": {"list_id": "TEXT",
                              # "The watcher should poll this even though
                              # config.toml never heard of it." Watchlist-
                              # compiled streams set it; the same mechanism
                              # tg_enabled already provides for dashboard
                              # searches, made explicit instead of implied.
                              "watched": "INTEGER NOT NULL DEFAULT 0",
                              "paused": "INTEGER NOT NULL DEFAULT 0",
                              "min_interval_s": "REAL",
                              "max_pages_per_poll": "INTEGER",
                              "tg_enabled": "INTEGER NOT NULL DEFAULT 0",
                              "tg_chat_id": "TEXT",
                              "tg_min_likes": "INTEGER NOT NULL DEFAULT 0",
                              "tg_skip_retweets": "INTEGER NOT NULL DEFAULT 0",
                              # Belt and braces against replies. X's own
                              # "-filter:replies" is a SEARCH hint, not a
                              # guarantee — and it does nothing at all for a
                              # stream collected some other way (a List, or a
                              # query written without it). Filtering on our own
                              # is_reply column at delivery time is the only
                              # check that cannot be talked round.
                              "tg_skip_replies": "INTEGER NOT NULL DEFAULT 0",
                              # Bound delivery by when a tweet was PUBLISHED,
                              # not when we happened to collect it. A stream
                              # that starts on an account with a week of
                              # history otherwise pushes all of it into the
                              # channel as if it were new. 0 = no limit.
                              "tg_max_age_h": "INTEGER NOT NULL DEFAULT 0"}}
        added = []
        for table, cols in wanted.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    added.append(name)

        if "media_json" in added:
            self._backfill_media()

        # Every pre-projects database gets a "Default" project holding its
        # existing streams, so nothing vanishes from the dashboard when the
        # projects layer arrives. Runs exactly once — the projects table being
        # non-empty is the marker, so a user who later renames or rearranges
        # is never fought by the migration.
        if not self.db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            now = _iso_ms(int(time.time() * 1000))
            self.db.execute(
                "INSERT INTO projects(name, created_at) VALUES('Default', ?)", (now,))

        # Adopt orphan streams — anything in NO project — into the oldest one.
        # config.toml can grow a [[streams]] block and the watcher will
        # ensure_stream it long after the one-shot backfill above ran; without
        # this, that stream's tweets would be invisible to every project feed
        # while still being collected. Watchlist-compiled streams are attached
        # to their own project at compile time, so this never touches them.
        oldest = self.db.execute(
            "SELECT project_id FROM projects ORDER BY project_id LIMIT 1").fetchone()
        if oldest:
            self.db.execute(
                "INSERT OR IGNORE INTO project_streams(project_id, stream_id) "
                "SELECT ?, s.stream_id FROM streams s WHERE NOT EXISTS "
                "  (SELECT 1 FROM project_streams ps WHERE ps.stream_id = s.stream_id)",
                (oldest["project_id"],))

    def _backfill_media(self) -> int:
        """
        Fill media_json for tweets collected before the column existed.

        This is R9 paying for itself. Video thumbnails were always in X's
        payload and we simply never extracted them — and because every tweet
        keeps its complete `Tweet.json()`, a parser change can be applied to
        history instead of only to whatever arrives next. Without the raw
        payloads this data would have been permanently lost for everything
        already collected.

        Runs once, inside the same migration that adds the column.
        """
        rows = self.db.execute(
            "SELECT tweet_id, raw_json FROM tweets "
            "WHERE media_json IS NULL AND raw_json IS NOT NULL").fetchall()
        n = 0
        for r in rows:
            try:
                raw = json.loads(r["raw_json"])
            except (TypeError, ValueError):
                continue
            # The raw payload is plain dicts; _media reads attributes, so give
            # it something that answers getattr.
            media = _media(_Attr(raw.get("media")) if raw.get("media") else None)
            self.db.execute("UPDATE tweets SET media_json = ? WHERE tweet_id = ?",
                            (json.dumps(media), r["tweet_id"]))
            n += 1
        return n

    async def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *exc):
        await self.close()

    # ---------------- streams ----------------

    async def ensure_stream(self, label: str, query: str, tab: str = "Latest",
                            watermarked: bool = True, list_id: str = "") -> int:
        cur = self.db.execute("SELECT stream_id FROM streams WHERE label = ?", (label,))
        row = cur.fetchone()
        if row:
            # Changing a stream's source changes what its watermark means, so
            # keep the stored definition in sync and let doctor surface it.
            self.db.execute(
                "UPDATE streams SET query = ?, tab = ?, watermarked = ?, list_id = ? "
                "WHERE stream_id = ?",
                (query, tab, int(watermarked), list_id or None, row["stream_id"]),
            )
            return row["stream_id"]
        cur = self.db.execute(
            "INSERT INTO streams(label, query, tab, watermarked, list_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (label, query, tab, int(watermarked), list_id or None,
             _iso_ms(int(time.time() * 1000))),
        )
        return cur.lastrowid

    # ----------------------------------------------------------------------
    # projects & watchlists
    # ----------------------------------------------------------------------
    #
    # See the schema comment: a watchlist is dashboard state that COMPILES into
    # ordinary streams. All the write paths live here so web.py stays a thin
    # validator, and the offline tests can exercise the whole lifecycle with no
    # HTTP involved.

    # Handles per compiled '(from:a OR from:b ...)' query. X caps search query
    # length; 20 fifteen-char handles plus glue sits comfortably under it.
    WATCHLIST_CHUNK = 20

    async def projects(self, include_archived: bool = False) -> list:
        rows = self.db.execute(
            "SELECT p.*, "
            "  (SELECT COUNT(*) FROM watchlists w WHERE w.project_id = p.project_id) AS watchlists, "
            "  (SELECT COUNT(*) FROM project_streams ps WHERE ps.project_id = p.project_id) AS streams "
            "FROM projects p "
            + ("" if include_archived else "WHERE p.archived = 0 ")
            + "ORDER BY p.project_id").fetchall()
        return [dict(r) for r in rows]

    async def create_project(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "a project needs a name"}
        try:
            cur = self.db.execute(
                "INSERT INTO projects(name, created_at) VALUES(?, ?)",
                (name, _iso_ms(int(time.time() * 1000))))
        except sqlite3.IntegrityError:
            return {"error": f"a project called {name!r} already exists"}
        return {"project_id": cur.lastrowid, "name": name}

    async def set_project_archived(self, project_id: int, archived: bool) -> bool:
        cur = self.db.execute(
            "UPDATE projects SET archived = ? WHERE project_id = ?",
            (int(bool(archived)), int(project_id)))
        return cur.rowcount > 0

    async def project_stream_ids(self, project_id: int) -> list:
        return [r["stream_id"] for r in self.db.execute(
            "SELECT stream_id FROM project_streams WHERE project_id = ?",
            (int(project_id),))]

    async def attach_stream(self, project_id: int, stream_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO project_streams(project_id, stream_id) VALUES(?,?)",
            (int(project_id), int(stream_id)))

    async def watchlists(self, project_id: int) -> list:
        out = []
        for w in self.db.execute(
                "SELECT * FROM watchlists WHERE project_id = ? ORDER BY watchlist_id",
                (int(project_id),)).fetchall():
            members = [dict(m) for m in self.db.execute(
                "SELECT handle, display_name, user_id, added_at "
                "FROM watchlist_members WHERE watchlist_id = ? ORDER BY handle",
                (w["watchlist_id"],))]
            streams = [dict(s) for s in self.db.execute(
                "SELECT s.stream_id, s.label, s.paused, "
                "       COUNT(h.tweet_id) AS tweets "
                "FROM streams s LEFT JOIN tweet_hits h USING(stream_id) "
                "WHERE s.label LIKE ? GROUP BY s.stream_id ORDER BY s.label",
                (f"wl:{w['watchlist_id']}:%",))]
            d = dict(w)
            d["members"] = members
            d["streams"] = streams
            out.append(d)
        return out

    async def create_watchlist(self, project_id: int, name: str,
                               kind: str = "query", list_id: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "a watchlist needs a name"}
        if kind not in ("query", "xlist"):
            return {"error": "kind must be 'query' or 'xlist'"}
        if kind == "xlist" and not (list_id or "").strip():
            return {"error": "an xlist watchlist needs the X List id"}
        if not self.db.execute("SELECT 1 FROM projects WHERE project_id = ?",
                               (int(project_id),)).fetchone():
            return {"error": f"no project {project_id}"}
        try:
            cur = self.db.execute(
                "INSERT INTO watchlists(project_id, name, kind, list_id, created_at) "
                "VALUES(?,?,?,?,?)",
                (int(project_id), name, kind, (list_id or "").strip() or None,
                 _iso_ms(int(time.time() * 1000))))
        except sqlite3.IntegrityError:
            return {"error": f"this project already has a watchlist called {name!r}"}
        wid = cur.lastrowid
        if kind == "xlist":
            await self.compile_watchlist(wid)
        return {"watchlist_id": wid, "name": name, "kind": kind}

    async def set_watchlist_members(self, watchlist_id: int,
                                    add: list | None = None,
                                    remove: list | None = None) -> dict:
        """
        Add/remove handles, then recompile. Handles are normalized (no '@',
        lowercase) and validated BEFORE anything is written: one bad handle
        rejects the whole request, so a typo never half-applies.
        """
        w = self.db.execute("SELECT * FROM watchlists WHERE watchlist_id = ?",
                            (int(watchlist_id),)).fetchone()
        if not w:
            return {"error": f"no watchlist {watchlist_id}"}

        adds, bad = [], []
        for h in (add or []):
            n = normalize_handle(h)
            (adds if n else bad).append(n or str(h))
        if bad:
            return {"error": "not valid X handles: " + ", ".join(repr(b) for b in bad)}
        removes = [normalize_handle(h) for h in (remove or [])]
        removes = [h for h in removes if h]

        now = _iso_ms(int(time.time() * 1000))
        for h in adds:
            self.db.execute(
                "INSERT OR IGNORE INTO watchlist_members(watchlist_id, handle, added_at) "
                "VALUES(?,?,?)", (int(watchlist_id), h, now))
        for h in removes:
            self.db.execute(
                "DELETE FROM watchlist_members WHERE watchlist_id = ? AND handle = ?",
                (int(watchlist_id), h))

        compiled = await self.compile_watchlist(int(watchlist_id))
        return {"watchlist_id": int(watchlist_id),
                "added": len(adds), "removed": len(removes), **compiled}

    async def compile_watchlist(self, watchlist_id: int) -> dict:
        """
        Rebuild the streams a watchlist stands for.

        Deterministic from the member list: chunk N handles into groups of
        WATCHLIST_CHUNK (sorted, so membership — not insertion order — decides
        the chunks), one '(from:a OR from:b ...)' stream per chunk, labelled
        'wl:<id>:<n>'. A chunk that falls out of use is PAUSED, never deleted:
        its tweet_hits are attribution history, and pausing is what
        forget_stream is for if the operator truly wants it gone.

        Every compiled stream carries watched=1 (the watcher polls it without a
        config.toml entry) and is attached to the watchlist's project.
        """
        w = self.db.execute("SELECT * FROM watchlists WHERE watchlist_id = ?",
                            (int(watchlist_id),)).fetchone()
        if not w:
            return {"error": f"no watchlist {watchlist_id}"}

        labels = []
        if w["kind"] == "xlist":
            label = f"wl:{w['watchlist_id']}:0"
            sid = await self.ensure_stream(label, "", "Latest", True,
                                           list_id=w["list_id"] or "")
            self.db.execute(
                "UPDATE streams SET watched = 1, paused = 0 WHERE stream_id = ?", (sid,))
            await self.attach_stream(w["project_id"], sid)
            labels.append(label)
        else:
            handles = [r["handle"] for r in self.db.execute(
                "SELECT handle FROM watchlist_members WHERE watchlist_id = ? "
                "ORDER BY handle", (int(watchlist_id),))]
            for n in range(0, len(handles), self.WATCHLIST_CHUNK):
                chunk = handles[n:n + self.WATCHLIST_CHUNK]
                label = f"wl:{w['watchlist_id']}:{n // self.WATCHLIST_CHUNK}"
                query = "(" + " OR ".join(f"from:{h}" for h in chunk) + ")"
                sid = await self.ensure_stream(label, query, "Latest", True)
                self.db.execute(
                    "UPDATE streams SET watched = 1, paused = 0 WHERE stream_id = ?",
                    (sid,))
                await self.attach_stream(w["project_id"], sid)
                labels.append(label)

        # Chunks beyond the current count (members shrank, or kind changed):
        # paused and un-watched, keeping their collected history attributable.
        for r in self.db.execute("SELECT stream_id, label FROM streams WHERE label LIKE ?",
                                 (f"wl:{w['watchlist_id']}:%",)).fetchall():
            if r["label"] not in labels:
                self.db.execute(
                    "UPDATE streams SET paused = 1, watched = 0 WHERE stream_id = ?",
                    (r["stream_id"],))
        return {"streams": labels}

    async def delete_watchlist(self, watchlist_id: int) -> dict:
        """
        Remove the watchlist and STOP its collection; keep collected tweets.

        Compiled streams are paused + un-watched rather than deleted — same
        reasoning as compile_watchlist's retirement path. Destroying the data
        stays an explicit per-stream forget_stream(delete_tweets=True).
        """
        w = self.db.execute("SELECT * FROM watchlists WHERE watchlist_id = ?",
                            (int(watchlist_id),)).fetchone()
        if not w:
            return {"error": f"no watchlist {watchlist_id}"}
        self.db.execute(
            "UPDATE streams SET paused = 1, watched = 0 WHERE label LIKE ?",
            (f"wl:{w['watchlist_id']}:%",))
        self.db.execute("DELETE FROM watchlist_members WHERE watchlist_id = ?",
                        (int(watchlist_id),))
        self.db.execute("DELETE FROM watchlists WHERE watchlist_id = ?",
                        (int(watchlist_id),))
        return {"removed": True, "streams_paused": True, "tweets_kept": True}

    # ----------------------------------------------------------------------
    # collections (curation boards)
    # ----------------------------------------------------------------------

    async def collections(self, project_id: int) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT c.*, COUNT(i.tweet_id) AS items "
            "FROM collections c LEFT JOIN collection_items i USING(collection_id) "
            "WHERE c.project_id = ? GROUP BY c.collection_id ORDER BY c.collection_id",
            (int(project_id),))]

    async def create_collection(self, project_id: int, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "a collection needs a name"}
        if not self.db.execute("SELECT 1 FROM projects WHERE project_id = ?",
                               (int(project_id),)).fetchone():
            return {"error": f"no project {project_id}"}
        try:
            cur = self.db.execute(
                "INSERT INTO collections(project_id, name, created_at) VALUES(?,?,?)",
                (int(project_id), name, _iso_ms(int(time.time() * 1000))))
        except sqlite3.IntegrityError:
            return {"error": f"this project already has a collection called {name!r}"}
        return {"collection_id": cur.lastrowid, "name": name}

    async def delete_collection(self, collection_id: int) -> dict:
        if not self.db.execute("SELECT 1 FROM collections WHERE collection_id = ?",
                               (int(collection_id),)).fetchone():
            return {"error": f"no collection {collection_id}"}
        self.db.execute("DELETE FROM collection_items WHERE collection_id = ?",
                        (int(collection_id),))
        self.db.execute("DELETE FROM collections WHERE collection_id = ?",
                        (int(collection_id),))
        return {"removed": True, "tweets_kept": True}

    async def collection_pin(self, collection_id: int,
                             add: list | None = None,
                             remove: list | None = None) -> dict:
        """
        Pin/unpin posts. Only ids that exist in tweets are pinned — a stale id
        (from a page open across a data wipe) is reported, not stored, so a
        board can never hold a reference that renders as a hole.
        """
        if not self.db.execute("SELECT 1 FROM collections WHERE collection_id = ?",
                               (int(collection_id),)).fetchone():
            return {"error": f"no collection {collection_id}"}
        now = int(time.time() * 1000)
        pinned, missing = 0, []
        for t in (add or []):
            try:
                tid = int(t)
            except (TypeError, ValueError):
                missing.append(str(t)); continue
            if self.db.execute("SELECT 1 FROM tweets WHERE tweet_id = ?",
                               (tid,)).fetchone():
                self.db.execute(
                    "INSERT OR IGNORE INTO collection_items(collection_id, tweet_id, added_ms) "
                    "VALUES(?,?,?)", (int(collection_id), tid, now))
                pinned += 1
            else:
                missing.append(str(t))
        removed = 0
        for t in (remove or []):
            try:
                tid = int(t)
            except (TypeError, ValueError):
                continue
            cur = self.db.execute(
                "DELETE FROM collection_items WHERE collection_id = ? AND tweet_id = ?",
                (int(collection_id), tid))
            removed += cur.rowcount
        out = {"pinned": pinned, "removed": removed}
        if missing:
            out["not_found"] = missing
        return out

    async def collection_rows(self, collection_id: int) -> list:
        """The board's posts, newest pin first, as full tweet rows."""
        return [dict(r) for r in self.db.execute(
            "SELECT t.*, i.added_ms FROM collection_items i "
            "JOIN tweets t USING(tweet_id) WHERE i.collection_id = ? "
            "ORDER BY i.added_ms DESC, t.tweet_id DESC", (int(collection_id),))]

    # ----------------------------------------------------------------------
    # velocity alerts
    # ----------------------------------------------------------------------

    async def alerts(self, project_id: int = 0, enabled_only: bool = False) -> list:
        where, params = [], []
        if project_id:
            where.append("a.project_id = ?"); params.append(int(project_id))
        if enabled_only:
            where.append("a.enabled = 1")
        rows = self.db.execute(
            "SELECT a.*, w.name AS watchlist_name, p.name AS project_name "
            "FROM alerts a "
            "LEFT JOIN watchlists w USING(watchlist_id) "
            "JOIN projects p ON p.project_id = a.project_id "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "ORDER BY a.alert_id", params).fetchall()
        return [dict(r) for r in rows]

    async def create_alert(self, project_id: int, watchlist_id=None,
                           threshold: float = 3.0, min_posts: int = 10,
                           tg_chat_id: str = "") -> dict:
        if not self.db.execute("SELECT 1 FROM projects WHERE project_id = ?",
                               (int(project_id),)).fetchone():
            return {"error": f"no project {project_id}"}
        if watchlist_id and not self.db.execute(
                "SELECT 1 FROM watchlists WHERE watchlist_id = ? AND project_id = ?",
                (int(watchlist_id), int(project_id))).fetchone():
            return {"error": f"no watchlist {watchlist_id} in this project"}
        try:
            threshold = max(1.1, float(threshold))
            min_posts = max(1, int(min_posts))
        except (TypeError, ValueError):
            return {"error": "threshold and min_posts must be numbers"}
        cur = self.db.execute(
            "INSERT INTO alerts(project_id, watchlist_id, threshold, min_posts, "
            " tg_chat_id, created_at) VALUES(?,?,?,?,?,?)",
            (int(project_id), int(watchlist_id) if watchlist_id else None,
             threshold, min_posts, (tg_chat_id or "").strip() or None,
             _iso_ms(int(time.time() * 1000))))
        return {"alert_id": cur.lastrowid}

    async def update_alert(self, alert_id: int, values: dict) -> bool:
        allowed = ("enabled", "threshold", "min_posts", "tg_chat_id")
        cols = [k for k in values if k in allowed]
        if not cols:
            return False
        cur = self.db.execute(
            f"UPDATE alerts SET {', '.join(f'{c} = ?' for c in cols)} "
            "WHERE alert_id = ?", [*(values[c] for c in cols), int(alert_id)])
        return cur.rowcount > 0

    async def delete_alert(self, alert_id: int) -> bool:
        return self.db.execute("DELETE FROM alerts WHERE alert_id = ?",
                               (int(alert_id),)).rowcount > 0

    async def alert_fired(self, alert_id: int, now_ms: int) -> None:
        self.db.execute("UPDATE alerts SET last_fired_ms = ? WHERE alert_id = ?",
                        (int(now_ms), int(alert_id)))

    async def alert_scope_streams(self, alert: dict) -> list:
        """The stream ids an alert watches — its watchlist's, or its project's."""
        if alert.get("watchlist_id"):
            return [r["stream_id"] for r in self.db.execute(
                "SELECT stream_id FROM streams WHERE label LIKE ?",
                (f"wl:{alert['watchlist_id']}:%",))]
        return await self.project_stream_ids(alert["project_id"])

    async def scope_velocity(self, stream_ids: list, now_ms: int) -> tuple:
        """
        (posts in the last hour, usual hourly pace) for a set of streams.

        The baseline is the trailing 24h ENDING AN HOUR AGO — the surge being
        measured must not sit inside its own yardstick, or a real spike
        halves its own ratio.
        """
        if not stream_ids:
            return 0, 0.0
        marks = ",".join("?" * len(stream_ids))
        hour_ago = now_ms - 3_600_000
        day_before = hour_ago - 86_400_000
        last_hour = self.db.execute(
            f"SELECT COUNT(DISTINCT tweet_id) c FROM tweet_hits "
            f"WHERE stream_id IN ({marks}) AND first_seen_ms > ?",
            [*stream_ids, hour_ago]).fetchone()["c"]
        prev = self.db.execute(
            f"SELECT COUNT(DISTINCT tweet_id) c FROM tweet_hits "
            f"WHERE stream_id IN ({marks}) AND first_seen_ms > ? AND first_seen_ms <= ?",
            [*stream_ids, day_before, hour_ago]).fetchone()["c"]
        return last_hour, prev / 24.0

    # ----------------------------------------------------------------------
    # webhook delivery
    # ----------------------------------------------------------------------
    #
    # Delivery is tracked as a CURSOR, not a queue of pending rows.
    #
    # A queue needs enqueueing on every insert, draining, retry counters per
    # row, and a cleanup job — and if the sender is down when a tweet arrives,
    # whether it is ever delivered depends on the enqueue having happened.
    # A cursor has none of that: "everything collected after this point" is a
    # query, so a receiver that was down for a day catches up by itself the
    # moment it comes back, and there is nothing to leak or prune.
    #
    # The cursor is (collected_ms, tweet_id), NOT tweet_id alone. Tweets do not
    # arrive in posting order — X indexes some late, so a tweet collected now
    # can have an older snowflake than one collected a minute ago. A tweet_id
    # cursor would step over those permanently, and the gap would be invisible.

    async def webhook_cursor(self, label: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM webhook_state WHERE label = ?", (label,)).fetchone()
        if row:
            return dict(row)
        return {"label": label, "last_ms": 0, "last_tweet_id": 0, "sent": 0,
                "failures": 0, "next_attempt_ms": 0, "last_error": None,
                "last_ok_ms": None}

    async def webhook_advance(self, label: str, last_ms: int, last_tweet_id: int,
                              sent: int) -> None:
        """Record a successful delivery. Only ever called after a 2xx."""
        now = int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO webhook_state(label, last_ms, last_tweet_id, sent, "
            "                          failures, next_attempt_ms, last_error, last_ok_ms) "
            "VALUES(?,?,?,?,0,0,NULL,?) "
            "ON CONFLICT(label) DO UPDATE SET "
            "  last_ms = excluded.last_ms, last_tweet_id = excluded.last_tweet_id, "
            "  sent = webhook_state.sent + excluded.sent, failures = 0, "
            "  next_attempt_ms = 0, last_error = NULL, last_ok_ms = excluded.last_ok_ms",
            (label, last_ms, last_tweet_id, sent, now))

    async def webhook_failed(self, label: str, error: str, next_attempt_ms: int) -> None:
        """Record a failure WITHOUT moving the cursor, so nothing is skipped."""
        self.db.execute(
            "INSERT INTO webhook_state(label, last_ms, last_tweet_id, sent, "
            "                          failures, next_attempt_ms, last_error) "
            "VALUES(?,0,0,0,1,?,?) "
            "ON CONFLICT(label) DO UPDATE SET "
            "  failures = webhook_state.failures + 1, "
            "  next_attempt_ms = excluded.next_attempt_ms, "
            "  last_error = excluded.last_error",
            (label, next_attempt_ms, error[:400]))

    async def webhook_start_here(self, label: str) -> None:
        """
        Point a brand-new endpoint at 'from now on'.

        Without this, adding a webhook to a database with months of history
        would immediately fire every tweet in it at the receiver. New endpoints
        want the future, not the archive; use --backfill to ask for the past
        deliberately.
        """
        row = self.db.execute(
            "SELECT MAX(collected_ms) m, MAX(tweet_id) t FROM tweets").fetchone()
        await self.webhook_advance(label, row["m"] or 0, row["t"] or 0, 0)
        self.db.execute("UPDATE webhook_state SET sent = 0 WHERE label = ?", (label,))

    async def tweets_after(self, last_ms: int, last_tweet_id: int, limit: int,
                           labels: list | None = None) -> list:
        """
        The next batch to deliver, oldest first.

        Strict ordering on the composite cursor: rows collected in the same
        millisecond are broken by tweet_id, so no row is ever visited twice and
        none is skipped.
        """
        where = ["t.source = 'result'",
                 "(t.collected_ms > ? OR (t.collected_ms = ? AND t.tweet_id > ?))"]
        params: list = [last_ms, last_ms, last_tweet_id]

        if labels:
            where.append(
                "EXISTS (SELECT 1 FROM tweet_hits h JOIN streams s USING(stream_id) "
                "        WHERE h.tweet_id = t.tweet_id AND s.label IN "
                f"       ({','.join('?' * len(labels))}))")
            params += list(labels)

        rows = self.db.execute(
            f"SELECT * FROM tweets t WHERE {' AND '.join(where)} "
            "ORDER BY t.collected_ms, t.tweet_id LIMIT ?", [*params, limit]
        ).fetchall()
        return [dict(r) for r in rows]

    async def stream_labels_for(self, tweet_id: int) -> list:
        return [r["label"] for r in self.db.execute(
            "SELECT s.label FROM tweet_hits h JOIN streams s USING(stream_id) "
            "WHERE h.tweet_id = ? ORDER BY s.label", (tweet_id,))]

    # ----------------------------------------------------------------------
    # per-stream settings and removal
    # ----------------------------------------------------------------------

    SETTINGS = ("paused", "min_interval_s", "max_pages_per_poll",
                "tg_enabled", "tg_chat_id", "tg_min_likes", "tg_skip_retweets")

    async def stream_settings(self, label: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM streams WHERE label = ?", (label,)).fetchone()
        return dict(row) if row else {}

    async def set_stream_settings(self, label: str, values: dict) -> bool:
        """
        Update tuning for one stream. Only known keys, only if it exists.

        NULL is meaningful here and is not the same as 0: it means "no override,
        use whatever config.toml says". Clearing a box in the dashboard has to
        put the setting back to inheriting, not pin it to zero — a
        min_interval_s of 0 would poll as fast as the loop can turn.
        """
        cols = [k for k in values if k in self.SETTINGS]
        if not cols:
            return False
        sets = ", ".join(f"{c} = ?" for c in cols)
        cur = self.db.execute(
            f"UPDATE streams SET {sets} WHERE label = ?",
            [*(values[c] for c in cols), label])
        return bool(cur.rowcount)

    async def forget_stream(self, label: str, delete_tweets: bool = False) -> dict:
        """
        Remove a stream. Two very different operations behind one door.

        delete_tweets=False — stop watching. The stream row and its links go;
        every tweet stays and is still searchable and still served by the API.
        Reversible: re-adding the same query reattaches to the same history.

        delete_tweets=True — destroy the data too. Only tweets that NO other
        stream also matched are removed, because a tweet shared with a stream
        you are keeping is that stream's data as well; deleting it would punch
        a hole in a list you never touched.

        X's index only reaches back about a week, so anything removed here that
        is older than that cannot be collected again. Ever. That is why this
        needs a typed confirmation in the UI and why it reports exactly what it
        removed rather than saying "done".
        """
        row = self.db.execute(
            "SELECT stream_id FROM streams WHERE label = ?", (label,)).fetchone()
        if not row:
            return {"found": False}
        sid = row["stream_id"]

        removed = 0
        if delete_tweets:
            removed = self.db.execute(
                "SELECT COUNT(*) c FROM tweets WHERE source = 'result' AND tweet_id IN ("
                "  SELECT tweet_id FROM tweet_hits WHERE stream_id = ?"
                "  EXCEPT SELECT tweet_id FROM tweet_hits WHERE stream_id != ?)",
                (sid, sid)).fetchone()["c"]
            self.db.execute(
                "DELETE FROM tweets WHERE tweet_id IN ("
                "  SELECT tweet_id FROM tweet_hits WHERE stream_id = ?"
                "  EXCEPT SELECT tweet_id FROM tweet_hits WHERE stream_id != ?)",
                (sid, sid))

        for table in ("tweet_hits", "polls", "watermarks", "gaps"):
            self.db.execute(f"DELETE FROM {table} WHERE stream_id = ?", (sid,))
        self.db.execute("DELETE FROM streams WHERE stream_id = ?", (sid,))
        return {"found": True, "label": label, "tweets_deleted": removed}

    async def mark_first_poll(self, stream_id: int, ms: int) -> None:
        self.db.execute(
            "UPDATE streams SET first_poll_ms = COALESCE(first_poll_ms, ?) WHERE stream_id = ?",
            (ms, stream_id),
        )

    async def first_poll_ms(self, stream_id: int) -> int | None:
        row = self.db.execute(
            "SELECT first_poll_ms FROM streams WHERE stream_id = ?", (stream_id,)
        ).fetchone()
        return row["first_poll_ms"] if row else None

    # ---------------- tweets ----------------

    async def upsert_tweets(self, rows, stream_id: int | None, poll_id: int | None) -> UpsertCounts:
        """
        rows: iterable of (tweet, page, source, entry).

        collected_at / collected_ms / lag_ms are written once and never
        updated — a tweet re-seen on a later poll must not look fresher than it
        was. Engagement counters and raw_json DO update, because those change.
        """
        counts = UpsertCounts()
        for tweet, page, source, entry in rows:
            tid = int(tweet.id)
            rec = normalize_tweet(tweet)
            collected_ms = page.collected_ms
            created_ms = id_to_ms(tid) if is_snowflake(tid) else collected_ms
            lag = lag_ms(tid, collected_ms) if is_snowflake(tid) else 0

            try:
                raw_json = tweet.json()
            except Exception:
                raw_json = None

            payload = {
                "tweet_id": tid,
                "created_at": rec["created_at"],
                "created_ms": created_ms,
                "collected_at": _iso_ms(collected_ms),
                "collected_ms": collected_ms,
                "last_seen_at": _iso_ms(collected_ms),
                "lag_ms": lag,
                "url": rec["url"],
                "text": rec["text"],
                "lang": rec["lang"],
                "author_username": rec["author_username"],
                "author_display_name": rec["author_display_name"],
                "author_id": rec["author_id"],
                "author_followers": rec["author_followers"],
                "reply_count": rec["reply_count"],
                "retweet_count": rec["retweet_count"],
                "like_count": rec["like_count"],
                "quote_count": rec["quote_count"],
                "view_count": rec["view_count"],
                "bookmark_count": getattr(tweet, "bookmarkedCount", None),
                "is_retweet": int(bool(rec["is_retweet"])),
                "is_reply": int(bool(rec["is_reply"])),
                "is_quote": int(bool(rec["is_quote"])),
                "in_reply_to": rec["in_reply_to"],
                "conversation_id": rec["conversation_id"],
                "source": source,
                "raw_json": raw_json,
                "raw_entry_json": (json.dumps(entry) if (entry and self.keep_entry_json) else None),
                "media_json": json.dumps(rec.get("media") or []),
                **{k: json.dumps(rec[k] or []) for k in LIST_FIELDS},
            }

            # ON CONFLICT DO UPDATE reports rowcount 1 for both branches, so
            # newness has to be established before the write.
            existed = (
                self.db.execute(
                    "SELECT 1 FROM tweets WHERE tweet_id = ?", (tid,)
                ).fetchone()
                is not None
            )

            cols = list(payload)
            self.db.execute(
                f"INSERT INTO tweets ({','.join(cols)}) "
                f"VALUES ({','.join(':' + c for c in cols)}) "
                "ON CONFLICT(tweet_id) DO UPDATE SET "
                "  last_seen_at   = excluded.last_seen_at,"
                "  reply_count    = excluded.reply_count,"
                "  retweet_count  = excluded.retweet_count,"
                "  like_count     = excluded.like_count,"
                "  quote_count    = excluded.quote_count,"
                "  view_count     = excluded.view_count,"
                "  bookmark_count = excluded.bookmark_count,"
                "  raw_json       = COALESCE(excluded.raw_json, tweets.raw_json),"
                # A tweet first seen as quoted context can later turn up as a
                # real search hit. Promote it; never demote.
                "  source = CASE WHEN tweets.source = 'embedded' AND excluded.source = 'result' "
                "                THEN 'result' ELSE tweets.source END",
                payload,
            )

            if source == "embedded":
                counts.embedded += 1
                continue

            if stream_id is None:
                # One-shot search: newness is global.
                counts.new += 0 if existed else 1
                counts.dup += 1 if existed else 0
                continue

            # Normal path: "new" means new TO THIS STREAM. A tweet already
            # collected by another stream is still new here, and the hit edge
            # is what the watermark and the lag report key off.
            hit = self.db.execute(
                "INSERT OR IGNORE INTO tweet_hits(stream_id, tweet_id, poll_id, first_seen_ms) "
                "VALUES(?,?,?,?)",
                (stream_id, tid, poll_id, collected_ms),
            )
            if hit.rowcount == 1:
                counts.new += 1
            else:
                counts.dup += 1
        return counts

    async def has_tweet(self, tweet_id: int) -> bool:
        return (
            self.db.execute("SELECT 1 FROM tweets WHERE tweet_id = ?", (tweet_id,)).fetchone()
            is not None
        )

    async def count_tweets(self, stream_id: int | None = None) -> int:
        if stream_id is None:
            row = self.db.execute("SELECT COUNT(*) c FROM tweets").fetchone()
        else:
            row = self.db.execute(
                "SELECT COUNT(*) c FROM tweet_hits WHERE stream_id = ?", (stream_id,)
            ).fetchone()
        return row["c"]

    # ---------------- watermarks ----------------

    async def get_watermark(self, stream_id: int):
        return self.db.execute(
            "SELECT * FROM watermarks WHERE stream_id = ?", (stream_id,)
        ).fetchone()

    async def set_watermark(self, stream_id: int, high_tweet_id: int, **state) -> None:
        high_ms = id_to_ms(high_tweet_id) if is_snowflake(high_tweet_id) else 0
        now = int(time.time() * 1000)
        cols = {
            "stream_id": stream_id,
            "high_tweet_id": high_tweet_id,
            "high_created_ms": high_ms,
            "interval_s": state.get("interval_s", 30.0),
            "ewma_rate": state.get("ewma_rate", 0.0),
            "consecutive_empty": state.get("consecutive_empty", 0),
            "next_poll_ms": state.get("next_poll_ms", now),
            "updated_at": _iso_ms(now),
        }
        names = list(cols)
        self.db.execute(
            f"INSERT INTO watermarks ({','.join(names)}) "
            f"VALUES ({','.join(':' + n for n in names)}) "
            "ON CONFLICT(stream_id) DO UPDATE SET "
            # max() guards against a watermark ever moving backwards, which
            # would silently re-collect and re-report old tweets as new.
            "  high_tweet_id     = max(watermarks.high_tweet_id, excluded.high_tweet_id),"
            "  high_created_ms   = max(watermarks.high_created_ms, excluded.high_created_ms),"
            "  interval_s        = excluded.interval_s,"
            "  ewma_rate         = excluded.ewma_rate,"
            "  consecutive_empty = excluded.consecutive_empty,"
            "  next_poll_ms      = excluded.next_poll_ms,"
            "  updated_at        = excluded.updated_at",
            cols,
        )

    # ---------------- polls ----------------

    async def begin_poll(self, stream_id: int, kind: str = "poll") -> int:
        cur = self.db.execute(
            "INSERT INTO polls(stream_id, kind, started_ms) VALUES(?,?,?)",
            (stream_id, kind, int(time.time() * 1000)),
        )
        return cur.lastrowid

    async def finish_poll(self, poll_id: int, **fields) -> None:
        lags = fields.pop("lags", None)
        if lags:
            fields["lag_p50_ms"] = _percentile(lags, 50)
            fields["lag_p95_ms"] = _percentile(lags, 95)
        fields["finished_ms"] = int(time.time() * 1000)
        sets = ",".join(f"{k} = :{k}" for k in fields)
        self.db.execute(
            f"UPDATE polls SET {sets} WHERE poll_id = :poll_id", {**fields, "poll_id": poll_id}
        )

    async def recent_polls(self, stream_id: int, limit: int = 20):
        return self.db.execute(
            "SELECT * FROM polls WHERE stream_id = ? ORDER BY started_ms DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()

    # ---------------- gaps ----------------

    async def open_gap(self, stream_id, lo, hi, cursor=None, poll_id=None) -> int:
        now = int(time.time() * 1000)
        cur = self.db.execute(
            "INSERT INTO gaps(stream_id, lo_tweet_id, hi_tweet_id, lo_ms, hi_ms, "
            "resume_cursor, cursor_ms, detected_poll, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                stream_id, lo, hi,
                id_to_ms(lo) if is_snowflake(lo) else 0,
                id_to_ms(hi) if is_snowflake(hi) else 0,
                cursor, now, poll_id, _iso_ms(now),
            ),
        )
        return cur.lastrowid

    async def open_gaps(self, stream_id: int | None = None):
        if stream_id is None:
            return self.db.execute(
                "SELECT g.*, s.label FROM gaps g JOIN streams s USING(stream_id) "
                "WHERE g.status = 'open' ORDER BY g.hi_tweet_id DESC"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM gaps WHERE status = 'open' AND stream_id = ? ORDER BY hi_tweet_id DESC",
            (stream_id,),
        ).fetchall()

    # ---------------- reporting ----------------

    async def lag_report(self, since: str = "24h") -> list[str]:
        since_ms = parse_window(since) or 0
        out: list[str] = []

        rows = self.db.execute(
            "SELECT s.label, s.first_poll_ms, COUNT(*) n "
            "FROM tweet_hits h "
            "JOIN tweets t USING(tweet_id) JOIN streams s USING(stream_id) "
            "WHERE h.first_seen_ms >= ? "
            "GROUP BY s.label ORDER BY s.label",
            (since_ms,),
        ).fetchall()

        if not rows:
            out.append("(no tweets collected in this window)")

        for r in rows:
            # Tweets that already existed when this stream started watching are
            # backlog, not a freshness signal: their "lag" is however long they
            # happened to predate us. Counting them would make p95 meaningless.
            # They are still reported, just separately.
            lags = [
                x["lag_ms"]
                for x in self.db.execute(
                    "SELECT t.lag_ms FROM tweet_hits h JOIN tweets t USING(tweet_id) "
                    "JOIN streams s USING(stream_id) "
                    "WHERE s.label = ? AND h.first_seen_ms >= ? "
                    "  AND t.created_ms >= COALESCE(s.first_poll_ms, 0)",
                    (r["label"], since_ms),
                ).fetchall()
            ]
            backlog = r["n"] - len(lags)
            if lags:
                out.append(
                    f"{r['label']}: n={len(lags)}  "
                    f"p50={_percentile(lags, 50) / 1000:.1f}s  "
                    f"p95={_percentile(lags, 95) / 1000:.1f}s  "
                    f"max={max(lags) / 1000:.1f}s"
                    + (f"   (+{backlog} backlog, excluded)" if backlog else "")
                )
            else:
                out.append(
                    f"{r['label']}: n={r['n']} collected, but none measurable for lag yet "
                    f"— all of them predate this stream's first poll. "
                    f"Expected right after starting; real numbers appear once new "
                    f"tweets arrive."
                )

        stops = self.db.execute(
            "SELECT s.label, p.stop_reason, COUNT(*) n, AVG(p.pages) pages "
            "FROM polls p JOIN streams s USING(stream_id) "
            "WHERE p.started_ms >= ? AND p.stop_reason IS NOT NULL "
            "GROUP BY s.label, p.stop_reason ORDER BY s.label, n DESC",
            (since_ms,),
        ).fetchall()
        if stops:
            out.append("")
            out.append("poll outcomes:")
            for r in stops:
                note = ""
                if r["stop_reason"] == "no_account_or_abort":
                    note = "   <-- pool starvation, NOT a quiet stream"
                elif r["stop_reason"] == "page_budget":
                    note = "   <-- stream outran the poller; raise max_pages or poll faster"
                out.append(
                    f"  {r['label']}: {r['stop_reason']} x{r['n']} "
                    f"(avg {r['pages']:.1f} pages){note}"
                )

        gaps = await self.open_gaps()
        if gaps:
            out.append("")
            out.append(f"open gaps: {len(gaps)} (windows the poller did not reach)")
        return out

    # ---------------- export ----------------

    def iter_export(self, stream=None, since=None, until=None, limit=None,
                    order="desc", include_embedded=False):
        where, params = [], []
        joins = ""
        if stream:
            joins = "JOIN tweet_hits h USING(tweet_id) JOIN streams s USING(stream_id)"
            where.append("s.label = ?")
            params.append(stream)
        if not include_embedded:
            where.append("t.source = 'result'")
        if since is not None:
            where.append("t.created_ms >= ?")
            params.append(since)
        if until is not None:
            where.append("t.created_ms <= ?")
            params.append(until)

        # stream_label is declared in FIELDS_EXT, so `--fields all` writes the
        # column — and nothing ever filled it, so every export carried a header
        # with nothing beneath it. A tweet can match several streams, so this is
        # a comma-joined list rather than a single label.
        sql = ("SELECT t.*, (SELECT GROUP_CONCAT(s2.label, ',') "
               "             FROM tweet_hits h2 JOIN streams s2 USING(stream_id) "
               "             WHERE h2.tweet_id = t.tweet_id) AS stream_label "
               f"FROM tweets t {joins}")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY t.tweet_id {'ASC' if order == 'asc' else 'DESC'}"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.db.execute(sql, params)

# ==========================================================================
# export writers
# ==========================================================================

# Columns that only exist once a tweet has been through the store.
_EXTRA = [f for f in FIELDS_EXT if f not in FIELDS]


def _fields_for(profile: str) -> list[str]:
    return FIELDS_EXT if profile == "all" else FIELDS


def _record(row, fields):
    rec = from_store_row(row, fields)
    keys = row.keys()
    for k in _EXTRA:
        if k in fields:
            rec[k] = row[k] if k in keys else None
    return rec


def write_json(rows, path, profile="default"):
    fields = _fields_for(profile)
    records = [_record(r, fields) for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return len(records)


def write_jsonl(rows, path, profile="default"):
    fields = _fields_for(profile)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(_record(row, fields), ensure_ascii=False) + "\n")
            n += 1
    return n


def write_csv(rows, path, profile="default"):
    fields = _fields_for(profile)
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(to_csv_row(_record(row, fields), fields))
            n += 1
    return n


def write_raw(rows, path):
    """
    The untouched Tweet.json() for each tweet, one per line.

    This is the source of truth. It carries every field the normalizer drops
    (bookmarkedCount, cashtags, place, source, card, edit history), which is
    what makes it possible to reparse history after X changes its schema
    instead of having to re-scrape it.
    """
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            raw = row["raw_json"] if "raw_json" in row.keys() else None
            if raw:
                f.write(raw + "\n")
                n += 1
    return n


def export(rows, out_prefix, fmt="csv", profile="default"):
    """Write `rows` in `fmt`. Returns (path, count)."""
    writers = {
        "json": (f"{out_prefix}.json", write_json),
        "jsonl": (f"{out_prefix}.jsonl", write_jsonl),
        "csv": (f"{out_prefix}.csv", write_csv),
    }
    if fmt == "raw":
        path = f"{out_prefix}.raw.jsonl"
        return path, write_raw(rows, path)
    path, fn = writers[fmt]
    return path, fn(rows, path, profile)


def write_legacy_outputs(records, raw_tweets, out_prefix):
    """
    The prototype's three-file output, byte-compatible.

    Kept so `search --no-store` behaves exactly as before for anyone with
    scripts pointed at results.json / results.csv / results.raw.jsonl.
    """
    json_path = f"{out_prefix}.json"
    csv_path = f"{out_prefix}.csv"
    raw_path = f"{out_prefix}.raw.jsonl"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow(to_csv_row(rec))

    with open(raw_path, "w", encoding="utf-8") as f:
        for tweet in raw_tweets:
            try:
                f.write(tweet.json() + "\n")
            except Exception:
                f.write(json.dumps(getattr(tweet, "__dict__", str(tweet)), default=str) + "\n")

    return json_path, csv_path, raw_path