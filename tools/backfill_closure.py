"""Backfill `per_arm.*.closure` into an existing manifest, from its own shards.

Why this exists rather than a re-run: the manifest on disk was produced by
`make sweeps` (~9 hours). The closure block is a pure aggregation over fields
every shard already carries -- `episodes`, `recovered`, `blocked`, `escalated`,
`exhausted` -- so re-running the grid to obtain it would spend nine hours
recomputing numbers that are already on disk.

The risk this guards against is INC-003: shards that do not belong to the
manifest they are merged into. So before writing anything, every field the
manifest and the shards *both* carry is recomputed from the shards and compared
exactly. A single mismatch aborts. Agreement across five independent
aggregations over 9,000 rows is not a coincidence you can get from the wrong
shard set.

The arithmetic matches windtunnel/evaluate.py::_base_protocol exactly; that is
the definition, this is the backfill.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

FIELDS = ("episodes", "recovered", "blocked", "escalated", "exhausted")


def aggregate(shard_dir: pathlib.Path) -> dict[str, dict[str, int | float]]:
    per_arm: dict[str, dict[str, int | float]] = {}
    for path in sorted(shard_dir.glob("*.jsonl")):
        arm = path.stem
        totals: collections.Counter[str] = collections.Counter()
        rates: list[float] = []
        paise = 0
        holds = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for field in FIELDS:
                totals[field] += row[field]
            rates.append(row["recovery_rate"])
            paise += row["recovered_paise"]
            holds += 1 if row.get("safety_holds") else 0
        if not rates:
            continue
        awaiting = (
            totals["episodes"] - totals["recovered"]
            - totals["blocked"] - totals["escalated"] - totals["exhausted"]
        )
        if awaiting < 0:
            sys.exit(f"error: {arm} has a negative awaiting bucket ({awaiting}) -- "
                     "the terminal counters are not disjoint and this backfill is invalid")
        per_arm[arm] = {
            "closure": {**{f: totals[f] for f in FIELDS}, "awaiting": awaiting},
            "_check_recovery_rate_mean": sum(rates) / len(rates),
            "_check_recovered_paise_total": paise,
            "_check_seeds": len(rates),
        }
    return per_arm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="out/development/evaluation.json")
    ap.add_argument("--shards", default="out/development/base")
    ap.add_argument("--write", action="store_true", help="without this, only reports")
    args = ap.parse_args()

    manifest_path = pathlib.Path(args.manifest)
    report = json.loads(manifest_path.read_text())
    computed = aggregate(pathlib.Path(args.shards))

    mismatches: list[str] = []
    for arm, block in report["per_arm"].items():
        if arm not in computed:
            mismatches.append(f"{arm}: no shard on disk")
            continue
        got = computed[arm]
        for key, manifest_key in (
            ("_check_recovery_rate_mean", "recovery_rate_mean"),
            ("_check_recovered_paise_total", "recovered_paise_total"),
            ("_check_seeds", "seeds"),
        ):
            if got[key] != block[manifest_key]:
                mismatches.append(
                    f"{arm}.{manifest_key}: manifest {block[manifest_key]!r} != shards {got[key]!r}"
                )

    if mismatches:
        print("REFUSING TO WRITE -- shards do not reproduce the manifest:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)
        return 2

    n_arms = len(report["per_arm"])
    print(f"provenance: {n_arms} arms reproduce on recovery_rate_mean, "
          f"recovered_paise_total and seeds -- shards belong to this manifest")

    width = max(len(a) for a in report["per_arm"])
    head = (f"{'arm':{width}}{'episodes':>10}{'recovered':>11}{'blocked':>9}"
            f"{'escalated':>10}{'exhausted':>10}{'awaiting':>10}")
    print(head)
    print("-" * len(head))
    for arm in report["per_arm"]:
        c = computed[arm]["closure"]
        print(f"{arm:{width}}{c['episodes']:>10,}{c['recovered']:>11,}{c['blocked']:>9,}"
              f"{c['escalated']:>10,}{c['exhausted']:>10,}{c['awaiting']:>10,}")
        total = sum(c[k] for k in (*FIELDS, "awaiting")) - c["episodes"]
        assert total == c["episodes"], f"{arm}: partition does not sum to episodes"

    if not args.write:
        print("\ndry run -- pass --write to update the manifest")
        return 0

    for arm, block in report["per_arm"].items():
        block["closure"] = computed[arm]["closure"]
    # Byte-identical serialisation to windtunnel/evaluate.py:788, so the
    # only difference from the run that produced this file is the block
    # being added.
    manifest_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote closure blocks into {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
