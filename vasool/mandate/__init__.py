"""The e-mandate: the standing authority a recurring debit is presented under.

Until 2026-09-15 a mandate was a boolean on PolicyFacts, set by the simulator,
with a comment saying production would read it from a mandate record. There was
no record and no lifecycle, so nothing could say that a mandate had been
revoked, had expired or was paused, and a retry built while it was live would
execute against it anyway.

Four modules, and the order they are read in matters:

  - `citations.py` — the documents this is built from, pinned by the SHA-256 of
    the bytes read, and every clause cited, quoted verbatim.
  - `states.py` — the six states.
  - `record.py` — `MandateRecord`, the thing `PolicyFacts.is_mandate` reads.
  - `machine.py` — the transitions, each carrying the clause that permits it.

**Built from documents, not observation.** Subscriptions are unavailable
pre-activation on the account this project was built against
(docs/VERIFIED.md), so no mandate of any kind has ever been observed here. A
subtly wrong lifecycle cannot be caught without a live account, so the design
makes every error visible instead: a transition that is wrong carries a
citation that is wrong, and a reader can find it, argue with it, and fix it.
tests/test_mandate.py fails on a transition without a citation, on a
citation that does not resolve to a pinned document, and on a quoted NPCI
response code that disagrees with data/cited_payloads/.
"""
