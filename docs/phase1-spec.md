## The core rule

An aircraft shows up on the map only if at least one radar node has detected it. If a plane is broadcasting ADS-B but no node has a detection — it's not on our map. That's the whole point: we add value by detecting aircraft that ADS-B doesn't cover, or detecting them faster/more accurately.

---

## What needs to work

### 1. What appears on the map (and how)

| # | Scenario | Should happen | Status |
|---|----------|---------------|--------|
| 1.1 | Radar detection + ADS-B available | Show on map, position = solver output (ADS-B used as initial guess) | TODO — change needed |
| 1.2 | Radar detection, multiple nodes, no ADS-B | Show on map, position = multinode solver | Done |
| 1.3 | Radar detection, single node only, no ADS-B | Show on map as bistatic ellipse arc | Done |
| 1.4 | ADS-B only, no radar detection | **Not shown** | TODO — change needed |
| 1.5 | Each aircraft entry has a `position_source` field | Labeled as: `solver_single_node`, `solver_multinode`, `single_node_ellipse_arc` | Done |

Right now the `aircraft.json` builder includes ADS-B-only aircraft and shows their ADS-B position directly without running the solver. Both of these need to change.

### 2. Using ADS-B as a solver seed (single-node case)

When a node detects something and we also have ADS-B data for that target, we should:

1. Convert the ADS-B lat/lon/alt to ENU coordinates
2. Feed that as the starting point `[x, y, z, vx, vy, vz]` into the LM solver (velocity from ADS-B if available)
3. Let the solver run against the bistatic measurements
4. Display what the solver outputs — not the raw ADS-B position

This matters because it proves the radar is actually working. If we just display the ADS-B position we haven't validated anything.

File to change: `backend/retina_geolocator/lm_solver_track.py`

### 3. Live updates

| # | Requirement | Status |
|---|-------------|--------|
| 3.1 | Positions update in real time via WebSocket (~2 Hz) | Done |
| 3.2 | Radar refresh rate faster than standard ADS-B feeds | Done — 1s frame interval, aircraft.json rebuilt at ~2 Hz |
| 3.3 | Smooth movement between updates (client dead-reckoning at 60fps) | Done |
| 3.4 | Aircraft disappears after 60s of no radar detections | Done |

### 4. Simulation / ground truth debug layer

| # | Requirement | Status |
|---|-------------|--------|
| 4.1 | Sim generates aircraft flying realistic routes | Done |
| 4.2 | Synthetic nodes generate bistatic delay/Doppler detections | Done |
| 4.3 | Ground truth positions (actual sim positions) stream to map as a separate layer | Done |
| 4.4 | Ground truth visually distinct from radar-derived positions | Done |
| 4.5 | Ground truth shows all aircraft, even those not yet detected | Done |
| 4.6 | Ground truth layer toggleable in the UI | TODO — needs check |
| 4.7 | "Dark" aircraft (no ADS-B) visible in ground truth, show on radar map only if detected | Done — 15% of sim aircraft are dark |

### 5. Multinode geolocation

| # | Requirement | Status |
|---|-------------|--------|
| 5.1 | 2+ nodes on same target → multinode LM solver gives lat/lon | Done |
| 5.2 | Cross-node association works (time + geometry matching) | Done |
| 5.3 | Multinode result preferred over single-node when available | Done |
| 5.4 | Position confidence / solver residual surfaced to frontend | Partial — residuals exist but not shown in UI |

### 6. Validation against ADS-B

| # | Requirement | Status |
|---|-------------|--------|
| 6.1 | For aircraft with both radar + ADS-B: compute and track position error | Not built |
| 6.2 | API endpoint or dashboard showing mean/P95 error stats | Not built |
| 6.3 | Simulation validation: solver position vs ground truth | Partial — `/api/test/validate` exists but basic |

---

## Data flow

```
Simulation World
  │
  ├── Ground truth ──────────────────────→ Debug overlay on map
  │
  └── Detection frames (delay, Doppler)
        │
        ├── Single node + ADS-B ──→ LM solver (ADS-B as x0) ──→ Position on map
        ├── Single node, no ADS-B ──→ Ellipse arc on map
        └── Multi-node ──→ Association ──→ Multinode solver ──→ Position on map

ADS-B:
  - initial guess for single-node solver
  - validation reference (compare to solver output)
  - callsign/squawk enrichment
  - never shown on map by itself
```

---

## What's left to build

| Priority | Task | Notes |
|----------|------|-------|
| P0 | Filter `aircraft.json` — exclude ADS-B-only aircraft | `frame_processor.py → build_combined_aircraft_json` |
| P0 | Single-node solver: accept ADS-B position as initial guess | `lm_solver_track.py` |
| P0 | Display solver output instead of raw ADS-B position | follows from above |
| P1 | Accuracy tracking: haversine(radar_pos, adsb_pos) per aircraft per update | new module |
| P1 | Expose accuracy stats via API / dashboard panel | mean, median, P95 |
| P1 | Surface solver residual as confidence score in frontend | small UI change |
| P2 | Ground truth toggle in map UI | verify it exists, add if not |
| P2 | Expand `/api/test/validate` with proper error breakdown | |

---

## Already done

- TCP protocol + node fleet connectivity
- Simulation world with realistic aircraft generation
- Per-node radar pipelines (bistatic detection → tracking)
- Multi-node association + solver
- WebSocket live push to map (~2 Hz)
- Frontend: aircraft markers, trails, arcs, ground truth layer
- Performance: 1000-node stable at 0 frame drops
- Node trust scoring, analytics, coverage maps
