#!/bin/bash
# ── Tower Finder: Full Server Setup + Security Hardening ──────────────────────
# Run this on a fresh Ubuntu 24.04 DigitalOcean droplet as root.
# Usage: bash setup-server.sh <MAPRAD_API_KEY>
#
# Prerequisites:
#   - SSH access as root
#   - The repo should be copied to the server (via git clone or scp)
set -euo pipefail

MAPRAD_API_KEY="${1:-}"
if [ -z "$MAPRAD_API_KEY" ]; then
    echo "Usage: bash setup-server.sh <MAPRAD_API_KEY>"
    exit 1
fi

echo "══════════════════════════════════════════════════"
echo "  Tower Finder — Server Setup & Hardening"
echo "══════════════════════════════════════════════════"

# ── 1. System updates ────────────────────────────────────────────────────────
echo ""
echo "→ Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

# ── 2. Security hardening ────────────────────────────────────────────────────
echo ""
echo "→ Configuring firewall (UFW)..."
apt-get install -y -qq ufw

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP
ufw allow 443/tcp   # HTTPS
ufw --force enable

echo ""
echo "→ Hardening SSH..."
# Disable password authentication (key-only)
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
# Disable X11 forwarding
sed -i 's/^#*X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
# Set idle timeout (10 min)
sed -i 's/^#*ClientAliveInterval.*/ClientAliveInterval 600/' /etc/ssh/sshd_config
sed -i 's/^#*ClientAliveCountMax.*/ClientAliveCountMax 2/' /etc/ssh/sshd_config
systemctl restart sshd

echo ""
echo "→ Installing fail2ban..."
apt-get install -y -qq fail2ban
cat > /etc/fail2ban/jail.local << 'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
maxretry = 5
findtime = 600
bantime = 3600
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo ""
echo "→ Configuring automatic security updates..."
apt-get install -y -qq unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# ── 3. Install Docker ────────────────────────────────────────────────────────
echo ""
echo "→ Installing Docker..."
apt-get install -y -qq docker.io docker-compose-v2
systemctl enable --now docker

# ── 4. Deploy application ────────────────────────────────────────────────────
echo ""
echo "→ Deploying Tower Finder..."

APP_DIR="/opt/tower-finder"

if [ -d "$APP_DIR/.git" ]; then
    echo "  Repo exists, pulling latest..."
    cd "$APP_DIR"
    git pull
else
    echo "  Cloning repository..."
    git clone https://github.com/pavlodef/Tower-Finder.git "$APP_DIR"
    cd "$APP_DIR"
fi

# Create .env with API key
echo "MAPRAD_API_KEY=${MAPRAD_API_KEY}" > backend/.env
# CORS for production domains
echo 'CORS_ORIGINS=https://retina.fm,https://api.retina.fm,http://localhost:5173' >> backend/.env

chmod 600 backend/.env

# Build and start
docker compose up -d --build

echo ""
echo "→ Waiting for health check..."
sleep 5
for i in $(seq 1 12); do
    if curl -sf http://localhost/api/health > /dev/null 2>&1; then
        echo "  ✓ Health check passed!"
        break
    fi
    if [ "$i" -eq 12 ]; then
        echo "  ✗ Health check failed after 60 seconds"
        echo "  Checking logs:"
        docker compose logs --tail 20
        exit 1
    fi
    sleep 5
done

# ── 5. Setup log rotation for Docker ─────────────────────────────────────────
echo ""
echo "→ Configuring Docker log rotation..."
cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
systemctl restart docker
# Restart app after Docker restart
cd "$APP_DIR"
docker compose up -d

# ── 6. Setup nightly backup timer ─────────────────────────────────────────────
echo ""
echo "→ Installing nightly backup timer..."
cp "$APP_DIR/deploy/retina-backup.service" /etc/systemd/system/
cp "$APP_DIR/deploy/retina-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now retina-backup.timer
echo "  ✓ Backup timer installed (daily at 03:00 UTC)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  ✓ Setup complete!"
echo "══════════════════════════════════════════════════"
echo ""
echo "  App running at: http://$(curl -s ifconfig.me)"
echo ""
echo "  Security:"
echo "    - UFW enabled (22, 80, 443 only)"
echo "    - SSH: key-only auth, no password"
echo "    - fail2ban: active on SSH"
echo "    - Automatic security updates: enabled"
echo "    - Docker log rotation: enabled"
echo ""