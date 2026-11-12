"""Owner-enabled file-change watcher (Manus fix F7 — the M11
remainder the M13 module docstring named, not invented).

A project tree the owner edits outside the Forge — another
editor, a tool, a checkout — can change under the agent's feet.
This module watches the tree and reports changed files. It
proposes nothing itself, edits nothing, runs no tool, starts no
turn, and spawns no thread: a QFileSystemWatcher and a debounce
timer, both on the GUI thread, emitting `changed` with the
project-relative path of each file that changed. The window's
slot raises the proposal through the existing
raise_event_proposal path, and only while the owner has the
setting on (Settings → File watcher, default OFF — OFF means no
watcher object exists at all).

Ignore rules (stated here and in the README):
- hidden directories and files — any path component starting
  with "." — which covers .git/, .forge/, .idea/ and the like;
- dependency / build / tooling directories: node_modules,
  __pycache__, venv, .venv;
- compiled Python (.pyc, .pyo) and .DS_Store droppings.

A burst of saves is one event per file: changes collect for
DEBOUNCE_MS after the last one before anything is emitted, and
the proposals store's same-kind+event dedupe is the second
layer — a burst to one file is still one proposal. The watch
list is NOT capped: every file under the root that passes the
ignore rules is watched (the owner's call, 2026-11-11 — no
unnecessary limitations). The directories stay watched so new
files are still noticed.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

# Ignore rules — see the module docstring. Hidden components
# (starting with ".") are matched by rule, not by name, so .git/,
# .forge/, .idea/ and anything like them are all covered.
IGNORED_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "venv", ".venv",
})
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
IGNORED_FILE_NAMES = frozenset({".DS_Store"})

# A burst of saves (editor autosave, a formatter, a checkout)
# collects for this long after the last change before any
# proposal is raised.
DEBOUNCE_MS = 1000


def should_watch(path, root) -> bool:
    """The pure ignore rule: is this path worth watching? A path
    outside the root is never watched; a hidden component, a
    named tooling directory, a compiled file, or a dropping is
    never watched; everything else under the root is."""
    try:
        rel = Path(path).relative_to(Path(root))
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return True
    # Every component obeys the directory rule — including the
    # last one, so a top-level node_modules/ is never watched
    # (checking parents only would let the directory itself
    # through, and only its contents would be filtered).
    for part in parts:
        if part.startswith(".") or part in IGNORED_DIR_NAMES:
            return False
    name = parts[-1]
    if name in IGNORED_FILE_NAMES:
        return False
    if Path(path).is_dir():
        return True
    if Path(name).suffix in IGNORED_SUFFIXES:
        return False
    return True


def collect_watch_paths(root):
    """(files, dirs): the paths to watch under root — every file
    that passes the ignore rules, sorted, uncapped (the owner's
    call, 2026-11-11: no unnecessary limitations), and the
    directories (root itself included, so new files directly
    under it are noticed). Both lists are absolute path
    strings."""
    root = Path(root)
    files: list[str] = []
    dirs: list[str] = []
    if not root.is_dir():
        return files, dirs
    dirs.append(str(root))
    for path in sorted(root.rglob("*")):
        if not should_watch(path, root):
            continue
        if path.is_dir():
            dirs.append(str(path))
        elif path.is_file():
            files.append(str(path))
    return files, dirs


class ProjectWatcher(QObject):
    """Watches one project tree on the GUI thread and emits
    `changed` with the project-relative path of each file that
    changed, debounced. It proposes nothing itself — the window
    owns the proposal. Start on the owner's setting only; stop
    when the setting goes off or the project switches."""

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(
            self._on_directory_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._flush)
        self._root: str | None = None
        self._pending: set[str] = set()

    @property
    def root(self):
        """The tree being watched, or None while stopped."""
        return self._root

    @property
    def is_running(self):
        return self._root is not None

    @property
    def watched_count(self):
        """Files currently watched (directories not counted)."""
        return len(self._watcher.files())

    def start(self, root):
        """Watch root's tree (replacing any previous tree)."""
        self.stop()
        root = Path(root)
        files, dirs = collect_watch_paths(root)
        self._watcher.addPaths(dirs + files)
        self._root = str(root)

    def stop(self):
        """Stop watching entirely — no watches, no pending
        changes, no timer."""
        self._timer.stop()
        self._pending.clear()
        self._watcher.removePaths(self._watcher.files())
        self._watcher.removePaths(self._watcher.directories())
        self._root = None

    def _on_file_changed(self, path):
        # QFileSystemWatcher drops a file the moment it is
        # deleted or replaced (editor save-via-rename): re-add
        # it while it still exists, so the next save is seen
        # too. A file that no longer exists is not a change to
        # propose — only edits are.
        if Path(path).exists():
            if path not in self._watcher.files():
                self._watcher.addPath(path)
        else:
            return
        self._pending.add(
            str(Path(path).relative_to(self._root)))
        # Restarting the single-shot timer on every change is
        # the debounce: a burst emits once, DEBOUNCE_MS after
        # its last save.
        self._timer.start()

    def _on_directory_changed(self, path):
        # A file appeared or vanished under a watched directory:
        # rescan just this directory and reconcile the watch
        # list, so a file created after the watcher started is
        # watched from its first edit.
        if self._root is None:
            return
        files, dirs = collect_watch_paths(Path(path))
        known = set(self._watcher.files()) | set(
            self._watcher.directories())
        for candidate in files + dirs:
            if candidate not in known:
                self._watcher.addPath(candidate)
        for watched in list(self._watcher.files()):
            if not Path(watched).exists():
                self._watcher.removePath(watched)

    def _flush(self):
        for rel in sorted(self._pending):
            self.changed.emit(rel)
        self._pending.clear()
