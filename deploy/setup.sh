#!/usr/bin/env bash
#
# Server-side setup. Run as root on the VPS:
#
#     bash /opt/xscraper/app/deploy/setup.sh
#
# Written as a script rather than a paste-able block on purpose: a block pasted
# into an ssh password prompt silently does nothing, and a block that fails
# halfway leaves you guessing which half ran. This is idempotent — run it again
# after a partial failure and it picks up where it left off.
#
# SAFE ON A SHARED SERVER SHARED WITH OTHER SITES. Every name it claims is
# scoped to this app or to its own domain, so it cannot collide with a
# neighbour:
#
#   nginx vhost   /etc/nginx/sites-available/<domain>   (not a generic "scraper")
#   nginx snippet /etc/nginx/snippets/xscraper-app.conf
#   systemd       xscraper-web, xscraper-watch
#   user / paths  xscraper, /opt/xscraper
#   port          the first free one from 8765 up — never one in use
#
# It never touches nginx's default site or any vhost it did not create, never
# restarts a service it does not own, and never takes a port from anything.

set -euo pipefail

APP_DIR=/opt/xscraper/app
APP_USER=xscraper

# Everything below is per-deployment. Override on the command line:
#   DOMAIN=xcollect.example.com CERT_EMAIL=you@example.com bash deploy/setup.sh
DOMAIN=${DOMAIN:-scraper.vedictech.in}
CERT_EMAIL=${CERT_EMAIL:-admin@vedictech.in}

# Loopback port for the dashboard. Leave unset to pick one automatically.
XS_PORT=${XS_PORT:-}

# Headless Chrome, so the dashboard's "Sign in to X" window works on this box.
# ~400 MB. Set SKIP_BROWSER=1 to leave it out and sign in on a laptop instead.
SKIP_BROWSER=${SKIP_BROWSER:-0}

