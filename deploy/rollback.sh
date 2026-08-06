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
FLEET_IMAGE_NAME="tower-finder-fleet"
# No `-f` flags below: the host's ./.env sets COMPOSE_FILE to the shared base
# plus that host's overlay (deploy/env.*.example), so `docker compose` here
# resolves exactly what the deploy resolved. Passing docker-compose.yml alone
# would roll back to the base with no environment identity, which start.sh
# refuses to boot.

cd "$APP_DIR"

# ── Keep ./.env consistent with the tree we roll back to ─────────────────────
# ./.env is gitignored, so moving the source tree does NOT move it. Every compose
# command in this script resolves through the COMPOSE_FILE it holds, so rolling
# back to a commit whose overlay this .env names but which does not exist there
# leaves every one of them failing on a missing file — the recovery path breaking
# at exactly the moment it is needed, with the app already stopped.
#
# That is reachable today, not hypothetically: production is checked out at a
# commit predating docker-compose.prod.yml, so the first deploy's rollback tag
# points at a tree that has no overlay at all while the deploy has just written
# an .env naming one.
#
# So re-derive it after each tree move. Prefer the rolled-back tree's own example
# file; if that tree predates the overlay mechanism entirely, drop .env so compose
# resolves docker-compose.yml alone — which is precisely what those commits' base
# file was, a complete standalone production config.
resync_env_to_tree() {
    [ -f .env ] || return 0

    local spec overlay env_name missing=0 f
    spec=$(sed -n 's/^COMPOSE_FILE=//p' .env | tail -1)
    [ -n "$spec" ] || return 0

    local IFS=':'
    for f in $spec; do
        [ -f "$f" ] || missing=1
    done
    unset IFS
    [ "$missing" = 1 ] || return 0

    # `|| true` is load-bearing under the `set -euo pipefail` at the top: a
    # non-matching grep exits 1, and a bare `var=$(... | grep ...)` takes the
    # pipeline's status as the assignment's, so the script would abort here —
    # silently, mid-rollback, with the tree already moved and the app not yet
    # back up. It also keeps the empty-`overlay` case reachable: the else branch
    # below is still entered when the example file is missing, but without this
    # it could never be entered with no name in hand, so the `${overlay:-overlay}`
    # default in its message was dead.
    overlay=$(printf '%s' "$spec" | grep -oE 'docker-compose\.[a-z]+\.yml' | tail -1 || true)
    env_name=${overlay#docker-compose.}
    env_name=${env_name%.yml}

    if [ -n "$env_name" ] && [ -f "deploy/env.${env_name}.example" ]; then
        cp "deploy/env.${env_name}.example" .env
        echo "  ./.env re-derived from deploy/env.${env_name}.example for the rolled-back tree"
    else
        rm -f .env
        echo "  Rolled-back tree has no ${overlay:-overlay}; removed ./.env so compose resolves the base alone"
    fi
}

# ── Determine rollback target ────────────────────────────────────────────────
TARGET="${1:-}"

if [ -n "$TARGET" ]; then
    echo "Rolling back to git ref: ${TARGET}"
    git fetch --tags
    git checkout "$TARGET"
    git submodule update --init --recursive
    resync_env_to_tree
    docker compose up -d --build
else
    # Rollback using the saved Docker image
    if docker image inspect "${IMAGE_NAME}:rollback" >/dev/null 2>&1; then
        echo "Rolling back to saved image: ${IMAGE_NAME}:rollback"
        # Revert the source tree to the commit the saved image was built from,
        # BEFORE stopping the app. pre-deploy.sh tags each pre-deploy commit
        # `deploy-<timestamp>`, so the newest is the last-known-good. Without this
        # the box runs the good image over the bad commit, and the next
        # `docker compose up --build` (a manual redeploy, or a fleet bounce)
        # silently rebuilds the bad code, undoing the rollback.
        # Ordering matters: doing this FIRST (before `down`) means a git failure —
        # e.g. a submodule fetch during a GitHub outage, which is exactly when
        # rollbacks run — aborts with the app STILL UP, rather than stranding it
        # between `down` and `up`. `reset --hard` (not `checkout`) matches the
        # deploy path, tolerates untracked/conflicting files, and keeps HEAD on
        # its branch instead of leaving a detached HEAD.
        LAST_GOOD=$(git tag --list 'deploy-*' --sort=-creatordate | head -1)
        if [ -n "$LAST_GOOD" ]; then
            echo "Reverting source tree to last-good tag: ${LAST_GOOD}"
            git reset --hard "$LAST_GOOD"
            git submodule update --init --recursive
            resync_env_to_tree
        else
            echo "WARNING: no deploy-* tag found; restoring image only, source tree left as-is."
        fi
        docker compose down --timeout 30
        docker tag "${IMAGE_NAME}:rollback" "${IMAGE_NAME}:latest"
        # Symmetric fleet rollback: retag the saved fleet image too, so the single
        # `up -d` below brings the fleet back on its good build alongside the app.
        # Guarded because a box predating this change (or one where pre-deploy.sh
        # found no running fleet) never captured tower-finder-fleet:rollback — in
        # that case roll the app back cleanly and leave the fleet image as-is.
        if docker image inspect "${FLEET_IMAGE_NAME}:rollback" >/dev/null 2>&1; then
            docker tag "${FLEET_IMAGE_NAME}:rollback" "${FLEET_IMAGE_NAME}:latest"
            echo "Restored fleet image ${FLEET_IMAGE_NAME}:rollback -> ${FLEET_IMAGE_NAME}:latest"
        else
            echo "No ${FLEET_IMAGE_NAME}:rollback image found; rolling back app only, fleet image left as-is."
        fi
        docker compose up -d
    else
        echo "No rollback image found. Falling back to previous git commit."
        # Note: this creates a detached HEAD. After recovery, re-attach with:
        #   git checkout main && git pull
        git checkout HEAD~1
        resync_env_to_tree
        git submodule update --init --recursive
        docker compose up -d --build
    fi
fi

# ── Wait for health ──────────────────────────────────────────────────────────
echo "Waiting for server to become healthy..."
for i in $(seq 1 12); do
    if docker compose exec -T tower-finder \
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
