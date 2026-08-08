"""
engine_fb.py — Facebook collection via ONE warm headless browser + session.

The transport proven by fb_probe.py, built for real use:
  * a single browser/context is opened once and REUSED across pages, so
    Facebook's JS app caches and every page after the first is far cheaper on
    bandwidth (the 1 GB/month constraint);
  * images / video / fonts are blocked — we keep media URLs, never the bytes;
  * each page is rendered, then structured posts are extracted from the DOM.

Still standalone — nothing in the live system imports it. Run on the SERVER
(needs Playwright chromium + the burner session). This version is a probe:
it loads N pages with ONE warm browser, prints structured posts as JSON, and
reports cold-vs-warm bandwidth so we can size the 1 GB budget honestly.

  export WEBSHARE_USER=moflqejy-in-1  WEBSHARE_PASS=...
  export FB_C_USER=...  FB_XS='...'
  python3 engine_fb.py narendramodi rajnathsingh
"""

import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

GATEWAY = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
USER, PWD = os.getenv("WEBSHARE_USER", ""), os.getenv("WEBSHARE_PASS", "")
C_USER, XS = os.getenv("FB_C_USER", ""), os.getenv("FB_XS", "")
BLOCK = {"image", "media", "font"}

# In-page extractor. Pulls candidate posts from the rendered DOM. Heuristic on
# purpose — refined from what real pages return. Returns author, permalink,
# timestamp, text, a reaction hint, and media image URLs (URLs only).
EXTRACT_JS = r"""
() => {
  const out = [];
  const arts = document.querySelectorAll('[role="article"]');
  for (const a of arts) {
    const links = [...a.querySelectorAll('a[href]')].map(l => l.href);
    // a post permalink on facebook contains one of these
    const perma = links.find(h => /\/posts\/|\/videos\/|story\.php|\/permalink\/|\/photos\//.test(h)) || null;
    // author = first profile/page link near the top
    const authorEl = a.querySelector('h2 a, h3 a, h4 a, strong a, a[role="link"]');
    const author = authorEl ? authorEl.innerText.trim() : null;
    const authorHref = authorEl ? authorEl.href : null;
    // body text: the message blocks
    const bodies = [...a.querySelectorAll('div[dir="auto"]')].map(d => d.innerText.trim()).filter(Boolean);
    const text = bodies.sort((x,y)=>y.length-x.length)[0] || "";
    // media: scontent images (post photos), skip tiny avatars via srcset presence
    const imgs = [...a.querySelectorAll('img')].map(i => i.src).filter(s => /scontent|fbcdn/.test(s));
    out.push({ author, authorHref, permalink: perma, text: text.slice(0, 500),
               media: [...new Set(imgs)].slice(0, 4), links_sample: links.slice(0,3) });
  }
  return out;
}
"""


async def fetch_pages(pages):
    stats = {"cold_kb": 0, "warm_kb": 0}
    tally = {"n": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=({"server": f"http://{GATEWAY}", "username": USER, "password": PWD}
                   if USER else None),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            viewport={"width": 1280, "height": 2400}, locale="en-US")
        await ctx.add_cookies([
            {"name": "c_user", "value": C_USER, "domain": ".facebook.com", "path": "/"},
            {"name": "xs", "value": XS, "domain": ".facebook.com", "path": "/"}])

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await ctx.route("**/*", route)

        async def on_resp(resp):
            try:
                cl = resp.headers.get("content-length")
                if cl:
                    tally["n"] += int(cl)
            except Exception:
                pass
        ctx.on("response", on_resp)

        page = await ctx.new_page()
        results = {}
        for idx, handle in enumerate(pages):
            tally["n"] = 0
            await page.goto(f"https://www.facebook.com/{handle}",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(6000)
            await page.mouse.wheel(0, 4000)          # nudge lazy posts in
            await page.wait_for_timeout(3000)
            posts = await page.evaluate(EXTRACT_JS)
            kb = tally["n"] // 1024
            if idx == 0:
                stats["cold_kb"] = kb
            else:
                stats["warm_kb"] = max(stats["warm_kb"], kb)
            results[handle] = posts
            print(f"\n===== {handle}  ({len(posts)} candidates, {kb} KB "
                  f"[{'cold' if idx == 0 else 'warm'}]) =====")
            for i, p in enumerate(posts[:6]):
                print(f"[{i}] author={p['author']!r} perma={bool(p['permalink'])} "
                      f"media={len(p['media'])}")
                print(f"    {p['text'][:140]!r}")

        await browser.close()

    with open("fb_posts.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nfull structured output -> fb_posts.json")
    print(f"bandwidth: cold {stats['cold_kb']} KB, warm {stats['warm_kb']} KB per page")
    print(f"→ at 1 GB/month, warm ≈ {1_000_000 // max(1, stats['warm_kb'])} page-loads")


if __name__ == "__main__":
    asyncio.run(fetch_pages(sys.argv[1:] or ["narendramodi"]))
