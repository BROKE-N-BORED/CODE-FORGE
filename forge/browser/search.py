"""search(query) — keyless web search, two interchangeable backends.

Backend order (all keyless; the first backend that answers with
results wins, a backend that answers empty or bot-walled falls
through to the next):

1. SearXNG, when a local instance is configured through the
   FORGE_SEARXNG_URL environment variable (for example
   http://127.0.0.1:8888). Self-hosted metasearch: no key, no account,
   no tracking. Its JSON format is requested directly.
2. The DuckDuckGo Lite endpoint (lite.duckduckgo.com/lite/), then
   the DuckDuckGo HTML endpoint (html.duckduckgo.com/html/). Both
   answer a plain GET with a results page and need no key or
   account. NOTE, found in the 2026-11-11 live smoke test: the HTML
   endpoint answered this sandbox's datacenter IP with its anomaly
   (bot-check) page while Lite answered normally — hence the chain.
3. The Bing HTML results page (www.bing.com/search), parsed the
   same way. No key is involved; it is a public web page, used only
   as the last fallback.

Both response parsers are pure functions of the response text, kept
separate from the network so the test suite pins them offline.
Search results carry title, URL, and snippet — the URL is the
citation Research mode will record.
"""

import json
import os
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlparse

from ._http import decode_body, http_get
from .errors import BrowserError

DEFAULT_MAX_RESULTS = 5


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""

    def to_dict(self):
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine,
        }


# -- SearXNG backend ---------------------------------------------------

def parse_searxng_json(text, max_results):
    """Parse a SearXNG ``format=json`` response into SearchResults."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrowserError("the search instance answered with invalid JSON") from exc
    results = []
    for item in data.get("results", []):
        url = item.get("url") or ""
        if not url:
            continue
        results.append(SearchResult(
            title=(item.get("title") or "").strip(),
            url=url,
            snippet=" ".join((item.get("content") or "").split()),
            engine=", ".join(item.get("engines") or []),
        ))
        if len(results) >= max_results:
            break
    return results


def search_searxng(query, max_results, base_url, timeout=20):
    endpoint = base_url.rstrip("/") + "/search?q=" + quote_plus(query) + "&format=json"
    _, headers, body = http_get(endpoint, timeout=timeout)
    results = parse_searxng_json(decode_body(body, headers), max_results)
    for result in results:
        result.engine = result.engine or "searxng"
    return results


# -- DuckDuckGo HTML backend --------------------------------------------

class _DDGParser(HTMLParser):
    """Pull results out of the DDG HTML and Lite result pages.

    HTML endpoint: <a class="result__a"> titles, <a class="result__snippet">
    snippets. Lite endpoint: <a class="result-link"> titles inside table
    cells, <td class="result-snippet"> snippets. One parser covers both.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._current = None
        self._capture = None  # "title" | "snippet"
        self._capture_tag = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        href = dict(attrs).get("href") or ""
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._current = {"title": "", "url": _unwrap_ddg(href), "snippet": ""}
            self._capture = "title"
            self._capture_tag = "a"
            self.results.append(self._current)
        elif "result__snippet" in classes and self.results:
            self._current = self.results[-1]
            self._capture = "snippet"
            self._capture_tag = tag
        elif tag == "td" and "result-snippet" in classes and self.results:
            self._current = self.results[-1]
            self._capture = "snippet"
            self._capture_tag = "td"

    def handle_endtag(self, tag):
        if self._capture and tag == self._capture_tag:
            self._capture = None
            self._capture_tag = None

    def handle_data(self, data):
        if self._capture and self._current is not None:
            self._current[self._capture] += data


