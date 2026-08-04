"""
ig_session.py — one place that OWNS an Instagram session's whole life: getting
it, saving it so it is reused instead of re-fetched, and quietly refreshing it
when it dies. Both onboarding paths (ig_login.py password, ig_import.py cookie)
funnel through here, and so does anything that collects.

THE EFFICIENCY IDEA, PLAINLY. The expensive thing is LOGGING IN — the captcha,
the code, the human. So you do it as few times as physically possible:

  * PERSIST everything. instagrapi's session is device fingerprint + cookies.
    We dump the whole thing to a sidecar file and reuse it every run. A logged
    in session, used gently from a steady address, lives for weeks. Reuse is
    the difference between "log in once a month" and "log in every run".

  * AUTO-RELOGIN. When a session finally dies, if the account's password is in
    .env (the X side's pattern — password_env names a variable, the value lives
    in .env), we log back in with no human at all and re-save. The manual paths
    are only ever for the FIRST login of an account, never the routine.

  * ONE SIDECAR, self-contained. profiles/ig_<username>.json holds instagrapi's
    settings AND the small metadata we need to refresh (label, proxy,
    password_env). The ig_accounts.db row is still written for the dashboard
    and guard to see; the sidecar is the reusable device state.

NOT VERIFIED END-TO-END against a live password login yet — challenge and 2FA
paths are wired to instagrapi's documented hooks but only exercised once you
run ig_login.py with a real account. Failures say what they saw, R6.
"""

import json
import os
import time
from pathlib import Path

import ig   # the session store (ig_accounts.db) and the Session dataclass

SETTINGS_DIR = Path("profiles")     # same folder the browser profiles live in


def sidecar_path(username: str, root: Path | str = ".") -> Path:
    return Path(root) / SETTINGS_DIR / f"ig_{username}.json"


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def persist(cl, username: str, *, label: str = "ig_a", proxy: str = "",
            password_env: str = "", store_path: str = "ig_accounts.db",
            root: Path | str = ".", log=lambda m: None) -> None:
    """
    Write the reusable session (sidecar) AND the visible row (ig_accounts.db).

    Call this after ANY successful login, however it was obtained. The sidecar
    is instagrapi's full settings plus the metadata needed to auto-relogin; the
    DB row is what the dashboard and guard read.
    """
    settings = cl.get_settings()
    path = sidecar_path(username, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {"username": username, "label": label, "proxy": proxy,
                 "password_env": password_env, "updated": _now()},
        "settings": settings,
    }
    path.write_text(json.dumps(payload, indent=2))
    try:
        path.chmod(0o600)   # holds live session cookies
    except OSError:
        pass
    log(f"[ig] saved reusable session -> {path}")

    cookies = (settings.get("cookies") or {})
    sess = ig.Session(
        username=username,
        user_id=str(cookies.get("ds_user_id") or getattr(cl, "user_id", "") or ""),
        user_agent=cl.user_agent,
        cookies={k: cookies.get(k) for k in
                 ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur")
                 if cookies.get(k)},
    )
    with ig.Store(Path(root) / store_path) as st:
        st.save(sess, label=label, proxy=proxy or "", active=True)


# --------------------------------------------------------------------------
# loading — reuse first, relogin only if forced to
# --------------------------------------------------------------------------

def _make_challenge_handler(log):
    def handler(username, choice):
        log(f"[ig] Instagram wants a code sent to your {choice}.")
        return input(f"    Enter the code Instagram sent to your {choice}: ").strip()
    return handler


