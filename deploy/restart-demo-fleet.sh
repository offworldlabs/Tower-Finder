#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/opt/tower-finder}"
HOST="${HOST:-localhost}"
PORT="${PORT:-3012}"
VALIDATION_URL="${VALIDATION_URL:-https://localhost}"

# ── Scaling profiles ───────────────────────────────────────────────────────────
# The Kalman tracker is pure-Python / GIL-bound and tops out at ~25 fps on a
# 2-core droplet.  The constraint is:  NODES / INTERVAL ≤ 25 fps.
#
# PROFILE  NODES  INTERVAL  effective fps  notes
# ───────  ─────  ────────  ─────────────  ──────────────────────────
# demo       12      0.5       24          original demo (2-core safe)
# 100       100      4.0       25          comfortable on 2-core
# 200       200      8.0       25          tight on 2-core
# 300       300     12.0       25          needs 4-core ideally
# 500       500     20.0       25          needs 4+ core
# 1000     1000     40.0       25          needs 4-core; overlap pre-filter required
#
PROFILE="${PROFILE:-demo}"
case "$PROFILE" in
  demo)
    NODES="${NODES:-12}"
    INTERVAL="${INTERVAL:-0.5}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-8}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-12}"
    CONCURRENCY="${CONCURRENCY:-6}"
    FRAME_WORKERS="${FRAME_WORKERS:-4}"
    ;;
  100)
    NODES="${NODES:-100}"
    INTERVAL="${INTERVAL:-4.0}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-20}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-35}"
    CONCURRENCY="${CONCURRENCY:-20}"
    FRAME_WORKERS="${FRAME_WORKERS:-6}"
    ;;
  200)
    NODES="${NODES:-200}"
    INTERVAL="${INTERVAL:-8.0}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-30}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-50}"
    CONCURRENCY="${CONCURRENCY:-30}"
    FRAME_WORKERS="${FRAME_WORKERS:-6}"
    ;;
  300)
    NODES="${NODES:-300}"
    INTERVAL="${INTERVAL:-12.0}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-40}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-60}"
    CONCURRENCY="${CONCURRENCY:-40}"
    FRAME_WORKERS="${FRAME_WORKERS:-8}"
    ;;
  500)
    NODES="${NODES:-500}"
    INTERVAL="${INTERVAL:-20.0}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-50}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-80}"
    CONCURRENCY="${CONCURRENCY:-50}"
    FRAME_WORKERS="${FRAME_WORKERS:-8}"
    ;;
  1000)
    NODES="${NODES:-1000}"
    INTERVAL="${INTERVAL:-40.0}"
    TIME_SCALE="${TIME_SCALE:-4.0}"
    MIN_AIRCRAFT="${MIN_AIRCRAFT:-60}"
    MAX_AIRCRAFT="${MAX_AIRCRAFT:-100}"
    CONCURRENCY="${CONCURRENCY:-80}"
    FRAME_WORKERS="${FRAME_WORKERS:-8}"
    ;;
  *)
    echo "Unknown PROFILE=$PROFILE (use: demo, 100, 200, 300, 500, 1000)" >&2
    exit 1
    ;;
esac

MODE="${MODE:-adsb}"
BEAM_WIDTH_DEG="${BEAM_WIDTH_DEG:-120}"
MAX_RANGE_KM="${MAX_RANGE_KM:-140}"
CONNECT_RETRIES="${CONNECT_RETRIES:-5}"
FRAME_QUEUE_SIZE="${FRAME_QUEUE_SIZE:-10000}"

LOG_FILE="${LOG_FILE:-/tmp/fleet.log}"
PID_FILE="${PID_FILE:-/tmp/fleet.pid}"

cd "$APP_DIR"

pkill -f "simulation/orchestrator.py" 2>/dev/null || true
sleep 2

echo "Starting fleet: PROFILE=$PROFILE  NODES=$NODES  INTERVAL=$INTERVAL  fps=$(echo "$NODES / $INTERVAL" | bc -l | head -c5)"

nohup python3 backend/simulation/orchestrator.py \
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