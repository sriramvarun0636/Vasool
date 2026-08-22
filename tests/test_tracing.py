"""tracing.py: trace_id_for is deterministic — not OTel's own random span id
(CLAUDE.md invariant 5 would break the instant a receipt embedded a random
value). recovery_span works with no SDK/exporter configured, since a test
suite has none.
"""
from __future__ import annotations

from vasool.ledger.tracing import recovery_span, trace_id_for


class TestTraceIdDeterminism:
    def test_the_same_entity_id_always_produces_the_same_trace_id(self):
        assert trace_id_for("pay_abc123") == trace_id_for("pay_abc123")

    def test_different_entity_ids_produce_different_trace_ids(self):
        assert trace_id_for("pay_abc123") != trace_id_for("pay_xyz789")

    def test_it_is_not_a_wire_value_razorpay_ever_sent(self):
        """32 hex chars — sha256 truncated, not a UUID some SDK generated."""
        trace_id = trace_id_for("pay_abc123")
        assert len(trace_id) == 32
        int(trace_id, 16)  # raises ValueError if not hex


class TestRecoverySpan:
    def test_it_works_with_no_sdk_configured(self):
        """The OTel API falls back to a no-op tracer when nothing has called
        set_tracer_provider — this must not raise even so."""
        with recovery_span("pay_abc123") as span:
            assert span is not None
