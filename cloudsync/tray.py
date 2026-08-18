r"""System-tray indicator: status at a glance plus the controls worth reaching
without opening the window.

Cloud Sync keeps syncing while its window is hidden, so something has to show
that it is alive and let the user act on it -- that is this module.  It wraps
``pystray`` behind a small surface (:class:`TrayIcon`) so the GUI never
imports it directly and, crucially, so the app still runs when a tray is not
available: a headless box, a session with no status-notifier host, or a build
without the optional dependency.  :func:`available` reports that up front and
every method is a no-op when the icon could not start.

The icon runs pystray on its own thread.  Menu callbacks therefore fire *off*
the Tk thread, so every handler is handed back to the GUI through the
``on_*`` callables the caller supplies -- the GUI is responsible for
marshalling those onto its own loop.  Nothing here touches Tk.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Dict, Optional

try:  # optional dependency -- the app must run without a tray
    import pystray
    from PIL import Image, ImageDraw
    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the host
    pystray = None
    Image = ImageDraw = None
    _IMPORT_ERROR = str(exc)


ICON_SIZE = 64
ICON_FILE = "cloud-sync.png"
#: A busy sync flips between "syncing" and "synced" many times a second. Below
#: this interval the icon is left alone: a blinking tray entry reads as a fault
#: and is hard to aim at.
MIN_ICON_INTERVAL = 2.0

#: Overall states the engine reports, mapped to what the tray should show.
#: Colours read against both light and dark panels.
STATE_STYLE: Dict[str, Dict[str, str]] = {
    "synced":  {"label": "Up to date",   "colour": "#2e7d32"},
    "syncing": {"label": "Syncing…",     "colour": "#1565c0"},
    "pending": {"label": "Changes queued", "colour": "#1565c0"},
    "paused":  {"label": "Paused",       "colour": "#8d8d8d"},
    "error":   {"label": "Attention needed", "colour": "#c62828"},
    "offline": {"label": "Not configured", "colour": "#8d8d8d"},
}
DEFAULT_STATE = "synced"


def available() -> bool:
    """True when a tray icon can be created on this system."""
    return pystray is not None and Image is not None


def unavailable_reason() -> str:
    """Why :func:`available` is False (empty when a tray can be created)."""
    if available():
        return ""
    return _IMPORT_ERROR or "pystray is not installed"


def _state_style(state: str) -> Dict[str, str]:
    return STATE_STYLE.get(state, STATE_STYLE[DEFAULT_STATE])


def app_icon_path() -> Optional[str]:
    """Locate the app's own PNG, from source or a frozen build."""
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, ICON_FILE)
        if os.path.exists(candidate):
            return candidate
    return None


def _load_app_icon(size: int):
    """The app icon at *size*, or None when it cannot be read."""
    path = app_icon_path()
    if not path or Image is None:
        return None
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        return None
    try:
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return img.resize((size, size))


