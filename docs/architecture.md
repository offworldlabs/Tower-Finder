# Architecture

A system overview for developers. For setup see [`../ONBOARDING.md`](../ONBOARDING.md);
for the detection internals see [`pipeline.md`](pipeline.md).

## One backend, several surfaces

A single FastAPI app (`backend/`) serves every user-facing surface. They differ
only by subdomain, resolved client-side in `frontend/src/utils/domains.ts`:

- **testmap** — live map fed by the synthetic simulation fleet (dev/demo). Only
  staging and local stacks run a fleet, so `testmap.retina.fm` is served by the
  staging droplet rather than production.
- **map** (`map.retina.fm`) — production live map, real radar nodes only.
- **Tower Finder** — `/api/towers` illuminator search (the original feature).
- **dashboard** (`dashboard/`, separate SPA) — admin: node ownership, claim
  codes, MLAT verification, metrics. Auth required.

## Data flow

```
receiver nodes ──TCP frames──▶ tcp_handler ──▶ frame_queue
                                                   │
                                          frame_processor (N workers)
                                                   │
                              ┌────────────────────┼─────────────────────┐
                              ▼                     ▼                     ▼
                    retina-tracker        node_associator          single-node
                    (Kalman + GNN)     (multi-node candidates)    bistatic arc
                              │                     │
                              ▼                     ▼
                                            solver_queue ──▶ solver workers
                                                              (retina-geolocator,
                                                               LM multinode solve)
                                                   │
                                                   ▼
                              state (in-memory): tracks, aircraft, arcs
                                                   │
                              aircraft_flush_task (~2 Hz) builds aircraft JSON
                                                   │
                    ┌──────────────────────────────┼───────────────────────────┐
                    ▼                               ▼                            ▼
              /ws/aircraft                 /ws/aircraft/live            /ws/aircraft/owner
              (all nodes)                  (real nodes only)            (one owner's nodes)
```

The detection pipeline (tracker → geolocator) is documented in detail in
[`pipeline.md`](pipeline.md). Bistatic uncertainty arcs and how they're rendered
are in [`arc-display.md`](arc-display.md).

## Backend components

- **`routes/`** — HTTP + WebSocket endpoints (towers, radar, streaming, auth,
  admin, analytics, test, output).
- **`services/frame_processor.py`** — turns detection frames into tracks and the
  combined aircraft JSON; builds single-node arcs.
- **`services/tasks/`** — background async tasks: `aircraft_flush` (broadcast),
  `solver` workers, `analytics_refresh`, archive lifecycle, snapshots,
  `health_monitor` + `heartbeat` (see [`alerting.md`](alerting.md)).
- **`core/state.py`** — the in-memory world: connected nodes, tracks, aircraft,
  arc buffers, WebSocket client sets, latest JSON payloads.
- **`core/users.py` + `core/auth.py`** — fastapi-users (cookie JWT, Google/GitHub
  OAuth) plus domain auth: invites, node ownership, claim codes (SQLite).

## The algorithm libraries (submodules)

The math lives in separate repos under `libs/` so it can be versioned and reused:

- **retina-geolocator** — bistatic delay/Doppler position solver. Single-node
  produces an ellipse arc (a locus, not a point); multi-node (n≥2) runs an LM
  least-squares solve for a position, with an altitude sweep for n≥3.
- **retina-tracker** — Kalman multi-target tracker + anomaly detection.
- **retina-simulation** — synthetic fleet generator (powers testmap + CI).
- **retina-custody**, **retina-analytics** — custody protocol and node
  trust/reputation.

## State & storage

- **In-memory first.** Tracks, aircraft, node sessions live in `core.state`.
  A restart drops them.
- **Snapshots.** State is serialized to disk every 60s and restored on boot
  (trust scores, reputations, accuracy samples, node identities).
- **SQLite** (`data/users.db`) — users, invites, node owners, claim codes.
- **R2 (Cloudflare).** Archived coverage/track Parquet is offloaded to the
  `retina-server-archive` bucket and pruned locally (see the runbook).

## Auth model

Cookie-based JWT issued via OAuth (Google/GitHub), shared across surfaces on the
same origin. `AUTH_ALLOW_ANONYMOUS_ADMIN=1` with no OAuth configured grants the
anonymous-admin bypass, independent of `RETINA_ENV`; every environment currently
sets it while OAuth is unconfigured. Node ownership maps
`node_id → user_id`; the `/ws/aircraft/owner` feed and dashboard use it to scope
data to a user's own nodes.

## Deploy

`.github/workflows/ci.yml`: push to `main` → build/test → deploy staging →
staging smoke + E2E → deploy production → prod smoke + E2E. Deploy is an SSH
`git reset --hard origin/main` + `docker compose up -d --build`, gated by a
free-disk pre-flight. Operational detail is in [`runbook.md`](runbook.md).
