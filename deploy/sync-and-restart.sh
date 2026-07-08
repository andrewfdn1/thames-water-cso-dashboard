#!/usr/bin/env bash
# Run by poll-and-deploy.sh (via the thames-cso-poll-deploy timer) whenever
# it detects new commits on main. Copies the given source checkout into the
# actual running location and restarts the service -- never touches .env,
# logs/, or the venv's installed packages unless requirements.txt changed.
#
# (Originally run by a GitHub Actions self-hosted runner instead of a local
# timer -- dropped after hitting an unresolved .NET/OpenSSL TLS
# incompatibility registering the runner on this Pi's 32-bit ARM / Debian
# Trixie combination. A local poll is simpler anyway: no extra agent
# process on an already memory-constrained Pi.)
set -euo pipefail

SRC_DIR="${1:?Usage: sync-and-restart.sh <source-checkout-dir>}"
APP_DIR="/opt/thames-cso-dashboard"

rsync -a --delete \
  --exclude ".git" --exclude "venv" --exclude "logs" --exclude ".env" \
  "$SRC_DIR/" "$APP_DIR/"

if ! cmp -s "$SRC_DIR/requirements.txt" "$APP_DIR/venv/.last-requirements.txt" 2>/dev/null; then
  echo "requirements.txt changed -- reinstalling dependencies"
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  cp "$SRC_DIR/requirements.txt" "$APP_DIR/venv/.last-requirements.txt"
else
  echo "requirements.txt unchanged -- skipping pip install"
fi

sudo systemctl restart thames-cso-dashboard.service
sleep 2
sudo systemctl is-active --quiet thames-cso-dashboard.service && echo "Service restarted OK" || {
  echo "ERROR: service failed to come back up after restart" >&2
  sudo systemctl status thames-cso-dashboard.service --no-pager || true
  exit 1
}
