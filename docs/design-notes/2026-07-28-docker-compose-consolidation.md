# Docker Compose Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse retina-server's four standalone Compose files into one prod base plus thin per-environment overrides, leaving a single container entrypoint and a single UI, so the laptop stack faithfully mirrors production.

**Architecture:** `docker-compose.yml` (production) becomes the base that a bare `docker compose up` reads. Staging and the laptop stack become thin overrides layered on it via `docker compose -f docker-compose.yml -f <override>.yml`, so they inherit the base and cannot drift. The legacy `start-test.sh` entrypoint and the tar1090 static-HTML UI (`nginx-test.conf`, `tar1090/html`) are removed; the tar1090 JSON data format and its API routes are untouched.

**Tech Stack:** Docker Compose (v2 merge semantics), bash entrypoint scripts, nginx, GitHub Actions, `just` (justfile) native dev runner.

## Global Constraints

- Compose merge semantics (v2): later `-f` files override earlier ones. Scalars are replaced; mapping keys (e.g. `environment`) merge by key; sequence keys (`ports`, `volumes`, `networks`) merge by append unless the override uses the `!override` tag to replace them wholesale. Copied from the existing `docker-compose.local.yml`, which already relies on `!override`.
- Prod base equals current production behaviour. The only intentional change to `docker-compose.yml` is surfacing `FRAME_WORKERS=8` from `backend/.env`; nothing else about the production deploy changes.
- The laptop stack runs `RETINA_ENV=test` (dev/test auth allowlist: JWT secret optional, auth bypass permitted). It must never run `RETINA_ENV` unset/prod, which trips the production-only auth guards.
- nginx transport selection stays decoupled from `RETINA_ENV`: the cert-free React config is selected by `NGINX_PROFILE=local` → `deploy/nginx-local.conf`, which keeps its name.
- Writing style for any doc/comment edits (per repo `CLAUDE.md`): no em-dashes, British spellings, state each fact once, understated tone.
- Commit staged paths explicitly (`git add <path>`); these flat clones carry unrelated working-tree cruft, so never `git add -A`.
- Commit messages explain the *why*, not just the *what*.

---

## File Structure

**Modified:**
- `docker-compose.yml` — prod base; gains `FRAME_WORKERS=8`.
- `docker-compose.staging.yml` — thinned to staging deltas over the base.
- `deploy/start.sh` — its staging-only nginx swap generalised to the `NGINX_PROFILE` form absorbed from `start-test.sh`.
- `Dockerfile` — drop the `start-test.sh` chmod target and the `tar1090/html` copy.
- `.github/workflows/ci.yml` — `deploy-staging` job invocations prepend `-f docker-compose.yml`.
- `justfile` — `up` recipe drops the `testmap` profile.
- `CLAUDE.md` — "Running locally" reflects the single override; stale tar1090-cache note removed.

