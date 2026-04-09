# Deploying Bread to GCP Free Tier

## Prerequisites

- GCP account with billing enabled (won't be charged — free tier)
- Alpaca Paper API key and secret (from [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys)
- Tailscale account (free at [tailscale.com](https://tailscale.com))
- GitHub Personal Access Token (PAT) if the repo is private — generate at GitHub → Settings → Developer settings → Personal access tokens

## 1. Create the VM

In GCP Console → Compute Engine → VM Instances → Create Instance:

| Setting | Value |
|---------|-------|
| Name | `bread-trading` |
| Region | `us-central1` (free tier eligible, good availability) |
| Zone | `us-central1-a` |
| Machine type | `e2-micro` (free tier eligible) |
| Boot disk | Debian 12, 30 GB standard persistent disk |
| Firewall | No HTTP/HTTPS needed (Tailscale handles access) |

**Cost: $0/month** under the free tier (1 non-preemptible e2-micro in us-east1/us-west1/us-central1).

> **Note:** If you get "resource unavailable" errors, try a different zone within us-central1 (b, c, f). Do not use "Edit and Retry" — create a fresh instance in the new zone.

## 2. SSH into the VM

```bash
gcloud compute ssh bread-trading --zone us-central1-a
```

## 3. Run Setup

```bash
# Clone the repo first
sudo apt-get install -y git

# For a private repo, include your PAT in the URL:
git clone https://<YOUR_PAT>@github.com/roman10/bread.git /tmp/bread-setup

# For a public repo:
# git clone https://github.com/roman10/bread.git /tmp/bread-setup

# Run setup as root
sudo bash /tmp/bread-setup/deploy/setup-gcp.sh
```

The script will:
- Install Python 3.11, create a `bread` user
- Clone the repo to `/home/bread/bread`, create venv, install dependencies
- Prompt for Alpaca API keys → saves to `/home/bread/.env`
- Create 1 GB swap file
- Initialize the database
- Install Tailscale and the systemd service

> **Note on pandas-ta:** The project uses pure pandas for technical indicators (SMA, EMA, RSI, MACD, ATR, BBands). pandas-ta has been removed as a dependency because it only publishes Python 3.12-only wheels on PyPI, incompatible with the VM's Python 3.11.

## 4. Configure Tailscale

### On the VM

```bash
sudo tailscale up
```

Visit the printed auth URL in your browser to link the VM to your Tailscale network. Then get the VM's Tailscale IP:

```bash
tailscale ip -4
# Example: 100.118.138.14
```

### On your devices (Mac, iPhone, Android, Windows)

Install Tailscale from the App Store / Play Store / [tailscale.com/download](https://tailscale.com/download), then sign in with the **same account** used to authenticate the VM. Once both devices are in the same tailnet, they can reach each other by Tailscale IP.

> Tailscale is a zero-config WireGuard VPN. No firewall rules or open ports needed — traffic flows through the encrypted mesh. The bot is never exposed to the public internet.

## 5. Start the Bot

```bash
sudo systemctl start bread
```

## 6. Access Dashboard

From any device **connected to your Tailscale network**, open:

```
http://<tailscale-ip>:8050
```

Replace `<tailscale-ip>` with the VM's Tailscale IP from step 4 (e.g. `http://100.118.138.14:8050`).

The dashboard shows portfolio overview, equity curve, open positions, and trade journal. It auto-refreshes every 30 seconds during market hours, every 5 minutes off-hours.

> **Can't connect?** The most common causes:
> - Tailscale not running on your device — open the Tailscale app and check it shows "Connected"
> - Tailscale not running on the VM — run `sudo tailscale up` on the VM
> - Both devices must be signed into the **same** Tailscale account

## Day-to-Day Operations

### Check bot status

```bash
sudo systemctl status bread
```

### View live logs

```bash
journalctl -u bread -f
```

### View recent logs

```bash
journalctl -u bread --since "1 hour ago"
```

### Trading status (account equity, positions, risk)

```bash
sudo -u bread bash -c "cd /home/bread/bread && set -a && source ~/.env && set +a && .venv/bin/python -m bread status"
```

### Restart the bot

```bash
sudo systemctl restart bread
```

### Stop the bot

```bash
sudo systemctl stop bread
```

### Update code and restart

```bash
sudo bash /home/bread/bread/deploy/update.sh
```

### Re-authenticate Tailscale (if it disconnects)

```bash
sudo tailscale up
# Follow the printed auth URL
tailscale status   # Verify connection
```

### Check Tailscale connectivity

```bash
tailscale status
tailscale ip -4    # Show this VM's Tailscale IP
```

## Specs vs Requirements

| Resource | VM has | App needs | Headroom |
|----------|--------|-----------|----------|
| CPU | 0.25 vCPU (burst to 2) | Bursty, ~5s every 15 min | Plenty |
| RAM | 1 GB | ~250–400 MB with dashboard | OK with 1 GB swap |
| Disk | 30 GB | ~1 GB (code + deps + DB) | 29 GB free |
| Network | 1 GB/month egress | <100 MB/month API calls | Plenty |

## Troubleshooting

**Bot won't start:**
```bash
journalctl -u bread --since "5 min ago" --no-pager
# Check for missing API keys, import errors, or connectivity issues
```

**Dashboard not loading:**
1. Verify Tailscale is connected on your device (open the Tailscale app — should show green/Connected)
2. Verify Tailscale is running on the VM: `tailscale status`
3. Verify bot is running: `systemctl status bread`
4. Verify port 8050 is listening: `ss -tlnp | grep 8050` (should show `0.0.0.0:8050`)

**Tailscale disconnected:**
```bash
sudo tailscale up    # Re-authenticate if needed
tailscale status     # Check connection state
```

**Out of memory:**
```bash
free -h          # Check swap usage
swapon --show    # Verify swap is active
```

**pytz missing (alpaca-py dependency):**
```bash
sudo -u bread /home/bread/bread/.venv/bin/pip install pytz
sudo systemctl restart bread
```

**Alpaca API errors:**
```bash
# Verify keys are set correctly
sudo -u bread cat /home/bread/.env
# Re-enter keys if needed
sudo -u bread nano /home/bread/.env
sudo systemctl restart bread
```
