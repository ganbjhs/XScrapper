"""Who is reading us, per project and platform.

Watch-Tower (and any other API-key consumer) pulls with ?project=P. Nothing
here recorded that, so the dashboard could not say which project is actually
being served and which is idle or a test. This module stamps every API-key
GET that names a project. Purely observational — it changes no response shape
and nothing a consumer reads.

Two kinds of read are told apart, because Watch-Tower LISTS every one of our
projects on its Collector page (/api/watchlists, /api/projects, 24h counts)
whether or not it is bound to them. A BOUND project is walked with a cursor
(since_collected_ms / since_id on /api/tweets, cursor on the IG/FB post
endpoints; handover §3). Only those pulls make a project "mirrored"/"live";
listing reads only move `listed_ms`.

State lives in consumers.json beside the databases so a restart does not
forget who was live a minute ago. Writes are throttled (at most one every
FLUSH_EVERY_S) because a consumer walking a corpus makes many requests.
"""
import json
import threading
import time
from pathlib import Path

FLUSH_EVERY_S = 15
LIVE_WINDOW_S = 15 * 60       # "live" = mirrored within the last 15 minutes
CURSOR_PARAMS = ("since_collected_ms", "since_id", "cursor")

_lock = threading.Lock()
_state: dict = {}             # {"<pid>": {"platforms": {p: ms}, "last_ms": ms, "listed_ms": ms, "keys": {hint: ms}}}
_path: Path | None = None
_dirty = False
_last_flush = 0.0


def init(root: Path) -> None:
    global _path, _state
    _path = Path(root) / "consumers.json"
    try:
        _state = json.loads(_path.read_text("utf-8")) or {}
    except Exception:
        _state = {}


def mirror_platform(path: str, q: dict):
    """The platform a request MIRRORS, or None when it is only listing."""
    if not any(q.get(k) not in (None, "") for k in CURSOR_PARAMS):
        return None
    if path == "/api/ig/posts":
        return "instagram"
    if path == "/api/fb/posts":
        return "facebook"
    if path == "/api/tweets":
        p = (q.get("platform") or "").lower()
        return {"instagram": "instagram", "ig": "instagram",
                "facebook": "facebook", "fb": "facebook"}.get(p, "x")
    return None


def record(project, platform, key_hint: str = "") -> None:
    """Stamp one read. platform None = listing/telemetry; a platform = a mirror pull."""
    global _dirty, _last_flush
    try:
        pid = str(int(project))
    except (TypeError, ValueError):
        return
    now_ms = int(time.time() * 1000)
    with _lock:
        row = _state.setdefault(pid, {"platforms": {}, "last_ms": 0, "listed_ms": 0, "keys": {}})
        if platform:
            row["platforms"][platform] = now_ms
            row["last_ms"] = now_ms
        else:
            row["listed_ms"] = now_ms
        if key_hint:
            row.setdefault("keys", {})[key_hint] = now_ms
        _dirty = True
        if _path and time.time() - _last_flush > FLUSH_EVERY_S:
            _flush_locked()


def _flush_locked() -> None:
    global _dirty, _last_flush
    if not _path or not _dirty:
        return
    try:
        tmp = _path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state), "utf-8")
        tmp.replace(_path)
        _dirty = False
        _last_flush = time.time()
    except Exception:
        pass


def snapshot() -> dict:
    """For the dashboard: every project a key ever touched, with live flags."""
    now_ms = int(time.time() * 1000)
    win = LIVE_WINDOW_S * 1000
    with _lock:
        out = {}
        for pid, row in _state.items():
            plats = row.get("platforms", {})
            out[pid] = {
                "last_ms": row.get("last_ms", 0),
                "listed_ms": row.get("listed_ms", 0),
                "mirrored": bool(plats),
                "live": bool(plats) and (now_ms - row.get("last_ms", 0)) < win,
                "platforms": {p: {"last_ms": ms, "live": (now_ms - ms) < win}
                              for p, ms in plats.items()},
                "keys": sorted(row.get("keys", {}).keys()),
            }
    return {"projects": out, "live_window_s": LIVE_WINDOW_S, "now_ms": now_ms}


