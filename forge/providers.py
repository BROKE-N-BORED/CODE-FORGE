"""Cloud provider seam for Code Forge (items #59-#62, maintenance #69).

Forge's worker, council, tester, SKRIBE and summarizer all consume one
client shape: ``chat_stream(model, messages, options, tools, ...)``
yielding Ollama-shaped events whose message carries ``content`` and
``tool_calls``.  This module keeps that shape while allowing a model ID
to name a hosted provider:

* ``groq:<model>``   -> Groq's OpenAI-compatible API
* ``gemini:<model>`` -> Google Gemini's generateContent API
* ``exaone:<model>`` -> LG EXAONE's OpenAI-compatible API
* anything else      -> the local Ollama client (including ``ollama:``)

The registry is deliberately small and data-only.  Entries live in the
connector config under ``model_registry`` and say whether a provider
model is enabled, which session scope may use it (project, maintenance,
or both), and which roles it may fill.  A provider model with no entry
gets the conservative default: enabled, both scopes, driver only.  The
registry never overrides forge/seats.py: a cloud model still needs the
owner's governing roster before it can hold a governing seat, and the
window applies both gates.

Provider access is allowlist-only (owner's order, 2026-11-10): a
provider model is usable only when its bare ID is listed in
``APPROVED_PROVIDER_MODELS`` below — the six best free models for
Code Forge (three each for Groq and Gemini), plus LG EXAONE's
k-exaone_v2.5, added by the owner's explicit order later the same
day when EXAONE became a third provider.  Catalogs
are filtered to that list, ``model_allowed`` refuses anything else
before the registry is consulted (a registry entry cannot resurrect
an unapproved model), and the facade refuses to chat with an
unapproved provider model.  Local Ollama models are unaffected.

API keys are read from the connector config (``groq_api_key`` /
``gemini_api_key`` / ``exaone_api_key``).  They are sent only in
provider request headers, never placed in prompts, logs, manifests,
or error messages.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .ollama_client import (CancelWatch, OllamaClient, OllamaError,
                            ensure_user_message)


PROVIDERS = ("groq", "gemini", "exaone")
PROVIDER_LABELS = {"groq": "Groq", "gemini": "Gemini",
                   "exaone": "EXAONE"}
# The provider allowlist (owner's order, 2026-11-10): the only bare
# model IDs Code Forge accepts from each provider.  Everything else
# a provider catalog or registry names is refused — see the module
# docstring.  Local Ollama models are not listed here and are never
# gated by this table.
APPROVED_PROVIDER_MODELS = {
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b",
             "qwen/qwen3.8-27b"),
    "gemini": ("gemini-3.8-flash", "gemini-3.6-flash",
               "gemini-3.5-flash-lite"),
    # Owner's explicit addition, 2026-11-10: EXAONE as a third
    # provider, with its one free model.
    "exaone": ("k-exaone_v2.5",),
}
VALID_SCOPES = ("project", "maintenance", "both")
VALID_ROLES = ("driver", "reviewer", "tester", "grader")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def split_model_id(model) -> tuple[str, str]:
    """Return (provider, bare_model_id).

    Only the exact ``groq:`` / ``gemini:`` / ``exaone:`` / ``ollama:``
    prefixes name a provider.  Every other colon belongs to an Ollama
    tag (for example ``llama3.2:latest``), so those IDs stay local.
    """
    text = str(model or "").strip()
    lower = text.lower()
    for provider in PROVIDERS + ("ollama",):
        prefix = provider + ":"
        if lower.startswith(prefix):
            return provider, text[len(prefix):].strip()
    return "ollama", text


def is_provider_model(model) -> bool:
    return split_model_id(model)[0] in PROVIDERS


def is_approved_provider_model(model) -> bool:
    """True only for a provider model on the allowlist.

    Works on prefixed IDs (``groq:openai/gpt-oss-120b``) via
    split_model_id.  Local models are never "approved provider
    models" — they are outside this gate entirely and return False
    here; callers that mean "may this model be used at all" must
    keep treating locals as always allowed (as model_allowed does).
    """
    provider, bare = split_model_id(model)
    if provider not in PROVIDERS:
        return False
    return bare in APPROVED_PROVIDER_MODELS.get(provider, ())


def provider_model_id(provider, model) -> str:
    """Normalize a provider-specific model field to ``provider:id``."""
    provider = str(provider or "").strip().lower()
    text = str(model or "").strip()
    if not text:
        return ""
    existing, bare = split_model_id(text)
    if existing in PROVIDERS + ("ollama",):
        # A provider-specific field names that provider's model even
        # when the owner pasted another prefix by mistake.
        text = bare
    if provider in PROVIDERS:
        return f"{provider}:{text}"
    return text


def _clean_model_id(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 300:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    return text


def _clean_context_window(value):
    """A stated context window in tokens, or None. Accepts a real
    int, an integral float, or a pure-digit string (an owner may
    hand-edit the registry JSON); bools, junk, zero/negatives and
    absurd magnitudes are absent, never coerced — context budgeting
    treats None as "no source stated one", and inventing a window
    is the one failure this feature must never have."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and float(value).is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    if number <= 0 or number > 2 ** 31:
        return None
    return number


