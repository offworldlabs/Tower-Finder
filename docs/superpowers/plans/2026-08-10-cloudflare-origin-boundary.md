# Cloudflare Origin Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production and staging droplets reachable only through Cloudflare, so the per-IP rate limiter is meaningful and the origin cannot be addressed directly.

**Architecture:** Two independent controls that fail at different layers. `ssl_verify_client` in the shared nginx TLS snippet rejects any TLS handshake not presenting Cloudflare's client certificate. `DOCKER-USER` iptables rules refuse TCP on 80/443 from outside Cloudflare's published ranges — necessary because nginx runs in a container whose published ports bypass ufw's `INPUT` chain entirely. A committed range file feeds both nginx and the firewall so they cannot drift.

**Tech Stack:** nginx (in-container, config rendered by `deploy/render-nginx-config.py`), iptables `DOCKER-USER` chain, systemd oneshot unit, bash provisioning scripts, Python 3.12 for the parity check.

**Spec:** `docs/superpowers/specs/2026-08-10-cloudflare-origin-boundary-design.md`

## Global Constraints

- **Baseline is `origin/main` at `ad119b1`.** Work happens in the worktree at `.claude/worktrees/cloudflare-origin-boundary` on branch `worktree-cloudflare-origin-boundary`.
- **`backend/.env` must exist** for any `docker compose config` to resolve. It is gitignored. Run `touch backend/.env` once before running `deploy/check-env-parity.py`. Never commit it.
- **Rendered nginx configs for staging and production must stay byte-identical** after hostname tokenisation. `deploy/check-env-parity.py` enforces this. No per-environment flag for `ssl_verify_client` is possible.
- **`EXPECTED_TLS_VHOSTS = 7`** (`deploy/check-env-parity.py:55`). Every assertion counting TLS server blocks uses this constant, never a literal.
- **Never commit droplet IP addresses.** They were scrubbed from `docs/runbook.md` on 2026-08-06. Use placeholders in docs and shell variables in scripts.
- **Port 3012 must remain open to the world.** Production nodes connect from arbitrary consumer addresses behind CGNAT. Any rule touching 3012 is out of scope and is a defect.
- **Port 80 stays open to Cloudflare**, not closed. Every server block in the template listens on it, including the redirect vhost.
- **The laptop overlay (`docker-compose.local.yml`) renders plain HTTP** via `TLS_ENABLED=false` and must keep working. It has no Cloudflare origin certificate.
- **Cloudflare's origin-pull CA is a static, publicly published certificate**, fetched from `https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem`. It is not per-zone and not a secret.
- **Cloudflare's published ranges** come from `https://www.cloudflare.com/ips-v4` and `https://www.cloudflare.com/ips-v6`. Verified 2026-08-10: 22 CIDRs, identical to the 22 `set_real_ip_from` entries already in `deploy/nginx-security.conf`.
- **`deploy/docker-user-firewall.sh` uses `mapfile`, which needs bash 4+.** The droplets run Ubuntu and are fine. macOS ships bash 3.2, where `mapfile` does not exist and the script fails at runtime with `command not found` — `bash -n` will not catch it because the syntax is valid. Test that script on a droplet or in a Linux container, not on a laptop.

## Task Ordering and Dependencies

Tasks 1–3 are repo changes and can be reviewed and merged as one PR. Task 4 is provisioning, applied by hand per droplet, and is independent of 1–3 — it can land before or after them. Task 5 is the dashboard action and **must be complete and soaked before Task 3's change reaches any droplet**.

Recommended sequence: Task 5 (day 1, soak) → Tasks 1, 2, 4 (any order) → Task 3 (day 2) → merge.

---

### Task 1: Pin the origin boundary in the parity check

Write the failing assertion first. This is the regression test for the whole change: without it, deleting `ssl_verify_client` from the shared snippet leaves staging and production identical and equally unprotected, so the parity diff passes.

**Files:**
- Modify: `deploy/check-env-parity.py:217-229`

