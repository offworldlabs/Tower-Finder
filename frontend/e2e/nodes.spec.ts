/**
 * Node registration E2E tests — staging.
 *
 * Covers the HTTP auto-registration path and verifies the resulting state
 * across /api/radar/nodes, /api/radar/analytics/{id}, /api/radar/analytics,
 * and /api/radar/association/overlaps.
 *
 * Environment:
 *  - Requires RADAR_API_KEY (= STAGING_RADAR_API_KEY in CI).
 *    The auth-enforcement describe block runs without it (it tests server rejection
 *    of bad credentials). The main suite skips gracefully on fork PRs without the secret.
 *  - Uses unique node IDs per run to avoid cross-run state pollution.
 *
 * Cache timing:
 *  - /api/radar/nodes                served from pre-computed bytes, refreshed every 30 s
 *  - /api/radar/analytics            same 30 s cycle + 60 s inner summary cache
 *  - /api/radar/analytics/{node_id}  reads analytics manager directly — NO cache, immediate
 *  - /api/radar/association/overlaps same pre-computed bytes refresh as nodes (same function)
 *
 * Node IDs used:
 *  REAL_NODE_ID   — non-synth- prefix → is_synthetic=false, registered via single POST
 *  SYNTH_NODE_ID  — synth- prefix     → is_synthetic=true,  registered via single POST
 *  BULK_A_NODE_ID — real prefix, no config,       registered via bulk POST
 *  BULK_B_NODE_ID — real prefix, full geo config, registered via bulk POST
 */
import { test, expect, request } from "@playwright/test";
import { hosts } from "../playwright.config";

const API = hosts.api;
const API_KEY = process.env.RADAR_API_KEY ?? "";

const RUN_ID = Date.now().toString(36);
const REAL_NODE_ID   = `e2e-real-${RUN_ID}`;
const SYNTH_NODE_ID  = `synth-e2e-${RUN_ID}`;
const BULK_A_NODE_ID = `e2e-bulk-a-${RUN_ID}`;
const BULK_B_NODE_ID = `e2e-bulk-b-${RUN_ID}`;

// Full geographic config sent with BULK_B — used to verify config propagation into analytics.
const BULK_B_CONFIG = {
  node_id: BULK_B_NODE_ID,
  rx_lat: 33.94, rx_lon: -84.65, rx_alt_ft: 950,
  tx_lat: 33.76, tx_lon: -84.33, tx_alt_ft: 1600,
  beam_width_deg: 45,
  max_range_km: 50,
};

// latest_nodes_bytes and latest_overlaps_bytes are rebuilt in the same 30 s background
// function. Add a 5 s buffer over the full cycle.
const CACHE_TIMEOUT_MS = 35_000;

type Ctx = Awaited<ReturnType<typeof request.newContext>>;
type NodeMap = Record<string, Record<string, unknown>>;

// ── Helpers ───────────────────────────────────────────────────────────────────

async function postDetections(
  ctx: Ctx,
  nodeId: string,
  extra: Record<string, unknown> = {},
) {
  return ctx.post(`${API}/api/radar/detections`, {
    headers: { "X-API-Key": API_KEY },
    data: { node_id: nodeId, timestamp: Date.now() / 1000, detections: [], ...extra },
  });
}

/** Poll /api/radar/nodes until all nodeIds appear in the map (max CACHE_TIMEOUT_MS). */
async function waitForNodes(ctx: Ctx, nodeIds: string[]) {
  const deadline = Date.now() + CACHE_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const res = await ctx.get(`${API}/api/radar/nodes`);
    if (res.status() === 200) {
      const body = await res.json();
      const nodes = (body.nodes ?? {}) as Record<string, unknown>;
      if (nodeIds.every((id) => id in nodes)) return body;
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }
  throw new Error(
    `Nodes [${nodeIds.join(", ")}] did not appear within ${CACHE_TIMEOUT_MS} ms`,
  );
}

