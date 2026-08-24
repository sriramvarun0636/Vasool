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

**Observed, 2026-08-24.** The free tier's daily cap on `gemini-3.6-flash` for
this project is **20 requests**, reported by the API itself as
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`. That is
far below the 250–1,500 third-party sources report, and it is smaller than one
full pass over the twelve-cell corpus at any k. It is the binding constraint on
this comparison; requests-per-minute never comes close to mattering.

Google no longer publishes free-tier limits in its own documentation —
ai.google.dev's rate-limits page defers to the AI Studio dashboard — so the
number above is what the API said when it refused, which is better evidence
than any page.

# VERIFY: 20/day is one project, one model, one day. It is not a documented
# constant and may differ per model or change without notice. The code below
# reads the refusal rather than assuming the number.
"""
from __future__ import annotations

import re
import time

PROVIDER = "gemini"
"""Recorded into every cassette. The cassette layer is provider-agnostic, so
this string is the only thing that identifies where a response came from."""

# There is deliberately no DEFAULT_MODEL here. The model is pinned once, in
# windtunnel/shadow.py::PINNED_MODEL, because it is part of every cassette's
# address and changing it orphans the whole recorded corpus. A default in this
# file would be a second place to change it, which is exactly the drift the
# pin exists to prevent — so GeminiClient requires the caller to say.

DEFAULT_RPM = 10
"""Requests per minute. See the VERIFY note above."""

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 15, 45, 90)
"""What to wait after a *per-minute* refusal. Longer than an ordinary API
backoff because the thing being waited out is a quota window, not congestion —
retrying in 200ms simply spends another request against it.

Used only when the refusal is a per-minute one. A daily refusal is not retried
at all; see `DailyQuotaExhausted`."""

MAX_SERVER_RETRY_DELAY = 120.0
"""Cap on an honoured server-supplied `retryDelay`. Anything longer is a wait
the operator should decide about, not one this process should take silently."""


class DailyQuotaExhausted(RuntimeError):
    """The per-day free-tier allowance is spent.

    Raised immediately and never retried, which is the whole point of having
    its own type. The first version of this file treated every 429 alike and
    spent four further requests against a cap of twenty — a fifth of a day's
    budget, on attempts that could not have succeeded, to learn something the
    first refusal already said. A daily quota does not clear in ninety seconds;
    it clears tomorrow.

    Cassettes recorded before the refusal are already on disk — the store
    writes each one as it arrives — so this is a stopping condition, not a
    lost run. Re-running with `--record` resumes at the first missing cell.
    """


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
        model: str,
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
        from google.genai import types

        config = types.GenerateContentConfig()
        if self._schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = self._schema

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._pace()
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last = exc
                if _is_daily_quota(exc):
                    raise DailyQuotaExhausted(
                        f"{self._model}: the free tier's daily request quota is "
                        "spent. Nothing is retried — a daily cap clears tomorrow, "
                        "not after a backoff. Cassettes already recorded are on "
                        "disk; re-run with --record to resume at the first "
                        "missing cell."
                    ) from exc
                if not _is_rate_limit(exc) or attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep(_wait_for(exc, attempt))
                continue

            text = getattr(response, "text", None)
            if not text:
                raise GeminiUnavailable(
                    f"{self._model} returned no text — the response carried "
                    f"{sorted(vars(response)) if hasattr(response, '__dict__') else type(response)}"
                )
            return text

        raise GeminiUnavailable(f"{self._model} failed after {MAX_ATTEMPTS} attempts: {last}")


def _is_daily_quota(exc: Exception) -> bool:
    """Whether this refusal is the per-day cap rather than the per-minute one.

    Keyed on the `quotaId` the API returns — `...RequestsPerDay...` — because
    that is the field that actually distinguishes them; the HTTP status, the
    message and the `retryDelay` are identical for both, and the `retryDelay`
    on a daily refusal is misleading (thirty-three seconds, for a window that
    reopens tomorrow).
    """
    return "PERDAY" in str(exc).upper().replace("_", "")


def _wait_for(exc: Exception, attempt: int) -> float:
    """How long to wait after a per-minute refusal.

    Prefers the server's own `retryDelay` over the local ladder — it knows
    when the window reopens and the ladder is guessing. Capped, and floored at
    the ladder's value so a suspiciously small server hint cannot turn the
    backoff into a hot loop against a quota.
    """
    ladder = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    if not match:
        return ladder
    return min(max(float(match.group(1)) + 1.0, ladder), MAX_SERVER_RETRY_DELAY)


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
