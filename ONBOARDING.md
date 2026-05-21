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
| **testmap** | Live aircraft map fed by the simulation fleet (synthetic nodes) — the main dev/demo surface. |
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
RETINA_ENV=dev uvicorn main:app --reload
```

API at `http://localhost:8000`, interactive docs at `/docs`.

`RETINA_ENV=dev` (or `test`) enables an auth bypass so you don't need OAuth keys
locally — you're treated as an anonymous admin. In production a real
`JWT_SECRET` and OAuth credentials are required.

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

### See real data without running the pipeline

The simulation fleet (`retina-simulation`) feeds the public testmap. To drive a
local backend with synthetic frames, see [`docs/simulation.md`](docs/simulation.md).

## Running tests

```bash
# backend
cd backend && RETINA_ENV=test pytest

# frontend / dashboard
cd frontend && npm run test && npm run typecheck && npm run lint
```

Backend coverage gate is 55%. Async tests need `pytest-asyncio` (in
`requirements-dev.txt`) — without it they silently skip.

## How code ships

CI runs on every PR and on push to `main` (`.github/workflows/ci.yml`):

1. PR: `backend-tests`, `frontend-build`, `dashboard-build`, `docker-build`, plus an automated review.
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

## Where to go next

- [`docs/architecture.md`](docs/architecture.md) — system overview, data flow, surfaces, storage.
- [`docs/pipeline.md`](docs/pipeline.md) — detection → tracker → geolocator → aircraft JSON.
- [`docs/arc-display.md`](docs/arc-display.md) — how bistatic uncertainty arcs are drawn.
- [`docs/runbook.md`](docs/runbook.md) — production operations, server access, incident response.
- [`docs/alerting.md`](docs/alerting.md) — monitoring, alerts, and the dead-man's-switch.
- [`docs/simulation.md`](docs/simulation.md) — running the fleet simulator.
