"""tests/test_demo.py: the filmed artifact gets test coverage.

`make demo` is the thing a judge watches. Before this file, nothing asserted
its output was correct, only that it ran — every diff that changed a hash, a
statute, a verdict, or the classification rationale between now and the
recording would have been discovered on camera, not in CI.

Golden fixtures live in data/golden/ and are regenerated with exactly one
documented command: `python3 tools/update_golden.py` (also `make golden`).
Never hand-edit or copy-paste a fixture — see that script's docstring.
"""
from __future__ import annotations

import io
import pathlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

import pytest

from vasool.demo import clock_start, load_scenario, main
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import known_reasons

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "data" / "golden"

TEST_PEPPER = "test-pepper-do-not-use-in-prod"
"""Same value tests/payloads.py::TEST_PEPPER uses. The receipt hash a golden
fixture pins covers customer_id, which is HMAC(pepper, contact|email) — a
real .env pepper would make every fixture unreproducible off this machine."""

MAX_LINE_LENGTH = 100


@pytest.fixture(autouse=True)
def _fixed_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VASOOL_ID_PEPPER", TEST_PEPPER)


def run_demo(argv: list[str]) -> tuple[int, str, str]:
    """(exit_code, stdout, stderr) from an in-process vasool.demo run."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# the money shot: byte-identical to a committed fixture
# ---------------------------------------------------------------------------
class TestGoldenOutput:
    def test_card_expired_1930_matches_the_committed_fixture(self):
        rc, out, err = run_demo(["--scenario", "card_expired", "--time", "19:30", "--replay"])
        golden = (GOLDEN_DIR / "demo_card_expired_1930.txt").read_text()

        assert rc == 0
        assert err == ""
        assert out == golden

    def test_card_expired_1930_settled_matches_the_committed_fixture(self):
        """Item 2's RECOVERED path, pinned the same way — a real, non-zero
        amount_recovered_paise and the receipt that carries it."""
        rc, out, err = run_demo(
            ["--scenario", "card_expired", "--time", "19:30", "--replay", "--settle"]
        )
        golden = (GOLDEN_DIR / "demo_card_expired_1930_settled.txt").read_text()

        assert rc == 0
        assert err == ""
        assert out == golden
        assert "outcome      : recovered" in out
        assert "recovered    : ₹500.00 (50000 paise)" in out

    def test_card_expired_1930_hostile_matches_the_committed_fixture(self):
        """item 6: a pinned BLOCK path -- until now only ALLOW and DEFER
        were pinned, which meant the two outputs that prove restraint had no
        regression protection at all."""
        rc, out, err = run_demo(
            ["--scenario", "card_expired", "--time", "19:30", "--world", "hostile", "--replay"]
        )
        golden = (GOLDEN_DIR / "demo_card_expired_1930_hostile.txt").read_text()

        assert rc == 0
        assert err == ""
        assert out == golden
        assert "outcome      : blocked" in out

    def test_payment_risk_check_failed_matches_the_committed_fixture(self):
        """item 6: a pinned ESCALATED path -- taxonomy.md §5's "correct
        behaviour is indistinguishable from broken" case."""
        rc, out, err = run_demo(["--scenario", "payment_risk_check_failed", "--replay"])
        golden = (GOLDEN_DIR / "demo_payment_risk_check_failed.txt").read_text()

        assert rc == 0
        assert err == ""
        assert out == golden
        assert "outcome      : escalated" in out

    def test_insufficient_fund_1930_settled_matches_the_committed_fixture(self):
        """item 2 + item 6: the retry-settlement path, using insufficient_fund
        -- the class §6's whole salary-timing argument rests on. A TIMED_RETRY
        episode reaching RECOVERED with a real amount is what makes that
        section demonstrable rather than asserted."""
        rc, out, err = run_demo(
            ["--scenario", "insufficient_fund", "--time", "19:30", "--replay", "--settle"]
        )
        golden = (GOLDEN_DIR / "demo_insufficient_fund_1930_settled.txt").read_text()

        assert rc == 0
        assert err == ""
        assert out == golden
        assert "outcome      : recovered" in out
        assert "recovered    : ₹500.00 (50000 paise)" in out
        assert "event        : payment.captured" in out


# ---------------------------------------------------------------------------
# line width: asserted, not eyeballed
# ---------------------------------------------------------------------------
class TestLineWidth:
    def test_no_line_in_the_golden_run_exceeds_the_limit(self):
        _, out, _ = run_demo(["--scenario", "card_expired", "--time", "19:30", "--replay"])
        too_long = [line for line in out.splitlines() if len(line) > MAX_LINE_LENGTH]
        assert not too_long, f"lines over {MAX_LINE_LENGTH} chars: {too_long!r}"

    @pytest.mark.parametrize("reason", sorted(known_reasons()))
    def test_no_line_exceeds_the_limit_for_any_scenario(self, reason: str):
        _, out, _ = run_demo(["--scenario", reason, "--replay"])
        too_long = [line for line in out.splitlines() if len(line) > MAX_LINE_LENGTH]
        assert not too_long, f"{reason}: lines over {MAX_LINE_LENGTH} chars: {too_long!r}"


