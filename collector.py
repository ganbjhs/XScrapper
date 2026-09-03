"""
collector.py — the freshness-first poll loop.

The whole design optimises for LAG (tweet posted -> tweet in the store), not
for coverage of a date range. Concretely that means:

  * Walk the Latest timeline from the top and STOP as soon as we reach tweets
    we already have. A poll normally costs exactly one request.
  * Re-read a little past the watermark every time (the overlap window),
    because X's index is not perfectly ordered at the edge and tweets arrive
    slightly late.
  * Speed up when a stream is busy, slow down when it is quiet, and never
    confuse "quiet" with "the account pool is starved".

Three failure modes this loop is explicitly built to avoid:

  1. Stopping on an embedded quoted tweet. Old quoted tweets have tiny
     snowflake ids, so the stop check uses page.result_ids (real search hits,
     from timeline entry ids) and never the full parsed set. engine.py enforces
     that split.
  2. Backing off on starvation. When no account is available twscrape's
     generator simply yields nothing, which is indistinguishable from a quiet
     stream unless you look at the page count. Zero pages means starvation and
     the interval must NOT grow.
  3. Leaking accounts. Breaking out of the page generator without aclosing can
     hold an account's 15-minute lock. Every exit path here goes through
     aclosing.
"""

import asyncio
import json
import random
import time
from contextlib import aclosing
from dataclasses import dataclass, field

import store as sf  # snowflake helpers now live in store

# Stop reasons. The distinction between these is the whole point of recording
# them: 'watermark' is healthy, 'page_budget' means we are falling behind, and
# 'no_account_or_abort' means the pool is dry and backing off would be exactly
# the wrong response.
STOP_WATERMARK = "watermark"
STOP_EXHAUSTED = "exhausted"
STOP_PAGE_BUDGET = "page_budget"
STOP_STARVED = "no_account_or_abort"
STOP_ERROR = "error"

# Adaptive-interval constants.
TARGET_FILL = 0.6      # aim for a poll to return ~60% of one page of new tweets
GROW = 1.6             # multiplier when a stream is quieter than expected
SHRINK = 0.5           # multiplier when we hit the page budget (we are behind)
ALPHA = 0.35           # EWMA weight on the newest observation
JITTER = 0.15          # +/- fraction, to decorrelate streams from each other
MAX_EMPTY_BACKOFF = 4  # cap on consecutive-empty exponential backoff

# Jitter for a stream whose cadence the operator PINNED from the dashboard.
# Spreading polls out still matters -- streams started together would otherwise
# stay phase-locked and hit the account pool in a burst -- but +/-15% of a
# five-minute promise is +/-45 seconds, which is exactly the kind of "it says
# 5 min and it isn't" that pinning exists to stop. 2% is enough to decorrelate
# and small enough that the number in the dropdown is still the truth.
JITTER_PINNED = 0.02

# How many pages one backfill pass may walk. A granted budget is spent a few
# pages at a time on the stream's normal cadence, so a 500-page grant costs the
# rate-limit guard the same as ordinary polling spread over a while, instead of
# arriving as one unbounded burst that trips it.
BACKFILL_PAGES_PER_PASS = 3

# Gap between backfill passes. Three pages a minute is roughly a third of the
# ~50-requests-per-15-minutes search budget, which leaves the live poll loop --
# the thing the whole system is actually for -- the larger share. It backs off
# from here whenever X says headroom is short, and it is a starting point, not
# a ceiling.
BACKFILL_GAP_S = 60.0
BACKFILL_GAP_MAX_S = 900.0
BACKFILL_LOW_HEADROOM = 10   # rl_remaining below this = slow down

# How often the long-running watcher does database housekeeping (WAL
# checkpoint + retention, see Store.maintain). Five minutes: frequent enough
# that the WAL never accumulates more than a few minutes of writes, rare
# enough to be invisible next to the polling itself.
MAINTAIN_EVERY_S = 300


def describe_error(e: BaseException) -> str:
    """
    One line for the poll log and the dashboard.

    Most exceptions are just named. The one that gets special treatment is
    twscrape's GqlFeaturesOutdatedError (0.20.0+): X has changed the GraphQL
    "features" contract, every request is now structurally invalid, and no
    amount of retrying or account rotation helps. Before 0.20.0 twscrape
    called exit(1) here, which killed the whole watcher; now it is an ordinary
    exception, so without this the symptom would be every stream reporting
    the same cryptic error forever. Say what to do instead.
    """
    try:
        from twscrape import GqlFeaturesOutdatedError
    except ImportError:            # older twscrape: no such class, plain path
        GqlFeaturesOutdatedError = ()  # type: ignore[assignment]
    if isinstance(e, GqlFeaturesOutdatedError):
        return (
            "twscrape is out of date for X's current API (GqlFeaturesOutdatedError) -- "
            "every request will fail until it is upgraded: bump the pin in "
            f"requirements.txt, then `python3 main.py doctor --selftest`. Detail: {e}"
        )
    return f"{type(e).__name__}: {e}"


