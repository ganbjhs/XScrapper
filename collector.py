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


@dataclass
class PollResult:
    stream_label: str
    pages: int = 0
    results: int = 0
    new: int = 0
    dup: int = 0
    embedded: int = 0
    orphans: int = 0
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

                # Quoted tweets, reply parents and other context. Stored so the
                # dataset is self-contained, but tagged 'embedded' so they can
                # never move the watermark or pollute the lag numbers.
                for tid in page.embedded_ids:
                    committed.append((page.tweets[tid], page, "embedded", None))

                if page.result_ids:
                    res.results += len(page.result_ids)
                    for tid in page.result_ids:
                        tweet = page.tweets.get(tid)
                        if tweet is None:
                            continue  # orphan, already counted
                        committed.append((tweet, page, "result", page.entries_by_id.get(tid)))
                        if sf.is_snowflake(tid):
                            res.lags.append(sf.lag_ms(tid, page.collected_ms))

                    page_min = page.min_result_id()
                    page_max = page.max_result_id()
                    res.min_id = page_min if res.min_id is None else min(res.min_id, page_min)
                    res.max_id = page_max if res.max_id is None else max(res.max_id, page_max)

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
                # Generator finished on its own: X ran out of results.
                res.stop_reason = STOP_EXHAUSTED
    except Exception as e:
        res.stop_reason = STOP_ERROR
        res.error = f"{type(e).__name__}: {e}"
        if log:
            log(f"[{stream.label}] error: {res.error}")

    res.elapsed_s = time.time() - started

    # Zero pages is NOT a quiet stream. twscrape's generator ends silently when
    # no account can be acquired, so this is the only place that distinction
    # can be made -- and it must not be treated as "nothing new".
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


def jittered(interval: float) -> float:
    """
    Spread polls out.

    Without this, streams started together stay phase-locked and hit the
    account pool in a burst every tick. It also makes the traffic pattern less
    obviously machine-generated, which matters more than the efficiency.
    """
    return interval * (1 + random.uniform(-JITTER, JITTER))


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
                "SELECT paused, min_interval_s, max_pages_per_poll FROM streams "
                "WHERE label = ?", (stream.label,)).fetchone()
        except Exception:
            return True     # settings unreadable is not a reason to stop collecting
        if row is None:
            return True

        if row["min_interval_s"] is not None:
            stream.min_interval_s = float(row["min_interval_s"])
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
        delay = jittered(interval)
        st["next_ms"] = int((time.time() + delay) * 1000)

        if res.max_id:
            await self.store.set_watermark(
                sid, res.max_id,
                interval_s=interval, ewma_rate=ewma,
                consecutive_empty=empty, next_poll_ms=st["next_ms"],
            )

        self.log(self.format_result(res, interval))
        return res

    def format_result(self, res: PollResult, interval: float) -> str:
        bits = [
            f"[{res.stream_label}]",
            f"new={res.new}",
            f"dup={res.dup}",
            f"pages={res.pages}",
            f"stop={res.stop_reason}",
        ]
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
        last_discover = 0.0
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

                # Global pause: let in-flight polls finish and clean up, but
                # launch no new ones while collection is switched off.
                paused = self._paused()

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

                await asyncio.sleep(0.25)
        finally:
            for t in tasks.values():
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
