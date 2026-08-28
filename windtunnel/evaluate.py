"""`make eval` — EVALUATION.md's protocol, executed. One command, resumable.

**What this runs.** §5's arms and §8's ablations over the registered seed
range, §6's metrics per seed per arm, §6a's paired differences, §6b's `pass^k`,
and §9's falsification criteria. §7's sweep grid is behind `--sweeps` because
it is roughly a hundred times the work and §10 registers it on its own seed
range.

**Running part of the grid.** `--sweep-target NAME ...` runs only the named
parameters' four configurations, always with §10's equally-powered reference,
and `--skip-base` runs the grid without §6a's thousand-seed protocol — useful
once the base is done at power, since it will not change. Both are CLI
surface: which configurations *exist* is `windtunnel/sweeps.py`'s business and
is untouched. Two consequences follow and are enforced here. A subset run
**refuses to report an F6 verdict**, because F6 is registered against the
whole grid and a partial one would count fewer flips than the rule allows.
And `--skip-base` writes `sweeps.json` rather than `evaluation.json`, so a
partial run can never overwrite the manifest from a run at power.

**Resumability, and why it is a JSONL shard per (config, arm).** A thousand
seeds times nine arms is half an hour, and the sweep grid is an overnight run;
a crash at hour six must not mean starting again. Each completed seed appends
one line to `out/<cohort>/<config>/<arm>.jsonl`, and a resumed run reads the
seeds already present and skips them. Append-only, one line per seed, so a
half-written line at the moment of a kill costs exactly one seed — which the
resume then recomputes, because it is byte-identical (CLAUDE.md invariant 5).

**The holdout is not run by default and cannot be run by accident.** §3c seals
it, `--cohort holdout` demands the unseal phrase, and its results go to a
separate directory tree. See windtunnel/split.py.

**Nothing here reads a secret, or the environment, at all.** `VASOOL_ID_PEPPER`
keys the customer-id HMAC, so it is required — and it arrives as an argument.
No module in windtunnel/ may read the environment or reach the network, which
is enforced by a package scan rather than by intent
(tests/windtunnel/test_runner.py), so the lookup lives in `tools/evaluate.py`
and `make eval` goes through it. The report records only that a pepper was
configured, never its value.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict

from windtunnel.arms import ALL_ARMS, arm_named
from windtunnel.inference import (
    PASS_K_VALUES,
    PairedComparison,
    paired_difference,
    pass_k,
    survives,
)
from windtunnel.metrics import Metrics, measure
from windtunnel.runner import run_seed
from windtunnel.split import UNSEAL_PHRASE, Cohort, HoldoutSealed, split_customers
from windtunnel.sweeps import REFERENCE, sweep_configurations

REGISTERED_SEEDS = range(0, 1000)
"""§6a: "bootstrap over 1000 seeds, seed range fixed now at 0..999"."""

SWEEP_SEEDS = range(0, 200)
"""§10, 2026-08-23: §7's grid runs on 200 seeds, with survival judged against
an unswept reference recomputed on the same 200 — without which a conclusion
could cross zero from lost power rather than from the parameter, and F6 fires
on flips."""

BASE_CONFIG = "base"
"""Directory name for the unswept protocol. Distinct from the sweep grid's own
`reference`, which is the same configuration measured on the sweep's seeds."""


# ---------------------------------------------------------------------------
# one unit of work
# ---------------------------------------------------------------------------
class _Base:
    """The unswept protocol, as a configuration `collect` and `_work` can take.

    Distinct from `sweeps.REFERENCE` even though it perturbs nothing: the base
    protocol runs on §6a's registered 0..999 and the reference on §10's sweep
    range, and they are written to different directories so a resumed run
    cannot confuse a 200-seed shard for a 1000-seed one.
    """

    name = BASE_CONFIG
    kind = "base"
    target = "none"
    factor = None

    def spec(self):
        return REFERENCE.spec()


def _config_named(name: str):
    """Resolve a configuration inside a worker process — by name, never by
    pickling, for the same reason arms are (see `_work`)."""
    if name == BASE_CONFIG:
        return _Base()
    for config in sweep_configurations():
        if config.name == name:
            return config
    raise KeyError(f"{name!r} is not a registered sweep configuration")