def orphaned_payload_error(res, first_failure) -> str | None:
    """
    The "X changed the payload shape under us" detector.

    A result is an ORPHAN when its timeline entry id is present but no tweet
    could be parsed for it. One or two on a page happen (deleted mid-page,
    withheld content). EVERY result on a poll being an orphan does not happen
    to a healthy engine: it means the parser no longer understands what X is
    sending, which is precisely what 2026-08 looked like — twscrape 0.19.2
    raising KeyError on each tweet after X untyped the author object, while
    the poll itself reported pages walked, results seen, nothing wrong.

    This must be an error, not a quiet count, for two reasons: the watermark
    must not advance over tweets that were never stored (poll_once now derives
    it from parsed results only, so an all-orphan poll has no max_id), and the
    dashboard must show a cause with a next step rather than "0 new". The
    version pin and the doctor asserts catch a changed LIBRARY; only this
    catches a changed PLATFORM.
    """
    if not res.results or res.orphans < res.results:
        return None
    detail = f" First failure: {first_failure[0]} -> {first_failure[1]}." if first_failure else ""
    return (
        f"payload shape changed: {res.results} results on {res.pages} page(s), none parseable "
        f"(all orphans). Nothing was stored and the watermark did not move. twscrape almost "
        f"certainly needs an upgrade for X's current API -- check for a new release, bump "
        f"the pin in requirements.txt, run `python3 main.py doctor --selftest`.{detail}"
    )


@dataclass
class PollResult:
    stream_label: str
    pages: int = 0
    results: int = 0
    new: int = 0
    dup: int = 0
    embedded: int = 0
    orphans: int = 0
    filtered: int = 0    # results seen, then dropped by the stream's collection filters
    stop_reason: str = STOP_EXHAUSTED
    account: str | None = None
    rl_limit: int | None = None
    rl_remaining: int | None = None
    rl_reset: int | None = None
    max_id: int | None = None
    min_id: int | None = None
    lags: list[int] = field(default_factory=list)
    gap_opened: bool = False
    error: str | None = None
    elapsed_s: float = 0.0

    @property
    def starved(self) -> bool:
        return self.stop_reason == STOP_STARVED


def _keep(stream, tweet) -> bool:
    """
    The stream's collection filters, applied to one fetched result.

    A search stream carries its filters inside the query, where X honours
    them as hints; an X List stream has no query, so this is the ONLY place
    its "No retweets" box can act. Both kinds go through here — for search
    it is the belt to X's braces (the RULEBOOK's `-filter:replies` lesson),
    for a list it is the whole mechanism. A stream with no filters, or one
    built from config.toml that never had the attribute, keeps everything.
    """
    flt = getattr(stream, "filters", None)
    if not flt:
        return True
    return sf.tweet_passes_filters(tweet, flt)


