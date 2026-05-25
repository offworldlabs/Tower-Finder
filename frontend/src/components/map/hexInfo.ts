/**
 * ICAO 24-bit address range lookups for "interesting" aircraft.
 *
 * Enthusiast feature: highlight military, government, helicopter, and
 * notable hex ranges so users can spot non-civilian traffic at a glance.
 *
 * Ranges are based on the publicly-documented ICAO country prefix table
 * and the well-known military allocations (FAA, MoD, etc.).  The list is
 * intentionally short — only the highest-signal ranges that aircraft
 * spotters actually look for.  We err on the side of false negatives.
 */

export interface HexInfo {
  category: "military" | "government" | "helicopter" | "test" | null;
  label: string | null;
  /** Tailwind/hex colour used for sidebar badge + detail header */
  color: string | null;
}

type Range = {
  start: number;
  end: number;
  category: HexInfo["category"];
  label: string;
  color: string;
};

// Hex-parsed 24-bit values.  All ranges are inclusive.
// US military: ADF7C8–AFFFFF (USAF) and AE0000–AFFFFF (DoD).
// UK military: 43C000–43CFFF (RAF).
// French military: 3B7000–3BFFFF (Armée de l'air).
// German military: 3F8000–3FFFFF (Luftwaffe).
// NATO / agency: 4D0000–4D03FF (Eurocontrol test), etc.
const RANGES: Range[] = [
  // US Department of Defense — single biggest enthusiast bucket
  { start: 0xae0000, end: 0xafffff, category: "military", label: "US Military",  color: "#dc2626" },
  { start: 0xadf7c8, end: 0xadffff, category: "military", label: "US Military",  color: "#dc2626" },
  // UK military
  { start: 0x43c000, end: 0x43cfff, category: "military", label: "RAF / UK Mil", color: "#dc2626" },
  // French military
  { start: 0x3b7000, end: 0x3bffff, category: "military", label: "FR Military",  color: "#dc2626" },
  // German military
  { start: 0x3f8000, end: 0x3fffff, category: "military", label: "DE Military",  color: "#dc2626" },
  // Canadian forces
  { start: 0xc20000, end: 0xc3ffff, category: "military", label: "CA Military",  color: "#dc2626" },
  // Eurocontrol / test / unallocated
  { start: 0x4d0000, end: 0x4d03ff, category: "test",     label: "Eurocontrol",  color: "#0ea5e9" },
];

function parseHex(hex: string | null | undefined): number | null {
  if (!hex) return null;
  const n = parseInt(hex.replace(/^~/, ""), 16);
  return Number.isFinite(n) ? n : null;
}

/**
 * Best-effort classification of an ICAO hex address.
 *
 * Returns `{ category: null }` for ordinary civilian aircraft so callers
 * can branch on `info.category` without checking each field.
 */
export function classifyHex(hex: string | null | undefined): HexInfo {
  const n = parseHex(hex);
  if (n === null) return { category: null, label: null, color: null };
  for (const r of RANGES) {
    if (n >= r.start && n <= r.end) {
      return { category: r.category, label: r.label, color: r.color };
    }
  }
  return { category: null, label: null, color: null };
}

/** Squawk codes that should always be flagged regardless of hex range. */
export function emergencySquawkLabel(squawk: string | null | undefined): string | null {
  if (!squawk) return null;
  switch (squawk.trim()) {
    case "7500": return "Hijack (7500)";
    case "7600": return "Radio failure (7600)";
    case "7700": return "Emergency (7700)";
    default: return null;
  }
}