def _row(metrics: Metrics) -> dict:
    """One seed's metrics, flattened for JSONL.

    Safety claims are written out by name rather than as a bare boolean: the
    report card renders all eight, and "the predicate held" without the claims
    behind it is exactly the unfalsifiable summary §2a warns against.
    """
    record = asdict(metrics)
    record["safety_holds"] = metrics.safety.holds
    record["safety"] = {c.name: {"passed": c.passed, "violations": c.violations} for c in metrics.safety.claims}
    record["reconciliation"] = [asdict(f) for f in metrics.reconciliation.findings]
    return record


def _work(job: tuple[int, str, str, str, str, str | None]) -> dict:
    """Run one (seed, arm, config) and measure it. Runs in a worker process.

    Arms and sweep configurations are looked up by name inside the worker
    rather than pickled across: a `Guard` is a live object the whole point of
    which is that every arm shares it, and shipping copies to subprocesses
    would quietly make that false.
    """
    seed, arm_name, config_name, cohort, pepper, unseal = job
    arm = arm_named(arm_name)
    config = _config_named(config_name)

    run = run_seed(seed, pepper=pepper, arm=arm, **config.spec().run_kwargs(seed))
    split = split_customers(run.universe)
    customers = (
        split.development
        if cohort == Cohort.DEVELOPMENT.value
        else split.holdout(unseal=unseal)
    )
    return _row(measure(run, arm=arm_name, cohort=cohort, customers=customers))


# ---------------------------------------------------------------------------
# the shard
# ---------------------------------------------------------------------------
def _shard(out: pathlib.Path, config: str, arm: str) -> pathlib.Path:
    path = out / config / f"{arm}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _done(path: pathlib.Path) -> dict[int, dict]:
    """Seeds already computed in this shard.

    A line that does not parse is dropped rather than fatal: the only way to
    get one is a kill mid-write, and the seed it belonged to is recomputed
    identically on the next pass.
    """
    if not path.exists():
        return {}
    rows: dict[int, dict] = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows[row["seed"]] = row
    return rows


def _execute(
    jobs: Sequence[tuple], *, workers: int, progress: bool
) -> Iterable[tuple[tuple, dict]]:
    if workers == 1:
        for job in jobs:
            yield job, _work(job)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for job, row in zip(jobs, pool.map(_work, jobs, chunksize=4)):
            yield job, row


def collect(
    *,
    out: pathlib.Path,
    configs: Sequence,
    arms: Sequence,
    seeds: Sequence[int],
    cohort: str,
    pepper: str,
    unseal: str | None,
    workers: int,
    progress: bool = True,
) -> dict[str, dict[str, dict[int, dict]]]:
    """Run everything not already on disk, and return every row.

    Returns `{config_name: {arm_name: {seed: row}}}`. Work is grouped by shard
    so a resumed run reads each file once.
    """
    results: dict[str, dict[str, dict[int, dict]]] = {}
    total = len(configs) * len(arms) * len(seeds)
    started = time.perf_counter()
    completed = 0

    for config in configs:
        results[config.name] = {}
        for arm in arms:
            path = _shard(out, config.name, arm.name)
            rows = _done(path)
            pending = [
                (seed, arm.name, config.name, cohort, pepper, unseal)
                for seed in seeds
                if seed not in rows
            ]
            completed += len(seeds) - len(pending)

            if pending:
                with path.open("a") as shard:
                    for job, row in _execute(pending, workers=workers, progress=progress):
                        shard.write(json.dumps(row, default=str) + "\n")
                        shard.flush()
                        rows[job[0]] = row
                        completed += 1
                        if progress and completed % 100 == 0:
                            rate = completed / max(time.perf_counter() - started, 1e-9)
                            print(
                                f"  {completed}/{total} runs  ({rate:.1f}/s)",
                                file=sys.stderr,
                            )
            results[config.name][arm.name] = rows
    return results


# ---------------------------------------------------------------------------
# §6a, §6b, §9
# ---------------------------------------------------------------------------
PRIMARY = "recovery_rate"
SECONDARY = (
    "recovered_paise",
    "attempts_per_recovery",
    "contacts_per_episode",
    "instrument_dead_retries",
    "escalated",
)
"""§6's primary and the secondaries that are scalar per seed. Time-to-recovery
is excluded from the paired comparison because it is None on a seed where an
arm recovered nothing, and a paired difference needs both sides on every
seed — it is reported per arm instead."""


