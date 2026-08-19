r"""stat_path must report a missing object as missing.

On an S3 backend `rclone lsjson --stat` does not fail for an object that is not
there: it succeeds and describes a phantom directory with an empty name and
ModTime set to *now*. Parsed naively that reads as "the remote has this file
and it is newer than yours", which pushes the engine into conflict resolution
for a file that does not exist -- and the fetch that follows can only fail.
"""

from __future__ import annotations

import json

import pytest

from cloudsync import rclone

# Exactly what rclone returns for a missing object on R2/S3.
PHANTOM = json.dumps({
    "Path": "", "Name": "", "Size": -1,
    "MimeType": "inode/directory",
    "ModTime": "2026-08-19T11:17:05.949208277-04:00",
    "IsDir": True,
})
REAL = json.dumps({
    "Path": "notes.txt", "Name": "notes.txt", "Size": 6,
    "MimeType": "text/plain",
    "ModTime": "2026-08-19T10:00:00.000000000-04:00",
    "IsDir": False,
})


def test_a_missing_object_is_none(monkeypatch):
    monkeypatch.setattr(rclone, "_run", lambda *a, **k: PHANTOM)
    assert rclone.stat_path("R", "bucket/gone.txt") is None


def test_a_real_object_is_returned(monkeypatch):
    monkeypatch.setattr(rclone, "_run", lambda *a, **k: REAL)
    e = rclone.stat_path("R", "bucket/notes.txt")
    assert e is not None
    assert e.name == "notes.txt" and e.size == 6 and e.is_dir is False


def test_the_phantom_timestamp_is_never_surfaced(monkeypatch):
    """The dangerous part is ModTime=now making the remote look newer."""
    monkeypatch.setattr(rclone, "_run", lambda *a, **k: PHANTOM)
    assert rclone.stat_path("R", "bucket/gone.txt") is None


def test_an_explicit_not_found_error_is_still_none(monkeypatch):
    from cloudsync.errors import CloudSyncError

    def boom(*_a, **_k):
        raise CloudSyncError("directory not found")
    monkeypatch.setattr(rclone, "_run", boom)
    assert rclone.stat_path("R", "bucket/gone.txt") is None


def test_a_real_failure_still_raises(monkeypatch):
    from cloudsync.errors import CloudSyncError

    def boom(*_a, **_k):
        raise CloudSyncError("connection refused")
    monkeypatch.setattr(rclone, "_run", boom)
    with pytest.raises(CloudSyncError):
        rclone.stat_path("R", "bucket/x.txt")


@pytest.mark.parametrize("payload", ["", "   ", "null", "not json", "[]"])
def test_empty_or_unparseable_output_is_none(monkeypatch, payload):
    monkeypatch.setattr(rclone, "_run", lambda *a, **k: payload)
    assert rclone.stat_path("R", "bucket/x.txt") is None
