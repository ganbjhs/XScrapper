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
import os
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
    # The structured media view — [{type, url, thumb, duration?}] — is what the
    # dashboard renders from, and it is the ONLY place a video's thumbnail
    # exists (media_urls is a flat list of the mp4/photo URLs). Leaving it out
    # meant the receiver could never show an image or a video still without
    # re-deriving it, which Watch-Tower rightly does not do. Same degrade-to-[]
    # rule as the other JSON columns: a malformed row must not stop delivery.
    try:
        out["media"] = json.loads(row.get("media_json") or "[]")
    except (TypeError, ValueError):
        out["media"] = []
    for k in ("is_retweet", "is_reply", "is_quote"):
        out[k] = bool(out.get(k))
    out["streams"] = labels
    return out


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
#
# A second delivery target, sharing every hard part with webhooks: the same
# cursor, the same back-off, the same "never block collection". Only the
# formatting and the transport differ, which is the whole reason this lives
# beside webhooks instead of being its own subsystem.
#
# ONE TWEET PER MESSAGE. Tweets used to be packed together up to TG_MAX_CHARS,
# which was cheaper but unreadable: several unrelated posts ran into one wall of
# text with no way to forward, reply to or delete a single one.
#
# The cost of that choice is real and is paid here. Telegram allows about 30
# messages a second overall but only ~20 a minute INTO ONE GROUP, so one message
# per tweet means the gap between sends is what keeps us under the limit — hence
# TG_GAP_S below is 3.2s, not the 1.2s that sufficed when messages were packed.
# A batch of 20 therefore takes just over a minute to land. That is the correct
# trade: arriving a minute later is a nuisance, collecting 429s is an outage.
TELEGRAM_API = "https://api.telegram.org"
TG_MAX_CHARS = 3500          # the hard cap is 4096; leave room for the footer
TG_GAP_S = 3.2               # ~20 messages/minute into one group, Telegram's cap


