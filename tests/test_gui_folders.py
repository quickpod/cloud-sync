"""GUI tests for the 1.1.0 Synced-folders rework (Dropbox benchmark).

Needs a display (run under ``xvfb-run -a python3 -m pytest``); skipped
headless.  Hermetic via $CLOUDSYNC_HOME; rclone is reported unavailable so
the engine stays dormant and no subprocess ever runs.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloudsync import gui, guiconfig, rclone, syncengine  # noqa: E402
from cloudsync.syncengine import Pair  # noqa: E402

needs_display = pytest.mark.skipif(
    sys.platform == "win32" or not os.environ.get("DISPLAY"),
    reason="needs a display (run under xvfb-run)")


def _pump(a, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        a.update()
        time.sleep(0.02)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    home = tmp_path_factory.mktemp("cs-home")
    old = os.environ.get("CLOUDSYNC_HOME")
    os.environ["CLOUDSYNC_HOME"] = str(home)
    App = gui.build_app()
    a = App()
    _pump(a, 0.8)
    yield a
    try:
        a._stop_engine()
        a.destroy()
    except Exception:
        pass
    if old is None:
        os.environ.pop("CLOUDSYNC_HOME", None)
    else:
        os.environ["CLOUDSYNC_HOME"] = old


@needs_display
def test_folders_is_home_with_empty_state(app):
    assert app.active_section == "folders"
    assert not app._pairs_tree.get_children()
    # seven curated pills, Help and About included
    assert len(gui.VIEWS) == 7
    assert gui.VIEWS[0][0] == "folders"


@needs_display
def test_add_pair_shows_row_and_state(app, tmp_path):
    d = tmp_path / "synced"
    d.mkdir()
    guiconfig.add_pair(str(d), "work", "bucket")
    app._start_engine()          # no rclone -> engine built but dormant
    app._folders_refresh()
    _pump(app, 0.3)
    rows = app._pairs_tree.get_children()
    assert len(rows) == 1
    vals = app._pairs_tree.item(rows[0])["values"]
    assert str(d) in vals[0]
    assert "work:bucket" in vals[1]


@needs_display
def test_activity_feed_and_chip(app, tmp_path):
    pair = Pair(local=str(tmp_path), remote="work", rpath="bucket")
    app._engine_event({"kind": "file", "pair": pair, "rel": "a.txt",
                       "status": syncengine.SYNCING, "detail": "uploading"})
    app._engine_event({"kind": "overall", "status": "syncing",
                       "pending": 2, "errors": 0, "detail": ""})
    _pump(app, 0.2)
    rows = app._act_tree.get_children()
    assert rows
    vals = app._act_tree.item(rows[0])["values"]
    assert vals[1] == "a.txt"
    assert "Syncing" in app._chip.cget("text")
    app._engine_event({"kind": "file", "pair": pair, "rel": "a.txt",
                       "status": syncengine.ERROR, "detail": "boom"})
    app._engine_event({"kind": "overall", "status": "error",
                       "pending": 0, "errors": 1, "detail": ""})
    _pump(app, 0.2)
    assert "error" in app._chip.cget("text")


@needs_display
def test_pause_resume_button(app):
    # engine exists (dormant); pause toggles the persisted flag + button
    if app.engine is None:
        app._start_engine()
    app._toggle_pause()
    _pump(app, 0.2)
    assert guiconfig.get_paused() is True
    assert "Resume" in app._pause_btn.cget("text")
    app._toggle_pause()
    assert guiconfig.get_paused() is False


@needs_display
def test_settings_mode_roundtrip(app):
    guiconfig.set_sync_mode("scheduled")
    guiconfig.set_interval_minutes(15)
    app._folders_refresh()
    assert "Scheduled" in app._mode_cap.cget("text")
    guiconfig.set_sync_mode("realtime")
    app._folders_refresh()
    assert "Realtime" in app._mode_cap.cget("text")


@needs_display
def test_both_themes_no_crash(app):
    for theme in ("light", "dark"):
        app.set_theme(theme)
        app.update_idletasks()
        app.update()
        assert app.theme == theme