**Interfaces:**
- Consumes: `EXPECTED_TLS_VHOSTS` (module constant, `int`, currently `7`), `rendered` (`dict[str, str]` mapping environment name to rendered nginx config), `problems` (`list[str]`).
- Produces: nothing consumed by later tasks. Task 3 makes this assertion pass.

- [ ] **Step 1: Write the failing assertion**

In `deploy/check-env-parity.py`, inside the `for env, text in rendered.items():` loop that begins at line 218, after the existing `Strict-Transport-Security` check, add:

```python
        # Authenticated Origin Pulls. Absolute for the same reason as the TLS
        # count above: this lives in one shared snippet, so deleting it would
        # drop the boundary from both environments at once and leave them in
        # perfect parity with each other. See the 2026-08-10 origin-boundary
        # spec; the firewall half of that boundary cannot be asserted from here.
        verify = text.count("ssl_verify_client on;")
        if verify != EXPECTED_TLS_VHOSTS:
            problems.append(
                f"  {env}: {verify} `ssl_verify_client on` directives, expected "
                f"{EXPECTED_TLS_VHOSTS}. Every TLS vhost must reject handshakes "
                f"that do not present Cloudflare's client certificate."
            )
        if "ssl_client_certificate " not in text:
            problems.append(
                f"  {env}: rendered config has no ssl_client_certificate "
                f"directive, so ssl_verify_client has no CA to verify against."
            )
```

- [ ] **Step 2: Run the check to verify it fails**

Run: `touch backend/.env && python3 deploy/check-env-parity.py`

Expected: exit status 1, output naming `production`, `staging` and `test`, each with `0 ssl_verify_client on directives, expected 7` and `rendered config has no ssl_client_certificate directive`.

Note: all three environments appear, production included. The `env == REFERENCE` skip exists only in the later diff-printing loop, not in this assertion loop — production is checked absolutely, like the others. If the command exits 0, the assertion is not wired into the `problems` list; check that the indentation places it inside the `for` loop.

- [ ] **Step 3: Commit the failing assertion**

```bash
git add deploy/check-env-parity.py
git commit -m "test: assert every TLS vhost verifies Cloudflare's client cert

Absolute assertion rather than a comparison, matching the existing TLS-count
and HSTS checks. ssl_verify_client lives in one shared snippet, so removing it
would drop the boundary from both environments and leave the parity diff clean.

Fails until the tls.conf change lands."
```

---

### Task 2: Commit Cloudflare's ranges as one source, with a refresh script

**Files:**
- Create: `deploy/cloudflare-ranges.txt`
- Create: `deploy/refresh-cloudflare-ranges.sh`

**Interfaces:**
- Produces: `deploy/cloudflare-ranges.txt` — newline-delimited CIDRs, IPv4 first then IPv6, `#`-prefixed comment lines allowed. Task 4's firewall script reads it with `grep -vE '^#|^$'`, then splits IPv4 from IPv6 with `grep -E '^[0-9.]+/[0-9]+$'` and `grep -E ':'` respectively.

- [ ] **Step 1: Write the refresh script**

Create `deploy/refresh-cloudflare-ranges.sh`:

```bash
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
```

- [ ] **Step 2: Make it executable and generate the file**

```bash
chmod +x deploy/refresh-cloudflare-ranges.sh
touch deploy/cloudflare-ranges.txt
deploy/refresh-cloudflare-ranges.sh
```

Expected: `✓ Wrote deploy/cloudflare-ranges.txt (N CIDRs)` with N of roughly 22.

- [ ] **Step 3: Verify the guard rejects a truncated fetch**

```bash
cp deploy/cloudflare-ranges.txt /tmp/cf-ranges-good.txt
printf '1.2.3.0/24\n' > /tmp/cf-fake.txt
```

Confirm by reading the script that a response yielding fewer than 20 CIDRs exits 1 before writing. Then re-run `deploy/refresh-cloudflare-ranges.sh --check`.