def _series(rows: dict[int, dict], metric: str) -> dict[int, float]:
    return {seed: float(row[metric] or 0.0) for seed, row in rows.items()}


def _interval(comparison: PairedComparison) -> dict:
    """§6a's paired difference, serialised. The manifest's one interval shape.

    Extracted so §7's grid emits the same object the headline comparison does
    rather than a second, subtly different one. The two saying the same thing
    in two shapes is how a report card ends up comparing a magnitude against a
    boolean without noticing.
    """
    return {
        "n_seeds": comparison.n_seeds,
        "point": comparison.interval.point,
        "low": comparison.interval.low,
        "high": comparison.interval.high,
        "excludes_zero": comparison.interval.excludes_zero,
        "superior": comparison.superior,
    }


def compare(results: dict[str, dict[int, dict]], *, treatment: str = "vasool") -> dict:
    """§6a's paired differences: every other arm against Vasool."""
    out: dict[str, dict] = {}
    for arm, rows in results.items():
        if arm == treatment:
            continue
        out[arm] = {}
        for metric in (PRIMARY, *SECONDARY):
            out[arm][metric] = _interval(
                paired_difference(
                    _series(results[treatment], metric),
                    _series(rows, metric),
                    metric=metric,
                )
            )
    return out


# ---------------------------------------------------------------------------
# §7
# ---------------------------------------------------------------------------
ZERO_DIFFERENCE_DETAIL = (
    "sign test undefined for a zero-difference arm; excludes_zero is false by "
    "definition."
)
"""Why an arm whose per-seed difference is identically zero reports
`survives: false` in every configuration.

A4 is the case: it differs from Vasool only in how the guard chain resolves a
refusal, which in this simulator never changes an outcome, so `d_s = 0` on
every seed and the bootstrap interval is [0, 0]. `survives` requires the
*reference* interval to exclude zero — there has to be a conclusion before
there can be a surviving one — so A4 fails at the first line for a reason that
has nothing to do with the swept parameter. Reported as a note rather than
fixed in the verdict: §7's registered rule is the rule, and a null result
labelled robust would be a finding invented out of nothing (see
`inference.survives`).
"""


def _primary_difference(rows: dict[str, dict[int, dict]], arm: str) -> PairedComparison:
    """§6a's paired difference on §6's primary, for one arm in one
    configuration. §7 judges survival on the primary alone."""
    return paired_difference(
        _series(rows["vasool"], PRIMARY), _series(rows[arm], PRIMARY), metric=PRIMARY
    )


def reference_differences(
    swept: dict[str, dict[str, dict[int, dict]]],
) -> dict[str, PairedComparison]:
    """§10's equally-powered reference: the unswept comparison recomputed on
    the sweep's own 200 seeds, one per arm.

    Computed once for the whole grid rather than inside each of its eighty-odd
    blocks. It is the same eighty-odd times over, and it is the number every
    survival verdict is read against, so it gets one evaluation.
    """
    rows = swept[REFERENCE.name]
    return {arm: _primary_difference(rows, arm) for arm in rows if arm != "vasool"}


def sweep_verdicts(
    rows: dict[str, dict[int, dict]],
    reference: dict[str, PairedComparison],
    *,
    config,
) -> dict:
    """One §7 configuration: per arm, the survival verdict *and* the paired
    difference it was read off.

    `survives` is unchanged and stays the registered verdict — §7's wording is
    "survives all sweeps / survives some / flips" and that is the field it
    names. But a boolean is a sign test: it cannot separate a conclusion that
    barely held from one the parameter never came close to moving, and the
    symptom is a grid in which every block prints the same eight booleans and
    §7 appears to have tested nothing. The interval beside it carries the
    magnitude, so "survives all sweeps" becomes a statement about effect sizes
    rather than about signs — which is what §7 says the evaluation's
    credibility actually lives on.
    """
    arms: dict[str, dict] = {}
    for arm, unswept in reference.items():
        comparison = _primary_difference(rows, arm)
        entry: dict = {
            "survives": survives(unswept, comparison),
            "interval": _interval(comparison),
        }
        if all(difference == 0.0 for difference in unswept.differences):
            entry["detail"] = ZERO_DIFFERENCE_DETAIL
        arms[arm] = entry

    return {
        "kind": str(config.kind),
        "target": config.target,
        "factor": config.factor,
        "arms": arms,
    }


