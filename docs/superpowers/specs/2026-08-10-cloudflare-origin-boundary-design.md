# Cloudflare Origin Boundary

**Date:** 2026-08-10
**Status:** Approved; implementation not started
**Ticket:** ClickUp 86cb2cymj, a subtask of "Node ↔ server phase 1" (86cb2cxdx)
**Baseline:** `origin/main` at `ad119b1`

Make the two droplets reachable only through Cloudflare, so that the per-IP rate
limiter means something and the origin cannot be addressed directly. Required
before the node cutover, and independent of it: this touches no Python and
shares no files with the node API work.

## 1. Where this starts from

The ticket lists three changes and calls all three missing. One had already
landed by the time work began, in PRs #146/#147 on 2026-08-10:

| Part | Ticket | Actual, at `ad119b1` |
|---|---|---|
| nginx trusts Cloudflare's ranges, reads `CF-Connecting-IP` | missing | **done** — `deploy/nginx-security.conf:19-41` |
| Authenticated Origin Pulls (`ssl_verify_client`) | missing | open — no occurrence under `deploy/` |
| Firewall narrowed to Cloudflare | missing | open — `deploy/setup-server.sh:64-66` |

So this design covers the second and third only. The first is re-verified, not
rebuilt.

## 2. The firewall cannot be ufw

nginx does not run on the host. It runs in the `tower-finder` container with
ports published in `docker-compose.yml:29-32`:

```
ports:
  - "80:80"
  - "443:443"
  - "3012:3012"
```

Docker publishes a port by writing DNAT rules into `nat/PREROUTING` and filter
rules into its own `DOCKER` chain off `FORWARD`. ufw's rules live in `INPUT`,
which those packets never traverse. A `ufw deny` on 443 would have no effect on
anything this application serves.

This is not a prediction. `deploy/setup-server.sh:64-66` allows only 22, 80 and
443, and `ufw default deny incoming` is set — yet production nodes reach `:3012`
continuously. They reach it because ufw is not in the path.

The rules therefore go in `DOCKER-USER`, the chain Docker guarantees it will not
rewrite, traversed before the `DOCKER` chain.

**This also decides which control can satisfy the acceptance test.** The ticket
asks for a TCP connect to 443 to be *refused*, checked with `nc -z`.
`ssl_verify_client` rejects during the TLS handshake, after the TCP connection is
established, so `nc -z` would still report success. Only a packet filter refuses
at connect time. The two controls are not redundant — they fail at different
layers, which is why the ticket says any one alone is bypassable.

Existing ufw configuration is left untouched. It still governs SSH and any future
host-level service; changing it would be unrelated churn.

## 3. Scope: ports 80 and 443 only

`:3012` stays open to the world. Nodes connect from arbitrary consumer
addresses behind NAT and CGNAT (ADR assumption A2), so there is no allowlist that
would not strand the fleet. Its exposure is already tracked by "Retire
`blah2_bridge` and the `:3012` ingest path" (86cb2czu3), and phase 1 explicitly
declined to retire it.

Rejected: a SYN-rate or concurrent-connection cap on 3012. It buys real
protection for the one genuinely internet-facing port, but adds a tuning
parameter this ticket does not cover and a way to throttle real nodes during the
cutover. Worth revisiting once the fleet size is known.

Verification asserts 3012 is *open*, not merely unmentioned, so a later change
cannot quietly close it.

## 4. Changes

| Change | Location |
|---|---|
| Enable Authenticated Origin Pulls | Cloudflare dashboard, zone `retina.fm` |
| `ssl_verify_client on` + CA path | `deploy/nginx/snippets/tls.conf` |
| CA presence assertion | `deploy/start.sh` |
| `DOCKER-USER` rules + systemd unit | `deploy/setup-server.sh`, new unit file |
| Range source and refresh script | `deploy/cloudflare-ranges.txt`, `deploy/refresh-cloudflare-ranges.sh` |
| Absolute assertion on the rendered config | `deploy/check-env-parity.py:217-229` |

The assertion is the regression test for this work. `check-env-parity.py` already
carries an "absolute assertion, not a comparison" block, added because a parity
diff proves only that the two environments match *each other* — a change dropping
TLS from both would sail through it. The same hole applies here: without an
absolute assertion, deleting `ssl_verify_client` from the shared snippet would
leave staging and production identical and equally unprotected. So the boundary
gets pinned the same way `listen 443 ssl` and HSTS already are.

`snippets/tls.conf` is inlined into every TLS server block by
`render-nginx-config.py`, so one edit covers all seven vhosts
(`EXPECTED_TLS_VHOSTS` in `check-env-parity.py:55`).

The origin-pull CA goes to `/etc/ssl/cloudflare/origin-pull-ca.pem`, beside the
existing cert and key. That directory is already bind-mounted read-only into the
container (`docker-compose.yml:34`), so no mount changes.

The laptop overlay renders the template's plain-HTTP branch (`RETINA_IF TLS`,
`render-nginx-config.py` pass 0) and never sees `ssl_verify_client`. "The laptop
stack still comes up" is satisfied by construction rather than by testing.

### Range handling

Cloudflare's published ranges become one committed file feeding both consumers,
with `refresh-cloudflare-ranges.sh` regenerating it from Cloudflare's endpoint
and diffing. Deploys stay deterministic and need no network at boot.

Rejected: fetching live during provisioning. Two runs of the same script would
produce different firewalls, which is hard to reason about and harder to roll
back.

This ships the script and the single source. Automating the refresh is 86cb2d022
and stays out of scope; without it, nginx's ranges and the firewall's ranges
would be two copies free to drift.

