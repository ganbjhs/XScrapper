"""
links.py — post LINKS as a watchlist: parse them, read them from a Google
Sheet, decide which are due, and classify what X said about one.

A `links` watchlist holds specific X post URLs rather than handles or
keywords. The collector fetches each post on its own cadence (12h/24h/48h)
with one TweetDetail call and OVERWRITES its engagement counters in place —
likes, retweets, replies, quotes, views, bookmarks — through the same
store.upsert_tweets every stream uses, which already keeps collected_ms
frozen and lets counters move. No history is kept here: the consumer that
reads /api/links keeps its own. X_LINKS_PLAN.md is the design; this module
is the part of it with no database and (mostly) no network, so the tests can
cover every branch offline.

Four things live here, in order:

  1. URL parsing      parse_status_url / find_status_links / scan_values
  2. Refresh planning is_due / order_due — pure functions over link rows
  3. Sheet reading    read_sheet (two Sheets API GETs) and sync_sheet, which
                      drives the store: every non-hidden tab is one links
                      watchlist keyed on the tab's numeric gid, every cell is
                      scanned, adds never delete
  4. Detail reading   classify_detail — what a TweetDetail payload says about
                      the ONE tweet we asked for: found, gone, or "try later"

Sheet access is the service-account route sheets.py already has (MODE_API):
reading is a subset of the scope we already request, and Viewer is enough.
"""

import re
import time
import urllib.parse
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 1. URL parsing
# --------------------------------------------------------------------------

# Hosts people actually paste. The "fixer" mirrors (vxtwitter, fxtwitter,
# fixupx, fixvx) rewrite x.com links for chat embeds and carry the same path.
_HOSTS = r"(?:x|twitter|vxtwitter|fxtwitter|fixupx|fixvx)\.com"

# One status URL, with or without a scheme, with or without www./mobile./m.,
# any handle (or the i/web and i forms X itself emits), /status/ or the old
# /statuses/, an optional /photo/N or /video/N tail, and any query string.
# Group 1 is the id. Ids are snowflakes (18-19 digits today); 5-25 keeps
# @jack's tweet 20 and leaves room. The look-behind is the LEFT boundary:
# without it `notx.com/…`, `spacex.com/updates/status/…` and
# `example.com/x.com/a/status/…` all matched and each burned a TweetDetail
# per cycle; the look-ahead stops `…/status/12345abc` at a partial id.
_LEFT = r"(?<![A-Za-z0-9.\-/])"
STATUS_RE = re.compile(
    r"(?i)" + _LEFT + r"(?:https?://)?(?:(?:www|mobile|m)\.)?" + _HOSTS +
    r"/(?:i/web|i|[A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,25})(?![\dA-Za-z])"
)

# A t.co short link. Resolved with one HEAD at sync time, never stored as is.
TCO_RE = re.compile(r"(?i)" + _LEFT + r"(?:https?://)?t\.co/[A-Za-z0-9]{3,}")

# Anything that looks like a link, for the "skipped" count: a cell that holds
# a URL that is NOT an X post (an Instagram reel, a news article) is worth
# reporting; a cell holding a label or a number is not.
_ANY_URL_RE = re.compile(r"(?i)(?:https?://|www\.)\S+")


def parse_status_url(text) -> int | None:
    """The tweet id in one URL (or a cell holding one), else None."""
    m = STATUS_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def find_status_links(text) -> list[tuple[int, str]]:
    """Every (tweet_id, url_as_written) in a piece of text, in order."""
    out = []
    for m in STATUS_RE.finditer(str(text or "")):
        out.append((int(m.group(1)), m.group(0)))
    return out


def find_tco_links(text) -> list[str]:
    return [m.group(0) for m in TCO_RE.finditer(str(text or ""))]


def canonical_url(tweet_id: int, handle: str | None = None) -> str:
    """https://x.com/<handle>/status/<id>; `i/web` when the handle is unknown."""
    who = (handle or "").strip().lstrip("@") or "i/web"
    return f"https://x.com/{who}/status/{int(tweet_id)}"


def looks_like_url(text) -> bool:
    return bool(_ANY_URL_RE.search(str(text or "")))


@dataclass
class ScanItem:
    tweet_id: int
    url: str
    row: int                    # 1-based sheet row
    section: str | None = None  # the heading the link sits under, if any


