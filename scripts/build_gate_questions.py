#!/usr/bin/env python3
"""Deterministic builder for the requirements gate (Фаза 1.5) question payload.

Reads the analyst's machine index and emits the exact payload the orchestrator
passes to its interactive question tool (`ask_user_question` / `AskUserQuestion`).
The orchestrator does not compose the questions itself — it reads this file and
passes `gateQuestions` through verbatim. That removes the run-to-run variance in
headers, labels and option counts that made the gate unstable.

Usage:
    python3 scripts/build_gate_questions.py
    python3 scripts/build_gate_questions.py --parallel 3 --unit journey
    python3 scripts/build_gate_questions.py --index output/suites/_index.json \
        --out output/gate/questions.json

Exit code 0 = payload written, 2 = index missing or unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_STAGES = 5
MIN_AREAS = 3
MAX_OPTIONS = 4  # hard limit of the interactive question tool
RECOMMENDED_PARALLEL_CAP = 3

# The orchestrator reads this stdout; a cp1251 console would mangle the Cyrillic.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian pluralisation — the gate question is read by a human."""
    if n % 10 == 1 and n % 100 != 11:
        form = one
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        form = few
    else:
        form = many
    return f"{n} {form}"


def _as_list(value: Any) -> list:
    """Tolerate a missing key, a null, or a single object where a list is specified."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(item: Any, *keys: str) -> str:
    """Pull the first present key off a dict, or stringify a bare scalar."""
    if not isinstance(item, dict):
        return str(item)
    for key in keys:
        if item.get(key):
            return str(item[key])
    return ""


def load_index(path: Path) -> dict:
    if not path.is_file():
        sys.stderr.write(
            f"[gate] индекс не найден: {path}\n"
            "[gate] сначала должен отработать requirements-analyst\n"
        )
        raise SystemExit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[gate] индекс не парсится как JSON: {path}: {exc}\n")
        raise SystemExit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"[gate] индекс должен быть объектом, а не {type(data).__name__}\n")
        raise SystemExit(2)
    return data


def collect_lists(index: dict) -> dict:
    """Flatten the five lists the gate must print in full."""
    journeys = _as_list(index.get("journeys"))

    uncovered = [
        {
            "id": _text(item, "id") or "REQ-??",
            "reason": _text(item, "reason", "why") or "причина не указана аналитиком",
        }
        for item in _as_list(index.get("uncoveredReqs"))
    ]

    gaps = [
        {
            "id": _text(item, "id") or f"G-{i:02d}",
            "journey": _text(item, "journey"),
            "what": _text(item, "what", "description", "text"),
            "quote": _text(item, "quote"),
        }
        for i, item in enumerate(_as_list(index.get("gaps")), start=1)
    ]

    questions = []
    for i, item in enumerate(_as_list(index.get("questions")), start=1):
        blocks = _as_list(item.get("blocks") if isinstance(item, dict) else None)
        questions.append(
            {
                "id": _text(item, "id") or f"Q-{i:02d}",
                "journey": _text(item, "journey"),
                "question": _text(item, "question", "text"),
                "blocks": [str(b) for b in blocks],
                "severity": (_text(item, "severity") or "blocking").lower(),
            }
        )
    # Most-blocking first; the gate must show them in that order.
    questions.sort(key=lambda q: (-len(q["blocks"]), q["id"]))
    blocking = [q for q in questions if q["severity"] == "blocking"]
    advisory = [q for q in questions if q["severity"] != "blocking"]

    stages_without_req = [
        {"journey": _text(j, "id"), "count": int(j.get("stagesWithoutReq") or 0)}
        for j in journeys
        if isinstance(j, dict) and int(j.get("stagesWithoutReq") or 0) > 0
    ]

    structural = []
    for j in journeys:
        if not isinstance(j, dict):
            continue
        reasons = []
        stages = j.get("stages")
        areas = _as_list(j.get("areas"))
        if isinstance(stages, int) and stages < MIN_STAGES:
            reasons.append(f"этапов {stages} < {MIN_STAGES}")
        if areas and len(areas) < MIN_AREAS:
            reasons.append(f"функциональных областей {len(areas)} < {MIN_AREAS}")
        for warning in _as_list(j.get("warnings")):
            reasons.append(str(warning))
        if reasons:
            structural.append({"journey": _text(j, "id"), "reasons": reasons})

    return {
        "journeys": journeys,
        "uncoveredReqs": uncovered,
        "gaps": gaps,
        "blockingQuestions": blocking,
        "advisoryQuestions": advisory,
        "stagesWithoutReq": stages_without_req,
        "structuralSuspicions": structural,
    }


def build_coverage_question(lists: dict) -> dict:
    n_uncovered = len(lists["uncoveredReqs"])
    n_gaps = len(lists["gaps"])
    n_blocking = len(lists["blockingQuestions"])
    return {
        "id": "B1",
        "header": "Покрытие",
        "question": (
            f"{plural(n_uncovered, 'требование', 'требования', 'требований')} не покрыто, "
            f"{plural(n_gaps, 'пробел', 'пробела', 'пробелов')}, "
            f"{plural(n_blocking, 'блокирующий вопрос', 'блокирующих вопроса', 'блокирующих вопросов')}"
            ". Как поступить?"
        ),
        "multiSelect": False,
        "options": [
            {
                "label": "Продолжить, зафиксировать пробелы",
                "description": (
                    "Пробелы уходят в кейсы как открытые вопросы, "
                    "Фаза 2 стартует сейчас."
                ),
                "value": "continue",
            },
            {
                "label": "Отвечу на вопросы сейчас",
                "description": (
                    f"Задать мне "
                    f"{plural(n_blocking, 'блокирующий вопрос', 'блокирующих вопроса', 'блокирующих вопросов')}"
                    ", записать ответы в input/requirements/_answers.md "
                    "и перезапустить аналитика."
                ),
                "value": "answer-now",
            },
            {
                "label": "Остановиться, поправлю требования",
                "description": (
                    "Прогон завершается, недостающее пишется в output/report.md."
                ),
                "value": "stop",
            },
        ],
    }


def build_parallel_question(n_journeys: int) -> dict:
    recommended = min(RECOMMENDED_PARALLEL_CAP, n_journeys)
    # Recommended first, then the rest, deduplicated, capped at the tool limit.
    candidates: list[int] = []
    for value in (recommended, 1, 2, n_journeys):
        if 1 <= value <= n_journeys and value not in candidates:
            candidates.append(value)
    candidates = candidates[:MAX_OPTIONS]

    def label(value: int) -> str:
        # No parentheses in the base — the recommendation suffix supplies the only pair.
        if value == 1:
            base = "1 последовательно"
        elif value == n_journeys and n_journeys > 2:
            base = f"{value} по одному на journey"
        else:
            base = f"{value} параллельно"
        return f"{base} (рекомендуется)" if value == recommended else base

    return {
        "id": "B2",
        "header": "Циклы",
        "question": (
            f"Сколько параллельных циклов designer→critic запускать "
            f"на {n_journeys} journey?"
        ),
        "multiSelect": False,
        "options": [
            {
                "label": label(value),
                "description": (
                    f"{value} journey обрабатываются одновременно, "
                    f"остальные ждут в очереди."
                ),
                "value": value,
            }
            for value in candidates
        ],
    }


def build_unit_question() -> dict:
    return {
        "id": "B3",
        "header": "Владение",
        "question": "Что владеет одним циклом?",
        "multiSelect": False,
        "options": [
            {
                "label": "Один journey на цикл (рекомендуется)",
                "description": "Цикл пишет только в output/cases/<J>/ своего journey.",
                "value": "journey",
            },
            {
                "label": "Одна область на цикл",
                "description": (
                    "Цикл ведёт все journey одной функциональной области, "
                    "последовательно."
                ),
                "value": "area",
            },
        ],
    }


def build_blocking_prompts(blocking: list[dict]) -> list[dict]:
    """Stubs for Q-01…Q-NN — the orchestrator fills `options` itself.

    Option text is a claim about product behaviour, which no script can derive
    from the index. The orchestrator supplies 2-4 plausible answers per question
    and marks any option the requirements do not confirm as an assumption.
    """
    prompts = []
    for i, q in enumerate(blocking, start=1):
        blocks = ", ".join(q["blocks"]) if q["blocks"] else "не указано"
        prompts.append(
            {
                "id": q["id"],
                "journey": q["journey"],
                "header": q["id"][:12],
                "question": q["question"],
                "blocks": q["blocks"],
                "blocksSummary": blocks,
                "multiSelect": False,
                "options": [],
                "optionsPolicy": (
                    "Заполните 2–4 варианта ответа продукта. Вариант, который "
                    "требования не подтверждают, пометьте в description словом "
                    "«предположение». Вариант «Другое» не добавляйте — среда "
                    "добавит его сама и он даст свободный текст."
                ),
                "order": i,
            }
        )
    return prompts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Построить payload вопросов шлюза требований из _index.json"
    )
    parser.add_argument("--index", default="output/suites/_index.json")
    parser.add_argument("--out", default="output/gate/questions.json")
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="значение флага --parallel прогона; подавляет вопрос В2",
    )
    parser.add_argument(
        "--unit",
        choices=("journey", "area"),
        default=None,
        help="значение флага --unit прогона; подавляет вопрос В3",
    )
    args = parser.parse_args()

    index = load_index(Path(args.index))
    lists = collect_lists(index)
    n_journeys = len(lists["journeys"])

    gate_questions = [build_coverage_question(lists)]
    skipped = []

    if args.parallel is not None:
        skipped.append({"id": "B2", "reason": f"передан --parallel {args.parallel}"})
    elif n_journeys <= 1:
        skipped.append({"id": "B2", "reason": "journey ровно один"})
    else:
        gate_questions.append(build_parallel_question(n_journeys))

    if args.unit is not None:
        skipped.append({"id": "B3", "reason": f"передан --unit {args.unit}"})
    elif n_journeys <= 1:
        skipped.append({"id": "B3", "reason": "journey ровно один"})
    else:
        gate_questions.append(build_unit_question())

    payload = {
        "generatedFrom": args.index,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "journeys": n_journeys,
            "uncoveredReqs": len(lists["uncoveredReqs"]),
            "gaps": len(lists["gaps"]),
            "blockingQuestions": len(lists["blockingQuestions"]),
            "advisoryQuestions": len(lists["advisoryQuestions"]),
            "stagesWithoutReq": sum(s["count"] for s in lists["stagesWithoutReq"]),
            "structuralSuspicions": len(lists["structuralSuspicions"]),
        },
        "lists": {
            key: lists[key]
            for key in (
                "uncoveredReqs",
                "gaps",
                "blockingQuestions",
                "advisoryQuestions",
                "stagesWithoutReq",
                "structuralSuspicions",
            )
        },
        "gateQuestions": gate_questions,
        "skippedQuestions": skipped,
        "blockingQuestionPrompts": build_blocking_prompts(lists["blockingQuestions"]),
        "defaults": {
            "parallel": args.parallel
            if args.parallel is not None
            else min(RECOMMENDED_PARALLEL_CAP, max(n_journeys, 1)),
            "unit": args.unit or "journey",
        },
        "answersPath": "output/gate/answers.json",
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    s = payload["summary"]
    print(f"[gate] payload записан: {out_path}")
    print(
        f"[gate] journey {s['journeys']}, непокрыто {s['uncoveredReqs']}, "
        f"пробелов {s['gaps']}, блокирующих вопросов {s['blockingQuestions']}"
    )
    print(f"[gate] вопросов шлюза к показу: {len(gate_questions)}")
    for item in skipped:
        print(f"[gate] пропущен {item['id']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
