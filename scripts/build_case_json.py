#!/usr/bin/env python3
"""Deterministic Markdown -> JSON converter for e2e test cases.

ВНИМАНИЕ: с прогона 10 первичным форматом стал JSON, и кейсы порождаются в другую
сторону — `scripts/json_to_md.py`. Этот скрипт остался как **путь миграции**: им
поднимают рукописный Markdown, написанный до разворота, в JSON, после чего Markdown
перегенерируют. Так мигрирован эталон в `examples/`. В конвейере он не вызывается,
и дизайнер его не запускает.

Одно ограничение миграции: `steps[].requirements` из Markdown не восстанавливается
(колонки с якорями в таблице шагов нет), поэтому скрипт пишет туда `[]` — якоря
переносят из прежнего JSON отдельно.


qa-designer used to hand-write both `.md` and `.json` for every case: same content,
typed twice, at full sequential-output-token cost each time (docs/format.md has always
required them to be "содержательно идентичны" — the LLM was doing a copy, not writing
twice on purpose). Per optimize.md's cost hierarchy, LLM time is dominated by output
tokens, not input, so this is a straight ~40-50% cut of the designer's printed output:
Markdown stays hand-written (it is the authoritative format, docs/format.md §0), JSON
becomes a derived artefact this script produces from it.

One field cannot round-trip: `steps[].requirements` in the JSON schema tags each step
with its REQ-XX anchors, but the Markdown steps table (docs/format.md §3) has no such
column — only the case-level "Покрываемые требования" field carries anchors. Nothing
in this repo reads the per-step field (checked: no agent spec or script greps for it),
so the generated JSON always emits an empty list there. See docs/format.md §5 for the
documented consequence.

Usage:
    python3 scripts/build_case_json.py output/cases/J01-slug            # (re)write *.json
    python3 scripts/build_case_json.py output/cases/J01-slug/TC-J01-00.md
    python3 scripts/build_case_json.py output/cases/J01-slug --check    # verify only

Exit codes: 0 = ok, 1 = --check found a stale/missing JSON, 2 = nothing to convert.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_cases as vc  # noqa: E402  (needs sys.path fix above)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

BULLET = re.compile(r"^\s*-\s+(.*\S)\s*$")
NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*\S)\s*$")


def parse_bullets(block: str) -> list[str]:
    return [m.group(1).strip() for line in block.splitlines() if (m := BULLET.match(line))]


def parse_numbered(block: str) -> list[str]:
    return [m.group(1).strip() for line in block.splitlines() if (m := NUMBERED.match(line))]


def build_case(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    sections = vc.split_sections(text)
    fields = vc.parse_fields(sections.get("Общая информация", ""))

    variant_raw = fields.get("Вариант от", "—").strip()
    variant_of = None if variant_raw in ("—", "-", "", "None") else variant_raw

    reqs_raw = fields.get("Покрываемые требования", "")
    requirements = [r.strip() for r in reqs_raw.split(",") if r.strip()]

    test_data = []
    for row in vc.parse_table(sections.get("Тестовые данные", "")):
        if len(row) < 2:
            continue
        test_data.append({
            "name": row[0],
            "value": row[1],
            "comment": row[2] if len(row) > 2 else "",
        })

    steps = []
    for idx, row in enumerate(vc.parse_table(sections.get("Шаги", "")), start=1):
        if len(row) != 4:
            continue
        num_raw, action, data, expected = row
        num_clean = num_raw.strip().rstrip(".")
        number = int(num_clean) if num_clean.isdigit() else idx
        steps.append({
            "number": number,
            "action": action,
            "testData": data,
            "expectedResult": expected,
            "requirements": [],  # not derivable from Markdown — see module docstring
        })

    return {
        "id": fields.get("ID", ""),
        "title": fields.get("Название", ""),
        "goal": fields.get("Цель", ""),
        "priority": fields.get("Приоритет", ""),
        "journeyId": fields.get("Journey", ""),
        "variantOf": variant_of,
        "requirements": requirements,
        "preconditions": parse_bullets(sections.get("Предусловия", "")),
        "testData": test_data,
        "steps": steps,
        "postconditions": parse_bullets(sections.get("Постусловия", "")),
        "gaps": parse_bullets(sections.get("Выявленные пробелы", "")),
        "clarifyingQuestions": parse_numbered(sections.get("Уточняющие вопросы", "")),
    }


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".md" else []
    return vc.collect(target)


def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown -> JSON кейса, детерминированно")
    ap.add_argument("target", help="файл .md или каталог кейсов (output/cases/<JOURNEY_ID>)")
    ap.add_argument("--check", action="store_true",
                     help="только сверить, ничего не писать; код 1 при расхождении")
    ap.add_argument("--quiet", action="store_true", help="печатать только сводку")
    args = ap.parse_args()

    target = Path(args.target)
    if not target.exists():
        sys.stderr.write(f"build_case_json: путь не найден: {target}\n")
        return 2

    files = collect(target)
    if not files:
        sys.stderr.write(f"build_case_json: нет .md кейсов под {target}\n")
        return 2

    written = 0
    stale: list[str] = []
    for md_path in files:
        expected = build_case(md_path)
        json_path = md_path.with_suffix(".json")

        if args.check:
            if not json_path.exists():
                stale.append(f"{json_path} — отсутствует")
                continue
            try:
                actual = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                stale.append(f"{json_path} — невалиден: {exc}")
                continue
            if actual != expected:
                stale.append(f"{json_path} — не совпадает с {md_path.name}")
                continue
            if not args.quiet:
                print(f"[build-json] OK    {json_path}")
        else:
            json_path.write_text(
                json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written += 1
            if not args.quiet:
                print(f"[build-json] wrote {json_path}")

    if args.check:
        if stale:
            print(f"[build-json] расхождений: {len(stale)}")
            for s in stale:
                print(f"[build-json]   {s}")
            return 1
        print(f"[build-json] сверено {len(files)} файл(ов), расхождений нет")
        return 0

    print(f"[build-json] записано {written} файл(ов) из {len(files)} кейсов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
