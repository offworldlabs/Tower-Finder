# Simulation Layer

The fleet simulator (`libs/retina-simulation`) runs synthetic radar nodes and
injects real or simulated aircraft traffic to exercise the full server
pipeline. It powers the staging/testmap surfaces and CI.

---

## Architecture

```
FleetOrchestrator (retina_simulation/orchestrator.py)
    │
    ├─ SimulationWorld          (shared physics state, world.py)
    │       ├─ SimulatedAircraft × N
    │       └─ node views              (per-node observation geometry)
    │
    ├─ AdsbLolClient            (optional real ADS-B feed)
    │
    ├─ config poller            (GET /api/simulation/config every 5 s)
    │
    └─ NodeConnection × M       (async TCP, one per node)
            │
            └─ HELLO → CONFIG → DETECTION / HEARTBEAT
```

All nodes share a single `SimulationWorld`. Each node observes the world
through its own geometry: position, beam azimuth, a **fixed 42° Yagi
half-power beamwidth** (the whole fleet is the same antenna — width jitter was
removed deliberately), and range. The detection cone honours
`max_bistatic_range_km` — the *differential-range* limit — matching the rule
the server's association and arc layers gate on; the monostatic RX-radius rule
is only a fallback for nodes that declare no bistatic limit.

---

## Scene generation (`generator.py`)

The generator places nodes and picks illuminators for a metro scene:

- **`--metro gvl`** scopes the scene to one metro (Greenville, SC is the
  deployed default): sites inside the metro radius, aircraft on the metro's
  regional waypoint net, real ADS-B polling confined to its bounding box.
- **`--layout ring | dual | scatter`** — ring places nodes around the core;
  dual gives each site two illuminators; scatter (the staging default)
  distributes sites with per-site reach drawn from a distribution
  (60 km differential is what a *good* setup achieves, not an average one).
- **Illuminators are real FCC sites** — the GVL table carries the actual
  broadcast towers (WYFF Caesars Head etc.); base-metro nodes draw from the
  cached `metro_tower_cache.json` FCC lookups. Receiver sites are synthetic,
  land-checked (Natural Earth coastline/lake polygons) positions.

Detections carry bistatic delay and Doppler computed from full 3D ENU
geometry (altitude included — which is why the server's arc builder solves a
3D locus), plus SNR. **Known simplification:** SNR falls off at a one-way
10 dB/decade rather than the bistatic ~40 dB/decade, so synthetic SNR is
optimistic at range; SNR-derived gates need re-tuning on real captures.
Clutter/false alarms are injected with small-differential bias (they cluster
near the TX–RX baseline, which is why the server floors tiny differentials
before drawing arcs).

---

## Runtime simulation config

The orchestrator polls `GET /api/simulation/config` every 5 s. The Physics
tab (or `PUT /api/simulation/config`, admin-authed) can change at runtime:

| Key | Default | Meaning |
|-----|---------|---------|
| `frac_anomalous` | 0.0 | Fraction of spawns with anomalous behaviour |
| `frac_drone` | 0.0 | Drone fraction (off by default; enable for drone scenarios) |
| `frac_dark` | 0.15 | Non-ADS-B ("dark") fraction |
| `min_aircraft` / `max_aircraft` | *(unset)* | Steady-state aircraft bounds |

The aircraft-count keys are **deliberately absent from the defaults**: the
fleet applies them only when explicitly set, otherwise its deployment env
(`FLEET_MIN_AIRCRAFT` / `FLEET_MAX_AIRCRAFT`) wins. Defaults here once
carried a stale fleet scale that silently overrode the deployment the first
time anyone touched an unrelated knob. The runtime config is in-memory only —
a backend restart returns it to these defaults.

---

## Deployed fleet (staging)

Staging's fleet container regenerates `fleet_config.json` from `FLEET_*` env
on every boot (`deploy/fleet-entrypoint.sh`); scale changes are compose-file
changes, not code changes. Current staging scale (`docker-compose.staging.yml`):

