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
dashboard read the same list. The account that collects is the active Instagram
login unless a source names its own (--account).
"""

import argparse
import asyncio
import os
import time

import asyncio as _asyncio

import activity_log
import ig
import ig_human
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


def _active_account() -> str:
    """The Instagram handle to collect with: the first active one in the store."""
    with ig.Store("ig_accounts.db") as st:
        rows = [r for r in st.all() if r.get("active")]
    if not rows:
        raise RuntimeError("no active Instagram account — onboard one with "
                           "ig_login.py or ig_import.py first")
    return rows[0]["username"]


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


async def run_once(store_path="ig_results.db", account_override="", *,
                   page_size=12, max_pages=2, log=print) -> int:
    log = _persist_log(log)
    with store_ig.Store(store_path) as store:
        if store.setting("ig_paused") == "1":
            log("collection is PAUSED from the dashboard — pass skipped "
                "(resume it in Watchlists → Network & settings)")
            return 0
        sources = store.sources(only_enabled=True)
        if not sources:
            log("no enabled sources — add one with `collect_ig.py add-source`")
            return 0

        # Group sources by the account that collects them, so one client serves
        # all of that account's sources.
        by_account = {}
        for s in sources:
            acct = s.account or account_override or _active_account()
            by_account.setdefault(acct, []).append(s)

        total = 0
        for acct, group in by_account.items():
            log(f"account @{acct}: {len(group)} source(s)")
            try:
                cl = ig_session.load_client(acct, log=log)
            except Exception as e:
                log(f"  could not load @{acct}: {e}")
                # Tell the Account Control Panel WHY this account is quiet.
                # Without this the card just sits at "last success —" and the 
                # operator has to read journalctl to learn the session is gone.
                pool_link.note_needs_login("ig", acct, f"{type(e).__name__}: {e}")
                continue

            # Daily budget per account (warm-up ramp for young sessions). Once
            # spent, this account rests until tomorrow — a person doesn't open
            # the app 500 times a day, and a burner that does gets flagged.
            budget = ig_human.daily_budget()
            if _DAY.remaining(acct, budget) <= 0:
                log(f"  @{acct}: daily budget spent ({budget}) — resting until "
                    f"tomorrow")
                continue

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
            for i, s in enumerate(group):
                # Human rhythm BETWEEN sources: a person doesn't machine-gun
                # profile after profile. First source in a pass starts right
                # away; each subsequent one waits a human "switch" gap, with an
                # occasional longer "put the phone down" break.
                if i > 0:
                    gap = ig_human.source_gap()
                    brk = ig_human.maybe_long_break()
                    if brk:
                        log(f"  …taking a {int(brk)}s break (human pause)")
                        gap += brk
                    await _asyncio.sleep(gap)
                _DAY.spend(acct)
                try:
                    total += await collect_source(engine, store, s, page_size=page_size,
                                              max_pages=max_pages, log=log)
                    acct_ok = True
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
                    continue
                engine = IGEngine(cl, account=acct,
                                  on_resolved=store.cache_platform_id)
                try:
                    total += await collect_source(engine, store, s, page_size=page_size,
                                              max_pages=max_pages, log=log)
                    acct_ok = True
                except Exception as e:
                    log(f"  [{s.label}] still failing after refresh: "
                        f"{type(e).__name__}: {e}")

            # One write per account per pass, not one per post: the column means
            # "this account was working at this time".
            if acct_ok:
                pool_link.record_success("ig", acct)
        log(f"done: {total} new post(s) stored")
        return total


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
    r.add_argument("--every", type=int, default=120, help="seconds between passes with --loop")
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
                    await asyncio.sleep(60)     # cheap idle tick, no log spam
                    continue
                # Human active-hours: overnight the collector mostly sleeps,
                # with only a rare "checked my phone" poll — a feed that never
                # goes quiet is not a person's.
                if not ig_human.active_now(started, tz_offset_s=tz_off):
                    await asyncio.sleep(ig_human.next_interval(max(600, every)))
                    continue
                try:
                    await run_once(store_path, args.account, **paging)
                except Exception as e:
                    print(f"pass error: {type(e).__name__}: {e}")
                # Jitter the wait so the between-cycle rhythm is never a fixed
                # clock (±35%).
                wait = ig_human.next_interval(every) - (time.time() - started)
                await asyncio.sleep(max(5, wait))
        try:
            asyncio.run(loop())
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
