const API_BASE = "/api";

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

export async function fetchElevation(lat, lon) {
  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
  });
  const res = await fetch(`${API_BASE}/elevation?${params}`);
  if (!res.ok) return null;
  const data = await res.json();
  return data.elevation_m;
}

export async function fetchRadar3Verification() {
  const res = await fetch(`${API_BASE}/test/radar3/verification`);
  if (!res.ok) return null;
  return res.json();
}

export async function fetchRadar3DetectionRange() {
  const res = await fetch(`${API_BASE}/test/radar3/detection-range`);
  if (!res.ok) return null;
  return res.json();
}
