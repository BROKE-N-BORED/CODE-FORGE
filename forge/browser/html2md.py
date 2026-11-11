"""HTML -> clean Markdown for a small local model to read.

This is the read engine's core, following the approach crawl4ai proved
out (page in, LLM-ready Markdown out) but written here in pure standard
library so Forge carries no new dependency for it:

* Structure is kept: headings, paragraphs, lists, blockquotes, code
  (fenced for <pre>, inline for <code>), simple tables, and links —
  links stay inline as [text](absolute-url) AND are collected into a
  separate list, because Research mode cites its sources.
* Noise is dropped: <script>, <style>, <noscript>, <svg>, <iframe>,
  and the boilerplate landmarks <nav>, <footer>, <aside> never reach
  the output.
* Whitespace is collapsed and blank runs are capped, so pages cost
  far fewer tokens than the raw HTML.

The parser is a streaming HTMLParser subclass: one pass, no DOM tree
held in memory, which keeps it light on an 8 GB machine.
"""

from html.parser import HTMLParser
from urllib.parse import urljoin

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "aside", "form", "button", "select", "textarea"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
_BLOCK_END = {"p", "div", "section", "article", "main", "blockquote", "ul", "ol", "table", "tr", "pre"} | set(_HEADINGS)


class _MarkdownParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts = []
        self.links = []
        self._seen_links = set()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._pre_depth = 0
        self._list_stack = []  # ("ul"|"ol", counter)
        self._link_href = None
        self._link_text = []
        self._heading_prefix = None
        self._row_cells = None
        self._cell_text = None

    # -- helpers ------------------------------------------------------
    def _emit(self, text):
        self.parts.append(text)

    def _newline(self, count=1):
        tail = "".join(self.parts[-4:])
        have = len(tail) - len(tail.rstrip("\n"))
        for _ in range(max(0, count - have)):
            self.parts.append("\n")

    def _add_link(self, text, href):
        absolute = urljoin(self.base_url, href)
        if absolute not in self._seen_links:
            self._seen_links.add(absolute)
            self.links.append({"text": text, "url": absolute})
        return absolute

    # -- tag handling -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._newline(2)
            self._heading_prefix = _HEADINGS[tag] + " "
            self._emit(self._heading_prefix)
        elif tag == "p":
            self._newline(2)
        elif tag == "br":
            self._newline(1)
        elif tag in ("ul", "ol"):
            self._list_stack.append([tag, 0])
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                self._list_stack[-1][1] += 1
                self._emit(f"{self._list_stack[-1][1]}. ")
            else:
                self._emit("- ")
        elif tag == "a":
            href = attrs.get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._link_href = href
                self._link_text = []
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self._emit(f" [image: {alt}] ")
        elif tag == "pre":
            self._newline(2)
            self._emit("```\n")
            self._pre_depth += 1
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag == "tr":
            self._row_cells = []
        elif tag in ("td", "th") and self._row_cells is not None:
            self._cell_text = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            self.title = self.title.strip()
        elif tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip() or self._link_href
            absolute = self._add_link(text, self._link_href)
            self._emit(f"[{text}]({absolute})")
            self._link_href = None
            self._link_text = []
        elif tag == "pre":
            self._emit("\n```")
            self._pre_depth = max(0, self._pre_depth - 1)
            self._newline(2)
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._newline(1)
        elif tag in ("td", "th") and self._cell_text is not None:
            self._row_cells.append(" ".join("".join(self._cell_text).split()))
            self._cell_text = None
        elif tag == "tr" and self._row_cells is not None:
            if any(self._row_cells):
                self._newline(1)
                self._emit("| " + " | ".join(self._row_cells) + " |")
            self._row_cells = None
        elif tag in _BLOCK_END:
            self._newline(2)

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if self._cell_text is not None:
            self._cell_text.append(data)
            return
        if self._link_href is not None:
            self._link_text.append(data)
        if self._pre_depth:
            self._emit(data)
        else:
            self._emit(" ".join(data.split()) if data.strip() else (" " if data else ""))


def html_to_markdown(html, base_url):
    """Convert an HTML string to (title, markdown, links)."""
    parser = _MarkdownParser(base_url)
    parser.feed(html)
    parser.close()
    raw = "".join(parser.parts)
    lines = [line.rstrip() for line in raw.split("\n")]
    cleaned = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            cleaned.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
    markdown = "\n".join(cleaned).strip()
    return parser.title, markdown, parser.links
