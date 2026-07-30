"""
engine.py — the scraping seam. Everything twscrape-specific lives here.

Two things this module does that twscrape's own `api.search()` cannot:

1. It yields PAGES, not a flat stream of tweets, and each page carries the
   timeline entry order, the serving account, and the rate-limit budget. The
   watermark poller needs all three: it must know whether a page's OLDEST
   result has fallen below the watermark, which requires the true timeline
   order, and the scheduler needs the rate-limit headers to pace itself.

2. It parses without twscrape's `_parse_items`, which is unusable here for two
   reasons (both verified in models.py, both asserted in engine.py):

     * It iterates `obj["tweets"].values()` — a dict harvested from the WHOLE
       payload, including quoted tweets, reply parents and card embeds. The
       order is dict-insertion order, not timeline order, and an embedded 2019
       quote carries a tiny snowflake id. Stopping at "the first id below the
       watermark" would terminate on page 1 every time.
     * It skips any tweet whose id appears in `retweeted_ids`. A tweet that is
       both a search hit and retweeted by another hit on the same page is
       dropped outright. That is silent data loss.

   So: entry ids (`tweet-<snowflake>`, in true timeline order) define the
   result set; `to_old_rep` + `Tweet.parse` provide the objects; everything
   else in the payload is kept as context, tagged `embedded`, and never allowed
   to influence the watermark.

CRITICAL — every caller MUST wrap search_pages in `contextlib.aclosing`:

    async with aclosing(engine.search_pages(q)) as gen:
        async for page in gen:
            ...
            break                      # safe

Acquiring an account locks it for 15 minutes (accounts_pool.py:268), and the
lock is released by QueueClient.__aexit__ — which only runs when the generator
is CLOSED, not when the caller stops iterating.

Measured behaviour (tests/test_all.py reproduces all three):

  * break, generator then goes out of scope -> CPython's refcounting finalizes
    it and the lock IS released. Plain `break` happens to work here.
  * break while ANY reference to the generator is still alive -> the lock is
    held for the full 15 minutes. gc.collect() does not help, because there is
    nothing to collect. The account is simply gone from the pool.
  * aclosing -> released immediately and deterministically, in every case,
    including when the loop body raises.

So this is a conditional trap rather than a guaranteed one: it fires whenever a
reference outlives the break (storing the generator, capturing it in a task,
wrapping it) or on any runtime without prompt refcounting. Since the watermark
poller breaks early on essentially every poll, and the failure mode is an
account silently vanishing for 15 minutes, aclosing is mandatory rather than
advisory.
"""

import inspect
import re
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from twscrape.models import Tweet
from twscrape.utils import find_obj, get_by_path, to_old_rep



# ==========================================================================
# compatibility — assert the twscrape internals we depend on
# ==========================================================================

PINNED_VERSION = "0.19.2"


