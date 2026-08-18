r"""The continuous-sync engine: realtime watch + scheduled runs over the
existing rclone backend (owner directive 2026-08-16; benchmark Dropbox/
OneDrive continuous sync).

This module is a *trigger + state layer* on top of :mod:`cloudsync.rclone` —
it never re-implements the transfer protocol.  Everything observable is pure
and testable without rclone, a network or even the ``watchdog`` package:

* **Pairs** — a :class:`Pair` is one synced folder: a local directory mapped
  to ``remote:path``.  Pairs are stored by :mod:`cloudsync.guiconfig`.
* **Conflicts** — :func:`conflicted_name` implements Dropbox's
  "name (conflicted copy YYYY-MM-DD).ext" convention.  Resolution is
  newer-wins with the losing version KEPT as a conflicted copy — data is
  never silently lost (:meth:`SyncEngine._resolve_conflict`).
* **State** — the last-synced (local mtime, remote ModTime, size) per file
  lives in a small JSON next to the rclone config, so a remote change since
  the last sync is distinguishable from a merely-older remote copy.
* **Modes** — ``realtime`` (default) watches the local folders with
  ``watchdog`` when available (a light polling scan otherwise) and syncs
  changed files immediately, with a periodic remote reconcile;
  ``scheduled`` runs a full reconcile every N minutes or daily at HH:MM.
* **Status** — per-file states (``pending`` ↻ / ``syncing`` ↻ / ``synced`` ✓ /
  ``conflict`` / ``error`` ⚠) stream to the GUI through a notify callback,
  plus an overall summary.  Pause/resume holds the queue without dropping it.

No telemetry: the only network calls go to the user's configured remotes,
through rclone.  100% AI-built, open source (quickopen.ai).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import queue
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import paths, rclone
from .errors import CloudSyncError

# File states surfaced to the UI.
PENDING = "pending"
SYNCING = "syncing"
SYNCED = "synced"
CONFLICT = "conflict"
ERROR = "error"

STATE_GLYPH = {PENDING: "↻", SYNCING: "↻", SYNCED: "✓",
               CONFLICT: "✓", ERROR: "⚠"}

# Modes
REALTIME = "realtime"
SCHEDULED = "scheduled"

DEBOUNCE_SECONDS = 1.5          # quiet window after a filesystem event
POLL_SECONDS = 10               # local scan cadence without watchdog
RECONCILE_SECONDS = 15 * 60     # remote reconcile cadence in realtime mode

# Transient/junk files that editors, browsers and build tools leave around.
# Syncing these wastes transfer, and worse, they change constantly, so they
# generate endless spurious activity and conflicts.
#: Where conflict resolution stages an incoming file before swapping it in.
#: It lands inside the synced folder, and nothing suppresses the engine's own
#: writes, so it MUST be ignored or the watcher uploads the half-written
#: staging file and the next reconcile pulls the orphan back down.
INCOMING_SUFFIX = ".cloudsync-incoming"

IGNORE_SUFFIXES = (".tmp", ".temp", ".part", ".partial", ".swp", ".swo",
                   ".swx", ".crdownload", ".download", ".log", ".bak", ".old",
                   ".pyc", ".pyo", ".class", ".o", ".obj", ".lock",
                   INCOMING_SUFFIX)
IGNORE_PREFIXES = ("~$", ".~", ".#")
#: Exact names, matched case-insensitively.
IGNORE_NAMES = (".ds_store", "thumbs.db", "desktop.ini", ".directory",
                "._.ds_store")

#: Directories that are never descended into.  ``.git`` is the important one:
#: its refs and objects are rewritten constantly, and a "conflicted copy" of
#: HEAD or a branch ref silently corrupts the repository.  The rest are build
#: and dependency caches that are reproducible and often enormous.
IGNORE_DIRS = (".git", ".hg", ".svn", "__pycache__", "node_modules",
               ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
               ".gradle", ".idea", ".vscode", "target", ".next", ".cache",
               ".Trash", "$RECYCLE.BIN", "System Volume Information")


def _extra_patterns() -> tuple:
    """User-supplied glob patterns from settings (never fatal if unreadable)."""
    try:
        from . import guiconfig
        return tuple(guiconfig.get_ignore_patterns())
    except Exception:
        return ()


def should_ignore_dir(name: str) -> bool:
    """True for a directory that must not be traversed at all."""
    base = os.path.basename(name.rstrip("/\\"))
    if not base:
        return False
    return any(base.lower() == d.lower() for d in IGNORE_DIRS)


def should_ignore(name: str) -> bool:
    """True for a path that must never be synced (pure apart from settings).

    Accepts either a bare filename or a relative path; any excluded directory
    anywhere in the path excludes what is under it.
    """
    normalised = str(name or "").replace("\\", "/")
    if not normalised:
        return True
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if not parts:
        return True
    if any(should_ignore_dir(p) for p in parts[:-1]):
        return True
    base = parts[-1]
    low = base.lower()
    if low in IGNORE_NAMES:
        return True
    if low.endswith(IGNORE_SUFFIXES):
        return True
    if any(base.startswith(p) for p in IGNORE_PREFIXES):
        return True
    if should_ignore_dir(base):
        return True
    for pattern in _extra_patterns():
        if fnmatch.fnmatch(low, pattern.lower()) or \
                fnmatch.fnmatch(normalised.lower(), pattern.lower()):
            return True
    return False


#: The counter is optional: the first conflict of a day takes the plain name
#: and every one after it is numbered, so leaving the counter out of this
#: pattern would skip exactly the copies that accumulate.
CONFLICT_RE = re.compile(
    r'(?: \(conflicted copy \d{4}-\d{2}-\d{2}(?: \d+)?\))+')


def _same_bytes(a: str, b: str, chunk: int = 1 << 16) -> bool:
    """True when two files are byte-identical (size first, then content)."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(chunk), fb.read(chunk)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def reconcile_conflicts(root_dir: str, remove_identical: bool = True):
    """Tidy conflicted copies that turned out not to be conflicts.

    Most "conflicted copy" files are not disagreements at all -- they are the
    same bytes under a second name, left behind when resolution ran on a file
    that had not really diverged. Every batch seen in the field has been
    byte-identical to its original.

    Three cases, and only the first two are safe to act on:

    * the original is missing -- the copy IS the file, so restore its name;
    * the original is present and identical -- the copy is noise, delete it;
    * the original is present and differs -- a real conflict, leave it alone
      and report it, because only the user can say which version they want.

    Returns ``(restored, removed, kept)``.
    """
    restored = removed = 0
    kept = []
    for dirpath, dirs, files in os.walk(root_dir):
        # Prune what the sync itself never descends into. This pass renames
        # and deletes, which is precisely the "acting on files that must never
        # move" that iter_local_files prunes .git and friends to prevent.
        dirs[:] = [d for d in dirs if not should_ignore_dir(d)]
        for name in sorted(files):
            if not CONFLICT_RE.search(name):
                continue
            src = os.path.join(dirpath, name)
            dst = os.path.join(dirpath, CONFLICT_RE.sub("", name))
            if not os.path.exists(dst):
                try:
                    os.rename(src, dst)
                    restored += 1
                except OSError:
                    pass
                continue
            if remove_identical and _same_bytes(src, dst):
                try:
                    os.remove(src)
                    removed += 1
                except OSError:
                    pass
            else:
                kept.append(src)
    return restored, removed, kept


