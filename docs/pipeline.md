# Detection Pipeline

The core processing chain that takes raw detection frames from nodes and produces
positioned aircraft in the output feed.

---

## Overview

```
TCP frame (node)
    │
    ├─ ADS-B fast-path → state.adsb_aircraft (immediate, no queuing)
    │
    └─ frame queue (asyncio, capacity 10 000)
           │
           └─ FRAME_WORKERS thread pool
                  │
                  ├─ PassiveRadarPipeline.process_frame()
                  │       ├─ Tracker.process_frame()  (Kalman + GNN)
                  │       └─ _run_geolocation()        (LM solver)
                  │
                  ├─ node_associator.submit_frame()    (cross-node correlation)
                  │
                  └─ state.node_analytics.record_detection_frame()

Aircraft flush task (1 Hz)
    └─ build_combined_aircraft_json()
           ├─ single-node geolocated tracks
           ├─ multi-node solved tracks
           ├─ ADS-B aircraft
           └─ detection arcs (promoted tracks, no ADS-B)
                  │
                  └─ broadcast to WebSocket clients + write aircraft.json
```

---

## 1. TCP Frame Ingestion

Each node maintains a persistent TCP connection to the server on port 3012.
Frames arrive as newline-delimited JSON and go through a handshake sequence:

```
HELLO  →  CONFIG (node sends its geometry/freq config)
       ←  CONFIG_ACK (server confirms, assigns node_id)

DETECTION  →  (streams indefinitely, one frame per interval)
HEARTBEAT  →  (every 60 s when no detections)
```

On receipt the server does two things in parallel:

1. **ADS-B fast-path**: if the frame contains an `adsb` array, every entry is
   written directly into `state.adsb_aircraft` before the frame touches any
   queue. This keeps the ADS-B map current even if the frame queue is saturated.

2. **Frame queue**: the frame is enqueued for CPU-bound processing by the
   `FRAME_WORKERS` thread pool. `FRAME_WORKERS=8` on the production server.

---

## 2. Kalman Tracker (retina-tracker)

Each node has its own `PassiveRadarPipeline` instance, and inside it a private
`Tracker` instance running standard M-of-N Kalman + GNN association.

**State vector**: `[delay_µs, doppler_Hz]` — the two bistatic observables.

**GNN (Global Nearest Neighbour) association**:
- Predicts each track one step forward with its Kalman filter.
- Builds a cost matrix using Mahalanobis distance as the gating metric.
- Solves the assignment with `scipy.optimize.linear_sum_assignment` (Hungarian).
- SNR-weights costs so high-SNR detections are preferred.
- ADS-B-initialized tracks get a 20% cost bonus to keep them associated.

**Track states** (following blah2 architecture):

| State | Meaning |
|-------|---------|
| `TENTATIVE` | Newly created, not yet confirmed |
| `ASSOCIATED` | Has received at least one update |
| `ACTIVE` | Promoted via M-of-N; assigned a track ID |
| `COASTING` | Missed last frame; gate expands to recover |

**M-of-N promotion**: a track is promoted from `TENTATIVE` to `ACTIVE` once
`n_associated >= M_THRESHOLD` (default 4) within an N-frame window (default 6).
Only at this point does it receive a stable `track_id` and get emitted to the
event writer for geolocation.

**Tracklet stitching**: when a new detection falls within
`TRACKLET_MAX_DELAY_RESIDUAL` and `TRACKLET_MAX_DOPPLER_RESIDUAL` of a recently
deleted track, it's linked rather than spawning a new hypothesis.

---

## 3. Geolocation (retina-geolocator, LM solver)

After each tracker frame, `_run_geolocation()` asks the event writer which
tracks have new data, then runs the Levenberg–Marquardt solver on each.

**Inputs**: a window of the last 20 detections in `{timestamp, delay_µs, doppler_Hz, snr}` form.
At least 3 detections are required before the solver is called.

**Initial guess**: `select_initial_guess()` uses the bistatic geometry to
enumerate candidate positions along the ellipsoid and picks the one whose
predicted delay/doppler best fits the most recent measurements. On subsequent
frames the previous solution is used as the warm-start (temporal continuity).

**Solver output** (`solve_track()`):
- 6-element state vector: `[east_km, north_km, up_km, vel_east, vel_north, vel_up]`
  all in km / km·s⁻¹, ENU relative to the receiver.
- RMS residuals for delay and Doppler.
- `success: bool` — false if the LM solver diverged or hit iteration limits.

The ENU solution is converted to WGS-84 `(lat, lon, alt_m)` via
`Geometry.ecef2lla` for output.

**Target classification** (per-node):
- `aircraft` — default; also auto-assigned when speed > 60 m/s or alt > 600 m.
- `drone` — speed ≤ 60 m/s and alt ≤ 600 m when `target_profile = "auto"`.
- `drone` profile nodes constrain the initial altitude guess and solver bounds
  for better convergence on slow, low targets.

---

## 4. Multi-Node Solver

Detections from different nodes seeing the same target at the same time can be
combined for a tighter position fix. This is handled by `NodeAssociator` and
`MultiNodeSolver`, which run independently of the per-node pipelines.

**Association**: `node_associator.submit_frame()` looks for delay bins shared
across nodes within a time window. Candidates with N ≥ 2 nodes are forwarded
to the `solver_queue`.

**Solver**: runs in `_registration_executor` (single-threaded, prevents O(n²)
overlap registration from racing). A successful multi-node solution (`rms_delay < 1 µs`)
goes into `state.multinode_results` keyed by a canonical frame-time bucket.

Multi-node solved aircraft appear in the output with `type = "multinode_solve"`,
`n_nodes` set, and `contributing_node_ids` listed. No ambiguity arc is emitted
for these since the position is precisely known.

---

## 5. ADS-B Integration

Nodes can piggyback ADS-B data on their detection frames (embedded in the
`adsb` field). The simulation fleet does this for all aircraft that have
`has_adsb = True`.

When a geolocated track has an `adsb_hex` and there's a fresh ADS-B fix in
`state.adsb_aircraft`:
- The displayed position is dead-reckoned from the ADS-B fix rather than
  taken from the LM solver. This is more accurate and smoother.
- `position_source` is set to `"adsb_associated"`.
- No ambiguity arc is emitted — the position is already known precisely.

ADS-B entries expire after 60 s. After expiry the aircraft falls back to the
solver position.

---

## 6. Aircraft JSON Builder (`build_combined_aircraft_json`)

Runs every 1 s in the `_aircraft_flush_executor` thread. Priority order
for deduplicated hex codes:

1. **Single-node geolocated tracks** (per-node LM solver) — with or without ADS-B.
2. **Multi-node solved tracks** — takes precedence over single-node for the same hex.
3. **ADS-B-only aircraft** — aircraft seen in ADS-B but not yet tracked by radar.
4. **Ground truth** (simulation only) — injected from the fleet orchestrator,
   keyed separately and not displayed as aircraft markers.
5. **Pending detection arcs** — bistatic ellipse arcs for promoted (non-TENTATIVE)
   tracks that don't have a known ADS-B position.

The result is broadcast to all WebSocket clients and written to
`tar1090_data/aircraft.json`.
