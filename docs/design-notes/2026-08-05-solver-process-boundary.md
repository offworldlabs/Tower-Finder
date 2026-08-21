# Solver Process Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the CPU-bound multinode LM solve out of the uvicorn process into worker
processes, so that the v1 node ingest endpoints built in step 2 have latency independent of
solve load.

**Architecture:** One uvicorn process keeps all of `core/state.py`, every background task and
all three of the in-memory caches the [phase 1 ADR](../../../../claude-shared/docs/decisions/2026-08-03-node-server-phase-1.md)
§6 puts there. Only `solve_multinode` and its altitude sweep leave, behind a pure function
whose arguments and return value are plain picklable dicts. The existing `state.solver_queue`
and its drain threads stay exactly as they are; each drain thread now submits to a
`ProcessPoolExecutor` and applies the result gates, EWMA smoothing and every state mutation in
the parent, where they run today.

**Tech Stack:** Python 3.12, FastAPI/uvicorn, `concurrent.futures.ProcessPoolExecutor` with the
`spawn` start method, numpy/scipy (`retina_geolocator`), pytest, ruff.

## Global Constraints

- Working directory for all commands is `~/owl/retina-server`.
- Branch first. The checkout is currently on `chore/compose-consolidation`, which is unrelated
  in-flight work; start from a fresh branch off the default rather than committing onto it.
- Stage the named paths per task. This clone carries unrelated working-tree cruft (dirty
  submodule pointers under `libs/`, local data files); never `git add -A`.
- Tests: `cd backend && .venv/bin/python -m pytest tests/ -q`. There is no top-level `python`,
  only `python3`.
- Coverage gate is `--cov-fail-under=55` in `backend/pyproject.toml` and runs on every pytest
  invocation. When running a single test file, add `--no-cov` or the run fails on coverage
  rather than on the test.
- Lint is two separate commands and both must pass: `backend/.venv/bin/ruff check backend/` and
  `backend/.venv/bin/ruff format backend/`. `ruff check` alone leaves formatting diffs.
- `line-length = 120`, `target-version = "py312"`, ruff rule set `E,W,F,I,B,UP,S,SIM`.
- uvicorn stays at `--workers 1`. The in-memory caches are per-process; a second worker halves
  the liveness view and doubles the revocation delay.
- No module reachable from `backend/services/solver_kernel.py` may import `core.state`.
- Writing style for any doc or comment changed here: no em-dashes, British spellings, state
  each fact once.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/services/solver_kernel.py` (new) | Pure solve: altitude sweep plus dispatch. No shared state, no I/O. This is the only code that runs in a child process. |
| `backend/services/solver_pool.py` (new) | Pool lifecycle: spawn-context creation, submission, crash recovery, shutdown. |
| `backend/services/tasks/solver.py` (modify) | Loses the sweep functions to the kernel. Keeps the queue drain, the result gates, EWMA and all state mutation. |
| `backend/services/frame_processor.py` (modify) | Narrows the configs attached to each candidate from the whole fleet to the contributing nodes. |
| `backend/main.py` (modify) | Starts the pool before the drain threads, shuts it down after them. |
| `backend/routes/admin.py` (modify) | Surfaces pool rebuild count and worker count alongside the existing solver metrics. |
| `backend/core/state.py` (modify) | One new counter, `solver_pool_rebuilds`. |
| `backend/tests/test_solver_kernel.py` (new) | Kernel purity and dispatch. |
| `backend/tests/test_solver_pool.py` (new) | Pool runs off-process, recovers from a worker crash. |
| `backend/tests/test_frame_processor.py` (modify) | Config narrowing at enqueue. |

---

### Task 1: Extract the pure solve kernel

The altitude sweep and the solve dispatch are already free of shared state. Moving them into
their own module makes that a property the tests enforce rather than a coincidence, and gives
the pool a picklable, module-level target.

**Files:**
- Create: `backend/services/solver_kernel.py`
- Modify: `backend/services/tasks/solver.py` (remove lines 137-207, rewire lines 337-345)
- Test: `backend/tests/test_solver_kernel.py`

**Interfaces:**
- Consumes: `retina_geolocator.multinode_solver.solve_multinode(solver_input, node_configs)`,
  returning a result dict with keys `success`, `lat`, `lon`, `alt_m`, `rms_delay`,
  `rms_doppler`, `n_nodes`, `timestamp_ms`, `contributing_node_ids`, or `None`.
- Produces: `solver_kernel.run_solve(s_in: dict, node_cfgs: dict, solve_fn=None) -> dict | None`.
  Task 3 submits this to the pool; Task 4 calls it in-process when a test injects `solve_fn`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_solver_kernel.py`:

