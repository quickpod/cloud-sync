r"""Tray indicator + login-autostart.

Neither needs a real tray, a desktop session or a network: the icon renderer
is pure, and autostart is redirected into a temp HOME by the fixtures.
"""

from __future__ import annotations

import os

import pytest

from cloudsync import autostart, guiconfig, tray


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    """Point config *and* the XDG autostart dir at a throwaway directory."""
    monkeypatch.setenv("CLOUDSYNC_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(autostart.paths, "home_dir", lambda: tmp_path)
    yield tmp_path


# --------------------------------------------------------------------------- #
# Icon rendering
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not tray.available(), reason="pystray/Pillow not installed")
@pytest.mark.parametrize("state", sorted(tray.STATE_STYLE))
def test_every_state_renders_a_badged_icon(state):
    img = tray.render_icon(state, size=64)
    assert img is not None and img.size == (64, 64)
    # The badge must actually paint something in the lower-right corner.
    corner = img.crop((40, 40, 64, 64)).getcolors(maxcolors=1 << 16)
    assert corner, "badge corner is empty"


#: "syncing" and "pending" deliberately share one "busy" look -- the user
#: does not need to tell queued-but-not-started from in-flight, and OneDrive
#: makes the same call. Everything else must be tellable apart at a glance.
DISTINCT_STATES = ("synced", "syncing", "paused", "error", "offline")


@pytest.mark.skipif(not tray.available(), reason="pystray/Pillow not installed")
def test_meaningfully_different_states_look_different():
    seen = {st: tray.render_icon(st, size=32).tobytes()
            for st in DISTINCT_STATES}
    assert len(set(seen.values())) == len(seen)


@pytest.mark.skipif(not tray.available(), reason="pystray/Pillow not installed")
def test_pending_shares_the_busy_look_with_syncing():
    assert (tray.render_icon("pending", size=32).tobytes()
            == tray.render_icon("syncing", size=32).tobytes())


@pytest.mark.skipif(not tray.available(), reason="pystray/Pillow not installed")
def test_unknown_state_falls_back_instead_of_raising():
    assert tray.render_icon("no-such-state", size=32) is not None


def test_unavailable_reason_is_empty_when_available():
    assert bool(tray.unavailable_reason()) is not tray.available()


# --------------------------------------------------------------------------- #
# Tray icon object (no real tray host involved)
# --------------------------------------------------------------------------- #
def test_tray_is_inert_before_start():
    icon = tray.TrayIcon()
    assert icon.running is False
    icon.update("syncing", "2 files")     # must not raise without a host
    icon.stop()                            # must not raise either
    assert icon.state == "syncing"


def test_tray_callbacks_are_contained():
    """A raising menu handler must not escape onto the tray thread."""
    def boom():
        raise RuntimeError("handler blew up")
    handler = tray.TrayIcon._wrap(boom)
    handler(None, None)   # no exception propagates


# --------------------------------------------------------------------------- #
# Autostart
# --------------------------------------------------------------------------- #
def test_autostart_enable_disable_roundtrip():
    assert autostart.is_enabled() is False
    assert autostart.set_enabled(True) is True
    assert autostart.is_enabled() is True
    # enabling twice must not create a second entry or fail
    assert autostart.set_enabled(True) is True
    assert autostart.set_enabled(False) is True
    assert autostart.is_enabled() is False


def test_autostart_disable_is_safe_when_absent():
    assert autostart.is_enabled() is False
    assert autostart.disable() is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX autostart file layout")
def test_autostart_entry_launches_minimized(temp_home):
    autostart.enable()
    entry = temp_home / "config" / "autostart" / "quickopen-cloud-sync.desktop"
    body = entry.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in body
    # Without this the login would pop a window instead of going to the tray.
    assert autostart.MINIMIZED_FLAG in body
    assert "X-GNOME-Autostart-enabled=true" in body


def test_launch_command_prefers_the_packaged_launcher(monkeypatch):
    monkeypatch.setenv(autostart.LAUNCHER_ENV, "/usr/bin/quickopen-cloud-sync")
    cmd = autostart.launch_command()
    assert cmd.startswith("/usr/bin/quickopen-cloud-sync")
    assert cmd.endswith(autostart.MINIMIZED_FLAG)


def test_launch_command_quotes_spaced_paths(monkeypatch):
    monkeypatch.setenv(autostart.LAUNCHER_ENV, "/opt/My Apps/cloud sync")
    assert "'/opt/My Apps/cloud sync'" in autostart.launch_command()


# --------------------------------------------------------------------------- #
# Settings persistence
# --------------------------------------------------------------------------- #
def test_background_settings_persist_and_default_sensibly():
    # Closing to the tray is the default: the whole point is not to stop
    # syncing when the window closes.
    assert guiconfig.get_close_to_tray() is True
    assert guiconfig.get_start_minimized() is False
    assert guiconfig.get_autostart() is False

    guiconfig.set_close_to_tray(False)
    guiconfig.set_start_minimized(True)
    guiconfig.set_autostart(True)
    assert guiconfig.get_close_to_tray() is False
    assert guiconfig.get_start_minimized() is True
    assert guiconfig.get_autostart() is True


def test_background_settings_survive_a_corrupt_config():
    with open(guiconfig.config_path(), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    assert guiconfig.get_close_to_tray() is True
