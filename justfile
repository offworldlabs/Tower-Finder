set shell := ["bash", "-cu"]

# Local dev runner for the testmap live map: backend + synthetic fleet + frontend.
# `just setup` once, then `just up` / `just down`. Runtime logs go in .testmap-run/.
# up/down/status are PORT-based (backend :8000+:3012, frontend :5173) so stale state
# can't strand orphaned processes or silently fail on a port clash.

root := justfile_directory()
be   := root / "backend"
fe   := root / "frontend"
venv := be / ".venv"
py   := venv / "bin/python"
run  := root / ".testmap-run"

# List targets
default:
    @just --list

# One-time setup: submodules, backend venv + editable libs (uv), .env, frontend deps
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "→ submodules"
    git -C "{{root}}" submodule update --init --recursive
    echo "→ backend venv + deps (uv)"
    cd "{{be}}"
    uv venv .venv   # interpreter pinned by backend/.python-version (3.12, matches Dockerfile)
    uv pip install --python "{{py}}" -r requirements.txt -r requirements-dev.txt
    # The fleet (retina-simulation) depends on the other four libs — install all five
    # editable together or imports fail. (README only needs two for tower search.)
    uv pip install --python "{{py}}" \
        -e ../libs/retina-geolocator -e ../libs/retina-tracker \
        -e ../libs/retina-custody -e ../libs/retina-analytics \
        -e ../libs/retina-simulation
    [ -f .env ] || cp .env.example .env   # Maprad key not needed for the testmap
    echo "→ frontend deps"
    cd "{{fe}}" && npm install
    echo "✓ setup complete — now: just up"

# This is the INNER LOOP, not a deploy rehearsal: uvicorn --reload + Vite HMR, which
# the Docker stack structurally cannot offer (its frontend bundle and nginx config are
# baked at build time). It deliberately does NOT mirror the deploy: no nginx, no
# start.sh, and the libs come from the editable ../libs/* checkouts rather than the
# pinned submodules in the image. For fidelity to the live deploy, use the Docker
# stack instead (see CLAUDE.md); for fast iteration, use this.
#
# The fleet runs a dense dev-only stream (~1 ellipse/s) with no deployed equivalent.
#
# Backend + synthetic fleet + frontend (background). Open http://testmap.localhost:5173/
up:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -x "{{py}}" ] || { echo "no backend venv — run: just setup"; exit 1; }
    [ -d "{{fe}}/node_modules" ] || { echo "no frontend deps — run: just setup"; exit 1; }
    FLEET_NODES=200; FLEET_MODE=detection; FLEET_INTERVAL=0.5
    FLEET_TIME_SCALE=1.0; FLEET_MIN_AIRCRAFT=40; FLEET_MAX_AIRCRAFT=60
    # preflight: refuse to start (silently half-broken) if a port is already taken
    for p in 8000 3012 5173; do
        if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
            echo "✗ port :$p already in use — run 'just down' first (inspect: lsof -iTCP:$p)"; exit 1
        fi
    done
    mkdir -p "{{run}}"

    echo "→ backend (uvicorn — http :8000, detection TCP ingest :3012)"
    ( cd "{{be}}" && RETINA_ENV=dev "{{venv}}/bin/uvicorn" main:app --reload ) \
        > "{{run}}/backend.log" 2>&1 &

    echo "→ waiting for backend TCP ingest on :3012 (max 30s) ..."
    ok=0; for _ in $(seq 1 30); do nc -z 127.0.0.1 3012 2>/dev/null && { ok=1; break; }; sleep 1; done
    if [ "$ok" != 1 ]; then
        echo "✗ backend never opened :3012 — see {{run}}/backend.log. Cleaning up."
        pkill -f 'uvicorn main:app' 2>/dev/null || true
        exit 1
    fi

    echo "→ synthetic fleet: ${FLEET_NODES} nodes, mode=${FLEET_MODE}, interval=${FLEET_INTERVAL}s, ${FLEET_MIN_AIRCRAFT}-${FLEET_MAX_AIRCRAFT} aircraft"
    ( cd "{{be}}" && PYTHONPATH=. "{{py}}" -m retina_simulation.orchestrator \
        --nodes "${FLEET_NODES}" --mode "${FLEET_MODE}" \
        --interval "${FLEET_INTERVAL}" --time-scale "${FLEET_TIME_SCALE:-1.0}" \
        --min-aircraft "${FLEET_MIN_AIRCRAFT}" --max-aircraft "${FLEET_MAX_AIRCRAFT}" \
        --seed "${FLEET_SEED:-42}" ) \
        > "{{run}}/fleet.log" 2>&1 &

    echo "→ frontend (vite :5173)"
    ( cd "{{fe}}" && npm run dev ) > "{{run}}/frontend.log" 2>&1 &

    echo
    echo "✓ up.  Open →  http://testmap.localhost:5173/"
    echo "  (plain localhost shows tower search; the testmap.* host selects the live map)"
    echo "  fleet: ${FLEET_NODES} nodes @ ${FLEET_INTERVAL}s (dense dev stream)"
    echo "  logs: just logs    status: just status    stop: just down"

