#!/bin/bash
# ── Tower Finder: Full Server Setup + Security Hardening ──────────────────────
# Run this on a fresh Ubuntu 24.04 DigitalOcean droplet as root.
#
# Usage:
#   MAPRAD_API_KEY=... RADAR_API_KEY=... bash setup-server.sh                    # prod
#   DEPLOY_PROFILE=test MAPRAD_API_KEY=... RADAR_API_KEY=... bash setup-server.sh # retina-test
#
# Prerequisites:
#   - SSH access as root
#   - The repo should be copied to the server (via git clone or scp)
#   - A Cloudflare Origin cert at /etc/ssl/cloudflare/{cert,key}.pem BEFORE the
#     compose bring-up below, or nginx cannot bind 443 and the app crash-loops.
set -euo pipefail

# Secrets come from the environment, not argv: keeps them out of `ps` output and
# shell history, and is order-independent (no positional-arg swap footgun). The
# `:?` form aborts with the given message if the var is unset or empty.
: "${MAPRAD_API_KEY:?set MAPRAD_API_KEY (Maprad tower API key)}"
: "${RADAR_API_KEY:?set RADAR_API_KEY (ingest key: the prod backend enforces X-API-Key on ingest and the fleet reads it from backend/.env, so its pushes 401 without a match)}"

# ── Deploy profile ───────────────────────────────────────────────────────────
# Which environment this box becomes. It selects the compose override and the
# browser-facing CORS origins, the only two things that differ at provisioning
# time; everything else (hardening, docker, log rotation) is identical.
#
# Defaults to `prod` so an unqualified run behaves exactly as it always has.
# Staging is absent deliberately: that box is provisioned and redeployed by CI
# (.github/workflows/ci.yml), never by hand.
DEPLOY_PROFILE="${DEPLOY_PROFILE:-prod}"
case "$DEPLOY_PROFILE" in
    prod)
        # Browser-facing origins, from deploy/nginx.conf server_name (not retina.fm).
        CORS_ORIGINS="https://towers.retina.fm,https://map.retina.fm,https://testmap.retina.fm,https://dash.retina.fm,https://admin.retina.fm,https://api.retina.fm"
        COMPOSE_FILES=(-f docker-compose.yml)
        ;;
    test)
        # From deploy/nginx-test.conf server_name. Kept in step with the
        # CORS_ORIGINS in docker-compose.test.yml, which overrides this at runtime;
        # writing it to .env as well keeps the file self-consistent if the stack is
        # ever brought up without the override.
        CORS_ORIGINS="https://test-towers.retina.fm,https://test-api.retina.fm,https://test-map.retina.fm,https://test-dash.retina.fm"
        COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.test.yml)
        ;;
    *)
        echo "Unknown DEPLOY_PROFILE=$DEPLOY_PROFILE (use: prod | test)" >&2
        exit 1
        ;;
esac

echo "══════════════════════════════════════════════════"
echo "  Tower Finder — Server Setup & Hardening"
echo "  Profile: ${DEPLOY_PROFILE}"
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
    git submodule update --init --recursive
elif [ -f "$APP_DIR/docker-compose.yml" ]; then
    # A tree with no .git but with the app in it: `just deploy-test` rsync'd it
    # here. That is the supported no-git workflow for retina-test, so use what is
    # already on disk rather than cloning over the top of it (which git refuses to
    # do into a non-empty directory anyway).
    echo "  Existing non-git tree found (rsync'd); using it as-is..."
    cd "$APP_DIR"
else
    echo "  Cloning repository..."
    # Canonical org repo. (The live boxes use the SSH remote
    # git@github.com:offworldlabs/Tower-Finder.git, but a fresh droplet has no
    # deploy key yet, so clone over HTTPS; `git remote set-url` to SSH later if
    # you add a key.) Must NOT be a personal fork — this becomes `origin`, which
    # every subsequent CI `git fetch origin main` deploys from.
    git clone --recursive https://github.com/offworldlabs/Tower-Finder.git "$APP_DIR"
    cd "$APP_DIR"
fi

# Create .env with API keys + CORS for the selected profile's domains.
#
# This writes only the three values needed to boot the app + fleet. A live box
# carries more that this script does not know about: RETINA_ENV, R2_* archive
# storage, TOWER_API_URL, COVERAGE_ENABLED, JWT_SECRET, the OAuth client IDs.
# Production's RETINA_ENV=test in particular exists ONLY in its backend/.env (no
# compose file sets it), so a blind overwrite here would silently unset it and
# change how the backend gates auth. Refuse to clobber an existing file: on a
# fresh droplet there is nothing to lose, and on an existing one the operator
# should merge by hand knowing what is already there.
if [ -f backend/.env ]; then
    echo "  backend/.env already exists — leaving it untouched."
    echo "  If this box needs the profile's values, merge them in yourself:"
    echo "    CORS_ORIGINS=${CORS_ORIGINS}"
    echo "    (MAPRAD_API_KEY / RADAR_API_KEY as passed to this script)"
