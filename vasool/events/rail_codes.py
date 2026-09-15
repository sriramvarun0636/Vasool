"""The port a rail's own failure code arrives through, and its null adapter.

NPCI's UPI response codes are mapped (vasool/diagnosis/npci.py), and on
Razorpay they never arrive: a Razorpay merchant is sent Razorpay's reason
(vasool/diagnosis/razorpay_upi.py), and no documented field carries NPCI's code
(docs/EVALUATION.md §10, 2026-09-15). A merchant on a provider that does pass
the rail's code on — a direct PSP integration — would supply an adapter here,
and a failure carrying a code is classified by NPCI's vocabulary rather than
the provider's.

The null adapter passes nothing, so on Razorpay every failure is classified
from what Razorpay sends. No adapter is built: which field would carry the code
depends on a provider this project has never seen, and guessing one would be
the invented field the project's rules forbid.
"""
from __future__ import annotations

from typing import Any, Protocol

__all__ = ["NullRailCodeSource", "RailCodeSource"]


class RailCodeSource(Protocol):
    def code_for(self, body: dict[str, Any]) -> tuple[str, str] | None:
        """NPCI's (section, code) for this failure webhook, or None when the
        provider does not say. Keyed by section because NPCI's own document
        gives one code two meanings (vasool/diagnosis/npci.py)."""
        ...


class NullRailCodeSource:
    """Passes no rail code. What every Razorpay webhook gets."""

    def code_for(self, body: dict[str, Any]) -> tuple[str, str] | None:
        return None
