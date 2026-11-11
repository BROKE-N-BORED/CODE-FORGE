"""Skill directory (Forge v2, Note 4).

A skill is a FOLDER: SKILL.md (instructions the agent follows) plus any
knowledge files beside it. At rest, only each skill's name + one-line
description enters the model's context (the index). The full skill loads
on demand through the load_skill tool — MetaMorphAi slice-loading, so a
whole library fits an 8192-token window.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

MAX_SKILL_CHARS = 9000  # a loaded skill must leave room to actually work


@dataclass
class Skill:
    name: str
    description: str
    folder: Path
    _body: str | None = field(default=None, repr=False)

    @staticmethod
    def _read_head(path: Path, limit: int) -> str:
        """At most limit+1 chars of a file — Sweep R152: body() used
        to read SKILL.md plus EVERY sibling .md whole before
        truncating the concatenation, so one huge knowledge file was
        fully loaded to keep 9,000 chars."""
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit + 1)

    def body(self) -> str:
        # Sweep R152: the cache sentinel is None, not "" — an
        # empty-body skill used to be re-read from disk on every
        # call because "" is falsy.
        if self._body is None:
            text = self._read_head(self.folder / "SKILL.md",
                                   MAX_SKILL_CHARS)
            parts = [text]
            used = len(text)
            for extra in sorted(self.folder.glob("*.md")):
                if extra.name == "SKILL.md":
                    continue
                if used >= MAX_SKILL_CHARS:
                    break
                piece = self._read_head(extra, MAX_SKILL_CHARS - used)
                parts.append(f"\n\n----- {extra.name} -----\n" + piece)
                used += len(piece)
            self._body = "".join(parts)[:MAX_SKILL_CHARS]
        return self._body


def _parse_header(text: str) -> tuple[str, str]:
    name, description = "", ""
    for line in text.splitlines()[:12]:
        low = line.strip().lower()
        if low.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif low.startswith("description:"):
            description = line.split(":", 1)[1].strip()
        elif name and description:
            break
    return name, description


def scan_skills(skills_dir: Path) -> list[Skill]:
    skills: list[Skill] = []
    if not Path(skills_dir).is_dir():
        return skills
    for folder in sorted(Path(skills_dir).iterdir()):
        skill_file = folder / "SKILL.md"
        if folder.is_dir() and skill_file.is_file():
            try:
                name, description = _parse_header(
                    skill_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            skills.append(Skill(name=name or folder.name,
                                description=description or "(no description)",
                                folder=folder))
    return skills


def index_text(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = ["Available skills — when a task matches one, call load_skill with its name "
             "to load the full instructions before starting:"]
    for s in skills:
        lines.append(f"- {s.name}: {s.description}")
    return "\n".join(lines)


LOAD_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load the full instructions of a skill from the skill directory by name.",
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"}},
                       "required": ["name"]},
    },
}


def make_load_skill_handler(skills: list[Skill], disabled=()):
    """The load_skill handler over the ACTIVE skills. `disabled`
    (S6, audit S1-25c) names skills that exist on disk but are
    switched off: asking for one by name earns a plain refusal that
    names Customize — not the misleading "unknown skill" a merely
    absent name gets. Disabled skills are never in the index and
    their bodies are never served."""
    by_name = {s.name: s for s in skills}
    disabled_names = {str(n) for n in (disabled or ())}

    def handler(args: dict) -> str:
        wanted = str(args.get("name", "")).strip()
        skill = by_name.get(wanted)
        if skill is None:
            if wanted in disabled_names:
                return (f"Skill '{wanted}' is disabled — it cannot be "
                        "loaded until the owner enables it in Customize.")
            return "Unknown skill. Available: " + (", ".join(by_name) or "none")
        return skill.body()

    return handler
