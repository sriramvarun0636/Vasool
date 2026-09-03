"""Export the demo's episodes as JSON for the episode theatre.

**Why this is not a second implementation.** The theatre replays a real
recovery episode, and a page that re-derived one from its own copy of the
logic could drift from the agent without anything failing — a demo that lies
is worse than no demo. So this drives `vasool.demo` itself with a different
`StageEmitter`. One traversal, two renderings, and the traversal is the one
`data/golden/*.txt` already pins to the byte.

**The episode list is imported, not retyped.** `tools/update_golden.py` owns
it. Loading that table rather than copying it means the theatre can only ever
show episodes whose text output is already byte-pinned by a golden fixture —
if the two lists could drift, the guarantee above would be worth nothing.

**The pepper is the fixed test value**, for the same reason the golden fixtures
use it: `customer_id` is HMAC(pepper, contact|email) and it feeds every receipt
hash. A real `.env` pepper would publish hashes nobody else could reproduce,
which is the opposite of the point — the page invites a stranger to recompute
them.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import pathlib
import sys
from contextlib import redirect_stdout

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run as a plain script, not -m

OUT_DIR = REPO_ROOT / "docs" / "theatre" / "episodes"
TEST_PEPPER = "test-pepper-do-not-use-in-prod"


def _fixtures() -> dict[str, list[str]]:
    """`update_golden.FIXTURES`, loaded by path because tools/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "_update_golden", REPO_ROOT / "tools" / "update_golden.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FIXTURES


_MOMENT = re.compile(r"\((\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) IST\)")
_CYCLE = re.compile(r"cycle (\d+)")


def _moment(title: str) -> str | None:
    """`2026-08-21 19:30` out of `guard chain -- cycle 1 (2026-08-21 19:30 IST)`."""
    found = _MOMENT.search(title)
    return f"{found.group(1)}T{found.group(2)}" if found else None


def _cycle(title: str) -> int | None:
    found = _CYCLE.search(title)
    return int(found.group(1)) if found else None


class JsonEmitter:
    """Accumulates the same traversal `TextEmitter` prints.

    `verdict` and `receipt` keep the domain objects' own fields rather than
    the formatted lines: the page needs every statute verbatim and the exact
    `canonical_payload` bytes the hash covers, and re-parsing those out of
    wrapped text is how a demo starts describing something it did not do.
    """

    def __init__(self) -> None:
        self.banner: dict = {"fields": [], "notes": []}
        self.steps: list[dict] = []
        self.conclusion: dict | None = None

    def _sink(self) -> dict:
        return self.steps[-1] if self.steps else self.banner

    # -- StageEmitter ------------------------------------------------------
    def rule(self, char: str = "=") -> None:
        """A horizontal rule is a typographic device, not an event."""

    def stage(self, n: int, title: str) -> None:
        self.steps.append(
            {
                "n": n,
                "title": title,
                # The moment the chain was evaluated, lifted out of the title so
                # the page never has to parse one. `_fmt_ist` writes it, golden
                # fixtures pin the format, and doing the extraction here means
                # it is done once in Python that tests cover rather than in a
                # browser where a miss would be silent.
                "at": _moment(title),
                "cycle": _cycle(title),
                "fields": [],
                "notes": [],
                "verdicts": [],
                "receipts": [],
            }
        )

    def kv(self, label: str, value: object, *, width: int = 13) -> None:
        self._sink()["fields"].append({"label": label, "value": str(value)})

    def block(self, label: str, text: str, *, width: int = 13) -> None:
        # The unwrapped text: the page wraps at its own measure, and the
        # fixture's 78-column wrap is an artifact of a terminal.
        self._sink()["fields"].append({"label": label, "value": text})

    def verdict(self, v) -> None:
        self.steps[-1]["verdicts"].append(v.model_dump(mode="json"))

    def receipt(self, r) -> None:
        self.steps[-1]["receipts"].append(
            {
                "receipt_id": r.receipt_id,
                "prev_hash": r.prev_hash,
                "hash": r.hash,
                "canonical_payload": r.canonical_payload,
                "entity_id": r.entity_id,
                "customer_id": r.customer_id,
                "event_id": r.event_id,
                "executed": r.executed,
                "outcome": r.outcome.value,
                "amount_recovered_paise": r.amount_recovered_paise,
                "at": r.at.isoformat(),
                "trace_id": r.trace_id,
            }
        )

    def line(self, text: str = "") -> None:
        if text.strip():
            self._sink()["notes"].append(text.strip())

    def summary(self, text: str, *, receipt_hash: str | None) -> None:
        # Document-level, not a note on the last step: it is the episode's
        # conclusion, and it arrives unwrapped so the page can set it at its
        # own measure rather than inheriting a 78-column terminal's.
        self.conclusion = {"text": text, "receipt_hash": receipt_hash}

    # -- result ------------------------------------------------------------
    def document(self, *, name: str, argv: list[str]) -> dict:
        return {
            "name": name,
            "argv": argv,
            "banner": self.banner,
            "steps": self.steps,
            "conclusion": self.conclusion,
        }


def export(name: str, argv: list[str]) -> dict:
    from vasool.demo import main  # deferred: needs VASOOL_ID_PEPPER set first

    emitter = JsonEmitter()
    # stdout is redirected rather than trusted to be silent: a stray print
    # added to demo.py later must not land in the exported file or the
    # terminal, and this makes that structural instead of hopeful.
    with redirect_stdout(io.StringIO()):
        rc = main(argv, emitter=emitter)
    if rc != 0:
        raise RuntimeError(f"vasool.demo {argv} exited {rc}; refusing to export a failed run")
    return emitter.document(name=name, argv=argv)


def regenerate() -> list[str]:
    os.environ["VASOOL_ID_PEPPER"] = TEST_PEPPER
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    index = []
    for filename, argv in _fixtures().items():
        name = filename.removeprefix("demo_").removesuffix(".txt")
        document = export(name, argv)
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(document, indent=2) + "\n")
        written.append(path.name)
        index.append(
            {
                "name": name,
                "file": path.name,
                "steps": len(document["steps"]),
                "receipts": sum(len(s["receipts"]) for s in document["steps"]),
            }
        )
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(path.read_text())} bytes)")
    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"wrote {(OUT_DIR / 'index.json').relative_to(REPO_ROOT)} ({len(index)} episodes)")

    # Also as a classic script assigning a global. The page could `fetch` these
    # files, but `fetch` is blocked under file:// and a judge who has cloned the
    # repository should be able to open the page by double-clicking it, the way
    # `make demo` works with no setup. A <script src> is not blocked, so this
    # costs one generated file and buys the page working from disk.
    bundle = [json.loads((OUT_DIR / row["file"]).read_text()) for row in index]
    script = OUT_DIR.parent / "episodes.js"
    script.write_text(
        "/* Generated by tools/export_episodes.py -- do not edit. */\n"
        "window.VASOOL_EPISODES = " + json.dumps(bundle, indent=1) + ";\n"
    )
    print(f"wrote {script.relative_to(REPO_ROOT)} ({len(script.read_text())} bytes)")
    return written


if __name__ == "__main__":
    regenerate()