```python
"""Unit tests for the pure solve kernel.

The kernel is the only code that runs in a solver child process, so it must stay
free of shared state: a `spawn`ed interpreter has no server state, and importing
`core.state` there would build a second, silently divergent copy of it.
"""

import ast
import os
from pathlib import Path

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import solver_kernel  # noqa: E402

_KERNEL_PATH = Path(solver_kernel.__file__)


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at module level or inside a function."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_kernel_does_not_import_shared_state():
    assert "core.state" not in _imported_modules(_KERNEL_PATH)
    assert "core" not in _imported_modules(_KERNEL_PATH)


def test_run_solve_without_initial_guess_calls_solver_once():
    calls = []

    def solve_fn(s_in, cfgs):
        calls.append(s_in)
        return {"success": True, "lat": 51.0, "lon": -1.0, "rms_delay": 0.5}

    result = solver_kernel.run_solve({"n_nodes": 2}, {}, solve_fn)

    assert len(calls) == 1
    assert result["lat"] == 51.0


def test_run_solve_n3_sweeps_altitude_layers_and_picks_lowest_rms():
    seen_alts = []

    def solve_fn(s_in, cfgs):
        alt = s_in["initial_guess"]["alt_km"]
        seen_alts.append(alt)
        # 7 km is the true layer: give it the smallest residual.
        return {"success": True, "lat": 51.0, "lon": -1.0,
                "rms_delay": 0.1 if alt == 7.0 else 5.0}

    s_in = {"n_nodes": 3, "initial_guess": {"lat": 51.0, "lon": -1.0, "alt_km": 7.0}}
    result = solver_kernel.run_solve(s_in, {}, solve_fn)

    assert seen_alts == solver_kernel.SOLVER_ALT_LAYERS_KM
    assert result["rms_delay"] == 0.1


def test_run_solve_n2_uses_the_association_altitude_without_sweeping():
    seen_alts = []

    def solve_fn(s_in, cfgs):
        seen_alts.append(s_in["initial_guess"]["alt_km"])
        return {"success": True, "lat": 51.0, "lon": -1.0, "rms_delay": 0.0}

    s_in = {"n_nodes": 2, "initial_guess": {"lat": 51.0, "lon": -1.0, "alt_km": 9.0}}
    solver_kernel.run_solve(s_in, {}, solve_fn)

    assert seen_alts == [9.0]


def test_run_solve_raises_when_every_layer_raised():
    def solve_fn(s_in, cfgs):
        raise ValueError("no convergence")

    s_in = {"n_nodes": 3, "initial_guess": {"lat": 51.0, "lon": -1.0, "alt_km": 7.0}}
    try:
        solver_kernel.run_solve(s_in, {}, solve_fn)
    except ValueError:
        return
    raise AssertionError("expected the last exception to propagate")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd backend && .venv/bin/python -m pytest tests/test_solver_kernel.py -v --no-cov
```

Expected: FAIL, collection error `ModuleNotFoundError: No module named 'services.solver_kernel'`.

- [ ] **Step 3: Create the kernel module**

Create `backend/services/solver_kernel.py`. The three sweep functions are moved verbatim from
`services/tasks/solver.py:137-207`; `run_solve` is new and replaces the dispatch that lived
inline at `solver.py:337-345`.

