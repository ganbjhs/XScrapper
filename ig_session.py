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

  * ONE DEVICE, MINTED ONCE. profiles/ig_device_<label>.json holds nothing but
    the fingerprint — uuids, device_settings, user-agent, locale, timezone. See
    "THE DEVICE SEED" below; this is the thing that makes the two bullets above
    actually work rather than merely look like they work.

THE DEVICE SEED — why this file exists at all.

  instagrapi's Client() calls set_uuids({}) when it is handed no settings, and
  set_uuids({}) MINTS FRESH RANDOM UUIDS. So every `Client()` in this project
  was a brand-new phone as far as Instagram was concerned: import a cookie from
  one device, collect with a second, relogin from a third. Instagram reads that
  as a session moving between handsets, which is exactly the shape of a stolen
  cookie — so it invalidates the session and raises a checkpoint whose own error
  text asks you to "retry with the same saved client settings, device
  identifiers, and proxy/IP".

  The fix is one file per account label, written ONCE and then only ever read:

      profiles/ig_device_<label>.json

  Every client this project builds — password login, cookie import, collection —
  is constructed through new_client(), which loads that seed before anything
  else touches the wire. The seed is authoritative: when a sidecar written by an
  older run disagrees about the device, the SEED wins, so a session can never
  silently drift onto a new fingerprint. Nothing regenerates a seed that already
  exists; delete the file by hand if you genuinely want a new handset.