Expected: `✓ deploy/cloudflare-ranges.txt is current`.

- [ ] **Step 4: Confirm the committed ranges match nginx's**

The 22 `set_real_ip_from` entries in `deploy/nginx-security.conf:19-40` should be the same set. Compare:

```bash
grep -oE '^set_real_ip_from ([0-9a-fA-F.:]+/[0-9]+);' deploy/nginx-security.conf \
  | sed 's/set_real_ip_from //; s/;//' | sort > /tmp/nginx-ranges.txt
grep -vE '^#|^$' deploy/cloudflare-ranges.txt | sort > /tmp/file-ranges.txt
diff /tmp/nginx-ranges.txt /tmp/file-ranges.txt && echo "IDENTICAL"
```

Expected: `IDENTICAL`. If they differ, Cloudflare has changed its ranges since #146 landed. Do not silently reconcile — report the diff, because it means the deployed nginx is also stale and that is a separate fix.

- [ ] **Step 5: Commit**

```bash
git add deploy/cloudflare-ranges.txt deploy/refresh-cloudflare-ranges.sh
git commit -m "deploy: commit Cloudflare's ranges as one source for nginx and the firewall

Committed rather than fetched at provisioning time so two runs of
setup-server.sh produce the same firewall. The refresh script guards against
writing a truncated or error-page response over a good file, which would
otherwise build a firewall that locks out the edge.

Automating the refresh is 86cb2d022 and is not done here."
```

---

### Task 3: Require Cloudflare's client certificate on every TLS vhost

**Files:**
- Modify: `deploy/nginx/snippets/tls.conf`
- Modify: `deploy/start.sh:44-60`

**Interfaces:**
- Consumes: `EXPECTED_TLS_VHOSTS` assertion from Task 1.
- Produces: `/etc/ssl/cloudflare/origin-pull-ca.pem` as a required file on every TLS-serving droplet. Task 4 does not depend on it.

**Precondition:** Task 5 must be complete and soaked. If Authenticated Origin Pulls is not enabled for the zone, Cloudflare sends no client certificate and every request 400s at the origin the moment this deploys.

- [ ] **Step 1: Add the verification directives to the shared TLS snippet**

Append to `deploy/nginx/snippets/tls.conf`:

```nginx

# Authenticated Origin Pulls. Cloudflare presents a client certificate signed by
# its origin-pull CA on every connection to the origin; anything that cannot is
# not Cloudflare and is refused at the handshake.
#
# This must not be enabled before Authenticated Origin Pulls is turned on for the
# zone in the Cloudflare dashboard — the edge would present nothing and every
# request, including yours, would 400 here.
#
# This is only half the boundary. It rejects during the TLS handshake, after the
# TCP connection is accepted, so a direct connection to the droplet still
# completes at the transport layer. Refusing that is the DOCKER-USER firewall's
# job (deploy/setup-server.sh); neither control is sufficient alone.
ssl_client_certificate /etc/ssl/cloudflare/origin-pull-ca.pem;
ssl_verify_client on;
```

- [ ] **Step 2: Run the parity check to verify Task 1's assertion now passes**

Run: `python3 deploy/check-env-parity.py`

Expected: exit 0, `in parity with production (compose + nginx): staging, test`.

- [ ] **Step 3: Add the named precondition check to start.sh**

`nginx -t` at `deploy/start.sh:59` already fails on a missing `ssl_client_certificate` file, so the container fails closed. What it does not do is say why. In `deploy/start.sh`, immediately before the `python3 /app/deploy/render-nginx-config.py` invocation at line 54, add:

