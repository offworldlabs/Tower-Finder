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
   by the `hostname:` each droplet overlay gives its `server` service, or
   `unknown` if that call fails or returns empty), because each droplet's
   `ALERT_WEBHOOK_URL` points at its own channel: channel routing is
   configuration, and a misrouted URL would otherwise put an alert in the
   wrong channel with nothing in the payload to reveal that.
   `ALERT_ENVIRONMENT` is deliberately its own setting rather than `RETINA_ENV`:
   that variable selects which backend guards apply, and staging and test both
   hold it at `test` for the build-out's auth-guard workaround (ClickUp
   86cb1emcx), so a field sourced from it could not tell those two apart, and
   would move whenever a guard decision did. The ClickUp branch exists because ClickUp has no inbound
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

   Delivery is retried up to three times, with jittered exponential backoff
   between attempts. The retry exists because ClickUp's chat API returns
   intermittent 500s (roughly one delivery in three, measured on production,
   with no 429s), which the health monitor survives, its conditions being
   still true at the next cycle, but `mender_unreachable` and
   `registration_held` do not, since both fire once at the moment they
   matter.

   What may be retried follows the same three-way split this repo hands its
   own nodes as `x-retry` (see `routes/node_responses.py`):

   | Outcome | Treatment |
   |---|---|
   | 2xx | Delivered |
   | 3xx | Terminal. Redirects are not followed, since 301/302/303 turn the POST into a GET and would deliver nothing, so a redirecting URL means the alert went nowhere |
   | 5xx, transport error | Retry with backoff: the request never reached a handler that made a decision |
   | 429, 408 | Retry, honouring `Retry-After` over the backoff when it asks for longer (delta-seconds only, capped at `_MAX_RETRY_AFTER_S`) |
   | any other 4xx | Terminal. The sink has decided about this token (401) or this body (400), so resending multiplies one failure into three |

   `Retry-After` is honoured on any retriable answer, not only the 4xx pair:
   a 503 in a maintenance window carries it as readily as a 429 does.

   Only one delivery per `alert_type` runs at a time. The cooldown
   reservation alone cannot guarantee that, because a retry sequence can
   outlast a short `ALERT_COOLDOWN_S`, so an in-flight marker does it
   directly: the next health cycle will not open a second delivery of an
   alert that is still being retried.

   What happens to the reservation afterwards depends on why the delivery
   failed. A terminal 4xx or 3xx keeps it: nothing will change until an
   operator acts, and re-reporting every cycle only floods the channel. An
   exhausted retriable failure replaces it with an explicit deadline
   `_FAILURE_REOPEN_S` out, since the sink may be back well before the full
   window is out, but a dropped alert must not buy a full `ALERT_COOLDOWN_S`
   of silence either. That constant is sized so a failing sink is never
   asked to carry more than it did before retries existed: three attempts
   per window against one per `HEALTH_MONITOR_INTERVAL_S` before, so the
   window has to cover a whole sequence of attempts.

   A delivery that fails every attempt, and any terminal answer, is logged at
   `error`; the individual retriable failures are logged at `warning`. All
   of them keep the `Alert webhook returned <code>` wording, which is what
   the droplet logs are grepped for when counting delivery failures.

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
  `solver_accuracy_degraded`, `high_miss_rate`, `config_degraded`.

Route critical → a paging channel and warning → a quieter channel in your
webhook receiver (e.g. Slack workflow rules).

## Thresholds

Most are constants in `services/health.py`. Two are settings, because the
right value depends on the box: `NODE_DROPOUT_THRESHOLD` (default 0.8) and
`HIGH_MISS_RATE_THRESHOLD` (default 0.98). Both are read per call and fall
back to their default on a value that does not parse, so a stray entry
degrades one check rather than stopping the server booting.

`high_miss_rate` needs care when reading it. The rate counts ADS-B aircraft
inside a node's theoretical beam wedge that the node's tracker did not
detect, and for passive bistatic radar that wedge is a much larger set than
what is physically detectable, so the rate has a high floor set by siting and
physics rather than by health. Production reports 72-94% when it is working.
The threshold sits above that band so the check is a tripwire for a network
that has genuinely gone blind, rather than a running commentary; it is a
stopgap, and replacing the measure with one that tracks a node against its
own history is tracked in ClickUp 86cb81gkn.

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
| `ALERT_ENVIRONMENT` | _(unset → `unknown`)_ | Labels each alert's `environment` field |
| `HEARTBEAT_URL` | _(unset → disabled)_ | External dead-man's-switch ping target |
| `HEARTBEAT_INTERVAL_S` | `60` | Heartbeat ping period |
| `HEALTH_MONITOR_INTERVAL_S` | `30` | Health evaluation period |
| `NODE_DROPOUT_THRESHOLD` | `0.8` | Active/peak node ratio below which dropout fires |
| `HIGH_MISS_RATE_THRESHOLD` | `0.98` | Fleet-average miss rate above which `high_miss_rate` fires |

## Deferred (needs real infrastructure)

Metrics history and dashboards (Prometheus + Grafana, Loki for logs, Sentry for
exceptions) are **not** required for launch — the webhook + heartbeat cover
"something is wrong, tell a human." Add them later if you want trend graphs or
exception aggregation; they require standing up and maintaining services.
