"""
collect_ig.py — the piece that was missing: run engine_ig over the configured
sources and SAVE what it finds into store_ig. This is what turns "engine_ig can
fetch" into "there is Instagram data to serve".

It reuses the exact freshness idea collector.py uses for X: walk newest-first
and STOP at the watermark (the newest post already stored), so a normal poll
reads one page and no post is fetched twice. IG media pks are time-monotonic, so
the same numeric "have I reached known ground" test works — no snowflake math.

CLI (the working model — simple on purpose):

  python3 collect_ig.py add-source --label "Narendra Modi" --type user --value narendramodi
  python3 collect_ig.py add-source --label home   --type following
  python3 collect_ig.py set-id --label "Narendra Modi" --id 1550693326
  python3 collect_ig.py resolve-ids
  python3 collect_ig.py list-sources
  python3 collect_ig.py run                 # one pass over every enabled source
  python3 collect_ig.py run --loop --every 300

NAME YOUR SOURCES HOWEVER YOU LIKE — the numeric id is no longer your problem.
A source carries three separate things and the collector keeps them apart:

    label        the PERSON     "Narendra Modi"   <- your cross-platform key
    value        the HANDLE     narendramodi      <- readable, yours to set
    platform_id  the NUMERIC ID 1550693326        <- resolved once, cached

Instagram grants name lookup and media reads as separate permissions, and a
restricted session (anything with an open checkpoint) routinely serves media by
id while refusing to resolve the name. That used to mean a source keyed by name
was uncollectable. Now the id is resolved once — via the private API, the web
web_profile_info endpoint, or the profile HTML, whichever answers — cached into
platform_id, and used for every fetch after that. The handle and the label are
never overwritten, so the identity mapping that links this account to the same
person on Facebook and X stays intact.

If all three lookups refuse (a badly restricted session), type the id in once:

  python3 collect_ig.py set-id --label "Narendra Modi" --id 1550693326
  python3 collect_ig.py resolve-ids      # or: try every pending source, paced

To find an id by hand: open https://www.instagram.com/<name>/ , view source,
search for "profile_id".

A `following` source needs the HOME feed, which is account-scoped and is the
first thing Instagram withdraws under a checkpoint. user/hashtag sources are
unaffected, which is why one failing source no longer stops the others.

Sources are stored in ig_results.db (store_ig), so the API and a future
dashboard read the same list.

WHO COLLECTS WHAT (since 2026-09-04). Every ACTIVE Instagram login with a
session and no checkpoint is a collector, and they run IN PARALLEL — one
asyncio task per account, each on its own phone (ig_identity), its own proxy,
its own human rhythm (ig_human, shifted per account) and its own daily budget.
Every source is owned by exactly one of them: a human pin (`--account`) wins,
otherwise the collector assigns it once and keeps it there (sticky) until the
owner drops out, at which point it moves to the least-loaded healthy account
and the move is logged (store_ig.assign_sources). No sort order ever decides.

WHEN IT COLLECTS (since 2026-09-04, later the same day). The --loop is not a
pass every N seconds. Each phone has a PLAN for its local day
(ig_human.day_plan): two to four sessions of random length at random times,
plus a glance or two, drawn from a seed so a restart replays it. Only while a
phone is in hand does the loop call run_once — and then as a VISIT
(max_sources=1): the account reads its single most overdue source, the loop
sleeps a human gap (floored so the day's budget lasts the day's plan), and
comes back for the next one. Ten sources are read one at a time across an
hour, not ten in a row and then silence. --every (the dashboard cadence) is
the per-source floor: a source seen within that window is not due. Fetch-now
and `run` without --loop are still full passes over everything.

ONE PASS AT A TIME, ACROSS PROCESSES. The service loop and the dashboard's
Fetch-now both call run_once; a file lock (profiles/.ig_pass.lock) makes the
second one say "a pass is already running" instead of starting a second wave
of the same profiles from other phones — the overlap that ended in a
checkpoint on 2026-09-03 (23:45 pass on @youssef, 23:47 pass on @sana,
23:49 ChallengeRequired).
"""

import argparse
import asyncio
import json
import os
import random
import time
from pathlib import Path

import asyncio as _asyncio

import activity_log
import decider
import ig
import ig_human
import ig_identity
import pool_link
import ig_session
import store_ig
from engine_ig import IGEngine
from instagrapi.exceptions import LoginRequired

# One process-lifetime day counter, shared across passes, so the per-account
# daily request budget is honored by the long-lived --loop service.
_DAY = ig_human.DayCounter()


def _persist_log(log=None):
    """Default logger: print AND persist to the account-activity log, so the
    dashboard's Account Log shows what the Instagram accounts are doing."""
    if log is not None and log is not print:
        return log
    return activity_log.logger("instagram")


ACCOUNTS_DB = "ig_accounts.db"


def _active_account(store_path=ACCOUNTS_DB) -> str:
    """Kept for the CLI paths that take ONE login (resolve-ids without
    --account): the first active row. Collection itself never calls this any
    more — see collectors()."""
    with ig.Store(store_path) as st:
        rows = [r for r in st.all() if r.get("active")]
    if not rows:
        raise RuntimeError("no active Instagram account — sign one in under "
                           "Accounts & Sessions (or ig_login.py / ig_import.py)")
    return rows[0]["username"]


