# Docker Compose consolidation

Date: 2026-07-28
Repo: Tower-Finder
Status: design, awaiting review

## Problem

There are four Compose files, each a near-complete standalone copy (98 to 123 lines),
so they drift from one another rather than sharing a base:

| File | Entrypoint | UI | Fleet | Consumer |
|---|---|---|---|---|
| `docker-compose.yml` (prod) | `start.sh` | React (`nginx.conf`) | 200 @ 40s, x1 | CI `deploy-production`; bare `up` |
| `docker-compose.staging.yml` | `start.sh` | React (`nginx-staging.conf`) | 50 @ 40s | CI `deploy-staging` |
| `docker-compose.test.yml` | `start-test.sh` (legacy) | tar1090 (`nginx-test.conf`) | 200 @ 8s, x4 | laptop (via local.yml); `deploy-test-network.sh` |
| `docker-compose.local.yml` | override on test.yml | flips back to React | inherits test | laptop |

`docker-compose.test.yml` is the outlier on three axes at once: it is the only file
still on the legacy `start-test.sh` (no crash-loop supervision, no constants.py
refresh, no dual nginx/uvicorn handling that `start.sh` provides), the only one serving
the old tar1090 UI, and it carries a stress fleet shape (8s interval, x4 time scale)
whose stated rationale ("25 fps matches the Kalman tracker ceiling") is known to be
wrong. `docker-compose.local.yml` exists largely to paper over those divergences: it
flips nginx back to React and drops the certs.

Two consequences: the laptop stack is not a faithful mirror of the deploy (different
entrypoint, different UI, different fleet), and there is one more config to maintain
than the system needs.

`start-test.sh` and `nginx-test.conf` have no other consumers; removing them takes
tar1090 out of the system entirely (prod never serves it). The test-network droplet
that `deploy/deploy-test-network.sh` targeted does not currently exist (live
`testmap.retina.fm` is served from the prod droplet), so that script and its config
can be retired.

## Goal

Collapse to one base plus thin per-environment overrides, related by Compose merge so
they cannot drift, with a single entrypoint and a single UI. As a direct consequence
the laptop stack becomes a faithful mirror of prod, which is the underlying reason for
the work: optimisation and configuration issues that do not reproduce under the current
local Docker should reproduce under a stack that inherits prod's fleet shape, resource
limits and entrypoint verbatim.

## Design

### File set after the change

- `docker-compose.yml` stays the base and equals prod. Bare `docker compose up` remains
  prod, exactly as CI deploys it today, so the production path is unchanged. One
  addition: fold `FRAME_WORKERS=8` in from the droplet's `backend/.env` so prod's tuning
  is visible in git and inheritable by the overrides. Secrets stay in `.env`; only this
  non-secret tuning value moves.

- `docker-compose.staging.yml` reduces to its deltas against the base:
  `RETINA_ENV=staging`, staging `CORS_ORIGINS`, `FRAME_WORKERS=6` (overriding the base's
  8), app memory 3G (overriding 4G), and the fleet deltas `FLEET_NODES=50`,
  `FLEET_MIN_AIRCRAFT=20`, `FLEET_MAX_AIRCRAFT=40`, `FLEET_CONCURRENCY=10`.
  `UVICORN_HOST=0.0.0.0` is already in the base, so it drops out of this file.
  `nginx-staging.conf` continues to be selected automatically by `start.sh` from
  `RETINA_ENV=staging`. CI's `deploy-staging` job gains a leading
  `-f docker-compose.yml` before `-f docker-compose.staging.yml`.

- `deploy/start.sh` absorbs the generalised `NGINX_PROFILE` selection currently living
  only in `start-test.sh` (`start-test.sh:15-18`): default `NGINX_PROFILE` to
  `RETINA_ENV`, and if `nginx-${NGINX_PROFILE}.conf` exists, copy it in. This supersedes
  `start.sh`'s present staging-only special case (`start.sh:36`), which becomes the
  `NGINX_PROFILE=staging` path of the general form, so staging is unchanged. This is a
  prerequisite, not a nicety: the laptop stack now runs `start.sh` (inherited from the
  base) and sets `NGINX_PROFILE=local`, and `start.sh` today does not understand that,
  so without this change it would fall back to the baked prod `nginx.conf` and its cert
  mount and break. `start-test.sh` can only be deleted after this move.

