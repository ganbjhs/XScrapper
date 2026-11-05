"""
search.py — the dashboard's deep search over everything already collected.

GET /api/search (web.py) is a thin wrapper around run() below. One search over
the archive, platform-wise (All / X / Instagram / Facebook), driven by
X-Advanced-Search-style boxes instead of a query syntax. Nothing here calls a
platform: it reads the databases the collectors wrote, so it spends no budget.

How it stays cheap on one vCPU (RULEBOOK §7, "Search"):

  * Text never goes through LIKE '%word%'. X uses the existing tweets_fts
    (store.py). Instagram and Facebook get their own FTS5 index, created here
    ONCE and kept current by TRIGGERS inside each database, so the collectors'
    code is untouched (ensure_fts).
  * FTS drives: `FROM fts CROSS JOIN posts`, ORDER BY the index rowid, keyset
    paging on that rowid. On X the rowid IS the snowflake tweet_id, so an IST
    date range becomes a rowid range and the index only walks those days.
  * Hard caps on every request: 50 per page, 5,000 matches counted per
    platform ("5,000+"), 20,000 rows looked at, 1.5 s per platform via
    set_progress_handler. Hitting the deadline returns what was found with
    partial=true — never a stuck CPU.
  * FTS is fuzzy, so every candidate row gets a precise check in Python:
    #bihar is not #biharelection or the word "bihar", from:/mentioning compare
    the handle exactly (case-insensitive), phrases are whole-token sequences.
  * Only the 50 rows on the page are hydrated into full posts (web.py does
    that with the same row → post code the other endpoints use).

Hindi / Devanagari: the Instagram and Facebook indexes use
unicode61 with combining marks as token characters, so "बिहार" is one token.
X's tweets_fts predates this and uses plain unicode61, which splits Devanagari
at every vowel sign (ब ह र). It is deliberately NOT rebuilt (a rebuild of the
live X index on the server is a heavy one-off): its matches are a superset of
the real ones, and the precise check below — which tokenizes Devanagari
correctly — throws the false ones away. Correct results; a common Hindi word
on X just looks at more rows (and can come back partial). RULEBOOK §7 records it.
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import sqlite3
import threading
import time

PAGE = 50
MAX_COUNT = 5000          # matches counted / sorted per platform; shown "5,000+"
MAX_SCAN = 20000          # candidate rows looked at per platform per request
DEADLINE_S = 1.5          # per platform
EXPORT_MAX = 5000
X_EPOCH_MS = 1288834974657
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PLATFORMS = ("x", "instagram", "facebook")
NAMES = {"x": "X", "instagram": "Instagram", "facebook": "Facebook"}
# Combining marks count as part of a word, so Devanagari stays whole.
TOKENIZE = "unicode61 categories 'L* N* Co M*'"


class BadRequest(ValueError):
    """A request the endpoint answers with 400."""


# --------------------------------------------------------------------------
# Tokens — one definition for the precise check
# --------------------------------------------------------------------------
# Letters and digits ([^\W_]) plus the combining-mark blocks Python's \w
# leaves out (Devanagari matras and viramas, Arabic harakat, Latin accents).
_MARKS = ("̀-ͯ҃-҉֑-ׇؐ-ًؚ-ٟ"
          "ٰۖ-ۭऀ-෿᪰-᫿᷀-᷿"
          "⃐-⃿︠-︯")
_TOK = re.compile(rf"(?:[^\W_]|[{_MARKS}])+")
_TAG = re.compile(rf"#((?:\w|[{_MARKS}])+)")
_MENTION = re.compile(r"@([A-Za-z0-9_.]+)")
_PROFILE = re.compile(r"(?:x|twitter|instagram|facebook)\.com/([^/?#\s]+)", re.I)


def tokens(s) -> list:
    return [t.lower() for t in _TOK.findall(str(s or ""))]


def _joined(toks) -> str:
    return " " + " ".join(toks) + " "


def _has_seq(hay: str, seq) -> bool:
    """hay is _joined(tokens(text)); seq a token list — whole-token phrase."""
    return _joined(seq) in hay


def text_tags(text) -> set:
    return {t.lower() for t in _TAG.findall(str(text or ""))}


def text_mentions(text) -> set:
    return {m.lower().rstrip(".") for m in _MENTION.findall(str(text or ""))}


def _json_list(v) -> list:
    try:
        out = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x.get("username") if isinstance(x, dict) else x) for x in out or []]


# --------------------------------------------------------------------------
# The boxes → a spec
# --------------------------------------------------------------------------

def _items(v) -> list:
    """Comma-separated box → stripped, non-empty items."""
    return [s.strip() for s in re.split(r"[,،，\n]", str(v or "")) if s.strip()]


def _handle(v) -> str:
    a = str(v or "").strip()
    m = _PROFILE.search(a)
    if m:
        a = m.group(1)
    return a.lstrip("@").strip().strip("/").lower()


def _int(p, k):
    v = p.get(k)
    if v in (None, ""):
        return None
    try:
        n = int(str(v).replace(",", "").strip())
    except ValueError:
        raise BadRequest(f"{k} must be a whole number")
    if n < 0:
        raise BadRequest(f"{k} cannot be negative")
    return n


def _flag(p, k) -> bool:
    return str(p.get(k) or "").lower() in ("1", "true", "yes", "on")


def _ist_day(v, k):
    """yyyy-mm-dd → midnight IST in ms, or None. A bad date is a 400."""
    if v in (None, ""):
        return None
    try:
        d = datetime.datetime.strptime(str(v).strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise BadRequest(f"{k} must be yyyy-mm-dd")
    return int(d.replace(tzinfo=IST).timestamp() * 1000)


X_TYPES = ("original", "reply", "quote", "repost")
IG_TYPES = ("photo", "video", "reel", "album")
X_ONLY = ("x_types", "lang", "verified", "min_followers", "min_reposts", "has_link")
IG_ONLY = ("ig_types",)
FB_ONLY = ("min_shares",)


def _phrase_items(raw_items):
    """[(raw, tokens)] — an item with no word characters is dropped."""
    out = []
    for raw in raw_items:
        t = tokens(raw)
        if t:
            out.append((raw, t))
    return out


def parse(p: dict) -> dict:
    platform = (p.get("platform") or "all").strip().lower()
    if platform not in ("all",) + PLATFORMS:
        raise BadRequest("platform must be all, x, instagram or facebook")
    sort = (p.get("sort") or "latest").strip().lower()
    if sort not in ("latest", "oldest"):
        raise BadRequest("sort must be latest or oldest")
    words = []
    for it in _items(p.get("all")):
        words += it.split()
    spec = {
        "platform": platform,
        "sort": sort,
        "all": _phrase_items(words),
        "phrase": _phrase_items(_items(p.get("phrase"))),
        "any": _phrase_items(_items(p.get("any"))),
        "none": _phrase_items(_items(p.get("none"))),
        "tags": [t for t in (i.lstrip("#").strip().lower() for i in _items(p.get("tags"))) if tokens(t)],
        "from": [h for h in (_handle(i) for i in _items(p.get("from"))) if h],
        "mention": [h for h in (_handle(i) for i in _items(p.get("mention"))) if h],
        "min_likes": _int(p, "min_likes"),
        "min_comments": _int(p, "min_comments"),
        "min_views": _int(p, "min_views"),
        "has_media": _flag(p, "has_media"),
        "no_reposts": _flag(p, "no_reposts"),
        "no_replies": _flag(p, "no_replies"),
        "from_ms": _ist_day(p.get("from_date"), "from_date"),
        "to_ms": None,
        "notes": [],
    }
    to = _ist_day(p.get("to_date"), "to_date")
    if to is not None:
        spec["to_ms"] = to + 86_400_000          # the whole IST day, inclusive
    if spec["from_ms"] and spec["to_ms"] and spec["from_ms"] >= spec["to_ms"]:
        raise BadRequest("from_date is after to_date")

    # Platform-only filters apply only when that ONE platform is chosen.
    x_types = [t for t in _items(p.get("x_types")) if t in X_TYPES]
    ig_types = [t for t in _items(p.get("ig_types")) if t in IG_TYPES]
    spec.update({
        "x_types": x_types if platform == "x" else [],
        "lang": (p.get("lang") or "").strip().lower() if platform == "x" else "",
        "verified": _flag(p, "verified") if platform == "x" else False,
        "min_followers": _int(p, "min_followers") if platform == "x" else None,
        "min_reposts": _int(p, "min_reposts") if platform == "x" else None,
        "has_link": _flag(p, "has_link") if platform == "x" else False,
        "ig_types": ig_types if platform == "instagram" else [],
        "min_shares": _int(p, "min_shares") if platform == "facebook" else None,
    })
    for plat, keys in (("x", X_ONLY), ("instagram", IG_ONLY), ("facebook", FB_ONLY)):
        sent = [k for k in keys if p.get(k) not in (None, "", "0")]
        if sent and platform != plat:
            spec["notes"].append(f"{', '.join(sent)}: {NAMES[plat]}-only — applied only "
                                 f"when {NAMES[plat]} is chosen, ignored here")
    spec["positive"] = bool(spec["all"] or spec["phrase"] or spec["any"]
                            or spec["tags"] or spec["from"] or spec["mention"])
    return spec


def left_out(spec) -> dict:
    """{platform: why} — platforms that cannot answer a filter. Never guessed."""
    out = {}
    if spec["min_views"]:
        out["facebook"] = "Facebook gives no view counts, so it cannot answer min views"
    return out


# --------------------------------------------------------------------------
# FTS expression
# --------------------------------------------------------------------------

def _q(raw: str) -> str:
    return '"' + raw.replace('"', '""') + '"'


def fts_expr(spec, text_col, author_col, safe_not) -> str | None:
    """The MATCH string for the positive boxes, or None when there are none.
    `safe_not`: push "None of these" into FTS only when the index tokenizes
    like the precise check (else FTS could drop a row that is fine)."""
    parts = [f"{text_col} : {_q(r)}" for r, _ in spec["all"] + spec["phrase"]]

    def any_of(col, raws):
        if raws:
            parts.append("(" + " OR ".join(f"{col} : {_q(r)}" for r in raws) + ")")
    any_of(text_col, [r for r, _ in spec["any"]])
    any_of(text_col, spec["tags"])
    any_of(text_col, spec["mention"])
    any_of(author_col, spec["from"])
    if not parts:
        return None
    expr = " AND ".join(parts)
    for raw, _ in spec["none"]:
        if safe_not(raw):
            expr = f"({expr}) NOT {text_col} : {_q(raw)}"
    return expr


def _ascii(raw):
    return raw.isascii()


def text_ok(spec, text, tags, mentions, handle) -> bool:
    """The precise check on one candidate row."""
    if spec["from"] and (handle or "").lower() not in spec["from"]:
        return False
    if spec["tags"] and not (set(tags) & set(spec["tags"])):
        return False
    if spec["mention"] and not (set(mentions) & set(spec["mention"])):
        return False
    if spec["all"] or spec["phrase"] or spec["any"] or spec["none"]:
        hay = _joined(tokens(text))
        for _, t in spec["all"] + spec["phrase"]:
            if not _has_seq(hay, t):
                return False
        if spec["any"] and not any(_has_seq(hay, t) for _, t in spec["any"]):
            return False
        for _, t in spec["none"]:
            if _has_seq(hay, t):
                return False
    return True


def _ge(v, n):
    return n is None or (v is not None and v >= n)


# --------------------------------------------------------------------------
# Indexes for Instagram and Facebook — created once, kept by triggers
# --------------------------------------------------------------------------

_FTS_LOCK = threading.Lock()
_FTS_READY: dict = {}       # str(path) -> tokenizer_is_hindi_safe (bool)

_IG_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS ig_posts_fts_ai AFTER INSERT ON posts BEGIN
  INSERT INTO ig_posts_fts(rowid, caption, username) VALUES (new.pk, new.caption, new.username);
END;
CREATE TRIGGER IF NOT EXISTS ig_posts_fts_ad AFTER DELETE ON posts BEGIN
  INSERT INTO ig_posts_fts(ig_posts_fts, rowid, caption, username)
  VALUES ('delete', old.pk, old.caption, old.username);
END;
CREATE TRIGGER IF NOT EXISTS ig_posts_fts_au AFTER UPDATE OF caption, username ON posts
WHEN old.caption IS NOT new.caption OR old.username IS NOT new.username BEGIN
  INSERT INTO ig_posts_fts(ig_posts_fts, rowid, caption, username)
  VALUES ('delete', old.pk, old.caption, old.username);
  INSERT INTO ig_posts_fts(rowid, caption, username) VALUES (new.pk, new.caption, new.username);
END;
"""