NOT VERIFIED END-TO-END against a live password login yet — challenge and 2FA
paths are wired to instagrapi's documented hooks but only exercised once you
run ig_login.py with a real account. Failures say what they saw, R6.
"""

import json
import os
import time
from pathlib import Path

import ig   # the session store (ig_accounts.db) and the Session dataclass
import ig_identity   # the coherent-phone catalogue; minting happens there

SETTINGS_DIR = Path("profiles")     # same folder the browser profiles live in

# Be gentle by default (IG4): instagrapi sleeps a random interval in this range
# between private requests, so a poll never machine-guns the API.
DELAY_RANGE = [1, 3]

# The subset of instagrapi's settings that describes the HANDSET rather than the
# login. Taken from Client.get_settings(); everything not listed here (cookies,
# authorization_data, last_login, ig_www_claim, retry knobs) is session or
# transport state and is free to change between logins.
#
# `mid` is Instagram's own device cookie. It is included so a learned mid is
# carried forward, but a null in the seed never overwrites a live one — see
# _splice_device.
DEVICE_KEYS = (
    "uuids",
    "device_settings",
    "user_agent",
    "country",
    "country_code",
    "locale",
    "timezone_offset",
    "timezone_name",
    "mid",
    # Ours, not instagrapi's (ig_identity): the phone's matching mobile
    # Chrome string for the web calls, and the human-readable identity block.
    # get_settings() drops them, so persist() splices them back from the seed.
    "web_user_agent",
    "identity",
)


def sidecar_path(username: str, root: Path | str = ".") -> Path:
    return Path(root) / SETTINGS_DIR / f"ig_{username}.json"


# --------------------------------------------------------------------------
# the device seed — created once per label, then read-only forever
# --------------------------------------------------------------------------

def device_path(label: str, root: Path | str = ".") -> Path:
    return Path(root) / SETTINGS_DIR / f"ig_device_{label or 'ig_a'}.json"


def load_device(label: str, root: Path | str = ".") -> dict:
    """The saved fingerprint for this label, or {} if none has been minted."""
    path = device_path(label, root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("device", {}) or {}
    except (OSError, ValueError):
        return {}


def _device_from_settings(settings: dict) -> dict:
    return {k: settings[k] for k in DEVICE_KEYS if settings.get(k) is not None}


def save_device(settings: dict, label: str, root: Path | str = ".",
                *, overwrite: bool = False, log=lambda m: None) -> dict:
    """
    Write the fingerprint for `label`, and NEVER rewrite one that exists.

    The refusal to overwrite is the whole point: a seed that can be replaced by
    the next login is not a stable device. Returns the seed now in force.
    """
    path = device_path(label, root)
    if path.exists() and not overwrite:
        return load_device(label, root)
    device = _device_from_settings(settings)
    if not device.get("uuids"):
        return {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"label": label, "created": _now(),
                                "device": device}, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log(f"[ig] minted the stable device for '{label}' -> {path} "
        f"(reused by every future login; delete it only to start a new handset)")
    return device


def ensure_device(label: str, root: Path | str = ".", *, username: str = "",
                  log=lambda m: None) -> dict:
    """
    Return the fingerprint for `label`, minting one if this is the first time.

    Minting order matters. If an account already has a sidecar, its device is
    the one Instagram has already seen — adopt THAT rather than inventing a new
    handset and throwing away whatever trust the old one earned. Only when there
    is nothing to adopt do we let instagrapi generate a fresh device.
    """
    device = load_device(label, root)
    if device:
        return device

    if username:
        path = sidecar_path(username, root)
        if path.exists():
            try:
                settings = json.loads(path.read_text()).get("settings", {}) or {}
            except (OSError, ValueError):
                settings = {}
            adopted = save_device(settings, label, root, log=log)
            if adopted:
                log(f"[ig] adopted @{username}'s existing device as the seed for "
                    f"'{label}' (the handset Instagram already knows)")
                return adopted

    # Nothing to adopt: mint a NEW, COHERENT phone (ig_identity), never the
    # library default. The default was the bug: every account this project
    # ever ran was the same US-locale Pixel 8 Pro (CHECKPOINT 2026-09-04).
    device = ig_identity.mint(label, taken=taken_models(root))
    dev = save_device(device, label, root, log=log)
    log(f"[ig] '{label}' is now {ig_identity.describe(dev)}")
    return dev


def taken_models(root: Path | str = ".") -> set:
    """Catalogue models already in use by other labels on this server, so a
    new mint does not hand two accounts the same handset."""
    out = set()
    d = Path(root) / SETTINGS_DIR
    if not d.exists():
        return out
    for p in d.glob("ig_device_*.json"):
        try:
            ds = (json.loads(p.read_text()).get("device") or {}).get("device_settings") or {}
            if ds.get("model"):
                out.add(ds["model"])
        except (OSError, ValueError):
            continue
    return out


def reseed(label: str, root: Path | str = ".", *, why: str = "",
           log=lambda m: None) -> dict:
    """
    Replace the device for `label` with a freshly minted one, ONLY ever as
    part of a sign-in (signin.py calls this; nothing in a collection pass
    may). The old seed is kept beside the new one as `.bak-<utc>` so a
    mistaken reseed is recoverable, and the reason is logged: a device
    change is a real event Instagram will notice, and the log must say why
    it was worth it.

    Legit reasons: the seed is the legacy library default (ig_identity
    .is_legacy) — a fresh login is being paid for anyway, so the new phone
    costs nothing extra; or the operator asked for a new handset.
    """
    path = device_path(label, root)
    old = load_device(label, root)
    if path.exists():
        bak = path.with_name(path.name + ".bak-" + _now().replace(":", ""))
        try:
            path.replace(bak)
            log(f"[ig] '{label}': previous device kept at {bak.name}")
        except OSError:
            path.unlink(missing_ok=True)
    device = ig_identity.mint(label, taken=taken_models(root))
    dev = save_device(device, label, root, log=log)
    log(f"[ig] '{label}' reseeded{(' — ' + why) if why else ''}: was "
        f"{ig_identity.describe(old) if old else 'nothing'}; now "
        f"{ig_identity.describe(dev)}")
    return dev


def _splice_device(settings: dict, device: dict) -> dict:
    """Lay the seed over a settings dict. The seed wins; nulls in it do not."""
    out = dict(settings or {})
    for k, v in (device or {}).items():
        if v is not None:
            out[k] = v
    return out


def new_client(label: str = "ig_a", *, proxy: str = "", settings: dict | None = None,
               username: str = "", root: Path | str = ".", log=lambda m: None):
    """
    THE ONLY WAY THIS PROJECT BUILDS AN instagrapi Client.

    Every caller — ig_login, ig_import, engine_ig.build_client, the reuse path
    below — comes through here, so there is exactly one place where the device
    is decided and no path can quietly mint a new one. `settings`, when given,
    is a saved session; the seed is spliced over it so the session cannot drag
    an old, different fingerprint back into play.
    """
    from instagrapi import Client

    device = ensure_device(label, root, username=username, log=log)
    cl = Client(settings=_splice_device(settings or {}, device),
                delay_range=list(DELAY_RANGE))
    if proxy:
        cl.set_proxy(proxy)          # HTTP or SOCKS5; residential per IG1
    return cl


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def persist(cl, username: str, *, label: str = "ig_a", proxy: str = "",
            password_env: str = "", store_path: str = "ig_accounts.db",
            root: Path | str = ".", exit: dict | None = None,
            log=lambda m: None) -> None:
    """
    Write the reusable session (sidecar) AND the visible row (ig_accounts.db).

    Call this after ANY successful login, however it was obtained. The sidecar
    is instagrapi's full settings plus the metadata needed to auto-relogin; the
    DB row is what the dashboard and guard read.
    """
    settings = cl.get_settings()

    # Pin the device before anything else. On the very first login for a label
    # this is what mints the seed; afterwards save_device is a no-op that just
    # confirms the client really did run on the pinned handset.
    device = save_device(settings, label, root, log=log)
    # get_settings() knows nothing of our keys (web UA, identity block); the
    # sidecar should still be self-describing, so lay the seed back over it.
    settings = _splice_device(settings, device)

    path = sidecar_path(username, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"username": username, "label": label, "proxy": proxy,
            "password_env": password_env, "updated": _now()}
    if exit:
        # Where this session was minted from, as seen from outside: the exit
        # IP and country of the proxy at sign-in. A later pass that finds a
        # different exit has something to say (the "one steady IP" rule now
        # has a number to check against).
        meta["exit"] = dict(exit)
    payload = {"meta": meta, "settings": settings}
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
                validate: bool = False, log=lambda m: None):
    """
    Return the saved instagrapi client for @username, doing the LEAST possible —
    which now means NO eager health check at all.

    WHY THERE IS NO PROBE ANY MORE. This function used to open with one
    "authenticated call" to decide whether the session was alive, and that
    decision was wrong in a way that cost us the whole feature. A checkpointed
    account still answers plenty of endpoints: measured on a live session,
    user_medias_paginated_v1 returned 12 posts while get_timeline_feed and
    user_info_v1 both returned login_required. The probe was therefore STRICTER
    THAN THE WORK — it threw away a session that could collect every configured
    source, then knocked on the checkpoint for a relogin nothing had asked for.

    A probe is only meaningful if it tests what the caller is about to do, and
    the caller here does several different things (home feed, user feed, hashtag)
    with different permissions. So the honest design is lazy: hand back the
    session, let the REAL call be the test, and refresh only once something
    actually fails. That is also one fewer request per poll (IG4).

    Pass validate=True to opt into the old eager check when you genuinely want
    "is the home feed reachable" answered up front.
    """
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
    label = meta.get("label") or "ig_a"
    use_proxy = proxy or meta.get("proxy") or ""

    # new_client splices the pinned device over the saved session, so reuse and
    # any relogin below both happen on the SAME handset the session was minted
    # on — the condition Instagram's checkpoint message actually asks for.
    cl = new_client(label, proxy=use_proxy, settings=settings,
                    username=username, root=root, log=log)
    cl.challenge_code_handler = _make_challenge_handler(log)

    if not validate:
        log(f"[ig] reusing saved session for @{username} (no login, no probe)")
        return cl

    from instagrapi.exceptions import ClientError, LoginRequired
    try:
        cl.get_timeline_feed()
        log(f"[ig] saved session for @{username} can reach the home feed")
        return cl
    except (LoginRequired, ClientError) as e:
        log(f"[ig] @{username} cannot reach the home feed: {type(e).__name__}")
    return refresh(username, proxy=use_proxy, store_path=store_path, root=root,
                   allow_relogin=allow_relogin, log=log)


def refresh(username: str, *, proxy: str = "", store_path: str = "ig_accounts.db",
            root: Path | str = ".", allow_relogin: bool = True,
            log=lambda m: None):
    """
    Get a NEW session for @username after the saved one has actually failed.

    Called on demand — when a real collection request came back login_required —
    rather than speculatively. That ordering matters: relogin is the expensive,
    risky operation (it is what draws checkpoints), so it should only ever be
    paid for by a caller that has genuinely hit a wall.
    """
    from instagrapi.exceptions import ChallengeRequired

    path = sidecar_path(username, root)
    if not path.exists():
        raise RuntimeError(f"no saved session for @{username} at {path} to refresh")
    data = json.loads(path.read_text())
    meta = data.get("meta", {})
    label = meta.get("label") or "ig_a"
    use_proxy = proxy or meta.get("proxy") or ""

    # A checkpoint already seen is a WALL, not a flaky call. Without this, a
    # `collect_ig.py run --loop` would fire a fresh login attempt at a locked
    # account every interval — the single most effective way to turn a
    # clearable checkpoint into a dead account. persist() rewrites meta on the
    # next successful login, so a good import clears this by itself.
    if meta.get("checkpoint_at"):
        raise RuntimeError(_checkpoint_help(
            username, label, root,
            f"a checkpoint was recorded at {meta['checkpoint_at']} and has not "
            f"been cleared since"))

    # An account onboarded by ig_import.py has no password_env recorded, but the
    # password may still be sitting in .env under the conventional name. Look
    # there before giving up — otherwise a cookie-imported account needs a human
    # for every expiry, which is the exact cost this module exists to remove.
    pw_env = meta.get("password_env") or f"IG_PASSWORD_{label.upper()}"
    password = _password(pw_env)
    if not (allow_relogin and password):
        raise RuntimeError(
            f"@{username}'s saved session is no longer valid and there is no "
            f"password in .env to refresh it automatically. Either set {pw_env} "
            f"in .env and re-run `python3 ig_login.py {username}`, or import a fresh "
            f"cookie with `python3 ig_import.py \"<sessionid>\"`.")

    cl = new_client(label, proxy=use_proxy, settings=data.get("settings", {}),
                    username=username, root=root, log=log)
    cl.challenge_code_handler = _make_challenge_handler(log)
    log(f"[ig] relogging in @{username} from {pw_env} …")
    try:
        cl.login(username, password, relogin=True)
    except ChallengeRequired as e:
        # NOT a bug, and NOT something to retry. Instagram is holding a
        # checkpoint that only the official app or web flow can clear, and
        # every further login attempt from here makes the lock stickier. Say so
        # instead of suggesting a command that cannot work — the honest failure
        # is the useful one (R6).
        _note_checkpoint(username, root, log=log)
        raise RuntimeError(_checkpoint_help(username, label, root, str(e)))
    except Exception as e:
        raise RuntimeError(
            f"@{username}'s session died and auto-relogin failed: "
            f"{type(e).__name__}: {e}. Re-onboard with `python3 ig_login.py "
            f"{username}` or `python3 ig_import.py \"<sessionid>\"`.")

    persist(cl, username, label=label, proxy=use_proxy, password_env=pw_env,
            store_path=store_path, root=root, log=log)
    log(f"[ig] @{username} refreshed automatically")
    return cl


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# --------------------------------------------------------------------------
# the exit — prove the proxy BEFORE a login is spent through it
# --------------------------------------------------------------------------

IP_ECHO = "https://api.ipify.org?format=json"
GEO_URL = "https://ipapi.co/{ip}/country/"
IG_HOME = "https://www.instagram.com/"


def redact_proxy(proxy: str) -> str:
    """A proxy URL is a credential. Show the host (and the session label in
    the username, which is what the operator recognises), never the password."""
    if not proxy:
        return "the server IP"
    try:
        import urllib.parse as up
        u = up.urlsplit(proxy)
        host = u.hostname or "?"
        who = f"{u.username}@" if u.username else ""
        return f"{who}{host}:{u.port}" if u.port else f"{who}{host}"
    except Exception:
        return "the configured proxy"


def proxy_check(proxy: str, *, timeout: int = 20, expect_country: str = "",
                get=None) -> dict:
    """
    What the world sees when this account speaks: exit IP, its country, and
    whether instagram.com answers through it with a certificate we trust.

    Run at sign-in (signin.py) and by the diag endpoint — never per pass,
    because none of these calls go to Instagram and one extra unrelated
    request per pass buys nothing. Result keys:
        ok            the exit is usable for Instagram
        why           '' | no_proxy | network | tls_intercepted | blocked
        exit_ip       what api.ipify.org saw
        country       ISO-2 from ipapi.co ('' if that lookup failed — a
                      lookup failure is not a proxy failure)
        ig_status     HTTP status instagram.com returned through the exit
        warn          a country that does not match `expect_country`
        detail        one sentence for the operator
    `get` is injectable (tests): get(url, proxies, timeout) -> response.
    """
    out = {"ok": False, "why": "", "proxy": redact_proxy(proxy), "exit_ip": "",
           "country": "", "ig_status": 0, "warn": "", "detail": "",
           "checked": _now()}
    if not proxy:
        out.update(why="no_proxy", detail="no proxy configured — Instagram "
                                          "must never see the server IP")
        return out
    if get is None:
        import requests

        def get(url, proxies, timeout):
            return requests.get(url, proxies=proxies, timeout=timeout,
                                headers={"User-Agent": "curl/8.4.0"})
    proxies = {"http": proxy, "https": proxy}
    from engine_ig import network_why

    try:
        r = get(IP_ECHO, proxies, timeout)
        out["exit_ip"] = str((r.json() or {}).get("ip") or "").strip()
    except Exception as e:
        out.update(why=network_why(e) or "network",
                   detail=f"the exit does not answer: {type(e).__name__}: "
                          f"{str(e)[:160]}")
        return out
    if out["exit_ip"]:
        try:
            r = get(GEO_URL.format(ip=out["exit_ip"]), proxies, timeout)
            cc = (r.text or "").strip().upper()
            if len(cc) == 2 and cc.isalpha():
                out["country"] = cc
        except Exception:
            pass
    try:
        r = get(IG_HOME, proxies, timeout)
        out["ig_status"] = int(getattr(r, "status_code", 0) or 0)
    except Exception as e:
        out.update(why=network_why(e) or "network",
                   detail=f"instagram.com is unreachable through this exit: "
                          f"{type(e).__name__}: {str(e)[:160]}")
        return out
    if out["ig_status"] in (401, 403, 429):
        out.update(why="blocked",
                   detail=f"instagram.com answered {out['ig_status']} to a bare "
                          f"request through this exit — the address itself is "
                          f"unwelcome; change the session number in the proxy "
                          f"username")
        return out
    out["ok"] = True
    if expect_country and out["country"] and out["country"] != expect_country:
        out["warn"] = (f"the exit is in {out['country']}, the identity says "
                       f"{expect_country} — a person whose phone and IP "
                       f"disagree about the country")
    out["detail"] = (f"exit {out['exit_ip'] or '?'}"
                     + (f" ({out['country']})" if out["country"] else "")
                     + f", instagram.com answers {out['ig_status']}")
    return out


def _checkpoint_help(username: str, label: str, root, detail: str) -> str:
    """The one message worth printing when Instagram has locked an account.

    It names the human steps because there are no code steps: instagrapi cannot
    answer a native challenge, and pretending otherwise just burns the account.
    """
    return (
        f"@{username} is CHECKPOINT-LOCKED by Instagram: {detail}\n"
        f"  No code can clear this — re-running ig_login.py hits the same wall, "
        f"and repeated attempts make it worse.\n"
        f"  A human must:\n"
        f"    1. open Accounts & Sessions → Sign in → 'Open this account's browser'\n"
        f"       (the account's own phone-shaped Chromium, through its own proxy),\n"
        f"       or instagram.com / the app on a trusted phone, as @{username}\n"
        f"    2. complete the 'confirm it's you' / security-check prompt\n"
        f"    3. the browser door adopts the session itself; from any other\n"
        f"       browser, paste the cookies into the same panel instead\n"
        f"  The device is already pinned ({device_path(label, root)}), so the "
        f"imported cookie is adopted by the same handset it was minted on — which "
        f"is what should stop the session being invalidated again.")


def _note_checkpoint(username: str, root, *, log=lambda m: None) -> None:
    """Record the lock in the sidecar so later polls do not knock again."""
    path = sidecar_path(username, root)
    try:
        data = json.loads(path.read_text())
        data.setdefault("meta", {})["checkpoint_at"] = _now()
        path.write_text(json.dumps(data, indent=2))
    except (OSError, ValueError) as e:
        log(f"[ig] could not record the checkpoint in {path}: {type(e).__name__}")


def _password(env_name: str) -> str:
    """Read a password from the environment, loading .env first like ig_login."""
    if not env_name:
        return ""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return os.environ.get(env_name) or ""


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
    label = row["label"] or "ig_a"
    try:
        # build_client's login_by_sessionid is itself a real authenticated call,
        # so it is proof enough that the cookie is live. No extra home-feed
        # probe: see load_client on why a probe stricter than the work is worse
        # than no probe at all.
        cl = engine_ig.build_client(cookies, row["user_agent"], proxy, log,
                                    label=label, root=root)
    except (ClientError, LoginRequired) as e:
        log(f"[ig] stored cookies for @{username} did not validate: {type(e).__name__}")
        return None
    except Exception as e:
        log(f"[ig] could not use stored cookies for @{username}: {type(e).__name__}: {e}")
        return None
    persist(cl, username, label=label, proxy=proxy,
            password_env="", store_path=store_path, root=root, log=log)
    log(f"[ig] migrated @{username}'s imported session into a reusable sidecar")
    return cl
