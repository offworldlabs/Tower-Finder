# Subsystem 2 — Parquet detection archive writer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-30s JSON detection archive with a Parquet+zstd archive using Hive-style partitioning, while keeping read-side backward compatibility with existing historical JSON files in R2.

**Architecture:**
- New module `services/parquet_writer.py` writes one Parquet file per flush.
- Schema is **per-detection** (not per-frame): each row = one detection, with frame metadata duplicated. This gives much better column-pruning and predicate pushdown than the nested per-frame layout.
- Path layout switches to Hive style: `archive/year=YYYY/month=MM/day=DD/node_id=XXX/part-HHMMSS.parquet`. DuckDB / pyarrow / pandas auto-prune partitions by these keys.
- The 30-second flush cadence is kept as-is. We accept many small Parquet files and rely on a future compaction task (Subsystem 4 backfill includes a compaction pass for historical data).
- Readers (`storage.read_archived_file`, `storage.list_archived_files`) and the lifecycle file iterator support both `*.parquet` (new) and `*.json` (legacy) by extension dispatch.
- The on-the-wire JSON shape returned by `/api/data/archive/{key}` is preserved by reconstructing the per-frame structure from the per-detection rows on read.

**Tech Stack:** `pyarrow` (already importable system-wide; pinned to 16.1.0 in requirements.txt).

---

### Task 1: Define the Parquet schema and writer module

**Files:**
- Create: `backend/services/parquet_writer.py`
- Test: `backend/tests/test_parquet_writer.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_parquet_writer.py`:

```python
"""Tests for the Parquet detection archive writer."""

from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from services import parquet_writer as pw


def _frame(timestamp_ms: int, n_dets: int = 3, with_adsb: bool = False) -> dict:
    return {
        "timestamp": timestamp_ms,
        "delay": [10.0 + i for i in range(n_dets)],
        "doppler": [-50.0 + i for i in range(n_dets)],
        "snr": [12.0 + i for i in range(n_dets)],
        "adsb": (
            [
                {"hex": "abcdef", "lat": 40.0, "lon": -74.0,
                 "alt_baro": 35000, "gs": 480, "track": 270, "flight": "UAL1"}
            ] + [None] * (n_dets - 1)
            if with_adsb else [None] * n_dets
        ),
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


def test_writes_hive_partitioned_path(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    expected = "year=2025/month=01/day=15/node_id=node-A/part-143022.parquet"
    assert key == expected
    assert (tmp_path / key).exists()


def test_schema_is_per_detection_with_required_columns(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=4)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 4  # one row per detection in the single frame
    cols = set(table.column_names)
    expected = {
        "frame_ts_ms", "node_id", "detection_index",
        "delay_us", "doppler_hz", "snr_db",
        "adsb_hex", "adsb_lat", "adsb_lon", "adsb_alt_baro",
        "adsb_gs", "adsb_track", "adsb_flight",
        "signing_mode", "signature_valid",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_adsb_match_populated_when_present(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=3, with_adsb=True)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    table = pq.read_table(tmp_path / key)
    rows = table.to_pylist()
    # First row has an ADS-B match, others are unmatched.
    assert rows[0]["adsb_hex"] == "abcdef"
    assert rows[0]["adsb_lat"] == 40.0
    assert rows[1]["adsb_hex"] is None
    assert rows[2]["adsb_hex"] is None


def test_multiple_frames_concatenate(tmp_path: Path):
    frames = [
        _frame(timestamp_ms=1700000000000, n_dets=3),
        _frame(timestamp_ms=1700000001000, n_dets=2),
    ]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 5  # 3 + 2
    # Frame ids cluster (sorted by frame, then detection_index)
    rows = table.to_pylist()
    assert rows[0]["frame_ts_ms"] == 1700000000000
    assert rows[3]["frame_ts_ms"] == 1700000001000
    assert rows[3]["detection_index"] == 0


def test_empty_frames_returns_none(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = pw.write_detections_parquet(
        node_id="node-A", frames=[], base_dir=tmp_path, write_ts=ts,
    )
    assert key is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_uses_zstd_compression(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=10)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    meta = pq.read_metadata(tmp_path / key)
    # Verify at least one column uses zstd
    rg = meta.row_group(0)
    codecs = {rg.column(i).compression for i in range(rg.num_columns)}
    assert "ZSTD" in codecs or "zstd" in {c.lower() for c in codecs}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_parquet_writer.py -v --no-cov`
