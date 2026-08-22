"""
Content labelling — the one analysis exception (RULEBOOK §1 directive 2).

This module turns a batch of collected posts into ONE category each, using an
external model (xAI's Grok). It is the only place in this project that asks a
machine what a post *means*, and it is deliberately shaped so that fact stays
contained:

  * Pure and injectable. No DB handle, no globals, no module-level client.
    `label_batch` takes the httpx client as its first argument exactly like
    `webhook.deliver_batch(client, ...)` does, so the test suite drives it
    offline with a fake and spends nothing.
  * Never raises. Every path returns (labels, usage, error) — an outbound call
    that blows up is an error string, never an exception that kills a run
    halfway and loses the posts already paid for.
  * Never invents a category. The model's answer is checked against the
    project's own category keys; anything unrecognised becomes the catch-all.
    A label the operator never defined must not appear on a board.

The vocabulary is NOT hardcoded here. `DEFAULT_CATEGORIES` only seeds a new
project; after that the categories live in the database and are edited from the
dashboard (RULEBOOK §6: every operational switch lives there), so a second
client gets its own vocabulary without a code change.

Note on the word "label": in this project `label` has long meant a *stream*
label (streams.label, store.stream_labels_for). A post's category is also
called a label on the wire — `t.label` — because that is what it reads as in
the UI. Backend identifiers for this feature are prefixed `post_label` so the
two can never be confused at a call site.
"""

from __future__ import annotations

import json
import os

# The API. OpenAI-shaped, so the request body below is the familiar one; we
# speak it with plain httpx rather than adding the openai SDK as a dependency
# (web.py's docstring boast about zero new dependencies is still policy).
API_URL = "https://api.x.ai/v1/chat/completions"

# Defaults only. Both are settings rows in results.db and are edited from the
# dashboard — a model rename or a price change must never need a deploy.
DEFAULT_MODEL = "grok-4.6"
DEFAULT_PRICE_IN = 2.00        # USD per 1M input tokens
DEFAULT_PRICE_OUT = 6.00       # USD per 1M output tokens

# How many posts go in one request. Small enough that a failure loses little
# and the model keeps every post in view; large enough that the instruction
# block (which is resent every time) is not the bulk of what we pay for.
BATCH = 25

# Requests time out well before web.py's own 300s budget so a slow provider
# surfaces as "Grok timed out" rather than as a dashboard that just hangs.
TIMEOUT_S = 90.0

# The key that every unrecognised answer falls back to. A project may rename
# it, but it may not delete it — see store.set_post_label_category.
CATCHALL = "other"

# Bumped when the instruction block below changes in a way that would make an
# older label mean something different. Stored on every row, so a stale label
# is visible as stale rather than silently trusted.
PROMPT_VERSION = 1