def sanitize_model_registry(entries) -> list[dict]:
    """Return a safe copy of a model-registry list.

    Malformed entries are dropped individually.  Hostile shapes (a
    dict/list as an ID, string role blobs, unknown scopes or roles)
    cannot smuggle permissions into the result: unknown scopes fall
    back to ``both`` only for an otherwise valid entry, unknown roles
    are discarded, and an entry left with no valid roles falls back to
    driver-only.
    """
    if isinstance(entries, dict):
        entries = entries.get("model_registry")
    if not isinstance(entries, (list, tuple)):
        return []
    clean: list[dict] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        model_id = _clean_model_id(raw.get("id"))
        if model_id is None or model_id in seen:
            continue
        seen.add(model_id)
        enabled_raw = raw.get("enabled", True)
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        elif isinstance(enabled_raw, int) and enabled_raw in (0, 1):
            enabled = bool(enabled_raw)
        else:
            enabled = True
        scope = str(raw.get("scope") or "both").strip().lower()
        if scope not in VALID_SCOPES:
            scope = "both"
        roles_raw = raw.get("roles", ["driver"])
        if isinstance(roles_raw, str):
            roles_raw = [roles_raw]
        roles: list[str] = []
        if isinstance(roles_raw, (list, tuple, set)):
            for role in roles_raw:
                role_text = str(role).strip().lower()
                if role_text in VALID_ROLES and role_text not in roles:
                    roles.append(role_text)
        if not roles:
            roles = ["driver"]
        entry = {"id": model_id, "enabled": enabled,
                 "scope": scope, "roles": roles}
        # Dynamic context windows (2026-11-10): a stated window
        # rides the entry so budgeting can resolve per model. Junk
        # is simply absent — never coerced, never invented.
        window = _clean_context_window(raw.get("context_window"))
        if window is not None:
            entry["context_window"] = window
        clean.append(entry)
    return clean


# A shorter alias for callers that think in terms of "the registry".
sanitize_registry = sanitize_model_registry


def resolve_context_budget(model, config=None, client=None,
                           fallback=8192) -> int:
    """The effective context budget (tokens) for ONE model — the
    dynamic context-window resolution (2026-11-10). Until now every
    budget in Forge keyed off the owner's single num_ctx setting, so
    a provider model with a ~1M window folded at ~7.4k tokens and a
    small-window model could be handed a prompt it must refuse.

    Sources, in order — a stated value or nothing:
    - Provider models: the registry entry's stated context_window
      (owner-persisted from the provider's own catalog), then the
      facade's catalog cache (populated whenever the catalog was
      fetched this session). The stated window IS the budget.
    - Local models: the model's trained context length from
      Ollama's /api/show (the client's cached context_length()),
      capped by the owner's setting — effective budget is
      min(model max, setting), because for locals the setting is
      the owner's RAM/KV cap, not just a preference.
    - Anything unknown: the fallback (the owner's setting) exactly
      as before. Never a guess, never bigger than a source stated.
    Never raises: budgeting must not be able to break a turn."""
    try:
        setting = int(fallback)
    except (TypeError, ValueError):
        setting = 8192
    if setting <= 0:
        setting = 8192
    provider, _bare = split_model_id(model)
    if provider in PROVIDERS:
        entry = None
        try:
            entry = registry_entry_for(config, model)
        except Exception:
            entry = None
        if entry is not None:
            window = _clean_context_window(entry.get("context_window"))
            if window is not None:
                return window
        catalog = getattr(client, "catalog_context_window", None)
        if callable(catalog):
            try:
                window = _clean_context_window(catalog(model))
            except Exception:
                window = None
            if window is not None:
                return window
        return setting
    length = getattr(client, "context_length", None)
    if callable(length):
        try:
            model_max = _clean_context_window(length(model))
        except Exception:
            model_max = None
        if model_max is not None:
            return min(model_max, setting)
    return setting


def registry_entries(config) -> list[dict]:
    if isinstance(config, dict):
        return sanitize_model_registry(config.get("model_registry"))
    return sanitize_model_registry(config)


def registry_entry_for(config, model) -> dict | None:
    text = str(model or "").strip()
    for entry in registry_entries(config):
        if entry["id"] == text:
            return entry
    return None


def default_registry_entry(model) -> dict:
    return {"id": str(model or "").strip(), "enabled": True,
            "scope": "both", "roles": ["driver"]}


