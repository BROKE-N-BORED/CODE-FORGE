"""Small Ollama HTTP client with newline-delimited streaming."""
from __future__ import annotations
import inspect
import ipaddress
import json
import socket
import threading
import urllib.parse
import urllib.request
import urllib.error


def ollama_url_warning(url: str) -> str | None:
    """S5 (audit S1-21): the save-time warning for an Ollama URL that
    points OFF this PC — or None when it does not. Every prompt,
    file content and image the agent handles is sent to whatever
    host this URL names, so a non-loopback address is a data-egress
    decision the owner should make with his eyes open. Loopback
    (localhost / 127.0.0.0/8 / ::1, any scheme) is the normal setup
    and earns no warning. A malformed URL reads as non-loopback
    (warn) — this helper never blocks; other validation owns that.
    """
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").lower()
    except Exception:
        host, scheme = "", ""
    if host:
        if host == "localhost" or host.endswith(".localhost"):
            return None
        try:
            if ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            pass
    if scheme == "http":
        return (f"The Ollama URL '{url}' points at another machine "
                "over plain http: every prompt, file content and "
                "image the agent handles will be sent there "
                "unencrypted, and its answers will be trusted as "
                "the model. The default (http://127.0.0.1:11434) "
                "keeps everything on this PC.")
    return (f"The Ollama URL '{url}' points at another machine: "
            "every prompt, file content and image the agent handles "
            "will be sent there, and its answers will be trusted "
            "as the model. The default (http://127.0.0.1:11434) "
            "keeps everything on this PC.")


class OllamaError(RuntimeError):
    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


# Note 26: some models' chat templates (multi-step tool templates) raise
# "No user query found in messages" when the payload carries no user-role
# message — possible with restored/forked conversation shapes. Two layers:
# ensure_user_message guarantees the shape at the single choke point every
# caller passes through (the last-resort backstop), and normalize_history
# repairs stored histories at load time so the guarantee holds by
# construction before a worker ever sees them.
SYNTHETIC_USER = "Continue."


def _has_user_message(messages: list[dict]) -> bool:
    return any(m.get("role") == "user" and str(m.get("content") or "").strip()
               for m in messages)


def _insert_synthetic_user(messages: list[dict]) -> list[dict]:
    """Copy of messages with the synthetic user message placed after any
    leading system messages."""
    out = list(messages)
    idx = 0
    while idx < len(out) and out[idx].get("role") == "system":
        idx += 1
    out.insert(idx, {"role": "user", "content": SYNTHETIC_USER})
    return out


def ensure_user_message(messages: list[dict]) -> list[dict]:
    """Return messages unchanged when a user message with content exists;
    otherwise return a copy with a minimal user message inserted after any
    leading system messages."""
    if _has_user_message(messages):
        return messages
    return _insert_synthetic_user(messages)


REPAIR_INSERTED_USER = "inserted synthetic user message"
REPAIR_DROPPED_ORPHAN_TOOL = "dropped orphan tool message"

# Sweep R143: a non-streaming response is read under a hard ceiling.
# The base_url is configurable, so a malfunctioning or hostile server
# must not be able to pour an unbounded body into RAM on an 8 GB box.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _read_json(response):
    """json.loads of a response body, read under the size ceiling.
    The read LOOPS: one read() is never promised to return the whole
    body (a chunked response may hand it over in pieces), so chunks
    accumulate until EOF, and the ceiling is judged on the true
    total. A response object whose read() takes no size at all (the
    test doubles') is read whole and judged the same way."""
    try:
        first = response.read(262144)
    except TypeError:
        raw = response.read()
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise OllamaError("Ollama's response was too large to load.")
        return json.loads(raw.decode("utf-8"))
    chunks = [first]
    total = len(first)
    while True:
        chunk = response.read(262144)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise OllamaError("Ollama's response was too large to load.")
        chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _response_socket(response):
    """The live socket under a urllib streaming response, or None.
    urlopen returns the HTTPResponse itself for http(s): its fp is a
    BufferedReader over a SocketIO over the socket; wrappers chain
    through .fp, so walk a few levels (fp / raw / _sock) and take the
    first real socket. Test doubles and exotic wrappers simply have
    no chain to walk and yield None."""
    obj = response
    for _ in range(6):
        if obj is None:
            return None
        if hasattr(obj, "shutdown") and hasattr(obj, "recv"):
            return obj
        sock = getattr(obj, "_sock", None)
        if sock is not None and hasattr(sock, "shutdown"):
            return sock
        obj = getattr(obj, "fp", None) or getattr(obj, "raw", None)
    return None


