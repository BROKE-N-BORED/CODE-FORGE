#!/usr/bin/env python3
"""Fallback dialogs for the Code Forge GUI installer.

Used only when `zenity` is not installed. Every mode is driven by
command-line arguments so the installer shell script can call it the
same way it calls zenity. Standard library only (tkinter).

Modes:
    confirm <title> <text>     Yes/No question, exit 0 = Yes, 1 = No
    info <title> <text>        Information box, exit 0
    error <title> <text>       Error box, exit 0
    password <title> <text>    Password prompt; prints the password
                               to stdout, exit 0; Cancel exits 1
    progress <pid>             Progress window; reads status lines
                               from stdin (a leading "# " is stripped),
                               closes on EOF with exit 0. Cancel kills
                               <pid> and exits 1.
"""
import os
import signal
import sys


def _root(title):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.title(title)
    # Keep the dialog above the file manager / desktop.
    root.attributes("-topmost", True)
    return root


def _confirm(title, text):
    import tkinter.messagebox as mb
    root = _root(title)
    try:
        return 0 if mb.askyesno(title, text, parent=root) else 1
    finally:
        root.destroy()


def _info(title, text):
    import tkinter.messagebox as mb
    root = _root(title)
    try:
        mb.showinfo(title, text, parent=root)
    finally:
        root.destroy()
    return 0


def _error(title, text):
    import tkinter.messagebox as mb
    root = _root(title)
    try:
        mb.showerror(title, text, parent=root)
    finally:
        root.destroy()
    return 0


def _password(title, text):
    import tkinter.simpledialog as sd
    root = _root(title)
    try:
        value = sd.askstring(title, text, show="*", parent=root)
    finally:
        root.destroy()
    if value is None:
        return 1
    # sudo reads the first line; the newline is expected.
    sys.stdout.write(value + "\n")
    sys.stdout.flush()
    return 0


def _progress(pid):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("Installing Code Forge")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    label = tk.Label(root, text="Installing Code Forge...",
                     wraplength=420, justify="left")
    label.pack(padx=20, pady=(20, 10), fill="x")
    bar = ttk.Progressbar(root, mode="indeterminate", length=420)
    bar.pack(padx=20, pady=(0, 10))
    bar.start(20)
    result = {"code": 0}

    def cancel():
        # The installer starts its worker in its own process group
        # (setsid), so killing the group stops the worker and any
        # children it started (pip, apt). Fall back to the pid alone.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        result["code"] = 1
        root.destroy()

    tk.Button(root, text="Cancel", command=cancel).pack(pady=(0, 16))
    # Centre the window on the screen.
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"+{x}+{y}")

    fd = sys.stdin.fileno()
    os.set_blocking(fd, False)
    buf = b""

    def poll():
        nonlocal buf
        # Non-blocking read: no data yet raises BlockingIOError,
        # EOF returns b"" once the writer closed the pipe.
        try:
            chunk = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            root.after(150, poll)
            return
        if chunk == b"":
            if buf:
                text = buf.decode("utf-8", "replace")
                if text.startswith("# "):
                    text = text[2:]
                if text.strip():
                    label.config(text=text.strip()[:200])
            root.destroy()
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace")
            if text.startswith("# "):
                text = text[2:]
            text = text.strip()
            if text:
                label.config(text=text[:200])
        root.after(150, poll)

    root.after(150, poll)
    root.mainloop()
    return result["code"]


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: installer-dialogs.py "
                         "<confirm|info|error|password|progress> ...\n")
        return 2
    mode = argv[1]
    if mode in ("confirm", "info", "error", "password"):
        if len(argv) != 4:
            sys.stderr.write(f"usage: installer-dialogs.py {mode} "
                             "<title> <text>\n")
            return 2
        fn = {"confirm": _confirm, "info": _info,
              "error": _error, "password": _password}[mode]
        return fn(argv[2], argv[3])
    if mode == "progress":
        if len(argv) != 3:
            sys.stderr.write("usage: installer-dialogs.py progress <pid>\n")
            return 2
        try:
            pid = int(argv[2])
        except ValueError:
            sys.stderr.write("progress needs a numeric pid\n")
            return 2
        return _progress(pid)
    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
