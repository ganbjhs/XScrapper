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
