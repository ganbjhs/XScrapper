# Deploying to scraper.vedictech.in

Target: `root@200.97.175.12`, served at `https://scraper.vedictech.in`.

Architecture — the dashboard **never** listens on a public port itself:

```
internet ──HTTPS──> nginx :443 ──HTTP──> 127.0.0.1:8765  (python3 main.py serve)
                    (TLS, certbot)        login required
```

nginx holds the certificate and is the only thing exposed. The Python server
binds to loopback, so even a misconfigured firewall cannot expose it directly.

---

## Before you start — read this

**The login is not the whole story.** Two risks remain, and neither is fixed by
a password:

1. **`accounts.db` holds a live `auth_token`.** That cookie is full control of
   the X account — posting, DMs, settings. Putting it on a remote box means
   anyone with root there owns the account. Use a throwaway you can afford to
   lose, and never a personal one.
2. **A datacenter IP is a ban signal.** X treats VPS ranges with more suspicion
   than residential ones. Expect a shorter account life than on your laptop.

Neither blocks deployment. Both are worth knowing before you file the account
under "working".

**Login cannot happen on the server.** It needs a visible Chrome window for the
captcha / 2FA. Log in locally, then copy `accounts.db` and `profiles/` up.

---

## 1. Point DNS

Add an **A record** for `scraper` on `vedictech.in` → `200.97.175.12`.
Verify before continuing (certbot fails without it):

```bash
dig +short scraper.vedictech.in     # must print 200.97.175.12
```

## 2. Server setup

```bash
ssh root@200.97.175.12
```

```bash
apt update && apt install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx

# A dedicated user: the app never needs root, and a compromise of the
# dashboard should not be a compromise of the box.
adduser --system --group --home /opt/xscraper xscraper

git clone https://github.com/ganbjhs/XScrapper.git /opt/xscraper/app
cd /opt/xscraper/app

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

chown -R xscraper:xscraper /opt/xscraper
```

Playwright's browser is **not** installed: the server never logs in, so it
never needs Chrome. That saves ~400 MB and a pile of system libraries.

## 3. Configure

```bash
cd /opt/xscraper/app
cp config.toml.example config.toml
cp .env.example .env

python3 -c "import secrets; print('DASH_PASSWORD=' + secrets.token_urlsafe(24))"
```

Edit `.env` — set `DASH_USER` and paste that `DASH_PASSWORD`:

```
DASH_USER=tilak
DASH_PASSWORD=<the generated string>
```

Then edit `config.toml` for your accounts and streams.

```bash
chown xscraper:xscraper .env config.toml
chmod 600 .env                 # the X password and dashboard password live here
```

## 4. Copy the session up (from your Mac, not the server)

```bash
cd /Users/tilaktiwari/Downloads/x-search-poc

# Stop any local watcher first so the databases are not mid-write.
sqlite3 accounts.db ".backup /tmp/accounts.db"
sqlite3 results.db  ".backup /tmp/results.db"

scp /tmp/accounts.db /tmp/results.db root@200.97.175.12:/opt/xscraper/app/
scp -r profiles root@200.97.175.12:/opt/xscraper/app/
```

Use `.backup`, not `cp` — both databases run in WAL mode and a plain copy can
land mid-transaction.

```bash
ssh root@200.97.175.12 'chown -R xscraper:xscraper /opt/xscraper/app && chmod 600 /opt/xscraper/app/*.db'
```

## 5. Services

```bash
cp /opt/xscraper/app/deploy/xscraper-web.service /etc/systemd/system/
cp /opt/xscraper/app/deploy/xscraper-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xscraper-web xscraper-watch
systemctl status xscraper-web --no-pager
```

## 6. nginx + TLS

```bash
cp /opt/xscraper/app/deploy/nginx-scraper.conf /etc/nginx/sites-available/scraper
ln -sf /etc/nginx/sites-available/scraper /etc/nginx/sites-enabled/scraper
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

certbot --nginx -d scraper.vedictech.in --redirect --agree-tos -m you@vedictech.in
```

certbot rewrites the config for TLS and installs a renewal timer.

