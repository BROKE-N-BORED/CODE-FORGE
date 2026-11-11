"""Item 2 — the rendered-page engine: CDP over the local WebSocket.

Talks the Chrome DevTools Protocol to a browser engine running on
this machine: a Chromium-family browser Forge launches headless, or
any CDP endpoint already serving (Lightpanda — Item 4 — or a browser
the user started with --remote-debugging-port). No network service,
no key: the engine is a local process and the protocol is a local
WebSocket (forge/browser/_ws.py, standard library only).

What a session can do:

* navigate(url) and wait for the load event
* html() / page() — the RENDERED page, converted by the same
  html_to_markdown the plain fetch uses, so JavaScript-built pages
  read the way a person sees them
* ax_snapshot() — the page as an indented accessibility tree (role +
  name lines), the compact shape a small local model can act on
* elements() — the interactive elements, numbered; click(i) and
  type_text(i, ...) act by number, the browser-use / playwright-mcp
  pattern, so a small model never has to write selectors
* screenshot(path) — a real PNG via Page.captureScreenshot,
  validated against the PNG signature before it is saved. Engine
  support varies: the Lightpanda nightly tested on 2026-11-11 did
  return a valid 1920x1080 PNG, with basic rendering (unstyled
  layout, some missing font glyphs); Chromium is the full-fidelity
  engine. An engine that cannot capture refuses in plain words.

Resource rules for an 8 GB machine: ONE browser process per session,
one page target, a temporary profile directory deleted on close,
and every wait carries a timeout.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

from ._http import USER_AGENT
from ._ws import WebSocket
from .errors import BrowserError
from .html2md import html_to_markdown

_CHROMIUM_NAMES = [
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "brave-browser", "microsoft-edge", "microsoft-edge-stable",
]

_ELEMENTS_JS = """
(() => {
  const els = [...document.querySelectorAll(
    'a,button,input,select,textarea,[role="button"],[onclick]')]
    .filter(e => { const s = getComputedStyle(e);
      return s.display !== 'none' && s.visibility !== 'hidden'; });
  window.__forgeEls = els;
  return JSON.stringify(els.map((e, i) => ({
    i: i, tag: e.tagName.toLowerCase(),
    type: e.getAttribute('type') || '',
    text: (e.innerText || e.value || e.getAttribute('aria-label')
           || e.placeholder || '').trim().slice(0, 80),
    href: e.getAttribute('href') || '',
    name: e.getAttribute('name') || ''
  })));
})()
"""


def find_chromium():
    """Path of a Chromium-family browser on this machine, or None."""
    override = os.environ.get("FORGE_CHROMIUM_PATH", "").strip()
    if override:
        return override if os.path.exists(override) else None
    for name in _CHROMIUM_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


class CDPClient:
    """Synchronous CDP caller over one WebSocket.

    Replies are matched by id; events (messages with a method and no
    id) are stashed for wait_event. One session id may be attached —
    after Target.attachToTarget with flatten, page calls carry it.
    """

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 0
        self._events = []
        self.session_id = None

    def call(self, method, params=None, timeout=20):
        self._next_id += 1
        call_id = self._next_id
        message = {"id": call_id, "method": method, "params": params or {}}
        if self.session_id:
            message["sessionId"] = self.session_id
        self._ws.send_text(json.dumps(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv_message()
            data = json.loads(raw)
            if data.get("id") == call_id:
                if "error" in data:
                    raise BrowserError(
                        f"the browser engine refused {method}: "
                        f"{data['error'].get('message', data['error'])}")
                return data.get("result") or {}
            if "method" in data:
                self._events.append(data)
        raise BrowserError(f"the browser engine did not answer {method} within {timeout}s")

    def wait_event(self, method, timeout=15):
        """True when the named event arrives (or already arrived)."""
        deadline = time.monotonic() + timeout
        while True:
            for index, event in enumerate(self._events):
                if event.get("method") == method and (
                        not self.session_id or event.get("sessionId") in (None, self.session_id)):
                    del self._events[index]
                    return True
            if time.monotonic() >= deadline:
                return False
            # Poll the socket briefly by attempting a no-op read cycle:
            # recv would block past the deadline, so use a short call.
            self._ws._sock.settimeout(0.25)
            try:
                raw = self._ws.recv_message()
            except Exception:
                continue
            finally:
                self._ws._sock.settimeout(None)
            data = json.loads(raw)
            if "method" in data:
                self._events.append(data)
            elif "id" in data:
                self._events.append(data)  # a stray reply; harmless to keep

    def close(self):
        self._ws.close()


class BrowserSession:
    """One engine, one page. Use as a context manager."""

    def __init__(self, client, process=None, profile_dir=None, engine_name="chromium",
                 persistent=False):
        self._client = client
        self._process = process
        self._profile_dir = profile_dir
        self._persistent = persistent
        self.engine_name = engine_name
        self._target_id = None

    # -- startup ------------------------------------------------------
    @classmethod
    def start(cls, engine="auto", timeout=25, profile_dir=None, headed=False):
        """Open a session.

        engine: "auto" (FORGE_CDP_URL if set, else FORGE_LIGHTPANDA_URL,
        else launch Chromium), "chromium", or an explicit ws:// / http://
        CDP endpoint for an engine already running.

        profile_dir: a PERSISTENT profile folder to use instead of a
        throwaway one — cookies and logins survive between sessions,
        which is what makes the human handoff work (the person clears
        a gate or logs in once, later sessions reuse it). Persistent
        profiles are never deleted on close.
        headed: launch with a visible window (the handoff mode, for
        the person to clear a human check); default headless.
        """
        cdp_http = None
        engine_name = "chromium"
        process = None
        persistent_profile = profile_dir
        if engine == "auto":
            cdp_http = (os.environ.get("FORGE_CDP_URL")
                        or os.environ.get("FORGE_LIGHTPANDA_URL") or "").strip() or None
            if os.environ.get("FORGE_LIGHTPANDA_URL") and not os.environ.get("FORGE_CDP_URL"):
                engine_name = "lightpanda"
        elif engine.startswith(("http://", "ws://")):
            cdp_http = engine
            engine_name = "cdp"
        elif engine in ("chromium", "lightpanda"):
            engine_name = engine
            if engine == "lightpanda":
                cdp_http = (os.environ.get("FORGE_LIGHTPANDA_URL") or "").strip() or None
                if not cdp_http:
                    raise BrowserError(
                        "Lightpanda is not serving. Start it with "
                        "`lightpanda serve --port 9222` or run browser-setup.sh "
                        "(Item 4), or use the chromium engine.")
        if cdp_http is None and engine_name in ("chromium", "auto"):
            binary = find_chromium()
            if not binary:
                raise BrowserError(
                    "no Chromium-family browser found on this machine "
                    "(looked for: " + ", ".join(_CHROMIUM_NAMES) + "). "
                    "Install Chromium, or run an engine with a CDP port and "
                    "set FORGE_CDP_URL — browser-setup.sh (Item 4) installs "
                    "the Lightpanda engine.")
            port = _free_port()
            if persistent_profile:
                os.makedirs(persistent_profile, exist_ok=True)
                profile_dir = persistent_profile
            else:
                profile_dir = tempfile.mkdtemp(prefix="forge-browser-profile-")
            args = [binary, "--disable-gpu", "--no-sandbox",
                    "--no-first-run", "--disable-dev-shm-usage",
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    f"--user-agent={USER_AGENT}", "about:blank"]
            if not headed:
                args.insert(1, "--headless=new")
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            cdp_http = f"http://127.0.0.1:{port}"
        # Resolve the browser-level WebSocket address.
        try:
            version = _http_json(cdp_http.rstrip("/") + "/json/version", timeout=10)
            ws_url = version["webSocketDebuggerUrl"]
        except Exception as exc:
            if process:
                process.terminate()
            raise BrowserError(f"the browser engine at {cdp_http} did not answer ({exc})") from exc
        client = CDPClient(WebSocket.connect(ws_url, timeout=15))
        session = cls(client, process=process, profile_dir=profile_dir,
                      engine_name=engine_name, persistent=bool(persistent_profile))
        try:
            target = client.call("Target.createTarget", {"url": "about:blank"}, timeout=timeout)
            session._target_id = target["targetId"]
            attached = client.call(
                "Target.attachToTarget",
                {"targetId": session._target_id, "flatten": True}, timeout=timeout)
            client.session_id = attached["sessionId"]
            client.call("Page.enable", timeout=timeout)
        except BrowserError:
            session.close()
            raise
        return session

    # -- page actions ---------------------------------------------------
    def navigate(self, url, timeout=25):
        self._client._events = [e for e in self._client._events
                                if e.get("method") != "Page.loadEventFired"]
        self._client.call("Page.navigate", {"url": url}, timeout=timeout)
        if not self._client.wait_event("Page.loadEventFired", timeout=timeout):
            # Some engines answer slowly or fire the event early; the
            # page is still usable — but say so rather than hide it.
            raise BrowserError(f"the page at {url} did not finish loading within {timeout}s")

    def _evaluate(self, expression, timeout=20):
        result = self._client.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True}, timeout=timeout)
        payload = result.get("result") or {}
        if "exceptionDetails" in result:
            raise BrowserError("the page refused a browser action (its scripts raised an error)")
        return payload.get("value")

    def html(self):
        return self._evaluate("document.documentElement.outerHTML") or ""

    def title(self):
        return self._evaluate("document.title") or ""

    def page(self, url=None, max_chars=12000, offset=0):
        """The rendered page as a fetch.Page (same shape as fetch())."""
        from .fetch import Page
        if url:
            self.navigate(url)
        html = self.html()
        final_url = self._evaluate("location.href") or (url or "")
        title, markdown, links = html_to_markdown(html, final_url)
        total = len(markdown)
        return Page(url=url or final_url, final_url=final_url, title=title,
                    markdown=markdown[offset:offset + max_chars], links=links,
                    total_chars=total, offset=offset,
                    truncated=(offset + max_chars) < total, rendered=True)

    def ax_snapshot(self, max_lines=200):
        """The accessibility tree as indented role/name lines."""
        try:
            self._client.call("Accessibility.enable")
            result = self._client.call("Accessibility.getFullAXTree")
            nodes = result.get("nodes") or []
        except BrowserError:
            nodes = []
        if not nodes:
            # Fallback for engines without the Accessibility domain:
            # the numbered interactive elements still describe the page.
            elements = self.elements()
            lines = ["(accessibility tree unavailable on this engine; interactive elements:)"]
            lines += [f"[{e['i']}] {e['tag']} {e['text']}".rstrip() for e in elements]
            return "\n".join(lines[:max_lines])
        return "\n".join(format_ax_tree(nodes)[:max_lines])

    def elements(self):
        raw = self._evaluate(_ELEMENTS_JS)
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []

    def click(self, index):
        self.elements()  # refresh window.__forgeEls for this page state
        outcome = self._evaluate(
            f"(() => {{ const e = window.__forgeEls && window.__forgeEls[{int(index)}];"
            " if (!e) return 'missing'; e.click(); return 'clicked'; })()")
        if outcome != "clicked":
            raise BrowserError(f"element {index} is not on the page anymore — take a fresh snapshot")
        return True

    def type_text(self, index, text):
        self.elements()
        literal = json.dumps(text)
        outcome = self._evaluate(
            f"(() => {{ const e = window.__forgeEls && window.__forgeEls[{int(index)}];"
            " if (!e) return 'missing'; e.focus();"
            " const tag = e.tagName.toLowerCase();"
            " if (tag === 'input' || tag === 'textarea') {"
            "   const setter = Object.getOwnPropertyDescriptor("
            "     Object.getPrototypeOf(e), 'value').set;"
            f"   setter.call(e, {literal});"
            "   e.dispatchEvent(new Event('input', {bubbles: true}));"
            "   e.dispatchEvent(new Event('change', {bubbles: true}));"
            f" }} else if (e.isContentEditable) {{ e.textContent = {literal}; }}"
            " else return 'not-typeable'; return 'typed'; })()")
        if outcome == "missing":
            raise BrowserError(f"element {index} is not on the page anymore — take a fresh snapshot")
        if outcome == "not-typeable":
            raise BrowserError(f"element {index} is not a field text can be typed into")
        return True

    def fill_login(self, username, password, submit=False):
        """Type a login into the page's fields WITHOUT reporting values.

        Finds the password field and the nearest preceding text/email
        field, types the credentials, and returns only field counts —
        the values go to the page and nowhere else: not into the
        return value, not into any log. The vault (forge/browser/
        vault.py) is the intended caller.
        """
        elements = self.elements()
        password_index = None
        user_index = None
        for element in elements:
            if element.get("tag") == "input" and element.get("type") == "password":
                password_index = element["i"]
                break
            if element.get("tag") == "input" and element.get("type") in (
                    "text", "email", ""):
                hint = ((element.get("name") or "") + " " +
                        (element.get("text") or "")).lower()
                if user_index is None or any(
                        word in hint for word in ("user", "email", "login", "account")):
                    user_index = element["i"]
        if password_index is None:
            raise BrowserError("no password field on this page — is this the login page?")
        filled = 0
        if user_index is not None and username:
            self.type_text(user_index, username)
            filled += 1
        self.type_text(password_index, password)
        filled += 1
        if submit:
            for element in self.elements():
                label = (element.get("text") or "").lower()
                if element.get("tag") in ("button", "input") and any(
                        word in label for word in ("log in", "login", "sign in", "submit")):
                    self.click(element["i"])
                    break
            else:
                self.press_enter()
        return {"filled_fields": filled, "submitted": bool(submit)}

    def press_enter(self):
        self._client.call("Input.dispatchKeyEvent",
                          {"type": "keyDown", "key": "Enter", "code": "Enter",
                           "windowsVirtualKeyCode": 13})
        self._client.call("Input.dispatchKeyEvent",
                          {"type": "keyUp", "key": "Enter", "code": "Enter",
                           "windowsVirtualKeyCode": 13})
        return True

    def screenshot(self, path):
        try:
            result = self._client.call("Page.captureScreenshot", {"format": "png"}, timeout=30)
        except BrowserError as exc:
            raise BrowserError(
                f"this engine could not take a screenshot: {exc}") from exc
        data = result.get("data")
        if not data:
            raise BrowserError("the browser engine returned no screenshot data")
        raw = base64.b64decode(data)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BrowserError("the browser engine's screenshot was not a PNG — not saved")
        with open(path, "wb") as handle:
            handle.write(raw)
        return path

    # -- teardown ---------------------------------------------------------
    def close(self):
        try:
            if self._target_id:
                self._client.session_id = None
                self._client.call("Target.closeTarget", {"targetId": self._target_id}, timeout=5)
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        if self._profile_dir and not self._persistent:
            shutil.rmtree(self._profile_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def format_ax_tree(nodes):
    """Render CDP accessibility nodes as indented role/name lines."""
    by_id = {node.get("nodeId"): node for node in nodes}
    children = {}
    referenced = set()
    for node in nodes:
        kids = node.get("childIds") or []
        children[node.get("nodeId")] = kids
        referenced.update(kids)
    roots = [node for node in nodes if node.get("nodeId") not in referenced] or nodes[:1]
    lines = []

    def visit(node, depth):
        if node.get("ignored"):
            for kid_id in children.get(node.get("nodeId"), []):
                if kid_id in by_id:
                    visit(by_id[kid_id], depth)
            return
        role = ((node.get("role") or {}).get("value") or "unknown")
        name = ((node.get("name") or {}).get("value") or "").strip()
        lines.append("  " * depth + f"- {role}" + (f": {name}" if name else ""))
        for kid_id in children.get(node.get("nodeId"), []):
            if kid_id in by_id:
                visit(by_id[kid_id], depth + 1)

    for root in roots:
        visit(root, 0)
    return lines
