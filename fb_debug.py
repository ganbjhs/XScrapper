"""
fb_debug.py — one-shot diagnostic: is our Facebook session actually logged in,
and does the page render posts?

This is the CLEAN rewrite (the server copy got corrupted by a botched heredoc).
It loads a page with the SAME cookies + desktop UA the real engine uses, then
prints exactly what came back: final URL, title, whether we hit the login wall,
how many role=article blocks rendered, and a few permalink samples so you can
see the extractor has something to grab.

Run on the SERVER (needs Playwright chromium):

    cd /opt/xscraper/app
    set -a; . ./.env; set +a
    .venv/bin/python3 fb_debug.py narendramodi

Reads the same env the engine does: FB_C_USER, FB_XS, and (recommended)
FB_DATR, FB_SB, FB_FR. FB_USE_PROXY / WEBSHARE_* honoured too.
"""

import asyncio
import json
import os
import sys

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
BLOCK = {"image", "media", "font"}

_DIAG_JS = r"""
() => {
  const q = (s) => document.querySelectorAll(s).length;
  const permalinks = [...document.querySelectorAll('a[href]')].map(a => a.href)
    .filter(h => /\/posts\/|\/story|story_fbid=|\/videos\/|\/photos\/|\/permalink\/|\/reel\//.test(h));
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
      'img_fbcdn': [...document.querySelectorAll('img')]
        .filter(i => /scontent|fbcdn/.test(i.src)).length,
      'permalink_links': permalinks.length,
    },
    roles,
    permalink_samples: [...new Set(permalinks)].slice(0, 8),
    body_head: (document.body ? document.body.innerText : "").slice(0, 300),
  };
}
"""


def _cookies():
    out = []
    for name, env, http_only in (
            ("c_user", "FB_C_USER", False),
            ("xs",     "FB_XS",     True),
            ("datr",   "FB_DATR",   True),
            ("sb",     "FB_SB",     True),
            ("fr",     "FB_FR",     True)):
        v = os.getenv(env, "").strip()
        if v:
            out.append({"name": name, "value": v, "domain": ".facebook.com",
                        "path": "/", "secure": True, "httpOnly": http_only})
    return out


async def probe(handle):
    from playwright.async_api import async_playwright

    if not os.getenv("FB_C_USER") or not os.getenv("FB_XS"):
        print("FB_C_USER / FB_XS not set — run `set -a; . ./.env; set +a` first")
        return

    have = [n for n in ("FB_C_USER", "FB_XS", "FB_DATR", "FB_SB", "FB_FR")
            if os.getenv(n)]
    print(f"cookies present: {', '.join(have)}")
    if "FB_DATR" not in have:
        print("  (note: FB_DATR missing — sessions replay more reliably with it)")

    use_proxy = os.getenv("FB_USE_PROXY", "0") == "1"
    proxy = None
    if use_proxy:
        gw = os.getenv("WEBSHARE_GATEWAY", "p.webshare.io:80")
        proxy = {"server": f"http://{gw}",
                 "username": os.getenv("WEBSHARE_USER", ""),
                 "password": os.getenv("WEBSHARE_PASS", "")}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 2200},
            locale="en-US")
        await ctx.add_cookies(_cookies())

        async def route(r):
            await (r.abort() if r.request.resource_type in BLOCK else r.continue_())
        await ctx.route("**/*", route)

        page = await ctx.new_page()
        url = f"https://www.facebook.com/{handle}"
        print(f"\n===== {url} (proxy={'on' if use_proxy else 'off'}) =====")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector('[role="article"]', timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        diag = await page.evaluate(_DIAG_JS)
        print(json.dumps(diag, indent=2, ensure_ascii=False))

        final = diag.get("final_url", "")
        title = (diag.get("title") or "").lower()
        walled = ("login" in final or "/?next=" in final or "checkpoint" in final
                  or "log in" in title or "log into" in title)
        arts = diag.get("counts", {}).get("role=article", 0)
        if walled:
            print("\nVERDICT: SESSION REJECTED — Facebook bounced to the login "
                  "wall. The xs cookie is dead or the cookie set is incomplete. "
                  "Refresh FB_C_USER / FB_XS (+ FB_DATR / FB_SB) from a browser "
                  "where the burner is logged in.")
        elif arts > 0 and diag["counts"]["permalink_links"] > 0:
            print(f"\nVERDICT: LOGGED IN and rendering — {arts} article blocks, "
                  f"{diag['counts']['permalink_links']} permalinks. The engine "
                  f"should collect here.")
        elif arts > 0:
            print(f"\nVERDICT: logged in ({arts} articles) but NO post permalinks "
                  f"found — the extractor regex may need a tune for this page.")
        else:
            print("\nVERDICT: logged in but no article blocks rendered — the page "
                  "may have no visible posts, or FB served a different layout. "
                  "Check body_head / roles above.")

        html = await page.content()
        fn = f"fb_dump_{handle}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  (full HTML saved to {fn}, {len(html)//1024} KB)")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(probe(sys.argv[1] if len(sys.argv) > 1 else "narendramodi"))
