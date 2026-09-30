"""
watchdog.py — "the scraper has been silent for an hour", as a Telegram ping.

WHY THIS EXISTS. On 2026-09-28 the X watcher was locked out of its account
pool for four hours. `systemctl` said active (running), the account cards
said "signed in · collecting", the pager had nothing to say — every poll
simply ended `pages=0 stop=error`. Nobody found out until someone looked.
Every alert this project had was about an ACCOUNT needing a human (decider.py)
or about DATA moving fast (alerts.py). Nothing watched the one thing that
matters most: is anything actually being collected?

So this does not ask "is the process alive?" — it was alive. It asks, per
platform, "when did the collector last complete a good unit of work?", and
pages when that is older than the threshold (default 60 minutes):

    x          newest poll in results.db whose stop_reason is not error /
               no_account_or_abort (a poll that reached X and came back)
    instagram  newest sources.last_run in ig_results.db — stamped per visit;
               the loop reaching a source is the unit of work (a session that
               fails every fetch is the decider's job, and it already pages)
    facebook   newest sources.last_run in fb_results.db — stamped only after a
               successful page scrape

A paused collector, or one with nothing enabled, is silent ON PURPOSE and is
never paged for. Silence that ends is announced too ("back after 4h 13m"),
and a long outage is reminded about every few hours rather than once.

Design rules, in the spirit of guard.py:

  * RUNS APART. Its own systemd unit (deploy/xscraper-watchdog.service), not
    a thread in the web process or the watcher: the process that hangs must
    not be the one expected to notice the hang. Today the web process was at
    1.5 GB and 200 s per request — a thread in it would have been starved.
  * READS ONLY. Every database is opened `mode=ro`; the only thing it writes
    is its own small state file (profiles/watchdog.json), which is also how
    the dashboard and the guard know the watchdog itself is alive.
  * NO NEW DEPENDENCIES. Telegram is one urllib call. A watchdog that cannot
    import is a watchdog that cannot page.
  * ITS OWN BOT. WATCHDOG_TELEGRAM_BOT_TOKEN / WATCHDOG_TELEGRAM_CHAT_ID in
    .env; falls back to the admin (pager) bot so an older .env still pages
    someone. .env is re-read every tick so a token saved in the dashboard is
    live without a restart.

Run it standalone at any time:

    python3 main.py watchdog            # one report, nothing sent
    python3 main.py watchdog --json
    python3 main.py watchdog --test     # one test message through the bot
    python3 main.py watchdog --loop     # what the service runs
"""

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PLATFORMS = ("x", "instagram", "facebook")
NAMES = {"x": "X", "instagram": "Instagram", "facebook": "Facebook"}
UNITS = {"x": "xscraper-watch", "instagram": "xscraper-ig", "facebook": "xscraper-fb"}

TICK_S = 60
STATE_FILE = "profiles/watchdog.json"

# Settings keys (results.db `settings` table, so the dashboard can edit them
# and the watchdog picks them up on its next tick without a restart).
KEY_ENABLED = "watchdog_enabled"          # "0" switches paging off; anything else = on
KEY_REMIND = "watchdog_remind_min"        # re-page this often while still down
KEY_STALE = "watchdog_stale_min_%s"       # per platform, minutes

DEFAULT_STALE_MIN = {"x": 60, "instagram": 60, "facebook": 60}
DEFAULT_REMIND_MIN = 360
MIN_STALE_MIN = 5

# A watchdog that has not ticked for this long is itself the problem.
SILENT_AFTER_S = 5 * 60

TELEGRAM_API = "https://api.telegram.org"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _env_file(root) -> dict:
    """KEY=value pairs from .env, re-read on every call: the dashboard writes
    tokens there (web._env_set) and the watchdog must see them without a
    restart. Falls back to the process environment for anything not in the
    file (the service may have it from systemd)."""
    out = {}
    p = Path(root) / ".env"
    try:
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    except OSError:
        pass
    return out


def _env(root, key: str) -> str:
    return (_env_file(root).get(key) or os.getenv(key, "") or "").strip()


def bot_token(root) -> str:
    """The WATCHDOG BOT: its own bot, so "the scraper is down" arrives in a
    voice that only ever says that. Falls back to the admin bot, then the
    delivery bot, so an older .env still pages someone."""
    return (_env(root, "WATCHDOG_TELEGRAM_BOT_TOKEN")
            or _env(root, "ADMIN_TELEGRAM_BOT_TOKEN")
            or _env(root, "TELEGRAM_BOT_TOKEN"))


