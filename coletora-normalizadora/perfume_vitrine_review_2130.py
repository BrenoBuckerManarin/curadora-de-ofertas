#!/usr/bin/env python3
"""Daily Fonte Eta image-bank triage.

Scans WhatsApp tap for recent Fonte Eta 03 messages with local media,
classifies usable perfume image candidates, writes a review snapshot, and prints
a short approval prompt. Does not promote or send promos.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
TAP = HOME / ".hermes/whatsapp/taps/messages.jsonl"
MINER_PATH = HOME / ".hermes/scripts/perfume_source_group_miner.py"
BANK = HOME / ".hermes/perfume_image_bank/products"
PENDING = HOME / ".hermes/perfume_image_bank/pending_reviews"
SOURCE_ID = "120363994228670851"
SOURCE_LABEL = "Fonte Eta 03"
WINDOW_SECONDS = 36 * 60 * 60
MAX_TAP_STALENESS_SECONDS = 6 * 60 * 60
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

spec = importlib.util.spec_from_file_location("perfume_source_group_miner", MINER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Não consegui carregar minerador: {MINER_PATH}")
miner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(miner)


def tsval(raw) -> int:
    if isinstance(raw, dict):
        raw = raw.get("low")
    try:
        ts = int(raw)
    except Exception:
        return 0
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def existing_hashes(product_key: str) -> set[str]:
    pdir = BANK / product_key
    hashes: set[str] = set()
    for sub in ("approved_final", "candidate"):
        d = pdir / sub
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_file():
                try:
                    hashes.add(sha256(p))
                except Exception:
                    pass
    return hashes


def classify_reject(body: str, product: str) -> str:
    hay = f"{body}\n{product}".casefold()
    if re.search(r"\b(principia|gel de limpeza|protetor solar|fps\s*60|skincare|pele sens[ií]vel)\b", hay):
        return "skincare/Principia"
    if re.search(r"\b(shampoo|condicionador|antiqueda|hidrata[cç][aã]o|m[aá]scara|leave[- ]?in)\b", hay):
        return "cabelo/higiene"
    if not miner.clear_product(product):
        return "produto ambíguo"
    # 2026-07-07: block generic/weak keys at triage time. A key like "75ml" or
    # "100ml" poisons the bank: later any 75ml perfume can borrow its image.
    # Known families (malbec, kaiak, ...) stay allowed via canonical expansion.
    key = miner.safe_slug(product)
    core_tokens = [p for p in key.split("-") if p and not re.fullmatch(r"\d{2,3}ml", p)]
    known_families = set(getattr(miner.pmn, "CANONICAL_FAMILY_EXPANSIONS", {}) or {})
    if len(core_tokens) < 2 and not (core_tokens and core_tokens[0] in known_families):
        return "chave genérica/fraca"
    return ""


def main() -> int:
    now = int(time.time())
    if not TAP.exists():
        print("Perfume Vitrine 21:30 — tap não encontrado.")
        return 0

    seen_products: set[str] = set()
    candidates: list[dict] = []
    duplicates: list[dict] = []
    rejected: list[dict] = []
    missing_media = 0
    scanned = 0
    latest_tap_ts = 0

    for raw in TAP.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        obj_ts = tsval(obj.get("timestamp") or obj.get("ts"))
        latest_tap_ts = max(latest_tap_ts, obj_ts)
        chat = miner.normalize_chat_id(obj.get("chat_id") or obj.get("chatId") or obj.get("remoteJid"))
        if chat != SOURCE_ID:
            continue
        ts = obj_ts
        if ts and ts < now - WINDOW_SECONDS:
            continue
        body = (obj.get("body") or obj.get("text") or obj.get("caption") or "").strip()
        media = obj.get("mediaPath") or obj.get("media_path")
        if not body:
            continue
        scanned += 1
        media_path = Path(media) if media else Path("")
        product = miner.extract_source_product(body)
        # Canonical folder for this SKU, so the review snapshot points at the
        # folder the reader already uses instead of inventing a new spelling.
        product_key = miner.pmn.resolve_bank_write_key(product) or miner.safe_slug(product)
        reject_reason = classify_reject(body, product)
        if reject_reason:
            rejected.append({"product": product, "reason": reject_reason})
            continue
        if not media or not media_path.exists() or media_path.suffix.lower() not in IMAGE_EXT:
            missing_media += 1
            continue
        try:
            digest = sha256(media_path)
        except Exception:
            missing_media += 1
            continue
        item = {
            "timestamp": ts,
            "time_local": datetime.fromtimestamp(ts).astimezone().strftime("%H:%M") if ts else "?",
            "message_id": obj.get("messageId") or obj.get("id"),
            "product": product,
            "product_key": product_key,
            "media_path": str(media_path),
            "sha256": digest,
            "body": body,
        }
        if digest in existing_hashes(product_key):
            duplicates.append(item)
            continue
        if product_key in seen_products:
            duplicates.append(item)
            continue
        seen_products.add(product_key)
        candidates.append(item)

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-2130")
    PENDING.mkdir(parents=True, exist_ok=True)
    json_path = PENDING / f"vitrine-{stamp}.json"
    md_path = PENDING / f"vitrine-{stamp}.md"
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_LABEL,
        "window_hours": WINDOW_SECONDS // 3600,
        "scanned": scanned,
        "new_candidates": candidates,
        "duplicates": duplicates,
        "rejected": rejected,
        "missing_media_after_product_filter": missing_media,
        "tap_latest_timestamp": latest_tap_ts,
        "tap_stale": not latest_tap_ts or latest_tap_ts < now - MAX_TAP_STALENESS_SECONDS,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"Perfume Vitrine 21:30 — triagem banco de imagens",
        "",
        f"Fonte: {SOURCE_LABEL}",
        f"Janela: últimas {WINDOW_SECONDS // 3600}h",
        f"Mensagens analisadas: {scanned}",
        f"Novas imagens aprováveis: {len(candidates)}",
        f"Duplicadas/já no banco: {len(duplicates)}",
        f"Rejeitadas: {len(rejected)}",
        f"Mídia ausente após filtro: {missing_media}",
        "",
    ]
    if report["tap_stale"]:
        age_hours = (now - latest_tap_ts) / 3600 if latest_tap_ts else None
        age = f"{age_hours:.1f}h" if age_hours is not None else "desconhecida"
        lines += [
            "⚠️ CAPTURA INDISPONÍVEL — resultado inválido.",
            f"Último evento no tap: {age} atrás.",
            "Não interpretar zero como ausência de mensagens.",
        ]
    elif candidates:
        lines.append("Candidatos:")
        for idx, item in enumerate(candidates[:20], 1):
            lines.append(f"{idx}. {item['time_local']} — {item['product']}")
        if len(candidates) > 20:
            lines.append(f"... +{len(candidates)-20}")
        lines += ["", "Responder: APROVAR PERFUME VITRINE"]
    else:
        lines.append("Nada novo para aprovar hoje.")
    lines += ["", f"Snapshot: {json_path}"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