```python
"""Pure multinode solve, the only code that runs in a solver child process.

Nothing here may import ``core.state``. A child is spawned into a fresh
interpreter with no server state, so an import of the shared-state module there
builds a second copy that diverges silently from the parent's.
``tests/test_solver_kernel.py`` enforces it.

Arguments and return values cross a pipe, so both must be plain picklable dicts.
"""

import logging

# Altitude layers (km) tried when n_nodes >= 3. For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ~ 0; wrong layers give rms > 0, so picking the
# minimum selects the true altitude. Layers match the altitudes_km used in
# compute_overlap_zone so the initial_guess alt from association matches a sweep
# point. Range 1.5-11 km covers simulation aircraft (0.3-15 km spawns) and
# commercial aviation. The 1.5 and 3.0 km layers fix systematic 7-10 km errors
# for low-altitude aircraft where the old [5,7,9,11] set forced wrong altitude.
SOLVER_ALT_LAYERS_KM = [1.5, 3.0, 5.0, 7.0, 9.0, 11.0]

# Resolved once per process on first use. In a pool worker this is where the
# scipy import cost lands, paid once per worker rather than per solve.
_solve_multinode = None


def _default_solve_fn():
    global _solve_multinode
    if _solve_multinode is None:
        from retina_geolocator.multinode_solver import solve_multinode
        _solve_multinode = solve_multinode
    return _solve_multinode


def _sweep_altitudes(s_in: dict, node_cfgs: dict, solve_fn,
                     layers_km: list[float], metric: str) -> dict | None:
    """Try each altitude layer; return the result with lowest value of `metric`.

    Args:
        metric: Solver output key to minimise across layers. Currently always
                'rms_delay' (used by n>=3 where the overdetermined system gives
                rms~0 at the correct altitude).
    """
    base_guess = s_in["initial_guess"]
    best_result: dict | None = None
    best_rms = float("inf")
    last_exc: BaseException | None = None

    for alt_km in layers_km:
        s_try = dict(s_in)
        s_try["initial_guess"] = dict(base_guess, alt_km=alt_km)
        try:
            result = solve_fn(s_try, node_cfgs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if result and result.get("success"):
            rms_raw = result.get(metric)
            rms = float("inf") if rms_raw is None else float(rms_raw)
            logging.debug(
                "altitude sweep: z=%.1fkm %s=%.3f (best so far=%.3f)",
                alt_km, metric, rms, best_rms,
            )
            if rms < best_rms:
                best_rms = rms
                best_result = result

    if best_result is None and last_exc is not None:
        raise last_exc

    return best_result


def _solve_best_altitude(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude sweep for n>=3: pick by minimum rms_delay.

    If the initial_guess already carries an ADS-B altitude (not one of the fixed
    grid layers), include it in the sweep so the correct exact altitude is tried.
    """
    ig_alt = s_in.get("initial_guess", {}).get("alt_km")
    if ig_alt is not None and ig_alt not in SOLVER_ALT_LAYERS_KM:
        layers = sorted(set(SOLVER_ALT_LAYERS_KM + [round(float(ig_alt), 3)]))
    else:
        layers = SOLVER_ALT_LAYERS_KM
    return _sweep_altitudes(s_in, node_cfgs, solve_fn, layers, "rms_delay")


def _solve_best_altitude_n2(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude solve for n=2: use the initial_guess altitude from association.

    For n=2 the solver state [x, y, vx, vy, vz] with altitude fixed is:
    - Exactly determined by the 2 delay equations for (x, y)
    - Underdetermined for (vx, vy, vz): 2 Doppler equations, 3 unknowns

    Both rms_delay and rms_doppler are ~0 at every altitude layer (the solver
    always finds a zero-residual solution within bounds). Neither metric can
    discriminate altitude.

    The initial_guess.alt_km from association.py is set to the delay-residual
    weighted mean of all candidate altitudes in the group. When the correct
    altitude layer has smaller delay residuals it is upweighted; when all layers
    tie (high altitude ambiguity), the mean falls back to ~(7+9+11)/3 = 9 km,
    which covers the typical commercial aviation cruise band (7-12 km).
    """
    return solve_fn(s_in, node_cfgs)


def run_solve(s_in: dict, node_cfgs: dict, solve_fn=None) -> dict | None:
    """Solve one candidate.

    `solve_fn` exists for tests that inject a stub; the pool never passes it, so
    a worker resolves the real solver itself and never pickles a function.
    Exceptions are allowed to propagate: in the pool they surface on the future
    and are counted by the caller, which is where they are counted today.
    """
    fn = solve_fn or _default_solve_fn()
    if "initial_guess" not in s_in:
        return fn(s_in, node_cfgs)
    if (s_in.get("n_nodes", 0) or 0) >= 3:
        return _solve_best_altitude(s_in, node_cfgs, fn)
    return _solve_best_altitude_n2(s_in, node_cfgs, fn)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd backend && .venv/bin/python -m pytest tests/test_solver_kernel.py -v --no-cov
```

Expected: 5 passed.

- [ ] **Step 5: Delete the moved code from `services/tasks/solver.py`**

Delete lines 137-207 entirely (`_sweep_altitudes`, `_solve_best_altitude`,
`_solve_best_altitude_n2`) and the now-unused `_SOLVER_ALT_LAYERS_KM` block at lines 70-80.
Add the kernel import next to the existing `from core import state`:

```python
from services import solver_kernel
```

Then replace the dispatch inside `_process_solver_item`, which currently reads:

```python
    try:
        if "initial_guess" not in s_in:
            result = solve_fn(s_in, node_cfgs)
        elif n_nodes >= 3:
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
        else:
            result = _solve_best_altitude_n2(s_in, node_cfgs, solve_fn)
```

with:

```python
    try:
        result = solver_kernel.run_solve(s_in, node_cfgs, solve_fn)
```

Leave the `except Exception:` block below it untouched. Task 4 changes what `solve_fn` being
`None` means; here it still receives a real function from `_run_solver_worker`.

- [ ] **Step 6: Run the full suite to verify nothing regressed**

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS. `tests/test_solver_worker.py` in particular, all 17 of its
`_process_solver_item(item, solve_fn)` call sites, must still pass unchanged: the dispatch
moved but its behaviour did not.

- [ ] **Step 7: Lint**

```bash
cd ~/owl/retina-server && backend/.venv/bin/ruff check backend/ && backend/.venv/bin/ruff format backend/
```

- [ ] **Step 8: Commit**

