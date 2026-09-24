#!/usr/bin/env python3
"""Глубокий анализ логов GigaCode v2 — поиск узких мест и оптимизаций."""

import json
import argparse
import glob
import os
import sys
from datetime import datetime
from collections import defaultdict

# A cp1251 console mangles the Cyrillic this script prints and raises
# UnicodeEncodeError on «→», turning a successful run into a non-zero exit for a
# reason that has nothing to do with the artefacts. Same guard as gate_check.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Каталог логов — аргумент, а не место, где лежит сам скрипт. Пока файл лежал
# в корне репозитория, «рядом с собой» совпадало с «там, где логи»; после переноса
# в scripts/ он стал искать в scripts/ и молча находить ноль.
DEFAULT_LOGS_DIR = "."


def load_logs(logs_dir=DEFAULT_LOGS_DIR):
    """Загружает все JSON логи в хронологическом порядке."""
    pattern = os.path.join(logs_dir, "openai-*.json")
    files = sorted(glob.glob(pattern))
    logs = []
    for f in files:
        try:
            with open(f, 'r') as fh:
                data = json.load(fh)
                data['_file'] = os.path.basename(f)
                logs.append(data)
        except Exception as e:
            print(f"⚠️  Ошибка чтения {f}: {e}", file=sys.stderr)
    return logs


def extract_agent_type(log):
    """Тип агента по первой строке системного промпта.

    Раньше классификация шла подстроками по всему тексту, и проверка «test case»
    стояла раньше проверок на имена агентов. Системный промпт самого GigaCode CLI
    содержит и «test case», и имена всех агентов (он их перечисляет), поэтому
    все 617 запросов настоящего лога были отнесены к qa-designer — 100 % в одну
    корзину. Первая строка спеки однозначна: `# QA Test Designer`, `# Test Critic`.
    """
    messages = (log.get('request') or {}).get('messages') or []
    head = ''
    for msg in messages:
        if msg.get('role') == 'system':
            content = msg.get('content') or ''
            lines = [l for l in content.strip().splitlines() if l.strip()]
            head = lines[0].strip() if lines else ''
            break
    if not head:
        return 'unknown'

    low = head.lower()
    if low.startswith('#'):
        title = low.lstrip('# ').strip()
        for needle, name in (
            ('qa test designer', 'qa-designer'),
            ('designer', 'qa-designer'),
            ('test critic', 'test-critic'),
            ('critic', 'test-critic'),
            ('requirements analyst', 'requirements-analyst'),
            ('analyst', 'requirements-analyst'),
            ('pytest', 'pytest-stub-writer'),
            ('browser', 'browser-test-writer'),
            ('cartographer', 'ui-cartographer'),
        ):
            if needle in title:
                return name
        return f'agent:{title[:40]}'

    if 'gigacode' in low:
        return 'orchestrator (GigaCode CLI)'
    if 'general-purpose' in low:
        return 'general-purpose'
    if 'file search' in low:
        return 'file-search'
    return 'unknown'

def extract_tool_calls(log):
    """Извлекает инструменты из ответа."""
    choices = (log.get('response') or {}).get('choices') or [{}]
    response = (choices[0] or {}).get('message') or {}
    tool_calls = response.get('tool_calls', [])
    tools = []
    for tc in tool_calls:
        func = tc.get('function', {})
        tools.append({
            'name': func.get('name', 'unknown'),
            'args_preview': func.get('arguments', '')[:200]
        })
    return tools


def analyze_token_efficiency(log):
    """Анализирует эффективность использования токенов."""
    # `response` бывает null: неуспешный запрос пишет ошибку в `error`.
    # На таких записях .get('usage') валился с AttributeError.
    usage = (log.get('response') or {}).get('usage') or {}
    if not usage:
        return None
    prompt = usage.get('prompt_tokens', 0)
    completion = usage.get('completion_tokens', 0)
    cached = usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
    
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'cached_tokens': cached,
        'cache_hit_rate': cached / prompt if prompt > 0 else 0,
        'total_tokens': prompt + completion,
        'output_ratio': completion / prompt if prompt > 0 else 0
    }


