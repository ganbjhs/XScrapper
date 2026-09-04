"""
ig_human.py — make the Instagram collector move like a person, not a cron job.

Instagram's automation detection watches CADENCE and RHYTHM as much as it
watches requests: a poller that fires every 120.0 s, all night, forever, at a
machine's steady tick is a bright flag even when every individual request is
valid. This module is the rhythm layer. It answers four questions the loop
asks each cycle, and nothing else — it makes NO network calls and holds NO
Instagram state, so it is fully testable offline (every function takes `now`
and an injectable rng).

The four questions:
  1. active_now(now)      — is it a plausible hour for THIS account to be
                            scrolling at all? People sleep. A feed that never
                            goes quiet overnight is not a person's.
     session_now(acct)    — and within that day, is the phone IN HAND right
                            now? People use the app in bursts (day_plan: two
                            to four sessions of random length at random
                            times, plus a glance or two), not evenly from
                            breakfast to midnight. Outside the plan: zero
                            requests.
  2. request_gap(rng)     — how long to pause between two page requests, drawn
                            from a human-shaped (log-normal) distribution:
                            mostly a few seconds, sometimes a long glance-away.
  3. source_gap(rng)      — the longer pause when switching from one watched
                            source to another (a person changes what they look
                            at; they don't machine-gun ten profiles in a row).
  4. budget/backoff       — a per-account DAILY request ceiling and a warm-up
                            ramp for young accounts, so a fresh burner is never
                            slammed at full volume on day one.

Everything is a knob with a safe default; the dashboard/env can widen or
tighten it, but the DEFAULTS are already conservative — the point of this file
is that doing nothing is already gentle.
"""

import hashlib
import math
import os
import random
import time

# ---- knobs (env-overridable; the defaults are deliberately gentle) ----

