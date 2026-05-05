#!/bin/sh
set -e

# Tunable via env vars (docker-compose or .env)
# NOTE: workers MUST stay at 1 — the app uses in-memory shared state and a TCP
# server bound to a single port.  Concurrency is handled by FRAME_WORKERS
# threads inside the single process.
FRAME_WORKERS="${FRAME_WORKERS:-6}"
FRAME_QUEUE_SIZE="${FRAME_QUEUE_SIZE:-10000}"

export FRAME_WORKERS
export FRAME_QUEUE_SIZE

# Ensure the image-fresh constants.py is always imported instead of the
# potentially-stale copy inside the /app/backend/config named volume.
# We store the pristine copy at /app/deploy/config-image/config/constants.py
# (outside the volume) and prepend its parent to PYTHONPATH so Python finds
# it first via namespace-package merging, regardless of whether the cp below
# succeeds.  This means the app always runs with the correct constants even
# on servers where the volume is still root-owned.
export PYTHONPATH="/app/deploy/config-image${PYTHONPATH:+:$PYTHONPATH}"

# Also attempt an in-place refresh of the volume copy so that the next
# restart (or any tool that reads the file directly) also sees the new
# version.  Non-fatal: the PYTHONPATH override above guarantees correctness
# even when this fails.
if [ -f /app/deploy/config-image/config/constants.py ]; then
    cp /app/deploy/config-image/config/constants.py /app/backend/config/constants.py 2>/dev/null \
        || echo "[start.sh] Info: could not refresh volume constants.py (volume may be root-owned); PYTHONPATH override is active"
fi

# Swap nginx config based on environment
if [ "${RETINA_ENV}" = "staging" ] && [ -f /app/deploy/nginx-staging.conf ]; then
    echo "[start.sh] Using staging nginx config for staging.retina.fm domains"
    cp /app/deploy/nginx-staging.conf /etc/nginx/sites-available/default
fi

# Start FastAPI backend with auto-restart supervision
cd /app/backend
(
  while true; do
    echo "[supervisor] starting uvicorn..."
    uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level warning
    EXIT_CODE=$?
    echo "[supervisor] uvicorn exited with code $EXIT_CODE, restarting in 2s..."
    sleep 2
  done
) &
UVICORN_PID=$!

# Clean up background supervisor on exit
cleanup() {
  kill "$UVICORN_PID" 2>/dev/null
  wait "$UVICORN_PID" 2>/dev/null
}
trap cleanup EXIT TERM INT

# Nginx foreground (pid already set in /etc/nginx/nginx.conf)
nginx -g "daemon off;"
