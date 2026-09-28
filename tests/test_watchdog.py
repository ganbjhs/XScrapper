"""
tests/test_watchdog.py — the scraper watchdog, offline (watchdog.py).

Run on its own:
    python3 tests/test_watchdog.py
or as a section of tests/test_all.py, which passes in its own ok().

What is covered:
  1. the probes   read what each collector's store says, and only that:
                  a good X poll vs. error / starved ones; a paused collector;
                  nothing enabled; no database at all
  2. the decision the hour of grace, one page per outage, a reminder later,
                  a recovery, paging off, a send that failed is retried
  3. the tick     end to end against real files: the state file, the
                  heartbeat, the message text that lands in Telegram
  4. the guard    a stale collector is a WARN with the unit to restart
"""

import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import watchdog as wd  # noqa: E402

FAILURES = []


def _ok(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)
    if not cond:
        FAILURES.append(msg)


H = 3600_000
NOW = 1_800_000_000_000


# ------------------------------------------------------------------ fixtures

def _results_db(root, polls=(), paused=False, streams=1):
    con = sqlite3.connect(root / "results.db")
    con.executescript("""
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE streams (stream_id INTEGER PRIMARY KEY, label TEXT, paused INTEGER DEFAULT 0);
      CREATE TABLE polls (poll_id INTEGER PRIMARY KEY, stream_id INTEGER, kind TEXT,
        started_ms INTEGER, finished_ms INTEGER, pages INTEGER DEFAULT 0,
        stop_reason TEXT, error TEXT);
    """)
    if paused:
        con.execute("INSERT INTO meta VALUES('collection_paused','1')")
    for i in range(streams):
        con.execute("INSERT INTO streams(label) VALUES(?)", (f"s{i}",))
    for started, reason, err in polls:
        con.execute("INSERT INTO polls(stream_id, kind, started_ms, finished_ms, pages, stop_reason, error) "
                    "VALUES(1,'poll',?,?,?,?,?)",
                    (started, started + 5000, 0 if reason == "error" else 2, reason, err))
    con.commit(); con.close()


def _sources_db(root, name, pause_key, last_runs=(), paused=False):
    con = sqlite3.connect(root / name)
    con.executescript("""
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE sources (label TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, last_run INTEGER);
    """)
    if paused:
        con.execute("INSERT INTO settings VALUES(?, '1')", (pause_key,))
    for label, enabled, last_run in last_runs:
        con.execute("INSERT INTO sources VALUES(?,?,?)", (label, enabled, last_run))
    con.commit(); con.close()


# ------------------------------------------------------------------ 1. probes