# A heading row: exactly ONE non-empty cell, holding no link and not a bare
# number. In the sheets this is built for, the day tabs are split by merged
# banner rows ("National X Influencers", "Counter Comments Links"), and the
# Sheets API returns a merged cell as a single value — so "one cell of text"
# is precisely what a heading looks like, and a "Posts | views | Likes"
# header row (three cells) or a stray "30,000" (a number) is not.
_MAX_HEADING = 80


def _heading_of(row) -> str | None:
    cells = [str(c or "").strip() for c in (row or [])]
    cells = [c for c in cells if c]
    if len(cells) != 1:
        return None
    text = " ".join(cells[0].split())
    if not text or len(text) > _MAX_HEADING or looks_like_url(text):
        return None
    if STATUS_RE.search(text) or TCO_RE.search(text):
        return None
    if text.replace(",", "").replace(".", "").replace("%", "").strip().isdigit():
        return None
    return text


# Tab titles that are dates. Day-first for the slash and dash forms — the
# sheets this reads are Indian (6/9/26 is 6 September), and an ambiguous
# 3/4/26 is read the same way, consistently. Month names, ISO and a bare
# "8 Sep" (year = current) are accepted too.
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DAY_RES = [
    re.compile(r"^(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})[/\-.](?P<y>\d{2}|\d{4})$"),
    re.compile(r"^(?P<y>\d{4})[/\-.](?P<m>\d{1,2})[/\-.](?P<d>\d{1,2})$"),
    re.compile(r"^(?P<d>\d{1,2})\s*(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{3,9})\.?,?\s*(?P<y>\d{2}|\d{4})?$"),
    re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s*(?P<y>\d{2}|\d{4})?$"),
]


def parse_tab_day(title, today=None) -> str | None:
    """
    ISO date (YYYY-MM-DD) when a tab title IS a date, else None.

      6/9/26  06-09-2026  2026-09-06  6 Sep  6 Sept 2026  Sep 6, 2026
    """
    import datetime as _dt

    t = " ".join(str(title or "").split()).strip()
    if not t:
        return None
    for rx in _DAY_RES:
        m = rx.match(t)
        if not m:
            continue
        g = m.groupdict()
        try:
            if g.get("mon"):
                mon = _MONTHS.get(g["mon"][:3].lower())
                if not mon:
                    return None
            else:
                mon = int(g["m"])
            y = g.get("y")
            if y:
                y = int(y)
                if y < 100:
                    y += 2000
            else:
                y = (today or _dt.date.today()).year
            return _dt.date(y, mon, int(g["d"])).isoformat()
        except (TypeError, ValueError):
            return None
    return None


@dataclass
class ScanResult:
    items: list = field(default_factory=list)       # ScanItem, deduped by id
    skipped: int = 0                                 # cells with a non-X link
    tco: list = field(default_factory=list)          # (row, url) to resolve
    cells: int = 0                                   # non-empty cells seen

    @property
    def ids(self) -> set:
        return {i.tweet_id for i in self.items}


def scan_values(values, start_row: int = 1) -> ScanResult:
    """
    Every X post link in a tab's cells, whatever column they sit in.

    `values` is what the Sheets API returns: a list of rows, each a list of
    cell strings (short rows are normal — trailing empties are omitted). The
    first occurrence of an id wins, so `row` is where the operator first put
    it. A cell holding some other URL counts as skipped so an Instagram link
    pasted by mistake is reported, not silently ignored; a cell holding a
    label or a date is neither. A one-cell text row is a SECTION heading and
    is remembered on every link beneath it (`ScanItem.section`).
    """
    res = ScanResult()
    seen: set[int] = set()
    section: str | None = None
    for r, row in enumerate(values or [], start=start_row):
        heading = _heading_of(row)
        if heading is not None:
            # The heading applies to every link below it, until the next.
            section = heading
            res.cells += 1
            continue
        for cell in (row or []):
            s = str(cell or "").strip()
            if not s:
                continue
            res.cells += 1
            hits = find_status_links(s)
            if hits:
                for tid, url in hits:
                    if tid in seen:
                        continue
                    seen.add(tid)
                    res.items.append(ScanItem(tid, url, r, section))
                continue
            tcos = find_tco_links(s)
            if tcos:
                for u in tcos:
                    res.tco.append((r, u))
                continue
            if looks_like_url(s):
                res.skipped += 1
    return res


# --------------------------------------------------------------------------
# 2. Refresh planning — pure functions over link rows
# --------------------------------------------------------------------------

STATUS_PENDING = "pending"          # never fetched, or waiting on a retry
STATUS_OK = "ok"                    # last fetch returned the post
STATUS_UNAVAILABLE = "unavailable"  # deleted / protected / suspended
STATUS_REMOVED = "removed"          # left the sheet or removed by hand
STATUSES = (STATUS_PENDING, STATUS_OK, STATUS_UNAVAILABLE, STATUS_REMOVED)