def iter_local_files(root_dir: str):
    """Yield ``(full_path, rel_path)`` for every syncable file under *root_dir*.

    Excluded directories are pruned rather than filtered afterwards: walking
    into ``node_modules`` or ``.git`` to discard the results costs seconds on a
    large tree and, for ``.git``, risks acting on files that must never move.
    """
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not should_ignore_dir(d)]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, root_dir).replace(os.sep, "/")
            if should_ignore(rel):
                continue
            yield full, rel


@lru_cache(maxsize=512)
def _pair_key(local: str, remote: str, rpath: str) -> str:
    """Stable identity for a pair. Cached because it is read in hot loops."""
    raw = f"{os.path.abspath(local)}|{remote}|{rpath}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Pair:
    """One synced folder: a local directory mapped onto ``remote:path``."""

    local: str
    remote: str
    rpath: str = ""

    @property
    def key(self) -> str:
        """A filesystem-safe identity used for the state file name.

        Cached: this is read inside per-file loops in the UI, and recomputing
        an abspath plus a SHA1 on every access made refreshing the folder list
        cost more than the syncing did.
        """
        return _pair_key(self.local, self.remote, self.rpath)

    @property
    def label(self) -> str:
        return f"{self.local}  ⇄  {rclone.remote_path(self.remote, self.rpath)}"

    def remote_file(self, rel: str) -> str:
        sub = "/".join(p for p in (self.rpath.strip("/"), rel) if p)
        return rclone.remote_path(self.remote, sub)

    def as_dict(self) -> Dict[str, str]:
        return {"local": self.local, "remote": self.remote,
                "rpath": self.rpath}


