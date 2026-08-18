r"""Filesystem events must be filtered before they cost anything.

Each queued path spawns an rclone process to stat the remote. Unfiltered, one
`git status` or Python run queued hundreds of paths that would only be
discarded later -- the machine ran hot while apparently idle.
"""

from __future__ import annotations

import os

import pytest

from cloudsync import syncengine as se
from cloudsync.syncengine import Pair, SyncEngine


@pytest.fixture
def handler(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    local = tmp_path / "local"
    local.mkdir()
    pair = Pair(local=str(local), remote="R", rpath="bucket/")
    eng = SyncEngine([pair], notify=lambda e: None)
    queued = []
    eng.queue_change = lambda p, rel: queued.append(rel)
    watchdog = pytest.importorskip("watchdog")            # noqa: F841
    started = eng._start_watchdog()
    if not started:
        pytest.skip("watchdog observer unavailable")
    # Recover the handler the engine built.
    obs = eng._observer
    h = next(iter(obs._handlers.values()))
    h = next(iter(h))
    yield h, pair, queued
    try:
        obs.stop()
    except Exception:
        pass


class Event:
    def __init__(self, path, is_dir=False):
        self.src_path = path
        self.dest_path = path
        self.is_directory = is_dir


@pytest.mark.parametrize("rel", [
    ".git/index", "repo/.git/refs/heads/main", "__pycache__/m.pyc",
    "node_modules/pkg/index.js", "build.log", "x.tmp", ".DS_Store",
])
def test_noise_never_reaches_the_queue(handler, rel):
    h, pair, queued = handler
    h.on_modified(Event(os.path.join(pair.local, rel)))
    assert queued == [], f"{rel} was queued"


@pytest.mark.parametrize("rel", ["notes.txt", "src/main.py", "a/b/c.md"])
def test_real_files_still_reach_the_queue(handler, rel):
    h, pair, queued = handler
    h.on_modified(Event(os.path.join(pair.local, rel)))
    assert queued == [rel]


def test_directory_events_are_ignored(handler):
    h, pair, queued = handler
    h.on_created(Event(os.path.join(pair.local, "newdir"), is_dir=True))
    assert queued == []


def test_a_path_outside_the_pair_is_refused(handler):
    """relpath() happily produces '../..'; that must not become a sync target."""
    h, _pair, queued = handler
    h.on_modified(Event("/etc/passwd"))
    assert queued == []


def test_moves_are_filtered_on_both_ends(handler):
    h, pair, queued = handler
    ev = Event(os.path.join(pair.local, ".git/a"))
    ev.dest_path = os.path.join(pair.local, ".git/b")
    h.on_moved(ev)
    assert queued == []


def test_deletes_are_filtered_too(handler):
    h, pair, queued = handler
    h.on_deleted(Event(os.path.join(pair.local, "__pycache__/x.pyc")))
    assert queued == []
