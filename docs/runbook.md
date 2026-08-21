# RETINA Operations Runbook

> Living document. Add notes after every real incident.initial draft — **operators must validate and annotate these steps against real events.**

---

## Environments

Three droplets. Each holds a gitignored `./.env` in `/opt/retina-server` carrying
`COMPOSE_FILE`, which selects that host's overlay, so a bare `docker compose up -d
--build` / `logs` / `ps` resolves correctly on all three and every command below is
identical everywhere.

On prod and staging the deploy writes that file itself each run, so it cannot be
missing, stale or wrong — if you find one naming the wrong environment, the deploy
stops before touching anything rather than deploying one environment onto
another's hostnames. The same is now true of test on a `deploy-test.yml` run;
after a `just deploy-test` rsync, which excludes `.env`, whatever is on the box
stays, so place it by hand once on a fresh droplet:
`cp deploy/env.test.example .env`.

| | prod | staging | test |
|---|---|---|---|
| **Overlay** | `docker-compose.prod.yml` | `docker-compose.staging.yml` | `docker-compose.test.yml` |
| **Deployed by** | CI, on push to `main` | CI, on push to `main` | `just deploy-test` (rsync, pre-review) or `deploy-test.yml` (CI, dispatch-only, git) |
| **Hostnames** | `*.retina.fm`, except `testmap` | `staging-*.retina.fm`, plus `testmap.retina.fm` | `test-*.retina.fm` |
| **RAM / swap** | 7941 MB / 4 GB | 3915 MB / none | 3915 MB / 2 GB |
| **Fleet** | none (see below) | 50 @ 1.0s (50 fps) | 50 @ 1.0s (50 fps) |
| **TCP 3012** | published (real nodes) | closed | closed |

### The test droplet has two deploy paths

`just deploy-test` rsyncs your working tree, including uncommitted edits, so a
branch can be run under load before it is reviewed. That remains its primary
purpose and is unchanged.

`.github/workflows/deploy-test.yml` is a dispatch-only CI path that deploys a
pushed ref by git, and exists so the production auto-rollback machinery can be
exercised end to end without breaking production to do it. `pre-deploy.sh` and
`rollback.sh` are both git-based, so it requires `/opt/retina-server` there to be
a **git clone** — which is a change from this droplet's original rsync-only
design, and worth understanding before you re-provision it.

The two coexist rather than compete. `just deploy-test`'s rsync rules carry
`--exclude '.git'` and there is no `--delete-excluded`, so a clone survives every
sync untouched. What an rsync does do is leave the git tree dirty relative to
`HEAD`, so after one, git's view of "what is deployed" is a label rather than a
guarantee — the same caveat `just deploy-test-status` already prints. The next
`deploy-test.yml` run resets the tree, which clears it.

prod is the reference environment: `deploy/check-env-parity.py` compares the other
two against it in CI and fails on any difference not listed in its
`ALLOWED_DIVERGENCE`. staging and test differ from prod deliberately on the
simulator (prod runs none at all), hostnames, container names and resource
limits, and on nothing else.

staging and test run the fleet 4x faster than production, on **half the cores** —
production has 4, they have 2 — so per core it is 8x. The frame path copes (41 of
50 fps sustained, nothing dropped, frame queue at zero); the solver does not.
Expect per-solve times of 45-52s against production's 17s, a solver queue
oscillating to ~28% where production sits at 0%, and a much lower solve success
rate. Nothing drops, so it is a usable environment, but a solver measurement taken
there does not transfer to production.

Do not raise the fleet without measuring on the test droplet first: at 200 nodes / 0.5s
the solver never reached steady state at all. And read `solver_avg_latency_s` over
minutes rather than seconds — it is a cumulative mean that starts low after a boot
and takes several minutes to converge. `solver_last_latency_s` is the honest
per-solve figure.

Note when reading alerts from any environment: production currently reports
`degraded` with `geolocated_tracks` at 0, so seeing that on staging or test is not
evidence of a problem with the fleet size or with a branch under test.

Only `test-towers`, `test-api`, `test-map` and `test-dash` have DNS and certificate
coverage on the test droplet. Its other three vhosts render but are unreachable by
design.

---

## Server basics

Production unless stated otherwise. Every command in this document runs **on the
droplet**, from `/opt/retina-server`, unless it says otherwise. Connection details
(addresses, key names, SSH aliases) are deliberately not recorded here: this repo
is public, and the origin addresses sit behind Cloudflare precisely so they are not
advertised. Get them from the DigitalOcean console or your own `~/.ssh/config`.

| | |
|---|---|
| **Working dir** | `/opt/retina-server` |
| **Logs** | `docker compose logs -f --tail=200` |
| **Restart (no rebuild)** | `docker compose restart` |
| **Rebuild and restart** | `docker compose up -d --build` (wait ~5 s before testing) |
| **Health endpoint** | `curl -sk https://localhost/api/health` |
| **Metrics endpoint** | `curl -sk https://localhost/api/admin/metrics` |
| **Dashboard** | `curl -sk https://localhost/api/test/dashboard` |