def force_close_response(response):
    """Break a blocked streaming read on `response` from another
    thread (sweep R4). A bare response.close() is NOT the mechanism
    for a real response: the buffered reader's close waits behind the
    very read it is meant to interrupt. Shutting the socket down
    makes the blocked read return at once and the iteration end as a
    clean EOF (probed on CPython 3.12 against a stalled local
    server); the reader's own cleanup then finishes normally, so
    close() is deliberately not called afterwards on that path. Only
    when no socket is reachable (test doubles, exotic wrappers) fall
    back to close()."""
    sock = _response_socket(response)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        return
    try:
        response.close()
    except Exception:
        pass


class CancelWatch:
    """Bridges a threading.Event to one live streaming response
    (sweep R4): when the event fires, the response is force-closed
    so a read blocked on a stalled stream breaks immediately and the
    stream ends as a clean EOF — the consumer's own per-event cancel
    checks then settle the turn exactly as they do for a cancel seen
    between events. One watch per stream; release() stands it down
    when the stream ends on its own."""

    def __init__(self, cancel_event):
        self._event = cancel_event
        self._response = None
        self._lock = threading.Lock()
        self._released = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="forge-cancel-watch")
        self._thread.start()

    def register(self, response):
        """Hand the watch its live response. A cancel that already
        fired closes it on the spot — the event may have been set
        while the request was still opening, before there was a
        response to close."""
        with self._lock:
            if self._released:
                return
            self._response = response
            fired = self._event.is_set()
        if fired:
            force_close_response(response)

    def release(self):
        """The stream is over; the watch stands down and forgets the
        response (the watcher thread exits on its next poll)."""
        with self._lock:
            self._released = True
            self._response = None

    def _run(self):
        while not self._released:
            if self._event.wait(0.25):
                break
        with self._lock:
            response = None if self._released else self._response
        if response is not None and self._event.is_set():
            force_close_response(response)


def client_accepts_cancel(client) -> bool:
    """True when the client's chat_stream takes the cancel_event
    kwarg (sweep R4). Callers pass the event ONLY to clients that
    accept it: the seam has many consumers and test doubles with the
    fixed pre-R4 signature, and an unexpected kwarg would raise
    TypeError in every one of them. A **kwargs parameter counts — it
    accepts the kwarg by construction. Anything uninspectable reads
    as legacy."""
    try:
        params = inspect.signature(client.chat_stream).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    if "cancel_event" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params.values())


