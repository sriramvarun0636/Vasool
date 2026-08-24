"""`make redteam`'s entry point: run the adversary, print the score, write it.

Nothing here decides anything. The survival criterion lives in
`windtunnel/adversary/criterion.py`, was registered before any attack was
written, and is the only thing that produces a verdict — this file formats
what it returned.

Run as a script, so the repo root goes on `sys.path` explicitly; `pytest.ini`
sets `pythonpath` for the test suite and nothing sets it here. No environment
is read and no secret is touched: the adversary signs its own webhooks with a
constant and peppers its own customer ids, because its ids only have to be
stable within a run.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from windtunnel.adversary.harness import run_all, summary  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "out" / "adversary"


def main(argv: list[str] | None = None) -> int:
    results = run_all()
    report = summary(results)

    print(f"ADVERSARIAL   {report['survived']} / {report['attacks']} survived\n")
    for result in results:
        mark = "ok  " if result.survival.survived else "FAIL"
        print(f"  {mark} {result.attack.id}  {result.attack.title}")
        for clause in result.survival.failed():
            print(f"         -> {clause.name}: {clause.detail}")

    drifted = [r for r in results if not r.as_registered]
    if drifted:
        print("\n  registered expectation no longer holds:")
        for result in drifted:
            print(
                f"    {result.attack.id} registered {result.attack.expectation.value}, "
                f"actually {'survived' if result.survival.survived else 'failed'}"
            )

    print(
        "\n  Every failure above is a finding, not a defect in the harness. "
        "A clean\n  sheet would be evidence the attacks are too weak."
    )

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "redteam.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"\n  wrote {path.relative_to(pathlib.Path.cwd()) if path.is_relative_to(pathlib.Path.cwd()) else path}")
    return 0 if report["as_registered"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
