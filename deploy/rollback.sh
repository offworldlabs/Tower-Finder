#!/bin/bash
# ── Production Rollback ──────────────────────────────────────────────────────
# Quick rollback to the previous Docker image or a specific git tag.
#
# Usage:
#   deploy/rollback.sh              # rollback to the previous image
#   deploy/rollback.sh <tag|commit> # rollback to a specific ref
#
# How it works:
#   Before each deploy, the CI pipeline (or manual deploy) should call
#   `deploy/pre-deploy.sh` which tags the current image as
#   `tower-finder:rollback` and creates a git tag `deploy-<timestamp>`.
#
#   This script restores service from that saved image or a given git ref.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tower-finder}"
IMAGE_NAME="tower-finder"
COMPOSE_FILE="${APP_DIR}/docker-compose.yml"

cd "$APP_DIR"

# ── Determine rollback target ────────────────────────────────────────────────
TARGET="${1:-}"

if [ -n "$TARGET" ]; then
    echo "Rolling back to git ref: ${TARGET}"
    git fetch --tags
    git checkout "$TARGET"
    git submodule update --init --recursive
    docker compose -f "$COMPOSE_FILE" up -d --build
else
    # Rollback using the saved Docker image
    if docker image inspect "${IMAGE_NAME}:rollback" >/dev/null 2>&1; then
        echo "Rolling back to saved image: ${IMAGE_NAME}:rollback"
        docker compose -f "$COMPOSE_FILE" down --timeout 30
        docker tag "${IMAGE_NAME}:rollback" "${IMAGE_NAME}:latest"
        docker compose -f "$COMPOSE_FILE" up -d
    else
        echo "No rollback image found. Falling back to previous git commit."
        # Note: this creates a detached HEAD. After recovery, re-attach with:
        #   git checkout main && git pull
        git checkout HEAD~1
        git submodule update --init --recursive
        docker compose -f "$COMPOSE_FILE" up -d --build
    fi
fi

# ── Wait for health ──────────────────────────────────────────────────────────
echo "Waiting for server to become healthy..."
for i in $(seq 1 12); do
    if docker compose -f "$COMPOSE_FILE" exec -T tower-finder \
        python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" 2>/dev/null; then
        echo "Rollback successful — healthy after ~$((i*5))s"
        echo "Current commit: $(git log --oneline -1)"
        exit 0
    fi
    echo "  Waiting... attempt $i/12"
    sleep 5
done

echo "WARNING: Health check failed after 60s. Check logs:"
echo "  docker compose logs --tail=50"
exit 1