## 7. Firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
ufw status
```

Port 8765 is deliberately **not** opened — nginx reaches it over loopback.

## 8. Verify

```bash
curl -sI https://scraper.vedictech.in | head -1              # 200
curl -s https://scraper.vedictech.in | grep -o 'Sign in'     # login page
curl -s -o /dev/null -w '%{http_code}\n' https://scraper.vedictech.in/api/status   # 401
curl -sI http://200.97.175.12:8765 2>&1 | head -1            # must FAIL to connect
```

That last one matters: the app port must not be reachable from outside.

---

## Operating

```bash
journalctl -u xscraper-watch -f          # live collection log
systemctl restart xscraper-web           # after editing config.toml or .env
sudo -u xscraper /opt/xscraper/app/.venv/bin/python3 main.py guard
sudo -u xscraper /opt/xscraper/app/.venv/bin/python3 main.py doctor
```

**Updating:**

```bash
cd /opt/xscraper/app && git pull
.venv/bin/pip install -r requirements.txt
systemctl restart xscraper-web xscraper-watch
```

**When the session dies** (`doctor` shows DEAD): log in again **on your Mac**,
then re-copy `accounts.db` and `profiles/` as in step 4. The server cannot do
this itself.

**Backups** — `results.db` is the only thing that is not reproducible:

```bash
sudo -u xscraper sqlite3 /opt/xscraper/app/results.db ".backup /tmp/results-$(date +%F).db"
```

---

## Reusing an existing Playwright install

The server does **not** need Playwright for normal collection — login happens
on your Mac. Install it only if you want `login --refresh-only` to run on the
server (silent session refresh, no file copying).

Check what is already there:

```bash
python3 -c "import playwright, sys; print('package:', playwright.__file__)" 2>/dev/null || echo "package: NOT installed"
ls -d ~/.cache/ms-playwright/chromium-* 2>/dev/null || echo "browsers: none for this user"
ls -d /usr/lib/playwright /opt/playwright /ms-playwright 2>/dev/null
find / -maxdepth 4 -type d -name "ms-playwright" 2>/dev/null | head
```

Two separate things, and they are reused differently:

| | reuse how |
|---|---|
| the `playwright` **pip package** | per-venv. Ours is isolated, so install it in ours. |
| the **browser binaries** (~400 MB) | shared. Point `PLAYWRIGHT_BROWSERS_PATH` at the existing directory. |

If another project already downloaded chromium, share the binaries:

```bash
# whatever the find above reported, e.g. /root/.cache/ms-playwright
export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
/opt/xscraper/app/.venv/bin/pip install playwright
/opt/xscraper/app/.venv/bin/python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); print('reusing existing chromium:', b.version); b.close()"
```

Make it permanent for the services:

```bash
mkdir -p /etc/systemd/system/xscraper-web.service.d
cat > /etc/systemd/system/xscraper-web.service.d/playwright.conf <<'CONF'
[Service]
Environment=PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
CONF
systemctl daemon-reload && systemctl restart xscraper-web
```

The `xscraper` user must be able to read that directory:

```bash
chmod -R a+rX /root/.cache/ms-playwright        # or move it somewhere neutral:
# mv /root/.cache/ms-playwright /opt/ms-playwright && chmod -R a+rX /opt/ms-playwright
```

**If it is not installed, or the version does not match:**

```bash
/opt/xscraper/app/.venv/bin/pip install playwright
PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /opt/xscraper/app/.venv/bin/python3 -m playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /opt/xscraper/app/.venv/bin/python3 -m playwright install-deps chromium
chmod -R a+rX /opt/ms-playwright
```

`install-deps` needs root and pulls the system libraries headless chromium
links against. Verify:

```bash
sudo -u xscraper PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
  /opt/xscraper/app/.venv/bin/python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); print('OK', b.version); b.close()"
```

A version mismatch between the pip package and the downloaded browsers shows up
as *"Executable doesn't exist at .../chromium-XXXX"*. The number in that path is
the build the package wants — run `playwright install chromium` in **our** venv
and it fetches that exact build alongside the existing ones.
