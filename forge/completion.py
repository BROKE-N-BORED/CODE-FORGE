"""Item #67: lexical TAB completions for code panes.

Forge's editors offered no completions at all. This module gives a
QPlainTextEdit code pane an instant, deterministic, offline
completion popup:

  * the vocabulary is Python keywords + builtins plus identifiers
    harvested from the document itself (def/class names, plain
    assignments, imported names) — no model is consulted, so a
    keystroke never burns the owner's small local models or waits
    on anything;
  * while a word prefix of 2+ characters is being typed, matching
    completions are offered; TAB (or Enter) accepts the
    highlighted one, replacing the typed prefix; Esc dismisses;
    Up/Down navigate (the QCompleter popup's own keys);
  * with NO popup visible, Tab keeps the pane's normal behaviour
    (indent) — typing is never hijacked;
  * the word currently being typed is never offered back as its
    own completion.

The behaviour lives here, attached to a pane by one call
(attach_code_completion), so another code surface — the Write
window's Editor tab when that build merges — can adopt it
unchanged. It is attached ONLY to code panes: not to prose /
instructions panes, not to the chat input, not to the deck.
"""
from __future__ import annotations

import builtins
import keyword
import re

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QCompleter

MIN_PREFIX = 2            # never pop up for a 1-character prefix
HARVEST_DEBOUNCE_MS = 300  # re-harvest at most this often while typing

_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

# The fixed vocabulary: every Python keyword plus the builtin names
# an owner plausibly types in a plugin/skill file. Underscore-led
# builtins (the __xxx__ protocol names) are left out — nobody
# completes those by prefix in these panes.
BASE_WORDS = sorted(
    set(keyword.kwlist)
    | {name for name in dir(builtins) if not name.startswith("_")})

_DEF_RE = re.compile(r"^\s*(?:async\s+def|def)\s+([A-Za-z_]\w*)", re.M)
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M)
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(.+)$", re.M)
_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+[\w\.]+\s+import\s+(.+)$", re.M)


def _imported_names(spec: str) -> set[str]:
    """The bindable names from one import tail ("a.b, c as d" ->
    {"a", "d"}; "x, y" -> {"x", "y"}). Parenthesised from-import
    lists arrive here with the parens still on; they are stripped.
    A "*" import binds nothing nameable and yields nothing."""
    names: set[str] = set()
    spec = spec.replace("(", " ").replace(")", " ")
    for part in spec.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        if " as " in part:
            alias = part.rsplit(" as ", 1)[1].strip()
            if alias.isidentifier():
                names.add(alias)
        else:
            head = part.split(".")[0].strip()
            if head.isidentifier():
                names.add(head)
    return names


def harvest_identifiers(text: str) -> set[str]:
    """Identifiers worth completing, read off the document: def and
    class names, plain assignment targets, imported names. A regex
    harvest on purpose — the pane's text may be mid-edit and not
    parse, and a completion must never wait on (or die on) an AST."""
    names: set[str] = set()
    for pattern in (_DEF_RE, _CLASS_RE, _ASSIGN_RE):
        names.update(pattern.findall(text))
    for match in _IMPORT_RE.finditer(text):
        names.update(_imported_names(match.group(1)))
    for match in _FROM_IMPORT_RE.finditer(text):
        names.update(_imported_names(match.group(1)))
    return names


def completion_words(text: str) -> list[str]:
    """The full candidate list for a document: base vocabulary plus
    its harvested identifiers, sorted, duplicates gone."""
    return sorted(set(BASE_WORDS) | harvest_identifiers(text))


def word_span(text: str, pos: int) -> tuple[int, int]:
    """The [start, end) span of the word characters around pos in a
    single line's text (the word being typed, possibly empty)."""
    start = pos
    while start > 0 and text[start - 1] in _WORD_CHARS:
        start -= 1
    end = pos
    while end < len(text) and text[end] in _WORD_CHARS:
        end += 1
    return start, end


