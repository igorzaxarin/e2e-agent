#!/usr/bin/env python3
"""Deterministic guard for the requirements gate (Фаза 1.5).

The orchestrator must run this before its first `qa-designer` dispatch. It fails
unless the human's answers exist and cover every question that was actually
shown. That turns "the model forgot to ask" from a silent judgment call into a
mechanical failure the orchestrator has to see and act on.

Usage:
    python3 scripts/gate_check.py
    python3 scripts/gate_check.py --questions output/gate/questions.json \
        --answers output/gate/answers.json

Exit codes:
    0  шлюз пройден — можно запускать Фазу 2
    1  шлюз НЕ пройден — ответы отсутствуют или неполны, нужно спросить человека
    2  payload вопросов отсутствует — сначала build_gate_questions.py
    3  человек выбрал не «продолжить» — следующее действие напечатано, но это не Фаза 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_COVERAGE = {"continue", "answer-now", "stop"}

# The orchestrator reads nextAction off this stdout; a cp1251 console mangles Cyrillic.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def load_json(path: Path, missing_code: int, label: str) -> dict:
    if not path.is_file():
        sys.stderr.write(f"[gate] {label} не найден: {path}\n")
        raise SystemExit(missing_code)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[gate] {label} не парсится как JSON: {path}: {exc}\n")
        raise SystemExit(missing_code)
    if not isinstance(data, dict):
        sys.stderr.write(f"[gate] {label} должен быть объектом\n")
        raise SystemExit(missing_code)
    return data


def fail(problems: list[str], answers_path: Path) -> int:
    sys.stderr.write("[gate] ШЛЮЗ НЕ ПРОЙДЕН — Фазу 2 запускать нельзя\n")
    for problem in problems:
        sys.stderr.write(f"[gate]   - {problem}\n")
    sys.stderr.write(
        f"[gate] спросите человека интерактивным инструментом и запишите {answers_path}\n"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить, что человек ответил на вопросы шлюза требований"
    )
    parser.add_argument("--questions", default="output/gate/questions.json")
    parser.add_argument("--answers", default="output/gate/answers.json")
    args = parser.parse_args()

    questions_path = Path(args.questions)
    answers_path = Path(args.answers)

    payload = load_json(questions_path, 2, "payload вопросов")
    asked_ids = {
        q.get("id")
        for q in payload.get("gateQuestions", [])
        if isinstance(q, dict) and q.get("id")
    }

    if not answers_path.is_file():
        return fail(
            [f"файл ответов отсутствует: {answers_path}", f"показано вопросов: {len(asked_ids)}"],
            answers_path,
        )

    answers = load_json(answers_path, 1, "файл ответов")
    problems: list[str] = []

    coverage = str(answers.get("coverage") or "").strip()
    if "B1" in asked_ids and coverage not in VALID_COVERAGE:
        problems.append(
            f"нет ответа на В1: поле coverage = {coverage!r}, "
            f"ожидается одно из {sorted(VALID_COVERAGE)}"
        )

    if "B2" in asked_ids:
        parallel = answers.get("parallel")
        if not isinstance(parallel, int) or parallel < 1:
            problems.append(f"нет ответа на В2: поле parallel = {parallel!r}")

    if "B3" in asked_ids:
        unit = answers.get("unit")
        if unit not in ("journey", "area"):
            problems.append(f"нет ответа на В3: поле unit = {unit!r}")

    if problems:
        return fail(problems, answers_path)

    # «Отвечу сейчас» is only satisfied once every blocking question has an answer.
    if coverage == "answer-now":
        expected = {
            p["id"]
            for p in payload.get("blockingQuestionPrompts", [])
            if isinstance(p, dict) and p.get("id")
        }
        given = {
            str(a.get("id")): str(a.get("answer") or "").strip()
            for a in answers.get("answers", [])
            if isinstance(a, dict) and a.get("id")
        }
        unanswered = sorted(q for q in expected if not given.get(q))
        if unanswered:
            return fail(
                [f"выбран «Отвечу на вопросы сейчас», но без ответа: {', '.join(unanswered)}"],
                answers_path,
            )
        print(f"[gate] ответов на блокирующие вопросы: {len(expected)}")
        print("[gate] nextAction: записать input/requirements/_answers.md "
              "и перезапустить requirements-analyst")
        return 3

    if coverage == "stop":
        print("[gate] nextAction: записать output/report.md и завершить прогон")
        return 3

    parallel = answers.get("parallel", payload.get("defaults", {}).get("parallel"))
    unit = answers.get("unit", payload.get("defaults", {}).get("unit"))
    print("[gate] ШЛЮЗ ПРОЙДЕН")
    print(f"[gate] coverage=continue, parallel={parallel}, unit={unit}")
    print("[gate] nextAction: Фаза 2 — параллельные циклы дизайна и ревью")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
