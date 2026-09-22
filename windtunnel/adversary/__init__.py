"""The adversary: the registered attacks, the criterion that scores them, and
the generator that proposes more.

Read `criterion.py` first. It was written before a single attack existed, and
it is the only thing in this package that can decide whether an attack
survived — including one a model proposed (`grammar.py`, `compile.py`,
`propose.py`, `novelty.py`; docs/EVALUATION.md §10, 2026-09-22).

This docstring used to say how many attacks there were, and the number went
stale twice; `attacks.py::ATTACKS` is the count, and a test holds it.
"""
