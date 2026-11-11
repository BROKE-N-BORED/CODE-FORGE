"""render(url) — the page after JavaScript, as a fetch.Page.

Uses a local browser engine over CDP (forge/browser/cdp.py). When no
engine is available the error says so in plain words and names the
fixes (install Chromium, or run browser-setup.sh for Lightpanda) —
it never silently falls back to the unrendered fetch, because a
page that needs rendering would then read as empty and the model
would trust it.
"""

from .cdp import BrowserSession
from .errors import BrowserError


def render(url, engine="auto", max_chars=12000, offset=0, timeout=25):
    """Render ``url`` in a local engine and return a Page (rendered=True)."""
    if not url.startswith(("http://", "https://")):
        raise BrowserError(f"render only takes http(s) pages — refused {url!r}")
    with BrowserSession.start(engine=engine, timeout=timeout) as session:
        return session.page(url, max_chars=max_chars, offset=offset)


def snapshot(url, engine="auto", timeout=25):
    """The page's accessibility snapshot plus its numbered elements."""
    with BrowserSession.start(engine=engine, timeout=timeout) as session:
        session.navigate(url)
        return {
            "url": url,
            "engine": session.engine_name,
            "title": session.title(),
            "snapshot": session.ax_snapshot(),
            "elements": session.elements(),
        }


def screenshot(url, path, engine="auto", timeout=25):
    """Save a PNG screenshot of ``url`` to ``path``; returns the path."""
    with BrowserSession.start(engine=engine, timeout=timeout) as session:
        session.navigate(url)
        return session.screenshot(path)
