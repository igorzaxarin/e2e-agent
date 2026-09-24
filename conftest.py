"""Register the J<NN> journey markers that the generated suites use.

`pytest.ini` runs with `--strict-markers`, so every `@pytest.mark.J<NN>` has to be
registered or collection fails for the whole tree — not just the journey at fault.

Keeping those lines in `pytest.ini` meant a tracked file that the pipeline rewrote on
every run over a new corpus: `git status` was never clean after a demo, and the diff
came from a script rather than from a person. Deriving them here instead removes the
churn at the source, and the set is exact by construction — the journeys that need a
marker are precisely the ones with generated tests on disk.

Markers owned by a human (stub, browser, blocker) stay in `pytest.ini`.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent / "output" / "tests"
JOURNEY_DIR = re.compile(r"^(J\d{2})-(.+)$")


def pytest_configure(config) -> None:
    if not TESTS_DIR.is_dir():
        return
    for entry in sorted(TESTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = JOURNEY_DIR.match(entry.name)
        if m:
            config.addinivalue_line(
                "markers", f"{m.group(1)}: Journey {m.group(1)} — {m.group(2)}"
            )