def collectors(*, store_path=ACCOUNTS_DB, root=".", log=print) -> tuple:
    """(owners, benched): every active login that can collect, and the ones
    that cannot with the reason. An owner keeps its sources even while it
    rests (rate limit / budget) — resting is a pause, not a departure; only a
    benched account loses them. Benched: inactive, no session at all, or a
    recorded checkpoint."""
    owners, benched = [], {}
    with ig.Store(store_path) as st:
        rows = st.all()
    for r in rows:
        u = r["username"]
        if not r.get("active"):
            continue
        sc = ig_session.sidecar_path(u, root)
        meta = {}
        if sc.exists():
            try:
                meta = json.loads(sc.read_text()).get("meta", {}) or {}
            except (OSError, ValueError):
                meta = {}
        else:
            try:
                jar = json.loads(r.get("cookies") or "{}")
            except ValueError:
                jar = {}
            if not jar.get("sessionid"):
                benched[u] = "no session"
                continue
        if meta.get("checkpoint_at"):
            benched[u] = f"checkpoint at {meta['checkpoint_at']}"
            continue
        owners.append(u)
    return owners, benched


class PassLock:
    """One collection pass at a time on this machine, whichever process asks.
    fcntl.flock on profiles/.ig_pass.lock; the file carries who holds it."""

    def __init__(self, root="."):
        self.path = Path(root) / "profiles" / ".ig_pass.lock"
        self.fh = None
        self.unlocked = ""      # why no lock could be taken, if so

    def acquire(self, who="loop") -> bool:
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError as e:
            # A lock file that cannot be opened must not stop collection;
            # it is said (run_once logs it) and the pass runs unguarded.
            self.unlocked = f"{type(e).__name__}: {e}"
            return True
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0); fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "who": who, "since": time.time()}))
        fh.flush()
        self.fh = fh
        return True

    def holder(self) -> dict:
        try:
            return json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return {}

    def release(self):
        import fcntl
        if self.fh is None:
            return
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


HEARTBEAT = "profiles/ig_loop.json"


def heartbeat(root=".", **fields) -> None:
    """What the loop is doing, for the dashboard's diag view: written every
    pass, never read by the collector. A missing file means no loop ran."""
    path = Path(root) / HEARTBEAT
    try:
        cur = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        cur = {}
    cur.update(fields, pid=os.getpid(), updated=time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cur, indent=1))
    except OSError:
        pass


def ig_failover(quarantined: str, reason: str, *, store_path="ig_accounts.db",
                root=".", log=print) -> str:
    """The self-healing move for a checkpoint: mark @quarantined inactive with
    the reason, and if it WAS the active account, promote the first other
    account that has a usable session and no recorded checkpoint. Returns the
    new active username, or "" when there is nobody to hand over to.

    Sources that name their own account (`sources.account`) do not move — a
    source pinned to a handle is pinned on purpose. Sources with no account
    follow the active row (`_active_account`), so they continue on the next
    pass without anyone touching them. The pool (store_accounts) is told the
    same story through pool_link so the Account Control Panel agrees.
    """
    with ig.Store(store_path) as st:
        rows = st.all()
        was_active = any(r["username"] == quarantined and r.get("active")
                         for r in rows)
        st.set_active(quarantined, False, error=reason[:300])
        if not was_active:
            return ""
    # With N collectors nothing usually needs promoting: the next pass finds
    # one owner fewer and moves its unpinned sources to the least-loaded
    # remaining account (store_ig.assign_sources). Name who that will be, so
    # the ping can say it.
    owners, _ = collectors(store_path=store_path, root=root, log=log)
    others = [u for u in owners if u != quarantined]
    if others:
        log(f"  failover: @{quarantined} is out; its unpinned sources move to "
            f"{', '.join('@' + u for u in others)} on the next pass")
        return others[0]
    # Nobody else is collecting: wake a WARM BACKUP — the first inactive row
    # with a usable session, no error and no recorded checkpoint — so a
    # single checkpoint never stops collection outright. The pool is told
    # (pool_link.promote) so the Account Control Panel agrees.
    with ig.Store(store_path) as st:
        for r in rows:
            u = r["username"]
            if u == quarantined or r.get("active") or r.get("error_msg"):
                continue
            sc = ig_session.sidecar_path(u, root)
            if sc.exists():
                try:
                    meta = json.loads(sc.read_text()).get("meta", {})
                except (OSError, ValueError):
                    meta = {}
                if meta.get("checkpoint_at"):
                    continue
            elif not r.get("cookies"):
                continue
            st.set_active(u, True)
            pool_link.promote("ig", u)
            log(f"  failover: @{quarantined} is out and nobody else was collecting; "
                f"warm backup @{u} is now active (unpinned sources move to it)")
            return u
    return ""


