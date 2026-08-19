# RETINA / Tower-Finder — developer onboarding

Welcome. This repo powers RETINA, a passive-radar system: a network of receiver
nodes detect aircraft by listening to reflections of broadcast transmitters
(bistatic radar), and the backend turns those detections into tracks and live
positions shown on a web map. It started life as "Tower Finder" (a tool to find
suitable broadcast illuminators near a receiver), which is still one of the
surfaces.

Read this top-to-bottom once; it should get you from a fresh clone to running
the whole thing locally and understanding how the pieces fit. For deeper dives,
see [`docs/architecture.md`](docs/architecture.md) and the other docs linked at
the end.

## The big picture

One FastAPI backend serves several React front-ends, distinguished by subdomain:

| Surface | What it is |
| --- | --- |
| **testmap** | Live aircraft map fed by the simulation fleet (synthetic nodes) — the main dev/demo surface. `testmap.retina.fm` is served by the **staging** droplet, the only environment still running a fleet. |
| **map** | Production live map showing only real radar nodes. |
| **dashboard** | Admin app (auth required): node ownership, claim codes, MLAT verification, metrics. |
| **Tower Finder** | The original `/api/towers` illuminator search. |

Receiver nodes connect over TCP and stream detection frames. The pipeline
(tracker → geolocator) turns frames into aircraft positions, broadcast to the
map over WebSocket. See [`docs/pipeline.md`](docs/pipeline.md).

## Repo layout

```
backend/      FastAPI API, TCP frame ingest, detection pipeline, background tasks
frontend/     React SPA — testmap / map / tower search (Vite + Leaflet)
dashboard/    React admin app (Vite)
libs/         Git submodules (the algorithm libraries — see below)
docs/         Architecture, pipeline, runbook, alerting, simulation, arc-display
```

### Submodules (`libs/`) — the "other repos"

These are separate GitHub repos under `offworldlabs/`, vendored as submodules so
the algorithms can be reused and versioned independently:

| Submodule | Purpose |
| --- | --- |
| `retina-geolocator` | Bistatic delay/Doppler position solver (single- and multi-node, LM least-squares). |
| `retina-tracker` | Multi-target Kalman tracker + anomaly detection. |
| `retina-simulation` | Fleet simulator that generates synthetic radar frames for testmap/CI. |
| `retina-custody` | Custody-protocol library. |
| `retina-analytics` | Node trust/reputation analysis. |

`retina-analytics` is pinned to a commit on its `pin/tower-finder` branch rather
than `main`. That branch carries no CI and no ruff config, so a PR against it
reports no checks at all; verify it from this repo's suite instead.

## Local setup

Clone with submodules, then set up backend and front-ends.

```bash
git clone --recursive https://github.com/offworldlabs/Tower-Finder.git
cd Tower-Finder
# already cloned without --recursive?
git submodule update --init --recursive
```

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -e ../libs/retina-geolocator -e ../libs/retina-tracker
cp .env.example .env          # fill in what you need (see below)
RETINA_ENV=dev AUTH_ALLOW_ANONYMOUS_ADMIN=1 SYNTHETIC_FLEET_ENABLED=1 uvicorn main:app --reload
```

API at `http://localhost:8000`, interactive docs at `/docs`.

`AUTH_ALLOW_ANONYMOUS_ADMIN=1` grants the anonymous-admin bypass, so you don't
need OAuth keys locally: with no OAuth client configured, you're treated as an
admin. `SYNTHETIC_FLEET_ENABLED=1` mounts the simulation ingest routes the
fleet pushes through, without which `/api/sim/adsb/push` answers 404.
`RETINA_ENV=dev` (or `test`) separately relaxes the boot-time secret checks, so a
local run needs no real `JWT_SECRET`; a deployed environment requires one.

All three go on the command line rather than in `.env`, because `main.py` loads
the dotenv file after the modules that read them. `just up` passes them for you.

### Frontend / dashboard

```bash
cd frontend   # or: cd dashboard
npm install
npm run dev
```

Frontend is at `http://localhost:5173`; `/api` and `/ws` are proxied to the
backend on `:8000`. To reach the live-map surface locally, open
`http://testmap.localhost:5173/` (the hostname selects the surface — see
`frontend/src/utils/domains.ts`).

There's a backend-free map sandbox at `/test-radar` (one node, one aircraft,
one ellipse) for working on map rendering without the pipeline.

### Full stack in Docker

To run the built image exactly as the droplets do (nginx rendered from the
shared template, plain HTTP), overlay the laptop compose file on the base:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Serves `http://testmap.localhost:8080` (live map + synthetic fleet),
`http://api.localhost:8080`, and towers/dash/admin on the same port — the
endpoint list and the reasoning live in `docker-compose.local.yml`'s header.
Always pass `--build`: the frontend bundle and backend are baked into the
image, so a plain `up` silently reuses the previous build.

### See real data without running the pipeline

The simulation fleet (`retina-simulation`) feeds testmap. Production runs no
fleet, so `testmap.retina.fm` is served by the staging droplet;
`staging-map.retina.fm` shows the same data under a staging-prefixed name. To
drive a local backend with synthetic frames, see
[`docs/simulation.md`](docs/simulation.md).

