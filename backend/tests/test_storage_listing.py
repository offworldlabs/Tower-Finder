"""Tests for list_archived_files() — date_prefix and no-prefix branches."""

import os

import pytest

import services.storage as _storage
from services.storage import list_archived_files


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    d = tmp_path / "archive"
    d.mkdir()
    monkeypatch.setattr(_storage, "_LOCAL_ARCHIVE_DIR", str(d))
    return d


class TestListArchivedFiles:
    def test_date_prefix_nonexistent_returns_empty(self, archive_dir):
        result = list_archived_files(date_prefix="2099/12/31")
        assert result == {"files": [], "count": 0, "total": 0}

    def test_date_prefix_returns_files_in_range(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_120000.json").write_text('{"test": 1}')
        (node_dir / "detections_130000.json").write_text('{"test": 2}')

        result = list_archived_files(date_prefix="2025/06/21")
        assert result["count"] == 2
        assert result["total"] == 2
        assert len(result["files"]) == 2

    def test_date_prefix_node_filter(self, archive_dir):
        for node in ("node-A", "node-B"):
            nd = archive_dir / "2025" / "06" / "21" / node
            nd.mkdir(parents=True)
            (nd / "detections_120000.json").write_text('{"node": "' + node + '"}')

        result = list_archived_files(date_prefix="2025/06/21", node_id="node-A")
        assert result["count"] == 1
        assert result["total"] == 1
        assert "node-A" in result["files"][0]["key"]

    def test_date_prefix_sort_desc(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        older = node_dir / "detections_100000.json"
        newer = node_dir / "detections_110000.json"
        older.write_text('{"order": "old"}')
        newer.write_text('{"order": "new"}')

        # Force distinct mtimes: older gets mtime in the past
        old_mtime = 1_000_000.0
        new_mtime = 2_000_000.0
        os.utime(str(older), (old_mtime, old_mtime))
        os.utime(str(newer), (new_mtime, new_mtime))

        result = list_archived_files(date_prefix="2025/06/21", sort_desc=True)
        files = result["files"]
        assert len(files) == 2
        # Newest file (detections_110000) must come first by identity, not string comparison.
        assert "detections_110000" in files[0]["key"]
        assert "detections_100000" in files[1]["key"]

    def test_date_prefix_pagination_offset(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        for i in range(3):
            (node_dir / f"detections_10000{i}.json").write_text(f'{{"i": {i}}}')

        result = list_archived_files(date_prefix="2025/06/21", limit=2, offset=1)
        assert result["total"] == 3
        assert result["count"] == 2
        assert len(result["files"]) == 2

    def test_no_date_prefix_returns_files(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-X"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_080000.json").write_text('{"x": 1}')

        result = list_archived_files()
        keys = [f["key"] for f in result["files"]]
        assert any("node-X" in k for k in keys)

    def test_no_date_prefix_missing_base_returns_empty(self, tmp_path, monkeypatch):
        # _ensure_local_dir() creates the dir if absent, then the traversal finds
        # no files and returns an empty result.
        missing = str(tmp_path / "does_not_exist")
        monkeypatch.setattr(_storage, "_LOCAL_ARCHIVE_DIR", missing)

        result = list_archived_files()
        assert result["files"] == []
        assert result["count"] == 0
        assert result["total"] == 0

    def test_file_entry_has_required_fields(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_120000.json").write_text('{"check": true}')

        result = list_archived_files(date_prefix="2025/06/21")
        assert result["count"] == 1
        entry = result["files"][0]
        assert "key" in entry
        assert "size_bytes" in entry
        assert "modified" in entry