# ---------------------------------------------------------------------------
# every scenario runs clean under --replay
# ---------------------------------------------------------------------------
class TestEveryScenarioRunsClean:
    @pytest.mark.parametrize("reason", sorted(known_reasons()))
    def test_scenario_exits_zero_with_no_stderr(self, reason: str):
        rc, out, err = run_demo(["--scenario", reason, "--replay"])
        assert rc == 0
        assert err == ""
        assert out  # something was actually printed


# ---------------------------------------------------------------------------
# no real API calls in tests -- enforced, not observed
# ---------------------------------------------------------------------------
class TestReplayNeverTouchesTheNetwork:
    def test_poisoning_the_sdk_client_constructor_does_not_break_replay(self, monkeypatch: pytest.MonkeyPatch):
        """If --replay ever regressed into constructing a live client, this
        would raise instead of silently passing. Poisons the one seam every
        live Razorpay call has to pass through
        (vasool.actions.razorpay_client.razorpay.Client) rather than trusting
        that --replay behaves -- see vasool/actions/razorpay_client.py's own
        docstring on why that module is the sole permitted SDK import site."""
        import vasool.actions.razorpay_client as razorpay_client_module

        class ExplodingSDKClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "razorpay.Client() was constructed under --replay -- the "
                    "network boundary was touched in a test"
                )

        monkeypatch.setattr(razorpay_client_module.razorpay, "Client", ExplodingSDKClient)

        rc, out, err = run_demo(["--scenario", "card_expired", "--time", "19:30", "--replay"])

        assert rc == 0
        assert err == ""
        assert "REAUTH_LINK" in out

    def test_poisoning_razorpay_config_from_env_does_not_break_replay(self, monkeypatch: pytest.MonkeyPatch):
        """A second, independent seam: even reading credentials from the
        environment must never happen on the --replay path."""
        import vasool.actions.razorpay_client as razorpay_client_module

        def exploding_from_env(*args, **kwargs):
            raise AssertionError("RazorpayConfig.from_env() was called under --replay")

        monkeypatch.setattr(razorpay_client_module.RazorpayConfig, "from_env", staticmethod(exploding_from_env))

        rc, out, err = run_demo(["--scenario", "card_expired", "--replay", "--settle"])

        assert rc == 0
        assert err == ""

    def test_bare_invocation_with_no_flags_at_all_never_touches_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Item 1: replay is the default now, not something --replay has to
        opt into. The bare command a judge types first -- no --replay, no
        --live -- must be exactly as offline as an explicit --replay run."""
        import vasool.actions.razorpay_client as razorpay_client_module

        class ExplodingSDKClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "razorpay.Client() was constructed with no --live flag -- "
                    "replay is supposed to be the default"
                )

        monkeypatch.setattr(razorpay_client_module.razorpay, "Client", ExplodingSDKClient)

        rc, out, err = run_demo(["--scenario", "card_expired", "--time", "19:30"])

        assert rc == 0
        assert err == ""
        assert "mode         : replay" in out


# ---------------------------------------------------------------------------
# item 1: replay is the default; --live is opt-in and caveats up front
# ---------------------------------------------------------------------------
class TestLiveIsOptIn:
    def test_bare_invocation_defaults_to_replay_mode(self):
        _, out, _ = run_demo(["--scenario", "card_expired", "--time", "19:30"])
        assert "mode         : replay" in out
        assert "LIVE MODE" not in out

    def test_replay_flag_still_works_as_an_explicit_noop(self):
        """The documented money-shot command (and the golden fixtures, which
        pin it) must keep working unchanged now that it's also the default."""
        _, out, _ = run_demo(["--scenario", "card_expired", "--time", "19:30", "--replay"])
        assert "mode         : replay" in out

    def test_live_flag_prints_its_caveat_before_the_first_stage(self, monkeypatch: pytest.MonkeyPatch):
        """No real credentials are touched here -- RazorpayConfig.from_env is
        poisoned so this can never depend on (or accidentally exercise) a real
        .env, the same seam test_poisoning_razorpay_config_from_env_does_not_
        break_replay uses. What's under test is ordering: the caveat has to be
        on screen before stage [1], not interleaved with or after it."""
        import vasool.actions.razorpay_client as razorpay_client_module

        def exploding_from_env():
            raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set (test double)")

        monkeypatch.setattr(razorpay_client_module.RazorpayConfig, "from_env", staticmethod(exploding_from_env))

        rc, out, err = run_demo(["--scenario", "card_expired", "--time", "19:30", "--live"])

        assert rc == 0
        assert err == ""
        assert "LIVE MODE" in out
        assert out.index("LIVE MODE") < out.index("[1] webhook received")

    def test_live_and_replay_together_falls_back_to_replay(self, monkeypatch: pytest.MonkeyPatch):
        """--replay is the safety net: if it's on the command line at all,
        nothing live happens, even if --live is also there."""
        import vasool.actions.razorpay_client as razorpay_client_module

        class ExplodingSDKClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("razorpay.Client() was constructed despite --replay")

        monkeypatch.setattr(razorpay_client_module.razorpay, "Client", ExplodingSDKClient)

        rc, out, err = run_demo(
            ["--scenario", "card_expired", "--time", "19:30", "--live", "--replay"]
        )

        assert rc == 0
        assert err == ""
        assert "LIVE MODE" not in out
        assert "mode         : replay" in out