VHOST=/etc/nginx/sites-available/$DOMAIN
SNIPPET=/etc/nginx/snippets/xscraper-app.conf

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok   %s\n' "$*"; }
warn() { printf '   WARN %s\n' "$*"; }
die()  { printf '\n   FAIL %s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

say "checking what is already here"
command -v nginx >/dev/null && ok "nginx present (other sites will not be touched)" || warn "nginx missing"

# Does another site on this box already claim our hostname? Two server blocks
# with the same server_name is a real clash: nginx warns and then silently
# serves whichever it parsed first, so requests land in someone else's app.
#
# Asked via `nginx -T`, which dumps the FULLY RESOLVED config — includes
# expanded, symlinks followed. Grepping /etc/nginx/sites-enabled/ directly does
# not work: every entry there is a symlink, and `grep -r` skips symlinks it
# finds while recursing, so that check silently passes no matter what.
if command -v nginx >/dev/null && nginx -t >/dev/null 2>&1; then
  claims=$(nginx -T 2>/dev/null | grep -cE "^[[:space:]]*server_name([[:space:]]|.*[[:space:]])${DOMAIN//./\\.}[[:space:];]" || true)
  ours=$([ -f "$VHOST" ] && echo 1 || echo 0)
  if [ "${claims:-0}" -gt "$ours" ]; then
    warn "$claims nginx server blocks already name $DOMAIN (expected $ours).
        Find them with:  nginx -T | grep -n '$DOMAIN'
        Two blocks with one name means nginx picks whichever it parsed first."
  else
    ok "no other site claims $DOMAIN"
  fi
fi

say "port"
#
# Pick a loopback port nothing else is using, and NEVER take one that is.
#
# Preference order, so the port stays stable across runs:
#   1. XS_PORT, if you set it
#   2. whatever our own unit is already configured for
#   3. the first free port from 8765 upward
port_free() { ! ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1$"; }
port_is_ours() { systemctl is-active --quiet xscraper-web 2>/dev/null; }

CURRENT=$(sed -n 's/.*--port \([0-9]\+\).*/\1/p' \
          /etc/systemd/system/xscraper-web.service 2>/dev/null | head -1)

if [ -n "$XS_PORT" ]; then
  PORT=$XS_PORT
  port_free "$PORT" || port_is_ours \
    || die "XS_PORT=$PORT is already in use by something else. Pick another."
  ok "using XS_PORT=$PORT"
elif [ -n "$CURRENT" ] && { port_free "$CURRENT" || port_is_ours; }; then
  PORT=$CURRENT
  ok "keeping the port this deployment already uses ($PORT)"
else
  PORT=""
  for p in $(seq 8765 8815); do
    if port_free "$p"; then PORT=$p; break; fi
  done
  [ -n "$PORT" ] || die "no free port between 8765 and 8815"
  [ "$PORT" = "8765" ] && ok "port 8765 is free" \
                       || ok "8765 is taken by another app; using $PORT instead"
fi

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# sqlite3 is for backups; the app itself uses Python's bundled driver.
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 >/dev/null
ok "python3, venv, git, sqlite3"
for p in nginx certbot python3-certbot-nginx; do
  dpkg -s "$p" >/dev/null 2>&1 || apt-get install -y -qq "$p" >/dev/null
done
ok "nginx + certbot"

say "user"
if id "$APP_USER" >/dev/null 2>&1; then
  ok "$APP_USER exists"
else
  adduser --system --group --home /opt/xscraper "$APP_USER"
  ok "created $APP_USER"
fi

say "code"
# The repo is owned by $APP_USER but this script runs as root, which git treats
# as "dubious ownership" and refuses outright. Declaring it safe is correct
# here: root already has full access, so the check protects nothing.
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
  ok "updated $(git -C "$APP_DIR" log --oneline -1)"
else
  git clone -q https://github.com/ganbjhs/XScrapper.git "$APP_DIR"
  ok "cloned $(git -C "$APP_DIR" log --oneline -1)"
fi

say "virtualenv"
[ -x "$APP_DIR/.venv/bin/python3" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "$("$APP_DIR/.venv/bin/python3" -c 'import twscrape,sys;print("python",sys.version.split()[0],"twscrape",twscrape.__name__)' 2>/dev/null || echo installed)"

say "browser"
# The dashboard signs accounts in by running a real Chrome here and streaming
# its screen into the page. Without this, that button reports a missing browser
# and the only way to add an account is on a machine that has one.
#
# Shared, not per-user: the service runs as $APP_USER, this script as root, and
# the default cache path would put the download somewhere $APP_USER cannot read.
#
# The path goes into .env, which is the ONE place every entry point already
# reads — config.load_config() calls load_dotenv() before anything touches a
# browser, so the CLI and the service both get it.
#
# It used to be set only on the systemd unit. The service was therefore fine
# while `main.py doctor --browser` looked in the default cache, found nothing,
# and reported the browser as missing when it was sitting in /opt/ms-playwright
# all along.
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
if [ "$SKIP_BROWSER" = "1" ]; then
  warn "skipped (SKIP_BROWSER=1) — sign accounts in on a machine with a browser"
elif "$APP_DIR/.venv/bin/python3" -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); b.close()" 2>/dev/null; then
  ok "headless chromium already working"
else
  "$APP_DIR/.venv/bin/python3" -m playwright install chromium >/dev/null
  "$APP_DIR/.venv/bin/python3" -m playwright install-deps chromium >/dev/null
  chmod -R a+rX /opt/ms-playwright
  if "$APP_DIR/.venv/bin/python3" -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); print(b.version); b.close()" >/dev/null 2>&1; then
    ok "headless chromium installed"
  else
    warn "chromium did not launch — the dashboard's Sign in to X button will not
        work on this box. Everything else is unaffected; sign in on a laptop and
        copy accounts.db + profiles/ up instead."
  fi
fi
# Recorded in .env so every entry point finds it, not just the service.
if ! grep -q '^PLAYWRIGHT_BROWSERS_PATH=' "$APP_DIR/.env" 2>/dev/null; then
  printf 'PLAYWRIGHT_BROWSERS_PATH=%s\n' /opt/ms-playwright >> "$APP_DIR/.env"
  ok "recorded the browser path in .env"
fi
# The old per-service override is now redundant and is a second place for the
# two to disagree.
rm -f /etc/systemd/system/xscraper-web.service.d/playwright.conf
rmdir /etc/systemd/system/xscraper-web.service.d 2>/dev/null || true

say "config"
cd "$APP_DIR"
[ -f config.toml ] || { cp config.toml.example config.toml; ok "created config.toml"; }
[ -f .env ]        || { cp .env.example .env;               ok "created .env"; }

# Fill in a real dashboard password if it is still the placeholder. Doing it
# here means the operator cannot forget and leave the shipped value in place —
# `serve` would refuse to start, but silently generating one is friendlier.
if grep -qE '^DASH_PASSWORD=(CHANGE_ME_TO_A_LONG_RANDOM_STRING)?$' .env; then
  GEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
  sed -i "s|^DASH_PASSWORD=.*|DASH_PASSWORD=$GEN|" .env
  ok "generated DASH_PASSWORD"
  NEW_PW="$GEN"
fi
if grep -qE '^DASH_USER=(changeme)?$' .env; then
  sed -i "s|^DASH_USER=.*|DASH_USER=admin_$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
  ok "generated DASH_USER"
fi
chmod 600 .env
chown -R "$APP_USER:$APP_USER" /opt/xscraper
ok "permissions set (.env is 600)"

say "ownership"
# git pull ran as root, so anything new is root-owned. The service runs as
# $APP_USER and would fail to read it — silently, on the next restart.
chown -R "$APP_USER:$APP_USER" /opt/xscraper
chmod 600 "$APP_DIR/.env"
[ -f "$APP_DIR/accounts.db" ] && chmod 600 "$APP_DIR"/*.db 2>/dev/null || true
ok "re-owned to $APP_USER after pull"

say "systemd"
# The web unit carries the chosen port, so it is rendered rather than copied.
sed "s|__PORT__|$PORT|g" deploy/xscraper-web.service > /etc/systemd/system/xscraper-web.service
cp deploy/xscraper-watch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q xscraper-web xscraper-watch
systemctl restart xscraper-web
sleep 2
if systemctl is-active --quiet xscraper-web; then
  ok "xscraper-web running on 127.0.0.1:$PORT"
else
  journalctl -u xscraper-web -n 20 --no-pager
  die "xscraper-web did not start — log above"
fi
# The watcher is left STOPPED: it needs accounts.db, which only exists after
# you copy a session up from the machine where you logged in.
systemctl stop xscraper-watch 2>/dev/null || true
ok "xscraper-watch enabled but stopped (needs accounts.db first)"

say "nginx"
# Only ever writes its own files. Other sites on this box are none of its
# business, and `rm sites-enabled/default` on a shared server is how you take
# down someone else's site by accident.

# Migration: earlier versions installed a vhost called plainly "scraper". Left
# in place it would be a SECOND server block claiming $DOMAIN — nginx warns
# about the duplicate and silently serves whichever it parsed first. Retire it.
if [ -e /etc/nginx/sites-available/scraper ] && [ "$VHOST" != "/etc/nginx/sites-available/scraper" ]; then
  if grep -q "server_name[[:space:]].*$DOMAIN" /etc/nginx/sites-available/scraper 2>/dev/null; then
    # Carry over certbot's TLS work rather than throwing it away and re-issuing.
    [ -f "$VHOST" ] || { mv /etc/nginx/sites-available/scraper "$VHOST"; ok "renamed the old 'scraper' vhost to $DOMAIN"; }
    rm -f /etc/nginx/sites-available/scraper /etc/nginx/sites-enabled/scraper
    ok "retired the old generically-named vhost"
  fi
fi

# The half that always gets rewritten: proxy target, port, headers. No server
# or listen directive lives in here, so certbot's TLS lines cannot be affected.
mkdir -p /etc/nginx/snippets
sed "s|__PORT__|$PORT|g" deploy/nginx-app.conf > "$SNIPPET"
ok "proxy snippet -> 127.0.0.1:$PORT"

# The half certbot owns. WRITTEN ONCE, NEVER OVERWRITTEN.
#
# The previous version copied this file on every run, which deleted the TLS
# block certbot had added. The site then kept answering on port 80 while HTTPS
# fell through to another app's server block and presented THAT app's
# certificate — a name-mismatch error in every browser, on a domain that looked
# perfectly healthy over HTTP. Re-running the deploy script must never be able
# to turn HTTPS off.
if [ -f "$VHOST" ]; then
  ok "vhost already exists — left alone (certbot owns its TLS block)"
else
  sed "s|__DOMAIN__|$DOMAIN|g" deploy/nginx-vhost.conf > "$VHOST"
  ok "vhost created for $DOMAIN"
fi
ln -sf "$VHOST" "/etc/nginx/sites-enabled/$DOMAIN"

if nginx -t 2>/dev/null; then
  # nginx reports duplicate server_names itself, and it is the only opinion
  # that counts. Surface it rather than letting it scroll past in a log.
  if nginx -t 2>&1 | grep -q "conflicting server name.*$DOMAIN"; then
    rm -f "/etc/nginx/sites-enabled/$DOMAIN"
    systemctl reload nginx
    die "another enabled site already serves $DOMAIN, so ours was disabled again
   rather than fight it. Your other sites are untouched. Find the duplicate:
       nginx -T | grep -n '$DOMAIN'"
  fi
  systemctl reload nginx
  ok "nginx reloaded (existing sites untouched)"
else
  rm -f "/etc/nginx/sites-enabled/$DOMAIN"
  nginx -t || true
  die "nginx config test failed — our site was disabled again, your other sites are fine"
fi

say "https"
#
# This used to be step 2 of a printed TODO list, and that is exactly why the
# dashboard sat on plain HTTP showing "Not secure" in the browser: a manual
# step after a script that reports success is a step that does not happen.
# It is part of the script now.
#
# Skipped only when DNS does not point here yet — certbot cannot prove control
# of a name that does not resolve to this machine, and a failed attempt counts
# against Let's Encrypt's rate limit.
MY_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
DNS_IP=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')

# The question is NOT "do we have a certificate" — it is "is nginx actually
# serving this hostname over TLS". Those came apart once already: the cert was
# sitting in /etc/letsencrypt while our vhost had no 443 block at all, so
# HTTPS fell through to a neighbouring app's certificate. Asking the wrong
# question meant the script cheerfully reported "certificate already present"
# and moved on, every single run.
HAVE_CERT=$([ -d "/etc/letsencrypt/live/$DOMAIN" ] && echo 1 || echo 0)
SERVES_TLS=$(grep -qE '^[[:space:]]*(listen[^;]*443|ssl_certificate)' "$VHOST" 2>/dev/null && echo 1 || echo 0)

if [ "$HAVE_CERT" = 1 ] && [ "$SERVES_TLS" = 1 ]; then
  ok "HTTPS already configured for $DOMAIN (renews automatically)"
elif [ -z "$DNS_IP" ]; then
  warn "$DOMAIN does not resolve yet — skipping HTTPS.
        Point an A record at $MY_IP, then re-run this script."
elif [ "$DNS_IP" != "$MY_IP" ]; then
  warn "$DOMAIN resolves to $DNS_IP but this server is $MY_IP — skipping HTTPS.
        Fix the A record, then re-run this script."
else
  # --redirect also installs the port-80 -> 443 redirect, so plain HTTP stops
  # being reachable rather than merely being discouraged.
  #
  # --reinstall when the certificate exists but nginx is not using it: that is
  # a config problem, not an expiry problem, and re-issuing would burn a
  # Let's Encrypt rate-limit slot to solve the wrong thing.
  if [ "$HAVE_CERT" = 1 ]; then
    warn "certificate exists but nginx was not serving TLS for $DOMAIN — reinstalling it"
    CB_ARGS="--reinstall"
  else
    CB_ARGS="--keep-until-expiring"
  fi
  if certbot --nginx -d "$DOMAIN" --redirect --agree-tos --non-interactive \
             $CB_ARGS -m "$CERT_EMAIL" >/dev/null 2>&1; then
    systemctl reload nginx
    ok "HTTPS enabled for $DOMAIN, HTTP redirects to it, renewal is automatic"
  else
    warn "certbot failed. Run it by hand to see why:
        certbot --nginx -d $DOMAIN --redirect -m $CERT_EMAIL
        The site still works over plain HTTP in the meantime, and browsers
        will call it Not secure until this succeeds."
  fi
fi

say "firewall"
# Port $PORT is deliberately NOT opened: nginx reaches it over loopback.
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "^Status: active"; then
  ufw allow 'Nginx Full' >/dev/null 2>&1 || true
  ok "ufw already active; allowed Nginx Full (80/443)"
else
  warn "ufw is not active. Turn it on when you are ready:
        ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable"
fi

say "check"
SCHEME=$(grep -qE '^[[:space:]]*(listen[^;]*443|ssl_certificate)' "$VHOST" 2>/dev/null && echo https || echo http)

# Whose certificate does this hostname actually get?
#
# This is the check that was missing. When the vhost lost its TLS block, HTTPS
# silently fell through to a neighbouring site on the same box and served ITS
# certificate. Over HTTP everything looked perfect, so nothing here noticed —
# meanwhile every browser was showing a certificate-name error. Verifying the
# response code alone is not enough; you have to ask who answered.
if [ "$SCHEME" = "https" ]; then
  SERVED=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null \
           | openssl x509 -noout -subject 2>/dev/null | sed 's/.*CN *= *//; s/,.*//' | tr -d ' ')
  if [ -z "$SERVED" ]; then
    warn "could not read the certificate served for $DOMAIN"
  elif [ "$SERVED" = "$DOMAIN" ]; then
    ok "$DOMAIN serves its own certificate"
  else
    die "$DOMAIN is serving the certificate for '$SERVED'.
   Our vhost is not answering on 443, so nginx fell through to another site on
   this machine. Every browser will show a certificate error. Fix with:
       certbot --nginx -d $DOMAIN --redirect --reinstall
   and check nothing else claims this name:
       nginx -T | grep -n '$DOMAIN'"
  fi
fi

CODE=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 15 "$SCHEME://$DOMAIN/" 2>/dev/null || echo "000")
[ "$CODE" = "200" ] && ok "$SCHEME://$DOMAIN/ answers 200" \
                    || warn "$SCHEME://$DOMAIN/ answered $CODE — check: journalctl -u xscraper-web -n 30"

say "done"
cat <<EOF

  Dashboard:  $SCHEME://$DOMAIN/

  Sign in with (from $APP_DIR/.env):
    DASH_USER=$(grep '^DASH_USER=' .env | cut -d= -f2)
$( [ -n "${NEW_PW:-}" ] && echo "    DASH_PASSWORD=$NEW_PW" || echo "    DASH_PASSWORD=(unchanged — the one already in .env)" )

  Then, in the dashboard itself:

  1. X accounts -> "+ Add an account". Give it a short name and press
     "Save and sign in". A browser window opens inside the page; sign in to X
     there. The session is captured and the browser is thrown away.

  2. Once one account shows "Working":
       systemctl start xscraper-watch

  Re-run this script after any git pull. It is safe to run repeatedly.

EOF