def _draw_badge(draw, state: str, size: int):
    """Stamp the status dot over the icon's lower-right corner."""
    style = _state_style(state)
    d = max(10, int(size * 0.46))          # badge diameter
    x1, y1 = size - d, size - d
    x2, y2 = size - 1, size - 1
    ring = max(2, d // 10)
    # A light ring keeps the badge readable against the icon underneath and
    # against any panel colour.
    draw.ellipse([x1 - ring, y1 - ring, x2 + ring, y2 + ring],
                 fill=(255, 255, 255, 235))
    draw.ellipse([x1, y1, x2, y2], fill=style["colour"])

    white = (255, 255, 255, 255)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    if state == "error":
        bar = max(2, d // 8)
        draw.rounded_rectangle([cx - bar / 2, y1 + d * 0.20,
                                cx + bar / 2, y1 + d * 0.58],
                               radius=bar / 2, fill=white)
        draw.ellipse([cx - bar / 2, y1 + d * 0.68,
                      cx + bar / 2, y1 + d * 0.68 + bar], fill=white)
    elif state == "paused":
        bar = max(2, d // 7)
        gap = max(1, bar)
        for dx in (-gap - bar / 2, gap - bar / 2):
            draw.rounded_rectangle([cx + dx, y1 + d * 0.26,
                                    cx + dx + bar, y2 - d * 0.26],
                                   radius=bar / 3, fill=white)
    elif state in ("syncing", "pending"):
        # Two chevrons: work is in flight.
        w = max(2, d // 9)
        draw.line([(cx - d * 0.22, cy - d * 0.04), (cx - d * 0.02, cy - d * 0.24),
                   (cx - d * 0.02, cy + d * 0.24)], fill=white, width=w,
                  joint="curve")
        draw.line([(cx + d * 0.06, cy - d * 0.24), (cx + d * 0.24, cy - d * 0.04),
                   (cx + d * 0.06, cy + d * 0.24)], fill=white, width=w,
                  joint="curve")
    elif state == "offline":
        w = max(2, d // 8)
        draw.line([(cx - d * 0.18, cy), (cx + d * 0.18, cy)], fill=white, width=w)
    else:  # synced
        w = max(2, d // 8)
        draw.line([(cx - d * 0.22, cy + d * 0.02),
                   (cx - d * 0.05, cy + d * 0.19),
                   (cx + d * 0.23, cy - d * 0.18)],
                  fill=white, width=w, joint="curve")


def render_icon(state: str, size: int = ICON_SIZE):
    """The app's own icon with a status badge in the corner.

    Keeping the familiar artwork means the tray entry still reads as Cloud
    Sync at a glance; the badge carries the state.  When the artwork cannot be
    loaded (a trimmed build, unreadable file) this falls back to a plain disc
    in the state colour so the indicator still appears and still communicates.
    """
    if Image is None:  # pragma: no cover - no Pillow
        return None
    style = _state_style(state)
    base = _load_app_icon(size)
    if base is None:
        base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        fallback = ImageDraw.Draw(base)
        pad = max(2, size // 16)
        fallback.ellipse([pad, pad, size - pad, size - pad],
                         fill=style["colour"])
    draw = ImageDraw.Draw(base)
    _draw_badge(draw, state, size)
    return base


class TrayIcon:
    """A tray indicator driven by :meth:`update`.

    All callbacks are optional; any that is None simply omits its menu entry,
    so the GUI can expose only what makes sense for the current state.
    """

    def __init__(self, *, on_open: Optional[Callable[[], None]] = None,
                 on_sync_now: Optional[Callable[[], None]] = None,
                 on_toggle_pause: Optional[Callable[[], None]] = None,
                 on_settings: Optional[Callable[[], None]] = None,
                 on_open_folder: Optional[Callable[[], None]] = None,
                 on_quit: Optional[Callable[[], None]] = None,
                 title: str = "Cloud Sync"):
        self._on_open = on_open
        self._on_sync_now = on_sync_now
        self._on_toggle_pause = on_toggle_pause
        self._on_settings = on_settings
        self._on_open_folder = on_open_folder
        self._on_quit = on_quit
        self._title = title
        self._state = DEFAULT_STATE
        self._detail = ""
        self._paused = False
        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_icon_at = 0.0
        self._last_icon_state = None
        self._settle_job = None

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> bool:
        """Create and run the icon on a background thread.  False if it can't."""
        if not available() or self._icon is not None:
            return False
        try:
            self._icon = pystray.Icon(
                "quickopen-cloud-sync", render_icon(self._state),
                self._tooltip(), menu=self._build_menu())
        except Exception:
            self._icon = None
            return False
        self._thread = threading.Thread(target=self._run, name="cloudsync-tray",
                                        daemon=True)
        self._thread.start()
        return True

    def _run(self):  # pragma: no cover - needs a real tray host
        try:
            self._icon.run()
        except Exception:
            # A session that advertises a tray but refuses to host one must
            # not take the app down with it.
            pass

    def stop(self):
        """Remove the icon.  Safe to call when it never started."""
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._icon is not None

    # -- state ------------------------------------------------------------ #
    def update(self, state: str, detail: str = "", *, paused: bool = False):
        """Set the indicator's state, tooltip detail and pause flag.

        Only what actually changed is pushed to the tray. Reassigning the icon
        image on every sync event makes it flicker, and rebuilding the *menu*
        that often tears it down under the pointer -- the menu becomes
        impossible to click while a sync is running, which is exactly when a
        user reaches for it.
        """
        with self._lock:
            state = state if state in STATE_STYLE else DEFAULT_STATE
            paused = bool(paused)
            detail = detail or ""
            icon_changed = (state != self._state) or (paused != self._paused)
            # The menu's text depends only on the pause flag.
            menu_changed = paused != self._paused
            tip_changed = (icon_changed or detail != self._detail)
            self._state, self._detail, self._paused = state, detail, paused
        icon = self._icon
        if icon is None:
            return
        try:
            if tip_changed:
                icon.title = self._tooltip()      # cheap, never flickers
            if menu_changed:
                icon.menu = self._build_menu()
                icon.update_menu()
            if icon_changed:
                self._set_icon_image()
        except Exception:
            pass

    def _set_icon_image(self) -> None:
        """Repaint the tray image, rate-limited so it cannot strobe.

        A held-back change is not dropped: a timer applies the final state
        once things settle, so the icon always ends up telling the truth.
        """
        icon = self._icon
        if icon is None:
            return
        now = time.monotonic()
        with self._lock:
            target = (self._state, self._paused)
            if target == self._last_icon_state:
                return
            due = now - self._last_icon_at >= MIN_ICON_INTERVAL
        if due:
            with self._lock:
                self._last_icon_at = now
                self._last_icon_state = target
            try:
                icon.icon = render_icon(self._state)
            except Exception:
                pass
            return
        # Too soon: schedule one catch-up rather than a burst of repaints.
        with self._lock:
            if self._settle_job is not None:
                return
            delay = MIN_ICON_INTERVAL - (now - self._last_icon_at)
            timer = threading.Timer(max(0.1, delay), self._settle)
            timer.daemon = True
            self._settle_job = timer
        timer.start()

    def _settle(self) -> None:
        with self._lock:
            self._settle_job = None
        self._set_icon_image()

    @property
    def state(self) -> str:
        return self._state

    def _tooltip(self) -> str:
        label = "Paused" if self._paused else _state_style(self._state)["label"]
        return f"{self._title} — {label}" + (f"\n{self._detail}" if self._detail else "")

    # -- menu ------------------------------------------------------------- #
    def _build_menu(self):
        if pystray is None:  # pragma: no cover
            return None
        items = []
        if self._on_open is not None:
            items.append(pystray.MenuItem("Open Cloud Sync",
                                          self._wrap(self._on_open),
                                          default=True))
        if self._on_sync_now is not None:
            items.append(pystray.MenuItem("Sync now",
                                          self._wrap(self._on_sync_now),
                                          enabled=not self._paused))
        if self._on_toggle_pause is not None:
            items.append(pystray.MenuItem(
                "Resume syncing" if self._paused else "Pause syncing",
                self._wrap(self._on_toggle_pause)))
        if self._on_open_folder is not None:
            items.append(pystray.MenuItem("Open synced folder",
                                          self._wrap(self._on_open_folder)))
        if items:
            items.append(pystray.Menu.SEPARATOR)
        # Settings reachable from the icon: the app spends most of its life
        # with no window, so the tray has to be a real entry point rather
        # than a status light.
        if self._on_settings is not None:
            items.append(pystray.MenuItem("Settings…",
                                          self._wrap(self._on_settings)))
            items.append(pystray.Menu.SEPARATOR)
        if self._on_quit is not None:
            items.append(pystray.MenuItem("Quit", self._wrap(self._on_quit)))
        return pystray.Menu(*items) if items else None

    @staticmethod
    def _wrap(callback: Callable[[], None]):
        """Adapt a zero-arg callback to pystray's (icon, item) signature.

        Menu handlers run on the tray thread; an exception there would kill
        the icon and leave the app running invisibly, so they are contained.
        """
        def handler(icon=None, item=None):  # noqa: ARG001 - pystray signature
            try:
                callback()
            except Exception:
                pass
        return handler
