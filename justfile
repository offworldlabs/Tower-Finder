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
    just --justfile "{{justfile()}}" migrate
    echo "→ frontend deps"
    cd "{{fe}}" && npm install
    echo "✓ setup complete — now: just up"

# Bring the dev database to head. Idempotent, and run by both setup and up.
migrate:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -x "{{py}}" ] || { echo "no backend venv — run: just setup"; exit 1; }
    # RETINA_ENV is passed explicitly because migrations/env.py does not load
    # .env (only main.py does). create_all is guarded off outside tests, so this
    # is the only thing that ever builds the dev schema: without it a fresh clone
    # boots against an empty file, and a tree that has just pulled a new revision
    # boots against a stale one — both failing later, on the first query to touch
    # the missing table or column, instead of here where the cause is obvious.
    # That is why `up` runs it on every start rather than `setup` running it
    # once, and it is the same reasoning that has deploy/start.sh migrate on
    # every container boot.
    #
    # A revision this tree cannot locate is not automatically fatal, for the
    # reason start.sh sets out at length: checking out an older branch is the dev
    # equivalent of a rollback, and a revision the older code never touches is
    # harmless to leave in place. Unlike a deploy, though, dev branches diverge,
    # so "a revision I do not recognise" does NOT imply "the database is ahead of
    # me" here — it is equally the shape of a sibling branch's revision, where
    # this tree's own tables and columns were never applied and booting gives the
    # "no such table" this recipe exists to prevent. Alembic reports both
    # identically, since the recorded revision is absent from the graph either
    # way, so the schema is asked instead: a rollback leaves a superset of what
    # the models want, a sibling leaves a gap. See scripts/check_schema.py.
    #
    # Anything else — a broken revision, a locked database — still aborts. The
    # substring matched is Alembic's own wording, kept honest by
    # backend/tests/test_migrations.py's
    # test_rollback_ahead_sentinel_matches_alembics_wording (the literal lives in
    # backend/tests/migration_helpers.py as ROLLBACK_AHEAD_SENTINEL).
    #
    # Testing the assignment itself is what keeps set -e from aborting on the
    # very failure this block exists to inspect.
    echo "→ database schema (alembic)"
    cd "{{be}}"
    if out=$(RETINA_ENV=dev "{{py}}" -m alembic upgrade head 2>&1); then
        # alembic.ini pins the root logger to WARN, so a successful upgrade says
        # nothing at all; print whatever it does say and leave it at that.
        if [ -n "$out" ]; then printf '%s\n' "$out"; fi
    elif printf '%s\n' "$out" | grep -q "Can't locate revision"; then
        printf '%s\n' "$out"
        if gaps=$(RETINA_ENV=dev "{{py}}" -m scripts.check_schema 2>&1); then
            echo "⚠ that revision is not in this tree, but the schema has everything"
            echo "  this tree expects (rolled back to an older branch?) — continuing"
        else
            echo "✗ the database is at a revision this tree does not have, AND is missing"
            echo "  what this tree expects — another branch's migrations are in it:"
            printf '%s\n' "$gaps"
            echo "  Fix it on the branch that made them (alembic downgrade), or, if the dev"
            echo "  data is expendable, delete that database file and re-run."
            exit 1
        fi
    else
        echo "✗ alembic upgrade head failed:"
        printf '%s\n' "$out"
        exit 1
    fi