# The seed vocabulary for a new project. Order is precedence: when a post could
# sit in two of these, the earlier one wins, and the prompt says so explicitly.
DEFAULT_CATEGORIES = [
    {
        "key": "hate",
        "name": "Hate Speech / Hate Content",
        "description": (
            "Contains explicit hateful, abusive, threatening, violent, "
            "dehumanising or derogatory content targeting a person or a "
            "protected group. Ordinary political criticism is NOT hate "
            "speech, however harsh — do not use this category simply because "
            "a post attacks a party, a politician, an ideology, a religion or "
            "a government."
        ),
    },
    {
        "key": "hindu_muslim",
        "name": "Hindu-Muslim",
        "description": (
            "Primarily about Hindu-Muslim relations, communal issues, "
            "religious conflict, communal incidents, religious polarisation "
            "or Hindu-Muslim narratives. If the communal angle is the main "
            "subject, this category wins."
        ),
    },
    {
        "key": "bjp_pro",
        "name": "BJP Pro",
        "description": (
            "Supports, praises, defends or positively promotes the BJP, BJP "
            "leaders, BJP governments, BJP policies, schemes or achievements."
        ),
    },
    {
        "key": "against_congress",
        "name": "Against Congress",
        "description": (
            "Criticises, attacks, opposes, mocks or negatively discusses "
            "Congress, Congress leaders, Congress governments, Congress "
            "policies or Congress-related political issues."
        ),
    },
    {
        "key": CATCHALL,
        "name": "Other / Not relevant",
        "description": (
            "Does not primarily belong to any category above. Use this "
            "freely: a post about cricket, weather, an advertisement or "
            "anything outside the categories above belongs here. Never force "
            "an unrelated post into a political category."
        ),
    },
]


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def build_prompt(categories: list) -> str:
    """
    Assemble the instruction block from the project's own categories.

    Built from the DB rows rather than written as a constant so that editing a
    category in the dashboard actually changes what the model is told. A
    category editor whose text never reaches the model would be theatre
    (RULEBOOK §4: a check that cannot fail the way the system fails is not a
    check).
    """
    lines = [
        "You are labelling social media posts for a political media-monitoring "
        "dashboard. Assign each post to exactly ONE category.",
        "",
        "CATEGORIES (in order of precedence — the earlier one wins a tie):",
    ]
    for i, c in enumerate(categories, 1):
        lines.append(f"{i}. {c['key']} — {c['name']}")
        lines.append(f"   {c['description']}")
    lines += [
        "",
        "HOW TO DECIDE:",
        "- Read the COMPLETE post, not individual keywords.",
        "- Understand its context, meaning and primary intent before deciding.",
        "- Assign only ONE category per post.",
        "- If several categories could apply, choose the one that represents "
        "the post's MAIN PURPOSE, then fall back to the precedence order above.",
        f"- If nothing fits, use '{CATCHALL}'. That is a correct answer, not a "
        "failure — never stretch a post into a category it does not belong in.",
        "- Judge the post as written. Do not infer beyond what it says.",
        "- Never change, rewrite, translate, summarise or repeat the post text "
        "back to me. Return labels only.",
        "",
        "OUTPUT:",
        'Return JSON only, of the form {"labels":[{"id":"<the id given>",'
        '"label":"<a category key>","confidence":<0.0-1.0>}]}.',
        "Include every id you were given, exactly once, using the id string "
        "verbatim. Use only the category keys listed above.",
    ]
    return "\n".join(lines)


def build_user_message(posts: list) -> str:
    """
    Render the batch. One post per block, id first.

    Ids are echoed back rather than positions being trusted: a model that drops
    or reorders an item then produces a mismatch we can see, instead of a set
    of labels quietly attached to the wrong posts.
    """
    out = []
    for p in posts:
        text = (p.get("text") or "").strip()
        if len(text) > 1500:      # a post this long is a thread dump; the tail
            text = text[:1500]    # never decides the category.
        out.append(f"ID: {p['id']}\nAUTHOR: @{p.get('author') or 'unknown'}\n"
                   f"POST: {text or '(no text)'}")
    return "\n\n---\n\n".join(out)