TURN_LIMITS = {
    "qa-designer": 60,
    "test-critic": 45,
    "requirements-analyst": 40,
    "pytest-stub-writer": 40,
    "browser-test-writer": 40,
    "ui-cartographer": 40,
}


def report_problems(logs):
    """Режимы отказа, которые иначе приходится искать руками.

    Всё, что печатается ниже, было найдено вручную на логе 18.08 — инструмент
    показывал токены и интервалы, но молчал о том, что 17 % выходного бюджета
    ушло в ответы без единого символа. Молчание инструмента о сбое читается
    как отсутствие сбоя, поэтому раздел печатается всегда, даже пустым.
    """
    errors, truncated, empty = [], [], []
    burned = 0
    total_out = 0

    for log in logs:
        agent = extract_agent_type(log)
        ts = str(log.get("timestamp", ""))[11:19]
        resp = log.get("response")
        usage = (resp or {}).get("usage") or {}
        out = usage.get("completion_tokens") or 0
        total_out += out

        if log.get("error") or not resp:
            msg = log.get("error")
            text = (msg or {}).get("message") if isinstance(msg, dict) else str(msg)
            errors.append((ts, agent, str(text)[:60]))
            continue

        choice = (resp.get("choices") or [{}])[0] or {}
        message = choice.get("message") or {}
        if choice.get("finish_reason") == "length":
            truncated.append((ts, agent, usage.get("prompt_tokens") or 0, out))
        if not (message.get("content") or "").strip() and not message.get("tool_calls"):
            empty.append((ts, agent, out))
            burned += out

    print("\n🚨 Проблемы")

    print(f"\n   Сетевые отказы и запросы без ответа: {len(errors)}")
    for ts, agent, text in errors[:10]:
        print(f"      {ts}  {agent:<26} {text}")

    print(f"\n   Оборвано по длине (finish_reason=length): {len(truncated)}")
    for ts, agent, pt, out in truncated[:10]:
        note = "  ← упёрлись в окно контекста" if pt + out > 250000 else ""
        print(f"      {ts}  {agent:<26} вход {pt:>7}  выход {out:>7}{note}")
    if truncated:
        print("      Оборванный ответ дизайнера = недописанный JSON-кейс: линтер объявит его")
        print("      битым, хотя это сбой инфраструктуры, а не дефект кейса.")

    share = burned / total_out * 100 if total_out else 0
    print(f"\n   Ответы без текста и без вызовов: {len(empty)}, "
          f"сожжено {burned:,} выходных токенов ({share:.1f} % выхода)")
    for ts, agent, out in sorted(empty, key=lambda r: -r[2])[:6]:
        print(f"      {ts}  {agent:<26} выход {out:>7}")

    if not (errors or truncated or empty):
        print("      Ни одного — все ответы содержательны.")


def session_key(log):
    """Идентификатор сессии агента по тексту его задания.

    Первое пользовательское сообщение у GigaCode служебное и одинаково во всех
    сессиях, поэтому ключ — второе: это бриф, а у двух параллельных дизайнеров
    брифы разные (разные journey). Возвращает None, если брифа нет.
    """
    import hashlib
    msgs = (log.get("request") or {}).get("messages") or []
    users = [str(m.get("content")) for m in msgs if m.get("role") == "user"]
    if len(users) < 2:
        return None
    return hashlib.sha1(users[1][:600].encode("utf-8")).hexdigest()[:12]


