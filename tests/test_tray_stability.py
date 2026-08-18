r"""The tray must stay usable while a sync is running.

Rebuilding the menu on every sync event tore it down under the pointer, so it
could not be clicked at exactly the moment a user reaches for it; repainting
the icon that often made it strobe.
"""

from __future__ import annotations

import time

import pytest

from cloudsync import tray

pytestmark = pytest.mark.skipif(not tray.available(),
                                reason="pystray/Pillow not installed")


class FakeIcon:
    """Counts what actually reaches the tray."""

    def __init__(self):
        self.paints = 0
        self.menus = 0
        self._icon = None
        self._menu = None
        self.title = ""

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self._icon = value
        self.paints += 1

    @property
    def menu(self):
        return self._menu

    @menu.setter
    def menu(self, value):
        self._menu = value
        self.menus += 1

    def update_menu(self):
        pass


@pytest.fixture
def icon():
    t = tray.TrayIcon(on_open=lambda: None, on_quit=lambda: None)
    fake = FakeIcon()
    t._icon = fake
    return t, fake


def test_a_busy_sync_does_not_strobe_the_icon(icon):
    t, fake = icon
    for i in range(200):
        t.update("syncing" if i % 2 else "synced", f"file {i}")
    assert fake.paints <= 3, f"icon repainted {fake.paints} times"


def test_the_menu_is_not_rebuilt_by_sync_activity(icon):
    """This is what made it unclickable: the menu vanished mid-click."""
    t, fake = icon
    for i in range(100):
        t.update("syncing", f"file {i}")
    assert fake.menus == 0


def test_pausing_does_rebuild_the_menu(icon):
    """The menu text depends on the pause flag, so that one must refresh."""
    t, fake = icon
    t.update("synced", "", paused=True)
    assert fake.menus == 1


def test_tooltip_still_tracks_every_change(icon):
    """Detail is cheap and never flickers, so it should stay live."""
    t, fake = icon
    t.update("syncing", "one")
    first = fake.title
    t.update("syncing", "two")
    assert fake.title != first


def test_a_held_back_change_still_lands(icon):
    """Rate limiting must not lose the final state."""
    t, fake = icon
    t.update("syncing", "busy")
    t.update("error", "failed")          # too soon to repaint
    assert t.state == "error"
    time.sleep(tray.MIN_ICON_INTERVAL + 0.4)
    assert fake.paints >= 2, "the catch-up repaint never happened"


def test_repeating_the_same_state_changes_nothing(icon):
    t, fake = icon
    t.update("synced", "x")
    before = fake.paints
    for _ in range(50):
        t.update("synced", "x")
    assert fake.paints == before
