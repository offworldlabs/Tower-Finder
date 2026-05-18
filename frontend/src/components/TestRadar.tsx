// @ts-nocheck — gradual TS migration
import { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, TileLayer, CircleMarker, Marker, Popup } from "react-leaflet";
import {
  ARC_HOLD_MS,
  ARC_FADE_MS,
  ARC_TOTAL_LIFE_MS,
  DetectionArcs,
  makeAircraftIcon,
  nodeIcon,
} from "./map";

// Marietta GA — same coords as the production radar3-retnode site.
const MARIETTA = { lat: 33.9526, lon: -84.5499 };

// Compute (range_km, bearing_deg) from a fixed RX to a given lat/lon. Lets
// the simulator march the aircraft in absolute world coordinates while still
// feeding buildArc the polar form it expects.
function rangeBearingFromRx(rxLat, rxLon, lat, lon) {
  const cosLat = Math.cos((rxLat * Math.PI) / 180);
  const east_km = (lon - rxLon) * 111.32 * cosLat;
  const north_km = (lat - rxLat) * 111.32;
  return {
    rangeKm: Math.hypot(east_km, north_km),
    bearingDeg: (Math.atan2(east_km, north_km) * 180) / Math.PI,
  };
}

// Build a synthetic bistatic ellipse arc around the node at a given bearing
// and radius. 21 points across a 36° arc is enough to read as a smooth ellipse
// without being expensive. Returns [lat, lon] pairs.
function buildArc(centerLat, centerLon, radiusKm, bearingDeg, spreadDeg = 36, n = 21) {
  const out = [];
  const earthR = 6371; // km
  const half = spreadDeg / 2;
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    const b = (bearingDeg - half + t * spreadDeg) * (Math.PI / 180);
    const d = radiusKm / earthR;
    const lat1 = centerLat * (Math.PI / 180);
    const lon1 = centerLon * (Math.PI / 180);
    const lat2 = Math.asin(
      Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(b),
    );
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(b) * Math.sin(d) * Math.cos(lat1),
        Math.cos(d) - Math.sin(lat1) * Math.sin(lat2),
      );
    out.push([(lat2 * 180) / Math.PI, (lon2 * 180) / Math.PI]);
  }
  return out;
}

// Pick the midpoint of an arc — used as the aircraft's "approximate" position
// for single-node-arc-only tracks.
function arcMidpoint(arc) {
  return arc[Math.floor(arc.length / 2)];
}

function ArcCountWatcher({ countRef, setStats }) {
  // Polls DOM every 250ms to count rendered SVG arcs and placeholder circles.
  // Lets the diagnostics panel show what the DOM actually looks like, not what
  // we *think* the buffer contains.
  useEffect(() => {
    const id = setInterval(() => {
      const polys = document.querySelectorAll(".leaflet-overlay-pane path").length;
      const buffered = Object.keys(countRef.current).length;
      setStats({ polys, buffered });
    }, 250);
    return () => clearInterval(id);
  }, [countRef, setStats]);
  return null;
}

