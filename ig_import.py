"""
ig_import.py — put a working Instagram session into the store WITHOUT the
automated-browser login.

WHY THIS EXISTS. ig.py's streamed browser is a Playwright-driven Chrome, and
Instagram detects that automation and re-serves the captcha forever — solving
it cannot succeed because the distrust is about the browser, not the answer.
But instagrapi authenticates from a `sessionid` cookie, and a sessionid minted
by your NORMAL browser (where Instagram already trusts you) works perfectly.
So: log in once by hand in real Chrome, copy the cookie, hand it to this
script. No captcha loop, because no automation is doing the logging in.

HOW TO GET THE sessionid (30 seconds, your own credential, never leaves your
machine):
  1. Open https://www.instagram.com in normal Chrome, logged in.
  2. Open DevTools (Cmd+Option+I) -> Application tab -> Cookies ->
     https://www.instagram.com
  3. Find the row named `sessionid`, copy its Value.
  4. Run:  python3 ig_import.py "PASTE_SESSIONID_HERE"
     (keep the quotes — the value contains : and %3A)

Optional:  python3 ig_import.py "<sessionid>" --label ig_a --proxy http://...
The proxy, if given, is stored and used for collection exactly like the browser
path would — one steady address per account (IG1).

WHAT IT DOES. Builds an instagrapi client from the cookie, proves it works with
a real authenticated call, reads back the TRUE handle (so the store key is
right), harvests the cookie jar, and saves an active row into ig_accounts.db —
the same shape ig.capture() would have written. Then engine_ig.py can load it.
"""

import argparse
import sys

import ig_session   # shared persist/reuse; it owns the ig_accounts.db writes


def _parse_ds_user_id(sessionid: str) -> str:
    """The sessionid begins with the numeric user id: '<id>%3A...' or '<id>:...'."""
    head = sessionid.split("%3A")[0].split(":")[0]
    return head if head.isdigit() else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Import an Instagram sessionid into ig_accounts.db")
    ap.add_argument("sessionid", help="the sessionid cookie value from your browser")
    ap.add_argument("--label", default="ig_a", help="short name for this account (default: ig_a)")
    ap.add_argument("--proxy", default="", help="optional proxy, e.g. http://user:pass@host:port")
    ap.add_argument("--store", default="ig_accounts.db", help="session store path")
    args = ap.parse_args()

    sessionid = args.sessionid.strip().strip('"').strip("'")
    if not sessionid or _parse_ds_user_id(sessionid) == "":
        print("That does not look like a sessionid — it should start with digits "
              "then ':' or '%3A'. Copy the whole Value of the `sessionid` cookie.")
        return 1

    from instagrapi import Client
    from instagrapi.exceptions import ClientError

    cl = Client()
    if args.proxy:
        cl.set_proxy(args.proxy)

    print("Validating the session with Instagram…")
    try:
        if not cl.login_by_sessionid(sessionid):
            print("Instagram rejected the sessionid. It may be expired — grab a "
                  "fresh one from your browser and try again.")
            return 2
        info = cl.account_info()             # a real authenticated call = proof
    except ClientError as e:
        print(f"Instagram rejected the session: {type(e).__name__}: {e}")
        print("Most often the sessionid is stale. Re-copy it from a browser where "
              "you are currently logged in.")
        return 2
    except Exception as e:
        print(f"Could not validate: {type(e).__name__}: {e}")
        return 2

    username = info.username or ""
    user_id = str(getattr(cl, "user_id", "") or _parse_ds_user_id(sessionid))

    # Persist through the shared module: it writes the reusable device+cookie
    # sidecar (so an imported cookie is reused exactly like a password login)
    # AND the ig_accounts.db row. There is no password_env here, so this
    # account cannot auto-relogin when the cookie expires — re-import a fresh
    # one, or onboard with ig_login.py to arm automatic refresh.
    ig_session.persist(cl, username, label=args.label, proxy=args.proxy or "",
                       password_env="", store_path=args.store, log=print)

    print(f"\n  Saved @{username} (id {user_id}) as active.")
    print(f"  Proxy: {args.proxy or 'none'}")
    print(f"  Tip: `python3 ig_login.py {username}` (password in .env) makes "
          f"future refreshes automatic, so you never copy a cookie again.")
    print(f"\nNow run:  python3 engine_ig.py {username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
