# Architecture

A system overview for developers. For setup see [`../ONBOARDING.md`](../ONBOARDING.md);
for the detection internals see [`pipeline.md`](pipeline.md).

## One backend, several surfaces

A single FastAPI app (`backend/`) serves every user-facing surface. They differ
only by subdomain, resolved client-side in `frontend/src/utils/domains.ts`:

- **testmap** — live map fed by the synthetic simulation fleet (dev/demo).
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
- **`services/frame_processor.py`** — frame ingest: turns detection frames into
  per-node tracks. Split satellites: `aircraft_feed.py` (combined aircraft JSON
  assembly), `track_gates.py` (per-track gates, dead-reckoning, anomaly flags,
  single-node arc builder), `feed_gc.py` (stale-store GC), `feed_helpers.py`
  (dedup, history, arc-motion velocity), `geo.py` (one home for the spherical
  geometry and the beam/range semantics every gate shares).  Node beam/range
  geometry (`beam_azimuth_deg`, `beam_width_deg`, `max_range_km`,
  `max_bistatic_range_km`) flows from node registration into the per-node
  pipelines, the arc builder, and inter-node association — one contract.
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
  Runtime-tunable via `PUT /api/simulation/config` (target-class fractions,
  aircraft counts), which the fleet polls every 5 s; fleet scale itself comes
  from the deployment env (`FLEET_*` in the compose files).
- **retina-custody** — custody protocol.
- **retina-analytics** — node trust/reputation, inter-node track association
  (pairing + top-down claiming), and per-node empirical coverage / learned
  FOV (see [`pipeline.md`](pipeline.md) §4 and §7).

## Feature gates

The multi-node stack's newer stages ship behind env flags (set in the
gitignored `backend/.env`; unset = the safe default):

| Env | Values | Default | Staging | Gates |
|-----|--------|---------|---------|-------|
| `SOLVER_CONSENSUS_MODE` | `off/shadow/active` | `off` | `active` | n≥3 consensus-refine hypothesis stage |
| `ASSOC_CLAIM_MODE` | `off/shadow/active` | `off` | `active` | top-down tracklet claiming from global tracks |
| `FOV_MODE` | `off/shadow/active` | `off` | `active` | learned empirical FOV as association grid + solver beam gate |
| `ADSB_SEED_MODE` | `off/shadow/active` | `off` | `active` | ADS-B-seeded detection assignment: verified lit tracklets leave dark pairing, re-emitted as `mn-adsb-*` seeded solves |
| `TRACK_SMOOTHER` | `kf/ewma/off` | `kf` | `kf` | display smoothing for multinode tracks (`ewma` is the rollback) |

`shadow` computes and counts a stage's verdicts (exposed in
`/api/test/solver-stats`) without letting them bind — the standard soak step
before flipping `active`. Production currently sets none of the mode flags
(all `off`).

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
