#!/bin/sh
set -e

NGINX_CONF="${NGINX_CONF:-/etc/nginx/sites-available/default}"
WORKERS="${UVICORN_WORKERS:-1}"

echo "Starting Retina server (workers=${WORKERS}, nginx=${NGINX_CONF})"

# Pick the nginx config for the selected profile when one exists. The profile
# defaults to RETINA_ENV (test → nginx-test.conf), but the local stack sets
# NGINX_PROFILE=local to select the cert-free nginx-local.conf without having to
# pretend the backend is running in a different environment (RETINA_ENV drives
# the app's own env gating, so overloading it for nginx selection broke the
# dev/test allowlists).
NGINX_PROFILE="${NGINX_PROFILE:-${RETINA_ENV:-}}"
if [ -n "${NGINX_PROFILE}" ] && [ -f "/app/deploy/nginx-${NGINX_PROFILE}.conf" ]; then
    echo "Using nginx-${NGINX_PROFILE}.conf (profile=${NGINX_PROFILE})"
    cp "/app/deploy/nginx-${NGINX_PROFILE}.conf" /etc/nginx/sites-available/default
fi

# Start FastAPI backend
cd /app/backend
uvicorn main:app --host 127.0.0.1 --port 8000 --workers "${WORKERS}" --log-level warning &

# Start Nginx in foreground (pid already set in /etc/nginx/nginx.conf)
nginx -g "daemon off;"
