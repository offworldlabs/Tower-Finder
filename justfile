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

# Bring up backend + synthetic fleet + frontend (background). Open http://testmap.localhost:5173/
# Fleet profile: `just up` (local, dense) · `just up testmap` (8s) · `just up prod` (40s).
# testmap/prod read their fleet params LIVE from the real deploy configs so they can't drift.
up profile="local":
    #!/usr/bin/env bash
    set -euo pipefail
    [ -x "{{py}}" ] || { echo "no backend venv — run: just setup"; exit 1; }
    [ -d "{{fe}}/node_modules" ] || { echo "no frontend deps — run: just setup"; exit 1; }
    # ── Resolve fleet params by profile ────────────────────────────────────────
    #  local   — dev-only dense stream (~1 ellipse/s); no deployed equivalent.
    #  testmap — sourced from docker-compose.test.yml   (the testmap.retina.fm test stack).
    #  prod    — sourced from docker-compose.yml's `fleet` service (the Compose
    #            service that actually serves the live testmap.retina.fm + map.retina.fm).
    case "{{profile}}" in
      local)
        FLEET_NODES=200; FLEET_MODE=detection; FLEET_INTERVAL=0.5
        FLEET_TIME_SCALE=1.0; FLEET_MIN_AIRCRAFT=40; FLEET_MAX_AIRCRAFT=60
        FLEET_METRO=gvl; FLEET_N_CLUSTER=30; FLEET_N_CLUSTERS=1 ;;
      testmap)
        # every FLEET_* value comes straight from the compose file's fleet-simulator block
        eval "$(grep -oE 'FLEET_[A-Z_]+=[^[:space:]]+' "{{root}}/docker-compose.test.yml")" ;;
      prod)
        # every FLEET_* value comes straight from docker-compose.yml's `fleet`
        # service block (same extraction the testmap profile uses on test.yml)
        eval "$(grep -oE 'FLEET_[A-Z_]+=[^[:space:]]+' "{{root}}/docker-compose.yml")" ;;
      *)
        echo "✗ unknown profile '{{profile}}' — use: local | testmap | prod"; exit 1 ;;
    esac
    # fail loudly if extraction ever silently breaks, rather than launch a wrong fleet
    : "${FLEET_INTERVAL:?could not resolve fleet params for profile '{{profile}}'}"
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

    # Metro scoping must be forwarded too, or the testmap/prod profiles would
    # silently run a nationwide fleet while claiming to mirror the compose files.
    METRO_ARGS=()
    if [ -n "${FLEET_METRO:-}" ]; then METRO_ARGS+=(--metro "${FLEET_METRO}"); fi
    if [ -n "${FLEET_METRO_TRAFFIC_FRAC:-}" ]; then METRO_ARGS+=(--metro-traffic-frac "${FLEET_METRO_TRAFFIC_FRAC}"); fi
    if [ -n "${FLEET_N_CLUSTER:-}" ]; then METRO_ARGS+=(--n-cluster "${FLEET_N_CLUSTER}"); fi
    if [ -n "${FLEET_N_CLUSTERS:-}" ]; then METRO_ARGS+=(--n-clusters "${FLEET_N_CLUSTERS}"); fi

    echo "→ synthetic fleet [{{profile}}]: ${FLEET_NODES} nodes, metro=${FLEET_METRO:-nationwide}, mode=${FLEET_MODE}, interval=${FLEET_INTERVAL}s, ${FLEET_MIN_AIRCRAFT}-${FLEET_MAX_AIRCRAFT} aircraft"
    ( cd "{{be}}" && PYTHONPATH=. "{{py}}" -m retina_simulation.orchestrator \
        --nodes "${FLEET_NODES}" --mode "${FLEET_MODE}" \
        ${METRO_ARGS[@]+"${METRO_ARGS[@]}"} \
        --interval "${FLEET_INTERVAL}" --time-scale "${FLEET_TIME_SCALE:-1.0}" \
        --min-aircraft "${FLEET_MIN_AIRCRAFT}" --max-aircraft "${FLEET_MAX_AIRCRAFT}" \
        --seed "${FLEET_SEED:-42}" ) \
        > "{{run}}/fleet.log" 2>&1 &

    echo "→ frontend (vite :5173)"
    ( cd "{{fe}}" && npm run dev ) > "{{run}}/frontend.log" 2>&1 &

    echo
    echo "✓ up [{{profile}}].  Open →  http://testmap.localhost:5173/"
    echo "  (plain localhost shows tower search — the testmap.* host selects the live map)"
    echo "  fleet [{{profile}}]: ${FLEET_NODES} nodes @ ${FLEET_INTERVAL}s.  Profiles: local | testmap (8s) | prod (40s)"
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