export default function TestRadar() {
  // Arc buffer mirrors the shape produced by useAircraftFeed.ingestAircraft —
  // DetectionArcs reads this exact structure in production.
  const arcsBufferRef = useRef({});
  const [refreshOn, setRefreshOn] = useState(true);
  // "bucketed" mimics ac.ambiguity_arc → per-hex-per-second bucket arcs.
  // "pending"  mimics data.detection_arcs → hex=null pending detections.
  // "gated"    mimics the production case where backend nulls the arc but
  //            still emits the aircraft with position_source =
  //            single_node_ellipse_arc — so only the approximate plane icon
  //            is rendered and no arc is in the buffer.  This is the state
  //            users complained looked like "no detection" on testmap.
  const [arcMode, setArcMode] = useState("bucketed");
  const [stats, setStats] = useState({ polys: 0, buffered: 0 });
  const [nodeStyle, setNodeStyle] = useState("divicon"); // "circle" | "divicon" | "both" — default to the production-real-node visual

  // Autopilot state: a single simulated aircraft flying straight at a fixed
  // ground speed and heading.  Lives in world coords (lat/lon) so motion
  // looks identical to a production track — radar polar coords are derived
  // for the arc builder.  Initial state: 25 km east of Marietta, heading
  // north-east at 480 kt, cruise altitude.
  const [posLat, setPosLat] = useState(MARIETTA.lat);
  const [posLon, setPosLon] = useState(MARIETTA.lon + 0.27); // ~25 km east at 34°N
  const [gs, setGs] = useState(480); // knots
  const [track, setTrack] = useState(45); // degrees
  const [flying, setFlying] = useState(true);

  // Advance lat/lon every animation frame.  Wrap the aircraft back to the
  // start when it strays past 80 km from the node so the demo keeps looping
  // without manual intervention.  The two functional setters compose lat/lon
  // updates from the latest values without stale-closure issues.
  useEffect(() => {
    if (!flying) return;
    let prev = performance.now();
    let rafId;
    const KNOTS_TO_MS = 0.514444;
    const DEG_PER_M = 1 / 111_320;
    const WRAP_RANGE_KM = 80;
    const START_LON_OFFSET = 0.27; // ~25 km east at 34°N
    const tick = (nowTs) => {
      const dt = Math.min((nowTs - prev) / 1000, 0.1);
      prev = nowTs;
      const gs_ms = gs * KNOTS_TO_MS;
      const rad = (track * Math.PI) / 180;
      setPosLat((lat) => {
        const next = lat + gs_ms * Math.cos(rad) * DEG_PER_M * dt;
        // Wrap check happens inside setPosLon so both axes reset together.
        return next;
      });
      setPosLon((lon) => {
        // Use the freshest lat for cos(lat) — slight staleness is fine since
        // dt is tens of ms; an exact value would need useRef.  Wrap on range.
        const cosLat = Math.cos((MARIETTA.lat * Math.PI) / 180) || 1;
        const nextLon = lon + (gs_ms * Math.sin(rad)) / (111_320 * cosLat) * dt;
        // Recompute range from the *next* lat/lon to decide wrap.  We can't
        // read posLat synchronously here, so approximate with the freshest
        // value seen in this scope.
        const { rangeKm } = rangeBearingFromRx(MARIETTA.lat, MARIETTA.lon, posLat, nextLon);
        if (rangeKm > WRAP_RANGE_KM) {
          setPosLat(MARIETTA.lat);
          return MARIETTA.lon + START_LON_OFFSET;
        }
        return nextLon;
      });
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [flying, gs, track, posLat]);

  // Geometry derived from the current aircraft position.  Memoised so
  // identity is stable when the position doesn't change.
  const { rangeKm: aircraftRangeKm, bearingDeg: aircraftBearing } = useMemo(
    () => rangeBearingFromRx(MARIETTA.lat, MARIETTA.lon, posLat, posLon),
    [posLat, posLon],
  );
  const arc = useMemo(
    () => buildArc(MARIETTA.lat, MARIETTA.lon, aircraftRangeKm, aircraftBearing),
    [aircraftBearing, aircraftRangeKm],
  );
  const aircraftPos = useMemo(() => arcMidpoint(arc), [arc]);

  const aircraft = useMemo(
    () => ({
      hex: "TESTAC1",
      flight: "TST123",
      lat: aircraftPos[0],
      lon: aircraftPos[1],
      alt_baro: 32000,
      track,
      gs,
      position_source: "single_node_ellipse_arc",
      node_id: "radar3-retnode",
      ambiguity_arc: arc,
      doppler_hz: 0,
    }),
    [arc, aircraftPos, track, gs],
  );

  const aircraftIcon = useMemo(
    () => makeAircraftIcon(aircraft, true, false),
    [aircraft],
  );

  // Refresh loop — when on, mirrors the production WS cadence by re-stamping
  // the buffer every 1s. When off, the existing entries age out naturally and
  // we can observe the hold + fade lifecycle.  "gated" mode skips writing
  // anything to the buffer entirely — that's the on-the-wire state when the
  // backend's RMS/speed gate fires (the aircraft is still emitted with
  // position_source = single_node_ellipse_arc but ambiguity_arc is null).
  useEffect(() => {
    if (!refreshOn) return;
    const writeOnce = () => {
      const now = Date.now();
      const buf = arcsBufferRef.current;
      if (arcMode === "bucketed") {
        const tsBucket = Math.floor(now / 1000);
        const key = `${aircraft.hex}-${aircraft.node_id}-${tsBucket}`;
        if (!(key in buf)) {
          buf[key] = {
            hex: aircraft.hex,
            node_id: aircraft.node_id,
            ambiguity_arc: arc,
            doppler_hz: aircraft.doppler_hz,
            target_class: undefined,
            ts: now,
          };
        }
      } else if (arcMode === "pending") {
        const mid = arcMidpoint(arc);
        const key = `det-${aircraft.node_id}-${Math.round(mid[0] * 100)}-${Math.round(mid[1] * 100)}`;
        buf[key] = {
          hex: null,
          node_id: aircraft.node_id,
          ambiguity_arc: arc,
          doppler_hz: aircraft.doppler_hz,
          target_class: undefined,
          ts: now,
        };
      }
      // "gated": no buffer write — emulates the production state where the
      // backend suppressed the arc.  Lets us verify the approximate-icon
      // rendering on its own.
      // Mirror hooks.ts: prune past full lifetime.
      for (const k of Object.keys(buf)) {
        if (now - buf[k].ts > ARC_TOTAL_LIFE_MS) delete buf[k];
      }
    };
    writeOnce();
    const id = setInterval(writeOnce, 1000);
    return () => clearInterval(id);
  }, [refreshOn, arcMode, arc, aircraft.hex, aircraft.node_id, aircraft.doppler_hz]);

  const clearBuffer = () => {
    arcsBufferRef.current = {};
  };

  return (
    <div style={{ display: "flex", height: "100vh", background: "#0f172a", color: "#f1f5f9" }}>
      <aside
        style={{
          width: 320,
          padding: "16px 18px",
          background: "#020617",
          borderRight: "1px solid #1e293b",
          overflowY: "auto",
          fontFamily: "ui-monospace, monospace",
          fontSize: 12,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 16, color: "#facc15" }}>TEST RADAR</h1>
        <p style={{ marginTop: 4, color: "#94a3b8" }}>
          1 node, 1 aircraft, 1 ellipse — no backend.
        </p>

        <h3 style={{ marginTop: 18, fontSize: 11, color: "#94a3b8", letterSpacing: 1 }}>NODE MARKER</h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {["circle", "divicon", "both"].map((opt) => (
            <label key={opt} style={{ cursor: "pointer" }}>
              <input
                type="radio"
                name="nodeStyle"
                value={opt}
                checked={nodeStyle === opt}
                onChange={() => setNodeStyle(opt)}
              />{" "}
              {opt === "circle" ? "CircleMarker (synth-* nodes)" : opt === "divicon" ? "divIcon (real radar nodes)" : "both side-by-side"}
            </label>
          ))}
        </div>

        <h3 style={{ marginTop: 18, fontSize: 11, color: "#94a3b8", letterSpacing: 1 }}>ARC TYPE</h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label style={{ cursor: "pointer" }}>
            <input type="radio" name="arcMode" checked={arcMode === "bucketed"} onChange={() => setArcMode("bucketed")} />{" "}
            bucketed (hex-node-ts)
          </label>
          <label style={{ cursor: "pointer" }}>
            <input type="radio" name="arcMode" checked={arcMode === "pending"} onChange={() => setArcMode("pending")} />{" "}
            pending (det-…, hex=null)
          </label>
          <label style={{ cursor: "pointer" }}>
            <input type="radio" name="arcMode" checked={arcMode === "gated"} onChange={() => setArcMode("gated")} />{" "}
            gated (icon only, arc=null)
          </label>
        </div>

        <h3 style={{ marginTop: 18, fontSize: 11, color: "#94a3b8", letterSpacing: 1 }}>FEED</h3>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            onClick={() => setRefreshOn((v) => !v)}
            style={{
              padding: "6px 10px",
              border: "1px solid #334155",
              background: refreshOn ? "#16a34a" : "#1e293b",
              color: "#f1f5f9",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 11,
            }}
          >
            {refreshOn ? "REFRESHING (1Hz)" : "PAUSED"}
          </button>
          <button
            onClick={clearBuffer}
            style={{
              padding: "6px 10px",
              border: "1px solid #334155",
              background: "#1e293b",
              color: "#f1f5f9",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 11,
            }}
          >
            CLEAR
          </button>
        </div>

        <h3 style={{ marginTop: 18, fontSize: 11, color: "#94a3b8", letterSpacing: 1 }}>AIRCRAFT</h3>
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          <button
            onClick={() => setFlying((v) => !v)}
            style={{
              padding: "6px 10px",
              border: "1px solid #334155",
              background: flying ? "#16a34a" : "#1e293b",
              color: "#f1f5f9",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 11,
            }}
          >
            {flying ? "FLYING" : "PAUSED"}
          </button>
          <button
            onClick={() => { setPosLat(MARIETTA.lat); setPosLon(MARIETTA.lon + 0.27); }}
            style={{
              padding: "6px 10px",
              border: "1px solid #334155",
              background: "#1e293b",
              color: "#f1f5f9",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 11,
            }}
          >
            RESET POS
          </button>
        </div>
        <label style={{ display: "block", marginBottom: 8 }}>
          Ground speed: {gs} kt
          <input
            type="range"
            min={0}
            max={600}
            value={gs}
            onChange={(e) => setGs(Number(e.target.value))}
            style={{ display: "block", width: "100%" }}
          />
        </label>
        <label style={{ display: "block" }}>
          Heading: {track.toFixed(0)}°
          <input
            type="range"
            min={0}
            max={360}
            value={track}
            onChange={(e) => setTrack(Number(e.target.value))}
            style={{ display: "block", width: "100%" }}
          />
        </label>

        <h3 style={{ marginTop: 18, fontSize: 11, color: "#94a3b8", letterSpacing: 1 }}>DIAGNOSTICS</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <tbody>
            <tr><td style={{ color: "#94a3b8" }}>range from node</td><td style={{ textAlign: "right" }}>{aircraftRangeKm.toFixed(1)} km</td></tr>
            <tr><td style={{ color: "#94a3b8" }}>bearing from node</td><td style={{ textAlign: "right" }}>{((aircraftBearing + 360) % 360).toFixed(0)}°</td></tr>
            <tr><td style={{ color: "#94a3b8" }}>buffer keys</td><td style={{ textAlign: "right" }}>{stats.buffered}</td></tr>
            <tr><td style={{ color: "#94a3b8" }}>SVG paths in overlay</td><td style={{ textAlign: "right" }}>{stats.polys}</td></tr>
            <tr><td style={{ color: "#94a3b8" }}>HOLD / FADE</td><td style={{ textAlign: "right" }}>{ARC_HOLD_MS / 1000}s / {ARC_FADE_MS / 1000}s</td></tr>
          </tbody>
        </table>

        <p style={{ marginTop: 22, color: "#475569", fontSize: 10, lineHeight: 1.4 }}>
          Aircraft autopilots from a point ~25 km east of the node at the chosen
          speed and heading; wraps back to start past 80 km. Pause to watch the
          {" "}{ARC_HOLD_MS / 1000}s hold + {ARC_FADE_MS / 1000}s fade.
        </p>
      </aside>

      <main style={{ flex: 1, position: "relative" }}>
        <MapContainer
          center={[MARIETTA.lat, MARIETTA.lon]}
          zoom={10}
          style={{ height: "100%", width: "100%" }}
        >
          <TileLayer
            url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
            attribution=""
          />

          {(nodeStyle === "circle" || nodeStyle === "both") && (
            <CircleMarker
              center={[MARIETTA.lat, MARIETTA.lon]}
              radius={5}
              pathOptions={{ color: "#facc15", fillColor: "#facc15", fillOpacity: 0.55, weight: 1.5 }}
            >
              <Popup>radar3-retnode (CircleMarker, prod style)</Popup>
            </CircleMarker>
          )}

          {(nodeStyle === "divicon" || nodeStyle === "both") && (
            <Marker
              position={[
                MARIETTA.lat,
                nodeStyle === "both" ? MARIETTA.lon + 0.04 : MARIETTA.lon,
              ]}
              icon={nodeIcon}
            >
              <Popup>radar3-retnode (divIcon, icons.ts)</Popup>
            </Marker>
          )}

          <Marker position={[aircraftPos[0], aircraftPos[1]]} icon={aircraftIcon} />

          <DetectionArcs
            arcsBufferRef={arcsBufferRef}
            selectedHex={null}
            onSelect={() => {}}
            onSelectNode={() => {}}
          />
          <ArcCountWatcher countRef={arcsBufferRef} setStats={setStats} />
        </MapContainer>
      </main>
    </div>
  );
}