# Bring up backend + synthetic fleet + frontend (background). Open http://testmap.localhost:5173/
# Fleet profile: `just up` (local, dense) · `just up test` (50 fps) · `just up prod` (12.5 fps).
# test/prod read their fleet params LIVE from the real deploy configs so they can't drift.
up profile="local":
    #!/usr/bin/env bash
    set -euo pipefail
    [ -x "{{py}}" ] || { echo "no backend venv — run: just setup"; exit 1; }
    [ -d "{{fe}}/node_modules" ] || { echo "no frontend deps — run: just setup"; exit 1; }
    # ── Resolve fleet params by profile ────────────────────────────────────────
    #  local — dev-only dense stream (~1 ellipse/s); no deployed equivalent.
    #  test  — the retina-test droplet's fleet: 50 nodes at a 1.0s per-node
    #          detection rate, so 50 frames/s reach the server. staging runs the
    #          same shape; there is no separate profile for it.
    #  prod  — docker-compose.prod.yml's `fleet` service, the one that actually
    #          serves live testmap.retina.fm + map.retina.fm: 25 nodes at 2.0s,
    #          12.5 fps.
    #
    # Both read the overlay PLUS docker-compose.yml, because the connection
    # settings the environments share live in the base. The extraction matches
    # only list entries (`- FLEET_X=y`), never prose: the overlays discuss
    # variables like FLEET_METRO="" in their comments, and an unanchored grep
    # would eval those too.
    fleet_env() {
        grep -hoE '^[[:space:]]*-[[:space:]]*FLEET_[A-Z_]+=[^[:space:]]+' "$@" \
            | sed -E 's/^[[:space:]]*-[[:space:]]*//'
    }
    case "{{profile}}" in
      local)
        FLEET_NODES=200; FLEET_MODE=detection; FLEET_INTERVAL=0.5
        FLEET_TIME_SCALE=1.0; FLEET_MIN_AIRCRAFT=40; FLEET_MAX_AIRCRAFT=60
        FLEET_METRO=gvl; FLEET_N_CLUSTER=30; FLEET_N_CLUSTERS=1 ;;
      test)
        eval "$(fleet_env "{{root}}/docker-compose.yml" "{{root}}/docker-compose.test.yml")" ;;
      prod)
        eval "$(fleet_env "{{root}}/docker-compose.yml" "{{root}}/docker-compose.prod.yml")" ;;
      *)
        echo "✗ unknown profile '{{profile}}' — use: local | test | prod"; exit 1 ;;
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

    # Every start, not just `setup`: pulling a branch that adds a revision is the
    # ordinary case, and booting against the schema it expects has to be the
    # default rather than something you remember to do. See the migrate recipe.
    just --justfile "{{justfile()}}" migrate

    echo "→ backend (uvicorn — http :8000, detection TCP ingest :3012)"
    # Both flags have to be granted by name since ClickUp 86cb49d29; RETINA_ENV=dev
    # alone no longer implies either. Without the bypass a laptop would need real
    # OAuth credentials to load the dashboard, and without SYNTHETIC_FLEET_ENABLED
    # main.py leaves the simulation ingest router unmounted, which the fleet
    # started below POSTs through.
    ( cd "{{be}}" && RETINA_ENV=dev AUTH_ALLOW_ANONYMOUS_ADMIN=1 SYNTHETIC_FLEET_ENABLED=1 "{{venv}}/bin/uvicorn" main:app --reload ) \
        > "{{run}}/backend.log" 2>&1 &

    echo "→ waiting for backend TCP ingest on :3012 (max 30s) ..."
    ok=0; for _ in $(seq 1 30); do nc -z 127.0.0.1 3012 2>/dev/null && { ok=1; break; }; sleep 1; done
    if [ "$ok" != 1 ]; then
        echo "✗ backend never opened :3012 — see {{run}}/backend.log. Cleaning up."
        pkill -f 'uvicorn main:app' 2>/dev/null || true
        exit 1
    fi

    # Metro scoping must be forwarded too, or the test/prod profiles would
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
    echo "  fleet [{{profile}}]: ${FLEET_NODES} nodes @ ${FLEET_INTERVAL}s/node.  Profiles: local | test (50 fps) | prod (12.5 fps)"
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

# ── retina-test droplet ──────────────────────────────────────────────────────
# `deploy-test` deploys by rsync from the working tree, not by git. That is
# deliberate: staging and production deploy from `main` through CI precisely so
# nothing unreviewed reaches them, and the whole point of this recipe is to run a
# branch under load BEFORE it is reviewed. That rationale is unchanged, and this
# remains the droplet's primary path.
#
# The consequence is that what runs there is whatever was in your tree, including
# uncommitted edits, so `deploy-test-status` prints the local HEAD it was cut from
# and whether that tree was dirty. Read it as a label, not a guarantee.
#
# What HAS changed: the droplet is no longer git-free. .github/workflows/
# deploy-test.yml added a dispatch-only CI path that deploys a pushed ref by git,
# so that the production auto-rollback machinery can be exercised end to end
# without breaking production — pre-deploy.sh and rollback.sh are both git-based
# and cannot run on a tree with no .git.
#
# The `--exclude '.git'` below therefore now means "leave the clone alone" rather
# than "there deliberately is not one". Nothing about this recipe changes: rsync
# still never creates or deletes a .git, and the clone survives every sync.
#
# The earlier worry that a git remote would "invite pushing to it directly" is
# handled by what the remote IS — origin is the GitHub repo, so deploy-test.yml
# can only deploy a ref that has been pushed there. Reaching the droplet without
# review still means this recipe, which is the point.

