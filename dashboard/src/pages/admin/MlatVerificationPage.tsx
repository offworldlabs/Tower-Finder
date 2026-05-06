import { useState, useEffect, useRef } from "react";
import { api } from "../../api/client";

const REFRESH_MS = 5000;

function fmt(n: number | undefined | null, decimals = 2): string {
  if (n === undefined || n === null || Number.isNaN(n)) return "—";
  return Number(n).toFixed(decimals);
}

function StatCard({ label, value, unit, hint }: {
  label: string; value: string; unit?: string; hint?: string;
}) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {value}
        {unit && <span style={{ fontSize: "0.6em", color: "var(--text-muted)", marginLeft: 4 }}>{unit}</span>}
      </div>
      {hint && <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

function ErrorRow({ label, stats, unit, decimals = 2 }: {
  label: string;
  stats: { mean_km?: number; median_km?: number; p95_km?: number; max_km?: number;
           mean_ms?: number; median_ms?: number; p95_ms?: number;
           mean_m?: number;  median_m?: number;  p95_m?: number };
  unit: string;
  decimals?: number;
}) {
  const mean   = (stats as any)[`mean_${unit}`];
  const median = (stats as any)[`median_${unit}`];
  const p95    = (stats as any)[`p95_${unit}`];
  const max    = (stats as any)[`max_${unit}`];
  return (
    <div className="stats-grid" style={{ marginBottom: 16 }}>
      <StatCard label={`${label} — mean`}   value={fmt(mean,   decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      <StatCard label={`${label} — median`} value={fmt(median, decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      <StatCard label={`${label} — p95`}    value={fmt(p95,    decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      {max !== undefined && (
        <StatCard label={`${label} — max`}  value={fmt(max,    decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      )}
    </div>
  );
}

function NodeBreakdownTable({ byNodeCount }: {
  byNodeCount: Record<string, { n_samples: number; mean_km: number; median_km: number; p95_km: number; max_km: number }>;
}) {
  const rows = Object.entries(byNodeCount).sort(([a], [b]) => Number(a) - Number(b));
  if (!rows.length) return <div className="empty-state">No samples yet.</div>;
  return (
    <table style={{ width: "100%", borderCollapse: "collapse" }}>
      <thead>
        <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 13 }}>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>Nodes</th>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>Samples</th>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>Mean (km)</th>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>Median (km)</th>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>p95 (km)</th>
          <th style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>Max (km)</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([nc, s]) => (
          <tr key={nc}>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{nc}</td>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{s.n_samples.toLocaleString()}</td>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{fmt(s.mean_km)}</td>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{fmt(s.median_km)}</td>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{fmt(s.p95_km)}</td>
            <td style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)" }}>{fmt(s.max_km)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function MlatVerificationPage() {
  const [verification, setVerification] = useState<any>(null);
  const [accuracy, setAccuracy]         = useState<any>(null);
  const [error, setError]               = useState<string | null>(null);
  const [loading, setLoading]           = useState(true);
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const fetchData = () => {
    Promise.all([api.mlatVerification(), api.mlatAccuracy()])
      .then(([v, a]) => { setVerification(v); setAccuracy(a); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    timerRef.current = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(timerRef.current);
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;
  if (error)   return <div className="empty-state" style={{ color: "var(--error)" }}>Error: {error}</div>;

  const v = verification ?? {};
  const a = accuracy ?? {};
  const matchThresh = v.match_threshold_km;

  return (
    <>
      <div className="page-header">
        <h1>MLAT Verification</h1>
        <p>Solver-vs-truth accuracy across all matched aircraft — auto-refreshes every 5 s</p>
      </div>

      {/* ── Latest snapshot ───────────────────────────────────────── */}
      <h2 style={{ fontSize: 16, marginTop: 24, marginBottom: 12 }}>Latest snapshot</h2>
      <div className="stats-grid" style={{ marginBottom: 16 }}>
        <StatCard label="Solves"          value={(v.n_solves ?? 0).toLocaleString()} />
        <StatCard label="Matched to truth" value={(v.n_matched ?? 0).toLocaleString()}
                  hint={matchThresh ? `≤ ${matchThresh} km from ground truth` : undefined} />
        <StatCard label="Match rate"      value={fmt(v.match_rate_pct, 1)} unit="%" />
      </div>

      {v.position && <ErrorRow label="Position error" stats={v.position} unit="km" />}
      {v.velocity && <ErrorRow label="Velocity error" stats={v.velocity} unit="ms" />}
      {v.altitude && <ErrorRow label="Altitude error" stats={v.altitude} unit="m" decimals={0} />}

      {/* ── Rolling accuracy ──────────────────────────────────────── */}
      <h2 style={{ fontSize: 16, marginTop: 32, marginBottom: 12 }}>
        Rolling accuracy
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: "normal", marginLeft: 8 }}>
          last {(a.n_samples ?? 0).toLocaleString()} matched samples
        </span>
      </h2>

      {a.n_samples > 0 ? (
        <>
          <div className="stats-grid" style={{ marginBottom: 16 }}>
            <StatCard label="Overall mean"   value={fmt(a.mean_km)}   unit="km" />
            <StatCard label="Overall median" value={fmt(a.median_km)} unit="km" />
            <StatCard label="Overall p95"    value={fmt(a.p95_km)}    unit="km" />
            <StatCard label="Overall max"    value={fmt(a.max_km)}    unit="km" />
          </div>

          {a.normal_only?.n_samples > 0 && (
            <>
              <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 8 }}>
                Normal-only (excludes spoofed/anomalous aircraft —
                {" "}{a.normal_only.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid" style={{ marginBottom: 16 }}>
                <StatCard label="Mean"   value={fmt(a.normal_only.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.normal_only.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.normal_only.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.normal_only.max_km)}    unit="km" />
              </div>
            </>
          )}

          {a.good_geometry?.n_samples > 0 && (
            <>
              <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 8 }}>
                Good-geometry only (bistatic angle &lt; {a.good_geometry.bistatic_angle_threshold_deg}° —
                {" "}{a.good_geometry.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid" style={{ marginBottom: 16 }}>
                <StatCard label="Mean"   value={fmt(a.good_geometry.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.good_geometry.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.good_geometry.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.good_geometry.max_km)}    unit="km" />
              </div>
            </>
          )}

          <h3 style={{ fontSize: 14, marginTop: 24, marginBottom: 8 }}>By node count</h3>
          <NodeBreakdownTable byNodeCount={a.by_node_count ?? {}} />
        </>
      ) : (
        <div className="empty-state">No matched samples in the rolling window yet.</div>
      )}
    </>
  );
}