def model_allowed(config, model, scope="project", role="driver") -> bool:
    """The registry gate for one model in one scope and role.

    Local models are outside the registry and always pass here; the
    governing roster in seats.py remains their authority for governing
    seats.  Provider models must first be on the allowlist
    (``APPROVED_PROVIDER_MODELS``): an unapproved provider model is
    refused before any registry lookup, so no registry entry — not
    even an enabled one — can resurrect it.  Approved provider models
    use their explicit entry when present and the driver-only default
    when absent.
    """
    provider, _bare = split_model_id(model)
    if provider not in PROVIDERS:
        return True
    if not is_approved_provider_model(model):
        return False
    scope = str(scope or "").strip().lower()
    role = str(role or "").strip().lower()
    if scope not in ("project", "maintenance") or role not in VALID_ROLES:
        return False
    entry = registry_entry_for(config, model) or default_registry_entry(model)
    if not entry.get("enabled", False):
        return False
    entry_scope = entry.get("scope", "both")
    if entry_scope != "both" and entry_scope != scope:
        return False
    return role in entry.get("roles", [])


def _config_bool(config, key, default=True):
    if not isinstance(config, dict) or key not in config:
        return default
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return default


def provider_enabled(config, provider) -> bool:
    return _config_bool(config, f"{provider}_enabled", True)


def provider_api_key(config, provider) -> str:
    """The provider's API key: the encrypted vault first, then the
    connector config dict (back-compat for setups whose keys have not
    been migrated yet).  A vault problem reads as "no vault key" —
    the config fallback still applies."""
    from . import vault as vault_mod
    stored = vault_mod.get_secret(f"{provider}_api_key")
    if stored:
        return stored.strip()
    if not isinstance(config, dict):
        return ""
    return str(config.get(f"{provider}_api_key") or "").strip()


def _max_tokens_cap_from_error(text):
    """The per-model output cap named in a provider's max_tokens
    refusal (Groq's shape: "`max_tokens` must be less than or equal
    to `512`"), or None when the text names no cap. Plain string
    surgery on purpose: the refusal is a fixed provider sentence,
    and a parse miss just means no retry. The error text reaches
    here already key-scrubbed, and a short key can eat a character
    inside "max_tokens" itself ("max_to[redacted]ens") — so the
    surviving skeleton is accepted as the marker too."""
    text = str(text or "")
    if "max_tokens" not in text and "max_to" not in text:
        return None
    phrase = text.find("less than or equal to")
    if phrase < 0:
        return None
    digits = ""
    for ch in text[phrase + len("less than or equal to"):]:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    try:
        cap = int(digits)
    except ValueError:
        return None
    return cap if cap > 0 else None


