#!/usr/bin/env python3
"""Stamp a new run and move the previous run's artefacts out of the way.

Two holes closed here, both of the same kind: the factory had no notion of *which run*
an artefact belongs to.

1. Nothing cleared `output/` between runs. A journey that existed in the previous corpus
   and not in this one stayed on disk — linted by /e2e:status, counted by the report,
   and visible to a human as if it were current.

2. `review_scope.py` keeps a hash baseline so a fix iteration only re-reviews what
   changed. That baseline survived the end of the run. On a fresh run over the same
   requirements, cases that happened to hash identically were reported as "unchanged",
   and the critic was told to carry findings forward from a review belonging to the
   *previous* run — up to and including a PASS on iteration 1 with no review at all.
   Everything else in review_scope fails closed; this failed open.

`output/.run` fixes the second by construction: review_scope hashes it with the other
global inputs, so a new run id invalidates every baseline. A missing `.run` also counts
as unknown, which means full review — the safe direction.

Usage:
    python3 scripts/start_run.py              # archive previous artefacts, stamp a new id
    python3 scripts/start_run.py --keep       # resuming: stamp nothing, report the id
    python3 scripts/start_run.py --show       # print the current run id and exit

Exit codes: 0 = run stamped, 2 = no run in progress (--show with no .run).
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

OUT = Path("output")
RUN_FILE = OUT / ".run"
PREVIOUS = OUT / ".previous"
RUN_STATE = OUT / ".run-state.json"

# Everything a run produces. Kept explicit: a glob would sweep .run and .previous too.
# pending.md was added to the contract one commit after this list was written and
# promptly missed — questions skipped by the previous run leaked into the next run's
# report. If a phase starts writing a new file under output/, it belongs here too.
# `.run-args.json` is deliberately NOT here: Phase 0 writes it before this script runs,
# and every later gate reads it. Archiving it would take the run's own arguments away.
# `.run-state.json` is not here either, for the opposite reason: it belongs to the run
# being replaced, so it is deleted below rather than archived.
ARTEFACTS = ["suites", "cases", "reviews", "state", "gate", "tests",
             "state.json", "report.md", "pending.md"]


def read_run() -> dict | None:
    if not RUN_FILE.is_file():
        return None
    try:
        return json.loads(RUN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def archive_previous() -> list[str]:
    """Move the previous run's artefacts into output/.previous, replacing what is there."""
    present = [name for name in ARTEFACTS if (OUT / name).exists()]
    if not present:
        return []
    if PREVIOUS.exists():
        shutil.rmtree(PREVIOUS)
    PREVIOUS.mkdir(parents=True)
    for name in present:
        shutil.move(str(OUT / name), str(PREVIOUS / name))
    return present


def main() -> int:
    ap = argparse.ArgumentParser(description="Начать прогон: новый id, прошлые артефакты в архив")
    ap.add_argument("--keep", action="store_true",
                    help="продолжить текущий прогон: ничего не архивировать и не менять id")
    ap.add_argument("--show", action="store_true", help="показать id текущего прогона")
    args = ap.parse_args()

    if args.show:
        run = read_run()
        if not run:
            sys.stderr.write("start_run: прогон не начат — нет output/.run\n")
            return 2
        print(f"[run] {run.get('runId')} начат {run.get('started')}")
        return 0

    OUT.mkdir(exist_ok=True)

    if args.keep:
        run = read_run()
        if run:
            print(f"[run] продолжаем {run.get('runId')} от {run.get('started')}")
            return 0
        print("[run] --keep, но идентификатора нет — ставлю новый, ничего не архивируя")

    moved = [] if args.keep else archive_previous()

    started = datetime.now().astimezone()
    # A second-resolution stamp is not an identity: two runs started inside the same
    # second collide, and a collision makes review_scope's invalidation silently pass —
    # fail-open, the exact failure this file exists to prevent. Caught in testing.
    previous_id = (read_run() or {}).get("runId")
    run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    while run_id == previous_id:
        run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    RUN_FILE.write_text(
        json.dumps({"runId": run_id, "started": started.isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # The progress store describes the run just replaced. run_state.py already refuses a
    # store stamped with another runId, but leaving it on disk means `show` reports
    # "no store" where it could report nothing at all.
    if RUN_STATE.exists():
        RUN_STATE.unlink()

    if moved:
        print(f"[run] артефакты прошлого прогона → {PREVIOUS}/: {', '.join(moved)}")
        print("[run] это единственный слот истории — следующий прогон их заменит")
    else:
        print("[run] прошлых артефактов не было")
    print(f"[run] новый прогон {run_id}")
    print("[run] базы хешей review_scope недействительны — первое ревью каждого journey полное")
    return 0


if __name__ == "__main__":
    sys.exit(main())
