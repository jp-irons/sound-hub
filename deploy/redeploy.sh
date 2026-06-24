#!/usr/bin/env bash
# Sound Hub — redeploy script
#
# Run from anywhere:
#   bash deploy/redeploy.sh
#
# Pulls latest, rebuilds the frontend, installs Python deps only if
# server/requirements.txt actually changed in the pulled commits, and
# restarts the systemd service.
#
# Flags:
#   --force-deps   always run pip install, even if requirements.txt didn't change
#   --skip-deps    never run pip install, even if requirements.txt changed
#   -h, --help     show this help

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="soundhub"

FORCE_DEPS=0
SKIP_DEPS=0
for arg in "$@"; do
    case "$arg" in
        --force-deps) FORCE_DEPS=1 ;;
        --skip-deps)  SKIP_DEPS=1 ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

cd "$REPO_DIR"

echo "▶ Pulling latest..."
BEFORE_REV="$(git rev-parse HEAD)"
git pull
AFTER_REV="$(git rev-parse HEAD)"

echo "▶ Installing frontend deps + building..."
npm ci
npm run build

if [[ "$SKIP_DEPS" -eq 1 ]]; then
    echo "▶ Skipping pip install (--skip-deps)"
elif [[ "$FORCE_DEPS" -eq 1 ]]; then
    echo "▶ Installing Python deps (--force-deps)..."
    venv/bin/pip install -r server/requirements.txt
elif [[ "$BEFORE_REV" != "$AFTER_REV" ]] && \
     ! git diff --quiet "$BEFORE_REV" "$AFTER_REV" -- server/requirements.txt; then
    echo "▶ server/requirements.txt changed — installing Python deps..."
    venv/bin/pip install -r server/requirements.txt
else
    echo "▶ server/requirements.txt unchanged — skipping pip install"
fi

echo "▶ Restarting $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"

echo "✓ Redeploy complete ($BEFORE_REV -> $AFTER_REV)"
