#!/usr/bin/env python3
"""Deterministic requirement indexer.

Pulls the requirement index out of the source files mechanically, so the analyst does
not spend serial output tokens retyping requirement text — and, more importantly, does
not have to *find* the requirements inside mixed prose before it can group them.

The analyst's own spec already says the numbering is not its decision: reuse the source
numbers when they exist. That makes this extraction, not judgement — and extraction
belongs in a script.

Two source shapes are recognised:

    - **REQ-01.** Регистрация выполняется по номеру телефона…      (list item)
    | **BR-001** | Авторизация | Пользователь входит по номеру… |  (table row)

Ответы человека на шлюзе требований — тоже требования, но не с теми же номерами:
`--answers input/requirements/_answers.md` кладёт их в отдельный список `answers`
с собственным пространством id `ANS-NN`. Придуманный `REQ-100` испортил бы учёт
покрытия — он навсегда числился бы непокрытым требованием, которого нет в корпусе.

Usage:
    python3 scripts/extract_reqs.py
    python3 scripts/extract_reqs.py --prefix BR,REQ
    python3 scripts/extract_reqs.py --markdown --only REQ-01,REQ-04
    python3 scripts/extract_reqs.py --answers input/requirements/_answers.md

Exit codes: 0 = index written, 2 = no requirements found, or --answers given
and no answer parsed out of the file named.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# **REQ-01.** / **BR-001** / **BG-01** / **P0-01** — bold id, optional trailing dot.
# The prefix is letters with an optional trailing digit (P0, P1, …) — a bare digit right
# before the dash is still part of the prefix, not the number that follows it. Found on
# zvuk.md: "P0-01/02/03" is a real corpus prefix and the letters-only version missed it
# silently (regex simply didn't match — no error, the ids were just never in reqIndex).
REQ_ID = re.compile(r"\*\*([A-Z]{1,4}\d?-\d{1,3})\.?\*\*")

# A list item that opens with a requirement id.
LIST_ITEM = re.compile(r"^\s*[-*]\s+\*\*([A-Z]{1,4}\d?-\d{1,3})\.?\*\*\s*(.*)$")

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# Cells that carry no requirement text — priority markers and the like.
NOISE_CELLS = {"must", "should", "could", "won't", "wont"}

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def clean(text: str) -> str:
    """Flatten a requirement cell to one quotable line."""
    text = text.replace("<br>", " ").replace("<br/>", " ")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # bold is layout, not content
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .;")


def is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def is_separator_row(line: str) -> bool:
    return bool(re.fullmatch(r"[|\-: ]+", line.strip()))


def parse_file(path: Path) -> list[dict]:
    """Return requirement records found in one markdown file, in reading order."""
    records: list[dict] = []
    section = ""
    pending: dict | None = None

    def flush() -> None:
        nonlocal pending
        if pending:
            pending["text"] = clean(pending["text"])
            if pending["text"]:
                records.append(pending)
            pending = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        heading = HEADING.match(raw)
        if heading:
            flush()
            section = heading.group(2).strip()
            continue

        if is_table_row(raw) and not is_separator_row(raw):
            flush()
            cells = [c.strip() for c in raw.strip().strip("|").split("|")]
            if not cells:
                continue
            found = REQ_ID.search(cells[0])
            # The id must own the first cell, otherwise it is a mention, not a definition.
            bare = clean(cells[0]).replace("*", "").strip(" .")
            if not found or bare != found.group(1):
                continue
            body = [
                clean(c) for c in cells[1:]
                if clean(c) and clean(c).replace("*", "").lower() not in NOISE_CELLS
            ]
            records.append({
                "id": found.group(1),
                "text": " — ".join(body),
                "source": f"{path.name} § {section}" if section else path.name,
            })
            continue

        item = LIST_ITEM.match(raw)
        if item:
            flush()
            pending = {
                "id": item.group(1),
                "text": item.group(2),
                "source": f"{path.name} § {section}" if section else path.name,
            }
            continue

        if pending is not None:
            # Continuation line of a multi-line list item.
            if raw.strip() and raw.startswith((" ", "\t")):
                pending["text"] += " " + raw.strip()
            else:
                flush()

    flush()
    return records


def collect(sources: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in sources:
        p = Path(pattern)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.md")))
        elif p.is_file():
            paths.append(p)
        else:
            paths.extend(sorted(Path().glob(pattern)))
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def as_markdown(records: list[dict]) -> str:
    """The «Индекс требований» block a suite plan carries verbatim.

    Plans repeat the rows they need on purpose: parallel designers never see each other,
    so a plan that points at another file is not self-sufficient. The duplication is
    deliberate — paying an LLM to retype it is not.

    The text is verbatim, not a précis. A paraphrase is where a requirement quietly loses
    the detail a case later needs.
    """
    lines = ["| Анкор | Формулировка требования | Источник (файл, раздел) |", "|---|---|---|"]
    for r in records:
        text = r["text"].replace("|", "\\|")
        lines.append(f"| {r['id']} | {text} | {r['source']} |")
    return "\n".join(lines)


# `## 3. Q-03: <заголовок>` — нумерация раздела необязательна, id обязателен.
# Заголовок ответа: `## 3. Q-03: …`, `## 13. G-01 (довесок к Q-04): …`.
# Раньше принимался только `Q-\d+`, и ответ, озаглавленный по пробелу, молча
# терялся — 12 разобранных из 13 объявленных, без единого слова в stderr.
ANSWER_HEADING = re.compile(r"^##\s+(?:\d+\.\s*)?([A-Z]{1,4}-\d+)\s*[:.\-(]?\s*(.*)$")
# `**Ответ продукта (выбран):** …` — жирным может быть обёрнуто и двоеточие.
ANSWER_MARK = re.compile(r"^\s*\*\*\s*Ответ[^*]*\*\*\s*:?\s*(.*)$")
QUESTION_MARK = re.compile(r"^\s*>\s*Вопрос\s*:?\s*(.*)$")


def parse_answers(path: Path) -> list[dict]:
    """Human answers from the requirements gate, in their own id namespace.

    An answer is binding text — the gate spec says so outright — but it is not a
    requirement of the corpus. Numbering it REQ-NN would put an id into coverage
    accounting that no source file carries.
    """
    records: list[dict] = []
    q_id = title = ""
    question = ""
    answer_lines: list[str] = []
    in_answer = False

    def flush() -> None:
        nonlocal q_id, title, question, answer_lines, in_answer
        if q_id and answer_lines:
            records.append({
                "id": f"ANS-{len(records) + 1:02d}",
                "qId": q_id,
                "question": clean(question) or clean(title),
                "answer": clean(" ".join(answer_lines)),
                "source": f"{path.name} § {q_id}",
            })
        q_id = title = question = ""
        answer_lines = []
        in_answer = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        head = ANSWER_HEADING.match(raw)
        if head:
            flush()
            q_id, title = head.group(1), head.group(2)
            continue
        if not q_id:
            continue
        if raw.startswith("#"):          # any other heading closes the block
            flush()
            continue
        mark = ANSWER_MARK.match(raw)
        if mark:
            in_answer = True
            if mark.group(1).strip():
                answer_lines.append(mark.group(1))
            continue
        quoted = QUESTION_MARK.match(raw)
        if quoted:
            question = quoted.group(1)
            continue
        if in_answer:
            if raw.strip():
                answer_lines.append(raw)
            else:
                in_answer = False
    flush()
    return records


def answers_as_markdown(answers: list[dict]) -> str:
    lines = ["| Анкор | Вопрос | Ответ человека | Источник |", "|---|---|---|---|"]
    for a in answers:
        lines.append(f"| {a['id']} ({a['qId']}) | {a['question'].replace('|', chr(92) + '|')} "
                     f"| {a['answer'].replace('|', chr(92) + '|')} | {a['source']} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract the requirement index deterministically")
    ap.add_argument("sources", nargs="*", default=["input/requirements"],
                    help="files, directories or globs (default: input/requirements)")
    ap.add_argument("--out", default="output/suites/_reqindex.json",
                    help="where to write the JSON index")
    ap.add_argument("--markdown", action="store_true",
                    help="print the Markdown «Индекс требований» table to stdout")
    ap.add_argument("--only", help="comma-separated ids to keep, e.g. REQ-01,REQ-04")
    ap.add_argument("--prefix", help="comma-separated id prefixes to keep, e.g. BR,REQ. "
                                     "Корпус может нумеровать не только требования — цели, "
                                     "допущения и риски тоже получают id")
    ap.add_argument("--answers", metavar="PATH",
                    help="файл ответов человека со шлюза требований "
                         "(input/requirements/_answers.md); они попадают в отдельный "
                         "список answers с id ANS-NN, а не в reqIndex")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    answers: list[dict] = []
    if args.answers:
        answers_path = Path(args.answers)
        if not answers_path.is_file():
            sys.stderr.write(f"extract_reqs: файла ответов нет: {answers_path}\n")
            return 2
        answers = parse_answers(answers_path)
        # Полная потеря ловится ниже, частичная — здесь: заголовков в файле больше,
        # чем разобранных ответов, значит какой-то формат регулярка не знает.
        headings = sum(1 for line in answers_path.read_text(encoding="utf-8").splitlines()
                       if line.startswith("## "))
        if answers and headings > len(answers):
            sys.stderr.write(
                f"extract_reqs: в {answers_path} разделов «## » {headings}, "
                f"а разобрано ответов {len(answers)} — часть заголовков не распознана. "
                "Ответы человека равны требованиям, потерять часть нельзя\n"
            )
        if not answers:
            sys.stderr.write(
                f"extract_reqs: в {answers_path} не разобран ни один ответ — ожидается "
                "«## <n>. Q-01: <заголовок>» и строка «**Ответ…:** <текст>». "
                "Молча продолжить нельзя: ответы человека равны требованиям\n"
            )
            return 2

    files = collect(args.sources)
    if not files:
        sys.stderr.write(f"extract_reqs: нет файлов требований: {args.sources}\n")
        return 2

    records: list[dict] = []
    for f in files:
        records.extend(parse_file(f))

    if not records:
        sys.stderr.write(
            "extract_reqs: требования не найдены — ожидается «- **REQ-01.** …» "
            "или строка таблицы «| **BR-001** | … |»\n"
        )
        return 2

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for r in records:
        if r["id"] in seen:
            duplicates.append(f"{r['id']} ({seen[r['id']]} и {r['source']})")
        else:
            seen[r["id"]] = r["source"]

    if args.prefix:
        keep = {p.strip().upper() for p in args.prefix.split(",") if p.strip()}
        records = [r for r in records if r["id"].split("-")[0] in keep]

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        missing = sorted(wanted - set(seen))
        records = [r for r in records if r["id"] in wanted]
        if missing:
            sys.stderr.write(f"extract_reqs: не найдены требования: {', '.join(missing)}\n")

    if args.markdown:
        print(as_markdown(records))
        if answers:
            print()
            print("Ответы человека (равны требованиям, нумерация своя):")
            print()
            print(answers_as_markdown(answers))
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "requirementsSource": [str(f) for f in files],
                "reqIndex": records,
                "answers": answers,
                "duplicateIds": duplicates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not args.quiet:
        print(f"extract_reqs: {len(records)} записей из {len(files)} файл(ов) → {out}")
        if answers:
            print(f"  ответов человека: {len(answers)} ({answers[0]['id']}…{answers[-1]['id']}) "
                  f"из {args.answers} — они равны требованиям, но нумеруются отдельно")
        groups: dict[str, list[str]] = {}
        for r in records:
            groups.setdefault(r["id"].split("-")[0], []).append(r["id"])
        for prefix, ids in sorted(groups.items()):
            sections = sorted({r["source"] for r in records if r["id"].startswith(prefix + "-")})
            print(f"  {prefix}-*: {len(ids)} — {'; '.join(sections)}")
        if len(groups) > 1 and not args.prefix:
            print("  корпус нумерует не только требования — сузьте набор через --prefix,")
            print("  если цели, допущения или риски не должны попасть в индекс")
        if duplicates:
            print("  повторяющиеся id: " + "; ".join(duplicates))

    return 0


if __name__ == "__main__":
    sys.exit(main())
