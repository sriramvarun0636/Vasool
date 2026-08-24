"""Recorded provider responses, addressed by what was asked.

**Why a recording and not a re-ask.** CLAUDE.md invariant 5 requires the same
seed to produce byte-identical output, and no provider offers that. There is
no temperature, seed or flag that makes one deterministic, and asserting
otherwise would be exactly the sort of unverified claim docs/VERIFIED.md
exists to stop. So Session 7 buys determinism twice: the classifier never runs
on any path that writes a ledger (tests/test_shadow_boundary.py walks the
import graph to prove it), and the comparison itself replays what was recorded
instead of asking again. Anyone who clones this repository can re-derive every
number in the artifact with no key configured and no network.

**A miss is a hard failure, never a quiet live call.** The failure this
prevents is not a deliberate re-record; it is an absent-minded one, weeks
later, when a prompt is edited by one character and a table silently reports
numbers measured against a model that has since moved. Same threat model as
windtunnel/split.py's unseal phrase — inattention, not malice — so the
mechanism is loud rather than clever.

**Provider-agnostic by construction.** This module knows a provider name, a
model name, a prompt and a repeat index. It knows nothing about who serves
them, holds no client, and offers no method that could obtain a response it
was not handed. A second arm on a different provider is a new client module
and no change here.

**Why the repeat index is part of the address.** Repeats are how
non-determinism gets measured rather than hidden: k answers to one question
give a self-consistency number the artifact reports beside accuracy. If the
key ignored the index, k repeats would collapse onto one cassette and that
column would read 1.00 for a reason that had nothing to do with the model. It
also makes raising k incremental — recording repeats 15..39 leaves 0..14
untouched.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime

CASSETTE_SUFFIX = ".json"

_UNSAFE_IN_A_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")
"""A label is for a human reading a directory listing. It is not a path, and
it must not be able to become one — it arrives from a cell name today, and a
cell name is derived from an error string read off disk."""


class CassetteMiss(LookupError):
    """No recording for this exact request. See the module docstring."""


@dataclass(frozen=True, slots=True)
class Request:
    """What was asked, in full. The address of a recording."""

    provider: str
    model: str
    prompt: str
    repeat: int

    @property
    def key(self) -> str:
        """sha256 over a separator-delimited basis.

        Two properties borrowed from windtunnel/rng.py, for the same reasons
        it gives. The delimiter is what stops ("ab", "c") and ("a", "bc")
        addressing the same recording — the ordinary way an addressing scheme
        silently correlates two things meant to be independent. And sha256
        rather than `hash()`, which is salted per process and would replay
        differently tomorrow.

        The prompt goes last because it is the only field that can contain the
        delimiter, and a field that can contain the delimiter cannot forge a
        different address when nothing follows it.
        """
        basis = "|".join([self.provider, self.model, str(self.repeat), self.prompt])
        return hashlib.sha256(basis.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Recording:
    """One response, as it came back, with the request that produced it."""

    request: Request
    response_text: str
    recorded_at: datetime | None
    """When this was recorded, if the recorder said. Optional, and injected
    rather than read: CLAUDE.md invariant 2 means this module cannot ask the
    wall clock what time it is, so the caller — who holds the clock — says.
    """

    @property
    def key(self) -> str:
        return self.request.key


def _document(recording: Recording, label: str) -> dict:
    return {
        "key": recording.key,
        "label": label,
        "provider": recording.request.provider,
        "model": recording.request.model,
        "repeat": recording.request.repeat,
        "recorded_at": (
            recording.recorded_at.isoformat() if recording.recorded_at else None
        ),
        "prompt": recording.request.prompt,
        "response_text": recording.response_text,
    }


def _recording_from(document: dict) -> Recording:
    recorded_at = document.get("recorded_at")
    return Recording(
        request=Request(
            provider=document["provider"],
            model=document["model"],
            prompt=document["prompt"],
            repeat=document["repeat"],
        ),
        response_text=document["response_text"],
        recorded_at=datetime.fromisoformat(recorded_at) if recorded_at else None,
    )


class CassetteStore:
    """A directory of recordings, indexed by request key.

    One file per recording rather than one appended log. A cassette is
    evidence: it should be readable on its own, reviewable in a diff, and
    nameable after the thing it answers — which an offset into a log is not.
    """

    def __init__(self, directory: pathlib.Path) -> None:
        self._directory = pathlib.Path(directory)
        self._index: dict[str, pathlib.Path] | None = None

    @property
    def directory(self) -> pathlib.Path:
        return self._directory

    def _load(self) -> dict[str, pathlib.Path]:
        if self._index is None:
            index: dict[str, pathlib.Path] = {}
            if self._directory.is_dir():
                for path in sorted(self._directory.glob(f"*{CASSETTE_SUFFIX}")):
                    document = json.loads(path.read_text())
                    index[document["key"]] = path
            self._index = index
        return self._index

    def count(self) -> int:
        return len(self._load())

    def has(self, request: Request) -> bool:
        return request.key in self._load()

    def get(self, request: Request) -> Recording:
        """The recording for this exact request, or `CassetteMiss`.

        Never a live call, never a nearest match, never None. The message names
        what is missing and how to obtain it, because whoever hits this needs
        to know which cell and repeat to re-record.
        """
        path = self._load().get(request.key)
        if path is None:
            raise CassetteMiss(
                f"no cassette for provider={request.provider!r} "
                f"model={request.model!r} repeat={request.repeat} "
                f"key={request.key[:12]}… in {self._directory} — "
                "replay cannot invent one. Re-record with --record, "
                "or restore the cassette."
            )
        return _recording_from(json.loads(path.read_text()))

    def put(
        self,
        request: Request,
        response_text: str,
        *,
        label: str,
        recorded_at: datetime | None = None,
    ) -> pathlib.Path:
        """Write one recording and return where it landed.

        Replaces any existing recording for the same key, including one filed
        under a different label — relabelling a cell must not orphan an
        expensive recording or leave two files claiming one address.
        """
        recording = Recording(
            request=request, response_text=response_text, recorded_at=recorded_at
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        index = self._load()

        existing = index.get(request.key)
        path = self._directory / self._filename(request, label)
        if existing is not None and existing != path:
            existing.unlink()

        path.write_text(
            json.dumps(_document(recording, label), indent=2, sort_keys=True) + "\n"
        )
        index[request.key] = path
        return path

    def _filename(self, request: Request, label: str) -> str:
        """`<label>__r<repeat>__<key prefix>.json`.

        The label is cosmetic and is not part of the key — it exists so a
        directory listing reads as the twelve cells of the corpus rather than
        as a wall of hashes. The key prefix is what makes the name unique.
        """
        safe = _UNSAFE_IN_A_LABEL.sub("-", label).strip("-.") or "cassette"
        return f"{safe}__r{request.repeat:02d}__{request.key[:12]}{CASSETTE_SUFFIX}"
