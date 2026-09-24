#!/usr/bin/env python3
"""Check how the analyst cut the corpus into journeys, before any case is written.

Three runs made the case for this. Runs 2 and 3 were given the *same* corpus and cut it
into 3 journeys of 7 stages and into 1 journey of 12 — so the decomposition is not a
property of the requirements, it is a decision that varies run to run. And the shape of
that decision dominates wall clock: the 1×12 cut put 9 cases on one designer for 27:27,
while 3×7 ran three designers of ~15 minutes each, in parallel.

The analyst's rules are all floors — «at least 5 stages», «at least 3 areas» — with no
ceiling and no balance requirement, even though the same spec states that one agent owns
one journey. This script supplies the missing half and shows it to the human at the
requirements gate, where a regrouping still costs nothing.

Nothing here is a blocker. A cut can be lopsided for a good reason, and the escape is
named in the output rather than argued with: a journey may exceed the ceiling when it
cannot be split at a point where the user has achieved a goal and the later part inherits
no state from the earlier one — splitting there would create exactly the cross-journey
dependency the architecture forbids.

Usage:
    python3 scripts/validate_plans.py
    python3 scripts/validate_plans.py --json output/gate/plans.json

Exit codes: 0 = nothing to flag, 1 = suspicions to show the human, 2 = no index.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Floor: below this a "journey" is feature testing in disguise. 5 is also what the
# linter's 8-step minimum implies at the lowest step density we have measured (1.7
# steps per stage in an API domain): 5 x 1.7 clears 8, 4 x 1.7 does not.
MIN_STAGES = 5
# Ceiling: 8 stages at the observed 2.9 steps/stage is a ~23-step main case, about the
# 15-minute designer we are comfortable with. Soft — see the module docstring.
#
# CONTRADICTION, unresolved on purpose. validate_cases.MAX_STEPS_PER_STAGE = 2 calls
# any density above 2.0 a finding, while the sentence above justifies this ceiling by
# assuming 2.9. Both were written the same day and they cannot both be right.
#
# Run 12 showed which way it bites: on a UI corpus two of three plans landed on 7-8
# stages — this ceiling — and their main cases then tripped the step rule at 2.29 and
# 2.50. The bundled stages were the cause, and this ceiling is what pushes an analyst
# to bundle. The Звук reference in examples/ declares twelve stages for nineteen steps
# and is the shape both rules would accept; it predates this ceiling.
#
# Do not resolve by guessing a new number. What decides it is which quantity actually
# tracks designer time and review cost — stages or steps — and that is measurable from
# the logs we now collect. See TODO, «Противоречие двух потолков».
MAX_STAGES = 8
MIN_AREAS = 3
# Ratios above which the journeys of one run are uneven enough to waste parallelism.
STAGE_SPREAD = 2.0
VARIANT_SPREAD = 3.0

# Сколько времени дизайнера стоит journey. Замер по восьми вызовам дизайнера
# прогонов 7–13 (result.md § 16): время держится на кейсах, а не на этапах.
#
#   единица   корреляция   удельное время   коэффициент вариации
#   кейсы        +0.90        141 с/кейс           19 %
#   шаги         +0.84         27 с/шаг            27 %
#   этапы        см. ниже      96 с/этап           34 %
#
# По этапам корреляцию считать нельзя: семь наблюдений из восьми — ровно 8 этапов,
# потому что MAX_STAGES прижал к потолку все планы. Величина без разброса не годится
# основанием для бюджета, и это главный вывод замера: считать нужно от кейсов.
SECONDS_PER_CASE = 141
# Кейсов на одного дизайнера, выше которых journey стоит резать или раздавать
# вариантных писателей (O-15). 10 кейсов ≈ 23 минуты одного агента: столько же —
# окно, в котором сбой стоит дороже всего, а расход ходов подбирается к лимиту.
# Наблюдалось 3, 4, 4, 4, 6, 7, 7, 9 — порог выше всех с запасом, как и положено
# потолку: он страхует от разрастания, а не навязывает размер.
MAX_CASES = 10


def check(index: Path) -> tuple[list[dict], list[dict], list[str]]:
    """(findings derived here, warnings passed through from the analyst, one-line shapes)."""
    data = json.loads(index.read_text(encoding="utf-8"))
    journeys = data.get("journeys", [])
    findings: list[dict] = []
    passed: list[dict] = []
    notes: list[str] = []

    if not journeys:
        return [{"scope": "прогон", "rule": "no-journeys",
                 "text": "в индексе нет ни одного journey"}], passed, notes

    for j in journeys:
        jid = str(j.get("id", "?"))
        stages = j.get("stages")
        areas = len(j.get("areas") or [])
        variants = len(j.get("variants") or [])

        if isinstance(stages, int):
            if stages < MIN_STAGES:
                findings.append({"scope": jid, "rule": "stages-below-floor",
                                 "text": f"{stages} этапов, минимум {MIN_STAGES} — "
                                         f"это проверка функции, а не сквозной путь; "
                                         f"вложить в больший journey этапом"})
            elif stages > MAX_STAGES:
                findings.append({"scope": jid, "rule": "stages-above-ceiling",
                                 "text": f"{stages} этапов при потолке {MAX_STAGES} — "
                                         f"вся работа ляжет на одного дизайнера. Разрезать "
                                         f"по границе достигнутой цели, где вторая часть не "
                                         f"наследует состояние первой; если такой границы "
                                         f"нет — оставить и объяснить в warnings"})
        if areas and areas < MIN_AREAS:
            findings.append({"scope": jid, "rule": "areas-below-floor",
                             "text": f"{areas} функциональных областей, минимум {MIN_AREAS} — "
                                     f"однообластной путь не journey"})
        if j.get("stagesWithoutReq"):
            findings.append({"scope": jid, "rule": "stages-without-req",
                             "text": f"{j['stagesWithoutReq']} этап(ов) без якоря REQ"})
        # The analyst's own `warnings` are its voice, not this script's finding. The spec
        # asks for structural problems there, but analysts also use it to explain why
        # something unusual is in fact correct — counting those as suspicions would
        # inflate the number the human is asked to judge.
        for w in (j.get("warnings") or []):
            passed.append({"scope": jid, "rule": "analyst-warning", "text": str(w)})
        # Кейсов у journey — основной плюс по одному на вариант.
        cases = variants + 1
        minutes = cases * SECONDS_PER_CASE / 60
        if cases > MAX_CASES:
            findings.append({"scope": jid, "rule": "cases-above-ceiling",
                             "text": f"кейсов {cases} при потолке {MAX_CASES} — "
                                     f"около {minutes:.0f} мин одного дизайнера. "
                                     "Разрезать journey либо раздать вариантных писателей"})
        notes.append(f"{jid}: этапов {stages}, областей {areas}, вариантов {variants}, "
                     f"кейсов {cases} ≈ {minutes:.0f} мин дизайнера")

    if len(journeys) > 1:
        st = [j["stages"] for j in journeys if isinstance(j.get("stages"), int)]
        if st and min(st) > 0 and max(st) / min(st) > STAGE_SPREAD:
            findings.append({"scope": "прогон", "rule": "uneven-stages",
                             "text": f"этапы разошлись {max(st)} против {min(st)} "
                                     f"(>{STAGE_SPREAD:g}x) — быстрые journey будут ждать "
                                     f"медленный, фора конвейера теряется"})
        vr = [len(j.get("variants") or []) for j in journeys]
        if vr and min(vr) > 0 and max(vr) / min(vr) > VARIANT_SPREAD:
            findings.append({"scope": "прогон", "rule": "uneven-variants",
                             "text": f"варианты разошлись {max(vr)} против {min(vr)} — "
                                     f"нагрузка дизайнеров неравномерна"})
    return findings, passed, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверить, как аналитик разрезал корпус")
    ap.add_argument("--index", default="output/suites/_index.json")
    ap.add_argument("--json", dest="json_out", help="куда записать находки")
    args = ap.parse_args()

    index = Path(args.index)
    if not index.is_file():
        sys.stderr.write(f"validate_plans: нет индекса аналитика: {index}\n")
        return 2
    try:
        findings, passed, notes = check(index)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"validate_plans: {index} не парсится: {exc}\n")
        return 2

    for n in notes:
        print(f"[plans] {n}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"findings": findings, "analystWarnings": passed,
                                   "journeys": notes},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

    if findings:
        print(f"[plans] структурных подозрений: {len(findings)}")
        for f in findings:
            print(f"[plans]   {f['scope']} — {f['text']}")
    else:
        print("[plans] нарезка вопросов не вызывает")

    if passed:
        print(f"[plans] предупреждений самого аналитика: {len(passed)} — передаю как есть")
        for w in passed:
            print(f"[plans]   {w['scope']} — {w['text']}")

    if findings or passed:
        print("[plans] это подозрения о группировке, не факты покрытия — "
              "решение принимает человек на шлюзе требований")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