def test_probes(tmp, ok):
    root = tmp / "probes"; root.mkdir()
    good = NOW - 30 * 60_000
    _results_db(root, polls=[(good, "watermark", None),
                             (NOW - 10 * 60_000, "error", "OperationalError: database is locked"),
                             (NOW - 5 * 60_000, "no_account_or_abort", None)])
    p = wd.probe_x(root)
    ok(p.watched, "x: one unpaused stream → watched")
    ok(p.last_ok_ms == good + 5000, "x: last good = the watermark poll, not the error after it")
    ok(p.last_seen_ms == NOW - 5 * 60_000, "x: last seen = the newest attempt, failed or not")
    ok("stop=no_account_or_abort" in p.detail, "x: detail names the newest failure")

    root2 = tmp / "probes2"; root2.mkdir()
    _results_db(root2, polls=[(good, "watermark", None)], paused=True)
    ok(not wd.probe_x(root2).watched, "x: collection_paused → not watched")
    root3 = tmp / "probes3"; root3.mkdir()
    _results_db(root3, polls=[], streams=0)
    ok(not wd.probe_x(root3).watched, "x: no streams → not watched")
    ok(not wd.probe_x(tmp / "nowhere").exists, "x: no results.db → not in use")

    _sources_db(root, "ig_results.db", "ig_paused",
                [("a", 1, (NOW - 2 * H) // 1000), ("b", 1, (NOW - 20 * 60_000) // 1000),
                 ("c", 0, NOW // 1000)])
    p = wd.probe_instagram(root)
    ok(p.sources == 2, "ig: counts enabled sources only")
    ok(p.last_ok_ms == (NOW - 20 * 60_000) // 1000 * 1000, "ig: newest last_run among enabled")
    _sources_db(root, "fb_results.db", "fb_paused", [("p", 1, NOW // 1000)], paused=True)
    ok(wd.probe_facebook(root).paused and not wd.probe_facebook(root).watched,
       "fb: fb_paused → not watched")


# ------------------------------------------------------------------ 2. decision

def _cfg(**kw):
    c = {"enabled": True, "remind_min": 360, "stale_min": {"x": 60, "instagram": 60, "facebook": 60}}
    c.update(kw); return c


def test_decision(ok):
    P = lambda **k: wd.Probe("x", sources=1, **k)   # noqa: E731
    st = {}
    a, st = wd.decide(st, P(last_ok_ms=NOW - 30 * 60_000), _cfg(), NOW)
    ok(a is None and not st["stale"], "30 min silent, 60 min threshold → nothing")
    a, st = wd.decide(st, P(last_ok_ms=NOW - 61 * 60_000), _cfg(), NOW)
    ok(a == "down" and st["down_since_ms"] == NOW - 61 * 60_000, "61 min silent → page: down")
    st["alerted_ms"] = NOW                                   # the send succeeded
    a, st = wd.decide(st, P(last_ok_ms=NOW - 61 * 60_000), _cfg(), NOW + 60_000)
    ok(a is None, "a minute later, still down → no second page")
    a, st = wd.decide(st, P(last_ok_ms=NOW - 61 * 60_000), _cfg(), NOW + 361 * 60_000)
    ok(a == "remind", "after remind_min → a reminder")
    a, st = wd.decide(st, P(last_ok_ms=NOW + 400 * 60_000), _cfg(), NOW + 401 * 60_000)
    ok(a == "recovered", "a fresh good poll while alerted → recovered")

    st = {}
    a, st = wd.decide(st, P(last_ok_ms=None), _cfg(), NOW)
    ok(a is None, "never polled: first sight starts the clock, no page")
    a, st = wd.decide(st, P(last_ok_ms=None), _cfg(), NOW + 61 * 60_000)
    ok(a == "down", "never polled for an hour after first sight → page")

    st = {"alerted_ms": NOW, "down_since_ms": NOW - H}
    a, st = wd.decide(st, P(last_ok_ms=NOW - 2 * H), _cfg(enabled=False), NOW)
    ok(a is None and "alerted_ms" not in st, "paging off → nothing, and the open incident is dropped")

    st = {}
    a, st = wd.decide(st, wd.Probe("x", sources=1, paused=True, last_ok_ms=NOW - 9 * H), _cfg(), NOW)
    ok(a is None and not st["stale"], "paused for nine hours → not stale (on purpose)")
    a, st = wd.decide({}, P(last_ok_ms=NOW - 20 * 60_000), _cfg(stale_min={"x": 15, "instagram": 60, "facebook": 60}), NOW)
    ok(a == "down", "per-platform threshold: 20 min silent, 15 min threshold → page")


# ------------------------------------------------------------------ 3. tick

def test_tick(tmp, ok):
    root = tmp / "tick"; (root / "profiles").mkdir(parents=True)
    _results_db(root, polls=[(NOW - 3 * H, "watermark", None),
                             (NOW - 10 * 60_000, "error", "sqlite3.OperationalError: database is locked")])
    _sources_db(root, "ig_results.db", "ig_paused", [("a", 1, NOW // 1000)])
    sent = []
    sender = lambda text: (sent.append(text), (True, ""))[1]   # noqa: E731

    rep = wd.tick(root, log=lambda *_: None, sender=sender, now_ms=NOW)
    ok(len(sent) == 1 and rep["sent"] == [{"platform": "x", "action": "down"}],
       "tick: X paged once, Instagram (fresh) not")
    msg = sent[0]
    ok(msg.startswith("🔴 X collector has stopped collecting"), "message: says which collector")
    ok("2h 59m ago" in msg, "message: how long since the last good poll (finished_ms of that poll)")
    ok("database is locked" in msg, "message: the newest error, so the fix is obvious")
    ok("still trying" in msg, "message: says it is running-but-failing, not dead")
    ok("systemctl restart xscraper-watch" in msg, "message: the unit to restart")
    st = json.loads((root / "profiles" / "watchdog.json").read_text())
    ok(st["last_tick_ms"] == NOW and st["platforms"]["x"]["alerted_ms"] == NOW,
       "state: heartbeat and the alert are persisted")
    ok(st["platforms"]["instagram"]["stale"] is False, "state: instagram healthy")

    rep = wd.tick(root, log=lambda *_: None, sender=sender, now_ms=NOW + 60_000)
    ok(len(sent) == 1, "tick: next minute, nothing new sent")

    # A good poll lands → recovery
    con = sqlite3.connect(root / "results.db")
    con.execute("INSERT INTO polls(stream_id, kind, started_ms, finished_ms, pages, stop_reason) "
                "VALUES(1,'poll',?,?,1,'watermark')", (NOW + 2 * 60_000, NOW + 2 * 60_000 + 500))
    con.commit(); con.close()
    rep = wd.tick(root, log=lambda *_: None, sender=sender, now_ms=NOW + 3 * 60_000)
    ok(len(sent) == 2 and sent[1].startswith("🟢 X collector is back"), "tick: recovery paged")
    ok("3h" in sent[1], "recovery: says how long it was silent")
    st = json.loads((root / "profiles" / "watchdog.json").read_text())
    ok("alerted_ms" not in st["platforms"]["x"], "state: incident closed")

    # A send that fails is retried, not forgotten
    root2 = tmp / "tick2"; (root2 / "profiles").mkdir(parents=True)
    _results_db(root2, polls=[(NOW - 3 * H, "watermark", None)])
    bad = lambda text: (False, "HTTP 401: Unauthorized")   # noqa: E731
    rep = wd.tick(root2, log=lambda *_: None, sender=bad, now_ms=NOW)
    ok(rep["platforms"]["x"]["action"] == "down" and rep["platforms"]["x"].get("send_error"),
       "send failed: reported")
    rep = wd.tick(root2, log=lambda *_: None, sender=sender, now_ms=NOW + 60_000)
    ok(rep["sent"] and rep["sent"][0]["action"] == "down", "send failed: retried next tick")

    # dry: decides, never sends, never persists
    root3 = tmp / "tick3"; (root3 / "profiles").mkdir(parents=True)
    _results_db(root3, polls=[(NOW - 3 * H, "watermark", None)])
    n = len(sent)
    rep = wd.tick(root3, log=lambda *_: None, sender=sender, now_ms=NOW, dry=True)
    ok(rep["platforms"]["x"]["stale"] and len(sent) == n and not (root3 / "profiles" / "watchdog.json").exists(),
       "dry tick: stale is reported, nothing sent, nothing written")

    # thresholds from settings, service liveness
    con = sqlite3.connect(root3 / "results.db")
    con.execute("INSERT INTO settings VALUES('watchdog_stale_min_x','300')")
    con.execute("INSERT INTO settings VALUES('watchdog_enabled','0')")
    con.commit(); con.close()
    c = wd.config(root3)
    ok(c["stale_min"]["x"] == 300 and c["enabled"] is False, "config: thresholds and the switch come from settings")
    svc = wd.service_status(root, NOW + 10 * 60_000)
    ok(svc["silent"], "service: no tick for ten minutes → silent")
    ok(not wd.service_status(root3)["ever_ran"], "service: never ran → says so")


# ------------------------------------------------------------------ 4. guard

def test_guard(tmp, ok):
    import types
    import guard
    root = tmp / "guard"; (root / "profiles").mkdir(parents=True)
    _results_db(root, polls=[(int(time.time() * 1000) - 3 * H, "watermark", None)])
    cfg = types.SimpleNamespace(root=root)
    fs = guard._watchdog_findings(cfg)
    codes = {f.code: f for f in fs}
    ok("collect.stale.x" in codes and codes["collect.stale.x"].level == guard.WARN,
       "guard: a stale X collector is a WARN")
    ok("xscraper-watch" in codes["collect.stale.x"].remedy, "guard: remedy names the unit")
    ok("watchdog.never" in codes, "guard: says the watchdog has never run here")


def run(tmp, ok=_ok):
    tmp = pathlib.Path(tmp)
    test_probes(tmp, ok)
    test_decision(ok)
    test_tick(tmp, ok)
    test_guard(tmp, ok)


if __name__ == "__main__":
    root = pathlib.Path(tempfile.mkdtemp(prefix="wd_"))
    try:
        run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All watchdog checks passed.")