async def poll_once(engine, store, stream, stream_id, *, kind="poll", log=None) -> PollResult:
    """
    One pass over a stream: walk pages until we reach known ground.

    Returns a PollResult; never raises for ordinary API trouble.
    """
    res = PollResult(stream_label=stream.label)
    started = time.time()
    poll_id = await store.begin_poll(stream_id, kind=kind)

    wm = await store.get_watermark(stream_id)
    high = wm["high_tweet_id"] if wm else None

    # Stop a little BELOW the watermark rather than exactly at it, so each poll
    # re-reads the last minute or so. X indexes some tweets late; without this
    # overlap they would fall permanently into the blind spot between polls.
    stop_id = None
    if high and stream.watermark:
        stop_id = sf.id_minus_ms(high, stream.overlap_ms)

    committed = []
    last_cursor = None
    first_failure = None
    max_pages = stream.max_pages_per_poll

    try:
        # pages_for picks search vs list. Everything downstream — watermark,
        # overlap window, dedup, stop reasons — is identical either way, which
        # is the point: a list is just another way of choosing tweets.
        async with aclosing(
            engine.pages_for(
                stream,
                tab=stream.tab,
                page_size=stream.page_size,
                max_pages=max_pages,
            )
        ) as pages:
            async for page in pages:
                res.pages += 1
                res.account = page.account or res.account
                if page.rl_limit is not None:
                    res.rl_limit = page.rl_limit
                if page.rl_remaining is not None:
                    res.rl_remaining = page.rl_remaining
                if page.rl_reset is not None:
                    res.rl_reset = page.rl_reset
                last_cursor = page.cursor
                res.orphans += len(page.orphan_ids)
                if first_failure is None and page.parse_failures:
                    first_failure = page.parse_failures[0]

                # Quoted tweets, reply parents and other context. Stored so the
                # dataset is self-contained, but tagged 'embedded' so they can
                # never move the watermark or pollute the lag numbers.
                for tid in page.embedded_ids:
                    committed.append((page.tweets[tid], page, "embedded", None))

                if page.result_ids:
                    res.results += len(page.result_ids)
                    stored_ids = []
                    for tid in page.result_ids:
                        tweet = page.tweets.get(tid)
                        if tweet is None:
                            continue  # orphan, already counted
                        # Seen and deliberately rejected is not the same as
                        # unseen: a filtered tweet still bounds the watermark
                        # (else the newest retweet on a list would be re-read
                        # and re-dropped every poll), it just never reaches
                        # the store. Lag is measured on what we keep.
                        stored_ids.append(tid)
                        if not _keep(stream, tweet):
                            res.filtered += 1
                            continue
                        committed.append((tweet, page, "result", page.entries_by_id.get(tid)))
                        if sf.is_snowflake(tid):
                            res.lags.append(sf.lag_ms(tid, page.collected_ms))

                    # min_id / max_id describe what we HAVE, so they come from
                    # the results that parsed. An orphan is a tweet X showed us
                    # and we could not keep; letting it raise the watermark
                    # would hide it behind "already collected" forever. (The
                    # stop check below still uses the true page minimum, so an
                    # unparseable oldest entry cannot make us walk further than
                    # the timeline order warrants.)
                    if stored_ids:
                        lo, hi = min(stored_ids), max(stored_ids)
                        res.min_id = lo if res.min_id is None else min(res.min_id, lo)
                        res.max_id = hi if res.max_id is None else max(res.max_id, hi)

                    page_min = page.min_result_id()
                    if stop_id is not None and page_min <= stop_id:
                        res.stop_reason = STOP_WATERMARK
                        break

                # Checked for every page, including result-free ones, so that a
                # run of empty pages is still reported as a budget stop rather
                # than as a clean exhaustion.
                if res.pages >= max_pages:
                    res.stop_reason = STOP_PAGE_BUDGET
                    break
            else:
                # Generator finished on its own: X ran out of results -- or,
                # since twscrape 0.20.0, echoed the previous page/cursor back
                # (its stall detection returns instead of looping). Both mean
                # there is nothing further down.
                res.stop_reason = STOP_EXHAUSTED
    except Exception as e:
        res.stop_reason = STOP_ERROR
        res.error = describe_error(e)
        if log:
            log(f"[{stream.label}] error: {res.error}")

    res.elapsed_s = time.time() - started

    # Results that all failed to parse are not "0 new" — they are the engine
    # no longer understanding X. Say so, loudly, with the next step.
    if res.stop_reason != STOP_ERROR:
        shape_error = orphaned_payload_error(res, first_failure)
        if shape_error:
            res.stop_reason = STOP_ERROR
            res.error = shape_error
            if log:
                log(f"[{stream.label}] error: {res.error}")

    # Zero pages is NOT a quiet stream. twscrape's generator ends silently when
    # no account can be acquired -- and, since 0.20.0, also after a transport
    # failure (bad proxy, unreachable host): it cools the account for 60s and
    # rotates rather than raising, so a dead proxy on the last free account
    # lands here too. This is the only place that distinction can be made, and
    # it must not be treated as "nothing new".
    if res.pages == 0 and res.stop_reason != STOP_ERROR:
        res.stop_reason = STOP_STARVED
        await store.finish_poll(
            poll_id, pages=0, stop_reason=res.stop_reason, account=res.account
        )
        return res

    counts = await store.upsert_tweets(committed, stream_id, poll_id)
    res.new, res.dup, res.embedded = counts.new, counts.dup, counts.embedded

    await store.mark_first_poll(stream_id, int(started * 1000))

    # If we stopped for any reason other than reaching known ground, everything
    # between the old watermark and the oldest tweet we actually saw is a hole.
    # Record it rather than pretending coverage was complete. (Backfilling is
    # Phase 6; `doctor` surfaces these meanwhile.)
    if (
        stream.watermark
        and high
        and res.min_id
        and res.stop_reason not in (STOP_WATERMARK, STOP_ERROR, STOP_STARVED)
        and res.min_id > high
    ):
        await store.open_gap(stream_id, lo=high, hi=res.min_id, cursor=last_cursor, poll_id=poll_id)
        res.gap_opened = True

    await store.finish_poll(
        poll_id,
        pages=res.pages,
        results=res.results,
        new_tweets=res.new,
        dup_tweets=res.dup,
        orphans=res.orphans,
        max_id=res.max_id,
        min_id=res.min_id,
        stop_reason=res.stop_reason,
        account=res.account,
        rl_limit=res.rl_limit,
        rl_remaining=res.rl_remaining,
        rl_reset=res.rl_reset,
        error=res.error,
        lags=res.lags,
    )
    return res


# --------------------------------------------------------------------------
# backfill — the same walk, in the other direction
# --------------------------------------------------------------------------

STOP_BACKFILL_IDLE = "backfill_idle"
STOP_BACKFILL_DONE = "backfill_exhausted"
STOP_BACKFILL_BUDGET = "backfill_budget"


