#!/bin/bash
# ── Refresh Cloudflare's published IP ranges ─────────────────────────────────
# Regenerates deploy/cloudflare-ranges.txt from Cloudflare's published lists and
# reports whether anything changed.
#
# The ranges are committed rather than fetched at provisioning time so that two
# runs of setup-server.sh produce the same firewall. That determinism is the
# whole point, and it costs a manual refresh when Cloudflare adds a range.
#
# Automating this refresh is ClickUp 86cb2d022 and is deliberately not done here.
#
# Usage: deploy/refresh-cloudflare-ranges.sh [--check]
#   --check  exit 1 if the committed file is stale; changes nothing (for CI)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

OUT="$(dirname "$0")/cloudflare-ranges.txt"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
    echo "# Cloudflare's published IP ranges. Do not hand-edit."
    echo "# Regenerate with deploy/refresh-cloudflare-ranges.sh"
    echo "# Sources: https://www.cloudflare.com/ips-v4 https://www.cloudflare.com/ips-v6"
    curl -fsS https://www.cloudflare.com/ips-v4
    echo
    curl -fsS https://www.cloudflare.com/ips-v6
    echo
} > "$tmp"

# A truncated or error-page response would otherwise be written straight over a
# good file, and the firewall built from it would lock out the edge.
count="$(grep -cE '^[0-9a-fA-F.:]+/[0-9]+$' "$tmp" || true)"
if [ "$count" -lt 20 ]; then
    echo "✗ Only ${count} CIDRs parsed from Cloudflare; refusing to write." >&2
    echo "  Expected 20+. Check the endpoints by hand before retrying." >&2
    exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    if diff -q "$OUT" "$tmp" >/dev/null 2>&1; then
        echo "✓ ${OUT} is current (${count} CIDRs)"
        exit 0
    fi
    echo "✗ ${OUT} is stale. Run deploy/refresh-cloudflare-ranges.sh to update." >&2
    diff "$OUT" "$tmp" >&2 || true
    exit 1
fi

if diff -q "$OUT" "$tmp" >/dev/null 2>&1; then
    echo "✓ ${OUT} already current (${count} CIDRs)"
else
    diff "$OUT" "$tmp" || true
    cp "$tmp" "$OUT"
    echo "✓ Wrote ${OUT} (${count} CIDRs)"
fi