Expected: ALL FAIL with `ModuleNotFoundError: No module named 'services.parquet_writer'`.

- [ ] **Step 3: Create the writer module**

Create `backend/services/parquet_writer.py`:

```python
"""Parquet writer for detection archive files.

Writes one Parquet file per flush in Hive-partitioned form:

    archive/year=YYYY/month=MM/day=DD/node_id=XXX/part-HHMMSS.parquet

Schema is per-detection: each row corresponds to one detection inside a frame.
Frame-level metadata (timestamp, signing mode, signature validity) is repeated
on every detection row from that frame. ADS-B truth data, when associated
with a detection at frame build time, lands in the adsb_* columns; when no
match is present those columns are null.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


# Stable schema — adding new columns is fine (Parquet is schema-on-read for
# missing cols), but never rename or reorder.
SCHEMA = pa.schema([
    ("frame_ts_ms", pa.int64()),
    ("node_id", pa.string()),
    ("detection_index", pa.int32()),
    ("delay_us", pa.float64()),
    ("doppler_hz", pa.float64()),
    ("snr_db", pa.float64()),
    ("adsb_hex", pa.string()),
    ("adsb_lat", pa.float64()),
    ("adsb_lon", pa.float64()),
    ("adsb_alt_baro", pa.int32()),
    ("adsb_gs", pa.float64()),
    ("adsb_track", pa.float64()),
    ("adsb_flight", pa.string()),
    ("signing_mode", pa.string()),
    ("signature_valid", pa.bool_()),
])


def _flatten(node_id: str, frames: list[dict]) -> dict[str, list]:
    """Flatten a list of per-frame dicts into per-detection columnar dict."""
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for frame in frames:
        delay = frame.get("delay") or []
        doppler = frame.get("doppler") or []
        snr = frame.get("snr") or []
        adsb = frame.get("adsb") or []
        n = max(len(delay), len(doppler), len(snr))
        if n == 0:
            continue
        frame_ts = int(frame.get("timestamp", 0) or 0)
        sig_mode = frame.get("_signing_mode") or frame.get("signing_mode")
        sig_valid = frame.get("_signature_valid")
        if sig_valid is None:
            sig_valid = frame.get("signature_valid")
        for i in range(n):
            ae = adsb[i] if i < len(adsb) and isinstance(adsb[i], dict) else None
            cols["frame_ts_ms"].append(frame_ts)
            cols["node_id"].append(node_id)
            cols["detection_index"].append(i)
            cols["delay_us"].append(_safe_float(delay, i))
            cols["doppler_hz"].append(_safe_float(doppler, i))
            cols["snr_db"].append(_safe_float(snr, i))
            cols["adsb_hex"].append(ae.get("hex") if ae else None)
            cols["adsb_lat"].append(_get_float(ae, "lat"))
            cols["adsb_lon"].append(_get_float(ae, "lon"))
            cols["adsb_alt_baro"].append(_get_int(ae, "alt_baro"))
            cols["adsb_gs"].append(_get_float(ae, "gs"))
            cols["adsb_track"].append(_get_float(ae, "track"))
            cols["adsb_flight"].append(ae.get("flight") if ae else None)
            cols["signing_mode"].append(sig_mode)
            cols["signature_valid"].append(bool(sig_valid) if sig_valid is not None else None)
    return cols


def _safe_float(arr, i: int) -> float | None:
    if i >= len(arr):
        return None
    v = arr[i]
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_float(d: dict | None, key: str) -> float | None:
    if not d:
        return None
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_int(d: dict | None, key: str) -> int | None:
    if not d:
        return None
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def write_detections_parquet(
    *,
    node_id: str,
    frames: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
) -> str | None:
    """Write a list of detection frames as a single Parquet file.

    Returns the relative key (Hive-partitioned path) or None if frames is empty.
    """
    if not frames:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    cols = _flatten(node_id, frames)
    if not cols["frame_ts_ms"]:
        return None

    table = pa.table(cols, schema=SCHEMA)

    key = (
        f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}/"
        f"node_id={node_id}/part-{write_ts:%H%M%S}.parquet"
    )
    out_path = Path(base_dir) / key
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_parquet_writer.py -v --no-cov`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/requirements.txt backend/services/parquet_writer.py backend/tests/test_parquet_writer.py
git commit -m "feat(archive): add Parquet writer with Hive-partitioned per-detection schema"
```

---

### Task 2: Switch `storage.archive_detections` to call the Parquet writer

**Files:**
- Modify: `backend/services/storage.py`
- Test: `backend/tests/test_storage_listing.py` (existing — extend)

- [ ] **Step 1: Read existing storage tests to understand contract**

Run: `cd backend && cat tests/test_storage_listing.py | head -80`

- [ ] **Step 2: Update `archive_detections` to write Parquet**

Edit `backend/services/storage.py`:

OLD `archive_detections`:

```python
def archive_detections(node_id: str, detections: list[dict], *, tag: str = "detections") -> str:
    """Archive a batch of detections to local filesystem.

    Returns the archive key (relative path like "2025/06/21/node01/detections_143022.json").
    """
    _ensure_local_dir()

    ts = datetime.now(timezone.utc)
    prefix = ts.strftime("%Y/%m/%d")
    filename = f"{tag}_{ts.strftime('%H%M%S')}.json"
    key = f"{prefix}/{node_id}/{filename}"

    payload = json.dumps(
        {"node_id": node_id, "timestamp": ts.isoformat(), "count": len(detections), "detections": detections},
        default=str,
    )

    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "w") as f:
        f.write(payload)

    return key
