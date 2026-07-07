#!/usr/bin/env bash
# One-time setup, run manually on the Pi after `git clone` and after copying
# in a real .env (see env.example). Re-running this is safe -- every step
# is idempotent -- but it's meant to run once, not on every deploy (see
# sync-and-restart.sh for that).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/thames-cso-dashboard"

echo "== Installing OS packages =="
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git rsync

echo "== Copying app into ${APP_DIR} =="
sudo mkdir -p "$APP_DIR"
sudo chown "$(whoami):$(whoami)" "$APP_DIR"
rsync -a --delete \
  --exclude ".git" --exclude "venv" --exclude "logs" --exclude ".env" \
  "$REPO_DIR/" "$APP_DIR/"

mkdir -p "$APP_DIR/logs"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "!! No $APP_DIR/.env found -- copy deploy/env.example there and fill in real values before starting the service."
fi

echo "== Creating virtualenv =="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "== Installing systemd service (running as $(whoami)) =="
sed "s/__APP_USER__/$(whoami)/" "$APP_DIR/deploy/thames-cso-dashboard.service" \
  | sudo tee /etc/systemd/system/thames-cso-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable thames-cso-dashboard.service

echo "== Allowing passwordless restart of just this one service (for the CI auto-deploy) =="
RUNNER_USER="${1:-$(whoami)}"
SUDOERS_LINE="${RUNNER_USER} ALL=(root) NOPASSWD: /bin/systemctl restart thames-cso-dashboard.service"
echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/thames-cso-dashboard-restart >/dev/null
sudo chmod 440 /etc/sudoers.d/thames-cso-dashboard-restart

echo ""
echo "Done. Next steps:"
echo "  1. Fill in $APP_DIR/.env (see deploy/env.example) if you haven't already."
echo "  2. sudo systemctl start thames-cso-dashboard"
echo "  3. sudo systemctl status thames-cso-dashboard"
echo "  4. Set up cloudflared (see docs/deploy-raspberry-pi.md)."
