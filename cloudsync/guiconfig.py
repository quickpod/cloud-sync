r"""Tiny JSON-backed config for the Cloud Sync GUI.

Stores the chosen theme ("system"/"light"/"dark" — "system" follows the OS
Aura appearance live and is the fresh-install default), a short list of
recently used local folders, the synced folder pairs and the sync-mode
settings (realtime is the default; scheduled runs every N minutes or daily at
HH:MM).  It lives next to the rclone config, in the directory chosen by
:mod:`cloudsync.paths` (``%LOCALAPPDATA%\CloudSync`` on Windows,
``~/Library/Application Support/CloudSync`` on macOS, and
``$XDG_CONFIG_HOME/cloud-sync`` elsewhere) -- honouring ``$CLOUDSYNC_HOME``
for tests.  Every function is defensive: a corrupt or unreadable config must
never stop the app from starting, and this file never contains cloud
credentials.
"""

from __future__ import annotations

import json
import os

from . import paths

CONFIG_NAME = "gui.json"
MAX_RECENT = 10
# "system" follows the OS Aura Dark/Light live (the fresh-install default).
VALID_THEMES = ("system", "light", "dark")
VALID_MODES = ("realtime", "scheduled")
DEFAULT_INTERVAL_MINUTES = 30


def config_path():
    return os.path.join(str(paths.config_dir()), CONFIG_NAME)


def _defaults():
    return {"theme": "system", "recent": [], "pairs": [],
            "sync_mode": "realtime",
            "interval_minutes": DEFAULT_INTERVAL_MINUTES,
            "daily_at": "", "paused": False,
            "close_to_tray": True, "start_minimized": False,
            "autostart": False}


def _clean_pairs(value):
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("local") or "").strip() \
                    and str(item.get("remote") or "").strip():
                out.append({"local": str(item["local"]),
                            "remote": str(item["remote"]),
                            "rpath": str(item.get("rpath") or "")})
    return out


def load():
    """Return the config dict, always with all known keys populated."""
    cfg = _defaults()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            if data.get("theme") in VALID_THEMES:
                cfg["theme"] = data["theme"]
            recent = data.get("recent")
            if isinstance(recent, list):
                cfg["recent"] = [p for p in recent if isinstance(p, str)][:MAX_RECENT]
            cfg["pairs"] = _clean_pairs(data.get("pairs"))
            if data.get("sync_mode") in VALID_MODES:
                cfg["sync_mode"] = data["sync_mode"]
            try:
                n = int(data.get("interval_minutes"))
                if 1 <= n <= 24 * 60:
                    cfg["interval_minutes"] = n
            except Exception:
                pass
            if isinstance(data.get("daily_at"), str):
                cfg["daily_at"] = data["daily_at"]
            cfg["paused"] = bool(data.get("paused", False))
            for key in ("close_to_tray", "start_minimized", "autostart"):
                if key in data:
                    cfg[key] = bool(data.get(key))
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    """Persist *cfg* (best-effort; failures are swallowed)."""
    try:
        paths.ensure_config_dir()
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "system",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
            "pairs": _clean_pairs(cfg.get("pairs")),
            "sync_mode": cfg.get("sync_mode") if cfg.get("sync_mode") in VALID_MODES else "realtime",
            "interval_minutes": cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES),
            "daily_at": cfg.get("daily_at", "") if isinstance(cfg.get("daily_at"), str) else "",
            "paused": bool(cfg.get("paused", False)),
            "close_to_tray": bool(cfg.get("close_to_tray", True)),
            "start_minimized": bool(cfg.get("start_minimized", False)),
            "autostart": bool(cfg.get("autostart", False)),
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    """The persisted theme preference: "system" (follow the OS), "light" or
    "dark".  Fresh installs return "system" so the app follows Aura live."""
    return load().get("theme", "system")


def set_theme(theme):
    if theme not in VALID_THEMES:
        return
    cfg = load()
    cfg["theme"] = theme
    save(cfg)


# ---- synced folder pairs ---------------------------------------------------
def get_pairs():
    return load().get("pairs", [])


def set_pairs(pairs):
    cfg = load()
    cfg["pairs"] = pairs
    save(cfg)


def add_pair(local, remote, rpath=""):
    cfg = load()
    pairs = cfg.get("pairs", [])
    entry = {"local": str(local), "remote": str(remote),
             "rpath": str(rpath or "")}
    if entry not in pairs:
        pairs.append(entry)
    cfg["pairs"] = pairs
    save(cfg)


def remove_pair(local, remote, rpath=""):
    cfg = load()
    entry = {"local": str(local), "remote": str(remote),
             "rpath": str(rpath or "")}
    cfg["pairs"] = [p for p in cfg.get("pairs", []) if p != entry]
    save(cfg)


# ---- sync mode -------------------------------------------------------------
def get_sync_mode():
    return load().get("sync_mode", "realtime")


def set_sync_mode(mode):
    if mode not in VALID_MODES:
        return
    cfg = load()
    cfg["sync_mode"] = mode
    save(cfg)


def get_interval_minutes():
    return load().get("interval_minutes", DEFAULT_INTERVAL_MINUTES)


def set_interval_minutes(minutes):
    cfg = load()
    try:
        n = int(minutes)
    except Exception:
        return
    cfg["interval_minutes"] = min(max(n, 1), 24 * 60)
    save(cfg)


def get_daily_at():
    return load().get("daily_at", "")


def set_daily_at(text):
    cfg = load()
    cfg["daily_at"] = str(text or "")
    save(cfg)


def get_paused():
    return load().get("paused", False)


def set_paused(flag):
    cfg = load()
    cfg["paused"] = bool(flag)
    save(cfg)


def get_recent():
    return load().get("recent", [])


def add_recent(path):
    """Push *path* to the front of the recent local-folder list."""
    if not path:
        return
    try:
        ap = os.path.abspath(path)
    except Exception:
        ap = path
    cfg = load()
    recent = [p for p in cfg.get("recent", []) if _abs(p) != ap]
    recent.insert(0, ap)
    cfg["recent"] = recent[:MAX_RECENT]
    save(cfg)


def _abs(p):
    try:
        return os.path.abspath(p)
    except Exception:
        return p


def get_close_to_tray():
    """True when closing the window should hide to the tray and keep syncing."""
    return bool(load().get("close_to_tray", True))


def set_close_to_tray(flag):
    cfg = load()
    cfg["close_to_tray"] = bool(flag)
    save(cfg)


def get_start_minimized():
    """True when the app should start hidden in the tray (used at login)."""
    return bool(load().get("start_minimized", False))


def set_start_minimized(flag):
    cfg = load()
    cfg["start_minimized"] = bool(flag)
    save(cfg)


def get_autostart():
    """The user's *preference*; :mod:`cloudsync.autostart` owns the real state."""
    return bool(load().get("autostart", False))


def set_autostart(flag):
    cfg = load()
    cfg["autostart"] = bool(flag)
    save(cfg)
