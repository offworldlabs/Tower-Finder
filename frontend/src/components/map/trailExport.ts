/**
 * Trail export helpers — turn the per-aircraft `recent_positions`
 * buffer into a CSV file the user can drop into Excel, QGIS, or any
 * GPS visualiser.
 */

export type TrailRow = [number, number, number, number]; // [lat, lon, alt_m, ts_sec]

function csvEscape(s: string): string {
  if (s.includes(",") || s.includes("\"") || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

export function trailToCsv(hex: string, callsign: string | null | undefined, rows: TrailRow[]): string {
  const header = [
    `# Tower-Finder trail export`,
    `# hex=${hex}`,
    `# callsign=${(callsign || "").trim()}`,
    `# exported_at=${new Date().toISOString()}`,
    `# columns: timestamp_iso,lat,lon,alt_ft`,
    "timestamp_iso,lat,lon,alt_ft",
  ];
  const body = rows.map(([lat, lon, alt_m, ts_sec]) => {
    const iso = new Date(ts_sec * 1000).toISOString();
    const altFt = alt_m != null ? Math.round(alt_m / 0.3048) : "";
    return [iso, lat?.toFixed(6) ?? "", lon?.toFixed(6) ?? "", altFt].map((c) => csvEscape(String(c))).join(",");
  });
  return [...header, ...body].join("\n") + "\n";
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
