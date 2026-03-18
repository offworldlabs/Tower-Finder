#!/bin/sh
set -e

NGINX_CONF="${NGINX_CONF:-/etc/nginx/sites-available/default}"
WORKERS="${UVICORN_WORKERS:-1}"

echo "Starting Retina server (workers=${WORKERS}, nginx=${NGINX_CONF})"

# Use test nginx config if RETINA_ENV=test
if [ "${RETINA_ENV}" = "test" ] && [ -f /app/deploy/nginx-test.conf ]; then
    echo "Using test nginx config for testmap/testapi domains"
    cp /app/deploy/nginx-test.conf /etc/nginx/sites-available/default
fi

# Start FastAPI backend
cd /app/backend
uvicorn main:app --host 127.0.0.1 --port 8000 --workers "${WORKERS}" --log-level warning &

# Start Nginx in foreground
nginx -g "daemon off;"