class _ProviderHTTP:
    """Shared HTTP/SSE plumbing for the hosted clients.

    ``urlopen`` is injectable so the whole provider layer can be tested
    offline with scripted responses.  Error text is passed through the
    key scrubber before it can reach a log or dialog.
    """

    provider = ""
    label = "Provider"
    timeout = 600
    # S6 (audit S1-29): a non-streaming JSON body is read with a hard
    # cap — read() is asked for at most MAX_JSON_BYTES + 1, so an
    # oversize (or endless) body is refused after the cap, never
    # buffered whole into RAM on an 8 GB machine.
    MAX_JSON_BYTES = 5 * 1024 * 1024
    # Cloudflare-fronted APIs (Groq) ban the default Python-urllib
    # signature outright (HTTP 403, error 1010) — verified live
    # 2026-11-09. Every provider request carries an explicit UA.
    USER_AGENT = "CodeForge/2.5"

    def __init__(self, api_key="", urlopen=None, timeout=None):
        self.api_key = str(api_key or "").strip()
        self._urlopen = urlopen or urllib.request.urlopen
        if timeout is not None:
            self.timeout = timeout
        # Per-model output caps learned from provider refusals
        # (see _max_tokens_cap_from_error): bare model id -> cap.
        # Session-scoped; a restart re-learns in one refused call.
        self._max_tokens_caps: dict[str, int] = {}

    def _scrub(self, text) -> str:
        text = str(text or "")
        if self.api_key:
            text = text.replace(self.api_key, "[redacted]")
        return text

    def _require_key(self):
        if not self.api_key:
            raise OllamaError(f"{self.label} API key is not set")

    def _headers(self, content_type=False):
        headers = {"User-Agent": self.USER_AGENT}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _open(self, request):
        try:
            response = self._urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            self._raise_http(exc)
        except OllamaError:
            raise
        except Exception as exc:
            raise OllamaError(
                f"{self.label} request failed: {self._scrub(exc)}") from exc
        status = getattr(response, "status", None)
        if status is None:
            status = getattr(response, "code", None)
        if isinstance(status, int) and status >= 400:
            try:
                detail = response.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise OllamaError(
                f"{self.label} returned HTTP {status}: {self._scrub(detail)}")
        return response

    def _raise_http(self, exc):
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        status = getattr(exc, "code", None) or getattr(exc, "status", "?")
        raise OllamaError(
            f"{self.label} returned HTTP {status}: {self._scrub(detail)}"
        ) from exc

    def _json(self, request):
        response = self._open(request)
        try:
            with response as resp:
                body = resp.read(self.MAX_JSON_BYTES + 1)
        except OllamaError:
            raise
        except Exception as exc:
            raise OllamaError(
                f"{self.label} request failed: {self._scrub(exc)}") from exc
        if body is not None and len(body) > self.MAX_JSON_BYTES:
            raise OllamaError(
                f"{self.label} response too large "
                f"(over {self.MAX_JSON_BYTES // (1024 * 1024)} MB) — refused")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        try:
            return json.loads(body or "{}")
        except Exception as exc:
            raise OllamaError(
                f"{self.label} returned invalid JSON: {self._scrub(exc)}"
            ) from exc

    def _sse(self, request, cancel_event=None):
        response = self._open(request)
        # Sweep R4: the same cancel bridge as the local client — a
        # watch force-closes the live response when the event fires,
        # so a hosted stream that stalls mid-generation stops at
        # once instead of holding the turn until the read timeout.
        watch = CancelWatch(cancel_event) if cancel_event is not None else None
        try:
            if watch is not None:
                watch.register(response)
            with response as resp:
                for raw in resp:
                    if isinstance(raw, bytes):
                        line = raw.decode("utf-8", "replace").strip()
                    else:
                        line = str(raw).strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        yield "[DONE]"
                        continue
                    try:
                        yield json.loads(data)
                    except Exception:
                        continue
        except OllamaError:
            raise
        except urllib.error.HTTPError as exc:
            self._raise_http(exc)
        except Exception as exc:
            # Sweep R4: a read broken by the cancel watch is a clean
            # end-of-stream, not a failure.
            if cancel_event is not None and cancel_event.is_set():
                return
            raise OllamaError(
                f"{self.label} request failed: {self._scrub(exc)}") from exc
        finally:
            if watch is not None:
                watch.release()


def _tool_call_key(call, fallback):
    fn = (call or {}).get("function") or {}
    return str(fn.get("name") or fallback or "")


