#!/usr/bin/env bash
# Installs the hand-edit sync service for this user: ~/.config/systemd/user/nw-handsync.service, enabled
# at login and started now. It runs `python3 -m pianovision hands sync` from this repository (README:
# "Hand edits sync"); log in ~/.cache/nw-handsync/sync.log.
#
#   desktop/install-handsync.sh               install (or update) and start
#   desktop/install-handsync.sh --uninstall   stop, disable and remove the unit
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/nw-handsync.service"
DEFAULT_ROOT="$HOME/Desktop/Piano/PianoVisionFormatter"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now nw-handsync.service 2>/dev/null || true
    rm -f -- "$UNIT"
    systemctl --user daemon-reload
    echo "removed: $UNIT"
    exit 0
fi

command -v systemctl >/dev/null || { echo "systemctl not found" >&2; exit 1; }
PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/lib/android-sdk/platform-tools" command -v adb >/dev/null \
    || echo "warning: adb is not on the service's PATH; set [device] adb in pianovision.toml" >&2
command -v notify-send >/dev/null || echo "note: notify-send not found: no desktop notifications (the log has all)" >&2

mkdir -p -- "$UNIT_DIR"
if [[ "$ROOT" == "$DEFAULT_ROOT" ]]; then
    install -m 644 -- "$ROOT/desktop/nw-handsync.service" "$UNIT"
else
    # the unit names %h/Desktop/Piano/PianoVisionFormatter; this copy of the repository is elsewhere
    sed "s#%h/Desktop/Piano/PianoVisionFormatter#$ROOT#g" "$ROOT/desktop/nw-handsync.service" > "$UNIT"
    chmod 644 -- "$UNIT"
    echo "note: the service runs the repository at $ROOT"
fi
if command -v systemd-analyze >/dev/null; then
    systemd-analyze --user verify "$UNIT" || true
fi
systemctl --user daemon-reload
systemctl --user enable --now nw-handsync.service
systemctl --user restart nw-handsync.service      # an update takes effect now
echo "installed: $UNIT"
systemctl --user --no-pager --lines=5 status nw-handsync.service || true
echo "log: ${HOME}/.cache/nw-handsync/sync.log   (journalctl --user -u nw-handsync -f)"
