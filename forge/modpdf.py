"""Modular PDF knowledge (modpdf) — owner-ordered build, 2026-11-21.

Knowledge the model needs (protocols, instructions, reference
material) lives in standalone PDF files on disk ("modules") instead
of riding in every prompt. At turn assembly, only the modules
relevant to the current turn are extracted and injected, inside a
strict token budget (MODPDF_BUDGET_FRACTION of the window). The
rest stay on disk and cost zero context.

Design rules this module keeps:

  * Module state lives in FILES (the manifest + the PDFs), never
    only in the transcript. Selection re-runs from disk on every
    turn, so the history fold cannot destroy it.
  * Extraction reuses the app's existing PDF text path
    (forge.tools.extract_document_text, then the ToUnicode reader
    in forge/converting.py for glyph-encoded PDFs). No second
    parser, no new dependency, no network.
  * Selection is deterministic: `always` modules first, then
    standing self modules in their modes, then relevance scoring
    (mode match strong, keyword hits in the latest user message
    weak and capped). Whole modules only, in score order, until
    the budget slice is full; a module that does not fit is
    skipped WITH a reason.
  * Self modules (source "self") are generated views of the
    app's own living stores (knowledge, learnings, guidelines,
    macros). The stores stay canonical; refresh_self_modules
    re-renders a view when its source text changes, and the
    extraction cache keys on the PDF bytes, so a stale view
    cannot survive a store write.
  * Nothing here raises into a turn: a corrupt manifest, a
    missing file, an unreadable PDF are problems on the pack,
    named in plain words (U32), never a broken turn.

Qt-free: the only Qt touchpoint is the app's own markdown-to-PDF
conversion (forge/converting.py), called lazily at render time,
inside the app process where Qt already lives.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from . import context as context_mod

# --- named constants -------------------------------------------------

#: The share of the context window the whole module pack may use.
MODPDF_BUDGET_FRACTION = 0.20

#: Extraction cache entries are keyed by the PDF bytes' sha256 plus
#: this version; bump it when the extraction chain changes.
EXTRACTOR_VERSION = 1

#: Extracted text kept per module; longer text is cut and the cut
#: is recorded on the module record ("truncated").
MAX_EXTRACT_CHARS = 200_000

STORE_DIRNAME = "modules"
MANIFEST_NAME = "manifest.json"
CACHE_DIRNAME = "cache"
SELF_SRC_DIRNAME = "self-src"
BUNDLED_SEED_NAME = "modules.json"

SOURCE_BUNDLED = "bundled"
SOURCE_USER = "user"
SOURCE_SELF = "self"

# Selection scoring: mode match is the strong signal, keyword hits
# in the latest user message are weak and capped.
MODE_MATCH_SCORE = 5
KEYWORD_HIT_SCORE = 2
KEYWORD_HITS_CAP = 3


# --- store layout ------------------------------------------------------

def store_dir(app_dir) -> Path:
    return Path(app_dir) / STORE_DIRNAME


def manifest_path(app_dir) -> Path:
    return store_dir(app_dir) / MANIFEST_NAME


def cache_dir(app_dir) -> Path:
    return store_dir(app_dir) / CACHE_DIRNAME


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:48] or "module"


# --- manifest ----------------------------------------------------------

_ENTRY_DEFAULTS = {
    "modes": "all",
    "keywords": [],
    "always": False,
    "enabled": True,
    "kind": "knowledge",
    "source": SOURCE_USER,
    "standing": False,
}


def _normalize_entry(raw):
    """One manifest entry with defaults filled, or None when the
    raw value cannot be a module (not a dict, no id, no file).
    Never raises: a bad entry is a skipped entry, not a crash."""
    if not isinstance(raw, dict):
        return None
    entry_id = str(raw.get("id") or "").strip()
    file_name = str(raw.get("file") or "").strip()
    if not entry_id or not file_name:
        return None
    entry = dict(_ENTRY_DEFAULTS)
    entry.update({k: v for k, v in raw.items() if v is not None})
    entry["id"] = entry_id
    entry["file"] = file_name
    entry["title"] = str(raw.get("title") or entry_id)
    entry["about"] = str(raw.get("about") or "")
    if entry["modes"] != "all":
        if isinstance(entry["modes"], (list, tuple)):
            entry["modes"] = [str(m).strip().lower()
                              for m in entry["modes"] if str(m).strip()]
        else:
            entry["modes"] = [str(entry["modes"]).strip().lower()] \
                if str(entry["modes"]).strip() else "all"
    if not isinstance(entry["keywords"], list):
        entry["keywords"] = [str(entry["keywords"])]
    entry["keywords"] = [str(k) for k in entry["keywords"]]
    entry["always"] = bool(entry["always"])
    entry["enabled"] = bool(entry["enabled"])
    entry["standing"] = bool(entry["standing"])
    return entry


def load_manifest(app_dir):
    """(entries, problems): the manifest's module entries, in
    manifest order, plus plain-words problems for anything that
    could not be read. A missing manifest is an empty store, not
    a problem. A corrupt manifest yields no entries and one
    problem naming the file. Never raises."""
    path = manifest_path(app_dir)
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [f"The modules list at {path} could not be read "
                    f"({exc}); no modules were loaded."]
    if isinstance(data, dict):
        raw_entries = data.get("modules")
    else:
        raw_entries = data
    if not isinstance(raw_entries, list):
        return [], [f"The modules list at {path} has an unexpected "
                    "shape; no modules were loaded."]
    entries, problems = [], []
    seen = set()
    for raw in raw_entries:
        entry = _normalize_entry(raw)
        if entry is None:
            problems.append(
                "One entry in the modules list was skipped: it has "
                "no usable id or file name.")
            continue
        if entry["id"] in seen:
            problems.append(
                f"One entry in the modules list was skipped: the id "
                f"'{entry['id']}' appears twice.")
            continue
        seen.add(entry["id"])
        entries.append(entry)
    return entries, problems


def save_manifest(app_dir, entries) -> None:
    """Write the manifest atomically (temp file, then replace), so
    a crash mid-write never leaves a torn list behind."""
    folder = store_dir(app_dir)
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"modules": list(entries)}, ensure_ascii=False,
                         indent=1, sort_keys=True)
    tmp = folder / (MANIFEST_NAME + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, manifest_path(app_dir))


def ensure_store(app_dir, bundled_dir=None):
    """Create the store if needed and register the bundled starter
    modules (copying their PDFs in) when their entries are absent.
    Existing entries are never rewritten here: an owner's toggle
    on a bundled module survives. A bundled file that went missing
    from the store is copied back. Returns (entries, problems).
    Cheap no-op once the store exists."""
    folder = store_dir(app_dir)
    folder.mkdir(parents=True, exist_ok=True)
    cache_dir(app_dir).mkdir(parents=True, exist_ok=True)
    entries, problems = load_manifest(app_dir)
    if problems and not entries and manifest_path(app_dir).is_file():
        # A corrupt manifest: keep the evidence under a new name
        # and start the list fresh rather than failing every turn.
        quarantine = folder / "manifest.corrupt.json"
        try:
            if not quarantine.exists():
                os.replace(manifest_path(app_dir), quarantine)
        except OSError:
            pass
    changed = False
    if bundled_dir:
        seed_path = Path(bundled_dir) / BUNDLED_SEED_NAME
        seeds = []
        if seed_path.is_file():
            try:
                seed_data = json.loads(
                    seed_path.read_text(encoding="utf-8"))
                if isinstance(seed_data, dict):
                    seeds = seed_data.get("modules") or []
                elif isinstance(seed_data, list):
                    seeds = seed_data
            except Exception as exc:
                problems.append(
                    f"The bundled modules list could not be read "
                    f"({exc}); starter modules were not registered.")
        by_id = {e["id"]: e for e in entries}
        for raw in seeds:
            seed = _normalize_entry(raw)
            if seed is None:
                continue
            seed["source"] = SOURCE_BUNDLED
            bundled_pdf = Path(bundled_dir) / seed["file"]
            existing = by_id.get(seed["id"])
            if existing is None:
                if bundled_pdf.is_file():
                    try:
                        shutil.copyfile(
                            bundled_pdf, folder / seed["file"])
                    except OSError as exc:
                        problems.append(
                            f"The starter module '{seed['title']}' "
                            f"could not be copied into the store "
                            f"({exc}).")
                        continue
                entries.append(seed)
                by_id[seed["id"]] = seed
                changed = True
            else:
                target = folder / existing["file"]
                if not target.is_file() and bundled_pdf.is_file():
                    try:
                        shutil.copyfile(bundled_pdf, target)
                    except OSError:
                        pass
    if changed:
        try:
            save_manifest(app_dir, entries)
        except OSError as exc:
            problems.append(
                f"The modules list could not be saved ({exc}).")
    return entries, problems


# --- extraction (cached) ----------------------------------------------

def _pdf_text(data: bytes) -> str:
    """The app's existing PDF text path, in the app's own order:
    the stdlib literal-string extraction first, then the
    ToUnicode reader for glyph-encoded PDFs (the kind the app's
    own PDF writer produces). No new parser. The result goes
    through _clean_extracted: the app's writer draws the whole
    document into every page's stream (the page clip does the
    paginating), so the raw extraction repeats the text once per
    page, and Qt's text shaping folds some letter pairs into
    ligature glyphs."""
    text = None
    try:
        from .tools import extract_document_text
        text = extract_document_text(Path("module.pdf"), data)
    except Exception:
        text = None
    if text is None or not str(text).strip():
        try:
            from . import converting
            text = converting._pdf_text_tounicode(data)
        except Exception:
            text = ""
    return _clean_extracted(str(text or ""))


_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
              "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


def _clean_extracted(text: str) -> str:
    """Post-steps on extracted text (never a second parser):
    unfold ligature glyphs back to plain letters, and collapse
    the whole-text repetition the app's own PDF writer produces
    (one full copy of the document per page, in order). The
    collapse only fires when the ENTIRE line sequence is exact
    repetitions of one prefix (modulo trailing spaces), so real
    documents with repeated sections are untouched."""
    if not text:
        return ""
    for ligature, plain in _LIGATURES.items():
        text = text.replace(ligature, plain)
    lines = text.split("\n")
    total = len(lines)
    if total >= 2:
        first = lines[0].rstrip()
        period = None
        for p in range(1, total // 2 + 1):
            if total % p or lines[p].rstrip() != first:
                continue
            head = [line.rstrip() for line in lines[:p]]
            if all([line.rstrip() for line in lines[i:i + p]] == head
                   for i in range(p, total, p)):
                period = p
                break
        if period is not None:
            lines = lines[:period]
    return "\n".join(lines).strip()


def extract_module(app_dir, entry):
    """The module's text, from the extraction cache when the PDF
    bytes are unchanged. Returns a dict {text, truncated, tokens,
    problem}: problem is "" on success; a missing or unreadable
    file yields empty text and a plain-words problem. The cache
    key is the sha256 of the PDF bytes plus EXTRACTOR_VERSION, so
    a changed PDF always re-extracts."""
    path = store_dir(app_dir) / str(entry.get("file") or "")
    result = {"text": "", "truncated": False, "tokens": 0,
              "problem": ""}
    if not path.is_file():
        result["problem"] = (
            f"The module '{entry.get('title')}' names a PDF that "
            f"is not in the modules folder ({path.name}).")
        return result
    try:
        data = path.read_bytes()
    except OSError as exc:
        result["problem"] = (
            f"The module '{entry.get('title')}' could not be read "
            f"({exc}).")
        return result
    key = hashlib.sha256(data).hexdigest()
    folder = cache_dir(app_dir)
    text_file = folder / f"{key}-v{EXTRACTOR_VERSION}.txt"
    meta_file = folder / f"{key}-v{EXTRACTOR_VERSION}.json"
    if text_file.is_file() and meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            result["text"] = text_file.read_text(encoding="utf-8")
            result["truncated"] = bool(meta.get("truncated"))
            result["tokens"] = int(meta.get("tokens") or 0)
            return result
        except Exception:
            pass  # a torn cache entry re-extracts below
    text = _pdf_text(data)
    original_chars = len(text)
    truncated = original_chars > MAX_EXTRACT_CHARS
    if truncated:
        text = text[:MAX_EXTRACT_CHARS]
    tokens = context_mod.estimate_text(text)
    result.update({"text": text, "truncated": truncated,
                   "tokens": tokens})
    try:
        folder.mkdir(parents=True, exist_ok=True)
        text_file.write_text(text, encoding="utf-8")
        meta_file.write_text(json.dumps(
            {"original_chars": original_chars,
             "truncated": truncated, "tokens": tokens}),
            encoding="utf-8")
    except OSError:
        pass  # the cache is an optimization; a failed write is fine
    return result


def module_tokens(app_dir, entry) -> int:
    """The module's size in estimated tokens (its extracted text),
    or 0 when it cannot be read."""
    return extract_module(app_dir, entry)["tokens"]


# --- user modules --------------------------------------------------------

def add_user_module(app_dir, pdf_path, title, about, keywords, modes):
    """Copy a user-picked PDF into the store (never referenced in
    place) and register it. Returns the new entry. Raises OSError
    or ValueError with a plain-words message on failure: the
    caller shows it, nothing is registered."""
    src = Path(pdf_path)
    if not src.is_file():
        raise ValueError("That file does not exist.")
    if src.suffix.lower() != ".pdf":
        raise ValueError("Modules are PDF files. Pick a file whose "
                         "name ends in .pdf.")
    ensure_store(app_dir)
    entries, _problems = load_manifest(app_dir)
    base = _slug(title) or "module"
    entry_id = f"user-{base}"
    taken = {e["id"] for e in entries}
    n = 2
    while entry_id in taken:
        entry_id = f"user-{base}-{n}"
        n += 1
    file_name = f"{entry_id}.pdf"
    target = store_dir(app_dir) / file_name
    try:
        shutil.copyfile(src, target)
    except OSError as exc:
        raise ValueError(
            f"The file could not be copied into the modules "
            f"folder ({exc}).") from exc
    entry = _normalize_entry({
        "id": entry_id, "file": file_name,
        "title": str(title or base), "about": str(about or ""),
        "modes": modes, "keywords": list(keywords or []),
        "always": False, "enabled": True, "kind": "knowledge",
        "source": SOURCE_USER})
    entries.append(entry)
    save_manifest(app_dir, entries)
    return entry


def remove_module(app_dir, entry_id) -> bool:
    """Remove a module's entry and its PDF (when the file lives in
    the store). True when an entry was removed."""
    entries, _problems = load_manifest(app_dir)
    kept, removed = [], None
    for entry in entries:
        if entry["id"] == entry_id:
            removed = entry
        else:
            kept.append(entry)
    if removed is None:
        return False
    save_manifest(app_dir, kept)
    target = store_dir(app_dir) / removed["file"]
    try:
        if target.is_file() and target.parent == store_dir(app_dir):
            target.unlink()
    except OSError:
        pass
    return True


def set_enabled(app_dir, entry_id, enabled) -> bool:
    """Flip a module's enabled flag in the manifest (the Customize
    checkbox state IS manifest data). True when an entry changed."""
    entries, _problems = load_manifest(app_dir)
    changed = False
    for entry in entries:
        if entry["id"] == entry_id:
            entry["enabled"] = bool(enabled)
            changed = True
    if changed:
        save_manifest(app_dir, entries)
    return changed


# --- self modules (generated views of the living stores) ---------------

_LIST_MARKER_RE = re.compile(r"^([ ]{0,3})([-*+])( )", re.M)
_NUMBERED_MARKER_RE = re.compile(r"^([ ]{0,3})(\d{1,9})([.)])( )", re.M)


def _render_source(markdown_text: str) -> str:
    """The markdown as the renderer must see it. Two escapes keep
    the store's words literal in the PDF:

    - Angle brackets are escaped (&lt; / &gt;): Qt's markdown
      parser reads a raw <word> as an HTML tag and swallows
      everything after it, so a store text that mentions <n> or
      <the file> would lose its tail. Escaped, the brackets
      render as the characters they are and extract back as
      themselves.
    - List markers at a line start are backslash-escaped: a
      markdown list item's bullet is drawn, not text, so the
      extraction would lose the "- " the owner's text carries.
      Escaped, the marker renders as plain text and the line
      extracts byte-for-byte."""
    text = str(markdown_text).replace("<", "&lt;").replace(
        ">", "&gt;")
    text = _LIST_MARKER_RE.sub(r"\1\\\2\3", text)
    text = _NUMBERED_MARKER_RE.sub(r"\1\2\\\3\4", text)
    return text


def render_markdown_pdf(app_dir, entry_id, markdown_text):
    """Render markdown to the module's PDF with the app's own
    markdown-to-PDF path. The render source is kept beside the
    store (self-src/) so the owner can see exactly what the view
    was rendered from. Writes to a temp PDF and replaces, so a
    failed render never tears the live file. Raises on failure;
    the caller reports it as a problem and keeps the old view."""
    folder = store_dir(app_dir)
    src_dir = folder / SELF_SRC_DIRNAME
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / f"{entry_id}.md"
    src.write_text(_render_source(markdown_text), encoding="utf-8")
    from . import converting
    tmp_pdf = folder / f"{entry_id}.pdf.tmp"
    converting._md_to_pdf(src, tmp_pdf)
    os.replace(tmp_pdf, folder / f"{entry_id}.pdf")


def refresh_self_modules(app_dir, sources):
    """Bring the self modules in line with the stores' current
    text. `sources` is the COMPLETE current set:
    {store_key: {title, about, modes, keywords, standing,
    markdown, workspace}}. A store whose markdown is blank loses
    its module; a store absent from the dict loses it too. A
    changed source re-renders its PDF (the extraction cache key
    changes with the bytes, so the new text is what loads). A
    failed render keeps the previous view and reports a problem.
    Returns a list of plain-words problems (possibly empty)."""
    problems = []
    ensure_store(app_dir)
    entries, load_problems = load_manifest(app_dir)
    problems.extend(load_problems)
    wanted = {}
    for store_key, spec in (sources or {}).items():
        if not isinstance(spec, dict):
            continue
        markdown = str(spec.get("markdown") or "")
        if not markdown.strip():
            continue
        wanted[f"self-{store_key}"] = (store_key, spec, markdown)
    by_id = {e["id"]: e for e in entries}
    changed = False
    # Drop self entries the current set no longer carries.
    for entry in list(entries):
        if entry.get("source") == SOURCE_SELF \
                and entry["id"] not in wanted:
            entries.remove(entry)
            by_id.pop(entry["id"], None)
            changed = True
            try:
                target = store_dir(app_dir) / entry["file"]
                if target.is_file():
                    target.unlink()
            except OSError:
                pass
    for entry_id, (store_key, spec, markdown) in wanted.items():
        digest = hashlib.sha256(
            markdown.encode("utf-8")).hexdigest()
        entry = by_id.get(entry_id)
        pdf_ok = entry is not None and (
            store_dir(app_dir) / entry["file"]).is_file()
        if entry is not None and entry.get("source_hash") == digest \
                and pdf_ok:
            # Same text: only metadata could have drifted.
            for key in ("title", "about", "modes", "keywords",
                        "standing"):
                if key in spec and entry.get(key) != spec[key]:
                    entry[key] = spec[key]
                    changed = True
            continue
        try:
            render_markdown_pdf(app_dir, entry_id, markdown)
        except Exception as exc:
            problems.append(
                f"The module view for '{spec.get('title')}' could "
                f"not be refreshed ({exc}); the previous view is "
                "still in place.")
            continue
        fresh = _normalize_entry({
            "id": entry_id, "file": f"{entry_id}.pdf",
            "title": spec.get("title") or entry_id,
            "about": spec.get("about") or "",
            "modes": spec.get("modes", "all"),
            "keywords": spec.get("keywords") or [],
            "always": False,
            "enabled": entry["enabled"] if entry else True,
            "kind": "knowledge", "source": SOURCE_SELF,
            "standing": bool(spec.get("standing"))})
        fresh["source_hash"] = digest
        if spec.get("workspace"):
            fresh["workspace"] = str(spec["workspace"])
        if entry is None:
            entries.append(fresh)
        else:
            entries[entries.index(entry)] = fresh
        by_id[entry_id] = fresh
        changed = True
    if changed:
        try:
            save_manifest(app_dir, entries)
        except OSError as exc:
            problems.append(
                f"The modules list could not be saved ({exc}).")
    return problems


# --- selection + budgeted pack assembly --------------------------------

def _mode_matches(entry, mode) -> bool:
    modes = entry.get("modes")
    if modes == "all":
        return True
    return str(mode or "").strip().lower() in (modes or [])


def _keyword_hits(entry, user_text_lower) -> int:
    hits = 0
    for keyword in entry.get("keywords") or []:
        word = str(keyword).strip().lower()
        if word and word in user_text_lower:
            hits += 1
    return hits


def latest_user_text(messages) -> str:
    """The newest user message's text in a history ("" when there
    is none). The ONLY transcript input selection uses: it is
    re-derived from whatever history the turn carries, folded or
    not, and all module state itself lives on disk. A fold
    summary riding as a user message is not the owner's words,
    so it is skipped and the search continues to the real
    message behind it."""
    try:
        from . import context as context_mod
        summary_prefix = context_mod.SUMMARY_PREFIX
    except Exception:
        summary_prefix = ""
    for message in reversed(list(messages or [])):
        if (message or {}).get("role") == "user":
            content = message.get("content")
            if not isinstance(content, str):
                return ""
            if summary_prefix and content.startswith(summary_prefix):
                continue
            return content
    return ""


def assemble_pack(app_dir, mode, messages, num_ctx, self_sources=None,
                  bundled_dir=None):
    """Select and load this turn's modules. Returns a pack dict:
    {text, tokens, loaded, skipped, problems} — text is the one
    delimited block to inject ("" when nothing loaded), loaded
    the titles that rode, skipped a list of (title, reason) for
    every module that did not, problems plain-words notes about
    anything unreadable (U32). Never raises."""
    pack = {"text": "", "tokens": 0, "loaded": [], "skipped": [],
            "problems": []}
    try:
        _entries, ensure_problems = ensure_store(app_dir, bundled_dir)
        pack["problems"].extend(ensure_problems)
        if self_sources is not None:
            pack["problems"].extend(
                refresh_self_modules(app_dir, self_sources))
        entries, problems = load_manifest(app_dir)
        pack["problems"].extend(problems)
    except Exception as exc:  # a broken store never breaks a turn
        pack["problems"].append(
            f"Knowledge modules were unavailable this turn ({exc}).")
        return pack
    mode = str(mode or "").strip().lower()
    user_text = latest_user_text(messages)
    user_lower = user_text.lower()
    try:
        budget = int(num_ctx or 0)
    except Exception:
        budget = 0
    slice_tokens = int(budget * MODPDF_BUDGET_FRACTION)
    candidates = []
    for index, entry in enumerate(entries):
        title = entry["title"]
        if not entry.get("enabled", True):
            pack["skipped"].append((title, "turned off in Customize"))
            continue
        got = extract_module(app_dir, entry)
        if got["problem"]:
            pack["problems"].append(got["problem"])
            pack["skipped"].append(
                (title, "its PDF could not be read"))
            continue
        text = got["text"].strip()
        if not text:
            pack["skipped"].append(
                (title, "no readable text could be extracted "
                        "from its PDF"))
            continue
        entry["truncated"] = got["truncated"]
        mode_match = _mode_matches(entry, mode)
        hits = _keyword_hits(entry, user_lower) if user_lower else 0
        if entry.get("always"):
            group, score = 0, 0
        elif entry.get("standing") and mode_match:
            group, score = 1, MODE_MATCH_SCORE
        elif user_lower and ((mode_match and hits >= 1)
                             or hits >= KEYWORD_HITS_CAP):
            score = (MODE_MATCH_SCORE if mode_match else 0) \
                + KEYWORD_HIT_SCORE * min(hits, KEYWORD_HITS_CAP)
            group = 2
        else:
            pack["skipped"].append(
                (title, "not relevant to this turn"))
            continue
        segment = f"## {title}\n{text}"
        candidates.append((group, -score, index, entry, segment,
                             context_mod.estimate_text(segment)))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    used = 0
    segments = []
    for _group, _neg, _index, entry, segment, cost in candidates:
        title = entry["title"]
        if used + cost <= slice_tokens:
            segments.append(segment)
            used += cost
            pack["loaded"].append(title)
        else:
            pack["skipped"].append(
                (title, f"needs about {cost} tokens but only "
                        f"{max(0, slice_tokens - used)} are left in "
                        "the module budget"))
    if segments:
        header = ("Knowledge modules loaded for this turn. These "
                  "came from PDF files on disk; only the modules "
                  "relevant to this turn are here.")
        block = "\n".join(
            [context_mod.MODULES_BLOCK_BEGIN, header] + segments
            + [context_mod.MODULES_BLOCK_END])
        pack["text"] = block
        pack["tokens"] = context_mod.estimate_text(block)
    return pack
