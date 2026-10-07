#!/usr/bin/env bash
#
# The sign-in window's browser: real Google Chrome on a virtual screen.
#
#     bash /opt/xscraper/app/deploy/browser.sh
#
# Run as root. Called by setup.sh and update.sh; safe to run by hand and safe
# to run twice (everything already present is skipped).
#
# WHY (2026-10-07): Instagram answered the headless bundled Chromium with 403
# before the login form, through every proxy. auth._launch already prefers
# Google Chrome and, for Instagram, a real window on Xvfb — this script is what
# puts both on the box. Without them the app falls back to what it did before.

set -uo pipefail
APP_DIR=${APP_DIR:-/opt/xscraper/app}
ok()   { printf '   ok  %s\n' "$*"; }
warn() { printf '   !!  %s\n' "$*"; }
export DEBIAN_FRONTEND=noninteractive
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

if command -v Xvfb >/dev/null 2>&1; then
  ok "Xvfb present"
elif apt-get install -y -qq xvfb >/dev/null 2>&1; then
  ok "Xvfb installed"
else
  warn "Xvfb did not install — the sign-in window stays headless"
fi

if command -v google-chrome-stable >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1; then
  ok "$(google-chrome --version 2>/dev/null || google-chrome-stable --version 2>/dev/null || echo 'Google Chrome present')"
elif [ "$(dpkg --print-architecture 2>/dev/null)" != "amd64" ]; then
  warn "Google Chrome ships for amd64 only; this box is $(dpkg --print-architecture 2>/dev/null) — staying on bundled Chromium"
elif "$APP_DIR/.venv/bin/python3" -m playwright install chrome >/dev/null 2>&1 \
     && command -v google-chrome >/dev/null 2>&1; then
  ok "installed $(google-chrome --version 2>/dev/null)"
else
  warn "Google Chrome did not install — staying on bundled Chromium.
        By hand: $APP_DIR/.venv/bin/python3 -m playwright install chrome"
fi
exit 0