// =============================================================================
// 1. Auth enforcement — does NOT require our RADAR_API_KEY to be set.
//    Tests that the server rejects wrong/missing credentials regardless.
// =============================================================================
test.describe("Node registration — auth enforcement", () => {
  let ctx: Ctx;
  test.beforeAll(async () => { ctx = await request.newContext(); });
  test.afterAll(async () => { await ctx.dispose(); });

  test("POST without X-API-Key header is rejected with 401", async () => {
    const res = await ctx.post(`${API}/api/radar/detections`, {
      data: { node_id: "e2e-auth-probe", timestamp: Date.now() / 1000, detections: [] },
    });
    expect(res.status()).toBe(401);
    const body = await res.json();
    expect(body).toHaveProperty("detail"); // FastAPI error shape
  });

  test("POST with an incorrect X-API-Key is rejected with 401", async () => {
    const res = await ctx.post(`${API}/api/radar/detections`, {
      headers: { "X-API-Key": "definitely-wrong-key-e2e" },
      data: { node_id: "e2e-auth-probe", timestamp: Date.now() / 1000, detections: [] },
    });
    expect(res.status()).toBe(401);
  });

  test("bulk POST without X-API-Key is rejected with 401", async () => {
    const res = await ctx.post(`${API}/api/radar/detections/bulk`, {
      data: { nodes: [{ node_id: "e2e-auth-probe", frames: [] }] },
    });
    expect(res.status()).toBe(401);
  });
});