# Stop everything (by port for the servers, by pattern for the portless fleet client)
down:
    #!/usr/bin/env bash
    set -uo pipefail
    kt() { local p="$1"; for c in $(pgrep -P "$p" 2>/dev/null); do kt "$c"; done; kill "$p" 2>/dev/null || true; }
    for port in 8000 3012 5173; do
        for pid in $(lsof -nP -tiTCP:$port -sTCP:LISTEN 2>/dev/null); do
            echo "→ killing pid $pid on :$port"; kt "$pid"
        done
    done
    # the fleet is an outbound TCP client (no listening port); uvicorn --reload has a
    # parent reloader that owns no socket — pattern-kill catches both
    pkill -f 'retina_simulation.orchestrator' 2>/dev/null && echo "→ killed fleet orchestrator" || true
    pkill -f 'uvicorn main:app' 2>/dev/null || true
    # Wait for them to actually go. SIGTERM only *asks*, and uvicorn --reload's
    # child plus the fleet orchestrator can take ~25s to unwind. Reporting "down"
    # on the strength of having sent the signal makes the very next `just status`
    # contradict it, which is exactly the sequence anyone types.
    for _ in $(seq 1 40); do
        alive=0
        for port in 8000 3012 5173; do
            lsof -nP -tiTCP:$port -sTCP:LISTEN >/dev/null 2>&1 && alive=1
        done
        pgrep -f 'retina_simulation.orchestrator' >/dev/null 2>&1 && alive=1
        [ "$alive" = 0 ] && break
        sleep 1
    done
    if [ "${alive:-0}" != 0 ]; then
        echo "⚠ still running after 40s — inspect: just status"; exit 1
    fi
    echo "✓ down"

# Which of the three are alive (port-based, so it never lies due to stale pids)
status:
    #!/usr/bin/env bash
    lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && echo "  backend:  running (:8000/:3012)" || echo "  backend:  not running"
    pgrep -f 'retina_simulation.orchestrator' >/dev/null 2>&1 && echo "  fleet:    running" || echo "  fleet:    not running"
    lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1 && echo "  frontend: running (:5173)" || echo "  frontend: not running"

# Tail all three logs (Ctrl-C to stop tailing; services keep running)
logs:
    tail -n +1 -f "{{run}}/backend.log" "{{run}}/fleet.log" "{{run}}/frontend.log"

# ── retina-test droplet (test-*.retina.fm) ───────────────────────────────────
# Deliberately git-free: rsyncs the WORKING TREE, so uncommitted and unpushed
# work deploys as-is. That is the point of this box, and the reason it is not a
# CI job. Nothing here can reach staging or prod: the host, compose override and
# nginx profile are all fixed to test.
#
# Consequences worth knowing: what runs on the droplet corresponds to no commit,
# so `git log` there tells you nothing (there is no .git). If you need to know
# what is deployed, look at your own working tree. Never point this at prod.
#
# --delete keeps the remote from accumulating files you have since removed
# locally, which otherwise produces builds that work there and nowhere else.
# .git is excluded so the droplet stays git-free; the heavy build inputs
# (node_modules, venvs, vendored tar1090) are excluded because the image builds
# them, and the data dirs because they are droplet state, not source.
#
# Deploy the WORKING TREE to the retina-test droplet (rsync + rebuild, no git)
deploy-test:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v rsync >/dev/null || { echo "rsync not installed"; exit 1; }
    echo "→ syncing working tree to retina-test:/opt/tower-finder"
    # --stats, not --info=stats1: macOS still ships rsync 2.6.9, which predates
    # --info entirely and dies on it.
    rsync -az --delete --stats \
        --exclude '.git/' \
        --exclude 'node_modules/' \
        --exclude '.venv/' \
        --exclude '__pycache__/' \
        --exclude 'tar1090/' \
        --exclude '.testmap-run/' \
        --exclude 'backend/coverage_data/' \
        --exclude 'backend/data/' \
        --exclude 'backend/.env' \
        "{{root}}/" retina-test:/opt/tower-finder/
    echo "→ rebuilding on the droplet (this takes a few minutes)"
    ssh retina-test 'cd /opt/tower-finder && docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build'
    echo
    echo "✓ deployed.  https://test-map.retina.fm  |  https://test-towers.retina.fm"
    echo "  logs:   just deploy-test-logs"
    echo "  health: curl -s https://test-api.retina.fm/api/health"

# Tail the droplet's container logs
deploy-test-logs:
    ssh retina-test 'cd /opt/tower-finder && docker compose -f docker-compose.yml -f docker-compose.test.yml logs -f --tail 100'

# Fleet/pipeline snapshot from the droplet (node count, queue depth, aircraft)
deploy-test-status:
    #!/usr/bin/env bash
    set -uo pipefail
    ssh retina-test 'cd /opt/tower-finder && docker compose -f docker-compose.yml -f docker-compose.test.yml ps' || true
    echo
    curl -s --max-time 10 https://test-api.retina.fm/api/test/dashboard | python3 -c \
      "import sys,json; d=json.load(sys.stdin); n=d['nodes']; h=d['server_health']; p=d['pipeline']; \
       print(f\"nodes={n['active']}  queue={h['frame_queue_utilization_pct']}%  drops={h['frames_dropped']}  on_map={p['aircraft_on_map']}\")" \
      2>/dev/null || echo "  (dashboard unreachable — is the stack up? try: just deploy-test-logs)"