- `docker-compose.test.yml` is recreated as the laptop override on the prod base (this
  is today's `docker-compose.local.yml`, renamed onto the freed name; the name is apt
  because the stack runs `RETINA_ENV=test`). It inherits prod's fleet shape, resource
  limits and `start.sh` verbatim, and overrides only what a laptop cannot do:
  - `RETINA_ENV=test`, to stay inside the dev/test auth allowlist (JWT secret optional,
    auth bypass permitted) rather than tripping the production-only guards.
  - `NGINX_PROFILE=local` to select the cert-free React `nginx-local.conf`. The nginx
    transport axis stays deliberately decoupled from `RETINA_ENV`, as the current code
    and its comments require; `nginx-local.conf` keeps its name.
  - `ports: !override` to publish only `8080:80` (no 443, no host-published 3012).
  - `volumes: !override` to drop the `/etc/ssl/cloudflare` bind mount, keeping the data
    volumes.
  - `env_file` marked `required: false` so a fresh clone runs without `just setup`.
  - `restart: "no"` on both services, so Docker Desktop does not resurrect the stack.

  The fleet inherits `FLEET_VALIDATE=false` from the base, so it pushes detections over
  the compose network without POSTing ground-truth to `:8000`, and the base's rate
  configuration (which already sustains 200 nodes at 40s in prod) applies unchanged.

### Deletions

- `docker-compose.test.yml` (old stress/tar1090 form; the name is reused per above).
  The stress fleet shape it carried (200 @ 8s, x4) is dropped, not preserved as an
  override: ad hoc load testing overrides `FLEET_INTERVAL`/`FLEET_NODES` when needed.
- `deploy/start-test.sh`, after `start.sh` absorbs its `NGINX_PROFILE` logic (above),
  and the `chmod` reference to it in `Dockerfile`.
- `deploy/nginx-test.conf` (the only tar1090 *static HTML UI* root in the tree; never
  used locally).
- `deploy/deploy-test-network.sh` (drove the retired test-network droplet).
- The `COPY tar1090/html /app/tar1090/html` step in `Dockerfile`, which staged the files
  that only `nginx-test.conf` served.

tar1090 here means two separate things and only the static HTML UI is removed. The
tar1090 *data format* stays: `backend/routes/radar.py` (`tar1090_aircraft`,
`tar1090_receiver`), the aircraft.json generation in `backend/pipeline/passive_radar.py`
and `backend/services/`, and the `tar1090_data` directory the backend reads and writes
are all core API surface and are untouched.

### Net effect

Four Compose files become three, two entrypoints become one, two UIs become one. The
laptop command becomes:

```
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
```

which is prod, minus certs and with test-env auth, on port 8080.

## Consumers to update

- `.github/workflows/ci.yml`, `deploy-staging` job: prepend `-f docker-compose.yml` to
  the staging compose invocations. `docker-compose.staging.yml` is thinned to deltas in
  the same change, verified by rendering `docker compose config` before and after and
  confirming the resolved config is unchanged.
- `justfile`, the `up` recipe: the `testmap` profile sources its `FLEET_*` from
  `docker-compose.test.yml` (`justfile:59`). Since the stress shape is dropped and the
  laptop docker stack now runs the prod fleet shape, `testmap` is redundant with `prod`;
  remove the `testmap` profile, leaving `local` (dense dev stream) and `prod` (sourced
  from `docker-compose.yml`).
- `Tower-Finder/CLAUDE.md` "Running locally": update the two commands from the
  `-f docker-compose.test.yml -f docker-compose.local.yml` pair to the new single
  override, and drop the stale "tar1090 UI after a rebuild is browser cache" note (it
  described the now-removed nginx-test path).
- `docker-compose.test.yml` header comment block: rewrite for its new role.

## Out of scope

- Any change to the staging or production deploy pipeline beyond the one `-f` addition.
- A dedicated stress profile. The old 8s/x4 shape is dropped; ad hoc load testing can
  pass `FLEET_INTERVAL` on the command line when needed. A `docker-compose.stress.yml`
  override can be added later if a standing profile proves necessary.
- The tar1090 *data format* and its API routes (see Deletions): core surface, untouched.

## Verification

- `docker compose -f docker-compose.yml config` and the same with each override render
  without error and show the expected merged values (fleet counts, memory, nginx
  profile, ports).
- The laptop stack brought up with the new override serves the React live-radar at
  `http://testmap.localhost:8080` and a healthy `/api/health`, with the fleet
  connecting.
- `grep -r` confirms no remaining references to `start-test.sh`, `nginx-test.conf`,
  `deploy-test-network.sh`, `docker-compose.local.yml`, or the old `docker-compose.test.yml`
  stress form (the `justfile testmap` reference is gone).
- `docker build .` succeeds after the `Dockerfile` edits (no missing `start-test.sh`
  chmod target, no `tar1090/html` copy).
- `just up prod` still resolves its fleet params (`FLEET_INTERVAL=40.0`); `just up testmap`
  now reports the unknown-profile error.
