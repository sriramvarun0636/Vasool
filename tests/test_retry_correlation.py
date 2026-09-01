"""A failed retry advances its own episode instead of opening a new one.

`RazorpayClient.retry_payment` wraps `createRecurring`, which creates a NEW
payment with its own id — which is exactly why RetryIndex exists at all: a
*successful* retry's `payment.captured` carries that new id and has to be
correlated back to the original episode (docs/VERIFIED.md). A *failed* retry
has the identical problem. Razorpay fires `payment.failed` for the new
payment, `from_webhook` mints a FailureEvent from it, and
`PolicyMachine.observe` keys on `entity_id` — so without correlation a real
failed retry opens a brand-new episode at attempt 1 rather than advancing the
one it belongs to.

Three of the submission's claims rest on this working:

  - `RetryCapGuard` never counts past attempt 1, so the four-attempt halt is
    never enforced
  - docs/taxonomy.md §6's salary ladder can never reach attempt 2 or 3
  - `card_expired`'s flagship argument — that a futile retry burns one of the
    four attempts the re-auth link needed — describes a budget the system
    does not actually track

The proof that the correlation is load-bearing is the last class here: run
the same failure through `from_webhook` with the index and without it, and
watch one episode at attempt 2 become two episodes at attempt 1.
"""
from __future__ import annotations

from datetime import timedelta

from vasool.events.schemas import from_webhook
from vasool.policy.episode import State
from tests.payloads import TEST_PEPPER, body_for, event_for
from tests.test_receipts import NOON, make_machine


def _failed_retry_webhook(payment_id: str, *, reason: str = "gateway_technical_error") -> dict:
    """The `payment.failed` Razorpay fires when the payment `createRecurring`
    created does not authorise.

    A real envelope off disk with the retry's own payment id stamped on it —
    never a hand-typed shape (the project rules). The id is the one
    `RazorpayExecutor._retry` recorded from Razorpay's own response, which is
    the whole basis of the correlation.
    """
    body = body_for(reason)
    body["payload"]["payment"]["entity"]["id"] = payment_id
    return body


def _dispatch_first_retry(machine, clock, executor, *, reason="gateway_technical_error"):
    """Get one episode as far as a dispatched retry, and return what the
    executor told its RetryIndex about the payment that retry created."""
    event = event_for(reason)
    machine.observe(event)
    clock.advance_by(timedelta(minutes=6))
    machine.tick()

    retry_payment_id = "pay_retry_1"  # what FakeRazorpayClient.retry_payment returns
    assert executor.retry_index.entity_id_for(retry_payment_id) == event.entity_id
    return event, retry_payment_id


class TestTheIndexResolvesAFailedRetryToItsEpisode:
    def test_a_payment_id_the_index_knows_mints_an_event_for_the_original_episode(self):
        machine, clock, executor = make_machine()
        event, retry_payment_id = _dispatch_first_retry(machine, clock, executor)

        followup = from_webhook(
            event_id="evt_retry_failed_1",
            body=_failed_retry_webhook(retry_payment_id),
            pepper=TEST_PEPPER,
            retry_index=executor.retry_index,
        )

        assert followup.entity_id == event.entity_id
        assert followup.retried_payment_id == retry_payment_id

    def test_a_payment_id_the_index_does_not_know_is_left_alone(self):
        """The ordinary case, and it must stay ordinary: a customer's own
        first-ever failure is not a retry of anything."""
        machine, clock, executor = make_machine()

        minted = from_webhook(
            event_id="evt_ordinary",
            body=_failed_retry_webhook("pay_never_dispatched_by_us"),
            pepper=TEST_PEPPER,
            retry_index=executor.retry_index,
        )

        assert minted.entity_id == "pay_never_dispatched_by_us"
        assert minted.retried_payment_id is None

    def test_with_no_index_wired_nothing_is_correlated(self):
        """`retry_index` is optional, and omitting it has to leave the event
        exactly as it was before this correlation existed."""
        body = _failed_retry_webhook("pay_retry_1")

        assert from_webhook(
            event_id="evt", body=body, pepper=TEST_PEPPER
        ).entity_id == "pay_retry_1"

    def test_the_original_failure_is_never_rewritten(self):
        """Every envelope on disk decodes to itself. The correlation narrows
        what is treated as a continuation; it never widens what a
        payment.failed is trusted to mean."""
        machine, clock, executor = make_machine()
        _event, _retry_payment_id = _dispatch_first_retry(machine, clock, executor)
        body = body_for("gateway_technical_error")

        minted = from_webhook(
            event_id="evt", body=body, pepper=TEST_PEPPER, retry_index=executor.retry_index
        )

        assert minted.entity_id == body["payload"]["payment"]["entity"]["id"]
        assert minted.retried_payment_id is None