**IPv6:** `nginx.conf.template` has no `listen [::]` directive, so nginx is
IPv4-only and Docker publishes no IPv6 path to it. `nginx-security.conf` does
carry seven IPv6 ranges for `set_real_ip_from`. Implementation verifies on the
droplet whether an IPv6 path to the published ports exists, and adds `ip6tables`
rules only if one does — rather than assuming either way.

## 5. Rollout

The ticket asks for staging first, a day's soak, then production. That is not
achievable as written, for two reasons discovered during design:

1. `deploy/check-env-parity.py` requires the rendered nginx configs for staging
   and production to be byte-identical after hostname tokenisation. Any flag that
   turns `ssl_verify_client` on in one and off in the other fails CI.
2. `.github/workflows/ci.yml` runs `deploy-staging → staging-smoke-tests →
   e2e-staging → deploy-production` in a single push-to-main run. Production
   follows staging by minutes.

Rather than weaken either guard, the soak is split by risk:

- **Day 1 — dashboard only.** Enable Authenticated Origin Pulls for the zone. An
  origin that does not verify simply ignores the client certificate, so this is
  inert at the origin and reversible with one toggle. Soak it: this is the half
  that can silently break edge→origin.
- **Day 2 — nginx.** Merge the `ssl_verify_client` change. It reaches staging
  first inside the pipeline, and `staging-smoke-tests` plus the ~127-test
  `e2e-staging` suite gate production. A wrong CA fails staging and production
  never deploys.

Ordering within day 2 is load-bearing and unchanged from the ticket: the
dashboard toggle must precede `ssl_verify_client`, or every request 400s at the
origin including your own.

The `DOCKER-USER` work is provisioning, not deploy: applied per droplet, staging
first, verified, then production. It is independent of the dashboard and nginx
ordering above and can land before either, because it does not interact with TLS
at all.

A mistake in those rules cannot lock anyone out of the droplets. SSH is a
host-level service on port 22, governed by ufw's `INPUT` rules, which
`DOCKER-USER` does not touch — the same separation that makes ufw useless for the
container ports makes it authoritative for SSH.

This is a real change to the ticket's stated plan and should be raised with its
author.

## 6. Failure handling

The sharp edge is nginx refusing to start when `ssl_verify_client on` is rendered
but `origin-pull-ca.pem` is absent on a droplet — an outage, not a degradation.
`start.sh:59` already runs `nginx -t` after rendering, which does catch a missing
`ssl_client_certificate`, so the container fails fast rather than serving
without verification. What it does not do is say why: the failure surfaces as a
generic nginx config error. `deploy/start.sh` therefore gains an explicit check
before the render that names the missing CA, mirroring how `setup-server.sh:182`
already refuses to run without the origin certificate.

This is a fail-closed path by design. A missing CA must never degrade to serving
unverified TLS, because that is indistinguishable from the boundary working.

`DOCKER-USER` rules are applied by a systemd oneshot ordered `After=docker.service`
so they survive reboots and Docker restarts, and applied immediately by
`setup-server.sh` so provisioning does not depend on a reboot.

## 7. Verification

Droplet addresses are written as placeholders throughout. They were scrubbed from
this repository's runbook on 2026-08-06 and must not be reintroduced in a
committed file, even though the history means they are already public.

- `nc -z <droplet-ip> 443` → refused, on both droplets. **Not `curl`:** TLS to a
  bare IP against an Origin certificate fails hostname verification whether or
  not the firewall is narrowed, so `curl` reports failure against a wide-open
  origin and reads as success.
- `nc -z <droplet-ip> 80` → refused.
- `nc -z <prod-ip> 3012` → open. Deliberate; see §3.
- Requests through the edge succeed on both environments — existing
  `deploy/staging-smoke-test.sh` covers this.
- `python3 deploy/check-env-parity.py` stays green.
- nginx access log shows real client addresses, not Cloudflare's. Already true
  since #146; re-verified, not rebuilt.
- Laptop stack (`docker-compose.local.yml`) comes up.

## 7a. Known limitations of the shipped boundary

Both fall out of the fix for the egress bug described below, and neither is
internet-reachable. Recorded so they are known rather than discovered.

- **The firewall filters only the default-route interface.** Every `DOCKER-USER`
  rule is scoped with `-i "$EXT_IF"`, resolved from the default route. Ingress
  arriving on any other interface — a DigitalOcean private-network `eth1`, for
  instance — is unfiltered by this boundary. The scoping is not optional: without
  it the DROP also matches container egress, because `DOCKER-USER` hangs off
  `FORWARD`, which carries both directions.
- **A box with no IPv6 default route but an `ip6tables DOCKER-USER` chain and an
  unreadable `ss` now exits 1** after applying the IPv4 rules, where it
  previously succeeded silently. Deliberate: that combination is contradictory
  and worth a human look. IPv4 protection is already in place when it fires.

The interface resolution refuses to guess. A rule bound to the wrong interface
fails **open**, not closed — the DROP matches nothing, the origin serves the
whole internet, and the script still prints success. Ambiguity therefore exits
non-zero rather than picking a candidate.

## 8. Out of scope

- **Origin IP rotation.** Both addresses are permanently public in this repo's
  git history. The ticket sizes rotation as lower priority than the three
  controls, and it is a droplet migration — DigitalOcean has no in-place IP
  change, and Reserved IPs do not hide a droplet's own address.
- **The world-readable production origin key** (86cb1kru4).
  `setup-server.sh:236-239` deliberately declines to tighten it, to avoid
  rewriting live key material as a side effect of re-provisioning. It pairs
  naturally with this work if droplet access is in hand, but is not part of it.
- **Range-refresh automation** (86cb2d022).
- **Any change to `:3012`,** including its retirement (86cb2czu3).
