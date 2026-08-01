#!/usr/bin/env python3
"""
ig_probe.py — find out whether Instagram's official API is good enough,
before writing a single line of collector.

This answers four questions, and they are the four that decide whether the
official route is viable at all:

  1. Can we see this handle?    business_discovery only reaches PUBLIC
                                Business/Creator accounts. Personal accounts
                                are invisible to it. If your targets are
                                personal, the official route is over before it
                                starts.
  2. How much do we get?        posts returned per call, and how far back.
  3. How fresh is it?           the gap between a post going up and the API
                                admitting it exists. This project's whole
                                metric is lag, so this is the number that
                                decides everything.
  4. Where is the ceiling?      how many calls before Meta says no.

Nothing here touches the collector, the databases or any X account. It only
reads from Meta's API with a token you supply.

    export IG_TOKEN='EAA...'                    # from the Graph API Explorer
    python3 tools/ig_probe.py --targets narendramodi,pmoindia
    python3 tools/ig_probe.py --targets narendramodi --freshness
    python3 tools/ig_probe.py --targets narendramodi --find-ceiling

NOT VERIFIED AGAINST THE LIVE API. It is written from Meta's documented
behaviour; nobody has run it with a real token yet. Treat a clean run as the
first piece of evidence, not as confirmation.
"""

import argparse
import os
import sys
import time

# httpx rather than urllib, and not for style: python.org's macOS build ships
# without a usable CA bundle until you run "Install Certificates.command", so
# urllib fails every HTTPS call with CERTIFICATE_VERIFY_FAILED. httpx carries
# its own trust store, and it is already a dependency here via twscrape.
import httpx

GRAPH = "https://graph.facebook.com/v21.0"

# What we ask for about each post. Deliberately close to what store.py keeps for
# a tweet, so the shapes can be compared rather than guessed at later.
MEDIA_FIELDS = ("id,caption,like_count,comments_count,timestamp,permalink,"
                "media_type,media_url,thumbnail_url")


def call(path: str, params: dict, token: str) -> dict:
    """
    One Graph API request. Returns the parsed body, error or not.

    Never raises on an HTTP error, because Meta puts the useful part in the
    BODY of a 4xx rather than the status line — "(#100) ... does not exist"
    and "rate limit reached" both arrive as plain 400s. Raising would throw
    away the only thing worth reading.
    """
    try:
        rep = httpx.get(f"{GRAPH}/{path.lstrip('/')}",
                        params={**params, "access_token": token}, timeout=30)
        return rep.json()
    except Exception as e:
        return {"error": {"message": f"{type(e).__name__}: {e}"}}


def my_ig_account(token: str) -> tuple:
    """
    Find the Instagram account this token can act as.

    business_discovery is phrased as "this account looking up that account", so
    every call needs YOUR professional account's id as the subject — you cannot
    query a handle in the abstract.
    """
    pages = call("me/accounts", {"fields": "name,instagram_business_account"}, token)
    if "error" in pages:
        return None, None, pages["error"].get("message", "unknown error")
    for page in pages.get("data", []):
        iba = page.get("instagram_business_account")
        if iba:
            return iba["id"], page.get("name", "?"), ""
    return None, None, (
        "This token can see no Facebook Page with an Instagram professional "
        "account attached.\n"
        "     Fix: convert the Instagram account to Business or Creator in the "
        "app, link it to a Facebook Page, then make a new token.")


def discover(ig_id: str, handle: str, token: str, limit: int = 25) -> dict:
    """Ask for one target's profile and recent posts. One API call."""
    field = (f"business_discovery.username({handle})"
             f"{{followers_count,media_count,media.limit({limit})"
             f"{{{MEDIA_FIELDS}}}}}")
    return call(ig_id, {"fields": field}, token)


