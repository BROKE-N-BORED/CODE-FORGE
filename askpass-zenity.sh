#!/bin/sh
# Graphical password prompt for sudo -A (SUDO_ASKPASS). sudo runs this
# with no arguments and reads the password from the first line of
# stdout. Zenity first, tkinter fallback — same pair the installer
# itself uses.
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
if command -v zenity >/dev/null 2>&1; then
    exec zenity --password \
        --title="Code Forge installer" \
        --text="Enter your password to install Code Forge's system packages."
fi
exec python3 "$HERE/installer-dialogs.py" password \
    "Code Forge installer" \
    "Enter your password to install Code Forge's system packages."
