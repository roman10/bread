#!/usr/bin/env bash
# update.sh — Pull latest code, refresh deps, and restart whichever bread
# services are currently enabled (paper, live, or both).
# Run as root or with sudo.
set -euo pipefail

BREAD_DIR="/home/bread/bread"

echo "=== Updating Bread Trading Bot ==="

echo "[1/4] Pulling latest code..."
sudo -u bread git -C "$BREAD_DIR" pull --ff-only

echo "[2/4] Installing dependencies..."
sudo -u bread bash -c "cd '$BREAD_DIR' && .venv/bin/pip install -e '.[dashboard]' -q"

echo "[3/4] Refreshing systemd unit files (in case they changed)..."
# Migration guard: the legacy single-mode `bread.service` was replaced by
# bread-paper.service / bread-live.service. If the legacy file still exists,
# the per-mode units never get restarted by step 4.
if [ -f /etc/systemd/system/bread.service ]; then
    echo
    echo "ERROR: legacy bread.service detected. Run the new setup script once"
    echo "to migrate to bread-paper / bread-live (it disables the old unit"
    echo "and enables bread-paper):"
    echo "  sudo bash $BREAD_DIR/deploy/setup-gcp.sh"
    exit 1
fi

for unit in bread-paper.service bread-live.service; do
    src="$BREAD_DIR/deploy/$unit"
    dst="/etc/systemd/system/$unit"
    if [ -f "$src" ]; then
        cp "$src" "$dst"
    fi
done
systemctl daemon-reload

echo "[4/4] Restarting enabled services..."
restarted=()
for unit in bread-paper bread-live; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        systemctl restart "$unit"
        restarted+=("$unit")
        echo "  Restarted $unit"
    else
        echo "  $unit not enabled — skipping"
    fi
done

if [ ${#restarted[@]} -eq 0 ]; then
    echo
    echo "No bread-* services were enabled. Enable one with:"
    echo "  sudo systemctl enable --now bread-paper"
    exit 0
fi

echo
echo "Done. Tailing logs (Ctrl+C to stop):"
journal_args=()
for u in "${restarted[@]}"; do
    journal_args+=(-u "$u.service")
done
journalctl "${journal_args[@]}" -f --no-pager -n 20
