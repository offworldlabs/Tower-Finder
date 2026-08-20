"""Tests for detection archive storage module."""

import pytest

from services.storage import archive_detections, list_archived_files, read_archived_file


class TestArchiveStorage:
    @pytest.fixture(autouse=True)
    def isolated_archive(self, monkeypatch, tmp_path):
        """Point the archive at a per-test directory.

        These tests used to write into the real backend/coverage_data/archive
        and delete every subdirectory of it afterwards. That is shared between
        concurrent suite runs, so one run's cleanup deletes another's parquet
        files mid-write, which surfaces as a FileNotFoundError from
        list_archived_files in whichever run loses. Two worktrees at once
        reproduces it; xdist workers would too. tmp_path is per test and pytest
        disposes of it, so nothing has to be torn down by hand. Matches
        test_storage_listing.py and test_parquet_writer.py, which already do
        this.
        """
        monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    def test_archive_returns_key(self):
        key = archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        assert isinstance(key, str) and "/" in key
        assert "test-storage-node" in key

    def test_list_finds_archived(self):
        archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        result = list_archived_files(node_id="test-storage-node")
        files = result["files"]
        assert len(files) >= 1
        assert "key" in files[0]
        assert "size_bytes" in files[0]

    def test_read_archived_file(self):
        archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        result = list_archived_files(node_id="test-storage-node")
        data = read_archived_file(result["files"][0]["key"])
        assert isinstance(data, dict)
        assert data.get("node_id") == "test-storage-node"
        assert isinstance(data.get("detections"), list)

    def test_path_traversal_blocked(self):
        assert read_archived_file("../../etc/passwd") is None
