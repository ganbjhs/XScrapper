"""
xclid_probe.py — what does X actually serve this box when twscrape asks?

    .venv/bin/python3 tools/xclid_probe.py            # every account in accounts.db
    .venv/bin/python3 tools/xclid_probe.py HanaMal93  # one account
    .venv/bin/python3 tools/xclid_probe.py --anon     # no cookies at all

Read-only. Spends no API budget: it fetches the x.com HTML page that
twscrape's XClIdGen fetches before it can sign any request, and nothing else.

Why this exists. `XClIdParseError: X web scripts not found` means the HTML
that came back from https://x.com/tesla contained neither the current
`/x-web/*.js` asset links nor the legacy webpack chunk map. twscrape cannot
tell you WHICH page it got instead, and the collector only sees "no page, so
starved". This prints the tell-tales: final URL after redirects, <title>,
size, the markers of a challenge / consent / logged-out / error page, and
saves the HTML to /tmp so it can be read. It also fetches with the REAL
browser user-agent stored for the account, because XClIdGen uses twscrape's
random "@chrome" UA rather than the one the cookies were harvested under —
if the real UA gets a normal page and "@chrome" does not, that is the fix.
"""

import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from twscrape import API
from twscrape.http import make_client
from twscrape.xclid import (
    ASSET_URL_RE, INDICES_FILE_RE, LOGGED_OUT_ENTRY_RE, get_tw_page_text,
)

PAGE = "https://x.com/tesla"

MARKERS = {
    "challenge/captcha": re.compile(r"challenge|captcha|arkose|funcaptcha|cf-chl|Just a moment", re.I),
    "JS-required shell": re.compile(r"JavaScript is not available|JavaScript is disabled", re.I),
    "consent/cookie wall": re.compile(r"consent|cookie policy", re.I),
    "login wall": re.compile(r'entry-client-logged-out|href="/login"|Sign in to X', re.I),
    "error page": re.compile(r"Something went wrong|Rate limit exceeded|Try again", re.I),
    "migrate form": re.compile(r"x\.com/x/migrate", re.I),
    "legacy chunk map": re.compile(r'\d+:"[0-9a-f]{7}"'),
}


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else "(no <title>)"


async def fetch(label: str, ua: str, cookies: dict | None, proxy: str | None):
    clt = make_client(headers={"user-agent": ua}, proxy=proxy)
    for k, v in (cookies or {}).items():
        clt.cookies.set(k, v, domain=".x.com")
    try:
        try:
            rep = await clt.get(PAGE)
        except Exception as e:
            print(f"  {label}: transport failed: {type(e).__name__}: {e}")
            return
        print(f"  {label}: HTTP {rep.status_code}, {len(rep.text)} bytes, final url {rep.url}")
        html = rep.text
        try:
            html = await get_tw_page_text(PAGE, clt)   # follows X's redirect/migrate dance
        except Exception as e:
            print(f"    get_tw_page_text (what twscrape runs): {type(e).__name__}: {e}")
        print(f"    title: {_title(html)}")
        assets = list(dict.fromkeys(ASSET_URL_RE.findall(html)))
        print(f"    x-web asset links: {len(assets)}"
              + (f"  e.g. {assets[0]}" if assets else "  <- 'X web scripts not found' when 0 and no chunk map"))
        if assets:
            print(f"    logged-out entry present: {any(LOGGED_OUT_ENTRY_RE.search(u) for u in assets)}")
            print(f"    signing script linked directly: {any(INDICES_FILE_RE.search(u) for u in assets)}")
        hits = [name for name, rx in MARKERS.items() if rx.search(html)]
        print(f"    markers: {', '.join(hits) or 'none'}")
        other_js = sorted(set(re.findall(r'https://[\w.-]+/[\w./-]+\.js', html)) - set(assets))[:3]
        if other_js:
            print(f"    other script hosts seen: {other_js}")
        out = pathlib.Path(f"/tmp/xclid_{re.sub(r'[^A-Za-z0-9]+', '_', label)}.html")
        out.write_text(html)
        print(f"    saved: {out}")
    finally:
        await clt.aclose()


async def main(argv):
    anon = "--anon" in argv
    want = [a for a in argv if not a.startswith("--")]
    print(f"== {PAGE} ==")
    if anon or not want:
        print("[anonymous, @chrome UA]")
        await fetch("anon", "@chrome", None, None)
        if anon:
            return
    api = API("accounts.db")
    accounts = await api.pool.get_all()
    for acc in accounts:
        if want and acc.username not in want:
            continue
        print(f"\n[{acc.username}]  active={acc.active} proxy={'yes' if acc.proxy else 'no'} "
              f"stored UA={'real' if acc.user_agent and not acc.user_agent.startswith('@') else acc.user_agent!r}")
        if not acc.cookies.get("auth_token"):
            print("  (no auth_token cookie — skipped)")
            continue
        await fetch(f"{acc.username} @chrome-UA (what XClIdGen does)", "@chrome", acc.cookies, acc.proxy)
        if acc.user_agent and not acc.user_agent.startswith("@"):
            await fetch(f"{acc.username} real-UA", acc.user_agent, acc.cookies, acc.proxy)
        await sign(acc)


async def sign(acc):
    """
    The end-to-end check: build a real XClIdGen for this account, exactly as
    every API request does, first with twscrape as shipped and then with the
    project's shim (engine.py) installed. Passing here means the collector
    will be able to sign requests after a restart.
    """
    import engine
    from twscrape.xclid import XClIdGen

    async def attempt(label, fn):
        try:
            gen = await XClIdGen.create(proxy=acc.proxy, cookies=acc.cookies)
            hdr = gen.calc("GET", "/i/api/graphql/x/SearchTimeline")
            print(f"  sign {label}: OK  (x-client-transaction-id={hdr[:24]}…)")
            return True
        except Exception as e:
            print(f"  sign {label}: {type(e).__name__}: {e}")
            return False

    # upstream first (temporarily swap the original function back in)
    live = engine._xclid.get_scripts_list
    engine._xclid.get_scripts_list = engine._UPSTREAM_get_scripts_list
    try:
        await attempt("twscrape as shipped", None)
    finally:
        engine._xclid.get_scripts_list = live
    print(f"  shim status: {engine.XCLID_SHIM}")
    await attempt("with engine.py shim", None)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
