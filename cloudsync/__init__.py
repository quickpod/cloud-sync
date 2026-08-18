"""cloudsync -- a permissively-licensed S3 / major-cloud file-sync library.

A friendly, OneDrive-style front-end over the open-source `rclone` engine.  The
user configures every remote themselves (endpoint, bucket, access keys); the
library connects to nothing on its own -- the only outbound calls happen when
the user explicitly tests a remote, browses it, or runs a sync.

Public API, grouped by area:

* **Providers** -- :data:`PROVIDERS`, :func:`get_provider` (the S3-compatible
  catalogue behind the add-remote form).
* **Remotes (offline config)** -- :func:`list_remotes`, :func:`add_remote`,
  :func:`delete_remote`, :func:`build_remote_section`, :func:`parse_config`,
  :func:`render_config`.
* **Cloud ops (user-initiated)** -- :func:`test_remote`, :func:`list_path`,
  :func:`about`, :func:`sync`, plus :func:`build_sync_args` / :func:`remote_path`.
* **Environment** -- :func:`rclone_available`, :func:`rclone_version`.
* **Local filesystem (A8)** -- :mod:`cloudsync.paths` auto-detects the OS and
  returns the correct local roots (drive letters on Windows, ``/`` + mounts on
  Linux/macOS).

Every function raises :class:`CloudSyncError` (and only that) on recoverable
failure.  Importing this package has no side effects; the tkinter GUI lives in
:mod:`cloudsync.gui` and is built lazily.
"""

from __future__ import annotations

from .errors import CloudSyncError
from . import paths
from .rclone import (
    BUCKET_KEY,
    OPERATIONS,
    PROVIDERS,
    PROVIDERS_BY_KEY,
    Entry,
    Provider,
    Remote,
    about,
    add_remote,
    build_remote_section,
    build_sync_args,
    delete_remote,
    friendly_test_error,
    get_provider,
    get_remote,
    human_size,
    list_path,
    list_remotes,
    obscure,
    parse_about,
    parse_config,
    parse_lsjson,
    provider_keys,
    rclone_available,
    rclone_version,
    remote_exists,
    remote_names,
    remote_path,
    render_config,
    sanitize_endpoint,
    save_remotes,
    sync,
    test_remote,
    validate_remote_name,
)

__version__ = "1.3.0"

__all__ = [
    "CloudSyncError",
    "paths",
    "BUCKET_KEY",
    "OPERATIONS",
    "PROVIDERS",
    "PROVIDERS_BY_KEY",
    "Provider",
    "Remote",
    "Entry",
    "provider_keys",
    "get_provider",
    "validate_remote_name",
    "build_remote_section",
    "sanitize_endpoint",
    "friendly_test_error",
    "parse_config",
    "render_config",
    "list_remotes",
    "remote_names",
    "get_remote",
    "remote_exists",
    "save_remotes",
    "add_remote",
    "delete_remote",
    "obscure",
    "remote_path",
    "build_sync_args",
    "parse_lsjson",
    "parse_about",
    "human_size",
    "rclone_available",
    "rclone_version",
    "test_remote",
    "list_path",
    "about",
    "sync",
    "__version__",
]
