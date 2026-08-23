"""`make eval`'s entry point: reads the environment, then hands off.

This file exists for one reason. No module in `windtunnel/` may read the
environment, reach the network, or touch a secret — the rule is structural and
`tests/windtunnel/test_runner.py` scans the package for it rather than trusting
that nobody will reach for `os.environ` in a later session. So the
`VASOOL_ID_PEPPER` lookup happens out here and the value is passed in, exactly
as `windtunnel/universe.py::build_universe` already requires.

`VASOOL_ID_PEPPER` keys the customer_id HMAC. Without it, customer ids are
brute-forcible from a phone number. Its value is never printed, logged, or
written to any output — only the fact that it was set.

Run as a script (`make eval`), so the repo root goes on `sys.path` explicitly
rather than relying on the working directory — `pytest.ini` sets `pythonpath`
for the test suite and nothing sets it here.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from windtunnel.evaluate import main  # noqa: E402

if __name__ == "__main__":
    load_dotenv()
    pepper = os.environ.get("VASOOL_ID_PEPPER")
    if not pepper:
        print("error: VASOOL_ID_PEPPER is not set -- see .env.example", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:], pepper=pepper))