def _unwrap_ddg(href):
    """DDG result links wrap the real URL in a ``uddg`` parameter."""
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    if "uddg" in params:
        return params["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_ddg_html(html, max_results):
    parser = _DDGParser()
    parser.feed(html)
    parser.close()
    results = []
    for item in parser.results:
        if not item["url"]:
            continue
        results.append(SearchResult(
            title=" ".join(item["title"].split()),
            url=item["url"],
            snippet=" ".join(item["snippet"].split()),
            engine="duckduckgo",
        ))
        if len(results) >= max_results:
            break
    return results


def search_ddg(query, max_results, timeout=20, host="lite.duckduckgo.com", path="/lite/"):
    endpoint = f"https://{host}{path}?q=" + quote_plus(query)
    _, headers, body = http_get(endpoint, timeout=timeout)
    results = parse_ddg_html(decode_body(body, headers), max_results)
    for result in results:
        result.engine = "duckduckgo-lite" if host.startswith("lite") else "duckduckgo"
    return results


# -- Bing HTML backend (last fallback) ------------------------------------

class _BingParser(HTMLParser):
    """Pull results out of a Bing results page: each <li class="b_algo">
    holds an <h2><a> title link and a <p> snippet."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._current = None
        self._in_algo = 0
        self._in_h2 = False
        self._capture = None  # "title" | "snippet"

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._in_algo += 1
            self._current = {"title": "", "url": "", "snippet": ""}
            self.results.append(self._current)
            return
        if not self._in_algo or self._current is None:
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._current["url"]:
            self._current["url"] = _unwrap_bing(attrs.get("href") or "")
            self._capture = "title"
        elif tag == "p" and not self._current["snippet"]:
            self._capture = "snippet"

    def handle_endtag(self, tag):
        if tag == "li" and self._in_algo:
            self._in_algo -= 1
            if not self._in_algo:
                self._current = None
        elif tag == "h2":
            self._in_h2 = False
        elif tag in ("a", "p"):
            self._capture = None

    def handle_data(self, data):
        if self._capture and self._current is not None:
            self._current[self._capture] += data


def _unwrap_bing(href):
    """Bing sometimes wraps results in /ck/a links carrying the real
    URL base64-encoded in the ``u`` parameter (prefixed ``a1``)."""
    if "/ck/a" not in href:
        return href
    params = parse_qs(urlparse(href).query)
    encoded = (params.get("u") or [""])[0]
    if encoded.startswith("a1"):
        import base64
        try:
            padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            return href
    return href


def parse_bing_html(html, max_results):
    parser = _BingParser()
    parser.feed(html)
    parser.close()
    results = []
    for item in parser.results:
        if not item["url"] or not item["title"].strip():
            continue
        results.append(SearchResult(
            title=" ".join(item["title"].split()),
            url=item["url"],
            snippet=" ".join(item["snippet"].split()),
            engine="bing",
        ))
        if len(results) >= max_results:
            break
    return results


def search_bing(query, max_results, timeout=20):
    endpoint = "https://www.bing.com/search?q=" + quote_plus(query)
    _, headers, body = http_get(endpoint, timeout=timeout)
    return parse_bing_html(decode_body(body, headers), max_results)


# -- Public entry point ---------------------------------------------------

def search(query, max_results=DEFAULT_MAX_RESULTS, timeout=20):
    """Search the web with no API key. Returns a list of SearchResult.

    Uses the configured local SearXNG instance when FORGE_SEARXNG_URL
    is set, otherwise the DuckDuckGo HTML endpoint. An empty result
    list means the backend answered with no results — it is reported
    as-is, never filled in.
    """
    if not query or not query.strip():
        raise BrowserError("search needs a query")
    if max_results <= 0:
        raise BrowserError("max_results must be at least 1")
    query = query.strip()
    searxng_url = os.environ.get("FORGE_SEARXNG_URL", "").strip()
    if searxng_url:
        return search_searxng(query, max_results, searxng_url, timeout=timeout)
    # Keyless fallback chain: first backend with results wins. A
    # backend that errors or answers empty (bot-check pages answer
    # 200 with no results) falls through to the next.
    backends = [
        lambda: search_ddg(query, max_results, timeout=timeout),
        lambda: search_ddg(query, max_results, timeout=timeout,
                           host="html.duckduckgo.com", path="/html/"),
        lambda: search_bing(query, max_results, timeout=timeout),
    ]
    last_error = None
    for backend in backends:
        try:
            results = backend()
        except BrowserError as exc:
            last_error = exc
            continue
        if results:
            return results
    if last_error is not None:
        raise last_error
    return []
