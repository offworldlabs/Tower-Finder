# Subsystem 5 — Track persistence (multi-node solver outputs)

**Goal:** Persist multi-node solver track outputs as a parallel Parquet stream so the dataset captures *what the system actually output* at a given solver version — not just the raw detections that fed it. Cheap (low row count) and impossible to reconstruct identically after solver code changes.

**Architecture:**
- New module `services/track_writer.py` defining the track schema and a flush helper.
- Append-only buffer `state.track_archive_buffer: deque[dict]` populated from `solver._process_solver_item` whenever a successful solve lands in `state.multinode_tracks`.
- New background task `track_flush_task` (every 60s) drains the buffer and writes one Parquet file at `tracks/year=YYYY/month=MM/day=DD/part-HHMMSS.parquet`.
- Wired into `main.py` lifespan alongside the existing archive flush task.

---

### Task 1: Track schema + writer module

**Files:**
- Create: `backend/services/track_writer.py`
- Test: `backend/tests/test_track_writer.py`

- [ ] Step 1: Test
- [ ] Step 2: Implement
- [ ] Step 3: Verify
- [ ] Step 4: Commit

**Schema:**

```
solve_ts_ms       int64    # server time when solve completed
frame_ts_ms       int64    # sensor timestamp (result.timestamp_ms)
lat               float64
lon               float64
alt_m             float64 nullable
vel_east_ms       float64 nullable
vel_north_ms      float64 nullable
vel_up_ms         float64 nullable
n_nodes           int32
contributing_node_ids string  # comma-separated for analytics simplicity
adsb_hex          string  nullable
rms_delay_us      float64 nullable
rms_doppler_hz    float64 nullable
target_class      string  nullable
```

### Task 2: State buffer + solver hook

**Files:**
- Modify: `backend/core/state.py`
- Modify: `backend/services/tasks/solver.py`

Add `track_archive_buffer: deque[dict] = deque(maxlen=10000)` in state.
Append to it inside `_process_solver_item` after `state.multinode_tracks[key] = result`.

### Task 3: Flush task

**Files:**
- Create: `backend/services/tasks/track_archive.py`
- Modify: `backend/main.py` to start the task in lifespan.

Every `TRACK_ARCHIVE_FLUSH_INTERVAL_S` (60s) drain `track_archive_buffer` and call `track_writer.write_tracks_parquet`.