def log_time(log):
    """Момент запроса, или None. Длительности в логе нет — см. e2e:profile."""
    ts = log.get("timestamp") or ""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def report_concurrency(logs):
    """Параллельность под-агентов — реальная или номинальная (O-00 / P-01).

    Вопрос открыт с июля и до сих пор закрывался только «на вашей инфраструктуре»:
    план предлагал отдельный прогон A/B — один journey против трёх. Такой прогон
    не нужен. Ответ виден в любом обычном логе, если смотреть на порядок запросов.

    Механизм. У роли, запущенной в N экземплярах, каждый экземпляр ведёт свою
    беседу и опознаётся своим брифом (`session_key`). Если экземпляры действительно
    идут одновременно, их запросы во времени ЧЕРЕДУЮТСЯ: A B A B B A. Если бэкенд
    выполняет их по очереди, те же запросы лягут СПЛОШНЫМИ БЛОКАМИ: A A A B B B.
    Считаем переключения между соседними запросами роли. Минимум, возможный при
    любом порядке, — (число сессий − 1); это и есть строго последовательный случай.

    Почему именно так, а не по длительностям: длительности запроса в логе нет
    (`duration_ms` отсутствует), поэтому пересечение интервалов посчитать нельзя,
    а порядок событий — можно.

    Цена ответа. Если параллельность номинальная, ВСЕ веерные пункты плана
    (P-50 веер вариантов, P-51 разделение аналитика, P-52 снятие лимита 3)
    не стоят ничего — агенты просто стоят в очереди к одному эндпоинту, — и
    усилия надо тратить на объём выхода. Ошибиться здесь дорого в обе стороны,
    поэтому «не хватает данных» печатается как отдельный исход, а не как «нет».
    """
    per_role = defaultdict(list)
    for log in logs:
        key = session_key(log)
        when = log_time(log)
        if key is None or when is None:
            continue
        per_role[extract_agent_type(log)].append((when, key))

    print("\n🔀 Параллельность под-агентов (O-00 / P-01)")

    if not per_role:
        print("   не определено: в логе нет ни одной записи с брифом и временем")
        return

    verdicts = {}
    for role in sorted(per_role):
        events = sorted(per_role[role])
        keys = [k for _, k in events]
        n_sessions = len(set(keys))
        if n_sessions < 2:
            print(f"   {role:<28} одна сессия — сравнивать не с чем")
            continue
        switches = sum(1 for a, b in zip(keys, keys[1:]) if a != b)
        floor = n_sessions - 1            # строго последовательный порядок
        ceiling = len(keys) - 1           # максимально возможное чередование
        if ceiling == floor:
            print(f"   {role:<28} по одному запросу на сессию — не определено")
            continue
        ratio = (switches - floor) / (ceiling - floor)
        if ratio >= 0.5:
            verdict, mark = "ПАРАЛЛЕЛЬНО", "✅"
        elif ratio <= 0.1:
            verdict, mark = "последовательно", "⛔"
        else:
            verdict, mark = "неоднозначно", "❓"
        verdicts[role] = verdict
        print(f"   {mark} {role:<26} сессий {n_sessions}, запросов {len(keys)}, "
              f"переключений {switches} при минимуме {floor} → {verdict}")

    if not verdicts:
        print("   вывод: данных не хватает. Нужен прогон, где хотя бы одна роль "
              "запускалась больше одного раза — иначе чередовать нечего")
        return

    if "ПАРАЛЛЕЛЬНО" in verdicts.values():
        print("   ВЫВОД: параллельность реальная — веерные пункты плана "
              "(P-50, P-51, P-52) имеют смысл")
    elif all(v == "последовательно" for v in verdicts.values()):
        print("   ВЫВОД: параллельность НОМИНАЛЬНАЯ — агенты стоят в очереди "
              "к одному эндпоинту.")
        print("   Любой веер (P-50, P-51, снятие лимита P-52) не даст ничего: "
              "он не добавляет пропускной способности.")
        print("   Тратьте усилия на объём выхода и входа, а не на распараллеливание.")
    else:
        print("   ВЫВОД: неоднозначно — не принимайте решение о веере по этому логу")


