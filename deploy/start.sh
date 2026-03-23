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

# Start FastAPI backend
cd /app/backend
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level warning &

# Start Nginx in foreground
nginx -g "daemon off;"
