#!/bin/bash
# ── Tower Finder: Full Server Setup + Security Hardening ──────────────────────
# Run this on a fresh Ubuntu 24.04 DigitalOcean droplet as root.
#
# Usage:
#   MAPRAD_API_KEY=... RADAR_API_KEY=... bash setup-server.sh                      # prod
#   RETINA_TARGET_ENV=test MAPRAD_API_KEY=... RADAR_API_KEY=... bash setup-server.sh
#
# Prerequisites:
#   - SSH access as root
#   - The repo should be copied to the server (via git clone, scp or the rsync
#     that `just deploy-test` does)
#   - A Cloudflare Origin cert at /etc/ssl/cloudflare/{cert,key}.pem BEFORE the
#     compose bring-up below, or nginx cannot bind 443 and the app crash-loops
set -euo pipefail

# Secrets come from the environment, not argv: keeps them out of `ps` output and
# shell history, and is order-independent (no positional-arg swap footgun). The
# `:?` form aborts with the given message if the var is unset or empty.
: "${MAPRAD_API_KEY:?set MAPRAD_API_KEY (Maprad tower API key)}"
: "${RADAR_API_KEY:?set RADAR_API_KEY (ingest key: the prod backend enforces X-API-Key on ingest and the fleet reads it from backend/.env, so its pushes 401 without a match)}"

# Which environment this box becomes. It selects nothing but the compose overlay
# (see the `cp deploy/env.*.example` below); hardening, docker and log rotation
# are identical everywhere. Defaults to prod so an unqualified run behaves as it
# always has. Validated here rather than left to `cp` to fail, so a typo names
# the valid values instead of reporting a missing file.
RETINA_TARGET_ENV="${RETINA_TARGET_ENV:-prod}"
case "$RETINA_TARGET_ENV" in
    prod|staging|test) ;;
    *)
        echo "Unknown RETINA_TARGET_ENV=$RETINA_TARGET_ENV (use: prod | staging | test)" >&2
        exit 1
        ;;
esac

# Name the box after the environment it is becoming. The deploy workflow asserts
# this before it touches anything, because it is the one identity signal that
# still holds on a droplet with no ./.env yet — precisely the state a freshly
# provisioned box is in. Setting it here means a rebuild satisfies that check
# without anyone having to remember; skipping it would leave the next deploy to
# fail on a name the operator never knew mattered. Idempotent, like the rest.
hostnamectl set-hostname "retina-${RETINA_TARGET_ENV}"

echo "══════════════════════════════════════════════════"
echo "  Tower Finder — Server Setup & Hardening"
echo "  Target environment: ${RETINA_TARGET_ENV}"
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

# ufw governs host services only: SSH on 22, and anything added later running
# outside a container. It cannot govern nginx, whose ports are published by
# Docker and never traverse INPUT — see deploy/docker-user-firewall.sh. That
# boundary is applied in section 4, once the repo carrying the script is on disk.
#
# The separation is also what makes the DOCKER-USER rules safe to get wrong: they
# cannot lock anyone out of SSH, which stays governed by the rules above.

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

APP_DIR="${APP_DIR:-/opt/tower-finder}"

if [ -d "$APP_DIR/.git" ]; then
    echo "  Repo exists, pulling latest..."
    cd "$APP_DIR"
    git pull
    git submodule update --init --recursive
elif [ -f "$APP_DIR/docker-compose.yml" ]; then
    # A tree with no .git but with the app in it: `just deploy-test` rsync'd it
    # here. That is the supported git-free workflow for retina-test, so use what
    # is already on disk rather than cloning over the top of it — which git
    # refuses to do into a non-empty directory anyway.
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

# The origin boundary. Applied here rather than beside the ufw rules because it
# needs the scripts this repo carries, which only reached disk a few lines above.
echo ""
echo "→ Applying the Cloudflare origin boundary (DOCKER-USER)..."
chmod +x "${APP_DIR}/deploy/docker-user-firewall.sh"
# Rendered, not copied: the unit's ExecStart has to carry the real deploy
# directory, and a literal path in the repo would silently keep pointing at the
# old location after the directory moves. The boundary failing is quiet, so it
# must not depend on someone remembering to edit the unit. Staged through a
# temporary file to keep the write atomic: if rendering fails, the existing unit
# stays intact rather than being left empty or partial.
sed "s|@APP_DIR@|${APP_DIR}|g" "${APP_DIR}/deploy/retina-firewall.service" \
    > /etc/systemd/system/retina-firewall.service.tmp