def _proxy_broken(dec, acct, group, e, why, *, log=print):
    """One 'proxy_broken' condition on the ACCOUNT, naming the proxy.

    `why` is 'tls_intercepted' (the exit re-signs HTTPS — a Sophos/ISP
    firewall; the server rightly refuses the forged certificate) or
    'network' (the connection died before any reply). Every per-handle
    'unresolved_source' card on this account is folded in — including the
    ones filed as 'unknown' by a collector that predates this rule, because
    on an account whose pipe is dead they were all this. If the proxy is
    fixed and a handle still will not resolve, its own card comes back.
    """
    from engine_ig import NETWORK_ADVICE
    pid = ""
    try:
        row = pool_link.find("ig", acct)
        pid = (getattr(row, "proxy_id", "") or "") if row else ""
    except Exception:
        pid = ""
    where = f"proxy {pid}" if pid else "its proxy"
    if why == "tls_intercepted":
        detail = (f"TLS verification failed through {where} — the exit "
                  f"intercepts HTTPS (\"unable to get local issuer certificate\")")
    else:
        detail = f"connection dies through {where} before Instagram answers ({type(e).__name__})"
    pending = sorted(x.label for x in group
                     if x.type == "user" and not x.platform_id
                     and not str(x.value).isdigit())
    dec.fold(acct, "unresolved_source", "proxy_broken",
             keep=lambda m: m.get("why") in ("tls_intercepted", "network", "unknown", None))
    return dec.on("proxy_broken", acct, detail=detail,
                  meta={"why": why, "proxy": pid, "pending": pending,
                        "note": NETWORK_ADVICE.get(why, "")})


def _decide_exc(dec, acct, group, e, *, fallback, log=print, src=None):
    """Every exception an account throws goes through here, so a checkpoint
    is handled ONE way wherever it surfaces: quarantine in the pool, record
    it in the sidecar (refresh refuses to knock on it), fail over, and tell
    the decider what happened so the ping says so.

    `src` is the source being collected when it happened. A handle that
    cannot be resolved to an id is THAT SOURCE's condition, not the
    account's: it is skipped, the other sources continue, and after three
    passes the admin is asked for the id (decider 'unresolved_source')."""
    reason = f"{type(e).__name__}: {e}"
    kind = decider.classify_exception(e)
    if kind == "pass_error":
        kind = fallback
    # The request never reached Instagram. Whether it surfaced as a resolve
    # failure (the lookup was the first request through the dead pipe) or as
    # a raw connection/TLS error from a post read, it is ONE account
    # condition — the proxy — and never a per-handle one.
    net = ""
    if kind == "unresolved_source":
        if getattr(e, "why", "") in ("tls_intercepted", "network"):
            net = e.why
    elif kind == "proxy_broken":
        from engine_ig import network_why
        net = network_why(e) or "network"
    if net:
        return _proxy_broken(dec, acct, group, e, net, log=log)
    if kind == "unresolved_source" and src is not None:
        why = getattr(e, "why", "unknown")
        # first line of the engine's message = the advice for THIS kind of
        # refusal (throttled / blocked IP / no such user); it rides along to
        # the Fix panel and the ping as the note.
        first = str(e).split("\n", 1)[0]
        advice = first.split("numeric id. ", 1)[-1] if "numeric id. " in first else ""
        if why in ("rate_limited", "blocked"):
            # The SESSION is refusing lookups, not this handle. One
            # account-level condition, and the caller stops asking for the
            # rest of the pass (Decision.kind == 'lookup_throttled').
            pending = sorted(x.label for x in group
                             if x.type == "user" and not x.platform_id
                             and not str(x.value).isdigit())
            dec.fold(acct, "unresolved_source", "lookup_throttled",
                     keep=lambda m: m.get("why") in ("rate_limited", "blocked"))
            return dec.on("lookup_throttled", acct, source="lookups",
                          detail={"rate_limited": "429, throttled",
                                  "blocked": "bounced to login / 401 — datacenter IP"}[why],
                          meta={"why": why, "pending": pending, "note": advice})
        return dec.on(kind, acct, detail=src.value or src.label, source=src.label,
                      meta={"label": src.label, "handle": src.value, "why": why,
                            "note": advice})
    if kind != "checkpoint":
        return dec.on(kind, acct, detail=reason)
    pool_link.quarantine("ig", acct, reason)
    ig_session._note_checkpoint(acct, ".", log=log)
    new = ig_failover(acct, reason, log=log)
    if new:
        note = (f"Collection failed over to @{new}: @{acct} is out of rotation "
                f"and its unpinned sources move there on the next pass "
                f"(sources pinned to @{acct} wait).")
    else:
        note = ("No other Instagram account with a working session — "
                "collection is STOPPED until this is fixed or another account "
                "is signed in under Accounts & Sessions.")
    return dec.on("checkpoint", acct, detail=reason,
                  meta={"sources": [s.label for s in group],
                        "failover_to": new, "note": note})


async def collect_source(engine, store, source, *, page_size=12, max_pages=2, log=print) -> int:
    """One pass over a source: collect posts newer than the watermark, save them.

    max_pages defaults to 2, not 5. A routine poll reads ONE page and stops at
    the watermark, so the only run that ever spends the full budget is the cold
    start — and a cold start that opens with five back-to-back requests is
    exactly how you earn a PleaseWaitFewMinutes on a fresh session (it is how we
    earned ours). Two pages is enough backlog to be useful; raise it with
    --max-pages once the account is warm, and note the watermark means you only
    pay it once.
    """
    wm = store.watermark(source.label)
    collected, newest, stop = [], None, False

    first_page = True
    async for page in engine.pages_for(source, page_size=page_size, max_pages=max_pages):
        # Human rhythm BETWEEN pages of the same source: a person scrolls, then
        # pauses, before loading more — not a steady machine tick. The first
        # page of a source loads immediately (you just opened it).
        if not first_page:
            await _asyncio.sleep(ig_human.request_gap())
        first_page = False
        if newest is None and page.result_ids:
            newest = max(page.result_ids)
        for pk in page.result_ids:
            if wm and pk <= wm:          # reached known ground
                stop = True
                break
            rec = page.entries_by_id.get(pk)
            if rec:
                collected.append(rec)
        if stop:
            break

    # project_id comes from the source row, not from this call — see
    # store_ig.upsert_posts. A post belongs to whoever was watching for it.
    new = store.upsert_posts(collected, source.label, source.project_id)
    if newest and (not wm or newest > wm):
        store.set_watermark(source.label, newest)
    log(f"  [{source.label}] type={source.type} handle={source.value or '-'} "
        f"id={source.platform_id or '(unresolved)'} "
        f"project={source.project_id or '(unassigned)'} "
        f"new={new} (had watermark={'yes' if wm else 'no'})")
    return new


