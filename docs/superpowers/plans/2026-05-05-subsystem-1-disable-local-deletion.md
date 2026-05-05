# Subsystem 1 — Disable 14-day local archive deletion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the archive lifecycle task from deleting local detection archives after 14 days. R2 already provides durable long-term storage, and we want to retain locally-cached detection data for as long as disk allows.

**Architecture:** The deletion is gated by `ARCHIVE_RETENTION_DAYS` in `config/constants.py` and applied in `services/tasks/archive_lifecycle.py`. We make retention configurable, default to "never delete," and adjust the lifecycle code so that when retention is disabled the deletion phase is skipped entirely. R2 offload still runs unchanged.

**Tech Stack:** Python, pytest, FastAPI background tasks (no new deps).

---

### Task 1: Add a "retention disabled" sentinel to the config

**Files:**
- Modify: `backend/config/constants.py:39-42`

- [ ] **Step 1: Add documented sentinel and flip default to disabled**

Replace the existing block in `backend/config/constants.py`:

```python
# ── Archive lifecycle (R2 offload + local disk cleanup) ──────────────────────
ARCHIVE_OFFLOAD_AGE_DAYS = 1          # Offload local files to R2 after this age
# Set to 0 (or any value <= 0) to disable local-disk deletion entirely.
# R2 retains everything indefinitely, so this controls only the local cache.
ARCHIVE_RETENTION_DAYS = 0            # 0 = never delete locally
```

- [ ] **Step 2: Commit**

```bash
git add backend/config/constants.py
git commit -m "config: disable local archive deletion by default (keep R2 forever)"
```

---

### Task 2: Skip deletion phase when retention is disabled

**Files:**
- Modify: `backend/services/tasks/archive_lifecycle.py:31-86`
- Test: `backend/tests/test_archive_lifecycle.py` (create if missing, otherwise extend)

- [ ] **Step 1: Write the failing test**

Locate the existing archive-lifecycle test file. If `backend/tests/test_archive_lifecycle.py` does not exist, create it. Otherwise add the test below into the existing file alongside any existing tests.

```python
# backend/tests/test_archive_lifecycle.py
import os
import time
from pathlib import Path
from unittest.mock import patch

from services.tasks.archive_lifecycle import run_archive_lifecycle


def _touch(path: Path, age_days: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


def test_lifecycle_does_not_delete_when_retention_disabled(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    old_file = archive_dir / "2020" / "01" / "01" / "node1" / "detections_000000.json"
    _touch(old_file, age_days=400)

    monkeypatch.setattr("services.tasks.archive_lifecycle._ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr("services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", 0)

    with patch("services.r2_client.is_enabled", return_value=False):
        stats = run_archive_lifecycle()

    assert old_file.exists(), "file should NOT be deleted when retention is disabled"
    assert stats["deleted"] == 0


def test_lifecycle_still_deletes_when_retention_set(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    old_file = archive_dir / "2020" / "01" / "01" / "node1" / "detections_000000.json"
    _touch(old_file, age_days=400)

    monkeypatch.setattr("services.tasks.archive_lifecycle._ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr("services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", 14)

    with patch("services.r2_client.is_enabled", return_value=False):
        stats = run_archive_lifecycle()

    assert not old_file.exists(), "file should be deleted when retention is non-zero and exceeded"
    assert stats["deleted"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && pytest tests/test_archive_lifecycle.py -v`
Expected: `test_lifecycle_does_not_delete_when_retention_disabled` FAILS — current code still deletes regardless.

- [ ] **Step 3: Update `archive_lifecycle.py` to honour the disabled sentinel**

Edit `backend/services/tasks/archive_lifecycle.py`. Replace the `delete_cutoff` line and the Phase 2 deletion block.

OLD (lines ~40-75):

```python
    stats = {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0}
    now = time.time()
    offload_cutoff = now - (ARCHIVE_OFFLOAD_AGE_DAYS * 86400)
    delete_cutoff = now - (ARCHIVE_RETENTION_DAYS * 86400)
```

NEW:

```python
    stats = {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0}
    now = time.time()
    offload_cutoff = now - (ARCHIVE_OFFLOAD_AGE_DAYS * 86400)
    # ARCHIVE_RETENTION_DAYS <= 0 disables local deletion (R2 keeps forever).
    deletion_enabled = ARCHIVE_RETENTION_DAYS > 0
    delete_cutoff = now - (ARCHIVE_RETENTION_DAYS * 86400) if deletion_enabled else 0.0
```

OLD (Phase 2 block):

```python
        # Phase 2: Delete from local disk if past retention
        if mtime < delete_cutoff:
            try:
                json_file.unlink()
                stats["deleted"] += 1
            except OSError:
                stats["errors"] += 1
```

NEW:

```python
        # Phase 2: Delete from local disk if past retention (only when enabled)
        if deletion_enabled and mtime < delete_cutoff:
            try:
                json_file.unlink()
                stats["deleted"] += 1
            except OSError:
                stats["errors"] += 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && pytest tests/test_archive_lifecycle.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing regressed**

Run: `cd backend && pytest -q`
Expected: PASS (same coverage threshold as before).

- [ ] **Step 6: Commit**

```bash
git add backend/services/tasks/archive_lifecycle.py backend/tests/test_archive_lifecycle.py
git commit -m "feat(lifecycle): honour ARCHIVE_RETENTION_DAYS<=0 to disable local deletion"
```

---

### Task 3: Update the docstring at the top of `archive_lifecycle.py`

**Files:**
- Modify: `backend/services/tasks/archive_lifecycle.py:1-10`

- [ ] **Step 1: Edit the module docstring**

OLD:

```python
"""Archive lifecycle management: offload old detections to R2, prune local disk.

Runs every hour as a background task. The lifecycle is:
  1. Files older than OFFLOAD_AGE_DAYS → upload to R2 under "archive/" prefix
  2. Files older than RETENTION_DAYS  → delete from local disk
  3. Empty date directories are cleaned up

If R2 is not configured, step 1 is skipped — local files still get deleted
after RETENTION_DAYS to prevent unbounded disk growth.
"""
```

NEW:

```python
"""Archive lifecycle management: offload old detections to R2, prune local disk.

Runs every hour as a background task. The lifecycle is:
  1. Files older than OFFLOAD_AGE_DAYS → upload to R2 under "archive/" prefix
  2. Files older than RETENTION_DAYS   → delete from local disk
                                         (skipped when RETENTION_DAYS <= 0)
  3. Empty date directories are cleaned up

R2 retains uploaded files indefinitely. The default config keeps local files
forever (RETENTION_DAYS = 0); set a positive value if disk pressure becomes
an issue on a given deployment.
"""
```

- [ ] **Step 2: Commit**

```bash
git add backend/services/tasks/archive_lifecycle.py
git commit -m "docs(lifecycle): document RETENTION_DAYS<=0 disable behaviour"
```

---

## Self-review checklist

- [x] **Spec coverage:** Disables 14-day local deletion (configurable; default off). R2 path unchanged. ✓
- [x] **No placeholders:** every step has concrete code or commands. ✓
- [x] **Type consistency:** `ARCHIVE_RETENTION_DAYS` type unchanged (`int`). `deletion_enabled` is local `bool`. ✓
- [x] **Tests cover both branches:** disabled (no delete) + enabled (still deletes). ✓
