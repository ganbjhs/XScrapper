"""
alerts.py — "this is moving faster than usual", as a Telegram ping.

Counts only, on data already collected. No sentiment, no topics, no AI —
analysis belongs to Watch-Tower (design invariant #7); this is plumbing over
tweet_hits timestamps. The rule:

    fire when   posts(last hour) >= min_posts
          and   posts(last hour) >= threshold × usual hourly pace

where the usual pace is the trailing 24h ending an hour ago (see
store.scope_velocity for why the surge is excluded from its own yardstick).
A scope with no history yet fires on min_posts alone — a brand-new watchlist
doing 40 posts in its first hour is exactly the thing to hear about.

Runs inside the delivery loop (webhook.run), which already lives alongside
the collector, already has an HTTP client, and already knows how not to let
one bad send take anything else down. Checked once a minute; a rule that
fires goes quiet for COOLDOWN_MS so a three-hour surge pings a few times,
not sixty.
"""

import os
import time

CHECK_EVERY_S = 60
COOLDOWN_MS = 30 * 60 * 1000


def decide(last_hour: int, baseline_per_hour: float,
           threshold: float, min_posts: int) -> tuple:
    """
    (should_fire, ratio). Pure, so the test suite can walk every branch.
    ratio is None when there is no history to compare against.
    """
    if last_hour < max(1, int(min_posts)):
        return False, None
    if baseline_per_hour <= 0:
        return True, None
    ratio = last_hour / baseline_per_hour
    return ratio >= float(threshold), ratio


def format_message(alert: dict, last_hour: int, ratio) -> str:
    scope = alert.get("watchlist_name") or f"project {alert.get('project_name', '')}".strip()
    pace = (f"{ratio:.1f}× its usual pace" if ratio is not None
            else "with no usual pace to compare yet")
    return (f"⚡ {scope}: {last_hour} posts in the last hour — {pace}. "
            f"Open the live feed to see what is moving.")


async def tick(store, client, log=print, send=None, now_ms=None) -> int:
    """
    Evaluate every enabled rule once. Returns how many fired.

    `send` defaults to the Telegram sender; tests inject a recorder instead.
    Never raises: an alert that cannot be evaluated or delivered is logged
    and skipped, exactly like a failing webhook target.
    """
    import webhook as wh

    token = wh.telegram_token()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    fired = 0
    try:
        rules = await store.alerts(enabled_only=True)
    except Exception as e:
        log(f"[alerts] could not read rules: {type(e).__name__}: {e}")
        return 0

    for a in rules:
        try:
            if now - (a.get("last_fired_ms") or 0) < COOLDOWN_MS:
                continue
            streams = await store.alert_scope_streams(a)
            last_hour, baseline = await store.scope_velocity(streams, now)
            fire, ratio = decide(last_hour, baseline, a["threshold"], a["min_posts"])
            if not fire:
                continue
            chat = (a.get("tg_chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
            msg = format_message(a, last_hour, ratio)
            if send is not None:
                ok, err = await send(a, msg)
            elif token and chat:
                ok, err = await wh.tg_send(client, token, chat, msg)
            else:
                log(f"[alerts] rule {a['alert_id']} fired but has nowhere to "
                    f"send — set TELEGRAM_BOT_TOKEN and a chat id")
                continue
            if ok:
                await store.alert_fired(a["alert_id"], now)
                fired += 1
                log(f"[alerts] fired: {msg}")
            else:
                log(f"[alerts] rule {a['alert_id']} could not send: {err}")
        except Exception as e:
            log(f"[alerts] rule {a.get('alert_id')}: {type(e).__name__}: {e}")
    return fired
