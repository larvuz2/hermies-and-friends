#!/usr/bin/env bash
# Hermix hub — one-shot deploy for a fresh Ubuntu VPS (tested on Ubuntu 22.04/24.04,
# e.g. Hostinger KVM). Run as root from the VPS terminal:
#
#   With a domain (recommended — gives you HTTPS):
#     bash <(curl -fsSL https://raw.githubusercontent.com/larvuz2/hermix/main/deploy/hostinger/deploy.sh) api.yourdomain.com you@email.com
#
#   Without a domain (HTTP on the VPS IP, port 80):
#     bash <(curl -fsSL https://raw.githubusercontent.com/larvuz2/hermix/main/deploy/hostinger/deploy.sh)
#
# Idempotent: safe to re-run to update (git pull + restart).
#
# ---------------------------------------------------------------------------
# Deploy semantics: AVAILABILITY-FIRST, and deliberately so.
#
# Order is: pull -> install -> restart -> product check. The restart happens
# BEFORE the check, so if the check fails the new revision is already serving.
# Nothing is rolled back automatically. That is a choice, not an oversight:
#
#   * The hub degrades rather than dies. Its one genuine failure mode here is
#     falling back to hashing embeddings, which still answers every request —
#     just with materially worse matching. Taking it offline to protect quality
#     would trade a degraded network for no network.
#   * The usual cause is environmental, not the code. The model download failed
#     on this box. Reverting the commit does not bring the model back; it just
#     silently undoes everything else the deploy carried, which could include a
#     security fix.
#   * An automatic revert is a destructive, surprising action to take on
#     someone's running service on the strength of one failing check.
#
# So on failure the script tells you exactly what is live, refuses to call the
# deploy a success (non-zero exit), and prints the precise rollback command with
# the pre-deploy revision filled in. Rolling back is a decision you make, not
# one the script makes for you. See rollback.sh.
#
# The check is also safe to re-run on its own at any time:
#   /opt/hermix/venv/bin/python /opt/hermix/deploy/hostinger/smoke.py
# ---------------------------------------------------------------------------
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
REPO="https://github.com/larvuz2/hermix"
APP_DIR="/opt/hermix"
DATA_DIR="/var/lib/hermix"
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
# Remember what was serving BEFORE the pull, so a failed product check can name
# the exact revision to go back to. See "Deploy semantics" in the header.
PREV_REV=""
if [ -d "$APP_DIR/.git" ]; then
  PREV_REV="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
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
cat > /etc/systemd/system/hermix.service <<EOF
[Unit]
Description=Hermix hub (FastAPI)
After=network.target

[Service]
WorkingDirectory=$APP_DIR/backend
Environment=HERMIX_DB=$DATA_DIR/hermix.db
ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hermix
systemctl restart hermix

echo "==> [4b/6] daily database backup"
# The database is the only copy of every account key, handle and card. This is
# installed here rather than left as a manual step precisely because a backup
# nobody remembered to set up is the same as no backup.
apt-get install -y --no-install-recommends sqlite3 >/dev/null 2>&1 || true
cat > /etc/cron.daily/hermix-backup <<EOF
#!/bin/sh
HERMIX_DB=$DATA_DIR/hermix.db exec sh $APP_DIR/deploy/hostinger/backup.sh
EOF
chmod +x /etc/cron.daily/hermix-backup
# Take one immediately so a fresh deploy is never a day away from its first
# backup, and so a broken setup is discovered now instead of after an incident.
if HERMIX_DB="$DATA_DIR/hermix.db" sh "$APP_DIR/deploy/hostinger/backup.sh"; then
  echo "    first backup taken; restore with deploy/hostinger/restore.sh"
else
  echo "    WARNING: the first backup FAILED — fix this before going public."
fi

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
cat > /etc/nginx/sites-available/hermix <<EOF
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
ln -sf /etc/nginx/sites-available/hermix /etc/nginx/sites-enabled/hermix
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

# Liveness is not quality. The hub stays up on fallback embeddings, which is a
# much weaker product than the one we advertise — and a 200 looks identical
# either way. Assert the real model loaded AND that a live query connects two
# cards sharing no vocabulary, before anyone is told the deploy succeeded.
echo "==> Product check:"
SMOKE_STATUS=0
"$APP_DIR/venv/bin/python" "$APP_DIR/deploy/hostinger/smoke.py" \
  "http://127.0.0.1:$PORT" || SMOKE_STATUS=$?
if [ "$SMOKE_STATUS" -ne 0 ]; then
  echo
  echo "!!! DEPLOY INCOMPLETE — read this, the new code IS live."
  echo "!!!"
  echo "!!! This deploy is AVAILABILITY-FIRST: the restart already happened, so"
  echo "!!! the new revision is serving right now and existing agents keep"
  echo "!!! working. It is NOT rolled back automatically, because the usual"
  echo "!!! cause of this failure is environmental (the embedding model could"
  echo "!!! not be downloaded on this box) and reverting the code would not fix"
  echo "!!! it — it would only silently undo whatever else this deploy carried."
  echo "!!!"
  echo "!!! DO NOT invite new users until the check above passes."
  echo "!!!"
  echo "!!! Re-run the check alone once you have fixed the cause:"
  echo "!!!   $APP_DIR/venv/bin/python $APP_DIR/deploy/hostinger/smoke.py"
  if [ -n "$PREV_REV" ]; then
    echo "!!!"
    echo "!!! If you decide the NEW CODE is at fault, roll back explicitly:"
    echo "!!!   bash $APP_DIR/deploy/hostinger/rollback.sh $PREV_REV"
  fi
  exit "$SMOKE_STATUS"
fi
echo

BASE="${DOMAIN:+https://$DOMAIN}"
BASE="${BASE:-http://$(curl -fsS -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')}"
echo "==============================================================="
echo " Hermix hub is LIVE:  $BASE"
echo
echo " Point every agent at it — in each machine's ~/.hermes/.env:"
echo "   HERMIX_API_URL=$BASE"
echo
echo " Useful:"
echo "   systemctl status hermix      # service status"
echo "   journalctl -u hermix -f      # live logs"
echo "   re-run this script            # update to latest main"
echo "==============================================================="
