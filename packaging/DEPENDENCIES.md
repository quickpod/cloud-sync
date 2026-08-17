# Packaging dependencies

The deb declares these so `apt` pulls them in automatically; nothing here is
bundled into the app itself.

## Required

| Package | Why |
|---|---|
| `python3`, `python3-tk` | the GUI toolkit |
| `python3-watchdog` | inotify-backed realtime folder watching |
| `quickopen-runtime` | vendors the pure-Python wheels (customtkinter, darkdetect, **pystray**, Pillow) |

`rclone` stays a runtime *recommendation* rather than a dependency: remotes can
be configured entirely offline, and the app says so when it is missing.

## Required for the tray indicator

`pystray` is already vendored in `quickopen-runtime`, but on Linux it needs a
**system** status-notifier backend, which cannot be vendored:

    Depends: gir1.2-ayatanaappindicator3-0.1, python3-gi

Without these pystray falls back to its bare X11 backend, which does not work
under Wayland and is ignored by modern GNOME/KDE trays — so the icon silently
never appears. `quickopen-runtime` currently declares only `python3-xlib`,
which is the fallback, not the working path.

The app degrades cleanly if the tray cannot start (`cloudsync.tray.available()`
returns False): syncing still works while the window is open, and Settings
explains why the background options are unavailable.
