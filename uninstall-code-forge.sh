#!/usr/bin/env bash
# Code Forge uninstaller (double-click to run, no terminal needed).
# Removes the program, its app-menu entry and its launcher. Your data
# (settings, keys, projects in ~/.local/share/linux-forge-agent-shell)
# and any install backups are KEPT.
set -u
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# The installer always installs to this one location, wherever this
# script itself was run from.
TARGET="${HOME:-}/.local/share/code-forge"

BACKEND=""
if command -v zenity >/dev/null 2>&1; then
    BACKEND="zenity"
elif python3 -c "import tkinter" 2>/dev/null; then
    BACKEND="tkinter"
fi
if [ -z "$BACKEND" ]; then
    exit 1
fi
confirm() {
    if [ "$BACKEND" = "zenity" ]; then
        zenity --question --title="$1" --text="$2" --width=420 2>/dev/null
    else
        python3 "$HERE/installer-dialogs.py" confirm "$1" "$2"
    fi
}
info() {
    if [ "$BACKEND" = "zenity" ]; then
        zenity --info --title="$1" --text="$2" --width=420 2>/dev/null
    else
        python3 "$HERE/installer-dialogs.py" info "$1" "$2"
    fi
}

if ! confirm "Uninstall Code Forge" \
    "This removes the Code Forge program, its app-menu entry and its launcher.

Your Code Forge data (settings, keys, projects) is kept, and so are any install backups."; then
    exit 1
fi
rm -rf "$TARGET"
rm -f "$HOME/.local/share/applications/code-forge.desktop"
rm -f "$HOME/.local/share/applications/linux-forge-agent-shell.desktop"
rm -f "$HOME/.local/bin/code-forge"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null
info "Code Forge removed" \
    "Code Forge is removed. Your data was kept."
exit 0