def _parse_tool_arguments(value):
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {"_raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _openai_content(message, attach_images=False, images=None):
    content = str(message.get("content") or "")
    image_values = []
    if attach_images and images:
        image_values.extend(str(v) for v in images if v)
    if message.get("images"):
        image_values.extend(str(v) for v in message.get("images") or [] if v)
    if not image_values:
        return content
    parts = []
    if content:
        parts.append({"type": "text", "text": content})
    for value in image_values:
        url = value if value.startswith("data:") else \
            f"data:image/jpeg;base64,{value}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _to_groq_messages(messages, images=None):
    messages = ensure_user_message(list(messages or []))
    image_target = None
    if images:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                image_target = idx
                break
    out = []
    pending: list[tuple[str, str]] = []
    for mi, message in enumerate(messages):
        role = str(message.get("role") or "user")
        if role == "system":
            out.append({"role": "system",
                        "content": str(message.get("content") or "")})
            continue
        if role == "assistant":
            converted = {"role": "assistant",
                         "content": str(message.get("content") or "")}
            calls = []
            pending = []
            for ci, call in enumerate(message.get("tool_calls") or []):
                fn = (call or {}).get("function") or {}
                name = str(fn.get("name") or "")
                if not name:
                    continue
                call_id = str(call.get("id") or f"call_{mi}_{ci}")
                args = fn.get("arguments") or {}
                if not isinstance(args, str):
                    args = json.dumps(args)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": args}})
                pending.append((name, call_id))
            if calls:
                converted["tool_calls"] = calls
            out.append(converted)
            continue
        if role == "tool":
            name = str(message.get("tool_name") or "")
            call_id = None
            for idx, (pending_name, pending_id) in enumerate(pending):
                if pending_name == name:
                    call_id = pending_id
                    del pending[idx]
                    break
            if call_id is None:
                call_id = f"call_tool_{mi}"
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": str(message.get("content") or "")})
            continue
        out.append({"role": "user",
                    "content": _openai_content(
                        message, attach_images=(mi == image_target),
                        images=images)})
    return out


def _groq_tools(tools):
    out = []
    for tool in tools or []:
        fn = (tool or {}).get("function") or tool or {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        out.append({"type": "function", "function": {
            "name": name,
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or
            {"type": "object", "properties": {}}}})
    return out


class GroqClient(_ProviderHTTP):
    """Groq's OpenAI-compatible chat API, translated to Ollama events."""

    provider = "groq"
    label = "Groq"
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key="", urlopen=None, timeout=None,
                 base_url=None):
        super().__init__(api_key, urlopen=urlopen, timeout=timeout)
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")

    def _headers(self, content_type=False):
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "User-Agent": self.USER_AGENT}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def models(self):
        self._require_key()
        req = urllib.request.Request(
            self.base_url + "/models", headers=self._headers())
        data = self._json(req)
        out = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                bare = str(item["id"])
                if not is_approved_provider_model(
                        f"{self.provider}:{bare}"):
                    continue
                entry = {"name": f"{self.provider}:{bare}",
                         "model": bare, "provider": self.provider}
                # The catalog states each model's context window —
                # keep it (dynamic context windows, 2026-11-10);
                # budgeting resolves from stated values only.
                window = _clean_context_window(item.get("context_window"))
                if window is not None:
                    entry["context_window"] = window
                out.append(entry)
        return out

    # The facade's uniform catalog name.
    def tags(self):
        return self.models()

    def _payload_extras(self):
        """Provider-specific fields merged into every chat payload.
        Empty for Groq itself; a subclass whose API takes extra
        documented fields (EXAONE's reasoning_effort) overrides
        this — nothing undocumented is ever added here."""
        return {}

    def chat_stream(self, model, messages, options, tools,
                    images=None, keep_alive="30m", cancel_event=None):
        self._require_key()
        _provider, bare = split_model_id(model)
        payload = {"model": bare,
                   "messages": _to_groq_messages(messages, images=images),
                   "stream": True}
        payload.update(self._payload_extras())
        options = options or {}
        if options.get("temperature") is not None:
            payload["temperature"] = options["temperature"]
        if options.get("num_predict") is not None:
            payload["max_tokens"] = options["num_predict"]
        converted_tools = _groq_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = "auto"
        # A cap learned from an earlier refusal this session is
        # honored up front — the model is never again asked for
        # more output than it has already said it can give.
        learned = self._max_tokens_caps.get(bare)
        if (learned is not None and payload.get("max_tokens") is not None
                and payload["max_tokens"] > learned):
            payload["max_tokens"] = learned

        def make_request():
            return urllib.request.Request(
                self.base_url + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=self._headers(content_type=True), method="POST")

        def consume(req):
            pending: dict[int, dict] = {}
            emitted_calls = False

            def emit_calls(done=True, usage=None):
                nonlocal emitted_calls
                calls = []
                for idx in sorted(pending):
                    item = pending[idx]
                    if not item.get("name"):
                        continue
                    calls.append({"function": {
                        "name": item["name"],
                        "arguments": _parse_tool_arguments(
                            item.get("arguments"))}})
                emitted_calls = True
                event = {"message": {"content": "", "tool_calls": calls},
                         "done": bool(done)}
                if isinstance(usage, dict):
                    if usage.get("prompt_tokens") is not None:
                        event["prompt_eval_count"] = usage["prompt_tokens"]
                    if usage.get("completion_tokens") is not None:
                        event["eval_count"] = usage["completion_tokens"]
                return event

            for data in self._sse(req, cancel_event=cancel_event):
                if data == "[DONE]":
                    if pending and not emitted_calls:
                        yield emit_calls(done=True)
                    break
                if not isinstance(data, dict):
                    continue
                usage = data.get("usage")
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                if content:
                    yield {"message": {"content": str(content),
                                       "tool_calls": []}, "done": False}
                for raw_call in delta.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    idx = raw_call.get("index")
                    if not isinstance(idx, int):
                        idx = len(pending)
                    item = pending.setdefault(
                        idx, {"name": "", "arguments": ""})
                    if raw_call.get("id"):
                        item["id"] = raw_call["id"]
                    fn = raw_call.get("function") or {}
                    if fn.get("name"):
                        item["name"] = str(fn["name"])
                    args = fn.get("arguments")
                    if isinstance(args, dict):
                        item["arguments"] = args
                    elif args:
                        item["arguments"] = str(item.get("arguments") or "") + str(args)
                if choice.get("finish_reason"):
                    if pending:
                        yield emit_calls(done=True, usage=usage)
                    else:
                        yield {"message": {"content": "", "tool_calls": []},
                               "done": True}
            if pending and not emitted_calls:
                yield emit_calls(done=True)

        # One retry, only while nothing has been emitted: when the
        # provider refuses the request by naming this model's
        # max_tokens cap (small Groq models cap at 512 while the
        # owner's output setting defaults far higher), learn the
        # cap, clamp, and resend. The refusal happens at open,
        # before a single event, so resending cannot duplicate
        # output. Any other error — or a second refusal — surfaces.
        req = make_request()
        attempts = 0
        while True:
            attempts += 1
            yielded_any = False
            try:
                for event in consume(req):
                    yielded_any = True
                    yield event
                break
            except OllamaError as exc:
                cap = _max_tokens_cap_from_error(str(exc))
                if (attempts == 1 and not yielded_any and cap is not None
                        and payload.get("max_tokens") is not None
                        and payload["max_tokens"] > cap):
                    self._max_tokens_caps[bare] = cap
                    payload["max_tokens"] = cap
                    req = make_request()
                    continue
                raise


