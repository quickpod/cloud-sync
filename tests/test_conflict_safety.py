r"""The local file must survive a failed conflict resolution.

A private key was lost in the field: the resolver moved the local file to a
"conflicted copy" name and then downloaded the remote version into its place.
The download did not land, so the real name had nothing at it. These tests pin
the ordering that prevents it.
"""

from __future__ import annotations

import os

import pytest

from cloudsync import rclone, syncengine as se
from cloudsync.errors import CloudSyncError
from cloudsync.syncengine import Pair, SyncEngine


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    local = tmp_path / "local"
    local.mkdir()
    return Pair(local=str(local), remote="R", rpath="bucket/")


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_local_file_survives_a_failed_download(pair, monkeypatch):
    """The exact field failure: the fetch fails, the local file must remain."""
    target = write(os.path.join(pair.local, "secret.key"), "PRIVATE KEY DATA")

    def boom(src, dst):
        raise CloudSyncError("connection dropped")
    monkeypatch.setattr(rclone, "copyto", boom)
    monkeypatch.setattr(rclone, "stat_path", lambda *a, **k: None)

    eng = SyncEngine([pair], notify=lambda e: None)
    eng._resolve_conflict(pair, "secret.key", target, local_m=1.0, remote_m=2.0)

    assert os.path.exists(target), "the local file was moved away and lost"
    with open(target) as fh:
        assert fh.read() == "PRIVATE KEY DATA"


def test_no_conflicted_copy_is_left_behind_on_failure(pair, monkeypatch):
    target = write(os.path.join(pair.local, "notes.txt"), "mine")
    monkeypatch.setattr(rclone, "copyto",
                        lambda *a, **k: (_ for _ in ()).throw(
                            CloudSyncError("nope")))
    monkeypatch.setattr(rclone, "stat_path", lambda *a, **k: None)
    SyncEngine([pair], notify=lambda e: None)._resolve_conflict(
        pair, "notes.txt", target, local_m=1.0, remote_m=2.0)
    leftovers = [n for n in os.listdir(pair.local) if n != "notes.txt"]
    assert leftovers == [], f"stray files left behind: {leftovers}"


def test_a_download_that_writes_nothing_is_treated_as_failure(pair, monkeypatch):
    """copyto returning cleanly without producing a file must not count."""
    target = write(os.path.join(pair.local, "doc.md"), "original")
    monkeypatch.setattr(rclone, "copyto", lambda src, dst: None)   # writes nothing
    monkeypatch.setattr(rclone, "stat_path", lambda *a, **k: None)
    SyncEngine([pair], notify=lambda e: None)._resolve_conflict(
        pair, "doc.md", target, local_m=1.0, remote_m=2.0)
    assert os.path.exists(target)
    with open(target) as fh:
        assert fh.read() == "original"


def test_successful_resolution_keeps_both_versions(pair, monkeypatch):
    target = write(os.path.join(pair.local, "report.txt"), "local version")

    def fake_copyto(src, dst):
        # Downloading the remote produces a local file; uploads are no-ops.
        if not str(src).startswith(pair.local):
            write(dst, "remote version")
    monkeypatch.setattr(rclone, "copyto", fake_copyto)
    monkeypatch.setattr(rclone, "stat_path", lambda *a, **k: None)

    SyncEngine([pair], notify=lambda e: None)._resolve_conflict(
        pair, "report.txt", target, local_m=1.0, remote_m=2.0)

    with open(target) as fh:
        assert fh.read() == "remote version"      # newer wins the real name
    kept = [n for n in os.listdir(pair.local) if "conflicted copy" in n]
    assert len(kept) == 1                          # and the local one survives
    with open(os.path.join(pair.local, kept[0])) as fh:
        assert fh.read() == "local version"


def test_no_incoming_temp_file_is_left_after_success(pair, monkeypatch):
    target = write(os.path.join(pair.local, "a.txt"), "local")
    monkeypatch.setattr(rclone, "copyto",
                        lambda src, dst: (None if str(src).startswith(pair.local)
                                          else write(dst, "remote")))
    monkeypatch.setattr(rclone, "stat_path", lambda *a, **k: None)
    SyncEngine([pair], notify=lambda e: None)._resolve_conflict(
        pair, "a.txt", target, local_m=1.0, remote_m=2.0)
    assert not any(n.endswith(".cloudsync-incoming")
                   for n in os.listdir(pair.local))