```bash
git add backend/services/solver_kernel.py backend/services/tasks/solver.py backend/tests/test_solver_kernel.py
git commit -m "refactor(solver): extract the pure solve into services/solver_kernel

The solve is about to run in a child process, which needs a picklable
module-level target that touches no shared state. The altitude sweep and
dispatch already met that condition by accident; moving them into their own
module makes it a property a test enforces, so a later import of core.state
cannot quietly reintroduce a second copy of server state in a worker."
```

---

### Task 2: Narrow the configs travelling with each candidate

`get_node_configs()` returns every connected node's config and the whole map is attached to
every candidate. That is free while the queue is in-process and becomes the dominant IPC cost
once it is not: at 200 nodes it pickles the whole fleet per solve.

**Files:**
- Modify: `backend/services/frame_processor.py:306-325`
- Test: `backend/tests/test_frame_processor.py`

**Interfaces:**
- Consumes: `s_in["measurements"]`, a list of dicts each carrying `node_id`, produced by
  `InterNodeAssociator.format_candidates_for_solver`.
- Produces: queue items of shape `(s_in: dict, node_cfgs: dict, enqueued_at: float)` where
  `node_cfgs` now holds only the contributing nodes. Task 3 pickles this; Task 4's beam gate
  reads it in the parent.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_frame_processor.py`:

```python
def test_solver_candidate_carries_only_contributing_node_configs(monkeypatch):
    """The whole-fleet config map must not ride along on every candidate.

    solve_multinode reads node_configs for the measurement nodes only, and the
    beam gate reads contributing_node_ids, which the solver builds from those
    same measurements. Anything else on the item is pickled per solve for nothing.
    """
    from core import state
    from services import frame_processor

    fleet = {f"node-{i}": {"rx_lat": 51.0 + i, "rx_lon": -1.0} for i in range(50)}
    monkeypatch.setattr(frame_processor, "get_node_configs", lambda: fleet)

    candidate = {
        "n_nodes": 2,
        "initial_guess": {"lat": 51.0, "lon": -1.0, "alt_km": 9.0},
        "measurements": [{"node_id": "node-3"}, {"node_id": "node-7"}],
    }

    class _Assoc:
        def submit_frame(self, node_id, frame, ts):
            return object()

        def format_candidates_for_solver(self, assoc):
            return [candidate]

    monkeypatch.setattr(state, "node_associator", _Assoc())

    while not state.solver_queue.empty():
        state.solver_queue.get_nowait()

    frame_processor.process_one_frame("node-3", {"timestamp": 0.0}, None)

    _s_in, node_cfgs, _ts = state.solver_queue.get_nowait()
    assert set(node_cfgs) == {"node-3", "node-7"}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd backend && .venv/bin/python -m pytest tests/test_frame_processor.py::test_solver_candidate_carries_only_contributing_node_configs -v --no-cov
```

Expected: FAIL with `AssertionError` comparing a 50-key set against the 2-key one.

- [ ] **Step 3: Narrow the map at enqueue**

In `backend/services/frame_processor.py`, replace the loop body at lines 308-312, currently:

```python
        for s_in in solver_inputs:
            if s_in["n_nodes"] < 2:
                continue
            try:
                state.solver_queue.put_nowait((s_in, node_cfgs, time.time()))
```

with:

```python
        for s_in in solver_inputs:
            if s_in["n_nodes"] < 2:
                continue
            # Only the contributing nodes' configs travel with the candidate.
            # The whole-fleet map is free to attach while the queue is in-process
            # and is pickled per solve once the solve runs in a child process,
            # where at fleet scale it dominates the IPC cost. The solver builds
            # contributing_node_ids from these same measurements, so the beam
            # gate in the parent still finds every config it needs.
            cand_cfgs = {
                m["node_id"]: node_cfgs[m["node_id"]]
                for m in s_in.get("measurements", [])
                if m.get("node_id") in node_cfgs
            }
            try:
                state.solver_queue.put_nowait((s_in, cand_cfgs, time.time()))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd backend && .venv/bin/python -m pytest tests/test_frame_processor.py -v --no-cov
```

Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Run the full suite**

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS. Watch `tests/test_mlat_verification.py` and `tests/test_e2e_pipeline.py`: they
exercise the solve end to end and would catch a config the beam gate can no longer resolve.

- [ ] **Step 6: Lint and commit**

```bash
cd ~/owl/retina-server && backend/.venv/bin/ruff check backend/ && backend/.venv/bin/ruff format backend/
git add backend/services/frame_processor.py backend/tests/test_frame_processor.py
git commit -m "perf(solver): attach only contributing node configs to a candidate