def bot_chat(root) -> str:
    return (_env(root, "WATCHDOG_TELEGRAM_CHAT_ID")
            or _env(root, "ADMIN_TELEGRAM_CHAT_ID")
            or _env(root, "TELEGRAM_CHAT_ID"))


def own_bot(root) -> bool:
    return bool(_env(root, "WATCHDOG_TELEGRAM_BOT_TOKEN"))


def bot_status(root) -> dict:
    """What the watchdog would page with — never the token itself."""
    tok = bot_token(root)
    return {"ready": bool(tok and bot_chat(root)),
            "has_token": bool(tok),
            "own_bot": own_bot(root),
            "own_chat": bool(_env(root, "WATCHDOG_TELEGRAM_CHAT_ID")),
            "token_hint": tok.split(":")[0] if tok else "",
            "chat": bot_chat(root)}


def _ro(path, timeout=5):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def _settings(root) -> dict:
    """The watchdog's knobs, from results.db `settings` (missing = defaults)."""
    out = {}
    p = Path(root) / "results.db"
    if not p.exists():
        return out
    try:
        con = _ro(p)
        try:
            for r in con.execute("SELECT key, value FROM settings WHERE key LIKE 'watchdog_%'"):
                if r["value"] not in (None, ""):
                    out[r["key"]] = r["value"]
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return out


def _int(v, default: int, lo: int = 1) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, n)


def config(root) -> dict:
    s = _settings(root)
    return {
        "enabled": s.get(KEY_ENABLED, "1") != "0",
        "remind_min": _int(s.get(KEY_REMIND), DEFAULT_REMIND_MIN, 30),
        "stale_min": {p: _int(s.get(KEY_STALE % p), DEFAULT_STALE_MIN[p], MIN_STALE_MIN)
                      for p in PLATFORMS},
    }


# --------------------------------------------------------------------------
# the probes — "when did this collector last do a good unit of work?"
# --------------------------------------------------------------------------

@dataclass
class Probe:
    platform: str
    last_ok_ms: int | None = None     # last good unit of work
    last_seen_ms: int | None = None   # last activity of any kind (attempt)
    paused: bool = False
    sources: int = 0                  # enabled streams / sources
    detail: str = ""                  # the newest failure, if that is what it is doing
    error: str = ""                   # the probe itself could not read
    exists: bool = True               # the database is there at all
    cadence_ms: int = 0               # the platform's OWN "each source at most
                                      # every N" setting: quiet for less than
                                      # that plus slack is the setting working,
                                      # not an outage (Instagram, 2026-09-30)

    @property
    def watched(self) -> bool:
        """Silent on purpose is not silent: paused, nothing enabled, or no
        database yet means this platform is not in use here."""
        return self.exists and not self.error and not self.paused and self.sources > 0


def probe_x(root) -> Probe:
    p = Probe("x")
    db = Path(root) / "results.db"
    if not db.exists():
        p.exists = False
        return p
    try:
        con = _ro(db)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'collection_paused'").fetchone()
            p.paused = bool(row and row["value"] == "1")
            try:
                p.sources = con.execute(
                    "SELECT COUNT(*) n FROM streams WHERE paused = 0").fetchone()["n"]
            except sqlite3.OperationalError:       # pre-pause schema
                p.sources = con.execute("SELECT COUNT(*) n FROM streams").fetchone()["n"]
            # A poll that reached X and came back — whatever it found.
            # 'error' never reached it; 'no_account_or_abort' had nothing to
            # reach it with (pool starvation). Both are silence.
            row = con.execute(
                "SELECT MAX(COALESCE(finished_ms, started_ms)) t FROM polls "
                "WHERE stop_reason IS NOT NULL "
                "  AND stop_reason NOT IN ('error', 'no_account_or_abort')").fetchone()
            p.last_ok_ms = row["t"]
            row = con.execute("SELECT MAX(started_ms) t FROM polls").fetchone()
            p.last_seen_ms = row["t"]
            last = con.execute(
                "SELECT stop_reason, error, pages FROM polls "
                "ORDER BY started_ms DESC LIMIT 1").fetchone()
            if last and last["stop_reason"] in ("error", "no_account_or_abort"):
                why = (last["error"] or "").strip().splitlines()
                p.detail = (f"newest poll: pages={last['pages']} stop={last['stop_reason']}"
                            + (f" — {why[-1][:160]}" if why else ""))
        finally:
            con.close()
    except sqlite3.Error as e:
        p.error = f"{type(e).__name__}: {e}"
    return p


