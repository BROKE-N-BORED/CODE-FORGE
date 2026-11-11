"""Forge Browser — Code Forge's own web layer.

Built for Code Forge at Joshua's order (2026-11-11): a browser layer
Forge can fully use with NO other company's API key, no account, and
no paid service anywhere in the chain — search included. Plain Python
standard library only, so it drops into the Forge tree with no new
dependencies and can be imported standalone by any future project.

What it does:

* ``search(query)`` — keyless web search (local SearXNG when
  configured, else a DuckDuckGo Lite/HTML + Bing fallback chain).
* ``fetch(url)`` — one page in, clean Markdown out, paged for small
  context windows. ``fetch_any(url)`` — any file type: PDF text,
  images, audio, video, JSON/CSV/text, binaries saved with a report.
* ``render(url)`` / ``snapshot(url)`` / ``screenshot(...)`` — a local
  browser engine over CDP (Chromium, or Lightpanda serving): the
  page after JavaScript, its accessibility snapshot with numbered
  interactive elements, real screenshots.
* Multimodal seats (media.py) — a local Ollama vision seat describes
  images into text; local whisper backends transcribe audio. Media
  is turned into text by a seat, never attached to a model that
  cannot parse it, and never described by invention when no seat
  exists.
* Barriers (barriers.py) — cookie/consent banners are dismissed by
  their own buttons; CAPTCHA / human checks and unknown logins are
  detected and handed to the person on a persistent profile, so one
  human minute makes later visits seamless.
* The vault (vault.py) — site passwords and account details in one
  encrypted file (passphrase key, encrypt-then-MAC, 0600). Logins
  are filled into pages without the values ever reaching a model's
  context or a log.
* ``verify_in_browser(target, workspace)`` — the #65 contract on
  this engine: project pages served throwaway on 127.0.0.1, text
  back, screenshot saved under .forge/artifacts/.
* Research (research.py) — plan gate first, then the search/read
  loop, then a cited Markdown report of what the sources said.

Fences: http(s) only for fetching; the CDP engine is a local process
on 127.0.0.1; requests carry no keys, cookies, or telemetry; secrets
live only in the vault and in the memory of the process using them.
"""

from .errors import BrowserError
from .fetch import Page, fetch
from .files import FileResult, fetch_any
from .render import render, screenshot, snapshot
from .research import make_plan, run_research
from .search import SearchResult, search
from .verify import verify_in_browser

__version__ = "0.2"

__all__ = [
    "BrowserError",
    "FileResult",
    "Page",
    "SearchResult",
    "fetch",
    "fetch_any",
    "make_plan",
    "render",
    "run_research",
    "screenshot",
    "search",
    "snapshot",
    "verify_in_browser",
    "__version__",
]
