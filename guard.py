"""
guard.py — the thing that stops you doing something you'd regret.

Every other module in this project does work. This one only ever says
"go ahead", "careful", or "no". It exists because the expensive mistakes here
are silent: nothing about clicking a button tells you that you are three
requests from a rate limit, that a watcher is already spending the same budget,
or that all four of your accounts share one IP.

Design rules:

  * ADVISORY BY DEFAULT, never silently destructive. It blocks an action or
    warns about it; it never changes state on its own.
  * Every finding carries a REMEDY. A warning you cannot act on is noise.
  * BLOCK is reserved for "this will damage something" — spending budget you do
    not have, or running with a broken twscrape. Everything else is a WARN the
    operator can override knowingly.
  * Checked server-side too. The dashboard asks the guard before offering a
    button, but /api/fetch re-checks independently — a UI check alone is a
    suggestion, not a control.

Run it standalone at any time:

    python3 main.py guard
    python3 main.py guard --action fetch --cost 5
"""

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field

BLOCK = "block"
WARN = "warn"
INFO = "info"

_RANK = {BLOCK: 0, WARN: 1, INFO: 2}

# X's window is 15 minutes. Keep a slice unspent: sustained 429s are themselves
# read as automation, so running at 100% is more dangerous than it is fast.
WINDOW_S = 900
RESERVE_FRACTION = 0.25

# Below this many requests left, even a small action is worth questioning.
HARD_FLOOR = 3


@dataclass
class Finding:
    level: str
    code: str
    title: str
    detail: str
    remedy: str = ""

    def line(self) -> str:
        tag = {BLOCK: "BLOCK", WARN: "WARN ", INFO: "note "}[self.level]
        return f"[{tag}] {self.title}"


@dataclass
class Verdict:
    action: str = ""
    cost: int = 0
    findings: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.level == BLOCK for f in self.findings)

    @property
    def warnings(self) -> list:
        return [f for f in self.findings if f.level == WARN]

    @property
    def blocks(self) -> list:
        return [f for f in self.findings if f.level == BLOCK]

    def sorted(self) -> list:
        return sorted(self.findings, key=lambda f: _RANK[f.level])

    def to_json(self) -> dict:
        return {
            "action": self.action,
            "cost": self.cost,
            "blocked": self.blocked,
            "findings": [asdict(f) for f in self.sorted()],
        }

    def summary(self) -> str:
        if self.blocked:
            return self.blocks[0].title
        if self.warnings:
            return self.warnings[0].title
        return "No risks detected."


# --------------------------------------------------------------------------
# state gathering (read-only, never mutates anything)
# --------------------------------------------------------------------------

def _budget(cfg, queue: str = "search") -> dict:
    """
    Best available view of the rate-limit budget for ONE queue.

    Queues are separate budgets, and wildly different sizes. Measured
    2026-07-29 on a live account:

        SearchTimeline            50 per 15 min
        ListLatestTweetsTimeline 500 per 15 min

    twscrape derives the queue from the GraphQL operation name, so a search
    stream and a list stream never draw from the same pool. Checking a list
    fetch against the search budget would block a perfectly affordable action —
    and, worse, letting a search fetch pass on the list budget would allow one
    that really is over the limit.

    X reports remaining/limit/reset on every response, so the most recent poll
    on that queue is the freshest truth we have. If that poll's reset time has
    already passed, the window rolled over and the budget is full again.
    """
    out = {"known": False, "remaining": None, "limit": None, "reset": None,
           "age_s": None, "window_rolled": False, "recent_requests": 0, "error": None}
    if not cfg.db_results.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{cfg.db_results}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        with con:
            # Column-aware on purpose. rl_reset was added later, and databases
            # created before that still work — a guard that silently disables
            # its most important rule because of a missing column is worse than
            # one that never existed.
            have = {r["name"] for r in con.execute("PRAGMA table_info(polls)")}
            cols = [c for c in ("rl_remaining", "rl_limit", "rl_reset", "started_ms")
                    if c in have]
            if "rl_remaining" not in have:
                out["error"] = "polls table has no rate-limit columns"
                con.close()
                return out

            # A stream's queue is implied by its source: list_id set means
            # ListLatestTweetsTimeline, otherwise SearchTimeline.
            scols = {r["name"] for r in con.execute("PRAGMA table_info(streams)")}
            if "list_id" in scols:
                pred = ("s.list_id IS NOT NULL AND s.list_id != ''" if queue == "list"
                        else "(s.list_id IS NULL OR s.list_id = '')")
            else:
                pred = "1" if queue == "search" else "0"
            join = "JOIN streams s USING(stream_id)"

            row = con.execute(
                f"SELECT {', '.join('p.' + c for c in cols)} FROM polls p {join} "
                f"WHERE p.rl_remaining IS NOT NULL AND {pred} "
                "ORDER BY p.started_ms DESC LIMIT 1"
            ).fetchone()
            # Our own count of requests in the last window, as a cross-check
            # for when X's headers are missing or stale.
            since = int((time.time() - WINDOW_S) * 1000)
            out["recent_requests"] = con.execute(
                f"SELECT COALESCE(SUM(p.pages), 0) n FROM polls p {join} "
                f"WHERE p.started_ms >= ? AND {pred}", (since,)
            ).fetchone()["n"]
        con.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if not row:
        return out
    row = dict(row)
    row.setdefault("rl_reset", None)

    now = time.time()
    out.update(known=True, remaining=row["rl_remaining"], limit=row["rl_limit"],
               reset=row["rl_reset"], age_s=int(now - row["started_ms"] / 1000))
    if row["rl_reset"] and row["rl_reset"] < now:
        # Window rolled over since that observation; assume a full budget.
        out["window_rolled"] = True
        out["remaining"] = row["rl_limit"]
    return out


