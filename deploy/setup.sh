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
# SAFE ON A SHARED SERVER. It never touches nginx's default site or any vhost
# it did not create, never restarts services it does not own, and refuses to
# start if the port it wants is already taken.

set -euo pipefail

APP_DIR=/opt/xscraper/app
APP_USER=xscraper
PORT=8765
DOMAIN=scraper.vedictech.in

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok   %s\n' "$*"; }
warn() { printf '   WARN %s\n' "$*"; }
die()  { printf '\n   FAIL %s\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

say "checking what is already here"
command -v nginx >/dev/null && ok "nginx present (other sites will not be touched)" || warn "nginx missing"
if ss -lntp 2>/dev/null | grep -q ":$PORT "; then
  die "port $PORT is already in use — another service has it. Edit PORT in this
   script and in deploy/nginx-scraper.conf + the systemd unit, then re-run."
fi
ok "port $PORT is free"

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
cp deploy/xscraper-web.service deploy/xscraper-watch.service /etc/systemd/system/
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
# Only ever writes its own vhost. Other sites on this box are none of its
# business, and `rm sites-enabled/default` on a shared server is how you take
# down someone else's site by accident.
cp deploy/nginx-scraper.conf /etc/nginx/sites-available/scraper
ln -sf /etc/nginx/sites-available/scraper /etc/nginx/sites-enabled/scraper
if nginx -t 2>/dev/null; then
  systemctl reload nginx
  ok "vhost installed for $DOMAIN (existing sites untouched)"
else
  rm -f /etc/nginx/sites-enabled/scraper
  nginx -t || true
  die "nginx config test failed — our vhost was removed again, your other sites are fine"
fi

say "done"
cat <<EOF

  Dashboard is up on 127.0.0.1:$PORT, behind nginx, login required.

  Credentials (in $APP_DIR/.env):
    DASH_USER=$(grep '^DASH_USER=' .env | cut -d= -f2)
$( [ -n "${NEW_PW:-}" ] && echo "    DASH_PASSWORD=$NEW_PW" || echo "    DASH_PASSWORD=(unchanged — the one already in .env)" )

  NEXT, in order:

  1. DNS: point $DOMAIN at this server, then confirm:
       dig +short $DOMAIN

  2. TLS (only after DNS resolves):
       certbot --nginx -d $DOMAIN --redirect

  3. From the MACHINE WHERE YOU LOGGED IN (not here) — the server has no
     browser, so it cannot do the X login itself:
       sqlite3 accounts.db ".backup /tmp/accounts.db"
       scp /tmp/accounts.db root@$(hostname -I | awk '{print $1}'):$APP_DIR/
       scp -r profiles root@$(hostname -I | awk '{print $1}'):$APP_DIR/

  4. Back here, once accounts.db exists:
       chown -R $APP_USER:$APP_USER $APP_DIR && chmod 600 $APP_DIR/*.db
       sudo -u $APP_USER $APP_DIR/.venv/bin/python3 $APP_DIR/main.py doctor
       systemctl start xscraper-watch

EOF
