"""Which agent produced this shard.

**The failure this exists to prevent.** `windtunnel/evaluate.py::_done` reads a
shard into `{seed: row}` and skips every seed already present. That is correct
for a fixed agent and wrong across a change to one — architectural invariant 5
says the same seed yields a byte-identical ledger, but only while the code is
the same code. A shard carries a seed and a set of results and, until this
module, nothing at all about what produced them.

It has already cost a run. `POSTMORTEM.md` INC-003: the first `make eval` after
a real fix finished in 5.5 seconds and re-emitted pre-fix rows as a post-fix
evaluation. Nothing was corrupt and nothing raised; the resume did exactly what
it was written to do. Only the implausible elapsed time surfaced it, and the
mitigation since has been procedural — recompute the whole protocol into a
scratch directory and compare field by field, which the §10 row of 2026-08-29
records someone actually doing across 207,000 comparisons. That works exactly
as long as somebody remembers.

**What is covered, and the one rule behind it.** A file belongs in the
fingerprint when its contents can change the bytes of a shard row. That reaches
all of `vasool/` and all of `windtunnel/`, because the agent decides and the
windtunnel measures and both land in the row. Two files are excluded and each
exclusion is a claim `tests/windtunnel/test_fingerprint.py` checks rather than
a judgement recorded here:

  - `vasool/demo.py` — a CLI entry point. Nothing on the run path imports it,
    which the test asserts by loading `windtunnel.runner` and inspecting
    `sys.modules`. Excluding it means the §2.3 emitter refactor does not
    invalidate a shard it cannot have affected.
  - `windtunnel/fingerprint.py` — this module. It produces the stamp, never a
    row, and including it would make every edit here invalidate every shard for
    a change with no reach into the data.

**Deliberately no allowance for "that edit was only cosmetic."** A docstring
change in `evaluate.py` invalidates the shards, and the only remedy is
`--rebuild`, which recomputes. That is not an oversight. "I know this one did
not matter" is precisely the judgement INC-003 punished, and the cost is paid
once per measurement rather than once per edit, because a run is what you do
when you want numbers.

**The glob set is declared, and the resolution is checked in.** A bare glob
that silently starts matching a new file changes what the fingerprint means
without anybody deciding it should. `agent_manifest.txt` holds the resolved
list; a new agent module is therefore a deliberate two-line change — the file,
and the line naming it — rather than an accident.
"""
from __future__ import annotations

import hashlib
import pathlib

__all__ = [
    "AGENT_SOURCES",
    "EXCLUDED",
    "MANIFEST_PATH",
    "ManifestDrift",
    "agent_fingerprint",
    "agent_sources",
    "read_manifest",
    "write_manifest",
]

ROOT = pathlib.Path(__file__).resolve().parent.parent
"""The repository root. Derived from this file's own location rather than from
the environment, because no module in windtunnel/ may read the environment —
`tests/windtunnel/test_runner.py::TestNoNetwork` scans the package for it."""

AGENT_SOURCES: tuple[str, ...] = (
    "vasool/**/*.py",
    "windtunnel/**/*.py",
)
"""Globs, POSIX, relative to ROOT. Broad on purpose: see the module docstring
on why the rule is "can it change a row" rather than "is it a guard"."""

EXCLUDED: tuple[str, ...] = (
    "vasool/demo.py",
    "windtunnel/fingerprint.py",
)
"""Each of these is a checked claim, not a preference. See the module
docstring, and the two tests that hold them to it."""

MANIFEST_PATH = ROOT / "windtunnel" / "agent_manifest.txt"
"""Not a `.py`, so the globs above never match it — the manifest is not part of
what it describes, and editing it therefore does not change the fingerprint."""


class ManifestDrift(RuntimeError):
    """The globs resolve to a different set of files than the manifest names.

    Raised rather than silently re-resolved: a file appearing in or vanishing
    from the agent source set changes what a fingerprint means, and that is a
    decision somebody should make on purpose.
    """


def agent_sources(root: pathlib.Path = ROOT) -> tuple[str, ...]:
    """Every agent-side source file, as sorted POSIX paths relative to `root`.

    Sorted, POSIX and relative so the fingerprint is identical on macOS, Linux
    and CI — a hash that depends on filesystem ordering or on a path separator
    would be a fingerprint of the machine as much as of the code.
    """
    excluded = set(EXCLUDED)
    found: set[str] = set()
    for pattern in AGENT_SOURCES:
        for path in root.glob(pattern):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in excluded:
                found.add(relative)
    return tuple(sorted(found))


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agent_fingerprint(root: pathlib.Path = ROOT, *, check_manifest: bool = True) -> str:
    """A 64-hex digest over the agent-side source set.

    The pre-image is one line per file, `"<relative posix path> <sha256>"`,
    sorted by path, newline-terminated. Written out rather than left implicit
    because a fingerprint whose construction is undocumented cannot be
    reproduced by a reader, and reproducing it is the entire point.

    `check_manifest=False` exists for the tests that build a synthetic tree,
    and for `write_manifest` itself. Every production caller leaves it on.
    """
    sources = agent_sources(root)
    if check_manifest:
        declared = read_manifest()
        if sources != declared:
            raise ManifestDrift(_drift_message(declared, sources))

    lines = [f"{relative} {_digest(root / relative)}" for relative in sources]
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def _drift_message(declared: tuple[str, ...], found: tuple[str, ...]) -> str:
    added = sorted(set(found) - set(declared))
    removed = sorted(set(declared) - set(found))
    parts = [
        f"the agent source set has drifted from {MANIFEST_PATH.name}.",
        "",
        "This is not a bug to route around: a file entering or leaving the set "
        "changes what every fingerprint means. Decide whether it belongs, then "
        "regenerate with:",
        "",
        "    python -c 'from windtunnel.fingerprint import write_manifest; write_manifest()'",
        "",
    ]
    if added:
        parts += ["found but not declared:", *(f"  + {p}" for p in added), ""]
    if removed:
        parts += ["declared but not found:", *(f"  - {p}" for p in removed), ""]
    return "\n".join(parts)


def read_manifest() -> tuple[str, ...]:
    """The declared agent source set. Blank lines and `#` comments ignored."""
    if not MANIFEST_PATH.exists():
        raise ManifestDrift(
            f"{MANIFEST_PATH} is missing. Generate it with:\n"
            "    python -c 'from windtunnel.fingerprint import write_manifest; write_manifest()'"
        )
    lines = MANIFEST_PATH.read_text().splitlines()
    return tuple(
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def write_manifest(root: pathlib.Path = ROOT) -> tuple[str, ...]:
    """Regenerate `agent_manifest.txt` from the globs. Run deliberately."""
    sources = agent_sources(root)
    header = (
        "# The agent-side source set, resolved from windtunnel/fingerprint.py's\n"
        "# AGENT_SOURCES globs. Checked in so that a file entering or leaving the\n"
        "# set is a reviewable diff rather than a silent change in what every\n"
        "# fingerprint means. Regenerate deliberately, never to make a test pass:\n"
        "#\n"
        "#     python -c 'from windtunnel.fingerprint import write_manifest; write_manifest()'\n"
        "#\n"
    )
    MANIFEST_PATH.write_text(header + "\n".join(sources) + "\n")
    return sources
