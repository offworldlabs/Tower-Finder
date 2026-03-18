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
ARGS="${ARGS} --ground-truth-path /app/data/ground_truth.json"

if [ "${VALIDATE}" = "true" ]; then
    ARGS="${ARGS} --validate --validation-url ${VALIDATION_URL}"
fi

# Launch fleet orchestrator
echo "Launching fleet orchestrator..."
exec python3 fleet_orchestrator.py ${ARGS}
