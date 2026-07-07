#!/usr/bin/env bash
# Run by the GitHub Actions self-hosted runner (see .github/workflows/
# deploy-to-pi.yml) on every push to main. Copies the freshly-checked-out
# code into the actual running location and restarts the service --
# never touches .env, logs/, or the venv's installed packages unless
# requirements.txt changed.
set -euo pipefail

SRC_DIR="${GITHUB_WORKSPACE:?GITHUB_WORKSPACE not set -- run this from the GitHub Actions runner}"
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
  echo "::error::Service failed to come back up after restart"
  sudo systemctl status thames-cso-dashboard.service --no-pager || true
  exit 1
}
