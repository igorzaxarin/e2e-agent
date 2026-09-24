#!/usr/bin/env python3
"""Decide what a fix iteration actually has to re-review.

On iteration 2+ the critic re-runs five passes and twelve criteria over *every* case,
including the ones nobody touched. With the usual "one blocker in one case out of five"
that is four fifths of the review stage spent re-confirming what was already confirmed.

This script narrows the scope — and, more importantly, narrows it *safely*. Skipping the
wrong case would silently weaken the gate the whole project rests on, so the rule is
fail-closed: anything it cannot prove unchanged goes back for a full review.

Invalidation rules
------------------
run id changed or absent        -> everything (a baseline from another run proves
                                   nothing about this one; absence means unknown)
requirements / answers changed  -> everything (behaviour was redefined)
suite plan changed              -> everything (the journey's scope moved)
rubric / criteria / format      -> everything (the rules of judgement moved)
main case changed               -> the main case AND every variant
                                   (variants inherit its preconditions — this is the
                                    trap a naive per-file hash diff would walk into)
variant changed                 -> that variant only
case disappeared                -> everything (the journey's scope shrank; a
                                   deletion is a change, and counting only the
                                   surviving files would read it as "nothing moved")
no baseline yet                 -> everything (first iteration)

Nothing changed at all
----------------------
A fix iteration whose designer produced a byte-identical tree is a special case, not
an empty scope. The critic would re-derive the verdict it already gave — a full
semantic pass, measured at 4:39-8:25 — and reach it again, because the input to that
judgement did not move. So this exits **3** and the journey goes to NEEDS_HUMAN.

This can only fire on a fix iteration: iteration 1 has no baseline, which is a global
invalidation and therefore a full review. And it is a *fact*, not a self-report — the
designer saying "I changed nothing" is a claim by a model, whereas SHA-256 over the
case tree is checkable.

Usage:
    python3 scripts/review_scope.py --journey J01-slug
    python3 scripts/review_scope.py --journey J01-slug --update   # stamp after review

Exit codes: 0 = scope written, 2 = journey/case directory missing,
            3 = designer changed nothing (do not call the critic).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Changing any of these re-opens every case: they define behaviour or how it is judged.
# output/.run is here so a baseline cannot outlive the run that produced it — see
# scripts/start_run.py. Its absence is handled separately, and also means full review.
RUN_FILE = "output/.run"

# The corpus is NOT listed here: which files hold the requirements is a property of
# the run, not of the repository. A hardcoded input/requirements silently skips any
# corpus outside it — see corpus_inputs().
GLOBAL_INPUTS = [
    RUN_FILE,
    "docs/critic-rubric.md",
    "docs/quality-criteria.md",
    "docs/format.md",
]

INDEX_FILE = "output/suites/_index.json"
CORPUS_FALLBACK = "input/requirements"

MAIN_CASE = re.compile(r"TC-J\d{2}-00$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def hash_tree(target: Path) -> dict[str, str]:
    """Hash a file, or every .md/.json under a directory."""
    if target.is_file():
        return {str(target): digest(target)}
    if not target.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.suffix in (".md", ".json"):
            out[str(p)] = digest(p)
    return out


def current_run_id() -> str | None:
    """Read the run id verbatim, so invalidation does not rest on a hash alone."""
    path = Path(RUN_FILE)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("runId")
    except json.JSONDecodeError:
        return None


def corpus_inputs() -> list[str]:
    """Where this run's requirements actually live, per the analyst's own index.

    A run against input/requirements-knox/ used to hash input/requirements/ — so
    editing the corpus between iterations changed nothing, and findings carried over
    as confirmed while the behaviour under them had been redefined. That is the exact
    fail-open this mechanism exists to prevent, so an unreadable index falls back to
    the default corpus path rather than to nothing.
    """
    path = Path(INDEX_FILE)
    if not path.is_file():
        return [CORPUS_FALLBACK]
    try:
        sources = json.loads(path.read_text(encoding="utf-8")).get("requirementsSource")
    except json.JSONDecodeError:
        return [CORPUS_FALLBACK]
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list) or not sources:
        return [CORPUS_FALLBACK]
    return [str(s) for s in sources if isinstance(s, str) and s]


# --- carried findings extraction -------------------------------------------

def _extract_carried_findings(jid: str) -> list[dict]:
    """Extract unfixed findings from the previous state.json.

    Reads output/state/<JID>.json, finds the previous iteration's state,
    extracts carriedFindings with status "unfixed", and returns them.
    Returns empty list if no previous state exists or is unreadable.
    """
    state_path = Path(f"output/state/{jid}.json")
    if not state_path.is_file():
        return []

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    # We need the PREVIOUS iteration's state. Since the current state may
    # already have been written by the critic on this run, we look for the
    # review path and check if there's an older state.
    # In practice, on iteration 2+ the state.json already has carriedFindings
    # from the previous iteration (written by the critic at the end of iter N-1).
    # We just need to filter out items that were "fixed".

    prev_carried = state.get("carriedFindings", [])
    if not prev_carried:
        return []

    # Filter: keep only unfixed items (fixed items were resolved by qa-designer)
    unfixed = [f for f in prev_carried if f.get("status") == "unfixed"]
    return unfixed


def _try_parse_review_markdown(jid: str) -> list[dict]:
    """Fallback: parse the previous review markdown for unfixed findings.

    If state.json doesn't have carriedFindings yet (legacy format),
    try to extract from the markdown review of the previous iteration.
    Returns empty list if parsing fails.
    """
    # Find the previous iteration's review
    reviews_dir = Path("output/reviews")
    if not reviews_dir.is_dir():
        return []

    # Pattern: <jid>-iter<N>.md, find the highest N < current
    # We don't know current iteration yet, so try iter1, iter2, ...
    prev_findings = []
    for n in range(20, 0, -1):
        review_path = reviews_dir / f"{jid}-iter{n}.md"
        if review_path.is_file():
            try:
                text = review_path.read_text(encoding="utf-8")
                prev_findings = _parse_findings_from_markdown(text, n)
            except Exception:
                pass
            break  # Take only the latest review

    return prev_findings


def global_fingerprint() -> dict[str, str]:
    fp: dict[str, str] = {}
    for entry in GLOBAL_INPUTS + corpus_inputs():
        fp.update(hash_tree(Path(entry)))
    return fp


def case_fingerprints(case_dir: Path) -> dict[str, str]:
    """One hash per case id, covering its Markdown and its JSON twin together."""
    cases: dict[str, list[str]] = {}
    for p in sorted(case_dir.glob("TC-*")):
        if p.suffix not in (".md", ".json"):
            continue
        cases.setdefault(p.stem, []).append(digest(p))
    return {cid: hashlib.sha256("".join(h).encode()).hexdigest()[:16]
            for cid, h in sorted(cases.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description="Что нужно переревьюить на этой итерации")
    ap.add_argument("--journey", required=True, help="JOURNEY_ID, например J01-registration-…")
    ap.add_argument("--cases-dir", help="по умолчанию output/cases/<JOURNEY_ID>")
    ap.add_argument("--plan", help="по умолчанию output/suites/<JOURNEY_ID>.md")
    ap.add_argument("--baseline", help="по умолчанию output/reviews/<JOURNEY_ID>-hashes.json")
    ap.add_argument("--update", action="store_true",
                    help="записать текущие хеши как базу (делать ПОСЛЕ успешного ревью)")
    ap.add_argument("--json", dest="json_out", help="куда записать решение об объёме")
    args = ap.parse_args()

    jid = args.journey
    case_dir = Path(args.cases_dir or f"output/cases/{jid}")
    plan = Path(args.plan or f"output/suites/{jid}.md")
    baseline_path = Path(args.baseline or f"output/reviews/{jid}-hashes.json")

    if not case_dir.is_dir():
        sys.stderr.write(f"review_scope: нет каталога кейсов: {case_dir}\n")
        return 2

    current = {
        "journeyId": jid,
        "runId": current_run_id(),
        "global": global_fingerprint(),
        "plan": digest(plan) if plan.is_file() else None,
        "cases": case_fingerprints(case_dir),
    }
    if not current["cases"]:
        sys.stderr.write(f"review_scope: в {case_dir} нет кейсов TC-*\n")
        return 2

    baseline = None
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            baseline = None  # fail closed: unreadable baseline means full review

    all_cases = sorted(current["cases"])
    reason_global: str | None = None

    if not Path(RUN_FILE).is_file():
        reason_global = ("нет идентификатора прогона (output/.run) — "
                         "неизвестно, к какому прогону относится база")
    elif baseline is None:
        reason_global = "базы нет — первая итерация или база нечитаема"
    elif baseline.get("runId") != current["runId"]:
        reason_global = (f"база от другого прогона ({baseline.get('runId')}), "
                         f"текущий {current['runId']}")
    elif baseline.get("global") != current["global"]:
        reason_global = "изменились требования или правила оценки"
    elif baseline.get("plan") != current["plan"]:
        reason_global = "изменился план сьюты"
    elif [c for c in baseline.get("cases", {}) if c not in current["cases"]]:
        # Кейс исчез. Считать только уцелевшие файлы нельзя: удаление — это изменение,
        # а набор совпавших хешей выглядел бы как «дизайнер ничего не тронул» и уводил
        # journey в NEEDS_HUMAN вместо ревью сузившегося объёма.
        gone = sorted(c for c in baseline.get("cases", {}) if c not in current["cases"])
        reason_global = "исчез кейс: " + ", ".join(Path(g).name for g in gone)

    if reason_global:
        required = all_cases
        carried: list[str] = []
        per_case = {c: "полное ревью: " + reason_global for c in all_cases}
    else:
        old_cases = baseline.get("cases", {})
        changed = [c for c in all_cases if old_cases.get(c) != current["cases"][c]]
        # A changed main case moves the preconditions every variant inherits.
        main_changed = any(MAIN_CASE.search(c) for c in changed)
        per_case = {}
        required = []
        for c in all_cases:
            if c in changed:
                per_case[c] = "кейс изменён"
                required.append(c)
            elif main_changed and not MAIN_CASE.search(c):
                per_case[c] = "изменён основной кейс — вариант наследует его предусловия"
                required.append(c)
            elif c not in old_cases:
                per_case[c] = "новый кейс"
                required.append(c)
            else:
                per_case[c] = "не изменён — findings переносятся"
        carried = [c for c in all_cases if c not in required]

    # Findings, вытащенные из прошлой итерации: их обязан перепечатать критик.
    # Список здесь, а не только в шаблоне отчёта, чтобы «перенесено» было данными,
    # а не обещанием — иначе суженное ревью теряет находки молча.
    carried_findings = _extract_carried_findings(jid) or _try_parse_review_markdown(jid)

    # P-41: фикс-итерация, не изменившая ни байта. Отличать от «объём пуст» нельзя
    # по одному лишь `required`: при первой итерации он тоже мог бы опустеть, поэтому
    # признак — база БЫЛА, глобальных инвалидаций НЕТ, и ни один кейс не сдвинулся.
    designer_no_op = (
        reason_global is None
        and baseline is not None
        and not required
        and bool(all_cases)
    )

    decision = {
        "journeyId": jid,
        "globalInvalidation": reason_global,
        "designerNoOp": designer_no_op,
        "reviewRequired": required,
        "carryForward": carried,
        "carriedFindings": carried_findings,
        "perCase": per_case,
        "savedShare": round(len(carried) / len(all_cases), 2) if all_cases else 0.0,
    }

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[scope] {jid}: кейсов {len(all_cases)}, "
          f"переревьюить {len(required)}, перенести {len(carried)}")
    if reason_global:
        print(f"[scope] полное ревью — {reason_global}")
    for c in all_cases:
        mark = "REVIEW " if c in required else "carry  "
        print(f"[scope]   {mark} {c} — {per_case[c]}")
    if not reason_global and carried:
        print("[scope] перенесённые findings обязаны попасть в отчёт дословно, "
              "с пометкой «перенесено с итерации N-1»")

    if args.update:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[scope] база хешей обновлена → {baseline_path}")

    if designer_no_op:
        print(f"[scope] {jid}: дизайнер не изменил НИ ОДНОГО байта в {len(all_cases)} кейсах "
              f"— критика не вызывать, journey уходит в NEEDS_HUMAN")
        print("[scope] критик вынес бы тот же вердикт: вход этого суждения не сдвинулся, "
              "а семантический проход стоит 4:39–8:25")
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
