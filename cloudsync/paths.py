r"""OS / filesystem detection and local-path resolution (A8 compliance).

Cloud Sync is filesystem-oriented: it syncs *local* folders to and from cloud
remotes, so it must always speak the host's own path dialect.  This module is
the single place that knows about the operating system.  It **auto-detects**
the platform via :data:`os.name` / :data:`sys.platform` and hands back the
correct locations with :mod:`pathlib` -- never a hard-coded Windows path:

* On **Windows** the local roots are the real drive letters (``C:\``, ``D:\``,
  ...) that currently exist, and config lives under ``%LOCALAPPDATA%``.
* On **Linux** the roots are ``/`` and ``$HOME`` plus mounted media under
  ``/mnt``, ``/media`` and ``/run/media/<user>``; config follows the XDG spec
  (``$XDG_CONFIG_HOME`` or ``~/.config``).
* On **macOS** the roots are ``/`` and ``$HOME`` plus volumes under
  ``/Volumes``; config lives under ``~/Library/Application Support``.

Everything is pure and safe to import on any platform, and every function is
defensive so a probe that raises on one host never aborts the caller.
"""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path
from typing import Dict, List

APP_DIRNAME = "CloudSync"
XDG_DIRNAME = "cloud-sync"
HOME_ENV = "CLOUDSYNC_HOME"      # test/override hook for the whole config tree


# --------------------------------------------------------------------------- #
# Platform predicates (the only place we branch on the OS)
# --------------------------------------------------------------------------- #
def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def platform_label() -> str:
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    if is_linux():
        return "Linux"
    return sys.platform or os.name


# --------------------------------------------------------------------------- #
# Home + config directories
# --------------------------------------------------------------------------- #
def home_dir() -> Path:
    """The current user's home directory as a :class:`~pathlib.Path`."""
    try:
        return Path(os.path.expanduser("~")).resolve(strict=False)
    except Exception:
        return Path(os.path.expanduser("~"))


def config_dir() -> Path:
    r"""Directory holding Cloud Sync's own config (created on demand by callers).

    Order of precedence:

    * ``$CLOUDSYNC_HOME`` (used by the test-suite to sandbox everything), then
    * ``%LOCALAPPDATA%\CloudSync`` on Windows,
    * ``~/Library/Application Support/CloudSync`` on macOS,
    * ``$XDG_CONFIG_HOME/cloud-sync`` or ``~/.config/cloud-sync`` elsewhere.
    """
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIRNAME
        return home_dir() / "AppData" / "Local" / APP_DIRNAME
    if is_macos():
        return home_dir() / "Library" / "Application Support" / APP_DIRNAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (home_dir() / ".config")
    return base / XDG_DIRNAME


def ensure_config_dir() -> Path:
    """Create (if needed) and return the config directory."""
    d = config_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def default_config_path() -> Path:
    """Path to Cloud Sync's dedicated ``rclone.conf`` (never the global one)."""
    return config_dir() / "rclone.conf"


# --------------------------------------------------------------------------- #
# Local mount points / drives the user can pick as a sync source or target
# --------------------------------------------------------------------------- #
def _push(seen: set, out: List[Dict[str, str]], path: Path, label: str) -> None:
    try:
        key = str(path)
    except Exception:
        return
    if key in seen:
        return
    seen.add(key)
    out.append({"path": key, "label": label})


def _windows_mounts(seen, out) -> None:
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            exists = root.exists()
        except Exception:
            exists = False
        if exists:
            _push(seen, out, root, f"{letter}: drive")


def _unix_media_dirs() -> List[Path]:
    dirs = [Path("/mnt"), Path("/media")]
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        dirs.append(Path("/run/media") / user)
        dirs.append(Path("/media") / user)
    if is_macos():
        dirs.append(Path("/Volumes"))
    return dirs


def _unix_mounts(seen, out) -> None:
    _push(seen, out, Path("/"), "Root (/)")
    for base in _unix_media_dirs():
        try:
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                try:
                    if child.is_dir():
                        _push(seen, out, child, f"{child.name} ({base}/)")
                except Exception:
                    continue
        except Exception:
            continue


def local_mounts() -> List[Dict[str, str]]:
    """Return the local roots a user can browse, newest-friendly first.

    The list always starts with Home and the OS-appropriate roots (drive
    letters on Windows; ``/`` plus mounted media on Linux/macOS).  Each entry
    is ``{"path": <str>, "label": <human label>}``.  Purely a *local*
    filesystem probe -- it connects to nothing.
    """
    seen: set = set()
    out: List[Dict[str, str]] = []
    _push(seen, out, home_dir(), "Home")
    if is_windows():
        _windows_mounts(seen, out)
    else:
        _unix_mounts(seen, out)
    return out


def default_local_dir() -> Path:
    """Sensible default local folder for a new sync (the user's home)."""
    return home_dir()


def normalize_local(path: str) -> str:
    """Expand ``~``/env vars and absolutize a user-typed local path.

    Uses :mod:`pathlib`/:mod:`os.path` so it is correct on every OS; it never
    assumes a separator or a drive layout.
    """
    if not path:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(str(path)))
    try:
        return str(Path(expanded))
    except Exception:
        return expanded


def local_dir_exists(path: str) -> bool:
    try:
        return Path(normalize_local(path)).is_dir()
    except Exception:
        return False


__all__ = [
    "APP_DIRNAME",
    "HOME_ENV",
    "is_windows",
    "is_macos",
    "is_linux",
    "platform_label",
    "home_dir",
    "config_dir",
    "ensure_config_dir",
    "default_config_path",
    "local_mounts",
    "default_local_dir",
    "normalize_local",
    "local_dir_exists",
]
