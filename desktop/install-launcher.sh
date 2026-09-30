#!/usr/bin/env bash
# Installs the "Piano Library" launcher: ~/Desktop/Piano Library.desktop (double-click) and
# ~/.local/share/applications/org.pianovision.Library.desktop (Activities / dock), with its icon.
# GNOME's desktop icons launch a .desktop file only when it is executable and trusted.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
ICON="$ICONS/org.pianovision.Library.svg"

mkdir -p -- "$APPS" "$ICONS" "$DESKTOP_DIR"
install -m 644 -- "$ROOT/desktop/piano-library.svg" "$ICON"
chmod +x -- "$ROOT/desktop/piano-library.sh"

write_entry() {
    cat > "$1" <<ENTRY
[Desktop Entry]
Type=Application
Version=1.0
Name=Piano Library
GenericName=Piano song library
Comment=Build the MuseScore piano library and push it to PianoVision and Note Waterfall on the Quest
Exec="$ROOT/desktop/piano-library.sh"
Path=$ROOT
Icon=$ICON
Terminal=false
StartupNotify=true
StartupWMClass=org.pianovision.Library
Categories=AudioVideo;Audio;Music;
Keywords=piano;musescore;pianovision;quest;headset;note waterfall;
ENTRY
    chmod 755 -- "$1"
    gio set -- "$1" metadata::trusted true 2>/dev/null || true
}

write_entry "$APPS/org.pianovision.Library.desktop"
write_entry "$DESKTOP_DIR/Piano Library.desktop"
update-desktop-database -q "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$APPS/org.pianovision.Library.desktop" || true
fi
echo "installed: $DESKTOP_DIR/Piano Library.desktop"
echo "           $APPS/org.pianovision.Library.desktop"
echo "           $ICON"