# ---------------------------------------------------------------- the ledger
#
# What was HANDED OVER, not only when (2026-10-02). record() above says a
# consumer was here a minute ago; it cannot answer "did Watch-Tower take the
# posts, and how many are still waiting?", which is the question asked every
# time a feed over there looks thin. Until this existed the only way to tell
# "the collector did not have it" from "Watch-Tower did not take it" from
# "Watch-Tower took it and dropped it" was an afternoon in two admin panels.
#
# So every CURSORED pull is written down: the position the consumer presented
# (`since`), how many rows it was given, the position of the last row given,
# and the HTTP status. Two positions matter and they are not the same thing:
#
#   served_ms  the newest row we have put on the wire for this consumer.
#   ack_ms     the newest `since_collected_ms` the consumer has PRESENTED. It
#              only presents a position after storing the page that ended
#              there, so everything at or before ack_ms is confirmed taken.
#
# Counts ("handed over today", "waiting") are NOT kept here — they are counted
# from the posts tables against these two positions (web._handover_json), so
# they cannot drift from what is actually stored and a repeated page is never
# counted twice. This file keeps only what the database cannot know: the
# positions, and the history of the pulls themselves.
#
# Observational, like everything in this module: no response changes.

PULL_RECENT = 40              # pulls kept per project+platform, newest last
PULL_DAYS = 8                 # daily tallies kept


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def record_pull(project, platform, *, since_ms=None, rows=0, to_ms=None,
                status=200, key_hint: str = "") -> None:
    """One cursored pull, answered. `since_ms` is the since_collected_ms the
    consumer presented (None when it walked by id), `to_ms` the collected_ms
    of the last row handed back (None for an empty page or an error)."""
    global _dirty
    try:
        pid = str(int(project))
    except (TypeError, ValueError):
        return
    if not platform:
        return
    now_ms = int(time.time() * 1000)
    ok = 200 <= int(status) < 300
    with _lock:
        row = _state.setdefault(pid, {"platforms": {}, "last_ms": 0, "listed_ms": 0, "keys": {}})
        led = row.setdefault("pulls", {}).setdefault(platform, {
            "since_ms": now_ms, "ack_ms": None, "served_ms": None,
            "last_ms": 0, "last_ok_ms": 0, "last_rows": 0, "last_status": 0,
            "days": {}, "recent": []})
        led["last_ms"] = now_ms
        led["last_status"] = int(status)
        led["last_rows"] = int(rows or 0)
        if ok:
            led["last_ok_ms"] = now_ms
            if since_ms is not None and (led.get("ack_ms") is None or since_ms > led["ack_ms"]):
                led["ack_ms"] = int(since_ms)
            if to_ms is not None and (led.get("served_ms") is None or to_ms > led["served_ms"]):
                led["served_ms"] = int(to_ms)
        day = led["days"].setdefault(_day(now_ms), {
            "pulls": 0, "rows": 0, "empty": 0, "errors": 0,
            "wait_sum_ms": 0, "wait_n": 0, "wait_max_ms": 0})
        day["pulls"] += 1
        if not ok:
            day["errors"] += 1
        elif not rows:
            day["empty"] += 1
        else:
            day["rows"] += int(rows)
            # How long the NEWEST row of this page sat with us before it was
            # taken: the freshest measure of the consumer's own cadence.
            if to_ms is not None and now_ms >= to_ms:
                w = now_ms - int(to_ms)
                day["wait_sum_ms"] += w
                day["wait_n"] += 1
                day["wait_max_ms"] = max(day["wait_max_ms"], w)
        for k in sorted(led["days"])[:-PULL_DAYS]:
            led["days"].pop(k, None)
        led["recent"].append({"at_ms": now_ms, "since_ms": since_ms,
                              "rows": int(rows or 0), "to_ms": to_ms,
                              "status": int(status), "key": key_hint})
        del led["recent"][:-PULL_RECENT]
        _dirty = True
        if _path and time.time() - _last_flush > FLUSH_EVERY_S:
            _flush_locked()


def pulls(project) -> dict:
    """{platform: ledger} for one project — a deep copy, safe to read."""
    try:
        pid = str(int(project))
    except (TypeError, ValueError):
        return {}
    with _lock:
        return json.loads(json.dumps((_state.get(pid) or {}).get("pulls") or {}))


def flush() -> None:
    with _lock:
        _flush_locked()