def sweep_targets() -> tuple[str, ...]:
    """Every name `--sweep-target` accepts.

    Derived from the grid rather than listed, so a parameter registered in a
    later §10 row is selectable without anyone remembering to touch the CLI.
    `ScalarSweep.target` is the parameter's name and `MixShift.target` is the
    composite's own name, so both kinds are addressable the same way.
    """
    return tuple(
        sorted({c.target for c in sweep_configurations() if c.name != REFERENCE.name})
    )


def selected_grid(targets: Sequence[str]) -> tuple:
    """§7's grid, or the subset `targets` names, with §10's reference in front.

    The reference is not optional and is never filtered out: survival is judged
    against an unswept comparison recomputed on the same 200 seeds (§10,
    2026-08-23), so a subset without it would have nothing to judge against.

    This selects which configurations *run*. It does not change which
    configurations exist — `sweep_configurations()` is still the registered
    grid, and F6 is still registered against all of it, which is why a subset
    run refuses to report an F6 verdict.
    """
    grid = sweep_configurations()
    if not targets:
        return grid
    wanted = set(targets)
    return tuple(c for c in grid if c.name == REFERENCE.name or c.target in wanted)


F6_PARTIAL_GRID = (
    "§7's grid was run in part (--sweep-target), and F6 is registered against "
    "the whole of it: every configuration is a chance to flip (§10, "
    "2026-08-24). A verdict from a subset would count fewer flips than the "
    "registered rule allows and report `fired: false` off work that was never "
    "done. Run the full grid — `make sweeps` — for a verdict."
)

F6_DENOMINATOR: tuple[str, ...] = (
    "naive_retry",
    "retry_plus_contact",
    "vasool_ungated",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
)
"""F6's denominator — the eight per-arm primary comparisons a swept parameter
can move.

Registered in two steps, and the second is the one that decides the size.
§10's **2026-08-24** row set the denominator at *seven*, excluding A4 on the
ground that its per-seed difference was identically [0, 0]. §10's
**2026-08-25** row, "A4's exclusion ground is now stale", dropped that
exclusion — post-fix A4's difference is +7.29e-05 and excludes zero, so the
original ground is empirically false and preserving the exclusion on a new
ground would have been the rescue the protocol forbids. Eight is therefore the
2026-08-25 reading, not the 2026-08-24 one; citing the earlier row for it
attributes this set to the row that registers its complement.

Not F1–F7: F6 is self-referential, F7 cannot be moved by a parameter, and F4
and F5 are not paired differences, so `survives` cannot express them.
"""

F6_THRESHOLD = 5
"""§9's "more than half", applied to §10's 2026-08-25 denominator of eight."""

F6_DETAIL = (
    "§10, 2026-08-25 ('A4's exclusion ground is now stale', which restored A4 "
    "to the denominator §10's 2026-08-24 row had set at seven): fires iff 5 or "
    "more of the 8 per-arm primary comparisons a swept parameter can move fail "
    "to survive in at least one of §7's configurations. Threshold is §9's "
    "'more than half' on that denominator. Primary metric only: a conclusion "
    "flipping solely on a secondary is not caught (§9)."
)


def f6_verdict(sweeps: dict[str, dict]) -> dict:
    """§9's F6, evaluated against §7's grid under §10's registered rule.

    A comparison counts as flipped if it fails to survive in **at least one**
    configuration — §9's "under some ±50% sweep" — and "±50% sweep" is the
    whole grid rather than its two endpoints, so every configuration in
    `sweeps` is a chance to flip.

    The configurations each arm flipped in are reported, not just the count.
    §7 exists to say *which* parameter made a conclusion an artifact, and a
    bare tally would answer the question §7 asks with the same sign bit this
    block was carrying before.
    """
    flipped: dict[str, list[str]] = {}
    for arm in F6_DENOMINATOR:
        configs = [
            name for name, block in sweeps.items() if not _survival(block, arm, name)
        ]
        if configs:
            flipped[arm] = configs

    return {
        "fired": len(flipped) >= F6_THRESHOLD,
        "detail": F6_DETAIL,
        "denominator": list(F6_DENOMINATOR),
        "threshold": F6_THRESHOLD,
        "configurations": len(sweeps),
        "flipped_count": len(flipped),
        "flipped": flipped,
    }


