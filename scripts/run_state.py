#!/usr/bin/env python3
"""The orchestrator's own progress store: which phase, which journeys are in flight.

Everything else about a run is already on disk. `output/state/<J>.json` carries each
journey's iteration and the critic's verdict, `output/cases/` carries its artefacts.
Two things were held only in the orchestrator's context and vanished with it: **the
phase** and **the split of journeys into the active batch and the queue**. This file
holds exactly those two, and nothing that another file already owns.

It is also the only place the order of the phases is checked. `set --phase` refuses a
transition the procedure does not describe: a mandatory phase stepped over, a backward
step that is not one of the two the procedure has, or entering a phase that requires an
empty batch while journeys are still in flight. Refusing writes nothing, so a refused
transition leaves the store on the last phase that actually happened.

It asks nothing, so the closed question registry still has nine entries.

Tied to `output/.run`: a store stamped with another `runId` is a leftover, not state, and
is refused rather than read. `start_run.py` drops it when stamping a new run.

Usage:
    python3 scripts/run_state.py set --phase 2 --active J01,J03 --queue J05
    python3 scripts/run_state.py set --active J03 --queue ""     # batch only, same phase
    python3 scripts/run_state.py done J01-registration --verdict PASS
    python3 scripts/run_state.py stop --reason "шлюз требований: остановить прогон"
    python3 scripts/run_state.py show

Exit codes:
    0 = recorded / store consistent with output/state/
    1 = refused: illegal phase transition, or `show` finding the store out of step
        with output/state/. Nothing is written.
    2 = cannot check: no run in progress, no store, unknown phase or verdict
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

OUT = Path("output")
RUN_FILE = OUT / ".run"
STORE = OUT / ".run-state.json"
STATE_DIR = OUT / "state"

PHASES = {
    "0": "разбор аргументов",
    "1": "анализ требований",
    "1.5": "шлюз требований",
    "2": "циклы дизайн/ревью",
    "2.5": "генерация pytest-тестов",
    "2.6": "публикация в Allure TestOps",
    "3": "шлюз покрытия",
    "4": "отчёт",
}

ORDER = list(PHASES)

# Phases the procedure cannot do without. 2.5 and 2.6 are optional by flag, so a run may
# step over them; stepping over any of these is a skipped phase, not a shortcut.
MANDATORY = {"0", "1", "1.5", "2", "3", "4"}

# The only two backward steps the procedure itself describes:
#   1.5 -> 1   requirements-gate, «отвечу на вопросы сейчас»: the analyst is re-run
#   3   -> 2   the coverage gate's «прогнать заново» for the affected journeys
BACK_EDGES = {("1.5", "1"), ("3", "2")}

# Phases that may not be entered while journeys are still in flight. This is the prose
# rule «выполняется только когда батч и очередь пусты», made checkable.
NEEDS_EMPTY_BATCH = {"2.5", "2.6", "3", "4"}

# Orchestrator verdicts. PASS and FIX_REQUIRED come from the critic; a journey reaches
# this store only once the orchestrator has decided it leaves the batch.
VERDICTS = ("PASS", "NEEDS_HUMAN", "FAILED")


def phase_label(phase: str | None) -> str:
    return "%s — %s" % (phase, PHASES.get(phase, "?")) if phase else "не отмечена"


def check_transition(current: str | None, target: str) -> list[str]:
    """Problems with going from `current` to `target`. Empty list means allowed."""
    if current is None:
        if target not in ("0", "1"):
            return ["прогон открывается фазой 0 или 1, а не %s: "
                    "предыдущие фазы не отмечены" % target]
        return []
    if current == target:
        return []

    here, there = ORDER.index(current), ORDER.index(target)
    if there < here:
        if (current, target) in BACK_EDGES:
            return []
        allowed = ", ".join("%s → %s" % edge for edge in sorted(BACK_EDGES))
        return ["возврат с фазы %s на %s процедурой не предусмотрен; "
                "назад можно только: %s" % (current, target, allowed)]

    skipped = [p for p in ORDER[here + 1:there] if p in MANDATORY]
    if skipped:
        return ["между фазами %s и %s пропущены обязательные: %s"
                % (current, target, ", ".join(skipped))]
    return []


def check_batch_empty(data: dict, target: str) -> list[str]:
    if target not in NEEDS_EMPTY_BATCH:
        return []
    problems = []
    if data["active"]:
        problems.append("активный батч не пуст: %s" % ", ".join(data["active"]))
    if data["queue"]:
        problems.append("очередь не пуста: %s" % ", ".join(data["queue"]))
    return problems


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run_id() -> str | None:
    run = read_json(RUN_FILE)
    return run.get("runId") if isinstance(run, dict) else None


def load(rid: str) -> dict | None:
    """Read the store, refusing one that belongs to a different run."""
    data = read_json(STORE)
    if not isinstance(data, dict):
        return None
    if data.get("runId") != rid:
        return None
    return data


def blank(rid: str) -> dict:
    return {"runId": rid, "phase": None, "active": [], "queue": [], "done": {},
            "note": "", "updated": None}


def save(data: dict) -> None:
    data["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    OUT.mkdir(exist_ok=True)
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")


def split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def cmd_set(args, rid: str) -> int:
    data = load(rid) or blank(rid)

    if args.phase is None:
        # Batch bookkeeping inside the phase already recorded.
        for key in ("active", "queue"):
            value = split(getattr(args, key))
            if value is not None:
                data[key] = value
        if args.note is not None:
            data["note"] = args.note
        save(data)
        print("[state] фаза %s" % phase_label(data["phase"]))
        print("[state] батч: %s | очередь: %s"
              % (", ".join(data["active"]) or "—", ", ".join(data["queue"]) or "—"))
        return 0

    if args.phase not in PHASES:
        sys.stderr.write("run_state: фаза %r не существует; известны: %s\n"
                         % (args.phase, ", ".join(PHASES)))
        return 2
    if data.get("stopped"):
        sys.stderr.write("run_state: прогон остановлен на фазе %s (%s) — "
                         "новых фаз у него не будет\n"
                         % (data["phase"], data.get("note") or "без причины"))
        return 1

    # The batch state that will hold once this call's --active/--queue are applied:
    # a call that both empties the batch and moves on is one legal action, not two.
    planned = dict(data)
    for key in ("active", "queue"):
        value = split(getattr(args, key))
        if value is not None:
            planned[key] = value

    problems = (check_transition(data.get("phase"), args.phase)
                + check_batch_empty(planned, args.phase))
    if problems:
        sys.stderr.write("run_state: переход %s → %s отклонён\n"
                         % (phase_label(data.get("phase")), phase_label(args.phase)))
        for p in problems:
            sys.stderr.write("  ⛔ %s\n" % p)
        sys.stderr.write("Ничего не записано. Выполните пропущенное, "
                         "а не отмечайте фазу задним числом.\n")
        return 1

    data = planned
    previous = data.get("phase")
    data["phase"] = args.phase
    if args.note is not None:
        data["note"] = args.note
    data.setdefault("history", []).append(
        {"phase": args.phase,
         "at": datetime.now().astimezone().isoformat(timespec="seconds")})
    save(data)

    arrow = "" if previous is None else "%s → " % previous
    print("[state] %sфаза %s" % (arrow, phase_label(args.phase)))
    if data["active"] or data["queue"]:
        print("[state] батч: %s | очередь: %s"
              % (", ".join(data["active"]) or "—", ", ".join(data["queue"]) or "—"))
    return 0


def cmd_stop(args, rid: str) -> int:
    """The run ends before phase 4 — the coverage branch «остановить прогон»."""
    data = load(rid) or blank(rid)
    data["stopped"] = True
    data["note"] = args.reason
    save(data)
    print("[state] прогон остановлен на фазе %s: %s"
          % (phase_label(data["phase"]), args.reason))
    return 0


def cmd_done(args, rid: str) -> int:
    if args.verdict not in VERDICTS:
        sys.stderr.write("run_state: вердикт %r; допустимо %s\n"
                         % (args.verdict, ", ".join(VERDICTS)))
        return 2
    data = load(rid) or blank(rid)
    data["done"][args.journey] = args.verdict
    data["active"] = [j for j in data["active"] if j != args.journey]
    data["queue"] = [j for j in data["queue"] if j != args.journey]
    save(data)
    print("[state] %s → %s   (батч: %s | очередь: %s)"
          % (args.journey, args.verdict,
             ", ".join(data["active"]) or "—", ", ".join(data["queue"]) or "—"))
    return 0


def cmd_show(args, rid: str) -> int:
    data = load(rid)
    if data is None:
        sys.stderr.write("run_state: хранилища для прогона %s нет — "
                         "прогресс не записывался\n" % rid)
        return 2

    print("Прогон %s, обновлён %s" % (data["runId"], data.get("updated")))
    print("Фаза %s%s" % (phase_label(data.get("phase")),
                         "  [ОСТАНОВЛЕН]" if data.get("stopped") else ""))
    passed = [h["phase"] for h in data.get("history") or []]
    if passed:
        print("Пройдено:      %s" % " → ".join(passed))
    print("Активный батч: %s" % (", ".join(data["active"]) or "—"))
    print("Очередь:       %s" % (", ".join(data["queue"]) or "—"))
    if data["done"]:
        print("Завершено:")
        for jid, verdict in sorted(data["done"].items()):
            print("  %-44s %s" % (jid, verdict))
    if data.get("note"):
        print("Заметка: %s" % data["note"])

    # The store is a record, not a source of truth: output/state/ is. Divergence means
    # the run moved on without recording it, and the store must not be resumed from.
    problems = []
    if STATE_DIR.is_dir():
        on_disk = {p.stem for p in STATE_DIR.glob("*.json")}
        tracked = set(data["active"]) | set(data["queue"]) | set(data["done"])
        for jid in sorted(on_disk - tracked):
            problems.append("journey %s имеет состояние в %s/, но в хранилище его нет"
                            % (jid, STATE_DIR.as_posix()))
        for jid in sorted(set(data["done"]) - on_disk):
            problems.append("journey %s помечен завершённым, но состояния в %s/ нет"
                            % (jid, STATE_DIR.as_posix()))
    if problems:
        print("\nХранилище разошлось с диском:")
        for p in problems:
            print("  ⚠️ %s" % p)
        print("Возобновлять по нему нельзя — пересоберите картину по output/state/.")
        return 1

    print("\nХранилище сходится с output/state/.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Хранилище прогресса прогона /e2e:run")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="записать фазу и состав батча")
    s.add_argument("--phase", help="номер фазы: " + ", ".join(PHASES))
    s.add_argument("--active", help="journey активного батча через запятую")
    s.add_argument("--queue", help="journey в очереди через запятую")
    s.add_argument("--note", help="одна строка свободного контекста")

    d = sub.add_parser("done", help="journey покинул батч с вердиктом оркестратора")
    d.add_argument("journey", help="полный id journey")
    d.add_argument("--verdict", required=True, help=" | ".join(VERDICTS))

    t = sub.add_parser("stop", help="прогон завершён раньше фазы 4")
    t.add_argument("--reason", required=True, help="почему прогон остановлен")

    sub.add_parser("show", help="показать, где стоит прогон")

    args = ap.parse_args()

    rid = run_id()
    if rid is None:
        sys.stderr.write("run_state: прогон не начат — нет output/.run. "
                         "Сначала scripts/start_run.py\n")
        return 2

    return {"set": cmd_set, "done": cmd_done, "stop": cmd_stop,
            "show": cmd_show}[args.cmd](args, rid)


if __name__ == "__main__":
    sys.exit(main())
