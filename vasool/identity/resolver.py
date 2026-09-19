"""The port, its refusing-to-guess default, and the one resolver that merges.

**Merging is the dangerous direction and the design says so.** Failing to merge
two records of one human lets a third contact through in a week — the bug A07
demonstrates. Merging two humans into one identity *suppresses* a contact
somebody was entitled to receive, and nothing in the ledger says it happened:
the receipt records a cap that bound, not a cap that bound the wrong person. So
the only join here is an exact match of a shared field after normalisation.
There is no similarity score and no threshold, because a threshold would be a
registered parameter whose sweep decides how often the system silences someone.

Pseudonymisation is unchanged. `identity_id` is derived under the same pepper
as `customer_id` (vasool/events/schemas.py), so this adds a join key and not a
second place where a raw phone number is kept.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Protocol

__all__ = [
    "IdentityResolver",
    "NullIdentityResolver",
    "UnionFindResolver",
    "normalise_contact",
    "normalise_email",
]


def normalise_email(email: str | None) -> str | None:
    """Case and surrounding whitespace, and nothing else.

    Not the local-part tricks — `a.b@gmail` and `ab@gmail` reach the same inbox
    at one provider and different inboxes at others, and a resolver that knows
    which is a resolver that merges on a guess.
    """
    if email is None:
        return None
    folded = email.strip().lower()
    return folded or None


def normalise_contact(contact: str | None) -> str | None:
    """Digits, with a leading country code dropped to the national number.

    `+91 98765 43210`, `+919876543210` and `9876543210` are one phone. Nothing
    is inferred beyond that: a ten-digit number and a nine-digit one are two
    numbers, however close they look.
    """
    if contact is None:
        return None
    digits = re.sub(r"\D", "", contact)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    return digits or None


class IdentityResolver(Protocol):
    """Which human a (contact, email) pair belongs to."""

    def identity_for(self, contact: str | None, email: str | None) -> str: ...


class NullIdentityResolver:
    """No resolution: every record is its own human.

    Exactly what the system did before this port existed, which is what makes
    it the safe default — wiring a resolver is a decision someone takes, and
    until they take it nothing about the cap changes. Unlike the DND registry's
    null adapter this one does not refuse: a cap that cannot resolve an
    identity still has a defensible unit to count, namely the record in front
    of it, and blocking every contact because identity is unknown would stop
    recovery on a system that has always run this way.
    """

    def __init__(self, pepper: str) -> None:
        self._pepper = pepper

    def identity_for(self, contact: str | None, email: str | None) -> str:
        return _keyed(self._pepper, f"{contact or ''}|{email or ''}")


class UnionFindResolver:
    """Records joined transitively through exactly-matching fields.

    Two records are the same human when they share a normalised contact or a
    normalised email. Union-find because sharing is transitive through a third
    record — A shares a phone with B, B shares an address with C — and because
    the answer must not depend on the order records arrive in: `identity_for`
    is a pure read of the sets built by `add`, and adding the same records in
    any order builds the same sets.

    The identity is named by its *lowest* member key rather than by a counter,
    so the same population always produces the same identity ids and a ledger
    stays comparable across runs.
    """

    def __init__(self, pepper: str) -> None:
        self._pepper = pepper
        self._parent: dict[str, str] = {}
        self._by_field: dict[tuple[str, str], str] = {}

    def add(self, contact: str | None, email: str | None) -> str:
        """Record one (contact, email) pair and return its identity."""
        key = _keyed(self._pepper, f"{contact or ''}|{email or ''}")
        self._parent.setdefault(key, key)
        for field, value in (("contact", normalise_contact(contact)), ("email", normalise_email(email))):
            if value is None:
                continue
            seen = self._by_field.setdefault((field, value), key)
            self._union(seen, key)
        return self._find(key)

    def identity_for(self, contact: str | None, email: str | None) -> str:
        key = _keyed(self._pepper, f"{contact or ''}|{email or ''}")
        if key not in self._parent:
            # A record nobody has added is its own human. Adding it here would
            # make a read mutate the resolver, and two guards reading in a
            # different order would then disagree about who someone is.
            return key
        return self._find(key)

    # -- union-find -------------------------------------------------------
    def _find(self, key: str) -> str:
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # path compression
            self._parent[key], key = root, self._parent[key]
        return root

    def _union(self, left: str, right: str) -> None:
        a, b = self._find(left), self._find(right)
        if a == b:
            return
        # The smaller key wins, so the identity a set answers with does not
        # depend on which record was added first.
        low, high = sorted((a, b))
        self._parent[high] = low


def _keyed(pepper: str, basis: str) -> str:
    """The same HMAC `derive_customer_id` uses, keyed on the same pepper."""
    return hmac.new(pepper.encode(), basis.encode(), hashlib.sha256).hexdigest()
