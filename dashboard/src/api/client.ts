const BASE = "";

async function request(path, opts: any = {}) {
  const controller = new AbortController();
  const timeoutMs = path === "/api/auth/me" ? 30000 : 10000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      credentials: "same-origin",
      ...opts,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...opts.headers },
    });
    clearTimeout(timer);
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Unauthorized");
    }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  } catch (e) {
    clearTimeout(timer);
    throw e;
  }
}

export function downloadUrl(path) {
  return `${BASE}${path}`;
}

export const api = {
  // Auth
  me: () => request("/api/auth/me"),
  logout: () => request("/api/auth/logout", { method: "POST" }),

  // Radar / nodes
  nodes: () => request("/api/radar/nodes"),
  status: () => request("/api/radar/status"),
  analytics: () => request("/api/radar/analytics"),
  nodeAnalytics: (id) => request(`/api/radar/analytics/${id}`),
  aircraft: () => request("/api/radar/data/aircraft.json"),
  overlaps: () => request("/api/radar/association/overlaps"),
  anomalies: () => request("/api/radar/anomalies"),

  // Archive
  archive: (limit = 50, offset = 0, nodeId = null) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (nodeId) params.set("node_id", nodeId);
    return request(`/api/data/archive?${params}`);
  },

  // Custody
  custody: () => request("/api/custody/status"),

  // Test dashboard (fleet overview)
  fleetDashboard: () => request("/api/test/dashboard"),

  // Leaderboard & alerts (user-facing)
  leaderboard: () => request("/api/admin/leaderboard"),
  alerts: () => request("/api/admin/alerts"),

  // Admin
  adminUsers: () => request("/api/admin/users"),
  adminSetRole: (uid, role) =>
    request(`/api/admin/users/${uid}/role`, {
      method: "PUT",
      body: JSON.stringify({ role }),
    }),
  adminEvents: (limit = 200) => request(`/api/admin/events?limit=${limit}`),
  adminNodeConfig: () => request("/api/admin/config/nodes"),
  adminTowerConfig: () => request("/api/admin/config/towers"),
  adminUpdateNodeConfig: (config) =>
    request("/api/admin/config/nodes", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  adminUpdateTowerConfig: (config) =>
    request("/api/admin/config/towers", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  adminConfigHistory: () => request("/api/admin/config/history"),
  adminStorage: () => request("/api/admin/storage"),
};