```

NEW:

```python
def archive_detections(node_id: str, detections: list[dict], *, tag: str = "detections") -> str | None:
    """Archive a batch of detection FRAMES to local filesystem as Parquet.

    Returns the Hive-style relative key, e.g.
        "year=2025/month=06/day=21/node_id=node01/part-143022.parquet"
    Returns None if `detections` is empty (nothing written).

    `tag` is accepted for callsite compatibility but no longer affects the
    filename — Parquet files are always named part-HHMMSS.parquet.
    """
    _ensure_local_dir()

    from services.parquet_writer import write_detections_parquet

    return write_detections_parquet(
        node_id=node_id,
        frames=detections,
        base_dir=_LOCAL_ARCHIVE_DIR,
    )
```

The `tag` parameter is kept for back-compat with any callsite that passes it.

- [ ] **Step 3: Update `list_archived_files` to discover both Parquet and legacy JSON**

In `backend/services/storage.py`, change every `*.json` glob/rglob to `("*.parquet", "*.json")` style discovery.

For the date-prefix branch around line 83:

```python
        for p in (*search_dir.rglob("*.parquet"), *search_dir.rglob("*.json")):
```

For the unbounded-traversal branch around line 130, replace the inner `files = sorted(ndir.glob("*.json"), ...)` with:

```python
                        try:
                            files = sorted(
                                [*ndir.glob("*.parquet"), *ndir.glob("*.json")],
                                key=lambda f: f.name,
                                reverse=sort_desc,
                            )
                        except OSError:
                            continue
