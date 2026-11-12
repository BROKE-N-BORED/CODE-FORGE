"""Regression tests for the Groq/Gemini free-tier rate-limit fix
(owner-PC find, 2026-11-11): the owner's keys work in his other app
but every Forge provider call failed instantly with "rate limit
reached" — before he had completed a single turn. Cause: since the
dynamic-context merge, provider turns budgeted against the catalog
window (131,072 for the Groq models) while every allowlisted Groq
model caps at 8,000 tokens per minute on the free tier, prompt and
requested output counted together. One agent-scale request — or
the second call of a tool loop resending the transcript — went
over the minute envelope and Groq refused it whole with HTTP 429.

The fix: resolve_context_budget caps an approved Groq model's
budget to the verified envelope (8000 - configured output - 500
margin, floored at 2048), and a provider 429 surfaces as one
plain-language line with the raw provider text riding on
OllamaError.raw for the session log. Gemini and EXAONE budgets are
untouched (no verified per-minute figure exists for either), and
no retry/backoff was added — the sizing is the fix.

EXAONE honesty fix (2026-11-11): the plain line claimed a shrink
("Code Forge has shrunk the size of its requests") and a
per-minute window for every provider — true only for Groq, whose
requests really are pre-sized to the recorded envelope. The line
now claims a shrink only where a cap exists, and names a window
only when the provider's own words or a recorded envelope says
which one. No EXAONE envelope was added: LG publishes no numeric
limit and the owner's raw 429 text has not arrived, so no number
is invented.

Offline: scripted urlopen transports, like test_providers_clamp.py.
"""

import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.ollama_client import OllamaError
from forge.providers import (ExaoneClient, GeminiClient, GroqClient,
                             ProviderClient, _rate_limit_message,
                             resolve_context_budget)

GROQ_429_MINUTE_BODY = json.dumps({"error": {
    "message": "Rate limit reached for model `openai/gpt-oss-120b` "
               "in organization `org_1` on tokens per minute (TPM): "
               "Limit 8000, Used 7950, Requested 2048. "
               "Please try again in 1.5s",
    "type": "tokens", "code": "rate_limit_exceeded"}}).encode()

GROQ_429_DAY_BODY = json.dumps({"error": {
    "message": "Rate limit reached for model `openai/gpt-oss-20b` "
               "in organization `org_1` on tokens per day (TPD): "
               "Limit 200000, Used 200000, Requested 1024. "
               "Please try again in 3600s",
    "type": "tokens", "code": "rate_limit_exceeded"}}).encode()

GEMINI_429_BODY = json.dumps({"error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your "
               "plan and billing details. Quota exceeded for quota "
               "metric 'Generate requests' and limit 'Generate "
               "requests per minute' of service "
               "'generativelanguage.googleapis.com'.",
    "status": "RESOURCE_EXHAUSTED"}}).encode()

