#!/usr/bin/env python3
"""Инвентарь адресуемых ручек с ЖИВОЙ СТРАНИЦЫ — тир B.

Слой 1 из трёх, ветка «исходников нет или они неинформативны». На этом корпусе
вторая ветка — ожидаемая, а не краевая: спека браузерного писателя (правило 12)
прямо говорит, что кнопки лежат внутри **Shadow DOM**, а значит подметание
исходников компонентного фреймворка отрендеренного дерева не покажет.

Чем тир B отличается от тира A — и почему их нельзя смешивать
-------------------------------------------------------------
Тир A (`extract_selectors.py`) — ручка объявлена разработчиком: намеренный
контракт. Тир B — ручка НАБЛЮДЕНА в текущей сборке. Она настоящая, но она не
обещание: тест, построенный на ней, фиксирует сегодняшний DOM вместе с его
багами. Поэтому `tier` пишется в каждую запись, а `check_selectors.py` считает
тиры отдельно и говорит об этом в отчёте.

Тавтология — главный риск этой ветки
------------------------------------
Если снять селектор со страницы, а потом утверждать, что элемент на странице
есть, — тест проходит по построению и не проверяет ничего. Ровно этот дефект
лежит в готовых сьютах: `test_phone_input_visible_and_active` логинится, делает
скриншот и не содержит ни одного `assert`.

Отсюда граница, которую этот скрипт держит физически: он собирает **адресацию**
и только её. Ни одного поля про то, что элемент должен делать, в выходном JSON
нет и быть не может — ожидания приходят из кейса, то есть из требований.

Shadow DOM
----------
Обход рекурсивный через `shadowRoot`, тем же способом, который спека писателя уже
требует применять в коде. Открытые корни обходятся; закрытые (`mode: 'closed'`)
недоступны из скрипта в принципе — они считаются и печатаются отдельной строкой,
потому что «внутрь не заглянули» и «внутри пусто» — разные исходы.

Запуск:

    python3 scripts/harvest_selectors.py --url http://localhost:3000/login
    python3 scripts/harvest_selectors.py --url ... --selenium-url http://localhost:4444
    python3 scripts/harvest_selectors.py --url ... --route /playlists --route /profile

Коды выхода: 0 — инвентарь записан, 2 — снять не удалось (нет selenium, стенд
не отвечает, страница пуста). Код 2 — это «не проверено», никогда не «пусто».
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Обход DOM вместе с открытыми shadow-корнями. Возвращает плоский список ручек.
# Скрипт намеренно НЕ собирает текст, состояние, видимость и любые признаки
# поведения — см. раздел про тавтологию в докстринге.
HARVEST_JS = r"""
const TESTID = ['data-testid','data-test-id','data-test','data-qa','data-cy'];
const out = [];
let closedRoots = 0;
let seen = 0;

function visit(root, depth) {
  if (depth > 40) return;
  let nodes;
  try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
  for (const el of nodes) {
    seen++;
    const tag = el.tagName ? el.tagName.toLowerCase() : '?';
    for (const a of TESTID) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) out.push({kind:'testid', anchor:a, value:v, tag:tag});
    }
    if (el.id) out.push({kind:'id', anchor:'id', value:el.id, tag:tag});
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) out.push({kind:'name', anchor:'name', value:nm, tag:tag});
    const role = el.getAttribute && el.getAttribute('role');
    const label = (el.getAttribute && (el.getAttribute('aria-label')
                  || el.getAttribute('aria-labelledby'))) || '';
    if (role && label) {
      out.push({kind:'aria', anchor:'role+aria-label',
                value: role + '|' + label, tag:tag, role:role, accessibleName:label});
    }
    if (el.shadowRoot) visit(el.shadowRoot, depth + 1);
    else if (el.shadowRoot === null && el.attachShadow && el.localName
             && el.localName.includes('-')) closedRoots++;
  }
}
visit(document, 0);
return {handles: out, closedRoots: closedRoots, elementsSeen: seen,
        url: location.href, title: document.title};
