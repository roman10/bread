#!/usr/bin/env bash
# setup-gcp.sh — One-time setup for Bread trading bot on GCP e2-micro (Debian 12).
# Run as root or with sudo on a fresh VM.
set -euo pipefail

BREAD_USER="bread"
BREAD_HOME="/home/$BREAD_USER"
BREAD_DIR="$BREAD_HOME/bread"
REPO_URL="${BREAD_REPO_URL:-https://github.com/waterdrop86/bread.git}"
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
# 6. Environment file (Alpaca keys)
# -------------------------------------------------------------------------
ENV_FILE="$BREAD_HOME/.env"
if [ -f "$ENV_FILE" ]; then
    echo "[6/8] .env already exists — skipping. Edit $ENV_FILE to update keys."
else
    echo "[6/8] Setting up Alpaca API keys..."
    echo "  Enter your Alpaca Paper API key (or press Enter to skip):"
    read -r api_key
    echo "  Enter your Alpaca Paper Secret key (or press Enter to skip):"
    read -r secret_key

    cat > "$ENV_FILE" <<EOF
ALPACA_PAPER_API_KEY=${api_key}
ALPACA_PAPER_SECRET_KEY=${secret_key}
EOF
    chown "$BREAD_USER:$BREAD_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  Saved to $ENV_FILE (mode 600)."
fi

# -------------------------------------------------------------------------
# 7. Initialize database and verify
# -------------------------------------------------------------------------
echo "[7/8] Initializing database and verifying connectivity..."
sudo -u "$BREAD_USER" bash -c "
    cd '$BREAD_DIR'
    set -a; source '$ENV_FILE'; set +a
    .venv/bin/python -m bread db init
    .venv/bin/python -m bread status
"

# -------------------------------------------------------------------------
# 8. Install Tailscale + systemd service
# -------------------------------------------------------------------------
echo "[8/8] Installing Tailscale and systemd service..."

# Tailscale
if command -v tailscale &>/dev/null; then
    echo "  Tailscale already installed."
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  Tailscale installed. Run 'sudo tailscale up' to authenticate."
fi

# systemd service
cp "$BREAD_DIR/deploy/bread.service" /etc/systemd/system/bread.service
systemctl daemon-reload
systemctl enable bread

echo
echo "=== Setup Complete ==="
echo
echo "Next steps:"
echo "  1. Authenticate Tailscale:  sudo tailscale up"
echo "  2. Note your Tailscale IP:  tailscale ip -4"
echo "  3. Start the bot:           sudo systemctl start bread"
echo "  4. Check status:            sudo systemctl status bread"
echo "  5. View logs:               journalctl -u bread -f"
echo "  6. Access dashboard:        http://<tailscale-ip>:8050"
echo
echo "To update keys later:  sudo nano $ENV_FILE && sudo systemctl restart bread"
