#!/usr/bin/env python3
"""
Profile a Qwen Code OpenAI-logging directory to find where wall-clock time goes.

Usage:
    python profile_qwen_logs.py logs/openai            # summary
    python profile_qwen_logs.py logs/openai --schema   # inspect JSON shape first
    python profile_qwen_logs.py logs/openai --csv out.csv

The log format is not formally documented and changes between versions, so run
--schema once and adjust FIELD_CANDIDATES below if anything shows as unknown.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# A cp1251 console mangles the Cyrillic this script prints and raises
# UnicodeEncodeError on «→», turning a successful run into a non-zero exit for a
# reason that has nothing to do with the artefacts. Same guard as gate_check.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Add alternatives here if --schema shows different key names in your version.
FIELD_CANDIDATES = {
    "timestamp": [
        ("timestamp",), ("time",), ("created_at",), ("startTime",),
        ("request", "timestamp"), ("response", "created"),
    ],
    "model": [
        ("model",), ("request", "model"), ("response", "model"),
    ],
    "messages": [
        ("request", "messages"), ("messages",), ("request", "body", "messages"),
    ],
    "prompt_tokens": [
        ("response", "usage", "prompt_tokens"),
        ("usage", "prompt_tokens"),
        ("response", "usage", "input_tokens"),
    ],
    "completion_tokens": [
        ("response", "usage", "completion_tokens"),
        ("usage", "completion_tokens"),
        ("response", "usage", "output_tokens"),
    ],
    "cached_tokens": [
        ("response", "usage", "prompt_tokens_details", "cached_tokens"),
        ("usage", "prompt_tokens_details", "cached_tokens"),
        ("response", "usage", "prompt_cache_hit_tokens"),
        ("usage", "cached_tokens"),
    ],
    "duration_ms": [
        ("duration_ms",), ("durationMs",), ("latency_ms",), ("elapsed_ms",),
    ],
    "tool_calls": [
        ("response", "choices", 0, "message", "tool_calls"),
        ("response", "tool_calls"),
    ],
}


def dig(obj, path):
    cur = obj
    for key in path:
        try:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def extract(obj, field):
    for path in FIELD_CANDIDATES[field]:
        val = dig(obj, path)
        if val is not None:
            return val
    return None


def parse_ts(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # Heuristic: treat large values as milliseconds.
        secs = val / 1000.0 if val > 1e11 else float(val)
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def walk_keys(obj, prefix="", depth=0, out=None, max_depth=3):
    if out is None:
        out = []
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            kind = type(v).__name__
            out.append(f"{path}  ({kind})")
            walk_keys(v, path, depth + 1, out, max_depth)
    elif isinstance(obj, list) and obj:
        walk_keys(obj[0], f"{prefix}[0]", depth + 1, out, max_depth)
    return out


def load_records(log_dir):
    records = []
    for path in sorted(Path(log_dir).rglob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  skipped {path.name}: {exc}", file=sys.stderr)
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict):
                entry["__file"] = path.name
                records.append(entry)
    return records


def build_rows(records):
    rows = []
    for rec in records:
        msgs = extract(rec, "messages")
        tool_calls = extract(rec, "tool_calls")
        rows.append({
            "file": rec.get("__file", ""),
            "ts": parse_ts(extract(rec, "timestamp")),
            "model": extract(rec, "model") or "unknown",
            "messages": len(msgs) if isinstance(msgs, list) else None,
            "prompt_tokens": extract(rec, "prompt_tokens"),
            "completion_tokens": extract(rec, "completion_tokens"),
            "cached_tokens": extract(rec, "cached_tokens"),
            "duration_ms": extract(rec, "duration_ms"),
            "n_tool_calls": len(tool_calls) if isinstance(tool_calls, list) else 0,
        })
    rows.sort(key=lambda r: (r["ts"] is None, r["ts"]))
    return rows


def total(rows, key):
    return sum(r[key] for r in rows if isinstance(r[key], (int, float)))


def report(rows):
    if not rows:
        print("No records found. Check the directory path.")
        return

    timed = [r for r in rows if r["ts"]]
    print(f"Requests: {len(rows)}")

    if len(timed) >= 2:
        span = (timed[-1]["ts"] - timed[0]["ts"]).total_seconds()
        print(f"Wall clock span: {span/60:.1f} min "
              f"({timed[0]['ts']:%H:%M:%S} -> {timed[-1]['ts']:%H:%M:%S})")

        gaps = [((timed[i+1]["ts"] - timed[i]["ts"]).total_seconds(), i)
                for i in range(len(timed) - 1)]
        gap_total = sum(g for g, _ in gaps)
        print(f"Sum of inter-request gaps: {gap_total/60:.1f} min "
              f"({100*gap_total/span:.0f}% of span)")
        print("\nLargest gaps (non-model time: tools, approvals, orchestration):")
        for secs, i in sorted(gaps, reverse=True)[:8]:
            print(f"  {secs:7.1f}s  after request {i+1:>4} "
                  f"({timed[i]['model']}, {timed[i]['n_tool_calls']} tool calls)")

    model_ms = total(rows, "duration_ms")
    if model_ms:
        print(f"\nSum of request durations: {model_ms/60000:.1f} min")

    pt, ct, cached = (total(rows, "prompt_tokens"),
                      total(rows, "completion_tokens"),
                      total(rows, "cached_tokens"))
    print(f"\nTokens  in: {pt:,}   out: {ct:,}   cached: {cached:,}")
    if pt:
        pct = 100 * cached / pt
        print(f"Cache hit rate on input: {pct:.1f}%")
        if pct < 20:
            print("  -> Low. Forks may not be sharing the parent prefix; "
                  "check fork usage and prompt stability.")

    by_model = {}
    for r in rows:
        m = by_model.setdefault(r["model"], {"n": 0, "in": 0, "out": 0})
        m["n"] += 1
        for src, dst in (("prompt_tokens", "in"), ("completion_tokens", "out")):
            if isinstance(r[src], (int, float)):
                m[dst] += r[src]
    print("\nPer model:")
    for name, m in sorted(by_model.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {name:<32} {m['n']:>5} req  "
              f"in {m['in']:>10,}  out {m['out']:>9,}")

    sized = [r for r in rows if isinstance(r["prompt_tokens"], (int, float))]
    if len(sized) >= 4:
        q = len(sized) // 4
        first = sum(r["prompt_tokens"] for r in sized[:q]) / q
        last = sum(r["prompt_tokens"] for r in sized[-q:]) / q
        print(f"\nInput-token growth: first quartile avg {first:,.0f} "
              f"-> last quartile avg {last:,.0f}")
        if first and last / first > 2:
            print("  -> Context is ballooning across the run. "
                  "Consider fork_turns to bound inherited history.")

    msg_counts = [r["messages"] for r in rows if r["messages"]]
    if msg_counts:
        print(f"Message-array length: min {min(msg_counts)}, "
              f"max {max(msg_counts)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir")
    ap.add_argument("--schema", action="store_true",
                    help="print the key structure of the first record and exit")
    ap.add_argument("--csv", metavar="PATH", help="write per-request rows to CSV")
    args = ap.parse_args()

    records = load_records(args.log_dir)
    if not records:
        sys.exit(f"No JSON files found under {args.log_dir}")

    if args.schema:
        print(f"Structure of first record ({records[0].get('__file')}):\n")
        for line in walk_keys({k: v for k, v in records[0].items()
                               if k != "__file"}):
            print("  " + line)
        return

    rows = build_rows(records)
    report(rows)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                r = dict(r)
                r["ts"] = r["ts"].isoformat() if r["ts"] else ""
                w.writerow(r)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
