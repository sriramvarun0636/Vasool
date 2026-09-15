"""Where a fact came from: three tiers, no fourth, and no fact without one.

  - `OBSERVED` — `data/observed_payloads/`: captured live. A capture carries no
    marker of its own, because a capture must never be edited, even to label
    it; its directory is its declaration.
  - `CITED` — `data/cited_payloads/`: published by the operator of a rail.
    Neither observed nor invented, so it is a tier of its own rather than a
    stub with better manners. Every file carries a `_PROVENANCE` block with the
    fields in `CITED_FIELDS`, the SHA-256 among them, so a citation pins the
    bytes it cites rather than a URL that may move.
  - `SIMULATED` — `data/stubbed_payloads/`: hand-built from documentation, and
    marked `_SIMULATED: true` inside the file as well as by its directory.

Registered in docs/EVALUATION.md §10, 2026-09-15. A file never moves between
directories: a stub that survives contact with a real bank stays a stub, and
the observation is added beside it. `tier_of` refuses a file whose own markings
disagree with its directory, and tests/test_npci.py runs it over every payload
file there is, so a file whose tier cannot be read fails the suite.
"""
from __future__ import annotations

import json
import pathlib
from enum import StrEnum

__all__ = ["CITED_FIELDS", "DIRECTORY", "Provenance", "ProvenanceError", "payload_files", "tier_of"]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


class Provenance(StrEnum):
    OBSERVED = "OBSERVED"
    CITED = "CITED"
    SIMULATED = "SIMULATED"


DIRECTORY: dict[Provenance, pathlib.Path] = {
    Provenance.OBSERVED: REPO_ROOT / "data" / "observed_payloads",
    Provenance.CITED: REPO_ROOT / "data" / "cited_payloads",
    Provenance.SIMULATED: REPO_ROOT / "data" / "stubbed_payloads",
}

CITED_FIELDS: tuple[str, ...] = (
    "tier", "publisher", "document", "version", "clause", "pages",
    "retrieved", "retrieved_from", "sha256",
)
"""What a citation has to say to be checkable: who published what, which
version, which clause on which pages, when and where it was fetched, and the
hash of the bytes — so any copy can be verified, wherever it came from."""


class ProvenanceError(ValueError):
    """A file's own markings contradict the directory it sits in."""


def payload_files() -> list[pathlib.Path]:
    """Every payload file in every tier's directory. Dotfiles (`.gitkeep`)
    are directory furniture, not facts."""
    return sorted(
        path for directory in DIRECTORY.values() if directory.is_dir()
        for path in directory.iterdir() if path.is_file() and not path.name.startswith(".")
    )


def tier_of(path: pathlib.Path) -> Provenance:
    """The tier a payload file declares, checked against where it lives."""
    tiers = [tier for tier, directory in DIRECTORY.items() if path.parent == directory]
    if len(tiers) != 1:
        raise ProvenanceError(f"{path} is not in any tier's directory")
    tier = tiers[0]
    if path.suffix != ".json":
        raise ProvenanceError(f"{path}: a payload is a JSON document")
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ProvenanceError(f"{path}: a payload is a JSON object")

    simulated, provenance = document.get("_SIMULATED"), document.get("_PROVENANCE")
    if tier is Provenance.OBSERVED and (simulated is not None or provenance is not None):
        raise ProvenanceError(f"{path}: a capture carries a tier marker — captures are never edited")
    if tier is Provenance.SIMULATED and (simulated is not True or provenance is not None):
        raise ProvenanceError(f"{path}: a stub must carry `_SIMULATED: true` and nothing else")
    if tier is Provenance.CITED:
        if simulated is not None or not isinstance(provenance, dict):
            raise ProvenanceError(f"{path}: a citation must carry `_PROVENANCE` and no `_SIMULATED`")
        missing = [field for field in CITED_FIELDS if not provenance.get(field)]
        if missing or provenance["tier"] != Provenance.CITED.value:
            raise ProvenanceError(f"{path}: `_PROVENANCE` is missing {missing or 'tier: CITED'}")
    return tier