def _probe_sources(root, platform: str, dbname: str, pause_key: str) -> Probe:
    p = Probe(platform)
    db = Path(root) / dbname
    if not db.exists():
        p.exists = False
        return p
    try:
        con = _ro(db)
        try:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (pause_key,)).fetchone()
            p.paused = bool(row and row["value"] == "1")
            p.sources = con.execute("SELECT COUNT(*) n FROM sources WHERE enabled = 1").fetchone()["n"]
            row = con.execute("SELECT MAX(last_run) t FROM sources WHERE enabled = 1").fetchone()
            t = row["t"]
            p.last_ok_ms = int(float(t) * 1000) if t else None
            p.last_seen_ms = p.last_ok_ms
        finally:
            con.close()
    except sqlite3.Error as e:
        p.error = f"{type(e).__name__}: {e}"
    return p


def probe_instagram(root) -> Probe:
    p = _probe_sources(root, "instagram", "ig_results.db", "ig_paused")
    # The loop's own heartbeat says whether passes are even happening.
    try:
        hb = json.loads((Path(root) / "profiles" / "ig_loop.json").read_text())
        t = hb.get("last_pass") or hb.get("updated")
        if t:
            p.last_seen_ms = max(p.last_seen_ms or 0, int(float(t) * 1000))
    except (OSError, ValueError, TypeError):
        pass
    # "Each account at most once every N" (ig_interval_s): with N sources
    # and a long cadence the collector is quiet by design for stretches of
    # cadence / N — so the stale window is never shorter than the cadence.
    try:
        con = _ro(Path(root) / "ig_results.db")
        try:
            row = con.execute("SELECT value FROM settings WHERE key = 'ig_interval_s'").fetchone()
            if row and str(row["value"] or "").isdigit():
                p.cadence_ms = int(row["value"]) * 1000
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        pass
    return p


def probe_facebook(root) -> Probe:
    return _probe_sources(root, "facebook", "fb_results.db", "fb_paused")


def probe_all(root) -> dict:
    return {"x": probe_x(root), "instagram": probe_instagram(root),
            "facebook": probe_facebook(root)}


# --------------------------------------------------------------------------
# the decision — pure, so the tests can walk every branch
# --------------------------------------------------------------------------

def is_stale(p: Probe, stale_ms: int, first_seen_ms: int, now_ms: int) -> bool:
    """Down means: watched, and no good unit of work within the window. A
    platform that has NEVER done one counts from when the watchdog first
    saw it, so a fresh install gets its hour of grace like everyone else."""
    if not p.watched:
        return False
    ref = p.last_ok_ms if p.last_ok_ms is not None else first_seen_ms
    return (now_ms - ref) >= stale_ms


def decide(state: dict, p: Probe, cfg: dict, now_ms: int) -> tuple:
    """
    (action, entry) for one platform. action is one of
        "down"       page: it just went stale
        "remind"     page again: still down after remind_min
        "recovered"  page: it is back
        None         nothing to send
    `state` is this platform's entry from the state file and is updated in
    place — the caller persists it only after a send succeeds (or when
    nothing needed sending), so a failed send is retried next tick.
    """
    st = state
    st.setdefault("first_seen_ms", now_ms)
    stale_ms = cfg["stale_min"][p.platform] * 60_000
    if p.cadence_ms:
        # Quiet for up to one cadence plus an hour of slack is the setting
        # doing its job (each source visited at most that often).
        stale_ms = max(stale_ms, p.cadence_ms + 60 * 60_000)
    remind_ms = cfg["remind_min"] * 60_000
    stale = is_stale(p, stale_ms, st["first_seen_ms"], now_ms)
    st["stale"] = stale
    st["watched"] = p.watched
    st["last_ok_ms"] = p.last_ok_ms

    if not cfg["enabled"]:
        # Switched off: forget any open incident quietly, so switching back
        # on does not announce a recovery nobody was told about.
        st.pop("down_since_ms", None)
        st.pop("alerted_ms", None)
        return None, st

    if stale:
        if not st.get("down_since_ms"):
            st["down_since_ms"] = p.last_ok_ms or st["first_seen_ms"]
        if not st.get("alerted_ms"):
            return "down", st
        if now_ms - st["alerted_ms"] >= remind_ms:
            return "remind", st
        return None, st

    if st.get("alerted_ms"):
        return "recovered", st
    # Healthy, or not watched: no open incident to carry.
    st.pop("down_since_ms", None)
    return None, st


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("WATCHDOG_TZ", "Asia/Kolkata"))
    except Exception:
        return timezone.utc


