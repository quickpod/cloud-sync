"""Quit from the tray must not depend on the Tk loop being responsive.

Quit is the one menu item that has to work when the app is busy or wedged --
that is precisely when a user reaches for it. So the watchdog that guarantees
the process exits has to be armed BEFORE anything touches Tk: a tkinter call
from the tray thread blocks on the Tcl interpreter lock for as long as the Tk
loop is busy, so arming it afterwards means a wedged loop prevents the very
watchdog meant to rescue it from ever being scheduled.

Needs a display (run under ``xvfb-run -a python3 -m pytest``); skipped
headless.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloudsync import gui  # noqa: E402

needs_display = pytest.mark.skipif(
    sys.platform == "win32" or not os.environ.get("DISPLAY"),
    reason="needs a display (run under xvfb-run)")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSYNC_HOME", str(tmp_path))
    App = gui.build_app()
    a = App()
    for _ in range(20):
        a.update()
        time.sleep(0.02)
    yield a
    try:
        a._stop_engine()
        a.destroy()
    except Exception:
        pass


@needs_display
def test_the_watchdog_is_armed_before_any_tk_call(app, monkeypatch):
    """The ordering that makes Quit survive a wedged Tk loop."""
    seen = {}

    def spy_after(delay, func=None, *a):
        # Whatever Tk state this call would block on, the watchdog must
        # already be running by the time we get here.
        seen["timer_at_after"] = getattr(app, "_quit_timer", None)
        return "after#0"

    monkeypatch.setattr(app, "after", spy_after)
    fired = []
    monkeypatch.setattr(gui.threading, "Timer",
                        lambda *a, **kw: _RecordingTimer(fired, *a, **kw))

    app._tray_quit()

    timer = seen.get("timer_at_after")
    assert timer is not None, "Tk was touched before the watchdog was armed"
    assert timer.started, "watchdog was created but not started before the Tk call"


@needs_display
def test_a_normal_quit_cancels_the_watchdog(app, monkeypatch):
    """An orderly teardown must not be turned into a hard exit."""
    monkeypatch.setattr(app, "_on_close", lambda: None)
    fired = []
    monkeypatch.setattr(gui.threading, "Timer",
                        lambda *a, **kw: _RecordingTimer(fired, *a, **kw))
    monkeypatch.setattr(app, "after", lambda *a, **kw: "after#0")

    app._tray_quit()
    app._quit_from_tray()

    assert app._quit_timer.cancelled, "watchdog left running after a clean quit"


class _RecordingTimer:
    """Stands in for threading.Timer so nothing can really _exit the run."""

    def __init__(self, fired, interval, function, *a, **kw):
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        self.daemon = True
        self._fired = fired

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return self.started and not self.cancelled