async def backfill_once(engine, store, stream, stream_id, *,
                        max_pages=BACKFILL_PAGES_PER_PASS, log=None) -> PollResult:
    """
    Walk OLDER, resuming from the last stored cursor.

    poll_once is watermark-first: it starts at the top of the timeline and stops
    the instant it reaches known ground. For a live stream that is the whole
    design and it is right. For a query with a fixed past — `from:someone
    until:2025-02-20`, an account that stopped posting, any archival sweep —
    it is a trap. The first poll takes page_size * max_pages_per_poll tweets and
    sets the watermark at the newest of them. Every poll after that finds the
    same newest tweet on page one, stops, and reports nothing new. The stream is
    capped there permanently: polling on schedule, costing requests, and never
    reaching tweet number 101. From the dashboard it looks like collection
    stopped, because in every sense that matters it did.

    This is the way past that. Three things make it safe to run next to the
    poller rather than instead of it:

      * It NEVER touches the watermark. The watermark answers "how far forward
        have we got", and an old tweet must not be allowed to answer it —
        set_watermark's max() guard would ignore a lower value anyway, but not
        asking is clearer than relying on being refused.

      * It resumes from a stored cursor, so a pass costs only new pages. A walk
        that restarted at the top would re-pay for everything it already had,
        and the deeper it got the more it would waste.

      * It spends a granted budget a few pages per pass, on the stream's own
        cadence. The rate-limit guard sees ordinary polling, not a burst.

    Starvation is the case worth being careful about: when no account is free,
    twscrape's generator simply yields nothing, and counting that as pages
    walked would silently eat an operator's budget without collecting a tweet.
    Zero pages therefore spends nothing.
    """
    res = PollResult(stream_label=stream.label)
    state = await store.backfill_state(stream_id)
    # Two ways to be authorised. A one-shot grant spends a page budget and goes
    # idle; a standing sweep (backfill_auto) has no budget to exhaust and stops
    # only when X itself runs out, which is what makes it fire-and-forget. Both
    # answer to `done`, so neither can keep asking a query that has no more
    # history to give.
    if state.get("auto"):
        budget = int(max_pages)
    else:
        budget = min(int(max_pages), int(state.get("remaining") or 0))
    if state.get("done") or budget <= 0:
        res.stop_reason = STOP_BACKFILL_IDLE
        return res

    started = time.time()
    poll_id = await store.begin_poll(stream_id, kind="backfill")
    committed = []
    cursor = state.get("cursor")
    exhausted = False
    first_failure = None

    try:
        async with aclosing(
            engine.pages_for(
                stream,
                tab=stream.tab,
                page_size=stream.page_size,
                max_pages=budget,
                cursor=cursor,
            )
        ) as pages:
            async for page in pages:
                res.pages += 1
                res.account = page.account or res.account
                if page.rl_limit is not None:
                    res.rl_limit = page.rl_limit
                if page.rl_remaining is not None:
                    res.rl_remaining = page.rl_remaining
                if page.rl_reset is not None:
                    res.rl_reset = page.rl_reset
                res.orphans += len(page.orphan_ids)
                if first_failure is None and page.parse_failures:
                    first_failure = page.parse_failures[0]

                # Only advance the resume point when X actually gave us one.
                # Overwriting a good cursor with None would rewind the sweep to
                # the top of the timeline on the next pass, which is both
                # expensive and invisible.
                if page.cursor:
                    cursor = page.cursor

                for tid in page.embedded_ids:
                    committed.append((page.tweets[tid], page, "embedded", None))

                if page.result_ids:
                    res.results += len(page.result_ids)
                    stored_ids = []
                    for tid in page.result_ids:
                        tweet = page.tweets.get(tid)
                        if tweet is None:
                            continue
                        stored_ids.append(tid)
                        if not _keep(stream, tweet):   # same rule as poll_once
                            res.filtered += 1
                            continue
                        committed.append((tweet, page, "result", page.entries_by_id.get(tid)))
                    if stored_ids:   # what we HAVE, as in poll_once
                        lo, hi = min(stored_ids), max(stored_ids)
                        res.min_id = lo if res.min_id is None else min(res.min_id, lo)
                        res.max_id = hi if res.max_id is None else max(res.max_id, hi)
                else:
                    # A cursored page with no search hits on it is X saying the
                    # walk is over. Believe it: continuing would spend the rest
                    # of the budget on empty pages.
                    exhausted = True
                    break
        # Finishing under budget means the generator ran out first, i.e. there
        # is no more history to have. Finishing AT budget says nothing about
        # whether more exists, so it must not be recorded as exhausted.
        if res.pages < budget:
            exhausted = True
    except Exception as e:
        res.stop_reason = STOP_ERROR
        res.error = describe_error(e)
        if log:
            log(f"[{stream.label}] backfill error: {res.error}")

    res.elapsed_s = time.time() - started

    if res.pages == 0 and res.stop_reason != STOP_ERROR:
        # Pool starvation, not the end of the archive. Spend nothing and try
        # again on the next cycle.
        res.stop_reason = STOP_STARVED
        await store.finish_poll(poll_id, pages=0, stop_reason=res.stop_reason,
                                account=res.account)
        return res

    # Same rule as starvation, for the same reason: a pass that stored nothing
    # because nothing would parse must not advance the cursor, spend the grant
    # or retire the sweep. Otherwise a broken parser digs through the whole
    # archive at full speed, keeping nothing, and reports the job done.
    if res.stop_reason != STOP_ERROR:
        shape_error = orphaned_payload_error(res, first_failure)
        if shape_error:
            res.stop_reason = STOP_ERROR
            res.error = shape_error
            if log:
                log(f"[{stream.label}] backfill error: {res.error}")
            await store.finish_poll(poll_id, pages=res.pages, results=res.results,
                                    orphans=res.orphans, stop_reason=res.stop_reason,
                                    account=res.account, error=res.error)
            return res

    counts = await store.upsert_tweets(committed, stream_id, poll_id)
    res.new, res.dup, res.embedded = counts.new, counts.dup, counts.embedded

    if res.stop_reason != STOP_ERROR:
        res.stop_reason = STOP_BACKFILL_DONE if exhausted else STOP_BACKFILL_BUDGET

    await store.save_backfill(
        stream_id, cursor=cursor, walked=res.pages, got=res.new,
        done=True if exhausted else None,
    )
    await store.finish_poll(
        poll_id,
        pages=res.pages,
        results=res.results,
        new_tweets=res.new,
        dup_tweets=res.dup,
        orphans=res.orphans,
        max_id=res.max_id,
        min_id=res.min_id,
        stop_reason=res.stop_reason,
        account=res.account,
        rl_limit=res.rl_limit,
        rl_remaining=res.rl_remaining,
        rl_reset=res.rl_reset,
        error=res.error,
    )
    return res