else
    {
        echo "MAPRAD_API_KEY=${MAPRAD_API_KEY}"
        echo "RADAR_API_KEY=${RADAR_API_KEY}"
        echo "CORS_ORIGINS=${CORS_ORIGINS}"
    } > backend/.env
    chmod 600 backend/.env
fi

# Fail before the build rather than crash-looping on a missing or unreadable cert:
# every profile this script provisions serves TLS, so nginx cannot start without
# one.
if [ ! -f /etc/ssl/cloudflare/cert.pem ] || [ ! -f /etc/ssl/cloudflare/key.pem ]; then
    echo "  ✗ No Cloudflare Origin cert at /etc/ssl/cloudflare/{cert,key}.pem." >&2
    echo "    Create one at Cloudflare → SSL/TLS → Origin Server and install it" >&2
    echo "    there, then re-run. Nothing else in this script needs redoing." >&2
    exit 1
fi

# Permissions, not just presence. nginx runs as appuser (uid 999) inside the image,
# NOT as root, so a cert installed root-owned 600 in a 700 directory is invisible to
# it and the container crash-loops on
#   [emerg] cannot load certificate ... Permission denied
# which reads like a missing file rather than a permissions problem.
#
# Check first, repair only if the check fails. A box whose cert already works is
# left exactly as it is: this script's default profile is prod, so an unconditional
# chown/chmod here would silently rewrite the ownership of production's live TLS
# private key on any re-run. Changing permissions on live key material should be a
# deliberate act, not a side effect of re-running provisioning.
CONTAINER_UID=999
_cert_readable() {
    setpriv --reuid "$1" --regid "$1" --clear-groups \
        head -c 1 /etc/ssl/cloudflare/cert.pem >/dev/null 2>&1 \
    && setpriv --reuid "$1" --regid "$1" --clear-groups \
        head -c 1 /etc/ssl/cloudflare/key.pem >/dev/null 2>&1
}
if _cert_readable "${CONTAINER_UID}"; then
    echo "  ✓ cert already readable by the container user (uid ${CONTAINER_UID}); left untouched"
else
    echo "  → cert not readable by uid ${CONTAINER_UID}; repairing permissions"
    chmod 755 /etc/ssl/cloudflare
    chmod 644 /etc/ssl/cloudflare/cert.pem
    # The key gets the narrower grant: owned by the container uid, 640, so it is
    # not world-readable. (Production predates this and has it 644; tightening
    # that is tracked separately and is not done implicitly from here.)
    chown "${CONTAINER_UID}:${CONTAINER_UID}" /etc/ssl/cloudflare/key.pem
    chmod 640 /etc/ssl/cloudflare/key.pem
    if ! _cert_readable "${CONTAINER_UID}"; then
        echo "  ✗ uid ${CONTAINER_UID} still cannot read /etc/ssl/cloudflare/{cert,key}.pem." >&2
        echo "    nginx runs as that uid in the container and will crash-loop." >&2
        exit 1
    fi
    echo "  ✓ cert readable by the container user (uid ${CONTAINER_UID})"
fi

# Build and start. COMPOSE_FILES carries the profile's override (empty for prod,
# which runs the base alone) so the box comes up on its own domains and nginx
# profile rather than inheriting production's.
docker compose "${COMPOSE_FILES[@]}" up -d --build

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
        docker compose "${COMPOSE_FILES[@]}" logs --tail 20
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
docker compose "${COMPOSE_FILES[@]}" up -d

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  ✓ Setup complete!"
echo "══════════════════════════════════════════════════"
echo ""
echo "  Profile: ${DEPLOY_PROFILE}"
echo "  Compose: docker compose ${COMPOSE_FILES[*]}"
echo "  App running at: http://$(curl -s ifconfig.me)"
echo ""
echo "  Security:"
echo "    - UFW enabled (22, 80, 443 only)"
echo "      NOTE: TCP node ingest on 3012 is published by Compose but NOT opened"
echo "      here. Run 'ufw allow 3012/tcp' if real nodes must reach this box."
echo "    - SSH: key-only auth, no password"
echo "    - fail2ban: active on SSH"
echo "    - Automatic security updates: enabled"
echo "    - Docker log rotation: enabled"
echo ""