def _due_sources(group, last_runs, *, now, due_after=0, limit=1) -> list:
    """The trickle's pick: of `group`, the sources not visited within
    `due_after` seconds, most overdue first (never visited = most overdue of
    all), ties by label so the order is stable. At most `limit` of them."""
    due = [s for s in group
           if now - float(last_runs.get(s.label) or 0) >= float(due_after or 0)]
    due.sort(key=lambda s: (float(last_runs.get(s.label) or 0), s.label))
    return due[:max(1, int(limit))]


async def _collect_account(acct, group, store, dec, *, page_size, max_pages,
                           log, budget_override=None, max_sources=None,
                           due_after=0) -> tuple:
    """Everything ONE account does in a pass, over the sources it owns.
    Returns (new_posts, ok). Runs concurrently with the other accounts'
    tasks; nothing here touches another account's state.

    Two shapes. A FULL pass (max_sources=None — Fetch-now, the CLI) walks
    every source the account owns, with human gaps between them. A VISIT
    (max_sources=N — the --loop trickle, 2026-09-04) picks the N most
    overdue sources not seen within `due_after` seconds and reads only
    those; the loop calls again after a human gap, all session long. Same
    reads, spread across the phone-time instead of fired in one burst."""
    total = 0
    full = group            # every source this account owns, for the decider
    trickle = bool(max_sources)
    if not trickle:
        log(f"account @{acct}: {len(group)} source(s)")

    # Sources still without a numeric id. Two things before any of
    # them costs a lookup request:
    #   1. one that was refused recently is LEFT ALONE (decider
    #      hold-off: 1h doubling to 24h) — asking a throttled lookup
    #      endpoint again every pass is what keeps the 429 alive;
    #   2. the rest are resolved off this account's FOLLOWING list in
    #      one ordinary request, no lookup endpoint touched. Follow
    #      the sources from the collecting account and this is the
    #      path that always works (engine_ig.resolve_from_following).
    # Decided here, before the session is even loaded: a hold is the
    # decider's memory, not Instagram's answer, and the trickle must not
    # pick a source it is then told to leave alone.
    held = set()
    unresolved = [s for s in full
                  if s.type == "user" and not s.platform_id
                  and not str(s.value).isdigit()]
    # The account-level breaker first: while lookups from this
    # session are refused, NO handle is asked about — not the
    # following list either. Sources with ids collect as usual.
    acct_hold = dec.holdoff(acct, "lookups")
    if acct_hold and unresolved:
        held.update(s.label for s in unresolved)
        if not trickle:
            log(f"  {len(held)} source(s) wait for ids — lookups from @{acct} "
                f"are held for {acct_hold // 3600}h {(acct_hold % 3600) // 60:02d}m "
                f"more (refused earlier)")
    for s in unresolved:
        if s.label in held:
            continue
        if dec.holdoff(acct, s.label) > 0:
            held.add(s.label)
    if held and not acct_hold and not trickle:
        log(f"  leaving {len(held)} unresolved source(s) alone this pass "
            f"({', '.join(sorted(held))}) — hold-off after a refused lookup")

    # Daily budget per account (warm-up ramp for young sessions). Once
    # spent, this account rests until tomorrow — a person doesn't open
    # the app 500 times a day, and a burner that does gets flagged.
    budget = ig_human.daily_budget() if budget_override is None else budget_override
    if _DAY.remaining(acct, budget) <= 0:
        dec.on("budget_spent", acct,
               detail=f"daily budget spent ({budget}) — resting until tomorrow")
        return 0, False

    if trickle:
        group = _due_sources([s for s in full if s.label not in held],
                             store.last_runs(), now=time.time(),
                             due_after=due_after, limit=max_sources)
        if not group:
            return 0, False         # nothing due: not a visit, not a word
        log(f"@{acct} visits {', '.join(s.label for s in group)} "
            f"(most overdue of {len(full)})")

    try:
        cl = ig_session.load_client(acct, log=log)
    except Exception as e:
        log(f"  could not load @{acct}: {e}")
        # Tell the Account Control Panel WHY this account is quiet.
        # Without this the card just sits at "last success —" and the
        # operator has to read journalctl to learn the session is gone.
        pool_link.note_needs_login("ig", acct, f"{type(e).__name__}: {e}")
        # A missing session is a checkpoint if that is what the
        # RuntimeError says (ig_session wraps ChallengeRequired), and
        # a session_missing otherwise. Either way: idle, tell a human.
        _decide_exc(dec, acct, full, e, fallback="session_missing", log=log)
        return 0, False

    # on_resolved turns a successful name lookup into a permanent row
    # in the DB. This is what makes the lookup a one-time cost instead
    # of a per-restart one — see engine_ig.resolve_user.
    engine = IGEngine(cl, account=acct,
                      on_resolved=store.cache_platform_id)
    refreshed = False       # relogin is attempted at most ONCE per pass
    # Did this account manage a single clean source this pass? That is
    # the honest definition of "the session still works", and it is what
    # stamps last_success_at in the pool (pool_link.record_success).
    acct_ok = False

    # Names to resolve off the following list: the unresolved sources this
    # pass will actually read (a visit resolves only what it visits).
    pending = [s.value for s in group
               if s.label not in held and s.type == "user"
               and not s.platform_id and not str(s.value).isdigit()]
    if pending:
        try:
            got = await _asyncio.to_thread(engine.resolve_from_following, pending)
        except Exception as e:
            got = {}
            log(f"  following-list lookup failed: {type(e).__name__}: {e}")
        if got:
            dec.ok(acct, source="lookups")      # the session answers again
        for name, pk in got.items():
            log(f"  resolved @{name} -> {pk} from @{acct}'s following list")
            for s in group:
                if s.value.lower() == name and not s.platform_id:
                    dec.ok(acct, source=s.label)

    for i, s in enumerate(group):
        if s.label in held:
            continue
        # Human rhythm BETWEEN sources: a person doesn't machine-gun
        # profile after profile. First source in a pass starts right
        # away; each subsequent one waits a human "switch" gap, with an
        # occasional longer "put the phone down" break.
        if i > 0:
            gap = ig_human.source_gap()
            brk = ig_human.maybe_long_break()
            if brk:
                log(f"  @{acct} …taking a {int(brk)}s break (human pause)")
                gap += brk
            await _asyncio.sleep(gap)
        _DAY.spend(acct)
        # "We looked" — stamped on the attempt, not the outcome, so the
        # trickle never re-picks a source that just failed ahead of the
        # ones it has not seen (the decider's hold-off is the retry policy).
        store.touch_source(s.label)
        try:
            total += await collect_source(engine, store, s, page_size=page_size,
                                          max_pages=max_pages, log=log)
            acct_ok = True
            if s.label in {x.label for x in unresolved}:
                dec.ok(acct, source="lookups")     # a lookup just worked
            dec.ok(acct, source=s.label)   # closes an unresolved_source, if open
            if _DAY.remaining(acct, budget) <= 0:
                log(f"  @{acct}: daily budget reached mid-pass — stopping")
                break
            continue
        except LoginRequired as e:
            log(f"  [{s.label}] session rejected: {type(e).__name__}")
            pool_link.note_needs_login(
                "ig", acct, f"session rejected on {s.label}: {type(e).__name__}")
        except Exception as e:
            log(f"  [{s.label}] error: {type(e).__name__}: {e}")
            # Not every exception is weather. A checkpoint or a
            # PleaseWaitFewMinutes here used to be logged and then
            # KNOCKED ON AGAIN by the very next source of the same
            # account. The decider names it and, when the answer is
            # "stop touching this account", the pass does.
            d = _decide_exc(dec, acct, full, e, fallback="pass_error", log=log, src=s)
            if d.kind == "lookup_throttled":
                rest = [x.label for x in unresolved if x.label not in held
                        and x.label != s.label]
                held.update(rest)
                if rest:
                    log(f"  lookups from @{acct} refused — not asking for the "
                        f"remaining {len(rest)} handle(s) this pass: "
                        f"{', '.join(rest)}")
                continue
            if d.stop_account:
                log(f"  @{acct}: {d.action} — leaving the remaining "
                    f"{len(group) - i - 1} source(s) for the next pass")
                break
            continue

        # THE SESSION IS THE TEST, not a probe run beforehand. Only a
        # source that actually came back login_required triggers a
        # relogin, and only the first one does — a checkpointed account
        # must not be knocked on once per source. Sources that still
        # work keep working either way: a partly-restricted session
        # (common while a checkpoint is open) collects what it can.
        if refreshed:
            continue
        refreshed = True
        try:
            cl = ig_session.refresh(acct, log=log)
        except Exception as e:
            log(f"  cannot refresh @{acct}: {e}")
            # ig_session.refresh refuses (RuntimeError) on a recorded
            # checkpoint, a missing sidecar, or a bad password; only a
            # human fixes any of those. Stop this account for the pass.
            _decide_exc(dec, acct, full, e, fallback="session_rejected", log=log)
            break
        engine = IGEngine(cl, account=acct,
                          on_resolved=store.cache_platform_id)
        try:
            total += await collect_source(engine, store, s, page_size=page_size,
                                          max_pages=max_pages, log=log)
            acct_ok = True
        except Exception as e:
            log(f"  [{s.label}] still failing after refresh: "
                f"{type(e).__name__}: {e}")
            d = _decide_exc(dec, acct, full, e, fallback="session_rejected", log=log, src=s)
            if d.stop_account:
                break

    # One write per account per pass, not one per post: the column means
    # "this account was working at this time".
    if acct_ok:
        pool_link.record_success("ig", acct)
        dec.ok(acct)        # closes any open condition on this account
    return total, acct_ok


