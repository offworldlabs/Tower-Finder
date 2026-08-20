"""Tests for the runtime config overlay helpers."""

import os

from core.runtime_config import write_runtime_file


class TestWriteRuntimeFile:
    def test_replaces_the_target(self, tmp_path):
        target = tmp_path / "tower_config.json"
        target.write_text("old")

        write_runtime_file(target, "new")

        assert target.read_text() == "new"

    def test_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "tower_config.json"

        write_runtime_file(target, "content")

        assert [p.name for p in tmp_path.iterdir()] == ["tower_config.json"]

    def test_each_call_uses_its_own_temp_file(self, tmp_path, monkeypatch):
        """Two writers to one path must not share a temp file.

        Both PUT /api/config and PUT /admin/config/towers write
        tower_config.json. With a shared temp name, one request's half-written
        content can be renamed into place by the other, so what lands on disk is
        a config neither of them validated.
        """
        seen = []
        real_replace = os.replace

        def _record(src, dst):
            seen.append(str(src))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _record)
        target = tmp_path / "tower_config.json"

        write_runtime_file(target, "a")
        write_runtime_file(target, "b")

        assert len(set(seen)) == 2, "both writes used the same temp path"
        assert target.read_text() == "b"

    def test_a_failed_write_does_not_touch_the_target(self, tmp_path):
        target = tmp_path / "tower_config.json"
        target.write_text("old")

        try:
            write_runtime_file(target, None)  # TypeError inside write_text
        except TypeError:
            pass

        assert target.read_text() == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["tower_config.json"]
