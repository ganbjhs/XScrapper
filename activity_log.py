"""
activity_log.py — ONE persistent account-activity log for every platform.

Every line the collectors and engines already print (session reuse, login
attempts, logged-out walls, posts fetched, avatar captures, bandwidth caps,
errors) is also written here, so the dashboard can show exactly what each
account is doing without anyone tailing a terminal. The log is the answer to
"did it actually log in?" — the engine's own words, timestamped.

Design:
  * SQLite (activity.db next to the other stores) so the collector SERVICES
    and the web dashboard — separate processes — see the same log.
  * levels are inferred from the message text (the collectors already speak a
    consistent language: "error", "NOT LOGGED IN", "logged out", ...), so no
    call site needs rewriting.
  * bounded: old rows beyond ~20k are pruned on write, so the file can't grow
    without limit.
  * logging must never break collection: every write is wrapped, and a failure
    to log is silently ignored (the echo to stdout still happens).
"""

import os
import re
import sqlite3
import threading
import time

DEFAULT_DB = os.getenv("ACTIVITY_LOG_DB", "activity.db")
_LOCK = threading.Lock()
_KEEP = 20_000          # newest rows kept; older pruned on write

_ERROR_RE = re.compile(
    r"error|failed|NOT LOGGED|cannot |could not|rejected|checkpoint"
    r"|cap reached|wrong password|still failing", re.I)
_WARN_RE = re.compile(
    r"logged out|re-login|login available|skipping|no enabled|stale"
    r"|not found|already running|fell back|home feed instead", re.I)


def _con(db=None):
    con = sqlite3.connect(db or DEFAULT_DB, timeout=10)
    con.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "  id       INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts_ms    INTEGER NOT NULL,"
        "  platform TEXT,"            # 'facebook' | 'instagram' | 'x' | ...
        "  account  TEXT,"            # which login was acting, when known
        "  level    TEXT,"            # 'info' | 'warn' | 'error'
        "  message  TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts_ms)")
    return con


def classify(message: str) -> str:
    """Level from the message text the collectors already write."""
    if _ERROR_RE.search(message):
        return "error"
    if _WARN_RE.search(message):
        return "warn"
    return "info"


def log_event(platform, message, account=None, level=None, db=None):
    """Persist one activity line. Never raises — a log must not stop a fetch."""
    msg = str(message)
    try:
        with _LOCK:
            con = _con(db)
            con.execute(
                "INSERT INTO events(ts_ms, platform, account, level, message) "
                "VALUES(?,?,?,?,?)",
                (int(time.time() * 1000), platform, account,
                 level or classify(msg), msg[:2000]))
            con.execute(
                "DELETE FROM events WHERE id <= "
                "  (SELECT MAX(id) FROM events) - ?", (_KEEP,))
            con.commit()
            con.close()
    except Exception:
        pass


def logger(platform, account=None, echo=print, db=None):
    """
    A drop-in replacement for the collectors' log=print callback: still prints
    (journalctl keeps working), AND persists every line for the dashboard.
    """
    def _log(msg):
        try:
            echo(msg)
        except Exception:
            pass
        log_event(platform, msg, account=account, db=db)
    return _log


def recent(limit=200, platform=None, level=None, db=None):
    """Newest events first, optionally filtered by platform and/or level."""
    try:
        con = _con(db)
    except Exception:
        return []
    con.row_factory = sqlite3.Row
    where, params = [], []
    if platform:
        where.append("platform = ?"); params.append(platform)
    if level:
        where.append("level = ?"); params.append(level)
    try:
        rows = con.execute(
            "SELECT * FROM events "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "ORDER BY id DESC LIMIT ?", [*params, int(limit)]).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
