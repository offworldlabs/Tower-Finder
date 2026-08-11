import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
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

function installFetchMock(configRef?: { current: Record<string, unknown> }) {
  const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
    const u = String(url);
    if (u.includes("/simulation/config")) {
      if (opts?.method === "PUT") {
        return Promise.resolve({
          ok: true,
          json: async () => ({ ok: true, config: configRef?.current ?? BARE_CONFIG }),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        json: async () => configRef?.current ?? BARE_CONFIG,
      } as Response);
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

// Every range input on the page, in DOM order:
// [0] anomalous, [1] drone, [2] dark, [3] total objects target, then scene.
function ranges() {
  return Array.from(
    document.querySelectorAll('input[type="range"]'),
  ) as HTMLInputElement[];
}

describe("PhysicsSettings", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
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

  // ── Drift detection ───────────────────────────────────────────────────
  // A backend restart drops simulation_config back to boot state and
  // re-stamps _updated_at at import. The drafts used to seed once and never
  // re-sync, so the sliders kept showing an applied scene while the
  // simulator had already been pushed back to defaults.

  const APPLIED = {
    frac_anomalous: 0.0,
    frac_drone: 0.30,
    frac_dark: 0.15,
    min_aircraft: 64,
    max_aircraft: 80,
    _updated_at: 2000,
    ground_truth_counts: { total: 0, anomalous: 0, drone: 0, aircraft: 0 },
  };

  // What a restarted backend serves: boot defaults, stamped at import — an
  // earlier wall-clock than the operator's PUT, so the stamp moves backwards.
  const AFTER_RESTART = {
    frac_anomalous: 0.0,
    frac_drone: 0.0,
    frac_dark: 0.15,
    _updated_at: 1000,
    ground_truth_counts: { total: 0, anomalous: 0, drone: 0, aircraft: 0 },
  };

  async function pollOnce() {
    // The config poll is on a 10 s interval; advance past one tick and let
    // the fetch promises settle inside act.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
  }

  it("re-syncs the sliders when the backend reverts and there are no unsaved edits", async () => {
    const configRef = { current: APPLIED as Record<string, unknown> };
    installFetchMock(configRef);
    vi.useFakeTimers();
    render(<PhysicsSettings />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(ranges()[1].value).toBe("30");   // drone, as applied
    expect(ranges()[3].value).toBe("80");   // total objects target

    configRef.current = AFTER_RESTART;
    await pollOnce();

    // Sliders now show what the simulator is actually running, not the
    // stale applied values.
    expect(ranges()[1].value).toBe("0");
    expect(ranges()[3].value).toBe("40");   // key absent → documented fallback
    expect(document.body.textContent).toMatch(/reverted to boot state/);
    expect(document.body.textContent).toMatch(/actually running/);

    vi.useRealTimers();
  });

  it("keeps unapplied edits when the backend reverts, and warns instead", async () => {
    const configRef = { current: APPLIED as Record<string, unknown> };
    installFetchMock(configRef);
    vi.useFakeTimers();
    render(<PhysicsSettings />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.change(ranges()[1], { target: { value: "25" } });
    expect(ranges()[1].value).toBe("25");

    configRef.current = AFTER_RESTART;
    await pollOnce();

    // The operator's in-progress edit is theirs — never overwritten.
    expect(ranges()[1].value).toBe("25");
    expect(document.body.textContent).toMatch(/no longer sit on top of what/);
    expect(screen.getByRole("button", { name: /Reload from simulator/i })).toBeInTheDocument();

    vi.useRealTimers();
  });

  it("does not flag drift for the page's own apply", async () => {
    const configRef = { current: APPLIED as Record<string, unknown> };
    installFetchMock(configRef);
    vi.useFakeTimers();
    render(<PhysicsSettings />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.change(ranges()[1], { target: { value: "25" } });

    // The PUT echoes the new config back with a fresh stamp; the poll that
    // follows must read that as our own write, not somebody else's.
    configRef.current = { ...APPLIED, frac_drone: 0.25, _updated_at: 3000 };
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply to Simulator/i }));
      await vi.advanceTimersByTimeAsync(0);
    });
    await pollOnce();

    expect(document.body.textContent).not.toMatch(/changed outside this page/);

    vi.useRealTimers();
  });
});
