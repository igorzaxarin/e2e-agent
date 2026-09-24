#!/usr/bin/env python3
"""Extract a minimal per-journey context payload.

Reads `output/suites/_index.json` and `output/suites/<JOURNEY_ID>.md`, then writes
`output/suites/<JOURNEY_ID>-context.json` containing only the requirements,
gaps and questions that belong to that journey. This is the contract
`qa-designer` and `test-critic` consume instead of the full index.

Usage:
    python3 scripts/extract_journey_context.py <JOURNEY_ID>
    python3 scripts/extract_journey_context.py --all

Exit codes:
    0  context written
    1  journey not found
    2  input missing
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# A cp1251 console mangles the Cyrillic this script prints and raises
# UnicodeEncodeError on «→», turning a successful run into a non-zero exit for a
# reason that has nothing to do with the artefacts. Same guard as gate_check.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


DEFAULT_ANSWERS = "input/requirements/_answers.md"


def load_answers(path):
    """Ответы человека из `_answers.md`, разобранные тем же кодом, что и extract_reqs.

    Индекс аналитика пишется ДО шлюза, а на шлюзе человек отвечает — поэтому
    вопрос, закрытый ответом, остаётся в индексе открытым. Контекст, собранный
    из такого индекса, ведёт агента по отменённой развилке: в прогоне 12
    дизайнера спасло только то, что бриф отдельно велел читать `_answers.md`,
    а критик написал, что следующий на его месте выпишет ложный BLOCKER.
    """
    src = Path(path)
    if not src.is_file():
        return []
    spec = importlib.util.spec_from_file_location(
        "extract_reqs", Path(__file__).with_name("extract_reqs.py"))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.parse_answers(src)
    except Exception:
        return []


def norm(text):
    return " ".join(str(text or "").lower().replace("«", "").replace("»", "").split())


def find_answer(question, answers):
    """Ответ, закрывающий вопрос, — **только по тексту вопроса**.

    По id сопоставлять нельзя, и это проверено на живом прогоне: локальные id
    у каждого прохода свои, поэтому «Q-01» второго прохода (что наблюдается при
    вводе истёкшего кода) совпал по id с «Q-01» первого (поддерживается ли
    перестановка треков) и получил чужой ответ. Текст вопроса ответ цитирует
    дословно — это единственная связь, переживающая перенумерацию.
    """
    body = norm(question.get("question"))
    if len(body) < 40:
        return None                      # слишком коротко, чтобы совпадение что-то значило
    probe = body[:120]
    for a in answers:
        if probe in norm(a.get("question")):
            return a
    return None


def belongs_to(entry: dict, jid: str) -> bool:
    """Match an index entry to a journey by either id form.

    The analyst writes `journey` as the full id in some runs and as the J<NN>
    prefix in others. Comparing against one form drops every entry of the other
    — and the drop is invisible, because an empty list is a valid payload.
    """
    own = str(entry.get("journey", ""))
    return own == jid or own == jid.split("-", 1)[0] or jid.startswith(own + "-")


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-journey context filter")
    ap.add_argument("journey", nargs="?", help="JOURNEY_ID like J01-purchase-flow")
    ap.add_argument("--all", action="store_true", help="extract for every journey")
    ap.add_argument("--index", default="output/suites/_index.json")
    ap.add_argument("--out-dir", default="output/suites")
    ap.add_argument("--answers", default=DEFAULT_ANSWERS,
                    help="файл ответов человека; вопросы, закрытые им, помечаются "
                         "answered даже если индекс аналитика писался до шлюза")
    args = ap.parse_args()

    answers = load_answers(args.answers)

    index_path = Path(args.index)
    if not index_path.is_file():
        sys.stderr.write(f"[context] индекс не найден: {index_path}\n")
        return 2

    index = json.loads(index_path.read_text(encoding="utf-8"))
    journeys = {j["id"]: j for j in index.get("journeys", [])}

    if args.all:
        targets = list(journeys.keys())
    elif args.journey:
        if args.journey not in journeys:
            sys.stderr.write(
                f"[context] journey '{args.journey}' не найден. "
                f"Доступные: {', '.join(journeys.keys())}\n"
            )
            return 1
        targets = [args.journey]
    else:
        sys.stderr.write("[context] укажите JOURNEY_ID или --all\n")
        return 2

    req_index = {r["id"]: r for r in index.get("reqIndex", [])}
    gaps_all = index.get("gaps", [])
    questions_all = index.get("questions", [])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for jid in targets:
        journey = journeys[jid]
        own_reqs = [req_index[rid] for rid in journey.get("reqs", []) if rid in req_index]
        own_gaps = [
            {
                "id": g["id"],
                "what": g["what"],
                "quote": g["quote"],
                "resolvedBy": g.get("resolvedBy"),
            }
            for g in gaps_all
            if belongs_to(g, jid)
        ]
        own_questions = []
        resolved_here = 0
        for q in questions_all:
            if not belongs_to(q, jid):
                continue
            hit = find_answer(q, answers)
            if hit:
                resolved_here += 1
            own_questions.append({
                "id": q["id"],
                "question": q["question"],
                "blocks": q.get("blocks", []),
                "answered": bool(hit) or q.get("answered", False),
                "answer": hit.get("answer") if hit else q.get("answer"),
                "answeredBy": hit.get("id") if hit else None,
            })

        if gaps_all and not own_gaps:
            sys.stderr.write(
                f"extract_journey_context: в индексе {len(gaps_all)} пробелов, "
                f"но ни один не отнесён к {jid} — проверьте поле «journey» в _index.json\n")
        if questions_all and not own_questions:
            sys.stderr.write(
                f"extract_journey_context: в индексе {len(questions_all)} вопросов, "
                f"но ни один не отнесён к {jid}\n")

        payload = {
            "journeyId": jid,
            "title": journey.get("title"),
            "plan": journey.get("plan"),
            "primaryArea": journey.get("primaryArea"),
            "areas": journey.get("areas", []),
            "reqIndex": own_reqs,
            "gaps": own_gaps,
            "questions": own_questions,
        }

        out_path = out_dir / f"{jid}-context.json"
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        closed = f", закрыто ответами: {resolved_here}" if resolved_here else ""
        print(
            f"[context] {jid}: {len(own_reqs)} REQ, "
            f"{len(own_gaps)} gaps, {len(own_questions)} questions{closed} → {out_path}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
