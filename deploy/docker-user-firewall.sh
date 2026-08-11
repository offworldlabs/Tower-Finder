#!/bin/bash
# ── DOCKER-USER boundary: 80 and 443 reachable only from Cloudflare ──────────
# ufw cannot do this. nginx runs in a container with published ports, and Docker
# publishes a port by writing DNAT rules into nat/PREROUTING and filter rules
# into its own DOCKER chain off FORWARD. ufw's rules live in INPUT, which those
# packets never traverse.
#
# The proof is in production: setup-server.sh allows only 22, 80 and 443 and sets
# `ufw default deny incoming`, yet nodes reach :3012 continuously. They reach it
# because ufw is not in the path.
#
# DOCKER-USER is the chain Docker guarantees it will not rewrite, and it is
# traversed before the DOCKER chain.
#
# Port 3012 is deliberately untouched. Real nodes connect from arbitrary consumer
# addresses behind NAT and CGNAT, so no allowlist could include them. Adding a
# rule for 3012 here would strand the fleet.
#
# Port 80 is narrowed to Cloudflare rather than closed: every server block in the
# nginx template listens on it, including the redirect vhost.
#
# Usage: deploy/docker-user-firewall.sh [ranges-file]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RANGES="${1:-$(dirname "$0")/cloudflare-ranges.txt}"
PORTS="80,443"

if [ ! -f "$RANGES" ]; then
    echo "✗ Ranges file not found: ${RANGES}" >&2
    exit 1
fi

mapfile -t v4 < <(grep -vE '^#|^$' "$RANGES" | grep -E '^[0-9.]+/[0-9]+$')
mapfile -t v6 < <(grep -vE '^#|^$' "$RANGES" | grep -E ':' )

# A short list means a truncated or corrupted file. Applying it would lock the
# edge out of the origin, which is a self-inflicted outage with no external
# cause and no obvious symptom beyond "the site is down".
if [ "${#v4[@]}" -lt 10 ]; then
    echo "✗ Only ${#v4[@]} IPv4 ranges parsed from ${RANGES}; refusing to apply." >&2
    echo "  Expected 10+. Regenerate with deploy/refresh-cloudflare-ranges.sh" >&2
    exit 1
fi

echo "→ Applying DOCKER-USER boundary (${#v4[@]} IPv4, ${#v6[@]} IPv6 ranges)"

# Flush only our own rules. DOCKER-USER may carry rules from elsewhere, and
# `iptables -F DOCKER-USER` would remove those too. Every rule this script adds
# is tagged with a comment, so it can find exactly its own on re-run — which is
# what makes this script idempotent.
TAG="retina-cf-boundary"
while iptables -D DOCKER-USER -m comment --comment "$TAG" -j DROP 2>/dev/null; do :; done
while iptables -D DOCKER-USER -m comment --comment "$TAG" -j RETURN 2>/dev/null; do :; done
for cidr in "${v4[@]}"; do
    while iptables -D DOCKER-USER -s "$cidr" -p tcp -m multiport --dports "$PORTS" \
        -m comment --comment "$TAG" -j RETURN 2>/dev/null; do :; done
done

# Order matters: accept Cloudflare first, then drop everything else on these
# ports. Rules are inserted at the head in reverse so the final order reads
# allow-allow-...-drop.
iptables -I DOCKER-USER 1 -p tcp -m multiport --dports "$PORTS" \
    -m comment --comment "$TAG" -j DROP
for cidr in "${v4[@]}"; do
    iptables -I DOCKER-USER 1 -s "$cidr" -p tcp -m multiport --dports "$PORTS" \
        -m comment --comment "$TAG" -j RETURN
done

# Established connections must survive, or the rule set would cut off in-flight
# responses to the origin's own outbound requests.
iptables -I DOCKER-USER 1 -m conntrack --ctstate ESTABLISHED,RELATED \
    -m comment --comment "$TAG" -j RETURN

echo "✓ DOCKER-USER boundary applied (IPv4)"
iptables -L DOCKER-USER -n --line-numbers | head -5

# IPv6. nginx.conf.template has no `listen [::]` directive, so nginx is IPv4-only
# and Docker publishes no IPv6 path to it — in which case there is nothing to
# filter and ip6tables rules would be theatre. But "no IPv6 path" is a claim
# about the droplet, not about the template, so check rather than assume: if a
# v6 path does exist, an IPv4-only boundary is bypassable and silently so.
if ip6tables -L DOCKER-USER -n >/dev/null 2>&1; then
    v6_published="$(ss -lntH '( sport = :80 or sport = :443 )' 2>/dev/null | grep -c ':::\|\[::\]' || true)"
    if [ "$v6_published" -gt 0 ]; then
        echo "  ! An IPv6 listener on 80/443 was found (${v6_published})." >&2
        echo "    The IPv4 boundary above does not cover it. Applying v6 rules." >&2
        for cidr in "${v6[@]}"; do
            while ip6tables -D DOCKER-USER -s "$cidr" -p tcp -m multiport --dports "$PORTS" \
                -m comment --comment "$TAG" -j RETURN 2>/dev/null; do :; done
        done
        while ip6tables -D DOCKER-USER -m comment --comment "$TAG" -j DROP 2>/dev/null; do :; done
        ip6tables -I DOCKER-USER 1 -p tcp -m multiport --dports "$PORTS" \
            -m comment --comment "$TAG" -j DROP
        for cidr in "${v6[@]}"; do
            ip6tables -I DOCKER-USER 1 -s "$cidr" -p tcp -m multiport --dports "$PORTS" \
                -m comment --comment "$TAG" -j RETURN
        done
        ip6tables -I DOCKER-USER 1 -m conntrack --ctstate ESTABLISHED,RELATED \
            -m comment --comment "$TAG" -j RETURN
        echo "✓ DOCKER-USER boundary applied (IPv6, ${#v6[@]} ranges)"
    else
        echo "  No IPv6 listener on 80/443; IPv4 boundary is sufficient."
    fi
else
    echo "  ip6tables has no DOCKER-USER chain; IPv6 not published by Docker."
fi
