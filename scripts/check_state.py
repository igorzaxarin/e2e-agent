#!/usr/bin/env python3
"""Schema gate for the JSON that sub-agents write.

`output/state/<JOURNEY_ID>.json` is the loop's exit condition: the orchestrator reads
`blockers` and decides PASS / iterate / NEEDS_HUMAN. Nothing checked that file before —
`run.md` catches "no file", not "file present, wrong shape".

That gap is silent and expensive. A critic writing `"blockers": "0"` instead of `0`, or
omitting the field, or wrapping the object one level deeper, still produces valid JSON;
the orchestrator's `blockers == 0` then yields False, the journey stays active and burns
every remaining iteration before being filed as NEEDS_HUMAN — a journey that had actually
passed. Three wasted design+review cycles, no error anywhere.

Usage:
    python3 scripts/check_state.py                        # all journey state files
    python3 scripts/check_state.py --state output/state/J01-x.json
    python3 scripts/check_state.py --index output/suites/_index.json

Exit codes: 0 = every file valid, 1 = at least one invalid, 2 = nothing to check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

VALID_VERDICTS = {"PASS", "FIX_REQUIRED"}

# field -> (python type, required)
STATE_FIELDS: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "journeyId": (str, True),
    "iteration": (int, True),
    "blockers": (int, True),
    "majors": (int, True),
    "minors": (int, True),
    "verdict": (str, True),
    "review": (str, True),
    "cases": (int, True),
    # Сколько кейсов критик реально открыл. Необязательное: пишется, когда ревью
    # оборвалось раньше полного обхода — блокер в основном кейсе останавливает
    # семантические проходы (см. test-critic.md, «Order of review»). Отсутствие поля
    # значит «прошёл все `cases`». Смысл разделения в том, что «findings нет, потому
    # что кейс чист» и «findings нет, потому что в кейс не заглядывали» — разные
    # исходы, и отличать их должен оркестратор, а не читатель отчёта.
    "casesReviewed": (int, False),
    "uncoveredReqs": (list, True),
    "openQuestions": (list, True),
}

# The aggregate the orchestrator itself writes. It gets the same treatment as the
# critics' files: the orchestrator is one more JSON author, no more trustworthy.
AGGREGATE_FIELDS: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "maxIterations": (int, True),
    "parallel": (int, True),
    "unit": (str, True),
    "journeys": (dict, True),
    "verdict": (str, True),
}

INDEX_FIELDS: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "requirementsSource": (list, True),
    "journeys": (list, True),
    "uncoveredReqs": (list, True),
    "gaps": (list, True),
    "questions": (list, True),
}


def typename(t) -> str:
    return t.__name__ if isinstance(t, type) else "/".join(x.__name__ for x in t)


def check_fields(data: dict, spec: dict, problems: list[str]) -> None:
    for field, (expected, required) in spec.items():
        if field not in data:
            if required:
                problems.append(f"нет обязательного поля «{field}»")
            continue
        value = data[field]
        # bool is a subclass of int — «"blockers": true» must not pass as a count.
        if expected is int and isinstance(value, bool):
            problems.append(f"поле «{field}» булево, ожидается целое число")
            continue
        if not isinstance(value, expected):
            problems.append(
                f"поле «{field}» имеет тип {type(value).__name__}, "
                f"ожидается {typename(expected)} (значение: {value!r})"
            )


def check_state_file(path: Path, expect_iteration: int | None = None) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"не парсится как JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"корень должен быть объектом, а не {type(data).__name__}"]

    check_fields(data, STATE_FIELDS, problems)

    verdict = data.get("verdict")
    blockers = data.get("blockers")
    if isinstance(verdict, str) and verdict not in VALID_VERDICTS:
        problems.append(
            f"неизвестный вердикт {verdict!r}, ожидается один из {sorted(VALID_VERDICTS)}"
        )
    for field in ("blockers", "majors", "minors", "cases", "casesReviewed", "iteration"):
        value = data.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            problems.append(f"поле «{field}» отрицательное: {value}")

    # The verdict and the count must agree — the orchestrator trusts the count.
    if isinstance(blockers, int) and not isinstance(blockers, bool) and isinstance(verdict, str):
        if blockers == 0 and verdict == "FIX_REQUIRED":
            problems.append("blockers = 0, но вердикт FIX_REQUIRED — противоречие")
        if blockers > 0 and verdict == "PASS":
            problems.append(f"blockers = {blockers}, но вердикт PASS — противоречие")

    # A partial review may never leave as PASS. Stopping at the main case is legitimate
    # (a blocker there is about to move the preconditions every variant inherits), but
    # it means the variants were not judged — and «не ревьюировано» must not be able to
    # reach the report as «прошло».
    cases = data.get("cases")
    reviewed = data.get("casesReviewed")
    if isinstance(reviewed, int) and not isinstance(reviewed, bool)             and isinstance(cases, int) and not isinstance(cases, bool):
        if reviewed > cases:
            problems.append(
                f"casesReviewed = {reviewed} больше, чем cases = {cases} — "
                f"отревьюировано больше кейсов, чем есть"
            )
        elif reviewed < cases and verdict == "PASS":
            problems.append(
                f"вердикт PASS, но отревьюировано {reviewed} из {cases} кейсов — "
                f"непросмотренный кейс не является пройденным"
            )

    # The orchestrator owns the iteration counter: it passes N in the prompt and in the
    # review path, the critic echoes it back. Two counters that are allowed to disagree
    # are one counter nobody owns, so the disagreement is an error, not a detail.
    iteration = data.get("iteration")
    if expect_iteration is not None and isinstance(iteration, int) \
            and not isinstance(iteration, bool) and iteration != expect_iteration:
        problems.append(
            f"iteration = {iteration}, а оркестратор вёл итерацию {expect_iteration} — "
            f"счётчик разошёлся, вердикту этого файла верить нельзя"
        )

    review = data.get("review")
    if isinstance(review, str) and review:
        # The path is written relative to the project root. Resolve it from the state
        # file's own location (…/output/state/X.json -> project root) instead of the
        # current directory, so the check does not depend on where the script is run.
        candidates = [Path(review)]
        root = path.resolve().parent.parent.parent
        candidates.append(root / review)
        if not any(c.is_file() for c in candidates):
            problems.append(f"файл ревью не найден: {review}")

    return problems


def check_aggregate_file(path: Path, state_dir: Path) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"не парсится как JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"корень должен быть объектом, а не {type(data).__name__}"]
    check_fields(data, AGGREGATE_FIELDS, problems)
    unit = data.get("unit")
    if isinstance(unit, str) and unit not in ("journey", "area"):
        problems.append(f"unit = {unit!r}, ожидается journey или area")
    # The aggregate is a snapshot of the per-journey files; a journey missing from it
    # is exactly the kind of quiet drop the snapshot exists to prevent.
    journeys = data.get("journeys")
    if isinstance(journeys, dict) and state_dir.is_dir():
        on_disk = {p.stem for p in state_dir.glob("*.json")}
        for jid in sorted(on_disk - set(journeys)):
            problems.append(f"journey {jid} есть в {state_dir}/, но отсутствует в агрегате")
    return problems


def check_index_file(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"не парсится как JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"корень должен быть объектом, а не {type(data).__name__}"]

    check_fields(data, INDEX_FIELDS, problems)

    seen: set[str] = set()
    for i, j in enumerate(data.get("journeys") or [], start=1):
        if not isinstance(j, dict):
            problems.append(f"journeys[{i}] не объект")
            continue
        jid = j.get("id")
        if not isinstance(jid, str) or not jid:
            problems.append(f"journeys[{i}]: нет поля «id»")
            continue
        # A repeated J<NN> collides in case ids, allure ids and module suffixes.
        prefix = jid.split("-")[0]
        if prefix in seen:
            problems.append(f"номер journey «{prefix}» выдан дважды — конфликт идентификаторов")
        seen.add(prefix)
        plan = j.get("plan")
        if not isinstance(plan, str) or not plan:
            problems.append(f"{jid}: нет поля «plan»")
        elif not Path(plan).is_file():
            problems.append(f"{jid}: план не найден по пути {plan}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверить JSON, который пишут под-агенты")
    ap.add_argument("--state", help="конкретный файл состояния вместо всех")
    ap.add_argument("--state-dir", default="output/state", help="каталог файлов состояния")
    ap.add_argument("--expect-iteration", type=int, metavar="N",
                    help="номер итерации, который оркестратор передал критику "
                         "(проверяется только вместе с --state)")
    ap.add_argument("--aggregate", default="output/state.json",
                    help="агрегат оркестратора; проверяется, если существует")
    ap.add_argument("--index", default="output/suites/_index.json", help="машинный индекс аналитика")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    targets: list[tuple[Path, str]] = []
    if args.state:
        targets.append((Path(args.state), "state"))
    else:
        state_dir = Path(args.state_dir)
        if state_dir.is_dir():
            targets.extend((p, "state") for p in sorted(state_dir.glob("*.json")))
    index_path = Path(args.index)
    if index_path.is_file():
        targets.append((index_path, "index"))
    aggregate = Path(args.aggregate)
    if aggregate.is_file():
        targets.append((aggregate, "aggregate"))

    if not targets:
        sys.stderr.write("check_state: нечего проверять — нет ни файлов состояния, ни индекса\n")
        return 2

    failed = 0
    for path, kind in targets:
        if kind == "aggregate":
            problems = check_aggregate_file(path, Path(args.state_dir))
        elif kind == "state":
            # --expect-iteration applies to the one file named by --state; sweeping the
            # whole directory means journeys at different iterations, so it is ignored there.
            expect = args.expect_iteration if args.state else None
            problems = check_state_file(path, expect)
        else:
            problems = check_index_file(path)
        if problems:
            failed += 1
            sys.stderr.write(f"[state] НЕВАЛИДЕН {path}\n")
            for p in problems:
                sys.stderr.write(f"[state]   - {p}\n")
        elif not args.quiet:
            print(f"[state] ok {path}")

    if failed:
        sys.stderr.write(
            f"[state] невалидных файлов: {failed} из {len(targets)} — "
            "journey с таким файлом должен быть помечен FAILED, "
            "а не получать вердикт по битому полю\n"
        )
        return 1

    print(f"[state] проверено файлов: {len(targets)}, все валидны")
    return 0


if __name__ == "__main__":
    sys.exit(main())
