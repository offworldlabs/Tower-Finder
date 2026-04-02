import L from "leaflet";

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

export function getAircraftColor(ac) {
  if (ac.multinode || ac.position_source === "multinode_solve") return "#a78bfa";
  if (ac.position_source === "solver_adsb_seed") return "#2dd4bf";
  return "#38bdf8";
}

export function makeAircraftIcon(ac, showLabel, isSelected) {
  const track = ac.track ?? 0;
  const color = getAircraftColor(ac);
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  const alt = ac.alt_baro ? `FL${Math.round(ac.alt_baro / 100)}` : "";

  const altFt = ac.alt_baro ?? 0;
  const size = altFt > 35000 ? 30 : altFt > 20000 ? 26 : altFt > 5000 ? 22 : 18;

  const glow = isSelected
    ? "filter:drop-shadow(0 0 7px #fbbf24) drop-shadow(0 0 3px #fbbf24);"
    : "filter:drop-shadow(0 2px 5px rgba(0,0,0,0.85));";

  const svgHtml = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 32 32"
    style="display:block;transform:rotate(${track}deg);${glow}">
    <path fill="${color}" stroke="rgba(255,255,255,0.7)" stroke-width="1.2" stroke-linejoin="round"
      d="${PLANE_PATH}"/>
  </svg>`;

  const labelHtml =
    showLabel && label
      ? `<div class="aircraft-label">${label}${alt ? `<span class="aircraft-alt"> ${alt}</span>` : ""}</div>`
      : "";

  return L.divIcon({
    className: `aircraft-marker ac-hex-${ac.hex}`,
    html: `<div style="display:flex;flex-direction:column;align-items:center;">${svgHtml}${labelHtml}</div>`,
    iconSize: [90, 44],
    iconAnchor: [45, Math.round(size / 2)],
  });
}

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
    className: "aircraft-marker",
    html: `<div style="display:flex;flex-direction:column;align-items:center;">${droneHtml}${labelHtml}</div>`,
    iconSize: [90, 40],
    iconAnchor: [45, 11],
  });
}

export const nodeIcon = L.divIcon({
  className: "node-marker",
  html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
    style="display:block;filter:drop-shadow(0 0 5px rgba(239,68,68,0.75));">
    <circle cx="12" cy="12" r="3.2" fill="#ef4444"/>
    <circle cx="12" cy="12" r="6.5" fill="none" stroke="#ef4444" stroke-width="1.5" opacity="0.6"/>
    <circle cx="12" cy="12" r="10.5" fill="none" stroke="#ef4444" stroke-width="1" opacity="0.25"/>
  </svg>`,
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});
