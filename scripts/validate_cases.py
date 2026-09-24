#!/usr/bin/env python3
"""Deterministic linter for e2e test cases produced by the qa-designer agent.

Checks structure, placeholders, step numbering, expected-result observability,
step atomicity, test-data usage and Markdown/JSON parity.

Usage:
    python3 scripts/validate_cases.py output/cases
    python3 scripts/validate_cases.py output/cases/J01-onboarding --json output/reviews/J01-lint.json

Exit code 0 = no blockers, 1 = blockers found, 2 = nothing to check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

# Exit code 1 means "blockers found". A cp1251 console dying on «→» while printing
# findings exits 1 too — so the console codepage could fabricate a blocker and lose
# the findings that would have explained it. gate_check.py already guarded this;
# the linter the whole gate rests on did not.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REQUIRED_SECTIONS = [
    "Общая информация",
    "Предусловия",
    "Тестовые данные",
    "Шаги",
    "Постусловия",
    "Выявленные пробелы",
    "Уточняющие вопросы",
]

# Sections that may legitimately contain only "—"
SOFT_SECTIONS = {"Выявленные пробелы", "Уточняющие вопросы"}

REQUIRED_FIELDS = [
    "ID",
    "Название",
    "Цель",
    "Приоритет",
    "Journey",
    "Вариант от",
    "Покрываемые требования",
]

VALID_PRIORITIES = {"критический", "высокий", "средний", "низкий"}

# Words that describe "it worked" without naming anything observable.
VAGUE = [
    "успешно",
    "успешный",
    "корректно",
    "корректный",
    "правильно",
    "работает",
    "без ошибок",
    "ошибок нет",
    "как ожидается",
    "как ожидалось",
    "ожидаемо",
    "выполнено",
    "нормально",
    "всё хорошо",
    "все хорошо",
    "соответствует ожиданиям",
    "должно работать",
    "ok",
    "ок",
]

# Text that means the author left a hole.
PLACEHOLDERS = [
    "todo",
    "tbd",
    "xxx",
    "заполнить",
    "любой трек",
    "какой-нибудь",
    "какой-либо",
    "n/a",
    "нужно уточнить у себя",
]

# An unfilled slot is a *pair* of angle brackets around a short label:
# <название плейлиста>, <track>. A lone "<" is arithmetic — «время отклика < 2 сек»,
# «лимит < 100» — and blocking it made every comparison a false BLOCKER, which the
# critic is forbidden to argue with (docs/critic-rubric.md), so a correct case lost
# a whole design+review iteration.
PLACEHOLDER_SLOT = re.compile(r"<[^<>\s][^<>]{0,58}>")

# Разметка отличается от незаполненного слота наличием закрывающего элемента:
# `</provider>` или `/>`. Плейсхолдер `<хост>` его не имеет по определению.
# Без этого XML нельзя было положить значением в тестовые данные — REQ-02 требует
# XML-описание топологии, а линтер объявлял каждый его тег заглушкой, и дизайнеру
# приходилось ссылаться на имя файла вместо самого значения.
MARKUP_HINT = re.compile(r"</[^<>\s]|/>")

# Markers of more than one action inside a single step.
MULTI_ACTION = [
    " и затем ",
    " затем ",
    " после чего ",
    ", после этого ",
    " и после ",
    ";",
]

MIN_MAIN_STEPS = 8

# Ceiling on how far one stage may be unrolled into steps. Measured density on every
# main case on disk: 1.25 and 1.38 and 1.75 (Knox, three runs on one corpus) and 1.58
# (the Звук reference in examples/). Two is above all four with margin, so the rule
# guards against runaway expansion instead of enforcing a house style — the floor had
# no counterpart until three runs turned 8 stages into 10, 11 and 14 steps.
#
# Run 12 fired it twice on a UI corpus and the diagnosis is worth keeping: the defect
# was in the plans, not here. The Звук reference is the same UI corpus and is *more*
# granular per action, staying at 1.58 only because its plan declares twelve stages.
# The plans that tripped the rule bundle three or four user actions into one stage,
# which is what MAX_STAGES=8 in validate_plans.py pushes an analyst toward. The two
# ceilings interact: cap the stages and the density has nowhere to go but up. Before
# raising this number, look at whether the stage ceiling is the thing to revisit.
MAX_STEPS_PER_STAGE = 2

MIN_EXPECTED_LEN = 20
VAGUE_SHORT_CELL = 60

# Populated by main() from --index before check_case() runs; None means "no index found,
# skip the check" rather than "index is empty" — a missing index must never read as
# zero valid ids, or every case would falsely BLOCKER on its first run.
VALID_REQ_IDS: set[str] | None = None


def load_req_ids(index_path: Path) -> set[str] | None:
    """Collect every requirement id a case is allowed to cite, from _index.json.

    A hallucinated or mistyped REQ-id is exactly the failure mode a weaker model produces
    more often — this catches it for free, without needing the critic to notice by reading.
    Best-effort: any missing/unreadable/malformed index means "skip this check", not "fail".
    """
    if not index_path.is_file():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    ids: set[str] = set()
    for entry in data.get("reqIndex", []) or []:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]))
    return ids or None


class Finding:
    def __init__(self, severity: str, rule: str, file: str, location: str, message: str, fix: str):
        self.severity = severity
        self.rule = rule
        self.file = file
        self.location = location
        self.message = message
        self.fix = fix

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "file": self.file,
            "location": self.location,
            "message": self.message,
            "fix": self.fix,
        }

    def __str__(self) -> str:
        return f"[{self.severity}] {self.file} :: {self.location} :: {self.message} → {self.fix}"


def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    buffer: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = m.group(1).strip()
            buffer = []
        else:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def parse_table(block: str) -> list[list[str]]:
    """Return data rows of the first Markdown table in the block."""
    rows: list[list[str]] = []
    seen_header = False
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if rows:
                break
            continue
        # `\|` — экранированная труба внутри ячейки, а не её граница: конвертер
        # экранирует значения, чтобы `|` в тексте шага не разъезжал таблицу.
        # Резать по ней — значит сообщать «5 колонок вместо 4» о корректной строке.
        cells = [c.strip().replace("\\|", "|").replace("\\\\", "\\")
                 for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if re.fullmatch(r"[-: ]+", "".join(cells).replace("|", "")) and set("".join(cells)) <= set("-: "):
            seen_header = True
            continue
        if not seen_header:
            continue
        rows.append(cells)
    return rows


def parse_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"^\s*[-*]\s*\*\*(.+?):\*\*\s*(.*)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def has_placeholder(value: str) -> str | None:
    low = value.lower()
    for p in PLACEHOLDERS:
        # Word boundaries, same as vague_hit: a bare substring match turned every
        # Admin API URL into a BLOCKER — «admi·n/a·pi» contains "n/a" — and the
        # designer worked around the linter with code spans instead of writing cases.
        if re.search(rf"(?<![a-zа-я0-9]){re.escape(p)}(?![a-zа-я0-9])", low):
            return p
    if MARKUP_HINT.search(value):
        # Значение — разметка. Слот внутри неё не ищем: цена — пропущенный
        # плейсхолдер внутри XML, что редко; выигрыш — XML вообще можно написать.
        return None
    slot = PLACEHOLDER_SLOT.search(value)
    if slot:
        return slot.group(0)
    return None


def vague_hit(value: str) -> str | None:
    low = value.lower()
    for w in VAGUE:
        if re.search(rf"(?<![а-яa-z]){re.escape(w)}(?![а-яa-z])", low):
            return w
    return None


def normalize_cell(value: str) -> str:
    """Markdown cell and JSON string, reduced to comparable text.

    Strips emphasis markers and collapses whitespace — nothing more. The point is to
    catch a JSON twin whose *content* drifted from the Markdown, not to punish a
    designer for `**bold**`. Anything this normalization cannot equate is a real
    difference in wording, and per the format contract the two must be
    «содержательно идентичны».
    """
    return " ".join(value.replace("**", "").replace("`", "").replace("*", "").split())


def section_is_empty(body: str) -> bool:
    stripped = re.sub(r"[-*\s—\d.]", "", body)
    return stripped == ""


def plan_for(case_path: Path, override: str | None) -> Path | None:
    """The suite plan that owns this case, or None when it cannot be located.

    Two layouts are recognised: a run tree (output/cases/<JID>/ beside
    output/suites/<JID>.md) and the self-contained example directory, which keeps
    its plan next to the cases as _suite-plan.md.
    """
    if override:
        p = Path(override)
        return p if p.is_file() else None
    jid = case_path.parent.name
    candidates = [
        case_path.parent.parent.parent / "suites" / f"{jid}.md",
        Path("output/suites") / f"{jid}.md",
        case_path.parent / "_suite-plan.md",
    ]
    return next((c for c in candidates if c.is_file()), None)


def stage_count(plan: Path) -> int | None:
    """Rows of the plan's «Этапы» table — the same table validate_plans.py reads."""
    m = re.search(r"^##\s+Этапы\s*$(.*?)(?=^##\s|\Z)", plan.read_text(encoding="utf-8"),
                  re.M | re.S)
    if not m:
        return None
    rows = sum(1 for line in m.group(1).splitlines() if re.match(r"^\|\s*\d+\s*\|", line))
    return rows or None


