"""`make eval` — EVALUATION.md's protocol, executed. One command, resumable.

**What this runs.** §5's arms and §8's ablations over the registered seed
range, §6's metrics per seed per arm, §6a's paired differences, §6b's `pass^k`,
and §9's falsification criteria. §7's sweep grid is behind `--sweeps` because
it is roughly a hundred times the work and §10 registers it on its own seed
range.

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
from windtunnel.inference import PASS_K_VALUES, paired_difference, pass_k, survives
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


def compare(results: dict[str, dict[int, dict]], *, treatment: str = "vasool") -> dict:
    """§6a's paired differences: every other arm against Vasool."""
    out: dict[str, dict] = {}
    for arm, rows in results.items():
        if arm == treatment:
            continue
        out[arm] = {}
        for metric in (PRIMARY, *SECONDARY):
            comparison = paired_difference(
                _series(results[treatment], metric), _series(rows, metric), metric=metric
            )
            out[arm][metric] = {
                "n_seeds": comparison.n_seeds,
                "point": comparison.interval.point,
                "low": comparison.interval.low,
                "high": comparison.interval.high,
                "excludes_zero": comparison.interval.excludes_zero,
                "superior": comparison.superior,
            }
    return out


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
    parser.add_argument("--cohort", default=Cohort.DEVELOPMENT.value,
                        choices=[c.value for c in Cohort])
    parser.add_argument("--unseal", default=None,
                        help="the §3c phrase; required for --cohort holdout")
    args = parser.parse_args(argv)

    if args.cohort == Cohort.HOLDOUT.value and args.unseal != UNSEAL_PHRASE:
        raise HoldoutSealed(
            "EVALUATION.md §3c evaluates the holdout once. Pass --unseal with the "
            "registered phrase, and write the §10 row before you do, not after."
        )

    out = args.out / args.cohort
    seeds = list(REGISTERED_SEEDS)[: args.seeds]
    started = time.perf_counter()

    print(f"base protocol: {len(ALL_ARMS)} arms x {len(seeds)} seeds -> {out}", file=sys.stderr)
    base = collect(
        out=out, configs=[_Base()], arms=ALL_ARMS, seeds=seeds, cohort=args.cohort,
        pepper=pepper, unseal=args.unseal, workers=args.workers,
    )[BASE_CONFIG]

    comparisons = compare(base)
    report = {
        "cohort": args.cohort,
        "seeds": {"first": seeds[0], "last": seeds[-1], "count": len(seeds)},
        "arms": [a.name for a in ALL_ARMS],
        "per_arm": {
            arm: {
                "recovery_rate_mean": sum(_series(rows, PRIMARY).values()) / len(rows),
                "recovered_paise_total": sum(r["recovered_paise"] for r in rows.values()),
                "safety_holds_on": sum(1 for r in rows.values() if r["safety_holds"]),
                "instrument_dead_retries_world": sum(r["instrument_dead_retries_world"] for r in rows.values()),
                "risk_block_actions_world": sum(
                    r["risk_block_actions_world"] for r in rows.values()
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
    }

    if args.sweeps:
        grid = sweep_configurations()
        print(f"§7 sweeps: {len(grid)} configs x {len(SWEEP_SEEDS)} seeds", file=sys.stderr)
        swept = collect(
            out=out / "sweeps", configs=grid, arms=ALL_ARMS, seeds=list(SWEEP_SEEDS),
            cohort=args.cohort, pepper=pepper, unseal=args.unseal, workers=args.workers,
        )
        reference = compare(swept[REFERENCE.name])
        report["sweeps"] = {
            config.name: {
                "kind": str(config.kind),
                "target": config.target,
                "factor": config.factor,
                "survives": {
                    arm: survives(
                        paired_difference(
                            _series(swept[REFERENCE.name]["vasool"], PRIMARY),
                            _series(swept[REFERENCE.name][arm], PRIMARY),
                            metric=PRIMARY,
                        ),
                        paired_difference(
                            _series(swept[config.name]["vasool"], PRIMARY),
                            _series(swept[config.name][arm], PRIMARY),
                            metric=PRIMARY,
                        ),
                    )
                    for arm in swept[config.name]
                    if arm != "vasool"
                },
            }
            for config in grid
            if config.name != REFERENCE.name
        }
        report["sweep_reference"] = reference

    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    destination = out / "evaluation.json"
    destination.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {destination} in {report['elapsed_seconds']}s", file=sys.stderr)

    predicate = report["per_arm"]["vasool"]["safety_holds_on"]
    print(f"§2a held on {predicate}/{len(seeds)} seeds", file=sys.stderr)
    return 0
