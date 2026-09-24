#!/usr/bin/env python3
"""Шлюз слоя 2: карта «шаг кейса → ручка интерфейса», до написания кода.

Карту пишет агент-картограф (`ui-cartographer`). Это единственный артефакт в
Фазе 2.5, который можно проверить механически ПЕРЕД тем, как написана хоть одна
строка теста, — и в этом весь смысл разделения. Раньше стадия писателей была
единственной, где «не выдумывать поведение» не проверял никто: у дизайнера есть
`validate_cases.py` до семантического ревью, у писателя не было ничего.

Что проверяется
---------------
handle-unknown   BLOCKER  ручки нет в инвентаре — то есть она выдумана
step-unmapped    BLOCKER  шаг не привязан и не объявлен пробелом (молчание)
expectation-leak BLOCKER  в карте появилось ожидание, а не адресация
handle-unstable  MAJOR    выбран хеш сборщика (`sc-a1b2c3`) — он переживёт одну сборку
anchor-weak      MAJOR    выбран слабый якорь при наличии сильного для того же шага
tier-b-only      MINOR    привязка держится на наблюдении, а не на контракте

Почему «expectation-leak» — это BLOCKER, а не придирка
------------------------------------------------------
Если карта снята с живой страницы и она же несёт ожидание, тест проходит по
построению: элемент найден, потому что его оттуда и взяли. Ровно этот дефект уже
лежит в готовых сьютах — `test_phone_input_visible_and_active` логинится, делает
скриншот и не содержит ни одного `assert`. Разведка поставляет адресацию;
что элемент ДОЛЖЕН делать, приходит из кейса, то есть из требований.

Fail-closed без fail-random
---------------------------
Нет инвентаря или нет карты — код 2, «НЕ ПРОВЕРЕНО», никогда «прошло». Отдельный
исход нужен потому, что `req-id-unknown` выше по течению однажды уже проверился
против устаревшего `_index.json` от другого корпуса и отчитался зелёным.

Коды выхода: 0 — блокеров нет, 1 — блокеры есть, 2 — проверка не состоялась.
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

# Поля, которых в карте быть не должно: это ожидания, а не адресация.
EXPECTATION_KEYS = {
    "expected", "expect", "assert", "assertion", "shouldBe", "should",
    "ожидание", "ожидается", "результат", "expectedResult", "visible",
    "enabled", "text", "value", "state",
}

ANCHOR_STRENGTH = {"testid": 4, "id": 3, "name": 2, "aria": 1}


def load(path: Path, label: str) -> dict | None:
    if not path.is_file():
        sys.stderr.write(f"ui-map: нет {label}: {path}\n")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ui-map: {label} нечитаем ({path}): {exc}\n")
        return None


def case_steps(cases_dir: Path) -> dict[str, int]:
    """Сколько шагов в каждом кейсе — по JSON, который теперь первичен."""
    out: dict[str, int] = {}
    for p in sorted(cases_dir.glob("TC-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        steps = data.get("steps")
        if isinstance(steps, list):
            out[p.stem] = len(steps)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка карты «шаг → ручка» до написания кода")
    ap.add_argument("--journey", required=True, help="JOURNEY_ID")
    ap.add_argument("--map", dest="map_path", help="по умолчанию output/ui/<JOURNEY_ID>-map.json")
    ap.add_argument("--inventory", default="output/ui/selectors.json")
    ap.add_argument("--cases-dir", help="по умолчанию output/cases/<JOURNEY_ID>")
    ap.add_argument("--json", dest="json_out", help="куда записать findings")
    args = ap.parse_args()

    jid = args.journey
    map_path = Path(args.map_path or f"output/ui/{jid}-map.json")
    inv_path = Path(args.inventory)
    cases_dir = Path(args.cases_dir or f"output/cases/{jid}")

    inventory = load(inv_path, "инвентарь селекторов")
    if inventory is None:
        sys.stderr.write("ui-map: НЕ ПРОВЕРЕНО. Без инвентаря карта непроверяема — "
                         "это не «прошло».\n")
        return 2
    ui_map = load(map_path, "карта journey")
    if ui_map is None:
        sys.stderr.write("ui-map: НЕ ПРОВЕРЕНО — картограф не отработал или писал не туда.\n")
        return 2

    known = {h.get("handleId"): h for h in inventory.get("handles", [])
             if isinstance(h, dict) and h.get("handleId")}
    if not known:
        sys.stderr.write("ui-map: инвентарь пуст — проверять карту не против чего. "
                         "НЕ ПРОВЕРЕНО.\n")
        return 2

    entries = ui_map.get("entries")
    if not isinstance(entries, list):
        sys.stderr.write("ui-map: в карте нет массива «entries» — структура не та\n")
        return 2

    findings: list[dict] = []

    def add(sev: str, rule: str, where: str, msg: str) -> None:
        findings.append({"severity": sev, "rule": rule, "where": where, "message": msg})

    # Сильнейший якорь, доступный в пределах одного шага, — для anchor-weak.
    per_step_best: dict[tuple[str, object], int] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        hid = e.get("handleId")
        if hid and hid in known:
            key = (str(e.get("case")), e.get("step"))
            strength = ANCHOR_STRENGTH.get(known[hid].get("kind", ""), 0)
            per_step_best[key] = max(per_step_best.get(key, 0), strength)

    mapped: dict[str, set] = {}
    for i, e in enumerate(entries):
        where = f"entry[{i}]"
        if not isinstance(e, dict):
            add("BLOCKER", "step-unmapped", where, "запись карты не является объектом")
            continue
        case = e.get("case")
        step = e.get("step")
        where = f"{case}, шаг {step}"
        hid = e.get("handleId")
        gap = e.get("gap")

        leaked = sorted(EXPECTATION_KEYS & {k for k in e})
        if leaked:
            add("BLOCKER", "expectation-leak", where,
                f"карта несёт ожидание, а не адресацию: поля {', '.join(leaked)}. "
                f"Что элемент должен делать, берётся из кейса, иначе тест проходит "
                f"по построению")

        if hid is None and not gap:
            add("BLOCKER", "step-unmapped", where,
                "шаг не привязан к ручке и не объявлен пробелом — молчание здесь "
                "неотличимо от «проверять нечего»")
            continue
        if hid is None:
            continue  # объявленный пробел — законный исход

        if hid not in known:
            add("BLOCKER", "handle-unknown", where,
                f"ручки «{hid}» нет в инвентаре — она выдумана. "
                f"Соберите инвентарь заново или объявите пробел")
            continue

        handle = known[hid]
        if case:
            mapped.setdefault(str(case), set()).add(step)
        if not handle.get("stable", True):
            add("MAJOR", "handle-unstable", where,
                f"«{handle.get('value')}» — хеш сборщика, он не переживёт пересборку")
        strength = ANCHOR_STRENGTH.get(handle.get("kind", ""), 0)
        best = per_step_best.get((str(case), step), 0)
        if strength < best:
            add("MAJOR", "anchor-weak", where,
                f"выбран якорь «{handle.get('kind')}», хотя для этого шага есть более "
                f"устойчивый")
        if handle.get("tier") == "B":
            add("MINOR", "tier-b-only", where,
                f"привязка держится на наблюдении живой страницы, а не на контракте "
                f"из исходников")

    # Шаги кейсов, о которых карта не сказала ничего.
    if cases_dir.is_dir():
        for case_id, n_steps in sorted(case_steps(cases_dir).items()):
            seen = mapped.get(case_id, set())
            declared = {e.get("step") for e in entries
                        if isinstance(e, dict) and str(e.get("case")) == case_id}
            for step_no in range(1, n_steps + 1):
                if step_no not in seen and step_no not in declared:
                    add("BLOCKER", "step-unmapped", f"{case_id}, шаг {step_no}",
                        "шаг кейса отсутствует в карте — ни привязки, ни пробела")
    else:
        add("MAJOR", "step-unmapped", str(cases_dir),
            "каталог кейсов не найден — полноту карты проверить не удалось")

    blockers = sum(1 for f in findings if f["severity"] == "BLOCKER")
    majors = sum(1 for f in findings if f["severity"] == "MAJOR")
    minors = sum(1 for f in findings if f["severity"] == "MINOR")

    for f in findings:
        print(f"[ui-map] {f['severity']:<7} {f['rule']:<16} {f['where']} — {f['message']}")

    tier_b = sum(1 for e in entries if isinstance(e, dict)
                 and e.get("handleId") in known and known[e["handleId"]].get("tier") == "B")
    gaps = sum(1 for e in entries if isinstance(e, dict) and e.get("gap"))
    print(f"[ui-map] {jid}: записей {len(entries)}, пробелов {gaps}, "
          f"привязок на тире B {tier_b}")
    print(f"[ui-map] BLOCKER {blockers}, MAJOR {majors}, MINOR {minors}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"journeyId": jid, "blockers": blockers, "majors": majors, "minors": minors,
             "findings": findings}, ensure_ascii=False, indent=2), encoding="utf-8")

    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