def check_case(path: Path, findings: list[Finding], stages: int | None = None) -> None:
    name = str(path)
    text = path.read_text(encoding="utf-8")
    sections = split_sections(text)

    def add(sev, rule, loc, msg, fix):
        findings.append(Finding(sev, rule, name, loc, msg, fix))

    # --- structure -------------------------------------------------------
    for required in REQUIRED_SECTIONS:
        if required not in sections:
            add("BLOCKER", "missing-section", f"## {required}",
                "Обязательный раздел отсутствует",
                f"Добавить раздел «{required}» по docs/format.md")
            continue
        body = sections[required]
        if section_is_empty(body) and required not in SOFT_SECTIONS:
            add("BLOCKER", "empty-section", f"## {required}",
                "Обязательный раздел пуст",
                "Заполнить раздел конкретным содержанием")

    # --- header fields ---------------------------------------------------
    fields = parse_fields(sections.get("Общая информация", ""))
    for f in REQUIRED_FIELDS:
        value = fields.get(f)
        if value is None:
            add("BLOCKER", "missing-field", f"Общая информация / {f}",
                "Поле отсутствует", f"Добавить поле **{f}:**")
        elif value in ("", "…") or (has_placeholder(value) and f != "Вариант от"):
            add("BLOCKER", "placeholder-field", f"Общая информация / {f}",
                f"Поле не заполнено или содержит заглушку: '{value}'",
                "Подставить конкретное значение")

    priority = fields.get("Приоритет", "").strip().lower().rstrip(".")
    if priority and priority not in VALID_PRIORITIES and "|" not in priority:
        add("MINOR", "priority-value", "Общая информация / Приоритет",
            f"Нестандартное значение приоритета: '{priority}'",
            "Использовать: Критический / Высокий / Средний / Низкий")

    case_id = fields.get("ID", "").strip()
    is_variant = fields.get("Вариант от", "—").strip() not in ("—", "-", "", "None")
    if case_id and not re.fullmatch(r"TC-J\d{2}-\d{2}", case_id):
        add("MINOR", "id-format", "Общая информация / ID",
            f"ID '{case_id}' не соответствует шаблону TC-J<NN>-<NN>",
            "Переименовать по docs/format.md")
    if case_id.endswith("-00") and is_variant:
        add("MAJOR", "variant-flag", "Общая информация / Вариант от",
            "Основной кейс помечен как вариант",
            "Поставить «—» в поле «Вариант от»")
    if case_id and not case_id.endswith("-00") and not is_variant:
        add("MAJOR", "variant-flag", "Общая информация / Вариант от",
            "Кейс-вариант не ссылается на основной кейс",
            "Указать «Вариант от: TC-J<NN>-00»")
    if is_variant:
        variant_of = fields.get("Вариант от", "").strip()
        target = path.with_name(f"{variant_of}.md")
        if variant_of and not target.exists():
            add("BLOCKER", "variant-target-missing", "Общая информация / Вариант от",
                f"«Вариант от: {variant_of}» не ссылается ни на один файл в этом каталоге",
                f"Проверить ID: должен существовать {target.name}, либо это опечатка/выдуманный ID")

    reqs_field = fields.get("Покрываемые требования", "")
    if reqs_field and reqs_field not in ("", "…") and not has_placeholder(reqs_field):
        cited = [r.strip() for r in reqs_field.split(",") if r.strip()]
        if VALID_REQ_IDS is not None:
            unknown = [r for r in cited if r not in VALID_REQ_IDS]
            if unknown:
                add("BLOCKER", "req-id-unknown", "Общая информация / Покрываемые требования",
                    f"ID не найден(ы) в индексе требований: {', '.join(unknown)}",
                    "Сверить с output/suites/_index.json / _reqindex.json — опечатка или "
                    "несуществующий (выдуманный) якорь требования")

    # Путь к источнику нужен и здесь, и в блоке паритета ниже: ссылки на параметры
    # видны только в JSON — в Markdown конвертер их уже разрешил.
    json_path = path.with_suffix(".json")

    # --- test data -------------------------------------------------------
    data_rows = parse_table(sections.get("Тестовые данные", ""))
    data_values: list[str] = []
    data_names: list[str] = []
    for i, row in enumerate(data_rows, start=1):
        if len(row) < 2:
            add("MAJOR", "data-table", f"Тестовые данные / строка {i}",
                "Строка таблицы данных неполна",
                "Формат: | Параметр | Значение | Комментарий |")
            continue
        param, value = row[0], row[1]
        if not param or not value:
            add("BLOCKER", "data-empty", f"Тестовые данные / строка {i}",
                "Пустой параметр или значение", "Указать конкретное значение")
            continue
        ph = has_placeholder(value)
        if ph:
            add("BLOCKER", "data-placeholder", f"Тестовые данные / {param}",
                f"Значение содержит заглушку '{ph}': {value}",
                "Подставить конкретное, стабильное значение")
        else:
            data_values.append(value)
            data_names.append(param.strip())
    if not data_rows:
        add("BLOCKER", "data-missing", "Тестовые данные",
            "Таблица тестовых данных отсутствует",
            "Добавить таблицу | Параметр | Значение | Комментарий |")

    # --- steps -----------------------------------------------------------
    step_rows = parse_table(sections.get("Шаги", ""))
    if not step_rows:
        add("BLOCKER", "steps-missing", "Шаги",
            "Таблица шагов отсутствует или не распознана",
            "Использовать таблицу | № | Действие | Тестовые данные | Ожидаемый результат |")
        return

    if not is_variant and len(step_rows) < MIN_MAIN_STEPS:
        add("BLOCKER", "steps-too-few", "Шаги",
            f"В основном кейсе {len(step_rows)} шагов, минимум {MIN_MAIN_STEPS} — это не сквозной сценарий",
            "Развернуть путь до полного journey по suite-плану")

    if not is_variant and stages:
        ceiling = MAX_STEPS_PER_STAGE * stages
        if len(step_rows) > ceiling:
            add("MAJOR", "steps-too-many", "Шаги",
                f"В основном кейсе {len(step_rows)} шагов на {stages} этапов плана "
                f"({len(step_rows) / stages:.2f} на этап), потолок {ceiling}",
                "Сначала проверьте план: этап, несущий три-четыре действия, — самая "
                "частая причина. Разделить такие этапы поднимает потолок и не трогает "
                "шаги. Только если каждый этап уже одно действие — сводить шаги, "
                "дробящие одно наблюдаемое")

    steps_text = " ".join(" ".join(r) for r in step_rows).lower()
    # Markdown markers stripped for substring matching below: a value written as
    # `https://host:8443` in the data table appears inside `https://host:8443/path`
    # in a step, and the trailing backtick made the literal token miss. Third
    # instance of a linter rule reporting a defect the case does not have —
    # after "<" arithmetic and "n/a" inside admi·n/a·pi.
    steps_text_plain = normalize_cell(steps_text).lower()

    for idx, row in enumerate(step_rows, start=1):
        loc = f"Шаги / шаг {idx}"
        if len(row) != 4:
            add("BLOCKER", "step-columns", loc,
                f"В строке {len(row)} колонок вместо 4",
                "Привести строку к формату из docs/format.md")
            continue
        num, action, data, expected = row
        num_clean = num.strip().rstrip(".")
        if not num_clean.isdigit() or int(num_clean) != idx:
            add("BLOCKER", "step-numbering", loc,
                f"Нарушена нумерация шагов: '{num}' на позиции {idx}",
                "Пронумеровать шаги подряд с 1")
        if not action:
            add("BLOCKER", "step-empty-action", loc, "Пустое действие пользователя",
                "Описать одно конкретное действие")
        if not expected:
            add("BLOCKER", "step-empty-expected", loc, "Пустой ожидаемый результат",
                "Описать наблюдаемый результат")
            continue

        ph = has_placeholder(action) or has_placeholder(expected)
        if ph:
            add("BLOCKER", "step-placeholder", loc,
                f"Шаг содержит заглушку '{ph}'",
                "Заменить на конкретное значение или вынести в пробелы")

        vw = vague_hit(expected)
        if vw:
            if len(expected) < VAGUE_SHORT_CELL:
                add("BLOCKER", "vague-expected", loc,
                    f"Расплывчатый ожидаемый результат («{vw}») без наблюдаемого признака: {expected}",
                    "Указать экран, элемент, текст, состояние, счётчик или порядок")
            else:
                add("MAJOR", "vague-wording", loc,
                    f"В ожидаемом результате есть оценочное слово «{vw}»",
                    "Убрать оценку, оставить наблюдаемый признак")
        elif len(expected) < MIN_EXPECTED_LEN:
            add("MAJOR", "expected-too-short", loc,
                f"Слишком короткий ожидаемый результат: {expected}",
                "Дополнить наблюдаемым признаком")

        low_action = action.lower()
        for marker in MULTI_ACTION:
            if marker in low_action:
                add("MAJOR", "step-not-atomic", loc,
                    f"В одном шаге несколько действий (маркер '{marker.strip()}')",
                    "Разбить на отдельные шаги")
                break
        else:
            if low_action.count(" и ") >= 2:
                add("MAJOR", "step-not-atomic", loc,
                    "В одном шаге несколько действий (два и более союза «и»)",
                    "Разбить на отдельные шаги")

        if not data:
            add("MINOR", "step-data-empty", loc,
                "Колонка тестовых данных пуста",
                "Указать значение или «—»")

    # --- data usage & continuity ----------------------------------------
    # Шаг может назвать параметр ссылкой `{Имя}` вместо того, чтобы вклеивать его
    # значение текстом. Тогда «использован» проверяется по имени — точно, а не
    # подстрокой, и стодвадцатисимвольный URL не приходится писать дважды.
    referenced = referenced_names(json_path)
    used = 0
    for name, value in zip(data_names, data_values):
        if name and name in referenced:
            used += 1
            continue
        token = normalize_cell(value).lower().strip()
        if len(token) < 3:
            continue
        if token in steps_text_plain:
            used += 1
        else:
            add("MAJOR", "data-unused", "Тестовые данные",
                f"Значение не используется ни в одном шаге: {value}",
                "Использовать значение в шагах, сослаться на него как {" + (name or "Имя") +
                "}, или убрать из таблицы")
    dangling = sorted(referenced - set(data_names))
    if dangling:
        add("BLOCKER", "testdata-ref-unknown", "Шаги",
            "Шаг ссылается на параметр, которого нет в таблице тестовых данных: "
            + ", ".join("{" + d + "}" for d in dangling),
            "Опечатка в имени или строка таблицы не заведена — ссылка осталась "
            "неразрешённой и попала в Markdown как есть")
    if data_values and used == 0:
        add("BLOCKER", "no-data-continuity", "Шаги",
            "Ни одно тестовое значение не встречается в шагах — сценарий не связан данными",
            "Ссылаться на конкретные значения из таблицы данных")

    if len(step_rows) >= 6 and data_values:
        tail = " ".join(" ".join(r) for r in step_rows[len(step_rows) // 2:]).lower()
        if not any(v.lower() in tail for v in data_values if len(v) >= 3):
            add("MAJOR", "weak-continuity", "Шаги",
                "Во второй половине сценария не переиспользуются данные из первой",
                "Проверять в поздних шагах объекты, созданные в ранних")

    # --- markdown / json parity -----------------------------------------
    # (json_path объявлен выше — он же нужен блоку тестовых данных)
    # Полная сверка идёт первой: она сравнивает файл целиком, поэтому находит и то,
    # чего не видят пофайловые правила ниже — предусловия, таблицу данных,
    # постусловия, пробелы и вопросы. Правила ниже остаются: они называют, ЧТО именно
    # разошлось, а эта — что разошлось хоть что-нибудь.
    conv = converter()
    if conv is not None and json_path.exists():
        try:
            expected = conv.json_to_md(json.loads(json_path.read_text(encoding="utf-8")))
        except Exception:
            expected = None
        # --- пошаговая трассируемость ------------------------------------
        # Единственное поле, которое и решило сделать JSON первичным: из Markdown
        # его не восстановить. До этих правил его никто не читал, и заполнение
        # было актом веры — а незачитываемое поле неизбежно расходится с истиной.
        try:
            case_json = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            case_json = None
        if isinstance(case_json, dict):
            case_reqs = [r for r in case_json.get("requirements", []) if isinstance(r, str)]
            step_reqs: dict[str, list[int]] = {}
            for i, st in enumerate(case_json.get("steps", []), start=1):
                if not isinstance(st, dict):
                    continue
                for r in st.get("requirements", []) or []:
                    if isinstance(r, str) and r.strip():
                        step_reqs.setdefault(r.strip(), []).append(i)

            if VALID_REQ_IDS is not None:
                unknown = sorted(r for r in step_reqs if r not in VALID_REQ_IDS)
                if unknown:
                    add("BLOCKER", "step-req-unknown", f"{json_path.name} / steps[].requirements",
                        f"ID не найден(ы) в индексе требований: {', '.join(unknown)}",
                        "Сверить с output/suites/_index.json — опечатка или выдуманный якорь")

            if case_reqs and step_reqs:
                stray = sorted(r for r in step_reqs if r not in case_reqs)
                if stray:
                    add("MAJOR", "step-req-outside-case",
                        f"{json_path.name} / steps[].requirements",
                        f"Шаги ссылаются на требования вне «Покрываемые требования» кейса: "
                        f"{', '.join(stray)}",
                        "Либо добавить их в requirements кейса, либо убрать из шагов — "
                        "кейс обязан объявлять всё, что проверяет")
                unexercised = sorted(r for r in case_reqs if r not in step_reqs)
                if unexercised:
                    add("MAJOR", "req-not-exercised",
                        f"{json_path.name} / requirements",
                        f"Кейс объявляет требования, которых не проверяет ни один шаг: "
                        f"{', '.join(unexercised)}",
                        "Либо привязать их к шагам через steps[].requirements, либо убрать "
                        "из requirements — иначе кейс заявляет покрытие, которого не даёт")

            # --- ссылки из шагов на пробелы и вопросы -----------------------
            # Позиционная нумерация ломается молча: вставка одного вопроса
            # сдвигает все последующие, а ссылки в шагах остаются старыми.
            # Шесть из семи MINOR прогона 8 были ровно этим.
            gap_ids = entry_ids(case_json.get("gaps"), "G")
            q_ids = entry_ids(case_json.get("clarifyingQuestions"), "Q")
            dangling = []
            for i, st in enumerate(case_json.get("steps", []) or [], start=1):
                if not isinstance(st, dict):
                    continue
                blob = " ".join(str(st.get(k, "")) for k in
                                ("action", "testData", "expectedResult"))
                for kind, num in REF_POSITIONAL.findall(blob):
                    pool = gap_ids if kind.lower().startswith("проб") else q_ids
                    if num not in pool:
                        dangling.append((i, f"{kind} {num}"))
                for ref in REF_BY_ID.findall(blob):
                    pool = gap_ids if ref.upper().startswith("G") else q_ids
                    if ref not in pool:
                        dangling.append((i, ref))
            for step_no, ref in dangling[:6]:
                add("MAJOR", "ref-dangling", f"Шаги / шаг {step_no}",
                    f"Ссылка «{ref}» не соответствует ни одному пробелу или вопросу кейса",
                    "Сослаться на существующий id — или завести недостающий пробел. "
                    "Позиционные номера ломаются при вставке: дайте элементам явные id")

        if expected is not None and expected != text:
            add("BLOCKER", "md-stale", str(path.name),
                "Markdown не совпадает с тем, что даёт конвертер из JSON — "
                "значит его правили руками или не перегенерировали после правки JSON",
                "Перегенерировать: python3 scripts/json_to_md.py "
                f"{json_path}")

    if not json_path.exists():
        add("BLOCKER", "json-missing", str(json_path.name),
            "Нет JSON-версии кейса — а JSON первичен, значит этот Markdown написан руками "
            "и не порождён из источника",
            "Написать кейс в `.json` и породить Markdown: python3 scripts/json_to_md.py")
    else:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            add("BLOCKER", "json-invalid", str(json_path.name),
                f"JSON невалиден: {exc}", "Исправить синтаксис JSON")
        else:
            if data.get("id", "") != case_id:
                add("BLOCKER", "json-id-mismatch", str(json_path.name),
                    f"ID в JSON ('{data.get('id')}') не совпадает с Markdown ('{case_id}')",
                    "Перегенерировать: python3 scripts/json_to_md.py на JSON кейса")
            if len(data.get("steps", [])) != len(step_rows):
                add("BLOCKER", "json-steps-mismatch", str(json_path.name),
                    f"Шагов в JSON {len(data.get('steps', []))}, в Markdown {len(step_rows)}",
                    "Перегенерировать: python3 scripts/json_to_md.py на JSON кейса")
            else:
                # Same count is necessary, not sufficient: the id/count checks pass while
                # a step's wording silently diverges, and downstream the JSON is what the
                # pytest writer consumes. MAJOR, not BLOCKER — the normalization is
                # deliberately shallow, and a false BLOCKER costs a design+review
                # iteration (the O-05 lesson).
                fields = (("action", 1, "действие"),
                          ("testData", 2, "тестовые данные"),
                          ("expectedResult", 3, "ожидаемый результат"))
                for idx, (jstep, row) in enumerate(zip(data["steps"], step_rows), start=1):
                    if not isinstance(jstep, dict) or len(row) != 4:
                        continue
                    for key, col, label in fields:
                        md_val = normalize_cell(row[col])
                        raw = str(jstep.get(key, ""))
                        # Ссылка `{Имя}` в источнике и её значение в выводе — не
                        # расхождение, а работа конвертера. Сравниваем разрешённое.
                        if conv is not None and "{" in raw:
                            raw = conv.resolve_refs(raw, data.get("testData", []))
                        js_val = normalize_cell(raw)
                        if md_val != js_val:
                            add("MAJOR", "json-step-drift",
                                f"{json_path.name} / шаг {idx}",
                                f"{label} в JSON расходится с Markdown: "
                                f"«{js_val[:60]}» ≠ «{md_val[:60]}»",
                                "Перегенерировать: python3 scripts/json_to_md.py "
                                "на JSON кейса")
                            break  # одного расхождения на шаг достаточно


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".md" else []
    return sorted(
        p for p in target.rglob("*.md")
        if not p.name.startswith("_") and "review" not in p.name.lower()
    )


_CONVERTER = None


def converter():
    """`json_to_md` as a module, or None when it cannot be loaded.

    The parity rules below compare hand-picked fields — id, step count, three step
    columns — so a divergence in preconditions, test data, postconditions, gaps or
    questions passes unseen. The converter already knows the whole answer: what the
    Markdown must be. Asking it turns a partial comparison into a total one.
    """
    global _CONVERTER
    if _CONVERTER is None:
        path = Path(__file__).with_name("json_to_md.py")
        if not path.is_file():
            _CONVERTER = False
        else:
            try:
                spec = importlib.util.spec_from_file_location("json_to_md", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _CONVERTER = mod
            except Exception:
                _CONVERTER = False
    return _CONVERTER or None


TESTDATA_REF = re.compile(r"(?<!\{)\{([^{}]+)\}(?!\})")


def referenced_names(json_path: Path) -> set[str]:
    """Имена параметров, на которые ссылаются шаги через `{Имя}`.

    Читается из JSON, а не из Markdown: в Markdown ссылки уже разрешены
    конвертером, там их не видно по определению.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    names: set[str] = set()
    for st in data.get("steps", []) or []:
        if isinstance(st, dict):
            cell = st.get("testData") or st.get("test_data") or ""
            if isinstance(cell, str):
                names.update(m.group(1).strip() for m in TESTDATA_REF.finditer(cell))
    return names


# Ссылка из шага на пробел или вопрос. Ловим обе формы: старую позиционную
# («пробел 3», «вопрос 2») и новую по идентификатору («G-03», «Q-J02-1»).
REF_POSITIONAL = re.compile(r"\b(пробел|вопрос)\s+(\d{1,2})\b", re.I)
REF_BY_ID = re.compile(r"\b([GQ]-[A-Za-z0-9-]{1,12})\b")


def entry_ids(items, letter):
    """Идентификаторы пробелов или вопросов кейса — явные, если они есть."""
    ids = set()
    for i, item in enumerate(items or [], start=1):
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
        else:
            ids.add(f"{letter}-{i}")          # позиционный запасной вариант
            ids.add(str(i))
    return ids


def orphan_json(target: Path) -> list[Path]:
    """JSON cases with no Markdown sibling.

    The linter walks `.md`, so a case whose Markdown was never generated is not merely
    unchecked — it is invisible, and an invisible case reads exactly like a clean one.
    Since JSON became the primary format, this is the likeliest way for a case to skip
    every rule in this file.
    """
    if target.is_file():
        return []
    return sorted(
        p for p in target.rglob("TC-*.json")
        if not p.with_suffix(".md").exists()
    )


def default_index_path(target: Path) -> Path | None:
    """Find the `_index.json` that actually belongs to this target, not just any file at a
    fixed cwd-relative path. A case directory outside `output/cases/...` (e.g. the bundled
    `examples/` fixture, or a second run checked from a different cwd) has no business being
    cross-checked against whatever `output/suites/_index.json` happens to be lying around from
    an unrelated run — that produced a false BLOCKER wave on the reference example the first
    time this shipped. Walk up from the target looking for a `cases` directory and use its
    sibling `suites/_index.json`; if there is none, there is no sensible default, and the
    requirement-id check is skipped rather than guessed.
    """
    for ancestor in (target.resolve(), *target.resolve().parents):
        if ancestor.name == "cases":
            return ancestor.parent / "suites" / "_index.json"
    return None


def main() -> int:
    global VALID_REQ_IDS

    ap = argparse.ArgumentParser(description="Lint e2e test cases")
    ap.add_argument("target", nargs="?", default="output/cases", help="file or directory")
    ap.add_argument("--json", dest="json_out", help="write JSON report to this path")
    ap.add_argument("--quiet", action="store_true", help="only print the summary line")
    ap.add_argument("--plan", help="suite plan for the stage ceiling; found automatically "
                                   "at output/suites/<JOURNEY_ID>.md or <case dir>/_suite-plan.md")
    ap.add_argument("--index", default=None,
                     help="reqIndex source for citation checks; default is the sibling "
                          "suites/_index.json of the target's cases/ directory, if any. "
                          "Missing/unresolvable file just skips that check.")
    ap.add_argument("--no-req-check", action="store_true",
                     help="skip requirement-id citation checking even if --index exists")
    args = ap.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"validate_cases: path not found: {target}", file=sys.stderr)
        return 2

    files = collect(target)
    orphans = orphan_json(target)
    if not files and not orphans:
        print(f"validate_cases: no .md cases under {target}", file=sys.stderr)
        return 2

    if not args.no_req_check:
        index_path = Path(args.index) if args.index is not None else default_index_path(target)
        if index_path is not None:
            VALID_REQ_IDS = load_req_ids(index_path)

    findings: list[Finding] = []
    for orphan in orphans:
        findings.append(Finding(
            "BLOCKER", "md-missing", str(orphan),
            "Markdown", "JSON есть, Markdown нет — кейс не попадает ни в одну проверку "
            "этого линтера и не виден человеку на шлюзе",
            "Породить Markdown: python3 scripts/json_to_md.py " + str(orphan)))

    unmeasured: set[str] = set()
    for f in files:
        plan = plan_for(f, args.plan)
        stages = stage_count(plan) if plan else None
        if stages is None:
            unmeasured.add(f.parent.name)
        check_case(f, findings, stages)

    counts = {"BLOCKER": 0, "MAJOR": 0, "MINOR": 0}
    for f in findings:
        counts[f.severity] += 1

    if not args.quiet:
        for severity in ("BLOCKER", "MAJOR", "MINOR"):
            for f in findings:
                if f.severity == severity:
                    print(f)

    print(
        f"checked {len(files)} case file(s): "
        f"BLOCKER {counts['BLOCKER']}, MAJOR {counts['MAJOR']}, MINOR {counts['MINOR']}"
    )
    if unmeasured:
        # Naming what went unchecked: a rule silently skipped reads as a rule that passed.
        print(f"  потолок шагов не проверен — план не найден: {', '.join(sorted(unmeasured))}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "checkedFiles": [str(f) for f in files],
                    "counts": counts,
                    "findings": [f.as_dict() for f in findings],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"json report → {out}")

    return 1 if counts["BLOCKER"] else 0


if __name__ == "__main__":
    sys.exit(main())
