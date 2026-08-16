#!/usr/bin/env bash
# Cloud Sync local installer (Linux). Installs to /opt/quickopen/cloud-sync,
# adds the menu entry + icon. Uninstall: sudo rm -rf /opt/quickopen/cloud-sync
# /usr/share/applications/quickopen-cloud-sync.desktop
# /usr/share/icons/hicolor/*/apps/quickopen-cloud-sync.png
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo $0"; exit 1; }
install -d /opt/quickopen/cloud-sync
install -m755 "$HERE/CloudSync" /opt/quickopen/cloud-sync/CloudSync
install -m644 "$HERE/quickopen-cloud-sync.desktop" /usr/share/applications/
for sz in 256 128 64 48 32; do
  d="/usr/share/icons/hicolor/${sz}x${sz}/apps"; install -d "$d"
  install -m644 "$HERE/cloud-sync.png" "$d/quickopen-cloud-sync.png"
done
command -v update-desktop-database >/dev/null && update-desktop-database -q || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v rclone >/dev/null || echo "note: install rclone for transfers (sudo apt install rclone) — remotes can be configured without it."
echo "Cloud Sync installed — find it in the menu, or run /opt/quickopen/cloud-sync/CloudSync"
