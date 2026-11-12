"""Regression tests for the Gemini Decisions API client (2026-11-12)
— the SKRIBE fast lane. The Decisions endpoint answers closed-set
questions instead of generating text: POST /v1beta/decisions with a
shared input and typed questions, back as calibrated probabilities.
Forge uses one question type, `score`, whose probability-weighted
score maps to the file's FZI.

Field names confirmed against Google's own docs
(https://aistudio.google.com/docs/decisions-api, read 2026-11-12):
request {model, input, questions: {key: {type, instructions,
criteria}}}; the score answer is {type: "score", score, legend,
probabilities, confidence} with score the probability-weighted
average of the 0-based level indices and legend/probabilities keyed
by index as a string.

Offline: scripted urlopen transports, like
test_providers_ratelimit.py. No live network call anywhere here.
"""

import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.decisions import (DECISION_MODEL, FZI_QUESTION,
                             FZI_QUESTION_KEY, FZI_RUBRIC_LEVELS,
                             MAX_INPUT_CHARS, DecisionsClient,
                             score_fzi)
from forge.ollama_client import OllamaError
from forge.providers import GeminiClient

LEVELS = ["broken", "weak", "adequate", "good", "excellent"]


def _http_error(body, code, url):
    return urllib.error.HTTPError(url, code, "error", {},
                                  io.BytesIO(body))


class _FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._body


class _ScriptedTransport:
    """Every call returns the scripted body; requests are captured
    so the request shape can be asserted."""

    def __init__(self, body):
        self.requests = []
        self._body = body

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return _FakeResponse(self._body)


class _AlwaysRefuse:
    """Every call raises the scripted error; requests are counted
    so a retry storm would show up as a count above one."""

    def __init__(self, error):
        self.requests = []
        self._error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        raise self._error


def _score_body(score, probabilities, confidence=0.9, key="score",
                levels=None):
    levels = levels if levels is not None else LEVELS
    return json.dumps({
        "answers": {key: {
            "type": "score", "score": score,
            "legend": {str(i): levels[i] for i in range(len(levels))},
            "probabilities": probabilities,
            "confidence": confidence}},
        "usage": {"total_input_tokens": 120, "total_output_tokens": 0,
                  "total_tokens": 120}}).encode()


DECISIONS_429_BODY = json.dumps({"error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your "
               "plan and billing details. Quota exceeded for quota "
               "metric 'Decisions requests' and limit 'Decisions "
               "requests per minute' of service "
               "'generativelanguage.googleapis.com'.",
    "status": "RESOURCE_EXHAUSTED"}}).encode()

SAFETY_400_BODY = json.dumps({"error": {
    "code": 400,
    "message": "Input blocked by safety filter.",
    "status": "INVALID_ARGUMENT",
    "details": [{"reason": "safety"}]}}).encode()


