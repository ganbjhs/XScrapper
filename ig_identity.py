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

MARKETS (2026-10-03). "India" in the rule above is now "the country this
account's proxy exits in". The market is chosen per account at mint time from
the MARKETS table below and written into that account's device file; adding a
country is one row there. Existing device files are never rewritten.

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

# Phones sold through European retail: Samsung's international "B"/"F"
# models, Xiaomi's global "G" models, Google's own. Shared by every European
# market below. A model that is also in the Indian list (SM-A155F, SM-S911B)
# is the same row on purpose — it IS the same handset.
DEVICES_INTL = (
    ("Samsung Galaxy A54 5G",  "samsung",      "SM-A546B",   "a54x",     "s5e8835",  "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A34 5G",  "samsung",      "SM-A346B",   "a34x",     "mt6877",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A53 5G",  "samsung",      "SM-A536B",   "a53x",     "s5e8825",  "1080x2400", "450dpi", 34, "14"),
    ("Samsung Galaxy A15",     "samsung",      "SM-A155F",   "a15",      "mt6789",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy S22",     "samsung",      "SM-S901B",   "r0s",      "s5e9925",  "1080x2340", "480dpi", 34, "14"),
    ("Samsung Galaxy S23",     "samsung",      "SM-S911B",   "dm1q",     "kalama",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy S24",     "samsung",      "SM-S921B",   "e1s",      "s5e9945",  "1080x2340", "480dpi", 34, "14"),
    ("Redmi Note 12 5G",       "Xiaomi/Redmi", "22111317G",  "sunstone", "qcom",     "1080x2400", "440dpi", 33, "13"),
    ("Redmi Note 13 Pro 5G",   "Xiaomi/Redmi", "2312DRA50G", "garnet",   "qcom",     "1220x2712", "480dpi", 34, "14"),
    ("POCO X5 Pro 5G",         "Xiaomi/POCO",  "22101320G",  "redwood",  "qcom",     "1080x2400", "440dpi", 33, "13"),
    ("Google Pixel 7",         "Google/google", "Pixel 7",   "panther",  "panther",  "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 7a",        "Google/google", "Pixel 7a",  "lynx",     "lynx",     "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 8",         "Google/google", "Pixel 8",   "shiba",    "shiba",    "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 8a",        "Google/google", "Pixel 8a",  "akita",    "akita",    "1080x2400", "420dpi", 34, "14"),
)

# Phones sold in the United States: Samsung's unlocked "U1" models and
# Google's own. (Never "Pixel 8 Pro": that is instagrapi's default handset,
# the one every script on earth presents — see LEGACY_MODELS.)
DEVICES_US = (
    ("Samsung Galaxy A54 5G",  "samsung",      "SM-A546U1",  "a54x",     "s5e8835",  "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy A53 5G",  "samsung",      "SM-A536U1",  "a53x",     "s5e8825",  "1080x2400", "450dpi", 34, "14"),
    ("Samsung Galaxy A15 5G",  "samsung",      "SM-A156U1",  "a15x",     "mt6835",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy S22",     "samsung",      "SM-S901U1",  "r0q",      "taro",     "1080x2340", "480dpi", 34, "14"),
    ("Samsung Galaxy S23",     "samsung",      "SM-S911U1",  "dm1q",     "kalama",   "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy S23 FE",  "samsung",      "SM-S711U1",  "r11q",     "taro",     "1080x2340", "450dpi", 34, "14"),
    ("Samsung Galaxy S24",     "samsung",      "SM-S921U1",  "e1q",      "pineapple", "1080x2340", "480dpi", 34, "14"),
    ("Google Pixel 6a",        "Google/google", "Pixel 6a",  "bluejay",  "bluejay",  "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 7",         "Google/google", "Pixel 7",   "panther",  "panther",  "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 7a",        "Google/google", "Pixel 7a",  "lynx",     "lynx",     "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 8",         "Google/google", "Pixel 8",   "shiba",    "shiba",    "1080x2400", "420dpi", 34, "14"),
    ("Google Pixel 8a",        "Google/google", "Pixel 8a",  "akita",    "akita",    "1080x2400", "420dpi", 34, "14"),
)

# One catalogue per retail region. A market names the set its phones come from.
DEVICE_SETS = {"india": DEVICES, "intl": DEVICES_INTL, "us": DEVICES_US}

# ---------------------------------------------------------------------------
# markets — the country an account's phone LIVES in (2026-10-03)
# ---------------------------------------------------------------------------
#
# Until 2026-10-03 the market was ONE module constant (India), because every
# proxy exit was Indian. Then static exits were bought in France and the US
# and two accounts were moved onto them: an Indian handset on IST, speaking
# from Paris. @sanaakhtar221 was challenged the next day. A phone must live
# where its IP lives, so the market is now chosen PER ACCOUNT, at mint time,
# and written into that account's device file. Nothing here can change a
# phone that already exists — a device file holds its own copy of every value.
#
# TO ADD A COUNTRY: add one row. That is the whole change — the dashboard's
# country picker, the proxy check and the collector's clock all read this
# table. validate_markets() (run by the tests) refuses a row that is
# incomplete, names a time zone this machine does not know, or points at an
# empty phone list.
#
#   name             what the dashboard shows
#   locale           the PHONE's language_REGION. English everywhere, on
#                    purpose: Instagram answers in the phone's language, and
#                    both instagrapi and this project recognise a rate limit
#                    or a challenge by its ENGLISH wording. A French-language
#                    phone would turn "Please wait a few minutes" into a
#                    sentence nothing here can classify. An English-language
#                    phone living in France is an ordinary thing to be.
#   country_code     the dialling code instagrapi sends
#   web_locale       BCP-47, for the browser
#   accept_language  the browser's header — the local language rides second
#   zones            IANA time zones; the FIRST is the default. More than one
#                    only where the country spans several (the US); the mint
#                    then takes the zone of the proxy's exit when it is known.
#   devices          a key of DEVICE_SETS
#
# The UTC offset is never typed here: it is read from the zone, so daylight
# saving is right on the day it changes (see live_offset).
MARKETS = {
    "IN": {"name": "India", "locale": "en_IN", "country_code": 91,
           "web_locale": "en-IN", "accept_language": "en-IN,en;q=0.9,hi;q=0.8",
           "zones": ("Asia/Kolkata",), "devices": "india"},
    "FR": {"name": "France", "locale": "en_GB", "country_code": 33,
           "web_locale": "en-GB", "accept_language": "en-GB,en;q=0.9,fr;q=0.8",
           "zones": ("Europe/Paris",), "devices": "intl"},
    "DE": {"name": "Germany", "locale": "en_GB", "country_code": 49,
           "web_locale": "en-GB", "accept_language": "en-GB,en;q=0.9,de;q=0.8",
           "zones": ("Europe/Berlin",), "devices": "intl"},
    "GB": {"name": "United Kingdom", "locale": "en_GB", "country_code": 44,
           "web_locale": "en-GB", "accept_language": "en-GB,en;q=0.9",
           "zones": ("Europe/London",), "devices": "intl"},
    "US": {"name": "United States", "locale": "en_US", "country_code": 1,
           "web_locale": "en-US", "accept_language": "en-US,en;q=0.9",
           "zones": ("America/New_York", "America/Chicago", "America/Denver",
                     "America/Los_Angeles", "America/Phoenix"),
           "devices": "us"},
}
DEFAULT_COUNTRY = "IN"

# Offsets for when the zone database is missing (a stripped container). Only
# the zones named above; standard time. live_offset() prefers the real thing.
_FALLBACK_OFFSETS = {"Asia/Kolkata": 19800, "Europe/Paris": 3600,
                     "Europe/Berlin": 3600, "Europe/London": 0,
                     "America/New_York": -18000, "America/Chicago": -21600,
                     "America/Denver": -25200, "America/Los_Angeles": -28800,
                     "America/Phoenix": -25200}


class UnknownMarket(ValueError):
    """Asked to mint a phone for a country that has no row in MARKETS."""


def known_market(country) -> bool:
    return str(country or "").strip().upper() in MARKETS


def market(country=None) -> tuple:
    """(code, row) for a country code. None/'' means the default market.
    An unknown code is REFUSED, never quietly turned into India: a phone in
    the wrong country is the fault this table exists to prevent."""
    cc = str(country or DEFAULT_COUNTRY).strip().upper()
    if cc not in MARKETS:
        raise UnknownMarket(
            f"no phones for country '{cc}' — known: {', '.join(sorted(MARKETS))}. "
            f"Add a row to ig_identity.MARKETS to support it.")
    return cc, MARKETS[cc]


def utc_offset(zone: str, now=None) -> int | None:
    """Seconds east of UTC for an IANA zone RIGHT NOW (daylight saving
    included). None when the zone is unknown to this machine and to the
    fallback table."""
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        t = (_dt.datetime.fromtimestamp(now, _dt.timezone.utc) if now is not None
             else _dt.datetime.now(_dt.timezone.utc))
        off = t.astimezone(ZoneInfo(zone)).utcoffset()
        if off is not None:
            return int(off.total_seconds())
    except Exception:
        pass
    return _FALLBACK_OFFSETS.get(zone)


def pick_zone(country=None, zone: str = "") -> str:
    """The time zone a phone in `country` is set to: `zone` when the market
    lists it (a US exit in Chicago), else the market's first."""
    _, mk = market(country)
    return zone if zone in mk["zones"] else mk["zones"][0]


def market_values(country=None, zone: str = "", now=None) -> dict:
    """Everything a device file records about its country, for one market."""
    cc, mk = market(country)
    tz = pick_zone(cc, zone)
    off = utc_offset(tz, now)
    return {"locale": mk["locale"], "country": cc,
            "country_code": mk["country_code"],
            "timezone_offset": _FALLBACK_OFFSETS.get(tz, 0) if off is None else off,
            "timezone_name": tz, "web_locale": mk["web_locale"],
            "accept_language": mk["accept_language"]}


# The default market's values, under the name the rest of the project (and
# every seed minted before 2026-10-03) knows them by. Fallbacks only: a device
# file's own values always win.
MARKET = market_values(DEFAULT_COUNTRY)


def markets_public() -> list:
    """What the dashboard's country picker needs. No device internals."""
    out = []
    for cc, mk in MARKETS.items():
        out.append({"country": cc, "name": mk["name"], "locale": mk["locale"],
                    "zones": list(mk["zones"]),
                    "phones": len(DEVICE_SETS.get(mk["devices"]) or ()),
                    "default": cc == DEFAULT_COUNTRY})
    return sorted(out, key=lambda m: (not m["default"], m["name"]))


def validate_markets() -> list:
    """Every reason the tables above are not safe to mint from. Empty = good.
    Run by the tests, so a half-typed new country fails there and not at 2am
    in front of Instagram."""
    bad = []
    need = ("name", "locale", "country_code", "web_locale", "accept_language",
            "zones", "devices")
    if DEFAULT_COUNTRY not in MARKETS:
        bad.append(f"DEFAULT_COUNTRY {DEFAULT_COUNTRY} has no row")
    for cc, mk in MARKETS.items():
        if not (len(cc) == 2 and cc.isalpha() and cc.isupper()):
            bad.append(f"{cc}: the key must be an ISO-2 country code in capitals")
        for k in need:
            if not mk.get(k):
                bad.append(f"{cc}: '{k}' is missing")
        if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", str(mk.get("locale") or "")):
            bad.append(f"{cc}: locale must look like en_GB")
        elif not mk["locale"].startswith("en_"):
            bad.append(f"{cc}: locale must be English (en_XX) — Instagram's "
                       f"errors are recognised by their English wording")
        for z in mk.get("zones") or ():
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(z)
            except Exception:
                if z not in _FALLBACK_OFFSETS:
                    bad.append(f"{cc}: time zone '{z}' is not one this machine knows")
        rows = DEVICE_SETS.get(mk.get("devices"))
        if not rows:
            bad.append(f"{cc}: phone list '{mk.get('devices')}' is missing or empty")
    for key, rows in DEVICE_SETS.items():
        seen = set()
        for row in rows:
            if len(row) != 9:
                bad.append(f"{key}: a phone row needs 9 fields: {row[:1]}")
                continue
            name, manu, model, dev, cpu, res, dpi, api, rel = row
            if not all((name, manu, model, dev, cpu)):
                bad.append(f"{key}: {name or '?'} has an empty field")
            if not re.fullmatch(r"\d{3,4}x\d{3,4}", str(res)) \
                    or not re.fullmatch(r"\d{3}dpi", str(dpi)):
                bad.append(f"{key}: {name} has a malformed resolution or dpi")
            if not isinstance(api, int) or not str(rel).isdigit():
                bad.append(f"{key}: {name} has a malformed Android version")
            if model in LEGACY_MODELS:
                bad.append(f"{key}: {name} is the library's default handset")
            if model in seen:
                bad.append(f"{key}: {model} is listed twice")
            seen.add(model)
    return bad


# How likely each app build is: people mostly run the latest, a tail lags.
# Keyed by app_version; must exist in instagrapi's APP_SETTINGS or it is
# ignored (see app_builds()).
# NOT USED FOR MINTING since 2026-10-04: Instagram refuses a login from an old
# build, so a new phone always gets newest_build(). Kept because app_builds()
# is also how the list of shipped builds is read.
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


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def newest_build() -> dict:
    """The NEWEST app build instagrapi ships — the only one a sign-in may use.

    WHY (2026-10-04). Minting used to draw a build by BUILD_WEIGHTS so that
    accounts differed: 60% the newest, 40% one of two older ones. Instagram
    refuses a LOGIN from an old build outright — "Your version of Instagram is
    out of date. Please upgrade your app to log in." — so four in ten new
    phones could never sign in (@youssefnasser168 and @saieemanjrekar.fc both
    drew 385.0.0.47.74 on 2026-10-03). An existing session on an old build
    keeps working; it is the login that is refused. Variety across accounts
    was never worth a phone that cannot log in, and a real phone's app is
    current anyway."""
    return max((b for _, b in app_builds()), key=lambda b: _ver(b["app_version"]))


def refresh_app_build(device: dict) -> dict | None:
    """Bring an EXISTING seed's Instagram app up to newest_build(), touching
    nothing that identifies the handset — the app updating itself on the same
    phone, which is what real phones do every couple of weeks.

    `uuids`, the model and the rest of `device_settings` are the identity and
    do not move. Only app_version / version_code / bloks_versioning_id change,
    and `user_agent` is re-formatted from them (it embeds the version).
    Returns a NEW device dict when the build moved, None when it is already
    current or the seed is a legacy one (that wants a reseed)."""
    if not device or not device.get("identity"):
        return None
    ds = dict(device.get("device_settings") or {})
    new = newest_build()
    if _ver(ds.get("app_version")) >= _ver(new["app_version"]):
        return None
    ds.update(new)
    out = dict(device)
    out["device_settings"] = ds
    out["user_agent"] = app_user_agent(ds, device.get("locale") or MARKET["locale"])
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

def mint(label: str, *, rng=None, chrome=None, taken=(), country=None,
         timezone: str = "") -> dict:
    """
    A brand-new coherent identity for `label`, as the dict ig_session stores
    under "device" and splices over every instagrapi settings dict.

    `taken` is the set of catalogue model strings other accounts on this
    server already use; the draw avoids them while it can (uniqueness), and
    only repeats a model once the catalogue is exhausted.

    `country` picks the market (MARKETS) the phone lives in — its catalogue,
    language, dialling code and time zone. Left out, it is the default market
    (India), which is what every caller did before markets existed. An unknown
    country raises UnknownMarket. `timezone` is honoured only when that market
    lists it (pick_zone).
    """
    rng = rng or random.SystemRandom()
    cc, mk = market(country)
    mv = market_values(cc, timezone)
    catalogue = DEVICE_SETS[mk["devices"]]
    avail = [d for d in catalogue if d[2] not in set(taken)] or list(catalogue)
    name, manu, model, dev, cpu, res, dpi, api, rel = rng.choice(avail)
    build = dict(newest_build())      # the only build that can LOG IN
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
        "user_agent": app_user_agent(device_settings, mv["locale"]),
        "country": mv["country"],
        "country_code": mv["country_code"],
        "locale": mv["locale"],
        "timezone_offset": mv["timezone_offset"],
        "timezone_name": mv["timezone_name"],
        # Ours, not instagrapi's: carried in the device file, read by
        # engine_ig._browser_session and ig.InteractiveLogin.
        "web_user_agent": WEB_UA.format(major=major),
        "identity": {
            "version": 1,
            "label": label,
            "name": name,
            "chrome_major": major,
            "market": cc,
            "web_locale": mv["web_locale"],
            "accept_language": mv["accept_language"],
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


def country_of(device: dict) -> str:
    """The ISO-2 country this phone says it is in ('' for no device)."""
    return str((device or {}).get("country") or "").strip().upper()


def country_name(device: dict) -> str:
    """'France' for a phone minted for France; the bare code for a country
    with no row (a seed from somewhere this table has never heard of)."""
    cc = country_of(device)
    return (MARKETS.get(cc) or {}).get("name") or cc


def expected_country(device: dict) -> str:
    """The country this account's proxy exit SHOULD be in: its own phone's.
    No device yet -> the default market."""
    return country_of(device) or DEFAULT_COUNTRY


def live_offset(device: dict, now=None) -> int | None:
    """The phone's UTC offset RIGHT NOW, from its time zone name.

    The device file stores the offset of the day it was minted. India never
    changes its clocks, so that number was always right; Paris and New York
    change twice a year, and a phone that keeps summer time into November is
    an hour out from the IP it speaks through. The zone is the identity, the
    offset is derived from it — like the Chrome major, not an identifier.
    None when the device names no zone (a legacy seed): leave it alone."""
    tz = (device or {}).get("timezone_name") or ""
    return utc_offset(tz, now) if tz else None


def describe(device: dict) -> str:
    """One line for the dashboard: what phone this account is."""
    if not device:
        return "no device minted yet"
    ds = device.get("device_settings") or {}
    ident = device.get("identity") or {}
    name = ident.get("name") or f"{ds.get('manufacturer', '?')} {ds.get('model', '?')}"
    bits = [name] + ([country_name(device)] if country_name(device) else []) + [
            f"Android {ds.get('android_release', '?')}",
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
        "country_name": country_name(device),
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


def refresh_web_browser(device: dict, *, chrome=None) -> dict | None:
    """Bring the DERIVED browser version on an EXISTING seed up to the binary
    this machine actually has, touching nothing that identifies the account.

    WHY THIS IS SAFE WHERE A RESEED IS NOT. `uuids` and `device_settings` ARE
    the identity: Instagram has seen them, they are what "the same handset"
    means, and they must never move outside a deliberate reseed. The Chrome
    major is not an identifier. It is a fact about the browser that will render
    the sign-in window, and on a real phone Chrome updates itself every few
    weeks without the handset changing at all.

    THE BUG THIS CLOSES (CHECKPOINT 2026-09-06, filed under "leave it"): seeds
    minted before Chromium 151 was installed still carry `chrome_major: 140`,
    the env fallback. So `web_headers`, `playwright_kwargs` and
    `cdp_user_agent_metadata` all say 140 — in the UA string and in every
    Client Hint — while the engine actually rendering the page is 151. The
    string and the thing behind it disagree, which is precisely the incoherence
    this module exists to prevent, and a frozen browser version across a Chrome
    release cycle is a stranger signal than a version that moves.

    Returns a NEW device dict when the major moved, None when there is nothing
    to do (unchanged, or a legacy seed with no identity block — that wants a
    reseed, not a bump).
    """
    if not device or not device.get("identity"):
        return None
    now = str(chrome or chrome_major())
    was = str((device.get("identity") or {}).get("chrome_major") or "")
    if not now.isdigit() or now == was:
        return None
    out = dict(device)
    out["identity"] = dict(device["identity"], chrome_major=now)
    out["web_user_agent"] = WEB_UA.format(major=now)
    return out


def stable_offset(username: str, span_h: float = 1.5) -> float:
    """A per-account shift, in hours, for the active-hours window: derived
    from the handle so it is the same every day (a person's habits) and
    different per account (three phones do not wake at 07:00:00 together)."""
    h = int(hashlib.sha1((username or "").lower().encode()).hexdigest()[:8], 16)
    return (h / 0xFFFFFFFF) * 2 * span_h - span_h