### Working in a git worktree

A fresh worktree has empty `libs/` directories and no venv of its own. Build one
the way CI does, or pytest fails at conftest import on a missing `sqlalchemy`
after silently creating an empty `.venv`:

```bash
git submodule update --init
cd backend && uv pip install -r requirements-dev.txt
uv pip install ../libs/retina-geolocator ../libs/retina-tracker \
  ../libs/retina-custody ../libs/retina-simulation ../libs/retina-analytics
```

## Running tests

```bash
# backend
cd backend && RETINA_ENV=test pytest

# frontend / dashboard
cd frontend && npm run test && npm run typecheck && npm run lint
```

Backend coverage gate is 55%. Async tests need `pytest-asyncio` (in
`requirements-dev.txt`) — without it they silently skip.

Trust pytest's **exit status**, not the tail of its output. The warnings block
and the coverage footer both print after the summary line, so piping the run
into `tail` loses the `N passed` and a passing-looking tail proves nothing.

`RADAR_TCP_PORT` defaults to `3012` (`backend/main.py`) and the suite binds it.
Two suites running at once, typically from two worktrees, give bogus route and
health errors in whichever started second. Set the variable in one of them.

Coverage under-reports. `[tool.coverage.run]` in `backend/pyproject.toml` sets no
`concurrency`, so every line after an `await session.…` in a greenlet-backed path
counts as unrun. Before concluding a module is untested, re-measure with
`concurrency = ["thread", "greenlet"]`; the figure can move by tens of percent.

### Before you push

The lint gate is pre-commit, not the two ruff commands:

```bash
backend/.venv/bin/pre-commit run --all-files
```

It runs `ruff-check`, `ruff-format`, a dead-code check (vulture) and `ruff-config`
twice, once per copy of the shared standard in this repo. A change can pass
`ruff check` and `ruff format` by hand and still fail CI on dead code.

Touching a node route or one of its models also moves the node API's wire
contract, which is generated rather than written. Regenerate it in the same
commit, or CI fails on a file you never edited:

```bash
cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi
```

That gate is what makes the generated contract trustworthy: the committed file
cannot be edited by hand to match a change, because the next run regenerates it
and notices.

Two traps in that command:

- **`--all-files` does not mean all files.** pre-commit enumerates through
  `git ls-files`, so untracked files are skipped silently. `git add` new modules
  and tests first, or a clean run tells you nothing about them.
- **Vulture flags module-level singletons** nothing imports yet, so a module built
  ahead of its callers fails on the instance rather than the class.
  `backend/vulture_whitelist.py` is for names referenced dynamically, not for
  future wiring: omit the instance until something uses it.

## How code ships

CI runs on every PR, on push to `main`, and on demand through
`workflow_dispatch` (`.github/workflows/ci.yml`):

1. Any PR, whatever its base: `backend-tests`, `frontend-build`,
   `dashboard-build`, `docker-build`, `env-parity`, plus an automated review.
2. Merge to `main` → deploy to **staging** → staging smoke + Playwright E2E → deploy to **production** → prod smoke + Playwright E2E.

So merging to `main` deploys to production automatically. Work on a feature
branch, open a PR, get it green, then merge.

## Things that will bite you

- **All backend state is in-memory.** A restart loses connected nodes and live
  tracks; state is snapshotted to disk every 60s and restored on boot. Don't
  assume persistence.
- **Submodules.** After pulling, run `git submodule update --init --recursive`
  if `libs/` looks stale or imports fail.
- **Surfaces are hostname-driven.** `localhost` shows tower search; you need a
  `*map.localhost` hostname to get the live map and its default tab.
- **Config vs runtime config.** `backend/config/` is image-only (baked into the
  Docker image); runtime-editable overrides live under `data/runtime/`. See the
  runbook for the volume-shadowing gotcha.
- **A branch opened before #187 runs no CI at all.** The workflow used to trigger
  on `branches: [main]`, which matches a PR's *base*, so a PR stacked on a feature
  branch got no tests, no lint and no build while the lone green automated-review
  tick made the page read as passing. Stacked PRs opened since run the full matrix,
  but an older branch keeps the old workflow until it is rebased.
- **The compose service is `tower-finder`, not `server`.** `docker compose logs
  server` returns nothing and reads as an all-clear when the app is down. Check
  `docker compose ps --services` first.
- **A new per-environment key needs an `env-parity` entry** or CI fails.

## Where to go next

- [`docs/architecture.md`](docs/architecture.md) — system overview, data flow, surfaces, storage.
- [`docs/pipeline.md`](docs/pipeline.md) — detection → tracker → geolocator → aircraft JSON.
- [`docs/arc-display.md`](docs/arc-display.md) — how bistatic uncertainty arcs are drawn.
- [`docs/runbook.md`](docs/runbook.md) — production operations, server access, incident response.
- [`docs/alerting.md`](docs/alerting.md) — monitoring, alerts, and the dead-man's-switch.
- [`docs/simulation.md`](docs/simulation.md) — running the fleet simulator.
