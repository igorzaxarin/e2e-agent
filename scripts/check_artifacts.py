#!/usr/bin/env python3
"""Compare what a stage produced against what the plan said it would produce.

Everything else in the factory checks whether an artefact is *correct*. Nothing
checked whether it *exists*. That gap is the difference between a run that stops and
a run that quietly continues with less than it should have:

  - an agent that dies mid-way (API error, timeout) leaves part of its work on disk,
    and the pipeline's only answer was FAILED — throwing away good artefacts;
  - an agent that hits max_turns returns like any other agent, just with fewer files;
  - an agent that writes 3 of 5 cases passes the linter, because the linter grades the
    cases that exist.

The expectation is derivable, not guessed: the suite plan's «Варианты» table names every
variant case, so the design stage owes exactly the main case plus one file per row.
Review and test stages have fixed contracts.

This script is also what makes *resuming* possible instead of restarting. A recovery
prompt built from `missing` says "produce exactly these" — which is cheaper, and keeps
the review-scope baseline meaningful: an agent that rewrites an already-good case
changes its hash and forces a full re-review on the next iteration.

Usage:
    python3 scripts/check_artifacts.py --journey J01-slug --stage design
    python3 scripts/check_artifacts.py --journey J01-slug --stage review --iteration 2
    python3 scripts/check_artifacts.py --journey J01-slug --stage tests
    python3 scripts/check_artifacts.py --journey J01-slug --stage all --json out.json

Exit codes: 0 = complete, 1 = something is missing, 2 = cannot tell (no plan / no index).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

CASE_ID = re.compile(r"TC-J\d{2}-\d{2}")
JID = re.compile(r"^(J\d{2})-")


def variants_from_plan(plan: Path) -> tuple[list[str], str | None]:
    """Case ids the plan's «Варианты» table declares, or a reason we cannot tell."""
    if not plan.is_file():
        return [], f"нет плана {plan}"
    text = plan.read_text(encoding="utf-8")
    m = re.search(r"^##\s+Варианты\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return [], "в плане нет раздела «Варианты»"
    ids: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|")[0].strip()
        found = CASE_ID.fullmatch(first)
        # A template row left unfilled (TC-J<NN>-01) is not a declared variant.
        if found and not found.group(0).endswith("-00"):
            ids.append(found.group(0))
    return ids, None


def check_design(jid_full: str) -> tuple[list[str], list[str], str | None]:
    plan = Path(f"output/suites/{jid_full}.md")
    variants, problem = variants_from_plan(plan)
    if problem:
        return [], [], problem
    prefix = jid_full.split("-")[0]
    expected: list[str] = []
    for cid in [f"TC-{prefix}-00"] + variants:
        expected += [f"output/cases/{jid_full}/{cid}.md",
                     f"output/cases/{jid_full}/{cid}.json"]
    return expected, [p for p in expected if not Path(p).is_file()], None


def check_review(jid_full: str, iteration: int | None) -> tuple[list[str], list[str], str | None]:
    expected = [f"output/state/{jid_full}.json"]
    if iteration is not None:
        expected.insert(0, f"output/reviews/{jid_full}-iter{iteration}.md")
    else:
        # No iteration given: any review file counts, but there must be one.
        if not list(Path("output/reviews").glob(f"{jid_full}-iter*.md")):
            expected.insert(0, f"output/reviews/{jid_full}-iter<N>.md")
    return expected, [p for p in expected if "<N>" in p or not Path(p).is_file()], None


def check_tests(jid_full: str) -> tuple[list[str], list[str], str | None]:
    m = JID.match(jid_full)
    if not m:
        return [], [], f"id journey не начинается с J<NN>: {jid_full}"
    jid = m.group(1).lower()
    names = ["conftest.py", f"data_{jid}.py", f"api_stub_{jid}.py",
             f"test_{jid}.py", "README.md"]
    expected = [f"output/tests/{jid_full}/{n}" for n in names]
    return expected, [p for p in expected if not Path(p).is_file()], None


def resolve_journey(value: str) -> tuple[str | None, str | None]:
    """Accept a full id or a J<NN> prefix, the same way --journey does elsewhere."""
    index = Path("output/suites/_index.json")
    if not index.is_file():
        return (value, None) if "-" in value else (None, f"нет {index}, а «{value}» — не полный id")
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"{index} не парсится: {exc}"
    ids = [str(j.get("id", "")) for j in data.get("journeys", []) if j.get("id")]
    hits = [i for i in ids if i.lower().startswith(value.lower())]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"под «{value}» не подходит ни один journey из {ids}"
    return None, f"под «{value}» подходит несколько journey: {hits}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Что стадия должна была произвести и чего нет")
    ap.add_argument("--journey", required=True, help="полный id или префикс J<NN>")
    ap.add_argument("--stage", required=True,
                    choices=["design", "review", "tests", "all"])
    ap.add_argument("--iteration", type=int, help="номер итерации для стадии review")
    ap.add_argument("--json", dest="json_out", help="куда записать решение")
    args = ap.parse_args()

    jid_full, problem = resolve_journey(args.journey)
    if problem:
        sys.stderr.write(f"check_artifacts: {problem}\n")
        return 2

    stages = ["design", "review", "tests"] if args.stage == "all" else [args.stage]
    report: dict[str, dict] = {}
    blocked: list[str] = []

    for stage in stages:
        if stage == "design":
            expected, missing, why = check_design(jid_full)
        elif stage == "review":
            expected, missing, why = check_review(jid_full, args.iteration)
        else:
            expected, missing, why = check_tests(jid_full)
        if why:
            blocked.append(f"{stage}: {why}")
            continue
        report[stage] = {"expected": expected, "missing": missing,
                         "complete": not missing}

    if blocked and not report:
        for b in blocked:
            sys.stderr.write(f"check_artifacts: {b}\n")
        return 2

    decision = {"journeyId": jid_full, "stages": report, "undetermined": blocked}
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")

    incomplete = False
    for stage, r in report.items():
        if r["complete"]:
            print(f"[artifacts] {stage}: полно — {len(r['expected'])} файл(ов)")
        else:
            incomplete = True
            print(f"[artifacts] {stage}: НЕ ХВАТАЕТ {len(r['missing'])} из {len(r['expected'])}")
            for p in r["missing"]:
                print(f"[artifacts]   нет {p}")
    for b in blocked:
        print(f"[artifacts] не определено — {b}")

    if incomplete:
        print("[artifacts] стадия незавершена: доручите агенту ровно недостающее, "
              "не переписывая готовое — переписанный кейс меняет хеш и откроет полное ревью")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
