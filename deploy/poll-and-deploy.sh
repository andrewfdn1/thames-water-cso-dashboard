#!/usr/bin/env bash
# Run periodically by the thames-cso-poll-deploy systemd timer. Checks
# main for new commits and deploys if there are any -- the auto-deploy
# mechanism after dropping the GitHub Actions self-hosted runner approach
# (see sync-and-restart.sh's header for why).
#
# Operates on THIS script's own git checkout (the persistent clone made
# during initial setup), not /opt/thames-cso-dashboard -- that's just the
# rsync'd deployment target and isn't itself a git repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

git fetch origin main --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

echo "New commits detected on main ($LOCAL -> $REMOTE), deploying..."
git reset --hard origin/main
bash "$REPO_DIR/deploy/sync-and-restart.sh" "$REPO_DIR"
