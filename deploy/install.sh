#!/usr/bin/env bash
# One-time bootstrap for Oracle Cloud Free Tier (Ubuntu 22.04 ARM/x86).
# Run as the 'ubuntu' user from the cloned repo root:
#     bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/kite
APP_USER=ubuntu
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing OS packages"
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx apache2-utils rsync

echo "==> Preparing $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Copying app files (excluding .env, csvs, venv)"
rsync -a --delete \
  --exclude '.env' --exclude 'venv' --exclude '__pycache__' \
  --exclude 'mtm_*.csv' --exclude '.git' \
  "$REPO_DIR"/ "$APP_DIR"/

echo "==> Creating Python venv"
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f "$APP_DIR/.env" ]; then
  echo ""
  echo "!! No .env found at $APP_DIR/.env"
  echo "   Upload it now from your laptop:"
  echo "     scp .env ubuntu@<vm-ip>:$APP_DIR/.env"
  echo "   Then re-run this script (or just the systemd + nginx steps below)."
  exit 1
fi
chmod 600 "$APP_DIR/.env"

echo "==> Installing systemd unit"
sudo cp deploy/kite-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kite-monitor

echo "==> Configuring nginx"
sudo cp deploy/nginx-kite.conf /etc/nginx/sites-available/kite
sudo ln -sf /etc/nginx/sites-available/kite /etc/nginx/sites-enabled/kite
sudo rm -f /etc/nginx/sites-enabled/default

if [ ! -f /etc/nginx/.htpasswd-kite ]; then
  echo "==> Set a password for HTTP basic auth (user: admin)"
  sudo htpasswd -c /etc/nginx/.htpasswd-kite admin
fi

sudo nginx -t
sudo systemctl reload nginx

echo "==> Starting kite-monitor"
sudo touch /var/log/kite-monitor.log
sudo chown "$APP_USER:$APP_USER" /var/log/kite-monitor.log
sudo systemctl restart kite-monitor

echo ""
echo "==> Done."
echo "   Status:  sudo systemctl status kite-monitor"
echo "   Logs:    tail -f /var/log/kite-monitor.log"
echo "   Open:    http://<vm-public-ip>/   (login: admin / <password you set>)"