# --------------------------------------------------------------------------
# account status taxonomy
# --------------------------------------------------------------------------
#
# Four states, and the colour is the whole point: an operator glancing at the
# panel must be able to tell "needs action now" from "fine" without reading.
#
#   green  live      Active, no complaints. Collecting normally.
#   amber  warning   Active and usable, but something raises ban risk or will
#                    bite later — no proxy, no known-device cookie, locked
#                    queues, or an unusually heavy request count.
#   red    dead      Not usable. X rejected it: session expired (32), access
#                    denied (326), or the rate-limit ban heuristic (88).
#   grey   unknown   Present but unclassifiable — usually a store read failure.
#                    NEVER shown as green: pretending an unknown account is
#                    healthy is the failure mode this taxonomy exists to avoid.
#
# amber is deliberately not a weaker red. red means "this account collects
# nothing right now"; amber means "it works, and here is what will hurt you".

STATUS_LIVE = "live"
STATUS_WARN = "warning"
STATUS_DEAD = "dead"
STATUS_UNKNOWN = "unknown"

# X error codes that mean the account itself is finished, not the request.
# twscrape deactivates on these (queue_client.py:218-238).
FATAL_CODES = {
    "32": "session expired or revoked",
    "326": "account locked by X",
    "88": "rate-limit ban heuristic tripped",
    "64": "account suspended",
}

# Above this many requests inside one window, an account looks less like a
# person and more like a script.
HEAVY_REQUESTS = 400


def classify_account(a) -> dict:
    """
    Map one account to (status, colour, reasons). Pure: no I/O, easy to test.

    Returns the reasons too, because a red dot with no explanation just moves
    the debugging problem somewhere else.
    """
    reasons, status = [], STATUS_LIVE

    if not a.active:
        status = STATUS_DEAD
        msg = (a.error_msg or "").strip()
        hit = next((d for c, d in FATAL_CODES.items() if f"({c})" in msg), "")
        reasons.append(hit or (msg[:120] if msg else "marked inactive, no reason recorded"))
        return {"status": status, "reasons": reasons,
                "action": "python3 main.py login --account <label> --force"}

    if not a.real_user_agent:
        status = STATUS_WARN
        reasons.append("placeholder user-agent — re-login to capture the browser's real one")
    if not a.has_known_device:
        status = STATUS_WARN
        reasons.append("no kdt cookie: X does not treat this as a trusted device")
    if not a.proxy:
        status = STATUS_WARN
        reasons.append("no proxy — shares this machine's IP with every other account")
    if getattr(a, "requests", 0) > HEAVY_REQUESTS:
        status = STATUS_WARN
        reasons.append(f"{a.requests} requests served — heavy usage on one account")
    if a.error_msg:
        status = STATUS_WARN
        reasons.append(f"last error: {a.error_msg[:100]}")

    return {"status": status, "reasons": reasons,
            "action": "" if status == STATUS_LIVE else "Add more accounts, or set a per-account proxy."}