def _survival(block: dict, arm: str, config: str) -> bool:
    """One arm's verdict in one configuration, and a hard failure if it is
    absent.

    Skipping a missing arm would quietly shrink the numerator and make F6
    *harder* to fire, which is the one direction an error here must not go.
    """
    try:
        return block["arms"][arm]["survives"]
    except KeyError:
        raise KeyError(
            f"§7's grid has no {arm!r} comparison in configuration {config!r}. "
            "F6 cannot be evaluated on a denominator with a hole in it, and "
            "skipping the arm would make the criterion harder to fire."
        ) from None


def _direction(interval: dict | None) -> str:
    """Which way a paired difference actually came out.

    §9's criteria are registered as "the interval includes zero", and they stay
    exactly that — a pre-registered threshold is not something to redefine after
    seeing a number. But "did not fire" covers two opposite worlds: the
    treatment won, or the treatment lost badly. Reporting only the boolean would
    let a result where Vasool is *worse* than a baseline render as a clean pass.
    So the direction is reported alongside, as description rather than as a
    criterion.
    """
    if interval is None:
        return "not measured"
    if not interval["excludes_zero"]:
        return "no detectable difference"
    return "vasool ahead" if interval["point"] > 0 else "vasool behind"


def falsification(results: dict[str, dict[int, dict]], comparisons: dict) -> dict:
    """§9's criteria, evaluated. Registered in advance, with thresholds.

    Each entry says whether the criterion *fired* — i.e. whether the thing that
    would make this project wrong actually happened. F6 and F7 are not decided
    here: F6 needs the sweep grid and F7 is the determinism check.
    """
    vasool = results["vasool"]
    safety = [row["safety_holds"] for _, row in sorted(vasool.items())]
    reliability = pass_k(safety, [k for k in PASS_K_VALUES if k <= len(safety)])

    def interval(arm: str, metric: str) -> dict | None:
        return comparisons.get(arm, {}).get(metric)

    f1 = interval("retry_plus_contact", PRIMARY)
    f2_rate = interval("A3", PRIMARY)
    f2_attempts = interval("A3", "attempts_per_recovery")
    f3 = interval("A2", PRIMARY)

    ungated = _series(results["vasool_ungated"], PRIMARY)
    mine = _series(vasool, PRIMARY)
    gap_pp = 100.0 * (
        sum(ungated.values()) / len(ungated) - sum(mine.values()) / len(mine)
    )

    return {
        "F1_taxonomy_adds_nothing": {
            "fired": f1 is not None and not f1["excludes_zero"],
            "detail": (
                "paired difference vs retry_plus_contact on recovery rate. "
                "Registered wording: fires iff the interval INCLUDES zero. Read "
                "`direction` too — an interval excluding zero on the wrong side "
                "does not fire F1 as registered and is a worse result than F1, "
                "so `fired: false` alone must not be read as 'good'."
            ),
            "direction": _direction(f1),
            "interval": f1,
        },
        "F2_flagship_claim_inert": {
            "direction": _direction(f2_rate),
            "fired": bool(
                f2_rate
                and f2_attempts
                and not f2_rate["excludes_zero"]
                and not f2_attempts["excludes_zero"]
            ),
            "detail": "A3 must move recovery rate or attempts consumed; inert on both fires",
            "recovery_rate": f2_rate,
            "attempts_per_recovery": f2_attempts,
        },
        "F3_salary_timing_is_noise": {
            "fired": f3 is not None and not f3["excludes_zero"],
            "direction": _direction(f3),
            "interval": f3,
        },
        "F4_guards_unreliable": {
            "fired": reliability.get(100, 1.0) < 1.0,
            "detail": "pass^100 on the §2a predicate",
            "pass_k": reliability,
        },
        "F5_compliance_unaffordable": {
            "fired": gap_pp > 20.0,
            "detail": (
                "vasool_ungated minus vasool, absolute percentage points of "
                "recovery rate (§10, 2026-08-23: absolute, not relative)"
            ),
            "gap_pp": gap_pp,
            "threshold_pp": 20.0,
        },
        "F6_conclusions_are_model_artifacts": {
            "fired": None,
            "detail": "needs §7's sweep grid — run with --sweeps",
        },
        "F7_determinism_fails": {
            "fired": None,
            "detail": "reported by the determinism check, see manifest",
        },
    }


