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
    REPEATS,
    build_corpus,
    compare,
    render_table,
    to_document,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CASSETTE_DIR = REPO_ROOT / "data" / "cassettes"
OUT_DIR = REPO_ROOT / "out" / "shadow"


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
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--model", default=None, help="override the recorded model")
    parser.add_argument("--rpm", type=int, default=None, help="requests per minute")
    parser.add_argument("--cassettes", type=pathlib.Path, default=CASSETTE_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=OUT_DIR)
    return parser.parse_args(argv)


def _replay_only(store, provider, model):
    def respond(prompt: str, repeat: int) -> str:
        return store.get(
            Request(provider=provider, model=model, prompt=prompt, repeat=repeat)
        ).response_text

    return respond


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

    from tools.gemini import DEFAULT_MODEL, DEFAULT_RPM, PROVIDER

    model = args.model or DEFAULT_MODEL
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
        respond = _replay_only(store, PROVIDER, model)

    try:
        comparison = compare(
            corpus, respond, repeats=args.repeats, provider=PROVIDER, model=model
        )
    except CassetteMiss as miss:
        print(f"error: {miss}", file=sys.stderr)
        return 3

    rendered = render_table(comparison)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "classifier_comparison.txt").write_text(rendered + "\n")

    import json

    (args.out / "classifier_comparison.json").write_text(
        json.dumps(to_document(comparison), indent=2, sort_keys=True) + "\n"
    )

    print()
    print(rendered)
    print(f"written: {args.out / 'classifier_comparison.txt'}")
    print(f"written: {args.out / 'classifier_comparison.json'}")
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
