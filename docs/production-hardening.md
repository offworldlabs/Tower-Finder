## 1. Security

### 1.1 Authentication secret has a hardcoded fallback

The server uses a default signing key if the real one isn't configured in
the environment. In development that's fine, but in production the server
should refuse to start with a clear error message rather than quietly fall
back to a known default value.

**Priority:** high
**Effort:** small (~10 lines in one file)

### 1.2 Container runs with more permissions than it needs

Standard starting point for a new project, but before going to production
it's worth tightening this up. The container should run as a dedicated
non-root user rather than with full system-level access. This is a standard
hardening step expected by any security review.

**Priority:** high
**Effort:** small (~15 lines in Dockerfile + a minor config adjustment)

### 1.3 Add standard web security headers

The nginx config doesn't yet include the HTTP security headers that modern
browsers and security scanners look for — things like HSTS (forces HTTPS),
clickjacking protection, and content type enforcement. These are a
one-time addition that meaningfully improves the posture of any public-facing
admin dashboard.

**Priority:** high
**Effort:** very small (~8 lines in nginx.conf)

### 1.4 Add rate limiting on the API

The API currently accepts unlimited requests from any client. Adding a
basic request rate limit at the nginx layer is standard practice for
any public-facing API and protects against accidental overload.

**Priority:** medium-high
**Effort:** very small (~6 lines in nginx.conf)

### 1.5 Nodes connect without authentication

Nodes can currently announce any identity when connecting. Worth addressing
as the deployment scales — a shared token as a first step, mutual TLS as
the longer-term approach.

