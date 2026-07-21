#!/usr/bin/env bash
# ============================================================
# AlgoFoundry — DigitalOcean Droplet Setup Script
# Run as root on a fresh Ubuntu 24.04 droplet.
# Usage: ssh root@<droplet-ip> 'bash -s' < setup.sh
# ============================================================
set -euo pipefail

APP_DIR="/opt/algofoundry"
APP_USER="algofoundry"
REPO_URL=""  # Set this to your Git repo URL, or leave blank to upload manually

echo "==> Updating system packages..."
apt update && apt upgrade -y

echo "==> Installing dependencies..."
apt install -y \
    python3.12 python3.12-venv python3.12-dev \
    build-essential git nginx certbot python3-certbot-nginx \
    sqlite3 ufw htop

# ---- Firewall ----
echo "==> Configuring firewall..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# ---- App user ----
echo "==> Creating app user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$APP_USER"
fi

# ---- App directory ----
echo "==> Setting up app directory..."
mkdir -p "$APP_DIR"
chown "$APP_USER":"$APP_USER" "$APP_DIR"

# ---- Clone or prompt for upload ----
if [ -n "$REPO_URL" ]; then
    echo "==> Cloning repo..."
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
else
    echo "==> No REPO_URL set. Upload your code to $APP_DIR manually:"
    echo "    scp -r ./* algofoundry@<droplet-ip>:/opt/algofoundry/"
    echo "    (or rsync -avz ./ algofoundry@<droplet-ip>:/opt/algofoundry/)"
fi

# ---- Python venv ----
echo "==> Creating Python virtual environment..."
sudo -u "$APP_USER" python3.12 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ---- .env file ----
if [ ! -f "$APP_DIR/.env" ]; then
    echo "==> Creating placeholder .env — EDIT THIS before starting!"
    sudo -u "$APP_USER" cat > "$APP_DIR/.env" <<'EOF'
# AlgoFoundry environment — EDIT ALL VALUES
ALGOFOUNDRY_HOST=127.0.0.1
ALGOFOUNDRY_PORT=8000
ALGOFOUNDRY_USER=admin
ALGOFOUNDRY_PASSWORD=CHANGE_ME
ALGOFOUNDRY_WEBHOOK_SECRET=
ALGOFOUNDRY_DB=/opt/algofoundry/algofoundry.db
EOF
    chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

# ---- Systemd service ----
echo "==> Installing systemd service..."
cp "$APP_DIR/deploy/algofoundry.service" /etc/systemd/system/algofoundry.service
systemctl daemon-reload
systemctl enable algofoundry

# ---- Nginx ----
echo "==> Configuring Nginx..."
rm -f /etc/nginx/sites-enabled/default
cp "$APP_DIR/deploy/nginx-algofoundry.conf" /etc/nginx/sites-available/algofoundry
ln -sf /etc/nginx/sites-available/algofoundry /etc/nginx/sites-enabled/algofoundry
nginx -t && systemctl reload nginx

echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo "  1. Upload your code:  rsync -avz ./ root@<ip>:/opt/algofoundry/"
echo "  2. Edit .env:         nano /opt/algofoundry/.env"
echo "  3. Start the app:     systemctl start algofoundry"
echo "  4. Check status:      systemctl status algofoundry"
echo "  5. View logs:         journalctl -u algofoundry -f"
echo ""
echo "  For HTTPS (after pointing your domain):"
echo "  certbot --nginx -d yourdomain.com"
echo ""