# The cadences the panel offers. Anything >= an hour is accepted by the
# store; these are the named ones.
REFRESH_CHOICES = {"12h": 43_200, "24h": 86_400, "48h": 172_800}
DEFAULT_REFRESH_S = 86_400
MIN_REFRESH_S = 3_600

# An unavailable post is retried on its normal cadence up to this many
# consecutive misses, then weekly — a deleted post is deleted, but a
# temporarily protected account comes back, and one call a week is nothing.
UNAVAILABLE_RETIRE_STREAK = 3
RETIRED_RETRY_S = 7 * 86_400

# A transient failure (no account free, network, X overloaded) backs off
# linearly per consecutive miss, capped, so a broken link cannot spin the
# loop and a broken pool cannot burn the day on one row.
TRANSIENT_RETRY_S = 1_800
TRANSIENT_RETRY_MAX_S = 6 * 3_600


def is_due(link: dict, now_ms: int) -> bool:
    """
    Should this link be fetched now?

      forced        -> yes (the panel's Refresh now)
      removed       -> never
      pending       -> yes, unless a transient failure just happened, in
                       which case after the back-off
      ok            -> when refresh_every_s has elapsed since last_refresh_ms
      unavailable   -> same cadence until UNAVAILABLE_RETIRE_STREAK misses,
                       then weekly
    """
    status = link.get("status") or STATUS_PENDING
    if status == STATUS_REMOVED:
        return False
    if link.get("force"):
        return True
    every_ms = int(link.get("refresh_every_s") or DEFAULT_REFRESH_S) * 1000
    last = link.get("last_refresh_ms")
    attempt = link.get("last_attempt_ms")
    streak = int(link.get("fail_streak") or 0)

    if status == STATUS_PENDING:
        if attempt and streak:
            wait = min(TRANSIENT_RETRY_S * streak, TRANSIENT_RETRY_MAX_S) * 1000
            return now_ms >= int(attempt) + wait
        return True

    # ok / unavailable: last_refresh_ms is set on both (a miss is a refresh
    # that learned something). A transient miss after a good read bumps the
    # streak without touching last_refresh_ms; it waits on last_attempt_ms.
    if last is None:
        return True
    if status == STATUS_UNAVAILABLE and streak >= UNAVAILABLE_RETIRE_STREAK:
        every_ms = RETIRED_RETRY_S * 1000
    if attempt and int(attempt) > int(last) and streak:
        wait = min(TRANSIENT_RETRY_S * streak, TRANSIENT_RETRY_MAX_S) * 1000
        return now_ms >= int(attempt) + wait
    return now_ms >= int(last) + every_ms


def order_due(links, now_ms: int) -> list:
    """
    The due subset, forced and never-fetched first, then the longest-waiting.

    A stable, explicit order rather than "whatever the query returned": the
    collector takes a batch off the front every tick, so the front has to be
    the rows an operator is watching for — the ones they just added or just
    pressed Refresh on.
    """
    due = [l for l in links if is_due(l, now_ms)]

    def key(l):
        never = l.get("last_refresh_ms") is None
        return (0 if l.get("force") else 1,
                0 if never else 1,
                int(l.get("last_refresh_ms") or 0),
                str(l.get("added_at") or ""),
                int(l.get("tweet_id") or 0))
    due.sort(key=key)
    return due


# --------------------------------------------------------------------------
# 3. Sheet reading
# --------------------------------------------------------------------------

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

# The sheet is re-read on this cadence. Two cheap GETs; a row pasted in has
# its first numbers within a couple of minutes of the next sync.
DEFAULT_SYNC_S = 600
MIN_SYNC_S = 60


@dataclass
class Tab:
    gid: int
    title: str
    hidden: bool = False
    values: list = field(default_factory=list)


@dataclass
class Snapshot:
    title: str
    tabs: list = field(default_factory=list)


def _why(rep) -> str:
    try:
        j = rep.json()
        msg = (j.get("error", {}).get("message")
               if isinstance(j.get("error"), dict) else j.get("error"))
    except Exception:
        msg = None
    return f"HTTP {rep.status_code}: {str(msg or rep.text or '')[:200]}"


