/* ------------------------------------------------------------------ */
/*  Shared API response types — RETINA dashboard                      */
/* ------------------------------------------------------------------ */

/** Authenticated user from /api/auth/me */
export interface User {
  uid: string;
  email: string;
  name: string;
  role: "admin" | "user";
}

/** Auth context value */
export interface AuthContextValue {
  user: User | null;
  loading: boolean;
  logout: () => Promise<void>;
}

/* ---- Aircraft ---- */

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
  is_anomalous?: boolean;
  anomaly_types?: string[];
  ground_truth_hex?: string;
  ambiguity_arc?: [number, number][];
}

/** Trail tuple: [lat, lon, alt, ts] */
export type TrailPoint = [number, number, number, number];

/* ---- Radar nodes ---- */

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

/* ---- Dashboard / fleet ---- */

export interface FleetDashboard {
  nodes: { active: number; total: number };
  server_health: {
    frame_queue_utilization_pct: number;
    frames_dropped: number;
  };
  pipeline: { aircraft_on_map: number };
}

export interface LeaderboardEntry {
  node_id: string;
  detections: number;
  uptime_pct: number;
  reputation: number;
}

export interface AdminEvent {
  ts: string;
  type: string;
  node_id?: string;
  message: string;
}

export interface StorageInfo {
  total_files: number;
  total_size_mb: number;
  entries: { name: string; size_kb: number; date: string }[];
}

export interface OverlapPair {
  node_a: string;
  node_b: string;
  has_overlap: boolean;
  overlap_area_km2?: number;
  shared_detections?: number;
}
