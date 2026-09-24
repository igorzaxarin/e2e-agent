#!/usr/bin/env python3
"""Targeted find-and-replace patcher for e2e test case Markdown files.

qa-designer's only file-writing tool is `write_file` — whole file, every call. On a fix
iteration that touches one row out of a 20-row case (the common shape per result.md: "1
BLOCKER in one case out of five"), that means reprinting the entire file to change one
row — full sequential-output cost for a one-line fix, on every iteration, for every
journey that needed one. `run_shell_command` is already in the designer's tool list, so
this script gives it a way to pay for the size of the *change*, not the size of the file.

Fail-closed like `review_scope.py`: an OLD block that does not match the file exactly
once aborts the WHOLE patch — nothing is written — rather than guessing which occurrence
was meant or silently applying a partial fix.

Input format on stdin, one or more OLD/NEW pairs:

    ===OLD===
    <exact text currently in the file — enough surrounding context to be unique>
    ===NEW===
    <replacement text>

Repeat the pair for multiple edits to the same file in one call; they are validated
together (as non-overlapping spans of the original content) and applied together, or
not at all.

Usage:
    python3 scripts/patch_case.py output/cases/J01-slug/TC-J01-00.md <<'EOF'
    ===OLD===
    | 6 | Подтвердить выбор жанров | Электроника, Хип-хоп, Инди | Открывается главный экран |
    ===NEW===
    | 6 | Подтвердить выбор жанров | Электроника, Хип-хоп, Инди | Открывается главный экран, блок «Рекомендации» содержит не менее одного элемента |
    EOF

    python3 scripts/patch_case.py <file> --dry-run < patches.txt   # validate only

Exit codes: 0 = applied (or --dry-run validated clean), 1 = an OLD block matched zero or
more than one time — nothing written, 2 = bad input (file missing, no patches parsed,
malformed stdin, overlapping patches).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def parse_patches(raw: str) -> list[tuple[str, str]]:
    """Split stdin into (old, new) pairs. Raises ValueError on malformed input."""
    patches: list[tuple[str, str]] = []
    old_lines: list[str] | None = None
    new_lines: list[str] | None = None
    state: str | None = None  # None | "old" | "new"

    def commit() -> None:
        if old_lines is None or new_lines is None:
            raise ValueError("незавершённый патч: нет пары ===OLD===/===NEW===")
        patches.append(("\n".join(old_lines), "\n".join(new_lines)))

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped == "===OLD===":
            if state is not None:
                commit()
            old_lines, new_lines = [], None
            state = "old"
        elif stripped == "===NEW===":
            if state != "old":
                raise ValueError("===NEW=== без предшествующего ===OLD===")
            new_lines = []
            state = "new"
        elif state == "old":
            old_lines.append(line)
        elif state == "new":
            new_lines.append(line)
        elif stripped:
            raise ValueError(f"текст вне блока ===OLD===/===NEW===: {line!r}")

    if state is not None:
        commit()

    return patches


def main() -> int:
    ap = argparse.ArgumentParser(description="Точечная замена old->new в файле кейса")
    ap.add_argument("file", help="путь к .md кейса, который нужно поправить")
    ap.add_argument("--dry-run", action="store_true",
                     help="только проверить однозначность патчей, ничего не писать")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.stderr.write(f"patch_case: файл не найден: {path}\n")
        return 2

    raw = sys.stdin.read()
    try:
        patches = parse_patches(raw)
    except ValueError as exc:
        sys.stderr.write(f"patch_case: {exc}\n")
        return 2
    if not patches:
        sys.stderr.write("patch_case: во входе нет ни одного патча (===OLD===/===NEW===)\n")
        return 2

    content = path.read_text(encoding="utf-8")

    problems: list[str] = []
    spans: list[tuple[int, int, str, int]] = []
    for i, (old, new) in enumerate(patches, start=1):
        count = content.count(old)
        if count == 0:
            problems.append(f"патч {i}: OLD не найден в файле — проверьте пробелы/перенос строк")
        elif count > 1:
            problems.append(f"патч {i}: OLD встречается {count} раз — сделайте контекст уникальным")
        else:
            start = content.index(old)
            spans.append((start, start + len(old), new, i))

    if not problems:
        spans.sort(key=lambda s: s[0])
        for a, b in zip(spans, spans[1:]):
            if b[0] < a[1]:
                problems.append(f"патчи {a[3]} и {b[3]} пересекаются — объедините их в один")

    if problems:
        for p in problems:
            sys.stderr.write(f"patch_case: {p}\n")
        sys.stderr.write("patch_case: ничего не записано — все патчи должны быть однозначны\n")
        return 1

    if args.dry_run:
        print(f"[patch] {len(patches)} патч(ей) валидны, файл не изменён (--dry-run)")
        return 0

    parts: list[str] = []
    cursor = 0
    for start, end, new, _ in spans:
        parts.append(content[cursor:start])
        parts.append(new)
        cursor = end
    parts.append(content[cursor:])
    path.write_text("".join(parts), encoding="utf-8")

    print(f"[patch] применено {len(patches)} патч(ей) → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
