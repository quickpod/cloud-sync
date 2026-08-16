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

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
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

# Transient/junk files that editors and browsers leave around.
IGNORE_SUFFIXES = (".tmp", ".temp", ".part", ".partial", ".swp", ".swx",
                   ".crdownload", ".download")
IGNORE_PREFIXES = ("~$", ".~", ".#")


def should_ignore(name: str) -> bool:
    """True for transient files that must never be synced (pure)."""
    base = os.path.basename(name)
    if not base:
        return True
    low = base.lower()
    return low.endswith(IGNORE_SUFFIXES) or \
        any(base.startswith(p) for p in IGNORE_PREFIXES)


@dataclass(frozen=True)
class Pair:
    """One synced folder: a local directory mapped onto ``remote:path``."""

    local: str
    remote: str
    rpath: str = ""

    @property
    def key(self) -> str:
        """A filesystem-safe identity used for the state file name."""
        raw = f"{os.path.abspath(self.local)}|{self.remote}|{self.rpath}"
        import hashlib
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

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
            if remote_changed or never_synced:
                same_size = remote_info.size == os.path.getsize(local_path)
                if never_synced and same_size and abs(remote_m - local_m) < 2:
                    pass          # identical enough — just record the state
                else:
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
            # remote is newer: local edition becomes the conflicted copy
            keep = conflicted_name(local_path)
            os.replace(local_path, keep)
            rclone.copyto(remote_file, local_path)
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
        info = rclone.stat_path(
            pair.remote, "/".join(p for p in (pair.rpath.strip("/"), rel) if p))
        state[rel] = {
            "lm": os.path.getmtime(local_path) if os.path.exists(local_path) else 0,
            "rm": info.mod_time if info else "",
            "size": os.path.getsize(local_path) if os.path.exists(local_path) else 0,
        }
        save_state(pair, state)
        if not quiet:
            self._file_event(pair, rel, SYNCED, "")

    # ---- reconcile (full scan; scheduled runs + realtime housekeeping) --
    def _reconcile_pair(self, pair: Pair) -> None:
        if not os.path.isdir(pair.local):
            raise CloudSyncError(f"Local folder missing: {pair.local}")
        state = load_state(pair)
        local_files = {}
        for root, _dirs, files in os.walk(pair.local):
            for fn in files:
                if should_ignore(fn):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, pair.local).replace(os.sep, "/")
                local_files[rel] = os.path.getmtime(full)

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
            def __init__(self, pair):
                self.pair = pair

            def _rel(self, src):
                try:
                    return os.path.relpath(src, self.pair.local).replace(
                        os.sep, "/")
                except Exception:
                    return None

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
                if os.path.isdir(pair.local):
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
                for root, _dirs, files in os.walk(pair.local):
                    for fn in files:
                        if should_ignore(fn):
                            continue
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, pair.local).replace(
                            os.sep, "/")
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