All state is **in-memory**. A container restart loses all connected nodes, active tracks, and in-flight frame data. State is snapshotted to disk every 60 s and restored on next startup (trust scores, reputations, accuracy samples, node identities).

### Database migrations

Applied automatically by `deploy/start.sh` on every container start, before
uvicorn. A failed migration aborts the boot and the deploy health gate reports
it; the container will not serve against a half-applied schema.

To see where a droplet stands:

```bash
ssh retina-prod 'cd /opt/retina-server && docker compose exec server \
    sh -c "cd /app/backend && python3 -m alembic current"'
```

Redeploying an older image does not undo a migration. `upgrade head` does not
stop gracefully at the newest revision the older image recognises: it fails
with `Can't locate revision identified by '<rev>'`, because that revision's
file is not in this image's `migrations/versions/`. `start.sh` treats
specifically that failure as tolerable and continues the boot rather than
crash-looping the container, which would defeat the point of rolling back. Any
other migration failure still aborts the boot as before.

That tolerance is not a judgement that the gap is harmless, and `start.sh`
cannot make that judgement: the revisions the database is ahead by are
precisely the ones missing from that image's `migrations/versions/`, so all it
holds is a revision id it cannot resolve. The grading happens at rollback time
instead, on the host, where both trees are reachable.

`deploy/rollback.sh` prints a `── Database` block once it reaches the health
check, on both of that check's outcomes. An abort before then (a failed `git
fetch`, checkout, submodule update or `docker compose` step) exits without one.
Where the gap can be computed the block names the revision the database is left
at and grades each revision in it:

- All additive: the restored code never reads what they added, so it is safe to
  serve, and the rollback exits `0`.
- Any destructive or undeclared revision: the rollback still completes, because
  restoring service comes first, then exits `2` naming the revision to
  downgrade to, or, if the tree being restored predates the migration history,
  saying that no safe target can be named.
- The gap could not be graded at all, either because no `deploy-*` tag records
  the commit the restored image was built from or because git failed: the
  rollback completes and exits `2` saying so, with no revision to name.
  Ungradable is treated as unsafe throughout.

Exit `1` keeps its existing meaning of a failed health check and says nothing
about the database.

To roll back past a destructive revision, downgrade and then redeploy:

```bash
ssh retina-prod 'cd /opt/retina-server && docker compose exec server \
    sh -c "cd /app/backend && python3 -m alembic downgrade <revision>"'
