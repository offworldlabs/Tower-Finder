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

2. **Webhook delivery.** `services/alerting.py` POSTs to `ALERT_WEBHOOK_URL`.
   `ALERT_WEBHOOK_FORMAT` selects the body shape: `raw` (default) sends a
   plain JSON payload (`alert_type`, `message`, `timestamp`, `environment`,
   `host`, `meta`), for a Slack/Discord/PagerDuty incoming webhook;
   `clickup_chat` sends `{"type": "message", "content": "<markdown>"}`, for
   ClickUp's chat message endpoint, with the rendered content carrying the
   bold `alert_type`, the message, an `environment: <value>` line, a
   `host: <value>` line, then one `key: value` line per `meta` entry. Both
   shapes carry `environment` (from `ALERT_ENVIRONMENT`, or the literal
   `unknown` when unset or empty) and `host` (from `socket.gethostname()`, set
   by the `hostname:` each droplet overlay gives its `tower-finder` service, or
   `unknown` if that call fails or returns empty), because each droplet's
   `ALERT_WEBHOOK_URL` points at its own channel: channel routing is
   configuration, and a misrouted URL would otherwise put an alert in the
   wrong channel with nothing in the payload to reveal that.
   `ALERT_ENVIRONMENT` is deliberately its own setting rather than `RETINA_ENV`:
   every deployed overlay pins `RETINA_ENV=test` for the build-out's auth-guard
   workaround (ClickUp 86cb1emcx), so a field sourced from it would read `test`
   everywhere and say nothing. The ClickUp branch exists because ClickUp has no inbound
   webhook of its own (its webhooks are outbound only), so reaching a
   ClickUp chat channel needs a shaped body and an `Authorization` header
   rather than a plain POST URL. `ALERT_WEBHOOK_AUTH`, when set, is sent
   verbatim as that header (no `Bearer` prefix: ClickUp personal tokens
   carry none). ClickUp documents the chat endpoint as experimental, so the
   server logs its alert destination (scheme and host only) at startup, as a
   trail back to the dependency if the endpoint ever breaks. Alerts are
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

1. Pick a destination for `ALERT_WEBHOOK_URL`:
   - Slack/Discord/PagerDuty incoming webhook → set `ALERT_WEBHOOK_URL` to it
     and leave `ALERT_WEBHOOK_FORMAT` at its `raw` default.
   - ClickUp chat channel → create a personal token, set `ALERT_WEBHOOK_AUTH`
     to it, set `ALERT_WEBHOOK_FORMAT=clickup_chat`, and set
     `ALERT_WEBHOOK_URL` to
     `https://api.clickup.com/api/v3/workspaces/{workspace_id}/chat/channels/{channel_id}/messages`.
2. Create a free Healthchecks.io check (period 1m, grace ~2m) → set
   `HEARTBEAT_URL` to its ping URL. Configure its notification channel.
3. (Optional) Add an UptimeRobot/BetterStack monitor on
   `https://<host>/api/health?strict=1`.

| Env var | Default | Purpose |
| --- | --- | --- |
| `ALERT_WEBHOOK_URL` | _(unset → disabled)_ | Where alerts are POSTed |
| `ALERT_COOLDOWN_S` | `300` | Per-alert-type re-notify cooldown |
| `ALERT_WEBHOOK_AUTH` | _(unset)_ | Sent verbatim as the `Authorization` header when set |
| `ALERT_WEBHOOK_FORMAT` | `raw` | Payload shape: `raw` or `clickup_chat` |
| `HEARTBEAT_URL` | _(unset → disabled)_ | External dead-man's-switch ping target |
| `HEARTBEAT_INTERVAL_S` | `60` | Heartbeat ping period |
| `HEALTH_MONITOR_INTERVAL_S` | `30` | Health evaluation period |
| `NODE_DROPOUT_THRESHOLD` | `0.8` | Active/peak node ratio below which dropout fires |

## Deferred (needs real infrastructure)

Metrics history and dashboards (Prometheus + Grafana, Loki for logs, Sentry for
exceptions) are **not** required for launch — the webhook + heartbeat cover
"something is wrong, tell a human." Add them later if you want trend graphs or
exception aggregation; they require standing up and maintaining services.
