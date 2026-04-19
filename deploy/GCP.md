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
- Write a `/home/bread/.env` template with empty `ALPACA_PAPER_*` and `ALPACA_LIVE_*` slots (you fill them in)
- Create 1 GB swap file
- Initialize per-mode databases (`data/bread-paper.db`, `data/bread-live.db`)
- Install Tailscale, both systemd services (`bread-paper`, `bread-live`), and enable `bread-paper` only — `bread-live` is left dormant until you opt in

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

## 5. Add Alpaca keys

The setup script wrote a placeholder `/home/bread/.env`. Open it and fill in your keys:

```bash
sudo nano /home/bread/.env
```

```
ALPACA_PAPER_API_KEY=...
ALPACA_PAPER_SECRET_KEY=...

# Leave the LIVE_* lines blank until you're ready to trade real money.
ALPACA_LIVE_API_KEY=
ALPACA_LIVE_SECRET_KEY=

# Optional labels shown in CLI / dashboard / alerts
# ALPACA_PAPER_NICKNAME="Paper"
# ALPACA_LIVE_NICKNAME="Main IRA"
```

If you skipped paper or live db init during setup (because keys were empty), initialize that mode now:

```bash
sudo -u bread /home/bread/bread/.venv/bin/bread db init --mode paper
# Later, when live keys are added:
# sudo -u bread /home/bread/bread/.venv/bin/bread db init --mode live
```

## 6. Start Paper Trading

```bash
sudo systemctl start bread-paper
sudo systemctl status bread-paper
```

Live trading stays dormant — see step 8 below when you're ready.

## 7. Access Paper Dashboard

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

## 8. Enable Live Trading (when ready)

Live trading runs as a separate systemd unit (`bread-live.service`) installed by the setup script but left disabled. To turn it on:

```bash
# 1. Make sure ALPACA_LIVE_API_KEY / ALPACA_LIVE_SECRET_KEY are filled in
sudo nano /home/bread/.env

# 2. Initialize the live database (if you haven't already)
sudo -u bread /home/bread/bread/.venv/bin/bread db init --mode live

# 3. Verify the live keys talk to Alpaca
sudo -u bread bash -c "set -a && source /home/bread/.env && set +a && /home/bread/bread/.venv/bin/bread status --mode live"

# 4. Enable + start the live service
sudo systemctl enable --now bread-live
sudo systemctl status bread-live
journalctl -u bread-live -f
```

The live unit ships with `Environment=BREAD_LIVE_CONFIRM=I_UNDERSTAND` so it can start non-interactively. To pause live trading, use `sudo systemctl disable --now bread-live` — re-enable later with `sudo systemctl enable --now bread-live`. (Removing the env line instead would put the unit into a restart loop until systemd marks it failed; disabling is the clean pause.)

The live process runs **without an auto-dashboard** to keep RAM headroom on the e2-micro. Inspect live state from any tailnet device with:

```bash
# CLI
sudo -u bread /home/bread/bread/.venv/bin/bread status --mode live
sudo -u bread /home/bread/bread/.venv/bin/bread journal --mode live

# Or spin up the live dashboard on demand on port 8051
sudo -u bread /home/bread/bread/.venv/bin/bread dashboard --mode live --port 8051
```

## Day-to-Day Operations

All of the commands below take `bread-paper` or `bread-live` (or both, space-separated) — the per-mode services run independently.

### Check bot status

```bash
sudo systemctl status bread-paper
sudo systemctl status bread-live           # only meaningful once enabled
```

### View live logs

```bash
journalctl -u bread-paper -f
journalctl -u bread-live -f
```

### View recent logs

```bash
journalctl -u bread-paper --since "1 hour ago"
```

### Trading status (account equity, positions, risk)

```bash
sudo -u bread bash -c "cd /home/bread/bread && set -a && source ~/.env && set +a && .venv/bin/python -m bread status --mode paper"
sudo -u bread bash -c "cd /home/bread/bread && set -a && source ~/.env && set +a && .venv/bin/python -m bread status --mode live"
```

### Restart the bot

```bash
sudo systemctl restart bread-paper
sudo systemctl restart bread-live
```

### Stop the bot

```bash
sudo systemctl stop bread-paper
sudo systemctl stop bread-live
```

### Pause live trading without uninstalling

```bash
sudo systemctl disable --now bread-live
```

(Re-enable with `sudo systemctl enable --now bread-live`.)

### Update code and restart

```bash
sudo bash /home/bread/bread/deploy/update.sh
```

`update.sh` pulls the latest code, refreshes systemd unit files, and restarts whichever `bread-*` services are currently enabled. Paper-only deployments keep working — the script skips `bread-live` if it isn't enabled.

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
journalctl -u bread-paper --since "5 min ago" --no-pager
journalctl -u bread-live  --since "5 min ago" --no-pager
# Check for missing API keys, import errors, or connectivity issues
```

**`bread-live` keeps restarting:**
- Most likely missing `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY` in `/home/bread/.env`. The `_check_credentials` validator will refuse to start.
- Or the unit is missing `Environment=BREAD_LIVE_CONFIRM=I_UNDERSTAND` (deliberately added by setup-gcp.sh; if you regenerated the unit by hand, add it back).

**Dashboard not loading:**
1. Verify Tailscale is connected on your device (open the Tailscale app — should show green/Connected)
2. Verify Tailscale is running on the VM: `tailscale status`
3. Verify the paper bot is running: `systemctl status bread-paper`
4. Verify port 8050 is listening: `ss -tlnp | grep 8050` (should show `0.0.0.0:8050`)
5. The live process runs `--no-dashboard`. If you want a live dashboard, start it on demand: `bread dashboard --mode live --port 8051`.

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
sudo systemctl restart bread-paper bread-live
```

**Alpaca API errors:**
```bash
# Verify keys are set correctly
sudo -u bread cat /home/bread/.env
# Re-enter keys if needed
sudo -u bread nano /home/bread/.env
sudo systemctl restart bread-paper bread-live
```