```

- [ ] **Step 4: Update `read_archived_file` to dispatch by extension**

Replace existing `read_archived_file`:

```python
def read_archived_file(key: str) -> dict | None:
    """Read an archived file by key. Returns parsed dict or None.

    Accepts both new Parquet keys (`*.parquet`, schema = per-detection rows)
    and legacy JSON keys (`*.json`, schema = nested per-frame). Parquet keys
    are reconstructed back into the legacy per-frame JSON shape so downstream
    consumers (the /api/data/archive/{key} endpoint) keep the same contract.
    """
    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    if not os.path.isfile(local_path):
        return None
    real_base = os.path.realpath(_LOCAL_ARCHIVE_DIR) + os.sep
    real_path = os.path.realpath(local_path)
    if not real_path.startswith(real_base):
        return None

    if key.endswith(".parquet"):
        return _read_parquet_as_legacy_json(local_path)
    with open(local_path) as f:
        return json.load(f)


def _read_parquet_as_legacy_json(path: str) -> dict:
    """Read a per-detection Parquet archive and reconstruct the per-frame JSON."""
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    rows = table.to_pylist()
    if not rows:
        return {"node_id": "", "timestamp": "", "count": 0, "detections": []}

    node_id = rows[0]["node_id"]
    by_frame: dict[int, dict] = {}
    for r in rows:
        ts = r["frame_ts_ms"]
        fr = by_frame.setdefault(ts, {
            "timestamp": ts,
            "delay": [], "doppler": [], "snr": [], "adsb": [],
            "_signing_mode": r.get("signing_mode"),
            "_signature_valid": r.get("signature_valid"),
        })
        fr["delay"].append(r["delay_us"])
        fr["doppler"].append(r["doppler_hz"])
        fr["snr"].append(r["snr_db"])
        if r.get("adsb_hex"):
            fr["adsb"].append({
                "hex": r["adsb_hex"],
                "lat": r["adsb_lat"],
                "lon": r["adsb_lon"],
                "alt_baro": r["adsb_alt_baro"],
                "gs": r["adsb_gs"],
                "track": r["adsb_track"],
                "flight": r["adsb_flight"],
            })
        else:
            fr["adsb"].append(None)

    frames = [by_frame[k] for k in sorted(by_frame.keys())]
    return {
        "node_id": node_id,
        "timestamp": "",  # Parquet keys don't carry the original wallclock ISO ts
        "count": len(frames),
        "detections": frames,
    }
```

- [ ] **Step 5: Run all storage tests**

Run: `cd backend && python3 -m pytest tests/test_storage_listing.py tests/test_parquet_writer.py -v --no-cov`
Expected: PASS — write+read round-trip works, listing finds both extensions.

- [ ] **Step 6: Add a round-trip test**

Append to `backend/tests/test_parquet_writer.py`:

```python
def test_round_trip_via_storage_module(tmp_path: Path, monkeypatch):
    """archive_detections + read_archived_file round-trips back to legacy JSON shape."""
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    from services.storage import archive_detections, read_archived_file

    frames = [_frame(timestamp_ms=1700000000000, n_dets=2, with_adsb=True)]
    key = archive_detections("node-X", frames)
    assert key is not None
    assert key.endswith(".parquet")

    decoded = read_archived_file(key)
    assert decoded is not None
    assert decoded["node_id"] == "node-X"
    assert decoded["count"] == 1
    fr0 = decoded["detections"][0]
    assert fr0["delay"] == [10.0, 11.0]
    assert fr0["adsb"][0]["hex"] == "abcdef"
    assert fr0["adsb"][1] is None
```

Run: `cd backend && python3 -m pytest tests/test_parquet_writer.py::test_round_trip_via_storage_module -v --no-cov`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/services/storage.py backend/tests/test_parquet_writer.py
git commit -m "feat(archive): write detections as Parquet; readers dispatch by extension"
```

---

### Task 3: Update `archive_lifecycle` to handle Parquet files

**Files:**
- Modify: `backend/services/tasks/archive_lifecycle.py:89-110`
- Test: `backend/tests/test_archive_lifecycle.py`