@dataclass
class Report:
    ok: bool = True
    lines: list[str] = field(default_factory=list)

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        self.lines.append(("OK   " if cond else "BROKEN ") + label + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            self.ok = False


def check() -> Report:
    r = Report()

    import twscrape
    from twscrape.accounts_pool import AccountsPool
    from twscrape.api import GQL_URL, OP_SearchTimeline, API
    from twscrape.models import Tweet
    from twscrape.utils import to_old_rep

    # twscrape exposes no __version__ attribute; the installed dist metadata is
    # the only reliable source.
    try:
        from importlib.metadata import version as _dist_version

        version = _dist_version("twscrape")
    except Exception:
        version = getattr(twscrape, "__version__", "?")
    r.check(
        f"twscrape version is the pinned {PINNED_VERSION} (found {version})",
        version == PINNED_VERSION,
        "re-run this after every upgrade; the checks below are what to verify",
    )

    # --- account pool ---
    r.check(
        "AccountsPool._order_by exists (we set it to LRU so one account is not hammered)",
        hasattr(AccountsPool, "_order_by"),
    )
    r.check(
        "AccountsPool.save() exists (the only correct upsert; add_account early-returns)",
        callable(getattr(AccountsPool, "save", None)),
    )
    for name in ("get_account", "set_active", "mark_inactive", "get_all", "reset_locks"):
        r.check(f"AccountsPool.{name}() exists", callable(getattr(AccountsPool, name, None)))

    try:
        src = inspect.getsource(AccountsPool._get_and_lock)
        r.check(
            "account acquisition uses the atomic UPDATE...RETURNING path",
            "RETURNING *" in src,
            "falls back to a racy _tx path on sqlite < 3.35",
        )
        r.check(
            "acquisition locks an account for 15 minutes (this is why aclosing is mandatory)",
            "+15 minutes" in src,
        )
    except (OSError, TypeError):
        r.check("could read AccountsPool._get_and_lock source", False)

    # --- cursor injection (lets us resume pagination mid-stream) ---
    try:
        src = inspect.getsource(API._gql_items)
        r.check(
            "_gql_items only sets variables['cursor'] when its own cursor is non-None, "
            "so a caller-supplied cursor survives page 1",
            'if cur is not None:' in src and 'params["variables"]["cursor"] = cur' in src,
        )
        r.check(
            "_gql_items retries empty-but-cursored pages (the 3x budget tax on quiet streams)",
            "empty_pages" in src,
        )
    except (OSError, TypeError):
        r.check("could read API._gql_items source", False)

    try:
        src = inspect.getsource(API.search_raw)
        r.check(
            "search_raw spreads caller kv LAST, so product/count/cursor overrides win",
            "**(kv or {})" in src,
        )
    except (OSError, TypeError):
        r.check("could read API.search_raw source", False)

    # --- response annotations we read off each page ---
    try:
        import twscrape.queue_client as qc

        src = inspect.getsource(qc)
        r.check(
            'responses are tagged with "__username" (how we attribute a page to an account)',
            'setattr(rep, "__username"' in src,
        )
        r.check(
            "rate-limit headers are read from the response (we size our budget from them)",
            "x-rate-limit-remaining" in src,
        )
    except (OSError, TypeError):
        r.check("could read queue_client source", False)

    # --- parse path ---
    r.check("to_old_rep() is importable (our parse bypass depends on it)", callable(to_old_rep))
    r.check("Tweet.parse() is importable", callable(getattr(Tweet, "parse", None)))
    try:
        from twscrape.models import _parse_items

        src = inspect.getsource(_parse_items)
        drops = "retweeted_ids" in src and "continue" in src
        r.check(
            "confirmed: twscrape's own parser drops retweeted originals — "
            "we bypass it for exactly this reason",
            drops,
            "twscrape may have fixed this; our bypass is then merely redundant, not wrong",
        )
    except (OSError, TypeError, ImportError):
        r.lines.append("note   could not inspect _parse_items (non-fatal)")

    # --- endpoint constants ---
    r.check(
        f"OP_SearchTimeline looks like '<doc_id>/SearchTimeline' ({OP_SearchTimeline})",
        OP_SearchTimeline.endswith("/SearchTimeline") and "/" in OP_SearchTimeline,
    )
    r.check("GQL_URL points at x.com's graphql endpoint", GQL_URL.endswith("/i/api/graphql"))

    return r


# ==========================================================================
# page parsing and the Engine
# ==========================================================================

# Timeline entries for search results look like "tweet-1750000000000000000".
_ENTRY_RE = re.compile(r"^tweet-(\d+)$")

# Where SearchTimeline puts its instruction list. We walk this explicitly
# rather than using get_by_path so that ordering is guaranteed; the path
# lookup is the fallback if X reshapes the response.
_TIMELINE_PATH = ("data", "search_by_raw_query", "search_timeline", "timeline", "instructions")


@dataclass
class Page:
    """One GraphQL response, parsed but not yet interpreted."""

    page_no: int
    received_ts: float                       # local clock, at response receipt
    server_ts: float | None                  # from the HTTP Date header
    account: str | None                      # which account served this page
    status: int

    # Rate-limit budget as reported by X. The scheduler sizes itself from these
    # rather than from an assumed 50/15min.
    rl_limit: int | None = None
    rl_remaining: int | None = None
    rl_reset: int | None = None

    # The authoritative, ordered result set: entry ids, newest first.
    result_ids: list[int] = field(default_factory=list)
    entries_by_id: dict[int, dict] = field(default_factory=dict)

    # EVERY tweet in the payload, including embedded context.
    tweets: dict[int, Tweet] = field(default_factory=dict)

    cursor: str | None = None
    raw: dict = field(default_factory=dict)
    parse_failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def collected_ms(self) -> int:
        """Preferred clock for lag: the server's, which no RDP box can skew."""
        ts = self.server_ts if self.server_ts is not None else self.received_ts
        return int(ts * 1000)

    @property
    def orphan_ids(self) -> list[int]:
        """Result ids with no parseable tweet — a payload-shape health signal."""
        return [i for i in self.result_ids if i not in self.tweets]

    @property
    def embedded_ids(self) -> list[int]:
        """Tweets present as context only (quotes, parents), never search hits."""
        return [i for i in self.tweets if i not in self.entries_by_id]

    def min_result_id(self) -> int | None:
        return min(self.result_ids) if self.result_ids else None

    def max_result_id(self) -> int | None:
        return max(self.result_ids) if self.result_ids else None


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------

def _timeline_entries(obj: dict) -> list[dict]:
    """Timeline entries in server order."""
    node = obj
    for key in _TIMELINE_PATH:
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(key)

    entries: list[dict] = []
    if isinstance(node, list):
        for ins in node:
            if not isinstance(ins, dict):
                continue
            kind = ins.get("type")
            if kind == "TimelineAddEntries":
                entries.extend(x for x in (ins.get("entries") or []) if isinstance(x, dict))
            elif kind == "TimelineReplaceEntry":
                # Carries a replacement cursor, and occasionally an entry.
                entry = ins.get("entry")
                if isinstance(entry, dict):
                    entries.append(entry)
    if not entries:
        # Shape drift fallback: find the first "entries" list anywhere.
        found = get_by_path(obj, "entries")
        if isinstance(found, list):
            entries = [x for x in found if isinstance(x, dict)]
    return entries


def _int_headers(headers) -> tuple[int | None, int | None, int | None]:
    def get(name):
        try:
            val = headers.get(name)
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    return get("x-rate-limit-limit"), get("x-rate-limit-remaining"), get("x-rate-limit-reset")


def _server_ts(headers) -> float | None:
    try:
        raw = headers.get("date")
        return parsedate_to_datetime(raw).timestamp() if raw else None
    except (TypeError, ValueError):
        return None


def parse_page(rep, page_no: int) -> Page:
    """Turn one twscrape Response into a Page. No twscrape parser involved."""
    obj = rep.json()
    rl_limit, rl_remaining, rl_reset = _int_headers(rep.headers)

    page = Page(
        page_no=page_no,
        received_ts=time.time(),
        server_ts=_server_ts(rep.headers),
        account=getattr(rep, "__username", None),
        status=rep.status_code,
        rl_limit=rl_limit,
        rl_remaining=rl_remaining,
        rl_reset=rl_reset,
        raw=obj,
    )

    # 1. Result set and order, from entry ids only.
    for entry in _timeline_entries(obj):
        m = _ENTRY_RE.match(str(entry.get("entryId") or ""))
        if not m:
            continue
        tid = int(m.group(1))
        if tid not in page.entries_by_id:
            page.result_ids.append(tid)
            page.entries_by_id[tid] = entry

    # 2. Objects, via twscrape's own normalizer but not its item filter.
    try:
        flat = to_old_rep(obj)
    except Exception as e:
        page.parse_failures.append(("<to_old_rep>", f"{type(e).__name__}: {e}"))
        flat = {"tweets": {}}

    for key, raw_tweet in (flat.get("tweets") or {}).items():
        try:
            tweet = Tweet.parse(raw_tweet, flat)
            page.tweets[int(tweet.id)] = tweet
        except Exception as e:
            page.parse_failures.append((str(key), f"{type(e).__name__}: {e}"))

    # 3. Next cursor. Mirrors api.py:145-150.
    cur = find_obj(obj, lambda x: x.get("cursorType") == "Bottom")
    page.cursor = (cur or {}).get("value") or get_by_path(obj, "next_cursor")

    return page


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

class Engine:
    """Thin wrapper over twscrape's API. Swap this to change the transport."""

    def __init__(self, api):
        self.api = api

    async def search_pages(
        self,
        query: str,
        *,
        tab: str = "Latest",
        page_size: int = 20,
        max_pages: int = 0,
        limit: int = -1,
        cursor: str | None = None,
    ):
        """
        Yield Page objects for an advanced-search query.

        `query` takes X's advanced-search operators verbatim:
            from:nasa since:2026-01-01 min_faves:500 -filter:replies

        `cursor` resumes pagination where a previous walk stopped. This works
        because twscrape only overwrites variables["cursor"] once it has a
        cursor of its own, so a caller-supplied one survives page 1
        (asserted in engine.py).

        The caller MUST wrap this in contextlib.aclosing — see the module
        docstring. This method wraps its own inner generator for the same
        reason, so that closing propagates all the way down to the account
        unlock.
        """
        kv = {"product": tab, "count": page_size}
        if cursor:
            kv["cursor"] = cursor

        page_no = 0
        async with aclosing(self.api.search_raw(query, limit=limit, kv=kv)) as gen:
            async for rep in gen:
                page_no += 1
                yield parse_page(rep, page_no)
                if max_pages and page_no >= max_pages:
                    return

    async def list_pages(
        self,
        list_id: int | str,
        *,
        page_size: int = 20,
        max_pages: int = 0,
        limit: int = -1,
        cursor: str | None = None,
    ):
        """
        Yield Page objects for an X List timeline.

        Same shape as search_pages, and deliberately so: the page parser, the
        watermark, the overlap window and the stop reasons are all identical.
        A list is just a different way of choosing which tweets appear.

        Two things make lists worth having beyond convenience:

        1. SEPARATE RATE-LIMIT BUDGET. twscrape derives the queue name from the
           GraphQL operation (api.py `_gql_items`: `op.split("/")[-1]`), so this
           runs on "ListLatestTweetsTimeline" while search runs on
           "SearchTimeline". They do not compete — adding list streams raises
           total capacity instead of dividing the existing budget.

        2. NO QUERY LENGTH LIMIT. Following 200 accounts via search means
           `from:a OR from:b OR ...`, which busts X's ~512-character query
           ceiling around 25 accounts. A list has no such bound.

        The trade-off: X applies no server-side filtering here. There is no
        min_faves or lang to pass, so any narrowing happens locally, after the
        tweets are already collected and paid for.

        As with search_pages, the caller MUST wrap this in aclosing.
        """
        kv = {"count": page_size}
        if cursor:
            kv["cursor"] = cursor

        page_no = 0
        async with aclosing(
            self.api.list_timeline_raw(int(list_id), limit=limit, kv=kv)
        ) as gen:
            async for rep in gen:
                page_no += 1
                yield parse_page(rep, page_no)
                if max_pages and page_no >= max_pages:
                    return

    def pages_for(self, stream, **kw):
        """
        Dispatch a stream to the right generator.

        Keeps the source choice in ONE place. Every caller — collector, CLI,
        dashboard — goes through here, so adding another source later means
        touching this function and nothing else.
        """
        if getattr(stream, "list_id", None):
            kw.pop("tab", None)          # a list timeline has no product tab
            return self.list_pages(stream.list_id, **kw)
        return self.search_pages(stream.query, **kw)

    async def search(self, query: str, limit: int = 50, tab: str = "Latest") -> list[Tweet]:
        """
        One-shot search returning tweets in timeline order.

        Signature-compatible with the prototype's Engine.search, but it no
        longer loses tweets that were retweeted by another result on the same
        page, and the ordering is now the real timeline order rather than dict
        insertion order.
        """
        out: list[Tweet] = []
        seen: set[int] = set()
        async with aclosing(self.search_pages(query, tab=tab, limit=limit)) as gen:
            async for page in gen:
                for tid in page.result_ids:
                    tweet = page.tweets.get(tid)
                    if tweet is not None and tid not in seen:
                        seen.add(tid)
                        out.append(tweet)
                        if 0 < limit <= len(out):
                            return out
        return out