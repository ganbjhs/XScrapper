# Facebook — go live (server runbook)

Facebook is fully integrated (store, engine, collector, API, dashboard). It runs
on the **server's own IP** (uses the 4 TB VPS bandwidth, not the residential
pool), with a **hard monthly byte cap** so it can never run away. These steps
turn it on.

## 1. Put the burner session in `.env` (on the server)

```
FB_ENABLED=1
FB_C_USER=61559732085463
FB_XS=47%3AukpoZcMUAFsovQ%3A2%3A1786349252%3A-1%3A-1%3A%3AAcyISzPXFmOt-6Uij4osjeDnUaANJEz1Ygij8FCE7w
FB_USE_PROXY=0            # 0 = server IP (recommended, uses 4 TB)
FB_MONTHLY_CAP_GB=200     # the runaway guard
FB_INTERVAL_S=21600       # 6h between checks per page
```

Keep this account gentle: it was just created, so let it rest a bit before the
first run, and use it from ONE consistent IP (the server) — hopping IPs is what
gets Facebook accounts checkpointed.

## 2. Add pages in the dashboard

Project → **Watchlists** → **Facebook pages** → type a page handle (from its
URL, e.g. `narendramodi`) → Add. Pages are project-scoped: each project's feed
and delivery see only its own Facebook pages.

## 3. First run — confirm it collects

```
cd /opt/xscraper/app
set -a; . ./.env; set +a          # load FB_* into the shell
.venv/bin/python3 collect_fb.py run
```

You should see `[fb] <page>: N posts, K KB` then `+N new`. Open the dashboard →
Live Feed → Source: **Facebook** — the posts appear with a blue **f** badge.

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