class TestTheLadderAdvances:
    def test_a_failed_retry_advances_the_episode_rather_than_opening_a_new_one(self):
        machine, clock, executor = make_machine()
        event, retry_payment_id = _dispatch_first_retry(machine, clock, executor)

        machine.observe(
            from_webhook(
                event_id="evt_retry_failed_1",
                body=_failed_retry_webhook(retry_payment_id),
                pepper=TEST_PEPPER,
                retry_index=executor.retry_index,
            )
        )

        assert machine.episodes.get(retry_payment_id) is None
        assert machine.state_of(event.entity_id) is State.SCHEDULED
        assert [item.proposal.attempt for item in machine.pending()] == [2]

    def test_the_ladder_reaches_the_attempt_taxonomy_gives_it(self):
        """docs/taxonomy.md §4: gateway_technical_error gets three silent
        retries on 5m/30m/4h, then a re-attempt link. Attempts 2 and 3 exist
        only if each failed retry is recognised as a continuation."""
        machine, clock, executor = make_machine()
        event, retry_payment_id = _dispatch_first_retry(machine, clock, executor)

        attempts = [1]
        for i in range(2):
            machine.observe(
                from_webhook(
                    event_id=f"evt_retry_failed_{i + 1}",
                    body=_failed_retry_webhook(retry_payment_id),
                    pepper=TEST_PEPPER,
                    retry_index=executor.retry_index,
                )
            )
            item = machine.pending()[0]
            attempts.append(item.proposal.attempt)
            clock.advance_to(item.proposal.execute_at)
            machine.tick()

        assert attempts == [1, 2, 3]
        assert machine.episodes.get(event.entity_id).attempts_used == 3

    def test_the_retry_cap_counts_a_failed_retry_against_the_episode(self):
        """RetryCapGuard counts `attempts_used` across the episode because
        that is what Razorpay counts before it halts a subscription. A ladder
        that fragments into fresh episodes leaves that counter at zero
        forever and the cap never binds."""
        machine, clock, executor = make_machine()
        event, retry_payment_id = _dispatch_first_retry(machine, clock, executor)

        machine.observe(
            from_webhook(
                event_id="evt_retry_failed_1",
                body=_failed_retry_webhook(retry_payment_id),
                pepper=TEST_PEPPER,
                retry_index=executor.retry_index,
            )
        )

        assert machine.episodes.get(event.entity_id).attempts_used == 1


class TestTheCorrelationIsLoadBearing:
    """The mechanical proof that the fix is wired.

    Invariance is what shows the fix is *correct* — windtunnel already
    modelled the documented intent, so the world it produces should not move
    — and invariance is therefore useless as evidence that anything is
    connected. This is the evidence: identical inputs, correlation withheld,
    and the ladder falls apart in exactly the way the defect described.
    """

    def _run(self, *, correlate: bool):
        machine, clock, executor = make_machine()
        event, retry_payment_id = _dispatch_first_retry(machine, clock, executor)
        machine.observe(
            from_webhook(
                event_id="evt_retry_failed_1",
                body=_failed_retry_webhook(retry_payment_id),
                pepper=TEST_PEPPER,
                retry_index=executor.retry_index if correlate else None,
            )
        )
        return machine, event, retry_payment_id

    def test_without_the_index_the_ladder_fragments_into_attempt_one_episodes(self):
        machine, event, retry_payment_id = self._run(correlate=False)

        assert machine.state_of(retry_payment_id) is State.SCHEDULED
        assert machine.episodes.get(event.entity_id).attempts_used == 1
        assert machine.episodes.get(retry_payment_id).attempts_used == 0
        assert [item.proposal.attempt for item in machine.pending()] == [1]

    def test_with_the_index_the_same_webhook_produces_one_episode_at_attempt_two(self):
        machine, event, retry_payment_id = self._run(correlate=True)

        assert machine.episodes.get(retry_payment_id) is None
        assert [item.proposal.attempt for item in machine.pending()] == [2]

    def test_the_two_runs_differ_only_in_whether_the_index_was_passed(self):
        """Stated as a comparison rather than two separate assertions, so the
        claim is that the index is the cause and not that two unrelated
        arrangements happen to differ."""
        fragmented, event, retry_payment_id = self._run(correlate=False)
        joined, _event, _retry_payment_id = self._run(correlate=True)

        assert len(fragmented.episodes.for_customer(event.customer_id)) == 2
        assert len(joined.episodes.for_customer(event.customer_id)) == 1
