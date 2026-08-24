"""`make shadow` — spec §4.5's classifier comparison, run and rendered.

This file exists for the same reason `tools/evaluate.py` does: no module in
`windtunnel/` may read the environment, reach the network, or touch a secret,
and that is enforced by a package scan rather than trusted. So the two
environment lookups happen out here and the values are passed in.

**Replay is the default, and a missing recording is fatal.** Without
`--record` this process constructs no provider client at all — the `respond`
callable handed to `windtunnel.shadow.compare` is a cassette lookup and
nothing else, so there is no code path from a cassette miss to a live call.
That is what makes the artifact's numbers re-derivable by anyone who clones
the repository with no key configured.

**`--record` is incremental.** Cassettes already on disk are reused and only
the missing ones are requested, so raising `--repeats` costs only the new
repeats and a run interrupted at request 90 of 180 resumes rather than
restarts. Same reasoning as the evaluator's JSONL shards.

`GEMINI_API_KEY` is read here and passed as an argument. Its value is never
printed, logged, or written to any output — only whether it was set.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from vasool.clock import RealClock  # noqa: E402
from vasool.diagnosis.llm import RESPONSE_SCHEMA  # noqa: E402
from windtunnel.cassette import CassetteMiss, CassetteStore, Request  # noqa: E402
from windtunnel.shadow import (  # noqa: E402
    PINNED_MODEL,
    PINNED_PROVIDER,
    REPEATS,
    build_corpus,
    compare,
    measure_stability,
    render_table,
    to_document,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CASSETTE_DIR = REPO_ROOT / "data" / "cassettes"
MAX_STABILITY_DEPTH = 200
"""Upper bound on how deep a cell's cassettes are counted. Only a search
bound — nothing is recorded by counting."""
OUT_DIR = REPO_ROOT / "out" / "shadow"


def default_model() -> str:
    """The model this tool records and replays against.

    A function rather than a re-exported constant so that nothing here can
    quietly hold a second copy of the pin — `tests/windtunnel/test_cassette_pin.py`
    asserts this returns exactly `PINNED_MODEL`, and the cassettes on disk
    agree with it.
    """
    return PINNED_MODEL


def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="make shadow",
        description="Run docs/VASOOL-design-spec.md §4.5's classifier comparison.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "call the provider for cassettes that are missing. Without this, a "
            "missing cassette is a hard failure and nothing reaches the network."
        ),
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help=(
            "replay only the cassettes that exist instead of failing on the "
            "first miss. Rates are computed over the recorded cells alone, the "
            "table is stamped PARTIAL, and the result is written to "
            "classifier_comparison_partial.* so it cannot impersonate a full run."
        ),
    )
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument(
        "--consistency-cell",
        default=None,
        metavar="REASON/SOURCE",
        help=(
            "additionally measure one cell at depth and render it as its own "
            "section, with its accuracy printed beside its stability. Depth is "
            "however many cassettes that cell has, unless --consistency-k says "
            "otherwise."
        ),
    )
    parser.add_argument("--consistency-k", type=int, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "override the pinned model. Every cassette is keyed by model, so "
            "this orphans the entire recorded corpus — see PINNED_MODEL."
        ),
    )
    parser.add_argument("--rpm", type=int, default=None, help="requests per minute")
    parser.add_argument("--cassettes", type=pathlib.Path, default=CASSETTE_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=OUT_DIR)
    return parser.parse_args(argv)


def _replay_only(store, provider, model, *, allow_missing: bool = False):
    """Replay, and nothing else. No client is constructed on this path.

    `allow_missing` reports an absent cassette as None rather than raising.
    That is a reporting mode, not a relaxation of the rule: the default is
    still a hard failure, and what a None becomes downstream is an em dash in
    the table and an exclusion from every rate — never a zero, and never a
    live call.
    """

    def respond(prompt: str, repeat: int) -> str | None:
        request = Request(
            provider=provider, model=model, prompt=prompt, repeat=repeat
        )
        if allow_missing and not store.has(request):
            return None
        return store.get(request).response_text

    return respond


def _coverage(store, corpus, provider, model, repeats):
    """Which cells have recordings, and how many, before anything is scored."""
    rows = []
    for cell in corpus:
        have = sum(
            1
            for repeat in range(repeats)
            if store.has(
                Request(
                    provider=provider, model=model, prompt=cell.prompt, repeat=repeat
                )
            )
        )
        rows.append((cell, have))
    return rows


def _recording(store, client, provider, labels, clock):
    """Reuse what is recorded, request only what is missing.

    The label is looked up from the corpus rather than passed through
    `compare`, which knows nothing about cassettes — it is cosmetic, and it
    exists so that a directory listing reads as the twelve cells rather than
    as a wall of hashes.
    """

    def respond(prompt: str, repeat: int) -> str:
        request = Request(
            provider=provider, model=client.model, prompt=prompt, repeat=repeat
        )
        try:
            return store.get(request).response_text
        except CassetteMiss:
            pass
        text = client.complete(prompt)
        store.put(
            request,
            text,
            label=labels.get(prompt, "cell"),
            recorded_at=clock.now(),
        )
        return text

    return respond


def main(argv, *, pepper: str, api_key: str | None) -> int:
    args = _parse(argv)

    from tools.gemini import DEFAULT_RPM

    model = args.model or default_model()
    if model != PINNED_MODEL:
        print(
            f"warning: --model {model!r} is not the pinned model "
            f"({PINNED_MODEL!r}). Cassettes are keyed by model, so every "
            "existing recording will miss and a full re-record costs a day "
            "against a 20-request quota. Continuing because you asked.",
            file=sys.stderr,
        )
    PROVIDER = PINNED_PROVIDER
    store = CassetteStore(args.cassettes)
    corpus = build_corpus(pepper=pepper)

    print(
        f"corpus: {len(corpus)} cells, "
        f"{sum(c.episodes for c in corpus)} development-cohort episodes, "
        f"{args.repeats} repeats — {len(corpus) * args.repeats} classifications"
    )
    print(f"cassettes: {store.count()} on disk in {args.cassettes}")

    if args.record:
        # Reported as a boolean, never as a value (CLAUDE.md).
        print(f"GEMINI_API_KEY configured: {bool(api_key)}")
        if not api_key:
            print(
                "error: --record needs GEMINI_API_KEY — see .env.example",
                file=sys.stderr,
            )
            return 2
        from tools.gemini import GeminiClient

        client = GeminiClient(
            api_key=api_key,
            model=model,
            rpm=args.rpm or DEFAULT_RPM,
            response_schema=RESPONSE_SCHEMA,
        )
        missing = sum(
            1
            for cell in corpus
            for repeat in range(args.repeats)
            if not store.has(
                Request(provider=PROVIDER, model=model, prompt=cell.prompt, repeat=repeat)
            )
        )
        print(f"recording {missing} missing cassettes at {args.rpm or DEFAULT_RPM} rpm")
        respond = _recording(
            store,
            client,
            PROVIDER,
            {cell.prompt: cell.label for cell in corpus},
            RealClock(),
        )
    else:
        respond = _replay_only(store, PROVIDER, model, allow_missing=args.partial)

    coverage = _coverage(store, corpus, PROVIDER, model, args.repeats)
    recorded = sum(have for _cell, have in coverage)
    print(
        f"coverage: {sum(1 for _c, h in coverage if h)} of {len(corpus)} cells, "
        f"{recorded} of {len(corpus) * args.repeats} classifications recorded"
    )
    for cell, have in coverage:
        marker = " " if have == args.repeats else ("~" if have else "!")
        print(f"  {marker} {cell.reason + ' / ' + cell.source:<44} {have:>3}/{args.repeats}")
    print()

    stability = None
    if args.consistency_cell:
        wanted = args.consistency_cell.replace("/", "__").replace(" ", "")
        matches = [c for c in corpus if c.label == wanted]
        if not matches:
            print(
                f"error: no cell named {args.consistency_cell!r}. Available: "
                + ", ".join(c.label for c in corpus),
                file=sys.stderr,
            )
            return 4
        cell = matches[0]
        depth = args.consistency_k or sum(
            1
            for repeat in range(MAX_STABILITY_DEPTH)
            if store.has(
                Request(
                    provider=PROVIDER, model=model, prompt=cell.prompt, repeat=repeat
                )
            )
        )
        if depth < 1:
            print(
                f"error: {cell.label} has no cassettes to measure at depth.",
                file=sys.stderr,
            )
            return 4
        try:
            stability = measure_stability(cell, respond, repeats=depth)
        except CassetteMiss as miss:
            print(f"error: {miss}", file=sys.stderr)
            return 3

    try:
        comparison = compare(
            corpus, respond, repeats=args.repeats, provider=PROVIDER, model=model
        )
    except CassetteMiss as miss:
        print(f"error: {miss}", file=sys.stderr)
        return 3

    rendered = render_table(comparison, stability=stability)
    args.out.mkdir(parents=True, exist_ok=True)

    # The name is decided by what the run achieved, not by which flag was
    # passed — the same rule windtunnel/evaluate.py applies when a partial
    # sweep grid writes sweeps.json rather than evaluation.json. A partial
    # result must not be able to overwrite or impersonate a full one.
    stem = "classifier_comparison" if comparison.complete else "classifier_comparison_partial"

    import json

    (args.out / f"{stem}.txt").write_text(rendered + "\n")
    (args.out / f"{stem}.json").write_text(
        json.dumps(to_document(comparison, stability=stability), indent=2, sort_keys=True)
        + "\n"
    )

    print()
    print(rendered)
    print(f"written: {args.out / (stem + '.txt')}")
    print(f"written: {args.out / (stem + '.json')}")
    if not comparison.complete:
        print(
            "note: PARTIAL — written under the _partial name so it cannot be "
            "mistaken for, or overwrite, a full run."
        )
    return 0


if __name__ == "__main__":
    load_dotenv()
    configured_pepper = os.environ.get("VASOOL_ID_PEPPER")
    if not configured_pepper:
        print("error: VASOOL_ID_PEPPER is not set -- see .env.example", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(
        main(
            sys.argv[1:],
            pepper=configured_pepper,
            api_key=os.environ.get("GEMINI_API_KEY"),
        )
    )
