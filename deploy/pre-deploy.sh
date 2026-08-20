#!/bin/bash
# ── Pre-Deploy Snapshot ──────────────────────────────────────────────────────
# Run BEFORE each deploy to save rollback points.
#
# 1. Tags the current running Docker image as `retina-server:rollback`
# 2. Creates a git tag `deploy-<YYYYMMDD-HHMMSS>` on the current commit
#
# Usage: deploy/pre-deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/retina-server}"
IMAGE_NAME="retina-server"
COMPOSE_SERVICE="server"
FLEET_IMAGE_NAME="retina-server-fleet"
FLEET_SERVICE="fleet"

cd "$APP_DIR"

# ── Save current Docker image ────────────────────────────────────────────────
# No `| head -1` here: piping into head can close the pipe early and, under
# `set -o pipefail`, surface a spurious SIGPIPE (exit 141) that would now abort
# the whole deploy (the CI step no longer swallows pre-deploy failures). Capture
# the full output and take the first line in-shell instead.
CURRENT_IMAGE=$(docker compose images -q "$COMPOSE_SERVICE" 2>/dev/null)
CURRENT_IMAGE=${CURRENT_IMAGE%%$'\n'*}
if [ -n "$CURRENT_IMAGE" ]; then
    docker tag "$CURRENT_IMAGE" "${IMAGE_NAME}:rollback"
    echo "Saved current image as ${IMAGE_NAME}:rollback (${CURRENT_IMAGE:0:12})"
else
    echo "Warning: no running image found for ${COMPOSE_SERVICE}"
fi

# ── Save current fleet image ─────────────────────────────────────────────────
# Mirror the app snapshot above so rollback is symmetric: without a saved fleet
# image, a rollback reverts the app but leaves the fleet on the bad deploy's
# build. Same empty-output handling (fresh box, fleet never built): warn, don't
# fail — a missing fleet image must not abort under `set -o pipefail`.
CURRENT_FLEET_IMAGE=$(docker compose images -q "$FLEET_SERVICE" 2>/dev/null)
CURRENT_FLEET_IMAGE=${CURRENT_FLEET_IMAGE%%$'\n'*}
if [ -n "$CURRENT_FLEET_IMAGE" ]; then
    docker tag "$CURRENT_FLEET_IMAGE" "${FLEET_IMAGE_NAME}:rollback"
    echo "Saved current fleet image as ${FLEET_IMAGE_NAME}:rollback (${CURRENT_FLEET_IMAGE:0:12})"
else
    echo "Warning: no running image found for ${FLEET_SERVICE}"
fi

# ── Tag current git commit ───────────────────────────────────────────────────
TAG="deploy-$(date -u +%Y%m%d-%H%M%S)"
git tag "$TAG"
echo "Git tag created: $TAG"

echo "Pre-deploy snapshot complete. To rollback: deploy/rollback.sh"