class ExaoneClient(GroqClient):
    """LG EXAONE's OpenAI-compatible chat API (owner addition,
    2026-11-10), translated to Ollama events by the Groq client it
    subclasses — same wire shape, same Bearer auth, same SSE
    streaming, same allowlist-filtered /models catalog (a catalog
    failure degrades in the facade exactly as Groq's does).

    Two documented EXAONE facts shape this class.  The model is
    text-only, so it never claims a vision seat; the shared
    converter only attaches images when a caller hands them over,
    and the upstream capability guard keeps images away from
    models that cannot see them.  And reasoning is always on and
    cannot be disabled, with reasoning tokens counting toward
    max_tokens — a small output budget could be eaten whole by
    reasoning before any answer text arrives.  The docs'
    reasoning_effort field (low / high / max, default max) is
    therefore pinned to "low", the minimal documented setting,
    leaving the output budget for the answer itself."""

    provider = "exaone"
    label = "EXAONE"
    BASE_URL = "https://api.exaone.ai/v1"

    def _payload_extras(self):
        return {"reasoning_effort": "low"}


def _gemini_image_part(value):
    text = str(value or "")
    if text.startswith("data:") and "," in text:
        header, text = text.split(",", 1)
        mime = header[5:].split(";")[0] or "image/jpeg"
        return {"inlineData": {"mimeType": mime, "data": text}}
    if text.startswith("/9j/"):
        mime = "image/jpeg"
    elif text.startswith("iVBOR"):
        mime = "image/png"
    elif text.startswith("R0lGOD"):
        mime = "image/gif"
    elif text.startswith("UklGR"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return {"inlineData": {"mimeType": mime, "data": text}}


def _to_gemini_payload(messages, tools, options, images=None):
    messages = ensure_user_message(list(messages or []))
    image_target = None
    if images:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                image_target = idx
                break
    system_texts = []
    contents = []
    for mi, message in enumerate(messages):
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                system_texts.append(content)
            continue
        if role == "assistant":
            parts = []
            if content:
                parts.append({"text": content})
            for call in message.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                name = str(fn.get("name") or "")
                if name:
                    parts.append({"functionCall": {
                        "name": name,
                        "args": _parse_tool_arguments(fn.get("arguments"))}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            continue
        if role == "tool":
            name = str(message.get("tool_name") or "")
            parsed = None
            if content:
                try:
                    parsed = json.loads(content)
                except Exception:
                    parsed = None
            response = parsed if isinstance(parsed, dict) else \
                {"result": content}
            part = {"functionResponse": {"name": name, "response": response}}
            if contents and contents[-1].get("_forge_tool_batch"):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part],
                                 "_forge_tool_batch": True})
            continue
        parts = []
        if content:
            parts.append({"text": content})
        image_values = []
        if mi == image_target and images:
            image_values.extend(str(v) for v in images if v)
        if message.get("images"):
            image_values.extend(str(v) for v in message.get("images") or [] if v)
        for value in image_values:
            parts.append(_gemini_image_part(value))
        contents.append({"role": "user", "parts": parts or [{"text": ""}]})
    for content in contents:
        content.pop("_forge_tool_batch", None)
    payload = {"contents": contents}
    if system_texts:
        payload["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_texts)}]}
    declarations = []
    for tool in tools or []:
        fn = (tool or {}).get("function") or tool or {}
        name = str(fn.get("name") or "")
        if name:
            declarations.append({
                "name": name,
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or
                {"type": "object", "properties": {}}})
    if declarations:
        payload["tools"] = [{"functionDeclarations": declarations}]
    generation = {}
    options = options or {}
    if options.get("temperature") is not None:
        generation["temperature"] = options["temperature"]
    if options.get("num_predict") is not None:
        generation["maxOutputTokens"] = options["num_predict"]
    if generation:
        payload["generationConfig"] = generation
    return payload


class GeminiClient(_ProviderHTTP):
    """Gemini generateContent streaming, translated to Ollama events."""

    provider = "gemini"
    label = "Gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key="", urlopen=None, timeout=None,
                 base_url=None):
        super().__init__(api_key, urlopen=urlopen, timeout=timeout)
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")

    def _headers(self, content_type=False):
        headers = {"x-goog-api-key": self.api_key,
                   "User-Agent": self.USER_AGENT}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def models(self):
        self._require_key()
        req = urllib.request.Request(
            self.base_url + "/models", headers=self._headers())
        data = self._json(req)
        out = []
        for item in data.get("models") or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            name = str(item.get("name") or "")
            bare = name[7:] if name.startswith("models/") else name
            if bare and is_approved_provider_model(f"gemini:{bare}"):
                entry = {"name": f"gemini:{bare}", "model": bare,
                         "provider": "gemini"}
                # The catalog states each model's input token limit
                # — that IS its context window; keep it (dynamic
                # context windows, 2026-11-10).
                window = _clean_context_window(item.get("inputTokenLimit"))
                if window is not None:
                    entry["context_window"] = window
                out.append(entry)
        return out

    def tags(self):
        return self.models()

    def chat_stream(self, model, messages, options, tools,
                    images=None, keep_alive="30m", cancel_event=None):
        self._require_key()
        _provider, bare = split_model_id(model)
        payload = _to_gemini_payload(messages, tools, options, images=images)
        path = ("/models/" + urllib.parse.quote(bare, safe="")
                + ":streamGenerateContent?alt=sse")
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(content_type=True), method="POST")
        for data in self._sse(req, cancel_event=cancel_event):
            if data == "[DONE]" or not isinstance(data, dict):
                continue
            feedback = data.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                raise OllamaError(
                    f"Gemini blocked the prompt: {feedback['blockReason']}")
            candidates = data.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0] or {}
            parts = ((candidate.get("content") or {}).get("parts") or [])
            text_parts = []
            calls = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("text"):
                    text_parts.append(str(part["text"]))
                call = part.get("functionCall")
                if isinstance(call, dict) and call.get("name"):
                    calls.append({"function": {
                        "name": str(call["name"]),
                        "arguments": _parse_tool_arguments(call.get("args"))}})
            done = bool(candidate.get("finishReason"))
            if text_parts or calls or done:
                yield {"message": {"content": "".join(text_parts),
                                   "tool_calls": calls}, "done": done}


