"""SQLite-backed conversation history.

Forge v2 (F6): conversations now remember WHICH MODEL and WHICH PROJECT
they belong to. The v1 bug — old threads silently continuing under
whatever model the dropdown happened to sit on — is fixed by storing
both and restoring/warning on load.
"""
import json, sqlite3
from pathlib import Path

from . import fileperms, scrub as scrub_mod


class CorruptConversationError(ValueError):
    """Sweep R140: one conversation's stored messages cannot be
    parsed. Raised (as a ValueError, like the JSONDecodeError it
    replaces) so the failure names the damaged conversation instead
    of surfacing as a bare decode error from deep inside load /
    export_text / fork."""


def _parse_messages(raw, conversation_id):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CorruptConversationError(
            f"Conversation {conversation_id} is damaged — its stored "
            f"messages can't be read ({exc}). The rest of the history "
            "is unaffected.") from exc
    if not isinstance(data, list):
        raise CorruptConversationError(
            f"Conversation {conversation_id} is damaged — its stored "
            "messages are not a message list.")
    return data


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, messages TEXT NOT NULL)")
            cols = {r[1] for r in db.execute("PRAGMA table_info(conversations)")}
            if "model" not in cols:
                db.execute("ALTER TABLE conversations ADD COLUMN model TEXT")
            if "project" not in cols:
                db.execute("ALTER TABLE conversations ADD COLUMN project TEXT")
        # S2: the database holds whole conversations — private file.
        fileperms.ensure_private_file(self.path)

    def _connect(self):
        # Sweep R149 (locking half): overlapping writers (the main
        # window plus deck-triggered saves) used to surface raw
        # "database is locked" errors — a busy timeout lets a writer
        # wait for the other's transaction instead.
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = sqlite3.Row
        return db

    def save(self, conversation_id, messages, model=None, project=None):
        visible = [m for m in messages if m.get("role") in ("user", "assistant", "system", "tool")]
        # Image payloads are intentionally excluded from persistent history to keep the database small.
        # S2 central scrub (audit S1-16): message text is scrubbed
        # BEFORE it persists, so a secret in a prompt or tool result
        # does not live in the database (and therefore not in any
        # export, which reads the database — title included).  Fail-soft
        # inside the scrubber; ordinary text is stored byte-identical.
        clean = scrub_mod.scrub_obj(
            [{k: v for k, v in m.items() if k != "images"} for m in visible])
        # The title is the first line of the first user message with
        # real text. Blank / None / non-string contents are skipped —
        # the old one-liner indexed splitlines()[0] on whatever came
        # first and crashed the save on an empty first message.
        title = "New conversation"
        for m in clean:
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                title = content.strip().splitlines()[0][:64]
                break
        with self._connect() as db:
            if conversation_id is None:
                cur = db.execute("INSERT INTO conversations(title,messages,model,project) VALUES(?,?,?,?)",
                                 (title, json.dumps(clean), model, project))
                new_id = cur.lastrowid
                fileperms.ensure_private_file(self.path)
                return new_id
            cur = db.execute("UPDATE conversations SET title=?, updated_at=CURRENT_TIMESTAMP, messages=?, model=?, project=? WHERE id=?",
                       (title, json.dumps(clean), model, project, conversation_id))
            if cur.rowcount == 0:
                # Sweep R148: the UPDATE path used to report success
                # while writing nothing when the id had been deleted
                # in another window — the turn was silently lost.
                # The caller still holds this id, so the row is
                # recreated under it.
                db.execute("INSERT INTO conversations(id,title,messages,model,project) VALUES(?,?,?,?,?)",
                           (conversation_id, title, json.dumps(clean), model, project))
            fileperms.ensure_private_file(self.path)
            return conversation_id

    def recent(self, limit=30):
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT id,title,updated_at,model,project FROM conversations ORDER BY updated_at DESC,id DESC LIMIT ?", (limit,))]

    def delete(self, conversation_id):
        """Remove a conversation for good. True iff a row went away."""
        with self._connect() as db:
            cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            return cur.rowcount > 0

    def search(self, query, limit=200):
        """Conversations whose title or message text contains `query`
        (case-insensitive), newest first — the same row shape as
        recent(). A blank query is just recent(). The query is matched
        literally: LIKE's %/_ wildcards are escaped away."""
        query = (query or "").strip()
        if not query:
            return self.recent(limit)
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,title,updated_at,model,project FROM conversations "
                "WHERE LOWER(title) LIKE LOWER(?) ESCAPE '\\' "
                "OR LOWER(messages) LIKE LOWER(?) ESCAPE '\\' "
                "ORDER BY updated_at DESC,id DESC LIMIT ?", (like, like, limit))]

    def export_text(self, conversation_id):
        """A Markdown transcript of a stored conversation, or None
        when the id is unknown."""
        headings = {"user": "You", "assistant": "Forge",
                    "system": "System", "tool": "Tool"}
        with self._connect() as db:
            row = db.execute("SELECT title,messages,model,project FROM conversations WHERE id=?",
                             (conversation_id,)).fetchone()
        if not row:
            return None
        lines = [f"# {row['title'] or 'Conversation'}"]
        facts = []
        if row["model"]:
            facts.append(f"Model: {row['model']}")
        if row["project"]:
            facts.append(f"Project: {row['project']}")
        if facts:
            lines += ["", " · ".join(facts)]
        for m in _parse_messages(row["messages"], conversation_id):
            role = str(m.get("role") or "")
            content = m.get("content")
            body = content if isinstance(content, str) else json.dumps(content)
            lines += ["", f"## {headings.get(role, role.title() or 'Message')}", "", body]
        lines.append("")
        return "\n".join(lines)

    def load(self, conversation_id):
        with self._connect() as db:
            row = db.execute("SELECT messages FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return _parse_messages(row["messages"], conversation_id) if row else None

    def fork(self, conversation_id, upto_index):
        """Copy a conversation up to and including message `upto_index`
        into a NEW conversation (the original is untouched) so the owner
        can try a second approach from that point. Returns the new id."""
        with self._connect() as db:
            row = db.execute("SELECT title,messages,model,project FROM conversations WHERE id=?",
                             (conversation_id,)).fetchone()
            if not row:
                return None
            messages = _parse_messages(row["messages"], conversation_id)[: upto_index + 1]
            cur = db.execute("INSERT INTO conversations(title,messages,model,project) VALUES(?,?,?,?)",
                             ((row["title"] or "Conversation") + " (fork)",
                              json.dumps(messages), row["model"], row["project"]))
            return cur.lastrowid

    def meta(self, conversation_id):
        """(model, project) recorded for a conversation, or (None, None)."""
        with self._connect() as db:
            row = db.execute("SELECT model, project FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return (row["model"], row["project"]) if row else (None, None)
