# AlgoFoundry — DigitalOcean Deployment Guide

## Droplet Specs

- **Image:** Ubuntu 24.04
- **Plan:** Regular $12/mo (2 vCPU, 2GB RAM, 60GB SSD)
- **Region:** NYC1 or NYC3 (lowest latency to IBKR servers in Greenwich, CT)
- **Auth:** SSH key (same one you generated for Oracle)
- **VPC:** Default

## Step 1: Create the Droplet

In the DigitalOcean dashboard:
1. Create → Droplets
2. Select Ubuntu 24.04, Regular $12/mo, NYC1
3. Add your SSH key
4. Name it `algofoundry`
5. Create Droplet

Note the public IP address.

## Step 2: Run the Setup Script

From your local machine (in the AlgoFoundry project root):

```bash
# Upload the entire project first
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
    ./ root@<DROPLET_IP>:/opt/algofoundry/

# Run the setup script
ssh root@<DROPLET_IP> 'bash /opt/algofoundry/deploy/setup.sh'
```

## Step 3: Configure Environment

SSH into the droplet and edit the `.env` file:

```bash
ssh root@<DROPLET_IP>
nano /opt/algofoundry/.env
```

Set your actual values for all API keys and credentials. Make sure the file
has restricted permissions (the setup script does this, but verify):

```bash
ls -la /opt/algofoundry/.env
# Should show: -rw------- 1 algofoundry algofoundry
```

## Step 4: Start the App

```bash
systemctl start algofoundry
systemctl status algofoundry
```

Check logs if something goes wrong:

```bash
journalctl -u algofoundry -f
```

## Step 5: Verify

Visit `http://<DROPLET_IP>` in your browser. You should see the AlgoFoundry
login page. Log in with the credentials from your `.env`.

## Step 6: HTTPS (Optional but Recommended)

Point your domain's A record to the droplet IP, then:

```bash
certbot --nginx -d yourdomain.com
```

Certbot auto-renews via a systemd timer. Verify with:

```bash
systemctl list-timers | grep certbot
```

## Step 7: IBKR Gateway

You need IB Gateway or TWS running somewhere your droplet can reach.
Options:

1. **Run IB Gateway on the droplet itself** (headless via IBC + Xvfb):
   ```bash
   apt install -y xvfb
   # Download IB Gateway from IBKR website
   # Use IBC (https://github.com/IbcAlpha/IBC) for auto-login
   ```

2. **Run TWS on your local machine** and tunnel:
   ```bash
   # From your local machine:
   ssh -R 4001:127.0.0.1:4001 root@<DROPLET_IP>
   ```
   This forwards the droplet's port 4001 to your local TWS.

## Common Commands

```bash
# Restart after code changes
systemctl restart algofoundry

# View live logs
journalctl -u algofoundry -f

# Deploy code updates
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
    ./ root@<DROPLET_IP>:/opt/algofoundry/
ssh root@<DROPLET_IP> 'systemctl restart algofoundry'

# Check resource usage
htop

# Nginx logs
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

## Security Checklist

- [ ] Change default SSH port (optional): edit `/etc/ssh/sshd_config`
- [ ] Disable password auth: `PasswordAuthentication no` in sshd_config
- [ ] Set strong ALGOFOUNDRY_PASSWORD in .env
- [ ] Set ALGOFOUNDRY_WEBHOOK_SECRET for TradingView
- [ ] Enable HTTPS via certbot
- [ ] Rotate API keys that were previously committed to version control

## IMPORTANT: Rotate Your API Keys

Your `.env` file currently contains API keys in plain text. Since these have
been in the repo, consider them compromised. Rotate all of these:

- OPENROUTER_API
- ALPHA_VANTAGE_API
- FINNHUB_API
- GROQ_API
- GEMINI_API

Also add `.env` to your `.gitignore` if it isn't already.
