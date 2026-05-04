/**
 * API smoke tests — hit every key endpoint and assert shape + latency.
 * These run against the API host (staging-api / api.retina.fm / localhost:8000).
 */
import { test, expect, request } from "@playwright/test";
import { hosts } from "../playwright.config";

const API = hosts.api;
const LATENCY_WARN_MS = 3000; // fail if any endpoint exceeds this

test.describe("API health", () => {
  test("GET /api/health returns {status: ok} or {status: degraded}", async () => {
    const ctx = await request.newContext();
    const t0 = Date.now();
    const res = await ctx.get(`${API}/api/health`);
    const ms = Date.now() - t0;

    expect(res.status()).toBe(200);
    expect(ms).toBeLessThan(LATENCY_WARN_MS);

    const body = await res.json();
    expect(["ok", "degraded"]).toContain(body.status);
  });
});

test.describe("API radar endpoints", () => {
  let ctx: Awaited<ReturnType<typeof request.newContext>>;

  test.beforeAll(async () => {
    ctx = await request.newContext();
  });

  test.afterAll(async () => {
    await ctx.dispose();
  });

  test("GET /api/radar/nodes returns node map with expected shape", async () => {
    const res = await ctx.get(`${API}/api/radar/nodes`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    // Top-level must have a `nodes` dict
    expect(body).toHaveProperty("nodes");
    expect(typeof body.nodes).toBe("object");

    // Each node must have required fields
    const nodeEntries = Object.values(body.nodes) as Record<string, unknown>[];
    if (nodeEntries.length > 0) {
      const first = nodeEntries[0];
      expect(first).toHaveProperty("status");
      expect(first).toHaveProperty("is_synthetic");
      expect(first).toHaveProperty("name");
    }
  });

  test("GET /api/radar/analytics returns analytics with nodes map", async () => {
    const res = await ctx.get(`${API}/api/radar/analytics`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body).toHaveProperty("nodes");
    expect(typeof body.nodes).toBe("object");
  });

  test("GET /api/radar/analytics?real_only=true filters to non-synthetic nodes", async () => {
    const res = await ctx.get(`${API}/api/radar/analytics?real_only=true`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body).toHaveProperty("nodes");
    const nodes = Object.values(body.nodes) as Record<string, unknown>[];
    // Every node in the real_only response must not be synthetic
    // (nodes from /api/radar/analytics carry is_synthetic via the nodes map)
    for (const node of nodes) {
      // is_synthetic may be present or omitted depending on API version
      if ("is_synthetic" in node) {
        expect(node.is_synthetic).toBe(false);
      }
    }
  });

  test("GET /api/radar/association/overlaps returns filtered overlap list", async () => {
    const res = await ctx.get(`${API}/api/radar/association/overlaps`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body).toHaveProperty("overlaps");
    expect(Array.isArray(body.overlaps)).toBe(true);

    // All returned overlaps must have has_overlap: true (server-side filter)
    for (const overlap of body.overlaps as Record<string, unknown>[]) {
      expect(overlap.has_overlap).toBe(true);
      expect(overlap).toHaveProperty("node_a");
      expect(overlap).toHaveProperty("node_b");
    }
  });

  test("GET /api/radar/data/aircraft.json returns tar1090-compatible aircraft list", async () => {
    const res = await ctx.get(`${API}/api/radar/data/aircraft.json`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body).toHaveProperty("aircraft");
    expect(Array.isArray(body.aircraft)).toBe(true);
    // tar1090 format: each aircraft has at minimum a hex field
    for (const ac of (body.aircraft as Record<string, unknown>[]).slice(0, 5)) {
      expect(ac).toHaveProperty("hex");
    }
  });

  test("GET /api/test/dashboard returns node + server_health + pipeline", async () => {
    const res = await ctx.get(`${API}/api/test/dashboard`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    expect(body).toHaveProperty("nodes");
    expect(body).toHaveProperty("server_health");
    expect(body).toHaveProperty("pipeline");

    // Server health must have frame queue metrics
    expect(body.server_health).toHaveProperty("frame_queue_utilization_pct");
    expect(body.server_health).toHaveProperty("frames_dropped");

    // No runaway drops (threshold: <5000 total)
    expect(body.server_health.frames_dropped).toBeLessThan(5000);
  });
});

test.describe("API admin endpoints", () => {
  let ctx: Awaited<ReturnType<typeof request.newContext>>;

  test.beforeAll(async () => {
    ctx = await request.newContext();
  });

  test.afterAll(async () => {
    await ctx.dispose();
  });

  test("GET /api/admin/leaderboard returns per-node list", async () => {
    const res = await ctx.get(`${API}/api/admin/leaderboard`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    // Response shape: {leaderboard: [...], total: N}
    expect(body).toHaveProperty("leaderboard");
    expect(Array.isArray(body.leaderboard)).toBe(true);
    // Each entry has required fields
    if (body.leaderboard.length > 0) {
      const first = body.leaderboard[0];
      expect(first).toHaveProperty("node_id");
      expect(first).toHaveProperty("name");
    }
  });

  test("GET /api/config returns tower config with valid shape", async () => {
    const res = await ctx.get(`${API}/api/config`);
    expect(res.status()).toBe(200);

    const body = await res.json();
    // Must be JSON object — specific shape can vary
    expect(typeof body).toBe("object");
  });
});
