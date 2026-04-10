/* ------------------------------------------------------------------ */
/*  Shared API response types — Tower Finder frontend                 */
/* ------------------------------------------------------------------ */

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

/** Arc detection buffer entry */
export interface ArcEntry {
  hex: string;
  node_id: string;
  ambiguity_arc: [number, number][];
  doppler_hz: number;
  target_class?: string;
  ts: number;
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
}

/** Radar node metadata from /api/radar/analytics */
export interface RadarNode {
  node_id: string;
  rx_lat: number;
  rx_lon: number;
  tx_lat: number;
  tx_lon: number;
  beam_azimuth_deg: number;
  beam_width_deg: number;
  max_range_km: number;
  empirical_polygon: [number, number][] | null;
  empirical_n_points: number;
}