def _tg_escape(s: str) -> str:
    """Telegram HTML mode understands only these three."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




def tg_format(rows: list, labels_for: dict | None = None) -> list:
    """
    Render tweets into Telegram messages — EXACTLY ONE MESSAGE PER TWEET.

    THE MESSAGE IS THE TWEET AND NOTHING ELSE. No display name, no @handle, no
    like count, no stream label, no permalink of ours — just the text as the
    author wrote it, which already ends with the t.co link X attaches to the
    post. Everything that used to sit around it was our framing of someone
    else's words, and in a channel you read all day that framing is most of what
    is on screen.

    Each of those was added at some point and each is deliberately gone:

      * the stream label printed the whole query for a dashboard-created search
        ("ui:from:RajeshGupta5766 -filter:replies -filter:retweets"),
      * the permalink duplicated the t.co already in the text,
      * the name/handle/likes header repeated what the linked post shows anyway.

    One tweet per message, never packed. See TG_GAP_S above for the price.

    HTML rather than Markdown: tweet text is full of underscores, asterisks and
    brackets, and Telegram's Markdown parser rejects the whole message if they
    do not balance. Escaping three characters for HTML always works; escaping
    Markdown correctly does not.

    labels_for is accepted and ignored so the webhook path can keep calling this
    with the same shape it always has.
    """
    out = []
    for r in rows:
        def build(body):
            return _tg_escape(body)

        # A tweet longer than a whole message is trimmed rather than dropped: a
        # truncated post you can click through beats silence.
        #
        # The RAW text is trimmed and escaped afterwards, never the other way
        # round. Cutting an already-escaped string can slice through the middle
        # of an entity — "&amp;" becoming "&am" — and Telegram rejects the whole
        # message as malformed HTML.
        raw = (r.get("text") or "").strip()
        # A media-only post can have no text at all. Telegram rejects an empty
        # message, so fall back to the link rather than losing the post.
        if not raw:
            raw = r.get("url") or f"https://x.com/i/status/{r['tweet_id']}"
        block = build(raw)
        while len(block) > TG_MAX_CHARS and raw:
            raw = raw[:max(0, len(raw) - (len(block) - TG_MAX_CHARS) - 16)]
            block = build(raw + "…")
        out.append(block)
    return out


async def tg_send(client, token: str, chat_id: str, text: str,
                  timeout: float = 15.0) -> tuple[bool, str]:
    """One sendMessage call. Never raises."""
    try:
        rep = await client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=timeout,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if rep.status_code == 200:
        return True, ""
    # Telegram explains itself in the body, and the explanation is usually the
    # whole answer ("chat not found", "bot was blocked by the user").
    try:
        detail = rep.json().get("description") or rep.text
    except Exception:
        detail = rep.text
    return False, f"HTTP {rep.status_code}: {str(detail)[:200]}"


async def deliver_telegram(client, hook, store, rows: list) -> tuple[bool, str]:
    """Send a batch to Telegram, respecting their rate limit between messages.

    No stream lookup here any more: the message no longer prints the label, so
    the per-tweet stream_labels_for query it needed is a database round trip
    per tweet for a line nobody wanted.
    """
    for i, msg in enumerate(tg_format(rows)):
        if i:
            await asyncio.sleep(TG_GAP_S)
        ok, err = await tg_send(client, hook.token, hook.chat_id, msg,
                                timeout=hook.timeout_s)
        if not ok:
            return False, err
    return True, ""


async def deliver_batch(client, hook, store, rows: list) -> tuple[bool, str]:
    """
    Deliver one batch. Returns (ok, error). Never raises.

    The cursor is the caller's business: it advances only when this says ok,
    which is what makes a failure retry the same tweets rather than skip them.
    """
    if getattr(hook, "kind", "webhook") == "telegram":
        return await deliver_telegram(client, hook, store, rows)

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


def _wanted(hook, row) -> bool:
    """Per-target filters, so a noisy stream can be narrowed on its way out."""
    if getattr(hook, "skip_retweets", False) and row.get("is_retweet"):
        return False
    # Checked HERE and not left to the search query. "-filter:replies" is a hint
    # to X's search, honoured imperfectly and not at all for a stream collected
    # another way; is_reply is our own parsed field, so this is the check that
    # actually holds.
    if getattr(hook, "skip_replies", False) and row.get("is_reply"):
        return False
    # Age is measured on created_ms (when it was POSTED), never collected_ms.
    # The cursor already keys on collection, so without this a stream that has
    # just started delivers its entire backlog — six-day-old posts landing as
    # though they had just appeared.
    max_age_h = getattr(hook, "max_age_h", 0) or 0
    if max_age_h:
        created = row.get("created_ms") or 0
        if created and (time.time() * 1000 - created) > max_age_h * 3600_000:
            return False
    if (row.get("like_count") or 0) < getattr(hook, "min_likes", 0):
        return False
    return True


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
            labels=hook.streams or None,
            project_id=getattr(hook, "project_id", None))
        if not rows:
            return sent

        # Filtering happens AFTER the cursor read, never inside the query.
        #
        # The cursor must advance past a tweet we chose not to send, or a
        # stream whose every tweet is filtered out would wedge the endpoint
        # forever: the same rows would come back, all be dropped, nothing would
        # be delivered, and the position would never move.
        keep = [r for r in rows if _wanted(hook, r)]
        if not keep:
            last = rows[-1]
            await store.webhook_advance(
                hook.label, last["collected_ms"], last["tweet_id"], 0)
            if once:
                return sent
            continue
        rows_all, rows = rows, keep

        ok, err = await deliver_batch(client, hook, store, rows)
        if not ok:
            fails = cur["failures"] + 1
            wait = backoff_ms(fails)
            await store.webhook_failed(hook.label, err, now_ms + wait)
            log(f"[webhook:{hook.label}] {len(rows)} tweet(s) not delivered: {err} "
                f"(retry in {wait // 1000}s, attempt {fails})")
            return sent

        # Advance past everything READ, not just everything sent.
        last = rows_all[-1]
        await store.webhook_advance(
            hook.label, last["collected_ms"], last["tweet_id"], len(rows))
        sent += len(rows)
        if cur["failures"]:
            log(f"[webhook:{hook.label}] recovered after {cur['failures']} failure(s)")
        log(f"[webhook:{hook.label}] delivered {len(rows)} tweet(s)")

        if once or len(rows_all) < hook.batch_size:
            return sent


class TelegramTarget:
    """
    A Telegram destination for one stream, assembled from the database.

    Deliberately NOT declared in config.toml like webhooks are. Telegram is
    switched on per stream from the dashboard, so its settings have to be
    somewhere the dashboard can write and the running watcher can re-read —
    which is the streams table. The bot token is the exception: it is a
    credential, so it lives in .env with every other secret.

    It quacks like a WebhookCfg because `pump` should not care which kind it is
    handling; only `deliver_batch` does.
    """

    kind = "telegram"
    url = TELEGRAM_API      # for logging only

    def __init__(self, stream_label, token, chat_id, *, min_likes=0,
                 skip_retweets=False, skip_replies=False, max_age_h=0,
                 batch_size=20, timeout_s=15.0):
        # The cursor is keyed on this label, so it must be stable and must not
        # collide with a webhook's.
        self.label = f"tg:{stream_label}"
        self.stream_label = stream_label
        self.streams = [stream_label]
        self.token = token
        self.chat_id = str(chat_id)
        self.min_likes = int(min_likes or 0)
        self.skip_retweets = bool(skip_retweets)
        self.skip_replies = bool(skip_replies)
        self.max_age_h = int(max_age_h or 0)
        self.batch_size = batch_size
        self.timeout_s = timeout_s


def telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


class DbTarget:
    """
    A dashboard-created, project-scoped delivery target (delivery_targets
    row). Quacks like a WebhookCfg / TelegramTarget so `pump` treats all
    three identically; the only new behavior is project_id, which
    store.tweets_after resolves at read time.
    """

    streams = None            # scoping is by project, never by stream list
    min_likes = 0
    skip_retweets = False
    skip_replies = False
    max_age_h = 0
    timeout_s = 15.0

    def __init__(self, row, secret: str = "", token: str = ""):
        self.label = f"dt:{row['target_id']}"
        self.kind = row["kind"]
        self.name = row["name"]
        self.project_id = row["project_id"]
        self.batch_size = row["batch_size"] or 50
        self.url = row["url"] or TELEGRAM_API
        self.secret = secret
        self.token = token
        self.chat_id = str(row["chat_id"] or "")


async def db_targets(store, log=print) -> list:
    """Build senders for every enabled dashboard-created target."""
    out = []
    try:
        rows = await store.delivery_targets(enabled_only=True)
    except Exception as e:
        log(f"[delivery] could not read targets: {type(e).__name__}: {e}")
        return out
    for r in rows:
        if r["kind"] == "webhook":
            secret = os.getenv(r["secret_env"] or "", "").strip()
            if not secret:
                # A webhook with no secret is refused, same rule as
                # config.toml targets: an unsigned delivery is forgeable.
                log(f"[delivery:{r['name']}] {r['secret_env']} is not set in "
                    f".env — target skipped")
                continue
            out.append(DbTarget(r, secret=secret))
        else:
            token = telegram_token()
            if not token:
                log(f"[delivery:{r['name']}] TELEGRAM_BOT_TOKEN missing — skipped")
                continue
            out.append(DbTarget(r, token=token))
    return out


async def telegram_targets(store, log=print) -> list:
    """Build a Telegram target for every stream that has it switched on."""
    token = telegram_token()
    if not token:
        return []
    out = []
    for row in store.db.execute(
            "SELECT * FROM streams WHERE tg_enabled = 1 ORDER BY label"):
        chat = (row["tg_chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        if not chat:
            # Switched on with nowhere to send is a misconfiguration, not a
            # reason to stay silent about it.
            log(f"[telegram:{row['label']}] enabled but no chat id — skipping")
            continue
        out.append(TelegramTarget(
            row["label"], token, chat,
            min_likes=row["tg_min_likes"], skip_retweets=row["tg_skip_retweets"],
            skip_replies=(row["tg_skip_replies"] if "tg_skip_replies" in row.keys() else 0),
            max_age_h=(row["tg_max_age_h"] if "tg_max_age_h" in row.keys() else 0)))
    return out


async def run(cfg, store, log=print, stop: asyncio.Event | None = None) -> None:
    """
    The delivery loop. Runs alongside the collector for as long as it does.

    Targets are rebuilt every cycle rather than once at startup, so switching
    Telegram on for a stream in the dashboard takes effect within seconds
    instead of at the next restart.

    New targets start from NOW, not from the beginning of the database — see
    store.webhook_start_here. Pointing a fresh endpoint at months of history and
    having it immediately fire all of it is a surprise nobody wants, and on
    Telegram it would be thousands of messages.
    """
    import httpx

    import alerts as _alerts

    announced = set()
    last_alert_check = 0.0

    async with httpx.AsyncClient(follow_redirects=False) as client:
        while stop is None or not stop.is_set():
            # Velocity alerts ride this loop: it already has the client, the
            # store, and the never-take-the-loop-down discipline. One check a
            # minute; tick() itself never raises.
            if time.time() - last_alert_check >= _alerts.CHECK_EVERY_S:
                last_alert_check = time.time()
                await _alerts.tick(store, client, log=log)
            targets = list(cfg.enabled_webhooks())
            try:
                targets += await telegram_targets(store, log=log)
            except Exception as e:
                log(f"[telegram] could not read settings: {type(e).__name__}: {e}")
            targets += await db_targets(store, log=log)

            for h in targets:
                if h.label not in announced:
                    announced.add(h.label)
                    cur = await store.webhook_cursor(h.label)
                    if not cur["last_ms"] and not cur["sent"]:
                        await store.webhook_start_here(h.label)
                        log(f"[{h.label}] new target — starting from now, not from "
                            f"the whole archive")
                    log(f"[{h.label}] -> {h.url}"
                        + (f" (streams: {', '.join(h.streams)})" if h.streams else ""))
                try:
                    await pump(h, store, client, log=log)
                except Exception as e:
                    # One target misbehaving must not take the loop down and
                    # stop every other target with it.
                    log(f"[{h.label}] sender error: {type(e).__name__}: {e}")
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_POLL_S)
                else:
                    await asyncio.sleep(IDLE_POLL_S)
            except asyncio.TimeoutError:
                pass
