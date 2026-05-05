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

# Refresh source-controlled config files from the image. The
# /app/backend/config directory is a Docker named volume so it can persist
# tower_config.json / nodes_config.json edits across container recreates,
# but that same persistence keeps constants.py stale after image upgrades.
# Copy the pristine constants.py from the image layer on every boot.
if [ -f /app/deploy/config-image/constants.py ]; then
    cp /app/deploy/config-image/constants.py /app/backend/config/constants.py
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
