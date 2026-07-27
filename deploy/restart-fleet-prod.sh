#!/bin/bash
# restart-fleet-prod.sh — manually bounce the production fleet simulator.
#
# HISTORY: this script used to start the fleet as a transient `systemd-run` unit
# (fleet$(date +%s)). Transient units live only in runtime memory and do NOT
# survive a reboot — which is exactly why the synthetic fleet (and its detector
# nodes) silently vanished from testmap.retina.fm after the droplet was rebooted
# to expand RAM. The fleet is now the `fleet` Compose service in
# docker-compose.yml (restart: unless-stopped), so it:
#   • starts automatically on deploy   (CI runs `docker compose up -d --build`)
#   • comes back on every reboot        (docker.service is enabled)
#
# This script is kept only as a convenience for a manual bounce. It no longer
# starts anything out-of-band, so it can never create a second, competing fleet.
#
# Usage (on server):
#   bash /opt/tower-finder/deploy/restart-fleet-prod.sh
#
# RADAR_API_KEY is NOT needed here anymore — the fleet service reads it from
# backend/.env via Compose, the same file the app loads.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tower-finder}"
cd "$APP_DIR"

echo "==> Recreating the fleet Compose service (docker-compose.yml)..."
# --no-deps is critical: `fleet` depends_on `tower-finder`, so without it
# `up --build --force-recreate` would ALSO rebuild and recreate the running
# production app (nginx/API/:3012) — a full redeploy + outage when all we want
# is to bounce the fleet. --no-deps scopes the action to `fleet` only.
docker compose up -d --build --force-recreate --no-deps fleet

echo "==> Done."
echo "    Status: docker compose ps fleet"
echo "    Logs:   docker compose logs -f fleet"
docker compose ps fleet
