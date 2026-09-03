#!/usr/bin/env bash
#
# Server-side UPDATE. The "every update" half of BLUEPRINT §6, as a script:
#
#     bash /opt/xscraper/app/deploy/update.sh [OLD_REV]
#
# Run as root on the VPS after `git pull --ff-only` (the GitHub Actions
# workflow in .github/workflows/deploy.yml does exactly that on every push to
# main, then calls this). It is safe to run by hand, and safe to run twice.
#
# What it does, in order:
#   1. chown the checkout back to the app user (git pull as root leaves root-
#      owned files, and the services run as xscraper under ProtectSystem=strict)
#   2. pip install only if requirements.txt changed since OLD_REV
#   3. re-install systemd units only if deploy/*.service changed
#   4. restart xscraper-web, and restart each collector that is currently
#      RUNNING. It never STARTS a stopped collector — setup.sh leaves FB/IG
#      stopped on purpose until an account is signed in, and this respects that.
#   5. verify xscraper-web came back; print the tail of its log if not
#
# OLD_REV is the commit that was checked out before the pull. Without it the
# script assumes everything changed (pip install, units, full restart) — slower
# but always correct.

set -euo pipefail

APP_DIR=/opt/xscraper/app
APP_USER=xscraper
OLD=${1:-}

cd "$APP_DIR"
NEW=$(git rev-parse HEAD)

say() { printf '\n== %s\n' "$*"; }
ok()  { printf '   ok  %s\n' "$*"; }

if [ -n "$OLD" ] && git cat-file -e "$OLD" 2>/dev/null; then
  CHANGED=$(git diff --name-only "$OLD" "$NEW")
  say "update ${OLD:0:7} -> ${NEW:0:7}"
else
  CHANGED=$(git ls-files)
  say "update -> ${NEW:0:7} (no previous revision given: treating everything as changed)"
fi
[ -z "$CHANGED" ] && { ok "nothing changed"; exit 0; }
printf '%s\n' "$CHANGED" | sed 's/^/   /'

changed() { printf '%s\n' "$CHANGED" | grep -qE "$1"; }

say "ownership"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
ok "$APP_DIR is $APP_USER"

if changed '^requirements\.txt$'; then
  say "python deps (requirements.txt changed)"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  ok "pip install done"
fi

if changed '^deploy/xscraper-.*\.service$'; then
  say "systemd units (deploy/*.service changed)"
  # The web unit carries the port setup.sh chose; keep it rather than picking a new one.
  PORT=$(grep -oE -- '--port [0-9]+' /etc/systemd/system/xscraper-web.service | grep -oE '[0-9]+' || true)
  if [ -n "$PORT" ]; then
    sed "s|__PORT__|$PORT|g" deploy/xscraper-web.service > /etc/systemd/system/xscraper-web.service
    ok "xscraper-web.service re-rendered on port $PORT"
  else
    echo "   !!  could not read the current port from the installed web unit; leaving it alone"
  fi
  cp deploy/xscraper-watch.service deploy/xscraper-fb.service deploy/xscraper-ig.service /etc/systemd/system/
  systemctl daemon-reload
  ok "daemon-reload"
fi

# Docs-only pushes (*.md, deploy/ scripts, workflow files) touch no running code.
if ! changed '\.(py|toml|txt|json|js|css|html)$' && ! changed '^frontend/dist/'; then
  say "restart"
  ok "no runtime files changed - nothing to restart"
  exit 0
fi

say "restart"
systemctl restart xscraper-web
ok "xscraper-web restarted"
for unit in xscraper-watch xscraper-fb xscraper-ig; do
  if systemctl is-active --quiet "$unit"; then
    systemctl restart "$unit"; ok "$unit restarted"
  else
    ok "$unit is stopped - left stopped"
  fi
done

say "verify"
sleep 3
if systemctl is-active --quiet xscraper-web; then
  ok "xscraper-web is up at $NEW"
else
  journalctl -u xscraper-web -n 30 --no-pager
  echo "   !!  xscraper-web did not come back - log above" >&2
  exit 1
fi