// =============================================================================
// 2. POST /api/radar/detections — response contract and frame queuing rules.
//    These tests run their own registrations (not the shared state) so they
//    have their own ctx and do not need the 30 s cache wait.
// =============================================================================
test.describe("Node registration — POST response and frame queuing", () => {
  let ctx: Ctx;

  test.beforeAll(async () => {
    if (!API_KEY) test.skip();
    ctx = await request.newContext();
  });
  test.afterAll(async () => { await ctx?.dispose(); });

  test("response shape: {status:'ok', frames_queued:number, tracks:number}", async () => {
    const res = await postDetections(ctx, `e2e-resp-${RUN_ID}`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.status).toBe("ok");
    expect(typeof body.frames_queued).toBe("number");
    expect(typeof body.tracks).toBe("number");
    expect(body.frames_queued).toBeGreaterThanOrEqual(0);
    expect(body.tracks).toBeGreaterThanOrEqual(0);
  });

  test("frame with a timestamp field is queued (frames_queued = 1)", async () => {
    const nodeId = `e2e-frames-${RUN_ID}`;
    const res = await postDetections(ctx, nodeId, {
      frames: [{ timestamp: Date.now() / 1000, delay: [120.0], doppler: [1.5] }],
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.frames_queued).toBe(1);
  });

  test("frame without a timestamp is NOT queued (frames_queued = 0 for that frame)", async () => {
    const nodeId = `e2e-notimestamp-${RUN_ID}`;
    // First POST registers the node. Second POST sends a frame without timestamp.
    await postDetections(ctx, nodeId); // register
    const res = await postDetections(ctx, nodeId, {
      frames: [{ delay: [50.0], doppler: [0.2] }], // no timestamp key
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.frames_queued).toBe(0);
  });

  test("node_id exceeding max length (> 128 chars) is rejected with 422", async () => {
    const res = await ctx.post(`${API}/api/radar/detections`, {
      headers: { "X-API-Key": API_KEY },
      data: { node_id: "x".repeat(130), timestamp: Date.now() / 1000, detections: [] },
    });
    expect(res.status()).toBe(422);
  });
});

// =============================================================================
// 3. Main suite — shares state across all inner describes.
//    beforeAll: registers 4 nodes (2 single, 2 bulk) and waits for the 30 s
//    cache cycle so /api/radar/nodes reflects all of them.
// =============================================================================
test.describe("Node registration — main integration suite", () => {
  let ctx: Ctx;

  // Cached response bodies populated in the outer beforeAll
  let nodesBody: {
    nodes: NodeMap;
    total: number;
    connected: number;
    synthetic: number;
  };
  let bulkResponseBody: { status: string; nodes_registered: number; frames_queued: number };
  let singleResponseBody: { status: string; frames_queued: number; tracks: number };

  test.beforeAll(async () => {
    if (!API_KEY) test.skip();
    ctx = await request.newContext();

    // ── Single-node registration (REAL + SYNTH) ────────────────────────────
    // REAL: include one valid frame so we can verify frames_queued response field.
    const singleRes = await ctx.post(`${API}/api/radar/detections`, {
      headers: { "X-API-Key": API_KEY },
      data: {
        node_id: REAL_NODE_ID,
        frames: [{ timestamp: Date.now() / 1000, delay: [100.5], doppler: [0.2], snr: [28.0] }],
      },
    });
    expect(singleRes.status()).toBe(200);
    singleResponseBody = await singleRes.json();

    await postDetections(ctx, SYNTH_NODE_ID);

    // ── Bulk registration (BULK_A: no config, BULK_B: full geo config) ────────
    const bulkRes = await ctx.post(`${API}/api/radar/detections/bulk`, {
      headers: { "X-API-Key": API_KEY },
      data: {
        nodes: [
          { node_id: BULK_A_NODE_ID, frames: [] },
          { node_id: BULK_B_NODE_ID, config: BULK_B_CONFIG, frames: [] },
        ],
      },
    });
    expect(bulkRes.status()).toBe(200);
    bulkResponseBody = await bulkRes.json();

    // ── Wait for 30 s refresh — all 4 nodes must appear simultaneously ────────
    nodesBody = (await waitForNodes(ctx, [
      REAL_NODE_ID, SYNTH_NODE_ID, BULK_A_NODE_ID, BULK_B_NODE_ID,
    ])) as typeof nodesBody;
  });

  test.afterAll(async () => { await ctx?.dispose(); });

  // ── 3a. Single-node POST response ────────────────────────────────────────
  test.describe("POST /api/radar/detections — response contract", () => {
    test("response has status:'ok', numeric frames_queued and tracks", () => {
      expect(singleResponseBody.status).toBe("ok");
      expect(typeof singleResponseBody.frames_queued).toBe("number");
      expect(typeof singleResponseBody.tracks).toBe("number");
      expect(singleResponseBody.frames_queued).toBeGreaterThanOrEqual(0);
      expect(singleResponseBody.tracks).toBeGreaterThanOrEqual(0);
    });

    test("the single frame with a timestamp was queued (frames_queued = 1)", () => {
      expect(singleResponseBody.frames_queued).toBe(1);
    });
  });

  // ── 3b. Bulk registration response ───────────────────────────────────────
  test.describe("POST /api/radar/detections/bulk — response contract", () => {
    test("response: {status:'ok', nodes_registered:2, frames_queued:0}", () => {
      expect(bulkResponseBody.status).toBe("ok");
      expect(bulkResponseBody.nodes_registered).toBe(2);
      expect(bulkResponseBody.frames_queued).toBe(0); // no frames submitted
    });
  });

  // ── 3c. Node registry — entry shape and per-field assertions ─────────────
  test.describe("/api/radar/nodes — node entry shape and flags", () => {
    test("REAL_NODE appears in the registry with status 'active'", () => {
      expect(nodesBody.nodes).toHaveProperty(REAL_NODE_ID);
      expect(nodesBody.nodes[REAL_NODE_ID].status).toBe("active");
    });

    test("REAL_NODE entry has all required top-level fields", () => {
      const node = nodesBody.nodes[REAL_NODE_ID];
      for (const field of [
        "status", "name", "config_hash", "last_heartbeat",
        "peer", "is_synthetic", "capabilities", "location",
      ]) {
        expect(node, `missing field: ${field}`).toHaveProperty(field);
      }
    });

    test("REAL_NODE peer is 'http' (registered via HTTP, not TCP)", () => {
      expect(nodesBody.nodes[REAL_NODE_ID].peer).toBe("http");
    });

    test("REAL_NODE is_synthetic is false (non-synth- prefix)", () => {
      expect(nodesBody.nodes[REAL_NODE_ID].is_synthetic).toBe(false);
    });

    test("SYNTH_NODE is_synthetic is true (synth- prefix)", () => {
      expect(nodesBody.nodes[SYNTH_NODE_ID]).toBeDefined();
      expect(nodesBody.nodes[SYNTH_NODE_ID].is_synthetic).toBe(true);
    });

    test("REAL_NODE name falls back to node_id when no name in config", () => {
      expect(nodesBody.nodes[REAL_NODE_ID].name).toBe(REAL_NODE_ID);
    });

    test("REAL_NODE config_hash is '' for HTTP auto-registration (no handshake)", () => {
      expect(nodesBody.nodes[REAL_NODE_ID].config_hash).toBe("");
    });

    test("REAL_NODE capabilities is {} for HTTP auto-registration", () => {
      const caps = nodesBody.nodes[REAL_NODE_ID].capabilities;
      expect(typeof caps).toBe("object");
      expect(Object.keys(caps as object)).toHaveLength(0);
    });

    test("REAL_NODE last_heartbeat is a valid ISO 8601 UTC string within the last 5 minutes", () => {
      const hb = nodesBody.nodes[REAL_NODE_ID].last_heartbeat as string;
      expect(typeof hb).toBe("string");
      const parsed = new Date(hb);
      expect(Number.isNaN(parsed.getTime())).toBe(false);
      const ageMs = Date.now() - parsed.getTime();
      expect(ageMs).toBeGreaterThanOrEqual(0);
      expect(ageMs).toBeLessThan(5 * 60 * 1000); // < 5 minutes
    });

    test("REAL_NODE location object has all 6 geographic sub-fields", () => {
      const loc = nodesBody.nodes[REAL_NODE_ID].location as Record<string, unknown>;
      for (const f of ["rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"]) {
        expect(loc, `missing location field: ${f}`).toHaveProperty(f);
      }
    });

    test("REAL_NODE location sub-fields are null (no config supplied at registration)", () => {
      const loc = nodesBody.nodes[REAL_NODE_ID].location as Record<string, unknown>;
      expect(loc.rx_lat).toBeNull();
      expect(loc.rx_lon).toBeNull();
      expect(loc.tx_lat).toBeNull();
      expect(loc.tx_lon).toBeNull();
    });

    test("BULK_A node appears with peer 'http-bulk'", () => {
      expect(nodesBody.nodes[BULK_A_NODE_ID]).toBeDefined();
      expect(nodesBody.nodes[BULK_A_NODE_ID].peer).toBe("http-bulk");
    });

    test("BULK_B location in nodes registry reflects the full geo config", () => {
      const loc = nodesBody.nodes[BULK_B_NODE_ID].location as Record<string, number | null>;
      expect(loc.rx_lat).toBeCloseTo(BULK_B_CONFIG.rx_lat, 4);
      expect(loc.rx_lon).toBeCloseTo(BULK_B_CONFIG.rx_lon, 4);
      expect(loc.tx_lat).toBeCloseTo(BULK_B_CONFIG.tx_lat, 4);
      expect(loc.tx_lon).toBeCloseTo(BULK_B_CONFIG.tx_lon, 4);
    });

    test("re-POST of the same node_id does not create a duplicate entry", async () => {
      await postDetections(ctx, REAL_NODE_ID);
      const res = await ctx.get(`${API}/api/radar/nodes`);
      const body = await res.json();
      const matches = Object.keys(body.nodes as object).filter((k) => k === REAL_NODE_ID);
      expect(matches).toHaveLength(1);
    });
  });

  // ── 3d. Count field consistency ──────────────────────────────────────────
  test.describe("/api/radar/nodes — count fields are internally consistent", () => {
    test("total equals the number of keys in the nodes map", () => {
      expect(nodesBody.total).toBe(Object.keys(nodesBody.nodes).length);
    });

    test("synthetic equals the count of nodes where is_synthetic:true", () => {
      const count = Object.values(nodesBody.nodes).filter(
        (n) => n.is_synthetic === true,
      ).length;
      expect(nodesBody.synthetic).toBe(count);
    });

    test("connected equals the count of nodes where status !== 'disconnected'", () => {
      const count = Object.values(nodesBody.nodes).filter(
        (n) => n.status !== "disconnected",
      ).length;
      expect(nodesBody.connected).toBe(count);
    });
  });

  // ── 3e. Per-node analytics — direct manager read, no cache lag ───────────
  test.describe("/api/radar/analytics/{node_id} — immediate per-node depth", () => {
    let analyticsBody: Record<string, unknown>;

    test.beforeAll(async () => {
      // Per-node analytics are registered synchronously during POST handling,
      // so they are available immediately — no cache wait needed.
      const res = await ctx.get(`${API}/api/radar/analytics/${REAL_NODE_ID}`);
      expect(res.status()).toBe(200);
      analyticsBody = await res.json();
    });

    test("returns HTTP 200 immediately after registration (no 30 s cache wait)", () => {
      expect(typeof analyticsBody).toBe("object");
      expect(analyticsBody).not.toBeNull();
    });

    test("response root contains node_id matching the requested ID", () => {
      expect(analyticsBody.node_id).toBe(REAL_NODE_ID);
    });

    test("metrics block is present with all expected keys", () => {
      const m = analyticsBody.metrics as Record<string, unknown>;
      expect(m).toBeDefined();
      for (const k of [
        "node_id", "uptime_s", "total_frames", "total_detections",
        "avg_detections_per_frame", "avg_snr", "max_snr",
        "total_tracks", "geolocated_tracks", "track_quality",
      ]) {
        expect(m, `metrics missing key: ${k}`).toHaveProperty(k);
      }
    });

    test("metrics.uptime_s is a non-negative number", () => {
      const m = analyticsBody.metrics as Record<string, unknown>;
      expect(typeof m.uptime_s).toBe("number");
      expect(m.uptime_s as number).toBeGreaterThanOrEqual(0);
    });

    test("metrics.total_frames is a non-negative integer", () => {
      // total_frames is incremented by the frame processor, not by the POST handler,
      // so it may be 0 or 1 depending on how quickly the worker drains the queue.
      const m = analyticsBody.metrics as Record<string, unknown>;
      expect(typeof m.total_frames).toBe("number");
      expect(m.total_frames as number).toBeGreaterThanOrEqual(0);
    });

    test("trust block is present with all expected keys and fresh-node values", () => {
      const t = analyticsBody.trust as Record<string, unknown>;
      expect(t).toBeDefined();
      for (const k of ["node_id", "trust_score", "n_samples", "rms_delay_error_us", "rms_doppler_error_hz"]) {
        expect(t, `trust missing key: ${k}`).toHaveProperty(k);
      }
      // No ADS-B correlation samples yet for a freshly registered node
      expect(t.trust_score).toBe(0);
      expect(t.n_samples).toBe(0);
      expect(t.rms_delay_error_us).toBe(0);
      expect(t.rms_doppler_error_hz).toBe(0);
    });

    test("detection_area block is present with all expected geometry keys", () => {
      const da = analyticsBody.detection_area as Record<string, unknown>;
      expect(da).toBeDefined();
      for (const k of [
        "node_id", "rx", "tx", "beam_azimuth_deg", "beam_width_deg", "max_range_km",
        "n_detections", "observed_delay_range_us", "observed_doppler_range_hz",
        "estimated_max_range_km", "furthest_detections",
      ]) {
        expect(da, `detection_area missing key: ${k}`).toHaveProperty(k);
      }
    });

    test("detection_area.rx and .tx each expose lat and lon", () => {
      const da = analyticsBody.detection_area as Record<string, Record<string, unknown>>;
      expect(da.rx).toHaveProperty("lat");
      expect(da.rx).toHaveProperty("lon");
      expect(da.tx).toHaveProperty("lat");
      expect(da.tx).toHaveProperty("lon");
    });

    test("detection_area.n_detections is a non-negative integer", () => {
      // We sent one frame with delay/doppler data — after the frame workers process it
      // n_detections will be 1. If the worker hasn't run yet it will be 0.
      // Either value is correct; what matters is the field is a valid number.
      const da = analyticsBody.detection_area as Record<string, unknown>;
      expect(typeof da.n_detections).toBe("number");
      expect(da.n_detections as number).toBeGreaterThanOrEqual(0);
    });

    test("detection_area.furthest_detections is an empty array for a fresh node", () => {
      const da = analyticsBody.detection_area as Record<string, unknown>;
      expect(Array.isArray(da.furthest_detections)).toBe(true);
      expect((da.furthest_detections as unknown[]).length).toBe(0);
    });

    test("reputation block: initial reputation is 1.0, not blocked, no penalties", () => {
      const rep = analyticsBody.reputation as Record<string, unknown>;
      expect(rep).toBeDefined();
      for (const k of ["node_id", "reputation", "blocked", "block_reason", "n_penalties", "recent_penalties"]) {
        expect(rep, `reputation missing key: ${k}`).toHaveProperty(k);
      }
      expect(rep.reputation).toBe(1);
      expect(rep.blocked).toBe(false);
      expect(rep.n_penalties).toBe(0);
      expect(rep.recent_penalties).toEqual([]);
    });

    test("GET /api/radar/analytics/{id} returns 404 for a node that was never registered", async () => {
      const res = await ctx.get(
        `${API}/api/radar/analytics/e2e-ghost-node-${RUN_ID}`,
      );
      expect(res.status()).toBe(404);
      const body = await res.json();
      expect(body).toHaveProperty("detail"); // FastAPI error body
    });
  });

  // ── 3f. Bulk node analytics — config propagation into analytics manager ───
  test.describe("Bulk registration — config propagated to analytics manager", () => {
    let bulkBAnalytics: Record<string, unknown>;

    test.beforeAll(async () => {
      const res = await ctx.get(`${API}/api/radar/analytics/${BULK_B_NODE_ID}`);
      expect(res.status()).toBe(200);
      bulkBAnalytics = await res.json();
    });

    test("BULK_B detection_area rx/tx lat+lon match the config submitted in the bulk POST", () => {
      const da = bulkBAnalytics.detection_area as Record<string, Record<string, number>>;
      expect(da).toBeDefined();
      expect(da.rx.lat).toBeCloseTo(BULK_B_CONFIG.rx_lat, 4);
      expect(da.rx.lon).toBeCloseTo(BULK_B_CONFIG.rx_lon, 4);
      expect(da.tx.lat).toBeCloseTo(BULK_B_CONFIG.tx_lat, 4);
      expect(da.tx.lon).toBeCloseTo(BULK_B_CONFIG.tx_lon, 4);
    });

    test("BULK_B detection_area beam_width_deg matches the registered config", () => {
      const da = bulkBAnalytics.detection_area as Record<string, number>;
      expect(da.beam_width_deg).toBeCloseTo(BULK_B_CONFIG.beam_width_deg, 1);
    });

    test("BULK_B detection_area max_range_km matches the registered config", () => {
      const da = bulkBAnalytics.detection_area as Record<string, number>;
      expect(da.max_range_km).toBeCloseTo(BULK_B_CONFIG.max_range_km, 1);
    });
  });

  // ── 3g. real_only filter ─────────────────────────────────────────────────
  test.describe("GET /api/radar/analytics — real_only filter", () => {
    test("real_only=true response contains no synthetic nodes (cross-checked vs /api/radar/nodes)", async () => {
      // Build the authoritative set of synthetic node IDs from the nodes endpoint.
      const syntheticIds = new Set(
        Object.entries(nodesBody.nodes)
          .filter(([, info]) => info.is_synthetic === true)
          .map(([id]) => id),
      );

      const res = await ctx.get(`${API}/api/radar/analytics?real_only=true`);
      expect(res.status()).toBe(200);
      const body = await res.json();
      for (const nodeId of Object.keys(body.nodes as Record<string, unknown>)) {
        expect(
          syntheticIds,
          `synthetic node ${nodeId} leaked into real_only response`,
        ).not.toContain(nodeId);
      }
    });

    test("real_only=true node count ≤ full analytics node count", async () => {
      const [fullRes, realRes] = await Promise.all([
        ctx.get(`${API}/api/radar/analytics`),
        ctx.get(`${API}/api/radar/analytics?real_only=true`),
      ]);
      expect(fullRes.status()).toBe(200);
      expect(realRes.status()).toBe(200);
      const fullCount = Object.keys((await fullRes.json()).nodes ?? {}).length;
      const realCount = Object.keys((await realRes.json()).nodes ?? {}).length;
      expect(realCount).toBeLessThanOrEqual(fullCount);
    });

    test("full analytics (no filter) includes synth- nodes from the running fleet", async () => {
      const res = await ctx.get(`${API}/api/radar/analytics`);
      expect(res.status()).toBe(200);
      const body = await res.json();
      const synths = Object.keys((body.nodes as Record<string, unknown>) ?? {}).filter(
        (id) => id.startsWith("synth-"),
      );
      expect(synths.length).toBeGreaterThan(0);
    });

    test("if SYNTH_NODE appears in full analytics, it must be absent from real_only", async () => {
      // The 30+60 s double cache means SYNTH_NODE might not appear in aggregated
      // analytics yet — so we only assert the property when it IS present.
      const [fullRes, realRes] = await Promise.all([
        ctx.get(`${API}/api/radar/analytics`),
        ctx.get(`${API}/api/radar/analytics?real_only=true`),
      ]);
      const fullNodes = (await fullRes.json()).nodes as Record<string, unknown>;
      const realNodes = (await realRes.json()).nodes as Record<string, unknown>;
      if (SYNTH_NODE_ID in fullNodes) {
        expect(realNodes).not.toHaveProperty(SYNTH_NODE_ID);
      }
    });
  });

  // ── 3h. Overlaps — registered_nodes list ────────────────────────────────
  test.describe("/api/radar/association/overlaps — registered_nodes", () => {
    let overlapsBody: { overlaps: Record<string, unknown>[]; registered_nodes: string[] };

    test.beforeAll(async () => {
      // Both latest_nodes_bytes and latest_overlaps_bytes are rebuilt in the same
      // 30 s background task, so they are always consistent after waitForNodes.
      const res = await ctx.get(`${API}/api/radar/association/overlaps`);
      expect(res.status()).toBe(200);
      overlapsBody = await res.json();
    });

    test("response has an overlaps array and a registered_nodes array", () => {
      expect(Array.isArray(overlapsBody.overlaps)).toBe(true);
      expect(Array.isArray(overlapsBody.registered_nodes)).toBe(true);
    });

    test("registered_nodes includes REAL_NODE after registration", () => {
      expect(overlapsBody.registered_nodes).toContain(REAL_NODE_ID);
    });

    test("registered_nodes includes SYNTH_NODE after registration", () => {
      expect(overlapsBody.registered_nodes).toContain(SYNTH_NODE_ID);
    });

    test("registered_nodes includes BULK_B_NODE (registered via bulk with full config)", () => {
      expect(overlapsBody.registered_nodes).toContain(BULK_B_NODE_ID);
    });

    test("all entries in the overlaps array have has_overlap:true (server-side filter)", () => {
      for (const overlap of overlapsBody.overlaps) {
        expect(overlap.has_overlap).toBe(true);
      }
    });

    test("overlap entries expose node_a, node_b, and has_overlap fields", () => {
      for (const overlap of overlapsBody.overlaps.slice(0, 10)) {
        expect(overlap).toHaveProperty("node_a");
        expect(overlap).toHaveProperty("node_b");
        expect(overlap).toHaveProperty("has_overlap");
      }
    });
  });
});
