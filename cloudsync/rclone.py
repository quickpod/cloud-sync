r"""The rclone engine: the provider catalogue, config-file model, pure builders
and parsers, and the privileged operations that shell out to ``rclone``.

The module is split into layers so the interesting logic is pure and testable
without a network, a real cloud account, or even ``rclone`` installed:

* **Providers** -- :data:`PROVIDERS` is the S3 / major-cloud catalogue behind
  the OneDrive-style "add a remote" form (Amazon S3, Cloudflare R2, Backblaze
  B2, Wasabi, DigitalOcean Spaces, MinIO, and a catch-all custom endpoint).
* **Config model** -- a :class:`Remote` is one named rclone remote.
  :func:`parse_config` / :func:`render_config` round-trip an ``rclone.conf``
  (INI) losslessly, and :func:`build_remote_section` turns the friendly form
  fields into the exact rclone keys.  All pure, no I/O.
* **Command builders** -- :func:`build_sync_args`, :func:`remote_path`,
  :func:`parse_lsjson`, :func:`parse_about`.  Pure, so the GUI and CLI share
  one source of truth and the args can be unit-tested.
* **Operations** -- :func:`list_remotes`, :func:`add_remote`,
  :func:`delete_remote`, :func:`test_remote`, :func:`list_path`, :func:`sync`,
  :func:`about`.  These shell out to ``rclone`` through the single
  :func:`_execute` boundary (the seam tests monkeypatch).

Everything raises :class:`CloudSyncError` (and only that) on failure.  The user
configures every remote themselves -- **nothing connects on its own**; the only
outbound calls happen when the user explicitly tests a remote, browses, or
syncs.  On a host without ``rclone`` the module still imports and
:func:`rclone_available` returns ``False`` so callers degrade with a clear
"install rclone" message instead of crashing.  100% AI-built, open source,
published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import configparser
import io
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import paths
from .errors import CloudSyncError

# --------------------------------------------------------------------------- #
# Provider catalogue (pure data — drives the "add a remote" form)
# --------------------------------------------------------------------------- #
OPERATIONS = ("sync", "copy", "bisync")


@dataclass(frozen=True)
class Provider:
    """One selectable cloud in the add-remote form.

    ``rclone_provider`` is the value rclone's S3 backend expects for its
    ``provider`` key.  ``needs_endpoint`` is True for every S3-compatible cloud
    that is addressed by a custom endpoint (everything except AWS, which is
    region-addressed).
    """

    key: str
    label: str
    rclone_provider: str
    needs_endpoint: bool
    default_region: str = ""
    endpoint_hint: str = ""
    note: str = ""


PROVIDERS: Tuple[Provider, ...] = (
    Provider("aws", "Amazon S3", "AWS", False, "us-east-1",
             "", "Region-addressed; leave endpoint blank."),
    Provider("r2", "Cloudflare R2", "Cloudflare", True, "auto",
             "https://<account-id>.r2.cloudflarestorage.com",
             "Find your endpoint in the R2 dashboard."),
    Provider("b2", "Backblaze B2 (S3)", "Other", True, "",
             "https://s3.<region>.backblazeb2.com",
             "Use the S3-compatible endpoint shown in your B2 bucket."),
    Provider("wasabi", "Wasabi", "Wasabi", True, "",
             "https://s3.<region>.wasabisys.com",
             "Endpoint is region-specific."),
    Provider("spaces", "DigitalOcean Spaces", "DigitalOcean", True, "",
             "https://<region>.digitaloceanspaces.com",
             "Region is part of the endpoint host."),
    Provider("minio", "MinIO / self-hosted", "Minio", True, "",
             "https://minio.example.com",
             "Point at your own MinIO / Ceph gateway."),
    Provider("other", "Other S3-compatible", "Other", True, "",
             "https://s3.example.com",
             "Any endpoint that speaks the S3 API."),
)

PROVIDERS_BY_KEY: Dict[str, Provider] = {p.key: p for p in PROVIDERS}
_PROVIDER_BY_RCLONE = {p.rclone_provider: p for p in reversed(PROVIDERS)}


def provider_keys() -> List[str]:
    return [p.key for p in PROVIDERS]


def get_provider(key: str) -> Provider:
    prov = PROVIDERS_BY_KEY.get((key or "").strip().lower())
    if prov is None:
        raise CloudSyncError(
            f"Unknown provider {key!r}; choose from: {', '.join(provider_keys())}.")
    return prov


# --------------------------------------------------------------------------- #
# The Remote config model
# --------------------------------------------------------------------------- #
_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")
# Keys that hold a secret and must never be printed/logged.
SECRET_KEYS = ("secret_access_key", "session_token", "sse_kms_key_id")


def validate_remote_name(name: str) -> str:
    """Return a cleaned, valid rclone remote name or raise.

    rclone remote names may contain letters, digits, ``_``, ``.`` and ``-``
    (no spaces, no leading ``-``).  Used by both add and rename paths.
    """
    n = (name or "").strip()
    if not n:
        raise CloudSyncError("Give the remote a name.")
    if not _NAME_RE.match(n):
        raise CloudSyncError(
            "Remote names may use letters, digits, '_', '.' and '-' "
            "(no spaces), and cannot start with '-'.")
    return n


@dataclass
class Remote:
    """One named rclone remote: its section name plus the raw rclone keys.

    Storing the full ``params`` dict (rather than a fixed set of columns) means
    a config written by rclone itself round-trips losslessly, including keys
    Cloud Sync does not model.
    """

    name: str
    params: Dict[str, str] = field(default_factory=dict)

    # -- typed accessors over the raw params --------------------------------
    @property
    def type(self) -> str:
        return self.params.get("type", "")

    @property
    def rclone_provider(self) -> str:
        return self.params.get("provider", "")

    @property
    def endpoint(self) -> str:
        return self.params.get("endpoint", "")

    @property
    def region(self) -> str:
        return self.params.get("region", "")

    @property
    def access_key_id(self) -> str:
        return self.params.get("access_key_id", "")

    @property
    def provider_key(self) -> str:
        """Best-effort reverse map to a :class:`Provider` key (for the form)."""
        if self.type != "s3":
            return "other"
        prov = _PROVIDER_BY_RCLONE.get(self.rclone_provider)
        return prov.key if prov else "other"

    def provider_label(self) -> str:
        prov = PROVIDERS_BY_KEY.get(self.provider_key)
        return prov.label if prov else (self.rclone_provider or self.type or "?")

    def as_dict(self) -> Dict[str, object]:
        """A safe summary for the CLI/GUI — secrets are redacted."""
        return {
            "name": self.name,
            "type": self.type,
            "provider": self.provider_label(),
            "provider_key": self.provider_key,
            "endpoint": self.endpoint,
            "region": self.region,
            "access_key_id": self.access_key_id,
            "has_secret": bool(self.params.get("secret_access_key")),
        }


def build_remote_section(
    name: str,
    provider_key: str,
    access_key_id: str,
    secret_access_key: str,
    *,
    endpoint: Optional[str] = None,
    region: Optional[str] = None,
) -> Remote:
    """Turn the friendly add-remote form fields into a :class:`Remote` (pure).

    ``secret_access_key`` is stored verbatim -- callers that talk to a real
    rclone should pass the value returned by :func:`obscure` first.  Validates
    the name, provider and required endpoint, and raises
    :class:`CloudSyncError` on anything unusable.
    """
    name = validate_remote_name(name)
    prov = get_provider(provider_key)
    access_key_id = (access_key_id or "").strip()
    secret_access_key = secret_access_key or ""
    endpoint = (endpoint or "").strip()
    region = (region or "").strip() or prov.default_region

    if not access_key_id:
        raise CloudSyncError("Enter the access key ID.")
    if not secret_access_key:
        raise CloudSyncError("Enter the secret access key.")
    if prov.needs_endpoint and not endpoint:
        raise CloudSyncError(
            f"{prov.label} needs an endpoint URL "
            f"(e.g. {prov.endpoint_hint or 'https://s3.example.com'}).")

    params: Dict[str, str] = {
        "type": "s3",
        "provider": prov.rclone_provider,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
    }
    if endpoint:
        params["endpoint"] = endpoint
    if region:
        params["region"] = region
    return Remote(name=name, params=params)


def parse_config(text: str) -> List[Remote]:
    """Parse ``rclone.conf`` INI text into a list of :class:`Remote` (pure).

    Tolerant of an empty/missing file (returns ``[]``) and of keys Cloud Sync
    does not model (they are preserved).  ``%`` in values is treated literally
    (interpolation disabled), matching how rclone stores endpoints/secrets.
    """
    remotes: List[Remote] = []
    if not text or not text.strip():
        return remotes
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve key case
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise CloudSyncError(f"Could not parse the rclone config: {exc}.")
    for section in parser.sections():
        params = {k: v for k, v in parser.items(section)}
        remotes.append(Remote(name=section, params=params))
    return remotes


def render_config(remotes: List[Remote]) -> str:
    """Render a list of :class:`Remote` back to ``rclone.conf`` INI text (pure)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for r in remotes:
        parser[r.name] = {k: str(v) for k, v in r.params.items()}
    buf = io.StringIO()
    parser.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Command / path builders + output parsers (pure)
