"""
ig_login.py — onboard an Instagram account by PASSWORD, once, then never again.

This is the efficient primary path. You run it a single time per account; it
logs in through instagrapi's mobile-app API (which Instagram trusts far more
than the automated browser that loops the captcha), handles a one-time code or
2FA if asked, and SAVES the session so every future run reuses it. When that
session eventually dies, ig_session.load_client relogins automatically from the
password in .env — so this command is genuinely once per account, not routine.

USAGE
  # password in .env (recommended — matches the X side's password_env pattern):
  #   echo 'IG_PASSWORD_IG_A=your_password' >> .env
  python3 ig_login.py omarfarooq724 --label ig_a

  # or type it once, not stored (auto-relogin then won't be possible):
  python3 ig_login.py omarfarooq724 --label ig_a --ask

  # options: --proxy http://user:pass@host:port     one steady address (IG1)
  #          --password-env IG_PASSWORD_IG_A         override the env var name
  #          --totp-secret <base32>                  if the account has 2FA

WHY password beats the browser here: the captcha loop you hit is Instagram
distrusting an automated *browser*. instagrapi speaks the app API with a stable
device fingerprint, which clears that bar far more often. It can still ask for
ONE email/SMS code the first time from a new address — that is normal, and the
prompt will tell you where the code was sent.
"""

import argparse
import getpass
import os
import sys

import ig_session


def _resolve_password(args) -> tuple[str, str]:
    """Return (password, password_env). Prefer .env; fall back to a prompt."""
    env_name = args.password_env or f"IG_PASSWORD_{args.label.upper()}"

    # Load .env the same way the rest of the project does, so a password put
    # there is found without exporting it by hand.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    val = os.environ.get(env_name)
    if val:
        return val, env_name
    if args.ask:
        return getpass.getpass(f"Instagram password for @{args.username}: "), ""
    print(f"No password found in {env_name}. Either add it to .env:\n"
          f"    echo '{env_name}=your_password' >> .env\n"
          f"then re-run, or pass --ask to type it once (auto-relogin then "
          f"won't be possible, since nothing is stored to relogin with).")
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Password-login an Instagram account and save it for reuse")
    ap.add_argument("username")
    ap.add_argument("--label", default="ig_a")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--password-env", default="")
    ap.add_argument("--totp-secret", default="", help="base32 2FA secret, if the account uses 2FA")
    ap.add_argument("--ask", action="store_true", help="type the password once instead of reading .env")
    ap.add_argument("--store", default="ig_accounts.db")
    args = ap.parse_args()

    password, password_env = _resolve_password(args)

    from instagrapi.exceptions import (
        TwoFactorRequired, ChallengeRequired, ClientError,
    )

    # Same device every time. Instagram's checkpoint message asks in so many
    # words for "the same saved client settings, device identifiers, and
    # proxy/IP" — new_client is where all three are held steady.
    cl = ig_session.new_client(args.label, proxy=args.proxy,
                               username=args.username, log=print)
    cl.challenge_code_handler = ig_session._make_challenge_handler(print)

    verification_code = ""
    if args.totp_secret:
        try:
            verification_code = cl.totp_generate_code(args.totp_secret)
        except Exception as e:
            print(f"Could not generate a 2FA code from --totp-secret: {e}")

    print(f"Logging in as @{args.username} …")
    try:
        ok = cl.login(args.username, password, verification_code=verification_code)
    except TwoFactorRequired:
        code = input("    Two-factor code (from your app/SMS): ").strip()
        try:
            ok = cl.login(args.username, password, verification_code=code)
        except Exception as e:
            print(f"2FA login failed: {type(e).__name__}: {e}")
            return 2
    except ChallengeRequired as e:
        # instagrapi drives the challenge through challenge_code_handler; if it
        # still surfaces here, the handler could not resolve it.
        print(f"Instagram raised a challenge it could not auto-resolve: {e}")
        print("Tip: if this keeps happening, use the cookie path instead: "
              "`python3 ig_import.py \"<sessionid>\"`.")
        return 2
    except ClientError as e:
        print(f"Login failed: {type(e).__name__}: {e}")
        return 2
    except Exception as e:
        print(f"Login failed: {type(e).__name__}: {e}")
        return 2

    if not ok:
        print("Instagram did not accept the login (no exception, but not ok).")
        return 2

    ig_session.persist(cl, args.username, label=args.label, proxy=args.proxy,
                       password_env=password_env, store_path=args.store, log=print)
    print(f"\n  @{args.username} is signed in and saved for reuse.")
    if password_env:
        print(f"  Auto-relogin is armed via {password_env} in .env — you should "
              f"not need to log in by hand again.")
    else:
        print("  Note: password was not stored, so a future expiry will need a "
              "manual re-login. Put it in .env to make refresh automatic.")
    print(f"\nNow run:  python3 engine_ig.py {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
