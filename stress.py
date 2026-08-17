"""
stress.py — deliberately push ONE account with back-to-back requests until it
goes "hot" (rate-limited / challenged), recording a per-request latency curve so
the UI can draw a hotness graph and answer "how many can this account pull
before it heats up?".

EXTENSIBLE BY DESIGN. A platform is nothing but an entry in _PROBES: an async
generator that performs ONE request per iteration and yields (posts, note). The
runner, the step accounting, the hot-detection and the whole StressTest UI work
unchanged for any platform that provides such a probe. To add a fourth platform
later you write one generator and register it with @probe("name") — nothing else
in this file, in web.py, or in the UI needs to know the platform exists ahead of
time (the UI reads the platform list from /api/stress/accounts).

WHAT "HOT" MEANS, PER PLATFORM.
  * X publishes a real budget header, so a probe can go cold gracefully: when
    rl_remaining hits 0 the next request 429s and twscrape raises — the runner
    records that request as the hot point. We also surface rl_remaining every
    step so the curve shows the budget draining before the wall.
  * Instagram publishes NO budget header (see engine_ig.IGPage). Heat shows up
    as a raised exception (PleaseWaitFewMinutes / login_required) — the runner
    records the first raise as hot. Latency climbing is the early warning.
  * Facebook is a headless browser render, not an HTTP call, so it is
    bandwidth/render-bound, not request-rate-bound. fetch_page swallows its own
    errors and returns [] rather than raising, so "hot" there reads as a run of
    empty pages plus climbing render latency — the note says so.

SAFETY — READ THIS. This is the ONLY tool in the project whose PURPOSE is to
make an account hot. Point it at a THROWAWAY account only. The runner clamps the
request count and stops the instant it sees heat so the blast radius is bounded,
but the intent is abuse: never run it against an account you actually need.
"""

import time
import contextlib

# request-count ceiling — the same spirit as web.MAX_FETCH_PAGES: a stress run
# is bounded so a slipped "1000" can never turn into a real ban campaign.
MAX_REQUESTS = 100

_PROBES = {}


def probe(name):
    """Register an async-generator probe for a platform under `name`."""
    def deco(fn):
        _PROBES[name] = fn
        return fn
    return deco


def platforms():
    """Registered platform keys, e.g. ['fb', 'ig', 'x'] — the UI reads this."""
    return sorted(_PROBES)


def _step(i, t0, posts, cum, ok, hot, note, extra=None):
    row = {
        "i": i,
        "latency_ms": int((time.time() - t0) * 1000),
        "posts": posts,
        "cum_posts": cum,
        "ok": ok,
        "hot": hot,
        "note": note,
    }
    if extra:
        row.update(extra)
    return row


async def run(platform, target, n, *, account=None, accounts_db="accounts.db",
              log=lambda m: None):
    """
    Drive `platform`'s probe for up to `n` back-to-back requests against
    `target` (a handle, id, or search query, per platform) using `account`
    (defaults to the platform's active account). Returns a JSON-able dict:

        {platform, account, target, requests, cum_posts, hot_at, steps:[...]}

    hot_at is the 1-based request index where heat was detected, or None if the
    run finished n requests still cold (or the feed simply ran out).
    """
    fn = _PROBES.get(platform)
    if not fn:
        return {"error": f"unknown platform '{platform}'. "
                f"known: {', '.join(platforms()) or '(none)'}"}
    n = max(1, min(int(n or 10), MAX_REQUESTS))

    ctx = {"accounts_db": accounts_db}
    steps, cum, hot_at = [], 0, None

    try:
        agen = fn(target, account, ctx, log).__aiter__()
    except Exception as e:
        return {"error": f"could not start probe: {type(e).__name__}: {e}"}

    for i in range(1, n + 1):
        t0 = time.time()
        try:
            posts, note, extra = await _next(agen)
        except StopAsyncIteration:
            steps.append(_step(i, t0, 0, cum, ok=True, hot=False,
                               note="feed exhausted — no more posts to page"))
            break
        except Exception as e:
            hot_at = i
            steps.append(_step(i, t0, 0, cum, ok=False, hot=True,
                               note=f"HOT: {type(e).__name__}: {e}"))
            break
        cum += posts
        steps.append(_step(i, t0, posts, cum, ok=True, hot=False,
                           note=note, extra=extra))

    with contextlib.suppress(Exception):
        await agen.aclose()

    return {
        "platform": platform,
        "account": account or "(active)",
        "target": target,
        "requests": len(steps),
        "cum_posts": cum,
        "hot_at": hot_at,
        "steps": steps,
    }


