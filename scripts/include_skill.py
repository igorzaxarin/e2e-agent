#!/usr/bin/env python3
"""Inline a shared skill into a command prompt at the right heading depth.

Commands include skills with `!{python3 scripts/include_skill.py <name> --level N}`.
Strips the YAML frontmatter and shifts every heading so the skill's top heading lands
at `--level`, leaving the command's own outline intact.

Conditional include: `--if-flag <flag> --args "{{args}}"` emits the skill only when the
flag is present in the run arguments; otherwise it emits a short stub telling the
orchestrator how to load the procedure on demand. An `--args` value that still carries
an unsubstituted `{{...}}` placeholder means "cannot tell" — the skill is then included
in full, so a harness that does not substitute arguments loses nothing.

Exit codes: 0 = written to stdout, 2 = no such skill (loud, never a silent empty include).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent / ".gigacode" / "skills"
HEADING = re.compile(r"^(#{1,6})\s")
FENCE = re.compile(r"^\s*(```|~~~)")


def strip_frontmatter(lines: list[str]) -> list[str]:
    if not lines or lines[0].strip() != "---":
        return lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[i + 1:]
    return lines  # unterminated frontmatter: leave the file alone rather than eat it


PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


def flag_present(flag: str, run_args: str | None) -> bool:
    """True when `flag` appears as a whole token in `run_args`.

    The flag name is given without leading dashes (`--if-flag generate-pytest`), so that
    argparse does not mistake it for an option of this script.

    None, an empty string, or a value that still holds an unsubstituted `{{...}}`
    placeholder all mean the caller could not tell us the arguments. Answer True there:
    an unknown argument list must never silently drop a phase from the procedure.
    """
    if run_args is None:
        return True
    if PLACEHOLDER.search(run_args):
        return True
    if not run_args.strip():
        return False
    wanted = flag.lstrip("-")
    return any(tok.lstrip("-") == wanted
               for tok in run_args.replace("=", " ").split())


def skip_stub(name: str, level: int, flag: str, note: str | None) -> str:
    head = "#" * max(1, min(6, level))
    body = note or f"Фаза не запрошена: флаг `--{flag.lstrip(chr(45))}` не передан."
    return "\n".join([
        f"{head} Процедура `{name}` не загружена",
        "",
        body,
        "",
        "Если фаза всё же выполняется — человек ответил «да» на её вопрос из реестра — "
        "загрузите её процедуру перед первым шагом и следуйте ей дословно:",
        "",
        "```bash",
        f"python3 scripts/include_skill.py {name} --level {level}",
        "```",
        "",
        "Без этой загрузки фазу не начинайте: её процедуры в вашем контексте нет.",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="Подставить скилл в команду на нужной глубине")
    ap.add_argument("name", help="имя скилла без .md, например human-gate")
    ap.add_argument("--level", type=int, default=2,
                    help="уровень, на который встаёт верхний заголовок скилла (по умолчанию 2)")
    ap.add_argument("--if-flag", dest="if_flag", default=None,
                    help="включать скилл только при этом флаге в --args, без ведущих дефисов")
    ap.add_argument("--args", dest="run_args", default=None,
                    help="строка аргументов прогона для --if-flag")
    ap.add_argument("--skip-note", dest="skip_note", default=None,
                    help="строка, которая печатается вместо скилла, когда флаг не передан")
    args = ap.parse_args()

    if args.if_flag and not flag_present(args.if_flag, args.run_args):
        print(skip_stub(args.name, args.level, args.if_flag, args.skip_note))
        return 0

    path = SKILL_DIR / f"{args.name}.md"
    if not path.is_file():
        sys.stderr.write(f"include_skill: нет скилла {path}\n")
        print(f"> **ОШИБКА ВКЛЮЧЕНИЯ:** скилл `{args.name}` не найден в `{SKILL_DIR}`. "
              f"Не выполняйте эту фазу — процедура не загружена.")
        return 2

    lines = strip_frontmatter(path.read_text(encoding="utf-8").split("\n"))

    in_fence = False
    tops = []
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and (m := HEADING.match(line)):
            tops.append(len(m.group(1)))
    shift = args.level - min(tops) if tops else 0

    in_fence = False
    out = []
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and (m := HEADING.match(line)):
            depth = min(6, max(1, len(m.group(1)) + shift))
            line = "#" * depth + line[len(m.group(1)):]
        out.append(line)

    print("\n".join(out).strip("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
