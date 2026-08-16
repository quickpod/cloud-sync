"""Tests for the continuous-sync engine (FEATURES-NEXT round).

Everything runs without rclone, a network or watchdog: the rclone module
functions the engine calls are replaced with an in-memory fake remote, so the
newer-wins/conflicted-copy semantics, deletion propagation, reconcile and
pause/resume are all asserted hermetically ($CLOUDSYNC_HOME keeps state in a
tmp dir).
"""

import os
import time

import pytest

from cloudsync import guiconfig, rclone, syncengine
from cloudsync.syncengine import (
    CONFLICT, ERROR, SYNCED, Pair, SyncEngine, conflicted_name,
    pair_from_dict, parse_daily_time, parse_mod_time, seconds_until_daily,
    should_ignore,
)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_should_ignore():
    assert should_ignore("foo.tmp")
    assert should_ignore("bar.part")
    assert should_ignore("~$report.docx")
    assert should_ignore(".~lock.ods")
    assert not should_ignore("notes.txt")
    assert not should_ignore("archive.tar.gz")


def test_conflicted_name(tmp_path):
    p = str(tmp_path / "report.txt")
    c1 = conflicted_name(p, when=1755300000)
    assert "(conflicted copy" in c1 and c1.endswith(".txt")
    open(c1, "w").write("x")
    c2 = conflicted_name(p, when=1755300000)
    assert c2 != c1 and "2)" in c2


def test_parse_mod_time():
    t = parse_mod_time("2026-08-16T07:00:00Z")
    assert t and abs(t - 1786863600) < 24 * 3600
    assert parse_mod_time("2026-08-16T07:00:00.123456789Z")
    assert parse_mod_time("") is None
    assert parse_mod_time("garbage") is None


def test_parse_daily_time_and_wait():
    assert parse_daily_time("21:30") == (21, 30)
    assert parse_daily_time("7:05") == (7, 5)
    assert parse_daily_time("25:00") is None
    assert parse_daily_time("") is None
    w = seconds_until_daily(0, 0)
    assert 0 < w <= 24 * 3600


def test_pair_from_dict():
    assert pair_from_dict({"local": "/a", "remote": "r"}) == \
        Pair(local="/a", remote="r", rpath="")
    assert pair_from_dict({"local": "", "remote": "r"}) is None
    p = Pair(local="/a", remote="r", rpath="bucket/docs")
    assert p.remote_file("x/y.txt") == "r:bucket/docs/x/y.txt"
    assert p.key == Pair(local="/a", remote="r", rpath="bucket/docs").key


def test_guiconfig_pairs_and_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSYNC_HOME", str(tmp_path))
    assert guiconfig.get_theme() == "system"
    assert guiconfig.get_sync_mode() == "realtime"
    guiconfig.add_pair("/data", "work", "bucket")
    assert guiconfig.get_pairs() == [
        {"local": "/data", "remote": "work", "rpath": "bucket"}]
    guiconfig.remove_pair("/data", "work", "bucket")
    assert guiconfig.get_pairs() == []
    guiconfig.set_sync_mode("scheduled")
    guiconfig.set_interval_minutes(15)
    guiconfig.set_daily_at("21:30")
    guiconfig.set_paused(True)
    cfg = guiconfig.load()
    assert cfg["sync_mode"] == "scheduled"
    assert cfg["interval_minutes"] == 15
    assert cfg["daily_at"] == "21:30"
    assert cfg["paused"] is True


# ---------------------------------------------------------------------------
# the engine against an in-memory fake remote
# ---------------------------------------------------------------------------
class FakeRemote:
    """A dict-backed stand-in for the rclone per-file operations."""

    def __init__(self):
        self.files = {}          # remote_file -> (bytes, mod_time_str)
        self.deleted = []

    def install(self, monkeypatch):
        def stat_path(remote, path):
            key = f"{remote}:{path}"
            if key not in self.files:
                return None
            data, mt = self.files[key]
            return rclone.Entry(name=os.path.basename(path), path=path,
                                size=len(data), is_dir=False, mod_time=mt)

        def copyto(src, dst):
            if ":" in dst.split(os.sep)[0] and not os.path.isabs(dst):
                # local -> remote
                with open(src, "rb") as fh:
                    self.files[dst] = (fh.read(),
                                       "2026-08-16T09:00:00Z")
            else:
                # remote -> local
                data, _mt = self.files[src]
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(data)

        def deletefile(remote_file):
            self.files.pop(remote_file, None)
            self.deleted.append(remote_file)

        def list_recursive(remote, path=""):
            prefix = f"{remote}:{path}".rstrip("/")
            out = []
            for key, (data, mt) in self.files.items():
                if not key.startswith(prefix):
                    continue
                rel = key[len(prefix):].lstrip("/")
                out.append(rclone.Entry(name=os.path.basename(rel), path=rel,
                                        size=len(data), is_dir=False,
                                        mod_time=mt))
            return out

        monkeypatch.setattr(rclone, "stat_path", stat_path)
        monkeypatch.setattr(rclone, "copyto", copyto)
        monkeypatch.setattr(rclone, "deletefile", deletefile)
        monkeypatch.setattr(rclone, "list_recursive", list_recursive)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSYNC_HOME", str(tmp_path / "home"))
    local = tmp_path / "local"
    local.mkdir()
    fake = FakeRemote()
    fake.install(monkeypatch)
    pair = Pair(local=str(local), remote="work", rpath="bucket")
    events = []
    eng = SyncEngine([pair], notify=events.append)
    return eng, pair, fake, local, events


