#!/bin/bash
# restart-fleet-prod.sh — Stop all running fleet systemd units, start a fresh one.
#
# Usage (on server):
#   RADAR_API_KEY=<key> bash /opt/tower-finder/deploy/restart-fleet-prod.sh
#
# Or with custom settings:
#   NODES=200 INTERVAL=40.0 RADAR_API_KEY=<key> bash .../restart-fleet-prod.sh
#
# The script:
#   1. Stops every fleet*.service that is currently active or failed
#   2. Starts a new unit with a unique name (fleetN where N = seconds since epoch)
#   3. Verifies the new unit is running before exiting
#
set -euo pipefail

# ── Config (all overridable via env) ──────────────────────────────────────────
NODES="${NODES:-200}"
INTERVAL="${INTERVAL:-40.0}"
TIME_SCALE="${TIME_SCALE:-1.0}"
MIN_AIRCRAFT="${MIN_AIRCRAFT:-60}"
MAX_AIRCRAFT="${MAX_AIRCRAFT:-100}"
CONCURRENCY="${CONCURRENCY:-20}"
VALIDATION_URL="${VALIDATION_URL:-https://localhost}"
CONNECT_RETRIES="${CONNECT_RETRIES:-999}"
METROS="${METROS:-atl,gvl,clt}"
APP_DIR="${APP_DIR:-/opt/tower-finder}"

# RADAR_API_KEY MUST be set — refuse to start without it so ground-truth
# push never silently 401s again.
if [ -z "${RADAR_API_KEY:-}" ]; then
    echo "ERROR: RADAR_API_KEY is not set. Export it before running this script." >&2
    echo "  export RADAR_API_KEY=<key>" >&2
    exit 1
fi

# ── Stop all existing fleet units ─────────────────────────────────────────────
echo "==> Stopping existing fleet units..."
ACTIVE_UNITS=$(systemctl list-units 'fleet*.service' --no-pager --no-legend \
    | awk '{print $1}' | tr '\n' ' ')

if [ -n "$ACTIVE_UNITS" ]; then
    # shellcheck disable=SC2086
    systemctl stop $ACTIVE_UNITS 2>/dev/null || true
    echo "    Stopped: $ACTIVE_UNITS"
else
    echo "    No active fleet units found."
fi

# Small pause so TCP connections close cleanly before new fleet connects
sleep 3

# ── Start new fleet unit ──────────────────────────────────────────────────────
UNIT_NAME="fleet$(date +%s | tail -c 5)"  # e.g. fleet48422
echo "==> Starting $UNIT_NAME (nodes=$NODES, interval=${INTERVAL}s, time_scale=${TIME_SCALE}x, metros=$METROS)..."

systemd-run \
    --unit="$UNIT_NAME" \
    --working-directory="$APP_DIR" \
    -E RADAR_API_KEY="$RADAR_API_KEY" \
    python3 -m retina_simulation.orchestrator \
        --nodes "$NODES" \
        --mode adsb \
        --validation-url "$VALIDATION_URL" \
        --concurrency "$CONCURRENCY" \
        --connect-retries "$CONNECT_RETRIES" \
        --interval "$INTERVAL" \
        --time-scale "$TIME_SCALE" \
        --min-aircraft "$MIN_AIRCRAFT" \
        --max-aircraft "$MAX_AIRCRAFT" \
        --metros "$METROS"

# ── Verify it's running ───────────────────────────────────────────────────────
sleep 2
if systemctl is-active --quiet "${UNIT_NAME}.service"; then
    echo "==> $UNIT_NAME is running."
    echo "    Logs:   journalctl -u $UNIT_NAME -f"
    echo "    Status: systemctl status $UNIT_NAME.service --no-pager"
else
    echo "ERROR: $UNIT_NAME failed to start." >&2
    journalctl -u "$UNIT_NAME" --no-pager -n 20 >&2
    exit 1
fi
