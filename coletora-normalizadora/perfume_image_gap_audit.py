#!/usr/bin/env python3
"""Audit recent perfume normalizer/source messages for image-bank gaps.

Outputs JSON + Markdown with cases where product image resolution is missing,
weak, same-base, candidate-only, rejected-only, or OK.
"""
from __future__ import annotations

import importlib.util
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
TAP = HOME / ".hermes/whatsapp/taps/messages.jsonl"
OUT_DIR = HOME / ".hermes/perfume_image_bank/gap_audits"
NORMALIZER = HOME / ".hermes/scripts/perfume_manual_normalizer.py"
PRODUCTS = HOME / ".hermes/perfume_image_bank/products"

# Normalizador + Vitrine da Equipe relevant chats. Keep broad enough for source evidence, not Julio/Shopee noise.
PERFUME_CHATS = {
    "120363995412228683@g.us": "Normalizador Promo",
    "120363991547992305@g.us": "Edição/Correções Breno",
    "120363993116679759@g.us": "Vitrine da Equipe",
    "120363991092469570@g.us": "#84 FONTE ALFA | PERFUMES",
    "120363995307638674@g.us": "FONTE EPSILON | 346",
    "120363997084299777@g.us": "Fonte Zeta | 133",
    "120363999077277988@g.us": "FONTE EPSILON | 348",
    "120363998926675391@g.us": "FONTE BETA #27",
    "120363994228670851@g.us": "Fonte Eta 03",
    "120363997452979005@g.us": "Fonte Teta 3",
    "120363992376882261@g.us": "FONTE GAMA #45",
    "120363993556613173@g.us": "Fonte Delta #52",
}


def load_normalizer():
    spec = importlib.util.spec_from_file_location("perfume_manual_normalizer", NORMALIZER)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def msg_ts(raw) -> int:
    if isinstance(raw, dict):
        raw = raw.get("low")
    try:
        ts = int(raw)
    except Exception:
        return 0
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def media_path(msg: dict) -> str:
    if msg.get("mediaPath"):
        return str(msg["mediaPath"])
    urls = msg.get("mediaUrls") or []
    if urls:
        return str(urls[0])
    return ""


def file_count(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for p in path.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


def bucket_counts(keys: list[str]) -> dict[str, int]:
    counts = Counter()
    seen_dirs = set()
    for key in keys:
        p = PRODUCTS / key
        if not p.is_dir() or p in seen_dirs:
            continue
        seen_dirs.add(p)
        for bucket in ["high_quality", "approved_final", "acceptable", "candidate", "rejected_template_border_low_quality", "rejected_watermark", "rejected"]:
            counts[bucket] += file_count(p / bucket)
    return dict(counts)


def status_for(image: str, counts: dict[str, int], same_base: bool) -> str:
    approved = counts.get("high_quality", 0) + counts.get("approved_final", 0) + counts.get("acceptable", 0)
    candidate = counts.get("candidate", 0)
    rejected = counts.get("rejected_template_border_low_quality", 0) + counts.get("rejected_watermark", 0) + counts.get("rejected", 0)
    if image and same_base and approved <= 1:
        return "same_base_only"
    if image and same_base and approved > 1:
        return "same_base_selected"
    if image:
        return "ok"
    if candidate:
        return "candidate_only"
    if rejected:
        return "rejected_only"
    return "missing"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    n = load_normalizer()
    now = time.time()
    rows = []
    if not TAP.exists():
        raise SystemExit(f"tap missing: {TAP}")
    for line_no, line in enumerate(TAP.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        try:
            msg = json.loads(line)
        except Exception:
            continue
        chat = msg.get("chatId") or ""
        if chat not in PERFUME_CHATS:
            continue
        ts = msg_ts(msg.get("timestamp"))
        if ts and now - ts > args.hours * 3600:
            continue
        body = (msg.get("body") or "").strip()
        if not body or not re.search(r"(?i)(perfume|edp|edt|parfum|col[oô]nia|\d{2,3}\s*ml|lattafa|natura|malbec|banderas|asad)", body):
            continue
        product = n.extract_product(body)
        if not product or product == "Produto":
            continue
        key = n.product_key(product)
        incoming = media_path(msg)
        image = n.find_image(product, incoming)
        same = bool(image and incoming and Path(incoming).exists() and n.looks_like_same_image_base(image, incoming))
        keys = n.image_lookup_keys(product)[:20]
        counts = bucket_counts(keys)
        status = status_for(image, counts, same)
        rows.append({
            "line_no": line_no,
            "ts": ts,
            "chat": chat,
            "chat_label": PERFUME_CHATS.get(chat, chat),
            "message_id": msg.get("messageId"),
            "product": product,
            "key": key,
            "status": status,
            "resolved_image": image,
            "incoming_media": incoming,
            "same_base": same,
            "bucket_counts": counts,
            "lookup_keys": keys[:8],
            "body_head": body.replace("\n", " | ")[:240],
        })

    # Deduplicate by key/status, keep newest evidence.
    best = {}
    for r in rows:
        k = (r["key"], r["status"])
        if k not in best or r.get("ts", 0) > best[k].get("ts", 0):
            best[k] = r
    dedup = sorted(best.values(), key=lambda r: (r["status"] != "missing", r["status"] != "candidate_only", -(r.get("ts") or 0)))[: args.limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = OUT_DIR / f"gap_audit_{stamp}.json"
    md_path = OUT_DIR / f"gap_audit_{stamp}.md"
    json_path.write_text(json.dumps({"generated_at": stamp, "hours": args.hours, "rows": dedup}, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = Counter(r["status"] for r in dedup)
    lines = ["# Perfume image gap audit", "", f"Window: last {args.hours:g}h", "", "## Summary", ""]
    for k, v in counts.most_common():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Actionable gaps", "", "| status | product | key | buckets | source |", "|---|---|---|---|---|"]
    for r in dedup:
        if r["status"] == "ok":
            continue
        b = r["bucket_counts"]
        bucket = ", ".join(f"{k}:{v}" for k, v in b.items() if v) or "none"
        lines.append(f"| {r['status']} | {r['product']} | `{r['key']}` | {bucket} | {r['chat_label']} line {r['line_no']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"json={json_path}")
    print(f"md={md_path}")
    print("summary=" + ", ".join(f"{k}:{v}" for k, v in counts.most_common()))


if __name__ == "__main__":
    main()
