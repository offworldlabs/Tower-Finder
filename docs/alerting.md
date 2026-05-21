# Alerting & monitoring

How RETINA detects problems and notifies operators. The goal is pre-launch
coverage with **no infrastructure we have to run ourselves** — alerting is
in-process plus a free external dead-man's-switch.

## How it works

Three layers, in order of what they catch:

1. **Health monitor (in-process).** `services/tasks/health_monitor.py` runs
   every `HEALTH_MONITOR_INTERVAL_S` (default 30s). It evaluates the shared
   checks in `services/health.py` and fires a webhook alert per issue. This is
   independent of who calls `/api/health` — the server alerts on its own
   schedule. Catches: degraded-but-running conditions (stale tasks, queue
   saturation, disk/memory pressure, solver accuracy, node dropout, etc.).

2. **Webhook delivery.** `services/alerting.py` POSTs a JSON payload to
   `ALERT_WEBHOOK_URL` (a Slack/Discord/PagerDuty incoming webhook). Alerts are
   deduplicated per `alert_type` with a `ALERT_COOLDOWN_S` cooldown (default
   300s), so an ongoing problem re-notifies at most every 5 minutes. A
   `resolved:<type>` alert is sent once when a condition clears.

3. **Dead-man's-switch (external).** `services/tasks/heartbeat.py` pings
   `HEARTBEAT_URL` every `HEARTBEAT_INTERVAL_S` (default 60s). Point it at a
   free [Healthchecks.io](https://healthchecks.io) check. The external service
   alerts when pings **stop** — the one failure mode in-process alerting can't
   catch: a crashed process, a dead host, or the disk-full deploy death-spiral.

## Severity

Each issue carries a severity in the alert payload's `meta`:

- **critical** — output is down or about to be: `stale_task:*`,
  `frame_queue_saturated`, `disk_low`, `memory_high`, `node_dropout`,
  `no_active_tracks`.
- **warning** — degraded but serving: `solver_queue_drops`,
  `solver_queue_high`, `solver_latency_high`, `anomaly_flood`,
  `solver_accuracy_degraded`, `high_miss_rate`.

Route critical → a paging channel and warning → a quieter channel in your
webhook receiver (e.g. Slack workflow rules).

## Health endpoint

`GET /api/health`

- Default: always **200**. Body `{"status": "ok"}` or `{"status":
  "degraded"}`. Used as the Docker container **liveness** check — it must not
  flip to non-200 on transient degradation or the container would restart-loop.
- `?strict=1`: **readiness** probe — returns **503** when degraded. Point an
  external uptime monitor (UptimeRobot/BetterStack free tier) at this for an
  independent outside-in alert.

Details are intentionally **not** exposed on this unauthenticated endpoint —
they're in the logs and the webhook payloads.

## Setup checklist (no servers to run)

1. Create a Slack/Discord incoming webhook → set `ALERT_WEBHOOK_URL`.
2. Create a free Healthchecks.io check (period 1m, grace ~2m) → set
   `HEARTBEAT_URL` to its ping URL. Configure its notification channel.
3. (Optional) Add an UptimeRobot/BetterStack monitor on
   `https://<host>/api/health?strict=1`.

| Env var | Default | Purpose |
| --- | --- | --- |
| `ALERT_WEBHOOK_URL` | _(unset → disabled)_ | Where alerts are POSTed |
| `ALERT_COOLDOWN_S` | `300` | Per-alert-type re-notify cooldown |
| `HEARTBEAT_URL` | _(unset → disabled)_ | External dead-man's-switch ping target |
| `HEARTBEAT_INTERVAL_S` | `60` | Heartbeat ping period |
| `HEALTH_MONITOR_INTERVAL_S` | `30` | Health evaluation period |
| `NODE_DROPOUT_THRESHOLD` | `0.8` | Active/peak node ratio below which dropout fires |

## Deferred (needs real infrastructure)

Metrics history and dashboards (Prometheus + Grafana, Loki for logs, Sentry for
exceptions) are **not** required for launch — the webhook + heartbeat cover
"something is wrong, tell a human." Add them later if you want trend graphs or
exception aggregation; they require standing up and maintaining services.