mv /etc/systemd/system/retina-firewall.service.tmp \
   /etc/systemd/system/retina-firewall.service
systemctl daemon-reload
systemctl enable retina-firewall.service
# restart, not start: the unit is Type=oneshot with RemainAfterExit=yes, so
# once it is active a `systemctl start` is a no-op that returns 0 without
# re-running ExecStart. daemon-reload above picks up a changed unit file but
# does not re-execute it either, so on a re-run of this script `start` would
# leave the newly rendered unit installed but never exercised, and the status
# line below would report whatever the previous activation left behind rather
# than the result of this run. `restart` always re-runs ExecStart.
#
# Fail-closed but not silently: under `set -euo pipefail` a bare `systemctl
# restart` that fails would abort the whole script right here, before .env is
# written and before `docker compose up`, leaving the operator with a raw
# systemd/iptables error and a half-configured box. Naming the failure and
# dumping the unit's status and recent logs first turns that into a diagnosis
# (the script still stops, an origin without this boundary must not proceed
# to serve traffic, it just says why before it does).
if ! systemctl restart retina-firewall.service; then
    echo "✗ retina-firewall.service failed to start — the Cloudflare origin" >&2
    echo "  boundary is not in place. Provisioning stops here rather than" >&2
    echo "  continue with an unprotected origin." >&2
    systemctl --no-pager status retina-firewall.service >&2 || true
    journalctl -u retina-firewall.service --no-pager -n 30 >&2 || true
    exit 1
fi
systemctl --no-pager status retina-firewall.service | head -5 || true

# Select the environment overlay for this host. Every `docker compose` command
# below — and every one a human types over SSH later — resolves through this, so
# the same commands work identically on staging and production. Without it a
# bare `docker compose up` starts the shared base alone, which start.sh refuses
# to boot because RETINA_ENV is unset.
cp deploy/env."${RETINA_TARGET_ENV}".example .env

# Create backend/.env with API keys. Hostnames, CORS and every other
# per-environment value now live in the compose overlay (docker-compose.prod.yml
# / docker-compose.staging.yml / docker-compose.test.yml), not here — keeping
# them out of a hand-edited per-host file is what stops the environments drifting
# apart.
#
# This writes only the minimum to boot the app + fleet. A live box carries more
# that this script does not know about: R2_* archive storage, TOWER_API_URL,
# COVERAGE_ENABLED. (RETINA_ENV and the auth flags beside it are not among them
# any more: they are set in the overlay, which overrides this file.)
# So refuse to clobber an existing file: on a fresh droplet there is nothing to
# lose, and on an existing one a blind overwrite would silently drop values that
# change how the backend gates auth. The operator should merge by hand, knowing
# what is already there.
#
# JWT_SECRET is generated here rather than left to the operator. Since production
# names itself `production`, core/users.py refuses to import without it, so a box
# provisioned without one crash-loops with no rollback (the deploy job's failure
# skips the jobs the rollback steps hang off). Generating beats prompting: the
# value is never needed by a human, and the alternative an operator reaches for
# is the placeholder in backend/.env.example, which is published in this repo.
if [ -f backend/.env ]; then
    echo "  backend/.env already exists — leaving it untouched."
    echo "  If this box needs the keys passed to this script, merge them yourself."
else
    {
        echo "MAPRAD_API_KEY=${MAPRAD_API_KEY}"
        echo "RADAR_API_KEY=${RADAR_API_KEY}"
        echo "JWT_SECRET=$(openssl rand -hex 32)"
    } > backend/.env
fi

