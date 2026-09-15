"""`make split-check` — could the published conclusion be an accident of the split?

Registered in docs/EVALUATION.md §10 on 2026-09-14, before any of its numbers
existed, and executed here exactly as registered.

**Why it exists.** §3c's split orders each stratum by an addressed draw keyed on
`customer_id`, and `customer_id` is keyed on the pepper — so the pepper chooses
which customers are development and which are holdout. The pepper behind every
published figure was registered with every output visible (§10, 2026-09-14,
POST-HOC), and nothing can now show it was not chosen after seeing a split.
What can be measured is what choosing a split could have bought, and this
measures it.

**As registered, and nothing else.** Five further peppers — the literals in
`SPLIT_CHECK_PEPPERS`, fixed in §10 — on the registered seeds `0..999`, with the
two arms §6a's primary comparison needs. §6a's inference is not re-implemented:
the rows come from `windtunnel.evaluate.collect` and the interval from
`windtunnel.evaluate.compare`, with the same seeded bootstrap that produced the
published one. **Registered outcome:** the published conclusion is robust to
the split if all five paired 95% intervals exclude zero with the published sign
(`registered_outcome` below). The five point estimates are reported beside the
published one with no threshold on their spread, because a threshold chosen
before seeing them would have had no basis.

**Why it was admissible only now.** Under another pepper, the development
cohort is dealt partly from customers who are holdout under the registered one.
§3c's holdout was evaluated once, on 2026-08-29, and is frozen; this could not
have been run before then. It reads no holdout result.

**It cannot impersonate itself.** A run over fewer than the registered thousand
seeds writes `split_check_partial.json`, never `split_check.json`. It refuses to
compare against a manifest produced by a different agent from the tree running
it. And it is resumable the way the evaluator is: one shard per (pepper, arm)
under `out/robustness/`, stamped with the agent fingerprint and refused by a
later tree. Nothing here reads the environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from windtunnel.arms import arm_named  # noqa: E402
from windtunnel.evaluate import (  # noqa: E402
    BASE_CONFIG,
    PRIMARY,
    REGISTERED_SEEDS,
    StaleShards,
    _Base,
    _series,
    collect,
    compare,
)
from windtunnel.fingerprint import agent_fingerprint  # noqa: E402
from windtunnel.split import Cohort  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "robustness"
MANIFEST = ROOT / "out" / "development" / "evaluation.json"

REGISTRATION = "docs/EVALUATION.md §10, 2026-09-14"

SPLIT_CHECK_PEPPERS: tuple[str, ...] = tuple(f"split-check-{i}" for i in range(1, 6))
"""The five literals §10 registered. tests/test_split_check.py reads them back
out of the row itself, so this tuple cannot drift from the registration."""

TREATMENT = "vasool"
BASELINE = "retry_plus_contact"
"""§6a's primary comparison, `paired_vs_vasool.retry_plus_contact.recovery_rate`:
Vasool minus the incumbent, per seed."""


def registered_outcome(published_point: float, intervals: list[dict]) -> bool:
    """§10's rule, verbatim: robust if every paired 95% interval excludes zero
    with the published sign — the whole interval on the side the published
    point is on, which is what "excludes zero" means once the sign is fixed."""
    if published_point < 0:
        return all(i["high"] < 0 for i in intervals)
    return all(i["low"] > 0 for i in intervals)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _published() -> dict:
    manifest = json.loads(MANIFEST.read_text())
    return {
        "agent_fingerprint": manifest["agent_fingerprint"],
        "pepper_sha256": manifest["pepper_sha256"],
        "vasool_recovery_rate_mean": manifest["per_arm"][TREATMENT]["recovery_rate_mean"],
        "paired": manifest["paired_vs_vasool"][BASELINE][PRIMARY],
    }


def run(*, out: pathlib.Path, seeds: list[int], workers: int, rebuild: bool) -> dict:
    fingerprint = agent_fingerprint()
    published = _published()
    if published["agent_fingerprint"] != fingerprint:
        raise SystemExit(
            f"error: out/development/evaluation.json was produced by agent "
            f"{published['agent_fingerprint'][:12]}…, but this tree is {fingerprint[:12]}…. "
            "A check against another agent's numbers would compare two different "
            "things; re-measure the manifest first."
        )

    arms = [arm_named(TREATMENT), arm_named(BASELINE)]
    checks = []
    for pepper in SPLIT_CHECK_PEPPERS:
        print(f"split check: {pepper}", file=sys.stderr)
        rows = collect(
            out=out / pepper, configs=[_Base()], arms=arms, seeds=seeds,
            cohort=Cohort.DEVELOPMENT.value, pepper=pepper, unseal=None,
            workers=workers, fingerprint=fingerprint,
            stale=StaleShards.REBUILD if rebuild else StaleShards.REFUSE,
        )[BASE_CONFIG]
        vasool = _series(rows[TREATMENT], PRIMARY)
        checks.append({
            "pepper": pepper,
            "pepper_sha256": _digest(pepper),
            "vasool_recovery_rate_mean": sum(vasool.values()) / len(vasool),
            "paired": compare(rows)[BASELINE][PRIMARY],
        })

    # Each pepper's pool imports the agent afresh, so an edit to a fingerprinted
    # file while this runs would put two agents under one stamp — INC-003's
    # shape. Checked, not assumed.
    if agent_fingerprint() != fingerprint:
        raise SystemExit(
            "error: the agent source changed while the check ran; its rows may "
            "come from two different agents. Nothing was written — run it again."
        )

    points = [c["paired"]["point"] for c in checks]
    return {
        "registered": REGISTRATION,
        "agent_fingerprint": fingerprint,
        "seeds": {"first": seeds[0], "last": seeds[-1], "count": len(seeds)},
        "cohort": Cohort.DEVELOPMENT.value,
        "treatment": TREATMENT,
        "baseline": BASELINE,
        "metric": PRIMARY,
        "published": published,
        "checks": checks,
        "criterion": (
            "robust to the split if all five paired 95% intervals exclude zero "
            "with the published sign"
        ),
        "robust": registered_outcome(published["paired"]["point"], [c["paired"] for c in checks]),
        "spread": {
            "point_min": min(points),
            "point_max": max(points),
            "published_point": published["paired"]["point"],
            "detail": "descriptive only: §10 registers no threshold on the spread",
        },
    }


def render(report: dict) -> str:
    def row(label: str, mean: float, paired: dict) -> str:
        return (
            f"  {label:<24}{mean:>10.4f}{paired['point'] * 100:>+12.2f}pp"
            f"   [{paired['low'] * 100:+.2f}, {paired['high'] * 100:+.2f}]pp"
        )

    published = report["published"]
    lines = [
        f"SPLIT CHECK   {REGISTRATION}   agent {report['agent_fingerprint'][:12]}…   "
        f"seeds {report['seeds']['first']}..{report['seeds']['last']}",
        "",
        f"  {'split':<24}{'Vasool':>10}{'Vasool − incumbent':>21}   95% interval",
        row("registered (published)", published["vasool_recovery_rate_mean"], published["paired"]),
    ]
    lines += [row(c["pepper"], c["vasool_recovery_rate_mean"], c["paired"]) for c in report["checks"]]
    verdict = "ROBUST" if report["robust"] else "NOT ROBUST"
    lines += ["", f"  {verdict} — {report['criterion']}."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="§10 2026-09-14's registered split check.")
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    parser.add_argument("--seeds", type=int, default=len(REGISTERED_SEEDS))
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--rebuild", action="store_true",
        help="recompute shards a different agent wrote, rather than refusing them",
    )
    args = parser.parse_args(argv)

    seeds = list(REGISTERED_SEEDS)[: args.seeds]
    report = run(out=args.out, seeds=seeds, workers=args.workers, rebuild=args.rebuild)
    complete = len(seeds) == len(REGISTERED_SEEDS)
    stem = "split_check" if complete else "split_check_partial"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (args.out / f"{stem}.txt").write_text(render(report))
    print(render(report))
    if not complete:
        print(f"note: {len(seeds)} of {len(REGISTERED_SEEDS)} registered seeds — written as "
              f"{stem}.* so it cannot be mistaken for the registered check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
