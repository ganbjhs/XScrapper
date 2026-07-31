"""
webhook.py — pushing collected tweets to other systems.

The dashboard is one consumer of `results.db`. This is how everything else gets
the same tweets, in near real time, without polling us.

    collector stores a tweet  ->  sender notices  ->  POST to your URL

Three properties, in the order they matter:

  * NOTHING IS LOST. Delivery position is a cursor in the database, not a queue
    in memory, so a receiver that was down for a day catches up by itself when
    it returns and a restart here changes nothing. See store.tweets_after.

  * DELIVERY NEVER SLOWS COLLECTION. The sender is its own task with its own
    HTTP client. A receiver that takes 10s to answer, or never answers, costs
    exactly one background task — polling X keeps its schedule. Getting this
    backwards would mean a third party's outage becoming our lag, which is the
    one number this project exists to protect.

  * EVERY DELIVERY IS SIGNED. The receiver can prove a request came from us.
    Without that, anyone who learns the URL can post fabricated tweets into
    your system and nothing downstream could tell the difference.

At-least-once, not exactly-once. The cursor advances only after a 2xx, so a
receiver that answers 200 and then dies before committing sees the batch again.
**Receivers must de-duplicate on `tweet_id`**, which is stable forever.
"""

import asyncio
import hashlib
import hmac
import json
import time

# Back-off after a failure: 5s, 10s, 20s ... capped. Capped rather than
# unbounded because the receiver coming back should be noticed in minutes, not
# hours, and the cursor means catching up is cheap when it does.
BACKOFF_BASE_S = 5
BACKOFF_MAX_S = 900

# How often the sender looks for new tweets when everything is healthy.
IDLE_POLL_S = 2.0