def response_schema(valid_keys: list) -> dict:
    """The structured-output schema, so the model cannot answer in prose."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "post_labels",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["labels"],
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "label", "confidence"],
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string",
                                          "enum": list(valid_keys)},
                                "confidence": {"type": "number"},
                            },
                        },
                    }
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def parse_response(text: str, valid_keys, wanted_ids) -> dict:
    """
    Turn the model's reply into {post_id: (label, confidence)}.

    Tolerant on purpose. Structured output should make this trivial, but a
    provider that ignores response_format, wraps the JSON in a code fence, or
    returns one stray key must degrade to a usable answer rather than lose the
    whole batch. An id we did not ask about is dropped; a key we do not know
    becomes the catch-all; a post the model skipped is simply absent, and the
    caller records it as failed rather than guessing.
    """
    valid = set(valid_keys)
    wanted = {str(i) for i in wanted_ids}
    raw = (text or "").strip()
    if not raw:
        return {}

    # Strip a ```json fence if one arrived.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
        raw = raw.strip()

    data = None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # Last resort: the first {...} or [...] span in the reply.
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = raw.find(opener), raw.rfind(closer)
            if i != -1 and j > i:
                try:
                    data = json.loads(raw[i:j + 1])
                    break
                except (TypeError, ValueError):
                    continue
    if data is None:
        return {}

    if isinstance(data, dict):
        items = data.get("labels")
        if items is None:
            # Some replies come back as {"<id>": "<label>"}.
            items = [{"id": k, "label": v} for k, v in data.items()
                     if isinstance(v, str)]
    else:
        items = data
    if not isinstance(items, list):
        return {}

    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        if pid not in wanted:
            continue
        key = str(it.get("label") or "").strip()
        if key not in valid:
            key = CATCHALL if CATCHALL in valid else (
                sorted(valid)[0] if valid else CATCHALL)
        try:
            conf = float(it.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        if conf is not None and not 0.0 <= conf <= 1.0:
            conf = None
        out[pid] = (key, conf)
    return out


def cost_usd(in_tokens: int, out_tokens: int,
             price_in: float = DEFAULT_PRICE_IN,
             price_out: float = DEFAULT_PRICE_OUT) -> float:
    """What a call cost, in dollars. Prices are per million tokens."""
    return (int(in_tokens or 0) * float(price_in)
            + int(out_tokens or 0) * float(price_out)) / 1_000_000.0


def estimate_tokens(posts: list, prompt: str) -> int:
    """
    A rough input-token estimate for the pre-flight cap check.

    Deliberately crude (~4 chars per token) and deliberately generous: this
    number only ever decides whether to REFUSE a run, so erring high refuses a
    borderline run instead of overshooting the operator's cap.
    """
    chars = len(prompt) + sum(len(p.get("text") or "") + 60 for p in posts)
    return int(chars / 3.5) + 200


def xai_api_key() -> str:
    """The Grok key. .env only — never the database, never the UI."""
    return os.getenv("XAI_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------

async def label_batch(client, api_key: str, model: str,
                      posts: list, categories: list) -> tuple[dict, dict, str]:
    """
    Label one batch. Returns (labels, usage, error) and never raises.

    `client` is an httpx.AsyncClient supplied by the caller — the same
    dependency-injected shape as webhook.deliver_batch, which is what lets the
    offline suite exercise every branch here without a key or a socket.

    `posts` is [{"id": str, "text": str, "author": str}]. `labels` comes back
    as {id: (label_key, confidence|None)}, holding only the posts the model
    actually answered for: a partial answer must cost the caller only the posts
    it missed, not the batch.
    """
    if not posts:
        return {}, {}, ""
    if not api_key:
        return {}, {}, ("no XAI_API_KEY set — add it to .env and restart, "
                        "the dashboard never stores it")

    keys = [c["key"] for c in categories]
    system = build_prompt(categories)
    body = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": build_user_message(posts)},
        ],
        # Labelling wants the same answer twice for the same post, so that a
        # re-run is a correction and not a coin flip.
        "temperature": 0,
        "response_format": response_schema(keys),
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}

    try:
        rep = await client.post(API_URL, json=body, headers=headers,
                                timeout=TIMEOUT_S)
    except Exception as e:
        return {}, {}, f"{type(e).__name__}: {e}"

    if rep.status_code == 400:
        # Most likely this deployment does not accept response_format. Say so
        # once and retry in plain JSON mode rather than failing the whole run
        # over a feature we only wanted for tidiness.
        body.pop("response_format", None)
        try:
            rep = await client.post(API_URL, json=body, headers=headers,
                                    timeout=TIMEOUT_S)
        except Exception as e:
            return {}, {}, f"{type(e).__name__}: {e}"

    if not 200 <= rep.status_code < 300:
        try:
            detail = (rep.text or "")[:200]
        except Exception:
            detail = ""
        # The body is included on purpose: "HTTP 429" on its own sends you to
        # read someone else's logs when it probably just told you what to fix.
        return {}, {}, f"HTTP {rep.status_code}: {detail}"

    try:
        data = rep.json()
    except Exception as e:
        return {}, {}, f"Grok returned something that is not JSON: {e}"

    usage = data.get("usage") or {}
    usage = {"in": int(usage.get("prompt_tokens") or 0),
             "out": int(usage.get("completion_tokens") or 0)}

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}, usage, "Grok returned no message content"

    labels = parse_response(content, keys, [p["id"] for p in posts])
    if not labels:
        return {}, usage, "Grok's reply held no usable labels"
    return labels, usage, ""


def chunk(posts: list, size: int = BATCH):
    """Split a work list into request-sized batches."""
    size = max(1, int(size))
    for i in range(0, len(posts), size):
        yield posts[i:i + size]
