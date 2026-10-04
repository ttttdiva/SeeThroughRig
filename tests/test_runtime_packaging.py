"""Release output must come from Git, not platform-specific checkout bytes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_runtime", ROOT / "scripts/package_runtime.py")
assert spec is not None and spec.loader is not None
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)

try:
    packager.git("cat-file", "-e", packager.BASELINE + "^{commit}")
    HAS_BASELINE_GIT = True
except (OSError, subprocess.CalledProcessError):
    HAS_BASELINE_GIT = False


@pytest.mark.skipif(not HAS_BASELINE_GIT, reason='historical runtime baseline is unavailable in a fresh public checkout')
def test_release_reads_immutable_git_bytes_not_worktree(tmp_path, monkeypatch):
    head = packager.git("rev-parse", "HEAD").decode().strip()
    original = Path.read_bytes

    def no_worktree_read(path):
        if path.is_relative_to(ROOT / "web") or path.is_relative_to(ROOT / "docs"):
            raise AssertionError("Release cannot depend on CRLF conversions or uncommitted working-tree contents")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", no_worktree_read)
    output = tmp_path / "runtime"
    packager.build(output, head)
    metadata = json.loads((output / "runtime-provenance.json").read_text(encoding="utf-8"))
    assert metadata["source_head"] == head and metadata["source_mode"] == "git"
    assert metadata["engines"][0]["source"] == "git"
    for row in metadata["files"]:
        assert row["source"] != "working-tree"
        assert hashlib.sha256((output / row["path"]).read_bytes()).hexdigest() == row["sha256"]
        if row["source"] == "git":
            assert (output / row["path"]).read_bytes() == packager.git("show", row["source_commit"] + ":" + row["source_path"])


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "runtime"
    output.mkdir()
    original = output / "keep.txt"
    original.write_text("existing artifact", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        packager.build(output, "HEAD")
    assert original.read_text(encoding="utf-8") == "existing artifact"
