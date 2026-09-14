"""`make eval`'s entry point. It reads nothing, and that is the point.

This file used to exist to read `VASOOL_ID_PEPPER` from the environment and
pass it in, because no module in `windtunnel/` may read the environment, reach
the network, or touch a secret. The pepper keys the customer_id HMAC and so
decides §3c's split, which made every published figure a function of a value
on one machine. Since 2026-09-14 it is registered in `windtunnel/pepper.py` and
`windtunnel.evaluate.main` uses it directly (docs/EVALUATION.md §10). Nothing
here may pass a pepper, load `.env`, or read the environment, so no shell and
no `.env` can change a number this command writes;
tests/windtunnel/test_evaluate.py holds that line.

Run as a script (`make eval`), so the repo root goes on `sys.path` explicitly
rather than relying on the working directory — `pytest.ini` sets `pythonpath`
for the test suite and nothing sets it here.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from windtunnel.evaluate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
