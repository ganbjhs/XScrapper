"""Who is reading us, per project and platform.

Watch-Tower (and any other API-key consumer) pulls with ?project=P. Nothing
here records that, so the dashboard cannot say which project is actually
being served and which is idle or a test. This module stamps every API-key
GET that names a project: (project, platform) -> last seen. Purely
observational — it changes no response shape and nothing a consumer reads.

State lives in consumers.json beside the databases so a restart does not
forget who was live a minute ago. Writes are throttled (at most one every
FLUSH_EVERY_S) because a consumer walking a corpus makes many requests.
"""
import json
import threading
import time
from pathlib import Path

FLUSH_EVERY_S = 15
LIVE_WINDOW_S = 15 * 60       # "live" = pulled within the last 15 minutes
PLATFORMS = ("x", "instagram", "facebook", "links", "telemetry")

_lock = threading.Lock()
_state: dict = {}             # {"<project_id>": {"platforms": {p: ms}, "last_ms": ms, "keys": {hint: ms}}}
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


def platform_of(path: str, q: dict) -> str:
    """Which platform a read is about, from its path and query."""
    if path.startswith("/api/ig/"):
        return "instagram"
    if path.startswith("/api/fb/"):
        return "facebook"
    if path == "/api/links":
        return "links"
    if path in ("/api/tweets", "/api/export", "/api/live"):
        p = (q.get("platform") or "").lower()
        return {"instagram": "instagram", "ig": "instagram",
                "facebook": "facebook", "fb": "facebook"}.get(p, "x")
    if path in ("/api/watchlists", "/api/streams", "/api/streams/assignments",
                "/api/watchlist/xmembers", "/api/collections",
                "/api/collections/items", "/api/collections/export"):
        return "x"
    return "telemetry"


def record(project, platform: str, key_hint: str = "") -> None:
    """Stamp one read. Cheap; called on every API-key GET."""
    global _dirty, _last_flush
    try:
        pid = str(int(project))
    except (TypeError, ValueError):
        return
    now_ms = int(time.time() * 1000)
    with _lock:
        row = _state.setdefault(pid, {"platforms": {}, "last_ms": 0, "keys": {}})
        row["platforms"][platform] = now_ms
        row["last_ms"] = now_ms
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
    """For the dashboard: every project ever pulled, with live flags."""
    now_ms = int(time.time() * 1000)
    win = LIVE_WINDOW_S * 1000
    with _lock:
        out = {}
        for pid, row in _state.items():
            plats = row.get("platforms", {})
            out[pid] = {
                "last_ms": row.get("last_ms", 0),
                "live": (now_ms - row.get("last_ms", 0)) < win,
                "platforms": {p: {"last_ms": ms, "live": (now_ms - ms) < win}
                              for p, ms in plats.items()},
                "keys": sorted(row.get("keys", {}).keys()),
            }
    return {"projects": out, "live_window_s": LIVE_WINDOW_S, "now_ms": now_ms}