"""

# Значение, которое заведомо не является стабильным якорем.
import re  # noqa: E402  (после докстринга, чтобы он оставался первым)

UNSTABLE = re.compile(
    r"^(?:sc-[0-9a-z]{6,}|css-[0-9a-z]{6,}|:r[0-9a-z]+:|[0-9a-f]{8,}"
    r"|mui-\d+|radix-[-:\w]+|headlessui-[-:\w]+)$", re.I)


def build_driver(selenium_url: str | None, headless: bool):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        sys.stderr.write(
            "harvest: пакет selenium не установлен — снять живую страницу нечем.\n"
            "harvest: это «не проверено», а не «ручек нет». "
            "pip install selenium, либо соберите инвентарь из исходников.\n")
        return None

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--no-sandbox")
    try:
        if selenium_url:
            return webdriver.Remote(command_executor=selenium_url, options=opts)
        return webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 — драйвер падает десятком разных классов
        sys.stderr.write(f"harvest: драйвер не поднялся: {type(exc).__name__}: {exc}\n")
        sys.stderr.write("harvest: стенд или Selenium-сервер недоступен — инвентарь НЕ снят\n")
        return None


def harvest(driver, url: str, wait: float) -> dict | None:
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        driver.get(url)
        WebDriverWait(driver, wait).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
        return driver.execute_script(HARVEST_JS)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"harvest: {url} — {type(exc).__name__}: {exc}\n")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Инвентарь адресуемых ручек с живой страницы (тир B)")
    ap.add_argument("--url", required=True, help="адрес стартовой страницы стенда")
    ap.add_argument("--route", action="append", default=[],
                    help="дополнительный маршрут относительно --url; можно повторять")
    ap.add_argument("--selenium-url", help="адрес Selenium-сервера (RemoteWebDriver)")
    ap.add_argument("--out", default="output/ui/selectors.json")
    ap.add_argument("--merge", action="store_true",
                    help="дополнить инвентарь, а не заменить (тир A остаётся на месте)")
    ap.add_argument("--wait", type=float, default=20.0, help="ожидание загрузки, секунд")
    ap.add_argument("--headed", action="store_true",
                    help="показать браузер (нужно, когда вход требует рук — см. ниже)")
    args = ap.parse_args()

    driver = build_driver(args.selenium_url, headless=not args.headed)
    if driver is None:
        return 2

    pages: list[dict] = []
    try:
        targets = [args.url] + [args.url.rstrip("/") + "/" + r.lstrip("/") for r in args.route]
        for target in targets:
            page = harvest(driver, target, args.wait)
            if page:
                pages.append(page)
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001 — сессия могла умереть раньше teardown
            pass

    if not pages:
        sys.stderr.write("harvest: ни одна страница не снята — инвентарь НЕ построен\n")
        return 2

    merged: dict[str, dict] = {}
    closed_total = 0
    seen_total = 0
    for page in pages:
        closed_total += int(page.get("closedRoots") or 0)
        seen_total += int(page.get("elementsSeen") or 0)
        for h in page.get("handles", []):
            value = (h.get("value") or "").strip()
            if not value:
                continue
            hid = f"{h['kind']}:{value}"
            rec = merged.setdefault(hid, {
                "handleId": hid,
                "kind": h["kind"],
                "anchor": h["anchor"],
                "value": value,
                "tier": "B",
                "tag": h.get("tag"),
                "stable": not bool(UNSTABLE.match(value)),
                "origins": [],
            })
            if h.get("role"):
                rec["role"] = h["role"]
            if h.get("accessibleName"):
                rec["accessibleName"] = h["accessibleName"]
            rec["origins"].append(page.get("url") or "")
    for rec in merged.values():
        rec["origins"] = sorted({o for o in rec["origins"] if o})

    out = Path(args.out)
    existing = {"handles": [], "sources": []}
    if args.merge and out.is_file():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.stderr.write(f"harvest: существующий {out} нечитаем — слияние отменено\n")
            return 2
        # Тир A старше: объявленный контракт не затирается наблюдением.
        keep = {h["handleId"]: h for h in existing.get("handles", []) if h.get("tier") == "A"}
        merged = merged | keep

    descriptor = {"kind": "live", "url": args.url,
                  "pages": [p.get("url") for p in pages],
                  "closedShadowRoots": closed_total}
    inventory = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [s for s in existing.get("sources", []) if s.get("kind") != "live"]
                   + [descriptor],
        "handles": sorted(merged.values(), key=lambda h: (h["kind"], h["value"])),
        "unresolved": existing.get("unresolved", []),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    tier_b = sum(1 for h in inventory["handles"] if h.get("tier") == "B")
    tier_a = sum(1 for h in inventory["handles"] if h.get("tier") == "A")
    unstable = sum(1 for h in inventory["handles"] if not h.get("stable", True))

    print(f"[harvest] страниц снято: {len(pages)}, элементов обойдено: {seen_total}")
    print(f"[harvest] ручек всего {len(inventory['handles'])}: тир B {tier_b}, тир A {tier_a}")
    if unstable:
        print(f"[harvest] нестабильных значений (хеши CSS-in-JS): {unstable} — "
              f"картографу их не выбирать")
    if closed_total:
        print(f"[harvest] ЗАКРЫТЫХ shadow-корней: {closed_total} — внутрь заглянуть нельзя.")
        print(f"[harvest] Это не «там пусто»: элементы за ними в инвентарь не попали, "
              f"и шаги на них станут объявленным пробелом, а не выдуманным селектором.")
    print(f"[harvest] ТИР B — это наблюдение текущей сборки, а не контракт из требований.")
    print(f"[harvest] записано → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