# Facebook's key is TEXT, so the index keeps its own copy keyed by post_id
# (UNINDEXED): the hidden rowid of `posts` can be renumbered by a VACUUM.
_FB_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS fb_posts_fts_ai AFTER INSERT ON posts BEGIN
  INSERT INTO fb_posts_fts(post_id, text, page) VALUES (new.post_id, new.text, new.page);
END;
CREATE TRIGGER IF NOT EXISTS fb_posts_fts_ad AFTER DELETE ON posts BEGIN
  DELETE FROM fb_posts_fts WHERE post_id = old.post_id;
END;
CREATE TRIGGER IF NOT EXISTS fb_posts_fts_au AFTER UPDATE OF post_id, text, page ON posts
WHEN old.post_id IS NOT new.post_id OR old.text IS NOT new.text OR old.page IS NOT new.page BEGIN
  DELETE FROM fb_posts_fts WHERE post_id = old.post_id;
  INSERT INTO fb_posts_fts(post_id, text, page) VALUES (new.post_id, new.text, new.page);
END;
"""


def ensure_fts(path, kind) -> bool | None:
    """Create ig_posts_fts / fb_posts_fts once (index + triggers + backfill, in
    one transaction). Returns whether the tokenizer is Hindi-safe, or None
    when no index could be made (the search then scans, capped, without FTS)."""
    key = str(path)
    if key in _FTS_READY:
        return _FTS_READY[key]
    with _FTS_LOCK:
        if key in _FTS_READY:
            return _FTS_READY[key]
        name = "ig_posts_fts" if kind == "instagram" else "fb_posts_fts"
        cols = ("caption, username, content='posts', content_rowid='pk'"
                if kind == "instagram" else "post_id UNINDEXED, text, page")
        con = sqlite3.connect(str(path), timeout=15)
        try:
            row = con.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                              (name,)).fetchone()
            if row is None:
                con.execute("BEGIN IMMEDIATE")
                try:
                    try:
                        con.execute(f"CREATE VIRTUAL TABLE {name} USING fts5({cols}, "
                                    f"tokenize=\"{TOKENIZE}\")")
                    except sqlite3.OperationalError:
                        # An sqlite too old for `categories`: plain unicode61.
                        con.execute(f"CREATE VIRTUAL TABLE {name} USING fts5({cols})")
                    for stmt in _split(_IG_TRIGGERS if kind == "instagram" else _FB_TRIGGERS):
                        con.execute(stmt)
                    if kind == "instagram":
                        con.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
                    else:
                        con.execute(f"INSERT INTO {name}(post_id, text, page) "
                                    f"SELECT post_id, text, page FROM posts")
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
                row = con.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                                  (name,)).fetchone()
            else:
                # The index exists: make sure its triggers do too.
                for stmt in _split(_IG_TRIGGERS if kind == "instagram" else _FB_TRIGGERS):
                    con.execute(stmt)
                con.commit()
            safe = "categories" in (row[0] or "")
        except (sqlite3.Error, AttributeError):
            safe = None
        finally:
            con.close()
        if safe is not None:
            _FTS_READY[key] = safe
        return safe


def _split(script):
    """Trigger statements one by one (each ends with END;)."""
    return [s.strip() + "\nEND;" for s in script.split("END;") if s.strip()]


# --------------------------------------------------------------------------
# One platform: walk candidates under the caps
# --------------------------------------------------------------------------

class _Deadline:
    def __init__(self, seconds):
        self.at = time.monotonic() + seconds
        self.hit = False

    def __call__(self):
        if time.monotonic() > self.at:
            self.hit = True
            return 1
        return 0


def _walk(con, sql, args, keep, want, count, deadline_s):
    """
    Iterate candidate rows (already in sort order), keep the ones `keep`
    accepts. Returns dict(matches, n, capped, partial, exhausted, last).
    `count`: keep counting past the page, up to MAX_COUNT (first page only).
    """
    dl = _Deadline(deadline_s)
    con.set_progress_handler(dl, 2000)
    matches, n, looked, last = [], 0, 0, None
    exhausted, stopped = False, False
    try:
        cur = con.execute(sql, args)
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                exhausted = True
                break
            if dl():
                break
            for r in rows:
                looked += 1
                last = r
                m = keep(r)
                if m is None:
                    continue
                n += 1
                if len(matches) < want:
                    matches.append(m)
                if (not count and len(matches) >= want) or n >= MAX_COUNT:
                    stopped = True
                    break
            if stopped:
                break
    except sqlite3.OperationalError as e:
        if not dl.hit and "interrupt" not in str(e).lower():
            raise
    finally:
        con.set_progress_handler(None, 0)
    if looked >= MAX_SCAN and not stopped:
        exhausted = False
    return {"matches": matches, "n": n, "partial": dl.hit,
            "capped": looked >= MAX_SCAN or n >= MAX_COUNT,
            "exhausted": exhausted and not dl.hit, "last": last}


def _ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _bounds(spec, cur):
    """(lo, hi) inclusive on a time-ordered id, after the keyset cursor."""
    lo, hi = 0, (1 << 63) - 1
    if spec.get("_lo") is not None:
        lo = spec["_lo"]
    if spec.get("_hi") is not None:
        hi = spec["_hi"]
    if cur is not None:
        if spec["sort"] == "latest":
            hi = min(hi, int(cur) - 1)
        else:
            lo = max(lo, int(cur) + 1)
    return lo, hi


def _common_ok(spec, likes, comments, views, has_media, repost, reply):
    return (_ge(likes, spec["min_likes"]) and _ge(comments, spec["min_comments"])
            and _ge(views, spec["min_views"])
            and (not spec["has_media"] or has_media)
            and not (spec["no_reposts"] and repost)
            and not (spec["no_replies"] and reply))


def search_x(path, spec, scope, cursor, want, count, deadline_s):
    con = _ro(path)
    try:
        order = "DESC" if spec["sort"] == "latest" else "ASC"
        x = dict(spec)
        if spec["from_ms"] is not None:
            x["_lo"] = (spec["from_ms"] - X_EPOCH_MS) << 22
        if spec["to_ms"] is not None:
            x["_hi"] = ((spec["to_ms"] - X_EPOCH_MS) << 22) - 1
        lo, hi = _bounds(x, cursor)
        where, args = ["t.source = 'result'"], []
        if scope != "all":
            sids = [r[0] for r in con.execute(
                "SELECT stream_id FROM project_streams WHERE project_id = ?", (scope,))]
            if not sids:
                return {"matches": [], "n": 0, "partial": False, "capped": False,
                        "exhausted": True, "last": None}
            where.append("EXISTS (SELECT 1 FROM tweet_hits h WHERE h.stream_id IN ("
                         + ",".join("?" * len(sids)) + ") AND h.tweet_id = t.tweet_id)")
            args += sids
        blue = ""
        join_raw = ""
        if spec["verified"]:
            blue = ", json_extract(COALESCE(r.raw_json, t.raw_json), '$.user.blue') AS blue"
            join_raw = " LEFT JOIN tweet_raw r ON r.tweet_id = t.tweet_id"
        cols = ("t.tweet_id AS rid, t.created_ms AS ms, t.text, t.author_username AS handle, "
                "t.hashtags, t.mentions, t.urls, t.media_urls, t.lang, t.like_count, "
                "t.reply_count, t.retweet_count, t.view_count, t.author_followers, "
                "t.is_retweet, t.is_reply, t.is_quote" + blue)
        expr = fts_expr(spec, "text", "author_username", _ascii)
        if expr:
            sql = (f"SELECT {cols} FROM tweets_fts f CROSS JOIN tweets t "
                   f"ON t.tweet_id = f.rowid{join_raw} "
                   f"WHERE f.tweets_fts MATCH ? AND f.rowid BETWEEN ? AND ? "
                   f"AND {' AND '.join(where)} ORDER BY f.rowid {order} LIMIT {MAX_SCAN}")
            args = [expr, lo, hi] + args
        else:
            sql = (f"SELECT {cols} FROM tweets t{join_raw} "
                   f"WHERE t.tweet_id BETWEEN ? AND ? AND {' AND '.join(where)} "
                   f"ORDER BY t.tweet_id {order} LIMIT {MAX_SCAN}")
            args = [lo, hi] + args

        def keep(r):
            repost, reply, quote = bool(r["is_retweet"]), bool(r["is_reply"]), bool(r["is_quote"])
            media = (r["media_urls"] or "[]") not in ("[]", "")
            if not _common_ok(spec, r["like_count"], r["reply_count"], r["view_count"],
                              media, repost, reply):
                return None
            if spec["x_types"]:
                kinds = {k for k, on in (("repost", repost), ("reply", reply),
                                         ("quote", quote)) if on} or {"original"}
                if not kinds & set(spec["x_types"]):
                    return None
            if spec["lang"] and (r["lang"] or "").lower() != spec["lang"]:
                return None
            if not _ge(r["author_followers"], spec["min_followers"]):
                return None
            if not _ge(r["retweet_count"], spec["min_reposts"]):
                return None
            if spec["has_link"] and (r["urls"] or "[]") in ("[]", ""):
                return None
            if spec["verified"] and r["blue"] not in (1, True):
                return None
            tags = {t.lower() for t in _json_list(r["hashtags"])} | text_tags(r["text"])
            ments = {m.lower() for m in _json_list(r["mentions"])} | text_mentions(r["text"])
            if not text_ok(spec, r["text"], tags, ments, r["handle"]):
                return None
            return {"platform": "x", "id": r["rid"], "ms": r["ms"] or 0, "pos": r["rid"]}

        out = _walk(con, sql, args, keep, want, count, deadline_s)
        last = out.pop("last")
        out["last_pos"] = last["rid"] if last is not None else cursor
        out["last_ms"] = last["ms"] if last is not None else None
        return out
    finally:
        con.close()


def search_ig(path, spec, scope, cursor, want, count, deadline_s, post_scope):
    safe = ensure_fts(path, "instagram")
    con = _ro(path)
    try:
        order = "DESC" if spec["sort"] == "latest" else "ASC"
        lo, hi = _bounds(spec, cursor)
        if scope == "all":
            where, args = ["p.project_id != 0"], []
        else:
            sq, sa = post_scope(con, scope, "p")
            where, args = [sq], list(sa)
        if spec["from_ms"] is not None:
            where.append("p.taken_at >= ?"); args.append(spec["from_ms"] // 1000)
        if spec["to_ms"] is not None:
            where.append("p.taken_at < ?"); args.append(spec["to_ms"] // 1000)
        cols = ("p.pk AS rid, p.taken_at AS ts, p.caption AS text, p.username AS handle, "
                "p.media_type, p.product_type, p.like_count, p.comment_count, "
                "p.play_count, p.thumbnail_url, p.video_url")
        expr = (fts_expr(spec, "caption", "username", (lambda r: True) if safe else _ascii)
                if safe is not None else None)
        if expr:
            sql = (f"SELECT {cols} FROM ig_posts_fts f CROSS JOIN posts p ON p.pk = f.rowid "
                   f"WHERE f.ig_posts_fts MATCH ? AND f.rowid BETWEEN ? AND ? "
                   f"AND {' AND '.join(where)} ORDER BY f.rowid {order} LIMIT {MAX_SCAN}")
            args = [expr, lo, hi] + args
        else:
            sql = (f"SELECT {cols} FROM posts p WHERE p.pk BETWEEN ? AND ? "
                   f"AND {' AND '.join(where)} ORDER BY p.pk {order} LIMIT {MAX_SCAN}")
            args = [lo, hi] + args

        def keep(r):
            media = bool(r["thumbnail_url"] or r["video_url"])
            if not _common_ok(spec, r["like_count"], r["comment_count"], r["play_count"],
                              media, False, False):
                return None
            if spec["ig_types"]:
                mt, reel = r["media_type"], (r["product_type"] or "") == "clips"
                kind = ("reel" if reel else "photo" if mt == 1 else "video" if mt == 2
                        else "album" if mt == 8 else "other")
                if kind not in spec["ig_types"]:
                    return None
            if not text_ok(spec, r["text"], text_tags(r["text"]),
                           text_mentions(r["text"]), r["handle"]):
                return None
            return {"platform": "instagram", "id": r["rid"],
                    "ms": int(r["ts"] or 0) * 1000, "pos": r["rid"]}

        out = _walk(con, sql, args, keep, want, count, deadline_s)
        last = out.pop("last")
        out["last_pos"] = last["rid"] if last is not None else cursor
        out["last_ms"] = int(last["ts"] or 0) * 1000 if last is not None else None
        if safe is None:
            out["note"] = "Instagram search index could not be created; scanned without it"
        return out
    finally:
        con.close()


def search_fb(path, spec, scope, cursor, want, count, deadline_s):
    """Facebook's index rowid is not time order, so candidates (≤ MAX_SCAN) are
    sorted by posted time here and paged by keyset on (ms, post_id)."""
    safe = ensure_fts(path, "facebook")
    con = _ro(path)
    try:
        where, args = [], []
        if scope != "all":
            where.append("p.project_id = ?"); args.append(scope)
        t = "COALESCE(p.created_ms, p.collected_ms)"
        if spec["from_ms"] is not None:
            where.append(f"{t} >= ?"); args.append(spec["from_ms"])
        if spec["to_ms"] is not None:
            where.append(f"{t} < ?"); args.append(spec["to_ms"])
        cond = (" AND " + " AND ".join(where)) if where else ""
        cols = (f"p.post_id AS rid, {t} AS ms, p.text, p.page AS handle, p.like_count, "
                f"p.comment_count, p.share_count, p.media_json")
        expr = (fts_expr(spec, "text", "page", (lambda r: True) if safe else _ascii)
                if safe is not None else None)
        if expr:
            sql = (f"SELECT {cols} FROM fb_posts_fts f CROSS JOIN posts p "
                   f"ON p.post_id = f.post_id WHERE f.fb_posts_fts MATCH ?{cond} "
                   f"ORDER BY f.rowid DESC LIMIT {MAX_SCAN}")
            args = [expr] + args
        else:
            sql = (f"SELECT {cols} FROM posts p WHERE 1{cond} "
                   f"ORDER BY p.rowid DESC LIMIT {MAX_SCAN}")
        latest = spec["sort"] == "latest"
        cur = tuple(cursor) if cursor else None

        def keep(r):
            if cur is not None:
                k = (r["ms"] or 0, r["rid"])
                if (latest and k >= cur) or (not latest and k <= cur):
                    return None
            media = (r["media_json"] or "[]") not in ("", "[]")
            if not _common_ok(spec, r["like_count"], r["comment_count"], None,
                              media, False, False):
                return None
            if not _ge(r["share_count"], spec["min_shares"]):
                return None
            if not text_ok(spec, r["text"], text_tags(r["text"]),
                           text_mentions(r["text"]), r["handle"]):
                return None
            return {"platform": "facebook", "id": r["rid"], "ms": r["ms"] or 0,
                    "pos": [r["ms"] or 0, r["rid"]]}

        # Every candidate is needed to sort by time: no early stop.
        out = _walk(con, sql, args, keep, MAX_COUNT, True, deadline_s)
        out.pop("last")
        ms = sorted(out["matches"], key=lambda m: (m["ms"], m["id"]), reverse=latest)
        out["matches"] = ms[:want]
        out["exhausted"] = len(ms) <= want
        out["last_pos"] = cursor
        out["last_ms"] = None
        if safe is None:
            out["note"] = "Facebook search index could not be created; scanned without it"
        return out
    finally:
        con.close()


# --------------------------------------------------------------------------
# Cursor + merge
# --------------------------------------------------------------------------

def encode_cursor(c: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(c, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(s) -> dict:
    if not s:
        return {}
    try:
        pad = "=" * (-len(s) % 4)
        c = json.loads(base64.urlsafe_b64decode(str(s) + pad))
        assert isinstance(c, dict)
        return c
    except Exception:
        raise BadRequest("bad cursor")


def run(p: dict, *, scope, paths: dict, post_scope, want=PAGE, deadline_s=DEADLINE_S):
    """
    One search. `scope` is a project id or "all". `paths` = {platform: Path}
    (missing files are skipped). Returns the page as LIGHT refs in `refs`
    ({platform, id, ms}); web.py hydrates only those.
    """
    spec = parse(p)
    cur_in = decode_cursor(p.get("cursor"))
    first = not cur_in
    skip = left_out(spec)
    plats = PLATFORMS if spec["platform"] == "all" else (spec["platform"],)
    latest = spec["sort"] == "latest"
    res, errors, took, notes = {}, {}, {}, list(spec["notes"])
    for plat in plats:
        if plat in skip:
            notes.append(f"{NAMES[plat]} left out: {skip[plat]}")
            continue
        if cur_in and cur_in.get(plat, "done") == "done":
            continue
        path = paths.get(plat)
        if not path or not path.exists():
            continue
        t0 = time.monotonic()
        try:
            c = cur_in.get(plat)
            if plat == "x":
                r = search_x(path, spec, scope, c, want, first, deadline_s)
            elif plat == "instagram":
                r = search_ig(path, spec, scope, c, want, first, deadline_s, post_scope)
            else:
                r = search_fb(path, spec, scope, c, want, first, deadline_s)
            r["cursor_in"] = c
            res[plat] = r
            if r.get("note"):
                notes.append(r["note"])
        except Exception as e:          # one platform down is not the page down
            errors[plat] = f"{type(e).__name__}: {e}"[:200]
        took[plat] = int((time.monotonic() - t0) * 1000)
    if spec["min_views"] and "instagram" in res:
        notes.append("Instagram photos have no view count; only videos and reels can pass min views")

    # Merge by posted time. A platform that stopped early (deadline / scan cap)
    # without filling its page sets a frontier: nothing past it is safe to show.
    pool = [m for r in res.values() for m in r["matches"]]
    pool.sort(key=lambda m: (m["ms"], str(m["id"])), reverse=latest)
    for plat, r in res.items():
        if (len(r["matches"]) < want and not r["exhausted"]
                and r.get("last_ms") is not None):
            f = r["last_ms"]
            pool = [m for m in pool if (m["ms"] >= f if latest else m["ms"] <= f)]
    page = pool[:want]

    cur_out, any_more = {}, False
    for plat in plats:
        if plat not in res:
            cur_out[plat] = "done"
            continue
        r = res[plat]
        mine = [m for m in page if m["platform"] == plat]
        if len(mine) < len(r["matches"]):
            cur_out[plat] = mine[-1]["pos"] if mine else r["cursor_in"]
        elif len(r["matches"]) >= want:
            cur_out[plat] = r["matches"][-1]["pos"]
        elif r["exhausted"]:
            cur_out[plat] = "done"
        else:
            cur_out[plat] = r["last_pos"]
        if cur_out[plat] != "done":
            any_more = True
    # Never hand back a cursor that cannot move (same position, nothing shown).
    if not page and cur_out == {k: cur_in.get(k) for k in cur_out}:
        any_more = False

    out = {
        "platform": spec["platform"], "sort": spec["sort"],
        "refs": page,
        "cursor": encode_cursor(cur_out) if any_more else None,
        "partial": any(r["partial"] for r in res.values()),
        "notes": notes, "took_ms": took,
    }
    if first:
        out["counts"] = {plat: {"n": min(r["n"], MAX_COUNT),
                                "plus": bool(r["capped"] or r["partial"] or r["n"] >= MAX_COUNT)}
                         for plat, r in res.items()}
    if errors:
        out["errors"] = errors
    return out
