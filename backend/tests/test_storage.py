"""Tests for detection archive storage module."""

import os
import shutil

import pytest

from services.storage import archive_detections, list_archived_files, read_archived_file


class TestArchiveStorage:
    @pytest.fixture(autouse=True)
    def cleanup_archive(self):
        """Remove test node data after each test."""
        yield
        archive_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "coverage_data", "archive",
        )
        if os.path.exists(archive_dir):
            for child in os.listdir(archive_dir):
                p = os.path.join(archive_dir, child)
                if os.path.isdir(p):
                    shutil.rmtree(p)

    def test_archive_returns_key(self):
        key = archive_detections("test-storage-node", [
            {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
        ])
        assert isinstance(key, str) and "/" in key
        assert "test-storage-node" in key

    def test_list_finds_archived(self):
        archive_detections("test-storage-node", [
            {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
        ])
        result = list_archived_files(node_id="test-storage-node")
        files = result["files"]
        assert len(files) >= 1
        assert "key" in files[0]
        assert "size_bytes" in files[0]

    def test_read_archived_file(self):
        archive_detections("test-storage-node", [
            {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
        ])
        result = list_archived_files(node_id="test-storage-node")
        data = read_archived_file(result["files"][0]["key"])
        assert isinstance(data, dict)
        assert data.get("node_id") == "test-storage-node"
        assert isinstance(data.get("detections"), list)

    def test_path_traversal_blocked(self):
        assert read_archived_file("../../etc/passwd") is None
