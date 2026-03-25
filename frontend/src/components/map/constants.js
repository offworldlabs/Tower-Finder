export const API_BASE = "/api";
export const ANIMATION_MS = 700;
export const STALE_AIRCRAFT_MS = 8000;
export const MAX_HISTORY = 150;
export const VIEWPORT_PAD_DEG = 1.5;
export const FOCUS_CLUSTER_LIMIT = 24;

// Doppler colour gradient — dark blue (approaching) → light blue → grey → light red → dark red (receding)
// t ∈ [-1, +1] maps linearly across the 5 stops.
const _DOPPLER_STOPS = [
  [0x1e, 0x3a, 0x8a], // -1.0  dark blue
  [0x60, 0xa5, 0xfa], // -0.5  light blue
  [0x94, 0xa3, 0xb8], //  0.0  grey
  [0xf8, 0x71, 0x71], // +0.5  light red
  [0x99, 0x1b, 0x1b], // +1.0  dark red
];
export function dopplerColor(doppler_hz, maxDop = 200) {
  const t = Math.max(-1, Math.min(1, doppler_hz / maxDop)); // [-1, +1]
  const pos = (t + 1) / 2 * (_DOPPLER_STOPS.length - 1);   // [0, 4]
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, _DOPPLER_STOPS.length - 1);
  const f = pos - lo;
  const [r, g, b] = _DOPPLER_STOPS[lo].map((c, i) => Math.round(c + f * (_DOPPLER_STOPS[hi][i] - c)));
  return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}