```

#### Declaring a revision's rollback safety

Every revision carries a module global beside `revision` and `down_revision`:

```python
rollback_safety = "additive"    # or "destructive"
```

`additive` means the schema after this revision is still valid for code written
against the schema before it: a new table, column or index, a column widened to
nullable, a relaxed constraint. `destructive` is everything else: a dropped or
renamed table or column, a narrowed type, a `NOT NULL` added without a default,
a tightened constraint. The test is whether the previous revision's code can run
its queries unchanged against this schema.

`backend/tests/test_migrations.py` fails if a revision does not declare one, and
a revision that reaches a droplet undeclared is graded destructive.

---

## Alert reference

Alerts fire via webhook (`ALERT_WEBHOOK_URL` env var) with a 5-minute cooldown per alert type. All alert types are listed here with their trigger condition and response steps.

---

### `server_start`

**Trigger:** Server process started (fires every restart/rebuild).  
**Meta:** `{"restored": true/false}`

`restored: false` means the snapshot was missing or corrupt — the server came up with empty trust scores and no prior node history. Nodes will reconnect and trust will rebuild over the next few hours. Not an emergency unless it happens repeatedly (indicates snapshot save is broken).

**Check:** Was this restart expected (deploy) or spontaneous (crash)?
```bash
docker compose logs --tail=50 | grep -E "ERROR|CRITICAL|Traceback"
```
If spontaneous and no crash in logs, check the host OOM killer:
```bash
dmesg | grep -i "killed process" | tail -5
```

---

### Health alerts (per-condition)

The `health_monitor` task evaluates the health checks every ~30s and fires a
**separate alert per failing condition** (the sub-checks listed below), each with
its own dedup/cooldown and a `severity` (`critical`/`warning`) in the alert meta.
When a condition clears, a one-off `resolved:<type>` alert is sent. This runs on
the server's own schedule, independent of who polls `/api/health` — see
[`alerting.md`](alerting.md).

`/api/health` itself stays **200** (liveness, used by the Docker healthcheck);
`/api/health?strict=1` returns **503** when degraded (readiness, for an external
uptime monitor). Details are never exposed on the endpoint — read them from logs:

```bash
docker compose logs --tail=200 | grep "Health check degraded"
curl -sk https://localhost/api/admin/metrics | python3 -m json.tool
```

---

### `config_degraded` (sub-check of `health_degraded`)

**Trigger:** `tower_config.json` could not be read, parsed or validated, so the process is running on something other than the file on disk.  
**What it means:** the config on disk is **not** the config in effect. `GET /api/config` returns the file, which is the one being ignored, so the two disagree until this is fixed. Tower ranking still works, on whichever config the alert names.

The alert carries the reason and says what is in effect, which is one of two things:

- `tower_config.json unusable, running on defaults (KeyError: 'max_km')` — the overlay was rejected and the
  defaults that ship with the image were applied. The reason in brackets is why the overlay was rejected.
- `tower_config.json unusable, the shipped defaults are unusable too (...), keeping the settings already in
  effect (overlay: ...)` — both were rejected, so nothing changed: the process is still on whatever it last
  loaded, which after a live reload is the operator's own previous config and only at startup is the in-code
  defaults. Two reasons are given, the defaults' first and the overlay's in the tail.

The second is the rarer and the more serious: a config shipped inside the image was rejected, so it cannot be
inspected or corrected from the droplet. Treat it as an image or validator problem, not an operator one.

**Check what the process actually loaded:**
```bash
docker compose logs backend | grep tower_config
```

**Common causes:**
1. The overlay was edited by hand inside the volume and is no longer valid. The endpoints validate on write, so a config that arrives this way is the usual source.
2. A field was renamed or removed in an image upgrade while the volume kept the old file. The volume survives `docker compose up -d --build`, so a redeploy does not clear it.
3. The file is not readable, or not UTF-8.

**Fix:** correct the file in the volume, then either `PUT /api/config` with a valid body (which validates before writing) or restart the service. The alert clears once a config loads cleanly.

---

### `frame_queue_saturated` (sub-check of `health_degraded`)

**Trigger:** `frame_queue` depth > 90% of max (default max: 10 000).  
**What it means:** Frame processor workers can't keep up with incoming frames from TCP nodes. New frames will start being dropped.

**Check load:**
```bash
top -bn1 | head -8
```

**Common causes:**
1. Too many nodes sending frames too fast — check `FRAME_WORKERS` env var (default 4, production uses 8)
2. Scipy/numpy solver is blocking frame workers — check `solver_queue_depth` in metrics; if solver queue is also backed up, the bottleneck is there
3. Host CPU pinned — look at load average in metrics

**Mitigation:**
- Short-term: `docker compose restart` to drain queues and reconnect nodes at staggered intervals
- If persistent: increase `FRAME_WORKERS` in `.env` and rebuild

---

### `stale_task` (sub-check of `health_degraded`)

**Trigger:** A background task hasn't reported success within its expected interval.

The health check only monitors these three tasks (defined in `critical_tasks` in `routes/towers.py`):

| Task | Expected interval | Stale after |
|---|---|---|
| `frame_processor` | ~10 s | 20 s |
| `aircraft_flush` | ~5 s | 15 s |
| `analytics_refresh` | 30 s | 120 s |

The blah2 bridge tasks and `solver` update `task_last_success` but are **not** checked by `/api/health` — their alerts fire via separate mechanisms (`solver_latency_high`, `solver_queue_drops`).

The bridge reports one task key per live node — `blah2_bridge:<node_id>` — so a single node going dark is visible on its own instead of being masked by its neighbours. The keys are registered at startup from the node list, which is config, not code (see below).

**Check logs for exceptions in the named task:**
```bash
docker compose logs --tail=500 | grep -i "error\|exception\|traceback" | tail -20
```

`frame_processor` stale is the most serious — it means detection frames are piling up unprocessed or the loop crashed. If the loop crashed, the container needs a restart (tasks are daemon threads and will not restart themselves).

A stale `blah2_bridge:<node_id>` means that node's `/api/detection` is unreachable or serving only stale frames; other nodes are unaffected. Stale bridge keys are expected wherever there is no upstream retnode access — safe to ignore there.

### Adding or changing a live blah2 node

The node list is `blah2_nodes.json` — url, rx, tx, fc and friends per node — read through the runtime-config overlay, so this is a config change with no rebuild:

```bash
docker compose exec server vi /app/backend/data/runtime/blah2_nodes.json
```

Restart the container afterwards; the list is read once, at startup.

`backend/config/blah2_nodes.json` in the repo is the shipped default that seeds that overlay on first boot. Once the overlay exists it wins, so editing the repo copy will not change a running deployment. `BLAH2_NODES_FILE` overrides the path entirely.

After a change, confirm the node registered and is solving sensibly:

```bash
curl -sk https://localhost/api/radar/nodes | jq '.nodes | keys'
```

```bash
curl -sk https://localhost/api/test/node/radar3a-retnode/verification | jq '{n_tracks, n_matched, position}'
```

A node missing from the first list failed validation — the reason is logged at error level, naming the offending field.

Bad geometry passes validation, and `position.median_km` will *not* reliably catch it: that figure is dominated by the single-node solver's own ~25–35 km uncertainty. A deliberate 20 km TX error moved it by about 5 km, inside the run-to-run spread. To check tx/rx/fc against the hardware, compare the node's published `adsb[].expected_delay` with the bistatic delay computed from the configured geometry — correct config agrees to tens of metres, a 20 km TX error to tens of kilometres.

---

### `solver_queue_drops`

**Trigger:** The stdlib Queue between frame workers and solver threads is full (max 200) and candidates are being dropped.

**What it means:** Solver threads are slower than frame workers produce multinode candidates. Drops mean some legitimate aircraft positions will never be computed for those frames.

**Check:**
```bash
curl -sk https://localhost/api/admin/metrics | python3 -c \
  "import sys,json; m=json.load(sys.stdin); print('queue_pct:', m['solver_queue_pct'], 'drops:', m['solver_queue_drops'], 'avg_latency:', m['solver_avg_latency_s'])"
