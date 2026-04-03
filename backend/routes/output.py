"""Public output APIs — solver aircraft and ground truth.

These endpoints expose the server's current aircraft state and ground-truth
data in a clean, documented JSON format suitable for external consumers.

Endpoints
---------
GET /api/v1/solver/aircraft              – All solver-positioned aircraft
GET /api/v1/solver/aircraft?real_only=1  – Real (non-synthetic) nodes only
GET /api/v1/ground-truth/aircraft        – Simulation ground truth trails
GET /api/v1/ground-truth/real            – Real ground truth (OpenSky ADS-B positions)
GET /api/v1/docs                         – HTML documentation
"""

import time

import orjson
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, Response

from core import state

router = APIRouter()


def _real_node_ids() -> set:
    """Return the set of node IDs that are NOT synthetic."""
    return {
        nid for nid, info in state.connected_nodes.items()
        if not info.get("is_synthetic", True)
    }


def _format_aircraft(ac: dict) -> dict:
    return {
        "hex": ac.get("hex"),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "alt_baro": ac.get("alt_baro"),
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "position_source": ac.get("position_source"),
        "multinode": ac.get("multinode", False),
        "n_nodes": ac.get("n_nodes", 1),
        "contributing_node_ids": ac.get("contributing_node_ids", []),
        "flight": ac.get("flight"),
        "node_id": ac.get("node_id"),
        "target_class": ac.get("target_class"),
        "delay_us": ac.get("delay_us"),
        "doppler_hz": ac.get("doppler_hz"),
        "rms_delay": ac.get("rms_delay"),
        "rms_doppler": ac.get("rms_doppler"),
        "recent_positions": ac.get("recent_positions", []),
    }


@router.get("/api/v1/solver/aircraft")
async def solver_aircraft(real_only: bool = Query(False, description="Return only aircraft detected by real (non-synthetic) nodes")):
    """Return all aircraft the solver is currently tracking.

    Each entry includes the solver's position estimate, position source,
    velocity, and detection quality metrics.

    **position_source** values:
    - ``solver_adsb_seed`` — LM solver converged using ADS-B position as initial guess
    - ``solver_single_node`` — LM solver converged without ADS-B
    - ``single_node_ellipse_arc`` — solver did not converge; position is midpoint of bistatic delay ellipse arc
    - ``multinode_solve`` — position solved from ≥2 node detections (highest accuracy)
    - ``adsb_fallback`` — solver failed; raw ADS-B coordinates used temporarily

    **Query params:**
    - ``real_only=true`` — filter to aircraft detected by real hardware nodes only (excludes simulated fleet)
    """
    data = state.latest_aircraft_json
    now = time.time()

    if real_only:
        real_ids = _real_node_ids()
        aircraft_list = [
            ac for ac in data.get("aircraft", [])
            if ac.get("node_id") in real_ids
            or (ac.get("multinode") and any(
                nid in real_ids for nid in ac.get("contributing_node_ids", [])
            ))
        ]
    else:
        aircraft_list = data.get("aircraft", [])

    aircraft_out = [_format_aircraft(ac) for ac in aircraft_list]

    body = orjson.dumps({
        "timestamp": now,
        "count": len(aircraft_out),
        "real_only": real_only,
        "aircraft": aircraft_out,
    }, option=orjson.OPT_SERIALIZE_NUMPY)
    return Response(content=body, media_type="application/json")


@router.get("/api/v1/ground-truth/aircraft")
async def ground_truth_aircraft():
    """Return simulation ground truth positions and metadata for all aircraft.

    Ground truth is pushed by the fleet orchestrator (simulation) every 2 s via
    ``POST /api/test/ground-truth/push``.  Each entry has a 30-point trail
    and metadata (object type, anomaly flag, speed, heading).

    This endpoint is only meaningful when the simulation fleet is running.
    For real ground truth from live hardware nodes, use ``/api/v1/ground-truth/real``.
    """
    now = time.time()
    aircraft_out = []
    for hex_code, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        meta = state.ground_truth_meta.get(hex_code, {})
        aircraft_out.append({
            "hex": hex_code,
            "object_type": meta.get("object_type"),
            "is_anomalous": meta.get("is_anomalous", False),
            "speed_ms": meta.get("speed_ms"),
            "heading": meta.get("heading"),
            "trail": list(trail)[-30:],
        })

    body = orjson.dumps({
        "timestamp": now,
        "count": len(aircraft_out),
        "source": "simulation",
        "aircraft": aircraft_out,
    }, option=orjson.OPT_SERIALIZE_NUMPY)
    return Response(content=body, media_type="application/json")


