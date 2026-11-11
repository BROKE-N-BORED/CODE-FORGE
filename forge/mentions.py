"""@file mentions — audit D43 (Continue-style context chips).

Typing ``@path/to/file`` in the message box names a workspace file;
on send, the file's content rides the outgoing message as a visible
``[Attached file: …]`` block, so the owner can hand the model a file
without spending tool cycles on reads. The mention token itself is
never rewritten — blocks are only appended, so what the owner typed
stays exactly what the conversation shows.

This module is the pure half: fragment math for the completer,
mention scanning, and the send-time expansion. Resolution goes
through forge/tools._inside — the file tools' own jail, never a
second one — so a mention can never reach outside the workspace or
into a protected path.
"""
from __future__ import annotations
import re
from pathlib import Path, PurePosixPath

from . import permissions as perm_mod
from . import tools as tools_mod

MAX_MENTIONS = 4        # files attached per send; extras are reported
MAX_MENTION_CHARS = 8000

_PATH_CHARS = r"A-Za-z0-9._\-/"
# An @ that starts a word (start of text or after whitespace), then
# path characters up to the end of the string — the fragment being
# typed right now. An @ inside a word (a@b, an email) never matches.
_FRAGMENT_RE = re.compile(r"(?:^|\s)@([" + _PATH_CHARS + r"]*)$")
# The same word-boundary rule anywhere in the text; a mention needs
# at least one path character after the @.
_MENTION_RE = re.compile(r"(?:^|\s)@([" + _PATH_CHARS + r"]+)")


def mention_fragment(text_before_cursor: str) -> str | None:
    """The path fragment being typed at the cursor, without the @.

    Returns "" when the cursor sits right after a word-start @, the
    fragment text while a path is being typed, and None when no
    mention is in progress (no trailing @token, an @ inside a word,
    or a completed mention already followed by a space)."""
    match = _FRAGMENT_RE.search(str(text_before_cursor or ""))
    return match.group(1) if match else None


def find_mentions(text: str) -> list[str]:
    """Every @<path> token in *text*, in order, deduplicated."""
    found: list[str] = []
    for match in _MENTION_RE.finditer(str(text or "")):
        token = match.group(1)
        if token not in found:
            found.append(token)
    return found


def _looks_like_path(token: str) -> bool:
    """A mention that names a would-be file (a slash or a dot
    suffix), as opposed to prose like @someone."""
    return "/" in token or bool(PurePosixPath(token).suffix)


def _read_mention_text(target) -> tuple[str, int]:
    """(head, total_chars) of a mention target — Sweep R151: the
    whole file used to be read into memory to keep 8,000 chars, so
    @-mentioning a huge log loaded all of it. The stream is read in
    bounded chunks instead: only the head the caller can use is
    retained, the rest is counted. Text mode throughout, so the
    strict decoding (a binary file refuses, wherever its bad bytes
    sit) and newline translation match the old whole read exactly."""
    keep = MAX_MENTION_CHARS + 1
    total = 0
    parts: list[str] = []
    kept = 0
    with target.open("r", encoding="utf-8") as fh:  # strict, like before
        while True:
            piece = fh.read(65536)
            if not piece:
                break
            total += len(piece)
            if kept < keep:
                parts.append(piece)
                kept += len(piece)
    return "".join(parts)[:keep], total


def _read_mention(workspace, token: str) -> tuple[str, str] | None:
    """(relpath, content) for a mention that resolves to a readable
    text file inside the workspace, else None. The content is cut to
    MAX_MENTION_CHARS with a trailing note naming the true length."""
    try:
        target = tools_mod._inside(Path(workspace), token)
    except Exception:
        return None  # outside the jail or a protected path: not a file
    if not target.is_file():
        return None
    try:
        content, total = _read_mention_text(target)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    root = Path(workspace).expanduser().resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        rel = token
    if total > MAX_MENTION_CHARS:
        content = (content[:MAX_MENTION_CHARS] +
                   f"\n… [first {MAX_MENTION_CHARS} of {total} chars]")
    return rel, content


def expand_mentions(text: str, workspace) -> tuple[str, list[str], list[str]]:
    """Append attachment blocks for the text's resolvable mentions.

    Returns (new_text, attached, unmatched): *attached* lists the
    workspace-relative paths whose content now rides the message
    (at most MAX_MENTIONS); *unmatched* lists path-like mentions
    that attached nothing — no such file, an unreadable/binary or
    protected target, or a file past the per-send cap — so the
    window can tell the owner they went out as plain text. Prose
    mentions (@someone) appear in neither list and the original
    text is returned with blocks only appended, never rewritten."""
    attached: list[str] = []
    unmatched: list[str] = []
    blocks: list[str] = []
    for token in find_mentions(text):
        if len(attached) >= MAX_MENTIONS:
            if _looks_like_path(token):
                unmatched.append(token)
            continue
        got = _read_mention(workspace, token)
        if got is None:
            if _looks_like_path(token):
                unmatched.append(token)
            continue
        rel, content = got
        if perm_mod.is_sensitive_path(rel):
            # H2 context-attach guard: a sensitive file's content
            # never rides a message on the strength of an @mention.
            # It reports as unmatched (the window names the reason),
            # so sharing one takes a deliberate act outside this
            # path — the same carve-out the approval gate applies
            # to tool calls, at this path's single choke point.
            # (Unconditional, unlike the branches above: a token
            # that RESOLVED to a real file is path-like by
            # construction — _looks_like_path would miss dotfiles
            # like .env, whose pathlib suffix is empty.)
            unmatched.append(token)
            continue
        attached.append(rel)
        blocks.append(f"[Attached file: {rel}]\n{content}")
    if blocks:
        text = str(text) + "\n\n" + "\n\n".join(blocks)
    return text, attached, unmatched
