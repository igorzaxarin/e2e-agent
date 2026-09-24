#!/usr/bin/env python3
"""Шлюз слоя 3: ни один селектор в сгенерированном коде не выдуман.

Эквивалент `validate_cases.py` для стадии писателей. Разделение владения намеренно
и не меняется — цикл дизайн/ревью владеет `output/cases/`, писатели владеют
`output/tests/`, `test-critic` сгенерированный код не ревьюит. Не хватало другого:
у дизайнера есть механический шлюз ДО семантического ревью, у писателя не было
никакого. Поэтому между выдуманным селектором и отчётом не стояло ничего.

Правила
-------
selector-unknown     BLOCKER  значения нет в инвентаре — выдумано
selector-positional  MAJOR    XPath по позиции (`/div[3]/span[2]`) — самый хрупкий якорь
selector-class       MAJOR    якорь по классу — непроверяемо, а не выдумано
selector-unstable    MAJOR    хеш сборщика в качестве якоря

Разница BLOCKER и MAJOR здесь принципиальна. «Выдумано» — утверждение о продукте,
которого никто не описывал, и это дефект того же класса, что ловит
`docs/critic-rubric.md` BLOCKER #1. «Непроверяемо» — селектор может быть верным,
просто инвентарь про него ничего не знает; ронять на этом весь прогон нельзя.

Три ошибки, воспроизведённые намеренно
--------------------------------------
Все три найдены тем, что инструмент запускали на настоящих сьютах, а не
рассуждали о нём. Ни одна не видна на корректно составленном синтетическом входе.

1. **`By.ID` принимает ГОЛОЕ значение, а не CSS.** В этих сьютах `By.ID` —
   самая частая форма из всех. Инструмент, читающий каждое значение как CSS,
   объявлял настоящие селекторы неразбираемыми MAJOR'ами. Способ чтения выбирает
   вид локатора, а не наоборот.
2. **Закрывающая кавычка — обратная ссылка, а не «любая из двух».** Шаблон
   `['\"]` обрезает `'[data-testid="phone-field"]'` по внутренней кавычке и даёт
   мусор `[data-testid=`. Форма с кавычками внутри — это ровно то, как выглядит
   тестовый идентификатор, то есть наивный шаблон отказывает сильнее всего на том
   случае, ради которого пишется. После правки обнаружение на готовых сьютах
   выросло с 40 до 79 находок: половина выдуманных селекторов проходила мимо
   самого проверяющего.
3. **`stdin` тоже нужно перенастраивать.** Перенастроив `stdout`/`stderr` и забыв
   `stdin`, инструмент получает кириллицу как мохито и обвиняет писателя в том,
   чего тот не делал.

Fail-closed без fail-random: нет инвентаря — код 2 и «НЕ ПРОВЕРЕНО», никогда
«прошло» против случайно лежащего рядом файла от другого корпуса.

Коды выхода: 0 — блокеров нет, 1 — блокеры есть, 2 — проверка не состоялась.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# stdin — тоже: см. ошибку 3 в докстринге.
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

# Локатор Selenium: By.X, "значение". Закрывающая кавычка — обратная ссылка (\2).
LOCATOR = re.compile(
    r"By\.([A-Z_]+)\s*,\s*(['\"])(.*?)\2", re.S)

# Именованная константа селектора: PHONE_INPUT_SELECTOR = "..." / PHONE_INPUT_ID = '...'
CONST = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*(?:SELECTOR|ID|NAME|TESTID|LOCATOR|XPATH|CSS))\s*=\s*(['\"])(.*?)\2",
    re.M)

# Как читать значение — решает ВИД локатора, а не форма строки (ошибка 1).
BARE_KINDS = {"ID", "NAME", "CLASS_NAME", "TAG_NAME", "LINK_TEXT", "PARTIAL_LINK_TEXT"}

CSS_TESTID = re.compile(r"\[\s*(data-testid|data-test-id|data-test|data-qa|data-cy)"
                        r"\s*=\s*(['\"]?)(.*?)\2\s*\]", re.I)
CSS_ID = re.compile(r"#([A-Za-z_][\w\-]*)")
CSS_NAME = re.compile(r"\[\s*name\s*=\s*(['\"]?)(.*?)\1\s*\]", re.I)
CSS_CLASS = re.compile(r"\.([A-Za-z_][\w\-]*)")

XPATH_TESTID = re.compile(r"@(data-testid|data-test-id|data-test|data-qa|data-cy)"
                          r"\s*=\s*(['\"])(.*?)\2", re.I)
XPATH_ID = re.compile(r"@id\s*=\s*(['\"])(.*?)\1")
XPATH_NAME = re.compile(r"@name\s*=\s*(['\"])(.*?)\1")
# Шаг по позиции: /div[3], //span[2], /*[1] — но НЕ [@id='x'] и не [contains(...)]
XPATH_POSITIONAL = re.compile(r"/[\w*]+\[\s*\d+\s*\]")

UNSTABLE = re.compile(
    r"^(?:sc-[0-9a-z]{6,}|css-[0-9a-z]{6,}|:r[0-9a-z]+:|[0-9a-f]{8,}"
    r"|mui-\d+|radix-[-:\w]+|headlessui-[-:\w]+)$", re.I)


def candidates(kind: str, value: str) -> tuple[list[str], list[str]]:
    """Из локатора — список handleId и список замечаний о форме якоря."""
    ids: list[str] = []
    notes: list[str] = []
    value = value.strip()
    if not value:
        return ids, notes

    if kind == "ID":
        ids.append(f"id:{value}")
        return ids, notes
    if kind == "NAME":
        ids.append(f"name:{value}")
        return ids, notes
    if kind == "CLASS_NAME":
        notes.append("class")
        return ids, notes
    if kind in ("TAG_NAME", "LINK_TEXT", "PARTIAL_LINK_TEXT"):
        # Тег и текст ссылки в инвентарь ручек не попадают: это не якорь-атрибут.
        return ids, notes

    if kind == "XPATH":
        if XPATH_POSITIONAL.search(value):
            notes.append("positional")
        for m in XPATH_TESTID.finditer(value):
            ids.append(f"testid:{m.group(3)}")
        for m in XPATH_ID.finditer(value):
            ids.append(f"id:{m.group(2)}")
        for m in XPATH_NAME.finditer(value):
            ids.append(f"name:{m.group(2)}")
        if not ids and not notes:
            notes.append("class")  # непроверяемая форма XPath
        return ids, notes

    # CSS_SELECTOR и всё остальное читаем как CSS.
    for m in CSS_TESTID.finditer(value):
        ids.append(f"testid:{m.group(3)}")
    for m in CSS_ID.finditer(value):
        ids.append(f"id:{m.group(1)}")
    for m in CSS_NAME.finditer(value):
        ids.append(f"name:{m.group(2)}")
    if not ids and CSS_CLASS.search(value):
        notes.append("class")
    return ids, notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ни один селектор в сгенерированном коде не выдуман")
    ap.add_argument("target", help="каталог с тестами или один файл")
    ap.add_argument("--inventory", default="output/ui/selectors.json")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    inv_path = Path(args.inventory)
    if not inv_path.is_file():
        sys.stderr.write(f"selectors: инвентаря нет: {inv_path}\n")
        sys.stderr.write("selectors: НЕ ПРОВЕРЕНО. Это не «прошло»: без инвентаря "
                         "выдуманный селектор неотличим от настоящего.\n")
        return 2
    try:
        inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"selectors: инвентарь нечитаем: {exc}\n")
        return 2

    known = {h.get("handleId"): h for h in inventory.get("handles", [])
             if isinstance(h, dict) and h.get("handleId")}
    if not known:
        sys.stderr.write("selectors: инвентарь пуст — сверять не с чем. НЕ ПРОВЕРЕНО.\n")
        return 2

    target = Path(args.target)
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted(target.rglob("test_*.py")) + sorted(target.rglob("data_*.py"))
    else:
        sys.stderr.write(f"selectors: цели нет: {target}\n")
        return 2
    if not files:
        sys.stderr.write(f"selectors: в {target} нет тестовых файлов — НЕ ПРОВЕРЕНО\n")
        return 2

    findings: list[dict] = []
    checked = 0

    def add(sev: str, rule: str, where: str, msg: str) -> None:
        findings.append({"severity": sev, "rule": rule, "where": where, "message": msg})

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path).replace("\\", "/")
        # Номер строки по смещению — без построчного разбора: локаторы бывают
        # многострочными, и разрезав файл по строкам мы теряем их середину.
        def lineno(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        occurrences: list[tuple[str, str, int]] = []
        for m in LOCATOR.finditer(text):
            occurrences.append((m.group(1).upper(), m.group(3), lineno(m.start())))
        for m in CONST.finditer(text):
            name, value = m.group(1), m.group(3)
            kind = "XPATH" if value.lstrip().startswith(("/", "(")) else (
                "ID" if name.endswith("_ID") else
                "NAME" if name.endswith("_NAME") else "CSS_SELECTOR")
            occurrences.append((kind, value, lineno(m.start())))

        for kind, value, ln in occurrences:
            checked += 1
            where = f"{rel}:{ln}"
            ids, notes = candidates(kind, value)
            if "positional" in notes:
                add("MAJOR", "selector-positional", where,
                    f"XPath по позиции: {value[:70]} — ломается от любой вставки в разметку")
            if not ids:
                if "class" in notes:
                    add("MAJOR", "selector-class", where,
                        f"якорь по классу или структуре: {value[:70]} — непроверяемо "
                        f"по инвентарю, это не «выдумано», но и не подтверждено")
                continue
            for hid in ids:
                if hid not in known:
                    add("BLOCKER", "selector-unknown", where,
                        f"«{hid.split(':', 1)[1]}» отсутствует в инвентаре — селектор "
                        f"выдуман. Требования такого элемента не называют")
                    continue
                handle = known[hid]
                if not handle.get("stable", True):
                    add("MAJOR", "selector-unstable", where,
                        f"«{handle.get('value')}» — хеш сборщика, переживёт одну пересборку")

    blockers = sum(1 for f in findings if f["severity"] == "BLOCKER")
    majors = sum(1 for f in findings if f["severity"] == "MAJOR")

    for f in findings:
        print(f"[selectors] {f['severity']:<7} {f['rule']:<20} {f['where']} — {f['message']}")

    tiers = {"A": 0, "B": 0}
    for h in known.values():
        tiers[h.get("tier", "A")] = tiers.get(h.get("tier", "A"), 0) + 1
    print(f"[selectors] файлов {len(files)}, локаторов {checked}, "
          f"инвентарь: тир A {tiers.get('A', 0)}, тир B {tiers.get('B', 0)}")
    print(f"[selectors] BLOCKER {blockers}, MAJOR {majors}")
    if tiers.get("A", 0) == 0 and tiers.get("B", 0):
        print("[selectors] инвентарь целиком на тире B: тесты описывают текущую "
              "сборку, а не обещание требований")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"blockers": blockers, "majors": majors, "locators": checked,
             "findings": findings}, ensure_ascii=False, indent=2), encoding="utf-8")

    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