- [ ] **Step 1: Update `_iter_archive_files` to glob both extensions**

Replace:

```python
                        for f in sorted(node_dir.glob("*.json")):
                            yield f
```

With:

```python
                        files = [
                            *node_dir.glob("*.parquet"),
                            *node_dir.glob("*.json"),
                        ]
                        for f in sorted(files):
                            yield f
```

Also widen the per-extension test in the existing test suite. In `test_archive_lifecycle.py::TestIterArchiveFiles::test_only_json_files_are_yielded`, rename + extend to `test_parquet_and_json_yielded`:

```python
def test_parquet_and_json_yielded(self):
    """Both .parquet and .json files in the node dir are yielded."""
    node_dir = self._archive_dir / "2024" / "01" / "15" / "node-A"
    node_dir.mkdir(parents=True)
    (node_dir / "data.json").write_bytes(b"{}")
    (node_dir / "part-120000.parquet").write_bytes(b"")  # contents irrelevant
    (node_dir / "data.txt").write_bytes(b"ignore me")

    results = self._collect()
    names = sorted(p.name for p in results)
    assert names == ["data.json", "part-120000.parquet"]
```

(Convert from unittest.assertEqual style to whatever is consistent in that file — likely `self.assertEqual(names, [...])`.)

- [ ] **Step 2: Update `_make_archive_file` and `_create_archive_file` helpers to use `.parquet` by default in any new test calls**, but leave existing JSON callers alone (legacy compat path).

- [ ] **Step 3: Run lifecycle tests**

Run: `cd backend && python3 -m pytest tests/test_archive_lifecycle.py --no-cov -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/services/tasks/archive_lifecycle.py backend/tests/test_archive_lifecycle.py
git commit -m "feat(lifecycle): iterate both Parquet and JSON files for offload/cleanup"
```

---

### Task 4: Smoke-test the live pipeline with a synthetic frame

**Files:** none (manual verification + ad-hoc script)

- [ ] **Step 1: Create a tiny smoke script**

Create `backend/scripts/smoke_parquet_archive.py`:

```python
"""Smoke test: drive a frame through archive_detections and read it back."""

import shutil
from pathlib import Path

from services.storage import archive_detections, read_archived_file


def main():
    base = Path("/tmp/parquet_smoke")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir()

    import services.storage as st
    st._LOCAL_ARCHIVE_DIR = str(base)

    frame = {
        "timestamp": 1700000000000,
        "delay": [12.34, 56.78],
        "doppler": [-100.5, 33.3],
        "snr": [15.0, 22.0],
        "adsb": [
            {"hex": "abcdef", "lat": 40.71, "lon": -74.0,
             "alt_baro": 35000, "gs": 480, "track": 270, "flight": "UAL1"},
            None,
        ],
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }
    key = archive_detections("smoke-node", [frame])
    print("wrote:", key)
    decoded = read_archived_file(key)
    print("decoded count:", decoded["count"])
    print("first detection delay:", decoded["detections"][0]["delay"])
    print("adsb match:", decoded["detections"][0]["adsb"][0])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `cd backend && python3 scripts/smoke_parquet_archive.py`
Expected output:

```
wrote: year=YYYY/month=MM/day=DD/node_id=smoke-node/part-HHMMSS.parquet
decoded count: 1
first detection delay: [12.34, 56.78]
adsb match: {'hex': 'abcdef', ...}
```

- [ ] **Step 3: Commit the script**

```bash
git add backend/scripts/smoke_parquet_archive.py
git commit -m "chore: add Parquet archive smoke script"
```

---

## Self-review

- [x] Spec coverage: Parquet writer, Hive partitioning, schema dispatch on read, lifecycle iterator extended.
- [x] No placeholders.
- [x] Type consistency: `archive_detections` now returns `str | None`; callers tolerate None.
- [x] Test coverage: writer schema, ADS-B handling, round-trip, lifecycle iteration.
