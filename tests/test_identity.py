"""Identity resolution, the unit the contact cap counts, and the partition.

docs/EVALUATION.md §10, 2026-09-17. A07 — one human, two customer ids, four
contacts against a cap of three — was open from the day the attack suite was
written. It is closed by changing what the cap counts, not by changing the
guard, so these tests are mostly about the resolver and about the two
directions it can be wrong in. Under-merging lets a third contact through in a
week. Over-merging suppresses a contact somebody was entitled to and says
nothing about it, which is why the only join here is an exact match.
"""
from __future__ import annotations

import pytest

from vasool.events.schemas import derive_customer_id
from vasool.identity.resolver import (
    NullIdentityResolver,
    UnionFindResolver,
    normalise_contact,
    normalise_email,
)
from vasool.policy.partition import owner_of, owns

PEPPER = "test-pepper-do-not-use-in-prod"


class TestNormalisation:
    def test_an_email_folds_case_and_whitespace_and_nothing_else(self):
        assert normalise_email("  Rahul@Example.Invalid ") == "rahul@example.invalid"
        # The local-part tricks are deliberately not applied: a.b@ and ab@ are
        # one inbox at one provider and two at another, and a resolver that
        # knows which is a resolver that merges on a guess.
        assert normalise_email("a.b@example.invalid") != normalise_email("ab@example.invalid")

    def test_a_contact_folds_formatting_and_the_country_code(self):
        assert normalise_contact("+91 98765 43210") == "9876543210"
        assert normalise_contact("+919876543210") == normalise_contact("9876543210")

    def test_nothing_is_inferred_beyond_the_digits(self):
        assert normalise_contact("987654321") != normalise_contact("9876543210")

    def test_absent_stays_absent(self):
        assert normalise_email(None) is None and normalise_contact(None) is None
        assert normalise_email("   ") is None and normalise_contact("-") is None


class TestTheNullResolverChangesNothing:
    def test_it_answers_exactly_what_the_customer_id_is(self):
        """The safe default: wiring a resolver is a decision, and until it is
        taken the cap counts what it has always counted."""
        null = NullIdentityResolver(PEPPER)
        assert null.identity_for("+919876543210", "a@x.invalid") == derive_customer_id(
            "+919876543210", "a@x.invalid", pepper=PEPPER
        )

    def test_two_addresses_stay_two_records(self):
        null = NullIdentityResolver(PEPPER)
        assert null.identity_for("+919876543210", "a@x.invalid") != null.identity_for(
            "+919876543210", "b@x.invalid"
        )


class TestTheUnionFindResolverMerges:
    @pytest.fixture
    def resolver(self):
        r = UnionFindResolver(PEPPER)
        r.add("+919876543210", "rahul@x.invalid")
        r.add("9876543210", "r.kumar@x.invalid")  # A07: same phone, second address
        r.add("+918888888888", "R.Kumar@X.Invalid")  # third record, shared address
        r.add("+917777777777", "someone@x.invalid")  # unrelated
        return r

    def test_a_shared_contact_is_one_human(self, resolver):
        assert resolver.identity_for("+919876543210", "rahul@x.invalid") == resolver.identity_for(
            "9876543210", "r.kumar@x.invalid"
        )

    def test_sharing_is_transitive_through_a_third_record(self, resolver):
        """A shares a phone with B and B shares an address with C, so all three
        are one person. Union-find is here for this case."""
        assert resolver.identity_for("+919876543210", "rahul@x.invalid") == resolver.identity_for(
            "+918888888888", "R.Kumar@X.Invalid"
        )

    def test_someone_unrelated_stays_unrelated(self, resolver):
        assert resolver.identity_for("+917777777777", "someone@x.invalid") != resolver.identity_for(
            "+919876543210", "rahul@x.invalid"
        )

    def test_nothing_merges_on_a_near_match(self):
        """The over-merge direction, which is the dangerous one."""
        r = UnionFindResolver(PEPPER)
        r.add("+919876543210", "rahul@x.invalid")
        r.add("+919876543211", "rahul.k@x.invalid")
        assert r.identity_for("+919876543210", "rahul@x.invalid") != r.identity_for(
            "+919876543211", "rahul.k@x.invalid"
        )

    def test_the_answer_does_not_depend_on_arrival_order(self):
        """Two populations, same records, opposite order. A cap that counted a
        different history depending on which webhook arrived first would be a
        cap nobody could reason about."""
        forward = UnionFindResolver(PEPPER)
        backward = UnionFindResolver(PEPPER)
        records = [
            ("+919876543210", "a@x.invalid"),
            ("9876543210", "b@x.invalid"),
            ("+918888888888", "B@X.invalid"),
        ]
        for record in records:
            forward.add(*record)
        for record in reversed(records):
            backward.add(*record)
        assert {forward.identity_for(*r) for r in records} == {
            backward.identity_for(*r) for r in records
        }

    def test_reading_does_not_mutate(self):
        """`identity_for` on an unknown record answers for that record alone
        and does not enrol it — otherwise two guards reading in a different
        order would disagree about who someone is."""
        r = UnionFindResolver(PEPPER)
        r.add("+919876543210", "a@x.invalid")
        before = r.identity_for("+919876543210", "a@x.invalid")
        r.identity_for("+919876543210", "stranger@x.invalid")
        assert r.identity_for("+919876543210", "a@x.invalid") == before

    def test_an_identity_is_pseudonymous_like_a_customer_id(self):
        """Derived under the same pepper, so this adds a join key rather than a
        second place a phone number is kept."""
        r = UnionFindResolver(PEPPER)
        identity = r.add("+919876543210", "a@x.invalid")
        assert identity == derive_customer_id("+919876543210", "a@x.invalid", pepper=PEPPER)
        other = UnionFindResolver("a-different-pepper")
        assert other.add("+919876543210", "a@x.invalid") != identity


class TestThePartition:
    IDS = [f"{i:064x}" for i in range(2000)]

    def test_every_human_has_exactly_one_owner(self):
        for identity in self.IDS[:50]:
            assert sum(owns(w, identity, workers=4) for w in range(4)) == 1

    def test_ownership_is_stable(self):
        assert owner_of(self.IDS[7], workers=4) == owner_of(self.IDS[7], workers=4)

    def test_the_load_spreads(self):
        """Not a statistical claim about fairness — just that the partition is
        a partition and not a constant, which a hash of a digest could be if
        the slicing were wrong."""
        owners = {owner_of(i, workers=4) for i in self.IDS}
        assert owners == {0, 1, 2, 3}

    def test_a_partition_needs_a_worker(self):
        with pytest.raises(ValueError):
            owner_of(self.IDS[0], workers=0)


class TestTheCapCountsHumansEndToEnd:
    """The arena's own A07, asserted here as well as in the red team, because
    the red team's verdict is a scan of a ledger and this is the mechanism."""

    def test_two_records_of_one_human_share_a_contact_history(self):
        from windtunnel.adversary.arena import Arena

        arena = Arena()
        one = arena.person("rahul", email="rahul@example.invalid")
        two = arena.person("rahul", email="r.kumar@example.invalid", contact=one.contact)
        assert one.customer_id != two.customer_id, "still two payment identifiers"
        assert arena.facts.identity_of(one) == arena.facts.identity_of(two), "one human"

    def test_a_stranger_is_not_merged_into_them(self):
        from windtunnel.adversary.arena import Arena

        arena = Arena()
        one = arena.person("rahul", email="rahul@example.invalid")
        other = arena.person("priya", email="priya@example.invalid")
        assert arena.facts.identity_of(one) != arena.facts.identity_of(other)
