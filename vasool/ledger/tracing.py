"""OpenTelemetry spans, one trace per recovery trajectory.

`trace_id_for` is deterministic — sha256 of the entity_id — and is what
receipts.py stores on every Receipt. It is deliberately not the value OTel's
own SDK would assign a live span: that generator is random by default, and
CLAUDE.md invariant 5 requires the same seed to produce a byte-identical
ledger, which a random id would break on every single run. The live span
still gets a real (random) trace/span id from the SDK for correlation in a
real tracing backend — but it also carries `vasool.trace_id` as an explicit
attribute set to the deterministic value, which is what a receipt and a
replay actually key on.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace

_TRACER = trace.get_tracer("vasool")


def trace_id_for(entity_id: str) -> str:
    """32 hex chars, deterministic. Not a live OTel trace id — see module
    docstring."""
    return hashlib.sha256(f"vasool-trace|{entity_id}".encode()).hexdigest()[:32]


@contextmanager
def recovery_span(entity_id: str, *, name: str = "recovery") -> Iterator[trace.Span]:
    """One span per recovery trajectory, tagged with the deterministic
    trace_id_for(entity_id) so a receipt and a live trace can be joined even
    though the ledger's id and OTel's own are computed independently.

    Works without a configured SDK/exporter — the OTel API falls back to a
    no-op tracer when nothing has called `set_tracer_provider`, so this is
    safe to call from tests and from a production entrypoint alike.
    """
    with _TRACER.start_as_current_span(name) as span:
        span.set_attribute("vasool.entity_id", entity_id)
        span.set_attribute("vasool.trace_id", trace_id_for(entity_id))
        yield span
