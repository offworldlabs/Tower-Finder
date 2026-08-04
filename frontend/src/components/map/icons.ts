import L from "leaflet";
import { POSITION_SOURCE_ARC_ONLY } from "./constants";

// Top-down airplane SVG path (nose pointing up/north at 0°)
export const PLANE_PATH =
  "M16,2 C15.3,5.5 14.7,9 14.7,13 L3,20 L3,23 L14.7,19 L14.7,26 L11.5,28 L11.5,30.5 L16,29 L20.5,30.5 L20.5,28 L17.3,26 L17.3,19 L29,23 L29,20 L17.3,13 C17.3,9 16.7,5.5 16,2Z";

// Quadrotor drone SVG — simple X-frame with four motor circles
export const DRONE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
  style="display:block;filter:drop-shadow(0 1px 4px rgba(0,0,0,0.7));">
  <!-- arms -->
  <line x1="4" y1="4" x2="20" y2="20" stroke="#f59e0b" stroke-width="2.2" stroke-linecap="round"/>
  <line x1="20" y1="4" x2="4" y2="20" stroke="#f59e0b" stroke-width="2.2" stroke-linecap="round"/>
  <!-- motor circles -->
  <circle cx="4"  cy="4"  r="3" fill="none" stroke="#f59e0b" stroke-width="1.5"/>
  <circle cx="20" cy="4"  r="3" fill="none" stroke="#f59e0b" stroke-width="1.5"/>
  <circle cx="4"  cy="20" r="3" fill="none" stroke="#f59e0b" stroke-width="1.5"/>
  <circle cx="20" cy="20" r="3" fill="none" stroke="#f59e0b" stroke-width="1.5"/>
  <!-- center hub -->
  <circle cx="12" cy="12" r="2.5" fill="#f59e0b"/>
</svg>`;

// Altitude → colour bands (low warm → high cool), FlightRadar-style. Band edges
// are multiples of 5000 ft so they line up with the AircraftMarker altBand
// memo key — crossing a band re-renders the icon with the new colour.
export function altitudeColor(altFt) {
  if (altFt >= 40000) return "#a855f7";
  if (altFt >= 30000) return "#3b82f6";
  if (altFt >= 20000) return "#22c55e";
  if (altFt >= 10000) return "#eab308";
  if (altFt >= 5000) return "#f97316";
  return "#ef4444";
}

export const ALTITUDE_LEGEND = [
  ["#ef4444", "<5k"],
  ["#f97316", "5–10k"],
  ["#eab308", "10–20k"],
  ["#22c55e", "20–30k"],
  ["#3b82f6", "30–40k"],
  ["#a855f7", "40k+"],
];

export function getAircraftColor(ac, colorByAlt = false) {
  if (colorByAlt && typeof ac.alt_baro === "number") return altitudeColor(ac.alt_baro);
  if (ac.multinode || ac.position_source === "multinode_solve") return "#a78bfa";
  if (ac.position_source === "solver_adsb_seed") return "#2dd4bf";
  return "#38bdf8";
}

export function makeAircraftIcon(ac, showLabel, isSelected, colorByAlt = false) {
  const track = ac.track ?? 0;
  const color = getAircraftColor(ac, colorByAlt);
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  // Nullish: FL0 (on the ground) is a value, not an absence.
  const alt = ac.alt_baro != null ? `FL${Math.round(ac.alt_baro / 100)}` : "";

  // Arc-only tracks: lat/lon is the arc-midpoint estimate — the aircraft is
  // actually somewhere along the arc, not exactly at this point.  Render the
  // icon smaller, semi-transparent, with a dashed outline to signal the
  // approximate position.
  const isApproximate = ac.position_source === POSITION_SOURCE_ARC_ONLY;

  const altFt = ac.alt_baro ?? 0;
  let size = altFt > 35000 ? 30 : altFt > 20000 ? 26 : altFt > 5000 ? 22 : 18;
  if (isApproximate) size = Math.round(size * 0.78);

  const glow = isSelected
    ? "filter:drop-shadow(0 0 7px #fbbf24) drop-shadow(0 0 3px #fbbf24);"
    : "filter:drop-shadow(0 2px 5px rgba(0,0,0,0.85));";

  const iconOpacity = isApproximate ? 0.65 : 1.0;
  const strokeDash = isApproximate ? 'stroke-dasharray="2 1.5"' : "";

  // pointer-events: only the visible SVG + label are clickable.  The outer
  // 90×44 container would otherwise grab clicks in its empty 90% area —
  // particularly bad when icons are smaller (approximate / low-altitude).
  const svgHtml = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 32 32"
    style="display:block;pointer-events:auto;transform:rotate(${track}deg);${glow}opacity:${iconOpacity};">
    <path fill="${color}" stroke="rgba(255,255,255,0.7)" stroke-width="1.2" stroke-linejoin="round" ${strokeDash}
      d="${PLANE_PATH}"/>
  </svg>`;

  const labelHtml =
    showLabel && label
      ? `<div class="aircraft-label" style="pointer-events:auto;">${label}${alt ? `<span class="aircraft-alt"> ${alt}</span>` : ""}</div>`
      : "";

  return L.divIcon({
    className: `aircraft-marker ac-hex-${ac.hex}`,
    html: `<div style="display:flex;flex-direction:column;align-items:center;pointer-events:none;">${svgHtml}${labelHtml}</div>`,
    iconSize: [90, 44],
    iconAnchor: [45, Math.round(size / 2)],
  });
}

// NOTE: the ac-hex-<hex> class is what the 60 fps DOM-rotation loop queries;
// drone icons lacked it, so every drone burned a document-wide selector miss
// per frame and never rotated to its heading.
export function makeDroneIcon(ac, showLabel, isSelected) {
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  const glowFilter = isSelected
    ? "filter:drop-shadow(0 0 7px #fbbf24);"
    : "";

  const droneHtml = `<div style="${glowFilter}">${DRONE_SVG}</div>`;
  const labelHtml =
    showLabel && label
      ? `<div class="aircraft-label" style="color:#f59e0b;">${label}</div>`
      : "";

  return L.divIcon({
    className: `aircraft-marker ac-hex-${ac.hex}`,
    html: `<div style="display:flex;flex-direction:column;align-items:center;">${droneHtml}${labelHtml}</div>`,
    iconSize: [90, 40],
    iconAnchor: [45, 11],
  });
}

// Nodes use amber instead of red so they don't share a palette with the
// anomalous-aircraft marker (#f43f5e — rose-red + dashed halo). The two reds
// were close enough that users mistook anomalous aircraft for static nodes.
export const nodeIcon = L.divIcon({
  className: "node-marker",
  html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
    style="display:block;filter:drop-shadow(0 0 5px rgba(250,204,21,0.75));">
    <circle cx="12" cy="12" r="3.2" fill="#facc15"/>
    <circle cx="12" cy="12" r="6.5" fill="none" stroke="#facc15" stroke-width="1.5" opacity="0.6"/>
    <circle cx="12" cy="12" r="10.5" fill="none" stroke="#facc15" stroke-width="1" opacity="0.25"/>
  </svg>`,
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});