def _envf(key, default):
    try:
        return float(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default

def _envi(key, default):
    try:
        return int(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default


# Active hours in the account's LOCAL time (24h clock). Outside this window the
# collector mostly sleeps — with a small chance of a "check my phone in the
# night" poll, because a human occasionally does, and perfect silence to the
# exact minute is itself a pattern.
ACTIVE_START_H = _envi("IG_ACTIVE_START_H", 7)     # 07:00
ACTIVE_END_H = _envi("IG_ACTIVE_END_H", 24)        # up to 24:00 (midnight)
NIGHT_POLL_CHANCE = _envf("IG_NIGHT_POLL_CHANCE", 0.06)

# Per-request gap: log-normal so the bulk sits low with a fat tail of longer
# pauses. median ~= exp(mu). Clamped to [min,max].
REQ_GAP_MU = _envf("IG_REQ_GAP_MU", 2.3)           # exp(2.3) ~= 10 s median
REQ_GAP_SIGMA = _envf("IG_REQ_GAP_SIGMA", 0.6)
REQ_GAP_MIN = _envf("IG_REQ_GAP_MIN", 3.0)
REQ_GAP_MAX = _envf("IG_REQ_GAP_MAX", 90.0)

# Between-source gap: a longer, human "switch what I'm looking at" pause.
SRC_GAP_MIN = _envf("IG_SRC_GAP_MIN", 20.0)
SRC_GAP_MAX = _envf("IG_SRC_GAP_MAX", 150.0)

# Occasional long break mid-run (put the phone down), and how long.
LONG_BREAK_CHANCE = _envf("IG_LONG_BREAK_CHANCE", 0.10)
LONG_BREAK_MIN = _envf("IG_LONG_BREAK_MIN", 180.0)   # 3 min
LONG_BREAK_MAX = _envf("IG_LONG_BREAK_MAX", 900.0)   # 15 min

# Daily request ceiling per account once warm, and the warm-up ramp for a
# freshly-onboarded account (age in days -> fraction of the full budget).
DAILY_BUDGET = _envi("IG_DAILY_BUDGET", 300)
WARMUP_DAYS = _envi("IG_WARMUP_DAYS", 7)
WARMUP_FLOOR = _envf("IG_WARMUP_FLOOR", 0.15)        # day 0 gets 15% of budget


def _lognormal(rng, mu, sigma, lo, hi):
    v = math.exp(rng.normalvariate(mu, sigma))
    return max(lo, min(hi, v))


def active_now(now=None, *, start_h=None, end_h=None, tz_offset_s=0, rng=None):
    """
    Is this a plausible waking hour for the account? `now` is epoch seconds;
    `tz_offset_s` shifts UTC to the account's local time (e.g. +5.5h for IST =
    19800). Outside the window returns True only NIGHT_POLL_CHANCE of the time,
    so overnight the feed goes mostly — not perfectly — quiet.
    """
    now = time.time() if now is None else now
    rng = rng or random
    start_h = ACTIVE_START_H if start_h is None else start_h
    end_h = ACTIVE_END_H if end_h is None else end_h
    local_h = ((now + tz_offset_s) % 86400) / 3600.0
    awake = start_h <= local_h < end_h
    if awake:
        return True
    return rng.random() < NIGHT_POLL_CHANCE


def request_gap(rng=None):
    """Seconds to wait before the next PAGE request — human-shaped jitter."""
    rng = rng or random
    return _lognormal(rng, REQ_GAP_MU, REQ_GAP_SIGMA, REQ_GAP_MIN, REQ_GAP_MAX)


def source_gap(rng=None):
    """Seconds to wait when moving to the NEXT source (longer than a page gap)."""
    rng = rng or random
    return rng.uniform(SRC_GAP_MIN, SRC_GAP_MAX)


def maybe_long_break(rng=None):
    """Return break-seconds if it's time to 'put the phone down', else 0."""
    rng = rng or random
    if rng.random() < LONG_BREAK_CHANCE:
        return rng.uniform(LONG_BREAK_MIN, LONG_BREAK_MAX)
    return 0.0


def daily_budget(account_age_days=None, *, full=None):
    """
    How many requests this account may make today. A young account ramps from
    WARMUP_FLOOR of the full budget up to 100% over WARMUP_DAYS — a brand-new
    burner behaving at full tilt on day one is exactly the footprint that gets
    it flagged.
    """
    full = DAILY_BUDGET if full is None else full
    if account_age_days is None or account_age_days >= WARMUP_DAYS:
        return full
    age = max(0, account_age_days)
    frac = WARMUP_FLOOR + (1.0 - WARMUP_FLOOR) * (age / max(1, WARMUP_DAYS))
    return max(1, int(full * frac))


def next_interval(base_s, rng=None):
    """
    Jitter a base loop interval by ±35% so the between-cycle rhythm is never a
    fixed clock. Used by the collect loop around whatever cadence the dashboard
    set — the cadence is the intent, this keeps it from being robotic.
    """
    rng = rng or random
    return max(30.0, base_s * rng.uniform(0.65, 1.35))


# ---- sessions: phone-time windows, not an all-day smear (2026-09-04) ----
#
# active_now says WHEN THE DAY IS. The plan below says when in that day the
# phone is actually in hand: a person opens Instagram in bursts — ten minutes
# over breakfast, half an hour at lunch, an hour in the evening — and the
# app is closed in between. A collector that is "on" from 07:00 to midnight
# with pauses is a thin, even smear across seventeen hours, and no person's
# usage looks like that.
#
# The plan is drawn ONCE per account per local day from a seed, so a restart
# replays the same plan and the same account keeps the same plan all day; a
# different account, or the same account tomorrow, gets a different one. It
# is pure arithmetic — no clock read, no network — and fully testable.

SESSIONS_MIN = _envi("IG_SESSIONS_MIN", 2)           # sessions per day
SESSIONS_MAX = _envi("IG_SESSIONS_MAX", 4)
SESSION_MIN_S = _envi("IG_SESSION_MIN_S", 20 * 60)   # 20 min
SESSION_MAX_S = _envi("IG_SESSION_MAX_S", 120 * 60)  # 2 h
GLANCES_MAX = _envi("IG_GLANCES_MAX", 2)             # short "checked my phone" looks
GLANCE_MIN_S = _envi("IG_GLANCE_MIN_S", 2 * 60)
GLANCE_MAX_S = _envi("IG_GLANCE_MAX_S", 6 * 60)


def local_day(now=None, tz_offset_s=0) -> int:
    """The account's LOCAL calendar day as an integer (days since the epoch)."""
    now = time.time() if now is None else now
    return int((now + tz_offset_s) // 86400)


def _plan_rng(account, day):
    # sha256 of a string, then random.Random(str): deterministic across
    # processes and restarts (a str seed never goes through hash()).
    seed = hashlib.sha256(
        f"ig-day-plan:{(account or '').lower()}:{int(day)}".encode()).hexdigest()
    return random.Random(seed)


def day_plan(account, now=None, *, tz_offset_s=0, start_h=None, end_h=None,
             day=None) -> list:
    """
    The phone-time plan for ONE account on ONE local day: a sorted list of
    (start_epoch, end_epoch, kind), kind 'session' or 'glance'.

    Sessions: SESSIONS_MIN..SESSIONS_MAX of them, each SESSION_MIN_S..
    SESSION_MAX_S long, spread through the active hours — the window is cut
    into one slot per session and each session lands at a random point in
    its slot, so the morning, the afternoon and the evening each get their
    turn and two sessions can never overlap. Glances: up to GLANCES_MAX
    short looks at a random time of the 24-hour day; one that falls outside
    the active hours is kept only NIGHT_POLL_CHANCE of the time, so the
    night is quiet but not perfectly, to-the-minute quiet.

    `day` (a local_day integer) selects a day other than the one `now` is
    in — session_now uses it to look at tomorrow's first window.
    """
    now = time.time() if now is None else now
    start_h = ACTIVE_START_H if start_h is None else start_h
    end_h = ACTIVE_END_H if end_h is None else end_h
    day = local_day(now, tz_offset_s) if day is None else int(day)
    rng = _plan_rng(account, day)
    midnight = day * 86400 - tz_offset_s              # epoch of local 00:00
    lo, hi = start_h * 3600.0, end_h * 3600.0          # seconds into the local day
    span = max(0.0, hi - lo)
    out = []
    n = rng.randint(max(1, SESSIONS_MIN), max(max(1, SESSIONS_MIN), SESSIONS_MAX))
    if span > 0:
        slot = span / n
        for i in range(n):
            s_lo, s_hi = lo + i * slot, lo + (i + 1) * slot
            length = min(rng.uniform(SESSION_MIN_S, SESSION_MAX_S), slot * 0.9)
            start = rng.uniform(s_lo, max(s_lo, s_hi - length))
            out.append((midnight + start, midnight + start + length, "session"))
    for _ in range(rng.randint(0, max(0, GLANCES_MAX))):
        at = rng.uniform(0.0, 86400.0)
        if not (lo <= at < hi) and rng.random() >= NIGHT_POLL_CHANCE:
            continue
        a = midnight + at
        b = a + rng.uniform(GLANCE_MIN_S, GLANCE_MAX_S)
        if any(a < e and b > s for s, e, _ in out):
            continue
        out.append((a, b, "glance"))
    out.sort()
    return out


def session_now(account, now=None, *, tz_offset_s=0, start_h=None, end_h=None):
    """
    (in_hand, seconds_until_change): is the phone in hand right now, and how
    long until that changes — the end of the current window, or the start of
    the next one (today's, else tomorrow's first).
    """
    now = time.time() if now is None else now
    kw = dict(tz_offset_s=tz_offset_s, start_h=start_h, end_h=end_h)
    day = local_day(now, tz_offset_s)
    for s, e, _ in day_plan(account, now, day=day, **kw):
        if s <= now < e:
            return True, e - now
        if now < s:
            return False, s - now
    for s, e, _ in day_plan(account, now, day=day + 1, **kw):
        return False, s - now
    # A plan with no windows at all (start_h == end_h): look again tomorrow.
    return False, 86400.0 - ((now + tz_offset_s) % 86400.0)


def planned_seconds(account, now=None, *, tz_offset_s=0, start_h=None,
                    end_h=None) -> float:
    """Total phone-time in today's plan — the denominator that paces the
    daily budget across the day instead of into its first hour."""
    return sum(e - s for s, e, _ in day_plan(
        account, now, tz_offset_s=tz_offset_s, start_h=start_h, end_h=end_h))


def visit_gap(budget, planned_s, rng=None) -> float:
    """
    Seconds between two source VISITS inside a session (the trickle): the
    human switch gap plus an occasional break, floored so that the day's
    budget lasts the day's plan. With 300 requests and three hours of
    planned phone-time the floor is 36 s — a visit every minute or two,
    all session long, instead of ten profiles in a row and then silence.
    """
    gap = source_gap(rng) + maybe_long_break(rng)
    try:
        floor = float(planned_s) / float(budget) if budget and planned_s else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        floor = 0.0
    return max(gap, floor)


class DayCounter:
    """
    Tracks requests spent 'today' (UTC day) per account, so the loop can stop
    an account once its daily budget is gone and resume tomorrow. In-memory:
    the loop is long-lived, and a restart resetting the count fails SAFE-ish
    (a restart is rare and the per-request/-source gaps still throttle).
    """
    def __init__(self):
        self._day = None
        self._spent = {}

    def _roll(self, now):
        day = int(now // 86400)
        if day != self._day:
            self._day, self._spent = day, {}

    def spend(self, account, n=1, now=None):
        now = time.time() if now is None else now
        self._roll(now)
        self._spent[account] = self._spent.get(account, 0) + n

    def spent(self, account, now=None):
        now = time.time() if now is None else now
        self._roll(now)
        return self._spent.get(account, 0)

    def remaining(self, account, budget, now=None):
        return max(0, budget - self.spent(account, now))