async def _next(agen):
    """Pull one (posts, note[, extra]) from a probe, normalising the tuple."""
    item = await agen.__anext__()
    if isinstance(item, tuple):
        if len(item) == 3:
            return item[0], item[1], item[2]
        if len(item) == 2:
            return item[0], item[1], None
    return item, "", None


# ==========================================================================
# probes — one async generator per platform. Each yields (posts, note[, extra])
# once per request and lets exceptions propagate so the runner marks them hot.
# ==========================================================================

@probe("x")
async def _probe_x(target, account, ctx, log):
    """
    X search, one page per request. `target` is a handle (becomes from:handle)
    or a raw advanced-search query. X reports a real rate-limit budget, so each
    step carries rl_remaining/rl_limit and the run heats up as a clean 429.

    Note on `account`: X serves from the twscrape pool (last_used ASC), not a
    single named login, so the account field here is informational — the pool
    picks the server. The probe still stresses the pool's budget for `target`.
    """
    import auth
    import engine

    q = (target or "").strip()
    if not q:
        raise RuntimeError("a search query or @handle is required for X")
    # A bare token with no operators is treated as an author handle — the most
    # common thing someone types when stress-testing "one account".
    if not any(c in q for c in (":", " ")):
        q = f"from:{q.lstrip('@')}"

    api = auth.open_api(ctx["accounts_db"])
    eng = engine.Engine(api)
    async with contextlib.aclosing(
            eng.search_pages(q, page_size=20, max_pages=0)) as pages:
        async for page in pages:
            posts = len(page.result_ids)
            served = page.account or account or "?"
            extra = {"rl_remaining": page.rl_remaining, "rl_limit": page.rl_limit,
                     "served_by": served}
            budget = (f", budget {page.rl_remaining}/{page.rl_limit} left"
                      if page.rl_remaining is not None else "")
            yield posts, f"page {page.page_no} via @{served}{budget}", extra


@probe("ig")
async def _probe_ig(target, account, ctx, log):
    """
    Instagram user feed, one paginated request per iteration via the same path
    collection uses (IGEngine.user_pages -> user_medias_paginated_v1). `target`
    is a username or numeric pk; a numeric id is the safer input on a restricted
    session (see engine_ig.resolve_user). No budget header exists, so heat is a
    raised PleaseWaitFewMinutes / login_required — the runner marks it hot.
    """
    import ig_session
    import collect_ig
    from engine_ig import IGEngine

    acct = account or collect_ig._active_account()
    cl = ig_session.load_client(acct, log=log)
    eng = IGEngine(cl, account=acct)
    async for page in eng.user_pages(target, page_size=12, max_pages=0):
        yield len(page.result_ids), f"page {page.page_no} via @{acct}"


@probe("fb")
async def _probe_fb(target, account, ctx, log):
    """
    Facebook page render, one desktop-site fetch per iteration. This is a
    browser scroll, not an HTTP call, so it is bandwidth/render-bound: expect
    far fewer useful iterations than X/IG and treat repeated 0-post pages as the
    heat signal (fetch_page swallows challenges and returns [] rather than
    raising). `account` is ignored — FB uses the one logged-in browser session.
    """
    import engine_fb

    zero = 0
    async with engine_fb.FacebookEngine(log=log) as eng:
        while True:
            posts = await eng.fetch_page(target, max_scroll=4)
            n = len(posts)
            if n == 0:
                zero += 1
                note = (f"0 posts (x{zero}) — possible block/challenge; "
                        f"check fb_diag.json")
            else:
                zero = 0
                note = "page render"
            yield n, note