@router.get("/api/v1/ground-truth/real")
async def ground_truth_real():
    """Return real ground truth aircraft positions from OpenSky Network.

    The server polls OpenSky every 2 minutes and caches positions for
    aircraft within ±1° of all real hardware nodes.  This provides an
    independent position source to validate solver output against known
    ADS-B transponder data.

    Each entry contains:
    - ``hex`` — ICAO 24-bit address
    - ``lat``, ``lon`` — last known position (degrees)
    - ``alt_m`` — barometric altitude (metres)
    - ``velocity`` — ground speed (m/s)
    - ``heading`` — track angle (degrees)
    - ``age_s`` — seconds since last OpenSky update

    **Usage:** Compare solver output from ``/api/v1/solver/aircraft?real_only=true``
    against these positions to measure geolocation accuracy on real traffic.
    """
    now = time.time()
    aircraft_out = []
    for icao, entry in list(state.external_adsb_cache.items()):
        aircraft_out.append({
            "hex": icao,
            "lat": entry.get("lat"),
            "lon": entry.get("lon"),
            "alt_m": entry.get("alt_m"),
            "velocity": entry.get("velocity"),
            "heading": entry.get("heading"),
        })

    body = orjson.dumps({
        "timestamp": now,
        "count": len(aircraft_out),
        "source": "opensky_network",
        "aircraft": aircraft_out,
    }, option=orjson.OPT_SERIALIZE_NUMPY)
    return Response(content=body, media_type="application/json")


# ── HTML docs ─────────────────────────────────────────────────────────────────

