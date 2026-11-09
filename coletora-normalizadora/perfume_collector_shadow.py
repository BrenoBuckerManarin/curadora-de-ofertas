#!/usr/bin/env python3
"""Perfume Collector shadow mode: real reads/validation, zero delivery/state mutation."""
import argparse
import json
import time
from collections import deque
from pathlib import Path

import perfume_deliver as p


def recent_source_messages(minutes: int, max_lines: int = 8000) -> list[dict]:
    lines = deque(maxlen=max_lines)
    with open(p.TAP_FILE, encoding="utf-8", errors="ignore") as f:
        for line in f:
            lines.append(line)
    now = time.time()
    rows: list[dict] = []
    for line in lines:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        chat_id = str(msg.get("chatId") or "").split("@")[0]
        ts = p._message_epoch_seconds(msg.get("timestamp"))
        if chat_id not in p.PERFUME_GROUPS or ts is None or not (0 <= now - ts <= minutes * 60):
            continue
        body = str(msg.get("body") or "").strip()
        if not body:
            continue
        rows.append({
            "messageId": msg.get("messageId"),
            "chatId": chat_id,
            "groupName": p.PERFUME_GROUPS[chat_id],
            "body": body,
            "hasMedia": bool(msg.get("hasMedia")),
            "mediaPath": p.message_media_path(msg),
            "timestamp": ts,
        })
    return rows


def dedupe_batch(rows: list[dict]) -> list[dict]:
    unique: list[dict] = []
    key_to_index: dict[str, int] = {}
    for row in rows:
        keys = p.normalize_offer_keys(row.get("body") or "")
        duplicate_key = next((key for key in keys if key in key_to_index), "")
        if duplicate_key:
            idx = key_to_index[duplicate_key]
            if row.get("mediaPath") and not unique[idx].get("mediaPath"):
                unique[idx] = row
            continue
        idx = len(unique)
        unique.append(row)
        for key in keys:
            key_to_index[key] = idx
    return unique


def compact(msg: dict) -> dict:
    return {
        "product": msg.get("product"),
        "source": msg.get("groupName"),
        "source_price": msg.get("price"),
        "public_price": msg.get("validated_price"),
        "category": msg.get("offer_category"),
        "opportunity_score": msg.get("opportunity_score"),
        "audience_score": msg.get("audience_score"),
        "price_confidence": msg.get("price_confidence"),
        "discount_pct": msg.get("inferred_discount_pct"),
        "reasons": msg.get("opportunity_reasons"),
        "alternative_offer_count": msg.get("alternative_offer_count", 0),
        "link": p.extract_link(msg.get("body") or ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=90)
    ap.add_argument("--shortlist", type=int, default=p.PREVALIDATE_LIMIT)
    ap.add_argument("--output", default="/home/ubuntu/.hermes/perfume_image_bank/logs/collector_shadow_last.json")
    args = ap.parse_args()

    started = time.perf_counter()
    rows = recent_source_messages(args.minutes)
    unique = dedupe_batch(rows)
    pre_ranked = p.rank_candidates(unique)
    shortlist = p.build_validation_shortlist(pre_ranked, max(1, args.shortlist))
    checked = p.validate_shortlist(shortlist)
    rejected = [m for m in checked if not m.get("validation_ok")]
    valid = [p.validated_opportunity_score(m) for m in checked if m.get("validation_ok")]
    valid = p.apply_source_consensus(valid)
    valid = p.collapse_same_product_offers(valid)
    valid.sort(key=lambda m: (m.get("opportunity_score", 0), m.get("audience_score", 0)), reverse=True)
    selected = p.select_diverse_batch(valid, p.MAX_PROMOS_PER_RUN)

    report = {
        "generated_at": int(time.time()),
        "window_minutes": args.minutes,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "counts": {
            "raw": len(rows),
            "unique": len(unique),
            "shortlist": len(shortlist),
            "valid": len(valid),
            "rejected": len(rejected),
            "selected": len(selected),
        },
        "selected": [compact(m) for m in selected],
        "ranked_valid": [compact(m) for m in valid],
        "rejected": [{
            "product": m.get("product"),
            "source": m.get("groupName"),
            "reason": m.get("validation_reason"),
            "link": p.extract_link(m.get("body") or ""),
        } for m in rejected],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **report["counts"], "elapsed_seconds": report["elapsed_seconds"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
