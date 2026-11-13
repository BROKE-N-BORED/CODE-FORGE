"""One small HTTP GET helper shared by search and fetch.

Standard library only. Sends a User-Agent that names the tool, follows
redirects (urllib default), decompresses gzip when a server insists on
it, and turns every failure into a BrowserError in plain words. No
cookies are stored, no headers beyond User-Agent / Accept are sent,
and there is no key or token of any kind to send.

S5-fix (audit W3): http_get takes an optional `check` — a callable
the redirect handler runs on EVERY hop's destination (the Browse
fence, forge.browse.check_open, in the package engine's case). A hop
the check refuses is refused with the check's own words, and the
refused destination is never fetched. No check means today's
behavior exactly: redirects followed, the final URL returned.
"""

import gzip
import urllib.error
import urllib.parse
import urllib.request

from .errors import BrowserError

USER_AGENT = "ForgeBrowser/0.1 (Code Forge; +https://github.com/BROKE-N-BORED/CODE-FORGE)"
DEFAULT_TIMEOUT = 20  # seconds


class _FencedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that re-fences every hop: the destination
    URL (resolved against the request, so a relative Location
    header is judged absolute) passes the check before the
    redirect is followed. A refused hop raises BrowserError with
    the check's refusal — the fetch stops there. Redirect counting
    and method rules are the base class's, untouched."""

    def __init__(self, check):
        super().__init__()
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._check is not None:
            target = urllib.parse.urljoin(req.full_url, newurl)
            refusal = self._check(target)
            if refusal:
                raise BrowserError(refusal)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, timeout=DEFAULT_TIMEOUT, check=None):
    """GET ``url``; return (final_url, headers, body_bytes).

    ``check`` (optional) is a callable(url) -> None | refusal,
    run on every redirect destination before the redirect is
    followed; a refusal raises BrowserError with its words.
    Without a check the behavior is exactly today's.

    Raises BrowserError with a plain-words message on any failure.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*"},
    )
    opener = None
    if check is not None:
        # build_opener skips its default redirect handler when a
        # passed handler is an instance of it — this one is the
        # only redirect handler on the opener, so every hop passes
        # through the fence.
        opener = urllib.request.build_opener(_FencedRedirectHandler(check))
    try:
        if opener is not None:
            response = opener.open(request, timeout=timeout)
        else:
            response = urllib.request.urlopen(request, timeout=timeout)
        with response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return response.geturl(), response.headers, body
    except BrowserError:
        raise
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
