"""Item 3 — Research wiring: plan gate, search/read loop, cited report.

Follows the Research-mode shape Joshua ordered on 2026-11-09
(modelled on how Gemini's Deep Research works, rebuilt for Forge):

1. PLAN FIRST. make_plan(topic, queries) drafts the research plan —
   the topic, the exact searches that will run, how many pages will
   be read. Nothing touches the web until a plan exists.
2. THE GATE. run_research refuses a plan that was not approved —
   approval is a human act (the CLI asks; a Forge tool caller sets
   approved=True only after the user says go), matching Forge's
   plan-gate rule everywhere else.
3. THE LOOP. Each query is searched with the keyless backends; the
   top results are read with fetch_any, so a source can be a web
   page, a PDF, or another file type. Sources are deduplicated by
   URL and numbered ONCE — that number is the citation, stable
   across the whole report.
4. THE REPORT. A Markdown document: the questions, what each source
   actually said (an excerpt of its real text, under its citation
   number, with its URL), barriers found on a source (login /
   paywall / captcha are reported, never silently read past), and
   the full source list. The report contains only what the sources
   said — synthesis is the reading model's job, so nothing here
   invents findings.
"""

from .errors import BrowserError
from .files import fetch_any
from .search import search


def make_plan(topic, queries=None, max_pages=6):
    """Draft a research plan. Pure data — runs no searches."""
    if not topic or not topic.strip():
        raise BrowserError("a research plan needs a topic")
    searches = [q.strip() for q in (queries or []) if q and q.strip()]
    if not searches:
        searches = [topic.strip()]
    return {
        "topic": topic.strip(),
        "queries": searches,
        "max_pages": max_pages,
        "steps": [
            f"Search: {query}" for query in searches
        ] + [f"Read up to {max_pages} sources (pages, PDFs, other files) "
             "and excerpt what each one says, with citations"],
    }


def format_plan(plan):
    lines = [f"Research plan: {plan['topic']}", ""]
    for index, step in enumerate(plan["steps"], 1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines)


def run_research(plan, approved=False, max_chars_per_source=4000,
                 search_fn=search, fetch_fn=fetch_any):
    """Run an approved plan; return (report_markdown, sources).

    Sources: [{n, title, url, kind, text, barriers}] — text is the
    source's own words, cut to max_chars_per_source.
    """
    if not approved:
        raise BrowserError(
            "this research plan has not been approved — show the plan "
            "first and run it only after the go (the plan gate)")
    from .barriers import detect_barriers

    sources = []
    seen_urls = set()
    failures = []
    for query in plan["queries"]:
        if len(sources) >= plan["max_pages"]:
            break
        try:
            results = search_fn(query, max_results=plan["max_pages"])
        except BrowserError as exc:
            failures.append(f"search {query!r} failed: {exc}")
            continue
        for result in results:
            if len(sources) >= plan["max_pages"]:
                break
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            try:
                found = fetch_fn(result.url)
            except BrowserError as exc:
                failures.append(f"could not read {result.url}: {exc}")
                continue
            text = getattr(found, "text", "") or ""
            barriers = detect_barriers(text, result.url) if found.kind == "html" else []
            sources.append({
                "n": len(sources) + 1,
                "title": getattr(found, "title", "") or result.title,
                "url": result.url,
                "kind": found.kind,
                "text": text[:max_chars_per_source],
                "truncated": len(text) > max_chars_per_source,
                "barriers": barriers,
                "query": query,
            })
    return build_report(plan, sources, failures), sources


def build_report(plan, sources, failures):
    lines = [f"# Research: {plan['topic']}", "",
             "## Questions", ""]
    for query in plan["queries"]:
        lines.append(f"- {query}")
    lines += ["", "## What the sources say", ""]
    if not sources:
        lines.append("No sources could be read.")
    for source in sources:
        lines.append(f"### [{source['n']}] {source['title'] or source['url']}")
        lines.append(f"Source: {source['url']} ({source['kind']})")
        for barrier in source["barriers"]:
            lines.append(f"Note: {barrier['note']}")
        lines.append("")
        lines.append(source["text"] or "(no readable text)")
        if source["truncated"]:
            lines.append("[... source continues — fetch it directly for the rest]")
        lines.append("")
    if failures:
        lines += ["## Could not be read", ""]
        for failure in failures:
            lines.append(f"- {failure}")
        lines.append("")
    lines += ["## Sources", ""]
    for source in sources:
        lines.append(f"[{source['n']}] {source['url']}")
    return "\n".join(lines).rstrip() + "\n"
