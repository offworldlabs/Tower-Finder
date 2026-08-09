import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import PhysicsSettings from "./PhysicsSettings";

// Config payload shaped like a fresh backend: the fraction keys are always
// present, but min_aircraft/max_aircraft/max_range_km/n_nodes/dual_fraction
// are only-if-set (core/state.py:545) and absent until an operator PUTs one.
const BARE_CONFIG = {
  frac_anomalous: 0.05,
  frac_drone: 0.1,
  frac_dark: 0.15,
  ground_truth_counts: { total: 0, anomalous: 0, drone: 0, aircraft: 0 },
};

function installFetchMock() {
  const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
    const u = String(url);
    if (u.includes("/simulation/config")) {
      if (opts?.method === "PUT") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ ok: true, config: BARE_CONFIG }),
        } as Response);
      }
      return Promise.resolve({ ok: true, json: async () => BARE_CONFIG } as Response);
    }
    if (u.includes("/simulation/ground-truth")) {
      return Promise.resolve({ ok: true, json: async () => ({ aircraft: [] }) } as Response);
    }
    if (u.includes("/test/solver-stats")) {
      return Promise.resolve({ ok: false, json: async () => ({}) } as Response);
    }
    if (u.includes("/radar/nodes")) {
      return Promise.resolve({ ok: true, json: async () => ({ nodes: {} }) } as Response);
    }
    return Promise.resolve({ ok: false, json: async () => ({}) } as Response);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("PhysicsSettings", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("falls back to numeric draft defaults when the config payload omits min/max_aircraft and max_range_km (no NaN)", async () => {
    installFetchMock();
    render(<PhysicsSettings />);

    // "Total objects target" sublabel is built from draft.min_aircraft /
    // draft.max_aircraft — must render the fallback 20-40, never NaN-NaN.
    await waitFor(() => {
      expect(screen.getByText(/spawns 20–40/)).toBeInTheDocument();
    });

    // sceneDraft.max_range_km falls back to 0, which renders as "auto"
    // (per-node generated ranges), not "0 km" and not "NaN km".
    await waitFor(() => {
      expect(screen.getByText("auto")).toBeInTheDocument();
    });

    expect(document.body.textContent).not.toMatch(/NaN/);
  });

  it("sends max_range_km in the Fleet Scene apply payload, not the main Apply payload", async () => {
    const fetchMock = installFetchMock();
    render(<PhysicsSettings />);

    await waitFor(() => {
      expect(screen.getByText(/spawns 20–40/)).toBeInTheDocument();
    });

    // Main Apply — must NOT carry max_range_km, and min/max_aircraft must
    // be numeric on the wire (the draft default guarantees this even
    // before the operator touches a slider).
    fireEvent.click(screen.getByRole("button", { name: /Apply to Simulator/i }));

    await waitFor(() => {
      const mainApplyCall = fetchMock.mock.calls.find(
        ([, opts]: any) =>
          opts?.method === "PUT" && !("max_range_km" in JSON.parse(opts.body)) &&
          "frac_anomalous" in JSON.parse(opts.body),
      );
      expect(mainApplyCall).toBeTruthy();
      const body = JSON.parse((mainApplyCall as any)[1].body);
      expect(body).not.toHaveProperty("max_range_km");
      expect(typeof body.min_aircraft).toBe("number");
      expect(typeof body.max_aircraft).toBe("number");
      expect(Number.isNaN(body.min_aircraft)).toBe(false);
      expect(Number.isNaN(body.max_aircraft)).toBe(false);
    });

    // Fleet Scene apply — arm, then confirm — MUST carry max_range_km.
    const sceneBtn = screen.getByRole("button", { name: /Apply Scene Change/i });
    fireEvent.click(sceneBtn); // arms
    const confirmBtn = await screen.findByRole("button", { name: /Confirm restart/i });
    fireEvent.click(confirmBtn); // confirms — PUTs

    await waitFor(() => {
      const sceneApplyCall = fetchMock.mock.calls.find(
        ([, opts]: any) =>
          opts?.method === "PUT" && "n_nodes" in JSON.parse(opts.body),
      );
      expect(sceneApplyCall).toBeTruthy();
      const body = JSON.parse((sceneApplyCall as any)[1].body);
      expect(body).toHaveProperty("max_range_km");
      expect(typeof body.max_range_km).toBe("number");
    });
  });
});
