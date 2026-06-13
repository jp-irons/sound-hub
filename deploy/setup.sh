#!/usr/bin/env bash
# Sound Hub — WSL2 deployment setup
#
# Run from the repo root inside WSL2 Ubuntu:
#   bash deploy/setup.sh
#
# Re-running is safe: all steps are idempotent.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
RUN_USER="$(whoami)"

echo "╔══════════════════════════════════════════════╗"
echo "║        Sound Hub — deployment setup          ║"
echo "╚══════════════════════════════════════════════╝"
echo "  repo : $REPO_DIR"
echo "  user : $RUN_USER"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "▶ Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    nginx python3-pip python3-venv nodejs npm curl

# ── 2. Python venv ────────────────────────────────────────────────────────────
echo "▶ Setting up Python venv..."
cd "$REPO_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip --quiet

venv/bin/pip install --quiet -r server/requirements.txt

# ── 3. Build frontend SPA ─────────────────────────────────────────────────────
echo "▶ Building frontend..."
cd "$REPO_DIR"
npm ci --silent
npm run build
echo "  SPA built → $REPO_DIR/dist"

# ── 4. Config file ────────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/config/soundhub.conf" ]; then
    cp "$REPO_DIR/config/soundhub.conf.example" "$REPO_DIR/config/soundhub.conf"
    echo ""
    echo "  ⚠  config/soundhub.conf created from example."
    echo "     Edit it and set BASE_STATION_IP to this machine's LAN IP."
    echo "     Then run:  sudo systemctl restart soundhub"
    echo ""
fi

# ── 5. Data directories ───────────────────────────────────────────────────────
mkdir -p "$REPO_DIR/audio" "$REPO_DIR/detections_audio"

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "▶ Installing systemd service..."
sed \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__USER__|$RUN_USER|g" \
    "$DEPLOY_DIR/soundhub.service" \
    | sudo tee /etc/systemd/system/soundhub.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable soundhub
sudo systemctl restart soundhub
echo "  soundhub: $(sudo systemctl is-active soundhub)"

# ── 7. nginx ──────────────────────────────────────────────────────────────────
echo "▶ Configuring nginx..."
sed \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    "$DEPLOY_DIR/nginx-lan.conf" \
    | sudo tee /etc/nginx/sites-available/sound-hub > /dev/null

sudo ln -sf /etc/nginx/sites-available/sound-hub /etc/nginx/sites-enabled/sound-hub
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
echo "  nginx: $(sudo systemctl is-active nginx)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✓ Setup complete.  Sound Hub is running at:"
hostname -I | tr ' ' '\n' | grep -v '^$' | head -3 | while read -r ip; do
    echo "  http://$ip"
done
echo ""
echo "Next steps:"
echo "  1. Edit config/soundhub.conf — set BASE_STATION_IP to this machine's LAN IP"
echo "     then: sudo systemctl restart soundhub"
echo "  2. On the Windows host, run deploy/windows-setup.ps1 as Administrator"
echo "     to configure WSL auto-start and open the firewall."