| Env | Value | Meaning |
|-----|-------|---------|
| `FLEET_METRO` | `gvl` | Greenville, SC scene |
| `FLEET_LAYOUT` | `scatter` | Scatter site layout |
| `FLEET_NODES` | 30 | Total synthetic nodes |
| `FLEET_N_CLUSTER` | 20 | Scatter-cluster size |
| `FLEET_MIN_AIRCRAFT` / `FLEET_MAX_AIRCRAFT` | 20 / 40 | Steady-state traffic |
| `FLEET_MODE` | `adsb` | Merge the real ADS-B feed |
| `FLEET_INTERVAL` | 0.5 s | Frame interval per node |

Two real hardware nodes (`radar3*-retnode`, via the blah2 bridge near
Atlanta) connect alongside the synthetic fleet; their geometry lives in
`backend/config/blah2_nodes.json` (42° Yagis) with a runtime overlay copy
under `backend/data/runtime/`.

---

## Real ADS-B Feed (`AdsbLolClient`)

With `--mode adsb`, a background task polls `api.adsb.lol` for the metro's
bounding box every 10 s and merges results into the world: hexes matching a
simulated aircraft update it in place; new hexes join as real aircraft.
Ground truth (positions + per-object metadata) is pushed to the server
(`POST /api/sim/ground-truth`) for accuracy evaluation and the debug-truth
map layer.

---

## TCP Protocol (NodeConnection)

```jsonc
// 1. HELLO (client → server)
{"type": "HELLO", "version": "1.0", "node_id": "synth-GVL-SCAT-0001"}

// 2. CONFIG (client → server) — carries the full node geometry, including
//    beam_azimuth_deg / beam_width_deg / max_range_km / max_bistatic_range_km.
//    The server threads these into its per-node pipelines, arc builder and
//    inter-node association — dropping any of them regresses those layers
//    to defaults, so the CONFIG payload is the geometry contract.
{"type": "CONFIG", "node_id": "...", "config": { ... }, "config_hash": "abc123"}

// 3. CONFIG_ACK (server → client)
{"type": "CONFIG_ACK", "status": "ok", "node_id": "synth-GVL-SCAT-0001"}

// 4. DETECTION (client → server, every frame_interval seconds)
{
  "type": "DETECTION",
  "node_id": "synth-GVL-SCAT-0001",
  "timestamp": 1711800000000,
  "delay": [45.2, 78.1],
  "doppler": [22.3, -18.7],
  "snr": [21.4, 15.2],
  "adsb": [{"hex": "a1b2c3", "lat": 34.9, "lon": -82.4, "alt_baro": 8000, "gs": 450, "track": 270}]
}

// 5. HEARTBEAT (client → server, when no detections)
{"type": "HEARTBEAT", "node_id": "...", "timestamp": ...}
```

The server sends no response after `CONFIG_ACK`; subsequent messages are
unidirectional node → server.

---

## Running the Fleet Locally

```bash
# Greenville scene matching staging's shape, against a local backend
python3 -m retina_simulation.orchestrator \
  --nodes 30 --metro gvl --layout scatter --n-cluster 20 \
  --mode adsb --interval 0.5 \
  --min-aircraft 20 --max-aircraft 40 \
  --host localhost --port 3012 --seed 42
```

Key parameters:

| Flag | Default | Effect |
|------|---------|--------|
| `--nodes` | 200 | Total synthetic nodes to generate |
| `--metro` | (none) | Scope scene + traffic + ADS-B to one metro |
| `--layout` | `ring` | Site layout: `ring`, `dual`, `scatter` |
| `--n-cluster` | — | Scatter cluster size |
| `--interval` | 5.0 s | Seconds between detection frames per node |
| `--time-scale` | 1.0 | Simulation speed multiplier |
| `--concurrency` | 20 | Max simultaneous TCP connects at startup |
| `--min-aircraft` / `--max-aircraft` | 5 / 20 | Aircraft count range (runtime config can override) |
| `--seed` | — | Deterministic scene generation |

Beam width needs no flag: every antenna is the 42° Yagi. `--beam-width-deg`
exists only as an explicit override for experiments.
