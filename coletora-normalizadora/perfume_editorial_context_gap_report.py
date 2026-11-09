#!/usr/bin/env python3
"""Report recurring perfume products that lack editorial_context.json entries.

Non-mutating. Uses real WhatsApp tap promos and the normalizer's product extraction
so the backlog reflects production misses, not random bank folders.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

TAP_FILE = Path.home() / ".hermes/whatsapp/taps/messages.jsonl"
NORMALIZER = Path.home() / ".hermes/scripts/perfume_manual_normalizer.py"
OUT_ROOT = Path("/tmp")

PERFUME_HINT = re.compile(r"(?i)(perfume|edt|edp|eau de|parfum|col[oô]nia|lattafa|afnan|armaf|rasasi|azzaro|banderas|natura|botic[aá]rio|dolce|gabbana|malbec|kaiak|maison|alhambra|french avenue|rayhaan|khamrah|hawas|asad|nautica|davidoff|versace|ysl|rabanne)")


def load_normalizer():
    spec = importlib.util.spec_from_file_location("pmn", NORMALIZER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def msg_ts(raw) -> int:
    if isinstance(raw, dict):
        try:
            return int(raw.get("low") or 0)
        except Exception:
            return 0
    try:
        return int(raw or 0)
    except Exception:
        return 0


def has_context(pmn, product: str) -> bool:
    return bool(pmn.load_editorial_context(product))


def has_approved_image_fast(pmn, product: str) -> bool:
    try:
        for d in pmn.approved_final_candidates(product):
            if d.is_dir() and any(p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'} for p in d.iterdir()):
                return True
    except Exception:
        return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--since-hours", type=int, default=720)
    ap.add_argument("--min-count", type=int, default=1)
    args = ap.parse_args()

    pmn = load_normalizer()
    cutoff = int(datetime.now(timezone.utc).timestamp()) - args.since_hours * 3600
    rows = []
    total = 0
    with TAP_FILE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            ts = msg_ts(msg.get("timestamp"))
            if ts and ts < cutoff:
                continue
            body = str(msg.get("body") or "").strip()
            if not body or not PERFUME_HINT.search(body):
                continue
            product = pmn.extract_product(body)
            if not product:
                continue
            key = pmn.product_key(product)
            total += 1
            rows.append({
                "product": product,
                "product_key": key,
                "body": body,
                "chatId": str(msg.get("chatId") or ""),
                "has_media": bool(msg.get("hasMedia")),
                "timestamp": ts,
                "has_context": has_context(pmn, product),
                "has_approved_image": has_approved_image_fast(pmn, product),
            })
            if len(rows) >= args.limit:
                break

    by_key = {}
    examples = defaultdict(list)
    counts = Counter()
    image_counts = Counter()
    for r in rows:
        key = r["product_key"]
        if r["has_context"]:
            continue
        counts[key] += 1
        if r["has_approved_image"]:
            image_counts[key] += 1
        by_key.setdefault(key, r["product"])
        if len(examples[key]) < 3:
            examples[key].append(r["body"].splitlines()[0][:120])

    items = []
    for key, count in counts.most_common():
        if count < args.min_count:
            continue
        items.append({
            "product_key": key,
            "product": by_key.get(key, key),
            "count": count,
            "approved_image_hits": image_counts[key],
            "examples": examples[key],
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"perfume_editorial_context_gaps_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "gaps.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n", encoding="utf-8")

    md = []
    md.append("# Perfume editorial context gaps")
    md.append("")
    md.append(f"tap_products_scanned: {total}")
    md.append(f"unique_missing_context: {len(items)}")
    md.append(f"output: {out_dir}")
    md.append("")
    md.append("| # | count | approved img hits | product_key | product | examples |")
    md.append("|---:|---:|---:|---|---|---|")
    for i, item in enumerate(items[:200], 1):
        ex = "<br>".join(item["examples"]).replace("|", "\\|")
        product_md = item["product"].replace("|", "\\|")
        md.append(f"| {i} | {item['count']} | {item['approved_image_hits']} | `{item['product_key']}` | {product_md} | {ex} |")
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(out_dir)
    print(f"tap_products_scanned={total} unique_missing_context={len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
