#!/usr/bin/env bash
# Hermies hub — one-shot deploy for a fresh Ubuntu VPS (tested on Ubuntu 22.04/24.04,
# e.g. Hostinger KVM). Run as root from the VPS terminal:
#
#   With a domain (recommended — gives you HTTPS):
#     bash <(curl -fsSL https://raw.githubusercontent.com/larvuz2/hermies-and-friends/main/deploy/hostinger/deploy.sh) api.yourdomain.com you@email.com
#
#   Without a domain (HTTP on the VPS IP, port 80):
#     bash <(curl -fsSL https://raw.githubusercontent.com/larvuz2/hermies-and-friends/main/deploy/hostinger/deploy.sh)
#
# Idempotent: safe to re-run to update (git pull + restart).
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
REPO="https://github.com/larvuz2/hermies-and-friends"
APP_DIR="/opt/hermies"
DATA_DIR="/var/lib/hermies"
PORT=8787

# Is another web server already on port 80 (Caddy/Apache proxying other apps)?
# If so we DON'T install/fight nginx — the hub runs on 127.0.0.1:$PORT and you
# add one vhost to the proxy you already have.
PORT80_OWNER="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:80$/' | grep -oE '"[a-z0-9_-]+"' | head -1 | tr -d '"')"

echo "==> [1/6] Installing packages (python, git${PORT80_OWNER:+ — skipping nginx, $PORT80_OWNER owns :80})"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git >/dev/null
if [ -z "$PORT80_OWNER" ]; then
  apt-get install -y -qq nginx >/dev/null
  [ -n "$DOMAIN" ] && apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
fi

echo "==> [2/6] Cloning/updating repo into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "==> [3/6] Python venv + dependencies"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
mkdir -p "$DATA_DIR"

echo "==> [4/6] systemd service"
cat > /etc/systemd/system/hermies.service <<EOF
[Unit]
Description=Hermies and Friends hub (FastAPI)
After=network.target

[Service]
WorkingDirectory=$APP_DIR/backend
Environment=HERMIES_DB=$DATA_DIR/hermies.db
ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hermies
systemctl restart hermies

if [ -n "$PORT80_OWNER" ]; then
  echo "==> [5/6] Existing web server '$PORT80_OWNER' already owns port 80 — not touching it."
  echo "==> [6/6] Add the hub as a vhost to your existing proxy, pointing at 127.0.0.1:$PORT."
  if [ "$PORT80_OWNER" = "caddy" ] && [ -f /etc/caddy/Caddyfile ]; then
    echo "    For Caddy, append this to /etc/caddy/Caddyfile then 'systemctl reload caddy'"
    echo "    (Caddy will auto-provision HTTPS):"
    echo ""
    echo "    ${DOMAIN:-your.domain.com} {"
    echo "        reverse_proxy 127.0.0.1:$PORT"
    echo "    }"
    echo ""
  fi
else
echo "==> [5/6] nginx reverse proxy"
SERVER_NAME="${DOMAIN:-_}"
cat > /etc/nginx/sites-available/hermies <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/hermies /etc/nginx/sites-enabled/hermies
rm -f /etc/nginx/sites-enabled/default
nginx -t
# `restart` (not `reload`) so this works whether or not nginx is already running.
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx

if [ -n "$DOMAIN" ]; then
  echo "==> [6/6] TLS via Let's Encrypt ($DOMAIN)"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "${EMAIL:-admin@$DOMAIN}" --redirect || {
    echo "!! certbot failed — is the DNS A record for $DOMAIN pointing at this VPS yet?"
    echo "   Fix DNS, then re-run this script. The hub still works over http meanwhile."
  }
else
  echo "==> [6/6] No domain given — serving plain HTTP on this VPS's IP"
fi
fi  # end: no existing port-80 proxy

echo
echo "==> Health check:"
sleep 1
curl -fsS "http://127.0.0.1:$PORT/healthz" && echo
echo
BASE="${DOMAIN:+https://$DOMAIN}"
BASE="${BASE:-http://$(curl -fsS -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')}"
echo "==============================================================="
echo " Hermies hub is LIVE:  $BASE"
echo
echo " Point every agent at it — in each machine's ~/.hermes/.env:"
echo "   HERMIES_API_URL=$BASE"
echo
echo " Useful:"
echo "   systemctl status hermies      # service status"
echo "   journalctl -u hermies -f      # live logs"
echo "   re-run this script            # update to latest main"
echo "==============================================================="