```

**Common causes and fixes:**
1. Node count too high for 2 solver workers — try `SOLVER_WORKERS=4` in `.env`
2. Solver itself slow (bad aircraft geometry, many 3+ node candidates) — check `solver_avg_latency_s` in metrics; if >5 s something is wrong with solver inputs
3. `grid_step_km` misconfiguration producing excessive candidates — check `InterNodeAssociator` config in `state.py` (must be `3.0`, not `30.0`)

> **Known issue from past**: `grid_step_km=30.0` (default was wrong) caused zero multinode associations. Fixed to `3.0`. If multinode_tracks suddenly drops to 0, check this first.

---

### `solver_latency_high`

**Trigger:** End-to-end time from frame enqueue to solver completion > 30 s.  
**What it means:** The solver pipeline is severely backed up. The 30 s threshold means the queue is likely saturated and candidates are waiting minutes before being solved.

Same diagnosis as `solver_queue_drops` above.

---

### `solver_queue_high` (sub-check of `health_degraded`)

**Trigger:** Solver queue > 50% full (>100 of 200 slots). Early warning before drops start.

No immediate action required. Watch `solver_queue_pct` over the next few minutes in metrics. If it keeps climbing, treat as `solver_queue_drops`.

---

### `node_dropout`

**Trigger:** Active connected nodes < 80% of peak since startup (and peak > 10).  
**What it means:** A significant fraction of the fleet went offline unexpectedly.

**Check which nodes are gone:**
```bash
curl -sk https://localhost/api/radar/nodes | python3 -c \
  "import sys,json; nodes=json.load(sys.stdin); [print(n['node_id'], n['status']) for n in nodes if n['status']=='disconnected']"
