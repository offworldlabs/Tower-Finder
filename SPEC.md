# RETINA — Phase 1 Product Specification

## Overview

RETINA is a passive-radar network that detects aircraft by analysing
reflections of existing FM/VHF/UHF broadcast transmissions. Each remote node
captures delay-Doppler detections and streams them to a central server, which
tracks, geolocates, and displays aircraft on a live map.

---

## Core Principles

1. **Radar-first detection.** An aircraft appears on the map **only** if at
   least one radar node has detected it. Pure ADS-B targets are excluded.
2. **ADS-B as solver seed, not position source.** When ADS-B data exists for
   a radar-detected aircraft, it provides the initial guess for the
   Levenberg-Marquardt (LM) solver — it never bypasses the solver entirely.
3. **Real-time movement.** Every aircraft on the map moves smoothly in real
   time via dead-reckoning between solver updates.
4. **Ground truth overlay.** In simulation/test mode, the actual simulated
   objects move live on the map as a debug overlay.

---

## Detection → Display Pipeline

```
Node (Raspberry Pi)                       Central Server
┌─────────────┐  TCP :3012   ┌───────────────────────────────────────────┐
│ RX antenna  │ ────────────►│ 1. TCP handler: extract ADS-B, enqueue   │
│ delay/Dopp  │              │ 2. Frame processor (8 threads):           │
│ + ADS-B tag │              │    a. retina-tracker (Kalman + GNN)       │
│             │              │    b. _run_geolocation:                   │
│             │              │       • ADS-B available → seed LM solver  │
│             │              │       • No ADS-B       → blind LM solver  │
│             │              │    c. GeolocatedTrack → state             │
│             │              │ 3. Aircraft flush (1 Hz):                 │
│             │              │    dead-reckon all tracks → aircraft.json │
│             │              │ 4. WebSocket broadcast to map clients     │
└─────────────┘              └───────────────────────────────────────────┘
```

### Step-by-step

| # | Stage | Input | Output | Frequency |
|---|-------|-------|--------|-----------|
| 1 | TCP ingest | Raw frame (delay[], doppler[], snr[], adsb[]) | ADS-B stored in `state.adsb_aircraft`; frame queued | Per node frame |
| 2 | Tracker | Detection frame | Confirmed `Track` objects with optional `adsb_hex` | Per frame per node |
| 3 | Geolocation | Track event + ADS-B seed (if available) | `GeolocatedTrack` (lat, lon, vel, alt) | Per track event |
| 4 | Aircraft flush | All `GeolocatedTrack` objects | Dead-reckoned `aircraft.json` | ~1 Hz |
| 5 | Broadcast | `aircraft.json` bytes | WebSocket push to all connected map clients | ~1 Hz |

---

## Position Source Hierarchy

Each aircraft in `aircraft.json` carries a `position_source` field:

| Source | Description | Dead-reckoned? |
|--------|-------------|----------------|
| `solver_adsb_seed` | LM solver seeded with ADS-B initial guess (best) | Yes |
| `solver_single_node` | LM solver without ADS-B (blind initial guess) | Yes |
| `multinode_solve` | Multi-node LM solver (2+ nodes, highest confidence) | Yes |
| `single_node_ellipse_arc` | Ambiguity arc midpoint (no solver convergence) | No (geometric) |

**ADS-B-only aircraft (`position_source` absent) are excluded from the map.**

---

## Dead-Reckoning

Between solver updates (which arrive per-frame, typically every 5–40 s
depending on fleet configuration), the displayed position is extrapolated:

```
lat_display = lat_fix + (vel_north / 111_320) × elapsed_s
lon_display = lon_fix + (vel_east / (111_320 × cos(lat))) × elapsed_s
```

- `vel_east`, `vel_north` stored on the `GeolocatedTrack` (m/s), set from
  ADS-B gs/track or solver velocity.
- Extrapolation capped at **60 s** to avoid runaway drift.
- Arc-midpoint tracks (`single_node_ellipse_arc`) are NOT dead-reckoned.

---

## Ground Truth (Simulation / Test Mode)

The fleet simulator pushes ground truth via `POST /api/test/ground-truth/push`
on every simulated step. Each push includes the true lat/lon/alt of every
simulated object.

- Stored in `state.ground_truth_trails` (per-hex deques, max 500 pts).
- Metadata (speed, heading, object_type, is_anomalous) in `state.ground_truth_meta`.
- Refreshed in the aircraft.json payload every 5 s (`_GT_REFRESH_S`).
- Frontend map overlay draws ground truth trails + current position.
- Ground truth objects move live because the simulation pushes new positions
  at its frame rate.

---

## Key Performance Targets

| Metric | Target | Current |
|--------|--------|---------|
| Frame processing latency | < 15 ms/frame | ~8 ms |
| Frame queue utilization | < 5% | ~0.1% |
| Frames dropped | 0 | 0 |
| Aircraft.json refresh | ~1 Hz | ~1 Hz |
| Map position update (WebSocket) | ~1 Hz | ~1 Hz |
| Nodes supported | 1000 | 915 |
| Aircraft on map | 50–100 (fleet dependent) | ~250 |

---

## Architecture Constraints

- **Single uvicorn worker** — all state is in-memory. Never `--workers N > 1`.
- **8 frame worker threads** — separate from registration, analytics, admin executors.
- **Pre-serialized bytes** — high-traffic endpoints serve cached `bytes` objects,
  never compute inside request handlers.
- **Per-node pipelines** — each TCP node gets its own tracker + solver pipeline,
  lazily created on first frame.

---

## Endpoints (Map-Relevant)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/radar/data/aircraft.json` | Dead-reckoned aircraft positions (tar1090-compatible) |
| `WS` | `/ws/aircraft` | Live push of aircraft.json (1 Hz, 86400 s timeout) |
| `GET` | `/api/test/dashboard` | Health: nodes, queue, drops, counts |
| `POST` | `/api/test/ground-truth/push` | Simulation pushes true object positions |

---

## What This Spec Does NOT Cover (Future Phases)

- Drone detection (small/slow target parametrisation)
- Meteorite / ionization trail detection
- Physical Raspberry Pi node onboarding
- Public dashboard (map.retina.fm production)
- Solver parameter tuning with real-world data
- Dropshipping / hardware fulfillment