class DecisionsRequestTests(unittest.TestCase):

    def test_request_shape(self):
        transport = _ScriptedTransport(_score_body(
            2.0, {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0, "4": 0.0}))
        client = DecisionsClient(api_key="test-key-123",
                                 urlopen=transport)
        client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertEqual(len(transport.requests), 1)
        req = transport.requests[0]
        self.assertEqual(
            req.full_url,
            "https://generativelanguage.googleapis.com/v1beta/decisions")
        self.assertEqual(req.get_header("X-goog-api-key"), "test-key-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("User-agent"), "CodeForge/2.5")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], DECISION_MODEL)
        self.assertEqual(body["model"], "gemini-decision-1")
        self.assertEqual(body["input"], "x = 1")
        question = body["questions"]["score"]
        self.assertEqual(question["type"], "score")
        self.assertEqual(question["instructions"], "Grade it.")
        self.assertEqual(question["criteria"], LEVELS)

    def test_no_key_errors_before_any_request(self):
        transport = _ScriptedTransport(_score_body(2.0, {"2": 1.0}))
        client = DecisionsClient(api_key="", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("API key is not set", str(caught.exception))
        self.assertEqual(len(transport.requests), 0)

    def test_timeout_default_matches_gemini(self):
        self.assertEqual(DecisionsClient().timeout,
                         GeminiClient().timeout)

    def test_rubric_needs_two_levels(self):
        transport = _ScriptedTransport(_score_body(0.0, {"0": 1.0}))
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(ValueError):
            client.score_text("x = 1", ["only one"], "Grade it.")
        self.assertEqual(len(transport.requests), 0)

    def test_empty_rubric_level_rejected(self):
        transport = _ScriptedTransport(_score_body(0.0, {"0": 1.0}))
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(ValueError):
            client.score_text("x = 1", ["fine", "   "], "Grade it.")
        self.assertEqual(len(transport.requests), 0)


class DecisionsMappingTests(unittest.TestCase):

    def _score(self, score, probabilities):
        transport = _ScriptedTransport(
            _score_body(score, probabilities))
        client = DecisionsClient(api_key="k", urlopen=transport)
        return client.score_text("x = 1", LEVELS, "Grade it.")

    def test_weighted_score_maps_to_fzi(self):
        # The docs' own example shape: 1.12 over 5 levels lands
        # just above level 1 -> FZI 28 (1.12 / 4 * 100).
        self.assertEqual(self._score(1.12, {"1": 0.83}).fzi, 28)
        self.assertEqual(self._score(2.0, {"2": 1.0}).fzi, 50)
        self.assertEqual(self._score(3.6, {"3": 0.4, "4": 0.6}).fzi, 90)

    def test_all_mass_on_bottom_level_is_fzi_zero(self):
        result = self._score(0.0, {"0": 1.0, "1": 0.0, "2": 0.0,
                                   "3": 0.0, "4": 0.0})
        self.assertEqual(result.fzi, 0)
        self.assertEqual(result.score, 0.0)

    def test_all_mass_on_top_level_is_fzi_100(self):
        result = self._score(4.0, {"0": 0.0, "1": 0.0, "2": 0.0,
                                   "3": 0.0, "4": 1.0})
        self.assertEqual(result.fzi, 100)
        self.assertEqual(result.score, 4.0)

    def test_uniform_distribution_is_the_middle_level(self):
        result = self._score(2.0, {"0": 0.2, "1": 0.2, "2": 0.2,
                                   "3": 0.2, "4": 0.2})
        self.assertEqual(result.fzi, 50)

    def test_raw_numbers_ride_along(self):
        result = self._score(2.99, {"0": 0.0, "1": 0.0, "2": 0.01,
                                    "3": 0.99, "4": 0.0})
        self.assertEqual(result.score, 2.99)
        self.assertEqual(result.probabilities["3"], 0.99)
        self.assertEqual(result.legend["3"], "good")
        self.assertEqual(result.confidence, 0.9)
        self.assertEqual(result.input_tokens, 120)

    def test_fewer_levels_rescale(self):
        # Two levels: score runs 0-1, so 0.75 -> FZI 75.
        transport = _ScriptedTransport(_score_body(
            0.75, {"0": 0.25, "1": 0.75}, levels=["bad", "good"]))
        client = DecisionsClient(api_key="k", urlopen=transport)
        result = client.score_text("x = 1", ["bad", "good"], "Grade it.")
        self.assertEqual(result.fzi, 75)


class DecisionsErrorTests(unittest.TestCase):

    def test_429_surfaces_plain_message_raw_rides_along(self):
        transport = _AlwaysRefuse(_http_error(
            DECISIONS_429_BODY, 429,
            "https://generativelanguage.googleapis.com/v1beta/decisions"))
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        exc = caught.exception
        self.assertIn("Gemini Decisions rate limit reached", str(exc))
        self.assertIn("per-minute", str(exc))
        self.assertNotIn("shrunk", str(exc))
        self.assertIsNotNone(exc.raw)
        self.assertIn("RESOURCE_EXHAUSTED", exc.raw)
        self.assertEqual(len(transport.requests), 1)

    def test_400_safety_block_is_an_error(self):
        transport = _AlwaysRefuse(_http_error(
            SAFETY_400_BODY, 400,
            "https://generativelanguage.googleapis.com/v1beta/decisions"))
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_500_is_an_error_with_no_retry(self):
        transport = _AlwaysRefuse(_http_error(
            b"internal error", 500,
            "https://generativelanguage.googleapis.com/v1beta/decisions"))
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_json_is_an_error(self):
        transport = _ScriptedTransport(b"not json{")
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("invalid JSON", str(caught.exception))

    def test_missing_answers_is_an_error(self):
        transport = _ScriptedTransport(b"{}")
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("no answer", str(caught.exception))

    def test_wrong_answer_type_is_an_error(self):
        body = json.dumps({"answers": {"score": {
            "type": "noul", "noul": 0.9, "confidence": 0.9}}}).encode()
        transport = _ScriptedTransport(body)
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("not a score", str(caught.exception))

    def test_non_numeric_score_is_an_error(self):
        body = json.dumps({"answers": {"score": {
            "type": "score", "score": "high",
            "probabilities": {}, "confidence": 0.9}}}).encode()
        transport = _ScriptedTransport(body)
        client = DecisionsClient(api_key="k", urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertIn("not a number", str(caught.exception))

    def test_key_never_leaks_into_error_text(self):
        transport = _AlwaysRefuse(_http_error(
            b"bad key: secret-key-123", 401,
            "https://generativelanguage.googleapis.com/v1beta/decisions"))
        client = DecisionsClient(api_key="secret-key-123",
                                 urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            client.score_text("x = 1", LEVELS, "Grade it.")
        self.assertNotIn("secret-key-123", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))


class ScoreFziTests(unittest.TestCase):

    def test_score_fzi_uses_the_fzi_rubric(self):
        seen = {}

        class _FakeClient:
            def score_text(self, text, levels, question, key):
                seen["text"] = text
                seen["levels"] = levels
                seen["question"] = question
                seen["key"] = key
                return "result"

        self.assertEqual(score_fzi(_FakeClient(), "x = 1"), "result")
        self.assertEqual(seen["text"], "x = 1")
        self.assertEqual(seen["levels"], FZI_RUBRIC_LEVELS)
        self.assertEqual(len(seen["levels"]), 5)
        self.assertEqual(seen["question"], FZI_QUESTION)
        self.assertEqual(seen["key"], FZI_QUESTION_KEY)

    def test_score_fzi_truncates_long_files(self):
        seen = {}

        class _FakeClient:
            def score_text(self, text, levels, question, key):
                seen["text"] = text
                return "result"

        score_fzi(_FakeClient(), "x" * (MAX_INPUT_CHARS + 5000))
        self.assertLess(len(seen["text"]), MAX_INPUT_CHARS + 5000)
        self.assertIn("truncated", seen["text"])
        self.assertIn(str(MAX_INPUT_CHARS), seen["text"])


if __name__ == "__main__":
    unittest.main()
