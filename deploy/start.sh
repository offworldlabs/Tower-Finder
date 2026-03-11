#!/bin/sh
set -e

# Start FastAPI backend
cd /app/backend
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2 --log-level warning &

# Start Nginx in foreground
nginx -g "daemon off;"
