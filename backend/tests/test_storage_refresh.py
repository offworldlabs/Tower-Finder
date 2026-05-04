"""Unit tests for services/tasks/storage_refresh.py.

Covers _scan_archive_dir() and _build_storage_result().
"""

import collections
import time
from unittest.mock import MagicMock, patch

import orjson
import pytest

from services.tasks.storage_refresh import _build_storage_result, _scan_archive_dir

# ── Helpers ───────────────────────────────────────────────────────────────────


def _du_mock(returncode: int, stdout: str) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


# ── _scan_archive_dir ─────────────────────────────────────────────────────────


class TestScanArchiveDirNonExistent:
    def test_returns_zeros_when_dir_missing(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        total_files, total_bytes, per_node = _scan_archive_dir(missing)
        assert total_files == 0
        assert total_bytes == 0
        assert per_node == {}


class TestScanArchiveDirBytes:
    def test_node_day_entry_sets_per_node_bytes(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        # Simulate: archive/2024/01/15/node-A  →  4-part relative path
        node_path = str(archive_dir / "2024" / "01" / "15" / "node-A")
        du_bytes_stdout = f"1024\t{node_path}\n"
        # inodes: no entries (returncode != 0)
        du_inodes_mock = _du_mock(1, "")

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=[_du_mock(0, du_bytes_stdout), du_inodes_mock],
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert "node-A" in per_node
        assert per_node["node-A"]["bytes"] == 1024
        # total_bytes falls back to sum of per_node since no root line provided
        assert total_bytes == 1024

    def test_root_line_sets_total_bytes(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        # A root line has path equal to archive_dir itself → relative_to returns
        # Path('.') with len(parts) == 0, so total_bytes is set from it.
        du_bytes_stdout = f"99999\t{archive_dir}\n"
        du_inodes_mock = _du_mock(1, "")

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=[_du_mock(0, du_bytes_stdout), du_inodes_mock],
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert total_bytes == 99999
        assert per_node == {}

    def test_total_bytes_zero_falls_back_to_sum(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        # Provide node-day bytes but no root line → total_bytes stays 0 → should
        # be replaced by sum of per-node bytes.
        node_path = str(archive_dir / "2024" / "02" / "20" / "node-B")
        du_bytes_stdout = f"8192\t{node_path}\n"
        du_inodes_mock = _du_mock(1, "")

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=[_du_mock(0, du_bytes_stdout), du_inodes_mock],
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert total_bytes == 8192  # summed from per_node

    def test_du_exception_triggers_fallback(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        fallback_mock = _du_mock(0, f"512000\t{archive_dir}\n")

        def _side_effect(cmd, **kwargs):
            if "--max-depth=4" in cmd:
                raise OSError("du not found")
            return fallback_mock

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=_side_effect,
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert total_bytes == 512000
        assert per_node == {}


class TestScanArchiveDirInodes:
    def test_inode_entry_sets_per_node_files(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        node_path = str(archive_dir / "2024" / "03" / "10" / "node-C")

        # First call: bytes du (no root line, one node entry)
        du_bytes_stdout = f"2048\t{node_path}\n"
        # Second call: inodes du
        du_inodes_stdout = f"7\t{node_path}\n"

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=[
                _du_mock(0, du_bytes_stdout),
                _du_mock(0, du_inodes_stdout),
            ],
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert "node-C" in per_node
        assert per_node["node-C"]["files"] == 7

    def test_inode_root_line_sets_total_files(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        node_path = str(archive_dir / "2024" / "04" / "01" / "node-D")
        du_bytes_stdout = f"4096\t{node_path}\n"
        # Inode root line → total_files
        du_inodes_stdout = f"42\t{archive_dir}\n"

        with patch(
            "services.tasks.storage_refresh.subprocess.run",
            side_effect=[
                _du_mock(0, du_bytes_stdout),
                _du_mock(0, du_inodes_stdout),
            ],
        ):
            total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

        assert total_files == 42


# ── _build_storage_result ─────────────────────────────────────────────────────


_DiskUsage = collections.namedtuple("DiskUsage", ["total", "used", "free"])


def _make_disk_usage(total=100 * 1024 ** 3, used=40 * 1024 ** 3, free=60 * 1024 ** 3):
    return _DiskUsage(total=total, used=used, free=free)


class TestBuildStorageResult:

    def test_returns_json_bytes_with_required_keys(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        with patch("services.tasks.storage_refresh._scan_archive_dir", return_value=(0, 0, {})):
            with patch("services.tasks.storage_refresh.shutil.disk_usage", return_value=_make_disk_usage()):
                result = _build_storage_result(archive_dir)

        assert isinstance(result, bytes)
        parsed = orjson.loads(result)
        for key in ("archive_files", "archive_bytes", "archive_mb", "per_node", "disk", "write_rate"):
            assert key in parsed, f"Missing key: {key}"

    def test_days_until_full_is_zero_when_no_write_rate(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        with patch("services.tasks.storage_refresh._scan_archive_dir", return_value=(0, 0, {})):
            with patch("services.tasks.storage_refresh.shutil.disk_usage", return_value=_make_disk_usage()):
                result = _build_storage_result(archive_dir)

        parsed = orjson.loads(result)
        assert parsed["write_rate"]["days_until_full"] == 0.0
        assert parsed["write_rate"]["total_bytes_per_day"] == 0.0

    def test_per_node_write_rate_computed_from_first_seen_ts(self, tmp_path):
        from core import state

        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        now = time.time()
        # node-X has been seen for 10 days, accumulated 864_000 bytes → 86400 B/day
        first_seen = now - 10 * 86400
        node_bytes = 864_000

        per_node_data = {"node-X": {"bytes": node_bytes, "files": 10}}
        state.connected_nodes["node-X"] = {"first_seen_ts": first_seen}

        try:
            with patch("services.tasks.storage_refresh._scan_archive_dir", return_value=(10, node_bytes, per_node_data)):
                with patch("services.tasks.storage_refresh.shutil.disk_usage", return_value=_make_disk_usage()):
                    result = _build_storage_result(archive_dir)
        finally:
            state.connected_nodes.pop("node-X", None)

        parsed = orjson.loads(result)
        rate = parsed["write_rate"]["per_node_bytes_per_day"].get("node-X")
        assert rate is not None
        # Should be close to 86400 B/day (within 1% given floating-point age calc)
        assert abs(rate - 86400) < 1000

        # days_until_full = free / total_rate
        assert parsed["write_rate"]["days_until_full"] > 0

    def test_archive_mb_matches_bytes(self, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        total_bytes = 10 * 1024 * 1024  # exactly 10 MB

        with patch("services.tasks.storage_refresh._scan_archive_dir", return_value=(5, total_bytes, {})):
            with patch("services.tasks.storage_refresh.shutil.disk_usage", return_value=_make_disk_usage()):
                result = _build_storage_result(archive_dir)

        parsed = orjson.loads(result)
        assert parsed["archive_bytes"] == total_bytes
        assert parsed["archive_mb"] == pytest.approx(10.0, abs=0.01)
