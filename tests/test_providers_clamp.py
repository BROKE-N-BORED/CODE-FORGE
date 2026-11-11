"""Regression tests for the Groq max_tokens cap clamp (owner-PC find,
2026-11-10): a small Groq model caps max_tokens at 512 while the
owner's output setting defaults to 2048; Groq refused the request
with HTTP 400 and the raw error hit a dialog. The client now learns
the cap from the refusal, clamps, retries once (only before any
output was emitted), and honors the learned cap up front afterwards.

Offline: the transport is a scripted urlopen, like the provider
tests in test_core.py.
"""

import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.ollama_client import OllamaError
from forge.providers import GroqClient, _max_tokens_cap_from_error

GROQ_CAP_BODY = json.dumps({"error": {
    "message": "`max_tokens` must be less than or equal to `512`, "
               "the maximum value for `max_tokens` is less than the "
               "`context_window` for this model",
    "type": "invalid_request_error", "param": "max_tokens"}}).encode()


class _FakeResponse:
    status = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self, *args):
        return b""


def _sse_response(text="hi"):
    chunk = json.dumps(
        {"choices": [{"delta": {"content": text}}]}).encode()
    return _FakeResponse([b"data: " + chunk + b"\n", b"data: [DONE]\n"])


def _http_error(body, code=400):
    return urllib.error.HTTPError(
        "https://api.groq.com/openai/v1/chat/completions",
        code, "error", {}, io.BytesIO(body))


class _ScriptedTransport:
    """First call raises the scripted error; later calls succeed."""

    def __init__(self, first_error=None):
        self.requests = []
        self._first_error = first_error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self._first_error is not None and len(self.requests) == 1:
            raise self._first_error
        return _sse_response()

    def payload_of(self, index):
        return json.loads(self.requests[index].data.decode("utf-8"))


MESSAGES = [{"role": "user", "content": "say hi"}]


class GroqMaxTokensClampTests(unittest.TestCase):

    def test_cap_refusal_retries_clamped_and_streams(self):
        transport = _ScriptedTransport(_http_error(GROQ_CAP_BODY))
        client = GroqClient(api_key="gsk_test_key_value", urlopen=transport)
        events = list(client.chat_stream(
            "groq:tiny-model", MESSAGES, {"num_predict": 2048}, []))
        contents = [e["message"]["content"] for e in events
                    if e["message"].get("content")]
        self.assertEqual(contents, ["hi"])
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(transport.payload_of(0)["max_tokens"], 2048)
        self.assertEqual(transport.payload_of(1)["max_tokens"], 512)

    def test_learned_cap_applies_up_front_next_call(self):
        transport = _ScriptedTransport(_http_error(GROQ_CAP_BODY))
        client = GroqClient(api_key="gsk_test_key_value", urlopen=transport)
        list(client.chat_stream(
            "groq:tiny-model", MESSAGES, {"num_predict": 2048}, []))
        transport2 = _ScriptedTransport()
        client._urlopen = transport2
        list(client.chat_stream(
            "groq:tiny-model", MESSAGES, {"num_predict": 2048}, []))
        self.assertEqual(len(transport2.requests), 1)
        self.assertEqual(transport2.payload_of(0)["max_tokens"], 512)

    def test_other_400_surfaces_without_retry(self):
        body = json.dumps({"error": {
            "message": "model not found"}}).encode()
        transport = _ScriptedTransport(_http_error(body))
        client = GroqClient(api_key="gsk_test_key_value", urlopen=transport)
        with self.assertRaises(OllamaError):
            list(client.chat_stream(
                "groq:tiny-model", MESSAGES, {"num_predict": 2048}, []))
        self.assertEqual(len(transport.requests), 1)

    def test_second_refusal_surfaces(self):
        class AlwaysRefuse:
            def __init__(self):
                self.requests = []

            def __call__(self, request, timeout=None):
                self.requests.append(request)
                raise _http_error(GROQ_CAP_BODY)

        transport = AlwaysRefuse()
        client = GroqClient(api_key="gsk_test_key_value", urlopen=transport)
        with self.assertRaises(OllamaError):
            list(client.chat_stream(
                "groq:tiny-model", MESSAGES, {"num_predict": 2048}, []))
        # Exactly one retry, never a loop.
        self.assertEqual(len(transport.requests), 2)

    def test_cap_parser(self):
        self.assertEqual(
            _max_tokens_cap_from_error(
                "Groq returned HTTP 400: " + GROQ_CAP_BODY.decode()), 512)
        # The key scrubber can eat a character inside "max_tokens"
        # itself (a one-character key turns it into
        # "max_to[redacted]ens"); the cap must still parse.
        self.assertEqual(
            _max_tokens_cap_from_error(
                "Groq returned HTTP 400: `max_to[redacted]ens` must be "
                "less than or equal to `512`, the maximum value"), 512)
        self.assertIsNone(_max_tokens_cap_from_error("some other error"))
        self.assertIsNone(_max_tokens_cap_from_error(""))
        self.assertIsNone(_max_tokens_cap_from_error(None))


if __name__ == "__main__":
    unittest.main()
