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
FB_DATR=                 # device cookie — copy from the SAME browser as xs
FB_SB=                   # device cookie — copy from the SAME browser as xs
FB_USE_PROXY=0            # 0 = server IP (recommended, uses 4 TB)
FB_MONTHLY_CAP_GB=200     # the runaway guard
FB_INTERVAL_S=21600       # 6h between checks per page
```

**Capture ALL of these from ONE desktop browser session** (see §1a). `c_user`
and `xs` are required; `datr` and `sb` are the device cookies Facebook expects
to see alongside them. Replaying `xs` *without* `datr` is the single most common
reason a fresh session gets bounced straight to the login wall — which is
exactly the `0 posts` symptom.

Keep this account gentle: it was just created, so let it rest a bit before the
first run, and use it from ONE consistent IP (the server) — hopping IPs is what
gets Facebook accounts checkpointed.

## 1a. Capturing the cookies (the fix for `0 posts` / login-wall)

The `0 posts, 4700 KB` you saw, plus `fb_debug` landing on
`m.facebook.com/?next=…` with the title *"Facebook - log in or sign up"*, means
**Facebook rejected the session and served the login page** — it never reached
any real posts. Two things caused it and both are now handled:

1. **UA mismatch.** The engine had been using a mobile Android UA, but the
   session was proven with a *desktop* UA (`fb_probe.py`). A desktop cookie
   replayed as a phone trips Facebook's device check → login wall. `engine_fb.py`
   and `fb_debug.py` now both use the desktop UA.
2. **Missing device cookies.** `datr`/`sb` were never carried. Add them.

To refresh the session cleanly:

1. On a normal computer, open a **desktop** Chrome/Firefox, log into the burner
   Facebook account, and clear any checkpoint/"was this you?" prompt until you
   land on the normal home feed.
2. Open DevTools → **Application → Cookies → https://www.facebook.com**.
3. Copy the *Value* of each cookie into `.env` (URL-encoded exactly as shown —
   don't decode `%3A`):
   - `c_user`  → `FB_C_USER`
   - `xs`      → `FB_XS`
   - `datr`    → `FB_DATR`
   - `sb`      → `FB_SB`
4. Reload the env and confirm with the diagnostic before the real run:

```
cd /opt/xscraper/app
set -a; . ./.env; set +a
.venv/bin/python3 fb_debug.py narendramodi
```

Look at the last line:
- **`VERDICT: LOGGED IN and rendering`** → cookies are good, go to §3.
- **`VERDICT: SESSION REJECTED`** → still walled; the `xs` is dead or was
  copied from a different browser than `datr`. Redo steps 1–3 in one browser.
- **`VERDICT: logged in but NO post permalinks`** → session is fine but the
  extractor needs a small tune; send me the `permalink_samples` block.

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

If it shows **0 posts**, run `fb_debug.py <page>` first (§1a) to tell the two
causes apart: a `SESSION REJECTED` verdict means the cookies need refreshing
(§1a), not an extractor problem. Only if `fb_debug` says *logged in but no
permalinks* is it the extractor (one function in `engine_fb.py`) — send me the
`permalink_samples` block and I'll tune it.

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