```bash
# Fail with the actual cause rather than a generic nginx config error. `nginx -t`
# below does catch this, but names only the directive. Mirrors how
# setup-server.sh refuses to run without the origin certificate.
#
# Deliberately fail-closed: a missing CA must never degrade to serving TLS
# without verification, because that is indistinguishable from the boundary
# working.
if [ "${TLS_ENABLED:-true}" != "false" ] && [ ! -f /etc/ssl/cloudflare/origin-pull-ca.pem ]; then
    echo "[start.sh] ✗ /etc/ssl/cloudflare/origin-pull-ca.pem is missing." >&2
    echo "[start.sh]   nginx is configured for Authenticated Origin Pulls and" >&2
    echo "[start.sh]   cannot start without Cloudflare's origin-pull CA. Fetch it:" >&2
    echo "[start.sh]     curl -fsS -o /etc/ssl/cloudflare/origin-pull-ca.pem \\" >&2
    echo "[start.sh]       https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem" >&2
    exit 1
fi
```

- [ ] **Step 4: Verify the laptop path is unaffected**

The laptop sets `TLS_ENABLED=false`, so the guard is skipped and the renderer takes the plain-HTTP branch, never emitting `ssl_verify_client`. Confirm:

```bash
python3 deploy/render-nginx-config.py \
  deploy/nginx/nginx.conf.template /tmp/local.conf \
  --env-file <(printf 'TLS_ENABLED=false\nHOST_MAIN=a\nHOST_API=b\nHOST_MAP=c\nHOST_DASH=d\nHOST_ADMIN=e\nHOST_TESTMAP=f\nHOST_LEGACY_REDIRECT=g\nCSP_CONNECT_SRC=h\n')
grep -c "ssl_verify_client" /tmp/local.conf
```

Expected: `0`.

- [ ] **Step 5: Verify a deployed render has all seven**

```bash
python3 deploy/render-nginx-config.py \
  deploy/nginx/nginx.conf.template /tmp/prod.conf \
  --env-file <(printf 'HOST_MAIN=a\nHOST_API=b\nHOST_MAP=c\nHOST_DASH=d\nHOST_ADMIN=e\nHOST_TESTMAP=f\nHOST_LEGACY_REDIRECT=g\nCSP_CONNECT_SRC=h\n')
grep -c "ssl_verify_client on;" /tmp/prod.conf
```

Expected: `7`.

- [ ] **Step 6: Commit**

```bash
git add deploy/nginx/snippets/tls.conf deploy/start.sh
git commit -m "deploy: require Cloudflare's client certificate on every TLS vhost

Half of the origin boundary. Rejects at the TLS handshake anything that cannot
present a certificate signed by Cloudflare's origin-pull CA. The other half is
the DOCKER-USER firewall, which refuses the TCP connection outright — this one
accepts the connection and fails the handshake, so neither is sufficient alone.

start.sh names the missing CA rather than letting it surface as a generic nginx
config error. Fail-closed by design: serving unverified TLS would be
indistinguishable from the boundary working.

Requires Authenticated Origin Pulls enabled for the zone first."
```

---

### Task 4: Refuse non-Cloudflare TCP on 80 and 443

Provisioning, not deploy. Applied by hand per droplet, staging first.

**Files:**
- Create: `deploy/docker-user-firewall.sh`
- Create: `deploy/retina-firewall.service`
- Modify: `deploy/setup-server.sh:55-67`

**Interfaces:**
- Consumes: `deploy/cloudflare-ranges.txt` from Task 2.
- Produces: a `DOCKER-USER` ruleset and a `retina-firewall.service` systemd unit. Nothing later depends on either.

- [ ] **Step 1: Write the firewall script**

Create `deploy/docker-user-firewall.sh`:

```bash
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
```

- [ ] **Step 2: Write the systemd unit**

Create `deploy/retina-firewall.service`:

