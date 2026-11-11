"""Regression tests for the llama-server start-timeout fix
(owner-PC find, 2026-11-11): during a Research turn the bench died
with a raw "Agent error" dialog — "Ollama returned HTTP 500:
{"error":"timed out waiting for llama-server to start - "}". Ollama
had tried to start the model's runner process and given up waiting:
a transient cold-start failure on the owner's hard-drive machine,
most likely while the disk was busy loading or swapping models.
Nothing was written or lost; the turn simply died.

The fix, mirroring the RATELIMITFIX pattern: OllamaClient.chat_stream
retries that one error exactly once after a short pause (only when
no chunk was emitted yet — a start timeout precedes every token, and
partial output is never replayed), the client's retry hook files a
plain line in the worker's session log, and when the retry also
fails the surfaced message is one plain-language line with the raw
Ollama body preserved on OllamaError.raw (the worker files it as
ollama_raw_error, as it already does for friendly errors). The
ProviderClient facade forwards the hook to its local client and its
error scrubber maps the same text to the same words. Every other
error keeps its existing single-attempt behavior and wording.

Offline: a scripted urllib.request.urlopen transport, like
test_providers_ratelimit.py.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.ollama_client import (START_RETRY_LOG_LINE,
                                 START_TIMEOUT_MESSAGE, OllamaClient,
                                 OllamaError, friendly_http_error,
                                 llama_start_timeout)
from forge.providers import ProviderClient

START_BODY = json.dumps(
    {"error": "timed out waiting for llama-server to start - "})
OTHER_500_BODY = json.dumps({"error": "the disk is on fire"})
TEMPLATE_500_BODY = json.dumps(
    {"error": "jinja template exception: no user query found"})
MSGS = [{"role": "user", "content": "hi"}]
MODEL = "maternion/minicpm5-heretic:2b"


def _http_error(code, body):
    return urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat", code, "error", None,
        io.BytesIO(body.encode("utf-8")))


def _chunk_line(text):
    return (json.dumps({"message": {"role": "assistant", "content": text},
                        "done": False}) + "\n").encode("utf-8")


def _done_line():
    return (json.dumps({"message": {"role": "assistant", "content": ""},
                        "done": True, "prompt_eval_count": 5,
                        "eval_count": 2}) + "\n").encode("utf-8")


class _StreamResponse:
    """A urlopen result: context manager + NDJSON line iterator,
    optionally raising a scripted error after its lines."""

    def __init__(self, lines, error=None):
        self._lines = lines
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for line in self._lines:
            yield line
        if self._error is not None:
            raise self._error


class _ScriptedTransport:
    """urlopen replacement playing a fixed list of behaviors:
    ("error", code, body) raises at open; ("stream", [chunks])
    streams; ("stream_error", [chunks], code, body) streams then
    raises mid-iteration."""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        # The final behavior repeats if a consumer asks again (a
        # full turn runs more generations than a unit test scripts);
        # the call count is what pins attempt numbers.
        behavior = (self.behaviors.pop(0) if len(self.behaviors) > 1
                    else self.behaviors[0])
        kind = behavior[0]
        if kind == "error":
            raise _http_error(behavior[1], behavior[2])
        if kind == "stream":
            return _StreamResponse(
                [_chunk_line(c) for c in behavior[1]] + [_done_line()])
        if kind == "stream_error":
            return _StreamResponse(
                [_chunk_line(c) for c in behavior[1]],
                error=_http_error(behavior[2], behavior[3]))
        raise AssertionError(f"unknown behavior {kind!r}")


def _client(hook=None):
    client = OllamaClient("http://127.0.0.1:11434")
    client.start_retry_delay = 0
    if hook is not None:
        client.on_start_retry = hook
    return client


def _run(client, transport):
    with mock.patch.object(urllib.request, "urlopen", transport):
        return list(client.chat_stream(MODEL, MSGS, {}, []))


def _content(events):
    return "".join(e.get("message", {}).get("content", "")
                   for e in events)


class StartFixClientTests(unittest.TestCase):

    def test_start_timeout_once_then_success(self):
        lines = []
        client = _client(hook=lines.append)
        transport = _ScriptedTransport([
            ("error", 500, START_BODY),
            ("stream", ["Hello", " world"]),
        ])
        events = _run(client, transport)
        self.assertEqual(_content(events), "Hello world")
        self.assertEqual(transport.calls, 2)
        self.assertEqual(lines, [START_RETRY_LOG_LINE])

    def test_start_timeout_twice_plain_message_raw_preserved(self):
        client = _client()
        transport = _ScriptedTransport([
            ("error", 500, START_BODY),
            ("error", 500, START_BODY),
        ])
        with self.assertRaises(OllamaError) as ctx:
            _run(client, transport)
        self.assertEqual(str(ctx.exception), START_TIMEOUT_MESSAGE)
        self.assertEqual(ctx.exception.raw, f"HTTP 500: {START_BODY}")
        # Exactly two attempts — never a third.
        self.assertEqual(transport.calls, 2)

    def test_other_500_no_retry_existing_words(self):
        client = _client()
        transport = _ScriptedTransport([("error", 500, OTHER_500_BODY)])
        with self.assertRaises(OllamaError) as ctx:
            _run(client, transport)
        self.assertEqual(str(ctx.exception),
                         f"Ollama returned HTTP 500: {OTHER_500_BODY}")
        self.assertIsNone(ctx.exception.raw)
        self.assertEqual(transport.calls, 1)

    def test_template_500_keeps_note26_words_no_retry(self):
        client = _client()
        transport = _ScriptedTransport(
            [("error", 500, TEMPLATE_500_BODY)])
        with self.assertRaises(OllamaError) as ctx:
            _run(client, transport)
        self.assertIn("chat template rejected", str(ctx.exception))
        self.assertEqual(ctx.exception.raw, f"HTTP 500: {TEMPLATE_500_BODY}")
        self.assertEqual(transport.calls, 1)

    def test_chunk_then_start_error_no_retry(self):
        client = _client()
        transport = _ScriptedTransport([
            ("stream_error", ["partial"], 500, START_BODY),
        ])
        seen = []
        with mock.patch.object(urllib.request, "urlopen", transport):
            with self.assertRaises(OllamaError) as ctx:
                for event in client.chat_stream(MODEL, MSGS, {}, []):
                    seen.append(event)
        # The partial chunk reached the consumer before the failure,
        # and seen output is never replayed behind a retry.
        self.assertEqual(_content(seen), "partial")
        self.assertEqual(str(ctx.exception), START_TIMEOUT_MESSAGE)
        self.assertEqual(transport.calls, 1)

    def test_matcher_and_defaults(self):
        self.assertTrue(llama_start_timeout(START_BODY))
        self.assertFalse(llama_start_timeout(OTHER_500_BODY))
        self.assertFalse(llama_start_timeout(""))
        self.assertEqual(friendly_http_error(500, START_BODY),
                         START_TIMEOUT_MESSAGE)
        self.assertIsNone(friendly_http_error(404, START_BODY))
        fresh = OllamaClient("http://127.0.0.1:11434")
        self.assertEqual(fresh.start_retry_delay, 3.0)
        self.assertIsNone(fresh.on_start_retry)


class StartFixFacadeTests(unittest.TestCase):

    def test_facade_forwards_hook_to_local_client(self):
        facade = ProviderClient(config={})
        hook = lambda line: None
        facade.on_start_retry = hook
        self.assertIs(facade.local.on_start_retry, hook)
        self.assertIs(facade.on_start_retry, hook)

    def test_facade_local_generation_retries(self):
        facade = ProviderClient(config={})
        facade.local.start_retry_delay = 0
        transport = _ScriptedTransport([
            ("error", 500, START_BODY),
            ("stream", ["through the facade"]),
        ])
        with mock.patch.object(urllib.request, "urlopen", transport):
            events = list(facade.chat_stream(MODEL, MSGS, {}, []))
        self.assertEqual(_content(events), "through the facade")
        self.assertEqual(transport.calls, 2)

    def test_scrub_maps_start_timeout_and_429_still_maps(self):
        facade = ProviderClient(config={})
        scrubbed = facade._scrub_provider_error(
            "groq", f"Ollama returned HTTP 500: {START_BODY}")
        self.assertEqual(scrubbed, START_TIMEOUT_MESSAGE)
        rate = facade._scrub_provider_error(
            "groq", "Groq returned HTTP 429: rate limit reached, "
                    "tokens per minute (TPM)")
        self.assertTrue(rate.startswith("Groq rate limit reached"))


class _FakeEventLog:
    def __init__(self):
        self.records = []

    def record(self, kind, **fields):
        self.records.append((kind, fields))


class StartFixWorkerTests(unittest.TestCase):

    def _worker(self, client, log):
        from forge.worker import AgentWorker
        return AgentWorker(client, MODEL, [dict(m) for m in MSGS], {},
                           tempfile.mkdtemp(), eventlog=log)

    def test_worker_wires_hook_and_logs_retry_end_to_end(self):
        log = _FakeEventLog()
        client = _client()
        worker = self._worker(client, log)
        # Construction wired this worker's log as the client's hook.
        self.assertEqual(client.on_start_retry, worker._note_start_retry)
        transport = _ScriptedTransport([
            ("error", 500, START_BODY),
            ("stream", ["Hello"]),
        ])
        with mock.patch.object(urllib.request, "urlopen", transport):
            text, _calls = worker._generate([])
        self.assertEqual(text, "Hello")
        self.assertEqual(transport.calls, 2)
        retries = [f for kind, f in log.records
                   if kind == "model_start_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["detail"], START_RETRY_LOG_LINE)
        self.assertEqual(retries[0]["model"], MODEL)

    def test_worker_failure_files_raw_and_plain_message(self):
        log = _FakeEventLog()
        client = _client()
        worker = self._worker(client, log)
        failures = []
        worker.failed.connect(failures.append)
        # A full turn runs several generations (homework, drafting);
        # every one of them meets the same start failure here. The
        # exact two-attempts contract is pinned at client level in
        # StartFixClientTests; this test pins the turn-level surface.
        transport = _ScriptedTransport([("error", 500, START_BODY)] * 8)
        with mock.patch.object(urllib.request, "urlopen", transport):
            worker.run()
        # The owner-facing failure is the plain line; the raw Ollama
        # body is filed in the session log (the RATELIMITFIX pattern).
        self.assertEqual(failures, [START_TIMEOUT_MESSAGE])
        raw_records = [f for kind, f in log.records
                       if kind == "ollama_raw_error"]
        self.assertGreaterEqual(len(raw_records), 1)
        self.assertIn("timed out waiting for llama-server",
                      raw_records[0]["detail"])
        self.assertGreaterEqual(transport.calls, 2)


if __name__ == "__main__":
    unittest.main()