Every candidate carried the whole connected-fleet config map. In-process that
costs a dict reference; across the process boundary the solve is about to move
to, it is a per-solve pickle of the entire fleet and would dominate the IPC at
the volumes the 500-node run is meant to measure."
```

---

### Task 3: The spawn-context process pool

**Files:**
- Create: `backend/services/solver_pool.py`
- Modify: `backend/core/state.py` (one counter, after line 132)
- Test: `backend/tests/test_solver_pool.py`

**Interfaces:**
- Consumes: `solver_kernel.run_solve` from Task 1.
- Produces: `solver_pool.start(max_workers: int | None = None) -> None`,
  `solver_pool.shutdown() -> None`, `solver_pool.submit(s_in: dict, node_cfgs: dict) -> dict | None`,
  `solver_pool.is_running() -> bool`, `solver_pool.worker_count() -> int`.
  Task 4 calls `submit` from the drain threads and `start`/`shutdown` from the lifespan.

- [ ] **Step 1: Add the counter to `core/state.py`**

After `solver_queue_drops: int = 0` at line 132, add:

```python
# Times the solver process pool was rebuilt after a worker died. A non-zero
# value means solves were lost; the pool alerts on each rebuild.
solver_pool_rebuilds: int = 0
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_solver_pool.py`:

```python
"""Tests for the solver process pool.

These start real child processes, so they are slower than the rest of the suite.
They are the only place the process boundary itself is proved.
"""

import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services import solver_pool  # noqa: E402


# These three are module-level so the pool can pickle them by reference.
def _echo_pid_solve(s_in, node_cfgs):
    """Return the pid that ran the solve."""
    return {"success": True, "pid": os.getpid(), "lat": 51.0, "lon": -1.0}


def _raise_value_error(s_in, node_cfgs):
    raise ValueError("no convergence")


def _kill_own_process(s_in, node_cfgs):
    """Kill the worker outright, which breaks the pool rather than raising."""
    os._exit(1)


@pytest.fixture
def pool():
    solver_pool.start(max_workers=2)
    yield solver_pool
    solver_pool.shutdown()


def test_start_is_idempotent(pool):
    first = pool.worker_count()
    pool.start(max_workers=2)
    assert pool.worker_count() == first


def test_solve_runs_in_a_child_process(pool):
    result = pool.submit({"n_nodes": 2}, {}, solve_fn=_echo_pid_solve)
    assert result["pid"] != os.getpid()


def test_solver_exception_propagates_to_the_caller(pool):
    with pytest.raises(ValueError):
        pool.submit({"n_nodes": 2}, {}, solve_fn=_raise_value_error)


def test_a_dead_worker_rebuilds_the_pool_and_counts_it(pool):
    state.solver_pool_rebuilds = 0

    with pytest.raises(Exception):
        pool.submit({"n_nodes": 2}, {}, solve_fn=_kill_own_process)

    assert state.solver_pool_rebuilds == 1
    # The rebuilt pool serves the next solve rather than staying broken.
    assert pool.submit({"n_nodes": 2}, {}, solve_fn=_echo_pid_solve)["success"] is True


def test_submit_without_a_running_pool_raises(pool):
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit({"n_nodes": 2}, {})
    pool.start(max_workers=2)  # restore for the fixture teardown
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd backend && .venv/bin/python -m pytest tests/test_solver_pool.py -v --no-cov
```

Expected: FAIL, collection error `ModuleNotFoundError: No module named 'services.solver_pool'`.

- [ ] **Step 4: Create the pool module**

Create `backend/services/solver_pool.py`:

```python
"""Process pool for the CPU-bound multinode solve.

The solve is the measured GIL holder in this process: the LM residual callback
is scalar Python deliberately inlined for small N, so solver threads reclaim the
GIL on every evaluation and anything sharing the interpreter waits behind them.
Measured, SOLVER_WORKERS=2 holds the flush loop at a 4.0s cadence with the box
pegged at ~115% CPU on 4 vCPU; SOLVER_WORKERS=0 drops it to the 1.0s floor at 8%.
Running the solve in child processes is what keeps ingest latency independent of
solve load.

`spawn`, not `fork`. This process is already running uvicorn's event loop, the
frame-worker thread pool and the solver drain threads, and forking an interpreter
whose threads may hold locks deadlocks the child. Spawn costs one scipy import
per worker at startup and nothing per solve.
"""

import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from core import state
from services import solver_kernel

_DEFAULT_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

_pool: ProcessPoolExecutor | None = None
_workers = 0
# Bumped on every (re)build. A drain thread that saw generation N and found the
# pool broken asks for a rebuild of generation N; concurrent drains that already
# lost the same pool therefore rebuild it once between them rather than N times.
_generation = 0
_lock = threading.Lock()


