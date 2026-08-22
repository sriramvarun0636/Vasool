"""comms.py: the second line behind DLTTemplateGuard. Refuses to send
without a template, or with one the merchant hasn't registered, even when
called directly — bypassing whatever guard chain would normally have caught
it first.
"""
from __future__ import annotations

import pytest

from vasool.actions.comms import CommsRefused, CommsSender
from tests.policy.strategies import proposal_for, proposals_for
from vasool.diagnosis.proposal import ProposalRole


def make_sender():
    calls: list[tuple] = []

    def deliver(proposal, params):
        calls.append((proposal, params))
        return {"ok": True}

    return CommsSender(deliver=deliver), calls


class TestRefusal:
    def test_refuses_a_proposal_with_no_template(self):
        sender, calls = make_sender()
        proposal = proposal_for("card_expired").model_copy(update={"template_id": None})

        with pytest.raises(CommsRefused):
            sender.send(proposal=proposal, registered_templates=frozenset({"VASOOL_REAUTH"}))
        assert calls == []

    def test_refuses_an_unregistered_template_even_when_called_directly(self):
        """The test named in the session brief: no guard runs here at all.
        DLTTemplateGuard is never invoked; comms.py is called directly with a
        template the merchant never registered, and must refuse on its own."""
        sender, calls = make_sender()
        proposal = proposal_for("card_expired")
        assert proposal.template_id is not None

        with pytest.raises(CommsRefused):
            sender.send(proposal=proposal, registered_templates=frozenset())
        assert calls == []

    def test_refuses_a_proposal_with_no_channel(self):
        sender, calls = make_sender()
        proposal = proposal_for("card_expired").model_copy(update={"channel": None, "message_category": None})

        with pytest.raises(CommsRefused):
            sender.send(proposal=proposal, registered_templates=frozenset({proposal.template_id}))
        assert calls == []


class TestSend:
    def test_sends_a_registered_template(self):
        sender, calls = make_sender()
        proposal = proposal_for("card_expired")

        result = sender.send(proposal=proposal, registered_templates=frozenset({proposal.template_id}))

        assert result == {"ok": True}
        assert len(calls) == 1
        assert calls[0][0] is proposal

    def test_passes_params_through_to_the_deliverer(self):
        sender, calls = make_sender()
        proposal = proposal_for("card_expired")

        sender.send(
            proposal=proposal,
            registered_templates=frozenset({proposal.template_id}),
            params={"link": "https://example.test/x"},
        )

        assert calls[0][1] == {"link": "https://example.test/x"}

    def test_a_nudge_sends_on_its_own_template(self):
        """The NUDGE role carries a different template from its sibling
        retry (vasool/diagnosis/proposal.py::_TEMPLATES) — worth confirming
        comms.py checks the one the proposal actually carries."""
        nudge = next(p for p in proposals_for("insufficient_fund") if p.role is ProposalRole.NUDGE)
        sender, calls = make_sender()

        sender.send(proposal=nudge, registered_templates=frozenset({nudge.template_id}))

        assert len(calls) == 1