**Created:**
- `docker-compose.test.yml` — recreated as the laptop override on the prod base (content derived from today's `docker-compose.local.yml`).

**Deleted:**
- `docker-compose.local.yml` (its role moves into the recreated `docker-compose.test.yml`).
- The old `docker-compose.test.yml` (stress/tar1090 form).
- `deploy/start-test.sh`.
- `deploy/nginx-test.conf`.
- `deploy/deploy-test-network.sh`.

---

## Task 1: Surface FRAME_WORKERS in the prod base

**Files:**
- Modify: `docker-compose.yml:20-27` (the `tower-finder` service `environment:` block)

**Interfaces:**
- Consumes: nothing.
- Produces: `docker-compose.yml` now sets `FRAME_WORKERS=8` in `environment:`, which later overrides inherit (staging overrides it to 6; the laptop override inherits 8).

- [ ] **Step 1: Capture the current resolved value for comparison**

Run:
```bash
cd ~/owl/retina-server
docker compose -f docker-compose.yml config | grep -A1 FRAME_WORKERS || echo "FRAME_WORKERS not in compose (comes from .env)"
```
Expected: not present in compose output (today it lives only in `backend/.env`).

- [ ] **Step 2: Add FRAME_WORKERS to the base environment block**

In `docker-compose.yml`, the `tower-finder` service `environment:` currently holds only `UVICORN_HOST=0.0.0.0` (with its comment). Add the worker count directly below it:

```yaml
      - UVICORN_HOST=0.0.0.0
      # Frame-processing thread pool. Production runs 8 (was previously set only
      # in backend/.env, invisible to git and to the compose overrides); surfaced
      # here so it is visible and inheritable. Staging overrides this to 6.
      - FRAME_WORKERS=8
```

- [ ] **Step 3: Verify the base config renders with the value**

Run:
```bash
docker compose -f docker-compose.yml config | grep FRAME_WORKERS
```
Expected: `FRAME_WORKERS: "8"` appears under the `tower-finder` service.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "compose: surface FRAME_WORKERS=8 in the prod base

It previously lived only in the droplet's backend/.env, so prod's frame-worker
tuning was invisible in git and could not be inherited by the compose overrides
this consolidation introduces. Moving the non-secret value into the base makes it
explicit; secrets stay in .env."
```

---

## Task 2: Generalise nginx profile selection in start.sh

`deploy/start.sh` (the entrypoint prod and staging use) only knows how to swap in the staging nginx config. The generalised `NGINX_PROFILE` selection currently lives only in `deploy/start-test.sh:15-18`. Absorb it so the laptop override (Task 3), which runs `start.sh` with `NGINX_PROFILE=local`, selects `nginx-local.conf`. This is a prerequisite for deleting `start-test.sh` (Task 4).

**Files:**
- Modify: `deploy/start.sh:35-39` (the staging-only nginx swap)
- Test: `deploy/tests/test_nginx_profile.sh` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `start.sh` selects `/app/deploy/nginx-${NGINX_PROFILE}.conf` when it exists, defaulting `NGINX_PROFILE` to `RETINA_ENV`. `NGINX_PROFILE=staging` (or `RETINA_ENV=staging`) still selects `nginx-staging.conf`; `NGINX_PROFILE=local` selects `nginx-local.conf`; unset/prod selects nothing and leaves the baked default in place.

- [ ] **Step 1: Write the failing test**

Create `deploy/tests/test_nginx_profile.sh`. It extracts the selection block from `start.sh` and exercises it against a stub filesystem, asserting each profile picks the right file (and that prod/unset picks nothing).

```bash
#!/usr/bin/env bash
# Unit test for start.sh's nginx-profile selection. Extracts the selection
# block (delimited by the two markers below) and runs it against a temp dir of
# stub nginx-<profile>.conf files, asserting the correct file is copied in.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
start_sh="${here}/../start.sh"

# Pull the block between the markers out of start.sh so the test tracks the
# real code rather than a copy.
block="$(awk '/# >>> nginx-profile-select >>>/{f=1;next} /# <<< nginx-profile-select <<</{f=0} f' "${start_sh}")"
[ -n "${block}" ] || { echo "FAIL: could not extract nginx-profile-select block from start.sh"; exit 1; }

run_case() {
  # $1 = RETINA_ENV, $2 = NGINX_PROFILE, $3 = expected basename copied (or "" for none)
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "${tmp}/app/deploy" "${tmp}/etc/nginx/sites-available"
  for p in staging local test; do echo "marker-${p}" > "${tmp}/app/deploy/nginx-${p}.conf"; done
  echo "marker-baked" > "${tmp}/etc/nginx/sites-available/default"
  RETINA_ENV="$1" NGINX_PROFILE="$2" APP_ROOT="${tmp}/app" NGINX_TARGET="${tmp}/etc/nginx/sites-available/default" \
    bash -c "${block}"
  local got; got="$(cat "${tmp}/etc/nginx/sites-available/default")"
  local want="marker-${3:-baked}"
  if [ "${got}" != "${want}" ]; then
    echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> got '${got}', want '${want}'"; rm -rf "${tmp}"; exit 1
  fi
  rm -rf "${tmp}"
}

run_case "staging" ""      "staging"   # staging via RETINA_ENV default
run_case ""        "local" "local"     # laptop override
run_case "test"    ""      "test"      # test env defaults NGINX_PROFILE=RETINA_ENV
run_case ""        ""      ""           # prod: nothing selected, baked default kept
echo "PASS: nginx-profile selection"
```

Make it executable:
```bash
chmod +x deploy/tests/test_nginx_profile.sh
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `deploy/tests/test_nginx_profile.sh`
Expected: FAIL with "could not extract nginx-profile-select block from start.sh" (the markers and generalised block do not exist yet).

- [ ] **Step 3: Replace the staging-only swap with the generalised, marked block**

In `deploy/start.sh`, replace the current block:

```bash
# Swap nginx config based on environment
if [ "${RETINA_ENV}" = "staging" ] && [ -f /app/deploy/nginx-staging.conf ]; then
    echo "[start.sh] Using staging nginx config for staging.retina.fm domains"
    cp /app/deploy/nginx-staging.conf /etc/nginx/sites-available/default
fi
```

with:

```bash
# >>> nginx-profile-select >>>
# Select the nginx config for the active profile. NGINX_PROFILE defaults to
# RETINA_ENV (so staging -> nginx-staging.conf as before), but the laptop stack
# sets NGINX_PROFILE=local to pick the cert-free nginx-local.conf WITHOUT
# pretending the backend runs in a different environment (RETINA_ENV drives the
# app's own auth gating; overloading it for nginx broke the dev/test allowlists).
# Paths are parameterised (APP_ROOT / NGINX_TARGET) so the block is unit-testable
# outside the container; both default to the in-container locations.
: "${APP_ROOT:=/app}"
: "${NGINX_TARGET:=/etc/nginx/sites-available/default}"
NGINX_PROFILE="${NGINX_PROFILE:-${RETINA_ENV:-}}"
if [ -n "${NGINX_PROFILE}" ] && [ -f "${APP_ROOT}/deploy/nginx-${NGINX_PROFILE}.conf" ]; then
    echo "[start.sh] Using nginx-${NGINX_PROFILE}.conf (profile=${NGINX_PROFILE})"
    cp "${APP_ROOT}/deploy/nginx-${NGINX_PROFILE}.conf" "${NGINX_TARGET}"
fi
# <<< nginx-profile-select <<<
```

This preserves staging (via the `RETINA_ENV` default), adds `local`, and leaves prod (unset profile) on the baked `nginx.conf`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `deploy/tests/test_nginx_profile.sh`
Expected: `PASS: nginx-profile selection`.

- [ ] **Step 5: Sanity-check the whole script still parses**

Run: `bash -n deploy/start.sh`
Expected: no output, exit 0 (no syntax error introduced).

- [ ] **Step 6: Commit**

```bash
git add deploy/start.sh deploy/tests/test_nginx_profile.sh
git commit -m "start.sh: generalise nginx selection to NGINX_PROFILE

The laptop stack is moving onto start.sh (the prod entrypoint) but start.sh only
knew how to swap in the staging nginx config; the general NGINX_PROFILE selection
lived only in the legacy start-test.sh. Absorbing it here lets the laptop pick the
cert-free nginx-local.conf and is the prerequisite for deleting start-test.sh.
Staging is unchanged (NGINX_PROFILE defaults to RETINA_ENV). Paths are
parameterised so the selection is unit-testable outside the container."
```

---

## Task 3: Replace the test/local compose pair with one laptop override on the prod base

Delete the old stress/tar1090 `docker-compose.test.yml`, recreate `docker-compose.test.yml` as the laptop override layered on the prod base (content from today's `docker-compose.local.yml`), delete `docker-compose.local.yml`, and repoint the `justfile` `up` recipe which sourced the old file. This commit must leave the tree self-consistent: the laptop stack works and `just` resolves.

**Files:**
- Delete: `docker-compose.test.yml` (old form), `docker-compose.local.yml`
- Create: `docker-compose.test.yml` (new override)
- Modify: `justfile:40-66` (the `up` recipe header comment, profile case, and error message)

**Interfaces:**
- Consumes: the prod base from Task 1 and the generalised `start.sh` from Task 2.
- Produces: laptop bring-up command `docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build`, serving React at `http://testmap.localhost:8080` with `RETINA_ENV=test`.

- [ ] **Step 1: Remove the two old compose files**

```bash
git rm docker-compose.test.yml docker-compose.local.yml
```

- [ ] **Step 2: Create the new `docker-compose.test.yml` laptop override**

```yaml
# ── Laptop override (RETINA_ENV=test) on the prod base ───────────────────────
# Runs the production stack (docker-compose.yml) on a laptop: same image, fleet
# shape, resource limits and start.sh entrypoint, overriding only what a laptop
# cannot provide (Cloudflare certs, 443/3012, the external edge network). This
# is the faithful pre-deploy mirror; it is not a second config to maintain, it
# is a thin delta over prod.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
#   docker compose -f docker-compose.yml -f docker-compose.test.yml logs -f
#   docker compose -f docker-compose.yml -f docker-compose.test.yml down
#
# Endpoints (nginx routing in deploy/nginx-local.conf, selected via
# NGINX_PROFILE=local; *.localhost resolves to 127.0.0.1):
#   http://testmap.localhost:8080  → React live-radar frontend (also localhost:8080)
#   http://testapi.localhost:8080  → API reverse proxy

services:
  tower-finder:
    # Only 8080 on the host; no 443 (no certs) and 3012 is not needed off-host
    # (the fleet reaches the server over the compose network).
    ports: !override
      - "8080:80"
    # Drop the /etc/ssl/cloudflare bind mount (absent on a laptop); keep the
    # prod data volumes so behaviour matches.
    volumes: !override
      - backend-data:/app/backend/data
      - backend-coverage-data:/app/backend/coverage_data
    # backend/.env holds deploy secrets the testmap does not need; make it
    # optional so a fresh clone runs this without `just setup`.
    env_file: !override
      - path: backend/.env
        required: false
    environment:
      # RETINA_ENV=test keeps the backend in the dev/test auth allowlist (JWT
      # secret optional, auth bypass permitted) rather than tripping the
      # production-only guards. NGINX_PROFILE=local selects the cert-free React
      # nginx on a separate axis (see start.sh).
      - RETINA_ENV=test
      - NGINX_PROFILE=local
    # bash, not sh: start.sh's supervisor uses `wait -n`, a bash builtin dash
    # rejects. Mirrors docker-compose.staging.yml.
    command: ["/bin/bash", "/app/deploy/start.sh"]
    # Don't resurrect the stack on every Docker Desktop start.
    restart: "no"
    # The prod base attaches this service to the external `retina-edge` network
    # (the droplet's Cloudflare edge), which does not exist on a laptop. Replace
    # the network list with just the compose default so bring-up does not fail.
    networks: !override
      - default

  fleet:
    restart: "no"

# Neutralise the external edge network the prod base declares, so `docker
# compose config`/`up` on a laptop does not fail looking for it. No service
# attaches to it here (tower-finder's list is overridden above).
networks:
  retina-edge:
    external: false
```

- [ ] **Step 3: Verify the merged laptop config renders**

Run:
```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml config >/tmp/laptop-config.yaml && echo OK
grep -E 'RETINA_ENV|NGINX_PROFILE|FRAME_WORKERS|8080|FLEET_INTERVAL|retina-edge' /tmp/laptop-config.yaml
```
Expected: `OK`, and the grep shows `RETINA_ENV: "test"`, `NGINX_PROFILE: "local"`, `FRAME_WORKERS: "8"` (inherited from base), the `8080:80` port mapping, `FLEET_INTERVAL: "40.0"` (prod shape inherited), and no unresolved external `retina-edge` error.

- [ ] **Step 4: Bring the stack up and confirm it serves the React UI**

Run:
```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
# wait for health, then:
curl -fsS http://localhost:8080/api/health && echo
curl -fsS http://testmap.localhost:8080/ | grep -iE '<title|root' | head
```
Expected: `/api/health` returns healthy JSON; the `testmap.localhost:8080` HTML is the React app (a `<div id="root">`/Vite bundle), NOT tar1090. If `testmap.localhost` does not resolve, add `127.0.0.1 testmap.localhost` handling or use `curl --resolve testmap.localhost:8080:127.0.0.1 http://testmap.localhost:8080/`.

- [ ] **Step 5: Confirm the fleet connects, then tear down**

Run:
```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml logs --tail=20 fleet
curl -fsS http://localhost:8080/api/radar/nodes | head -c 200 && echo
docker compose -f docker-compose.yml -f docker-compose.test.yml down
```
Expected: fleet log shows nodes connecting; `/api/radar/nodes` returns a non-empty node set.

- [ ] **Step 6: Repoint the justfile `up` recipe off the deleted file**

This is the *minimal* justfile change needed to keep the tree consistent, not the
broader justfile revamp (tracked as a separate follow-on ticket, see "Follow-on work"
below). The `testmap` profile sourced `FLEET_*` from the old `docker-compose.test.yml`;
its stress shape is gone and it now equals the `prod` profile, so remove it. In
`justfile`:

Change the header comment (line 41) from:
```
# Fleet profile: `just up` (local, dense) · `just up testmap` (8s) · `just up prod` (40s).
```
to:
```
# Fleet profile: `just up` (local, dense) · `just up prod` (40s, from docker-compose.yml).
```

Remove the `testmap` case branch (the two lines):
```bash
      testmap)
        # every FLEET_* value comes straight from the compose file's fleet-simulator block
        eval "$(grep -oE 'FLEET_[A-Z_]+=[^[:space:]]+' "{{root}}/docker-compose.test.yml")" ;;
```

Update the profile-comment lines 49-52 to drop the `testmap` line, and change the unknown-profile message (line 65) from:
```bash
        echo "✗ unknown profile '{{profile}}' — use: local | testmap | prod"; exit 1 ;;
```
to:
```bash
        echo "✗ unknown profile '{{profile}}' — use: local | prod"; exit 1 ;;
```

- [ ] **Step 7: Verify the justfile still resolves prod and rejects testmap**

Run:
```bash
just --evaluate >/dev/null && echo "justfile parses"
# prod profile resolves its interval:
bash -c 'eval "$(grep -oE "FLEET_[A-Z_]+=[^[:space:]]+" docker-compose.yml)"; echo "prod FLEET_INTERVAL=$FLEET_INTERVAL"'
```
Expected: `justfile parses` and `prod FLEET_INTERVAL=40.0`. (`just up testmap` would now hit the unknown-profile branch.)

- [ ] **Step 8: Commit**

```bash
git add docker-compose.test.yml justfile
git commit -m "compose: replace test/local pair with one laptop override on the prod base

The old docker-compose.test.yml diverged from the deploy on three axes (legacy
start-test.sh entrypoint, tar1090 UI, an 8s stress fleet) and docker-compose.local.yml
existed only to paper over it. The laptop stack now layers a thin override directly on
the production base, inheriting its fleet shape, resource limits and start.sh verbatim,
so it is a faithful pre-deploy mirror. The justfile 'testmap' profile sourced the
deleted file and its stress shape is dropped, so it is removed (prod covers the 40s
shape). git records the old test.yml as deleted and the new one as added under the
reused name."
```

---

## Task 4: Delete the legacy entrypoint, tar1090 UI, and test-network script

With `start.sh` generalised (Task 2) and the laptop stack moved onto it (Task 3), nothing consumes the legacy files. Remove them and their `Dockerfile` references. The tar1090 JSON data format and API routes are NOT touched (only the static HTML UI is removed).

**Files:**
- Delete: `deploy/start-test.sh`, `deploy/nginx-test.conf`, `deploy/deploy-test-network.sh`
- Modify: `Dockerfile:47-48` (drop the `tar1090/html` copy), `Dockerfile:54-56` (drop `start-test.sh` from the chmod)

- [ ] **Step 1: Confirm nothing still references the files**

Run:
```bash
cd ~/owl/retina-server
grep -rn 'start-test\|nginx-test\.conf\|deploy-test-network' \
  --include='*.yml' --include='*.sh' --include='*.md' --include='Dockerfile*' --include='justfile' . | grep -v node_modules
```
Expected: the only hits are inside the files about to be deleted (and this plan/spec docs). No live consumer in compose, CI, justfile, or other scripts.

- [ ] **Step 2: Delete the three legacy files**

```bash
git rm deploy/start-test.sh deploy/nginx-test.conf deploy/deploy-test-network.sh
```

- [ ] **Step 3: Drop the tar1090 UI copy from the Dockerfile**

Remove these two lines (`Dockerfile:47-48`):
```dockerfile
# tar1090 static files
COPY tar1090/html /app/tar1090/html
```
Leave every other tar1090 reference alone: the backend still writes/reads `tar1090_data` (the JSON aircraft feed), which is unrelated to the deleted static HTML.

- [ ] **Step 4: Drop start-test.sh from the chmod line**

Change `Dockerfile:54-56` from:
```dockerfile
# Deploy scripts + test nginx config (used when RETINA_ENV=test)
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh /app/deploy/start-test.sh
```
to:
```dockerfile
# Deploy scripts
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh
```

- [ ] **Step 5: Verify the image still builds**

Run:
```bash
docker build -t retina-server:consolidation-check .
```
Expected: build succeeds. No error about a missing `tar1090/html` build-context path or a missing `start-test.sh` chmod target.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile deploy/start-test.sh deploy/nginx-test.conf deploy/deploy-test-network.sh
git commit -m "deploy: remove legacy start-test.sh, tar1090 UI, and test-network script

start-test.sh's only remaining logic (nginx-profile selection) moved into start.sh,
so the legacy entrypoint is dead. nginx-test.conf served the old tar1090 static HTML
map that the React frontend replaced; it and the tar1090/html copy are the only place
that UI was built, and nothing serves it now. deploy-test-network.sh drove a
test-network droplet that no longer exists. The tar1090 JSON data format and its API
routes are untouched."
```

---

## Task 5: Thin staging to an override on the base and rewire its CI invocations

`docker-compose.staging.yml` is a full standalone file today. Reduce it to a delta over the prod base and prepend `-f docker-compose.yml` to the `deploy-staging` job's compose invocations, in one commit (a thinned staging file invoked without the base would be broken). Correctness is proved by rendering the resolved config before and after and confirming it is unchanged.

**Files:**
- Modify: `docker-compose.staging.yml` (reduce to deltas)
- Modify: `.github/workflows/ci.yml` — the five `deploy-staging` compose invocations (lines 186, 198, 201, 210, 216), plus removal of two now-orphaned tar1090 clone steps (lines 79-80 in the `docker-build` job and 184-185 in the `deploy-staging` job) that fetched the `tar1090/html` directory the `Dockerfile` no longer copies after Task 4.

**Interfaces:**
- Consumes: the prod base (Task 1).
- Produces: staging is deployed as `docker compose -f docker-compose.yml -f docker-compose.staging.yml ...`; the resolved config is byte-for-byte equivalent to the previous standalone `docker-compose.staging.yml`.

- [ ] **Step 1: Snapshot the current resolved staging config**

Run:
```bash
cd ~/owl/retina-server
docker compose -f docker-compose.staging.yml config > /tmp/staging-before.yaml && echo "snapshot saved"
```
Expected: `snapshot saved`. This is the reference the thinned override must reproduce.

- [ ] **Step 2: Rewrite `docker-compose.staging.yml` as deltas over the base**

Replace the file contents with the deltas below. Most of the base is inherited, but three base attributes that standalone staging never had must be overridden back so the merged config preserves today's staging behaviour, not silently adopt prod's: the base mounts named data volumes (`backend-data`, `backend-coverage-data`) staging lacks; the base attaches the server to the external `retina-edge` network staging never used; and the base's `fleet` loads `backend/.env`, which staging's fleet does not. The `!override`/`external: false` blocks below neutralise all three.

```yaml
# ── Staging override on the prod base ────────────────────────────────────────
# Deltas over docker-compose.yml for the CI staging gate. Deployed as:
#   docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build
# Most of the base is inherited so staging cannot drift from production; the
# !override / external:false blocks below strip three prod-only base attributes
# staging never had (data volumes, the retina-edge edge network, the fleet's
# .env), keeping staging's resolved config equivalent to its former standalone
# form.

services:
  tower-finder:
    container_name: retina-staging-server
    environment:
      - RETINA_ENV=staging
      - CORS_ORIGINS=https://staging.retina.fm,https://staging-api.retina.fm,https://staging-dash.retina.fm,https://staging-map.retina.fm
      - RADAR_TCP_PORT=3012
      - FRAME_WORKERS=6
      - FRAME_QUEUE_SIZE=10000
    command: ["/bin/bash", "/app/deploy/start.sh"]
    # Standalone staging mounted only the cert; the base additionally mounts
    # backend-data / backend-coverage-data. Replace the list so staging keeps
    # just the cert mount it had.
    volumes: !override
      - /etc/ssl/cloudflare:/etc/ssl/cloudflare:ro
    # Standalone staging published ports directly and never joined retina-edge
    # (the base does). Keep the server on the compose default network only.
    networks: !override
      - default
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 90s
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 3G
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"

  fleet:
    container_name: retina-staging-fleet
    # The base's fleet loads backend/.env (for RADAR_API_KEY); standalone
    # staging's fleet did not. Drop the inherited env_file so staging's fleet
    # runtime environment matches its former self. (If Compose rejects an empty
    # !override list, use `env_file: !reset null` instead.)
    env_file: !override []
    environment:
      - FLEET_NODES=50
      - FLEET_REGIONS=us
      - FLEET_MODE=adsb
      - FLEET_INTERVAL=40.0
      - FLEET_TIME_SCALE=1.0
      - FLEET_MIN_AIRCRAFT=20
      - FLEET_MAX_AIRCRAFT=40
      - FLEET_BEAM_WIDTH_DEG=0
      - FLEET_MAX_RANGE_KM=0
      - FLEET_CONCURRENCY=10
      - FLEET_CONNECT_RETRIES=10
      - FLEET_HOST=server
      - FLEET_PORT=3012
      - FLEET_VALIDATE=false
      - FLEET_VALIDATION_URL=http://server:8000
      - FLEET_SEED=42
    deploy:
      resources:
        limits:
          cpus: "1"
          memory: 1G
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"

# Neutralise the inherited external retina-edge declaration to a plain local
# network so a staging deploy does not fail looking for an edge network that is
# not present (no service attaches to it: the server is overridden to `default`
# above). This declaration is the one accepted residual difference from the
# former standalone config.
networks:
  retina-edge:
    external: false
```

Note on `environment`: Compose merges environment by key, so the base's `UVICORN_HOST=0.0.0.0` is inherited (staging need not restate it) and the base's `FRAME_WORKERS=8` is overridden by the `6` above. The fleet's `FLEET_*` values differ from the base and so are fully restated here.

- [ ] **Step 3: Render the thinned config and diff against the snapshot**

Run:
```bash
docker compose -f docker-compose.yml -f docker-compose.staging.yml config > /tmp/staging-after.yaml
diff <(sort /tmp/staging-before.yaml) <(sort /tmp/staging-after.yaml)
```

The two configs will NOT be byte-identical, because the base declares top-level items the former standalone staging never had. The pass condition is: **every difference the diff reports must be one of the accepted residuals below, and nothing else.** Read every non-empty diff line and classify it:

Accepted residuals (top-level declarations that NO staging service attaches to, so they change nothing about a running container):
- the `retina-edge` network (now `external: false` / a plain local network),
- the `backend-data` and `backend-coverage-data` top-level named volume declarations.

Any difference in a SERVICE definition (the `tower-finder` or `fleet` blocks: image, command, environment, env_file, volumes actually mounted, networks actually joined, ports, healthcheck, resources, logging, restart) is NOT acceptable and means an override is missing or wrong: fix it and re-run. In particular confirm the resolved `tower-finder` mounts only the cert (not the data volumes), joins only `default` (not `retina-edge`), and the `fleet` shows no inherited `backend/.env`. Do not proceed until the diff shows only the accepted top-level residuals. Record the exact residual diff lines in the task report so the reviewer can confirm the classification.

- [ ] **Step 4: Prepend the base to the deploy-staging compose invocations**

In `.github/workflows/ci.yml`, the `deploy-staging` job runs five `docker compose -f docker-compose.staging.yml ...` commands (lines 186, 198, 201, 210, 216). Prepend `-f docker-compose.yml ` to each so the base is layered first. For example:

```
docker compose -f docker-compose.staging.yml down --timeout 30
```
becomes
```
docker compose -f docker-compose.yml -f docker-compose.staging.yml down --timeout 30
```

Apply the identical prefix change to all five invocations (`down`, `up ... tower-finder`, `exec ... tower-finder`, `logs`, `up ... --no-deps fleet`). Do not change the production job (lines ~308-334): it already runs the bare `docker compose` against the base.

- [ ] **Step 5: Remove the orphaned tar1090 clone steps from ci.yml**

Task 4 removed the `COPY tar1090/html` from the `Dockerfile`, so the two CI steps that clone `wiedehopf/tar1090` into `tar1090/html` now fetch an external repo nothing consumes. Remove both.

In the `docker-build` job, delete this step (currently lines 79-80):
```yaml
      - name: Fetch tar1090 static assets
        run: git clone --depth 1 https://github.com/wiedehopf/tar1090.git tar1090
```
so the job goes straight from the `actions/checkout` step to `- run: docker build ...`.

In the `deploy-staging` job's remote script, delete these two lines (currently 184-185):
```bash
            # Ensure tar1090 static assets are present
            [ -d tar1090/html ] || git clone --depth 1 https://github.com/wiedehopf/tar1090.git tar1090
```
Leave the surrounding `git fetch`/`git reset --hard`/`git submodule update` and the `docker compose ... down` lines intact. Do not touch the production deploy job (it has no such clone step).

- [ ] **Step 6: Verify the CI file — base-prefixed staging invocations, no tar1090 left**

Run:
```bash
grep -n 'docker compose' .github/workflows/ci.yml | grep 'docker-compose.staging.yml'
grep -n 'tar1090' .github/workflows/ci.yml || echo "no tar1090 references remain in ci.yml"
```
Expected: every line mentioning `docker-compose.staging.yml` also contains `-f docker-compose.yml -f docker-compose.staging.yml` (base first, no bare staging invocation); and no `tar1090` references remain in `ci.yml`.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.staging.yml .github/workflows/ci.yml
git commit -m "compose: make staging an override on the prod base

Staging was a full standalone copy that drifted from production. It becomes a delta
over docker-compose.yml, and the deploy-staging CI job layers the base first. The
resolved config is unchanged (verified by diffing docker compose config before and
after), so the staging gate's behaviour is identical; only the duplication is gone.

Also drops the two CI steps that cloned wiedehopf/tar1090 into tar1090/html: the
Dockerfile no longer copies that directory (the tar1090 static UI was removed), so the
clone fetched an external repo nothing consumes."
```

---

## Task 6: Update the running-locally docs

`CLAUDE.md` documents the old two-file laptop command and carries a note about the tar1090 UI that no longer applies (the nginx-test path is deleted).

**`CLAUDE.md` is deliberately untracked repo-wide (it is a developer-local file, absent from `main` and `HEAD`). This task edits it on disk so the local doc is correct, but MUST NOT `git add` or commit it — it stays untracked.** There is no commit step for this task.

**Files:**
- Modify (on disk only, not committed): `CLAUDE.md` ("Running locally" section)

- [ ] **Step 1: Update the Docker bring-up command and drop the stale tar1090 note**

In `CLAUDE.md`, change the Docker command block from:
```
  docker compose -f docker-compose.test.yml -f docker-compose.local.yml up -d --build
  docker compose -f docker-compose.test.yml -f docker-compose.local.yml down
```
to:
```
  docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
  docker compose -f docker-compose.yml -f docker-compose.test.yml down
```

Update the surrounding prose so it describes the stack as the production base plus the laptop `test` override (rather than "the test stack"), and keep the `--build` warning (the nginx config, frontend bundle and backend are still baked into the image).

Remove the final paragraph about `testmap.localhost` showing "the old tar1090 UI after a rebuild" being browser cache: that failure mode described the now-deleted `nginx-test.conf` path and is misleading once the only UI is React.

- [ ] **Step 2: Verify no stale references remain in the docs**

Run:
```bash
grep -n 'docker-compose.local.yml\|nginx-test\|tar1090 UI\|just up testmap' CLAUDE.md
```
Expected: no matches.

- [ ] **Step 3: Do NOT commit**

`CLAUDE.md` is intentionally untracked. Leave the edit on disk uncommitted; do not `git add` it. Confirm it is still untracked:

```bash
git status --short CLAUDE.md
```
Expected: `?? CLAUDE.md` (untracked), not staged.

---

## Task 7: Final verification sweep

**Files:** none (verification only).

- [ ] **Step 1: Confirm the whole tree is free of references to the removed artefacts**

Run:
```bash
cd ~/owl/retina-server
grep -rn 'start-test\.sh\|nginx-test\.conf\|deploy-test-network\|docker-compose\.local\.yml' \
  --include='*.yml' --include='*.yaml' --include='*.sh' --include='*.md' --include='Dockerfile*' --include='justfile' . \
  | grep -v node_modules | grep -v 'docs/superpowers/'
```
Expected: no matches outside the spec/plan docs under `docs/superpowers/`.

- [ ] **Step 2: Confirm the three intended compose files exist and render**

Run:
```bash
ls docker-compose*.yml
docker compose -f docker-compose.yml config >/dev/null && echo "prod OK"
docker compose -f docker-compose.yml -f docker-compose.staging.yml config >/dev/null && echo "staging OK"
docker compose -f docker-compose.yml -f docker-compose.test.yml config >/dev/null && echo "laptop OK"
```
Expected: exactly `docker-compose.yml`, `docker-compose.staging.yml`, `docker-compose.test.yml`; each `... OK` line prints.

- [ ] **Step 3: Re-run the start.sh unit test**

Run: `deploy/tests/test_nginx_profile.sh`
Expected: `PASS: nginx-profile selection`.

- [ ] **Step 4: Update REPO_MAP.md if this changed retina-server's described role**

The consolidation does not change retina-server's cross-repo role, so `../REPO_MAP.md` likely needs no edit. Confirm by checking whether it enumerates the compose files:
```bash
grep -n 'docker-compose\|start-test\|tar1090' ~/owl/REPO_MAP.md || echo "no compose detail in REPO_MAP; nothing to update"
```
If it does list them, update to match the new three-file layout in a final commit; otherwise no action.

---

## Follow-on work (separate ticket)

- **justfile revamp.** This plan makes only the minimal justfile change required by the
  file deletion (removing the `testmap` profile). A broader rework of the `up` recipe and
  the profile model is deferred to its own ticket, to be created and linked to this work.

---

## Self-Review (completed by plan author)

- **Spec coverage:** base FRAME_WORKERS (Task 1) ✓; start.sh NGINX_PROFILE absorb (Task 2) ✓; laptop override on base + old test.yml deletion + local.yml deletion (Task 3) ✓; legacy start-test.sh/nginx-test.conf/deploy-test-network.sh + tar1090/html Dockerfile deletions (Task 4) ✓; staging thinning + CI rewire (Task 5) ✓; justfile testmap removal (Task 3) ✓; CLAUDE.md docs (Task 6) ✓; verification greps + build + config renders (Tasks 4,5,7) ✓. tar1090 data format explicitly preserved (Task 4) ✓.
- **Placeholder scan:** no TBD/TODO; every code and config block is spelled out in full.
- **Type/name consistency:** `NGINX_PROFILE`, `nginx-local.conf`, `RETINA_ENV=test`, the `>>> nginx-profile-select >>>` markers, and the `-f docker-compose.yml -f <override>.yml` invocation form are used identically across Tasks 2, 3, 5, 6.
