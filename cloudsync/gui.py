#!/usr/bin/env python3
r"""Cloud Sync -- an Aura (QuickOpen design system) GUI over ``cloudsync``.

Layout per branding/aura-design-system/APP-LAYOUT-LANGUAGE.md, benchmarked
against Dropbox/OneDrive continuous sync.  Sections:

* **Synced folders** (the hero view) -- folder pairs that stay in sync via
  :mod:`cloudsync.syncengine`: realtime watching by default (scheduled every
  N minutes / daily at HH:MM as the alternative), a per-pair state column, a
  live activity feed with per-file ✓/↻/⚠ status, newer-wins conflicts that
  keep a Dropbox-style conflicted copy, pause/resume, and a live overall
  status chip in the header.
* **Remotes** -- a OneDrive-style list of your configured clouds with an
  inline "add / edit remote" form (name, provider, endpoint, region, access
  keys).  You enter everything yourself; the app connects to nothing here.
* **Browse** -- pick a remote and walk its buckets/folders (user-initiated).
* **Transfer** -- one-off: a local folder up, down or both ways, with a
  dry-run preview.
* **Status** -- rclone availability/version, the platform, and the config path.

Every operation calls the tested core library (never re-implements the logic)
and runs on a background thread so the UI stays responsive; results are
marshalled back with ``self.after`` and shown in the Aura status bar -- a
summary on success, or the :class:`CloudSyncError` message (never a traceback)
on failure.

Design goals baked in (mirrors the QuickOpen house style):
  * built on the vendored ``cloudsync/aura.py`` design system.  Runtime deps:
    ``customtkinter`` (+ ``darkdetect``); the PyInstaller build adds
    ``--collect-all customtkinter``.
  * Importing this module does nothing.  Only :func:`main` builds a root
    window, and it degrades gracefully (prints a message, returns 0) with no
    display or with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the
    exe directory when ``sys.frozen`` is set -- never ``__file__``.
  * Offline-first: no telemetry, no dial-home.  The only network calls are the
    ones the user explicitly triggers (Test, Browse, Sync).

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (packaging, headless CI) never fails.

APP_NAME = "Cloud Sync"
APP_VERSION = "1.2.0"
WINDOW_TITLE = "Cloud Sync — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#5b86f7"      # Aura brand accent (the old per-app cyan was a
                        # legacy scaffold accent)

VIEWS = [
    ("folders", "Synced folders", "⇅"),
    ("remotes", "Remotes", "☁"),
    ("browse", "Browse", "▤"),
    ("sync", "Transfer", "→"),
    ("status", "Status", "◉"),
    ("help", "Help", "?"),
    ("about", "About", "ℹ"),
]

VIEW_DESCRIPTIONS = {
    "folders": "Folders that stay in sync with your cloud — realtime by "
               "default, scheduled if you prefer. Newer file wins; the other "
               "version is kept as a conflicted copy.",
    "remotes": "Configure your S3 / cloud remotes. You supply each endpoint, "
               "bucket and access key — nothing connects on its own.",
    "browse": "Walk a remote's buckets and folders. Runs only when you ask it to.",
    "sync": "One-off transfer: sync a local folder up, down or both ways. "
            "Preview with a dry run before applying.",
    "status": "rclone availability, your platform, and where the config lives.",
    "help": "How to connect each cloud, what the Bucket field is for, and "
            "what the common errors mean.",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
#: Set by :func:`main` from ``--minimized``: start hidden in the tray.
START_MINIMIZED = False


def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build."""
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
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def open_in_file_manager(path):
    """Best-effort 'reveal in file manager', guarded on every platform."""
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if hasattr(os, "startfile"):          # Windows
            os.startfile(folder)              # noqa: S606 - intended
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import customtkinter as ctk

    from . import aura, autostart, guiconfig, paths, tray
    from .errors import CloudSyncError
    from . import rclone, syncengine
    from .rclone import (
        PROVIDERS, PROVIDERS_BY_KEY, get_provider, human_size, remote_path,
    )
    from .syncengine import Pair, SyncEngine, pair_from_dict

    PROVIDER_LABELS = [p.label for p in PROVIDERS]
    LABEL_TO_KEY = {p.label: p.key for p in PROVIDERS}

    # ---- reusable local-folder picker row ------------------------------
    class FolderRow(ctk.CTkFrame):
        """A labelled 'local folder + Browse' row that remembers recent picks."""

        def __init__(self, master, label):
            super().__init__(master, fg_color="transparent")
            aura.SectionLabel(self, label).pack(anchor="w", pady=(0, 3))
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x")
            self.entry = aura.AuraEntry(row, placeholder="Choose a local folder…")
            self.entry.pack(side="left", fill="x", expand=True)
            aura.AuraButton(row, "Browse…", kind="secondary",
                            command=self._browse).pack(side="left", padx=(8, 0))

        def _browse(self):
            initial = self.get() or str(paths.default_local_dir())
            chosen = filedialog.askdirectory(initialdir=initial, mustexist=True)
            if chosen:
                self.set(chosen)

        def get(self):
            return self.entry.get().strip()

        def set(self, value):
            self.entry.delete(0, "end")
            if value:
                self.entry.insert(0, value)
                guiconfig.add_recent(value)

    class App(aura.AuraApp):
        def __init__(self):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("cloud-sync.png"), version=APP_VERSION,
                tagline="offline-first",
                on_theme_change=guiconfig.set_theme,
                size=(1080, 720), min_size=(920, 620))

            self._busy = False
            self._img_refs_gui = []
            self._edit_name = None          # remote being edited (else None)
            self._browse_history = []       # path breadcrumbs for the Up button
            self.engine = None              # the continuous-sync engine
            self._activity = []             # newest-first activity rows
            self._tray = None               # tray indicator (None when absent)
            self._hidden = False            # window hidden but still syncing
            self._quitting = False          # a real quit, not a hide-to-tray

            self._set_icon()
            self._build_menu()

            self.progress = aura.ProgressBar(self.statusbar.actions,
                                             mode="indeterminate", width=140)
            # live overall-status chip (header, window-global)
            self._chip = aura.Caption(self.header_actions, "")
            self._chip.pack(side="left", padx=(0, 10))

            for vid, label, glyph in VIEWS:
                self.add_section(vid, label, glyph,
                                 getattr(self, "_build_" + vid))
            self.show("folders")
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(200, self._start_engine)
            self._start_tray()
            if START_MINIMIZED:
                # Launched at login: go straight to the tray. Without a tray
                # there would be no way back, so only hide when one exists.
                self.after(300, self._hide_to_tray_if_possible)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("cloud-sync.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("cloud-sync.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs_gui.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Add synced folder…", accelerator="Ctrl+N",
                              command=lambda: (self.show("folders"),
                                               self._pair_dialog()))
            filem.add_command(label="Sync now", accelerator="F5",
                              command=self._sync_now)
            filem.add_separator()
            filem.add_command(label="Open config folder",
                              command=self._open_config_folder)
            filem.add_command(label="Settings…", accelerator="Ctrl+,",
                              command=self._open_settings)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)
            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(label="Toggle sidebar", accelerator="Ctrl+\\",
                              command=self.toggle_sidebar)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)
            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="Help topics", accelerator="F1",
                              command=lambda: self.show("help"))
            helpm.add_command(label="About",
                              command=lambda: self.show("about"))
            bar.add_cascade(label="Help", menu=helpm)
            self.bind_all("<F1>", lambda e: self.show("help"))
            try:
                self.config(menu=bar)
            except Exception:
                pass
            self.bind_all("<Control-n>",
                          lambda e: (self.show("folders"),
                                     self._pair_dialog(), "break")[2])
            self.bind_all("<F5>", lambda e: self._sync_now())
            self.bind_all("<Control-comma>",
                          lambda e: (self._open_settings(), "break")[1])

        def _open_config_folder(self):
            paths.ensure_config_dir()
            open_in_file_manager(str(paths.config_dir()))

        # ---- navigation: refresh the section being shown
        def show(self, sid):
            super().show(sid)
            self.set_status("Ready")
            try:
                if sid == "folders":
                    self._folders_refresh()
                elif sid == "remotes":
                    self._remotes_refresh()
                elif sid == "browse":
                    self._browse_refresh_remotes()
                elif sid == "sync":
                    self._sync_refresh_remotes()
                elif sid == "status":
                    self._status_refresh()
            except Exception:
                pass

        # ===============================================================
        # The continuous-sync engine (realtime / scheduled)
        # ===============================================================
        def _config_pairs(self):
            out = []
            for d in guiconfig.get_pairs():
                p = pair_from_dict(d)
                if p is not None:
                    out.append(p)
            return out

        def _start_engine(self):
            """(Re)start the engine from the persisted pairs + mode."""
            self._stop_engine()
            pairs = self._config_pairs()
            self.engine = SyncEngine(pairs, notify=self._engine_notify)
            mode = guiconfig.get_sync_mode()
            daily = guiconfig.get_daily_at()
            if pairs and rclone.rclone_available():
                self.engine.start(
                    syncengine.REALTIME if mode == "realtime"
                    else syncengine.SCHEDULED,
                    interval_minutes=guiconfig.get_interval_minutes(),
                    daily_at=daily or None)
                if guiconfig.get_paused():
                    self.engine.pause()
            self._update_chip()
            self._update_pause_btn()

        def _stop_engine(self):
            if self.engine is not None:
                try:
                    self.engine.stop()
                except Exception:
                    pass
                self.engine = None

        def _engine_notify(self, event):
            """Engine thread → UI thread."""
            try:
                self.after(0, lambda: self._engine_event(event))
            except Exception:
                pass

        def _engine_event(self, event):
            if event.get("kind") == "file":
                pair, rel = event["pair"], event["rel"]
                status, detail = event["status"], event.get("detail", "")
                self._activity = [
                    a for a in self._activity
                    if not (a[0] == pair.key and a[1] == rel)]
                import time as _t
                self._activity.insert(0, (pair.key, rel, status, detail,
                                          pair, _t.time()))
                del self._activity[120:]
                self._render_activity()
            self._update_chip(event if event.get("kind") == "overall" else None)
            self._folders_refresh_status()

        def _update_chip(self, overall=None):
            eng = self.engine
            if eng is None or not self._config_pairs():
                text, color = "", None
            elif overall is not None:
                s = overall.get("status")
                if s == "paused":
                    text, color = "⏸  Paused", "muted"
                elif s == "syncing":
                    text, color = f"↻  Syncing ({overall.get('pending', 0)})", "accent"
                elif s == "error":
                    text, color = f"⚠  {overall.get('errors', 0)} error(s)", "danger"
                else:
                    text, color = "●  All synced", "ok"
            elif eng.paused:
                text, color = "⏸  Paused", "muted"
            else:
                text, color = "●  Watching" if eng.mode == syncengine.REALTIME \
                    else "◷  Scheduled", "ok"
            try:
                self._chip.configure(
                    text=text,
                    text_color=aura.P(color) if color else aura.P("muted"))
            except Exception:
                pass
            self._sync_tray_state(overall)

        def _sync_now(self):
            if self.engine is None or not self._config_pairs():
                self.set_error("Add a synced folder first.")
                return
            if not rclone.rclone_available():
                self.set_error(rclone.NO_RCLONE_MSG)
                return
            if self.engine.mode is None:
                self._start_engine()
            self.engine.sync_now()
            self.set_status("Sync requested.", kind="working")

        def _toggle_pause(self):
            if self.engine is None:
                return
            if self.engine.paused:
                self.engine.resume()
                guiconfig.set_paused(False)
            else:
                self.engine.pause()
                guiconfig.set_paused(True)
            self._update_pause_btn()
            self._update_chip()

        def _update_pause_btn(self):
            try:
                paused = self.engine is not None and self.engine.paused
                self._pause_btn.configure(
                    text="▶ Resume" if paused else "⏸ Pause")
            except Exception:
                pass

        # ===============================================================
        # Synced folders section (the Dropbox-style hero view)
        # ===============================================================
        def _build_folders(self, frame):
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)
            frame.grid_rowconfigure(3, weight=2)

            tb = aura.Toolbar(frame)
            tb.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            tb.add_button("＋ Add folder", self._pair_dialog, kind="primary")
            self._pause_btn = tb.add_button("⏸ Pause", self._toggle_pause)
            tb.add_button("⟳ Sync now", self._sync_now, kind="ghost",
                          tooltip="Reconcile every synced folder now (F5)")
            self._mode_cap = aura.Caption(tb, "")
            tb.add_right(self._mode_cap)

            pwrap = ctk.CTkFrame(frame, fg_color=aura._pair("surface"),
                                 corner_radius=10, border_width=1,
                                 border_color=aura._pair("border"))
            pwrap.grid(row=1, column=0, sticky="nsew")
            self._pairs_wrap = pwrap
            cols = ("local", "remote", "state")
            self._pairs_tree = ttk.Treeview(pwrap, columns=cols,
                                            show="headings",
                                            selectmode="browse", height=4)
            self._pairs_tree.heading("local", text="Local folder", anchor="w")
            self._pairs_tree.heading("remote", text="Remote", anchor="w")
            self._pairs_tree.heading("state", text="State", anchor="w")
            self._pairs_tree.column("local", width=320, stretch=True)
            self._pairs_tree.column("remote", width=260, stretch=True)
            self._pairs_tree.column("state", width=130, stretch=False)
            psb = aura.AuraScrollbar(pwrap, command=self._pairs_tree.yview)
            self._pairs_tree.configure(yscrollcommand=psb.set)
            psb.pack(side="right", fill="y", padx=(0, 4), pady=6)
            self._pairs_tree.pack(side="left", fill="both", expand=True,
                                  padx=(6, 0), pady=6)
            self._pairs_tree.bind("<Button-3>", self._show_pair_menu)
            self._pairs_tree.bind("<Delete>", lambda e: self._remove_pair())
            self._pair_menu = tk.Menu(self, tearoff=0)
            aura.track(self._pair_menu, "menu")

            self._act_label = aura.SectionLabel(frame, "Activity")
            self._act_label.grid(row=2, column=0, sticky="w", pady=(12, 4))
            awrap = ctk.CTkFrame(frame, fg_color=aura._pair("surface"),
                                 corner_radius=10, border_width=1,
                                 border_color=aura._pair("border"))
            awrap.grid(row=3, column=0, sticky="nsew")
            self._act_wrap = awrap
            cols = ("st", "file", "detail", "when")
            self._act_tree = ttk.Treeview(awrap, columns=cols,
                                          show="headings",
                                          selectmode="browse")
            self._act_tree.heading("st", text="")
            self._act_tree.heading("file", text="File", anchor="w")
            self._act_tree.heading("detail", text="Detail", anchor="w")
            self._act_tree.heading("when", text="When", anchor="e")
            self._act_tree.column("st", width=36, minwidth=32,
                                  anchor="center", stretch=False)
            self._act_tree.column("file", width=320, stretch=True)
            self._act_tree.column("detail", width=260, stretch=True)
            self._act_tree.column("when", width=76, minwidth=64, anchor="e",
                                  stretch=False)
            asb = aura.AuraScrollbar(awrap, command=self._act_tree.yview)
            self._act_tree.configure(yscrollcommand=asb.set)
            asb.pack(side="right", fill="y", padx=(0, 4), pady=6)
            self._act_tree.pack(side="left", fill="both", expand=True,
                                padx=(6, 0), pady=6)
            self._style_activity_tags()

            self.empty_pairs = aura.EmptyState(
                frame, title="Nothing syncing yet",
                caption="Pick a local folder and a remote — Cloud Sync keeps "
                        "them in step in realtime (or on your schedule), and "
                        "conflicts always keep both versions.",
                action_text="＋ Add synced folder", action=self._pair_dialog,
                image=(asset_path("assets/sync-empty-light.png"),
                       asset_path("assets/sync-empty-dark.png")))
            self._folders_refresh()

        def _style_activity_tags(self):
            try:
                self._act_tree.tag_configure(
                    "error", foreground=aura.P("danger"))
                self._act_tree.tag_configure(
                    "conflict", foreground=aura.P("warn"))
            except Exception:
                pass

        def _folders_refresh(self):
            if not hasattr(self, "_pairs_tree"):
                return
            pairs = self._config_pairs()
            self._pairs_tree.delete(*self._pairs_tree.get_children())
            for p in pairs:
                self._pairs_tree.insert(
                    "", "end", iid=p.key,
                    values=(p.local,
                            rclone.remote_path(p.remote, p.rpath), ""))
            if pairs:
                self.empty_pairs.place_forget()
                self._pairs_wrap.grid()
                self._act_label.grid()
                self._act_wrap.grid()
            else:
                self._pairs_wrap.grid_remove()
                self._act_label.grid_remove()
                self._act_wrap.grid_remove()
                self.empty_pairs.place(relx=0, rely=0.06, relwidth=1,
                                       relheight=0.9)
                self.empty_pairs.lift()
            self._folders_refresh_status()
            self._render_activity()
            mode = guiconfig.get_sync_mode()
            if mode == "realtime":
                self._mode_cap.configure(text="Realtime — watching for changes")
            else:
                daily = guiconfig.get_daily_at()
                self._mode_cap.configure(
                    text=("Scheduled daily at " + daily) if daily else
                    f"Scheduled every {guiconfig.get_interval_minutes()} min")

        def _folders_refresh_status(self):
            """Update the per-pair State column from the engine's file map."""
            if not hasattr(self, "_pairs_tree") or self.engine is None:
                return
            for p in self._config_pairs():
                if not self._pairs_tree.exists(p.key):
                    continue
                states = [s for (pk, _rel), (s, _d, _t)
                          in self.engine.file_status.items() if pk == p.key]
                if any(s == syncengine.ERROR for s in states):
                    txt = "⚠ attention"
                elif any(s in (syncengine.SYNCING, syncengine.PENDING)
                         for s in states):
                    txt = "↻ syncing"
                elif self.engine.paused:
                    txt = "⏸ paused"
                else:
                    txt = "✓ up to date"
                try:
                    self._pairs_tree.set(p.key, "state", txt)
                except Exception:
                    pass

        def _render_activity(self):
            if not hasattr(self, "_act_tree"):
                return
            import time as _t
            tree = self._act_tree
            tree.delete(*tree.get_children())
            now = _t.time()
            for pkey, rel, status, detail, pair, ts in self._activity[:80]:
                glyph = syncengine.STATE_GLYPH.get(status, "")
                age = now - ts
                when = "now" if age < 90 else (
                    "%dm" % (age // 60) if age < 3600 else "%dh" % (age // 3600))
                tags = ("error",) if status == syncengine.ERROR else (
                    ("conflict",) if status == syncengine.CONFLICT else ())
                tree.insert("", "end",
                            values=(glyph, rel, detail, when), tags=tags)

        def _show_pair_menu(self, event):
            iid = self._pairs_tree.identify_row(event.y)
            if not iid:
                return
            self._pairs_tree.selection_set(iid)
            m = self._pair_menu
            m.delete(0, "end")
            m.add_command(label="Sync now", command=self._sync_now)
            m.add_command(label="Open local folder",
                          command=self._open_pair_folder)
            m.add_separator()
            m.add_command(label="Remove…  (Del)", command=self._remove_pair)
            aura.style_menu(m)
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    m.grab_release()
                except Exception:
                    pass

        def _selected_pair(self):
            sel = self._pairs_tree.selection()
            if not sel:
                return None
            for p in self._config_pairs():
                if p.key == sel[0]:
                    return p
            return None

        def _open_pair_folder(self):
            p = self._selected_pair()
            if p:
                open_in_file_manager(p.local)

        def _remove_pair(self):
            p = self._selected_pair()
            if p is None:
                self.set_error("Select a synced folder to remove.")
                return
            if not messagebox.askyesno(
                    "Stop syncing",
                    f"Stop keeping\n  {p.local}\nin sync with\n  "
                    f"{rclone.remote_path(p.remote, p.rpath)}?\n\n"
                    "No files are deleted anywhere."):
                return
            guiconfig.remove_pair(p.local, p.remote, p.rpath)
            self._start_engine()
            self._folders_refresh()
            self.set_success("Stopped syncing that folder.")

        def _pair_dialog(self):
            names = self._all_remote_names()
            if not names:
                self.set_error("Add a remote first (Remotes tab).")
                self.show("remotes")
                return
            dlg = aura.Dialog(self, title="Add synced folder",
                              size=(520, 400))
            aura.Caption(dlg.body, "Local folder:").pack(anchor="w")
            row = ctk.CTkFrame(dlg.body, fg_color="transparent")
            row.pack(fill="x", pady=(4, 10))
            local_e = aura.AuraEntry(row, placeholder="Choose a folder…")
            local_e.pack(side="left", fill="x", expand=True)

            def browse():
                chosen = filedialog.askdirectory(
                    initialdir=str(paths.default_local_dir()), parent=dlg)
                if chosen:
                    local_e.delete(0, "end")
                    local_e.insert(0, chosen)
                    suggest_rpath()
            aura.AuraButton(row, "Browse…", kind="secondary", height=30,
                            command=browse).pack(side="left", padx=(8, 0))

            aura.Caption(dlg.body, "Remote:").pack(anchor="w")

            def suggest_rpath(_e=None):
                """Default the remote path to <bucket>/<folder name>.

                Pointing two folders at the same remote path makes each one
                see the other's files as missing locally and download them, so
                every folder ends up holding every folder's contents. Giving
                each its own subfolder by default is what stops that.
                """
                bucket = self._bucket_of(remote_o.get())
                local = paths.normalize_local(local_e.get().strip())
                leaf = os.path.basename(local.rstrip(os.sep)) if local else ""
                parts = [x for x in (bucket, leaf) if x]
                rpath_e.delete(0, "end")
                if parts:
                    rpath_e.insert(0, "/".join(parts) + ("/" if not leaf else ""))

            def remote_picked(name=None):
                suggest_rpath()

            remote_o = aura.AuraOption(dlg.body, values=names, width=220,
                                       command=remote_picked)
            remote_o.set(names[0])
            remote_o.pack(anchor="w", pady=(4, 10))
            aura.Caption(dlg.body, "Remote path (bucket/folder):").pack(
                anchor="w")
            rpath_e = aura.AuraEntry(dlg.body,
                                     placeholder="bucket/folder …")
            rpath_e.pack(fill="x", pady=(4, 0))
            aura.Caption(dlg.body,
                         "Each folder gets its own subfolder.").pack(
                anchor="w", pady=(4, 0))
            local_e.bind("<FocusOut>", suggest_rpath)
            remote_picked(names[0])

            def ok(_e=None):
                local = paths.normalize_local(local_e.get().strip())
                rp = rpath_e.get().strip()
                if not local or not os.path.isdir(local):
                    self.set_error("Choose an existing local folder.")
                    return
                remote_name = remote_o.get()
                clash = self._pair_target_clash(remote_name, rp, local)
                if clash:
                    self.set_error(
                        f"{clash} already syncs to that remote path. Two "
                        f"folders sharing one path copy each other's files — "
                        f"give this one its own subfolder.")
                    return
                dlg.close()
                guiconfig.add_pair(local, remote_name, rp)
                guiconfig.add_recent(local)
                self._start_engine()
                self._folders_refresh()
                self.set_success("Folder added — first sync starting.")

            dlg.add_button("Start syncing", ok)
            dlg.add_button("Cancel", dlg.close, kind="secondary")
            self.after(120, local_e.focus_set)

        def _pair_target_clash(self, remote, rpath, local):
            """Name of an existing pair already using this remote path, if any."""
            want = (rpath or "").strip("/")
            for entry in guiconfig.get_pairs():
                if entry.get("remote") != remote:
                    continue
                if (entry.get("rpath") or "").strip("/") != want:
                    continue
                if paths.normalize_local(entry.get("local", "")) == local:
                    continue        # editing the same pair
                return entry.get("local", "another folder")
            return None

        # ---- settings (Ctrl+,)
        def _open_settings(self):
            dlg = aura.Dialog(self, title="Settings", size=(560, 470))

            aura.SectionLabel(dlg.body, "Sync mode").pack(anchor="w",
                                                          pady=(0, 2))
            mrow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            mrow.pack(anchor="w", pady=(4, 6))
            mode_seg = aura.SegmentedControl(
                mrow, values=["Realtime", "Scheduled"], width=220,
                command=lambda v: apply_mode(v))
            mode_seg.set("Realtime" if guiconfig.get_sync_mode() == "realtime"
                         else "Scheduled")
            mode_seg.pack(side="left")
            aura.Caption(dlg.body,
                         "Realtime watches your folders and syncs changes "
                         "immediately (Dropbox-style).").pack(anchor="w")

            srow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            srow.pack(anchor="w", pady=(8, 2))
            aura.Caption(srow, "Scheduled:").pack(side="left", padx=(0, 8))
            ivals = ["Every 15 min", "Every 30 min", "Every hour",
                     "Every 4 hours", "Daily at…"]
            daily = guiconfig.get_daily_at()
            iv = guiconfig.get_interval_minutes()
            cur = "Daily at…" if daily else {15: "Every 15 min",
                                             30: "Every 30 min",
                                             60: "Every hour",
                                             240: "Every 4 hours"}.get(
                iv, "Every 30 min")
            int_o = aura.AuraOption(srow, values=ivals, width=140, height=30,
                                    command=lambda v: apply_sched(v))
            int_o.set(cur)
            int_o.pack(side="left")
            daily_e = aura.AuraEntry(srow, placeholder="HH:MM", width=70,
                                     height=30)
            if daily:
                daily_e.insert(0, daily)
            daily_e.pack(side="left", padx=(8, 0))

            def apply_mode(v):
                guiconfig.set_sync_mode(
                    "realtime" if v == "Realtime" else "scheduled")
                self._start_engine()
                self._folders_refresh()

            def apply_sched(v):
                mins = {"Every 15 min": 15, "Every 30 min": 30,
                        "Every hour": 60, "Every 4 hours": 240}.get(v)
                if mins:
                    guiconfig.set_daily_at("")
                    guiconfig.set_interval_minutes(mins)
                    self._start_engine()
                    self._folders_refresh()

            def apply_daily(_e=None):
                t = daily_e.get().strip()
                if t and syncengine.parse_daily_time(t):
                    guiconfig.set_daily_at(t)
                    int_o.set("Daily at…")
                    self._start_engine()
                    self._folders_refresh()
                elif t:
                    self.set_error("Daily time must be HH:MM (e.g. 21:30).")
            daily_e.bind("<Return>", apply_daily)
            daily_e.bind("<FocusOut>", apply_daily)

            aura.SectionLabel(dlg.body, "Background & startup").pack(
                anchor="w", pady=(14, 2))
            if tray.available():
                tray_sw = ctk.CTkSwitch(
                    dlg.body, text="Keep syncing in the tray when I close the "
                    "window", command=lambda: guiconfig.set_close_to_tray(
                        bool(tray_sw.get())))
                tray_sw.select() if guiconfig.get_close_to_tray() \
                    else tray_sw.deselect()
                tray_sw.pack(anchor="w", pady=(4, 2))

                start_sw = ctk.CTkSwitch(
                    dlg.body, text="Start hidden in the tray",
                    command=lambda: guiconfig.set_start_minimized(
                        bool(start_sw.get())))
                start_sw.select() if guiconfig.get_start_minimized() \
                    else start_sw.deselect()
                start_sw.pack(anchor="w", pady=(2, 2))
            else:
                aura.Caption(
                    dlg.body,
                    "This desktop has no system tray, so Cloud Sync stops "
                    "when you close the window.").pack(anchor="w")

            if autostart.is_supported():
                auto_sw = ctk.CTkSwitch(
                    dlg.body, text="Start Cloud Sync when I log in",
                    command=lambda: apply_autostart(bool(auto_sw.get())))
                # Reflect what is actually registered, not just our preference:
                # the user may have removed it with the desktop's own tool.
                auto_sw.select() if autostart.is_enabled() else auto_sw.deselect()
                auto_sw.pack(anchor="w", pady=(2, 2))

                def apply_autostart(flag):
                    if autostart.set_enabled(flag):
                        guiconfig.set_autostart(flag)
                        self.set_success("Cloud Sync will start at login."
                                         if flag else
                                         "Cloud Sync will no longer start at login.")
                    else:
                        auto_sw.deselect() if flag else auto_sw.select()
                        self.set_error("Could not change the startup setting.")

            aura.SectionLabel(dlg.body, "Appearance").pack(anchor="w",
                                                           pady=(14, 2))
            trow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            trow.pack(anchor="w", pady=(4, 2))
            aura.Caption(trow, "Theme").pack(side="left", padx=(0, 10))
            cur_t = guiconfig.get_theme()
            th = aura.AuraOption(trow, values=["System", "Light", "Dark"],
                                 width=110, height=30,
                                 command=self._set_theme_pref)
            th.set(cur_t.capitalize() if cur_t in ("light", "dark")
                   else "System")
            th.pack(side="left")
            aura.Caption(dlg.body,
                         "System follows the OS Aura Dark/Light live.").pack(
                anchor="w")

            aura.SectionLabel(dlg.body, "Data").pack(anchor="w", pady=(14, 2))
            aura.Caption(dlg.body, str(paths.config_dir())).pack(anchor="w")

            dlg.add_button("Close")

        def _set_theme_pref(self, choice):
            pref = str(choice).lower()
            if pref == "system":
                guiconfig.set_theme("system")
                self._follow_system = True
                if self._sys_listener is None:
                    self._start_system_listener()
                self.set_theme(aura._system_theme(), _system=True)
            elif pref in ("light", "dark"):
                self.set_theme(pref)     # persists via on_theme_change

        # ---- theme: keep tracked tags + chip in sync
        def set_theme(self, theme, _system=False):
            super().set_theme(theme, _system=_system)
            try:
                self._style_activity_tags()
                self._update_chip()
            except Exception:
                pass

        # ===============================================================
        # About section
        # ===============================================================
        def _build_about(self, frame):
            card = aura.Card(frame, title="About Cloud Sync")
            card.pack(fill="x")
            aura.Heading(card.body, APP_NAME).pack(anchor="w")
            aura.Caption(card.body, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                card.body, font=aura.font(), justify="left", anchor="w",
                wraplength=560,
                text="Offline-first S3 / cloud file sync, backed by rclone. "
                     "Keep folders continuously in sync (realtime or "
                     "scheduled) with per-file status and Dropbox-style "
                     "conflicted copies — data is never silently lost.\n\n"
                     "You configure every remote yourself; nothing dials "
                     "home. 100% AI-built, open source, published on "
                     "QuickOpen.").pack(anchor="w")
            aura.Caption(card.body,
                         "Shortcuts: Ctrl+N add synced folder · F5 sync now "
                         "· Ctrl+, settings · Ctrl+\\ sidebar").pack(
                anchor="w", pady=(10, 0))
            aura.Caption(card.body,
                         "Licensed under Apache-2.0. Built on rclone (MIT), "
                         "watchdog (Apache-2.0) and CustomTkinter (MIT)."
                         ).pack(anchor="w", pady=(10, 4))
            def _open_site():
                try:
                    import webbrowser
                    webbrowser.open(PROJECT_URL)
                except Exception:
                    pass
            aura.AuraButton(card.body, "Project page: quickopen.ai",
                            kind="ghost", command=_open_site).pack(
                anchor="w", pady=(6, 0))

        # ---- background operation runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            if self._busy:
                self.set_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self.set_status(busy, kind="working")
            try:
                self.progress.pack(side="left", padx=(8, 4), pady=6)
                self.progress.start()
            except Exception:
                pass

            def run():
                try:
                    res, err = work(), None
                except CloudSyncError as ex:
                    res, err = None, str(ex)
                except Exception as ex:  # never leak a traceback
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                try:
                    self.progress.stop()
                    self.progress.pack_forget()
                except Exception:
                    pass
                if button is not None:
                    try:
                        button.state(["!disabled"])
                    except Exception:
                        pass
                if err is not None:
                    self.set_error(err)
                    return
                self.set_status("Done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self.set_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ===============================================================
        # Remotes section
        # ===============================================================
        def _build_remotes(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["remotes"],
                         wraplength=780, justify="left").pack(
                anchor="w", pady=(0, 10))

            top = ctk.CTkFrame(frame, fg_color="transparent")
            top.pack(fill="x")
            self._remotes_add_btn = aura.AuraButton(
                top, "＋ Add remote", kind="primary",
                command=lambda: self._remote_show_form(None))
            self._remotes_add_btn.pack(side="left")
            aura.AuraButton(top, "Refresh", kind="secondary",
                            command=self._remotes_refresh).pack(side="left", padx=8)

            # list view (cards) and form view live in the same content area
            self._remotes_list = ctk.CTkScrollableFrame(
                frame, fg_color="transparent")
            self._remotes_list.pack(fill="both", expand=True, pady=(12, 0))

            self._remote_form = ctk.CTkFrame(frame, fg_color="transparent")
            self._build_remote_form(self._remote_form)

        def _build_remote_form(self, parent):
            card = aura.Card(parent, title="Remote details", padding=16)
            card.pack(fill="both", expand=True)
            body = card.body

            def field(label):
                aura.SectionLabel(body, label).pack(anchor="w", pady=(8, 2))
                e = aura.AuraEntry(body)
                e.pack(fill="x")
                return e

            self._f_name = field("Name (e.g. work-s3)")

            aura.SectionLabel(body, "Provider").pack(anchor="w", pady=(8, 2))
            self._f_provider = aura.AuraOption(
                body, values=PROVIDER_LABELS, command=self._remote_provider_changed)
            self._f_provider.pack(fill="x")

            self._f_access = field("Access key ID")

            aura.SectionLabel(body, "Secret access key").pack(anchor="w", pady=(8, 2))
            self._f_secret = aura.AuraEntry(body, show="•")
            self._f_secret.pack(fill="x")
            self._secret_hint = aura.Caption(body, "")
            self._secret_hint.pack(anchor="w", pady=(2, 0))

            self._f_endpoint = field("Endpoint URL (host only — no bucket, no path)")
            self._endpoint_hint = aura.Caption(body, "", wraplength=640,
                                               justify="left")
            self._endpoint_hint.pack(anchor="w", pady=(2, 0))

            self._f_bucket = field(
                "Bucket — required if your credentials are scoped to one bucket")
            self._bucket_hint = aura.Caption(
                body,
                "Optional otherwise. With a bucket set, Test and Browse start "
                "inside that bucket (bucket-scoped keys — e.g. a Cloudflare R2 "
                "token limited to one bucket — cannot list the account root).",
                wraplength=640, justify="left")
            self._bucket_hint.pack(anchor="w", pady=(2, 0))

            self._f_region = field("Region (optional)")

            btns = ctk.CTkFrame(body, fg_color="transparent")
            btns.pack(fill="x", pady=(16, 0))
            self._remote_save_btn = aura.AuraButton(
                btns, "Save remote", kind="primary", command=self._remote_save)
            self._remote_save_btn.pack(side="left")
            aura.AuraButton(btns, "Cancel", kind="secondary",
                            command=self._remote_hide_form).pack(side="left", padx=8)

        def _remote_provider_changed(self, label=None):
            key = LABEL_TO_KEY.get(self._f_provider.get(), "other")
            prov = PROVIDERS_BY_KEY.get(key)
            if not prov:
                return
            hint = (prov.endpoint_hint or "https://s3.example.com") \
                if prov.needs_endpoint else "(not needed — AWS is region-addressed)"
            self._endpoint_hint.configure(text=hint + ("  " + prov.note if prov.note else ""))
            # prefill region default if empty
            if not self._f_region.get().strip() and prov.default_region:
                self._f_region.delete(0, "end")
                self._f_region.insert(0, prov.default_region)

        def _remote_show_form(self, remote):
            self._edit_name = remote.name if remote else None
            for e in (self._f_name, self._f_access, self._f_secret,
                      self._f_endpoint, self._f_bucket, self._f_region):
                e.delete(0, "end")
            if remote is not None:
                self._f_name.insert(0, remote.name)
                self._f_provider.set(remote.provider_label())
                self._f_access.insert(0, remote.access_key_id)
                if remote.endpoint:
                    self._f_endpoint.insert(0, remote.endpoint)
                if remote.bucket:
                    self._f_bucket.insert(0, remote.bucket)
                if remote.region:
                    self._f_region.insert(0, remote.region)
                self._secret_hint.configure(
                    text="Leave blank to keep the saved secret.")
            else:
                self._f_provider.set(PROVIDER_LABELS[0])
                self._secret_hint.configure(text="")
            self._remote_provider_changed()
            self._remotes_list.pack_forget()
            self._remote_form.pack(fill="both", expand=True, pady=(12, 0))
            try:
                self._remotes_add_btn.state(["disabled"])
            except Exception:
                pass

        def _remote_hide_form(self):
            self._remote_form.pack_forget()
            self._remotes_list.pack(fill="both", expand=True, pady=(12, 0))
            try:
                self._remotes_add_btn.state(["!disabled"])
            except Exception:
                pass

        def _sanitize_endpoint_field(self):
            """Strip any path off the endpoint entry; return (clean, notice)."""
            raw = self._f_endpoint.get().strip()
            clean, stripped = rclone.sanitize_endpoint(raw)
            if stripped:
                self._f_endpoint.delete(0, "end")
                self._f_endpoint.insert(0, clean)
                notice = (f"Removed “/{stripped}” from the endpoint — bucket "
                          "names go in the Bucket field, not the endpoint.")
                self._endpoint_hint.configure(text=notice)
                return clean, notice
            return clean, ""

        def _remote_save(self):
            name = self._f_name.get().strip()
            label = self._f_provider.get()
            key = LABEL_TO_KEY.get(label, "other")
            access = self._f_access.get().strip()
            secret = self._f_secret.get()
            endpoint, ep_notice = self._sanitize_endpoint_field()
            bucket = self._f_bucket.get().strip().strip("/")
            region = self._f_region.get().strip()
            editing = self._edit_name is not None

            # When editing and the secret is left blank, reuse the stored one.
            if editing and not secret:
                try:
                    secret = rclone.get_remote(self._edit_name).params.get(
                        "secret_access_key", "")
                    # Reuse the stored key as-is; it is already the literal
                    # secret, so it needs no further encoding.
                    return self._remote_save_keep_secret(
                        name, key, access, secret, endpoint, bucket, region,
                        ep_notice)
                except CloudSyncError:
                    pass

            def work():
                return rclone.add_remote(name, key, access, secret,
                                         endpoint=endpoint, region=region,
                                         bucket=bucket, overwrite=editing)

            def done(remote):
                if editing and self._edit_name != remote.name:
                    try:
                        rclone.delete_remote(self._edit_name)
                    except CloudSyncError:
                        pass
                self.set_success(f"Saved remote “{remote.name}”."
                                 + (f"  {ep_notice}" if ep_notice else ""))
                self._remote_hide_form()
                self._remotes_refresh()

            self._bg(work, done, button=self._remote_save_btn, busy="Saving…")

        def _remote_save_keep_secret(self, name, key, access, stored_secret,
                                     endpoint, bucket, region, ep_notice=""):
            """Save an edit where the secret is unchanged (reused verbatim)."""
            def work():
                remote = rclone.build_remote_section(
                    name, key, access, stored_secret,
                    endpoint=endpoint, region=region, bucket=bucket)
                remotes = [r for r in rclone.list_remotes()
                           if r.name not in (name, self._edit_name)]
                remotes.append(remote)
                rclone.save_remotes(remotes)
                return remote

            def done(remote):
                self.set_success(f"Saved remote “{remote.name}”."
                                 + (f"  {ep_notice}" if ep_notice else ""))
                self._remote_hide_form()
                self._remotes_refresh()

            self._bg(work, done, button=self._remote_save_btn, busy="Saving…")

        def _remotes_refresh(self):
            if not hasattr(self, "_remotes_list"):
                return
            for child in list(self._remotes_list.winfo_children()):
                child.destroy()
            try:
                remotes = rclone.list_remotes()
            except CloudSyncError as ex:
                aura.Caption(self._remotes_list, str(ex)).pack(anchor="w", padx=4)
                return
            if not remotes:
                aura.Caption(
                    self._remotes_list,
                    "No remotes yet. Click “＋ Add remote” to configure your "
                    "first S3 / cloud connection.").pack(anchor="w", padx=4, pady=8)
                return
            for r in remotes:
                self._remote_card(r)

        def _remote_card(self, remote):
            card = aura.Card(self._remotes_list, padding=12)
            card.pack(fill="x", pady=6)
            head = ctk.CTkFrame(card.body, fg_color="transparent")
            head.pack(fill="x")
            aura.Heading(head, f"☁  {remote.name}").pack(side="left")
            aura.Caption(head, remote.provider_label()).pack(side="left", padx=12)
            meta = remote.endpoint or "(AWS region-addressed)"
            if remote.region:
                meta += f"   ·   {remote.region}"
            if remote.bucket:
                meta += f"   ·   bucket: {remote.bucket}"
            aura.Caption(card.body, meta).pack(anchor="w", pady=(4, 8))
            aura.Caption(card.body,
                         f"access key: {remote.access_key_id or '—'}   ·   "
                         f"secret: {'saved' if remote.params.get('secret_access_key') else 'missing'}").pack(
                anchor="w")
            btns = ctk.CTkFrame(card.body, fg_color="transparent")
            btns.pack(fill="x", pady=(10, 0))
            aura.AuraButton(btns, "Test", kind="secondary",
                            command=lambda n=remote.name: self._remote_test(n)).pack(side="left")
            aura.AuraButton(btns, "Browse", kind="secondary",
                            command=lambda n=remote.name: self._open_browse(n)).pack(side="left", padx=8)
            aura.AuraButton(btns, "Edit", kind="ghost",
                            command=lambda r=remote: self._remote_show_form(r)).pack(side="left")
            aura.AuraButton(btns, "Delete", kind="danger",
                            command=lambda n=remote.name: self._remote_delete(n)).pack(side="right")

        def _remote_test(self, name):
            if not rclone.rclone_available():
                self.set_error(rclone.NO_RCLONE_MSG)
                return
            bucket = self._bucket_of(name)
            ok_msg = (f"“{name}” is reachable (tested inside bucket "
                      f"“{bucket}”)." if bucket else f"“{name}” is reachable.")
            self._bg(lambda: rclone.test_remote(name),
                     lambda _out: self.set_success(ok_msg),
                     busy=f"Testing {name}…")

        def _bucket_of(self, name):
            """The configured bucket for a remote name ('' when none/unknown)."""
            try:
                return rclone.get_remote(name).bucket
            except CloudSyncError:
                return ""

        def _remote_delete(self, name):
            if not messagebox.askyesno(
                    "Delete remote",
                    f"Remove the remote “{name}”?\n\nThis only deletes the local "
                    "config entry — nothing in the cloud is touched."):
                return
            self._bg(lambda: rclone.delete_remote(name),
                     lambda _r: (self.set_success(f"Deleted “{name}”."),
                                 self._remotes_refresh()))

        def _open_browse(self, name):
            self.show("browse")
            try:
                self._browse_remote.set(name)
                self._browse_path.delete(0, "end")
                bucket = self._bucket_of(name)
                if bucket:      # scoped credentials: start AT the bucket
                    self._browse_path.insert(0, bucket)
                self._browse_go()
            except Exception:
                pass

        # ===============================================================
        # Browse section
        # ===============================================================
        def _build_browse(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["browse"],
                         wraplength=780, justify="left").pack(
                anchor="w", pady=(0, 10))
            bar = ctk.CTkFrame(frame, fg_color="transparent")
            bar.pack(fill="x")
            aura.SectionLabel(bar, "Remote").pack(side="left")
            self._browse_remote = aura.AuraOption(
                bar, values=["(none)"], width=200,
                command=self._browse_remote_changed)
            self._browse_remote.pack(side="left", padx=(6, 14))
            self._browse_path = aura.AuraEntry(bar, placeholder="bucket/folder …")
            self._browse_path.pack(side="left", fill="x", expand=True)
            aura.AuraButton(bar, "Up", kind="secondary",
                            command=self._browse_up).pack(side="left", padx=(8, 0))
            self._browse_btn = aura.AuraButton(bar, "Open", kind="primary",
                                               command=self._browse_go)
            self._browse_btn.pack(side="left", padx=(8, 0))

            table = ctk.CTkFrame(frame, fg_color="transparent")
            table.pack(fill="both", expand=True, pady=10)
            self._browse_tree = ttk.Treeview(
                table, columns=("size",), show="tree headings",
                selectmode="browse")
            self._browse_tree.heading("#0", text=aura.spaced("Name"), anchor="w")
            self._browse_tree.heading("size", text=aura.spaced("Size"), anchor="w")
            self._browse_tree.column("#0", width=620, anchor="w")
            self._browse_tree.column("size", width=120, anchor="e", stretch=False)
            sb = ttk.Scrollbar(table, orient="vertical",
                               command=self._browse_tree.yview)
            self._browse_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._browse_tree.pack(side="left", fill="both", expand=True)
            self._browse_tree.bind("<Double-1>", self._browse_double)
            self._browse_entries = {}

        def _all_remote_names(self):
            try:
                return rclone.remote_names()
            except CloudSyncError:
                return []

        def _browse_refresh_remotes(self):
            names = self._all_remote_names()
            self._browse_remote.configure(values=names or ["(none)"])
            if names and self._browse_remote.get() not in names:
                self._browse_remote.set(names[0])
                self._browse_remote_changed(names[0])
            elif names and not self._browse_path.get().strip():
                self._browse_remote_changed(self._browse_remote.get())

        def _browse_remote_changed(self, name=None):
            """A remote with a bucket always starts browsing AT the bucket."""
            name = name or self._browse_remote.get()
            bucket = self._bucket_of(name) if name not in ("", "(none)") else ""
            self._browse_path.delete(0, "end")
            if bucket:
                self._browse_path.insert(0, bucket)

        def _browse_go(self):
            name = self._browse_remote.get()
            if name in ("", "(none)"):
                self.set_error("Add a remote first (Remotes tab).")
                return
            if not rclone.rclone_available():
                self.set_error(rclone.NO_RCLONE_MSG)
                return
            path = self._browse_path.get().strip()
            bucket = self._bucket_of(name)
            if bucket and not path:
                # never list the account root of a bucket-scoped remote
                path = bucket
                self._browse_path.insert(0, bucket)

            def work():
                return rclone.list_path(name, path)

            def done(entries):
                self._browse_render(entries)
                self.set_success(f"{remote_path(name, path)} — {len(entries)} item(s).")

            self._bg(work, done, button=self._browse_btn, busy="Listing…")

        def _browse_render(self, entries):
            tree = self._browse_tree
            tree.delete(*tree.get_children())
            self._browse_entries = {}
            for e in entries:
                glyph = "▸ " if e.is_dir else "  "
                iid = tree.insert("", "end", text=glyph + e.name,
                                  values=("" if e.is_dir else human_size(e.size),))
                self._browse_entries[iid] = e

        def _browse_double(self, _evt):
            sel = self._browse_tree.selection()
            if not sel:
                return
            entry = self._browse_entries.get(sel[0])
            if not entry or not entry.is_dir:
                return
            base = self._browse_path.get().strip().strip("/")
            new = f"{base}/{entry.name}".strip("/") if base else entry.name
            self._browse_path.delete(0, "end")
            self._browse_path.insert(0, new)
            self._browse_go()

        def _browse_up(self):
            base = self._browse_path.get().strip().strip("/")
            if not base:
                return
            bucket = self._bucket_of(self._browse_remote.get())
            if bucket and base == bucket:
                return          # the bucket is the top for scoped credentials
            parent = base.rsplit("/", 1)[0] if "/" in base else ""
            self._browse_path.delete(0, "end")
            if parent:
                self._browse_path.insert(0, parent)
            self._browse_go()

        # ===============================================================
        # Sync section
        # ===============================================================
        def _build_sync(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["sync"],
                         wraplength=780, justify="left").pack(
                anchor="w", pady=(0, 10))

            self._sync_local = FolderRow(frame, "Local folder")
            self._sync_local.pack(fill="x", pady=4)

            rrow = ctk.CTkFrame(frame, fg_color="transparent")
            rrow.pack(fill="x", pady=6)
            aura.SectionLabel(rrow, "Remote").pack(side="left")
            self._sync_remote = aura.AuraOption(
                rrow, values=["(none)"], width=200,
                command=self._sync_remote_changed)
            self._sync_remote.pack(side="left", padx=(6, 14))
            self._sync_rpath = aura.AuraEntry(rrow, placeholder="bucket/folder …")
            self._sync_rpath.pack(side="left", fill="x", expand=True)

            opts = ctk.CTkFrame(frame, fg_color="transparent")
            opts.pack(fill="x", pady=8)
            aura.SectionLabel(opts, "Direction").pack(side="left")
            self._sync_dir = aura.SegmentedControl(
                opts, values=["Upload ↑", "Download ↓", "Both ⇅"],
                command=self._sync_dir_changed)
            self._sync_dir.set("Upload ↑")
            self._sync_dir.pack(side="left", padx=(6, 18))
            aura.SectionLabel(opts, "Mode").pack(side="left")
            self._sync_op = aura.AuraCombo(
                opts, width=130, values=["sync (mirror)", "copy (add only)"])
            self._sync_op.set("sync (mirror)")
            self._sync_op.pack(side="left", padx=(6, 0))

            actions = ctk.CTkFrame(frame, fg_color="transparent")
            actions.pack(fill="x", pady=(6, 8))
            self._sync_preview_btn = aura.AuraButton(
                actions, "Preview (dry run)", kind="secondary",
                command=lambda: self._sync_run(dry=True))
            self._sync_preview_btn.pack(side="left")
            self._sync_run_btn = aura.AuraButton(
                actions, "Run sync", kind="primary",
                command=lambda: self._sync_run(dry=False))
            self._sync_run_btn.pack(side="left", padx=10)
            aura.Caption(actions,
                         "“Both ⇅” uses rclone bisync; mirror can delete on the "
                         "destination.").pack(side="left", padx=6)

            out = aura.Card(frame, title="Output", padding=10)
            out.pack(fill="both", expand=True)
            self._sync_out = aura.AuraTextbox(out.body)
            self._sync_out.pack(fill="both", expand=True)

        def _sync_dir_changed(self, _v=None):
            pass

        def _sync_refresh_remotes(self):
            names = self._all_remote_names()
            self._sync_remote.configure(values=names or ["(none)"])
            if names and self._sync_remote.get() not in names:
                self._sync_remote.set(names[0])
                self._sync_remote_changed(names[0])
            elif names and not self._sync_rpath.get().strip():
                self._sync_remote_changed(self._sync_remote.get())

        def _sync_remote_changed(self, name=None):
            """Prefill the remote path with the configured bucket, if any."""
            name = name or self._sync_remote.get()
            if name in ("", "(none)"):
                return
            bucket = self._bucket_of(name)
            cur = self._sync_rpath.get().strip()
            if bucket and (not cur or cur == bucket or
                           not cur.startswith(bucket)):
                self._sync_rpath.delete(0, "end")
                self._sync_rpath.insert(0, bucket + "/")

        def _sync_endpoints(self):
            local = paths.normalize_local(self._sync_local.get())
            name = self._sync_remote.get()
            if not local:
                raise CloudSyncError("Choose a local folder.")
            if name in ("", "(none)"):
                raise CloudSyncError("Add and select a remote first.")
            remote = remote_path(name, self._sync_rpath.get().strip())
            direction = self._sync_dir.get()
            op = "copy" if self._sync_op.get().startswith("copy") else "sync"
            if direction.startswith("Both"):
                return local, remote, "bisync"
            if direction.startswith("Download"):
                return remote, local, op
            return local, remote, op   # Upload

        def _sync_run(self, dry):
            if not rclone.rclone_available():
                self.set_error(rclone.NO_RCLONE_MSG)
                return
            try:
                source, dest, op = self._sync_endpoints()
            except CloudSyncError as ex:
                self.set_error(str(ex))
                return
            if not dry and op == "sync":
                if not messagebox.askyesno(
                        "Confirm sync",
                        f"Mirror\n  {source}\nto\n  {dest}\n\n'sync' can DELETE "
                        "files on the destination so it matches the source. "
                        "Continue?"):
                    return
            self._sync_out.delete("1.0", "end")
            self._sync_out.insert("end",
                                  f"{'DRY RUN' if dry else op.upper()}: "
                                  f"{source}  ->  {dest}\n\n")
            btn = self._sync_preview_btn if dry else self._sync_run_btn

            def work():
                return rclone.sync(source, dest, operation=op, dry_run=dry)

            def done(out):
                self._sync_out.insert("end", (out or "(no changes)").rstrip() + "\n")
                if dry:
                    self._sync_out.insert(
                        "end", "\nPreview only — click “Run sync” to apply.\n")
                    self.set_success("Dry run complete.")
                else:
                    self.set_success("Sync complete.")

            self._bg(work, done, button=btn,
                     busy=("Previewing…" if dry else "Syncing…"))

        # ===============================================================
        # Status section
        # ===============================================================
        def _build_status(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["status"],
                         wraplength=780, justify="left").pack(
                anchor="w", pady=(0, 10))
            self._status_card = aura.Card(frame, title="Environment", padding=16)
            self._status_card.pack(fill="x")
            self._status_body = self._status_card.body
            aura.AuraButton(frame, "Refresh", kind="secondary",
                            command=self._status_refresh).pack(anchor="w", pady=12)

        def _status_refresh(self):
            if not hasattr(self, "_status_body"):
                return
            for child in list(self._status_body.winfo_children()):
                child.destroy()
            avail = rclone.rclone_available()
            rows = [
                ("Platform", paths.platform_label()),
                ("Config file", str(paths.default_config_path())),
                ("Remotes configured", str(len(self._all_remote_names()))),
                ("rclone", "installed" if avail else "NOT installed"),
            ]
            if avail:
                try:
                    rows.append(("rclone version", rclone.rclone_version()))
                except CloudSyncError:
                    pass
            for k, v in rows:
                row = ctk.CTkFrame(self._status_body, fg_color="transparent")
                row.pack(fill="x", pady=3)
                aura.SectionLabel(row, k).pack(side="left")
                aura.Caption(row, v).pack(side="left", padx=12)
            if not avail:
                aura.Caption(self._status_body, rclone.NO_RCLONE_MSG,
                             wraplength=760, justify="left").pack(
                    anchor="w", pady=(10, 0))

        # ===============================================================
        # Help section (in-app help, day-to-day-user tone)
        # ===============================================================
        def _build_help(self, frame):
            aura.Caption(frame, VIEW_DESCRIPTIONS["help"],
                         wraplength=780, justify="left").pack(
                anchor="w", pady=(0, 10))
            wrap = ctk.CTkScrollableFrame(frame, fg_color="transparent")
            wrap.pack(fill="both", expand=True)

            def topic(title, text):
                card = aura.Card(wrap, title=title, padding=14)
                card.pack(fill="x", pady=6)
                ctk.CTkLabel(card.body, text=text, font=aura.font(),
                             justify="left", anchor="w", wraplength=680
                             ).pack(anchor="w", fill="x")

            topic(
                "Add a cloud remote",
                "Remotes tab → “＋ Add remote”. Pick your provider, then "
                "paste the Endpoint URL (just the host — never add a bucket "
                "or path after it), your Access Key ID and Secret Access "
                "Key, and save. Nothing connects until you click Test, "
                "Browse or start a sync.")
            topic(
                "The Bucket field (scoped credentials)",
                "Some credentials only work inside one bucket — a Cloudflare "
                "R2 API token limited to a single bucket, or an AWS IAM "
                "policy scoped to one bucket. Those credentials cannot list "
                "your account's buckets, so a plain connection test looks "
                "broken (“access denied”) even though the key is fine.\n\n"
                "Fix: type that bucket's name in the remote's Bucket field. "
                "Cloud Sync then tests, browses and syncs inside the bucket "
                "instead of the account root. If your key can see the whole "
                "account, leave the field empty.")
            topic(
                "Cloudflare R2, step by step",
                "1. Endpoint: your bare account endpoint, e.g. "
                "https://<account-id>.r2.cloudflarestorage.com — with "
                "nothing after the host. If you paste a URL with a bucket "
                "on the end, Cloud Sync removes it and tells you.\n"
                "2. Keys: when you create an R2 API token, Cloudflare also "
                "shows an S3 “Access Key ID” and “Secret Access Key” pair — "
                "use those two values here. The secret is the SHA-256 hash "
                "of the token value and is only shown at token creation; it "
                "is NOT the token value itself. If you only kept the token, "
                "create a new one and copy the S3 pair this time.\n"
                "3. Scoped token? If the token was limited to one bucket, "
                "put that bucket's name in the Bucket field.\n"
                "4. Region: “auto” is right for R2.")
            topic(
                "“SignatureDoesNotMatch” — what it really means",
                "The cloud computed a different request signature than your "
                "key produced. Two usual causes:\n"
                "• Wrong Secret Access Key — on R2, pasting the token value "
                "instead of the S3 secret shown at token creation.\n"
                "• A path after the endpoint host (a bucket name pasted "
                "into the endpoint URL) — keep the endpoint host-only and "
                "put the bucket in the Bucket field.")
            topic(
                "“Access denied” when testing",
                "If Test fails with access denied and the Bucket field is "
                "empty, your credentials are probably scoped to one bucket "
                "and simply cannot list the account root. Enter the bucket "
                "name in the Bucket field and test again. If a bucket IS "
                "set, check its spelling matches the bucket your key is "
                "scoped to.")
            topic(
                "Synced folders, transfers and safety",
                "Synced folders keep a local folder and a remote path in "
                "step — realtime by default, or on a schedule. Conflicts "
                "never lose data: the newer file wins and the other version "
                "is kept as a “conflicted copy”. One-off transfers live in "
                "the Transfer tab; “Preview (dry run)” shows what would "
                "change before anything is touched, and mirror mode asks "
                "before it can delete on the destination.")
            topic(
                "Where your settings live",
                "Remotes are stored in Cloud Sync's own rclone.conf (readable "
                "only by you) at:\n" + str(paths.default_config_path()) +
                "\nCloud Sync never touches your global rclone config, "
                "sends no telemetry, and connects only when you ask it to.")

        # ---- tray
        def _start_tray(self):
            """Create the tray indicator; harmless when one isn't available."""
            if not tray.available():
                return
            self._tray = tray.TrayIcon(
                on_open=lambda: self._from_tray(self._show_from_tray),
                on_sync_now=lambda: self._from_tray(self._tray_sync_now),
                on_toggle_pause=lambda: self._from_tray(self._toggle_pause),
                on_settings=lambda: self._from_tray(self._tray_settings),
                on_open_folder=lambda: self._from_tray(self._tray_open_folder),
                on_quit=self._tray_quit,
                title=APP_NAME)
            if not self._tray.start():
                self._tray = None
                return
            self._sync_tray_state()

        def _from_tray(self, fn):
            """Run a tray menu handler on the Tk thread (pystray calls us
            from its own thread, and Tk is not thread-safe)."""
            try:
                self.after(0, fn)
            except Exception:
                pass

        def _sync_tray_state(self, overall=None):
            """Push the current engine state onto the tray indicator."""
            if self._tray is None:
                return
            eng = self.engine
            paused = bool(eng.paused) if eng is not None else \
                bool(guiconfig.get_paused())
            if eng is None or not self._config_pairs():
                state, detail = "offline", "No folders are set up yet"
            elif paused:
                state, detail = "paused", "Syncing is paused"
            elif overall is not None:
                s = overall.get("status")
                if s == "syncing":
                    state = "syncing"
                    detail = f"{overall.get('pending', 0)} file(s) to go"
                elif s == "error":
                    state = "error"
                    detail = f"{overall.get('errors', 0)} file(s) need attention"
                else:
                    state, detail = "synced", "Everything is up to date"
            else:
                state = "synced"
                detail = ("Watching for changes"
                          if eng.mode == syncengine.REALTIME
                          else "Scheduled syncing")
            self._tray.update(state, detail, paused=paused)

        def _hide_to_tray_if_possible(self):
            """Hide the window, but only when the tray can bring it back."""
            if self._tray is None or not self._tray.running:
                return False
            try:
                self.withdraw()
            except Exception:
                return False
            self._hidden = True
            self._sync_tray_state()
            return True

        def _show_from_tray(self):
            self._hidden = False
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass

        def _tray_sync_now(self):
            eng = self.engine
            if eng is None:
                return
            try:
                eng.sync_now()
            except Exception:
                pass

        def _tray_settings(self):
            """Bring the window up and open Settings straight from the icon."""
            self._show_from_tray()
            try:
                self._open_settings()
            except Exception:
                pass

        def _tray_open_folder(self):
            pairs = guiconfig.get_pairs()
            if pairs:
                open_in_file_manager(pairs[0].get("local", ""))

        def _tray_quit(self):
            """Quit from the tray, on the tray's own thread.

            Marshalling this through after() like the other menu items is not
            safe enough for Quit: if the Tk loop is busy -- mid-transfer, or
            wedged -- the callback never runs and the menu item does nothing,
            which is exactly when a user wants out. So the icon is stopped
            here directly (the menu closes immediately, and the app stops
            claiming to be running), the orderly teardown is asked for on the
            Tk thread, and a watchdog guarantees the process actually exits.
            """
            self._quitting = True
            tray_icon, self._tray = self._tray, None
            if tray_icon is not None:
                try:
                    tray_icon.stop()
                except Exception:
                    pass
            def watchdog():
                # Give the orderly path a few seconds to stop the engine and
                # flush state; if it has not finished, leave anyway. A quit
                # that does not quit is worse than an abrupt one.
                import os as _os
                _os._exit(0)

            # The watchdog is armed BEFORE the Tk call below, not after: a
            # tkinter call from this thread blocks on the Tcl interpreter lock
            # for as long as the Tk loop is busy. Arming it afterwards means a
            # wedged loop -- the whole reason this path exists -- stops the
            # watchdog from ever being scheduled, and Quit hangs exactly as it
            # did before.
            timer = threading.Timer(6.0, watchdog)
            timer.daemon = True
            timer.start()
            self._quit_timer = timer
            try:
                self.after(0, self._quit_from_tray)
            except Exception:
                pass

        def _quit_from_tray(self):
            self._quitting = True
            self._on_close()
            # Orderly teardown finished: cancel the watchdog so a normal exit
            # is not turned into a hard one.
            timer = getattr(self, "_quit_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

        # ---- lifecycle
        def _on_close(self):
            """Closing the window keeps syncing in the tray, OneDrive-style.

            Only an explicit Quit (tray menu, or the setting turned off) really
            exits -- otherwise closing the window would silently stop syncing,
            which is exactly the behaviour this feature exists to remove.
            """
            if not self._quitting and guiconfig.get_close_to_tray() \
                    and self._hide_to_tray_if_possible():
                return
            self._stop_engine()
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
            try:
                self.destroy()
            except Exception:
                pass

    return App


def main(argv=None):
    """Entry point: build the root window and run.  Degrades on headless hosts.

    ``--minimized`` starts hidden in the tray -- what the autostart entry uses
    so a login does not throw a window at the user.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising.
    """
    global START_MINIMIZED
    args = list(sys.argv[1:] if argv is None else argv)
    if "--minimized" in args or "--tray" in args:
        START_MINIMIZED = True

    try:
        import tkinter as tk
    except Exception as exc:  # tkinter missing entirely
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
