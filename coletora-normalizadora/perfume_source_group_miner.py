#!/usr/bin/env python3
"""Mine curated WhatsApp perfume source groups into the perfume image bank.

Narrow use: read WhatsApp tap JSONL, copy media from configured source groups into
product candidate folders, and optionally seed approved_final when the product
has no approved image yet. Never sends messages.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

TAP = Path.home() / ".hermes/whatsapp/taps/messages.jsonl"
BANK = Path.home() / ".hermes/perfume_image_bank/products"
SCRIPT = Path.home() / ".hermes/scripts/perfume_manual_normalizer.py"
DEFAULT_SOURCE = "120363994228670851@g.us"  # Fonte Eta 03 (current cleaner curated image source)
DEFAULT_LABEL = "Fonte Eta 03"
DISABLED_WATERMARK_SOURCES = {"120363997452979005@g.us"}  # Fonte Teta 3: @sopromosperfumes watermark


def label_slug(label: str) -> str:
    return safe_slug(label).replace("-", "_")[:40] or "source"

spec = importlib.util.spec_from_file_location("pmn", SCRIPT)
pmn = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pmn)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_slug(s: str) -> str:
    return pmn.product_key(s) or "produto"


def load_recent(limit_lines: int) -> list[dict]:
    if not TAP.exists():
        return []
    lines = TAP.read_text(errors="replace").splitlines()
    out = []
    for raw in lines[-limit_lines:]:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def existing_hashes(product_dir: Path) -> set[str]:
    hashes: set[str] = set()
    for sub in ["approved_final", "candidate"]:
        d = product_dir / sub
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_file():
                try:
                    hashes.add(sha256(p))
                except Exception:
                    pass
    return hashes


def clear_product(product: str) -> bool:
    p = (product or "").strip().lower()
    if not p or p in {"produto", "unknown-product", "- perfume 100ml", "perfume 100ml", "100ml", "• colônia", "colônia"}:
        return False
    if re.search(r"(?i)\b(m[áa]scara|reconstru[cç][aã]o|hidrata[cç][aã]o|hidratante|lo[cç][aã]o|shampoo|condicionador|leave[- ]?in|kerastase|k[ée]rastase|cerave|sem\s+perfume)\b", product):
        return False
    return bool(re.search(r"(?i)\b(edp|edt|parfum|eau\s+de\s+(?:toilette|parfum|cologne)|perfume|body splash|body spray|col[oô]nia|malbec|lattafa|mawwal|mercedes|azzaro|natura|botic[aá]rio|armaf|french avenue|carolina herrera|jean paul gaultier|dolce\s*&?\s*gabbana|davidoff|armani|granado)\b", product))


def extract_source_product(body: str) -> str:
    """Extract product from curated captions without treating headline as product."""
    normalized_body = re.sub(r"\s+\|\s+", "\n", body or "")
    lines = [x.strip() for x in normalized_body.splitlines() if x.strip()]
    for idx, raw in enumerate(lines):
        line = raw.strip(" *_`~")
        if re.search(r"(?i)https?://|cupom|^de\b|\bpor\b|loja|oficial|selecion|link para compra", line):
            continue
        # In Fonte Teta 3, the first bold line is normally the product
        # and the second line is often just sales copy/headline. Accept a clean
        # first line unless it is obviously a promo phrase. This prevents keys
        # like "perfume delicioso..." instead of "LATTAFA PISA".
        if idx == 0:
            promoish = re.search(r"(?i)\b(pre[cç]o|garantir|cupom|promo|obra de arte|delicioso|qualidade|evolu[cç][aã]o|dia a dia|perfeito|bizarro|absurdo|brabo|litr[aã]o|s[aã]o\s+\d{2,3}\s*ml|lan[cç]amento|assalariado|sedu[cç][aã]o|mulherada|cheiro de|banho tomado|bom pra|academia|balada|noitada|rainha|poderos|presen[cç]a|fiel|praiano|calor|frio|ver[aã]o|date|dupe|inspirado|lembra|fixa[cç][aã]o|caro|chique|aventureiro|impressionar|lal[aá]|baixou|sereias|sheik|c[ií]trico da)\b", line)
            looks_product = re.search(r"(?i)\b(\d{2,3}\s*ml|edp|edt|parfum|eau\s+de\s+(?:toilette|parfum|cologne)|perfume|body splash|body spray|col[oô]nia|lattafa|rayhaan|natura|malbec|azzaro|ysl|givenchy|gentleman|212|one million|stronger with you|issey|armaf|afnan|ralph lauren|lacoste|granado|mawwal|al wataniah|davidoff|armani)\b", line)
            has_product_class = re.search(r"(?i)\b(\d{2,3}\s*ml|edp|edt|parfum|eau\s+de\s+(?:toilette|parfum|cologne)|body splash|body spray|col[oô]nia)\b", line)
            headline_shape = re.search(r"[?!]|[🔥🦸💇🐯]", line) or (line.upper() == line and len(line.split()) >= 4)
            if promoish or headline_shape:
                continue
            if looks_product and has_product_class:
                return line[:120]
            if not has_product_class:
                continue
        if re.search(r"(?i)\b(\d{2,3}\s*ml|edp|edt|parfum|eau\s+de\s+(?:toilette|parfum|cologne)|perfume|body splash|body spray|col[oô]nia)\b", line):
            return line[:120]
    return pmn.extract_product(body)


def normalize_chat_id(chat: str | None) -> str:
    return (chat or "").replace("@g.us", "").replace("@s.whatsapp.net", "")


def mine(args: argparse.Namespace) -> dict:
    source_group_key = normalize_chat_id(args.source_group)
    disabled_keys = {normalize_chat_id(x) for x in DISABLED_WATERMARK_SOURCES}
    if source_group_key in disabled_keys:
        return {
            "source_group": args.source_group,
            "source_label": args.source_label,
            "disabled": True,
            "reason": "source disabled: recent images have visible watermark/text overlay",
            "scanned_lines": 0,
            "selected_with_media": 0,
            "mined_new_candidates": 0,
            "auto_promoted_approved_final": 0,
            "skipped_duplicates": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mined": [],
            "promoted": [],
        }
    BANK.mkdir(parents=True, exist_ok=True)
    rows = load_recent(args.limit_lines)
    selected = []
    for obj in rows:
        chat = normalize_chat_id(obj.get("chat_id") or obj.get("chatId") or obj.get("remoteJid"))
        if chat != source_group_key:
            continue
        media = obj.get("mediaPath") or obj.get("media_path")
        if not media:
            continue
        media_path = Path(media)
        if not media_path.exists() or media_path.suffix.lower() not in IMAGE_EXT:
            continue
        body = (obj.get("body") or obj.get("text") or obj.get("caption") or "").strip()
        product = extract_source_product(body)
        if not clear_product(product):
            continue
        selected.append((obj, body, product, media_path))

    # Deduplicate by product+hash, newest wins but processing all is okay.
    mined = []
    promoted = []
    skipped_dup = 0
    for obj, body, product, media_path in selected[-args.max_items:]:
        key = safe_slug(product)
        if key in {"produto", "100ml", "kit-body-splash"} or len([x for x in key.split("-") if x]) < 3:
            continue
        # Route into the canonical folder for this SKU instead of slugging the
        # promo title blindly, which is what grew the bank to 226 fragmented
        # SKUs / 329 redundant folders (2026-08-02).
        key = pmn.resolve_bank_write_key(product) or key
        pdir = BANK / key
        cand_dir = pdir / "candidate"
        approved_dir = pdir / "approved_final"
        cand_dir.mkdir(parents=True, exist_ok=True)
        (pdir / "source_examples").mkdir(parents=True, exist_ok=True)
        mh = sha256(media_path)
        if mh in existing_hashes(pdir):
            skipped_dup += 1
            continue
        ts = obj.get("timestamp") or obj.get("ts") or int(datetime.now(timezone.utc).timestamp())
        stamp = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y%m%d-%H%M%S") if str(ts).isdigit() else datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        ext = media_path.suffix.lower()
        source_slug = label_slug(args.source_label)
        base = f"source_{source_slug}_{stamp}_{mh[:12]}{ext}"
        cand_path = cand_dir / base
        shutil.copy2(media_path, cand_path)
        item = {
            "ts": ts,
            "source_group": args.source_group,
            "source_label": args.source_label,
            "product": product,
            "product_key": key,
            "body": body,
            "media_original": str(media_path),
            "candidate": str(cand_path),
            "sha256": mh,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        mined.append(item)
        with (pdir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({**item, "decision": "candidate_from_curated_source"}, ensure_ascii=False) + "\n")
        idx = pdir / "index.json"
        data = {"product_key": key, "canonical_guess": product, "approved": [], "raw_items": [], "updated_at": None}
        if idx.exists():
            try:
                data.update(json.loads(idx.read_text(encoding="utf-8")))
            except Exception:
                pass
        raw_items = data.get("raw_items") or []
        raw_items.append({"media_path": str(cand_path), "body": body, "product_guess": product, "source_group_label": args.source_label, "sha256": mh})
        data["product_key"] = key
        data["canonical_guess"] = data.get("canonical_guess") or product
        data["raw_items"] = raw_items[-20:]
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        idx.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.promote_if_missing or args.promote_all_new:
            approved_dir.mkdir(exist_ok=True)
            has_approved = any(p.is_file() for p in approved_dir.iterdir())
            if args.promote_all_new or not has_approved:
                approved_path = approved_dir / ("approved_" + base)
                shutil.copy2(cand_path, approved_path)
                promoted.append({**item, "approved_final": str(approved_path)})
                with (pdir / "decisions.jsonl").open("a", encoding="utf-8") as f:
                    decision = "auto_promoted_from_curated_source_new_photo" if args.promote_all_new else "auto_promoted_from_curated_source_missing_approved"
                    f.write(json.dumps({**item, "approved_final": str(approved_path), "decision": decision}, ensure_ascii=False) + "\n")

    report = {
        "source_group": args.source_group,
        "source_label": args.source_label,
        "scanned_lines": args.limit_lines,
        "selected_with_media": len(selected),
        "mined_new_candidates": len(mined),
        "auto_promoted_approved_final": len(promoted),
        "skipped_duplicates": skipped_dup,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mined": mined,
        "promoted": promoted,
    }
    out = Path.home() / ".hermes/perfume_image_bank/source_group_mine_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-group", default=DEFAULT_SOURCE)
    ap.add_argument("--source-label", default=DEFAULT_LABEL)
    ap.add_argument("--limit-lines", type=int, default=20000)
    ap.add_argument("--max-items", type=int, default=120)
    ap.add_argument("--promote-if-missing", action="store_true")
    ap.add_argument("--promote-all-new", action="store_true", help="Promote every new non-duplicate image hash to approved_final, even if product already has another approved photo.")
    args = ap.parse_args()
    report = mine(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