def determinism_check(seeds: Sequence[int], *, pepper: str) -> dict:
    """§6b / F7: two runs of one seed must produce byte-identical ledgers.

    Checked separately from `pass^k` and on a subset, because it is a property
    of the implementation rather than of the world — if it holds on a seed it
    holds on every seed for the same reason, and re-running all thousand twice
    buys a stronger statement only about compute spent.
    """
    mismatches = []
    for seed in seeds:
        first = run_seed(seed, pepper=pepper).ledger_digest()
        second = run_seed(seed, pepper=pepper).ledger_digest()
        if first != second:
            mismatches.append({"seed": seed, "first": first, "second": second})
    return {
        "seeds_checked": list(seeds),
        "identical": not mismatches,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None, *, pepper: str) -> int:
    """Run the protocol. `pepper` is required and is never read from here.

    Nothing in windtunnel/ touches the environment or a secret — the rule is
    structural and tests/windtunnel/test_runner.py scans the package for it, so
    the `VASOOL_ID_PEPPER` lookup lives in `tools/evaluate.py` and the value
    arrives as an argument. That is the same discipline
    `windtunnel/universe.py` already holds to for `build_universe`.
    """
    if not pepper:
        raise ValueError("a pepper is required — see tools/evaluate.py")

    parser = argparse.ArgumentParser(description="Run EVALUATION.md's protocol.")
    parser.add_argument("--out", default="out", type=pathlib.Path)
    parser.add_argument("--seeds", type=int, default=len(REGISTERED_SEEDS))
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--sweeps", action="store_true", help="also run §7's grid (hours)")
    parser.add_argument(
        "--sweep-target", nargs="+", metavar="NAME", default=[],
        help="run only these parameters' configurations from §7's grid, plus the "
             "reference. Implies --sweeps. F6 is not reported off a subset.",
    )
    parser.add_argument(
        "--skip-base", action="store_true",
        help="run §7's grid only, not §6a's 1000-seed protocol. Writes sweeps.json "
             "rather than evaluation.json, so a partial run cannot overwrite the "
             "manifest from a run at power. Requires --sweeps.",
    )
    parser.add_argument("--cohort", default=Cohort.DEVELOPMENT.value,
                        choices=[c.value for c in Cohort])
    parser.add_argument("--unseal", default=None,
                        help="the §3c phrase; required for --cohort holdout")
    args = parser.parse_args(argv)

    if args.sweep_target:
        unknown = sorted(set(args.sweep_target) - set(sweep_targets()))
        if unknown:
            parser.error(
                f"not a registered sweep target: {', '.join(unknown)}\n"
                "registered targets:\n  " + "\n  ".join(sweep_targets())
            )
        args.sweeps = True
    if args.skip_base and not args.sweeps:
        parser.error("--skip-base needs --sweeps: it would otherwise run nothing")

    if args.cohort == Cohort.HOLDOUT.value and args.unseal != UNSEAL_PHRASE:
        raise HoldoutSealed(
            "EVALUATION.md §3c evaluates the holdout once. Pass --unseal with the "
            "registered phrase, and write the §10 row before you do, not after."
        )

    out = args.out / args.cohort
    seeds = list(REGISTERED_SEEDS)[: args.seeds]
    started = time.perf_counter()

    report: dict = {"cohort": args.cohort, "arms": [a.name for a in ALL_ARMS]}

    if args.skip_base:
        print("--skip-base: §6a's protocol not run, §7's grid only", file=sys.stderr)
        report["base_protocol"] = {
            "run": False,
            "detail": (
                "§6a's 1000-seed protocol was not run (--skip-base), so F1–F5 and "
                "F7 are absent from this file rather than reported as not having "
                "fired. They are read off the base run, which is already done at "
                "power: see evaluation.json in this directory."
            ),
        }
    else:
        _base_protocol(
            report, out=out, seeds=seeds, cohort=args.cohort,
            pepper=pepper, unseal=args.unseal, workers=args.workers,
        )

    if args.sweeps:
        grid = selected_grid(args.sweep_target)
        swept_count = len(grid) - 1
        print(
            f"§7 sweeps: {swept_count} config(s) + reference x {len(SWEEP_SEEDS)} seeds",
            file=sys.stderr,
        )
        swept = collect(
            out=out / "sweeps", configs=grid, arms=ALL_ARMS, seeds=list(SWEEP_SEEDS),
            cohort=args.cohort, pepper=pepper, unseal=args.unseal, workers=args.workers,
        )
        unswept = reference_differences(swept)
        report["sweeps"] = {
            config.name: sweep_verdicts(swept[config.name], unswept, config=config)
            for config in grid
            if config.name != REFERENCE.name
        }
        report["sweep_reference"] = compare(swept[REFERENCE.name])
        report.setdefault("falsification", {})["F6_conclusions_are_model_artifacts"] = (
            f6_verdict(report["sweeps"])
            if not args.sweep_target
            else {
                "fired": None,
                "detail": F6_PARTIAL_GRID,
                "targets_run": sorted(args.sweep_target),
                "configurations_run": swept_count,
            }
        )

    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    destination = out / ("sweeps.json" if args.skip_base else "evaluation.json")
    destination.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {destination} in {report['elapsed_seconds']}s", file=sys.stderr)

    if not args.skip_base:
        predicate = report["per_arm"]["vasool"]["safety_holds_on"]
        print(f"§2a held on {predicate}/{len(seeds)} seeds", file=sys.stderr)
    return 0