@dataclass
class AccountView:
    username: str
    active: bool
    proxy: str | None
    error_msg: str | None
    has_known_device: bool
    real_user_agent: bool
    requests: int


def _accounts(cfg):
    """
    Read accounts.db directly, with plain sqlite3.

    Deliberately NOT via auth.health(), which is async: the guard is called
    from a CLI command that already runs inside asyncio.run(), from the web
    server's own loop, and from plain sync code. Nesting event loops in the
    first case raises, and the earlier version swallowed that into an empty
    list — which made the guard report "no accounts" for a perfectly healthy
    account. A false BLOCK is worse than no guard at all.

    accounts.db is just SQLite, so reading it needs no loop and cannot
    contend with twscrape's module-level lock.

    Returns (accounts, error). A read failure is surfaced, never swallowed.
    """
    if not cfg.db_accounts.exists():
        return [], None
    try:
        con = sqlite3.connect(f"file:{cfg.db_accounts}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        with con:
            rows = con.execute(
                "SELECT username, active, cookies, user_agent, proxy, error_msg, stats "
                "FROM accounts"
            ).fetchall()
        con.close()
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    out = []
    for r in rows:
        try:
            cookies = json.loads(r["cookies"] or "{}")
        except Exception:
            cookies = {}
        try:
            stats = json.loads(r["stats"] or "{}")
        except Exception:
            stats = {}
        out.append(AccountView(
            username=r["username"],
            active=bool(r["active"]),
            proxy=r["proxy"],
            error_msg=r["error_msg"],
            has_known_device="kdt" in cookies,
            real_user_agent=not (r["user_agent"] or "").startswith("@"),
            requests=sum(v for v in stats.values() if isinstance(v, int)),
        ))
    return out, None


def _poll_stats(cfg) -> dict:
    out = {"page_budget": 0, "starved": 0, "errors": 0, "total": 0, "gaps": 0}
    if not cfg.db_results.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{cfg.db_results}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        since = int((time.time() - 3600) * 1000)
        with con:
            for r in con.execute(
                "SELECT stop_reason, COUNT(*) n FROM polls WHERE started_ms >= ? "
                "GROUP BY stop_reason", (since,)
            ):
                out["total"] += r["n"]
                if r["stop_reason"] == "page_budget":
                    out["page_budget"] = r["n"]
                elif r["stop_reason"] == "no_account_or_abort":
                    out["starved"] = r["n"]
                elif r["stop_reason"] == "error":
                    out["errors"] = r["n"]
            out["gaps"] = con.execute(
                "SELECT COUNT(*) n FROM gaps WHERE status = 'open'"
            ).fetchone()["n"]
        con.close()
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

def assess(cfg, action: str = "", cost: int = 0, host: str = "",
           queue: str = "search") -> Verdict:
    """
    Evaluate current state, optionally against an action about to be taken.

    action: "fetch" | "watch" | "login" | "" (general health check)
    cost:   requests the action will spend (1 per page)
    queue:  "search" or "list" — separate budgets, 50 vs 500 per window
    """
    v = Verdict(action=action, cost=cost)
    add = v.findings.append

    import auth as _auth

    # --- twscrape compatibility: everything else is meaningless if this moved
    try:
        from engine import check as compat_check

        rep = compat_check()
        if not rep.ok:
            broken = [x for x in rep.lines if x.startswith("BROKEN")]
            add(Finding(
                BLOCK, "compat.broken",
                "twscrape internals changed — results may be silently wrong",
                "; ".join(b[:110] for b in broken[:2]),
                "Run `python3 main.py doctor --selftest`. Usually a twscrape "
                "upgrade; re-pin it or update this project to match.",
            ))
    except Exception as e:
        add(Finding(WARN, "compat.unchecked", "Could not run the compatibility check",
                    f"{type(e).__name__}: {e}", "Run `python3 main.py doctor --selftest`."))

    # --- accounts
    accounts, acc_err = _accounts(cfg)
    live = [a for a in accounts if a.active]
    dead = [a for a in accounts if not a.active]

    if acc_err:
        # Never let a read failure masquerade as "no accounts" — that would
        # produce a confident, wrong BLOCK.
        add(Finding(
            WARN, "account.unreadable", "Could not read the session store",
            f"{cfg.db_accounts}: {acc_err}",
            "Account checks below are incomplete. Run `python3 main.py doctor --accounts`.",
        ))
    elif not accounts:
        add(Finding(
            BLOCK, "account.none", "No accounts in the session store",
            "Nothing can talk to X until an account is logged in.",
            "python3 main.py login --all",
        ))
    elif not live:
        add(Finding(
            BLOCK, "account.none_active", "No active account",
            "; ".join(f"@{a.username}: {(a.error_msg or 'inactive')[:70]}" for a in dead[:3]),
            "python3 main.py login --all --refresh-only   (silent) "
            "or  --force  if the session is genuinely gone.",
        ))
    else:
        if dead:
            add(Finding(
                WARN, "account.some_dead",
                f"{len(dead)} of {len(accounts)} accounts are inactive",
                "; ".join(f"@{a.username}: {(a.error_msg or 'inactive')[:60]}" for a in dead[:3]),
                "Check `doctor --accounts`. Code (32) means the session expired — "
                "re-login. (326)/(88) suggest the account is restricted.",
            ))
        if len(live) == 1:
            add(Finding(
                WARN, "account.single",
                "Only one active account — a single point of failure",
                f"@{live[0].username} carries all traffic. If X restricts it, "
                f"collection stops entirely, and one account has the smallest "
                f"rate budget available.",
                "Add 2-4 more throwaway accounts in config.toml. Losing one then "
                "costs a fraction of throughput instead of everything.",
            ))
        no_proxy = [a for a in live if not a.proxy]
        if len(live) > 1 and len(no_proxy) > 1:
            add(Finding(
                WARN, "account.shared_ip",
                f"{len(no_proxy)} accounts share one IP address",
                "Several accounts polling the same queries from one address "
                "correlate to a single operator, so a ban on one raises "
                "suspicion on the others.",
                "Set `proxy` per account in config.toml. Residential beats "
                "datacenter. Never set TWS_PROXY — it overrides all of them.",
            ))
        for a in live:
            if not a.has_known_device:
                add(Finding(
                    INFO, "account.no_kdt",
                    f"@{a.username} is not on X's trusted-device path",
                    "The kdt (known-device) cookie is absent, so X is likelier "
                    "to challenge this account on future logins.",
                    "Harmless day to day. Keep profiles/ backed up so the "
                    "browser profile itself stays trusted.",
                ))
                break

    # --- rate budget: the rule the operator most often trips
    b = _budget(cfg, queue)
    if b.get("error"):
        add(Finding(
            WARN, "rate.unreadable", "Cannot read the rate-limit budget",
            f"{b['error']} — so the check that would stop an over-budget fetch "
            f"is not running.",
            "Run `python3 main.py watch --once` once to bring the database "
            "schema up to date.",
        ))
    elif cost and not b["known"]:
        add(Finding(
            WARN, "rate.unknown", "No rate-limit reading yet",
            f"Nothing has recorded X's budget headers, so this {cost}-request "
            f"action cannot be checked against the limit ({cost * 20} tweets, "
            f"out of ~50 requests per 15 minutes).",
            "Proceed if the number looks small to you. After the first fetch "
            "the real budget is known and enforced.",
        ))
    if b["known"] and b["limit"]:
        remaining = b["remaining"] or 0
        reserve = max(HARD_FLOOR, int(b["limit"] * RESERVE_FRACTION))
        after = remaining - cost
        per_account = max(1, len(live) or 1)

        if cost and after < 0:
            add(Finding(
                BLOCK, "rate.exceeded",
                f"Not enough {queue} budget: {cost} requests needed, {remaining} left",
                f"X allows {b['limit']} {queue} requests per 15 minutes per account. "
                f"Going over returns 429s, and sustained 429s are themselves "
                f"read as automation.",
                f"Wait {_reset_in(b)} for the window to reset, or fetch fewer "
                f"pages ({max(0, remaining)} available now).",
            ))
        elif cost and after < reserve:
            add(Finding(
                WARN, "rate.reserve",
                f"This would leave {after} of {b['limit']} requests — below the "
                f"{reserve} kept in reserve",
                f"That headroom exists so the watcher can keep streams fresh "
                f"and so a burst does not trigger 429s. Spending it is not fatal, "
                f"but it is the same budget collection depends on.",
                f"Fetch fewer pages, or wait {_reset_in(b)} for the reset.",
            ))
        elif remaining <= reserve and not cost:
            add(Finding(
                WARN, "rate.low",
                f"Rate budget is low: {remaining} of {b['limit']} left",
                f"Resets in {_reset_in(b)}.",
                "Avoid manual fetches until the window rolls over.",
            ))

        if b["age_s"] is not None and b["age_s"] > WINDOW_S and not b["window_rolled"]:
            add(Finding(
                INFO, "rate.stale",
                "Rate-limit reading is stale",
                f"Last observed {b['age_s'] // 60} minutes ago; the real budget "
                f"is probably higher.",
                "It refreshes on the next request.",
            ))

    # --- a watcher and a human competing for the same budget
    watcher = _auth.read_watcher_pid(cfg.root)
    if watcher and action in ("fetch", "search"):
        add(Finding(
            WARN, "budget.contended",
            f"A watcher is running (pid {watcher}) and shares this budget",
            "Manual fetches take requests the poller needs, which shows up as "
            "higher lag on your streams — or as pool starvation if it runs out.",
            "Fine occasionally. If you need lots of ad-hoc queries, stop the "
            "watcher first or add another account.",
        ))
    if watcher and action == "login":
        add(Finding(
            WARN, "login.during_watch",
            f"A watcher is running (pid {watcher})",
            "Re-authenticating an account the collector is actively using can "
            "clear locks it depends on.",
            "Stop the watcher first unless the account is already dead.",
        ))

    # --- collection health
    st = _poll_stats(cfg)
    if st["starved"]:
        add(Finding(
            WARN, "collect.starved",
            f"{st['starved']} polls in the last hour got no account at all",
            "That is pool starvation, NOT a quiet stream. Every account was "
            "rate-limited or locked when the poll ran.",
            "Add accounts, raise min_interval_s, or reduce the number of streams.",
        ))
    if st["page_budget"] >= 3:
        add(Finding(
            WARN, "collect.behind",
            f"{st['page_budget']} polls in the last hour hit the page ceiling",
            "Streams are producing tweets faster than the poller collects them, "
            "so gaps are being recorded.",
            "Raise max_pages_per_poll, lower min_interval_s, or narrow the query.",
        ))
    if st["errors"]:
        add(Finding(WARN, "collect.errors", f"{st['errors']} polls errored in the last hour",
                    "Check the watcher output.", "Run `python3 main.py doctor`."))
    if st["gaps"]:
        add(Finding(
            INFO, "collect.gaps", f"{st['gaps']} open gaps recorded",
            "Windows the poller could not reach. They are recorded rather than "
            "hidden, but nothing fills them automatically yet.",
            "Narrow the query or poll faster to stop new gaps forming.",
        ))

    # --- exposure
    if host and host not in ("127.0.0.1", "localhost", "::1"):
        add(Finding(
            WARN, "web.exposed", f"The dashboard is bound to {host}, not localhost",
            "It has no authentication and can spend your rate-limit budget. "
            "Anyone who can reach this port can drive it.",
            "Bind to 127.0.0.1 and use an SSH tunnel for remote access.",
        ))

    # --- backups
    prof = cfg.profiles_dir
    if prof.exists() and not (cfg.root / "profiles.backup").exists():
        add(Finding(
            INFO, "backup.profiles", "Browser profiles are not backed up",
            "profiles/ IS the trusted-device asset. Losing it means redoing "
            "headed logins and facing new-device challenges on every account.",
            "With Chrome closed: cp -r profiles profiles.backup",
        ))

    return v


def _reset_in(b) -> str:
    if not b.get("reset"):
        return "up to 15 minutes"
    secs = int(b["reset"] - time.time())
    if secs <= 0:
        return "now"
    return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

def report(cfg, action="", cost=0, log=print, queue="search") -> int:
    """Human-readable report. Returns an exit code: 0 ok, 1 warnings, 2 blocked."""
    v = assess(cfg, action=action, cost=cost, queue=queue)
    if action:
        log(f"== guard: {action}" + (f" (costs {cost} requests)" if cost else "") + " ==")
    else:
        log("== guard ==")

    if not v.findings:
        log("  Nothing to flag. Safe to proceed.")
        return 0

    for f in v.sorted():
        log(f"  {f.line()}")
        if f.detail:
            log(f"         {f.detail}")
        if f.remedy:
            log(f"         -> {f.remedy}")
        log("")

    if v.blocked:
        log("  VERDICT: blocked. Fix the items above first.")
        return 2
    if v.warnings:
        log(f"  VERDICT: {len(v.warnings)} warning(s). Proceed only if you meant to.")
        return 1
    return 0