```

**Common causes:**
1. Server restart — nodes will reconnect within their retry interval. If `restored: false` in the `server_start` alert, expect a full reconnect cycle.
2. Network issue at node site — check if a geographic cluster of nodes dropped (all from one ISP/location)
3. Port 3012 unreachable — check firewall: `ufw status` on server, or DigitalOcean firewall rules
4. Node-side crash — contact node operator

**After a docker rebuild:** The fleet always loses all connections. Stop the old simulator unit and start a new one (see fleet simulator section of server-ops.instructions.md).

---

### `no_active_tracks`

**Trigger:** `frames_processed > 500` AND `len(adsb_aircraft) == 0` AND `len(multinode_tracks) == 0`.  
**What it means:** Pipeline is running (frames processed) but producing nothing. Either ADS-B feed is down or the tracker is broken.

**Check ADS-B feed:**
```bash
curl -sk https://localhost/api/radar/data/aircraft.json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(len(d.get('aircraft',[])), 'aircraft')"
```

If 0 aircraft: check `adsb_truth_fetcher` task in metrics (`task_last_success.adsb_truth_fetcher`). The external ADS-B source (`adsb.lol` or similar) may be down.

If aircraft exist but `multinode_tracks == 0`: check node count — multinode tracks require at least 2 active nodes with overlapping coverage.

---

### `anomaly_flood`

**Trigger:** More than 50% of tracked aircraft have active anomalies.  
**What it means:** The tracker (retina-tracker) is misfiring — likely a GNN misassociation cascade where track swaps create spurious altitude/speed anomalies. Real anomalies are buried in noise.

**Past incident:** This was the first bug we investigated. Root cause was dense simulation causing single-frame hex mismatches. Fix was in retina-tracker: debouncing `identity_swap` to 2 consecutive frames and adding hex guards on `altitude_jump`.

**Check current anomaly state:**
```bash
curl -sk https://localhost/api/test/dashboard | python3 -c \
  "import sys,json; d=json.load(sys.stdin); p=d['pipeline']; print('aircraft:', p['aircraft_on_map'], 'anomalies:', p.get('anomaly_count', '?'))"
```

**Immediate mitigation:** None without code change. If tracker library was recently updated, rollback:
```bash
# On server:
cd /opt/retina-server && pip show retina-tracker  # check installed version
```

---

### `solver_accuracy_degraded`

**Trigger:** Mean haversine error between solver output and ADS-B ground truth > 10 km (requires >20 samples).  
**What it means:** The LM solver is producing positions that don't match ADS-B. Either the geometry is bad (node configs wrong) or there's a systematic solver issue.

Check `/api/radar/analytics` for per-node data. If specific nodes have bad calibration points it'll skew the solver inputs.

> **Known cause:** `grid_step_km=30.0` produced zero valid associations (multinode_tracks=0) so no accuracy samples accumulated. If accuracy is 0 samples, that's the issue.

> **Known cause:** Kalman filter `dt` mismatch — if simulated nodes use 40 s frame intervals but the tracker was built with dt=0.5, the prediction barely moves state and Mahalanobis gate rejects all associations. Check retina-tracker version.

---

### `high_miss_rate`

**Trigger:** Average per-node miss rate above `HIGH_MISS_RATE_THRESHOLD` (default 0.98) across nodes that have aircraft in range.  
**What it means:** The network is seeing almost nothing. This is a tripwire, not a measure of how well the fleet is doing.

**Read the number correctly before acting on it.** The miss rate counts ADS-B
aircraft inside a node's *theoretical* beam wedge that its tracker did not
detect, and for passive bistatic radar that wedge is a much larger set than
what is physically detectable: low RCS, poor bistatic geometry, terrain and
receiver sensitivity all put aircraft in the wedge no node could see. A high
reading is the normal operating point. Production reports 72-94% when it is
working, so this alert means a reading well outside even that.

**Check per-node miss rates:**
```bash
curl -sk https://localhost/api/admin/leaderboard | python3 -c \
  "import sys,json; rows=json.load(sys.stdin); [print(r['node_id'], r.get('miss_rate','?')) for r in rows]"
