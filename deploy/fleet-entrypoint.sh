#!/bin/sh
set -e

cd /app/backend

# Generate fleet config from env
NODES="${FLEET_NODES:-200}"
REGIONS="${FLEET_REGIONS:-us}"
# Metro scoping: generate the whole fleet inside one metro area, and keep the
# simulated aircraft on that metro's regional waypoint net. Set FLEET_METRO=""
# to restore the continent-wide fleet that REGIONS alone produces.
METRO="${FLEET_METRO:-gvl}"
METRO_TRAFFIC_FRAC="${FLEET_METRO_TRAFFIC_FRAC:-0.85}"
SEED="${FLEET_SEED:-42}"
HOST="${FLEET_HOST:-localhost}"
PORT="${FLEET_PORT:-3012}"
MODE="${FLEET_MODE:-adsb}"
INTERVAL="${FLEET_INTERVAL:-0.5}"
TIME_SCALE="${FLEET_TIME_SCALE:-1.0}"
MIN_AIRCRAFT="${FLEET_MIN_AIRCRAFT:-0}"
MAX_AIRCRAFT="${FLEET_MAX_AIRCRAFT:-0}"
BEAM_WIDTH_DEG="${FLEET_BEAM_WIDTH_DEG:-0}"
MAX_RANGE_KM="${FLEET_MAX_RANGE_KM:-0}"
CONCURRENCY="${FLEET_CONCURRENCY:-50}"
CONNECT_RETRIES="${FLEET_CONNECT_RETRIES:-3}"
VALIDATE="${FLEET_VALIDATE:-false}"
N_CLUSTER="${FLEET_N_CLUSTER:-16}"
N_CLUSTERS="${FLEET_N_CLUSTERS:-1}"
VALIDATION_URL="${FLEET_VALIDATION_URL:-http://localhost:8000}"

echo "═══════════════════════════════════════════════════"
echo "  Retina Fleet Simulator"
echo "═══════════════════════════════════════════════════"
echo "  Nodes:      ${NODES}"
echo "  Regions:    ${REGIONS}"
echo "  Metro:      ${METRO:-nationwide}"
echo "  Mode:       ${MODE}"
echo "  Server:     ${HOST}:${PORT}"
echo "  Interval:   ${INTERVAL}s"
echo "  Time scale: ${TIME_SCALE}x"
echo "  Aircraft:   ${MIN_AIRCRAFT}-${MAX_AIRCRAFT}"
echo "  Validate:   ${VALIDATE}"
echo "═══════════════════════════════════════════════════"

# Wait for server to be ready
echo "Waiting for server at ${HOST}:${PORT}..."
for i in $(seq 1 30); do
    if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
try:
    s.connect(('${HOST}', ${PORT}))
    s.close()
    exit(0)
except:
    exit(1)
" 2>/dev/null; then
        echo "  Server is ready!"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  Server not ready after 30 attempts, starting anyway..."
    fi
    sleep 2
done

# Generate fleet config
echo "Generating fleet config (${NODES} nodes, regions=${REGIONS}, metro=${METRO:-nationwide})..."
if [ -n "${METRO}" ]; then
    GEN_METRO_ARGS="--metro ${METRO}"
else
    GEN_METRO_ARGS=""
fi
# shellcheck disable=SC2086  # GEN_METRO_ARGS is intentionally word-split
python3 -m retina_simulation.generator --nodes "${NODES}" --regions "${REGIONS}" ${GEN_METRO_ARGS} --seed "${SEED}" --n-cluster "${N_CLUSTER}" --n-clusters "${N_CLUSTERS}" --output /app/data/fleet_config.json

# Build orchestrator args
ARGS="--config /app/data/fleet_config.json"
# --metro is passed to the orchestrator too: with --config it no longer affects
# generation, but it still selects the regional waypoint net for aircraft spawn
# and the metro area used for real ADS-B injection.
if [ -n "${METRO}" ]; then
    ARGS="${ARGS} --metro ${METRO}"
fi
ARGS="${ARGS} --metro-traffic-frac ${METRO_TRAFFIC_FRAC}"
ARGS="${ARGS} --host ${HOST} --port ${PORT}"
ARGS="${ARGS} --mode ${MODE} --interval ${INTERVAL}"
ARGS="${ARGS} --time-scale ${TIME_SCALE}"
ARGS="${ARGS} --min-aircraft ${MIN_AIRCRAFT} --max-aircraft ${MAX_AIRCRAFT}"
ARGS="${ARGS} --beam-width-deg ${BEAM_WIDTH_DEG} --max-range-km ${MAX_RANGE_KM}"
ARGS="${ARGS} --concurrency ${CONCURRENCY} --connect-retries ${CONNECT_RETRIES}"
ARGS="${ARGS} --ground-truth-path /app/data/ground_truth.json"

# Always pass the server URL: the orchestrator uses it to POST live ADS-B and
# ground-truth positions (the moving "truth" tracks on the map), not just for
# --validate. Without it the orchestrator falls back to http://localhost:8000,
# which inside the fleet container is the fleet itself → all pushes refused.
ARGS="${ARGS} --validation-url ${VALIDATION_URL}"
if [ "${VALIDATE}" = "true" ]; then
    ARGS="${ARGS} --validate"
fi

# Launch fleet orchestrator
echo "Launching fleet orchestrator..."
exec python3 -m retina_simulation.orchestrator ${ARGS}