# --------------------------------------------------------------------------
# adaptive interval
# --------------------------------------------------------------------------

def next_interval(stream, prev_interval: float, elapsed: float, res: PollResult,
                  ewma_rate: float, consecutive_empty: int) -> tuple[float, float, int]:
    """
    Choose the delay before this stream's next poll.

    Controls on new-tweets-per-poll rather than on a fixed cadence: the target
    is for one poll to return a bit over half a page, which keeps a poll at one
    request while staying ahead of the stream.

    Returns (interval, ewma_rate, consecutive_empty).
    """
    instant = res.new / max(elapsed, 1.0)
    ewma = ALPHA * instant + (1 - ALPHA) * ewma_rate if ewma_rate else instant

    if res.stop_reason == STOP_PAGE_BUDGET:
        # We hit the page ceiling, so the stream is outrunning us. Poll sooner.
        target = prev_interval * SHRINK
    elif res.starved:
        # No account was free. Polling later would not help and polling sooner
        # would just spin, so hold steady and let the pool recover.
        target = prev_interval
    elif ewma > 0:
        target = (TARGET_FILL * stream.page_size) / ewma
    else:
        target = prev_interval * GROW

    if res.new == 0 and not res.starved:
        consecutive_empty += 1
        target = max(target, prev_interval * (GROW ** min(consecutive_empty, MAX_EMPTY_BACKOFF)))
    elif not res.starved:
        consecutive_empty = 0

    interval = max(stream.min_interval_s, min(stream.max_interval_s, target))
    return interval, ewma, consecutive_empty


def jittered(interval: float, frac: float = JITTER) -> float:
    """
    Spread polls out.

    Without this, streams started together stay phase-locked and hit the
    account pool in a burst every tick. It also makes the traffic pattern less
    obviously machine-generated, which matters more than the efficiency.

    `frac` is smaller for a pinned stream -- see JITTER_PINNED. The spread is
    still there; it is just held inside the tolerance of a cadence someone was
    shown in an interface and is entitled to believe.
    """
    return interval * (1 + random.uniform(-frac, frac))


# --------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------

