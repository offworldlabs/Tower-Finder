# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Production image ───────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend code
COPY backend/ ./backend/

# Built frontend
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# tar1090 static files
COPY tar1090/html /app/tar1090/html

# Nginx config (default: production domains)
COPY deploy/nginx.conf /etc/nginx/sites-available/default

# Deploy scripts + test nginx config (used when RETINA_ENV=test)
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh /app/deploy/start-test.sh

EXPOSE 80

CMD ["/app/deploy/start.sh"]