def _access_hint(rep, sid: str) -> str:
    """Google's message plus the one fix operators actually need."""
    why = _why(rep)
    if rep.status_code in (403, 404):
        try:
            import sheets as _sheets
            email = _sheets.service_account_email()
        except Exception:
            email = ""
        who = f" with {email}" if email else " with the service account's client_email"
        return (f"{why} — share the sheet{who} (Viewer is enough), and check the "
                f"id {sid!r} is the /d/<id>/ part of its URL")
    return why


async def read_sheet(client, token: str, sheet_id: str,
                     include_hidden: bool = False) -> tuple[Snapshot | None, str]:
    """
    (snapshot, error). The spreadsheet's title and every tab with its cells.

    One GET for the tab list (titles, numeric ids, hidden flags), one per tab
    for its values. Never raises: the sync loop reports and moves on.
    """
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        rep = await client.get(
            f"{SHEETS_API}/{sheet_id}",
            params={"fields": "properties.title,sheets.properties(sheetId,title,hidden)"},
            headers=hdr, timeout=30.0)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if rep.status_code != 200:
        return None, _access_hint(rep, sheet_id)
    try:
        meta = rep.json()
    except ValueError as e:
        return None, f"unreadable sheet metadata: {e}"

    snap = Snapshot(title=str((meta.get("properties") or {}).get("title") or ""))
    for s in meta.get("sheets") or []:
        p = s.get("properties") or {}
        hidden = bool(p.get("hidden"))
        if hidden and not include_hidden:
            continue
        tab = Tab(gid=int(p.get("sheetId") or 0), title=str(p.get("title") or ""),
                  hidden=hidden)
        # Quote the tab name: 'Campaign A'!A:Z. A single quote inside is
        # doubled, per A1 notation — and the whole range is URL-encoded,
        # because a tab called "Q3/Q4", "What?" or "100% done" is a path
        # segment here, and unencoded it truncated the request and failed
        # the sync of every tab, forever.
        rng = "'" + tab.title.replace("'", "''") + "'!A:ZZ"
        try:
            vrep = await client.get(
                f"{SHEETS_API}/{sheet_id}/values/{urllib.parse.quote(rng, safe='')}",
                params={"majorDimension": "ROWS"}, headers=hdr, timeout=60.0)
        except Exception as e:
            return None, f"{tab.title!r}: {type(e).__name__}: {e}"
        if vrep.status_code != 200:
            return None, f"{tab.title!r}: {_access_hint(vrep, sheet_id)}"
        try:
            tab.values = list((vrep.json() or {}).get("values") or [])
        except ValueError as e:
            return None, f"{tab.title!r}: unreadable values: {e}"
        snap.tabs.append(tab)
    return snap, ""


async def resolve_tco(client, url: str) -> str | None:
    """Follow a t.co redirect to its status URL, or None. One request, no auth."""
    u = url if url.lower().startswith("http") else "https://" + url
    try:
        rep = await client.head(u, follow_redirects=True, timeout=10.0)
        final = str(rep.url)
        if parse_status_url(final):
            return final
        # Some t.co targets refuse HEAD; a GET's final URL is the same answer.
        rep = await client.get(u, follow_redirects=True, timeout=10.0)
        final = str(rep.url)
        return final if parse_status_url(final) else None
    except Exception:
        return None