def normalize_history(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """Note 26, load-time half: repair a STORED history's template-breaking
    shapes before a worker runs with it. Conservative — only two repairs:

    1. No content-bearing user message anywhere -> the synthetic user
       message is inserted after any leading system messages (the same
       one ensure_user_message uses — one implementation, two doors).
    2. Orphan tool messages — a "tool"-role message positioned before
       the first assistant message carrying tool_calls (e.g. a history
       restored mid-turn, its head a dangling tool result) -> dropped.
       Tool messages after an assistant tool_call are untouched.

    Nothing is reordered, trimmed, or edited. A healthy history comes
    back as the SAME list object with repairs == []. The input list is
    never mutated; whether the stored record on disk changes is the
    caller's business — this shapes the in-memory copy only."""
    repairs: list[str] = []
    out: list[dict] = []
    seen_tool_calls = False
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            seen_tool_calls = True
        elif m.get("role") == "tool" and not seen_tool_calls:
            repairs.append(REPAIR_DROPPED_ORPHAN_TOOL)
            continue
        out.append(m)
    if not _has_user_message(out):
        out = _insert_synthetic_user(out)
        repairs.append(REPAIR_INSERTED_USER)
    if not repairs:
        return messages, repairs
    return out, repairs


_TEMPLATE_MARKERS = ("jinja", "template", "no user query")


def friendly_http_error(code: int, detail: str) -> str | None:
    """Note 26: a template-rejection 500 becomes one plain-language line
    for the owner; the raw body rides along on OllamaError.raw so the
    worker can keep it in the session log for diagnosis."""
    if code < 500:
        return None
    low = (detail or "").lower()
    if any(marker in low for marker in _TEMPLATE_MARKERS):
        return ("The model's chat template rejected this conversation's shape "
                "(Ollama HTTP 500) — nothing was lost. Send any message to "
                "continue, or start a new conversation. The raw error is "
                "kept in the session log.")
    return None


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: int = 1800):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Dynamic context windows (2026-11-10): per-model context
        # lengths resolved from /api/show, cached for the session —
        # a show call per turn would be far too much, and a model's
        # trained window does not change under a running app. A
        # cached None (unresolvable) is also kept: keyed by model
        # name, so switching models is the only invalidation.
        self._context_lengths: dict = {}

    def _get(self, path: str):
        req = urllib.request.Request(self.base_url + path)
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return _read_json(response)
        except Exception as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc

    def tags(self):
        data = self._get("/api/tags")
        # Sweep R143: a non-dict body used to raise a bare
        # AttributeError out of .get(), escaping the OllamaError
        # contract every caller expects — ps() below already guards
        # its shape; tags() now does the same.
        if not isinstance(data, dict):
            return []
        return data.get("models", [])

    def ps(self):
        """GET /api/ps — the models Ollama currently holds in RAM,
        each a dict with name / size (bytes) / expires_at. Residency
        is a convenience surface for the owner, never an error path:
        any failure (server down, bad body) returns []."""
        req = urllib.request.Request(self.base_url + "/api/ps")
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                data = _read_json(response)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        return [m for m in data.get("models", []) if isinstance(m, dict)]

    def unload_model(self, model: str) -> bool:
        """Unload one resident model (Light Machine profile): an
        empty /api/generate request carrying keep_alive 0 — Ollama's
        own unload idiom, the same per-request field chat_stream
        sends. Returns True when the request was accepted; any
        failure (server down, bad body) returns False — residency
        management must never break a generation."""
        data = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/generate", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                response.read()
            return True
        except Exception:
            return False

    def show(self, model: str):
        data = json.dumps({"model": model}).encode()
        req = urllib.request.Request(self.base_url + "/api/show", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return _read_json(response)
        except Exception as exc:
            raise OllamaError(f"Unable to inspect model '{model}': {exc}") from exc

    def context_length(self, model: str):
        """The model's trained context length in tokens, from
        /api/show's model_info ("<arch>.context_length"), or None
        when no source states one (server down, model missing, junk
        body). Never raises — context budgeting treats None as "fall
        back to the owner's num_ctx setting", and a resolution
        failure must never break a turn. Resolved lazily and cached
        per model name for the life of this client."""
        key = str(model or "")
        if key in self._context_lengths:
            return self._context_lengths[key]
        found = None
        try:
            data = self.show(model)
        except Exception:
            data = None
        if isinstance(data, dict):
            info = data.get("model_info")
            candidates = []
            if isinstance(info, dict):
                for name, value in info.items():
                    if name == "context_length" or \
                            str(name).endswith(".context_length"):
                        candidates.append(value)
            candidates.append(data.get("context_length"))
            for value in candidates:
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and value > 0:
                    found = value
                    break
                if isinstance(value, float) and value > 0 \
                        and float(value).is_integer():
                    found = int(value)
                    break
        self._context_lengths[key] = found
        return found

    def chat_stream(self, model: str, messages: list[dict], options: dict, tools: list[dict], images: list[str] | None = None, keep_alive: str = "30m", cancel_event=None):
        messages = ensure_user_message(messages)
        # Audit B20: keep_alive is a per-request residency policy, not
        # a constant. The old hard-coded "5m" unloaded the model
        # during any untimed approval wait past five minutes, and the
        # next generation paid a full reload from disk. The session
        # default is now 30m (the worker passes the owner's setting);
        # a guest generation (the council reviewer) passes "5m" so it
        # does not squat in RAM after its one critique.
        # Sweep R135: the images ride on COPIES — the old code set
        # messages[-1]["images"] on the caller's own list (ensure_user_
        # message returns it unchanged when a user message exists), so
        # the stored history permanently gained an images key that was
        # re-sent on every later turn. The payload carries the same
        # shape it always did; the caller's dicts are never touched.
        if images and messages:
            messages = [dict(m) for m in messages]
            messages[-1] = {**messages[-1], "images": images}
        payload = {"model": model, "messages": messages, "stream": True, "options": options, "tools": tools, "keep_alive": keep_alive}
        request = urllib.request.Request(
            self.base_url + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        # Sweep R4: with a cancel event, a watch force-closes the
        # live response the moment the event fires, so a stream that
        # stalls mid-generation (no bytes for the consumer's
        # per-event cancel check to run against) still stops at
        # once. No event -> no watch -> the legacy path untouched.
        watch = CancelWatch(cancel_event) if cancel_event is not None else None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if watch is not None:
                    watch.register(response)
                for raw in response:
                    if raw.strip():
                        yield json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            friendly = friendly_http_error(exc.code, detail)
            if friendly:
                raise OllamaError(friendly, raw=f"HTTP {exc.code}: {detail}") from exc
            raise OllamaError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            # Sweep R4: a read broken by the cancel watch is a clean
            # end-of-stream, not a failure — swallowing it here lets
            # the consumer's own cancel checks settle the turn.
            if cancel_event is not None and cancel_event.is_set():
                return
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        finally:
            if watch is not None:
                watch.release()
