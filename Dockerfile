# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ── Stage 1b: Build dashboard ───────────────────────────────────────────────
FROM node:20-alpine AS dashboard-build
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci --ignore-scripts
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

# ── Non-root user ────────────────────────────────────────────────────────────
RUN useradd -r -s /usr/sbin/nologin appuser && \
    # Allow nginx to bind to privileged ports as non-root
    setcap cap_net_bind_service=+ep /usr/sbin/nginx && \
    # nginx runtime dirs
    chown -R appuser:appuser /var/log/nginx /var/lib/nginx /run && \
    # app dirs that need write access
    chown -R appuser:appuser /app/backend/coverage_data /app/backend/tar1090_data && \
    mkdir -p /app/backend/data && chown -R appuser:appuser /app/backend/data

USER appuser

EXPOSE 80 443

ENTRYPOINT ["tini", "--"]
CMD ["/app/deploy/start.sh"]
