#!/usr/bin/env python3
r"""Cloud Sync -- an Aura (QuickOpen design system) GUI over ``cloudsync``.

A single Aura window with four sections in the sidebar:

* **Remotes** -- a OneDrive-style list of your configured clouds with an
  inline "add / edit remote" form (name, provider, endpoint, region, access
  keys).  You enter everything yourself; the app connects to nothing here.
* **Browse** -- pick a remote and walk its buckets/folders (user-initiated).
* **Sync** -- choose a local folder (via the OS-correct pickers), a remote
  path and a direction, preview with a dry run, then apply.
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
APP_VERSION = "1.0.0"
WINDOW_TITLE = "Cloud Sync — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#0e9bd6"      # UI-accent registry: cloud-sync -> #0e9bd6

VIEWS = [
    ("remotes", "Remotes", "☁"),
    ("browse", "Browse", "▤"),
    ("sync", "Sync", "⇅"),
    ("status", "Status", "◉"),
]

VIEW_DESCRIPTIONS = {
    "remotes": "Configure your S3 / cloud remotes. You supply each endpoint, "
               "bucket and access key — nothing connects on its own.",
    "browse": "Walk a remote's buckets and folders. Runs only when you ask it to.",
    "sync": "Sync a local folder up, down or both ways. Preview with a dry run "
            "before applying.",
    "status": "rclone availability, your platform, and where the config lives.",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
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

    from . import aura, guiconfig, paths
    from .errors import CloudSyncError
    from . import rclone
    from .rclone import (
        PROVIDERS, PROVIDERS_BY_KEY, get_provider, human_size, remote_path,
    )

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

            self._set_icon()
            self._build_menu()

            self.progress = aura.ProgressBar(self.statusbar.actions,
                                             mode="indeterminate", width=140)

            for vid, label, glyph in VIEWS:
                self.add_section(vid, label, glyph,
                                 getattr(self, "_build_" + vid))
            self.show("remotes")
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)

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
            filem.add_command(label="Open config folder",
                              command=self._open_config_folder)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)
            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)
            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About",
                              command=lambda: messagebox.showinfo(
                                  "About " + APP_NAME,
                                  f"{APP_NAME} {APP_VERSION}\n\n"
                                  "Offline-first S3 / cloud file sync, backed by "
                                  "rclone.\nYou configure every remote; nothing "
                                  "dials home.\n\nBuilt by QuickOpen — quickopen.ai"))
            bar.add_cascade(label="Help", menu=helpm)
            try:
                self.config(menu=bar)
            except Exception:
                pass

        def _open_config_folder(self):
            paths.ensure_config_dir()
            open_in_file_manager(str(paths.config_dir()))

        # ---- navigation: refresh the section being shown
        def show(self, sid):
            super().show(sid)
            self.set_status("Ready")
            try:
                if sid == "remotes":
                    self._remotes_refresh()
                elif sid == "browse":
                    self._browse_refresh_remotes()
                elif sid == "sync":
                    self._sync_refresh_remotes()
                elif sid == "status":
                    self._status_refresh()
            except Exception:
                pass

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

            self._f_endpoint = field("Endpoint URL")
            self._endpoint_hint = aura.Caption(body, "")
            self._endpoint_hint.pack(anchor="w", pady=(2, 0))

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
                      self._f_endpoint, self._f_region):
                e.delete(0, "end")
            if remote is not None:
                self._f_name.insert(0, remote.name)
                self._f_provider.set(remote.provider_label())
                self._f_access.insert(0, remote.access_key_id)
                if remote.endpoint:
                    self._f_endpoint.insert(0, remote.endpoint)
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

        def _remote_save(self):
            name = self._f_name.get().strip()
            label = self._f_provider.get()
            key = LABEL_TO_KEY.get(label, "other")
            access = self._f_access.get().strip()
            secret = self._f_secret.get()
            endpoint = self._f_endpoint.get().strip()
            region = self._f_region.get().strip()
            editing = self._edit_name is not None

            # When editing and the secret is left blank, reuse the stored one.
            if editing and not secret:
                try:
                    secret = rclone.get_remote(self._edit_name).params.get(
                        "secret_access_key", "")
                    # already-obscured; add_remote would double-obscure, so
                    # write via build+save instead when unchanged.
                    return self._remote_save_keep_secret(
                        name, key, access, secret, endpoint, region)
                except CloudSyncError:
                    pass

            def work():
                return rclone.add_remote(name, key, access, secret,
                                         endpoint=endpoint, region=region,
                                         overwrite=editing)

            def done(remote):
                if editing and self._edit_name != remote.name:
                    try:
                        rclone.delete_remote(self._edit_name)
                    except CloudSyncError:
                        pass
                self.set_success(f"Saved remote “{remote.name}”.")
                self._remote_hide_form()
                self._remotes_refresh()

            self._bg(work, done, button=self._remote_save_btn, busy="Saving…")

        def _remote_save_keep_secret(self, name, key, access, obscured_secret,
                                     endpoint, region):
            """Save an edit where the secret is unchanged (already obscured)."""
            def work():
                remote = rclone.build_remote_section(
                    name, key, access, obscured_secret,
                    endpoint=endpoint, region=region)
                remotes = [r for r in rclone.list_remotes()
                           if r.name not in (name, self._edit_name)]
                remotes.append(remote)
                rclone.save_remotes(remotes)
                return remote

            def done(remote):
                self.set_success(f"Saved remote “{remote.name}”.")
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
            self._bg(lambda: rclone.test_remote(name),
                     lambda _out: self.set_success(f"“{name}” is reachable."),
                     busy=f"Testing {name}…")

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
            self._browse_remote = aura.AuraOption(bar, values=["(none)"], width=200)
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

        def _browse_go(self):
            name = self._browse_remote.get()
            if name in ("", "(none)"):
                self.set_error("Add a remote first (Remotes tab).")
                return
            if not rclone.rclone_available():
                self.set_error(rclone.NO_RCLONE_MSG)
                return
            path = self._browse_path.get().strip()

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
            self._sync_remote = aura.AuraOption(rrow, values=["(none)"], width=200)
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

        # ---- lifecycle
        def _on_close(self):
            try:
                self.destroy()
            except Exception:
                pass

    return App


def main():
    """Entry point: build the root window and run.  Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising.
    """
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
