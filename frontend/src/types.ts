/* ------------------------------------------------------------------ */
/*  Shared API response types — Tower Finder frontend                 */
/* ------------------------------------------------------------------ */

import type { ArcBufferEntry } from "./components/map/arcBuffer";

/** Single tower returned by /api/towers */
export interface Tower {
  callsign: string | null;
  frequency_mhz: number;
  frequency_matched: boolean;
  band: string;
  distance_km: number;
  distance_class: string;
}

/** /api/towers response */
export interface TowerSearchResponse {
  towers: Tower[];
  query: { lat: number; lon: number };
}

/** /api/elevation response */
export interface ElevationResponse {
  elevation_m: number;
}

/* ---- Aircraft / live feed ---- */

export interface Aircraft {
  hex: string;
  flight?: string;
  lat: number;
  lon: number;
  alt_baro?: number;
  alt_m?: number;
  gs?: number;
  track?: number;
  squawk?: string;
  type?: string;
  node_id?: string;
  target_class?: string;
  object_type?: string;
  position_source?: string;
  doppler_hz?: number;
  delay_us?: number;
  bistatic_range?: number;
  multinode?: boolean;
  n_nodes?: number;
  rms_delay?: number;
  rms_doppler?: number;
  is_anomalous?: boolean;
  anomaly_types?: string[];
  max_velocity_ms?: number;
  /** Debug: emitted position teleported (solver mis-association noise). */
  position_jump?: boolean;
  contributing_node_ids?: string[];
  ground_truth_hex?: string;
  ambiguity_arc?: [number, number][];
  recent_positions?: [number, number, number, number][];
  rssi?: number;
  snr?: number;
  speed_ms?: number;
  heading?: number;
  geolocation_method?: string;
}

/** Trail / recent-position tuple: [lat, lon, alt, ts] */
export type TrailPoint = [number, number, number, number];

/** Arc detection buffer entry — canonical shape lives in map/arcBuffer.ts. */
export type ArcEntry = ArcBufferEntry;

/** Per-object simulation ground-truth metadata (testmap debug feed). */
export interface GroundTruthMeta {
  object_type?: string;
  is_anomalous?: boolean;
  speed_ms?: number;
  heading?: number;
  /** False for "dark" simulated objects flying without a transponder. */
  has_adsb?: boolean;
  adsb_callsign?: string | null;
  anomaly_event?: string | null;
}

/** Data returned by useAircraftFeed() */
export interface AircraftFeedReturn {
  aircraft: Aircraft[];
  connected: boolean;
  trailsRef: React.MutableRefObject<Record<string, TrailPoint[]>>;
  groundTruthRef: React.MutableRefObject<Record<string, TrailPoint[]>>;
  groundTruthMetaRef: React.MutableRefObject<Record<string, unknown>>;
  anomalyHexesRef: React.MutableRefObject<Set<string>>;
  trailTick: number;
  groundTruthTick: number;
  historyRef: React.MutableRefObject<{ aircraft: Aircraft[]; ts: number }[]>;
  setPaused: (val: boolean) => void;
  arcsBufferRef: React.MutableRefObject<Record<string, ArcEntry>>;
  /** "hex|node_id" → timestamp of that node's most recent detection of the aircraft. */
  detectionsRef: React.MutableRefObject<Record<string, number>>;
}

/** Radar node metadata from /api/radar/analytics (as shaped by useNodes) */
export interface RadarNode {
  node_id: string;
  /** Display position — privacy-fuzzed by ~400 m, stable per node_id. */
  rx_lat: number;
  rx_lon: number;
  /** True RX position, for geometry that must match the backend's curves. */
  rx_lat_real: number;
  rx_lon_real: number;
  tx_lat: number;
  tx_lon: number;
  /**
   * RX/TX altitudes (m ASL) for the altitude-corrected arc rebuild.  Null
   * when the analytics feed doesn't carry them (it currently emits rx/tx as
   * {lat, lon} only); consumers then fall back to 0 m.
   */
  rx_alt_m: number | null;
  tx_alt_m: number | null;
  beam_azimuth_deg: number;
  beam_width_deg: number;
  max_range_km: number;
  /** Differential-range limit; null keeps the legacy circular sector. */
  max_bistatic_range_km: number | null;
  empirical_polygon: [number, number][] | null;
  empirical_n_points: number;
}
