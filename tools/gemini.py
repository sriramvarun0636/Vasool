"""The one module allowed to talk to the model provider.

Same rule `vasool/actions/razorpay_client.py` lives under, and enforced the
same way: `tests/test_shadow_boundary.py` scans every source file in the
repository and fails if the provider SDK is imported anywhere else. That test
also walks the import graph in both directions to prove that nothing which can
take an action reaches this file, and that this file reaches nothing which
can.

**This module is only ever reached with `--record`.** Replay — which is the
default for `make shadow` and the only mode `pytest` ever runs — is served
entirely by `windtunnel/cassette.py`, which holds no client and offers no
method that could obtain a response it was not handed. A missing recording
raises `CassetteMiss`; it never falls through to here.

**The import is lazy on purpose.** `google-genai` is a dependency of the
recording path alone, and the boundary scan reads this file as text rather
than importing it, so the whole suite runs on a machine that has never
installed it.

# VERIFY: the free tier's requests-per-minute and requests-per-day are not
# published in Google's own documentation any more — ai.google.dev's
# rate-limits page defers to the AI Studio dashboard. DEFAULT_RPM below is the
# pessimistic end of what third-party sources report. Check
# aistudio.google.com/rate-limit against the project before a record run; the
# failure mode of guessing low is a slower run, and of guessing high is a
# burnt daily quota.
"""
from __future__ import annotations

import time

PROVIDER = "gemini"
"""Recorded into every cassette. The cassette layer is provider-agnostic, so
this string is the only thing that identifies where a response came from."""

DEFAULT_MODEL = "gemini-3.7-flash"
"""The current stable Flash model, and free of charge on the free tier.

Chosen for cost, which is a fact the artifact states about itself rather than
hides: a stronger model might close whatever gap the comparison finds, and
nothing measured here bounds a model that was not run.
"""

DEFAULT_RPM = 10
"""Requests per minute. See the VERIFY note above."""

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 15, 45, 90)
"""What to wait after a rate-limit refusal. Longer than an ordinary API
backoff because the thing being waited out is a per-minute quota, not
congestion — retrying in 200ms simply spends another request against it."""


class GeminiUnavailable(RuntimeError):
    """The provider could not be reached, or refused past the retry budget.

    Deliberately fatal. A record run that quietly skipped a cell would produce
    a table with a hole in it, and the hole would be invisible by the time
    anyone read the numbers.
    """


class GeminiClient:
    """A rate-paced, retrying wrapper around one call.

    Exactly one method, taking a prompt and returning raw text. Everything
    about what to ask and how to read the answer lives in
    `vasool/diagnosis/llm.py`; everything about what to do with the answer
    lives in `windtunnel/shadow.py`. This class knows neither.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        rpm: int = DEFAULT_RPM,
        response_schema: dict | None = None,
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no API key was supplied")
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise GeminiUnavailable(
                "google-genai is not installed — `pip install -U google-genai`. "
                "It is needed only to record; replay never reaches this module."
            ) from exc

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._interval = 60.0 / max(rpm, 1)
        self._schema = response_schema
        self._paced = False

    @property
    def model(self) -> str:
        return self._model

    def _pace(self) -> None:
        """Sleep the inter-request interval, except before the first request.

        A fixed interval rather than a token bucket, and no clock reading at
        all: over-sleeping slightly costs minutes on a twenty-minute run, and
        under-sleeping costs a 429 that costs one of the day's requests.
        """
        if self._paced:
            time.sleep(self._interval)
        self._paced = True

    def complete(self, prompt: str) -> str:
        """One request. Returns the model's raw text, unparsed.

        Unparsed on purpose: what came back is what gets recorded, so a
        cassette holds the provider's actual output rather than this file's
        interpretation of it. Parsing — and rejecting — happens in
        `vasool.diagnosis.llm.parse_verdict`, on replay, where it can be
        re-run against an edited parser without spending another request.
        """
        request: dict = {"model": self._model, "input": prompt}
        if self._schema is not None:
            request["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": self._schema,
            }

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._pace()
            try:
                interaction = self._client.interactions.create(**request)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last = exc
                if not _is_rate_limit(exc) or attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                continue

            text = getattr(interaction, "output_text", None)
            if not text:
                raise GeminiUnavailable(
                    f"{self._model} returned no text — the response carried "
                    f"{sorted(vars(interaction)) if hasattr(interaction, '__dict__') else type(interaction)}"
                )
            return text

        raise GeminiUnavailable(f"{self._model} failed after {MAX_ATTEMPTS} attempts: {last}")


def _is_rate_limit(exc: Exception) -> bool:
    """Whether this refusal is worth waiting out.

    Checked by code and by message rather than by exception class: the SDK's
    error hierarchy is not something this file should hard-code, and a quota
    refusal is unmistakable in either field.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    text = str(exc).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text