```ini
[Unit]
# Docker recreates its chains when it starts, so these rules must be applied
# after it, on every boot. Without this the boundary silently disappears on the
# first reboot and the origin is wide open with nothing to indicate it.
Description=RETINA DOCKER-USER Cloudflare boundary
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/tower-finder/deploy/docker-user-firewall.sh
# A failed firewall must be loud. Without this the unit fails quietly at boot and
# the origin serves the whole internet.
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3a: Leave a pointer in the ufw block**

The firewall wiring cannot go next to the ufw rules, even though that is where a
reader looks for it: `setup-server.sh` sets `APP_DIR` at line 117 and the repo
does not reach disk until roughly line 140, so the scripts do not exist yet at
line 67. With `set -euo pipefail` at line 15, referencing `${APP_DIR}` there is a
hard failure, not a silent one.

In `deploy/setup-server.sh`, immediately after `ufw --force enable` at line 67, add
a comment only — no commands:

```bash
# ufw governs host services only: SSH on 22, and anything added later running
# outside a container. It cannot govern nginx, whose ports are published by
# Docker and never traverse INPUT — see deploy/docker-user-firewall.sh. That
# boundary is applied in section 4, once the repo carrying the script is on disk.
#
# The separation is also what makes the DOCKER-USER rules safe to get wrong: they
# cannot lock anyone out of SSH, which stays governed by the rules above.
```

- [ ] **Step 3b: Wire it in after the repo is on disk**

In `deploy/setup-server.sh`, immediately **before** the `cp deploy/env."${RETINA_TARGET_ENV}".example .env` line (currently line 147), add:

```bash
# The origin boundary. Applied here rather than beside the ufw rules because it
# needs the scripts this repo carries, which only reached disk a few lines above.
echo ""
echo "→ Applying the Cloudflare origin boundary (DOCKER-USER)..."
chmod +x "${APP_DIR}/deploy/docker-user-firewall.sh"
cp "${APP_DIR}/deploy/retina-firewall.service" /etc/systemd/system/retina-firewall.service
systemctl daemon-reload
systemctl enable retina-firewall.service
systemctl start retina-firewall.service
systemctl --no-pager status retina-firewall.service | head -5
```

The unit's `ExecStart` points at `/opt/tower-finder/deploy/docker-user-firewall.sh`,
which is `${APP_DIR}/deploy/docker-user-firewall.sh` — the same file. No copy is
needed, only the executable bit.

- [ ] **Step 4: Make scripts executable and commit**

```bash
chmod +x deploy/docker-user-firewall.sh
git add deploy/docker-user-firewall.sh deploy/retina-firewall.service deploy/setup-server.sh
git commit -m "deploy: refuse non-Cloudflare TCP on 80 and 443 via DOCKER-USER

ufw cannot do this. Container-published ports are DNAT'd into Docker's own
chains and never traverse INPUT — which is why nodes reach :3012 today despite
ufw denying it. The rules go in DOCKER-USER instead.

3012 stays open deliberately: nodes connect from arbitrary CGNAT addresses and
any allowlist would strand the fleet.

