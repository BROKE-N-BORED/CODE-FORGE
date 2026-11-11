"""fetch(url) — one page in, clean Markdown out.

Paging is the small-model contract: the returned Page always reports
the FULL cleaned length, but its ``markdown`` carries only the slice
asked for (``offset`` / ``max_chars``). ``next_offset`` tells the
caller exactly where to resume, so a 2B model with an 8k context can
read a long page in slices instead of losing the tail silently.
"""

from dataclasses import dataclass, field
from urllib.parse import urlparse

from ._http import decode_body, http_get
from .errors import BrowserError
from .html2md import html_to_markdown

DEFAULT_MAX_CHARS = 12000


@dataclass
class Page:
    url: str          # the URL asked for
    final_url: str    # after redirects
    title: str
    markdown: str     # the slice [offset : offset + max_chars]
    links: list = field(default_factory=list)  # [{"text": ..., "url": ...}]
    total_chars: int = 0
    offset: int = 0
    truncated: bool = False
    rendered: bool = False  # True when a browser engine rendered the page (Item 2)

    @property
    def next_offset(self):
        """Where the next fetch should resume, or None when complete."""
        end = self.offset + len(self.markdown)
        return end if end < self.total_chars else None

    def to_dict(self):
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "markdown": self.markdown,
            "links": self.links,
            "total_chars": self.total_chars,
            "offset": self.offset,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "rendered": self.rendered,
        }


def fetch(url, max_chars=DEFAULT_MAX_CHARS, offset=0, timeout=20):
    """Fetch ``url`` and return a Page of clean Markdown.

    Only http(s) URLs are fetched; anything else is refused in plain
    words, matching Forge's existing Browse posture.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise BrowserError(
            f"Forge Browser only fetches http(s) pages — refused {url!r}. "
            "Local project files are checked with verify_in_browser instead."
        )
    if offset < 0:
        raise BrowserError("offset cannot be negative")
    if max_chars <= 0:
        raise BrowserError("max_chars must be at least 1")

    final_url, headers, body = http_get(url, timeout=timeout)
    content_type = ""
    if headers is not None:
        content_type = (headers.get_content_type() or "").lower()
    text = decode_body(body, headers)

    if content_type == "text/plain":
        title, markdown, links = "", text.strip(), []
    else:
        title, markdown, links = html_to_markdown(text, final_url)

    total = len(markdown)
    page_slice = markdown[offset:offset + max_chars]
    return Page(
        url=url,
        final_url=final_url,
        title=title,
        markdown=page_slice,
        links=links,
        total_chars=total,
        offset=offset,
        truncated=(offset + len(page_slice)) < total,
    )
