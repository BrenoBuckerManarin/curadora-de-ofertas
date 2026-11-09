#!/usr/bin/env python3
"""Actionable calibration report for Perfume Collector observations."""
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perfume_deliver as collector

BANK = Path.home() / ".hermes/perfume_image_bank"
BASELINE = BANK / "logs/collector_24h_baseline.json"
HISTORY = BANK / "logs/collector_price_history.jsonl"
PREFS = BANK / "collector_preferences.json"
REPORT = BANK / "logs/collector_24h_report.json"
FEEDBACK_REPORT = BANK / "logs/collector_feedback_report.json"


def load_rows(started_at: int):
    rows = []
    seen = set()
    if not HISTORY.exists():
        return rows
    for line in HISTORY.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if int(row.get("ts") or 0) < started_at:
            continue
        price = round(float(row.get("effective_price") or 0), 2)
        key = collector.canonical_product_key(str(row.get("product") or ""))
        sig = (key, price, row.get("link"))
        if sig in seen or price <= 0:
            continue
        seen.add(sig)
        row = dict(row)
        row["product_key"] = key
        row["category"] = collector.classify_offer_category(str(row.get("product") or ""), "")
        rows.append(row)
    return rows


def main():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    prefs = json.loads(PREFS.read_text(encoding="utf-8"))
    generated_at = int(time.time())
    started_at = max(int(baseline["started_at"]), generated_at - 24 * 3600)
    rows = load_rows(started_at)
    by_category = defaultdict(list)
    by_sku = defaultdict(list)
    source_groups = defaultdict(set)
    source_counts = defaultdict(int)
    for row in rows:
        category = row.get("category") or "other"
        price = float(row["effective_price"])
        by_category[category].append(price)
        by_sku[row["product_key"]].append(price)
        source_counts[str(row.get("source") or "unknown")] += 1
        bucket = int(round(price / 5.0) * 5)
        source_groups[(row["product_key"], bucket)].add(row.get("source") or "unknown")

    categories = {}
    for category, prices in sorted(by_category.items()):
        ordered = sorted(prices)
        excellent = ((prefs.get("category_price_bands") or {}).get(category) or {}).get("excellent")
        good = ((prefs.get("category_price_bands") or {}).get(category) or {}).get("good")
        categories[category] = {
            "observations": len(ordered), "min": ordered[0], "median": statistics.median(ordered),
            "p25": ordered[max(0, int((len(ordered) - 1) * 0.25))],
            "configured_excellent": excellent, "configured_good": good,
            "excellent_count": sum(bool(excellent and p <= excellent) for p in ordered),
            "good_or_better_count": sum(bool(good and p <= good) for p in ordered),
        }

    consensus = [
        {"product_key": key[0], "price_bucket": key[1], "sources": sorted(sources), "count": len(sources)}
        for key, sources in source_groups.items() if len(sources) >= 2
    ]
    repeated_skus = sorted(
        ({"product_key": sku, "observations": len(values), "min": min(values), "median": statistics.median(values)}
         for sku, values in by_sku.items() if len(values) >= 2),
        key=lambda x: (-x["observations"], x["product_key"]),
    )

    opportunity_rows = []
    for row in rows:
        category = row["category"]
        price = float(row["effective_price"])
        band = (prefs.get("category_price_bands") or {}).get(category) or {}
        excellent = float(band.get("excellent") or 0)
        public = float(row.get("public_price") or 0)
        depth = (excellent - price) / excellent if excellent else 0
        discount = (public - price) / public if public > price else 0
        calibration_score = round(depth * 100 + discount * 60, 1)
        opportunity_rows.append({
            "product": row.get("product"), "product_key": row["product_key"], "category": category,
            "price": price, "public_price": public or None, "source": row.get("source"),
            "link": row.get("link"), "calibration_score": calibration_score,
        })
    top_opportunities = sorted(opportunity_rows, key=lambda x: x["calibration_score"], reverse=True)[:10]

    elapsed_hours = max(1 / 60, (datetime.now().astimezone().timestamp() - started_at) / 3600)
    observed_per_hour = round(len(rows) / elapsed_hours, 2)
    sku_per_hour = round(len(by_sku) / elapsed_hours, 2)
    other_share = round(len(by_category.get("other", [])) / len(rows), 3) if rows else 0
    feedback = {}
    if FEEDBACK_REPORT.exists():
        try:
            feedback = json.loads(FEEDBACK_REPORT.read_text(encoding="utf-8"))
        except Exception:
            feedback = {}

    report = {
        "started_at": datetime.fromtimestamp(started_at).astimezone().isoformat(),
        "generated_at": datetime.fromtimestamp(generated_at).astimezone().isoformat(),
        "capacity": {"observations_per_hour": observed_per_hour, "canonical_skus_per_hour": sku_per_hour,
                     "supports_one_per_10min_before_validation": sku_per_hour >= 6},
        "unique_observations": len(rows), "unique_skus": len(by_sku), "other_share": other_share,
        "categories": categories,
        "source_observations": dict(sorted(source_counts.items(), key=lambda x: (-x[1], x[0]))),
        "independent_source_consensus": consensus,
        "top_opportunities": top_opportunities,
        "repeated_skus": repeated_skus[:20],
        "normalizer_feedback": {"sample_size": feedback.get("sample_size", 0), "overall": feedback.get("overall", {})},
        "decisions": {
            "other_target_met": other_share < 0.10,
            "enough_raw_capacity": sku_per_hour >= 6,
            "threshold_retune_ready": feedback.get("sample_size", 0) >= 50,
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("## Promo Coletora — calibração acionável")
    print(f"- Capacidade: {sku_per_hour:.1f} SKUs/h; alvo 1/10min: {'✅' if sku_per_hour >= 6 else '❌'}")
    print(f"- Observações: {len(rows)} · SKUs canônicos: {len(by_sku)} · other: {other_share:.1%}")
    print(f"- Consensos: {len(consensus)} · feedback real: n={feedback.get('sample_size', 0)}")
    print(f"- Retunar thresholds: {'sim' if feedback.get('sample_size', 0) >= 50 else 'não; aguardar 50 outcomes'}")
    print(f"- Relatório completo: {REPORT}")


if __name__ == "__main__":
    main()
