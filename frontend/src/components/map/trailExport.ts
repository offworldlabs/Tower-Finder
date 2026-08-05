/**
 * Trail export helpers — turn the per-aircraft trail buffer into CSV the
 * user can drop into Excel, QGIS, or any GPS visualiser.
 *
 * The frontend's `trailsRef` stores rows as `[lat, lon, alt_baro_ft, ts_ms]`
 * (matches `mergeTrailPositions` in hooks.ts). `frontendTrailsRef` stores
 * `[lat, lon, ts_sec]` (no altitude). We accept both via `normaliseRow`.
 */


function csvEscape(s: string): string {
  if (s.includes(",") || s.includes("\"") || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function normaliseRow(row: number[]): [number, number, number | null, number] {
  if (!row || row.length < 3) return [row?.[0], row?.[1], null, Date.now()];
  if (row.length === 3) {
    const ts = row[2];
    return [row[0], row[1], null, ts > 1e12 ? ts : ts * 1000];
  }
  const ts = row[3];
  const tsMs = ts > 1e12 ? ts : ts * 1000;
  return [row[0], row[1], row[2], tsMs];
}

export function trailToCsv(
  hex: string,
  callsign: string | null | undefined,
  rows: number[][],
): string {
  const header = [
    `# Tower-Finder trail export`,
    `# hex=${hex}`,
    `# callsign=${(callsign || "").trim()}`,
    `# exported_at=${new Date().toISOString()}`,
    `# columns: timestamp_iso,hex,callsign,lat,lon,alt_ft`,
    "timestamp_iso,hex,callsign,lat,lon,alt_ft",
  ];
  const cs = (callsign || "").trim();
  const body = (rows || []).map((raw) => {
    const [lat, lon, alt, tsMs] = normaliseRow(raw);
    const iso = new Date(tsMs).toISOString();
    const altOut = alt == null ? "" : String(Math.round(alt));
    return [iso, hex, cs, lat?.toFixed(6) ?? "", lon?.toFixed(6) ?? "", altOut]
      .map((c) => csvEscape(String(c)))
      .join(",");
  });
  return [...header, ...body].join("\n") + "\n";
}

/** Dump every supplied aircraft's trail into one CSV. */
export function trailsToBulkCsv(
  aircraft: Array<{ hex: string; flight?: string | null }>,
  trails: Record<string, number[][]>,
): string {
  const header = [
    `# Tower-Finder bulk trail export`,
    `# exported_at=${new Date().toISOString()}`,
    `# aircraft=${aircraft.length}`,
    `# columns: timestamp_iso,hex,callsign,lat,lon,alt_ft`,
    "timestamp_iso,hex,callsign,lat,lon,alt_ft",
  ];
  const lines: string[] = [];
  for (const ac of aircraft) {
    const rows = trails[ac.hex];
    if (!rows?.length) continue;
    const cs = (ac.flight || "").trim();
    for (const raw of rows) {
      const [lat, lon, alt, tsMs] = normaliseRow(raw);
      const iso = new Date(tsMs).toISOString();
      const altOut = alt == null ? "" : String(Math.round(alt));
      lines.push(
        [iso, ac.hex, cs, lat?.toFixed(6) ?? "", lon?.toFixed(6) ?? "", altOut]
          .map((c) => csvEscape(String(c)))
          .join(","),
      );
    }
  }
  return [...header, ...lines].join("\n") + "\n";
}

/** Trigger a browser download for the supplied CSV text. */
export function downloadCsv(filename: string, csv: string) {
  if (typeof window === "undefined") return;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