def report_turn_budget(logs):
    """Сколько ходов агенты действительно тратят против своих лимитов.

    Ход — один запрос. Сессия опознаётся по тексту задания (см. session_key).
    """
    sessions = {}
    for log in logs:
        key = session_key(log)
        if key is None:
            continue
        rec = sessions.setdefault(key, {"n": 0, "agent": extract_agent_type(log)})
        rec["n"] += 1

    per = defaultdict(list)
    for rec in sessions.values():
        per[rec["agent"]].append(rec["n"])

    print("\n🎚  Расход ходов против лимита")
    print(f"   {'агент':<28} {'сессий':>7} {'макс':>6} {'медиана':>8} {'лимит':>6}")
    for agent in sorted(per, key=lambda a: -max(per[a])):
        lens = sorted(per[agent])
        mx, med = lens[-1], lens[len(lens) // 2]
        lim = TURN_LIMITS.get(agent)
        flag = ""
        if lim:
            if mx >= lim:
                flag = "  ← УПЁРСЯ В ЛИМИТ"
            elif mx > lim * 0.75:
                flag = f"  ← {round(100 * mx / lim)} % лимита"
        print(f"   {agent:<28} {len(lens):>7} {mx:>6} {med:>8} "
              f"{(lim if lim else '—'):>6}{flag}")


def main():
    ap = argparse.ArgumentParser(description="Глубокий анализ логов GigaCode")
    ap.add_argument("logs_dir", nargs="?", default=DEFAULT_LOGS_DIR,
                    help="каталог с файлами openai-*.json (по умолчанию текущий)")
    args = ap.parse_args()

    print("=" * 80)
    print("GigaCode Logs Analysis v2 — Deep Dive")
    print("=" * 80)

    logs = load_logs(args.logs_dir)
    if not logs:
        # Пустой результат обязан отличаться от успешного: код 2 и путь в сообщении,
        # иначе «логов нет» читается как «всё в порядке, просто нечего сказать».
        print(f"❌ Логи не найдены: {os.path.join(args.logs_dir, 'openai-*.json')}",
              file=sys.stderr)
        return 2
    
    total_requests = len(logs)
    print(f"\n📊 Всего запросов: {total_requests}")
    
    # 1. Временные диапазоны
    timestamps = []
    for log in logs:
        ts_str = log.get('timestamp', '')
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                timestamps.append(ts)
            except:
                pass
    
    if timestamps:
        first = timestamps[0]
        last = timestamps[-1]
        span = (last - first).total_seconds()
        print(f"⏱  Диапазон: {first} → {last}")
        print(f"⏱  Общее время: {span:.0f}s ({span/60:.1f} мин)")
    
    # 2. Распределение по агентам
    agent_dist = defaultdict(int)
    agent_requests = defaultdict(list)
    for i, log in enumerate(logs):
        agent = extract_agent_type(log)
        agent_dist[agent] += 1
        agent_requests[agent].append((i, log))
    
    print(f"\n🤖 Распределение по агентам:")
    for agent, count in sorted(agent_dist.items(), key=lambda x: -x[1]):
        print(f"   {agent:25s}: {count:4d} req ({count/total_requests*100:.1f}%)")
    
    # 3. Эффективность токенов
    print(f"\n📈 Анализ токенов:")
    total_prompt = 0
    total_completion = 0
    total_cached = 0
    agent_tokens = defaultdict(lambda: {'prompt': 0, 'completion': 0, 'cached': 0})
    
    for log in logs:
        eff = analyze_token_efficiency(log)
        if eff:
            total_prompt += eff['prompt_tokens']
            total_completion += eff['completion_tokens']
            total_cached += eff['cached_tokens']
            agent = extract_agent_type(log)
            agent_tokens[agent]['prompt'] += eff['prompt_tokens']
            agent_tokens[agent]['completion'] += eff['completion_tokens']
            agent_tokens[agent]['cached'] += eff['cached_tokens']
    
    cache_rate = total_cached / total_prompt if total_prompt > 0 else 0
    print(f"   Входящих токенов:     {total_prompt:>12,}")
    print(f"   Исходящих токенов:    {total_completion:>12,}")
    print(f"   Закешированных:       {total_cached:>12,}")
    print(f"   Cache hit rate:       {cache_rate*100:.1f}%")
    print(f"   avg output/prompt:    {total_completion/total_prompt*100:.2f}%")
    
    print(f"\n   Токены по агентам:")
    for agent, tokens in sorted(agent_tokens.items(), key=lambda x: -x[1]['prompt']):
        cr = tokens['cached'] / tokens['prompt'] * 100 if tokens['prompt'] > 0 else 0
        ratio = tokens['completion'] / tokens['prompt'] * 100 if tokens['prompt'] > 0 else 0
        print(f"      {agent:25s}: in={tokens['prompt']:>10,}  out={tokens['completion']:>7,}  cache={cr:5.1f}%  ratio={ratio:5.2f}%")
    
    # 4. Анализ интервалов между запросами
    print(f"\n⏱  Анализ интервалов:")
    gaps = []
    for i in range(1, len(timestamps)):
        gap = (timestamps[i] - timestamps[i-1]).total_seconds()
        gaps.append((i, gap, logs[i]))
    
    if gaps:
        avg_gap = sum(g for _, g, _ in gaps) / len(gaps)
        max_gap = max(gaps, key=lambda x: x[1])
        min_gap = min(gaps, key=lambda x: x[1])
        
        print(f"   Средний интервал:     {avg_gap:.1f}s")
        print(f"   Минимальный:          {min_gap[1]:.1f}s (запрос {min_gap[0]})")
        print(f"   Максимальный:         {max_gap[1]:.1f}s (запрос {max_gap[0]})")
        
        # Распределение интервалов
        buckets = {'0-5s': 0, '5-15s': 0, '15-30s': 0, '30-60s': 0, '1-3min': 0, '3-10min': 0, '>10min': 0}
        for idx, gap, _ in gaps:
            if gap < 5:
                buckets['0-5s'] += 1
            elif gap < 15:
                buckets['5-15s'] += 1
            elif gap < 30:
                buckets['15-30s'] += 1
            elif gap < 60:
                buckets['30-60s'] += 1
            elif gap < 180:
                buckets['1-3min'] += 1
            elif gap < 600:
                buckets['3-10min'] += 1
            else:
                buckets['>10min'] += 1
        
        print(f"\n   Распределение интервалов:")
        for bucket, count in buckets.items():
            pct = count / len(gaps) * 100
            bar = '█' * int(pct / 2)
            print(f"      {bucket:>8s}: {count:4d} ({pct:5.1f}%) {bar}")
    
    # 5. Топ-10 самых долгих интервалов — детальный анализ
    print(f"\n🔍 Топ-15 самых долгих интервалов (не модельное время):")
    top_gaps = sorted(gaps, key=lambda x: -x[1])[:15]
    for idx, gap, log in top_gaps:
        agent = extract_agent_type(log)
        tools = extract_tool_calls(log)
        eff = analyze_token_efficiency(log)
        
        tool_names = [t['name'] for t in tools]
        tool_str = ', '.join(tool_names) if tool_names else 'no tools'
        
        cache_pct = eff['cache_hit_rate'] * 100 if eff else 0
        
        print(f"   #{idx:>3d}  {gap:6.1f}s  agent={agent:20s}  tools=[{tool_str}]  cache={cache_pct:5.1f}%  out={eff['completion_tokens'] if eff else 0:5d}")
        
        # Покажем context windows для самых больших
        if gap > 60:
            messages = log.get('request', {}).get('messages', [])
            total_chars = sum(len(str(m.get('content', ''))) for m in messages)
            print(f"         context: {len(messages)} messages, ~{total_chars:,} chars system+user")
    
    # 6. Анализ tool calls
    print(f"\n🔧 Анализ инструментов:")
    tool_usage = defaultdict(int)
    for log in logs:
        tools = extract_tool_calls(log)
        for t in tools:
            tool_usage[t['name']] += 1
    
    for tool, count in sorted(tool_usage.items(), key=lambda x: -x[1]):
        pct = count / total_requests * 100
        print(f"   {tool:25s}: {count:4d} ({pct:5.1f}%)")
    
    # 7. Анализ оркестратора — batch calls
    print(f"\n📦 Параллельные вызовы (batch):")
    batch_count = 0
    max_batch_size = 0
    for i, log in enumerate(logs):
        tools = extract_tool_calls(log)
        # Ищем agent calls в инструментах
        agent_calls = [t for t in tools if t['name'] == 'agent']
        if len(agent_calls) > 1:
            batch_count += 1
            max_batch_size = max(max_batch_size, len(agent_calls))
            # Показываем первый agent call
            first = agent_calls[0]
            args = first.get('args_preview', '')
            try:
                args_dict = json.loads(args) if isinstance(args, str) else args
                sub = args_dict.get('subagent_type', '')
                desc = args_dict.get('description', '')[:50]
                print(f"   batch@#{i}: {len(agent_calls)} agents → sub={sub}, desc={desc}")
            except:
                pass
    
    print(f"   Batch-вызовов (>1 agent): {batch_count}")
    print(f"   Макс. размер батча:      {max_batch_size}")
    
    # 8. Анализ эффективности по фазам
    print(f"\n📊 Анализ по фазам:")
    # defaultdict, а не фиксированный словарь: любой агент вне карты — включая
    # `general-purpose`, `file-search` и «unknown» — ронял разбор по фазам
    # KeyError'ом, и до этого места отчёт просто обрывался.
    phases = defaultdict(list)
    phase_map = {
        'requirements-analyst': 'Фаза 1 — аналитик',
        'qa-designer': 'Фаза 2 — дизайнер',
        'test-critic': 'Фаза 2 — критик',
        'pytest-stub-writer': 'Фаза 2.5 — писатель тестов',
        'browser-test-writer': 'Фаза 2.5 — браузерные тесты',
        'orchestrator (GigaCode CLI)': 'оркестратор и шлюзы',
    }
    
    for i, log in enumerate(logs):
        agent = extract_agent_type(log)
        phase = phase_map.get(agent, f'прочее — {agent}')
        eff = analyze_token_efficiency(log)
        if eff:
            phases[phase].append((i, eff, log))
    
    for phase, reqs in phases.items():
        if not reqs:
            continue
        # e — уже словарь метрик; e[1] индексировал его как последовательность.
        total_out = sum(e['completion_tokens'] for _, e, _ in reqs)
        total_in = sum(e['prompt_tokens'] for _, e, _ in reqs)
        avg_out = total_out / len(reqs)
        avg_in = total_in / len(reqs)
        print(f"   {phase:35s}: {len(reqs):3d} req, avg in={avg_in:>8,.0f}, avg out={avg_out:>7,.0f}, total out={total_out:>9,}")
    
    # 9. Ключевые метрики для оптимизации
    print(f"\n{'='*80}")
    print("🎯 КЛЮЧЕВЫЕ НАБЛЮДЕНИЯ ДЛЯ ОПТИМИЗАЦИИ:")
    print(f"{'='*80}")
    
    # Находим агентов с самым большим контекстом
    print(f"\n1. Агенты с самым большим средним контекстом (prompt_tokens):")
    agent_avg_prompt = {}
    for agent, reqs in agent_requests.items():
        total_p = 0
        count = 0
        for _, log in reqs:
            eff = analyze_token_efficiency(log)
            if eff:
                total_p += eff['prompt_tokens']
                count += 1
        if count > 0:
            agent_avg_prompt[agent] = total_p / count
    
    for agent, avg in sorted(agent_avg_prompt.items(), key=lambda x: -x[1])[:5]:
        print(f"   {agent:25s}: {avg:10,.0f} avg prompt tokens")
    
    # Находим запросы с самым низким output/prompt ratio
    print(f"\n2. Запросы с самым низким соотношением output/prompt (дорогой контекст):")
    inefficient = []
    for i, log in enumerate(logs):
        eff = analyze_token_efficiency(log)
        if eff and eff['prompt_tokens'] > 1000:
            inefficient.append((i, eff, extract_agent_type(log)))
    
    inefficient.sort(key=lambda x: x[1]['output_ratio'])
    for idx, eff, agent in inefficient[:10]:
        print(f"   #{idx:>3d}  ratio={eff['output_ratio']:.4f}  in={eff['prompt_tokens']:>8,}  out={eff['completion_tokens']:>6,}  agent={agent}")
    
    # Находим паттерн: чтение файлов → большие интервалы
    print(f"\n3. Паттерн: read_file / read_many_calls перед большими интервалами:")
    for idx, gap, log in top_gaps[:5]:
        if idx > 0:
            prev_log = logs[idx - 1]
            prev_tools = extract_tool_calls(prev_log)
            tool_names = [t['name'] for t in prev_tools]
            if any(t in tool_names for t in ['read_file', 'read_many_files', 'glob', 'grep_search']):
                print(f"   Перед интервалом {gap:.1f}s (#{idx}) был tool: {tool_names}")

    # Разделы ниже печатаются последними намеренно: их читают, даже когда всё
    # остальное пролистывают.
    report_turn_budget(logs)
    report_concurrency(logs)
    report_problems(logs)
    print()


if __name__ == '__main__':
    sys.exit(main() or 0)