class Collector:
    """Runs one or more streams concurrently, each on its own adaptive interval."""

    def __init__(self, engine, store, streams, *, max_concurrency=4, log=print):
        self.engine = engine
        self.store = store
        self.streams = streams
        self.log = log
        self.sem = asyncio.Semaphore(max(1, max_concurrency))
        self.stream_ids: dict[str, int] = {}
        self.state: dict[str, dict] = {}
        # Backfill pacing, per stream label. Kept separate from self.state
        # because the two schedules are genuinely independent: a stream can be
        # polling forward every five minutes and walking backwards every
        # minute, and collapsing them into one clock would make the operator
        # choose between the two.
        self.bf_state: dict[str, dict] = {}

    async def prepare(self):
        for s in self.streams:
            sid = await self.store.ensure_stream(
                s.label, s.query, s.tab, s.watermark, getattr(s, 'list_id', '') or ''
            )
            self.stream_ids[s.label] = sid
            wm = await self.store.get_watermark(sid)
            self.state[s.label] = {
                "interval": wm["interval_s"] if wm else s.min_interval_s,
                "ewma": wm["ewma_rate"] if wm else 0.0,
                "empty": wm["consecutive_empty"] if wm else 0,
                "next_ms": 0,
                "last_poll_ts": 0.0,
            }

    async def discover_new_streams(self) -> int:
        """
        Pick up streams added to the database AFTER this collector started.

        A watchlist created in the dashboard compiles to `watched = 1` streams;
        a collector that read its list only at startup would ignore them until
        a restart — which is exactly the "my new project isn't collecting" bug.
        This re-scans on a slow cadence and ADDS any it hasn't seen, so a new
        watchlist begins collecting within a minute, no restart.

        Additive only: it never drops a stream. A deleted or paused watchlist
        is already handled — its streams go `paused = 1`, and apply_settings
        skips paused rows every poll.
        """
        import config as _config

        try:
            rows = self.store.db.execute(
                "SELECT label, query, list_id, tab FROM streams "
                "WHERE (watched = 1 OR tg_enabled = 1) AND paused = 0 "
                "AND (query != '' OR list_id IS NOT NULL)").fetchall()
        except Exception:
            return 0

        added = 0
        for r in rows:
            if r["label"] in self.stream_ids:
                continue
            s = _config.StreamCfg(
                label=r["label"], query=r["query"] or "",
                list_id=r["list_id"] or "", tab=r["tab"] or "Latest",
                watermark=True)
            sid = await self.store.ensure_stream(
                s.label, s.query, s.tab, s.watermark, s.list_id or "")
            self.stream_ids[s.label] = sid
            wm = await self.store.get_watermark(sid)
            self.state[s.label] = {
                "interval": wm["interval_s"] if wm else s.min_interval_s,
                "ewma": wm["ewma_rate"] if wm else 0.0,
                "empty": wm["consecutive_empty"] if wm else 0,
                "next_ms": 0, "last_poll_ts": 0.0,
            }
            self.streams.append(s)
            added += 1
            self.log(f"[watch] + {s.label}  (new watchlist stream — picked up "
                     f"without a restart)")
        return added

    def apply_settings(self, stream) -> bool:
        """
        Re-read this stream's dashboard settings. Returns False if it is paused.

        Read every poll rather than once at startup, so pausing a stream or
        changing how often it runs takes effect on the next cycle instead of at
        the next restart. It is one indexed row from a database this process
        already holds open.

        NULL means "no override" — the value from config.toml stands. That is
        why it is not stored as 0: an interval of 0 would poll as fast as the
        loop can turn, which is exactly the accident this distinction avoids.
        """
        try:
            row = self.store.db.execute(
                "SELECT paused, min_interval_s, max_interval_s, max_pages_per_poll, "
                "filters FROM streams WHERE label = ?", (stream.label,)).fetchone()
        except Exception:
            return True     # settings unreadable is not a reason to stop collecting
        if row is None:
            return True

        # Collection filters, re-read every poll like the rest: unticking
        # "No retweets" in the dashboard takes effect on the next check, not
        # the next restart. Unparseable JSON reads as "no filters" — the
        # failure mode must be "collected a retweet", never "collected nothing".
        try:
            stream.filters = json.loads(row["filters"]) if row["filters"] else None
        except (TypeError, ValueError):
            stream.filters = None

        if row["min_interval_s"] is not None:
            stream.min_interval_s = float(row["min_interval_s"])
        # The CEILING, and the reason the dashboard's interval control is now
        # a control rather than a hint. next_interval clamps into
        # [min_interval_s, max_interval_s]; when the dashboard writes the same
        # number to both there is nothing left for the adaptive controller to
        # decide, and the stream polls at exactly the chosen cadence. Left NULL
        # ("auto") the config value stands and the controller adapts as before.
        if row["max_interval_s"] is not None:
            stream.max_interval_s = float(row["max_interval_s"])
        # A floor above the ceiling is not a configuration, it is a mistake --
        # from the older per-stream speed control, which writes only the floor.
        # Raise the ceiling to meet it rather than letting the clamp resolve it
        # by accident: max(min, min(max, target)) happens to return the floor
        # here, and relying on the happy accident of an expression is how the
        # next edit to it breaks something far away.
        if stream.max_interval_s < stream.min_interval_s:
            stream.max_interval_s = stream.min_interval_s
        if row["max_pages_per_poll"] is not None:
            stream.max_pages_per_poll = int(row["max_pages_per_poll"])
        return not row["paused"]

    async def poll_stream(self, stream) -> PollResult:
        sid = self.stream_ids[stream.label]
        st = self.state[stream.label]

        if not self.apply_settings(stream):
            # Paused. Check back on the normal cadence rather than spinning,
            # so un-pausing is picked up without a restart.
            st["next_ms"] = int((time.time() + max(stream.min_interval_s, 10)) * 1000)
            return PollResult(stream_label=stream.label, stop_reason="paused")

        async with self.sem:
            res = await poll_once(self.engine, self.store, stream, sid, log=self.log)

        # The rate denominator is the gap SINCE THE LAST POLL of this stream,
        # not how long this poll took. Using the poll duration would understate
        # the arrival rate whenever a poll waited on the concurrency semaphore,
        # which would then push the interval in the wrong direction.
        now = time.time()
        elapsed = max(now - st["last_poll_ts"], 1.0) if st["last_poll_ts"] else st["interval"]
        st["last_poll_ts"] = now

        interval, ewma, empty = next_interval(
            stream, st["interval"], elapsed, res, st["ewma"], st["empty"]
        )
        st.update(interval=interval, ewma=ewma, empty=empty)
        pinned = stream.min_interval_s == stream.max_interval_s
        delay = jittered(interval, JITTER_PINNED if pinned else JITTER)
        st["next_ms"] = int((time.time() + delay) * 1000)

        if res.max_id:
            await self.store.set_watermark(
                sid, res.max_id,
                interval_s=interval, ewma_rate=ewma,
                consecutive_empty=empty, next_poll_ms=st["next_ms"],
            )

        self.log(self.format_result(res, interval))
        return res

    async def backfill_stream(self, stream) -> PollResult:
        """
        One backwards pass for a stream that has been granted budget.

        Runs on the SAME concurrency semaphore as the forward polls, so
        backfilling can never starve live collection of account slots — it just
        queues behind it. That is the correct priority: history is not going
        anywhere, and the next tweet is.
        """
        sid = self.stream_ids.get(stream.label)
        if sid is None:
            return PollResult(stream_label=stream.label, stop_reason=STOP_BACKFILL_IDLE)

        st = self.bf_state.setdefault(
            stream.label, {"next_ms": 0, "gap": BACKFILL_GAP_S})

        if not self.apply_settings(stream):
            st["next_ms"] = int((time.time() + max(stream.min_interval_s, 10)) * 1000)
            return PollResult(stream_label=stream.label, stop_reason="paused")

        # A standing sweep digs on the cadence the operator chose in the
        # dashboard; a one-shot grant runs at the default pace until spent.
        # Read every pass, so changing the dropdown takes effect on the next
        # cycle rather than at the next restart -- the same rule the forward
        # poller's settings follow.
        base = BACKFILL_GAP_S
        try:
            bs = await self.store.backfill_state(sid)
            if bs.get("auto") and bs.get("every_s"):
                base = float(bs["every_s"])
        except Exception:
            pass
        if st.get("base") != base:
            # The operator just changed the cadence (or this is the first pass).
            # Reset the adaptive gap to it rather than letting a backed-off
            # value from the old setting quietly outlive the change.
            st["base"], st["gap"] = base, base

        async with self.sem:
            res = await backfill_once(self.engine, self.store, stream, sid, log=self.log)

        # Pace on what X just told us about headroom rather than on a fixed
        # clock. Starvation and a short rate-limit window are the same message
        # -- "not now" -- and the response to both is to wait longer, not to
        # keep asking. An error backs off too: retrying a failing page every
        # minute is how a transient problem turns into a rate-limit ban.
        gap = st["gap"]
        base = st.get("base", BACKFILL_GAP_S)
        ceiling = max(BACKFILL_GAP_MAX_S, base * 4)
        if (res.starved or res.error
                or (res.rl_remaining is not None
                    and res.rl_remaining < BACKFILL_LOW_HEADROOM)):
            gap = min(ceiling, gap * 2)
        else:
            # Recovers TOWARDS the operator's cadence, never below it. A
            # standing sweep set to 15 minutes must not creep to 1 minute just
            # because the last few passes went well -- the number on the card
            # is a promise (RULEBOOK 3).
            gap = max(base, gap * 0.5)
        st["gap"] = gap
        st["next_ms"] = int((time.time() + jittered(gap)) * 1000)

        if res.stop_reason != STOP_BACKFILL_IDLE:
            self.log(self.format_backfill(res, gap))
        return res

    def format_backfill(self, res: PollResult, gap: float) -> str:
        bits = [f"[{res.stream_label}]", "backfill",
                f"older={res.new}", f"dup={res.dup}",
                f"pages={res.pages}", f"stop={res.stop_reason}"]
        if res.filtered:
            bits.append(f"filtered={res.filtered}")
        if res.account:
            bits.append(f"acct=@{res.account}")
        if res.rl_remaining is not None:
            bits.append(f"rl={res.rl_remaining}/{res.rl_limit}")
        if res.starved:
            bits.append("<< POOL STARVED (budget not spent)")
        if res.error:
            bits.append(f"err={res.error[:80]}")
        if res.stop_reason == STOP_BACKFILL_DONE:
            bits.append("-- no more history to fetch")
        else:
            bits.append(f"next={gap:.0f}s")
        return "  ".join(bits)

    def format_result(self, res: PollResult, interval: float) -> str:
        bits = [
            f"[{res.stream_label}]",
            f"new={res.new}",
            f"dup={res.dup}",
            f"pages={res.pages}",
            f"stop={res.stop_reason}",
        ]
        if res.filtered:
            bits.append(f"filtered={res.filtered}")
        if res.lags:
            p50 = sorted(res.lags)[len(res.lags) // 2] / 1000
            bits.append(f"lag_p50={p50:.1f}s")
        if res.account:
            bits.append(f"acct=@{res.account}")
        if res.rl_remaining is not None:
            bits.append(f"rl={res.rl_remaining}/{res.rl_limit}")
        if res.gap_opened:
            bits.append("GAP-OPENED")
        if res.starved:
            bits.append("<< POOL STARVED (not a quiet stream)")
        if res.error:
            bits.append(f"err={res.error[:80]}")
        bits.append(f"next={interval:.0f}s")
        return "  ".join(bits)

    def _paused(self) -> bool:
        """The dashboard's global Start/Stop flag. Cheap indexed read; a bad
        read never stops collection (fail open)."""
        try:
            row = self.store.db.execute(
                "SELECT value FROM meta WHERE key = 'collection_paused'").fetchone()
            return bool(row and row[0] == "1")
        except Exception:
            return False

    async def run_once(self) -> list[PollResult]:
        return list(
            await asyncio.gather(*(self.poll_stream(s) for s in self.streams))
        )

    async def run_forever(self, duration: float | None = None):
        """Poll every stream on its own schedule until stopped."""
        deadline = (time.time() + duration) if duration else None
        tasks: dict[str, asyncio.Task] = {}
        # Backfill tasks are tracked separately so a slow backwards walk can
        # never occupy the slot that a stream's forward poll needs. The two
        # share the concurrency semaphore, which is the resource that actually
        # needs protecting; they do not share a queue position.
        bf_tasks: dict[str, asyncio.Task] = {}
        last_backfill_scan = 0.0
        backfill_labels: set[str] = set()
        last_discover = 0.0
        last_maintain = time.time()   # not at boot — opening the DB just ran
                                      # the migration; let polls start first
        try:
            while True:
                if deadline and time.time() >= deadline:
                    break
                now_ms = int(time.time() * 1000)

                # Re-scan for watchlists added since startup, ~once a minute, so
                # a new project starts collecting on its own. Cheap: one indexed
                # read from a DB this process already holds open.
                if time.time() - last_discover >= 60:
                    last_discover = time.time()
                    try:
                        await self.discover_new_streams()
                    except Exception as e:
                        self.log(f"[watch] stream discovery error: {e!r}")

                # Housekeeping: checkpoint the WAL, apply retention if the
                # operator turned it on. A failure here must never stop
                # collection — log it and try again next cycle.
                if time.time() - last_maintain >= MAINTAIN_EVERY_S:
                    last_maintain = time.time()
                    try:
                        stats = await self.store.maintain()
                        pruned = (stats.get("tweets_pruned", 0)
                                  + stats.get("raw_pruned", 0))
                        if pruned:
                            self.log(f"[maintain] pruned: "
                                     f"{stats.get('tweets_pruned', 0)} tweets, "
                                     f"{stats.get('raw_pruned', 0)} raw payloads, "
                                     f"{stats.get('polls_pruned', 0)} poll records")
                    except Exception as e:
                        self.log(f"[maintain] error: {e!r}")

                # Global pause: let in-flight polls finish and clean up, but
                # launch no new ones while collection is switched off.
                paused = self._paused()

                # Which streams have unspent backfill budget? Re-read on a
                # slow cadence rather than every tick: a grant arrives from a
                # dashboard click, so noticing it within a few seconds is
                # ample, and this way the scheduler's hot path stays free of
                # database work.
                if time.time() - last_backfill_scan >= 5:
                    last_backfill_scan = time.time()
                    try:
                        backfill_labels = set(self.store.streams_with_backfill())
                    except Exception as e:
                        self.log(f"[backfill] scan error: {e!r}")

                for s in list(self.streams):
                    st = self.state[s.label]
                    running = tasks.get(s.label)
                    if running and not running.done():
                        continue
                    if running and running.done():
                        # Surface a crashed poll rather than losing it silently,
                        # and push the next attempt out — otherwise a task that
                        # dies before updating next_ms spins the scheduler.
                        exc = running.exception()
                        if exc:
                            self.log(f"[{s.label}] poll task failed: {exc!r}")
                            st["next_ms"] = int((time.time() + s.min_interval_s) * 1000)
                        tasks.pop(s.label, None)
                    if not paused and now_ms >= st["next_ms"]:
                        tasks[s.label] = asyncio.create_task(self.poll_stream(s))

                    # Backwards walk, on its own clock. Gated by the same
                    # global pause as forward polling -- "stop collecting"
                    # has to mean stop, not "stop the half you can see".
                    bf_running = bf_tasks.get(s.label)
                    if bf_running and bf_running.done():
                        exc = bf_running.exception()
                        if exc:
                            self.log(f"[{s.label}] backfill task failed: {exc!r}")
                            self.bf_state.setdefault(s.label, {})["next_ms"] = int(
                                (time.time() + BACKFILL_GAP_MAX_S) * 1000)
                        bf_tasks.pop(s.label, None)
                        bf_running = None
                    if (not paused and s.label in backfill_labels
                            and bf_running is None
                            and now_ms >= self.bf_state.get(s.label, {}).get("next_ms", 0)):
                        bf_tasks[s.label] = asyncio.create_task(self.backfill_stream(s))

                await asyncio.sleep(0.25)
        finally:
            for t in list(tasks.values()) + list(bf_tasks.values()):
                t.cancel()
            if tasks or bf_tasks:
                await asyncio.gather(*tasks.values(), *bf_tasks.values(),
                                     return_exceptions=True)
