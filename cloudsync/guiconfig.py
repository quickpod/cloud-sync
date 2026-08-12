r"""Tiny JSON-backed config for the Cloud Sync GUI.

Stores just the chosen theme ("light"/"dark") and a short list of recently used
local folders (the sync pickers).  It lives next to the rclone config, in the
directory chosen by :mod:`cloudsync.paths` (``%LOCALAPPDATA%\CloudSync`` on
Windows, ``~/Library/Application Support/CloudSync`` on macOS, and
``$XDG_CONFIG_HOME/cloud-sync`` elsewhere) -- honouring ``$CLOUDSYNC_HOME`` for
tests.  Every function is defensive: a corrupt or unreadable config must never
stop the app from starting, and this file never contains cloud credentials.
"""

from __future__ import annotations

import json
import os

from . import paths

CONFIG_NAME = "gui.json"
MAX_RECENT = 10
VALID_THEMES = ("light", "dark")


def config_path():
    return os.path.join(str(paths.config_dir()), CONFIG_NAME)


def _defaults():
    return {"theme": "dark", "recent": []}


def load():
    """Return the config dict, always with ``theme`` and ``recent`` keys."""
    cfg = _defaults()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in VALID_THEMES:
                cfg["theme"] = theme
            recent = data.get("recent")
            if isinstance(recent, list):
                cfg["recent"] = [p for p in recent if isinstance(p, str)][:MAX_RECENT]
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    """Persist *cfg* (best-effort; failures are swallowed)."""
    try:
        paths.ensure_config_dir()
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "dark",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    return load().get("theme", "dark")


def set_theme(theme):
    if theme not in VALID_THEMES:
        return
    cfg = load()
    cfg["theme"] = theme
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
