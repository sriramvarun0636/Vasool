"""The pepper every simulated customer is keyed under -- registered, and public.

`customer_id` is an HMAC of a pepper over contact and email
(`vasool/events/schemas.py::derive_customer_id`), and in the simulator that
reaches every number the protocol reports: ContactWindowGuard's jitter is
derived from `customer_id`, and so is the order in which §3c's split deals each
stratum to development or holdout (`windtunnel/split.py`). A different pepper is
a different world. Until 2026-09-14 this value was read from the author's `.env`
and named nowhere, so nobody else could regenerate any published figure. The
record, and the reasoning, is docs/EVALUATION.md §10, 2026-09-14.

**Not a secret, and never to be used as one.** It keys simulated customers
only: `windtunnel/payloads.py::failure_event` stamps the simulator's own contact
and email onto every envelope before the id is derived. It stands beside
`windtunnel/adversary/arena.py::ADVERSARY_PEPPER` and the tests' `TEST_PEPPER`
for the same reason. A deployment keying real customers needs a pepper of its
own, configured where it runs -- `vasool/demo.py --live` refuses to start
without one.

**Why the value that was already in use.** It is the pepper every development,
sweep, holdout and shadow figure was produced under, so publishing it moves no
number and makes all of them -- the holdout included -- reproducible from
source. Registering a new one would have re-dealt every universe. That it was
registered with every output visible is marked POST-HOC in §10, and the check
that bounds what the choice could have bought is registered beside it.

**It is inside the fingerprint.** This file matches `windtunnel/**/*.py`, so
editing the value moves `agent_fingerprint()` and a resume refuses every shard
produced under the old one: the world is pinned the same way the code is.
"""
from __future__ import annotations

__all__ = ["REGISTERED_PEPPER"]

REGISTERED_PEPPER: str = "2ad9cc2d32f55767507e493432c44fcfa3c4b838b02d68870311168a5a66da23"
"""The world. Changing it re-deals every universe; see the module docstring."""