# Outside the branch above, so it applies to a file this script did not write.
# The mode is not contents: correcting it does not undo the refusal to clobber,
# and every live box already has a backend/.env, so a chmod that only ran on
# files this script created would never reach any of them. That file holds the
# ingest key and the archive credentials, and is about to hold a Mender token
# that can deploy to the whole fleet, so it should not be world-readable.
chmod 600 backend/.env

# Fail before the build rather than crash-loop afterwards: every environment this
# script provisions serves TLS, so nginx cannot start without a certificate.
if [ ! -f /etc/ssl/cloudflare/cert.pem ] || [ ! -f /etc/ssl/cloudflare/key.pem ]; then
    echo "  ✗ No Cloudflare Origin cert at /etc/ssl/cloudflare/{cert,key}.pem." >&2
    echo "    Create one at Cloudflare → SSL/TLS → Origin Server and install it" >&2
    echo "    there, then re-run. Nothing else in this script needs redoing." >&2
    exit 1
fi

# Permissions, not just presence. nginx runs as appuser (uid 999) inside the
# image, NOT as root, so a cert installed root-owned 600 in a 700 directory is
# invisible to it and the container crash-loops on
#   [emerg] cannot load certificate ... Permission denied
# which reads like a missing file rather than a permissions problem.
#
# Check first, repair only if the check fails. A box whose cert already works is
# left exactly as it is: this script's default target is prod, so an
# unconditional chown/chmod would silently rewrite the ownership of production's
# live TLS private key on any re-run. Changing permissions on live key material
# should be a deliberate act, not a side effect of re-running provisioning.
CONTAINER_UID=999
_readable_as() {
    setpriv --reuid "$1" --regid "$1" --clear-groups \
        head -c 1 "$2" >/dev/null 2>&1
}
_cert_readable() {
    _readable_as "$1" /etc/ssl/cloudflare/cert.pem \
        && _readable_as "$1" /etc/ssl/cloudflare/key.pem
}

# Positive control before trusting any negative result. The probe answers "uid 999
# cannot read this" and "the probe did not run" with the same exit status, and the
# repair below acts on that answer — so a missing setpriv, or one blocked by a
# seccomp/AppArmor profile, would chmod and chown production's live TLS private
# key on a box where nothing was wrong. That is exactly what checking first is
# supposed to prevent, so a broken probe must fail loudly rather than "repair".
if ! command -v setpriv >/dev/null 2>&1; then
    echo "  ✗ setpriv not found (util-linux) — cannot verify the container user" >&2
    echo "    can read the certificate, and will not guess. Install util-linux." >&2
    exit 1
fi
if ! _readable_as "${CONTAINER_UID}" /etc/hostname; then
    echo "  ✗ Readability probe is broken: uid ${CONTAINER_UID} cannot read" >&2
    echo "    /etc/hostname, which is world-readable. setpriv is being blocked." >&2
    echo "    Refusing to touch the certificate on the strength of a probe that" >&2
    echo "    does not work — it would rewrite live key material for no reason." >&2
    exit 1
fi

if _cert_readable "${CONTAINER_UID}"; then
    echo "  ✓ cert already readable by the container user (uid ${CONTAINER_UID}); left untouched"
else
    echo "  → cert not readable by uid ${CONTAINER_UID}; repairing permissions"
    chmod 755 /etc/ssl/cloudflare
    chmod 644 /etc/ssl/cloudflare/cert.pem
    # The key gets the narrower grant: owned by the container uid, 640, so it is
    # not world-readable. Production predates this and has it 644; tightening
    # that is ClickUp 86cb1kru4 and is not done implicitly from here.
    chown "${CONTAINER_UID}:${CONTAINER_UID}" /etc/ssl/cloudflare/key.pem
    chmod 640 /etc/ssl/cloudflare/key.pem
    if ! _cert_readable "${CONTAINER_UID}"; then
        echo "  ✗ uid ${CONTAINER_UID} still cannot read /etc/ssl/cloudflare/{cert,key}.pem." >&2
        echo "    nginx runs as that uid in the container and will crash-loop." >&2
        exit 1
    fi
    echo "  ✓ cert readable by the container user (uid ${CONTAINER_UID})"
fi

# Build and start. The ./.env written above selects the overlay, so this bare
# form resolves to base + this environment's overlay.
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