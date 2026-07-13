set shell := ["bash", "-cu"]

# Local dev runner for the testmap live map: backend + synthetic fleet + frontend.
# `just setup` once, then `just up` / `just down`. Runtime pids/logs go in .testmap-run/.

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
    uv venv .venv
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
up:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -x "{{py}}" ] || { echo "no backend venv — run: just setup"; exit 1; }
    [ -d "{{fe}}/node_modules" ] || { echo "no frontend deps — run: just setup"; exit 1; }
    if [ -f "{{run}}/backend.pid" ] && kill -0 "$(cat {{run}}/backend.pid)" 2>/dev/null; then
        echo "already running — 'just down' first, or 'just status'"; exit 1
    fi
    mkdir -p "{{run}}"

    echo "→ backend (uvicorn — http :8000, detection TCP ingest :3012)"
    ( cd "{{be}}" && RETINA_ENV=dev "{{venv}}/bin/uvicorn" main:app --reload ) \
        > "{{run}}/backend.log" 2>&1 &
    echo $! > "{{run}}/backend.pid"

    echo "→ waiting for backend TCP ingest on :3012 (max 30s) ..."
    for _ in $(seq 1 30); do nc -z 127.0.0.1 3012 2>/dev/null && break; sleep 1; done

    echo "→ synthetic fleet (retina_simulation.orchestrator, 200 nodes, ~40-60 aircraft, offline detection mode)"
    ( cd "{{be}}" && PYTHONPATH=. "{{py}}" -m retina_simulation.orchestrator \
        --nodes 200 --mode detection --min-aircraft 40 --max-aircraft 60 ) \
        > "{{run}}/fleet.log" 2>&1 &
    echo $! > "{{run}}/fleet.pid"

    echo "→ frontend (vite :5173)"
    ( cd "{{fe}}" && npm run dev ) > "{{run}}/frontend.log" 2>&1 &
    echo $! > "{{run}}/frontend.pid"

    echo
    echo "✓ up.  Open →  http://testmap.localhost:5173/"
    echo "  (plain localhost shows tower search — the testmap.* host selects the live map)"
    echo "  logs: just logs    status: just status    stop: just down"

# Stop everything 'just up' started (kills each process tree, then a pattern-based sweep)
down:
    #!/usr/bin/env bash
    set -uo pipefail
    kt() { local p="$1"; for c in $(pgrep -P "$p" 2>/dev/null); do kt "$c"; done; kill "$p" 2>/dev/null || true; }
    if [ -d "{{run}}" ]; then
        for svc in frontend fleet backend; do
            f="{{run}}/$svc.pid"
            [ -f "$f" ] || continue
            echo "→ stopping $svc (pid $(cat "$f") + children)"
            kt "$(cat "$f")"
            rm -f "$f"
        done
    fi
    # belt-and-braces in case a pid file was stale
    pkill -f 'uvicorn main:app' 2>/dev/null || true
    pkill -f 'retina_simulation.orchestrator' 2>/dev/null || true
    echo "✓ down"

# Which of the three are alive
status:
    #!/usr/bin/env bash
    for svc in backend fleet frontend; do
        f="{{run}}/$svc.pid"
        if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
            echo "  $svc: running (pid $(cat "$f"))"
        else
            echo "  $svc: not running"
        fi
    done

# Tail all three logs (Ctrl-C to stop tailing; services keep running)
logs:
    tail -n +1 -f "{{run}}/backend.log" "{{run}}/fleet.log" "{{run}}/frontend.log"