def test_upload_new_file(env):
    eng, pair, fake, local, events = env
    (local / "a.txt").write_text("hello")
    eng._process_file(pair, "a.txt")
    assert "work:bucket/a.txt" in fake.files
    assert fake.files["work:bucket/a.txt"][0] == b"hello"
    state = syncengine.load_state(pair)
    assert "a.txt" in state
    assert eng.file_status[(pair.key, "a.txt")][0] == SYNCED


def test_delete_propagates_only_synced(env):
    eng, pair, fake, local, events = env
    (local / "a.txt").write_text("hello")
    eng._process_file(pair, "a.txt")
    (local / "a.txt").unlink()
    eng._process_file(pair, "a.txt")
    assert "work:bucket/a.txt" not in fake.files
    assert "work:bucket/a.txt" in fake.deleted
    # a file that never synced is NOT deleted remotely
    eng._process_file(pair, "ghost.txt")
    assert "work:bucket/ghost.txt" not in fake.deleted


def test_conflict_remote_newer_keeps_local_copy(env):
    eng, pair, fake, local, events = env
    (local / "doc.txt").write_text("v1")
    eng._process_file(pair, "doc.txt")
    # someone changes the file remotely AFTER our sync…
    fake.files["work:bucket/doc.txt"] = (b"remote-v2",
                                         "2030-01-01T00:00:00Z")
    # …and we also edit it locally (older than the remote edit)
    (local / "doc.txt").write_text("local-v2")
    os.utime(local / "doc.txt", (time.time() - 60, time.time() - 60))
    eng._process_file(pair, "doc.txt")
    # canonical local file now carries the (newer) remote content
    assert (local / "doc.txt").read_bytes() == b"remote-v2"
    # the losing local edit is KEPT as a conflicted copy — and synced up
    copies = [p for p in os.listdir(local) if "conflicted copy" in p]
    assert len(copies) == 1
    assert (local / copies[0]).read_text() == "local-v2"
    assert any("conflicted copy" in k for k in fake.files)
    assert eng.file_status[(pair.key, "doc.txt")][0] == CONFLICT


def test_conflict_local_newer_keeps_remote_copy(env):
    eng, pair, fake, local, events = env
    (local / "doc.txt").write_text("v1")
    eng._process_file(pair, "doc.txt")
    fake.files["work:bucket/doc.txt"] = (b"remote-v2",
                                         "2020-01-01T00:00:00Z")
    (local / "doc.txt").write_text("local-newer")
    eng._process_file(pair, "doc.txt")
    # local (newer) wins the canonical name on the remote
    assert fake.files["work:bucket/doc.txt"][0] == b"local-newer"
    # the losing remote edition is preserved locally as a conflicted copy
    copies = [p for p in os.listdir(local) if "conflicted copy" in p]
    assert len(copies) == 1
    assert (local / copies[0]).read_bytes() == b"remote-v2"


def test_reconcile_downloads_new_remote_files(env):
    eng, pair, fake, local, events = env
    fake.files["work:bucket/fresh.txt"] = (b"from-cloud",
                                           "2026-08-16T08:00:00Z")
    eng._reconcile_pair(pair)
    assert (local / "fresh.txt").read_bytes() == b"from-cloud"
    assert eng.file_status[(pair.key, "fresh.txt")][0] == SYNCED


def test_worker_queue_pause_resume(env):
    eng, pair, fake, local, events = env
    (local / "q.txt").write_text("queued")
    eng._start_worker()
    try:
        eng.pause()
        eng._enqueue(pair, "q.txt")
        time.sleep(1.2)
        assert "work:bucket/q.txt" not in fake.files   # held while paused
        assert eng.paused
        eng.resume()
        deadline = time.time() + 10
        while "work:bucket/q.txt" not in fake.files and time.time() < deadline:
            time.sleep(0.1)
        assert "work:bucket/q.txt" in fake.files
    finally:
        eng.stop()


def test_engine_error_surfaces(env, monkeypatch):
    eng, pair, fake, local, events = env
    (local / "x.txt").write_text("x")

    def boom(*a, **k):
        from cloudsync.errors import CloudSyncError
        raise CloudSyncError("network down")
    monkeypatch.setattr(rclone, "copyto", boom)
    eng._start_worker()
    try:
        eng._enqueue(pair, "x.txt")
        deadline = time.time() + 10
        while (pair.key, "x.txt") not in eng._errors and \
                time.time() < deadline:
            time.sleep(0.1)
        assert eng.file_status[(pair.key, "x.txt")][0] == ERROR
        assert "network down" in eng.file_status[(pair.key, "x.txt")][1]
        overall = [e for e in events if e.get("kind") == "overall"]
        assert overall and overall[-1]["errors"] >= 1
    finally:
        eng.stop()
