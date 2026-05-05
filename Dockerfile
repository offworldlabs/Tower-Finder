# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── Stage 1b: Build dashboard ───────────────────────────────────────────────
FROM node:20-alpine AS dashboard-build
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ── Stage 2: Production image ───────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# Install system deps (nginx + tini for PID 1 + libcap2-bin for setcap)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx tini libcap2-bin && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Submodule packages (retina_geolocator + retina_tracker)
COPY libs/retina-geolocator/ ./libs/retina-geolocator/
COPY libs/retina-tracker/ ./libs/retina-tracker/
COPY libs/retina-custody/ ./libs/retina-custody/
COPY libs/retina-simulation/ ./libs/retina-simulation/
COPY libs/retina-analytics/ ./libs/retina-analytics/
RUN pip install --no-cache-dir ./libs/retina-geolocator ./libs/retina-tracker ./libs/retina-custody ./libs/retina-simulation ./libs/retina-analytics

# Backend code
COPY backend/ ./backend/

# Built frontend
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Built dashboard
COPY --from=dashboard-build /app/dashboard/dist /app/dashboard/dist

# tar1090 static files
COPY tar1090/html /app/tar1090/html

# Nginx config (default: production domains)
COPY deploy/nginx.conf /etc/nginx/sites-available/default
COPY deploy/nginx-security.conf /etc/nginx/conf.d/security.conf

# Deploy scripts + test nginx config (used when RETINA_ENV=test)
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh /app/deploy/start-test.sh

# Save a pristine copy of source-controlled config files outside the
# /app/backend/config volume so they always reflect the current image.
# tower_config.json / nodes_config.json are runtime-editable and stay in
# the volume; constants.py is source code and must follow the image.
#
# Layout: /app/deploy/config-image/config/constants.py (no __init__.py so
# Python treats 'config' as a namespace package and merges all 'config/'
# dirs on sys.path).  start.sh prepends /app/deploy/config-image to
# PYTHONPATH so this copy takes priority over the potentially-stale volume
# copy at /app/backend/config/constants.py — even when the volume is
# root-owned and the cp refresh fails.
RUN mkdir -p /app/deploy/config-image/config && \
    cp /app/backend/config/constants.py /app/deploy/config-image/config/constants.py

# ── Non-root user ────────────────────────────────────────────────────────────
RUN useradd -r -s /usr/sbin/nologin appuser && \
    # Allow nginx to bind to privileged ports as non-root
    setcap cap_net_bind_service=+ep /usr/sbin/nginx && \
    # nginx runtime dirs
    chown -R appuser:appuser /var/log/nginx /var/lib/nginx /run && \
    # allow start.sh to swap nginx config at runtime (for staging/test envs)
    chown appuser:appuser /etc/nginx/sites-available /etc/nginx/sites-available/default && \
    # app dirs that need write access
    mkdir -p /app/backend/coverage_data /app/backend/tar1090_data /app/backend/data && \
    chown -R appuser:appuser /app/backend/coverage_data /app/backend/tar1090_data /app/backend/data && \
    # /app/backend/config is mounted as a named volume; set appuser ownership
    # on the image layer so that freshly-created volumes inherit the right owner.
    chown appuser:appuser /app/backend/config

USER appuser

EXPOSE 80 443

ENTRYPOINT ["tini", "--"]
CMD ["/app/deploy/start.sh"]
