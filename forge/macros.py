"""Workflow macros — Note 17.

Saved procedures the owner runs with one word. A macro is a Markdown
file in the app's commands/ folder: the filename is the command
(`audit.md` → `/audit`), the first line may be a one-line description
after an em dash in a leading `# /name — description` heading, and the
body is the instruction the model receives. Anything the owner types
after the command is appended as additional instructions.

Distinct from skills (knowledge the model loads) and the command
palette (finding UI actions): a macro is the owner's own canned
procedure, spelled out once so a small model executes it instead of
improvising it.
"""
from __future__ import annotations
from pathlib import Path


def scan_macros(commands_dir: Path) -> dict[str, tuple[str, str]]:
    """name -> (description, body)."""
    macros: dict[str, tuple[str, str]] = {}
    if not Path(commands_dir).is_dir():
        return macros
    for path in sorted(Path(commands_dir).glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        description = ""
        body = text.strip()
        lines = body.splitlines()
        if lines and lines[0].startswith("#"):
            heading = lines[0].lstrip("# ").strip()
            if "—" in heading:
                description = heading.split("—", 1)[1].strip()
            body = "\n".join(lines[1:]).strip()
        macros[path.stem] = (description, body)
    return macros


def expand(macros: dict[str, tuple[str, str]], text: str) -> str | None:
    """If text is a /command, return the expanded prompt, else None."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    name = parts[0].lower() if parts else ""
    if name not in macros:
        return None
    _desc, body = macros[name]
    rest = parts[1] if len(parts) > 1 else ""
    if rest:
        body += f"\n\nAdditional instructions from the owner: {rest}"
    return body


def index_text(macros: dict[str, tuple[str, str]]) -> str:
    if not macros:
        return ""
    lines = ["Owner's workflow macros (the owner runs these by typing the command; "
             "when one is expanded you receive its full text as the instruction):"]
    for name, (desc, _body) in sorted(macros.items()):
        lines.append(f"- /{name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)
