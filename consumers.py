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