# The ssh target for the test droplet. Overridable, and deliberately not a
# hostname or an address: this repo is public, so it should not be where anyone
# learns what the infrastructure is called or where it lives. Set RETINA_TEST_HOST
# to whatever your own ~/.ssh/config calls it.
host_test := env_var_or_default("RETINA_TEST_HOST", "retina-test")
app_test  := env_var_or_default("RETINA_TEST_APP_DIR", "/opt/tower-finder")

# rsync the working tree to retina-test and rebuild the stack there
deploy-test:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "→ syncing working tree to {{host_test}}:{{app_test}}"
    # --delete so a file removed here is removed there: without it the droplet
    # accumulates the very stale configs this branch spent its time deleting.
    # (Excluded paths are NOT deleted on the receiver — no --delete-excluded — so
    # the droplet's own .env, secrets and data volumes survive every sync.)
    #
    # Rules are evaluated in the order given and the first match wins, which is
    # what makes the three groups below meaningful:
    #
    # 1. Protect the receiver's state and leave .git alone — the droplet is a
    #    clone (see the note above), and rsync must neither create nor touch one.
    # 2. Force back in the things git IGNORES but the build needs. rsync has no
    #    notion of "tracked anyway", which git does: a tracked file still ships
    #    even when a pattern matches it. Without these the gitignore filter below
    #    silently drops them and --delete removes them from the droplet.
    #      metro_tower_cache — tracked in retina-simulation, whose own .gitignore
    #                          says *.json; it is what spares fleet generation the
    #                          Tower API round-trips
    #      .gitkeep          — tracked placeholders inside ignored data dirs
    # 3. Honour .gitignore. This is a REMOTE host, and .gitignore marks paths as
    #    local-only precisely because they must not leave the machine —
    #    .github/instructions/, .github/prompts/ and .claude/ are labelled
    #    "server credentials, ops, private prompts — never push" in it. A
    #    hand-maintained exclude list silently ships every one of them the day
    #    someone creates it; deferring to .gitignore cannot go stale that way.
    rsync_rules=(
        --exclude '.git'
        --exclude '.env'
        --exclude 'backend/.env'
        --exclude 'backend/data'
        --exclude 'backend/coverage_data'
        --include '**/metro_tower_cache.json'
        --include '**/.gitkeep'
        --filter=':- .gitignore'
        --exclude '.venv'
        --exclude 'node_modules'
        --exclude '__pycache__'
        --exclude '.testmap-run'
    )
    # Preflight: prove the rules above do not drop anything git tracks. The two
    # systems disagree by design — git keeps tracked files regardless of ignore
    # rules, rsync does not — so every new ignore pattern is a chance to silently
    # stop shipping a source file and then delete it on the far side. Assert it
    # instead of trusting the include list to stay complete.
    #
    # Two deliberate omissions are allowed through:
    #   .claude/      tracked inside a submodule; agent config, no business on a
    #                 server, and .gitignore marks it never-push anyway
    #   backend/data/ excluded above to protect the droplet's live users.db and
    #                 archive; its tracked README is collateral and not needed
    dropped=$(comm -23 \
        <({ git -C "{{root}}" ls-files; \
            git -C "{{root}}" submodule --quiet foreach --recursive 'git ls-files | sed "s#^#$sm_path/#"'; \
          } | sort -u) \
        <(rsync -an --out-format='%n' "${rsync_rules[@]}" "{{root}}/" "{{root}}/.rsync-check/" 2>/dev/null \
            | sed 's#/$##' | sort -u) \
        | grep -vE '/\.claude/|^backend/data/' || true)
    rmdir "{{root}}/.rsync-check" 2>/dev/null || true
    if [ -n "$dropped" ]; then
        echo "✗ these tracked files would NOT reach the droplet — fix the rsync rules:"
        printf '    %s\n' $dropped
        exit 1
    fi
    rsync -az --delete "${rsync_rules[@]}" "{{root}}/" "{{host_test}}:{{app_test}}/"
    # Record what was sent, so deploy-test-status can report it. Written after the
    # rsync rather than before, or --delete would remove it again. Built with
    # printf rather than a heredoc: just indents every recipe line, and an indented
    # terminator does not close a <<EOF (nor does <<- strip spaces, only tabs).
    if git -C "{{root}}" diff --quiet && git -C "{{root}}" diff --cached --quiet; then
        dirty=no
    else
        dirty=YES
    fi
    printf 'commit=%s\nbranch=%s\ndirty=%s\ndeployed=%s\n' \
        "$(git -C "{{root}}" rev-parse --short HEAD)" \
        "$(git -C "{{root}}" rev-parse --abbrev-ref HEAD)" \
        "$dirty" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        | ssh "{{host_test}}" "cat > {{app_test}}/.deployed-from"
    # Select the test overlay, from the tree we just synced — the same thing the
    # CI deploys do for prod and staging, and for the same reason. This is the one
    # box with no CI, so it was also the one box where a missing ./.env stayed a
    # silent, hand-fixed failure: compose would resolve the base alone, start.sh
    # would abort on the unset RETINA_ENV, and the only symptom would be the
    # health gate below timing out after 120s saying nothing about the cause.
    # The example file is already on the droplet by now, so just use it.
    echo "→ rebuilding on {{host_test}}"
    ssh "{{host_test}}" "cd {{app_test}} && cp deploy/env.test.example .env && docker compose up -d --build"
    # Ask uvicorn directly, inside the container, exactly as the compose
    # healthcheck does. Going through nginx on plain HTTP would only prove the
    # template's HTTP->HTTPS redirect works: it answers 301, and curl -sf treats a
    # 301 as success, so a crash-looping app would still have reported healthy.
    #
    # The retry loop runs HERE rather than on the far side, so the remote command
    # stays a single-quoting-level string. A loop sent through ssh would need the
    # python source escaped through both shells, which is how this went wrong the
    # first time.
    echo "→ waiting for health..."
    healthy=no
    for _ in $(seq 1 24); do
        if ssh "{{host_test}}" "cd {{app_test}} && docker compose exec -T tower-finder python3 -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8000/api/health\")'" >/dev/null 2>&1; then
            healthy=yes; break
        fi
        sleep 5
    done
    if [ "$healthy" != yes ]; then
        echo "  ✗ not healthy after 120s — inspect: just deploy-test-logs tower-finder"
        exit 1
    fi
    echo "  ✓ healthy"
    just --justfile "{{justfile()}}" deploy-test-status