_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RETINA API v1 — Documentation</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;line-height:1.6}
  .wrap{max-width:900px;margin:0 auto;padding:40px 24px}
  h1{font-size:2rem;font-weight:700;color:#7dd3fc;margin-bottom:4px}
  h1 span{color:#94a3b8;font-size:1rem;font-weight:400;margin-left:10px}
  .subtitle{color:#64748b;margin-bottom:40px;font-size:.9rem}
  h2{font-size:1.25rem;font-weight:600;color:#e2e8f0;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid #1e293b}
  h3{font-size:1rem;font-weight:600;color:#7dd3fc;margin:20px 0 8px}
  .endpoint{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:20px 24px;margin-bottom:20px}
  .method{display:inline-block;background:#166534;color:#86efac;font-size:.75rem;font-weight:700;padding:2px 8px;border-radius:4px;font-family:monospace;margin-right:10px}
  .path{font-family:'SF Mono',Consolas,monospace;font-size:1rem;color:#e2e8f0}
  .desc{color:#94a3b8;margin:10px 0 14px;font-size:.9rem}
  .param{background:#0f172a;border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.85rem}
  .param-name{font-family:monospace;color:#fbbf24}
  .param-type{color:#64748b;margin:0 6px}
  .param-desc{color:#94a3b8}
  .field-table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:8px}
  .field-table th{text-align:left;padding:6px 10px;background:#0f172a;color:#64748b;font-weight:600}
  .field-table td{padding:6px 10px;border-bottom:1px solid #1e293b;color:#cbd5e1}
  .field-table td:first-child{font-family:monospace;color:#fbbf24}
  .example{background:#020617;border:1px solid #1e293b;border-radius:6px;padding:14px 16px;margin-top:12px;overflow-x:auto}
  .example pre{font-family:'SF Mono',Consolas,monospace;font-size:.78rem;color:#86efac;white-space:pre}
  .tag{display:inline-block;font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:8px;font-weight:600;vertical-align:middle}
  .tag-sim{background:#1e3a5f;color:#7dd3fc}
  .tag-real{background:#14532d;color:#86efac}
  a{color:#7dd3fc;text-decoration:none}
  a:hover{text-decoration:underline}
  .note{background:#1e293b;border-left:3px solid #7dd3fc;padding:10px 14px;border-radius:0 6px 6px 0;font-size:.85rem;color:#94a3b8;margin-top:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>RETINA API <span>v1</span></h1>
  <p class="subtitle">Passive Radar Solver Output &amp; Ground Truth — <strong>api.retina.fm</strong></p>

  <h2>Overview</h2>
  <p style="color:#94a3b8;font-size:.9rem;margin-bottom:12px">
    All endpoints are unauthenticated HTTP GET (read-only).
    Responses are JSON (<code style="color:#fbbf24">Content-Type: application/json</code>).
    Base URL: <code style="color:#fbbf24">https://api.retina.fm</code>
  </p>

  <table class="field-table" style="margin-bottom:0">
    <tr><th>Endpoint</th><th>Description</th><th>Source</th></tr>
    <tr><td><a href="/api/v1/solver/aircraft">/api/v1/solver/aircraft</a></td><td>All solver-positioned aircraft</td><td>All nodes</td></tr>
    <tr><td><a href="/api/v1/solver/aircraft?real_only=true">/api/v1/solver/aircraft?real_only=true</a></td><td>Real-hardware nodes only</td><td>Real nodes</td></tr>
    <tr><td><a href="/api/v1/ground-truth/aircraft">/api/v1/ground-truth/aircraft</a></td><td>Simulation ground truth (physics layer)</td><td>Simulation</td></tr>
    <tr><td><a href="/api/v1/ground-truth/real">/api/v1/ground-truth/real</a></td><td>Real ground truth (OpenSky ADS-B)</td><td>OpenSky Network</td></tr>
  </table>

  <h2>Solver Aircraft <span class="tag tag-sim">SIM</span><span class="tag tag-real">REAL</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/solver/aircraft</span>
    <p class="desc">Returns all aircraft currently tracked and positioned by the passive radar solver. Each entry includes the solved lat/lon, position quality metadata, and ADS-B enrichment when available.</p>

    <h3>Query Parameters</h3>
    <div class="param">
      <span class="param-name">real_only</span><span class="param-type">bool, default false</span>
      <span class="param-desc">— When true, returns only aircraft detected by real hardware nodes (excludes the simulated fleet). Use this to query map.retina.fm live data.</span>
    </div>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>ICAO hex or synthetic ID (e.g. <code>A1B2C3</code> or <code>pr1a2b</code>)</td></tr>
      <tr><td>lat / lon</td><td>float</td><td>Solver-derived position (degrees)</td></tr>
      <tr><td>alt_baro</td><td>int</td><td>Altitude (feet, from ADS-B when available)</td></tr>
      <tr><td>gs</td><td>float</td><td>Ground speed (knots)</td></tr>
      <tr><td>track</td><td>float</td><td>Track angle (degrees, 0 = North)</td></tr>
      <tr><td>position_source</td><td>string</td><td>How position was derived — see values below</td></tr>
      <tr><td>multinode</td><td>bool</td><td>True if solved from ≥2 nodes (highest accuracy)</td></tr>
      <tr><td>n_nodes</td><td>int</td><td>Number of contributing nodes</td></tr>
      <tr><td>flight</td><td>string</td><td>Callsign / flight number</td></tr>
      <tr><td>node_id</td><td>string</td><td>Detecting node ID</td></tr>
      <tr><td>target_class</td><td>string</td><td><code>aircraft</code>, <code>drone</code>, or null</td></tr>
      <tr><td>delay_us</td><td>float</td><td>Bistatic delay of latest detection (µs)</td></tr>
      <tr><td>doppler_hz</td><td>float</td><td>Doppler shift of latest detection (Hz)</td></tr>
      <tr><td>rms_delay</td><td>float</td><td>RMS delay residual from LM solver (µs) — lower is better</td></tr>
      <tr><td>rms_doppler</td><td>float</td><td>RMS Doppler residual from LM solver (Hz)</td></tr>
      <tr><td>recent_positions</td><td>array</td><td>Last 60 [lat, lon, alt_ft, ts] positions</td></tr>
    </table>

    <h3>position_source Values</h3>
    <table class="field-table">
      <tr><th>Value</th><th>Meaning</th></tr>
      <tr><td>solver_adsb_seed</td><td>LM solver converged; ADS-B used as initial guess. Best accuracy for ADS-B-tagged targets.</td></tr>
      <tr><td>solver_single_node</td><td>LM solver converged without ADS-B seed.</td></tr>
      <tr><td>single_node_ellipse_arc</td><td>Solver did not converge. Position is midpoint of bistatic delay ellipse.</td></tr>
      <tr><td>multinode_solve</td><td>Solved from ≥2 node detections. Independent of ADS-B. Highest geometric accuracy.</td></tr>
      <tr><td>adsb_fallback</td><td>Solver failed on this cycle. Position is ADS-B coordinates (temporary).</td></tr>
    </table>

    <div class="example">
<pre>GET https://api.retina.fm/api/v1/solver/aircraft?real_only=true

{
  "timestamp": 1743714161.5,
  "count": 3,
  "real_only": true,
  "aircraft": [
    {
      "hex": "a3f8c1",
      "lat": 33.7753,
      "lon": -84.3963,
      "alt_baro": 34000,
      "gs": 421.5,
      "track": 187.3,
      "position_source": "solver_adsb_seed",
      "multinode": false,
      "n_nodes": 1,
      "flight": "DAL1234",
      "node_id": "radar3-retnode",
      "target_class": "aircraft",
      "delay_us": 14.22,
      "doppler_hz": -83.4,
      "rms_delay": 0.31,
      "rms_doppler": 1.07,
      "recent_positions": [[33.78,-84.40,34000,1743714120], ...]
    }
  ]
}</pre>
    </div>
  </div>

  <h2>Simulation Ground Truth <span class="tag tag-sim">SIM</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/ground-truth/aircraft</span>
    <p class="desc">Returns the full ground truth state of all simulated physics objects (aircraft, drones, dark targets, anomalous objects). Updated every 2 s by the fleet orchestrator. Use this to validate solver accuracy against known positions.</p>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>Synthetic ICAO identifier</td></tr>
      <tr><td>object_type</td><td>string</td><td><code>aircraft</code>, <code>drone</code>, or <code>dark</code></td></tr>
      <tr><td>is_anomalous</td><td>bool</td><td>True if this object has been flagged as anomalous (high speed, instant accel, etc.)</td></tr>
      <tr><td>speed_ms</td><td>float</td><td>True speed (m/s)</td></tr>
      <tr><td>heading</td><td>float</td><td>True heading (degrees)</td></tr>
      <tr><td>trail</td><td>array</td><td>Last 30 true positions: [lat, lon, alt_m, t_epoch]</td></tr>
    </table>

    <div class="note">
      Compare with <code>/api/v1/solver/aircraft</code> to measure detection rate and position error.
      The server's live validation endpoint <code>POST /api/test/validate</code> does this automatically and
      returns per-source accuracy statistics.
    </div>
  </div>

  <h2>Real Ground Truth (OpenSky) <span class="tag tag-real">REAL</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/ground-truth/real</span>
    <p class="desc">Returns real aircraft positions fetched from OpenSky Network every ~2 minutes. These provide independent ADS-B ground truth to validate the solver output from real hardware nodes.</p>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>ICAO 24-bit address</td></tr>
      <tr><td>lat / lon</td><td>float</td><td>ADS-B position (degrees)</td></tr>
      <tr><td>alt_m</td><td>float</td><td>Barometric altitude (metres)</td></tr>
      <tr><td>velocity</td><td>float</td><td>Ground speed (m/s)</td></tr>
      <tr><td>heading</td><td>float</td><td>Track angle (degrees)</td></tr>
    </table>

    <div class="note">
      Cross-reference with <code>/api/v1/solver/aircraft?real_only=true</code>
      to compute haversine error between the solver's radar-derived positions and the OpenSky ADS-B truth.
    </div>
  </div>

  <h2>Validation Endpoint</h2>

  <div class="endpoint">
    <span class="method">POST</span><span class="path">/api/test/validate</span>
    <p class="desc">Submit a ground truth list; the server matches each truth object to the nearest solver aircraft and returns position error statistics broken down by <code>position_source</code>.</p>
    <div class="example">
<pre>POST https://api.retina.fm/api/test/validate
Content-Type: application/json

{
  "ground_truth": [
    {"id": "abc123", "lat": 33.77, "lon": -84.40, "alt_km": 10.5,
     "has_adsb": true, "is_anomalous": false}
  ]
}

Response:
{
  "validation": {"truth_aircraft": 1, "matched": 1, "detection_rate_pct": 100.0, ...},
  "accuracy": {"avg_position_error_km": 0.42, "p95_position_error_km": 1.1, ...},
  "by_source": {"solver_adsb_seed": {"count": 1, "mean_km": 0.42, ...}}
}</pre>
    </div>
  </div>

  <h2>Live Accuracy Statistics</h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/radar/accuracy</span>
    <p class="desc">Returns rolling solver-vs-ADS-B accuracy statistics from the last 5000 position fixes. Updated every 30 s.</p>
    <div class="example">
<pre>{
  "n_samples": 2841,
  "mean_km": 0.38,
  "median_km": 0.22,
  "p95_km": 1.41,
  "by_source": {
    "solver_adsb_seed": {"n_samples": 2100, "mean_km": 0.19, ...},
    "solver_single_node": {"n_samples": 741, "mean_km": 0.72, ...}
  }
}</pre>
    </div>
  </div>

  <p style="margin-top:48px;color:#475569;font-size:.8rem;text-align:center">
    RETINA Passive Radar Network — api.retina.fm
  </p>
</div>
</body>
</html>"""


@router.get("/api/v1/docs", response_class=HTMLResponse, include_in_schema=False)
async def api_docs():
    """Human-readable HTML documentation for all v1 public API endpoints."""
    return HTMLResponse(content=_DOCS_HTML)