# --------------------------------------------------------------------------- #
def remote_path(remote_name: str, path: str = "") -> str:
    """Build an ``rclone`` ``remote:path`` string from a name and sub-path."""
    name = (remote_name or "").strip().rstrip(":")
    if not name:
        raise CloudSyncError("Choose a remote first.")
    sub = (path or "").strip().lstrip("/")
    return f"{name}:{sub}"


def build_sync_args(
    source: str,
    dest: str,
    *,
    operation: str = "sync",
    dry_run: bool = False,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """Build the ``rclone`` argument vector for a transfer (no leading rclone).

    Examples::

        build_sync_args("/home/me/docs", "s3:bucket/docs")
            -> ["sync", "/home/me/docs", "s3:bucket/docs"]
        build_sync_args("s3:bucket", "/home/me/dl", operation="copy",
                        dry_run=True)
            -> ["copy", "s3:bucket", "/home/me/dl", "--dry-run"]

    ``operation`` is one of :data:`OPERATIONS`.  ``sync`` makes the destination
    match the source (it can delete on the destination); ``copy`` only adds;
    ``bisync`` reconciles both ways.  Raises on a bad operation or an empty
    endpoint.
    """
    operation = (operation or "").strip().lower()
    if operation not in OPERATIONS:
        raise CloudSyncError(
            f"Unknown operation {operation!r} (expected one of "
            f"{', '.join(OPERATIONS)}).")
    source = (source or "").strip()
    dest = (dest or "").strip()
    if not source or not dest:
        raise CloudSyncError("Both a source and a destination are required.")
    args = [operation, source, dest]
    if dry_run:
        args.append("--dry-run")
    if extra:
        args.extend(extra)
    return args


@dataclass
class Entry:
    """One row from ``rclone lsjson`` (a file or a folder)."""

    name: str
    path: str
    size: int
    is_dir: bool
    mod_time: str = ""

    def as_dict(self):
        return {"name": self.name, "path": self.path, "size": self.size,
                "is_dir": self.is_dir, "mod_time": self.mod_time}


def parse_lsjson(text: str) -> List[Entry]:
    """Parse ``rclone lsjson`` output into :class:`Entry` rows (pure).

    Folders sort before files, then case-insensitively by name.  Malformed or
    empty input yields ``[]`` (never raises) so a listing glitch cannot crash
    the browser view.
    """
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Entry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        is_dir = bool(item.get("IsDir"))
        size = item.get("Size", 0)
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        out.append(Entry(
            name=str(item.get("Name", "")),
            path=str(item.get("Path", item.get("Name", ""))),
            size=max(0, size),
            is_dir=is_dir,
            mod_time=str(item.get("ModTime", "")),
        ))
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def parse_about(text: str) -> Dict[str, Optional[int]]:
    """Parse ``rclone about --json`` into ``{total, used, free, objects}`` (pure)."""
    result: Dict[str, Optional[int]] = {
        "total": None, "used": None, "free": None, "objects": None}
    if not text or not text.strip():
        return result
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return result
    if isinstance(data, dict):
        for key in result:
            val = data.get(key)
            if isinstance(val, (int, float)):
                result[key] = int(val)
    return result


def human_size(num_bytes) -> str:
    """Human-readable byte size, e.g. ``1.1MB`` (mirrors the other QuickOpen apps)."""
    if num_bytes is None:
        return "—"
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


# --------------------------------------------------------------------------- #
# Environment / availability
# --------------------------------------------------------------------------- #
def _rclone_path() -> Optional[str]:
    """Absolute path to the ``rclone`` binary, or None if not installed."""
    found = shutil.which("rclone")
    if found:
        return found
    candidates = [
        "/usr/bin/rclone", "/usr/local/bin/rclone", "/snap/bin/rclone",
        "/opt/homebrew/bin/rclone",
    ]
    if paths.is_windows():
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "rclone", "rclone.exe"))
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None


