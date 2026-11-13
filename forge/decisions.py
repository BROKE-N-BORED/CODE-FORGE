"""Gemini Decisions API client (2026-11-12) — the SKRIBE fast lane.

Google's Decisions API (announced 2026-11-11) answers closed-set
questions instead of generating text: one shared input plus typed
questions, back as calibrated probabilities in ~150-250 ms, zero
output tokens. Model gemini-decision-1, endpoint POST /v1beta/decisions
on the Gemini Developer API only (Vertex support "coming soon" at
launch). It is NOT a chat model and NOT a row in the provider
allowlist — it is a separate endpoint with its own client, riding
the same Gemini API key Forge already stores.

Forge uses exactly one question type: `score` — one rubric-scored
question per written file, whose probability-weighted score maps to
the file's FZI. The endpoint MEASURES; the shell decides
finalize/revise from the FZI by its own deterministic rule
(worker.py) — no model holds the grader seat here (governing-seat
rule).

Free tier (Google AI Studio, checked 2026-11-12): 200 requests/min,
50,000 tokens/min, 10,000 requests/day; input free. A SKRIBE
grading pass is one request per written file — far inside all
three. Free-tier data may be used by Google to improve its
products; the Customize note says so in plain words.

Raw urllib, like the other provider clients — no google-genai SDK.
Field names confirmed against Google's own docs
(https://aistudio.google.com/docs/decisions-api, read 2026-11-12):
request {model, input, questions: {key: {type, instructions,
criteria}}}; score criteria is an array of level descriptions,
lowest first, indexed from 0; the score answer is {type: "score",
score, legend, probabilities, confidence} where score is the
probability-weighted average of the 0-based level indices and
legend/probabilities are keyed by index as a string.
"""
from __future__ import annotations

import json
import urllib.request

from . import providers as providers_mod
from . import secrets as secrets_mod
from .ollama_client import OllamaError

DECISION_MODEL = "gemini-decision-1"

# The FZI rubric as Decisions levels — five, lowest first. Five
# because Google recommends 2-10 levels and five maps cleanly onto
# FZI: the weighted score runs 0-4, so FZI = score * 25, with the
# weighted average giving continuous values between the steps.
FZI_RUBRIC_LEVELS = [
    "Broken or unusable: the file does not do what it was written "
    "to do, or misses the point entirely.",
    "Weak: the file does what it was written to do only partly; "
    "major gaps, errors, or confusion remain.",
    "Adequate: the file does what it was written to do, with minor "
    "gaps or rough edges.",
    "Good: the file does what it was written to do well; clear and "
    "complete, only small polish missing.",
    "Excellent: the file does what it was written to do thoroughly "
    "and well; nothing material left to fix.",
]

FZI_QUESTION = ("Grade this file the way SKRIBE would: does it do "
                "what it was written to do, completely and "
                "correctly? Score it against the rubric levels.")

FZI_QUESTION_KEY = "skribe_fzi"

# One file's contents per request, capped so a single request stays
# near half the free tier's 50,000-token-per-minute budget (~4 chars
# per token). Files past the cap are scored on their head, with the
# truncation named in the input itself.
MAX_INPUT_CHARS = 100_000


def _short_json(data, limit=2000) -> str:
    """A scrubbed, truncated rendering of a parsed response body for
    the OllamaError.raw slot — enough to diagnose, never a dump."""
    try:
        text = json.dumps(data)
    except Exception:
        text = str(data)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class ScoreResult:
    """One scored question: the FZI the shell decides on, plus the
    raw numbers behind it (probabilities keyed by level index as a
    string, legend mapping those indices back to their level
    descriptions, confidence 0-1, and the request's input tokens
    when the usage block carried them)."""

    def __init__(self, fzi, score, probabilities, legend, confidence,
                 input_tokens=None):
        self.fzi = fzi
        self.score = score
        self.probabilities = probabilities
        self.legend = legend
        self.confidence = confidence
        self.input_tokens = input_tokens


