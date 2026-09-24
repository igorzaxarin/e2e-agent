#!/usr/bin/env python3
"""Parse and validate the argument line of `/e2e:run`, then say what will stop the run.

The orchestrator runs this once, as the first action of a run, and every gate afterwards
reads `output/.run-args.json` instead of re-deriving the argument line from prose.

Two jobs:

1. **Strict accounting.** Every known flag is listed here with its kind and allowed
   values. An unknown flag, a missing value, a bad value, or a combination that makes a
   passed flag a silent no-op is an error — exit 2, nothing written.
2. **The question registry.** The nine gates that may stop a run are enumerated below
   with what resolves each one. The script prints which gates are pre-answered by the
   given arguments and which will ask. Anything not in that table may not be asked.

Usage:
    python3 scripts/parse_run_args.py --args "{{args}}"
    python3 scripts/parse_run_args.py --args "input/requirements/*.md --yes --no-ask"

Exit codes:
    0 = arguments valid, `output/.run-args.json` written
    2 = invalid arguments, nothing written, the run must not start
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PLACEHOLDER = re.compile(r"\{\{.*?\}\}")
OUT = Path("output/.run-args.json")

# kind: "bool" = presence only; "str" / "int" take the next token as their value
FLAGS: dict[str, tuple[str, str]] = {
    "--max": ("int", "maxIterations"),
    "--journey": ("str", "journey"),
    "--parallel": ("int", "parallel"),
    "--unit": ("str", "unit"),
    "--yes": ("bool", "yes"),
    "--no-ask": ("bool", "noAsk"),
    "--keep": ("bool", "keep"),
    "--generate-pytest": ("bool", "generatePytest"),
    "--selenium": ("bool", "selenium"),
    "--writers": ("str", "writers"),
    "--selenium-url": ("str", "seleniumUrl"),
    "--base-url": ("str", "baseUrl"),
    "--app-source": ("str", "appSource"),
    "--harvest-ui": ("bool", "harvestUi"),
    "--publish-allure": ("bool", "publishAllure"),
    "--allure-project-id": ("int", "allureProjectId"),
    "--allure-project-name": ("str", "allureProjectName"),
}

ENUMS = {"unit": ("journey", "area"), "writers": ("sequential", "parallel")}

DEFAULTS: dict[str, object] = {
    "source": "input/requirements/*.md",
    "maxIterations": 2,
    "journey": None,
    "parallel": None,
    "unit": None,
    "yes": False,
    "noAsk": False,
    "keep": False,
    "generatePytest": False,
    "selenium": False,
    "writers": None,
    "seleniumUrl": "http://localhost:4444",
    "baseUrl": None,
    "appSource": None,
    "harvestUi": False,
    "publishAllure": False,
    "allureProjectId": None,
    "allureProjectName": None,
}


def tokenize(raw: str) -> list[str]:
    try:
        return [t.strip('"').strip("'") for t in shlex.split(raw, posix=False)]
    except ValueError:
        return raw.split()


def parse(raw: str) -> tuple[dict, list[str], list[str]]:
    cfg = dict(DEFAULTS)
    errors: list[str] = []
    warnings: list[str] = []
    positional: list[str] = []

    tokens = tokenize(raw)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if tok.startswith("--"):
            name, _, inline = tok.partition("=")
            if name not in FLAGS:
                errors.append("неизвестный флаг " + name + " — процедура его не определяет")
                i += 1
                continue
            kind, key = FLAGS[name]
            if kind == "bool":
                if inline:
                    errors.append(name + " не принимает значение, получено: " + inline)
                cfg[key] = True
                i += 1
                continue
            value = inline
            if not value:
                if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                    errors.append(name + " требует значение, оно не передано")
                    i += 1
                    continue
                value = tokens[i + 1]
                i += 1
            if kind == "int":
                try:
                    cfg[key] = int(value)
                except ValueError:
                    errors.append(name + " ожидает целое число, получено: " + value)
            else:
                cfg[key] = value
            i += 1
            continue
        positional.append(tok)
        i += 1

    if len(positional) == 1:
        cfg["source"] = positional[0]
    elif len(positional) > 1:
        errors.append(
            "источник требований задаётся одним путём или glob'ом, получено "
            + str(len(positional)) + ": " + ", ".join(positional)
        )

    for key, allowed in ENUMS.items():
        if cfg[key] is not None and cfg[key] not in allowed:
            errors.append("--" + key + ": допустимо " + " | ".join(allowed)
                          + ", получено: " + str(cfg[key]))

    if isinstance(cfg["maxIterations"], int) and cfg["maxIterations"] < 1:
        errors.append("--max должен быть не меньше 1")
    if isinstance(cfg["parallel"], int) and not 1 <= cfg["parallel"] <= 8:
        errors.append("--parallel должен быть в диапазоне 1..8")

    # Combinations under which a passed flag would silently do nothing.
    if cfg["selenium"] and not cfg["generatePytest"] and cfg["noAsk"]:
        errors.append(
            "--selenium без --generate-pytest при --no-ask: Фаза 2.5 не выполнится и "
            "браузерные тесты написаны не будут. Добавьте --generate-pytest или уберите --selenium"
        )
    if cfg["publishAllure"] and not cfg["generatePytest"] and cfg["noAsk"]:
        errors.append(
            "--publish-allure без --generate-pytest при --no-ask: публиковать будет нечего. "
            "Добавьте --generate-pytest или уберите --publish-allure"
        )

    if cfg["selenium"] and not cfg["baseUrl"]:
        warnings.append(
            "--selenium без --base-url: адрес стенда должен быть зафиксирован в источнике "
            "требований, иначе браузерный писатель не запускается (Фаза 5)"
        )
    for flag, key, need, need_flag in (
        ("--writers", "writers", "generatePytest", "--generate-pytest"),
        ("--selenium-url", "seleniumUrl", "selenium", "--selenium"),
        ("--app-source", "appSource", "selenium", "--selenium"),
        ("--base-url", "baseUrl", "selenium", "--selenium"),
    ):
        if cfg[key] != DEFAULTS[key] and not cfg[need]:
            warnings.append(flag + " передан без " + need_flag + " — будет проигнорирован")
    if cfg["harvestUi"] and not cfg["selenium"]:
        warnings.append("--harvest-ui передан без --selenium — будет проигнорирован")
    if (cfg["allureProjectId"] or cfg["allureProjectName"]) and not cfg["publishAllure"]:
        warnings.append("--allure-project-* передан без --publish-allure — будет проигнорирован")
    if cfg["parallel"] and cfg["journey"]:
        warnings.append("--parallel вместе с --journey: работа одна, параллельность не применяется")

    return cfg, errors, warnings


def gate(qid: str, phase: str, header: str, resolved: str,
         value: object = None, note: str = "") -> dict:
    return {"id": qid, "phase": phase, "header": header, "resolvedBy": resolved,
            "value": value, "note": note, "willAsk": resolved == "ASK"}


def registry(cfg: dict) -> list[dict]:
    """The closed list of questions a run may ask. Nothing outside it may be asked."""
    no_ask = bool(cfg["noAsk"])
    single = bool(cfg["journey"])
    fallback = "--no-ask" if no_ask else "ASK"

    # Q1 — coverage. No flag closes it; only --no-ask, which answers "continue".
    q1 = gate("Q1", "1.5", "Покрытие", fallback, "continue" if no_ask else None,
              "флага нет; закрывается только --no-ask")

    # Q2 / Q3 — parallelism and ownership. A single journey suppresses both.
    if cfg["parallel"]:
        q2 = gate("Q2", "1.5", "Потоки", "--parallel", cfg["parallel"])
    elif single:
        q2 = gate("Q2", "1.5", "Потоки", "подавлен (--journey)", 1)
    else:
        q2 = gate("Q2", "1.5", "Потоки", fallback,
                  "defaults payload" if no_ask else None)

    if cfg["unit"]:
        q3 = gate("Q3", "1.5", "Владение", "--unit", cfg["unit"])
    elif single:
        q3 = gate("Q3", "1.5", "Владение", "подавлен (--journey)", "journey")
    else:
        q3 = gate("Q3", "1.5", "Владение", fallback,
                  "defaults payload" if no_ask else None)

    # Q4 — go/no-go.
    if cfg["yes"]:
        q4 = gate("Q4", "1.5", "GoNoGo", "--yes", "go")
    else:
        q4 = gate("Q4", "1.5", "GoNoGo", fallback, "go" if no_ask else None)

    # Q5 — whether to generate pytest at all.
    if cfg["generatePytest"]:
        q5 = gate("Q5", "2.5", "Pytest", "--generate-pytest", "да")
    else:
        q5 = gate("Q5", "2.5", "Pytest", fallback, "нет" if no_ask else None)

    # Q6 — sequential or parallel writers. Exists only if Phase 2.5 runs.
    phase25_runs = cfg["generatePytest"] or not no_ask
    if not phase25_runs:
        q6 = gate("Q6", "2.5", "Писатели", "пропущен (Фаза 2.5 не выполняется)")
    elif cfg["writers"]:
        q6 = gate("Q6", "2.5", "Писатели", "--writers", cfg["writers"])
    else:
        q6 = gate("Q6", "2.5", "Писатели", fallback,
                  "sequential" if no_ask else None,
                  "при одном PASS-journey вопрос подавляется")

    # Q7 — source of the UI inventory. Exists only under --selenium.
    if not cfg["selenium"]:
        q7 = gate("Q7", "5.1", "Источник UI", "пропущен (нет --selenium)")
    elif cfg["appSource"]:
        q7 = gate("Q7", "5.1", "Источник UI", "--app-source", cfg["appSource"])
    elif cfg["harvestUi"]:
        q7 = gate("Q7", "5.1", "Источник UI", "--harvest-ui", "живой стенд")
    else:
        q7 = gate("Q7", "5.1", "Источник UI", fallback,
                  "источника нет" if no_ask else None)

    # Q8 — Allure project. Exists only under --publish-allure.
    env_pid = os.environ.get("ALLURE_PROJECT_ID")
    if not cfg["publishAllure"]:
        q8 = gate("Q8", "2.6", "AllurePID", "пропущен (нет --publish-allure)")
    elif cfg["allureProjectId"]:
        q8 = gate("Q8", "2.6", "AllurePID", "--allure-project-id", cfg["allureProjectId"])
    elif cfg["allureProjectName"]:
        q8 = gate("Q8", "2.6", "AllurePID", "--allure-project-name", cfg["allureProjectName"])
    elif env_pid:
        q8 = gate("Q8", "2.6", "AllurePID", "ALLURE_PROJECT_ID", env_pid)
    else:
        q8 = gate("Q8", "2.6", "AllurePID", fallback,
                  "публикация пропускается" if no_ask else None)

    # Q9 — what to do with the run's leftovers. No flag closes it; only --no-ask.
    q9 = gate("Q9", "3", "Итог", fallback,
              "записать отчёт как есть" if no_ask else None,
              "флага нет; закрывается только --no-ask")

    return [q1, q2, q3, q4, q5, q6, q7, q8, q9]


def main() -> int:
    ap = argparse.ArgumentParser(description="Разобрать и проверить аргументы /e2e:run")
    ap.add_argument("--args", dest="raw", default="", help="строка аргументов прогона")
    ap.add_argument("--json", dest="out", default=str(OUT), help="куда писать разбор")
    a = ap.parse_args()

    raw = a.raw or ""
    unavailable = bool(PLACEHOLDER.search(raw))
    if unavailable:
        sys.stderr.write(
            "parse_run_args: аргументы не подставлены средой — {{...}} дошёл дословно. "
            "Разбираю как пустую строку: ни один шлюз не считается предрешённым.\n"
        )
        raw = ""

    cfg, errors, warnings = parse(raw)

    if errors:
        print("## Аргументы отвергнуты — прогон не начинайте\n")
        for e in errors:
            print("- ⛔ " + e)
        print("\nПокажите этот список человеку, попросите исправленную строку аргументов "
              "и ничего больше в этом сообщении не делайте. Файл разбора не записан, "
              "Фаза 1 не начинается.")
        return 2

    gates = registry(cfg)
    asking = [g for g in gates if g["willAsk"]]
    cfg["argsUnavailable"] = unavailable
    cfg["raw"] = a.raw
    cfg["gates"] = {g["id"]: g for g in gates}
    cfg["stops"] = len(asking)
    cfg["warnings"] = warnings

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    head = ("Источник требований: `" + str(cfg["source"]) + "`   "
            "MAX_ITERATIONS: " + str(cfg["maxIterations"]))
    if cfg["journey"]:
        head += "   journey: `" + str(cfg["journey"]) + "`"
    print(head)
    print("Разбор записан: `" + out.as_posix() + "`\n")

    print("| Вопрос | Фаза | header | Чем закрыт | Значение |")
    print("|---|---|---|---|---|")
    for g in gates:
        mark = "❓ **СПРОСИТЬ**" if g["willAsk"] else "`" + str(g["resolvedBy"]) + "`"
        val = "—" if g["value"] in (None, "") else "`" + str(g["value"]) + "`"
        print("| " + g["id"] + " | " + g["phase"] + " | " + g["header"] + " | "
              + mark + " | " + val + " |")

    for w in warnings:
        print("\n- ⚠️ " + w)

    print("\n**Остановок за прогон: " + str(len(asking)) + ".**")
    if asking:
        print("Спросить придётся: "
              + ", ".join(g["id"] + " (" + g["header"] + ")" for g in asking) + ".")
        print("Прогон без единой остановки даёт `--no-ask`: вопросы уходят в "
              "`output/pending.md` со значениями из колонки «Значение».")
    else:
        print("Ни один шлюз не спрашивает — полный сценарий выполняется без вмешательства человека.")
    print("\nВопросы вне этой таблицы задавать запрещено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
