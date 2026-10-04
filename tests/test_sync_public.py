"""Focused safety checks for the managed public projection."""
from pathlib import Path

import pytest

from scripts import sync_public


def test_protected_paths_are_rejected_case_insensitively():
    for value in (".GiT/config", "docs/.git/objects", "public-manifest.json"):
        with pytest.raises(sync_public.SyncError):
            sync_public.clean_path(value)


def test_unmanaged_destination_file_is_a_conflict(tmp_path):
    destination = tmp_path / "public"
    destination.mkdir()
    (destination / "README.md").write_bytes(b"local edit")
    entry = sync_public.Entry("README.md", "README.md", b"source bytes", "source")
    operations, conflicts = sync_public.plan_changes(destination, None, {"README.md": entry})
    assert operations == ["write README.md"]
    assert conflicts == ["unmanaged destination would be overwritten: README.md"]
    assert (destination / "README.md").read_bytes() == b"local edit"


def test_regular_parent_file_is_rejected_before_apply(tmp_path):
    destination = tmp_path / "public"
    destination.mkdir()
    (destination / "docs").write_bytes(b"file blocks directory")
    entry = sync_public.Entry("docs/readme.md", "docs/readme.md", b"source bytes", "source")
    with pytest.raises(sync_public.SyncError, match="regular file"):
        sync_public.plan_changes(destination, None, {"docs/readme.md": entry})