def start(max_workers: int | None = None) -> None:
    """Create the pool. Idempotent, so a restart path cannot double it."""
    global _pool, _workers, _generation
    with _lock:
        if _pool is not None:
            return
        _workers = max_workers or _DEFAULT_WORKERS
        _pool = ProcessPoolExecutor(
            max_workers=_workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        _generation += 1
        logging.info("Solver process pool started with %d worker(s)", _workers)


def shutdown() -> None:
    """Tear the pool down. Safe to call when it was never started."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
        logging.info("Solver process pool shut down")


def is_running() -> bool:
    return _pool is not None


def worker_count() -> int:
    return _workers if _pool is not None else 0


def submit(s_in: dict, node_cfgs: dict, solve_fn=None) -> dict | None:
    """Run one solve in a worker and block until it answers.

    Called from a solver drain thread, never from the event loop. `solve_fn` is
    for the pool's own tests; production submits None so the worker resolves the
    real solver itself and no function is pickled.
    """
    with _lock:
        pool, generation = _pool, _generation
    if pool is None:
        raise RuntimeError("solver pool is not running")
    try:
        return pool.submit(solver_kernel.run_solve, s_in, node_cfgs, solve_fn).result()
    except BrokenProcessPool:
        _rebuild(generation)
        raise


def _rebuild(generation: int) -> None:
    """Replace a pool whose worker died, once per generation."""
    global _pool, _generation
    with _lock:
        if _pool is None or _generation != generation:
            return  # someone else already rebuilt this one, or we are shutting down
        broken, _pool = _pool, None
        state.solver_pool_rebuilds += 1
        rebuilds = state.solver_pool_rebuilds
    broken.shutdown(wait=False, cancel_futures=True)
    from services.alerting import send_alert
    send_alert(
        "solver_pool_broken",
        f"Solver worker died; pool rebuilt ({rebuilds} total)",
        {"rebuilds": rebuilds},
    )
    start(_workers)
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd backend && .venv/bin/python -m pytest tests/test_solver_pool.py -v --no-cov
```

Expected: 6 passed. If `test_solve_runs_in_a_child_process` hangs rather than failing, the
context is resolving to `fork` and inheriting the test process; check the `mp_context`
argument.

- [ ] **Step 6: Lint and commit**

```bash
cd ~/owl/retina-server && backend/.venv/bin/ruff check backend/ && backend/.venv/bin/ruff format backend/
git add backend/services/solver_pool.py backend/core/state.py backend/tests/test_solver_pool.py
git commit -m "feat(solver): add a spawn-context worker pool for the solve

Nothing consumes it yet. Spawn rather than fork because the parent already runs
uvicorn's loop, the frame-worker pool and the drain threads, and forking an
interpreter whose threads hold locks deadlocks the child. A dead worker breaks a
ProcessPoolExecutor rather than restarting it, so the pool rebuilds itself once
per generation and alerts, instead of silently ceasing to solve."
```

---

### Task 4: Route the drain threads through the pool

**Files:**
- Modify: `backend/services/tasks/solver.py` (`_process_solver_item`, `_run_solver_worker`)
- Modify: `backend/main.py:126-129`
- Test: `backend/tests/test_solver_worker.py`

**Interfaces:**
- Consumes: `solver_pool.submit(s_in, node_cfgs)` and `solver_pool.start()`/`shutdown()` from
  Task 3, `solver_kernel.run_solve` from Task 1.
- Produces: `_process_solver_item(item: tuple, solve_fn=None) -> dict | None`. With `solve_fn`
  given it solves in-process, which is what the 17 existing call sites in
  `tests/test_solver_worker.py` rely on. With it omitted the solve goes to the pool.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_solver_worker.py`:

```python
def test_process_solver_item_uses_the_pool_when_no_solve_fn_is_given(monkeypatch):
    """Production passes no solve_fn, and that must reach the process pool.

    The gates, EWMA and every state mutation stay in the parent; only the solve
    crosses. This asserts the crossing happens and that the parent still records
    the result it gets back.
    """
    from services import solver_pool

    _reset_state()
    state.node_analytics = _StubAnalytics()

    calls = []

    def _fake_submit(s_in, node_cfgs):
        calls.append((s_in, node_cfgs))
        return {"success": True, "lat": 51.0, "lon": -1.0, "alt_m": 9000,
                "rms_delay": 0.5, "rms_doppler": 10.0, "n_nodes": 2,
                "timestamp_ms": 1753900000123, "contributing_node_ids": []}

    monkeypatch.setattr(solver_pool, "submit", _fake_submit)

    item = ({"n_nodes": 2, "measurements": []}, {}, time.time())
    result = solver_mod._process_solver_item(item)

    assert len(calls) == 1
    assert result["success"] is True
    assert state.solver_successes == 1
    assert len(state.multinode_tracks) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd backend && .venv/bin/python -m pytest tests/test_solver_worker.py::test_process_solver_item_uses_the_pool_when_no_solve_fn_is_given -v --no-cov
```

Expected: FAIL with `TypeError: _process_solver_item() missing 1 required positional argument: 'solve_fn'`.

- [ ] **Step 3: Default `solve_fn` to the pool**

In `backend/services/tasks/solver.py`, change the signature at line 317:

```python
def _process_solver_item(item: tuple, solve_fn=None) -> dict | None:
```

and add to its docstring:

```python
    """Solve one queued candidate and apply every result gate in this process.

    `solve_fn` given means solve in-process, which is how the tests inject a
    stub. Omitted means submit to the worker pool: the solve is the only part
    that crosses, and the gates, EWMA smoothing and all state mutation below
    stay here.
    """
```

Replace the dispatch Task 1 left at that site, currently:

```python
    try:
        result = solver_kernel.run_solve(s_in, node_cfgs, solve_fn)
```

with:

```python
    try:
        if solve_fn is not None:
            result = solver_kernel.run_solve(s_in, node_cfgs, solve_fn)
        else:
            result = solver_pool.submit(s_in, node_cfgs)
```

Add the import beside the Task 1 one:

```python
from services import solver_kernel, solver_pool
```

The `except Exception:` block below is unchanged and now also catches what the pool re-raises,
including a `BrokenProcessPool` after a rebuild, so a dead worker costs one candidate and
increments `state.solver_failures` exactly as an unconverged solve does.

- [ ] **Step 4: Point the drain threads at the pool**

At line 466, `_run_solver_worker` currently reads:

```python
def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    from retina_geolocator.multinode_solver import solve_multinode
    while True:
        try:
            item = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        _process_solver_item(item, solve_multinode)
```

Replace it with:

```python
def _run_solver_worker():
    """Drain state.solver_queue and solve via the pool. Runs as a daemon thread.

    The thread blocks on the worker's answer, which is why there is one drain
    thread per pool worker: the thread is a handle on a child process rather
    than a unit of compute, and it holds the GIL only for the gates.
    """
    while True:
        try:
            item = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        _process_solver_item(item)
```

- [ ] **Step 5: Start the pool before the drain threads in the lifespan**

In `backend/main.py`, add the import beside the other service imports at line 68:

```python
from services import solver_pool
```

At lines 126-129 the lifespan currently reads:

```python
    async with server:
        # Start background daemon threads for multinode LM solving.
        # These drain solver_queue independently of frame workers.
        start_solver_workers()
```

Replace with:

```python
    async with server:
        # The pool must exist before the drain threads, or the first candidate
        # off the queue finds no pool and is lost.
        solver_pool.start()
        # Start background daemon threads for multinode LM solving. These drain
        # solver_queue independently of frame workers; each blocks on one pool
        # worker, so the count is deliberately the same.
        start_solver_workers()
```

In the teardown, after the `for t in tasks: t.cancel()` loop and before `save_snapshot()`:

```python
        # After the tasks are cancelled, so nothing is mid-submit.
        solver_pool.shutdown()
```

- [ ] **Step 6: Run the full suite**

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS, all of `tests/test_solver_worker.py` included: every existing call site passes
`solve_fn` explicitly and so still solves in-process.

- [ ] **Step 7: Verify the boundary end to end against a running stack**

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
```

Then confirm the solve genuinely left the uvicorn process:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml exec server ps -eo pid,ppid,rss,comm
```

Expected: `SOLVER_WORKERS` (default 2) child python processes under the uvicorn pid. Then check
solves are still landing on the map, not merely that processes exist:

```bash
curl -s http://testapi.localhost:8080/api/test/dashboard | python3 -m json.tool | grep -A 5 '"solver"'
```

Expected: `successes` rising over successive calls, `queue_drops` not climbing. Tear down with
`docker compose -f docker-compose.yml -f docker-compose.test.yml down`.

- [ ] **Step 8: Lint and commit**

```bash
cd ~/owl/retina-server && backend/.venv/bin/ruff check backend/ && backend/.venv/bin/ruff format backend/
git add backend/services/tasks/solver.py backend/main.py backend/tests/test_solver_worker.py
git commit -m "feat(solver): run the solve in worker processes

Closes the process boundary the v1 ingest endpoints need. Endpoint latency can
no longer be held by an LM residual: the drain threads keep the GIL only for the
result gates, and the solve itself is in a child. Everything else is unchanged
by design, because the token cache, the active-config cache and frame-arrival
liveness all have readers spread across this process and splitting further would
move the shared state rather than the workload."
```

---

### Task 5: Surface the pool in the metrics, and record the measurement

Step 4 of the [phase 1 order](../../../../claude-shared/docs/node-server-phase-1-plan.md) needs
solver throughput readable separately from ingest, or a plateau in the 500-node run cannot be
attributed. The counters exist; the pool's own state does not.

**Files:**
- Modify: `backend/routes/admin.py:493-504`
- Modify: `backend/services/health.py` (after line 54)
- Modify: `docs/architecture.md`, `deploy/start.sh`
- Test: `backend/tests/test_admin_routes.py`

**Interfaces:**
- Consumes: `solver_pool.worker_count()`, `solver_pool.is_running()`,
  `state.solver_pool_rebuilds` from Task 3.
- Produces: two new keys on the existing `/api/admin/metrics` payload,
  `solver_pool_workers` and `solver_pool_rebuilds`.

- [ ] **Step 1: Write the failing test**

Add a method to the existing `TestMetrics` class in `backend/tests/test_admin_routes.py`
(line 173), beside `test_metrics_returns_expected_fields`. That file's `client` fixture is the
whole of the setup: `RETINA_ENV=test` grants admin access, so there are no auth headers.

```python
    def test_metrics_report_the_solver_pool(self, client):
        """A stopped pool must be visible, not inferred from a flat solve count."""
        r = client.get("/api/admin/metrics")
        assert r.status_code == 200
        body = r.json()
        assert "solver_pool_workers" in body
        assert "solver_pool_rebuilds" in body
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd backend && .venv/bin/python -m pytest tests/test_admin_routes.py::TestMetrics -v --no-cov
```

Expected: FAIL with `AssertionError` on the missing key.

- [ ] **Step 3: Add the keys**

In `backend/routes/admin.py`, alongside `"solver_queue_drops": state.solver_queue_drops,` at
line 497, add:

```python
        "solver_pool_workers": solver_pool.worker_count(),
        "solver_pool_rebuilds": state.solver_pool_rebuilds,
```

with the import at the top of the file:

```python
from services import solver_pool
```

- [ ] **Step 4: Alert when the pool has stopped**

In `backend/services/health.py`, after the `solver_queue_drops` check at lines 53-54, add:

```python
    from services import solver_pool
    if not solver_pool.is_running():
        add("solver_pool_down", CRITICAL, "Solver process pool is not running")
    elif state.solver_pool_rebuilds > 0:
        add("solver_pool_rebuilds", WARNING,
            f"Solver pool rebuilt {state.solver_pool_rebuilds} time(s) after a worker died")
```

- [ ] **Step 5: Run the tests**

```bash
cd backend && .venv/bin/python -m pytest tests/test_admin_routes.py tests/test_solver_pool.py -v --no-cov && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 6: Take the before-and-after measurement**

The claim this whole plan rests on is measured, so verify it rather than assume it. Run the
prod-shaped fleet, which is the 40 s interval the droplet actually runs:

```bash
just up prod
```

Let it settle for five minutes, then record four numbers: the aircraft flush cadence from the
logs, and `solver_queue_depth` and `solver_avg_latency_s` from `/api/admin/metrics`, plus total
CPU across the uvicorn process and its children:

```bash
ps -o pid,ppid,%cpu,rss,command -g $(pgrep -f "uvicorn main:app" | head -1)
```

Compare against the same four on the commit before Task 4. The expectation from the profile is
that total CPU rises above one core, which is the point of the exercise, while flush cadence
falls towards the 1.0 s loop-sleep floor. Write them into
`retina-server-optimisations.md` under a new "Measured (2026-08-05)" heading. If flush
cadence has not moved, stop and say so rather than proceeding: it would mean the residual is
not where the earlier profile put it.

- [ ] **Step 7: Update the docs**

`docs/architecture.md` carries the process diagram (line 22, `frame_processor (N workers)`) and
the component list. Record there that the solve now runs in `SOLVER_WORKERS` child processes
under the spawn start method while everything else, including all shared state, stays in the
single uvicorn process, and that the drain-thread count is deliberately the same as the worker
count. In `deploy/start.sh`, the comment at lines 8-10 says concurrency is handled by
`FRAME_WORKERS` threads inside the single process; extend it to name the pool, since that is no
longer the whole story. Leave the `workers MUST stay at 1` instruction as it is: it is still
true and now has a second reason, the ADR's per-process caches.

- [ ] **Step 8: Lint and commit**

```bash
cd ~/owl/retina-server && backend/.venv/bin/ruff check backend/ && backend/.venv/bin/ruff format backend/
git add backend/routes/admin.py backend/services/health.py backend/tests/test_admin_routes.py docs/architecture.md deploy/start.sh
git commit -m "feat(solver): surface pool health, and document the process model

A stopped pool previously showed only as a solve count that stopped rising,
which reads the same as a quiet sky. Step 4 of the phase 1 order also needs
solver throughput readable apart from ingest, or the 500-node run has a plateau
with no attributable cause."
```

---

## What this plan does not do

Named so that a reader does not take their absence for an oversight.

- **The v1 endpoints.** Step 2 of the phase 1 order. This plan builds the boundary they need
  and stops there.
- **Association.** `node_associator.submit_frame` holds a rolling 4 s window of cross-node state
  and cannot be sharded per node without destroying what it does. It is pure-Python and
  GIL-holding, and it becomes the next contention once the solve leaves. Task 5's measurement
  is what will show whether that matters at 50 nodes.
- **Arc building.** The other pure-Python GIL holder, in the flush path rather than the ingest
  path. It has its own options recorded in `retina-server-optimisations.md`.
- **Backpressure on a full queue.** Today a full `solver_queue` increments a drop counter, which
  is right for a TCP producer that is not waiting and wrong for an HTTP node that is. The 429
  with `Retry-After` belongs with the endpoint that returns it, in step 2.
