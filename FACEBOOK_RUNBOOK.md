# Facebook — go live (server runbook)

Facebook is fully integrated (store, engine, collector, API, dashboard). It runs
on the **server's own IP** (uses the 4 TB VPS bandwidth, not the residential
pool), with a **hard monthly byte cap** so it can never run away. These steps
turn it on.

## 1. Put the account in `.env` (on the server)

The robust way is **email + password** — the collector logs in with a real
browser on the server, so Facebook issues a session bound to that browser's own
`datr`, and it saves the whole session to `fb_state.json` and reuses it. No
cookie hand-copying, and no "session expired" logouts.

```
FB_ENABLED=1
FB_EMAIL=the-account@example.com
FB_PASSWORD=the-password
FB_USE_PROXY=0            # 0 = server IP (recommended, uses the 4 TB VPS)
FB_MONTHLY_CAP_GB=200     # the runaway guard
FB_INTERVAL_S=21600       # 6h between checks per page
```

Cookies (`FB_C_USER` / `FB_XS` / `FB_DATR`) are still accepted as an alternative
seed, but they die within a day or two unless `datr` matches — the login path
above avoids that entirely. After the first successful login, `fb_state.json`
is the source of truth; delete it to force a fresh login.

Keep the account gentle: use it from ONE consistent IP (the server). Hopping
IPs, or hammering a fresh account, is what gets Facebook accounts checkpointed.
If a run logs `NOT LOGGED IN` and re-login fails, Facebook is asking for a
one-time "Was this you?" / 2FA confirmation — clear it once by logging into
that account in a normal browser, then delete `fb_state.json` and re-run.

## Why a DESKTOP user-agent (do not change this)

The engine reads each post from the **embedded JSON** Facebook ships in the page
(matched by post type — layout-proof), and falls back to the visible
`role="article"` DOM, then to `mbasic.facebook.com`, if that ever comes up
empty. All of this needs the **desktop** Chrome user-agent: a **mobile** UA gets
the "WebLite / Bloks" shell, which carries neither the post JSON nor real
`role="article"` blocks — nothing to extract. Do not switch it to mobile.

The JSON path also gives us the **profile picture, exact post time, and
reaction/comment/share counts**. If posts ever stop parsing, the "Fetch now" log
prints `json_stories=` / `json_posts=` (did the JSON have posts?) plus
`all_links=` / `containers=` (the DOM shape) — enough to retune quickly.

## 2. Add pages in the dashboard

Project → **Watchlists** → **Facebook pages** → type a page handle (from its
URL, e.g. `narendramodi`) → Add. Pages are project-scoped: each project's feed
and delivery see only its own Facebook pages.

## 3. First run — confirm it collects (from the dashboard)

After adding the `.env` block, **restart the dashboard so it sees the vars**:

```
systemctl restart xscraper-web
```

Then in the dashboard: Project → **Watchlists** → **Facebook pages** →
**Fetch now**. It logs in (first time takes ~a minute), saves the session, and
shows `N new posts` plus a run log. Open **Live Feed → Source: Facebook** — the
posts appear with a blue **f** badge.

CLI alternative (same thing, from a shell):

```
cd /opt/xscraper/app
set -a; . ./.env; set +a
.venv/bin/python3 collect_fb.py run
```

You should see `[fb] <page>: N posts via www, K KB` then `+N new`.

If it shows 0 posts, send me the run output; the extractor (one function in
`engine_fb.py`) may need a small tune against the real page — everything else is
solid.

## 4. Run it continuously

Simplest — a loop:

```
.venv/bin/python3 collect_fb.py run --loop --every 21600
```

Better — its own service (survives reboot). Create
`/etc/systemd/system/xscraper-fb.service`:

```
[Unit]
Description=X Collector — Facebook
After=network-online.target
Wants=network-online.target

[Service]
User=xscraper
Group=xscraper
WorkingDirectory=/opt/xscraper/app
EnvironmentFile=/opt/xscraper/app/.env
ExecStart=/opt/xscraper/app/.venv/bin/python3 collect_fb.py run --loop --every 21600
Restart=always
RestartSec=120

[Install]
WantedBy=multi-user.target
```

Then:

```
systemctl daemon-reload
systemctl enable --now xscraper-fb
```

## What's protected

- **Bandwidth:** every response is counted in `fb_meter.db`; at
  `FB_MONTHLY_CAP_GB` it refuses to fetch — no "1 GB gone in seconds".
- **No media bytes:** images/videos are never downloaded, only their URLs
  (same as X/IG). Watch-Tower / viewers fetch media from Facebook directly.
- **Gentle cadence:** a few checks a day, newest posts only (watermark stop).
- **Account:** one IP, rest between bursts. A checkpoint means logging into
  that account in a normal browser once to clear it, then a fresh `xs` cookie.