**Priority:** medium (port isn't publicly advertised, lower urgency)
**Effort:** moderate (~30 lines across two files)

---

## 2. Reliability

### 2.1 Background task errors need structured logging

Background tasks currently use broad exception handling to keep the system
running through transient failures — a reasonable approach for stability.
The next step is to make those failure points visible: each catch block
should write to the log and increment a counter that surfaces in the
metrics dashboard, so issues can be caught proactively.

**Priority:** high
**Effort:** small (~40 small changes across 3 files)

### 2.2 Shared state should have explicit locking

Several core data structures are accessed from multiple concurrent threads.
Python's GIL handles most cases correctly, but compound read-modify-write
operations on the same structures can behave unexpectedly under high
concurrency. Adding explicit locks to the three most-contended structures
is the standard solution and closes this gap cleanly.

**Priority:** high
**Effort:** moderate (~45 lines across 4 files)

### 2.3 Pin Python dependency versions exactly

Several Python dependencies are pinned loosely, meaning a fresh Docker
build could pull in a newer version that changes behaviour or breaks
compatibility. The key ones to watch are numpy (had major breaking changes
in version 2.0) and scipy (optimizer API changed in 1.11). We should pin
everything to exact versions based on a known-good build.

**Priority:** high — this can cause a broken build with no code changes
**Effort:** very small (6 lines in requirements.txt)

### 2.4 Add lightweight state persistence across restarts

All runtime state lives in memory. A container restart means nodes have to
reconnect, track histories reset, and analytics rebuild from scratch. Worth addressing at some point — a lightweight recovery mechanism
(saving a snapshot to disk periodically and reloading it on startup)
would make restarts much less disruptive.

**Priority:** medium
**Effort:** moderate (~100 lines for save/restore logic)

### 2.5 Node configuration should be validated on arrival

The node connection handshake currently trusts incoming configuration
values without range-checking them. A node with a misconfigured position
or frequency would produce incorrect analytics output. Adding basic
bounds validation at connection time is a small change that makes the
pipeline more robust against misconfiguration.

**Priority:** medium
**Effort:** small (~20 lines)

---

## 3. Observability

### 3.1 Expand internal metrics

The system currently exposes a single counter for dropped frames. Expanding
this into a proper metrics endpoint — tracking frame throughput, queue
depths, solver success rates, and per-task health — would make operational
diagnostics significantly faster and eliminate the need to read raw logs
for routine troubleshooting.

**Priority:** high
**Effort:** moderate (~120 lines total)

### 3.2 Background task health should be surfaced in the health endpoint

Background tasks should record a last-success timestamp alongside their
error count. The existing health endpoint can then surface a warning when
any task hasn't completed within its expected interval — turning silent
staleness into a visible, actionable signal.

**Priority:** medium
**Effort:** small (~30 lines)

---

## 4. Code Maintainability

### 4.1 Consolidate tuning constants into one place

There are roughly 15 numeric constants scattered across the codebase that
control meaningful system behaviour — things like how long before a node
is considered offline, how often the solver runs, or what tolerance the
delay matching uses. Adjusting these for a specific deployment means editing several different
files. Collecting them in one place makes tuning and auditing significantly
easier.

**Priority:** medium
**Effort:** moderate (new constants file + import updates)

### 4.2 Add type definitions to shared state

Most of the core shared state uses plain Python dicts with no type
definitions — the expected keys and value types are only known by convention.
Defining explicit types for the most important structures means errors show
up during development rather than as runtime surprises.

**Priority:** medium
**Effort:** moderate (~60 lines of type definitions)

### 4.3 Split background tasks into separate modules

All background processing — analytics refresh, aircraft flush, reputation
scoring, cloud sync, and more — lives in a single 850-line file. Splitting
this into separate modules with a shared task harness makes it easier to
work on individual tasks, add tests, and trace issues.

**Priority:** medium (maintainability improvement, not a correctness issue)
**Effort:** moderate (mostly moving existing code)

---

## 5. Infrastructure

### 5.1 Set up a CI pipeline

Every deployment is currently a manual process. There's no automated step
that runs tests or verifies the build before code reaches production. A basic GitHub Actions pipeline — run tests, build the frontend, build
the Docker image — would catch the majority of accidental breakage before
it reaches production.

**Priority:** high
**Effort:** small (one config file, ~40 lines)

### 5.2 Add resource limits to the production container

The production Docker Compose config doesn't cap how much memory or CPU
the container can use. The test config already does this — we should bring
the production config in line. It gives the system more predictable
behaviour under load.

**Priority:** medium
**Effort:** very small (5 lines)

### 5.3 Make the container self-healing on backend crash

The startup script runs the application server in the background and nginx
in the foreground. If the application server crashes, the container stays
up and nginx returns errors, but nothing restarts it. Adding a process
supervisor or adjusting the startup order would make the container
self-healing.

**Priority:** medium
**Effort:** small (~20 lines)


---

## 6. Testing

### 6.1 Add tests for the core pipeline

The most critical parts of the system — frame processing, the detection
pipeline, TCP handling, the geolocation solver — have no automated tests.
That's a reasonable trade-off for a fast-moving prototype, but it's worth
adding basic coverage on each of these: at minimum a happy path test and
one error case.

**Priority:** medium-high
**Effort:** moderate (~300 lines across 3 test files)

### 6.2 Add linting and tests to the frontend

Standard starting point for a fast MVP. Adding ESLint first (quick to set up,
catches obvious issues immediately) and basic component tests later will
reduce regressions as the UI grows.

**Priority:** medium
**Effort:** Phase 1 (ESLint) is under an hour

### 6.3 Migrate frontend to TypeScript

Both `frontend/` and `dashboard/` are plain JavaScript. Migrating to TypeScript
catches type mismatches between API response shapes and component expectations
at build time rather than as runtime surprises. This is especially valuable
given the number of nested structures coming from `/api/radar/analytics`,
`/api/radar/nodes`, and the aircraft WebSocket — all of which have complex,
variant-heavy shapes that are currently untyped. The migration can be done
incrementally: rename files to `.tsx`/`.ts`, add `tsconfig.json` and the
`@types/*` packages, then tighten types file by file.

**Priority:** medium
**Effort:** moderate (~1–2 days to rename + add tsconfig + fix initial errors;
  typing all API shapes fully is a longer tail)

---

## Suggested order of work

```
Week 1  — Security foundations
  [1.1] Enforce auth secret at startup
  [1.2] Non-root container
  [1.3] Security headers in nginx
  [1.4] API rate limiting
  [2.3] Pin all dependency versions
  [5.1] Basic CI pipeline

Week 2  — Reliability + visibility
  [2.1] Log all background errors
  [3.1] Internal metrics endpoint
  [3.2] Background task health tracking
  [2.2] Thread safety locks on shared state
  [5.2] Resource limits in production compose

Week 3  — Validation + infrastructure
  [2.5] Validate node configuration on connect
  [5.3] Process supervisor in container
  [4.1] Consolidate tuning constants

Month 2 — Code quality + testing
  [4.2] Type definitions for shared state
  [4.3] Split background tasks into modules
  [6.1] Core pipeline tests
  [6.2] Frontend ESLint + basic tests
  [6.3] Migrate frontend to TypeScript

Month 3 — Durability + auth
  [2.4] State snapshot and restore on restart
  [1.5] Node authentication (shared token → mutual TLS)
```

Each item is a self-contained change that can be reviewed and shipped
independently. The goal is to avoid big-bang refactors — every week ships
something concrete, and the system gets measurably more solid throughout.