def _age(ts: str) -> str:
    """'2026-08-01T09:12:00+0000' -> a human gap from now."""
    try:
        from datetime import datetime, timezone
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
        s = (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:
        return "?"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return f"{int(s)}s ago"


def probe(ig_id: str, handles: list, token: str, limit: int) -> int:
    reachable = 0
    for h in handles:
        d = discover(ig_id, h, token, limit)
        if "error" in d:
            msg = d["error"].get("message", "?")
            print(f"  UNREACHABLE  @{h}")
            print(f"               {msg}")
            # This specific failure is the important one: it is not a bug, it
            # is the answer to question 1.
            if "does not exist" in msg or "cannot be found" in msg:
                print("               -> almost certainly a personal account, "
                      "or private. business_discovery cannot see either.")
            continue

        bd = d.get("business_discovery", {})
        media = (bd.get("media") or {}).get("data", [])
        reachable += 1
        print(f"  OK           @{h}")
        print(f"               {bd.get('followers_count', '?'):,} followers, "
              f"{bd.get('media_count', '?')} posts total")
        print(f"               this call returned {len(media)} posts")
        if media:
            print(f"               newest: {_age(media[0].get('timestamp',''))}"
                  f"  oldest in this batch: {_age(media[-1].get('timestamp',''))}")
            first = media[0]
            print(f"               fields present: "
                  f"{', '.join(k for k in first if first[k] not in (None, ''))}")
    return reachable


def freshness(ig_id: str, handle: str, token: str, minutes: int) -> None:
    """
    Watch one handle and time how long a new post takes to appear.

    The honest way to run this: post something on the account yourself, then
    start this. You control the moment it went up, so the gap it reports is the
    real end-to-end delay rather than a guess.
    """
    print(f"Watching @{handle} for {minutes} minutes.")
    print("Post something on that account now — this reports how long it takes "
          "to show up.\n")
    seen, started, calls = set(), time.time(), 0
    d = discover(ig_id, handle, token, 5)
    for m in (d.get("business_discovery", {}).get("media") or {}).get("data", []):
        seen.add(m["id"])
    calls += 1
    print(f"  baseline: {len(seen)} posts known")

    while time.time() - started < minutes * 60:
        time.sleep(30)
        d = discover(ig_id, handle, token, 5)
        calls += 1
        if "error" in d:
            print(f"  {int(time.time()-started):4d}s  error: "
                  f"{d['error'].get('message','?')}")
            continue
        for m in (d.get("business_discovery", {}).get("media") or {}).get("data", []):
            if m["id"] not in seen:
                seen.add(m["id"])
                print(f"  {int(time.time()-started):4d}s  NEW POST appeared — "
                      f"posted {_age(m.get('timestamp',''))}, {m.get('permalink','')}")
    print(f"\n  {calls} calls used over {minutes} minutes.")


def find_ceiling(ig_id: str, handle: str, token: str, cap: int) -> None:
    """
    Call until Meta refuses, so the real limit is measured rather than assumed.

    Documented as 200 per rolling hour per app user for business_discovery, but
    a separate per-target weekly cap is also mentioned in places without a
    number attached. This is how you find out which one you hit first.
    """
    print(f"Calling business_discovery for @{handle} until it is refused "
          f"(giving up at {cap}).\n")
    t0 = time.time()
    for i in range(1, cap + 1):
        d = discover(ig_id, handle, token, 1)
        if "error" in d:
            e = d["error"]
            print(f"  refused after {i} calls in {int(time.time()-t0)}s")
            print(f"  code {e.get('code')}: {e.get('message')}")
            return
        if i % 25 == 0:
            print(f"  {i} calls, {int(time.time()-t0)}s elapsed, still fine")
    print(f"  {cap} calls with no refusal. The ceiling is above that.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe Instagram's official API before building anything.")
    ap.add_argument("--token", default=os.getenv("IG_TOKEN", ""),
                    help="Graph API token (or set IG_TOKEN).")
    ap.add_argument("--targets", default="",
                    help="Comma-separated handles to look up, without the @.")
    ap.add_argument("--limit", type=int, default=25,
                    help="Posts to request per call (default 25).")
    ap.add_argument("--freshness", action="store_true",
                    help="Time how long a new post takes to appear.")
    ap.add_argument("--minutes", type=int, default=10,
                    help="How long --freshness watches for (default 10).")
    ap.add_argument("--find-ceiling", action="store_true",
                    help="Call until Meta refuses, to measure the real limit.")
    ap.add_argument("--max-calls", type=int, default=250,
                    help="Give up --find-ceiling after this many.")
    args = ap.parse_args()

    if not args.token:
        print("No token. Get one from the Graph API Explorer:\n"
              "  https://developers.facebook.com/tools/explorer\n"
              "then:  export IG_TOKEN='EAA...'", file=sys.stderr)
        return 2

    ig_id, page, err = my_ig_account(args.token)
    if not ig_id:
        print(f"Could not find your Instagram professional account.\n  {err}",
              file=sys.stderr)
        return 2
    print(f"Acting as Instagram account {ig_id} (via Page {page!r})\n")

    handles = [h.strip().lstrip("@") for h in args.targets.split(",") if h.strip()]
    if not handles:
        print("Nothing to look up. Pass --targets handle1,handle2", file=sys.stderr)
        return 2

    if args.find_ceiling:
        find_ceiling(ig_id, handles[0], args.token, args.max_calls)
        return 0
    if args.freshness:
        freshness(ig_id, handles[0], args.token, args.minutes)
        return 0

    print(f"Looking up {len(handles)} handle(s):\n")
    ok = probe(ig_id, handles, args.token, args.limit)
    print(f"\n{ok} of {len(handles)} reachable via the official API.")
    if ok < len(handles):
        print("The rest are personal or private accounts. Nothing official "
              "will reach those.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
