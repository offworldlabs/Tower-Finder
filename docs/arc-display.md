# Bistatic Ambiguity Arcs

The arc curves visible on the map are bistatic ellipse sections — the set of
points inside a node's detection area that share the same TX→target→RX path
length as the measured detection.

---

## What an Arc Represents

In a passive radar system the receiver measures the extra travel time
(bistatic delay) of a signal that bounced off a target. That single
measurement constrains the target to an ellipsoid with the TX and RX as foci —
nothing more. The arc shown on the map is the portion of that locus the node
could actually have detected: the full delay ellipse **clipped to the node's
detection area** (its beam wedge and range limits).

The arc deliberately spans the *entire* detection-area crossing, not a short
segment near a guessed position. Earlier revisions trimmed the arc to a
~25 km blip around the track's own position estimate, which made the arc read
as a position fix; that fake precision is exactly what the arc exists to
avoid. For the same reason, arc-only tracks render **no aircraft icon** — the
arc itself is the complete statement of what the node knows.

---

## Arc Computation (`_build_single_node_arc`, `backend/services/track_gates.py`)

For each bearing step across the node's beam wedge (36 steps → 37 points for
a directional node; 72 steps → a closed 73-point ring for an omnidirectional
one), the builder bisects along the bearing for the range whose bistatic
differential matches the measured delay.

The differential is evaluated in **3D** when the track has a usable altitude:

```
differential_3d = √(g² + (h−h_rx)²) + √(g_tx² + (h−h_tx)²) − √(baseline² + (h_tx−h_rx)²)
```

with `g`/`g_tx` the ground ranges to RX/TX, `h` the target altitude and
`h_rx`/`h_tx` the node altitudes. Solving on the ground plane alone put the
drawn locus a median 3.6 km outside the aircraft's true ground track (worse at
high altitude / short range). Tracks with no altitude (radar-only `pr*`
tracks) fall back to the 2D solve. Arcs are cached per
`(delay, 500 m-altitude-bucket)` — see `ARC_ALT_BUCKET_M`.

Range bounds per bearing: the differential-range limit when the node declares
`max_bistatic_range_km` (a single yes/no for the whole locus — every point on
it shares the measured differential), else the monostatic `max_range_km`
circle. A measured delay beyond the node's declared limit yields no arc at
all: the node could not have made that detection.

The output is a list of `[lat, lon]` points. The arc **midpoint** (the
boresight crossing of the locus) is used as the track's displayed lat/lon —
a canonical point on the ambiguity curve, not a position estimate.

---

## When Arcs Are Suppressed

| Condition | Why |
|-----------|-----|
| `position_source = "adsb_associated"` | Position already known precisely from ADS-B. |
| Multi-node solved (`type = "multinode_solve"`) | Position solved; the arc would be noise. |
| RMS gate firing (`rms_delay` above threshold) | The pipeline itself distrusts the measurement — mis-associated delays must not draw wrong-target arcs. (The *speed* gate still emits its arc: it distrusts the position association, not the measurement.) |
| Differential below `ARC_MIN_DIFFERENTIAL_KM` (3 km) | Near-baseline slivers clip to 1–5 km stubs that render as meaningless blobs. The track still emits, position only. |
| Delay beyond the node's `max_bistatic_range_km` | Physically inconsistent with the node's declared reach. |
| Promoted track with a fresh ADS-B entry (< 60 s) | Redundant — see pending arcs below. |

**Pending detection arcs** (`detection_arcs` in the feed) are still published
for tracks confirmed by the M-of-N tracker that have not yet accumulated
enough detections for the solver — early visual feedback before any fix.

---

## Frontend Rendering (`DetectionArcs.tsx`, `arcBuffer.ts`, `bistaticArc.ts`)

Arcs accumulate in an afterglow buffer keyed by
`hex + node_id + measured delay (quantized to 0.1 µs)`:

- Re-ingesting an **unchanged** measurement refreshes the one existing
  stroke's fade clock — a stationary target stays bright as a single stroke.
- A **changed** delay lays a new stroke at new geometry while the old one
  fades — that is the genuine afterglow trail.

Each stroke's geometry is rebuilt client-side from the **measured**
`delay_us` via `buildBistaticArc` (which mirrors the backend's 3D formula,
falling back to the backend-emitted arc when node geometry is missing).
Geometry is frozen at creation; only style refreshes. Opacity fades linearly
over `ARC_FADE_MS` (5 s), after which the buffer entry is pruned.

Arc-only tracks dead-reckon their reference position for at most
`ARC_DR_MAX_S` (10 s, vs 60 s for solved tracks) — the backend pins their
position to the arc midpoint, so a long glide walks the reference off the
measured locus.

Selecting an arc track (from the list panel or by clicking the arc) highlights
its arcs in amber, draws the detecting node's beam wedge, and centers the map
on the arc midpoint.