```

**Common causes:**
1. Ingest has stopped — check `stale_task:*` and `no_active_tracks`, which will
   usually be firing alongside if the pipeline is the problem
2. Nodes are connected but not detecting — compare the per-node rates above; a
   fleet-wide 98% is a pipeline or ADS-B problem, one node at 100% is that node
3. Beam config wrong after a config push — a node aimed at empty sky has every
   aircraft in its claimed wedge and none in reality

A reading that sits just above the threshold, rather than near 100%, is more
likely the threshold being wrong than the network being blind: the floor moves
with traffic, time of day and which nodes are up. See `docs/alerting.md`, and
ClickUp 86cb81gkn for replacing the measure with one that tracks a node against
its own history.

---

### `snapshot_corrupt`

**Trigger:** SHA-256 of the on-disk snapshot doesn't match the saved checksum.

**Immediate action:**
```bash
# On server — check if backup exists on R2:
# (if R2 is configured)
curl -sk https://localhost/api/admin/storage
```

Server will start with empty state if snapshot is corrupt. Trust scores and reputation data need to rebuild from scratch — this takes hours under normal node load. Not a functional outage.

**Investigation:** Check disk health (`df -h`, `dmesg | grep -i error`) and whether a partial write happened during a previous crash.

---

### `r2_replication_failed`

**Trigger:** R2 upload of the state snapshot failed.  
**What it means:** Local snapshot is being saved, but off-server backup is stale. If the droplet is lost, recovery falls back to an older R2 snapshot.

**Check R2 config:**
```bash
# R2 credentials are in backend/.env — verify they're set:
grep R2 /opt/retina-server/backend/.env
```

Not an emergency. The local snapshot still runs every 60 s. Urgent only if combined with `snapshot_corrupt` (no local backup AND R2 backup stale).

---

### `disk_low`

**Trigger:** Free disk space on `coverage_data` partition < 500 MB.  
**What it means:** Archive flush or snapshot save is about to fail. If the disk fills completely, the frame processor will crash on the next archive write and the state snapshot won't save.

**Check current usage:**
```bash
df -h /opt/retina-server/backend/coverage_data && du -sh /opt/retina-server/backend/coverage_data/*
```

**What to clean first:**
1. Old archive files — these are the biggest consumers. The `archive_lifecycle_task` background task should be rotating them, but it may not be running or its retention window may be too long.
2. Log files: `docker compose logs` doesn't write to disk — check `/var/log` on the host.
3. `coverage_data/` subdirectories — each node accumulates coverage map data here.

**If you need space immediately:**
```bash
# Check archive files (oldest first)
find /opt/retina-server/backend/coverage_data -name "*.json.gz" | sort | head -20
```

> **Do not delete the `state_snapshot.json` or `state_snapshot.json.sha256` files** — those are the restore point. Delete archive `.json.gz` files instead.

---

### `memory_high`

**Trigger:** Process RSS > 3 GB on the 4 GB droplet.  
**What it means:** Memory pressure. The OS will start swapping and the OOM killer may fire, which would crash the container without warning.

**Check current memory:**
```bash
cat /proc/$(docker compose -f /opt/retina-server/docker-compose.yml top | grep uvicorn | awk "{print \$1}" | head -1)/status | grep VmRSS
# Simpler:
free -h && top -bn1 | head -10
```

**Common causes:**
1. `track_histories` or `ground_truth_trails` deques not bounded — check constants in `config/constants.py` for `TRACK_HISTORY_MAX` and `GROUND_TRUTH_MAX`.
2. `accuracy_samples` deque — bounded at 5000 entries, shouldn't be a problem.
3. `multinode_tracks` dict — grows unbounded if old entries aren't purged. Check if the analytics refresh task is evicting stale tracks.
4. Memory leak in a dependency (e.g. retina-tracker, scipy) after many thousands of solve calls.

**Immediate mitigation:** `docker compose restart` — restarts the process with fresh memory, state is restored from snapshot within a few seconds.

If memory climbs back over 3 GB within an hour, there is a leak. File an issue with the output of `docker stats` sampled over time.

---

## Common operational tasks

### Deploy a code change
```bash
# Local
git add -A && git commit -m "..." && git push

# Server
cd /opt/retina-server && git pull && docker compose up -d --build

# After ~5 s, verify
curl -sk https://localhost/api/health
```

> **Always `git push` before deploying.** `git pull` on the server does nothing if the commit isn't pushed.

### Restart without deploying
```bash
cd /opt/retina-server && docker compose restart
```

### Tail live logs
```bash
cd /opt/retina-server && docker compose logs -f --tail=100
```

### Check resource usage
```bash
top -bn1 | head -20
df -h /opt/retina-server/backend/coverage_data
```

### Start / bounce the fleet simulator
The fleet is a Compose service (`fleet` in `/opt/retina-server/docker-compose.yml`,
`restart: unless-stopped`). On staging and test it starts automatically on deploy
(CI `docker compose up -d --build`) and on reboot (docker is enabled) — you
normally do NOT start it by hand. To manually bounce just the fleet (it loses its
TCP connections and regenerates its nodes, taking up to a minute):
```bash
cd /opt/retina-server && docker compose up -d --build --force-recreate --no-deps fleet
```

⚠️ **Not on production.** Production runs no simulator: `docker-compose.prod.yml`
puts the service behind a `sim` profile that nothing enables, because 25 synthetic
nodes cost the solver about as much as the real fleet does and the box was already
at 107% CPU reporting `solver_latency_high` at rest. Naming `fleet` on the command
line *auto-enables its profile*, so the bounce command above is not inert on the
prod droplet — it would silently put all 25 back. Do not run it there unless you
intend exactly that, and if you do, undo it with `docker compose stop fleet`
followed by `docker compose restart server` (stopping the container does not
deregister the geometry it already registered).
`--no-deps` is the important flag: `fleet` declares `depends_on: server`, so
without it Compose would also rebuild and recreate the running app, turning a fleet
bounce into a full redeploy and an outage. The host's `./.env` sets `COMPOSE_FILE`,
so the bare `docker compose` above resolves to base + the production overlay.

Params (nodes/interval/mode/aircraft) live in the `fleet` service block in
`docker-compose.yml` — edit them there, not on the command line.

**Staging's fleet is public.** `testmap.retina.fm` is served by the staging
droplet and fed by this fleet, so bouncing it, or a staging deploy, blanks the
demo people are shown for a minute or so. `staging-map.retina.fm` is the same
surface under a staging-prefixed name. Note the tuning is deliberate: staging
runs 50 nodes @ 1.0s on 2 cores, which saturates the solver (45–52 s per solve),
so the public map is denser but laggier than production's used to be.

⚠️ Do NOT start the fleet as a host process (`systemd-run`, a systemd unit, or a
bare `python3 -m retina_simulation.orchestrator`) while the Compose `fleet` service
is running — two fleets both push synthetic traffic and double-count nodes and
aircraft. Compose is the only supported way to run it.

### Quick fleet health snapshot
```bash
curl -sk https://localhost/api/test/dashboard | python3 -c \
  "import sys,json; d=json.load(sys.stdin); n=d['nodes']; h=d['server_health']; p=d['pipeline']; \
  print(f\"nodes={n['active']}/200  queue={h['frame_queue_utilization_pct']}%  drops={h['frames_dropped']}  on_map={p['aircraft_on_map']}\")"
```

### Feature-gate flips (consensus / claiming / FOV / smoother)

The multi-node stages are env-gated in the gitignored `backend/.env` — see
the Feature gates table in [`architecture.md`](architecture.md). To flip one:
edit `backend/.env`, then `docker compose up -d` (env-only, no `--build`).
Standard rollout is `shadow` first: shadow counters accumulate in
`/api/test/solver-stats` (`fov`, `claiming`, `consensus` blocks) without the
stage binding; flip to `active` only after the shadow soak looks sane.
Instant rollbacks: any mode flag back to `shadow`/`off`, and
`TRACK_SMOOTHER=ewma` for display smoothing.

### Empirical coverage / learned FOV health

Per-node learned FOV state persists on the `coverage_data` volume and is
summarized per node in `/api/radar/analytics` (`empirical_coverage.fov`:
`n_pos`, `bins_observed/prior/closed`, `max_limit_km`). Sanity rule for
**synthetic** nodes: open bins must fit the 42° wedge — `bins_observed`
persistently above ~12 means a calibration leak, not real coverage (the
simulator only generates detections in-wedge). Real nodes (radar3/radar3a)
legitimately learn near-omni.

To force a fleet-wide relearn (e.g. after a calibration-semantics change):
bump `CALIBRATION_SCHEMA` in
`libs/retina-analytics/src/retina_analytics/empirical_coverage.py` with a
ledger entry (the pin test in `tests/test_coverage_binning.py` forces the
rationale to be written down). Persisted state with an older schema is
discarded at node registration on the next deploy — staging and prod both,
no manual volume surgery. Do NOT hand-delete files on the volume; the
running server re-persists its in-memory copy over them.

---
