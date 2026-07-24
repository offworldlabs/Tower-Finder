#!/bin/sh
set -e

NGINX_CONF="${NGINX_CONF:-/etc/nginx/sites-available/default}"
WORKERS="${UVICORN_WORKERS:-1}"

echo "Starting Retina server (workers=${WORKERS}, nginx=${NGINX_CONF})"

# Use the environment-specific nginx config when one exists
# (test → nginx-test.conf, local → nginx-local.conf)
if [ -n "${RETINA_ENV:-}" ] && [ -f "/app/deploy/nginx-${RETINA_ENV}.conf" ]; then
    echo "Using nginx-${RETINA_ENV}.conf for RETINA_ENV=${RETINA_ENV}"
    cp "/app/deploy/nginx-${RETINA_ENV}.conf" /etc/nginx/sites-available/default
fi

# Start FastAPI backend
cd /app/backend
uvicorn main:app --host 127.0.0.1 --port 8000 --workers "${WORKERS}" --log-level warning &

# Start Nginx in foreground (pid already set in /etc/nginx/nginx.conf)
nginx -g "daemon off;"
