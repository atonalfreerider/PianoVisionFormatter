#!/usr/bin/env bash
# Starts the Piano Library window (python3 -m pianovision gui). Used by the "Piano Library"
# desktop launcher; extra arguments go to `gui` (e.g. --read-only).
set -uo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/piano-library"
mkdir -p -- "$LOG_DIR"
LOG="$LOG_DIR/launcher.log"
PY=/usr/bin/python3
[[ -x "$PY" ]] || PY="$(command -v python3)"

cd -- "$ROOT" || exit 1
{
    echo "=== $(date '+%F %T') starting Piano Library ($ROOT)"
} >> "$LOG"
"$PY" -m pianovision gui "$@" >> "$LOG" 2>&1
code=$?
if (( code != 0 )); then
    msg="Piano Library could not start (exit $code). Details: $LOG"
    tail -n 15 -- "$LOG" >&2
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="Piano Library" --width=520 \
            --text="$msg\n\n$(tail -n 8 -- "$LOG" | sed 's/[<>&]/ /g')" >/dev/null 2>&1 || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "Piano Library" "$msg" || true
    fi
fi
exit $code
