#!/bin/bash
# ── Pre-Deploy Snapshot ──────────────────────────────────────────────────────
# Run BEFORE each deploy to save rollback points.
#
# 1. Tags the current running Docker image as `tower-finder:rollback`
# 2. Creates a git tag `deploy-<YYYYMMDD-HHMMSS>` on the current commit
#
# Usage: deploy/pre-deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tower-finder}"
IMAGE_NAME="tower-finder"
COMPOSE_SERVICE="tower-finder"

cd "$APP_DIR"

# ── Save current Docker image ────────────────────────────────────────────────
CURRENT_IMAGE=$(docker compose images -q "$COMPOSE_SERVICE" 2>/dev/null | head -1)
if [ -n "$CURRENT_IMAGE" ]; then
    docker tag "$CURRENT_IMAGE" "${IMAGE_NAME}:rollback"
    echo "Saved current image as ${IMAGE_NAME}:rollback (${CURRENT_IMAGE:0:12})"
else
    echo "Warning: no running image found for ${COMPOSE_SERVICE}"
fi

# ── Tag current git commit ───────────────────────────────────────────────────
TAG="deploy-$(date -u +%Y%m%d-%H%M%S)"
git tag "$TAG" 2>/dev/null && echo "Git tag created: $TAG" || echo "Warning: failed to create git tag"

echo "Pre-deploy snapshot complete. To rollback: deploy/rollback.sh"
