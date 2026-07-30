"""
Canned X GraphQL payloads, so the parsing and watermark logic can be tested
without touching the network or burning an account's rate-limit budget.
"""

import pathlib
import sys
import time
from email.utils import formatdate

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import store as sf  # snowflake helpers  # noqa: E402

# Fixtures are anchored to the CURRENT time, not to hardcoded ids. Two things
# in the pipeline are time-relative and would be silently untested otherwise:
# the lag report excludes tweets created before a stream's first poll, and the
# 24h reporting window excludes anything older.
NOW_MS = int(time.time() * 1000)


def id_at(offset_ms: int) -> int:
    """A snowflake id for `offset_ms` relative to now (negative = older)."""
    return sf.ms_to_id(NOW_MS + offset_ms)


# Descending in time, spaced minutes apart like a real stream.
ID_NEWEST = id_at(-60_000)
ID_RETWEET = id_at(-180_000)
ID_ORIGINAL = id_at(-300_000)
ID_OLD_QUOTE = 300000000000000000  # a genuine 2015-era embedded quote

DATE_HEADER_TS = NOW_MS / 1000
DATE_HEADER = formatdate(DATE_HEADER_TS, usegmt=True)


def mk_tweet(tid, uid, text, created="Tue Jul 28 09:59:00 +0000 2026", rt_of=None):
    legacy = {
        "id_str": str(tid),
        "full_text": text,
        "created_at": created,
        "user_id_str": str(uid),
        "conversation_id_str": str(tid),
        "lang": "en",
        "reply_count": 1,
        "retweet_count": 2,
        "favorite_count": 3,
        "quote_count": 4,
        "bookmark_count": 5,
        "entities": {},
    }
    if rt_of:
        legacy["retweeted_status_id_str"] = str(rt_of)
    return {"__typename": "Tweet", "rest_id": str(tid), "legacy": legacy, "views": {"count": "99"}}


def mk_user(uid, name):
    return {
        "__typename": "User",
        "rest_id": str(uid),
        "legacy": {
            "screen_name": name,
            "name": name.title(),
            "created_at": "Tue Jul 28 10:00:00 +0000 2020",
            "followers_count": 5,
            "friends_count": 1,
            "statuses_count": 1,
            "favourites_count": 0,
            "listed_count": 0,
            "media_count": 0,
            "description": "",
            "location": "",
            "profile_image_url_https": "",
        },
    }


def _entry(tid, obj, user):
    return {
        "entryId": f"tweet-{tid}",
        "sortIndex": str(tid),
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {"result": {**obj, "core": {"user_results": {"result": user}}}},
            },
        },
    }


def search_payload(ids=None, cursor="CURSOR_PAGE2", include_traps=True):
    """
    A SearchTimeline response.

    With include_traps (the default) it contains the two shapes that break
    twscrape's own parser and any naive watermark logic:

      * ID_RETWEET is a retweet OF ID_ORIGINAL, while ID_ORIGINAL is itself a
        separate search hit on the same page. twscrape's _parse_items drops
        ID_ORIGINAL entirely in this situation.
      * ID_OLD_QUOTE is a decade-old tweet embedded as quoted context. Its
        snowflake id is tiny, so any watermark check that looks at all parsed
        tweets rather than at entry ids would stop on page 1 forever.
    """
    alice, bob, carol = mk_user(11, "alice"), mk_user(12, "bob"), mk_user(13, "carol")
    entries = []

    if ids is None:
        entries.append(_entry(ID_NEWEST, mk_tweet(ID_NEWEST, 11, "newest post"), alice))
        if include_traps:
            rt = mk_tweet(ID_RETWEET, 12, "RT @alice: hello", rt_of=ID_ORIGINAL)
            entries.append(_entry(ID_RETWEET, rt, bob))
        entries.append(_entry(ID_ORIGINAL, mk_tweet(ID_ORIGINAL, 11, "hello"), alice))
    else:
        for i, tid in enumerate(ids):
            entries.append(_entry(tid, mk_tweet(tid, 11, f"post {i}"), alice))

    if include_traps:
        quoted = mk_tweet(ID_OLD_QUOTE, 13, "ancient", created="Tue Jul 28 10:00:00 +0000 2015")
        quoted["core"] = {"user_results": {"result": carol}}
        host_id = (ids[0] if ids else ID_NEWEST)
        entries[0]["content"]["itemContent"]["tweet_results"]["result"]["quoted_status_result"] = {
            "result": quoted
        }
        # Keep the host's own id distinct from the quote's.
        entries[0]["content"]["itemContent"]["tweet_results"]["result"]["rest_id"] = str(host_id)

    if cursor:
        entries.append(
            {"entryId": "cursor-bottom-0", "content": {"value": cursor, "cursorType": "Bottom"}}
        )
    # Entries X sends that are not results, and must be filtered out.
    entries.append({"entryId": "who-to-follow-1", "content": {"entryType": "TimelineTimelineModule"}})

    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}
                }
            }
        }
    }


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class FakeResponse:
    """Quacks like twscrape's Response wrapper."""

    def __init__(self, payload, username="alice", remaining=47, limit=50, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = FakeHeaders(
            {
                "x-rate-limit-limit": str(limit),
                "x-rate-limit-remaining": str(remaining),
                "x-rate-limit-reset": "1900000000",
                "date": DATE_HEADER,
                "content-type": "application/json",
            }
        )
        setattr(self, "__username", username)

    def json(self):
        return self._payload