class CodeCompletion(QObject):
    """The completion behaviour for one code pane. Owns the
    QCompleter, its model, the harvest debounce timer, and the key
    filter. Construct via attach_code_completion(edit); keep the
    returned object alive for as long as the pane lives (parenting
    it to the pane does that)."""

    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit
        self._model = QStringListModel([], self)
        self.completer = QCompleter(self._model, self)
        self.completer.setWidget(edit)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setWrapAround(False)
        self._harvest_timer = QTimer(self)
        self._harvest_timer.setSingleShot(True)
        self._harvest_timer.setInterval(HARVEST_DEBOUNCE_MS)
        self._harvest_timer.timeout.connect(self.refresh_words)
        self._typing = False
        edit.installEventFilter(self)
        edit.textChanged.connect(self._on_text_changed)
        edit.cursorPositionChanged.connect(self._on_cursor_moved)
        self.refresh_words()

    # ----- vocabulary -----
    def refresh_words(self):
        """Rebuild the candidate list from the document as it now
        stands, minus the word currently being typed (it is never
        its own completion). Cheap at pane scale; debounced while
        typing actually changes the text."""
        _prefix, token = self._prefix_and_token()
        words = [w for w in completion_words(self._edit.toPlainText())
                 if w != token]
        self._model.setStringList(words)

    def words(self) -> list[str]:
        """The candidate list currently on the model (tests + any
        future surface that wants to show what would be offered)."""
        return self._model.stringList()

    # ----- the word at the cursor -----
    def _prefix_and_token(self):
        """(prefix, token): the word characters immediately left of
        the cursor — what a completion would replace — and the full
        word token the cursor sits in."""
        cursor = self._edit.textCursor()
        block_text = cursor.block().text()
        pos = cursor.positionInBlock()
        start, end = word_span(block_text, pos)
        return block_text[start:pos], block_text[start:end]

    # ----- popup driving -----
    def _on_text_changed(self):
        self._harvest_timer.start()
        if self._typing:
            self._typing = False
            self._update_popup()

    def _on_cursor_moved(self):
        # A bare cursor move (click, arrow) never opens the popup,
        # and a stale popup never follows the cursor around.
        if not self._typing:
            popup = self.completer.popup()
            if popup is not None and popup.isVisible():
                popup.hide()

    def _update_popup(self):
        prefix, _token = self._prefix_and_token()
        popup = self.completer.popup()
        if len(prefix) < MIN_PREFIX:
            if popup is not None:
                popup.hide()
            return
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0:
            if popup is not None:
                popup.hide()
            return
        if self._edit.hasFocus():
            self.completer.complete(self._edit.cursorRect())

    # ----- acceptance -----
    def accept_current(self) -> bool:
        """Accept the popup's highlighted completion: replace the
        typed prefix with the full word and leave the cursor right
        after it. False when there is nothing to accept."""
        popup = self.completer.popup()
        if popup is None or not popup.isVisible():
            return False
        index = popup.currentIndex()
        if not index.isValid():
            index = self.completer.completionModel().index(0, 0)
        if not index.isValid():
            return False
        completion = str(index.data(Qt.DisplayRole) or "")
        if not completion:
            return False
        cursor = self._edit.textCursor()
        block_text = cursor.block().text()
        pos = cursor.positionInBlock()
        start, _end = word_span(block_text, pos)
        cursor.setPosition(cursor.block().position() + start)
        cursor.setPosition(cursor.block().position() + pos,
                           QTextCursor.KeepAnchor)
        cursor.insertText(completion)
        self._edit.setTextCursor(cursor)
        popup.hide()
        return True

    # ----- keys -----
    def eventFilter(self, obj, event):
        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            popup = self.completer.popup()
            visible = popup is not None and popup.isVisible()
            key = event.key()
            if visible and key in (Qt.Key_Tab, Qt.Key_Return,
                                   Qt.Key_Enter):
                # Item #67: Tab is the acceptance key this item is
                # named for; Enter accepts too. Consuming the event
                # keeps Tab from ALSO indenting and Enter from ALSO
                # breaking the line — one keypress, one effect.
                return self.accept_current()
            if visible and key == Qt.Key_Escape:
                popup.hide()
                return True
            if key == Qt.Key_Tab and not visible:
                # No popup: Tab is the pane's own indent, untouched.
                return False
            if event.text():
                # Any key that produces text may extend the prefix;
                # textChanged (fired once the text lands) drives the
                # popup refresh.
                self._typing = True
        return super().eventFilter(obj, event)


def attach_code_completion(edit) -> CodeCompletion:
    """Attach Item #67 tab completions to a code pane (a
    QPlainTextEdit). Returns the behaviour object, parented to the
    pane. Code panes only — prose panes must not be passed here."""
    return CodeCompletion(edit)