# EXAONE 429 fixtures: LG's real 429 body text has NOT arrived
# (the owner's raw text was requested, 2026-11-11) — these are
# generic OpenAI-style bodies, not LG's words. They pin Forge's
# behavior for a 429 that names no window, names a per-day window,
# and names a per-minute window, respectively.
EXAONE_429_BODY = json.dumps({"error": {
    "message": "Rate limit exceeded. Please try again later.",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"}}).encode()

EXAONE_429_DAY_BODY = json.dumps({"error": {
    "message": "Rate limit exceeded: daily request quota reached. "
               "Please try again tomorrow.",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"}}).encode()

EXAONE_429_MINUTE_BODY = json.dumps({"error": {
    "message": "Rate limit exceeded: too many requests per minute. "
               "Please try again in a few seconds.",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"}}).encode()


def _http_error(body, code, url):
    return urllib.error.HTTPError(url, code, "error", {},
                                  io.BytesIO(body))


class _AlwaysRefuse:
    """Every call raises the scripted error; requests are counted
    so a retry storm would show up as a count above one."""

    def __init__(self, error):
        self.requests = []
        self._error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        raise self._error


class _Catalog:
    def __init__(self, window):
        self._window = window

    def catalog_context_window(self, model):
        return self._window


class _Local:
    def __init__(self, value):
        self._value = value

    def context_length(self, model):
        return self._value


MESSAGES = [{"role": "user", "content": "say hi"}]


class GroqTpmBudgetTests(unittest.TestCase):

    def test_catalog_window_capped_to_envelope(self):
        # 8000 TPM - 2048 default output - 500 margin = 5452.
        self.assertEqual(
            resolve_context_budget(
                "groq:openai/gpt-oss-120b", config={},
                client=_Catalog(131072), fallback=8192), 5452)
        self.assertEqual(
            resolve_context_budget(
                "groq:qwen/qwen3.8-27b", config={},
                client=_Catalog(131072), fallback=8192), 5452)

    def test_registry_window_capped_to_envelope(self):
        config = {"model_registry": [
            {"id": "groq:openai/gpt-oss-20b", "context_window": 131072}]}
        self.assertEqual(
            resolve_context_budget("groq:openai/gpt-oss-20b",
                                   config=config, fallback=8192), 5452)

    def test_unknown_window_fallback_capped_too(self):
        # No stated window anywhere: the fallback setting is the
        # resolved budget, and the envelope caps it just the same —
        # 8192 of prompt + 2048 of output is over the minute cap.
        self.assertEqual(
            resolve_context_budget("groq:openai/gpt-oss-120b",
                                   config={}, fallback=8192), 5452)

    def test_larger_configured_output_shrinks_budget_further(self):
        config = {"num_predict": 4096, "model_registry": [
            {"id": "groq:openai/gpt-oss-120b", "context_window": 131072}]}
        self.assertEqual(
            resolve_context_budget("groq:openai/gpt-oss-120b",
                                   config=config, fallback=8192), 3404)

    def test_floor_honored_when_output_eats_the_envelope(self):
        config = {"num_predict": 16384, "model_registry": [
            {"id": "groq:openai/gpt-oss-120b", "context_window": 131072}]}
        self.assertEqual(
            resolve_context_budget("groq:openai/gpt-oss-120b",
                                   config=config, fallback=8192), 2048)

    def test_stated_window_still_capped_by_output_headroom(self):
        # The stated window is the PROMPT budget; prompt + requested
        # output must fit the minute envelope together, so even a
        # window that is itself under 8000 resolves to the headroom.
        config = {"model_registry": [
            {"id": "groq:openai/gpt-oss-120b", "context_window": 6000}]}
        self.assertEqual(
            resolve_context_budget("groq:openai/gpt-oss-120b",
                                   config=config, fallback=8192), 5452)


class UntouchedBudgetTests(unittest.TestCase):
    """Gemini, EXAONE, unapproved IDs and locals resolve exactly as
    before the envelope existed — pinned current behavior."""

    def test_gemini_stated_window_uncapped(self):
        config = {"model_registry": [
            {"id": "gemini:gemini-3.8-flash",
             "context_window": 1000000}]}
        self.assertEqual(
            resolve_context_budget("gemini:gemini-3.8-flash",
                                   config=config, fallback=8192),
            1000000)

    def test_gemini_unknown_falls_back_to_setting(self):
        self.assertEqual(
            resolve_context_budget("gemini:gemini-3.8-flash",
                                   config={}, fallback=8192), 8192)

    def test_exaone_stated_window_uncapped(self):
        config = {"model_registry": [
            {"id": "exaone:k-exaone_v2.5", "context_window": 131072}]}
        self.assertEqual(
            resolve_context_budget("exaone:k-exaone_v2.5",
                                   config=config, fallback=8192), 131072)

    def test_unapproved_groq_id_uncapped(self):
        # The envelope belongs to the allowlisted models only; an
        # unapproved ID is refused by the registry gate elsewhere
        # and its resolution is unchanged (the DynamicContextWindow
        # worker tests stand on this premise).
        config = {"model_registry": [
            {"id": "groq:big", "context_window": 131072}]}
        self.assertEqual(
            resolve_context_budget("groq:big", config=config,
                                   fallback=8192), 131072)

    def test_local_min_rule_unchanged(self):
        self.assertEqual(
            resolve_context_budget("local-x", client=_Local(4096),
                                   fallback=8192), 4096)
        self.assertEqual(
            resolve_context_budget("local-x", client=_Local(32768),
                                   fallback=8192), 8192)
        self.assertEqual(
            resolve_context_budget("local-x", client=_Local(None),
                                   fallback=8192), 8192)


class GroqRateLimitSurfacingTests(unittest.TestCase):

    def test_429_surfaces_plain_message_raw_rides_along(self):
        transport = _AlwaysRefuse(_http_error(
            GROQ_429_MINUTE_BODY, 429,
            "https://api.groq.com/openai/v1/chat/completions"))
        client = GroqClient(api_key="gsk_test_key_value",
                            urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            list(client.chat_stream("groq:openai/gpt-oss-120b",
                                    MESSAGES, {"num_predict": 2048},
                                    []))
        exc = caught.exception
        # The caller sees the plain line...
        self.assertIn("Groq rate limit reached", str(exc))
        self.assertIn("per-minute", str(exc))
        self.assertIn("shrunk", str(exc))
        # ...and the raw provider text is intact for the log.
        self.assertIsNotNone(exc.raw)
        self.assertIn("HTTP 429", exc.raw)
        self.assertIn("Rate limit reached for model", exc.raw)
        # No retry storm: exactly one request went out.
        self.assertEqual(len(transport.requests), 1)

    def test_429_per_day_names_the_day_limit(self):
        transport = _AlwaysRefuse(_http_error(
            GROQ_429_DAY_BODY, 429,
            "https://api.groq.com/openai/v1/chat/completions"))
        client = GroqClient(api_key="gsk_test_key_value",
                            urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            list(client.chat_stream("groq:openai/gpt-oss-20b",
                                    MESSAGES, {"num_predict": 2048},
                                    []))
        self.assertIn("per-day", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_other_errors_keep_raw_message_and_no_raw(self):
        body = json.dumps({"error": {"message": "model not found"}})
        transport = _AlwaysRefuse(_http_error(
            body.encode(), 400,
            "https://api.groq.com/openai/v1/chat/completions"))
        client = GroqClient(api_key="gsk_test_key_value",
                            urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            list(client.chat_stream("groq:openai/gpt-oss-120b",
                                    MESSAGES, {"num_predict": 2048},
                                    []))
        self.assertIn("Groq returned HTTP 400", str(caught.exception))
        self.assertIsNone(caught.exception.raw)


class GeminiRateLimitSurfacingTests(unittest.TestCase):

    def test_gemini_429_surfaces_plain_message(self):
        transport = _AlwaysRefuse(_http_error(
            GEMINI_429_BODY, 429,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.8-flash:streamGenerateContent?alt=sse"))
        client = GeminiClient(api_key="test_key_value",
                              urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            list(client.chat_stream("gemini:gemini-3.8-flash",
                                    MESSAGES, {"num_predict": 2048},
                                    []))
        exc = caught.exception
        self.assertIn("Gemini rate limit reached", str(exc))
        # Gemini's own text names "requests per minute", so the
        # window IS named — but no shrink is claimed: Gemini has
        # no recorded envelope and its requests are not pre-sized.
        self.assertIn("per-minute", str(exc))
        self.assertNotIn("shrunk", str(exc))
        self.assertIn("does not know Gemini's rate limit", str(exc))
        self.assertIsNotNone(exc.raw)
        self.assertIn("RESOURCE_EXHAUSTED", exc.raw)
        self.assertEqual(len(transport.requests), 1)


class ExaoneRateLimitSurfacingTests(unittest.TestCase):
    """The owner's live find, 2026-11-11: a first-ever EXAONE turn
    429'd and the dialog claimed a shrink that never happened for
    EXAONE. The line must stay honest — no shrink, no window the
    provider did not name."""

    def _run(self, body):
        transport = _AlwaysRefuse(_http_error(
            body, 429, "https://api.exaone.ai/v1/chat/completions"))
        client = ExaoneClient(api_key="test_key_value",
                              urlopen=transport)
        with self.assertRaises(OllamaError) as caught:
            list(client.chat_stream("exaone:k-exaone_v2.5",
                                    MESSAGES, {"num_predict": 2048},
                                    []))
        self.assertEqual(len(transport.requests), 1)
        return caught.exception

    def test_429_names_no_window_and_no_shrink(self):
        exc = self._run(EXAONE_429_BODY)
        self.assertIn("EXAONE rate limit reached", str(exc))
        self.assertNotIn("per-minute", str(exc))
        self.assertNotIn("per-day", str(exc))
        self.assertNotIn("shrunk", str(exc))
        self.assertIn("does not know EXAONE's rate limit", str(exc))
        self.assertIsNotNone(exc.raw)
        self.assertIn("HTTP 429", exc.raw)
        self.assertIn("Rate limit exceeded", exc.raw)

    def test_429_per_day_names_the_day_limit_only(self):
        exc = self._run(EXAONE_429_DAY_BODY)
        self.assertIn("per-day", str(exc))
        self.assertNotIn("per-minute", str(exc))
        self.assertNotIn("shrunk", str(exc))
        self.assertIn("does not know EXAONE's rate limit", str(exc))

    def test_429_per_minute_names_the_minute_limit_only(self):
        exc = self._run(EXAONE_429_MINUTE_BODY)
        self.assertIn("per-minute", str(exc))
        self.assertNotIn("per-day", str(exc))
        self.assertNotIn("shrunk", str(exc))
        self.assertIn("does not know EXAONE's rate limit", str(exc))


class RateLimitMessageHonestyTests(unittest.TestCase):
    """The message builder itself: the shrink claim and the window
    follow what Forge actually knows, per provider."""

    def test_groq_claims_shrink_and_per_minute(self):
        # Groq has a recorded 8,000 TPM envelope, so both claims
        # hold even when the provider text names no window.
        msg = _rate_limit_message(
            "Groq", "Groq returned HTTP 429: rate limit exceeded",
            provider="groq")
        self.assertIn("per-minute", msg)
        self.assertIn("shrunk", msg)

    def test_exaone_claims_neither_without_a_named_window(self):
        msg = _rate_limit_message(
            "EXAONE", "EXAONE returned HTTP 429: rate limit exceeded",
            provider="exaone")
        self.assertIn("EXAONE rate limit reached", msg)
        self.assertNotIn("per-minute", msg)
        self.assertNotIn("per-day", msg)
        self.assertNotIn("shrunk", msg)
        self.assertIn("does not know EXAONE's rate limit", msg)

    def test_gemini_per_day_text_names_day_not_shrink(self):
        msg = _rate_limit_message(
            "Gemini",
            "Gemini returned HTTP 429: quota exceeded for today, "
            "daily limit reached",
            provider="gemini")
        self.assertIn("per-day", msg)
        self.assertNotIn("shrunk", msg)

    def test_gemini_per_minute_text_names_minute_not_shrink(self):
        msg = _rate_limit_message(
            "Gemini",
            "Gemini returned HTTP 429: quota exceeded, requests "
            "per minute limit reached",
            provider="gemini")
        self.assertIn("per-minute", msg)
        self.assertNotIn("shrunk", msg)

    def test_unknown_provider_claims_nothing(self):
        # No provider given: the defensive default — a window or a
        # shrink is claimed only from a source that says so.
        msg = _rate_limit_message(
            "Provider", "Provider returned HTTP 429: rate limited")
        self.assertNotIn("per-minute", msg)
        self.assertNotIn("per-day", msg)
        self.assertNotIn("shrunk", msg)


class RateLimitMessageTests(unittest.TestCase):

    def test_detection(self):
        self.assertIsNotNone(_rate_limit_message(
            "Groq", "Groq returned HTTP 429: {}"))
        self.assertIsNotNone(_rate_limit_message(
            "Gemini",
            'Gemini returned HTTP 429: {"error": {"status": '
            '"RESOURCE_EXHAUSTED"}}'))
        self.assertIsNone(_rate_limit_message(
            "Groq", "Groq returned HTTP 400: bad request"))
        self.assertIsNone(_rate_limit_message("Groq", ""))
        self.assertIsNone(_rate_limit_message("Groq", None))

    def test_facade_scrub_maps_429_to_plain_line(self):
        facade = ProviderClient("http://127.0.0.1:11434", {})
        plain = facade._scrub_provider_error(
            "groq", "Groq returned HTTP 429: "
                    "{\"error\": {\"message\": \"Rate limit reached\"}}")
        self.assertIn("Groq rate limit reached", plain)
        self.assertIn("shrunk", plain)
        untouched = facade._scrub_provider_error(
            "groq", "Groq returned HTTP 400: bad request")
        self.assertEqual(untouched, "Groq returned HTTP 400: bad request")

    def test_facade_scrub_exaone_429_claims_no_shrink(self):
        # The test-connection surface gets the same honest words
        # as the chat path — the provider rides through.
        facade = ProviderClient("http://127.0.0.1:11434", {})
        plain = facade._scrub_provider_error(
            "exaone", "EXAONE returned HTTP 429: "
                      "{\"error\": {\"message\": \"Rate limit exceeded\"}}")
        self.assertIn("EXAONE rate limit reached", plain)
        self.assertNotIn("shrunk", plain)
        self.assertNotIn("per-minute", plain)
        self.assertIn("does not know EXAONE's rate limit", plain)


if __name__ == "__main__":
    unittest.main()
