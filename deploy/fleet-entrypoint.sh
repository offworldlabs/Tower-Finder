#!/bin/sh
set -e

cd /app/backend

# Generate fleet config from env
NODES="${FLEET_NODES:-200}"
REGIONS="${FLEET_REGIONS:-us}"
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
VALIDATION_URL="${FLEET_VALIDATION_URL:-http://localhost:8000}"

echo "═══════════════════════════════════════════════════"
echo "  Retina Fleet Simulator"
echo "═══════════════════════════════════════════════════"
echo "  Nodes:      ${NODES}"
echo "  Regions:    ${REGIONS}"
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
echo "Generating fleet config (${NODES} nodes, regions=${REGIONS})..."
python3 fleet_generator.py --nodes "${NODES}" --regions "${REGIONS}" --seed "${SEED}" --output /app/data/fleet_config.json

# Build orchestrator args
ARGS="--config /app/data/fleet_config.json"
ARGS="${ARGS} --host ${HOST} --port ${PORT}"
ARGS="${ARGS} --mode ${MODE} --interval ${INTERVAL}"
ARGS="${ARGS} --time-scale ${TIME_SCALE}"
ARGS="${ARGS} --min-aircraft ${MIN_AIRCRAFT} --max-aircraft ${MAX_AIRCRAFT}"
ARGS="${ARGS} --beam-width-deg ${BEAM_WIDTH_DEG} --max-range-km ${MAX_RANGE_KM}"
ARGS="${ARGS} --concurrency ${CONCURRENCY} --connect-retries ${CONNECT_RETRIES}"
ARGS="${ARGS} --ground-truth-path /app/data/ground_truth.json"

if [ "${VALIDATE}" = "true" ]; then
    ARGS="${ARGS} --validate --validation-url ${VALIDATION_URL}"
fi

# Launch fleet orchestrator
echo "Launching fleet orchestrator..."
exec python3 fleet_orchestrator.py ${ARGS}