async def sync_sheet(store, client, sheet: dict, now_ms: int | None = None,
                     log=None) -> dict:
    """
    One pass over one bound sheet: read every tab, add every link, mark the
    ones that left. Returns a summary for the log and the panel.

    `sheet` is a link_sheets row (dict). The store does every write; this
    function only decides what to tell it. Errors go onto the sheet row
    (`last_error`) and into the summary; nothing raises.
    """
    import sheets as _sheets

    now_ms = now_ms or int(time.time() * 1000)
    lsid = int(sheet["link_sheet_id"])
    sid = str(sheet["sheet_id"])
    summary = {"link_sheet_id": lsid, "sheet_id": sid, "tabs": 0,
               "watchlists": [], "found": 0, "added": 0, "revived": 0,
               "removed": 0, "skipped": 0, "tco_unresolved": 0, "error": ""}

    creds = _sheets.load_creds()
    if not creds:
        summary["error"] = (f"{_sheets.CREDS_ENV} is not set in .env on the server, "
                            f"or does not point at a readable service-account key")
        await store.link_sheet_synced(lsid, error=summary["error"], now_ms=now_ms)
        return summary
    token, err = await _sheets.access_token(client, creds)
    if err:
        summary["error"] = err
        await store.link_sheet_synced(lsid, error=err, now_ms=now_ms)
        return summary

    snap, err = await read_sheet(client, token, sid)
    if err or snap is None:
        summary["error"] = err or "empty response"
        await store.link_sheet_synced(lsid, error=summary["error"], now_ms=now_ms)
        return summary

    for tab in snap.tabs:
        summary["tabs"] += 1
        w = await store.links_watchlist_for_tab(lsid, tab.gid, tab.title,
                                                day=parse_tab_day(tab.title))
        if "error" in w:
            summary["error"] = w["error"]
            continue
        scan = scan_values(tab.values)
        items = [(i.tweet_id, i.url, i.row, i.section) for i in scan.items]
        present = set(scan.ids)
        for row, u in scan.tco:
            final = await resolve_tco(client, u)
            tid = parse_status_url(final) if final else None
            if tid and tid not in present:
                present.add(tid)
                items.append((tid, final, row, None))
            elif not tid:
                summary["tco_unresolved"] += 1
        added = await store.add_links(w["watchlist_id"], items, via="sheet",
                                      now_ms=now_ms)
        gone = await store.sync_removed_links(w["watchlist_id"], present)
        summary["found"] += len(items)
        summary["added"] += added.get("added", 0)
        summary["revived"] += added.get("revived", 0)
        summary["removed"] += gone
        summary["skipped"] += scan.skipped
        summary["watchlists"].append({
            "watchlist_id": w["watchlist_id"], "name": w["name"], "gid": tab.gid,
            "tab": tab.title, "day": w.get("sheet_day"), "found": len(items),
            "added": added.get("added", 0), "removed": gone, "skipped": scan.skipped})
        if log:
            log(f"[links] sheet {sid[:8]}… tab {tab.title!r}: {len(items)} links, "
                f"+{added.get('added', 0)} new, {gone} gone, {scan.skipped} skipped")

    await store.link_sheet_synced(lsid, title=snap.title,
                                  error=summary["error"] or None, now_ms=now_ms)
    return summary


# --------------------------------------------------------------------------
# 4. Detail reading — what X said about the one tweet we asked for
# --------------------------------------------------------------------------

OUTCOME_OK = "ok"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_TRANSIENT = "transient"

_DETAIL_PATH = ("data", "threaded_conversation_with_injections_v2", "instructions")


def _focal_entry(obj: dict, tweet_id: int) -> dict | None:
    """The timeline entry for exactly this tweet, wherever X put it."""
    want = f"tweet-{int(tweet_id)}"
    node = obj
    for k in _DETAIL_PATH:
        node = node.get(k) if isinstance(node, dict) else None
    instructions = node if isinstance(node, list) else []
    stack = list(instructions)
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if str(cur.get("entryId") or "") == want:
                return cur
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _tombstone_text(entry: dict) -> str:
    """X's own sentence for why the post cannot be shown, if it gave one."""
    stack = [entry]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("__typename") == "TweetTombstone" or "tombstone" in cur:
                ts = cur.get("tombstone") or {}
                text = ((ts.get("text") or {}).get("text")
                        if isinstance(ts.get("text"), dict) else ts.get("text"))
                if text:
                    return str(text).replace("Learn more", "").strip()
            if cur.get("__typename") == "TweetUnavailable":
                reason = cur.get("reason") or "unavailable"
                return f"unavailable ({reason})"
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def classify_detail(obj: dict, tweet_id: int, found: bool) -> tuple[str, str]:
    """
    (outcome, note) for a TweetDetail payload, given whether the parser found
    the tweet in it.

      found                         -> ok
      "No status found with that ID" (X's 200-with-errors for a deleted or
        never-existing id, which twscrape passes through)       -> unavailable
      a tombstone / TweetUnavailable entry for this id          -> unavailable,
        with X's sentence (deleted, protected, suspended…)
      anything else (a payload we do not recognise, an empty body from a
        shape change)                                           -> transient
    """
    if found:
        return OUTCOME_OK, ""
    errors = obj.get("errors") if isinstance(obj, dict) else None
    msgs = [str(e.get("message") or "") for e in (errors or []) if isinstance(e, dict)]
    if any("No status found" in m for m in msgs):
        return OUTCOME_UNAVAILABLE, "deleted (no status with that id)"
    entry = _focal_entry(obj, tweet_id) if isinstance(obj, dict) else None
    if entry is not None:
        text = _tombstone_text(entry)
        if text:
            return OUTCOME_UNAVAILABLE, text[:200]
    if msgs:
        return OUTCOME_TRANSIENT, ("X: " + "; ".join(msgs))[:200]
    return OUTCOME_TRANSIENT, "post not in the response (shape change or empty page)"
