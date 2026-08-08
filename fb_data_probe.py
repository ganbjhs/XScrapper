"""
fb_data_probe.py — find Facebook's DATA request (the lean path).

The browser render is heavy because it downloads the whole app. But the posts
themselves arrive in a small GraphQL/JSON response. This probe loads a page
once, watches every network response, and reports which ones actually carry
posts — their SIZE (the lean number we care about) and the request recipe
(friendly name / doc_id / that it's a POST with fb_dtsg) so we can later
replay just that call directly, no browser, X-style.

Run on the SERVER. Test TWICE:
  1) with the proxy (env WEBSHARE_USER set) — through residential IP
  2) with no proxy (unset WEBSHARE_USER) — to see if a logged-in session works
     straight from the server's own big bandwidth, freeing the 1 GB pool

  export FB_C_USER=...  FB_XS='...'
  export WEBSHARE_USER=moflqejy-in-1 WEBSHARE_PASS=jx9i38zp6za3   # run 1
  python3 fb_data_probe.py narendramodi
  unset WEBSHARE_USER                                            # run 2
  python3 fb_data_probe.py narendramodi
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

GATEWAY = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
USER, PWD = os.getenv("WEBSHARE_USER", ""), os.getenv("WEBSHARE_PASS", "")
C_USER, XS = os.getenv("FB_C_USER", ""), os.getenv("FB_XS", "")
BLOCK = {"image", "media", "font"}

# Signals that a response body actually contains posts.
POST_MARKERS = ("creation_time", "comet_sections", '"message":', "story_id",
                "feedback", "wwwURL")


async def main(handle):
    total = {"all": 0, "gql": 0}
    hits = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=({"server": f"http://{GATEWAY}", "username": USER, "password": PWD}
                   if USER else None),
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            viewport={"width": 1280, "height": 2200}, locale="en-US")
        await ctx.add_cookies([
            {"name": "c_user", "value": C_USER, "domain": ".facebook.com", "path": "/"},
            {"name": "xs", "value": XS, "domain": ".facebook.com", "path": "/"}])

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await ctx.route("**/*", route)

        captured = []

        async def on_resp(resp):
            url = resp.url
            try:
                body = await resp.body()
            except Exception:
                body = b""
            n = len(body)
            total["all"] += n
            if "/api/graphql" in url or "/graphql" in url:
                total["gql"] += n
                text = body[:200000].decode("utf-8", "ignore")
                if any(m in text for m in POST_MARKERS):
                    req = resp.request
                    pd = req.post_data or ""
                    name = ""
                    for kv in pd.split("&"):
                        if kv.startswith("fb_api_req_friendly_name="):
                            name = kv.split("=", 1)[1]
                        if kv.startswith("doc_id="):
                            name += " doc_id=" + kv.split("=", 1)[1]
                    captured.append((n, name or "(graphql)", url.split("?")[0]))
        ctx.on("response", on_resp)

        page = await ctx.new_page()
        print(f"loading {handle} via {'proxy' if USER else 'SERVER IP (no proxy)'} …")
        await page.goto(f"https://www.facebook.com/{handle}",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(4000)
        await browser.close()

    print(f"\ntotal downloaded: {total['all']//1024} KB "
          f"(of that, graphql: {total['gql']//1024} KB)")
    print(f"post-bearing data responses: {len(captured)}")
    for n, name, path in sorted(captured, reverse=True)[:8]:
        print(f"  {n//1024:5d} KB  {name[:60]}")
    if captured:
        biggest = sorted(captured, reverse=True)[0][0]
        print(f"\n→ the posts arrive in ~{biggest//1024} KB of JSON. "
              f"At 1 GB that's ~{1_000_000 // max(1, biggest//1024)} page-loads "
              f"IF we replay just this call (no browser).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "narendramodi"))
