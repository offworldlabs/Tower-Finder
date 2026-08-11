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
TAG="retina-cf-boundary"

if [ ! -f "$RANGES" ]; then
    echo "✗ Ranges file not found: ${RANGES}" >&2
    exit 1
fi

# Trim leading/trailing whitespace before classifying each line. Without this a
# line with a trailing space matches neither the IPv4 nor the IPv6 pattern and
# is silently dropped from both arrays — a corruption that a shorter-than-usual
# count would catch, but a single dropped line among many would not.
RANGES_CLEAN="$(grep -vE '^#|^$' "$RANGES" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
mapfile -t v4 < <(printf '%s\n' "$RANGES_CLEAN" | grep -E '^[0-9.]+/[0-9]+$')
mapfile -t v6 < <(printf '%s\n' "$RANGES_CLEAN" | grep -E ':')

# A short list means a truncated or corrupted file. Applying it would lock the
# edge out of the origin, which is a self-inflicted outage with no external
# cause and no obvious symptom beyond "the site is down".
if [ "${#v4[@]}" -lt 10 ]; then
    echo "✗ Only ${#v4[@]} IPv4 ranges parsed from ${RANGES}; refusing to apply." >&2
    echo "  Expected 10+. Regenerate with deploy/refresh-cloudflare-ranges.sh" >&2
    exit 1
fi

# Surface a missing chain as a diagnosis, not a raw "iptables: No chain/target/
# match by that name" error from the first -D/-I call below. DOCKER-USER is
# created by Docker at startup, so its absence means Docker is not installed,
# not running, or has not yet created its chains.
if ! iptables -L DOCKER-USER -n >/dev/null 2>&1; then
    echo "✗ DOCKER-USER chain not found (iptables -L DOCKER-USER failed)." >&2
    echo "  This chain is created by Docker at startup — is docker.service running?" >&2
    exit 1
fi

echo "→ Applying DOCKER-USER boundary (${#v4[@]} IPv4, ${#v6[@]} IPv6 ranges)"

# Delete every rule in the given chain (iptables or ip6tables, on DOCKER-USER)
# that carries our "$TAG" comment, by line number, repeating until none remain.
#
# `iptables -D <full-rule-spec>` requires an exact match against the rule as the
# kernel stored it — get the argument order, or a missing match module, subtly
# wrong and the delete silently fails to match nothing, leaving stale rules
# behind on every re-run. That is what happened before this fix: the DROP
# deletion omitted `-p tcp -m multiport --dports "$PORTS"` and the conntrack
# RETURN rule had no deletion loop at all, so the chain grew by two rules per
# boot despite the script claiming to be idempotent.
#
# Deleting by line number instead only requires the tag comment to be present,
# which is true of every rule-spec this script inserts (DROP, per-CIDR RETURN,
# and the conntrack RETURN) regardless of its shape, so one function covers all
# three. It is also what keeps this safe: the tag comment is a literal string
# unique to rules this script wrote. Docker's own trailing `-j RETURN` in
# DOCKER-USER carries no comment at all, and any rule a third party added would
# carry a different (or no) comment — neither ever contains the exact substring
# "/* $TAG */", so grep -F never selects them and they are never deleted.
delete_tagged_rules() {
    local ipt="$1"
    local line
    while line="$("$ipt" -L DOCKER-USER -n --line-numbers 2>/dev/null \
                  | grep -F "/* ${TAG} */" | head -1 | awk '{print $1}')" \
          && [ -n "$line" ]; do
        "$ipt" -D DOCKER-USER "$line"
    done
}

delete_tagged_rules iptables

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
iptables -L DOCKER-USER -n --line-numbers | head -5 || true

# IPv6. nginx.conf.template has no `listen [::]` directive, so nginx is IPv4-only
# and Docker publishes no IPv6 path to it — in which case there is nothing to
# filter and ip6tables rules would be theatre. But "no IPv6 path" is a claim
# about the droplet, not about the template, so check rather than assume: if a
# v6 path does exist, an IPv4-only boundary is bypassable and silently so.
if ip6tables -L DOCKER-USER -n >/dev/null 2>&1; then
    # The `ss` probe answers "a listener exists" and "the probe could not tell
    # me" with the same shape of output (a count of zero) unless we look at how
    # it failed. Treat "ss is missing" and "ss errored" as distinct from "ss
    # ran cleanly and found nothing", and in the uncertain case apply the IPv6
    # boundary anyway rather than print a reassuring "sufficient" message that
    # may be wrong: an unnecessary set of IPv6 rules is inert, but a skipped
    # set on a box that does publish IPv6 is a silent bypass of the boundary.
    if ! command -v ss >/dev/null 2>&1; then
        echo "  ! ss not found — cannot determine whether nginx has an IPv6" >&2
        echo "    listener on 80/443. Applying the IPv6 boundary defensively." >&2
        v6_published=1
    elif ! ss_output="$(ss -lntH '( sport = :80 or sport = :443 )' 2>&1)"; then
        echo "  ! ss failed (${ss_output}) — cannot determine whether nginx has" >&2
        echo "    an IPv6 listener on 80/443. Applying the IPv6 boundary defensively." >&2
        v6_published=1
    else
        v6_published="$(printf '%s\n' "$ss_output" | grep -c ':::\|\[::\]' || true)"
    fi

    if [ "$v6_published" -gt 0 ]; then
        echo "  ! An IPv6 listener on 80/443 was found or assumed (${v6_published})." >&2
        echo "    The IPv4 boundary above does not cover it. Applying v6 rules." >&2

        # Same short-list protection as IPv4: a truncated file with no v6 lines
        # would otherwise insert a bare DROP with no allow rules above it,
        # blackholing Cloudflare over IPv6 rather than leaving it unfiltered.
        if [ "${#v6[@]}" -lt 5 ]; then
            echo "✗ Only ${#v6[@]} IPv6 ranges parsed from ${RANGES}; refusing to apply" >&2
            echo "  IPv6 rules. Expected 5+. Regenerate with deploy/refresh-cloudflare-ranges.sh" >&2
            exit 1
        fi

        delete_tagged_rules ip6tables
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