def pair_from_dict(d: Dict[str, str]) -> Optional[Pair]:
    try:
        local = str(d.get("local") or "").strip()
        remote = str(d.get("remote") or "").strip()
        if not local or not remote:
            return None
        return Pair(local=local, remote=remote,
                    rpath=str(d.get("rpath") or "").strip())
    except Exception:
        return None


def conflicted_name(path: str, when: Optional[float] = None) -> str:
    """Dropbox-style conflicted-copy name for *path* (pure).

    ``report.txt`` → ``report (conflicted copy 2026-08-16).txt``; a counter is
    appended while the name already exists on disk.
    """
    stamp = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
    root, ext = os.path.splitext(path)
    candidate = f"{root} (conflicted copy {stamp}){ext}"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{root} (conflicted copy {stamp} {n}){ext}"
        n += 1
    return candidate


def parse_mod_time(text: str) -> Optional[float]:
    """Parse an rclone ``ModTime`` (RFC3339) into an epoch second (pure)."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        # 2026-08-16T07:00:00.123456789Z / +02:00 — trim sub-µs digits
        if "." in s:
            head, tail = s.split(".", 1)
            frac = ""
            zone = ""
            for i, ch in enumerate(tail):
                if ch.isdigit():
                    frac += ch
                else:
                    zone = tail[i:]
                    break
            s = head + "." + (frac[:6] or "0") + zone
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_daily_time(text: str) -> Optional[Tuple[int, int]]:
    """Parse "HH:MM" into ``(hour, minute)`` or None (pure)."""
    try:
        parts = (text or "").strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def seconds_until_daily(hour: int, minute: int, now: Optional[float] = None) -> float:
    """Seconds from *now* until the next HH:MM occurrence (pure)."""
    now_dt = datetime.fromtimestamp(now if now is not None else time.time())
    target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (target - now_dt).total_seconds()
    if delta <= 0:
        delta += 24 * 3600
    return delta


# --------------------------------------------------------------------------- #
# Per-pair last-synced state (JSON beside the rclone config)
# --------------------------------------------------------------------------- #
def _state_dir() -> str:
    d = os.path.join(str(paths.config_dir()), "sync-state")
    os.makedirs(d, exist_ok=True)
    return d


def load_state(pair: Pair) -> Dict[str, Dict]:
    try:
        with open(os.path.join(_state_dir(), pair.key + ".json"),
                  "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(pair: Pair, state: Dict[str, Dict]) -> None:
    try:
        target = os.path.join(_state_dir(), pair.key + ".json")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, target)
    except Exception:
        pass  # state is an optimization; never fatal


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class SyncEngine:
    """Watches pairs and keeps them synced; see the module docstring.

    ``notify(event)`` is called from worker threads with dicts::

        {"kind": "file", "pair": Pair, "rel": str, "status": str,
         "detail": str}
        {"kind": "overall", "status": "idle|syncing|paused|error",
         "pending": int, "errors": int, "detail": str}

    GUI callers must marshal onto their UI thread themselves.
    """

    def __init__(self, pairs: Optional[List[Pair]] = None,
                 notify: Optional[Callable[[dict], None]] = None):
        self.pairs: List[Pair] = list(pairs or [])
        self._notify_cb = notify
        self._q: "queue.Queue[Tuple[Pair, str]]" = queue.Queue()
        self._queued: set = set()
        self._pending_events: Dict[Tuple[str, str], float] = {}
        self._pending_lock = threading.Lock()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._observer = None
        self._errors: Dict[Tuple[str, str], str] = {}
        self.mode = None
        self.file_status: Dict[Tuple[str, str], Tuple[str, str, float]] = {}

    # ---- events ---------------------------------------------------------
    def _notify(self, event: dict) -> None:
        if self._notify_cb:
            try:
                self._notify_cb(event)
            except Exception:
                pass

    def _file_event(self, pair: Pair, rel: str, status: str,
                    detail: str = "") -> None:
        self.file_status[(pair.key, rel)] = (status, detail, time.time())
        if status == ERROR:
            self._errors[(pair.key, rel)] = detail
        else:
            self._errors.pop((pair.key, rel), None)
        self._notify({"kind": "file", "pair": pair, "rel": rel,
                      "status": status, "detail": detail})
        self._overall_event()

    def _overall_event(self, detail: str = "") -> None:
        pending = self._q.qsize() + len(self._pending_events)
        if self._paused.is_set():
            status = "paused"
        elif pending:
            status = "syncing"
        elif self._errors:
            status = "error"
        else:
            status = "idle"
        self._notify({"kind": "overall", "status": status,
                      "pending": pending, "errors": len(self._errors),
                      "detail": detail})

    # ---- lifecycle ------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()
        self._overall_event("Paused.")

    def resume(self) -> None:
        self._paused.clear()
        self._overall_event("Resumed.")

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass
        # unblock the worker
        try:
            self._q.put_nowait((None, None))
        except Exception:
            pass

    def start(self, mode: str = REALTIME, *, interval_minutes: int = 30,
              daily_at: Optional[str] = None) -> None:
        """Start the engine in *mode*.  Safe to call once per instance."""
        self.mode = mode
        self._start_worker()
        if mode == REALTIME:
            if not self._start_watchdog():
                self._start_thread(self._poll_loop, "cloudsync-poll")
            self._start_thread(self._reconcile_loop, "cloudsync-reconcile")
            self.sync_now()          # initial reconcile brings state up
        else:
            self._daily_at = parse_daily_time(daily_at or "")
            self._interval = max(1, int(interval_minutes or 30))
            self._start_thread(self._schedule_loop, "cloudsync-schedule")

    def _start_thread(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # ---- enqueue --------------------------------------------------------
    def queue_change(self, pair: Pair, rel: str) -> None:
        """Register a (debounced) local change for *rel* under *pair*."""
        if should_ignore(rel):
            return
        with self._pending_lock:
            self._pending_events[(pair.key, rel)] = time.time()
        self._file_event(pair, rel, PENDING, "waiting to sync")

    def sync_now(self) -> None:
        """Enqueue a full reconcile of every pair (both modes)."""
        for pair in list(self.pairs):
            self._enqueue(pair, None)
        self._overall_event("Sync requested.")

    def _enqueue(self, pair: Pair, rel: Optional[str]) -> None:
        key = (pair.key, rel)
        if key in self._queued:
            return
        self._queued.add(key)
        self._q.put((pair, rel))

    # ---- worker ---------------------------------------------------------
    def _start_worker(self):
        self._start_thread(self._worker_loop, "cloudsync-worker")
        self._start_thread(self._debounce_loop, "cloudsync-debounce")

    def _debounce_loop(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            due = []
            now = time.time()
            with self._pending_lock:
                for (pkey, rel), ts in list(self._pending_events.items()):
                    if now - ts >= DEBOUNCE_SECONDS:
                        del self._pending_events[(pkey, rel)]
                        due.append((pkey, rel))
            for pkey, rel in due:
                pair = next((p for p in self.pairs if p.key == pkey), None)
                if pair is not None:
                    self._enqueue(pair, rel)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                pair, rel = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if pair is None:
                continue
            while self._paused.is_set() and not self._stop.is_set():
                time.sleep(0.3)
            if self._stop.is_set():
                return
            self._queued.discard((pair.key, rel))
            try:
                if rel is None:
                    self._reconcile_pair(pair)
                else:
                    self._process_file(pair, rel)
            except CloudSyncError as exc:
                target = rel or "(reconcile)"
                self._file_event(pair, target, ERROR, str(exc))
            except Exception as exc:  # defensive: engine must never die
                self._file_event(pair, rel or "(reconcile)", ERROR,
                                 f"Unexpected error: {exc}")
            self._overall_event()

    # ---- the per-file operation ----------------------------------------
    def _process_file(self, pair: Pair, rel: str) -> None:
        """Sync ONE file: newer-wins with a kept conflicted copy."""
        local_path = os.path.join(pair.local, rel)
        state = load_state(pair)
        entry = state.get(rel)
        self._file_event(pair, rel, SYNCING, "")

        if not os.path.exists(local_path):
            # deleted locally → propagate (the file was synced before);
            # an unknown file that never synced is simply forgotten.
            if entry is not None:
                rclone.deletefile(pair.remote_file(rel))
                state.pop(rel, None)
                save_state(pair, state)
                self._file_event(pair, rel, SYNCED, "deleted")
            else:
                self.file_status.pop((pair.key, rel), None)
            return

    # -- conflict decision needs the remote's current idea of the file
        remote_info = rclone.stat_path(
            pair.remote, "/".join(p for p in (pair.rpath.strip("/"), rel) if p))
        local_m = os.path.getmtime(local_path)

        if remote_info is not None:
            remote_m = parse_mod_time(remote_info.mod_time) or 0.0
            known_rm = (entry or {}).get("rm")
            remote_changed = known_rm is not None and \
                remote_info.mod_time != known_rm
            never_synced = entry is None
            if never_synced:
                # No prior state means we have never seen this file before --
                # typically the first sync of a folder that already exists in
                # the cloud. That is NOT evidence that both sides changed, and
                # treating it as a conflict manufactures a "conflicted copy"
                # for every pre-existing file. A cloud object's ModTime is
                # when it was uploaded, so mtimes almost never agree and the
                # old size+2s test almost never spared anything.
                #
                # Only genuinely differing content is a conflict here; matching
                # size means adopt the remote's identity and move on.
                if remote_info.size == os.path.getsize(local_path):
                    self._record_synced(pair, rel, local_path, load_state(pair))
                    return
                self._resolve_conflict(pair, rel, local_path,
                                       local_m, remote_m)
                return
            if remote_changed:
                # We synced this before and the remote has moved since. That is
                # a real divergence only if the local side also changed; if it
                # did not, the remote simply wins and we download.
                local_changed = abs((entry or {}).get("lm", 0) - local_m) > 1e-6
                if not local_changed and remote_m >= local_m:
                    rclone.copyto(pair.remote_file(rel), local_path)
                    self._record_synced(pair, rel, local_path, load_state(pair))
                    return
                if not local_changed:
                    # The remote moved but is OLDER than what is on disk — a
                    # restore from an old backup, a clock-skewed writer, a
                    # rolled-back object. Downloading here would overwrite a
                    # newer local file with an older one and upload nothing:
                    # silent data loss, which is the one thing this engine
                    # promises never to do. Newer still wins, and the loser is
                    # kept as a conflicted copy.
                    self._resolve_conflict(pair, rel, local_path,
                                           local_m, remote_m)
                    return
                self._resolve_conflict(pair, rel, local_path,
                                       local_m, remote_m)
                return

        # no conflict: local version wins → upload
        rclone.copyto(local_path, pair.remote_file(rel))
        self._record_synced(pair, rel, local_path, state)

    def _resolve_conflict(self, pair: Pair, rel: str, local_path: str,
                          local_m: float, remote_m: float) -> None:
        """Newer wins; the losing version is KEPT as a conflicted copy."""
        remote_file = pair.remote_file(rel)
        if remote_m > local_m:
            # Remote is newer, so it takes the real name and the local edition
            # is kept alongside it.
            #
            # The incoming copy is fetched to a temporary file FIRST and only
            # swapped in once it is actually on disk. Moving the user's file
            # out of the way before the replacement exists means any failure --
            # a dropped connection, a deleted remote object, a full disk --
            # leaves nothing at the real name. That is how a private key went
            # missing: renamed away, and the download that was meant to replace
            # it never landed.
            incoming = local_path + INCOMING_SUFFIX
            try:
                rclone.copyto(remote_file, incoming)
                if not os.path.exists(incoming):
                    raise CloudSyncError("the remote copy did not download")
            except Exception:
                try:
                    os.unlink(incoming)
                except OSError:
                    pass
                # Nothing has moved: the local file is exactly as it was.
                self._file_event(pair, rel, ERROR,
                                 "could not fetch the newer cloud version — "
                                 "your local file is untouched")
                return
            keep = conflicted_name(local_path)
            os.replace(local_path, keep)      # only now, with the swap ready
            os.replace(incoming, local_path)
            # the conflicted copy is a new local file — sync it too
            rel_keep = os.path.relpath(keep, pair.local).replace(os.sep, "/")
            rclone.copyto(keep, pair.remote_file(rel_keep))
            state = load_state(pair)
            self._record_synced(pair, rel_keep, keep, state, quiet=True)
            self._record_synced(pair, rel, local_path, state)
            self._file_event(pair, rel, CONFLICT,
                             "remote was newer — kept your version as "
                             + os.path.basename(keep))
        else:
            # local is newer: preserve the remote edition locally, then upload
            keep = conflicted_name(local_path)
            rclone.copyto(remote_file, keep)
            rel_keep = os.path.relpath(keep, pair.local).replace(os.sep, "/")
            rclone.copyto(keep, pair.remote_file(rel_keep))
            rclone.copyto(local_path, remote_file)
            state = load_state(pair)
            self._record_synced(pair, rel_keep, keep, state, quiet=True)
            self._record_synced(pair, rel, local_path, state)
            self._file_event(pair, rel, CONFLICT,
                             "kept the cloud version as "
                             + os.path.basename(keep))

    def _record_synced(self, pair: Pair, rel: str, local_path: str,
                       state: Dict[str, Dict], quiet: bool = False) -> None:
        if not os.path.exists(local_path):
            # Recording lm=0/size=0 for a missing file poisons the state: the
            # remote then looks newer forever and the file re-conflicts on
            # every pass. Leave the previous entry alone and report it instead.
            self._file_event(pair, rel, ERROR,
                             "local file disappeared before it could be recorded")
            return
        info = rclone.stat_path(
            pair.remote, "/".join(p for p in (pair.rpath.strip("/"), rel) if p))
        state[rel] = {
            "lm": os.path.getmtime(local_path),
            "rm": info.mod_time if info else "",
            "size": os.path.getsize(local_path),
        }
        save_state(pair, state)
        if not quiet:
            self._file_event(pair, rel, SYNCED, "")

    # ---- reconcile (full scan; scheduled runs + realtime housekeeping) --
    def _reconcile_pair(self, pair: Pair) -> None:
        if not os.path.isdir(pair.local):
            raise CloudSyncError(f"Local folder missing: {pair.local}")
        # Clear conflicted copies that are not actually conflicts before
        # scanning, or they get treated as ordinary new files and uploaded.
        try:
            restored, removed, kept = reconcile_conflicts(pair.local)
            if restored or removed:
                self._notify({"kind": "info", "pair": pair,
                              "text": f"tidied {restored + removed} conflicted "
                                      f"copy(ies)"})
            for path in kept:
                self._file_event(
                    pair,
                    os.path.relpath(path, pair.local).replace(os.sep, "/"),
                    CONFLICT,
                    "both versions differ — keeping this copy for you to check")
        except Exception:
            pass          # tidying must never stop a sync

        state = load_state(pair)
        local_files = {}
        for full, rel in iter_local_files(pair.local):
            try:
                local_files[rel] = os.path.getmtime(full)
            except OSError:
                continue

        # local news: new files or changed mtimes vs the recorded state
        for rel, lm in local_files.items():
            entry = state.get(rel)
            if entry is None or abs(entry.get("lm", 0) - lm) > 1e-6:
                self._enqueue(pair, rel)

        # remote news: changed or brand-new remote files → download;
        # remotely-deleted files that we synced before → delete locally? NO:
        # conservative — a remote deletion never deletes local data here,
        # the file simply re-uploads on the next local change/scan.
        try:
            remote_entries = rclone.list_recursive(pair.remote, pair.rpath)
        except CloudSyncError:
            remote_entries = []
        for e in remote_entries:
            rel = e.path
            if should_ignore(rel):
                continue
            entry = state.get(rel)
            local_path = os.path.join(pair.local, rel)
            if rel not in local_files:
                # new on the remote → download it
                self._file_event(pair, rel, SYNCING, "downloading")
                os.makedirs(os.path.dirname(local_path) or pair.local,
                            exist_ok=True)
                rclone.copyto(pair.remote_file(rel), local_path)
                self._record_synced(pair, rel, local_path, state)
            elif entry is not None and e.mod_time != entry.get("rm") and \
                    abs(state.get(rel, {}).get("lm", 0)
                        - local_files[rel]) <= 1e-6:
                # remote changed while local did not → the remote wins cleanly
                self._file_event(pair, rel, SYNCING, "downloading update")
                rclone.copyto(pair.remote_file(rel), local_path)
                self._record_synced(pair, rel, local_path, state)

    # ---- realtime: watchdog / polling ----------------------------------
    def _start_watchdog(self) -> bool:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except Exception:
            return False

        engine = self

        class _Handler(FileSystemEventHandler):
            """Filesystem events, filtered before they cost anything.

            The exclusion list was applied when scanning but not to events, so
            everything inside .git, __pycache__, node_modules and every .tmp
            was queued -- and each queued path spawns an rclone process to
            stat the remote. One `git status` or Python run produced hundreds
            of them, which is why the machine ran hot while apparently idle.
            """

            def __init__(self, pair):
                self.pair = pair

            def _rel(self, src):
                try:
                    rel = os.path.relpath(src, self.pair.local).replace(
                        os.sep, "/")
                except Exception:
                    return None
                if rel.startswith("..") or should_ignore(rel):
                    return None
                return rel

            def on_created(self, event):
                if not event.is_directory:
                    rel = self._rel(event.src_path)
                    if rel:
                        engine.queue_change(self.pair, rel)

            on_modified = on_created

            def on_moved(self, event):
                if not event.is_directory:
                    for p in (event.src_path, event.dest_path):
                        rel = self._rel(p)
                        if rel:
                            engine.queue_change(self.pair, rel)

            def on_deleted(self, event):
                if not event.is_directory:
                    rel = self._rel(event.src_path)
                    if rel:
                        engine.queue_change(self.pair, rel)

        try:
            obs = Observer()
            for pair in self.pairs:
                if not os.path.isdir(pair.local):
                    continue
                # One recursive watch per pair. Scheduling each subdirectory
                # separately to skip excluded trees costs an emitter *thread*
                # per call -- six pairs became eighty-eight threads, and the
                # scheduling overhead outweighed the watches it avoided.
                # Filtering in the handler is what actually removes the work.
                obs.schedule(_Handler(pair), pair.local, recursive=True)
            obs.daemon = True
            obs.start()
            self._observer = obs
            return True
        except Exception:
            return False

    def _poll_loop(self):
        """Realtime fallback without watchdog: scan mtimes every few seconds."""
        snapshots: Dict[str, Dict[str, float]] = {}
        while not self._stop.is_set():
            for pair in list(self.pairs):
                if not os.path.isdir(pair.local):
                    continue
                snap = {}
                for full, rel in iter_local_files(pair.local):
                    try:
                        snap[rel] = os.path.getmtime(full)
                    except OSError:
                        continue
                prev = snapshots.get(pair.key)
                if prev is not None:
                    for rel, m in snap.items():
                        if prev.get(rel) != m:
                            self.queue_change(pair, rel)
                    for rel in set(prev) - set(snap):
                        self.queue_change(pair, rel)
                snapshots[pair.key] = snap
            for _ in range(POLL_SECONDS * 2):
                if self._stop.is_set():
                    return
                time.sleep(0.5)

    def _reconcile_loop(self):
        """Realtime housekeeping: periodic remote reconcile."""
        while not self._stop.is_set():
            for _ in range(RECONCILE_SECONDS * 2):
                if self._stop.is_set():
                    return
                time.sleep(0.5)
            if not self._paused.is_set():
                self.sync_now()

    def _schedule_loop(self):
        """Scheduled mode: interval minutes, or daily at HH:MM."""
        while not self._stop.is_set():
            if self._daily_at:
                wait = seconds_until_daily(*self._daily_at)
            else:
                wait = self._interval * 60
            end = time.time() + wait
            while time.time() < end:
                if self._stop.is_set():
                    return
                time.sleep(0.5)
            if not self._paused.is_set():
                self.sync_now()