# How far apart the accounts start within one pass. Three phones do not all
# open Instagram in the same second; the first goes at once, the others
# follow at a human distance.
STAGGER_S = (15.0, 120.0)


async def run_once(store_path="ig_results.db", account_override="", *,
                   page_size=12, max_pages=2, log=print, dec=None,
                   accounts_path=ACCOUNTS_DB, root=".", who="loop",
                   rng=None, awake=None, max_sources=None, due_after=0) -> int:
    """One collection pass: every owning account runs its own sources, in
    parallel, on its own phone. Every condition the pass meets goes through
    the decider (decider.py): it says what to do next, how long to wait, and
    logs the condition ONCE rather than once per pass.

    `dec` is the long-lived Decider the --loop holds (its state persists in
    activity.db, so a restart does not re-announce an open condition). Left
    None — the dashboard's Fetch-now button — a one-shot, in-memory decider
    is used, which always speaks, so the UI log shows the reason every time.

    `account_override` (CLI --account) makes that one login the sole owner
    for this pass — a debugging aid, and it is said in the log. `awake`, when
    given, is the set of accounts whose phone is in hand right now
    (ig_human.session_now); the others keep their sources and skip this pass
    (the loop decides it once per cycle; Fetch-now and the CLI pass None =
    everyone).

    `max_sources` turns the pass into a VISIT: each account reads only its
    N most overdue sources not seen within `due_after` seconds, and the
    pass says nothing when there is nothing due. This is the --loop trickle
    (2026-09-04); Fetch-now and the CLI leave it None and walk everything.
    """
    log = _persist_log(log)
    if dec is None:
        dec = decider.Decider("instagram", log=log, db=None)
    dec.begin_pass()
    rng = rng or random

    lock = PassLock(root)
    if not lock.acquire(who):
        h = lock.holder()
        since = time.time() - float(h.get("since") or time.time())
        log(f"a pass is already running (pid {h.get('pid', '?')}, "
            f"{h.get('who', '?')}, started {int(since // 60)}m ago) — "
            f"not starting a second one on top of it")
        return 0
    if lock.unlocked:
        log(f"warning: could not take the pass lock ({lock.unlocked}) — running unguarded")
    try:
        with store_ig.Store(store_path) as store:
            if store.setting("ig_paused") == "1":
                dec.on("paused", detail="paused from the dashboard — resume it in "
                                        "Watchlists → Network & settings")
                return 0
            sources = store.sources(only_enabled=True)
            if not sources:
                # Was: a warn line per pass, forever. Now: one line when the
                # condition opens, a reminder every 6h, the operator told after 2h,
                # and one "recovered" line when a source appears.
                dec.on("no_sources",
                       detail="add one in Watchlists → + New watchlist → Instagram")
                return 0
            if account_override:
                owners, benched = [account_override], {}
                log(f"--account {account_override}: this pass runs on that login only")
            else:
                owners, benched = collectors(store_path=accounts_path, root=root, log=log)
            for u, why in benched.items():
                log(f"  @{u} is benched ({why}) — its sources go to the others")
            if not owners:
                # No collector at all. Not a crash: a condition, idle on it
                # and tell the operator once. (session_missing is a
                # PLATFORM-scope condition, so the platform-level ok() below
                # must not run first: live on 2026-09-04 it did, and every
                # pass said "recovered" and then "session_missing" again —
                # two Telegram pings per pass for a state that never changed.)
                dec.on("session_missing",
                       detail="no active Instagram account with a usable "
                              "session — sign one in under Accounts & Sessions")
                return 0
            dec.ok()        # platform-level: closes no_sources / paused /
                            # session_missing / pass_error — the pass is on

            groups = store.assign_sources(owners, log=log)
            plan = {a: [s.label for s in g] for a, g in groups.items() if g}
            quiet = bool(max_sources)       # a visit narrates only what it reads
            if len(owners) > 1 and not quiet:
                log("plan: " + "; ".join(f"@{a} × {len(v)}" for a, v in plan.items()))

            # Per-account rest: an account that was told to back off (rate
            # limit, dead proxy, budget) sits this pass out WITHOUT giving
            # its sources away — resting is not leaving.
            runnable = {}
            for acct, group in groups.items():
                if not group:
                    continue
                if awake is not None and acct not in awake:
                    continue            # asleep by its own clock; quiet
                w = dec.account_wait(acct)
                if w > 0:
                    log(f"  @{acct} rests for {w // 60}m more (open condition) — "
                        f"{len(group)} source(s) wait with it")
                    continue
                runnable[acct] = group

            async def one(i, acct, group):
                if i:
                    await _asyncio.sleep(rng.uniform(*STAGGER_S))
                try:
                    return acct, await _collect_account(
                        acct, group, store, dec, page_size=page_size,
                        max_pages=max_pages, log=log,
                        max_sources=max_sources, due_after=due_after)
                except Exception as e:
                    # The whole account blew up (not one source). Named,
                    # never silent; the others carry on.
                    log(f"  @{acct}: pass failed — {type(e).__name__}: {e}")
                    dec.on("pass_error", acct, detail=f"{type(e).__name__}: {e}")
                    return acct, (0, False)

            results = await _asyncio.gather(
                *(one(i, a, g) for i, (a, g) in enumerate(runnable.items())))
            total = 0
            per_account = {}
            for acct, (n, ok) in results:
                total += n
                per_account[acct] = {"new": n, "ok": ok, "sources": len(runnable[acct]),
                                     "at": time.time()}
            heartbeat(root, last_pass=time.time(), who=who,
                      owners=owners, benched=benched, plan=plan,
                      accounts=per_account)
            if total or not quiet:
                log(f"done: {total} new post(s) stored"
                    + (f" across {len(runnable)} account(s)" if len(owners) > 1 else ""))
            return total
    finally:
        lock.release()


