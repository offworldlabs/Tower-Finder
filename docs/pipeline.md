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

Tracks from different nodes seeing the same target can be combined for a
tighter position fix. Association is **track-level**: each node's confirmed
tracklets (not raw detections) are submitted to the `InterNodeAssociator`
(`retina_analytics.association`), which pairs them across nodes on predicted
delay/Doppler consistency inside precomputed overlap zones, with one-to-one
χ² assignment. Candidate pairs go to the `solver_queue` for the LM multinode
solve (run in a process pool; `retina-geolocator.solve_multinode`, which also
reports per-solve east/north position covariance).

**Top-down claiming** (`ASSOC_CLAIM_MODE`, active on staging): after the
pairing round, established multi-node global tracks predict their expected
delay/Doppler at each node and *claim* matching tracklets directly — matched
tracklets are excluded from fresh pairing and emitted as anchored solver
inputs. This cuts track fragmentation (one stable key per object instead of a
new key per pairing) and recovers solves the bottom-up round would miss.

**Publication gates**, in order, for a converged solve:

- **rms residual / χ² trimming** — contaminated contributing nodes are
  trimmed and the solve retried before rejection.
- **beam/FOV gate** — each contributing node's solution bearing/range must be
  consistent with where that node can see. Under `FOV_MODE=active` this is
  the node's *learned* empirical FOV (section 7); n=2 uses the FOV alone,
  n≥3 uses it widen-only alongside the theoretical range rule.
- **displacement gate + n=2 confirmation** — an n=2 pairing publishes only
  after a constant-velocity fit over its observation window justifies it
  (5 unknowns vs 4 residuals means the residual gates cannot discriminate at
  n=2 on their own).
- **consensus** (`SOLVER_CONSENSUS_MODE`, active on staging) — a
  pairwise-intersection hypothesis stage refines n≥3 solves.

**Identity**: `multinode_key_decision` keys the published track, preferring
the anchor key of the claiming global track (mn-dark keys for targets with no
ADS-B seed) so repeated solves of one object share one identity. Display
positions are smoothed per key (`TRACK_SMOOTHER=kf|ewma|off`, default `kf` —
a CV Kalman filter; `ewma` is the env-only rollback).

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

---

## 7. Empirical Coverage & Learned FOV

Each node accumulates an empirical picture of where it can actually see
(`retina_analytics.empirical_coverage`, 72 × 5° bearing bins, persisted on the
`coverage_data` volume). Under `FOV_MODE` (`off | shadow | active`) that
learned field-of-view replaces the theoretical beam wedge as the association
grid and solver beam gate; the theoretical beam (declared aim, or the
broadside+90° fallback for nodes that never declared one) is only a *prior*
that binds until a bin has evidence. Broadening is fast (3 ADS-B-calibrated
positives open a bin; 10 extend range to P95 × 1.25); shrinking requires
negative evidence over time (≥3 recorded disappearances spanning ≥10 min,
newer than the bin's last positive) — absence of traffic never shrinks.

**What counts as a calibration positive is deliberately narrow.** The polygon
is used to judge solves and gate association, so it must be built only from
evidence independent of both, and only from *detections*:

- the position recorded is the aircraft's **reported ADS-B fix** (≤ 10 s old,
  `services/calibration.py`) — never a solver output;
- the node's track must have associated a real detection within
  `CAL_DETECTION_FRESH_S` (5 s) and have ≥ 3 detections — a track coasting on
  ADS-B enrichment is not evidence (`track_gates.py`);
- that newest detection must itself carry the track's own ADS-B tag — a track
  that identity-swaps onto an untagged target keeps a stale hex and would
  otherwise record the departed aircraft's position;
- published solves record **nothing** for their contributing nodes: that
  attribution rides on the very association the polygon judges, and under an
  active FOV gate it once formed a ghost → positive → wider-gate feedback
  loop.

`CALIBRATION_SCHEMA` (currently 5) versions what a stored positive *means*;
persisted state with an older schema is discarded and relearned at node
registration, on every deployment, with no operator action (the ledger of
past bumps is in `empirical_coverage.py`).
