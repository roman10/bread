#!/usr/bin/env bash
# update.sh — Pull latest code and restart the Bread trading bot.
# Run as root or with sudo.
set -euo pipefail

BREAD_DIR="/home/bread/bread"

echo "=== Updating Bread Trading Bot ==="

echo "[1/3] Pulling latest code..."
sudo -u bread git -C "$BREAD_DIR" pull --ff-only

echo "[2/3] Installing dependencies..."
sudo -u bread bash -c "cd '$BREAD_DIR' && .venv/bin/pip install -e '.[dashboard]' -q"

echo "[3/3] Restarting service..."
systemctl restart bread

echo
echo "Done. Tailing logs (Ctrl+C to stop):"
journalctl -u bread -f --no-pager -n 20