def fmt_when(ms) -> str:
    if not ms:
        return "never"
    return datetime.fromtimestamp(ms / 1000, _tz()).strftime("%d %b %H:%M")


def fmt_dur(ms: int) -> str:
    s = max(0, int(ms // 1000))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m:02d}m"
    return f"{h // 24}d {h % 24}h"


def base_url() -> str:
    try:
        import decider
        return decider.base_url()
    except Exception:
        return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _watcher_alive(root, platform: str) -> str:
    """Is the process there at all? Only X has a lockfile to check; for the
    others the answer is in the message's own timestamps."""
    if platform != "x":
        return ""
    try:
        import auth
        pid = auth.read_watcher_pid(root)
    except Exception:
        return ""
    return (f"process: alive (pid {pid}) — running but not collecting"
            if pid else "process: NOT running")


def format_down(p: Probe, st: dict, cfg: dict, now_ms: int, root=".", remind=False) -> str:
    name = NAMES[p.platform]
    since = st.get("down_since_ms") or p.last_ok_ms
    lines = [f"🔴 {name} collector {'is STILL down' if remind else 'has stopped collecting'}",
             f"last good poll: {fmt_when(p.last_ok_ms)}"
             + (f" ({fmt_dur(now_ms - p.last_ok_ms)} ago)" if p.last_ok_ms else ""),
             f"threshold: {cfg['stale_min'][p.platform]} min"]
    if since and since != p.last_ok_ms:
        lines.append(f"silent since: {fmt_when(since)} ({fmt_dur(now_ms - since)})")
    if p.last_seen_ms and p.last_ok_ms and p.last_seen_ms > p.last_ok_ms:
        lines.append(f"still trying: last attempt {fmt_when(p.last_seen_ms)} — every one fails")
    if p.detail:
        lines.append(p.detail)
    alive = _watcher_alive(root, p.platform)
    if alive:
        lines.append(alive)
    lines.append("")
    lines.append(f"fix: sudo systemctl restart {UNITS[p.platform]}   "
                 f"(then: journalctl -u {UNITS[p.platform]} -n 50)")
    url = base_url()
    if url:
        lines.append(f"guard: {url}/#guard")
    return "\n".join(lines)


def format_recovered(p: Probe, st: dict, now_ms: int) -> str:
    since = st.get("down_since_ms") or st.get("alerted_ms") or now_ms
    return (f"🟢 {NAMES[p.platform]} collector is back — silent for "
            f"{fmt_dur((p.last_ok_ms or now_ms) - since)}, "
            f"good poll at {fmt_when(p.last_ok_ms)}.")


# --------------------------------------------------------------------------
# telegram — one urllib call, never raises
# --------------------------------------------------------------------------

def tg_send(token: str, chat: str, text: str, timeout=15) -> tuple:
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as rep:
            body = json.loads(rep.read().decode() or "{}")
        return (True, "") if body.get("ok", True) else (False, str(body.get("description"))[:200])
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        return False, f"HTTP {e.code}: {desc or e.reason}"[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]


def send(root, text: str) -> tuple:
    token, chat = bot_token(root), bot_chat(root)
    if not token or not chat:
        return False, "no watchdog bot — set WATCHDOG_TELEGRAM_BOT_TOKEN and a chat id"
    return tg_send(token, chat, text)


# --------------------------------------------------------------------------
# state — the only thing this module writes
# --------------------------------------------------------------------------

def load_state(root) -> dict:
    p = Path(root) / STATE_FILE
    try:
        st = json.loads(p.read_text())
        if isinstance(st, dict):
            st.setdefault("platforms", {})
            return st
    except (OSError, ValueError):
        pass
    return {"platforms": {}}


def save_state(root, st: dict) -> None:
    p = Path(root) / STATE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        os.replace(tmp, p)
    except OSError:
        pass


def service_status(root, now_ms=None) -> dict:
    """Is the WATCHDOG alive? (the dashboard and the guard ask this.)"""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    st = load_state(root)
    last = st.get("last_tick_ms")
    return {"ever_ran": bool(last),
            "last_tick_ms": last,
            "silent": bool(last) and (now - last) > SILENT_AFTER_S * 1000,
            "pid": st.get("pid"),
            "platforms": st.get("platforms", {})}


# --------------------------------------------------------------------------
# one tick
# --------------------------------------------------------------------------

def tick(root=".", *, log=print, sender=None, now_ms=None, dry=False) -> dict:
    """
    Probe every platform, decide, send what is due, persist. Returns a
    report the CLI prints and the dashboard shows. `sender(text) -> (ok,
    err)` is injectable so the tests record instead of paging; `dry` decides
    without sending or persisting alert state (the CLI's one-shot report).
    """
    root = Path(root)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    cfg = config(root)
    probes = probe_all(root)
    state = load_state(root)
    send_fn = sender or (lambda text: send(root, text))
    report = {"now_ms": now, "config": cfg, "bot": bot_status(root),
              "platforms": {}, "sent": []}

    for plat, p in probes.items():
        entry = dict(state["platforms"].get(plat) or {})
        action, entry = decide(entry, p, cfg, now)
        row = {**asdict(p), "watched": p.watched, "stale": entry.get("stale", False),
               "down_since_ms": entry.get("down_since_ms"),
               "alerted_ms": entry.get("alerted_ms"), "action": action}
        if p.error:
            log(f"[watchdog] {plat}: cannot read — {p.error}")
        if action and not dry:
            if action == "recovered":
                text = format_recovered(p, entry, now)
            else:
                text = format_down(p, entry, cfg, now, root=root, remind=(action == "remind"))
            ok, err = send_fn(text)
            if ok:
                if action == "recovered":
                    entry.pop("alerted_ms", None)
                    entry.pop("down_since_ms", None)
                else:
                    entry["alerted_ms"] = now
                report["sent"].append({"platform": plat, "action": action})
                log(f"[watchdog] {plat}: {action} — paged")
            else:
                # Do not remember an alert that never arrived: next tick tries again.
                log(f"[watchdog] {plat}: {action} but could not send — {err}")
                row["send_error"] = err
                if action == "recovered":
                    pass                       # keep alerted_ms; retry next tick
                else:
                    entry.pop("alerted_ms", None)
        report["platforms"][plat] = row
        if not dry:
            state["platforms"][plat] = entry

    if not dry:
        state["last_tick_ms"] = now
        state["pid"] = os.getpid()
        state["last_report"] = {k: {kk: vv for kk, vv in v.items()
                                    if kk in ("watched", "stale", "last_ok_ms", "paused",
                                              "sources", "action", "send_error")}
                                for k, v in report["platforms"].items()}
        save_state(root, state)
    return report


def run(root=".", *, log=print, stop=None, tick_s=TICK_S) -> int:
    """The service loop: one tick a minute, forever. One bad tick is logged
    and the next one runs — a watchdog that dies on an exception is a
    watchdog that has to be watched."""
    log(f"[watchdog] watching {', '.join(NAMES[p] for p in PLATFORMS)} every {tick_s}s "
        f"from {Path(root).resolve()}")
    bs = bot_status(root)
    if not bs["ready"]:
        log("[watchdog] NOTE: no bot/chat configured yet — nothing can be paged. "
            "Settings → Watchdog in the dashboard, or WATCHDOG_TELEGRAM_BOT_TOKEN "
            "and WATCHDOG_TELEGRAM_CHAT_ID in .env.")
    while True:
        try:
            tick(root, log=log)
        except Exception as e:
            log(f"[watchdog] tick failed: {type(e).__name__}: {e}")
        if stop is not None and stop.is_set():
            return 0
        time.sleep(tick_s)


# --------------------------------------------------------------------------
# the CLI report
# --------------------------------------------------------------------------

def report_lines(rep: dict) -> list:
    now = rep["now_ms"]
    out = []
    bot = rep["bot"]
    out.append("bot:  " + ("ready" if bot["ready"] else "NOT configured")
               + (f" ({'own' if bot['own_bot'] else 'admin/delivery'} bot {bot['token_hint']}…"
                  f", chat {bot['chat']})" if bot["has_token"] else ""))
    out.append("paging: " + ("on" if rep["config"]["enabled"] else "OFF")
               + f", remind every {rep['config']['remind_min']} min")
    for plat, r in rep["platforms"].items():
        th = rep["config"]["stale_min"][plat]
        if not r["exists"]:
            s = "no database — not in use"
        elif r["error"]:
            s = f"unreadable: {r['error']}"
        elif r["paused"]:
            s = "paused (on purpose)"
        elif not r["sources"]:
            s = "nothing enabled"
        else:
            ago = fmt_dur(now - r["last_ok_ms"]) if r["last_ok_ms"] else "never"
            s = (f"{'STALE' if r['stale'] else 'ok'} — last good {fmt_when(r['last_ok_ms'])}"
                 f" ({ago} ago), threshold {th} min")
            if r["detail"]:
                s += f"\n            {r['detail']}"
        out.append(f"  {NAMES[plat]:<10} {s}")
    return out
