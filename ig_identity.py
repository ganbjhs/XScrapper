"""
ig_identity.py — ONE coherent phone per Instagram account, minted once.

WHY THIS FILE EXISTS. Every device seed this project had ever minted was the
same handset: instagrapi's built-in default — a Google Pixel 8 Pro, husky,
480dpi, app 428.0.0.47.67, locale en_US, country US, timezone −14400 (US
Eastern). Two seeds minted a day apart (`ig_device_ig_a.json`,
`ig_device_ig_b.json`) differed only in their UUIDs. Every instagrapi user on
earth presents that exact phone, and Instagram has seen it a few hundred
million times. On top of that our accounts run through Indian residential
exits (Webshare `resi-in-*`) and were born from a DESKTOP browser cookie — so
the story Instagram was told was: a US-English Pixel in the US Eastern time
zone, logging in from Uttar Pradesh, whose session was minted by Chrome on a
Mac. Three accounts, identical phone, three different IPs. That is not a
person; that is a script, and it was treated as one (2026-09-03: every
lookup 429 on sight, `PleaseWaitFewMinutes` on the first feed read, a
checkpoint the same evening, zero posts ever stored).

THE RULE THIS ENFORCES: an identity is COHERENT and it is UNIQUE.

  * coherent — the phone, the app build, the locale, the country, the time
    zone and the web user-agent all tell the same story, and that story
    matches the proxy exit (India). A Samsung sold in India, Instagram from
    the Play Store, en_IN, Asia/Kolkata, a mobile Chrome on the same Android
    release for the web calls.
  * unique — no two accounts share a handset. The catalogue is drawn from at
    random per label, and the draw is written down once (the device file) and
    then only ever read (ig_session's seed rule). Nothing in a collection pass
    can change it.

WHAT IS DELIBERATELY NOT INVENTED. App builds come from instagrapi's own
`APP_SETTINGS` — the (app_version, version_code, bloks_versioning_id) triples
the library ships and has verified against the wire. A made-up version code is
a louder signal than the default, so the list is read from the library, never
typed here. The Chrome major for the web user-agent is read from the browser
this machine actually has (Playwright's Chromium), so the string we send is the
string a real render would send; only when no browser is installed does the
env fallback apply.

Pure module: no network, no instagrapi import at module load, every random
draw takes an injectable rng. Test: tests/test_all.py::test_ig_identity.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import uuid

# ---------------------------------------------------------------------------
# the catalogue — phones a person in India actually carries
# ---------------------------------------------------------------------------
#
# Fields follow instagrapi's device_settings exactly, because that is what the
# app user-agent is formatted from:
#   Instagram {app_version} Android ({android_version}/{android_release}; {dpi};
#   {resolution}; {manufacturer}; {model}; {device}; {cpu}; {locale}; {version_code})
#
# `name` is for humans (the dashboard card). Everything else is what the wire
# sees. Add a phone by adding a row; never edit a row that a live account was
# minted from (its device file holds its own copy, so the edit would only
# affect NEW accounts — but the point of a catalogue is that its rows are
# real).
DEVICES = (
    # name,                    manufacturer,   model,        device,     cpu,        resolution,  dpi,      api, release
    ("Samsung Galaxy A54 5G",  "samsung",      "SM-A546E",   "a54x",     "s5e8835",  "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A34 5G",  "samsung",      "SM-A346E",   "a34x",     "mt6877",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy M34 5G",  "samsung",      "SM-M346B",   "m34x",     "s5e8835",  "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A15",     "samsung",      "SM-A155F",   "a15",      "mt6789",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A73 5G",  "samsung",      "SM-A736B",   "a73xq",    "qcom",     "1080x2400", "450dpi", 33, "13"),
    ("Samsung Galaxy S23",     "samsung",      "SM-S911B",   "dm1q",     "kalama",   "1080x2340", "450dpi", 34, "14"),
    ("Redmi Note 12 5G",       "Xiaomi/Redmi", "22111317I",  "sunstone", "qcom",     "1080x2400", "440dpi", 33, "13"),
    ("Redmi Note 13 Pro 5G",   "Xiaomi/Redmi", "2312DRA50I", "garnet",   "qcom",     "1220x2712", "480dpi", 34, "14"),
    ("POCO X5 Pro 5G",         "Xiaomi/POCO",  "22101320I",  "redwood",  "qcom",     "1080x2400", "440dpi", 33, "13"),
    ("realme 11 Pro 5G",       "realme",       "RMX3771",    "RE5C82L1", "mt6877",   "1080x2412", "480dpi", 34, "14"),
    ("realme narzo 60 5G",     "realme",       "RMX3750",    "RE5A6BL1", "mt6877",   "1080x2400", "480dpi", 34, "14"),
    ("OnePlus Nord CE 3 Lite", "OnePlus",      "CPH2467",    "OP5958L1", "qcom",     "1080x2400", "480dpi", 34, "14"),
    ("OnePlus Nord 3 5G",      "OnePlus",      "CPH2491",    "OP5A8FL1", "mt6983",   "1240x2772", "480dpi", 34, "14"),
    ("vivo T2 5G",             "vivo",         "V2247",      "2247",     "qcom",     "1080x2400", "480dpi", 34, "14"),
    ("vivo V29 5G",            "vivo",         "V2250",      "2250",     "qcom",     "1260x2800", "480dpi", 34, "14"),
    ("OPPO A78 5G",            "OPPO",         "CPH2481",    "OP56E9L1", "mt6833",   "1080x2400", "480dpi", 33, "13"),
    ("OPPO Reno10 5G",         "OPPO",         "CPH2531",    "OP5A57L1", "mt6877",   "1080x2412", "480dpi", 34, "14"),
)

# The market the identities are minted for. One value, on purpose: the proxy
# exits are Indian and a mixed bag of countries across accounts on one server
# would be its own tell. Change the whole tuple, not one field of it.
MARKET = {
    "locale": "en_IN",
    "country": "IN",
    "country_code": 91,
    "timezone_offset": 19800,          # +05:30
    "timezone_name": "Asia/Kolkata",
    "web_locale": "en-IN",             # BCP-47, for the browser
    "accept_language": "en-IN,en;q=0.9,hi;q=0.8",
}

# How likely each app build is: people mostly run the latest, a tail lags.
# Keyed by app_version; must exist in instagrapi's APP_SETTINGS or it is
# ignored (see app_builds()).
BUILD_WEIGHTS = {"428.0.0.47.67": 60, "385.0.0.47.74": 25, "364.0.0.35.86": 15}

# Chrome's REDUCED mobile user-agent (Chrome ≥ 110 freezes the Android
# release to "10" and the model to "K"; the real values travel in Client
# Hints). Sending the unreduced form with a real model is what an OLD Chrome
# would do — a browser from 2022 is its own flag.
WEB_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36")
FALLBACK_CHROME_MAJOR = "140"


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------

def app_builds() -> list:
    """The (app_version, version_code, bloks_versioning_id) triples instagrapi
    ships, weighted by BUILD_WEIGHTS. Read from the library so a pin bump that
    retires a build retires it here too; falls back to the one default build
    if the library is not importable (tests, doctor)."""
    try:
        from instagrapi import config as _c
        table = dict(_c.APP_SETTINGS)
    except Exception:
        table = {"428.0.0.47.67": {"app_version": "428.0.0.47.67",
                                   "version_code": "961145276",
                                   "bloks_versioning_id": "7189b949425f9bf80ea8bd880cf5a3080b292d9b1c4b38a18d112f7c4b71e7a8"}}
    out = []
    for ver, row in table.items():
        w = BUILD_WEIGHTS.get(ver, 5)
        out.append((w, {k: row[k] for k in ("app_version", "version_code",
                                            "bloks_versioning_id")}))
    return out


def chrome_major(env=os.environ) -> str:
    """The Chrome major this machine's browser really is.

    Order: IG_WEB_CHROME_MAJOR (an explicit operator pin) → Playwright's
    Chromium `--version` → FALLBACK_CHROME_MAJOR. Cached into the device
    file at mint time, so this runs once per account, not per request."""
    pinned = (env.get("IG_WEB_CHROME_MAJOR") or "").strip()
    if pinned.isdigit():
        return pinned
    # In a child process, on purpose: the sync Playwright API refuses to run
    # inside an asyncio loop, and the streamed sign-in window mints the
    # identity from exactly such a loop (web.py runs it on _LOOP).
    code = ("from playwright.sync_api import sync_playwright\n"
            "import subprocess\n"
            "with sync_playwright() as pw:\n"
            "    exe = pw.chromium.executable_path\n"
            "print(subprocess.run([exe, '--version'], capture_output=True, "
            "text=True, timeout=15).stdout)\n")
    try:
        import sys
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=40, env=dict(env)).stdout
        m = re.search(r"(\d{2,3})\.\d+\.\d+\.\d+", out or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return FALLBACK_CHROME_MAJOR


def _android_device_id(rng) -> str:
    return "android-%016x" % rng.getrandbits(64)


def new_uuids(rng=None) -> dict:
    """Fresh, unrelated UUIDs in the shape instagrapi expects."""
    rng = rng or random.SystemRandom()
    u = lambda: str(uuid.UUID(int=rng.getrandbits(128), version=4))
    return {
        "phone_id": u(), "uuid": u(), "client_session_id": u(),
        "advertising_id": u(), "android_device_id": _android_device_id(rng),
        "request_id": u(), "tray_session_id": u(),
    }


def app_user_agent(device_settings: dict, locale: str) -> str:
    """instagrapi's exact format, so the string we PIN is the string the
    library would have built — and it keeps our locale, which the library's
    own set_device() would not (it formats with en_US before set_locale
    runs)."""
    d = dict(device_settings, locale=locale)
    return ("Instagram {app_version} Android ({android_version}/{android_release}; "
            "{dpi}; {resolution}; {manufacturer}; {model}; {device}; {cpu}; "
            "{locale}; {version_code})").format(**d)


def _pick(rng, weighted):
    total = sum(w for w, _ in weighted)
    r = rng.uniform(0, total)
    acc = 0
    for w, item in weighted:
        acc += w
        if r <= acc:
            return item
    return weighted[-1][1]


# ---------------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------------

def mint(label: str, *, rng=None, chrome=None, taken=()) -> dict:
    """
    A brand-new coherent identity for `label`, as the dict ig_session stores
    under "device" and splices over every instagrapi settings dict.

    `taken` is the set of catalogue model strings other accounts on this
    server already use; the draw avoids them while it can (uniqueness), and
    only repeats a model once the catalogue is exhausted.
    """
    rng = rng or random.SystemRandom()
    avail = [d for d in DEVICES if d[2] not in set(taken)] or list(DEVICES)
    name, manu, model, dev, cpu, res, dpi, api, rel = rng.choice(avail)
    build = _pick(rng, app_builds())
    device_settings = {
        "android_version": api, "android_release": rel, "dpi": dpi,
        "resolution": res, "manufacturer": manu, "device": dev,
        "model": model, "cpu": cpu,
        **build,
    }
    major = str(chrome or chrome_major())
    w, h = (int(x) for x in res.split("x"))
    scale = int(dpi.rstrip("dpi")) / 160.0
    return {
        "uuids": new_uuids(rng),
        "device_settings": device_settings,
        "user_agent": app_user_agent(device_settings, MARKET["locale"]),
        "country": MARKET["country"],
        "country_code": MARKET["country_code"],
        "locale": MARKET["locale"],
        "timezone_offset": MARKET["timezone_offset"],
        "timezone_name": MARKET["timezone_name"],
        # Ours, not instagrapi's: carried in the device file, read by
        # engine_ig._browser_session and ig.InteractiveLogin.
        "web_user_agent": WEB_UA.format(major=major),
        "identity": {
            "version": 1,
            "label": label,
            "name": name,
            "chrome_major": major,
            "web_locale": MARKET["web_locale"],
            "accept_language": MARKET["accept_language"],
            "screen": {"width": w, "height": h, "scale": round(scale, 3),
                       "css_width": round(w / scale), "css_height": round(h / scale)},
        },
    }


# ---------------------------------------------------------------------------
# reading one back
# ---------------------------------------------------------------------------

LEGACY_MODELS = {"Pixel 8 Pro"}


def is_legacy(device: dict) -> bool:
    """True for a seed minted before this module existed: instagrapi's
    default handset with a US locale and no identity block. Such a device is
    replaced at the account's NEXT SIGN-IN (a new phone costs a login anyway),
    never during collection — ig_session.ensure_device holds that line."""
    if not device:
        return False
    if device.get("identity"):
        return False
    ds = device.get("device_settings") or {}
    return (ds.get("model") in LEGACY_MODELS
            or (device.get("locale") or "").endswith("_US")
            or device.get("country") == "US")


def describe(device: dict) -> str:
    """One line for the dashboard: what phone this account is."""
    if not device:
        return "no device minted yet"
    ds = device.get("device_settings") or {}
    ident = device.get("identity") or {}
    name = ident.get("name") or f"{ds.get('manufacturer', '?')} {ds.get('model', '?')}"
    bits = [name, f"Android {ds.get('android_release', '?')}",
            f"Instagram {ds.get('app_version', '?')}",
            device.get("locale") or "?",
            device.get("timezone_name") or f"UTC{device.get('timezone_offset', 0) / 3600:+.1f}"]
    if is_legacy(device):
        bits.append("LEGACY default phone — re-sign-in mints a real one")
    return " · ".join(bits)


def summary(device: dict) -> dict:
    """The JSON the dashboard card shows. No UUIDs — they are identifiers."""
    ds = device.get("device_settings") or {}
    ident = device.get("identity") or {}
    return {
        "name": ident.get("name") or "",
        "model": ds.get("model") or "", "manufacturer": ds.get("manufacturer") or "",
        "android": ds.get("android_release") or "",
        "app_version": ds.get("app_version") or "",
        "locale": device.get("locale") or "", "country": device.get("country") or "",
        "timezone": device.get("timezone_name") or "",
        "chrome_major": ident.get("chrome_major") or "",
        "legacy": is_legacy(device),
        "text": describe(device),
    }


def web_headers(device: dict) -> dict:
    """Headers for a requests.Session that must look like this phone's
    Chrome: the reduced UA plus the Client Hints Chrome sends unprompted."""
    ident = (device or {}).get("identity") or {}
    ds = (device or {}).get("device_settings") or {}
    major = ident.get("chrome_major") or FALLBACK_CHROME_MAJOR
    ua = (device or {}).get("web_user_agent") or WEB_UA.format(major=major)
    return {
        "User-Agent": ua,
        "Accept-Language": ident.get("accept_language") or MARKET["accept_language"],
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
                     f'"Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua-model": f'"{ds.get("model", "")}"' if ds.get("model") else '""',
        "sec-ch-ua-platform-version": f'"{ds.get("android_release", "")}.0.0"',
    }


def playwright_kwargs(device: dict) -> dict:
    """What the streamed sign-in browser must be told to BE this phone."""
    ident = (device or {}).get("identity") or {}
    scr = ident.get("screen") or {}
    major = ident.get("chrome_major") or FALLBACK_CHROME_MAJOR
    return {
        "user_agent": (device or {}).get("web_user_agent") or WEB_UA.format(major=major),
        "viewport": {"width": int(scr.get("css_width") or 412),
                     "height": int(scr.get("css_height") or 915)},
        "device_scale_factor": float(scr.get("scale") or 2.625),
        "is_mobile": True,
        "has_touch": True,
        "locale": ident.get("web_locale") or MARKET["web_locale"],
        "timezone_id": (device or {}).get("timezone_name") or MARKET["timezone_name"],
    }


def cdp_user_agent_metadata(device: dict) -> dict:
    """Client Hints for CDP Emulation.setUserAgentOverride, so the browser's
    own hints agree with the UA we set (Playwright sets only the UA string,
    which leaves sec-ch-ua-platform saying Linux under an Android UA)."""
    ident = (device or {}).get("identity") or {}
    ds = (device or {}).get("device_settings") or {}
    major = ident.get("chrome_major") or FALLBACK_CHROME_MAJOR
    brands = [{"brand": "Chromium", "version": major},
              {"brand": "Google Chrome", "version": major},
              {"brand": "Not-A.Brand", "version": "99"}]
    return {
        "brands": brands,
        "fullVersionList": [{"brand": b["brand"], "version": f"{b['version']}.0.0.0"}
                            for b in brands],
        "fullVersion": f"{major}.0.0.0",
        "platform": "Android",
        "platformVersion": f"{ds.get('android_release', '14')}.0.0",
        "architecture": "",
        "model": ds.get("model", ""),
        "mobile": True,
        "bitness": "",
        "wow64": False,
    }


def stable_offset(username: str, span_h: float = 1.5) -> float:
    """A per-account shift, in hours, for the active-hours window: derived
    from the handle so it is the same every day (a person's habits) and
    different per account (three phones do not wake at 07:00:00 together)."""
    h = int(hashlib.sha1((username or "").lower().encode()).hexdigest()[:8], 16)
    return (h / 0xFFFFFFFF) * 2 * span_h - span_h
