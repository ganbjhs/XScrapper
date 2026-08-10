"""
fb_debug.py — one-shot diagnostic for the Facebook extractor.

Logs in with the SAME cookies/UA/proxy settings as engine_fb.py, opens a page,
and dumps what the rendered DOM actually contains so the extractor can be tuned
to it. Also saves the full rendered HTML to fb_dump.html.

Run on the server:
    cd /opt/xscraper/app
    set -a; . ./.env; set +a
    .venv/bin/python3 fb_debug.py narendramodi
Then send me everything it prints (and, if asked, fb_dump.html).
"""

import asyncio
import os
import sys

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

# Diagnostic: count many possible post containers and show samples, so we can
# see which structure THIS page uses without another round-trip.
_DIAG_JS = r"""
() => {
  const q = (s) => document.querySelectorAll(s).length;
  const sample = (s, n=3) =>
    [...document.querySelectorAll(s)].slice(0, n)
      .map(e => (e.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120));

  // Links that look like a post permalink, from anywhere on the page.
  const permalinks = [...document.querySelectorAll('a[href]')]
    .map(a => a.href)
    .filter(h => /\/posts\/|\/story|story_fbid=|\/videos\/|\/photos\/|\/permalink\/|\/reel\//.test(h));

  // Role histogram — what roles exist and how many.
  const roles = {};
  for (const e of document.querySelectorAll('[role]')) {
    const r = e.getAttribute('role'); roles[r] = (roles[r] || 0) + 1;
  }

  return {
    final_url: location.href,
    title: document.title,
    body_len: document.body ? document.body.innerText.length : 0,
    counts: {
      'role=article': q('[role="article"]'),
      'role=feed': q('[role="feed"]'),
      'div[dir=auto]': q('div[dir="auto"]'),
      'a[href]': q('a[href]'),
      'img': q('img'),
      'img_fbcdn': [...document.querySelectorAll('img')].filter(i => /scontent|fbcdn/.test(i.src)).length,
      'data-ad-preview': q('[data-ad-preview]'),
      'data-pagelet': q('[data-pagelet]'),
      'permalink_links': permalinks.length,
    },
    roles,
    permalink_samples: [...new Set(permalinks)].slice(0, 8),
    pagelets: [...document.querySelectorAll('[data-pagelet]')]
      .map(e => e.getAttribute('data-pagelet')).slice(0, 15),
    // Per-article breakdown: is it inside a popup dialog, its text, and only
    // the post-like links — this is what the real extractor sees.
    articles: [...document.querySelectorAll('[role="article"]')].slice(0, 8).map(a => ({
      inDialog: !!a.closest('[role="dialog"]'),
      text: (a.innerText || "").replace(/\s+/g, " ").trim().slice(0, 150),
      links: [...new Set([...a.querySelectorAll('a[href]')].map(x => x.href)
        .filter(h => /\/posts\/|story|\/reel\/|\/permalink\/|\/videos\/|\/photos\//.test(h)))].slice(0, 4),
    })),
    body_head: (document.body ? document.body.innerText : "").replace(/\s+/g, " ").trim().slice(0, 600),
  };
}
"""


async def probe(url, label):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        use_proxy = os.getenv("FB_USE_PROXY", "0") == "1"
        proxy = None
        if use_proxy:
            gw = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
            proxy = {"server": f"http://{gw}",
                     "username": os.getenv("WEBSHARE_USER", ""),
                     "password": os.getenv("WEBSHARE_PASS", "")}
        browser = await pw.chromium.launch(
            headless=True, proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=MOBILE_UA, viewport={"width": 412, "height": 2400},
            locale="en-US")
        ck = [
            {"name": "c_user", "value": os.getenv("FB_C_USER", ""),
             "domain": ".facebook.com", "path": "/"},
            {"name": "xs", "value": os.getenv("FB_XS", ""),
             "domain": ".facebook.com", "path": "/"}]
        for nm, ev in (("datr", "FB_DATR"), ("sb", "FB_SB")):
            v = os.getenv(ev, "")
            if v:
                ck.append({"name": nm, "value": v,
                           "domain": ".facebook.com", "path": "/"})
        await ctx.add_cookies(ck)
        page = await ctx.new_page()
        print(f"\n===== {label}: {url} =====")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector('[role="article"]', timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, 2500)")
                await page.wait_for_timeout(2500)
            diag = await page.evaluate(_DIAG_JS)
            html = await page.content()
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            await browser.close()
            return

        import json
        print(json.dumps(diag, indent=2, ensure_ascii=False))
        fn = f"fb_dump_{label}.html"
        with open(fn, "w") as f:
            f.write(html)
        print(f"  (full HTML saved to {fn}, {len(html)//1024} KB)")
        await browser.close()


async def main():
    handle = sys.argv[1] if len(sys.argv) > 1 else "narendramodi"
    if not os.getenv("FB_C_USER") or not os.getenv("FB_XS"):
        print("FB_C_USER / FB_XS not set — run `set -a; . ./.env; set +a` first")
        return
    # Try both hosts; whichever renders posts is the one to use.
    await probe(f"https://www.facebook.com/{handle}", "www")
    await probe(f"https://m.facebook.com/{handle}", "m")


if __name__ == "__main__":
    asyncio.run(main())
