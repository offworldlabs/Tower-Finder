const API_BASE = "/api";

// Wrappers accept an optional AbortSignal so unmounting components can cancel
// in-flight requests instead of resolving into setState after unmount.

const MLAT_VERIFICATION_TTL_MS = 5000;
let mlatVerificationCache: unknown = null;
let mlatVerificationCacheTs = 0;
let mlatVerificationInflight: Promise<unknown | null> | null = null;

export async function fetchTowers(lat, lon, altitude = 0, limit = 20, source = "us", frequencies = []) {
  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
    altitude: String(altitude),
    limit: String(limit),
    source,
  });
  if (frequencies.length > 0) {
    params.set("frequencies", frequencies.join(","));
  }
  const res = await fetch(`${API_BASE}/towers?${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

export async function fetchElevation(lat, lon, signal?: AbortSignal) {
  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
  });
  const res = await fetch(`${API_BASE}/elevation?${params}`, { signal });
  if (!res.ok) return null;
  const data = await res.json();
  return data.elevation_m;
}

export async function fetchNodeVerification(nodeId: string, signal?: AbortSignal) {
  if (!nodeId) return null;
  const res = await fetch(
    `${API_BASE}/test/node/${encodeURIComponent(nodeId)}/verification`, { signal });
  if (!res.ok) return null;
  return res.json();
}

export async function fetchNodeDetectionRange(nodeId: string, signal?: AbortSignal) {
  if (!nodeId) return null;
  const res = await fetch(
    `${API_BASE}/test/node/${encodeURIComponent(nodeId)}/detection-range`, { signal });
  if (!res.ok) return null;
  return res.json();
}

export async function fetchMlatVerification() {
  const now = Date.now();
  if (mlatVerificationCache && (now - mlatVerificationCacheTs) < MLAT_VERIFICATION_TTL_MS) {
    return mlatVerificationCache;
  }

  if (mlatVerificationInflight) {
    return mlatVerificationInflight;
  }

  mlatVerificationInflight = (async () => {
    const res = await fetch(`${API_BASE}/test/mlat-verification`);
    if (!res.ok) return null;
    const data = await res.json();
    mlatVerificationCache = data;
    mlatVerificationCacheTs = Date.now();
    return data;
  })();

  try {
    return await mlatVerificationInflight;
  } finally {
    mlatVerificationInflight = null;
  }
}

export async function fetchMlatAccuracy(signal?: AbortSignal) {
  const res = await fetch(`${API_BASE}/test/mlat-accuracy`, { signal });
  if (!res.ok) return null;
  return res.json();
}

// Per-solve history for one MLAT map marker (mn<sha256[:10]> hex): the raw
// solves behind the marker over the last ~30 min, plus gate rejections near
// its position. Debug surface — see AircraftDetailPanel's solve history.
export async function fetchMlatHistory(hex: string, signal?: AbortSignal) {
  if (!hex) return null;
  const res = await fetch(
    `${API_BASE}/test/mlat-history?hex=${encodeURIComponent(hex)}`, { signal });
  if (!res.ok) return null;
  return res.json();
}

// Returns the current user dict, or null when not authenticated (401) or unreachable.
export async function fetchMe() {
  try {
    const res = await fetch(`${API_BASE}/auth/me`, { credentials: "same-origin" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// Returns the list of nodes owned by the current user ([] when unauthenticated/unreachable).
export async function fetchMyNodes() {
  try {
    const res = await fetch(`${API_BASE}/auth/me/nodes`, { credentials: "same-origin" });
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}
