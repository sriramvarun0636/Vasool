"""Telling a per-minute refusal from a per-day one.

Both are HTTP 429, both say RESOURCE_EXHAUSTED, both carry a `retryDelay` of a
few seconds. Only one of them is worth waiting out. The observed daily cap on
this project is twenty requests, so treating the two alike costs a fifth of a
day's budget on attempts that cannot succeed — which is exactly what the first
record run did before this existed.

**The fixture below is the refusal the API actually returned on 2026-08-24**,
copied verbatim rather than paraphrased. A hand-written approximation of an
error message is the same class of mistake as a hand-written error_reason: it
tests our idea of the wire format instead of the wire format.

Nothing here imports the provider SDK — the helpers under test are pure string
inspection, and `tools/gemini.py` imports the SDK lazily inside the client
constructor precisely so this file runs on a machine that has never installed
it.
"""
from __future__ import annotations

import pytest

from tools.gemini import (
    BACKOFF_SECONDS,
    MAX_SERVER_RETRY_DELAY,
    _is_daily_quota,
    _is_rate_limit,
    _wait_for,
)

OBSERVED_DAILY_REFUSAL = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded "
    "your current quota, please check your plan and billing details. For more "
    "information on this error, head to: "
    "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current "
    "usage, head to: https://ai.dev/rate-limit. \\n* Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 20, model: gemini-3.6-flash\\nPlease retry in 33.422413855s.', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': "
    "'type.googleapis.com/google.rpc.Help', 'links': [{'description': 'Learn "
    "more about Gemini API quotas', 'url': "
    "'https://ai.google.dev/gemini-api/docs/rate-limits'}]}, {'@type': "
    "'type.googleapis.com/google.rpc.QuotaFailure', 'violations': "
    "[{'quotaMetric': "
    "'generativelanguage.googleapis.com/generate_content_free_tier_requests', "
    "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
    "'quotaDimensions': {'model': 'gemini-3.6-flash', 'location': 'global'}, "
    "'quotaValue': '20'}]}, {'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
    "'retryDelay': '33s'}]}}"
)
"""Verbatim, from the record run on 2026-08-24. See tools/gemini.py."""

PER_MINUTE_REFUSAL = OBSERVED_DAILY_REFUSAL.replace(
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
)
"""The same refusal with only the quotaId changed — which is the only field
that actually distinguishes the two cases."""


class TestTheObservedRefusal:
    def test_it_is_recognised_as_a_rate_limit_at_all(self):
        assert _is_rate_limit(Exception(OBSERVED_DAILY_REFUSAL))

    def test_it_is_recognised_as_the_daily_cap(self):
        assert _is_daily_quota(Exception(OBSERVED_DAILY_REFUSAL))

    def test_a_per_minute_refusal_is_not_the_daily_cap(self):
        """The distinction the whole module turns on. Identical status,
        identical message, identical retryDelay — only the quotaId differs."""
        assert _is_rate_limit(Exception(PER_MINUTE_REFUSAL))
        assert not _is_daily_quota(Exception(PER_MINUTE_REFUSAL))

    def test_an_unrelated_failure_is_neither(self):
        connection_error = Exception("Connection reset by peer")
        assert not _is_rate_limit(connection_error)
        assert not _is_daily_quota(connection_error)

    def test_a_400_is_not_retried(self):
        bad_request = Exception("400 INVALID_ARGUMENT: unknown field response_format")
        assert not _is_rate_limit(bad_request)
        assert not _is_daily_quota(bad_request)


class TestTheWait:
    def test_the_servers_own_delay_is_preferred_over_the_ladder(self):
        """33s from the server beats 5s from a local guess: the server knows
        when the window reopens and the ladder is guessing."""
        assert _wait_for(Exception(PER_MINUTE_REFUSAL), 0) == pytest.approx(34.0)

    def test_the_ladder_is_used_when_the_server_says_nothing(self):
        bare = Exception("429 RESOURCE_EXHAUSTED")
        assert _wait_for(bare, 0) == BACKOFF_SECONDS[0]
        assert _wait_for(bare, 3) == BACKOFF_SECONDS[3]

    def test_a_suspiciously_short_server_delay_cannot_create_a_hot_loop(self):
        """Floored at the ladder. A 0s hint against a quota would spend the
        remaining budget in a second."""
        impatient = Exception("429 {'retryDelay': '0s'}")
        assert _wait_for(impatient, 2) >= BACKOFF_SECONDS[2]

    def test_an_absurd_server_delay_is_capped(self):
        """A wait measured in hours is an operator's decision, not one this
        process should take silently."""
        glacial = Exception("429 {'retryDelay': '86400s'}")
        assert _wait_for(glacial, 0) == MAX_SERVER_RETRY_DELAY

    def test_the_ladder_never_goes_backwards(self):
        bare = Exception("429")
        waits = [_wait_for(bare, attempt) for attempt in range(len(BACKOFF_SECONDS))]
        assert waits == sorted(waits)