def load_client(username: str, *, proxy: str = "", store_path: str = "ig_accounts.db",
                root: Path | str = ".", allow_relogin: bool = True,
                log=lambda m: None):
    """
    Return a working instagrapi client for @username, doing the LEAST possible.

    Order, cheapest first:
      1. Load the saved sidecar and prove it still works with one real call.
         This is the routine path — no login, no network beyond the check.
      2. If it is dead and the account's password is in .env, relogin and
         re-save. Automatic, no human.
      3. Otherwise raise, naming what to do (re-run ig_login or ig_import).
    """
    from instagrapi import Client
    from instagrapi.exceptions import ClientError, LoginRequired

    path = sidecar_path(username, root)
    if not path.exists():
        # No sidecar yet — e.g. the account was imported before this module
        # existed. Fall back to the cookies already saved in ig_accounts.db,
        # prove they still work, and write a sidecar so next time is the fast
        # reuse path. This is what makes an older ig_import session collectable.
        cl = _from_accounts_db(username, proxy_hint=proxy, store_path=store_path,
                               root=root, log=log)
        if cl is not None:
            return cl
        raise RuntimeError(
            f"no saved session for @{username} at {path}, and no working cookies "
            f"in ig_accounts.db either. Onboard it: `python3 ig_login.py {username}` "
            f"(password) or `python3 ig_import.py \"<sessionid>\"` (browser cookie).")

    data = json.loads(path.read_text())
    meta = data.get("meta", {})
    settings = data.get("settings", {})
    use_proxy = proxy or meta.get("proxy") or ""

    cl = Client()
    if use_proxy:
        cl.set_proxy(use_proxy)
    cl.set_settings(settings)
    cl.challenge_code_handler = _make_challenge_handler(log)

    # 1. Reuse: one authenticated call is the whole health check.
    try:
        cl.account_info()
        log(f"[ig] reused saved session for @{username} (no login needed)")
        return cl
    except (LoginRequired, ClientError) as e:
        log(f"[ig] saved session for @{username} is stale: {type(e).__name__}")

    # 2. Auto-relogin from .env, if we can.
    pw_env = meta.get("password_env") or ""
    password = os.environ.get(pw_env) if pw_env else None
    if allow_relogin and password:
        log(f"[ig] relogging in @{username} from {pw_env} …")
        try:
            cl.login(username, password, relogin=True)
            persist(cl, username, label=meta.get("label", "ig_a"),
                    proxy=use_proxy, password_env=pw_env,
                    store_path=store_path, root=root, log=log)
            log(f"[ig] @{username} refreshed automatically")
            return cl
        except Exception as e:
            raise RuntimeError(
                f"@{username}'s session died and auto-relogin failed: "
                f"{type(e).__name__}: {e}. Re-onboard with `python3 ig_login.py "
                f"{username}` or `python3 ig_import.py \"<sessionid>\"`.")

    raise RuntimeError(
        f"@{username}'s saved session is no longer valid and there is no "
        f"password in .env to refresh it automatically. Either set {pw_env or 'IG_PASSWORD_<LABEL>'} "
        f"in .env and re-run `python3 ig_login.py {username}`, or import a fresh "
        f"cookie with `python3 ig_import.py \"<sessionid>\"`.")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _from_accounts_db(username, *, proxy_hint="", store_path="ig_accounts.db",
                      root=".", log=lambda m: None):
    """
    Build a client from the cookies already stored in ig_accounts.db, validate
    them, and (on success) write a reusable sidecar. Returns the client, or None
    if there is no usable stored session. This is the bridge from an old-style
    imported session to the reuse/auto-relogin world.
    """
    import json
    import ig
    import engine_ig
    from instagrapi.exceptions import ClientError, LoginRequired

    apath = Path(root) / store_path
    if not apath.exists():
        return None
    with ig.Store(apath) as st:
        row = st.get(username)
    if not row:
        return None
    try:
        cookies = json.loads(row["cookies"] or "{}")
    except Exception:
        return None
    proxy = proxy_hint or (row["proxy"] or "")
    try:
        cl = engine_ig.build_client(cookies, row["user_agent"], proxy, log)
        cl.account_info()          # one real call proves the cookies still work
    except (ClientError, LoginRequired) as e:
        log(f"[ig] stored cookies for @{username} did not validate: {type(e).__name__}")
        return None
    except Exception as e:
        log(f"[ig] could not use stored cookies for @{username}: {type(e).__name__}: {e}")
        return None
    persist(cl, username, label=(row["label"] or "ig_a"), proxy=proxy,
            password_env="", store_path=store_path, root=root, log=log)
    log(f"[ig] migrated @{username}'s imported session into a reusable sidecar")
    return cl
