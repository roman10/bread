# Deploying Bread to GCP Free Tier

## Prerequisites

- GCP account with billing enabled (won't be charged — free tier)
- Alpaca Paper API key and secret
- Tailscale account (free at https://tailscale.com)

## 1. Create the VM

In GCP Console → Compute Engine → VM Instances → Create Instance:

| Setting | Value |
|---------|-------|
| Name | `bread-trading` |
| Region | `us-east1` (closest to NYSE/Alpaca) |
| Zone | `us-east1-b` |
| Machine type | `e2-micro` (free tier eligible) |
| Boot disk | Debian 12, 30 GB standard persistent disk |
| Firewall | No HTTP/HTTPS needed (Tailscale handles access) |

**Cost: $0/month** under the free tier (1 non-preemptible e2-micro in us-east1/us-west1/us-central1).

## 2. SSH into the VM

```bash
gcloud compute ssh bread-trading --zone us-east1-b
```

## 3. Run Setup

```bash
# Clone the repo first (or scp the deploy/ directory)
sudo apt-get install -y git
git clone https://github.com/waterdrop86/bread.git /tmp/bread-setup

# Run setup as root
sudo bash /tmp/bread-setup/deploy/setup-gcp.sh
```

The script will:
- Install Python 3.11, create a `bread` user
- Clone the repo, create venv, install dependencies
- Prompt for Alpaca API keys
- Create 1 GB swap file
- Initialize the database
- Install Tailscale and the systemd service

## 4. Configure Tailscale

```bash
sudo tailscale up
```

Follow the auth URL to link the VM to your Tailscale network. Then:

```bash
# Note the Tailscale IP
tailscale ip -4
# Example output: 100.64.0.3
```

Install Tailscale on your phone/laptop too (https://tailscale.com/download).

## 5. Start the Bot

```bash
sudo systemctl start bread
```

## 6. Access Dashboard

From any device on your Tailscale network:

```
http://100.x.x.x:8050
```

Replace `100.x.x.x` with the VM's Tailscale IP from step 4.

## Common Operations

```bash
# Check bot status
sudo systemctl status bread

# View live logs
journalctl -u bread -f

# View recent logs
journalctl -u bread --since "1 hour ago"

# Restart
sudo systemctl restart bread

# Stop
sudo systemctl stop bread

# Trading status (account, positions)
sudo -u bread bash -c "cd /home/bread/bread && set -a && source ~/.env && set +a && .venv/bin/python -m bread status"

# Update code and restart
sudo bash /home/bread/bread/deploy/update.sh
```

## Specs vs Requirements

| Resource | VM has | App needs | Headroom |
|----------|--------|-----------|----------|
| CPU | 0.25 vCPU (burst to 2) | Bursty, ~5s every 15 min | Plenty |
| RAM | 1 GB | ~250-400 MB with dashboard | OK with swap |
| Disk | 30 GB | ~1 GB (code + deps + DB) | 29 GB free |
| Network | 1 GB/month egress | <100 MB/month API calls | Plenty |

## Troubleshooting

**Bot won't start:**
```bash
journalctl -u bread --since "5 min ago" --no-pager
# Check for missing API keys or connectivity issues
```

**Out of memory:**
```bash
free -h          # Check swap usage
swapon --show    # Verify swap is active
```

**Dashboard not loading:**
- Verify Tailscale is connected on both devices: `tailscale status`
- Verify bot is running: `systemctl status bread`
- Check if port 8050 is listening: `ss -tlnp | grep 8050`

**Tailscale disconnected:**
```bash
sudo tailscale up   # Re-authenticate if needed
tailscale status     # Check connection state
```