A systemd oneshot reapplies the rules after docker.service on every boot,
because Docker recreates its chains on start and the boundary would otherwise
vanish on first reboot with nothing to show for it."
```

- [ ] **Step 5: Apply to staging and verify**

On the staging droplet, with `$STAGING_IP` set locally and never committed:

```bash
ssh <staging-host> 'cd /opt/tower-finder && sudo bash deploy/docker-user-firewall.sh'
```

Then from a machine outside Cloudflare:

```bash
nc -z -w 5 "$STAGING_IP" 443; echo "443 exit=$?"   # expect non-zero (refused)
nc -z -w 5 "$STAGING_IP" 80;  echo "80  exit=$?"   # expect non-zero (refused)
curl -sS -o /dev/null -w '%{http_code}\n' https://staging-towers.retina.fm/api/health
```

Expected: both `nc` probes non-zero, the edge request `200`.

**Use `nc`, not `curl`, for the direct probes.** TLS to a bare IP against an Origin certificate fails hostname verification whether or not the firewall is narrowed, so `curl` reports failure against a wide-open origin and reads as success.

- [ ] **Step 6: Verify 3012 is still open on production**

```bash
nc -z -w 5 "$PROD_IP" 3012; echo "3012 exit=$?"    # expect 0 (open)
```

Expected: `0`. A non-zero here means the fleet is cut off — revert immediately with `iptables -F DOCKER-USER`.

- [ ] **Step 7: Verify the rules survive a reboot**

```bash
ssh <staging-host> 'sudo reboot'
# wait for it to come back
ssh <staging-host> 'sudo iptables -L DOCKER-USER -n | head'
```

Expected: the tagged RETURN and DROP rules are present. If the chain is empty, `retina-firewall.service` did not run — check `systemctl status retina-firewall`.

---

### Task 5: Enable Authenticated Origin Pulls for the zone

Dashboard action. No repository change. **Must complete and soak for a day before Task 3 deploys.**

**Files:** none.

**Interfaces:**
- Produces: the edge presenting a client certificate to the origin. Task 3 depends on this being true.

- [ ] **Step 1: Enable it**

In the Cloudflare dashboard for zone `retina.fm`: SSL/TLS → Origin Server → Authenticated Origin Pulls → enable at the zone level.

- [ ] **Step 2: Confirm it is inert at the origin**

Because no origin yet sets `ssl_verify_client`, the certificate is presented and ignored. Confirm nothing broke:

```bash
bash deploy/staging-smoke-test.sh
curl -sS -o /dev/null -w '%{http_code}\n' https://towers.retina.fm/api/health
```

Expected: smoke tests pass, production returns `200`.

- [ ] **Step 3: Fetch the origin-pull CA onto both droplets**

The CA is Cloudflare's static, publicly published certificate — not per-zone and not a secret.

```bash
ssh <host> 'sudo curl -fsS -o /etc/ssl/cloudflare/origin-pull-ca.pem \
  https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem \
  && sudo chmod 644 /etc/ssl/cloudflare/origin-pull-ca.pem \
  && sudo openssl x509 -in /etc/ssl/cloudflare/origin-pull-ca.pem -noout -subject -dates'
```

Expected: a subject naming Cloudflare and a `notAfter` date in the future. If `openssl` cannot parse it, the download returned an error page — do not proceed, or Task 3 will fail the container's `nginx -t` on deploy.

Note the mode is 644, not 640. This is a public CA certificate, not key material, so it does not fall under the private-key handling in `setup-server.sh:236-239` or ClickUp 86cb1kru4.

- [ ] **Step 4: Soak for a day**

Leave it enabled with no origin change. Re-run the smoke tests the next day before starting Task 3. If anything has broken at the edge, disabling the dashboard toggle reverts it with no deploy.

---

## Post-Implementation Verification

Run the full acceptance list from the spec's §7 once Tasks 1–5 are complete:

- [ ] `nc -z <staging-ip> 443` → refused
- [ ] `nc -z <staging-ip> 80` → refused
- [ ] `nc -z <prod-ip> 443` → refused
- [ ] `nc -z <prod-ip> 80` → refused
- [ ] `nc -z <prod-ip> 3012` → **open**
- [ ] `bash deploy/staging-smoke-test.sh` passes
- [ ] `curl https://towers.retina.fm/api/health` → 200
- [ ] `python3 deploy/check-env-parity.py` → exit 0
- [ ] nginx access log shows real client addresses, not Cloudflare's
- [ ] `docker compose -f docker-compose.yml -f docker-compose.local.yml up` comes up on the laptop
- [ ] Reboot both droplets; `iptables -L DOCKER-USER -n` still shows the tagged rules
- [ ] `ss -lntH '( sport = :80 or sport = :443 )'` on each droplet — record whether any IPv6 listener exists, and confirm the script's IPv6 branch took the matching path

## Rollback

- **nginx half:** revert the `tls.conf` commit and redeploy, or on a droplet comment out the two directives and `docker compose restart tower-finder`.
- **Firewall half:** `sudo iptables -F DOCKER-USER` clears it immediately; `systemctl disable --now retina-firewall` stops it returning at boot.
- **Dashboard half:** disable Authenticated Origin Pulls in the dashboard. Do this **last** if rolling back everything — with `ssl_verify_client on` still deployed and the edge no longer presenting a certificate, every request 400s.
