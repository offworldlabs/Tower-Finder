# Subsystem 3 — Detection schema enrichment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three columns to the per-detection Parquet schema that capture chain-of-custody and ingest-latency metadata that's already present on the frame dict at archive time but currently being thrown away.

**Architecture:** Extend `services.parquet_writer.SCHEMA` with three new nullable columns and propagate the values through `_flatten`. The reverse path in `services.storage._read_parquet_as_legacy_json` is unaffected — those new columns surface as additional fields on the reconstructed per-frame JSON, which is fine for backward compatibility.

**Tech Stack:** Same as Subsystem 2 — `pyarrow`.

**Why these three:**
- `payload_hash` (string, nullable) — HMAC of the original frame payload. The dataset becomes self-verifying without depending on server-side custody state.
- `signature` (string, nullable) — base64/hex signature bytes from the sender. Same rationale.
- `ingest_ts_ms` (int64) — wall-clock server timestamp at archive time. Lets analysts measure end-to-end ingestion latency vs. the sensor's `frame_ts_ms`.

---

### Task 1: Extend the schema and writer

**Files:**
- Modify: `backend/services/parquet_writer.py`
- Test: `backend/tests/test_parquet_writer.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_parquet_writer.py`:

```python
def test_schema_includes_custody_and_ingest_columns(tmp_path: Path):
    """Schema must include payload_hash, signature, ingest_ts_ms."""
    frames = [{
        "timestamp": 1700000000000,
        "delay": [10.0, 11.0],
        "doppler": [-50.0, -49.0],
        "snr": [12.0, 13.0],
        "adsb": [None, None],
        "payload_hash": "deadbeef",
        "signature": "abcd1234",
        "_signing_mode": "ed25519",
        "_signature_valid": True,
    }]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    table = pq.read_table(tmp_path / key)
    cols = set(table.column_names)
    assert {"payload_hash", "signature", "ingest_ts_ms"} <= cols

    rows = table.to_pylist()
    assert rows[0]["payload_hash"] == "deadbeef"
    assert rows[0]["signature"] == "abcd1234"
    # ingest_ts_ms is the wall-clock time at write — must equal write_ts to ms.
    expected_ms = int(ts.timestamp() * 1000)
    assert rows[0]["ingest_ts_ms"] == expected_ms


def test_custody_columns_default_null_when_absent(tmp_path: Path):
    """Frames without payload_hash/signature get nulls."""
    frames = [_frame(timestamp_ms=1700000000000, n_dets=2)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    rows = pq.read_table(tmp_path / key).to_pylist()
    assert all(r["payload_hash"] is None for r in rows)
    assert all(r["signature"] is None for r in rows)
    assert all(isinstance(r["ingest_ts_ms"], int) for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_parquet_writer.py::test_schema_includes_custody_and_ingest_columns -v --no-cov`
Expected: FAIL — column missing.

- [ ] **Step 3: Update SCHEMA and `_flatten`**

In `backend/services/parquet_writer.py`, replace the `SCHEMA` definition:

```python
SCHEMA = pa.schema([
    ("frame_ts_ms", pa.int64()),
    ("ingest_ts_ms", pa.int64()),
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
    ("payload_hash", pa.string()),
    ("signature", pa.string()),
])
```

Update `_flatten` to accept an `ingest_ts_ms` argument and populate the new columns:

```python
def _flatten(node_id: str, frames: list[dict], ingest_ts_ms: int) -> dict[str, list]:
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
        payload_hash = frame.get("payload_hash") or None
        signature = frame.get("signature") or None
        for i in range(n):
            ae = adsb[i] if i < len(adsb) and isinstance(adsb[i], dict) else None
            cols["frame_ts_ms"].append(frame_ts)
            cols["ingest_ts_ms"].append(ingest_ts_ms)
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
            cols["payload_hash"].append(payload_hash)
            cols["signature"].append(signature)
    return cols
```

Update the public `write_detections_parquet` to compute `ingest_ts_ms` from `write_ts` and pass it through:

```python
    write_ts = write_ts or datetime.now(timezone.utc)
    ingest_ts_ms = int(write_ts.timestamp() * 1000)
    cols = _flatten(node_id, frames, ingest_ts_ms)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_parquet_writer.py -v --no-cov`
Expected: all PASS (existing 7 + new 2 = 9).

- [ ] **Step 5: Run the full suite**

Run: `cd backend && python3 -m pytest --no-cov -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/parquet_writer.py backend/tests/test_parquet_writer.py docs/superpowers/plans/2026-05-05-subsystem-3-schema-enrichment.md
git commit -m "feat(archive): persist payload_hash, signature, ingest_ts_ms in Parquet schema"
```
