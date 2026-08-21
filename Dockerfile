# syntax=docker/dockerfile:1
# The pragma pins the Dockerfile frontend rather than leaving it to whatever the
# building daemon happens to bundle. The uv install below is a BuildKit feature,
# and the two build paths that matter, CI and `docker compose up -d --build` on
# the droplets, are both BuildKit-backed but not both the same version. Docker
# Hub is already a build-time dependency through the base images, so this adds
# no new one.

# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` with the same flags CI's frontend-build job uses, so the bundle that
# ships is built from the tree CI tested. `npm install` would be free to
# re-resolve and rewrite the lockfile.
RUN npm ci --legacy-peer-deps --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── Stage 1b: Build dashboard ───────────────────────────────────────────────
FROM node:20-alpine AS dashboard-build
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --legacy-peer-deps --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ── uv, for the Python installs in the production stage ─────────────────────
# A stage of its own so the version is written once. It is only ever a mount
# source, so nothing from it reaches the shipped image.
ARG UV_VERSION=0.12.5
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ── Stage 2: Production image ───────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# Install system deps (nginx + tini for PID 1 + libcap2-bin for setcap)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx tini libcap2-bin && \
    rm -rf /var/lib/apt/lists/*

# Python deps, installed with the same uv command CI uses. The version behind it
# is not the same: CI takes whatever astral-sh/setup-uv gives it, this pins. That
# is tolerable because every package in requirements.txt is pinned with ==, so
# the resolver has nothing to decide.
#
# uv is bind-mounted for the duration of the RUN rather than copied in, so its
# 54 MiB never lands in a layer of the shipped image, which has no use for uv at
# runtime.
#
# --no-cache is pip's --no-cache-dir. --compile-bytecode keeps pip's default of
# shipping .pyc alongside the sources: site-packages is root-owned and the app
# runs as appuser, so whatever is left uncompiled here can never be written at
# runtime and is recompiled on every boot.
COPY backend/requirements.txt ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --compile-bytecode -r requirements.txt

# Submodule packages (retina_geolocator + retina_tracker)
COPY libs/retina-geolocator/ ./libs/retina-geolocator/
COPY libs/retina-tracker/ ./libs/retina-tracker/
COPY libs/retina-custody/ ./libs/retina-custody/
COPY libs/retina-simulation/ ./libs/retina-simulation/
COPY libs/retina-analytics/ ./libs/retina-analytics/
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --compile-bytecode ./libs/retina-geolocator ./libs/retina-tracker ./libs/retina-custody ./libs/retina-simulation ./libs/retina-analytics

# Backend code
COPY backend/ ./backend/

# Built frontend
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Built dashboard
COPY --from=dashboard-build /app/dashboard/dist /app/dashboard/dist

# Rate-limit zones — http{} context, identical in every environment.
COPY deploy/nginx-security.conf /etc/nginx/conf.d/security.conf

# The vhosts themselves are NOT baked in: at boot start.sh runs
# deploy/render-nginx-config.py over deploy/nginx/nginx.conf.template, so one
# template serves staging and production and they cannot drift apart. A
# placeholder is installed here only so the file exists to be chowned below.
RUN echo "# replaced at boot by deploy/start.sh" > /etc/nginx/sites-available/default

# Deploy scripts + nginx template/snippets
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh

# Save a pristine copy of source-controlled config files outside the
# /app/backend/config volume so they always reflect the current image.
# tower_config.json / nodes_config.json are runtime-editable and stay in
# the volume; constants.py is source code and must follow the image.
#
# blah2_nodes.json is runtime-editable too, but it also has to be *seedable*:
# on an existing deployment the volume masks backend/config, so a copy shipped
# only there would be invisible and the bridge would poll nothing. The pristine
# copy is what runtime_config.default_source_path() seeds the overlay from.
#
# Layout: /app/deploy/config-image/config/constants.py (no __init__.py so
# Python treats 'config' as a namespace package and merges all 'config/'
# dirs on sys.path).  start.sh prepends /app/deploy/config-image to
# PYTHONPATH so this copy takes priority over the potentially-stale volume
# copy at /app/backend/config/constants.py — even when the volume is
# root-owned and the cp refresh fails.
RUN mkdir -p /app/deploy/config-image/config && \
    cp /app/backend/config/constants.py /app/deploy/config-image/config/constants.py && \
    cp /app/backend/config/blah2_nodes.json /app/deploy/config-image/config/blah2_nodes.json

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
