#!/usr/bin/env python3
"""Join collector observations with delivered normalizer outcomes by exact link."""
import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

BANK = Path.home() / ".hermes/perfume_image_bank"
HISTORY = BANK / "logs/collector_price_history.jsonl"
FEEDBACK = BANK / "logs/normalizer_feedback.jsonl"
DEFAULT_OUTPUT = BANK / "logs/collector_feedback_report.json"


def rows(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = ap.parse_args()
    cutoff = int(time.time()) - max(1, args.days) * 86400

    history_by_link: dict[str, dict] = {}
    for row in rows(HISTORY):
        link = str(row.get("link") or "")
        if link and int(row.get("ts") or 0) >= cutoff:
            history_by_link[link] = row

    joined = []
    for feedback in rows(FEEDBACK):
        if int(feedback.get("ts") or 0) < cutoff:
            continue
        observation = history_by_link.get(str(feedback.get("link") or ""), {})
        joined.append({**feedback, "source": observation.get("source") or "unknown",
                       "category": observation.get("category") or "unknown"})

    def aggregate(field: str) -> dict:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in joined:
            groups[str(row.get(field) or "unknown")].append(row)
        result = {}
        for key, items in groups.items():
            delivered = sum(bool(x.get("content_delivered")) for x in items)
            approved = sum(bool(x.get("approved")) for x in items)
            result[key] = {
                "normalized": len(items), "delivered": delivered, "approved": approved,
                "approval_rate": round(approved / len(items), 3) if items else None,
                "price_checked_rate": round(sum(bool(x.get("price_checked")) for x in items) / len(items), 3) if items else None,
                "image_rate": round(sum(bool(x.get("image_path")) for x in items) / len(items), 3) if items else None,
            }
        return dict(sorted(result.items(), key=lambda x: (-x[1]["normalized"], x[0])))

    report = {
        "generated_at": int(time.time()), "window_days": args.days,
        "sample_size": len(joined),
        "overall": {
            "delivered": sum(bool(x.get("content_delivered")) for x in joined),
            "approved": sum(bool(x.get("approved")) for x in joined),
            "price_unchecked": sum(not bool(x.get("price_checked")) for x in joined),
            "without_image": sum(not bool(x.get("image_path")) for x in joined),
        },
        "by_source": aggregate("source"),
        "by_category": aggregate("category"),
        "failures": [
            {k: row.get(k) for k in ("ts", "product", "link", "source", "category", "price_source")}
            for row in joined if not row.get("approved")
        ][-30:],
        "note": "Rates require delivered Normalizadora outcomes; small samples are not used to retune score automatically.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(output)
    print(json.dumps({"output": str(output), "sample_size": len(joined)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