# Version the payload so a receiver can tell what shape to expect.
PAYLOAD_VERSION = 1


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """
    HMAC-SHA256 over "<timestamp>.<body>", hex, prefixed with the algorithm.

    The timestamp is INSIDE the signed material on purpose: signing the body
    alone lets anyone who captures one delivery replay it forever. The receiver
    should reject a timestamp more than a few minutes old.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str,
           max_age_s: int = 300) -> bool:
    """
    Reference implementation of the check a receiver should run.

    Not used by this project — it exists so the documentation can point at
    working code rather than prose, and so the test suite exercises the same
    function the far end will reimplement.
    """
    try:
        if abs(time.time() - int(timestamp)) > max_age_s:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature or "")


def _payload(hook, rows: list, labels_for: dict) -> dict:
    return {
        "version": PAYLOAD_VERSION,
        "webhook": hook.label,
        "sent_at": int(time.time()),
        "count": len(rows),
        "tweets": [_tweet_json(r, labels_for.get(r["tweet_id"], [])) for r in rows],
    }


def _tweet_json(row: dict, labels: list) -> dict:
    """
    One tweet, in the shape the API serves it.

    tweet_id is a STRING. Tweet ids passed 2^53 years ago, so any consumer
    using JSON numbers — every JavaScript one — would silently round it and
    corrupt the id. Sending a string makes that impossible rather than
    unlikely.
    """
    out = {k: row.get(k) for k in (
        "created_at", "collected_at", "lag_ms", "url", "text", "lang",
        "author_username", "author_display_name", "author_id", "author_followers",
        "reply_count", "retweet_count", "like_count", "quote_count", "view_count",
        "bookmark_count", "is_retweet", "is_reply", "is_quote",
        "in_reply_to", "conversation_id",
    )}
    out["tweet_id"] = str(row["tweet_id"])
    for k in ("hashtags", "mentions", "urls", "media_urls"):
        try:
            out[k] = json.loads(row.get(k) or "[]")
        except (TypeError, ValueError):
            out[k] = []
    for k in ("is_retweet", "is_reply", "is_quote"):
        out[k] = bool(out.get(k))
    out["streams"] = labels
    return out


async def deliver_batch(client, hook, store, rows: list) -> tuple[bool, str]:
    """
    POST one batch. Returns (ok, error). Never raises.

    The cursor is the caller's business: it advances only when this says ok,
    which is what makes a failure retry the same tweets rather than skip them.
    """
    labels_for = {r["tweet_id"]: await store.stream_labels_for(r["tweet_id"])
                  for r in rows}
    body = json.dumps(_payload(hook, rows, labels_for),
                      ensure_ascii=False, separators=(",", ":")).encode()
    ts = str(int(time.time()))

    try:
        rep = await client.post(
            hook.url,
            content=body,
            timeout=hook.timeout_s,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "x-collector-webhook/1",
                "X-XS-Timestamp": ts,
                "X-XS-Signature": sign(hook.secret, ts, body),
                "X-XS-Webhook": hook.label,
                "X-XS-Count": str(len(rows)),
            },
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if 200 <= rep.status_code < 300:
        return True, ""
    # Body included because "HTTP 422" alone sends you to read the receiver's
    # logs when it probably just told you what was wrong.
    return False, f"HTTP {rep.status_code}: {(rep.text or '')[:200]}"


def backoff_ms(failures: int) -> int:
    return int(min(BACKOFF_BASE_S * (2 ** max(0, failures - 1)), BACKOFF_MAX_S) * 1000)


async def pump(hook, store, client, log=print, once: bool = False) -> int:
    """
    Deliver everything outstanding for one endpoint.

    Returns the number of tweets sent. Stops at the first failure, leaving the
    cursor where it was, so the next attempt resends the same batch.
    """
    sent = 0
    while True:
        cur = await store.webhook_cursor(hook.label)
        now_ms = int(time.time() * 1000)
        if cur["next_attempt_ms"] > now_ms:
            return sent

        rows = await store.tweets_after(
            cur["last_ms"], cur["last_tweet_id"], hook.batch_size,
            labels=hook.streams or None, include_hidden=hook.include_hidden)
        if not rows:
            return sent

        ok, err = await deliver_batch(client, hook, store, rows)
        if not ok:
            fails = cur["failures"] + 1
            wait = backoff_ms(fails)
            await store.webhook_failed(hook.label, err, now_ms + wait)
            log(f"[webhook:{hook.label}] {len(rows)} tweet(s) not delivered: {err} "
                f"(retry in {wait // 1000}s, attempt {fails})")
            return sent

        last = rows[-1]
        await store.webhook_advance(
            hook.label, last["collected_ms"], last["tweet_id"], len(rows))
        sent += len(rows)
        if cur["failures"]:
            log(f"[webhook:{hook.label}] recovered after {cur['failures']} failure(s)")
        log(f"[webhook:{hook.label}] delivered {len(rows)} tweet(s)")

        if once or len(rows) < hook.batch_size:
            return sent


async def run(cfg, store, log=print, stop: asyncio.Event | None = None) -> None:
    """
    The delivery loop. Runs alongside the collector for as long as it does.

    New endpoints start from NOW, not from the beginning of the database — see
    store.webhook_start_here. Pointing a fresh webhook at months of history and
    having it immediately fire all of it is a surprise nobody wants.
    """
    import httpx

    hooks = cfg.enabled_webhooks()
    if not hooks:
        return

    for h in hooks:
        cur = await store.webhook_cursor(h.label)
        if not cur["last_ms"] and not cur["sent"]:
            await store.webhook_start_here(h.label)
            log(f"[webhook:{h.label}] new endpoint — starting from now, not from "
                f"the whole archive")
        log(f"[webhook:{h.label}] -> {h.url}"
            + (f" (streams: {', '.join(h.streams)})" if h.streams else ""))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        while stop is None or not stop.is_set():
            for h in hooks:
                try:
                    await pump(h, store, client, log=log)
                except Exception as e:
                    # One endpoint misbehaving must not take the loop down and
                    # stop every other endpoint with it.
                    log(f"[webhook:{h.label}] sender error: {type(e).__name__}: {e}")
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_POLL_S)
                else:
                    await asyncio.sleep(IDLE_POLL_S)
            except asyncio.TimeoutError:
                pass
