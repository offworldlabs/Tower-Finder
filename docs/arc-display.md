# Bistatic Ambiguity Arcs

The arc lines visible on the map are bistatic ellipse sections — the set of
points in the detection beam that share the same TX→target→RX path length as
the measured detection.

---

## What an Arc Represents

In a passive radar system the receiver can measure the extra travel time
(bistatic delay) of a signal that bounced off a target, but that single
measurement only constrains the target to lie somewhere on an ellipsoid with
the TX and RX as foci. Intersected with the horizontal plane at a given
altitude, this ellipsoid becomes an ellipse. The arc shown on the map is the
portion of that ellipse that falls inside the node's detection beam.

The arc is useful visual information even when the target position is not yet
solved: it tells you exactly which region of the sky a node is detecting
something in, and it constrains where the aircraft could be when no GPS/ADS-B
is available.

---

## Arc Computation (`_build_single_node_arc`)

The arc is computed by binary-searching for each bearing within the detection
beam. For each of 36 equally-spaced bearing steps:

1. Compute the differential range `δr = ‖TX→point‖ + ‖point→RX‖ - ‖TX→RX‖`
   at a candidate range along that bearing.
2. Bisect between `r=0` and `r=max_range_km` until `δr` matches
   `delay_µs × c` to within ~1 m.
3. Convert the ENU result to lat/lon.

The output is a list of `[lat, lon]` points — typically 30–36 — that trace the
arc curve. Points that can't be reached within `max_range_km` (i.e. the
target is off the far edge of the ellipse) are omitted, so shorter arcs are
normal for detections with large delays.

---

## When Arcs Are Shown

Arcs are displayed in two contexts:

### 1. Geolocated single-node tracks

Each aircraft entry in the output that came from the single-node LM solver
(`position_source = "single_node_ellipse_arc"` or `"solver_single_node"`)
carries an `ambiguity_arc` field. This arc is built from the track's most
recent `latest_delay_us` value.

**Suppressed when**: `position_source = "adsb_associated"` — meaning the
node has correlated this track with a live ADS-B transponder. The position is
already known precisely; the arc would just be noise.

**Multi-node solved aircraft** (`type = "multinode_solve"`) never carry an arc.

### 2. Promoted pre-solve tracks (`detection_arcs` in the feed)

For tracks that have been confirmed by the M-of-N tracker (promoted out of
`TENTATIVE` state) but have not yet accumulated enough detections for the LM
solver to converge, an arc is published in the top-level `detection_arcs`
array of the aircraft feed.

These arcs show up as soon as 4 consecutive detections on the same target
have been associated — before any position fix — giving early visual feedback
that something is being tracked.

**Suppressed when** the track's ADS-B hex maps to a fresh entry in
`state.adsb_aircraft` (< 60 s old). In that case the position is already known
from ADS-B, so an arc is redundant.

---

## Arc Fade (Frontend)

The frontend accumulates arcs in a ring buffer keyed by `{node_id}_{ts}`.
Each arc carries a `ts` field (Unix timestamp). The `DetectionArcs` component
computes opacity as:

```
age = now - arc.ts          // seconds
opacity = max(0, 1 - age / FADE_WINDOW_S)
```

`FADE_WINDOW_S = 10`. Arcs older than 10 s are filtered out entirely before
rendering. This means the most recent detection is bright and older ones fade
out, so the display stays readable even at 1 Hz update rate.

---

## Decision Table

| Scenario | Arc shown? |
|----------|-----------|
| TENTATIVE track (< M detections) | No |
| Promoted track, no ADS-B | Yes — in `detection_arcs` |
| Promoted track, fresh ADS-B for this hex | No |
| Single-node geolocated, no ADS-B | Yes — on the aircraft entry |
| Single-node geolocated, ADS-B associated | No |
| Multi-node solved | No |
| ADS-B-only aircraft (not tracked by radar) | No |
