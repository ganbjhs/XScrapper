"""
fb_probe.py — logged-in Facebook render test (headless Chrome + residential proxy).

Runs on the SERVER (needs Playwright chromium — the same one the X login uses).
Loads a public Page as the logged-in burner account, lets Facebook's JavaScript
render, and prints the post text it can see, plus a screenshot — so we can
confirm the browser approach and see the post structure BEFORE building
engine_fb. Blocks images / video / fonts so a render costs far less bandwidth.

Nothing in the live system imports this; it is a throwaway probe.

Usage (on the server):
  export WEBSHARE_USER=moflqejy-in-1
  export WEBSHARE_PASS=jx9i38zp6za3
  export FB_C_USER=61552330170395
  export FB_XS='5%3A...the xs value...'
  python3 fb_probe.py narendramodi
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

PAGE = sys.argv[1] if len(sys.argv) > 1 else "narendramodi"
GATEWAY = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
USER = os.getenv("WEBSHARE_USER", "")
PWD = os.getenv("WEBSHARE_PASS", "")
C_USER = os.getenv("FB_C_USER", "")
XS = os.getenv("FB_XS", "")

# Don't spend bandwidth on things we never keep — media bytes dwarf the text.
BLOCK = {"image", "media", "font"}


async def main():
    bytes_seen = {"n": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=({"server": f"http://{GATEWAY}", "username": USER, "password": PWD}
                   if USER else None),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"),
            viewport={"width": 1280, "height": 2200},
            locale="en-US",
        )
        await ctx.add_cookies([
            {"name": "c_user", "value": C_USER, "domain": ".facebook.com", "path": "/"},
            {"name": "xs", "value": XS, "domain": ".facebook.com", "path": "/"},
        ])

        async def route(r):
            if r.request.resource_type in BLOCK:
                await r.abort()
            else:
                await r.continue_()
        await ctx.route("**/*", route)

        # rough bandwidth tally
        async def on_response(resp):
            try:
                cl = resp.headers.get("content-length")
                if cl:
                    bytes_seen["n"] += int(cl)
            except Exception:
                pass
        ctx.on("response", on_response)

        page = await ctx.new_page()
        url = f"https://www.facebook.com/{PAGE}"
        print(f"loading {url} …")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)          # let the feed render

        title = await page.title()
        arts = await page.query_selector_all('[role="article"]')
        print(f"\npage={PAGE}  title={title!r}  articles_found={len(arts)}  "
              f"~bytes={bytes_seen['n']//1024} KB")

        for i, a in enumerate(arts[:5]):
            try:
                txt = (await a.inner_text())[:300].replace("\n", " ").strip()
            except Exception:
                txt = "(could not read)"
            print(f"\n--- post {i+1} ---\n{txt}")

        shot = f"fb_{PAGE}.png"
        await page.screenshot(path=shot, full_page=False)
        print(f"\nscreenshot saved: {shot}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
