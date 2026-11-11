"""One small HTTP GET helper shared by search and fetch.

Standard library only. Sends a User-Agent that names the tool, follows
redirects (urllib default), decompresses gzip when a server insists on
it, and turns every failure into a BrowserError in plain words. No
cookies are stored, no headers beyond User-Agent / Accept are sent,
and there is no key or token of any kind to send.
"""

import gzip
import urllib.error
import urllib.request

from .errors import BrowserError

USER_AGENT = "ForgeBrowser/0.1 (Code Forge; +https://github.com/BROKE-N-BORED/CODE-FORGE)"
DEFAULT_TIMEOUT = 20  # seconds


def http_get(url, timeout=DEFAULT_TIMEOUT):
    """GET ``url``; return (final_url, headers, body_bytes).

    Raises BrowserError with a plain-words message on any failure.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return response.geturl(), response.headers, body
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"the site answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BrowserError(f"could not reach {url} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise BrowserError(f"timed out after {timeout}s reading {url}") from exc


def decode_body(body, headers):
    """Decode bytes using the declared charset, falling back to UTF-8."""
    charset = "utf-8"
    if headers is not None:
        try:
            declared = headers.get_content_charset()
        except Exception:
            declared = None
        if declared:
            charset = declared
    return body.decode(charset, errors="replace")
