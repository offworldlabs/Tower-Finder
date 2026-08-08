/**
 * Pure formatting/shaping helpers for the Physics tab's Solver Report panel.
 * Kept dependency-free from React so they're trivial to unit test — all the
 * JSX lives in PhysicsSettings.tsx, this file only turns
 * GET /api/test/solver-stats payloads into render-ready shapes.
 */

export interface SolverStats {
  window_minutes: number;
  attempts: number;
  published: { total: number; n2: number; n3plus: number };
  rejects: { total: number; by_reason: Record<string, number> };
  position_error_km: { median: number | null; p90: number | null; n: number };
  ghosts: {
    live_tracks: number;
    adsb_associated: number;
    gt_matched: number;
    ghost_tracks: number;
    precision_pct: number;
  };
  consensus: {
    mode: string;
    selected: number;
    filtered: number;
    fallback: number;
    shadow: number;
  };
  counters: {
    successes: number;
    failures: number;
    n2_unconfirmed: number;
    solver_trimmed: number;
    stale_drops: number;
    queue_drops: number;
  };
}

/** "0.42 km" / "—" for null (no published solves inside the error gate). */
export function formatKm(v: number | null | undefined): string {
  return v != null ? `${v.toFixed(2)} km` : "—";
}

/** "91.7%" / "—" for null. */
export function formatPct(v: number | null | undefined, digits = 1): string {
  return v != null ? `${v.toFixed(digits)}%` : "—";
}

export interface FunnelSegment {
  key: "n2" | "n3plus" | "rejected";
  label: string;
  count: number;
  /** Share of (published + rejected) attempts, 0–100. 0 when there's nothing yet. */
  pct: number;
  color: string;
}

const FUNNEL_COLORS: Record<FunnelSegment["key"], string> = {
  n2: "#38bdf8",
  n3plus: "#a78bfa",
  rejected: "#f43f5e",
};

/**
 * Three-way funnel split for the stacked bar: published n=2, published
 * n>=3, rejected. Order matches the legend and the color spec.
 */
export function funnelSegments(
  stats: Pick<SolverStats, "published" | "rejects">,
): FunnelSegment[] {
  const total = stats.published.n2 + stats.published.n3plus + stats.rejects.total;
  const seg = (key: FunnelSegment["key"], label: string, count: number): FunnelSegment => ({
    key,
    label,
    count,
    pct: total > 0 ? (count / total) * 100 : 0,
    color: FUNNEL_COLORS[key],
  });
  return [
    seg("n2", "Published n=2", stats.published.n2),
    seg("n3plus", "Published n≥3", stats.published.n3plus),
    seg("rejected", "Rejected", stats.rejects.total),
  ];
}

export interface RejectReasonBar {
  reason: string;
  count: number;
  /** Width of the mini-bar relative to the largest reason, 0–100. */
  pct: number;
}

/** Reject reasons sorted desc by count, scaled for a proportional mini-bar row each. */
export function rejectReasonBars(byReason: Record<string, number>): RejectReasonBar[] {
  const entries = Object.entries(byReason).sort((a, b) => b[1] - a[1]);
  const max = entries.length ? entries[0][1] : 0;
  return entries.map(([reason, count]) => ({
    reason,
    count,
    pct: max > 0 ? (count / max) * 100 : 0,
  }));
}

/** Display label for the consensus mode badge. */
export function consensusModeLabel(mode: string): string {
  if (mode === "active") return "Active";
  if (mode === "shadow") return "Shadow";
  return "Off";
}
