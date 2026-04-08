# Simulation Layer

The fleet simulator runs synthetic radar nodes and injects real or simulated
aircraft traffic to exercise the full server pipeline under load.

---

## Architecture

```
FleetOrchestrator
    │
    ├─ SimulationWorld          (shared physics state)
    │       ├─ SimulatedAircraft × N
    │       └─ SyntheticNodeView × M   (per-node observation geometry)
    │
    ├─ AdsbLolClient            (optional real ADS-B feed)
    │
    └─ NodeConnection × M       (async TCP, one per node)
            │
            └─ HELLO → CONFIG → DETECTION / HEARTBEAT
```

All nodes share a single `SimulationWorld`. Each node observes the world through
its own geometry (position, beam azimuth, beam width, max range). A node only
generates detections for aircraft that fall inside its detection cone.

---

## SimulationWorld

Each simulation tick (`world.step(dt)`) advances all aircraft positions using
a simple kinematic model:

- Aircraft follow waypoint routes drawn from `_US_WAYPOINTS` (major US airports).
- Speed is sampled at spawn time: 220–280 m/s for normal aircraft, 2–15 m/s for
  drones, 350–500 m/s for anomalous targets.
- A small random walk is applied to heading and speed each tick for realism.
- Aircraft with `has_adsb = True` (default fraction is configurable) carry an
  ICAO hex code and callsign that are included in detection frames.

### Detection generation (SyntheticNodeView)

For each aircraft inside the node's detection cone
`generate_detections_for_node()` computes:

**Bistatic delay** — the extra path length beyond the direct TX→RX baseline:
```
delay_µs = (‖TX→target‖ + ‖target→RX‖ - ‖TX→RX‖) / c
```

**Doppler shift**:
```
fd = (fc / c) × (v · r̂_TX + v · r̂_RX)
```
where `v` is the aircraft velocity vector and `r̂_TX`, `r̂_RX` are unit vectors
from the target toward the transmitter and receiver respectively.

**SNR** — gaussian with mean 20 dB, σ = 3 dB; drops by 0.05 dB per km of
bistatic range and by an additional 3 dB for targets outside the half-power
beam edge. Filtered at MIN_SNR = 7 dB before the tracker sees it.

---

## Node Generation (`generate_fleet`)

`generate_fleet(n_nodes, regions, seed)` places nodes around real broadcast
tower sites. For each metro area it queries `towers.retina.fm` (the Tower
Search API, cached in `backend/simulation/metro_tower_cache.json`) to get a
list of real transmitters. Nodes are placed 5–40 km from the tower at a random
azimuth with randomised beam orientation pointing roughly toward the tower.

A configurable fraction (`solo_fraction`, default 10%) are placed at isolated
rural sites to test single-node geometry without any inter-node overlap.

---

## `--metros` Flag

When `--metros atl,gvl` is passed to the orchestrator:

1. **Node filtering**: only nodes within a 2° lat/lon box around any listed
   metro are kept. The metro coordinates come from `_KNOWN_METROS`.
2. **ADS-B filtering**: the real ADS-B feed (`AdsbLolClient`) polls only the
   bounding boxes of the listed metros, reducing API calls and keeping the
   aircraft set geographically relevant.

All other nodes are generated as normal but dropped before connecting, so the
full node-generation + tower-API lookup still runs (and uses the cache for
most metros).

---

## Real ADS-B Feed (`AdsbLolClient`)

When `--mode adsb` is active, the orchestrator starts a background task that
polls `api.adsb.lol/v2/lat/.../lon/.../dist/...` every 10 s per metro area.

Aircraft returned by the API are merged with the simulated world:
- Aircraft matching an existing simulated hex are updated in-place (position,
  heading, speed).
- New hexes are added as real aircraft (`has_adsb = True`, `is_real = True`).
- Every aircraft in the feed — including dark/anomalous and those without ADS-B
  — is pushed to the simulation so it can be observed by in-range nodes.

Ground truth is pushed separately to the server (`POST /api/sim/ground-truth`
every 2 s) and used to evaluate solver accuracy, not for display.

---

## TCP Protocol (NodeConnection)

```jsonc
// 1. HELLO (client → server)
{"type": "HELLO", "version": "1.0", "node_id": "synth-atl-001"}

// 2. CONFIG (client → server)  
{"type": "CONFIG", "node_id": "...", "config": { ... node geometry ... }, "config_hash": "abc123"}

// 3. CONFIG_ACK (server → client)
{"type": "CONFIG_ACK", "status": "ok", "node_id": "synth-atl-001"}

// 4. DETECTION (client → server, every frame_interval seconds)
{
  "type": "DETECTION",
  "node_id": "synth-atl-001",
  "timestamp": 1711800000000,
  "delay": [45.2, 78.1],
  "doppler": [22.3, -18.7],
  "snr": [21.4, 15.2],
  "adsb": [{"hex": "a1b2c3", "lat": 33.9, "lon": -84.4, "alt_baro": 8000, "gs": 450, "track": 270}]
}

// 5. HEARTBEAT (client → server, when no detections)
{"type": "HEARTBEAT", "node_id": "...", "timestamp": ...}
```

The server sends no response after `CONFIG_ACK`. All subsequent messages are
unidirectional from node to server.

---

## Running the Fleet

```bash
# 1000-node fleet focused on Atlanta + Greenville, real ADS-B
python3 backend/simulation/orchestrator.py \
  --nodes 1000 --mode adsb \
  --validation-url https://localhost \
  --concurrency 80 --connect-retries 999 \
  --interval 40.0 --time-scale 4.0 \
  --min-aircraft 60 --max-aircraft 100 \
  --metros atl,gvl
```

Key parameters:

| Flag | Default | Effect |
|------|---------|--------|
| `--nodes` | 200 | Total synthetic nodes to generate |
| `--interval` | 5.0 s | Seconds between detection frames per node |
| `--time-scale` | 1.0 | Simulation speed multiplier |
| `--concurrency` | 20 | Max simultaneous TCP connects at startup |
| `--metros` | (all) | Restrict nodes and ADS-B to listed metros |
| `--min-aircraft` / `--max-aircraft` | 5 / 20 | Aircraft count range |
| `--beam-width-deg` | 0 (use config) | Yagi half-power beamwidth override (0 = per-node ~40°) |
| `--max-range-km` | 0 (use config) | Detection range override (0 = per-node ~45 km) |

`--time-scale 4.0` means simulation time runs 4× faster than wall clock, so
a 10-minute flight takes 2.5 minutes. Detection frame rate stays constant in
wall-clock time; aircraft just move faster between frames.
