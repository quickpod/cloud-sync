r"""Start Cloud Sync automatically when the user logs in.

Registering autostart is per-user and needs no privileges on any of the three
platforms, which matters because the app is installed system-wide (from the
deb on Linux) but must not start for users who never asked for it:

* **Linux** -- an XDG ``.desktop`` file in ``$XDG_CONFIG_HOME/autostart``.
  Honoured by KDE, GNOME, XFCE and every other freedesktop-compliant session.
* **Windows** -- a ``Run`` value under ``HKEY_CURRENT_USER``.
* **macOS** -- a LaunchAgent plist in ``~/Library/LaunchAgents``.

The entry always launches with ``--minimized`` so a login lands in the tray
rather than throwing a window in the user's face.

Every function is best-effort and returns a bool rather than raising: failing
to register autostart must never stop the app from running.  :func:`is_enabled`
reports what is *actually* on disk, so a user who removes the entry by hand
(or with their desktop's own startup-apps tool) is not overridden by a stale
preference in our config.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from . import paths

APP_NAME = "Cloud Sync"
ENTRY_NAME = "quickopen-cloud-sync"
#: Set by the packaged launcher so autostart records the supported entry point
#: rather than a bare interpreter invocation.
LAUNCHER_ENV = "CLOUDSYNC_LAUNCHER"
MINIMIZED_FLAG = "--minimized"


def launch_command() -> str:
    r"""The command line that should run at login, as a shell-ready string.

    Prefers an explicit launcher (``$CLOUDSYNC_LAUNCHER``, or the
    distro-installed ``quickopen-cloud-sync`` shim) because that is the
    entry point the package supports and it survives Python upgrades.  Falls
    back to re-running the current interpreter with the app's entry script.
    """
    launcher = os.environ.get(LAUNCHER_ENV, "").strip()
    if not launcher:
        for candidate in ("/usr/bin/" + ENTRY_NAME, "/usr/local/bin/" + ENTRY_NAME):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                launcher = candidate
                break
    if launcher:
        return f"{shlex.quote(launcher)} {MINIMIZED_FLAG}"
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if script and os.path.isfile(script):
        return (f"{shlex.quote(sys.executable)} {shlex.quote(script)} "
                f"{MINIMIZED_FLAG}")
    return f"{shlex.quote(sys.executable)} -m cloudsync {MINIMIZED_FLAG}"


# --------------------------------------------------------------------------- #
# Linux -- XDG autostart
# --------------------------------------------------------------------------- #
def _xdg_autostart_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (paths.home_dir() / ".config")
    return root / "autostart" / (ENTRY_NAME + ".desktop")


def _xdg_desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Keep folders in sync with your cloud, in the background\n"
        f"Exec={launch_command()}\n"
        f"Icon={ENTRY_NAME}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        # Give the session's tray time to appear, or the icon can be dropped.
        "X-GNOME-Autostart-Delay=10\n"
        "X-KDE-autostart-after=panel\n"
    )


# --------------------------------------------------------------------------- #
# Windows -- HKCU\...\Run
# --------------------------------------------------------------------------- #
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_registry():
    import winreg  # noqa: F401  (Windows only)
    return winreg


# --------------------------------------------------------------------------- #
# macOS -- LaunchAgent
# --------------------------------------------------------------------------- #
def _launch_agent_path() -> Path:
    return (paths.home_dir() / "Library" / "LaunchAgents"
            / f"io.quickopen.{ENTRY_NAME}.plist")


def _launch_agent_plist() -> str:
    args = shlex.split(launch_command())
    items = "\n".join(f"        <string>{a}</string>" for a in args)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            '    <key>Label</key>\n'
            f'    <string>io.quickopen.{ENTRY_NAME}</string>\n'
            '    <key>ProgramArguments</key>\n'
            '    <array>\n'
            f'{items}\n'
            '    </array>\n'
            '    <key>RunAtLoad</key>\n'
            '    <true/>\n'
            '</dict>\n'
            '</plist>\n')


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_supported() -> bool:
    """True when autostart can be registered on this platform."""
    return paths.is_windows() or paths.is_macos() or paths.is_linux()


def is_enabled() -> bool:
    """Whether an autostart entry currently exists on disk (not a preference)."""
    try:
        if paths.is_windows():
            winreg = _win_registry()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, APP_NAME)
                return bool(value)
        if paths.is_macos():
            return _launch_agent_path().is_file()
        return _xdg_autostart_path().is_file()
    except Exception:
        return False


def enable() -> bool:
    """Register the login entry.  Returns True on success."""
    try:
        if paths.is_windows():
            winreg = _win_registry()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  launch_command())
            return True
        target = _launch_agent_path() if paths.is_macos() else _xdg_autostart_path()
        body = _launch_agent_plist() if paths.is_macos() else _xdg_desktop_entry()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(str(tmp), str(target))
        if not paths.is_macos():
            os.chmod(str(target), 0o644)
        return True
    except Exception:
        return False


def disable() -> bool:
    """Remove the login entry.  Returns True when none remains."""
    try:
        if paths.is_windows():
            winreg = _win_registry()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            return True
        target = _launch_agent_path() if paths.is_macos() else _xdg_autostart_path()
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return True
    except Exception:
        return False


def set_enabled(flag: bool) -> bool:
    """Enable or disable autostart; returns whether the state now matches."""
    ok = enable() if flag else disable()
    return ok and (is_enabled() == bool(flag))