def _base_protocol(
    report: dict, *, out: pathlib.Path, seeds: Sequence[int], cohort: str,
    pepper: str, unseal: str | None, workers: int,
) -> None:
    """§5's arms over §6a's registered range, and everything read off them.

    Split out of `main` so `--skip-base` omits these keys rather than filling
    them with placeholders: a manifest carrying `F1: null` beside a real F6
    reads as a criterion that was evaluated and did not fire.
    """
    print(f"base protocol: {len(ALL_ARMS)} arms x {len(seeds)} seeds -> {out}", file=sys.stderr)
    base = collect(
        out=out, configs=[_Base()], arms=ALL_ARMS, seeds=list(seeds), cohort=cohort,
        pepper=pepper, unseal=unseal, workers=workers,
    )[BASE_CONFIG]

    comparisons = compare(base)
    report.update({
        "seeds": {"first": seeds[0], "last": seeds[-1], "count": len(seeds)},
        "per_arm": {
            arm: {
                "recovery_rate_mean": sum(_series(rows, PRIMARY).values()) / len(rows),
                "recovered_paise_total": sum(r["recovered_paise"] for r in rows.values()),
                "safety_holds_on": sum(1 for r in rows.values() if r["safety_holds"]),
                "instrument_dead_retries_world": sum(r["instrument_dead_retries_world"] for r in rows.values()),
                "risk_block_actions_world": sum(
                    r["risk_block_actions_world"] for r in rows.values()
                ),
                # §10, 2026-08-24 registered the absence of this third counter as
                # an open limit and estimated the cost of closing it as a full
                # re-run of the §7 grid. That estimate was right when the row was
                # written and is no longer: `measure()` has recorded the field
                # since the obligation-loop re-run, so every shard on disk already
                # carries it and the closure is this line. See §10, 2026-08-28.
                "customer_action_retries_world": sum(
                    r["customer_action_retries_world"] for r in rows.values()
                ),
                "seeds": len(rows),
            }
            for arm, rows in base.items()
        },
        "paired_vs_vasool": comparisons,
        "pass_k": pass_k(
            [row["safety_holds"] for _, row in sorted(base["vasool"].items())],
            [k for k in PASS_K_VALUES if k <= len(seeds)],
        ),
        "determinism": determinism_check(seeds[:3], pepper=pepper),
        "falsification": falsification(base, comparisons),
        "pepper_configured": True,
    })
