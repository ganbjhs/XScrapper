# Receiving push deliveries from the Collector — integration note

*Hand this to the Watch-Tower developer. It is everything their side needs.*

The Collector can PUSH collected posts to you seconds after they are stored,
instead of (or alongside) you pulling via API key. You build ONE endpoint;
we do the rest.

## What you provide us

1. **An HTTPS URL** that accepts `POST` with a JSON body
   (e.g. `https://app.watch-tower.in/hooks/tweets`).
2. **A shared secret** — a long random string, agreed once, stored securely
   on both sides. Never sent in the payload; used only for signing.

## What arrives

`POST` with `Content-Type: application/json`. Body:

```json
{
  "version": 1,
  "webhook": "watch-tower",
  "sent_at": 1785991902,
  "count": 2,
  "tweets": [
    {
      "tweet_id": "1953001234567890123",
      "url": "https://x.com/SomeHandle/status/1953001234567890123",
      "created_at": "2026-08-06T12:41:02+00:00",
      "collected_at": "2026-08-06T12:41:08+00:00",
      "lag_ms": 6200,
      "text": "…post text…",
      "lang": "hi",
      "author_username": "SomeHandle",
      "author_display_name": "Some Handle",
      "author_id": "123456",
      "author_followers": 759900,
      "reply_count": 0, "retweet_count": 0, "like_count": 2,
      "quote_count": 0, "view_count": 946, "bookmark_count": 0,
      "is_retweet": false, "is_reply": false, "is_quote": false,
      "in_reply_to": null, "conversation_id": "1953001234567890123",
      "hashtags": ["Article370Abrogation"],
      "mentions": [], "urls": [],
      "media_urls": ["https://video.twimg.com/....mp4"],
      "media": [
        {"type": "video",
         "url": "https://video.twimg.com/....mp4",
         "thumb": "https://pbs.twimg.com/....jpg",
         "duration": 170.5}
      ],
      "streams": ["wl:1:0"]
    }
  ]
}
```

Notes on the shape:

- **`tweet_id` is a STRING.** Ids exceed JavaScript's safe-integer range;
  parsing them as numbers silently corrupts them.
- **`media`** is the structured view — `type` is `photo | video | gif`;
  videos carry both the mp4 (`url`) and a lightweight still (`thumb`).
  Render the thumb, lazy-load the mp4. All URLs are X's own CDN.
- `media_urls` is a legacy flat list; prefer `media`.

## Headers, and how to verify a delivery

| Header | Meaning |
|---|---|
| `X-XS-Timestamp` | Unix seconds when we signed the request |
| `X-XS-Signature` | `sha256=<hex>` — HMAC-SHA256 with the shared secret over `"<timestamp>.<raw body bytes>"` |
| `X-XS-Webhook` | The target's label on our side |
| `X-XS-Count` | Number of tweets in this batch |

Verification (pseudocode — any language):

```
if abs(now - int(X-XS-Timestamp)) > 300: reject          # replay guard
expected = "sha256=" + hex(hmac_sha256(secret, timestamp + "." + raw_body))
if not constant_time_equal(expected, X-XS-Signature): reject
```

Sign over the RAW bytes you received, before any JSON parsing/re-encoding.

## The three rules of the contract

1. **Answer `2xx` quickly** (just enqueue and return; process later).
   Anything else, or a timeout, means "not delivered" and we retry with
   backoff.
2. **De-duplicate on `tweet_id`.** Delivery is at-least-once: our cursor
   only advances after your `2xx`, so if you answer 200 and crash before
   committing, the same batch comes again. Skipping seen ids makes this
   harmless.
3. **You never lose data by going down.** Our position is a durable cursor —
   when your endpoint comes back after an outage, everything you missed is
   delivered automatically, in order, oldest first. No replay requests
   needed.

## Scope

Each webhook target on our side is bound to one project — you receive only
that project's posts. If you want separate ingestion per project, give us
one URL (or one path) per project and we create one target for each.

## Ordering & volume

Batches are ordered by collection time, oldest first, up to 50 tweets per
POST (configurable). Sustained volume follows the watchlists' activity;
bursts arrive as consecutive batches.

## Test procedure (10 minutes, together)

1. You stand up the endpoint with the secret; give us the URL.
2. We create the target in our dashboard; it starts from NOW (no historical
   flood).
3. We trigger one real delivery; you confirm: signature verifies, tweet
   parses, media renders from `thumb`.
4. You return a 500 on purpose once; watch it retry and recover — that's the
   whole failure story, tested.