def rclone_available() -> bool:
    """True when an ``rclone`` binary can be found on this host (any OS)."""
    return _rclone_path() is not None


NO_RCLONE_MSG = (
    "rclone was not found. Cloud Sync uses the open-source rclone engine to "
    "talk to your cloud. Install it from https://rclone.org/downloads/ "
    "(or your package manager), then reopen Cloud Sync. You can still add and "
    "edit remotes offline — they are saved for when rclone is available.")


def _require_rclone() -> str:
    path = _rclone_path()
    if not path:
        raise CloudSyncError(NO_RCLONE_MSG)
    return path


def rclone_version() -> str:
    """Return the installed rclone version string (privileged), or raise."""
    out = _run(["version"], timeout=15)
    for line in out.splitlines():
        if line.lower().startswith("rclone v"):
            return line.strip()
    return out.strip().splitlines()[0] if out.strip() else "rclone (unknown version)"


# --------------------------------------------------------------------------- #
# Subprocess boundary (the single seam tests monkeypatch)
# --------------------------------------------------------------------------- #
def _execute(argv: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run *argv* and return ``(returncode, stdout, stderr)``.

    The one and only place cloudsync shells out.  Tests replace this to assert
    on the argument vector without touching rclone or the network.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise CloudSyncError(f"Command not found: {argv[0]} ({exc}).")
    except subprocess.TimeoutExpired:
        raise CloudSyncError("Timed out waiting for rclone to respond.")
    except Exception as exc:  # pragma: no cover - defensive
        raise CloudSyncError(f"Could not run rclone: {exc}.")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run(args: List[str], timeout: int = 120,
         config_path: Optional[str] = None) -> str:
    """Build the rclone command line, run it, and return stdout (or raise).

    Every call is pinned to Cloud Sync's dedicated ``--config`` file so the
    user's global rclone config is never touched.
    """
    path = _require_rclone()
    cfg = config_path or str(paths.default_config_path())
    argv = [path, "--config", cfg, *args]
    rc, out, err = _execute(argv, timeout=timeout)
    if rc != 0:
        detail = (err or out).strip()
        raise CloudSyncError(detail or f"rclone exited with status {rc}.")
    return out


# --------------------------------------------------------------------------- #
# Config-file operations
# --------------------------------------------------------------------------- #
def _read_config_text() -> str:
    cfg = paths.default_config_path()
    try:
        with open(cfg, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise CloudSyncError(f"Could not read the config file {cfg}: {exc}.")


def list_remotes() -> List[Remote]:
    """Return every configured remote (read from Cloud Sync's rclone.conf)."""
    return parse_config(_read_config_text())


def remote_names() -> List[str]:
    return [r.name for r in list_remotes()]


def get_remote(name: str) -> Remote:
    name = (name or "").strip()
    for r in list_remotes():
        if r.name == name:
            return r
    raise CloudSyncError(f"No remote named {name!r}.")


def remote_exists(name: str) -> bool:
    name = (name or "").strip()
    return any(r.name == name for r in list_remotes())


def save_remotes(remotes: List[Remote]) -> None:
    """Atomically write *remotes* to Cloud Sync's rclone.conf (0600 where possible)."""
    paths.ensure_config_dir()
    cfg = paths.default_config_path()
    text = render_config(remotes)
    tmp = str(cfg) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, 0o600)   # secrets live here; tighten perms on POSIX
        except Exception:
            pass
        os.replace(tmp, str(cfg))
    except OSError as exc:
        raise CloudSyncError(f"Could not write the config file {cfg}: {exc}.")


def obscure(secret: str) -> str:
    """Obscure a secret with ``rclone obscure`` (how rclone stores secrets).

    Requires rclone.  Raises :class:`CloudSyncError` if it is not installed so
    the caller can offer to save the remote as-is for later.
    """
    if not secret:
        raise CloudSyncError("Enter the secret access key.")
    out = _run(["obscure", secret], timeout=15)
    return out.strip()


def add_remote(name: str, provider_key: str, access_key_id: str,
               secret_access_key: str, *, endpoint: Optional[str] = None,
               region: Optional[str] = None, overwrite: bool = False) -> Remote:
    """Validate, obscure the secret, and persist a new remote.

    The secret is obscured via rclone when available; if rclone is missing the
    remote is still saved (secret stored as typed) so setup can happen offline.
    Raises if the name already exists and ``overwrite`` is False.
    """
    name = validate_remote_name(name)
    remotes = list_remotes()
    if any(r.name == name for r in remotes) and not overwrite:
        raise CloudSyncError(
            f"A remote named {name!r} already exists. Pick another name or edit it.")
    stored_secret = secret_access_key
    if rclone_available():
        stored_secret = obscure(secret_access_key)
    remote = build_remote_section(
        name, provider_key, access_key_id, stored_secret,
        endpoint=endpoint, region=region)
    remotes = [r for r in remotes if r.name != name]
    remotes.append(remote)
    save_remotes(remotes)
    return remote


def delete_remote(name: str) -> None:
    """Remove a remote from the config (raises if it does not exist)."""
    name = (name or "").strip()
    remotes = list_remotes()
    if not any(r.name == name for r in remotes):
        raise CloudSyncError(f"No remote named {name!r}.")
    save_remotes([r for r in remotes if r.name != name])


# --------------------------------------------------------------------------- #
# Cloud operations (user-initiated only — nothing here runs on its own)
# --------------------------------------------------------------------------- #
def test_remote(name: str) -> str:
    """Connectivity check: list the remote's top level (privileged, network).

    Only ever called when the user clicks "Test" / runs ``cloudsync test``.
    Returns rclone's stdout on success; a clean error otherwise.
    """
    if not remote_exists(name):
        raise CloudSyncError(f"No remote named {name!r}.")
    return _run(["lsd", remote_path(name)], timeout=60)


def list_path(remote_name: str, path: str = "") -> List[Entry]:
    """List one level of a remote via ``rclone lsjson`` (privileged, network)."""
    if not remote_exists(remote_name):
        raise CloudSyncError(f"No remote named {remote_name!r}.")
    out = _run(["lsjson", remote_path(remote_name, path)], timeout=120)
    return parse_lsjson(out)


def about(name: str) -> Dict[str, Optional[int]]:
    """Storage usage for a remote via ``rclone about --json`` (privileged)."""
    if not remote_exists(name):
        raise CloudSyncError(f"No remote named {name!r}.")
    out = _run(["about", remote_path(name), "--json"], timeout=60)
    return parse_about(out)


def sync(source: str, dest: str, *, operation: str = "sync",
         dry_run: bool = False, extra: Optional[List[str]] = None) -> str:
    """Run a transfer (privileged, network) and return rclone's output.

    See :func:`build_sync_args` for the operation semantics.  A ``--stats``
    flag is added so long transfers report progress.
    """
    args = build_sync_args(source, dest, operation=operation,
                           dry_run=dry_run, extra=extra)
    args += ["--stats-one-line", "--stats", "1s"]
    return _run(args, timeout=24 * 3600)


# --------------------------------------------------------------------------- #
# Per-file operations (the realtime sync engine's building blocks)
# --------------------------------------------------------------------------- #
def stat_path(remote_name: str, path: str) -> Optional[Entry]:
    """Stat ONE remote file/folder via ``rclone lsjson --stat`` (privileged).

    Returns ``None`` when the object does not exist (rclone reports "not
    found"); raises :class:`CloudSyncError` on real failures.
    """
    target = remote_path(remote_name, path)
    try:
        out = _run(["lsjson", "--stat", target], timeout=60)
    except CloudSyncError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "directory not found" in msg or \
                "object not found" in msg or "error 404" in msg:
            return None
        raise
    if not out or not out.strip() or out.strip() == "null":
        return None
    try:
        item = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(item, dict):
        return None
    entries = parse_lsjson(json.dumps([item]))
    return entries[0] if entries else None


def list_recursive(remote_name: str, path: str = "") -> List[Entry]:
    """Recursively list files under a remote path (``lsjson -R``, files only)."""
    if not remote_exists(remote_name):
        raise CloudSyncError(f"No remote named {remote_name!r}.")
    try:
        out = _run(["lsjson", "-R", "--files-only",
                    remote_path(remote_name, path)], timeout=600)
    except CloudSyncError as exc:
        if "not found" in str(exc).lower():
            return []
        raise
    return parse_lsjson(out)


def copyto(source: str, dest: str) -> None:
    """Copy ONE file (``rclone copyto``) — local→remote or remote→local."""
    if not source or not dest:
        raise CloudSyncError("Both a source and a destination are required.")
    _run(["copyto", source, dest], timeout=3600)


def deletefile(remote_file: str) -> None:
    """Delete ONE remote file (``rclone deletefile``); missing files are fine."""
    try:
        _run(["deletefile", remote_file], timeout=120)
    except CloudSyncError as exc:
        if "not found" in str(exc).lower():
            return
        raise
