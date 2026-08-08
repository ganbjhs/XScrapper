"""
proxy_pool.py — residential-IP rotation + a hard monthly bandwidth cap.

Facebook collection is bandwidth-bound, not IP-bound (80M IPs but ~1 GB/month
on the Webshare pool). So this module's real job is NOT "get an IP" — it is
"never let Facebook fetching blow the month's byte budget." Every response's
bytes are counted and persisted; once the cap is reached, fetch() refuses
rather than silently overrunning and going dark mid-month.

Standalone on purpose: nothing in the live X/Instagram system imports this yet.
It is the first brick of the Facebook module (see FACEBOOK_PLAN.md).

Webshare specifics (from the account):
  gateway  : p.webshare.io:80
  auth     : <user>-<mode>:<pass>  embedded in the proxy URL
  rotate   : username suffix '-rotate'      -> a fresh IP every request
  sticky   : username suffix '-<sessionId>' -> one pinned IP per id
  targeting: extra '-country-XX' style tokens fold into the username

Credentials come from the environment, never from code or git:
  WEBSHARE_USER, WEBSHARE_PASS, WEBSHARE_GATEWAY (default p.webshare.io:80)
"""

import os
import sqlite3
import time


class BandwidthExceeded(RuntimeError):
    """Raised by fetch() when the month's byte budget is already spent."""


def _month_key(ts: float) -> str:
    """'2026-08' for a unix timestamp — the bucket the cap resets on."""
    t = time.gmtime(ts)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def build_proxy_url(user: str, password: str, gateway: str,
                    session: str | None = None,
                    country: str | None = None) -> str:
    """
    The proxy URL for one fetch.

    session=None  -> '-rotate'    : a new IP every request (default, for polling)
    session='abc' -> '-abc'       : a pinned IP for that id (for a login flow)
    country='in'  -> '-country-in': Webshare folds targeting into the username

    Kept pure so the suffix logic is unit-tested without a network.
    """
    suffix = f"-{session}" if session else "-rotate"
    if country:
        suffix += f"-country-{country.lower()}"
    return f"http://{user}{suffix}:{password}@{gateway}"


class ProxyPool:
    def __init__(self, meter_db: str, monthly_cap_bytes: int = 1_000_000_000,
                 user: str = "", password: str = "", gateway: str = ""):
        self.user = user or os.getenv("WEBSHARE_USER", "")
        self.password = password or os.getenv("WEBSHARE_PASS", "")
        self.gateway = gateway or os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
        self.cap = int(monthly_cap_bytes)
        self.db = sqlite3.connect(meter_db)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS bandwidth ("
            "  month TEXT PRIMARY KEY, bytes INTEGER NOT NULL DEFAULT 0, "
            "  fetches INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()

    # -- the meter -----------------------------------------------------------

    def used(self, now: float | None = None) -> int:
        row = self.db.execute("SELECT bytes FROM bandwidth WHERE month = ?",
                              (_month_key(now or time.time()),)).fetchone()
        return row[0] if row else 0

    def remaining(self, now: float | None = None) -> int:
        return max(0, self.cap - self.used(now))

    def record(self, nbytes: int, now: float | None = None) -> None:
        m = _month_key(now or time.time())
        self.db.execute(
            "INSERT INTO bandwidth(month, bytes, fetches) VALUES(?,?,1) "
            "ON CONFLICT(month) DO UPDATE SET bytes = bytes + excluded.bytes, "
            "  fetches = fetches + 1", (m, int(nbytes)))
        self.db.commit()

    def stats(self, now: float | None = None) -> dict:
        m = _month_key(now or time.time())
        row = self.db.execute(
            "SELECT bytes, fetches FROM bandwidth WHERE month = ?", (m,)).fetchone()
        used = row[0] if row else 0
        return {"month": m, "used_bytes": used, "fetches": row[1] if row else 0,
                "cap_bytes": self.cap, "remaining_bytes": max(0, self.cap - used),
                "pct": round(100 * used / self.cap, 1) if self.cap else 0}

    # -- the fetch ------------------------------------------------------------

    def fetch(self, url: str, *, session=None, country=None,
              timeout: float = 20.0, headers: dict | None = None):
        """
        GET `url` through a residential IP, counting bytes against the cap.

        Refuses BEFORE spending a request if the budget is already gone, and
        records the real response size after. httpx is imported lazily so this
        module loads (and its pure logic tests) with no network stack present.
        Runs on the server — not reachable from the build sandbox.
        """
        if self.remaining() <= 0:
            raise BandwidthExceeded(
                f"monthly cap {self.cap} bytes reached for {_month_key(time.time())}")
        import httpx

        proxy = build_proxy_url(self.user, self.password, self.gateway,
                                session=session, country=country)
        # mbasic is tiny; a lean UA + gzip keeps each fetch in the tens of KB.
        hdrs = {"User-Agent": "Mozilla/5.0 (Android 10; Mobile) mbasic",
                "Accept-Encoding": "gzip, deflate", **(headers or {})}
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True) as c:
            rep = c.get(url, headers=hdrs)
        body = rep.content or b""
        self.record(len(body))
        return rep.status_code, body