# What retina-test is running, and what it was cut from
deploy-test-status:
    #!/usr/bin/env bash
    set -uo pipefail
    echo "── {{host_test}} ──"
    ssh "{{host_test}}" "cat {{app_test}}/.deployed-from 2>/dev/null || echo '(no deploy marker — provisioned by hand?)'"
    # Four opening braces is just's escape for two literal ones; the closing pair
    # needs no escaping. Do not put backticks in a recipe comment — just evaluates
    # them as shell substitution even inside a comment, and the recipe dies.
    ssh "{{host_test}}" "cd {{app_test}} && docker compose ps --format 'table {{{{.Name}}\t{{{{.Status}}'"
    # Same reason as the health gate above: in-container, straight to uvicorn, so
    # this reports the app rather than nginx's redirect. Single-quoted python
    # inside a double-quoted remote command — one level of escaping, no heredoc
    # (just indents every recipe line, so a heredoc terminator never closes).
    echo "── fleet ──"
    ssh "{{host_test}}" "cd {{app_test}} && docker compose exec -T tower-finder python3 -c 'import json,urllib.request; d=json.load(urllib.request.urlopen(\"http://localhost:8000/api/radar/nodes\")); n=d[\"nodes\"]; print(\"  nodes:\", len(n), \"total,\", sum(1 for v in n.values() if v.get(\"is_synthetic\")), \"synthetic,\", d.get(\"connected\"), \"connected\")'" 2>/dev/null || echo "  (nodes endpoint unreachable)"

# Tail retina-test's container logs (Ctrl-C to stop; the stack keeps running)
deploy-test-logs service="":
    ssh "{{host_test}}" "cd {{app_test}} && docker compose logs -f --tail 100 {{service}}"