async def resolve_ids(store_path="ig_results.db", account_override="", *,
                      log=print) -> int:
    """Fill platform_id for every user source that still has only a handle.

    Runs the same paced rhythm as a collection pass — one lookup, a human gap,
    the next — because a burst of profile lookups is exactly the pattern that
    earns a checkpoint, and a checkpoint is what caused this problem in the
    first place. Failures are reported and skipped, never retried in a tight
    loop; a source that cannot be resolved keeps its handle and waits for a
    `set-id`.

    Returns the number of sources newly resolved.
    """
    log = _persist_log(log)
    with store_ig.Store(store_path) as store:
        pending = store.unresolved_sources()
        if not pending:
            log("every user source already has a numeric id — nothing to do")
            return 0
        log(f"{len(pending)} source(s) need an id")

        by_account = {}
        for s in pending:
            acct = s.account or account_override or _active_account()
            by_account.setdefault(acct, []).append(s)

        done = 0
        for acct, group in by_account.items():
            try:
                cl = ig_session.load_client(acct, log=log)
            except Exception as e:
                log(f"  could not load @{acct}: {e}")
                continue
            engine = IGEngine(cl, account=acct, on_resolved=store.cache_platform_id)
            # The following list first: one request, no lookup endpoint, and
            # it answers for every followed handle at once.
            got = await _asyncio.to_thread(engine.resolve_from_following,
                                           [s.value for s in group])
            for name, pk in got.items():
                log(f"  @{name} -> {pk} (from @{acct}'s following list)")
                done += 1
            group = [s for s in group if s.value.lower() not in got]
            for i, src in enumerate(group):
                if i > 0:
                    await _asyncio.sleep(ig_human.source_gap())
                try:
                    pk = await _asyncio.to_thread(engine.resolve_user, src.value)
                except Exception as e:
                    log(f"  [{src.label}] {src.value}: unresolved — {e}")
                    continue
                # cache_platform_id has already run via on_resolved, but a row
                # whose handle differs in case would be missed by that path.
                store.set_platform_id(src.label, pk)
                log(f"  [{src.label}] {src.value} -> {pk}")
                done += 1
        log(f"resolved {done}/{len(pending)}")
        return done


