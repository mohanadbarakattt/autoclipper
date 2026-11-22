#!/usr/bin/env bash
# Virlo Clips service: one-time server setup (Ubuntu 22.04/24.04, x86_64 or ARM / Oracle Ampere A1).
# Safe to run again: every step checks before it changes anything.
#
#   curl -fsSL https://raw.githubusercontent.com/mohanadbarakattt/autoclipper/saas/deploy/oracle/setup.sh -o setup.sh
#   sudo bash setup.sh                       # HTTPS with Caddy (default)
#   sudo HTTPS=tunnel bash setup.sh          # or Cloudflare Tunnel (zone must be on Cloudflare)
#
# Env overrides: REPO_URL, BRANCH (default saas), DOMAIN (default clips-api.virlo.studio), HTTPS (caddy|tunnel|none)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/mohanadbarakattt/autoclipper.git}"
BRANCH="${BRANCH:-saas}"
DOMAIN="${DOMAIN:-clips-api.virlo.studio}"
HTTPS="${HTTPS:-caddy}"
APP_USER=virlo
APP_DIR=/opt/virlo-clips
DATA_DIR=/var/lib/virlo-clips
ENV_DIR=/etc/virlo-clips
ENV_FILE="$ENV_DIR/saas.env"

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

say "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ffmpeg python3 python3-venv python3-dev build-essential sqlite3 curl \
  unattended-upgrades fonts-dejavu-core >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null || true

say "Swap (4 GB) so Whisper never gets OOM-killed"
if ! swapon --show 2>/dev/null | grep -q /swapfile; then
  [ -f /swapfile ] || { fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null; }
  if swapon /swapfile 2>/dev/null; then
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    echo "swap not allowed here (container?), skipping"; rm -f /swapfile
  fi
fi

say "App user and folders"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$APP_USER --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g "$APP_USER" -m 750 "$ENV_DIR"

say "Code ($BRANCH)"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch -q origin "$BRANCH" && git -C "$APP_DIR" checkout -q "$BRANCH" && git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Python environment (first run downloads ~1 GB)"
sudo -u "$APP_USER" bash -c "
  cd $APP_DIR
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt -r saas/requirements.txt
  .venv/bin/python scripts/get_fonts.py
"

say "Config ($ENV_FILE)"
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/saas.env.example" "$ENV_FILE"
  token=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  media=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i \
    -e "s|^SAAS_MODE=.*|SAAS_MODE=service|" \
    -e "s|^BILLING=.*|BILLING=virlo|" \
    -e "s|^CLIPS_SERVICE_TOKEN=.*|CLIPS_SERVICE_TOKEN=$token|" \
    -e "s|^SAAS_MEDIA_SECRET=.*|SAAS_MEDIA_SECRET=$media|" \
    -e "s|^SAAS_PUBLIC_URL=.*|SAAS_PUBLIC_URL=https://$DOMAIN|" \
    -e "s|^SAAS_HOST=.*|SAAS_HOST=127.0.0.1|" \
    -e "s|^SAAS_PORT=.*|SAAS_PORT=8101|" \
    -e "s|^SAAS_COOKIE_SECURE=.*|SAAS_COOKIE_SECURE=1|" \
    -e "s|^SAAS_DATA_DIR=.*|SAAS_DATA_DIR=$DATA_DIR|" \
    -e "s|^SAAS_MAX_PARALLEL_JOBS=.*|SAAS_MAX_PARALLEL_JOBS=1|" \
    "$ENV_FILE"
  echo "Created $ENV_FILE with a fresh CLIPS_SERVICE_TOKEN (copy it to Vercel later)."
fi
chown root:"$APP_USER" "$ENV_FILE" && chmod 640 "$ENV_FILE"
# the app reads saas.env from its own folder
ln -sfn "$ENV_FILE" "$APP_DIR/saas.env"

say "Services"
install -m 644 "$APP_DIR"/deploy/systemd/*.service "$APP_DIR"/deploy/systemd/*.timer /etc/systemd/system/
install -m 755 "$APP_DIR/deploy/oracle/backup.sh" /usr/local/bin/virlo-clips-backup
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now virlo-clips-web.service virlo-clips-worker.service virlo-clips-backup.timer
else
  echo "systemd isn't running here (container?): units installed but not started"
fi

say "Firewall (Oracle Ubuntu images ship strict iptables)"
if [ "$HTTPS" = "caddy" ]; then
  for port in 80 443; do
    if ! iptables -C INPUT -m state --state NEW -p tcp --dport $port -j ACCEPT 2>/dev/null; then
      # insert before Oracle's catch-all REJECT rule (append if there is none)
      reject=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
      if [ -n "$reject" ]; then
        iptables -I INPUT "$reject" -m state --state NEW -p tcp --dport $port -j ACCEPT
      else
        iptables -A INPUT -m state --state NEW -p tcp --dport $port -j ACCEPT
      fi
    fi
  done
  if command -v netfilter-persistent >/dev/null; then netfilter-persistent save >/dev/null; fi
fi

case "$HTTPS" in
  caddy)
    say "HTTPS with Caddy for $DOMAIN"
    if ! command -v caddy >/dev/null; then
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq && apt-get install -y -qq caddy >/dev/null
    fi
    sed "s|{{DOMAIN}}|$DOMAIN|g" "$APP_DIR/deploy/caddy/Caddyfile" > /etc/caddy/Caddyfile
    if [ -d /run/systemd/system ]; then systemctl reload caddy || systemctl restart caddy; fi
    ;;
  tunnel)
    say "Cloudflare Tunnel"
    if ! command -v cloudflared >/dev/null; then
      arch=$(dpkg --print-architecture)
      curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch.deb"
      dpkg -i /tmp/cloudflared.deb >/dev/null
    fi
    if [ -n "${TUNNEL_TOKEN:-}" ]; then
      cloudflared service install "$TUNNEL_TOKEN" >/dev/null 2>&1 || true
      echo "Tunnel service installed. In Cloudflare, route $DOMAIN -> http://127.0.0.1:8101"
    else
      echo "Re-run with TUNNEL_TOKEN=... (Cloudflare Zero Trust > Tunnels > Create) to finish."
    fi
    ;;
  none) say "No HTTPS front set up (HTTPS=none)" ;;
esac

say "Health"
if [ -d /run/systemd/system ]; then
  sleep 3
  curl -fsS http://127.0.0.1:8101/health && echo
fi
echo
echo "Done. Next: put your paid GEMINI_API_KEY and ANTHROPIC_API_KEY in $ENV_FILE, then"
echo "  sudo systemctl restart virlo-clips-web virlo-clips-worker"
echo "Service token for Vercel:  sudo grep ^CLIPS_SERVICE_TOKEN $ENV_FILE"