class DecisionsClient(providers_mod._ProviderHTTP):
    """The Decisions endpoint, on the shared provider-HTTP base:
    key scrubbing, the CodeForge user agent, the 5 MB JSON cap, and
    the plain-language 429 shape all ride along. v1beta only."""

    provider = "gemini"
    label = "Gemini Decisions"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key="", urlopen=None, timeout=None,
                 base_url=None):
        super().__init__(api_key, urlopen=urlopen, timeout=timeout)
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        # Pre-send secret scan (2026-11-12): mirrored from the
        # window's settings like the facade's own attributes.
        # Default ON; no workspace until one is set.
        self.presend_scan = True
        self.presend_workspace = None
        # Findings the allow-list exempted on this thread's last
        # scan — the SKRIBE lane drains + logs them
        # (presend_allowed), like the chat facade's stash.
        self._allowed = providers_mod._AllowedStash()

    def take_allowed_findings(self):
        """Findings the allow-list exempted on this thread's last
        scan, drained (the stash empties)."""
        return self._allowed.take()

    def _headers(self, content_type=False):
        headers = {"x-goog-api-key": self.api_key,
                   "User-Agent": self.USER_AGENT}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def score_text(self, text, rubric_levels, question,
                   question_key="score"):
        """One Decisions request: score `text` against
        `rubric_levels` (level descriptions, lowest first). Returns a
        ScoreResult. Raises OllamaError on any API failure (HTTP
        refusal, bad JSON, missing or malformed answer) with the raw
        provider text kept where one exists; ValueError on a bad
        rubric (a programming error, not an API failure). No
        retries — the caller falls back to local grading."""
        self._require_key()
        # Pre-send secret scan (2026-11-12): the Decisions lane
        # sends queued FILE contents to Gemini — a secret in a
        # queued file must never make the trip. A hit raises
        # BEFORE the request is built; the caller's existing
        # fallback grades the batch locally instead. No dialog
        # here: this lane is a background optimization, and the
        # local pass is the safe answer.
        if self.presend_scan:
            scanned = providers_mod.scan_outbound_text(
                text, where="decisions input")
            # Item 5: the project's allow-list covers matches the
            # owner already accepted. Without a workspace there is
            # no list to consult — fail closed. The exempted half
            # is stashed for the caller to log (security review,
            # 2026-11-12).
            findings, allowed = secrets_mod.partition_allowed(
                scanned, self.presend_workspace)
            self._allowed.store(allowed)
            if findings:
                raise providers_mod.SecretBlocked(
                    "gemini", "decisions", findings)
        levels = [str(level).strip() for level in (rubric_levels or [])]
        if len(levels) < 2:
            raise ValueError(
                "a score question needs at least 2 rubric levels")
        if any(not level for level in levels):
            raise ValueError(
                "rubric levels must be non-empty descriptions")
        body = {
            "model": DECISION_MODEL,
            "input": str(text or ""),
            "questions": {
                question_key: {
                    "type": "score",
                    "instructions": str(question),
                    "criteria": levels,
                },
            },
        }
        req = urllib.request.Request(
            self.base_url + "/decisions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(content_type=True), method="POST")
        data = self._json(req)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or question_key not in answers:
            raise OllamaError(
                f"{self.label} returned no answer for the score "
                "question",
                raw=_short_json(data))
        answer = answers[question_key]
        if not isinstance(answer, dict) or answer.get("type") != "score":
            raise OllamaError(
                f"{self.label} returned an answer that is not a "
                "score",
                raw=_short_json(data))
        score = answer.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise OllamaError(
                f"{self.label} returned a score that is not a number",
                raw=_short_json(data))
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict):
            probabilities = {}
        legend = answer.get("legend")
        if not isinstance(legend, dict):
            legend = {}
        confidence = answer.get("confidence")
        if isinstance(confidence, bool) or not isinstance(
                confidence, (int, float)):
            confidence = None
        # The score is the probability-weighted average of the
        # 0-based level indices; normalize by the top index (the
        # docs' own recipe) and scale to FZI 0-100.
        fzi = int(round(score / (len(levels) - 1) * 100))
        fzi = max(0, min(100, fzi))
        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = None
        if isinstance(usage, dict):
            tokens = usage.get("total_input_tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                input_tokens = tokens
        return ScoreResult(fzi, score, probabilities, legend,
                           confidence, input_tokens)


def score_fzi(client, text):
    """One file's FZI through a Decisions client: the file's text
    against the FZI rubric. The scorer interface the SKRIBE lane
    (and the standalone SKRIBE tool behind it) builds on."""
    content = str(text or "")
    if len(content) > MAX_INPUT_CHARS:
        content = (content[:MAX_INPUT_CHARS]
                   + "\n\n[SKRIBE NOTE: file truncated to its first "
                   f"{MAX_INPUT_CHARS} characters for scoring.]")
    return client.score_text(content, FZI_RUBRIC_LEVELS, FZI_QUESTION,
                             FZI_QUESTION_KEY)
