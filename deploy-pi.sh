#!/bin/bash
# Krama — Raspberry Pi Deployment Script
# Run this ON the Pi after copying the project over

set -e

echo "========================================"
echo "  Krama — Raspberry Pi Setup"
echo "========================================"

# 1. System dependencies
echo "[1/6] Installing system packages..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx

# 2. Create app directory and venv
echo "[2/6] Setting up Python environment..."
cd ~/ai-astro
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn

# 3. Create systemd service (auto-start on boot)
echo "[3/6] Creating systemd service..."
sudo tee /etc/systemd/system/krama.service > /dev/null <<UNIT
[Unit]
Description=Krama Vedic Astrology App
After=network.target

[Service]
User=$USER
WorkingDirectory=$HOME/ai-astro
Environment="PATH=$HOME/ai-astro/.venv/bin"
ExecStart=$HOME/ai-astro/.venv/bin/gunicorn --bind 127.0.0.1:5001 --workers 2 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable krama
sudo systemctl start krama

# 4. Configure nginx reverse proxy
echo "[4/6] Configuring nginx..."
sudo tee /etc/nginx/sites-available/krama > /dev/null <<'NGINX'
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_buffering off;
    }

    location /api/reading/stream {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/krama /etc/nginx/sites-enabled/krama
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# 5. Get network info
echo "[5/6] Network info..."
PI_IP=$(hostname -I | awk '{print $1}')
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "could not detect")

echo ""
echo "[6/6] Setting up DuckDNS (free dynamic DNS)..."
echo ""
echo "========================================"
echo "  SETUP COMPLETE!"
echo "========================================"
echo ""
echo "  Pi local IP:  $PI_IP"
echo "  Public IP:    $PUBLIC_IP"
echo ""
echo "  Local access: http://$PI_IP"
echo ""
echo "  NEXT STEPS for external access:"
echo ""
echo "  1. PORT FORWARDING (do this on your router):"
echo "     - Login to your router admin panel"
echo "     - Add port forwarding rule:"
echo "       External port 80 → $PI_IP port 80 (TCP)"
echo ""
echo "  2. After port forwarding, anyone can access:"
echo "     http://$PUBLIC_IP"
echo ""
echo "  3. OPTIONAL — Free domain via DuckDNS:"
echo "     - Go to https://www.duckdns.org"
echo "     - Sign in with Google/GitHub"
echo "     - Create a subdomain (e.g. krama.duckdns.org)"
echo "     - Point it to your public IP: $PUBLIC_IP"
echo "     - Then run this to auto-update your IP:"
echo "       echo \"*/5 * * * * curl -s 'https://www.duckdns.org/update?domains=YOUR_SUBDOMAIN&token=YOUR_TOKEN'\" | crontab -"
echo ""
echo "  Useful commands:"
echo "     sudo systemctl status krama   # check app status"
echo "     sudo systemctl restart krama  # restart app"
echo "     sudo journalctl -u krama -f   # view logs"
echo "========================================"