def main() -> int:
    # The service units set no EnvironmentFile, so .env is read here: the
    # decider's Telegram token / chat id and PUBLIC_BASE_URL (the link in a
    # ping) live there. Missing dotenv is not fatal — the env may be real.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Collect Instagram posts into store_ig")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-source")
    a.add_argument("--label", required=True)
    a.add_argument("--type", required=True, choices=["following", "user", "hashtag"])
    a.add_argument("--value", default="")
    a.add_argument("--account", default="")
    a.add_argument("--project", type=int, default=0,
                   help="which project owns this source. A source with no "
                        "project is collected but invisible to every scoped "
                        "read — assign one unless you mean to park it.")

    sp = sub.add_parser("set-project", help="move a source to a project "
                                            "(0 parks it, hiding it everywhere)")
    sp.add_argument("--label", required=True)
    sp.add_argument("--project", type=int, required=True)

    sub.add_parser("list-sources")

    si = sub.add_parser("set-id", help="cache the numeric Instagram id for a source "
                                       "(label and handle are left untouched)")
    si.add_argument("--label", required=True)
    si.add_argument("--id", required=True, dest="pk")

    ri = sub.add_parser("resolve-ids", help="resolve every source that still has "
                                            "only a handle, paced like a human")
    ri.add_argument("--account", default="", help="override which IG login looks up")

    d = sub.add_parser("disable"); d.add_argument("label")
    e = sub.add_parser("enable");  e.add_argument("label")

    r = sub.add_parser("run")
    r.add_argument("--account", default="", help="override which IG login collects")
    r.add_argument("--loop", action="store_true")
    r.add_argument("--every", type=int, default=120,
                   help="with --loop: a source seen within this many seconds is "
                        "not due again (the dashboard's cadence setting wins)")
    r.add_argument("--max-pages", type=int, default=2,
                   help="pages per source per pass (default 2; a warm poll uses 1)")
    r.add_argument("--page-size", type=int, default=12,
                   help="posts per page (default 12)")

    args = ap.parse_args()
    store_path = "ig_results.db"

    if args.cmd == "add-source":
        if args.type == "user" and not args.value:
            print("--value is the username for a user source"); return 1
        if args.type == "hashtag" and not args.value:
            print("--value is the hashtag for a hashtag source"); return 1
        with store_ig.Store(store_path) as st:
            st.add_source(args.label, args.type, args.value, args.account,
                          project_id=args.project)
        where = f"project {args.project}" if args.project else "NO project (parked)"
        print(f"added source '{args.label}' ({args.type} {args.value}) -> {where}")
        if not args.project:
            print("  note: a source with no project is invisible to the "
                  "dashboard and the API. Set one with `set-project`.")
        return 0

    if args.cmd == "set-project":
        with store_ig.Store(store_path) as st:
            if not st.db.execute("SELECT 1 FROM sources WHERE label=?",
                                 (args.label,)).fetchone():
                print(f"no source labelled '{args.label}'"); return 1
            st.set_project(args.label, args.project)
        print(f"[{args.label}] -> project {args.project or '(none — parked)'}"
              "  (posts already collected keep the project they were collected under)")
        return 0

    if args.cmd == "set-id":
        if not str(args.pk).isdigit():
            print("--id must be numeric (the Instagram profile_id)"); return 1
        with store_ig.Store(store_path) as st:
            if not st.db.execute("SELECT 1 FROM sources WHERE label=?",
                                 (args.label,)).fetchone():
                print(f"no source labelled '{args.label}' — add it first"); return 1
            st.set_platform_id(args.label, args.pk)
        print(f"[{args.label}] id cached: {args.pk} (handle unchanged)")
        return 0

    if args.cmd == "resolve-ids":
        asyncio.run(resolve_ids(store_path, args.account))
        return 0

    if args.cmd in ("enable", "disable"):
        with store_ig.Store(store_path) as st:
            st.set_enabled(args.label, args.cmd == "enable")
        print(f"{args.label}: {args.cmd}d")
        return 0

    if args.cmd == "list-sources":
        with store_ig.Store(store_path) as st:
            rows = st.sources(only_enabled=False)
            if not rows:
                print("no sources yet")
            for s in rows:
                print(f"  {s.label:22} {s.type:10} {s.value or '-':20} "
                      f"id={s.platform_id or '-':<14} "
                      f"project={s.project_id or '-':<6} "
                      f"account={s.account or '(active)'}")
            parked = [s.label for s in rows if not s.project_id]
            if parked:
                print(f"\n  {len(parked)} source(s) with NO project — collected, "
                      f"but invisible to the dashboard and the API:")
                for lab in parked:
                    print(f"    {lab}")
            print("stats:", st.stats())
        return 0

    if args.cmd == "run":
        paging = {"page_size": args.page_size, "max_pages": args.max_pages}
        if not args.loop:
            asyncio.run(run_once(store_path, args.account, **paging))
            return 0

        # Account-local timezone for the active-hours window (IST by default,
        # the media house's clock). Env IG_TZ_OFFSET_S overrides.
        tz_off = int(os.getenv("IG_TZ_OFFSET_S", str(int(5.5 * 3600))))

        # ONE decider for the life of the service. Its state lives in
        # activity.db, so an open condition is announced once, not once per
        # pass and not again after a restart (decider.py).
        loop_log = _persist_log(None)
        dec = decider.Decider("instagram", log=loop_log)

        heartbeat(".", started=time.time(), every=args.every)

        async def loop():
            while True:
                started = time.time()
                # Dashboard settings win over the CLI flag, re-read EVERY
                # cycle so a change applies without restarting the service
                # (RULEBOOK §6 — same contract as the Facebook loop).
                with store_ig.Store(store_path) as st:
                    paused = st.setting("ig_paused") == "1"
                    every = int(st.setting("ig_interval_s") or args.every)
                if paused:
                    dec.begin_pass()
                    dec.on("paused")            # said once; then a quiet tick
                    await asyncio.sleep(60)
                    continue
                # Phone-time, PER ACCOUNT (ig_human.day_plan, 2026-09-04):
                # each phone has its own plan for the day — two to four
                # sessions of random length at random times, a glance or
                # two — drawn from a seed so a restart replays it, and
                # shifted by a stable hour or so (ig_identity.stable_offset)
                # so three accounts never wake together. An account whose
                # phone is not in hand makes ZERO requests; the loop sleeps
                # until the earliest next window (re-reading the dashboard
                # settings at least every half hour).
                owners, _ = collectors(log=loop_log)
                in_hand, until = {}, []
                for a in owners:
                    off = tz_off + int(ig_identity.stable_offset(a) * 3600)
                    on, change = ig_human.session_now(a, started, tz_offset_s=off)
                    until.append(change)
                    if on:
                        in_hand[a] = off
                if owners and not in_hand:
                    nap = min(until) + random.uniform(5, 60)
                    await asyncio.sleep(max(30, min(1800, nap)))
                    continue
                # A VISIT, not a pass: every phone in hand reads its ONE most
                # overdue source (not seen within the dashboard cadence), and
                # the loop comes back after a human gap — a trickle across
                # the whole session instead of every source in one burst.
                try:
                    await run_once(store_path, args.account, dec=dec,
                                   awake=set(in_hand) or None,
                                   max_sources=1, due_after=every, **paging)
                except Exception as e:
                    # The whole pass blew up (DB locked, import gone, ...).
                    # Three in a row and the operator hears about it.
                    dec.on("pass_error", detail=f"{type(e).__name__}: {e}")
                # Between visits: the human switch gap plus an occasional
                # break, floored so today's budget lasts today's plan — but
                # never shorter than a PLATFORM decision asked for (no
                # sources: 30m; paused; a pass that blew up). An account's
                # own backoff (rate limit up to 4h, checkpoint 6h) is served
                # per account by run_once (dec.account_wait) and never idles
                # the other phones. The decider's wait is a floor, never a
                # ceiling, so the human rhythm still applies.
                budget = ig_human.daily_budget()
                planned = max(ig_human.planned_seconds(a, started, tz_offset_s=off)
                              for a, off in in_hand.items())
                wait = max(ig_human.visit_gap(budget, planned), dec.platform_wait_s())
                wait -= (time.time() - started)
                await asyncio.sleep(max(5, wait))
        try:
            asyncio.run(loop())
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
