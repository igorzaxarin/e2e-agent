#!/usr/bin/env python3
"""Инвентарь адресуемых ручек интерфейса из ИСХОДНИКОВ приложения — тир A.

Слой 1 из трёх (сбор → привязка → код). Здесь ничего не решается и не связывается
с кейсами: скрипт только перечисляет то, чем элемент вообще можно адресовать, и
откуда это взято. Привязку шага кейса к ручке делает агент-картограф, потому что
«какая из 340 ручек — это «поле ввода телефона» из шага 3» требует чтения, а
«найти все data-testid» — не требует.

Тир A означает «намеренный контракт»: ручка объявлена в исходниках разработчиком.
Тир B (`harvest_selectors.py`) — наблюдение с живой страницы, оно слабее, и
смешивать их в одном поле нельзя: тест на тире B фиксирует текущую сборку вместе
с её багами, и читатель отчёта обязан это видеть.

Источник — локальный путь или git-URL:

    python3 scripts/extract_selectors.py --source input/app
    python3 scripts/extract_selectors.py --source https://host/team/app.git

Клон делается поверхностным и во временный каталог; неудача клона — это
ОТСУТСТВИЕ источника (код 2), а не разрешение угадывать дальше.

Ловушки, воспроизведённые намеренно
-----------------------------------
1. **Закрывающая кавычка — обратная ссылка, а не «любая из двух».** Наивный
   `["']` обрезает `'[data-testid="phone-field"]'` по внутренней кавычке и даёт
   мусор `[data-testid=`. Именно так выглядит тестовый идентификатор — то есть
   наивный шаблон отказывает сильнее всего ровно на том случае, ради которого
   пишется. Здесь закрывающая кавычка — `\1`.
2. **Динамическое значение не додумывается.** `data-testid={id}` во JSX или
   `:data-testid="x"` во Vue значения в исходнике не имеет. Такие попадают в
   `unresolved` со счётчиком, а не в `handles` с выдуманным именем. Отчёт о том,
   что скрипт не смог разрешить, — часть результата.
3. **`id` собирается только из разметки.** В CSS `#foo` — это использование, а не
   объявление, и подметание стилей раздуло бы инвентарь именами, которых в DOM
   может не быть вовсе.

Коды выхода: 0 — инвентарь записан, 2 — источника нет или он пуст.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Файлы, в которых разметка вообще может быть объявлена. CSS сюда не входит
# намеренно — см. ловушку 3 в докстринге.
MARKUP_SUFFIXES = {
    ".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx", ".js", ".ts",
    ".astro", ".hbs", ".mustache", ".twig", ".erb", ".php", ".jinja", ".jinja2",
}

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "out", ".next", ".nuxt",
    "coverage", "__pycache__", ".venv", "venv", "vendor", ".cache",
}

# Значения тестовых идентификаторов, в порядке приоритета якоря.
TESTID_ATTRS = ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy")


def _attr_pattern(attr: str) -> re.Pattern[str]:
    """attr="value" / attr='value', закрывающая кавычка — обратная ссылка.

    Запрет на `\\w`, `-` и `:` слева делает три вещи, и вторая найдена прогоном
    по настоящей разметке, а не рассуждением:

    * `data-test` не совпадает внутри `data-testid` — иначе одна ручка попадала
      бы в инвентарь дважды под разными якорями;
    * `:data-qa="computed"` (привязка Vue) не уходит в ручки как статическое
      значение `computed`. Это имя ВЫРАЖЕНИЯ, а не значение атрибута: в DOM
      окажется то, что выражение вернёт. Такие идут в `unresolved`;
    * `v-bind:data-qa` — то же самое, оно кончается на `:`.
    """
    return re.compile(rf"(?<![\w\-:]){re.escape(attr)}\s*=\s*(['\"])(.*?)\1", re.I)


# Динамическое значение: attr={...} (JSX), :attr="..." / v-bind:attr (Vue).
DYNAMIC = re.compile(
    r"(?<![\w-])(?::|v-bind:)?"
    r"(data-testid|data-test-id|data-test|data-qa|data-cy|id|name)"
    r"\s*=\s*\{", re.I,
)
DYNAMIC_VUE = re.compile(
    r"(?<![\w-])(?::|v-bind:)"
    r"(data-testid|data-test-id|data-test|data-qa|data-cy|id|name)"
    r"\s*=\s*(['\"])", re.I,
)

ID_RE = _attr_pattern("id")
NAME_RE = _attr_pattern("name")
ROLE_RE = _attr_pattern("role")
ARIA_LABEL_RE = _attr_pattern("aria-label")

# Значение, которое явно не является стабильным якорем: хеш CSS-in-JS, служебные
# id сборщика. Такие записываются, но помечаются — картограф должен их избегать.
UNSTABLE = re.compile(r"^(?:sc-[0-9a-z]{6,}|css-[0-9a-z]{6,}|:r[0-9a-z]+:|[0-9a-f]{8,})$", re.I)


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in MARKUP_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def scan_text(text: str, rel: str, handles: list[dict], unresolved: list[dict]) -> None:
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for attr in TESTID_ATTRS:
            for m in _attr_pattern(attr).finditer(line):
                value = m.group(2).strip()
                if value:
                    handles.append(_handle("testid", attr, value, rel, lineno))
        for m in ID_RE.finditer(line):
            value = m.group(2).strip()
            # Шаблонная подстановка внутри строки — значение всё равно не статично.
            if value and "{" not in value and "$" not in value:
                handles.append(_handle("id", "id", value, rel, lineno))
        for m in NAME_RE.finditer(line):
            value = m.group(2).strip()
            if value and "{" not in value and "$" not in value:
                handles.append(_handle("name", "name", value, rel, lineno))

        role = ROLE_RE.search(line)
        label = ARIA_LABEL_RE.search(line)
        if role and label:
            h = _handle("aria", "role+aria-label",
                        f"{role.group(2).strip()}|{label.group(2).strip()}", rel, lineno)
            h["role"] = role.group(2).strip()
            h["accessibleName"] = label.group(2).strip()
            handles.append(h)

        for m in DYNAMIC.finditer(line):
            unresolved.append({"file": rel, "line": lineno, "attr": m.group(1).lower(),
                               "reason": "значение вычисляется в коде"})
        for m in DYNAMIC_VUE.finditer(line):
            unresolved.append({"file": rel, "line": lineno, "attr": m.group(1).lower(),
                               "reason": "привязка Vue, значение вычисляется"})


def _handle(kind: str, anchor: str, value: str, rel: str, lineno: int) -> dict:
    return {
        "handleId": f"{kind}:{value}",
        "kind": kind,
        "anchor": anchor,
        "value": value,
        "tier": "A",
        "origin": f"{rel}:{lineno}",
        "stable": not bool(UNSTABLE.match(value)),
    }


def resolve_source(source: str, keep: Path | None) -> tuple[Path, dict, Path | None]:
    """Локальный путь или git-клон. Возвращает (корень, описание, что удалить)."""
    if re.match(r"^(https?://|git@|ssh://|git://)", source):
        target = keep or Path(tempfile.mkdtemp(prefix="uisrc-"))
        cmd = ["git", "clone", "--depth", "1", source, str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            sys.stderr.write(f"selectors: клон не выполнен: {exc}\n")
            return Path(), {}, None
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["без вывода"]
            sys.stderr.write(f"selectors: git clone вернул {proc.returncode}: {tail[0]}\n")
            return Path(), {}, None
        head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        return target, {"kind": "git", "url": source,
                        "commit": (head.stdout or "").strip()[:12]}, (None if keep else target)

    root = Path(source)
    if not root.is_dir():
        sys.stderr.write(f"selectors: каталог источника не найден: {root}\n")
        return Path(), {}, None
    return root, {"kind": "path", "path": str(root)}, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Инвентарь адресуемых ручек из исходников приложения (тир A)")
    ap.add_argument("--source", required=True,
                    help="локальный путь к исходникам или git-URL для поверхностного клона")
    ap.add_argument("--out", default="output/ui/selectors.json",
                    help="куда записать инвентарь (по умолчанию output/ui/selectors.json)")
    ap.add_argument("--keep-clone", help="куда положить клон, чтобы не удалять его после разбора")
    ap.add_argument("--merge", action="store_true",
                    help="дополнить существующий инвентарь, а не заменить (для добавления тира B)")
    args = ap.parse_args()

    root, descriptor, cleanup = resolve_source(args.source,
                                               Path(args.keep_clone) if args.keep_clone else None)
    if not descriptor:
        sys.stderr.write("selectors: источника нет — инвентарь НЕ построен.\n")
        sys.stderr.write("selectors: это не «ручек не найдено»; без инвентаря браузерные "
                         "тесты пишутся скелетами с объявленным пробелом.\n")
        return 2

    try:
        handles: list[dict] = []
        unresolved: list[dict] = []
        n_files = 0
        for path in iter_files(root):
            n_files += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scan_text(text, str(path.relative_to(root)).replace("\\", "/"), handles, unresolved)
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)

    # Дедупликация по handleId: одна ручка, много мест объявления.
    merged: dict[str, dict] = {}
    for h in handles:
        cur = merged.setdefault(h["handleId"], dict(h, origins=[]))
        cur.setdefault("origins", []).append(h["origin"])
    for h in merged.values():
        h["origins"] = sorted(set(h["origins"]))
        h.pop("origin", None)

    out = Path(args.out)
    existing = {"handles": [], "sources": []}
    if args.merge and out.is_file():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.stderr.write(f"selectors: существующий {out} нечитаем — слияние отменено\n")
            return 2
        # Тир B не затирается тиром A и наоборот: у них разный статус.
        merged = {h["handleId"]: h for h in existing.get("handles", [])
                  if h.get("tier") != "A"} | merged

    inventory = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [s for s in existing.get("sources", []) if s.get("kind") == "live"]
                   + [descriptor],
        "handles": sorted(merged.values(), key=lambda h: (h["kind"], h["value"])),
        "unresolved": unresolved,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    by_kind: dict[str, int] = {}
    for h in inventory["handles"]:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
    unstable = sum(1 for h in inventory["handles"] if not h.get("stable", True))

    print(f"[selectors] источник: {descriptor.get('path') or descriptor.get('url')}"
          + (f" @ {descriptor['commit']}" if descriptor.get("commit") else ""))
    print(f"[selectors] файлов разметки: {n_files}, ручек: {len(inventory['handles'])}")
    for kind in ("testid", "id", "name", "aria"):
        if by_kind.get(kind):
            print(f"[selectors]   {kind:<7} {by_kind[kind]}")
    if unstable:
        print(f"[selectors] нестабильных значений (хеши сборщика): {unstable} — "
              f"картографу их не выбирать")
    if unresolved:
        print(f"[selectors] НЕ РАЗРЕШЕНО: {len(unresolved)} атрибутов с вычисляемым значением")
        print(f"[selectors] это не ноль ручек, а ручки без статического имени — "
              f"их придётся снимать с живой страницы (harvest_selectors.py)")
    if not inventory["handles"]:
        sys.stderr.write("selectors: в источнике нет ни одной адресуемой ручки. "
                         "Исходники есть, но неинформативны — нужен тир B.\n")
        return 2
    print(f"[selectors] записано → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
