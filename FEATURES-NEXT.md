# Cloud Sync — next enhancement round (owner directives, 2026-08-16)

Recorded during the QuickOpen bundled-app enhancement program (phase 1;
reference app was note-nest). Implement in cloud-sync's own round, under the
same rules: Aura layout language (branding/aura-design-system/
APP-LAYOUT-LANGUAGE.md), Xvfb tests, live 0.1.10-VM validation including the
open-app live Dark↔Light flip, dual-theme screenshots, deb build.

## Owner directive: sync modes (benchmark: Dropbox/OneDrive continuous sync)

1. **Realtime mode (default)** — watch the configured synced folders for
   changes (inotify via the `watchdog` python package if it is an acceptable
   dependency for the deb — pure-python wheel exists; else a light polling
   fallback) and sync changed files immediately instead of manual/interval-only.
2. **Scheduled mode** — user-choosable alternative: interval (every N minutes)
   or at set times (e.g. daily at HH:MM). Presented as a simple mode selector
   in Settings (Dropbox-style continuous is the default; scheduled for users
   who prefer batch syncs).
3. **Live status** — per-file and overall state in the UI (synced ✓ /
   syncing ↻ / error), plus a tray-style unobtrusive indication consistent
   with the layout language. Applies to both modes.
4. **Conflict behavior** — newer-wins with a kept conflicted-copy (Dropbox's
   "conflicted copy" naming convention); never silently lose data. Applies to
   both modes.
5. **Pause/resume toggle.**

Constraints:
- Keep the existing rclone-backed sync backend/protocol and data layout
  intact — realtime is a trigger + UX layer on top, NOT a protocol rewrite.
- No telemetry / no phoning anywhere except the user's configured sync target.