def create_provider_client(provider, config, ollama_url=DEFAULT_OLLAMA_URL,
                           urlopen=None):
    """The provider factory: one client for one provider name."""
    provider = str(provider or "ollama").strip().lower()
    if provider == "groq":
        return GroqClient(provider_api_key(config, "groq"), urlopen=urlopen)
    if provider == "gemini":
        return GeminiClient(provider_api_key(config, "gemini"),
                            urlopen=urlopen)
    if provider == "exaone":
        return ExaoneClient(provider_api_key(config, "exaone"),
                            urlopen=urlopen)
    return OllamaClient(ollama_url)


class ProviderClient:
    """Facade preserving the OllamaClient surface Forge already uses.

    Local Ollama remains the default and the fallback.  Cloud catalogs
    are merged only when their provider is enabled and keyed; a failed
    local fetch does not hide cloud models, and a failed cloud fetch
    does not break the local list.
    """

    def __init__(self, ollama_url=DEFAULT_OLLAMA_URL, config=None,
                 urlopen=None):
        self.config = config if isinstance(config, dict) else {}
        self._urlopen = urlopen
        self.local = OllamaClient(ollama_url or DEFAULT_OLLAMA_URL)
        # Some existing surfaces read a timeout attribute (bench's
        # per-task wall); keep the local client's value visible.
        self.timeout = getattr(self.local, "timeout", 1800)
        # Dynamic context windows (2026-11-10): stated provider
        # windows learned from catalog fetches this session, keyed
        # by full model id ("groq:..."). tags() already fetches the
        # catalogs; the windows ride along so budgeting can resolve
        # per model without a per-turn network call.
        self._catalog_windows: dict[str, int] = {}

    @property
    def base_url(self):
        return self.local.base_url

    @base_url.setter
    def base_url(self, value):
        self.local.base_url = str(value).rstrip("/")

    def _provider_client(self, provider):
        return create_provider_client(
            provider, self.config, self.local.base_url,
            urlopen=self._urlopen)

    def client_for_model(self, model):
        provider, _bare = split_model_id(model)
        if provider in PROVIDERS:
            return self._provider_client(provider)
        return self.local

    def ps(self):
        return self.local.ps()

    def show(self, model):
        provider, _bare = split_model_id(model)
        if provider in PROVIDERS:
            return {"model": model, "provider": provider}
        return self.local.show(model)

    def catalog_context_window(self, model):
        """The stated context window for a provider model learned
        from a catalog fetch this session, or None."""
        return self._catalog_windows.get(str(model or "").strip())

    def context_length(self, model):
        """One seam for "what window does this model state?":
        provider models answer from the catalog cache, locals from
        Ollama's /api/show (the local client's own cache). None
        when nothing is stated — callers fall back, never guess."""
        provider, _bare = split_model_id(model)
        if provider in PROVIDERS:
            return self.catalog_context_window(model)
        return self.local.context_length(model)

    def _cloud_entries(self, provider, scope):
        entries: list[dict] = []
        seen: set[str] = set()

        def add(model_id, window=None):
            model_id = str(model_id or "").strip()
            if not model_id or model_id in seen:
                return
            if not model_allowed(self.config, model_id, scope=scope,
                                 role="driver"):
                return
            seen.add(model_id)
            _provider, bare = split_model_id(model_id)
            entry = {"name": model_id, "model": bare,
                     "provider": provider}
            # A stated window rides the entry (Models window shows
            # it) and is remembered for per-model budgeting.
            window = _clean_context_window(window)
            if window is not None:
                self._catalog_windows[model_id] = window
                entry["context_window"] = window
            entries.append(entry)

        catalog_error = None
        catalog_ok = False
        if provider_enabled(self.config, provider) and \
                provider_api_key(self.config, provider):
            try:
                for item in self._provider_client(provider).models():
                    add(item.get("name"), item.get("context_window"))
                catalog_ok = True
            except Exception as exc:
                catalog_error = exc
        for entry in registry_entries(self.config):
            entry_provider, _bare = split_model_id(entry.get("id"))
            if entry_provider == provider:
                add(entry.get("id"), entry.get("context_window"))
        if scope == "maintenance":
            configured = self.config.get(f"maintenance_{provider}_model")
            if configured:
                add(provider_model_id(provider, configured))
        return entries, catalog_ok, catalog_error

    def tags(self, scope="project"):
        scope = str(scope or "project").strip().lower()
        if scope not in ("project", "maintenance"):
            scope = "project"
        merged: list[dict] = []
        seen: set[str] = set()
        local_ok = False
        local_error = None
        try:
            for item in self.local.tags() or []:
                local_ok = True
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if name and name not in seen:
                    seen.add(name)
                    merged.append(item)
        except Exception as exc:
            local_error = exc
        cloud_error = None
        cloud_any = False
        for provider in PROVIDERS:
            entries, catalog_ok, error = self._cloud_entries(provider, scope)
            cloud_any = cloud_any or catalog_ok or bool(entries)
            if error is not None:
                cloud_error = error
            for item in entries:
                if item["name"] not in seen:
                    seen.add(item["name"])
                    merged.append(item)
        if merged or local_ok or cloud_any:
            return merged
        if local_error is not None:
            raise local_error
        if cloud_error is not None:
            raise cloud_error
        return []

    def maintenance_models(self):
        out: list[str] = []
        seen: set[str] = set()
        for provider in PROVIDERS:
            entries, _ok, _error = self._cloud_entries(
                provider, "maintenance")
            for item in entries:
                if item["name"] not in seen:
                    seen.add(item["name"])
                    out.append(item["name"])
        return out

    def test_provider(self, provider):
        provider = str(provider or "").strip().lower()
        if provider not in PROVIDERS:
            return False, f"Unknown provider: {provider}", 0
        label = PROVIDER_LABELS[provider]
        if not provider_enabled(self.config, provider):
            return False, f"{label} is disabled in Customize → Providers.", 0
        if not provider_api_key(self.config, provider):
            return False, f"{label} API key is not set.", 0
        try:
            models = self._provider_client(provider).models()
        except OllamaError as exc:
            return False, self._scrub_provider_error(provider, str(exc)), 0
        except Exception as exc:
            return False, self._scrub_provider_error(provider, str(exc)), 0
        return True, f"{label} connected — {len(models)} model(s) available.", \
            len(models)

    def _scrub_provider_error(self, provider, text):
        key = provider_api_key(self.config, provider)
        return str(text).replace(key, "[redacted]") if key else str(text)

    def chat_stream(self, model, messages, options, tools,
                    images=None, keep_alive="30m", cancel_event=None):
        provider, _bare = split_model_id(model)
        if provider in PROVIDERS:
            if not is_approved_provider_model(model):
                raise OllamaError(
                    f"{model} is not on Code Forge's approved "
                    "provider list.")
            if not provider_enabled(self.config, provider):
                raise OllamaError(
                    f"{PROVIDER_LABELS[provider]} is disabled in "
                    "Customize → Providers.")
            if not provider_api_key(self.config, provider):
                raise OllamaError(
                    f"{PROVIDER_LABELS[provider]} API key is not set")
            client = self._provider_client(provider)
            yield from client.chat_stream(
                model, messages, options, tools, images=images,
                keep_alive=keep_alive, cancel_event=cancel_event)
            return
        yield from self.local.chat_stream(
            model, messages, options, tools, images=images,
            keep_alive=keep_alive, cancel_event=cancel_event)