# ---------------------------------------------------------------------------
# item 4: a hostile world, and a guard that genuinely blocks
# ---------------------------------------------------------------------------
class TestHostileWorld:
    def test_hostile_world_blocks_with_a_statute_on_screen(self):
        rc, out, err = run_demo(["--scenario", "card_expired", "--world", "hostile", "--replay"])

        assert rc == 0
        assert err == ""
        assert "resolved     : BLOCK" in out
        assert "DPDP Act 2023 s.6 + DPDP Rules 2025" in out
        assert "outcome      : blocked" in out

    def test_permissive_world_is_still_the_default(self):
        _, out, _ = run_demo(["--scenario", "card_expired", "--replay"])
        assert "world        : permissive" in out

    def test_hostile_dlt_world_blocks_with_a_different_statute_and_guard(self):
        """item 5: a second guard, DLTTemplateGuard, blocking through a
        different mechanism (an unregistered template, not a missing
        consent record) and a different statute (TRAI, not DPDP)."""
        rc, out, err = run_demo(
            ["--scenario", "card_expired", "--world", "hostile_dlt", "--replay"]
        )
        flattened = " ".join(out.split())  # the statute line wraps; join across it

        assert rc == 0
        assert err == ""
        assert "resolved     : BLOCK" in out
        assert "TRAI TCCCPR — DLT template registration (Feb 2025 amendment)" in flattened
        assert "outcome      : blocked" in out
        # the deciding clause is the DLT statute, not ConsentGuard's DPDP one
        # (which still prints ALLOW alongside it -- every guard runs, per CLAUDE.md)
        assert "clause : TRAI TCCCPR" in flattened


# ---------------------------------------------------------------------------
# item 3: the escalation path no longer reads as its own opposite
# ---------------------------------------------------------------------------
class TestEscalationRendersHonestly:
    def test_a_risk_block_reads_escalated_not_allowed(self):
        _, out, _ = run_demo(["--scenario", "payment_risk_check_failed", "--replay"])
        flattened = " ".join(out.split())  # the summary line wraps; join across it

        assert "ESCALATED" in out
        assert "-- ALLOWED" not in out
        assert "stayed with a human, executor never called" in flattened


# ---------------------------------------------------------------------------
# item 5: the clock's roll-forward rule
# ---------------------------------------------------------------------------
class TestClockAnchoring:
    def test_a_time_before_the_payloads_own_capture_instant_rolls_forward(self):
        """The card_expired stub was captured in the afternoon IST. Asking
        for a --time before that, on the same day, must land on the next
        calendar day rather than before the webhook it is processing."""
        _, _, event = load_scenario("card_expired", pepper=TEST_PEPPER)
        occurred_ist = event.occurred_at.astimezone(IST)

        before = (occurred_ist - timedelta(hours=1)).strftime("%H:%M")
        start = clock_start(event, before)

        assert start.astimezone(IST).date() == occurred_ist.date() + timedelta(days=1)

    def test_a_time_after_the_payloads_own_capture_instant_does_not_roll(self):
        _, _, event = load_scenario("card_expired", pepper=TEST_PEPPER)
        occurred_ist = event.occurred_at.astimezone(IST)

        after = (occurred_ist + timedelta(hours=1)).strftime("%H:%M")
        start = clock_start(event, after)

        assert start.astimezone(IST).date() == occurred_ist.date()

    def test_the_default_time_is_documented_and_tested_the_same_way(self):
        """--time 19:30 does not roll (docs/VERIFIED.md's money shot); the
        default of noon does, for the same scenario. Pins the exact
        behaviour the session brief flagged as looking like nondeterminism."""
        _, _, event = load_scenario("card_expired", pepper=TEST_PEPPER)
        occurred_ist = event.occurred_at.astimezone(IST)

        default_start = clock_start(event, None)
        explicit_start = clock_start(event, "19:30")

        assert default_start.astimezone(IST).date() == occurred_ist.date() + timedelta(days=1)
        assert explicit_start.astimezone(IST).date() == occurred_ist.date()

    def test_the_roll_forward_is_visible_in_the_demos_own_output(self):
        _, out, _ = run_demo(["--scenario", "card_expired", "--replay"])  # default noon
        assert "rolled +1d" in out

        _, out_no_roll, _ = run_demo(["--scenario", "card_expired", "--time", "19:30", "--replay"])
        assert "rolled +1d" not in out_no_roll
