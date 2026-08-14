# Real Node Bench Test: Design

> **For agentic workers:** this is a test design, not an implementation plan. Steps use checkbox (`- [ ]`) syntax so a run can be tracked against them. Do not start a phase whose prerequisites in §3 are unmet, and do not reorder §7: the phases are ordered by what they cost to undo, and §7.9 is last for that reason.

**Goal:** Put one board we physically hold through everything the node ingest work is building, end to end into staging, and find out what breaks before twelve boards do it at once in production on 2026-08-16.

**Ticket:** [86cb5b5p5](https://app.clickup.com/t/86cb5b5p5). Parent [86cb2cxdx](https://app.clickup.com/t/86cb2cxdx). Blocks [86cb2cz4f](https://app.clickup.com/t/86cb2cz4f), the production cut-over. Sibling, not substitute: [86cb2cyt9](https://app.clickup.com/t/86cb2cyt9), the conformance harness.

**Spec:** `nodes_api_v1.yml` at 1.1.1. **Coverage target:** `docs/superpowers/plans/2026-08-06-node-ingest-minimal.md`, tasks 1 to 11.

**Status:** Design, written 2026-08-14. The run is gated on §3.

---

## 1. Two constraints that do not move

### 1.1 Staging only

Every step in this document runs against staging. Nothing here touches production, and no result from this test authorises a production run: [86cb2cz4f](https://app.clickup.com/t/86cb2cz4f) is where that decision is taken, with these results in hand.

The one shared resource is the Mender tenant. Registration resolves `node_id` against Mender's management API while the request is open, and that lookup is a read: it does not enrol, accept or modify anything, so a staging registration leaves the fleet's Mender state as it found it. Assumption A12 of the phase 1 ADR records no published rate limit on any Mender plan and calls the fleet's projected five to seven thousand calls a day modest but not something we are entitled to. A handful of registrations sits well inside that; a retry loop would not, which is why §7.4 bounds the limiter probing to a throwaway identity.

### 1.2 Nothing points at a real `node_id`

The standing rule is that no test tooling ever registers under an identity we do not physically hold, because registration is how a node's token is taken away from it. The conformance harness carries the same rule in its own plan, in the words that it must never be pointed at the twelve real identities.

This test is the sanctioned exception. Three things hold at once here: the target is staging, the board is on the bench in front of us, and no other process is streaming that identity. Remove any one and the rule applies again in full. Nothing in this document licenses pointing anything at the other eleven identities, at any identity in production, or at an identity whose board is not physically accounted for.

The mechanism is worth stating precisely, because the exposure is deliberate and documented. Re-registration is not gated: the reactivation gate was dropped from this phase because no operator subcommands exist and the gate would permanently brick any reflashed board. A successful re-registration mints a new token, revokes the previous one, and raises a `registration_held` alert. So the rule protects against exactly what the plan says it accepts: anyone who can register under a known identity can take that node over. Contract 1.1.1 describes a stricter server, in which an identity already holding a valid token is refused; §11.1 records the divergence.

---

## 2. What this proves that the conformance harness cannot

The harness proves the server matches the contract, using a fake node and synthesised frames whose numbers are the right shape rather than true. It is the right tool for the contract and the wrong tool for four things, all of which need a real board: a real Mender record, looked up live; a token written to `/data` on real hardware and read back after a real power cycle; real cadence, which is the radar's frame rate rather than a timer, with real gaps in it; and Cloudflare in front of the whole thing, where two of the contract's open questions live.

The first of those needs work rather than an assumption, and it is the largest single gap in §3. Staging sets no Mender variables at all, so the lookup runs with an empty credential, fails, and every board gets the shared 403. Until that is fixed no board can register against staging, whoever it is. The related check is cheap: the harness's `MENDER_FAKE_ACCEPTED` bypass is gated on `RETINA_ENV` being `dev` or `test`, and staging runs `RETINA_ENV=test`, so the bypass the harness plan describes as inert on staging would in fact be live there. It exists on no branch today. P13 covers both halves, because a live lookup and a bypassed one are indistinguishable from the board, and A1 means nothing if the wrong one is in play.

The rest of this design covers the same surface the harness covers, deliberately, because a real board exercises it against a real config, a real token and a real network path. A green harness run is not evidence for any line in §7.

---

## 3. Prerequisites

State verified 2026-08-14, which is ahead of the ingest plan's own progress table.

**Merged and available to test:** Alembic and the three tables (#152, #166), the shared configuration validator (#165), tokens and bearer authentication (#164), the Mender lookup (#170), the pipeline handoff (#168), the limiters (#174, though nothing routes to them yet), and the Cloudflare origin boundary (#150).

**Not yet:**

| # | Prerequisite | Why it blocks | Where |
|---|---|---|---|
| P1 | `beam_width_deg` required-and-nullable, in the validator and the column | A real board carries no antenna geometry, so its config is refused twice over: on the numeric bound and on `NOT NULL`. Registration fails and nothing after it runs | PR #175, [86cb5auz8](https://app.clickup.com/t/86cb5auz8) |
| P2 | `POST /v1/nodes/register` | No handler exists | [86cb2cy3v](https://app.clickup.com/t/86cb2cy3v) |
| P3 | `PUT /v1/nodes/config` | No handler exists | [86cb51nr7](https://app.clickup.com/t/86cb51nr7) |
| P4 | `POST /v1/nodes/detection` and `POST /v1/nodes/heartbeat` | No handlers exist | [86cb51p1v](https://app.clickup.com/t/86cb51p1v) |
| P5 | Body caps and router mounting | Until the router is mounted every path is a 404 behind the caps | PR #176, open and green, [86cb51p76](https://app.clickup.com/t/86cb51p76) |
| P6 | The three agreement records carry real versions | Registration is built against placeholder versions today, and the split is a phase 1 deadline falling before the first real node streams. A board registering under a placeholder has agreed to nothing | Q3 and A11 of the phase 1 ADR |
| P7 | A way to revoke a token and to set `status = "blocked"` on staging | §7.9 cannot be provoked otherwise | see below |
| P8 | `ALERT_WEBHOOK_URL` set on staging | Otherwise every alert returns quietly and any check asserting one passes against nothing | [86cb2cyft](https://app.clickup.com/t/86cb2cyft) |
| P9 | `LOG_LEVEL` is not set above INFO on staging | The application root logger is configured at INFO by default and `LOG_LEVEL` is set in no compose file or deploy script, so the INFO evidence in §8 should be emitted. Confirm rather than assume: several comments in the repo assert the opposite, that uvicorn leaves the root at WARNING | check |
| P10 | The bench `node_id` is absent from staging's `blah2_nodes.json` and from the `nodes` table | Four writers key `connected_nodes` by `node_id`, the bridge, the v1 handoff, the shared-key HTTP route and the legacy TCP handler, with no cross-path uniqueness check, so a collision is silent last-writer-wins and doubles the apparent frame rate. Check all four surfaces, not only the node table. Today's bridge entries are not `ret`-prefixed, so the collision is unlikely rather than impossible | check |
| P11 | The board's Mender identity is accepted, and the board is on the bench | Registration refuses until acceptance and says nothing about why | check |
| P12 | A driver per §5 | | §5 |
| P13 | Staging holds `MENDER_SERVER` and `MENDER_PAT`, and no bypass is in play | Staging sets neither today, so the lookup runs with an empty credential, raises unreachable, and refuses every board with the shared 403. `MENDER_FAKE_ACCEPTED` must also stay absent, per §2 | check |
| P14 | The conformance harness runs, if the shim is the driver | The shim is built on the harness's node client, and the harness is itself unstarted | [86cb2cyt9](https://app.clickup.com/t/86cb2cyt9) |
| P15 | One build serves all four endpoints | The handlers are being written on three separate branches and no single one serves the whole contract. Two of them also carry different `upsert_config` implementations that disagree on version numbering after a supersession, and the register branch still rejects a null beam width, so the merge is a real piece of work rather than a formality | P1 to P4 |

Four notes on that table.

**P7.** The four operator subcommands on the critical path for the first real node do not exist: clear a lockout, reactivate, set `status`, revoke or reissue a token. For this test the two §7.9 needs, setting `status` and revoking a token, are single writes against staging's database and need no subcommand. The other two are not needed here. There is no token cache in this phase, the three in-memory caches having been dropped from it, so a revocation written to the database takes effect on the next request and needs no restart. Record which mechanism was used: doing it by hand is evidence that P7 is still outstanding for production, not evidence that revocation works.

**P8.** Alerts are deduplicated per alert type for 300 s. A phase provoking the same alert twice inside five minutes sees one webhook, which reads as a missing alert if the design is not expecting it.

**P14.** If the node client is the driver, P14 does not apply. This is the one prerequisite that differs by which branch of §5 the run takes.

**P15.** Confirm on the deployed build, not on a branch: post to all four paths and check that none answers 404 behind the body caps.

---

## 4. Identifying the board

The 2026-08-13 sweep records `ret4c844c20` and `ret2b3d917e` as accepted. Which is on the bench is a question about hardware, answered on the hardware.

- [ ] Read `/data/mender/node_id` on the board. That file is written once on first boot and returned unconditionally thereafter, and it is the only authority. Do not derive the identity from `/proc/cpuinfo`, from a MAC address, or from the sweep list.
- [ ] Confirm the value is one of the two above. If the file is absent the device fell back to a `mac=` identity or never enrolled, and registering is not the fix.
- [ ] Record which identity was used, in the ticket, before anything registers.

**If the bench board is the one currently polled over the legacy `blah2_bridge` path, it stays on that path.** That path is an outbound poll: the server reaches out to the board's blah2 API and the board is passive in it. The production bridge therefore keeps polling throughout, untouched, and the v1 driver reads the same local blah2 output alongside it. Two readers of one read-only API do not contend. `blah2_bridge` and `:3012` are the rollback for the whole phase and stay up regardless of what this test finds.

`:3012` is not published on staging, which exposes 80 and 443 only. Nothing here needs it, but a tester reaching for the TCP path as a fallback will find it closed, and that is deliberate.

---

## 5. The driver

**Decision: build the blah2 frame-source shim now, and run with whichever driver is ready at the cutoff, preferring the node client.**

The node client is being built elsewhere and was verified against a mock on 2026-08-11. If it is ready it is the better driver, because it is what will run on twelve boards and its bugs are the ones worth finding. But the fallback is not free, and pretending otherwise is how the test slips: the conformance harness synthesises every uplink value and has no frame input of any kind. Pointing it at real blah2 output is new work, not a flag.

The work is small and specified:

- A module that reads the board's local blah2 output, converts each response into a `DetectionFrame`, and hands it to `FakeNode.send_frame()` in place of the synthesiser.
- **It must not inherit the bridge's read loop.** The bridge polls on a fixed one-second timer and discards a response whose timestamp it has already seen, which is correct for a poller and fatal here: against a radar producing every 886 ms, roughly one frame in nine is never observed, and §7.5's inter-arrival distribution would then be measuring the shim's aliasing. Poll several times faster than the frame rate and emit on each new timestamp, or read blah2's output directly.
- **It must not inherit `_convert_frame`'s two gates either.** That function returns nothing for a response with no detections, and drops frames older than ten seconds. An empty frame is valid and worth sending under the contract, and live captures put empty frames at between a third and a half of all frames, so a shim that drops them discards most of the traffic this test exists to measure. Reuse the arithmetic, in particular the delay axis moving from kilometres to microseconds, and not the control flow: the harness deliberately shares no code with the server.
- Everything else the harness already does: registration, the 60 s heartbeat with its phase offset, one kept-alive connection, at most one POST in flight, latest wins rather than a queue.

The harness targets Python 3.12 under `uv`, with httpx, PyYAML and jsonschema. PyYAML builds a C extension and ships per-architecture wheels, so the aarch64 story is a question rather than a formality, and no cross-architecture run has been done. Confirm the harness starts on the board as the first step of building the shim, not on the morning of the test.

**Cutoff rule.** The shim starts now and is finished before P2 to P4 land. On the day the last handler reaches staging, the test runs at the next window with the node client if it has passed its own smoke test against staging by then, and with the shim if it has not. "Nearly ready" is not a reason to wait, and the decision is made once, against that single question.

Whichever driver runs, the phases below need five things of it, and the cutoff question is whether the candidate has them, not whether it is nearly finished:

1. A per-request log: monotonic send time, the frame's own `t`, `seq`, `boot_id`, the HTTP status, the response body, and whether the connection was reused. §8 depends on it.
2. Sending a single hand-built request on demand, including malformed and oversized bodies a well-behaved client would never produce (§7.3, §7.4).
3. Posting a frame carrying a deliberately stale `config_version` (§7.9).
4. Pausing and resuming on command, for the idle ladder (§7.8).
5. Starting unattended on boot and resuming from persisted state without operator action (§7.6).

The node client is expected to have 1, 3 and 5 and may not have 2 or 4; the shim inherits 2 from the harness. Whichever driver is chosen, close the gaps before the run rather than during it.

---

## 6. Coverage

Every task in the ingest plan, and where this test exercises it. **[wire]** is visible to the board; **[server]** is a database row, a log line or an alert.

| Task | Exercised by | Observable |
|---|---|---|
| 1, auth bypass and alerting | §7.1, §7.9 | [server] `registration_held` on a re-registration is out of scope by §1.2; alerting is checked once, by hand, under P8 |
| 2, Alembic | §7.7 | [server] staging is already migrated; the restart confirms the schema serves a live node |
| 3, three tables | §7.1, §7.2 | [server] node row, config rows with `superseded_at`, token row holding only a hash |
| 4, shared config validator | §7.2, §7.3 | [wire] 400 `invalid_config` naming a field, on both register and PUT |
| 5, tokens and bearer auth | §7.1, §7.3, §7.6, §7.9 | [wire] 401 shapes; [server] hash-only storage; the token surviving a power cycle |
| 6, Mender lookup | §7.1, §7.4 | [wire] 200 on an accepted identity, 403 on an unknown one, `Retry-After` in [240, 359] and varying |
| 7, register | §7.1 | [wire] token, `node_ref`, `config_version: 1`, `server_time` ending `Z` |
| 8, PUT config | §7.2 | [wire] version stability across identical PUTs, +1 on a real change |
| 9, pipeline handoff | §7.5, §7.7 | [wire] the node on the map with geometry; [server] frames on `frame_queue` under its own `node_id`, `peer: "v1"` |
| 10, detection and heartbeat | §7.5, §7.3, §7.4, §7.9 | [wire] 202 acks, 422s, 409, 429s, heartbeat's four keys |
| 10, limiters | §7.4 | [wire] 429 with `Retry-After: 1` on detection, 403 rather than 429 on registration |
| 10, body caps | §7.3 | [wire] 413 `{"error": "too_large"}` ahead of parsing |
| 11, Cloudflare | §7.1, §7.8 | [wire] arrival through the edge, idle timeout, WAF behaviour |

Not covered, and deliberately: solver correctness, which the plan puts out of scope in terms this test adopts, that a frame landing on `frame_queue` under the right `node_id` is the whole of the requirement; and load, which is the harness's twelve-node mode and a different question.

---

## 7. The run

### 7.1 Registration, through Cloudflare, against live Mender

The first time this chain runs outside a fixture.

- [ ] Register once. Set `publication.choice` to `private` explicitly: the default is `public`, a bench board has a real receiver coordinate, and that is a household location. The choice does not gate streaming either way; only the licence does.
- [ ] Send all fifteen config fields, including `cpi_s`, `delay_tolerance_us` and `doppler_tolerance_hz`. Config bodies quoted in the ingest plan predate those three becoming required and would be refused with `detail: "cpi_s"`, alphabetically first among the missing.
- [ ] Capture the 200 body. Assert `config_version: 1`, `node_ref` of 15 characters beginning `nde`, a token of 32 to 128 characters, and `server_time` ending `Z` rather than `+00:00`.
- [ ] Confirm from the origin access log that the request arrived through Cloudflare with the client address restored, rather than an edge address.
- [ ] Compare `server_time` against the board's clock and record the offset. This is the only clock comparison that means anything; see §7.5.
- [ ] [server] Confirm the token row stores a hash and never the plaintext, and that the three agreement records landed with `publication = private`.

**Pass:** 200, the four response assertions, and the request visibly through the edge.

**On failure, read the server side.** Every refusal class returns a byte-identical `{"error": "forbidden"}` with a `Retry-After` jittered between 240 s and 359 s, unrelated to any real window, so unknown device, pending acceptance, ambiguous auth sets, Mender unreachable and rate-limited are indistinguishable to the caller. Only Mender being unreachable raises an alert. A board registering immediately after enrolling can legitimately take one 403 before its retry succeeds, because it races the acceptance sweep.

**Five failed attempts in an hour lock the identity out for the rest of that window**, whatever the cause, because the limiter spends the allowance before anything knows whether the device is real. With P13 unmet every attempt fails, so a tester who retries into a misconfigured staging burns the budget in under a minute and then cannot tell the lockout from the original fault. Check P13 by other means before the board registers at all, and treat a 403 as a reason to stop and read the server, not to retry.

**Registration is effectively one-shot.** Get the configuration right before sending it. A second registration is not a recovery path here, it is the thing §1.2 is about.

### 7.2 Configuration, and the null geometry case

Contract 1.1.1 made `beam_width_deg` required-and-nullable because no node in the fleet carries antenna geometry: retina-gui does not collect it and is not scheduled to. Null is the fleet-wide case, not an edge case.

- [ ] Register with `beam_width_deg: null` and `beam_azimuth_deg: null`. Send `null`, never `0.0`: a placeholder is wrong data the server cannot detect, and for azimuth the two mean different things, broadside against aimed due north.
- [ ] `PUT /nodes/config` with the identical body. Confirm the version does not advance.
- [ ] `PUT` it again. Confirm again.
- [ ] Change `tx_callsign`, confirm the version advances by exactly one, then restore it and confirm it advances again. Use `tx_callsign` rather than a geometry field: the node streams for four hours after this, and it should not do so holding a knowingly wrong receiver position.
- [ ] [server] Confirm the superseded rows are stamped and the newest is not.
- [ ] [server] Confirm the new version reached the pipeline.

`NULL = NULL` is never true in SQL, so a comparison done in the database mints a new version on every resend for every node with null geometry, which is all of them, and every one then reports `config_stale` for ever. The harness plan calls this the single most likely server bug, and this test has a real null-geometry board in hand.

There is no config GET. What the server holds is verified server-side or not at all.

**Pass:** version 1 after registration, unchanged across two identical PUTs, then 2 and 3 across the change and its restoration, with the node ending on its surveyed configuration.

### 7.3 The validation and refusal surface

A real board with a hand-built request is the cheapest way to check that the shapes a node will actually meet are the documented ones. All of these are recoverable, so they run before the soak.

Config validator, via `PUT /nodes/config` on the authenticated node:

- [ ] An out-of-range `rx_lat` gives 400 `{"error": "invalid_config", "detail": "rx_lat"}`. The detail is the field name alone; the reason is not on the wire.
- [ ] A payload wrong in several places names one field, stably across retries. Unknown and missing keys are reported alphabetically first; a bounds violation is reported in the validator's own field order, which is not alphabetical. Two fields out of range therefore name the earlier one in that order rather than the earlier one in the alphabet.
- [ ] A boolean where a number belongs, a numeric string, and a bare `NaN` are each refused rather than coerced or silently passed to the solver.
- [ ] A receiver and illuminator at the same point are refused, reported against `tx_lat`.

Wire models, via detection and heartbeat:

- [ ] Mismatched parallel array lengths are refused.
- [ ] A frame carrying `node_ref` is refused, as is any unknown key.
- [ ] An array of 512 is accepted and 513 refused.
- [ ] An all-empty frame is accepted, with `accepted: 0`. Empty frames are worth sending and this is the assertion that says so.
- [ ] A malformed or absent `boot_id` is refused on both endpoints. Heartbeat bodies quoted in the ingest plan omit `boot_id` entirely and would be refused; use the wire-models plan's bodies.
- [ ] Record the **shape** of every rejection body. Pydantic 422s arrive in FastAPI's `{"detail": [...]}` form, not the taxonomy's `{"error": ...}` form, and no handler converts them. A node parsing only `{"error": ...}` meets an unfamiliar body on every malformed request.
- [ ] Send a bad token to each of the three authenticated endpoints in turn and compare the bodies. As the handlers are being written, the config endpoint rewrites its 401 to `{"error": "unauthorized"}` while detection and heartbeat let the dependency's `{"detail": "unauthorized"}` through, so the same condition has two shapes depending on which path met it. Confirm whether that survived the merge in P15.

Body caps, which arrive with P5:

- [ ] An oversized registration body is refused at 8 KiB and an oversized frame at 64 KiB, in both cases before parsing, so a body that is oversized and malformed gives the size error rather than a validation error.
- [ ] Record the body. It is `{"error": "too_large"}`, a code that appears in neither the plan's taxonomy table nor the contract, which declares no 413 for any endpoint.
- [ ] Send one oversized body with chunked transfer encoding. The cap reads `Content-Length` only, so the origin does not apply it and Cloudflare's own limit is the only backstop. This is the one case where the answer is genuinely unknown before the test.

The ordering rule that configuration validation runs after identity resolution is a security property: a 400 reachable before it makes the difference between 400 and 403 an oracle for which identities exist. Proving it fully needs two registrations with the same bad body under two different identities, which this test will not do, because both halves would have to be real identities. The safe half is checked here:

- [ ] Register with a deliberately invalid config under an identity chosen to be outside the fleet's serial-derived space, and confirmed absent from Mender first. Expect 403, not 400.

The other half is the harness's job, against seeded identities. Record it as covered there, not here.

**Pass:** every rejection above produces the documented status; the two shape findings (422 form, 413 code) are recorded whether or not they are judged defects; the chunked case has an answer.

### 7.4 The limits

Keyed per identity, so the throwaway identity from §7.3 absorbs the registration probing and the bench board's own budget is left intact.

- [ ] On the throwaway identity, register six times within the hour. Expect the sixth refused with the shared 403 body and the shared `Retry-After`, not a 429. A 429 would tell an unregistered caller that the identity is real.
- [ ] Confirm the sixth attempt is refused at all, and record it as a divergence if it is. The contract says the counters exclude attempts that could never have succeeded, so that an unknown identity is never counted and a node waiting on Mender acceptance is not punished for being early. The limiter as written spends the allowance on the supplied `node_id` before anything knows whether the device exists, which is the opposite. The consequence is §7.1's lockout, and it lands on a real board on a slow acceptance, so the answer decides whether the twelve can safely retry on the day.
- [ ] Time those refusals. A refusal returning without spending the Mender timeout budget is the observable for the limiter firing before the Mender call, which is what keeps the unauthenticated endpoint from amplifying against the tenant.
- [ ] Post configurations faster than 30 in a minute and record what happens. The limit is defined but the config endpoint does not call the limiter, so the expected result is that nothing stops it.
- [ ] Confirm `Retry-After` on 403s falls in [240, 359] and varies between calls. A fixed value, or one that correlates with the reason, is the leak this guards.
- [ ] On the bench node, post detections as fast as the driver will go until a 429 appears. Confirm the body is `{"error": "rate_limited"}` and `Retry-After` is 1, the window being one second, and that the driver drops the skipped frames rather than accumulating them.
- [ ] Confirm the node returns to 202 on the next window without operator action.

Do not assert an escalating cooldown. It is in the contract and was explicitly dropped from this phase.

**Pass:** the registration limit refuses as a 403, the detection limit as a 429 with a one-second retry, and neither leaves the node stuck.

### 7.5 The soak

**Four hours continuous as the pass condition, overnight as the confidence run if the bench is free.**

An hour shows the cadence. Four hours crosses hourly boundaries several times, gathers enough inter-arrival samples that a ten per cent drop in rate is visible rather than plausible noise, and gives the edge time to do something a short run would miss. At roughly 1.1 Hz that is around 16,000 frames and 240 heartbeats. The idle timeout is not among the reasons: at this cadence the connection is never idle, which is why §7.8 provokes it separately.

- [ ] Stream continuously. Confirm the node carries `peer: "v1"`. That field takes five shapes across the ingest surfaces: `v1`, the hostname for a bridge poll, `http` and `http-bulk` for the shared-key route, and the repr of a Python address tuple for the legacy TCP handler. It identifies which path a node arrived on rather than merely distinguishing two, and only the first is a pass here. The roster it appears on is a snapshot rebuilt every 30 s, so treat it as a 30 s-granular check.
- [ ] Sample the admin metrics every 60 s to a file for the whole window.
- [ ] Confirm the node renders on the staging map under its own `node_id`, with a detection area.
- [ ] Confirm heartbeats return exactly the four documented keys, and that `config_stale` is false while the versions agree.

Two things about the map that would otherwise read as failures. The rendered position is deliberately fuzzed by a few hundred metres by a per-identity offset, so the marker not sitting on the surveyed point is correct. And one node never produces a solve, because candidates need two, so solver successes stay at zero and the only tracks are the node's own. Detections reaching the map means the node and its detection area are rendered and attributed; it does not mean aircraft are being fixed.

**Cadence, lag and loss.** The node has no send cadence: it posts once per frame blah2 produces and never otherwise, so the arrival rate is the radar's frame rate. One board configured `cpi: 0.5` was measured emitting every 886 ms; another emitted every 951 ms, with no configuration recorded for it. 2 Hz is the ceiling and 1.1 Hz is what to expect.

The trap is the lag. blah2 stamps `t` when the buffer holds a full CPI, before any processing, so a frame reaches the server roughly one CPI-processing-time after its own `t`, about 900 ms today. That is not clock skew, and anything comparing `t` against arrival must not read it as such. The two are separable and this test separates them: lag is arrival minus `t`, measured by pairing the driver's send log with the origin access log on `seq`; skew is the board's clock against `server_time`, which arrives on heartbeat responses and nowhere else, since the detection ack does not carry it. A lag that drifts is processing falling behind. An offset that drifts is NTP on a board with no battery-backed clock.

- [ ] Compute the inter-arrival distribution over the whole soak: median, 5th and 95th percentiles, longest gap.
- [ ] Compute the lag distribution and confirm the median sits between 0.5 s and 2.0 s.
- [ ] Record the clock offset at the start, middle and end.
- [ ] Count frames sent, from the driver's `seq`, against frames acked 202, and account for every difference.

- [ ] Read the per-node frame total from the admin leaderboard at the start and end of the soak, and difference it. This is the one server-side count of this node's frames that exists.

On gaps being counted rather than papered over: there are no accepted or rejected counters, nothing tracks sequence discontinuities, and `boot_id` has no column, so restart-versus-gap accounting has nowhere to live yet. The design intent is that `(boot_id, seq)` is what loss and wedge detection are computed from. This step verifies the intent against what landed. If the merged handlers expose no accounting, the evidence comes from the node side and the access log, and the absence is a finding rather than a result to shrug at.

One thing to confirm rather than assume, because it is the node's only signal that a frame did not land: a frame dropped for a full queue is answered 202 with `accepted: 0`, so the status says the request was fine and the count says the detections were not taken. A node reading only the status cannot tell a delivered frame from a discarded one, and `accepted` is the field that distinguishes them.

**Pass:** four hours uninterrupted, the node continuously present and attributed, median inter-arrival between 0.7 s and 1.2 s, median lag between 0.5 s and 2.0 s, every gap accounted for, clock offset under a second and not drifting.

### 7.6 Reboot the board

The token is the one thing the node persists. `boot_id` is generated in memory and never written, and `seq` is restart-local.

- [ ] Power-cycle the board. Not a process restart: the point is the disk and the clock as well as the process.
- [ ] Confirm streaming resumes on the original token, without registering again. A 401 means the token did not survive; a registration attempt is a defect in the driver.
- [ ] Confirm `boot_id` differs and `seq` restarts from 0.
- [ ] Confirm the reset is harmless: frames keep being accepted across it and nothing treats the `seq` drop as an error. Do not expect the restart to be recorded anywhere. `boot_id` is validated on both endpoints and carried into the frame handed to the pipeline, but it is stored in no column, the heartbeat reads it and discards it, and nothing computes restart against gap from `(boot_id, seq)`. The contract's reason for requiring the field on every frame is unimplemented, which is a finding for the cut-over rather than a failure of the board.
- [ ] Confirm the first heartbeat after boot carries `config_version: null`, that the response reports `config_stale: true` because the server holds a version the node does not, and that the node PUTs its config and obtains a version before its first frame.

That last step reads as a bug and is not one. `config_version` is deliberately not persisted on the node while the token is, so every start has a window where the node genuinely holds none. It cannot post a frame until it has one, because the field is required and non-null on a detection frame, so a reboot necessarily means a config PUT before streaming resumes.

**Pass:** streaming resumes on the original token, `boot_id` changes, `seq` resets, and the null-version window closes without operator action.

### 7.7 Restart the server

The failure most likely to be found late, because everything looks fine until a deploy. The ingest plan insists on this check by hand and says not to skip it because the unit tests pass.

- [ ] Restart the staging app while the board keeps streaming.
- [ ] Confirm the node is still listed afterwards, without re-registering. It could not re-register if it wanted to.
- [ ] Confirm a heartbeat posted after the restart returns 200 rather than 500.
- [ ] Confirm the priming log line reports a count of at least one, and that the warning naming this node as having no active configuration is absent. Both of those are emitted at WARNING and will be visible whatever P9 resolves to. The line naming a successfully primed node is at INFO, which is what P9 exists to keep visible.
- [ ] Confirm frames resume within one detection interval and the heartbeat within one interval. The heartbeat's phase offset is applied once at node process start, and a server restart does not restart the node, so it does not widen this window.

The registry the pipeline reads is in-process and starts empty, so priming from the database is the whole of the mechanism, backed by the heartbeat re-registering a node it cannot find. Priming is swallowed on failure by design, raising an alert and continuing with an empty fleet rather than taking the API down, so a silent failure presents as the node vanishing and nothing else. Check the log line, not just the map.

**Pass:** the node is primed from the database, reappears without re-registering, and the heartbeat after the restart is a 200.

### 7.8 The edge

Two of the contract's open questions are Cloudflare's, and they block the first real node streaming through production. This phase runs on two clocks. Its observations belong inside §7.5 and change nothing; its probes interrupt the stream and so cannot, which is why they sit here, after the soak has passed and before §7.9 revokes the token they need.

**Observed during the soak, changing nothing:**

- [ ] Count every response that is not the endpoint's documented success, which is 202 for a detection and 200 for a heartbeat or a config PUT, and separate those that did not come from the origin at all: a Cloudflare error page rather than a JSON body is the signature.
- [ ] Note whether responses degrade in the last hour relative to the first, which is what a rate-based rule would look like.
- [ ] Confirm the origin logs the board's real address rather than an edge address.

**Run after the soak has passed**, because it deliberately interrupts the stream for about an hour in total:

- [ ] With the connection warm, pause the driver for 2 minutes, then post. Record whether the POST succeeds on the existing connection or the connection was reset.
- [ ] Repeat at 5, 10, 15 and 20 minutes. Record the first interval at which the connection does not survive.
- [ ] Record how the driver recovers and how many frames were lost to the reconnection.
- [ ] Confirm a direct request to the origin, bypassing the edge, is refused.

**Pass:** the idle timeout is a measured number rather than an open question, and either the WAF is shown to be inert on this traffic or the rule that fires is identified.

### 7.9 The refusals

Provoked deliberately, and last, because the token is at stake. In this order.

**409, an unknown `config_version`.** Recoverable, so first.

- [ ] Advance the server's active version with a config PUT that changes a field, then post a frame carrying the previous version. Expect **202 with `config_stale: true`**, not a 409: a version the server once issued is still known to it, and the soft signal is what asks for the PUT. Confirm the driver re-PUTs on that signal alone, adopts the returned version and resumes.
- [ ] Post a frame carrying a version the server has never issued, a large integer will do. That is the 409 case. Confirm `{"error": "unknown_config_version"}` and the same recovery.
- [ ] Confirm the same staleness is reported identically on the heartbeat, which is the only channel a node has when it is not sending frames.

The distinction matters more than it looks. A node that treats 409 as its only cue to re-PUT will never re-PUT in the case that actually happens in the field, which is a superseded version, and will sit reporting `config_stale` indefinitely.

**A blocked node.** Recoverable.

- [ ] Set `status = "blocked"` for this node. `streaming_allowed` is derived from that column and is true only for `active`, so `retired` pauses a node identically.
- [ ] Confirm detections return 202 with `streaming_allowed: false` and `accepted: 0`, and that nothing reaches the pipeline. The node is told to pause, not refused, and a 403 here would be the bug.
- [ ] Confirm the driver stops sending detections and keeps heartbeating at 60 s, reporting state `paused`.
- [ ] Confirm it learns it may resume from the flag on a heartbeat response, since it is no longer sending the detections that would carry an ack.
- [ ] While it is blocked, confirm the heartbeat does not quietly restore it to the pipeline registries. Recovery on a missing registry entry is deliberately reserved for active nodes, so a blocked node stays out.
- [ ] Clear the status and confirm streaming resumes.

**401, a revoked token.** Not recoverable without P7, which is why it is last.

- [ ] Revoke the token. There is no expiry, so revocation is the only way a token stops working.
- [ ] Confirm 401 `{"error": "unauthorized"}`, and that a malformed `Authorization` header gives the same.
- [ ] Confirm the node stops streaming, surfaces the failure locally, and **does not register again**. Treating a 401 as a trigger to re-register turns a deliberate revocation into a registration storm, and this is the behaviour most likely to differ between the node client and the harness.
- [ ] Confirm heartbeats continue, so the failure stays visible.
- [ ] Record how the board was recovered, and by what mechanism.

**Pass:** each refusal produces the documented status and node behaviour, and the 401 produces no registration attempt of any kind.


---

## 8. Evidence

Captured for the whole window and attached to the ticket. Without P9 most of the server side is never emitted.

**Server.** Container logs for the window, filtered to this node. Worth grepping: the priming count and the not-primed warning for §7.7, and the frame-queue and solver-queue drop warnings for §7.5. Two cautions. The one merged line containing the word `registered` is the pipeline handoff, not the registration endpoint, so grepping it for §7.1 finds the wrong event; what the handlers log is not yet known and should be established when they land. And the bearer dependency imports no logger at all, so a 401 leaves no server-side trace and §7.9's auth evidence comes from the node side and the access log. The structured event log and the admin metrics, sampled every 60 s, are the rest of it.

**Origin access log.** Arrival timestamp per request, the restored client address, status, body size. This is the authority for arrival time in §7.5 and for what the edge did in §7.8.

**Node.** The driver's per-request log from §5, plus the board's own system log across the reboot, so the token surviving is shown rather than inferred.

**Map.** A screenshot, since the acceptance is written in terms of the map, plus the analytics response saved for the detection area and the attribution. **Redact the receiver coordinates before attaching either.** The position fuzzing is applied in the browser; the API serves the surveyed latitude and longitude verbatim. Attaching that response unredacted to a ticket would publish the household location that §7.1 sets `publication.choice: private` to protect. The node roster carries `peer`, `status` and `last_heartbeat` and no coordinates.

**Derived.** The inter-arrival and lag distributions, the idle-timeout number, and one table of every response that was not the endpoint's documented success, with its cause.

---

## 9. Pass and fail, decided now

| # | Criterion | Section |
|---|---|---|
| A1 | Registers once through Cloudflare against a live Mender lookup, `config_version: 1`, `server_time` ending `Z` | §7.1 |
| A2 | Null geometry is accepted, identical PUTs do not advance the version, a real change advances it by one, and the node ends on its surveyed configuration | §7.2 |
| A3 | The validator, wire models and body caps refuse what they should, and the two shape findings are recorded | §7.3 |
| A4 | Registration limit refuses as 403, detection limit as 429, neither leaves the node stuck | §7.4 |
| A5 | Streams and heartbeats for four hours, present and attributed on the map throughout | §7.5 |
| A6 | Median inter-arrival 0.7 s to 1.2 s, median lag 0.5 s to 2.0 s, every gap accounted for, clock offset under a second and not drifting | §7.5 |
| A7 | Survives a board reboot on the same token, new `boot_id`, `seq` from 0, and the null-version window closes without operator action | §7.6 |
| A8 | Survives a server restart, primed from the database, heartbeat afterwards is a 200 | §7.7 |
| A9 | The edge idle timeout is a measured number and WAF behaviour is characterised | §7.8 |
| A10 | A superseded version is accepted and reported stale, an unissued one is a 409, both recover by re-PUT; a blocked node gets 202 with `accepted: 0` and `streaming_allowed: false`; 401 stops streaming and triggers no registration | §7.9 |

Anything that fails becomes a ticket before the twelve go live. A criterion that could not be run for want of a prerequisite is recorded as not run, and is not a pass.

---

## 10. What this test does not assert

Three promises the contract makes that this phase knowingly does not implement. Asserting them would fail a server that is behaving as designed.

- **Constant latency across registration refusal classes.** One shared 403 body was kept; the timing guarantee was dropped.
- **The escalating registration cooldown.** Dropped in favour of an in-process counter behind Cloudflare.
- **`node_ref` rotation.** Not in this phase, so the heartbeat field that exists to carry a rotation never changes.

Also not asserted: solver correctness or fix quality, in the plan's own terms; multi-node association, since one node cannot produce a candidate; load; an alert on `stalled`, for which none exists; and the pending branch of the Mender lookup, which cannot be reached against the live tenant because the acceptance sweep clears pending within about thirty seconds.

---

## 11. Divergences and open questions

### 11.1 Re-registration

Contract 1.1.1 says an identity already holding a valid token is refused and recovery runs through an operator reactivation. The ingest plan, as amended, permits re-registration, revokes the previous token and raises an alert, because the reactivation gate would brick any reflashed board while no operator subcommands exist. Under the first reading a stray registration is refused; under the second it takes the node off the air. This test does not resolve it and must not be used to, since probing it means registering twice under a real identity. Raised as [86cb5dbhf](https://app.clickup.com/t/86cb5dbhf), wanted before the cut-over rather than before this run: the bench test is safe under either reading, because we hold the board, but §7.9's recovery path depends on the answer.

### 11.2 Shapes not in the contract

Three, all recorded by §7.3 as findings for the node client and the contract to reconcile rather than as failures of this run. A 413 carrying `{"error": "too_large"}` appears in neither the taxonomy nor the contract, which declares no 413 anywhere. Pydantic rejections arrive in FastAPI's shape rather than the taxonomy's. And the same 401 has two shapes depending on which endpoint met it, because one handler rewrites the dependency's response and the others do not.

### 11.3 Carried forward

The `staging-node` identity already exists as a polluted value; do not let the bench node be confused for it. Three ingest surfaces are live concurrently in this phase, the bridge, `:3012` and the shared-key HTTP detection route, so confirm which one a detection arrived on before attributing it. And the generated OpenAPI shows `config` as free-form where the frozen contract shows a closed schema, which is a documentation divergence rather than a gap, and a conformance diff will flag it correctly and harmlessly.

---

## 12. Teardown

- [ ] Restore the node's status and token to a working state, and record how.
- [ ] Leave the node registered on staging or retire it explicitly. Nothing sweeps it automatically, so leaving it is a choice and should be a recorded one.
- [ ] Revert `LOG_LEVEL` if P9 required setting it.
- [ ] Confirm the board is still being polled on the legacy path, if it was at the start.
- [ ] Attach the §8 evidence to the ticket and raise a ticket per failed criterion.
