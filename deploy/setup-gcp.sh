#!/usr/bin/env bash
# setup-gcp.sh — One-time setup for Bread trading bot on GCP e2-micro (Debian 12).
# Run as root or with sudo on a fresh VM.
set -euo pipefail

BREAD_USER="bread"
BREAD_HOME="/home/$BREAD_USER"
BREAD_DIR="$BREAD_HOME/bread"
REPO_URL="${BREAD_REPO_URL:-https://github.com/roman10/bread.git}"
SWAP_SIZE="1G"

echo "=== Bread Trading Bot — GCP Setup ==="
echo

# -------------------------------------------------------------------------
# 1. System packages
# -------------------------------------------------------------------------
echo "[1/8] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl

# -------------------------------------------------------------------------
# 2. Create bread user
# -------------------------------------------------------------------------
if id "$BREAD_USER" &>/dev/null; then
    echo "[2/8] User '$BREAD_USER' already exists — skipping."
else
    echo "[2/8] Creating user '$BREAD_USER'..."
    useradd --create-home --shell /bin/bash "$BREAD_USER"
fi

# -------------------------------------------------------------------------
# 3. Swap file (1 GB safety net for 1 GB RAM VM)
# -------------------------------------------------------------------------
if swapon --show | grep -q /swapfile; then
    echo "[3/8] Swap already active — skipping."
else
    echo "[3/8] Creating ${SWAP_SIZE} swap file..."
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# -------------------------------------------------------------------------
# 4. Clone or update repo
# -------------------------------------------------------------------------
if [ -d "$BREAD_DIR/.git" ]; then
    echo "[4/8] Repo exists — pulling latest..."
    sudo -u "$BREAD_USER" git -C "$BREAD_DIR" pull --ff-only
else
    echo "[4/8] Cloning repo..."
    sudo -u "$BREAD_USER" git clone "$REPO_URL" "$BREAD_DIR"
fi

# -------------------------------------------------------------------------
# 5. Python venv + install
# -------------------------------------------------------------------------
echo "[5/8] Setting up Python venv and installing Bread..."
sudo -u "$BREAD_USER" bash -c "
    cd '$BREAD_DIR'
    python3.11 -m venv .venv
    .venv/bin/pip install --upgrade pip -q
    .venv/bin/pip install -e '.[dashboard]' -q
"

# -------------------------------------------------------------------------
# 6. Environment file (Alpaca keys placeholder — fill in before starting)
# -------------------------------------------------------------------------
ENV_FILE="$BREAD_HOME/.env"
if [ -f "$ENV_FILE" ]; then
    echo "[6/8] .env already exists — skipping. Edit $ENV_FILE to update keys."
else
    echo "[6/8] Writing .env template (no keys prompted — fill in before starting)..."
    cat > "$ENV_FILE" <<'EOF'
# Alpaca paper trading credentials (required to start bread-paper.service)
ALPACA_PAPER_API_KEY=
ALPACA_PAPER_SECRET_KEY=

# Alpaca live trading credentials (required to start bread-live.service)
ALPACA_LIVE_API_KEY=
ALPACA_LIVE_SECRET_KEY=

# Optional human-readable account labels (shown in CLI, dashboard, alerts)
# ALPACA_PAPER_NICKNAME="Paper"
# ALPACA_LIVE_NICKNAME="Main IRA"
EOF
    chown "$BREAD_USER:$BREAD_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  Wrote template to $ENV_FILE (mode 600)."
    echo "  → Edit it with your real keys before starting either service."
fi

# -------------------------------------------------------------------------
# 7. Initialize per-mode databases (only for modes whose keys are present —
#    bread.config.AppConfig validates that the active mode's creds are set,
#    so `db init --mode live` against an empty .env would fail and abort the
#    whole script. We skip cleanly so paper-only installs work today and
#    live can be initialized later without re-running all of setup-gcp.sh.)
# -------------------------------------------------------------------------
echo "[7/8] Initializing per-mode databases..."
sudo -u "$BREAD_USER" bash -c "
    cd '$BREAD_DIR'
    set -a; source '$ENV_FILE' 2>/dev/null || true; set +a

    if [ -n \"\${ALPACA_PAPER_API_KEY:-}\" ] && [ -n \"\${ALPACA_PAPER_SECRET_KEY:-}\" ]; then
        .venv/bin/python -m bread db init --mode paper
    else
        echo '  ⚠ ALPACA_PAPER_* not set in .env — skipping paper db init.'
        echo '    Run: sudo -u $BREAD_USER $BREAD_DIR/.venv/bin/bread db init --mode paper'
        echo '    once the paper keys are added to $ENV_FILE.'
    fi

    if [ -n \"\${ALPACA_LIVE_API_KEY:-}\" ] && [ -n \"\${ALPACA_LIVE_SECRET_KEY:-}\" ]; then
        .venv/bin/python -m bread db init --mode live
    else
        echo '  ⚠ ALPACA_LIVE_* not set in .env — skipping live db init.'
        echo '    Run: sudo -u $BREAD_USER $BREAD_DIR/.venv/bin/bread db init --mode live'
        echo '    once the live keys are added to $ENV_FILE.'
    fi
"

# -------------------------------------------------------------------------
# 8. Install Tailscale + systemd services
# -------------------------------------------------------------------------
echo "[8/8] Installing Tailscale and systemd services..."

# Tailscale
if command -v tailscale &>/dev/null; then
    echo "  Tailscale already installed."
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  Tailscale installed. Run 'sudo tailscale up' to authenticate."
fi

# Disable + remove the legacy single-mode unit if a previous install left it
if [ -f /etc/systemd/system/bread.service ]; then
    echo "  Removing legacy bread.service (replaced by bread-paper / bread-live)..."
    systemctl disable --now bread.service 2>/dev/null || true
    rm -f /etc/systemd/system/bread.service
fi

# Install both per-mode units
cp "$BREAD_DIR/deploy/bread-paper.service" /etc/systemd/system/bread-paper.service
cp "$BREAD_DIR/deploy/bread-live.service" /etc/systemd/system/bread-live.service
systemctl daemon-reload

# Enable paper by default; leave live disabled until the operator opts in.
# bread-live.service has BREAD_LIVE_CONFIRM=I_UNDERSTAND baked in, so enabling
# it is the explicit "I'm ready for real money" action.
systemctl enable bread-paper
echo "  bread-paper enabled (will start on boot once started)."
echo "  bread-live installed but NOT enabled. Enable explicitly when ready:"
echo "      sudo systemctl enable --now bread-live"

echo
echo "=== Setup Complete ==="
echo
echo "Next steps:"
echo "  1. Add Alpaca keys:         sudo nano $ENV_FILE"
echo "  2. Authenticate Tailscale:  sudo tailscale up"
echo "  3. Note your Tailscale IP:  tailscale ip -4"
echo "  4. Start paper:             sudo systemctl start bread-paper"
echo "  5. Verify paper running:    sudo systemctl status bread-paper"
echo "  6. View paper logs:         journalctl -u bread-paper -f"
echo "  7. Access dashboard:        http://<tailscale-ip>:8050"
echo
echo "When ready for live:"
echo "  8. Add live keys to $ENV_FILE if not already done"
echo "  9. Enable + start live:     sudo systemctl enable --now bread-live"
echo "  10. View live logs:         journalctl -u bread-live -f"
echo "  11. Live dashboard on demand: bread dashboard --mode live --port 8051"
echo
echo "To update keys later:  sudo nano $ENV_FILE && sudo systemctl restart bread-paper bread-live"
