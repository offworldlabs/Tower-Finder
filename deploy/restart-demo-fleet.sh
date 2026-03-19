#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/opt/tower-finder}"
HOST="${HOST:-localhost}"
PORT="${PORT:-3012}"
VALIDATION_URL="${VALIDATION_URL:-https://localhost}"

# Demo-oriented defaults: enough traffic to feel alive without turning the UI
# into an unreadable debug wall.
NODES="${NODES:-80}"
MODE="${MODE:-adsb}"
INTERVAL="${INTERVAL:-0.5}"
TIME_SCALE="${TIME_SCALE:-4.0}"
MIN_AIRCRAFT="${MIN_AIRCRAFT:-48}"
MAX_AIRCRAFT="${MAX_AIRCRAFT:-72}"
BEAM_WIDTH_DEG="${BEAM_WIDTH_DEG:-120}"
MAX_RANGE_KM="${MAX_RANGE_KM:-140}"
CONCURRENCY="${CONCURRENCY:-20}"
CONNECT_RETRIES="${CONNECT_RETRIES:-5}"

LOG_FILE="${LOG_FILE:-/tmp/fleet.log}"
PID_FILE="${PID_FILE:-/tmp/fleet.pid}"

cd "$APP_DIR"

pkill -f "backend/fleet_orchestrator.py" 2>/dev/null || true
sleep 2

nohup python3 backend/fleet_orchestrator.py \
  --nodes "$NODES" \
  --mode "$MODE" \
  --validate \
  --validation-url "$VALIDATION_URL" \
  --concurrency "$CONCURRENCY" \
  --connect-retries "$CONNECT_RETRIES" \
  --interval "$INTERVAL" \
  --time-scale "$TIME_SCALE" \
  --min-aircraft "$MIN_AIRCRAFT" \
  --max-aircraft "$MAX_AIRCRAFT" \
  --beam-width-deg "$BEAM_WIDTH_DEG" \
  --max-range-km "$MAX_RANGE_KM" \
  > "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "PID $(cat "$PID_FILE")"
echo "LOG $LOG_FILE"