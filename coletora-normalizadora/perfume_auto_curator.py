#!/usr/bin/env python3
"""Vitrine da Equipe Curator pilot.

Every run: read recent source-group perfume promos, rank them, and send the best
1-2 normalized posts to the Normalizador Perfume group using the existing manual
normalizer pipeline. State prevents duplicate links/products.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
TAP = HOME / ".hermes/whatsapp/taps/messages.jsonl"
STATE = HOME / ".hermes/whatsapp/taps/.perfume_auto_curator_state.json"
NORMALIZER_PATH = HOME / ".hermes/scripts/perfume_manual_normalizer.py"

SOURCE_GROUPS = {
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
    spec = importlib.util.spec_from_file_location("perfume_manual_normalizer", NORMALIZER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load normalizer: {NORMALIZER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def msg_ts(raw) -> int:
    if isinstance(raw, dict):
        raw = raw.get("low")
    try:
        ts = int(raw or 0)
    except Exception:
        return 0
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def media_path(msg: dict) -> str:
    if msg.get("mediaPath"):
        return str(msg["mediaPath"])
    urls = msg.get("mediaUrls") or []
    return str(urls[0]) if urls else ""


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"sent_message_ids": [], "sent_links": [], "sent_product_keys": {}, "last_run": 0}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["sent_message_ids"] = list(dict.fromkeys(state.get("sent_message_ids", [])))[-1500:]
    state["sent_links"] = list(dict.fromkeys(state.get("sent_links", [])))[-1500:]
    # prune product cooldowns older than 36h
    now = int(time.time())
    state["sent_product_keys"] = {
        k: v for k, v in (state.get("sent_product_keys") or {}).items()
        if isinstance(v, int) and now - v < 36 * 3600
    }
    state["last_run"] = now
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_link(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(".,;:)>") if m else ""


def price_value(text: str) -> float:
    prices = []
    for m in re.finditer(r"R\$\s*([0-9][0-9.,]*)", text or "", re.I):
        s = m.group(1).replace(".", "").replace(",", ".")
        try:
            prices.append(float(s))
        except Exception:
            pass
    return min(prices) if prices else 999999.0


def score_candidate(n, msg: dict, state: dict) -> dict | None:
    body = (msg.get("body") or "").strip()
    if not body:
        return None
    link = extract_link(body)
    if not link or "meli" not in link and "mercadolivre" not in link:
        return None
    mid = str(msg.get("messageId") or "")
    if mid in set(state.get("sent_message_ids", [])) or link in set(state.get("sent_links", [])):
        return None
    product = n.extract_product(body)
    if not product or product == "Produto":
        return None
    key = n.product_key(product)
    if not key or len(key) < 6:
        return None
    now = int(time.time())
    last_product = (state.get("sent_product_keys") or {}).get(key)
    if isinstance(last_product, int) and now - last_product < 12 * 3600:
        return None
    incoming = media_path(msg)
    image = n.find_image(product, incoming)
    has_incoming = bool(incoming and Path(incoming).exists())
    if not image and not has_incoming:
        return None
    same_base = bool(image and incoming and Path(incoming).exists() and n.looks_like_same_image_base(image, incoming))
    score = 0
    score += 10  # eligible with valid-looking ML link/product/image
    if image:
        score += 8
    if image and not same_base:
        score += 4
    if has_incoming:
        score += 2
    if re.search(r"(?i)cupom|use o cupom|c[oó]digo", body):
        score += 3
    if re.search(r"(?i)loja oficial|vendido por _?loja oficial", body):
        score += 3
    if re.search(r"(?i)no pix|\bpix\b", body):
        score += 1
    p = price_value(body)
    if p <= 120:
        score += 4
    elif p <= 180:
        score += 3
    elif p <= 250:
        score += 2
    ts = msg_ts(msg.get("timestamp"))
    age_min = (time.time() - ts) / 60 if ts else 999
    if age_min <= 30:
        score += 3
    elif age_min <= 75:
        score += 1
    source = SOURCE_GROUPS.get(msg.get("chatId"), msg.get("chatId"))
    if source in {"Fonte Eta 03", "Fonte Teta 3", "FONTE GAMA #45"}:
        score += 1
    return {
        "score": score,
        "message_id": mid,
        "chat": msg.get("chatId"),
        "source": source,
        "ts": ts,
        "body": body,
        "link": link,
        "product": product,
        "key": key,
        "incoming_media": incoming,
        "resolved_image": image,
        "same_base": same_base,
    }


def collect_recent(hours: float) -> list[dict]:
    if not TAP.exists():
        return []
    cutoff = time.time() - hours * 3600
    out = []
    for line in TAP.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        chat = msg.get("chatId") or ""
        if chat not in SOURCE_GROUPS or msg.get("fromMe"):
            continue
        ts = msg_ts(msg.get("timestamp"))
        if ts and ts < cutoff:
            continue
        out.append(msg)
    return out


def send_selected(n, row: dict, dry_run: bool) -> bool:
    group = n.target_group()
    body = row["body"]
    try:
        copy, meta = n.normalize_copy(body)
    except Exception as exc:
        print(f"skip normalize_failed product={row['product']!r} error={exc}")
        return False
    incoming = row.get("incoming_media") or ""
    # A social/card resolver may surface a neighbouring listing. Never publish
    # when the normalized SKU conflicts with the source message's SKU.
    resolved_product = str(meta.get("product") or "")
    if resolved_product and n.product_variant_conflict(str(row.get("product") or ""), resolved_product):
        print(f"skip identity_conflict source={row['product']!r} resolved={resolved_product!r} link={row['link']}")
        return False
    image = n.find_image(resolved_product, incoming)
    if not image:
        print(f"skip no_approved_after_normalize product={meta.get('product')!r} source={row['source']} link={row['link']}")
        return False
    report_image = image
    image = n.prepare_image_for_send(image)
    report = n.normalizer_report(meta, report_image or image, incoming)
    audit = (
        f"\n\n---\n*Auto Curator*\n"
        f"Score: {row['score']} | Fonte: {row['source']}\n"
        f"Produto-chave: `{row['key']}`"
    )
    if dry_run:
        print(f"DRY selected score={row['score']} source={row['source']} product={row['product']}")
        print(copy)
        print("image=", image)
        print(report)
        return True
    ok = n.send_media(group, image, copy, dry_run=False) if image else n.send_text(group, copy, dry_run=False)
    if ok:
        n.send_text(group, (report + audit).strip(), dry_run=False)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    n = load_normalizer()
    state = load_state()
    scored = []
    for msg in collect_recent(args.hours):
        row = score_candidate(n, msg, state)
        if row:
            scored.append(row)
    scored.sort(key=lambda r: (r["score"], r.get("ts") or 0), reverse=True)
    selected = []
    seen_links = set()
    seen_keys = set()
    for row in scored:
        if row["link"] in seen_links or row["key"] in seen_keys:
            continue
        selected.append(row)
        seen_links.add(row["link"])
        seen_keys.add(row["key"])
        if len(selected) >= 20:
            break
    print(f"candidates={len(scored)} selected={len(selected)} dry_run={args.dry_run}")
    sent = 0
    for row in selected:
        if send_selected(n, row, args.dry_run):
            sent += 1
            if not args.dry_run:
                state.setdefault("sent_message_ids", []).append(row["message_id"])
                state.setdefault("sent_links", []).append(row["link"])
                state.setdefault("sent_product_keys", {})[row["key"]] = int(time.time())
                save_state(state)
            time.sleep(1.5)
            if sent >= max(1, min(args.limit, 3)):
                break
    if args.dry_run:
        print("top=" + json.dumps(selected[:5], ensure_ascii=False, indent=2))
    return 0 if sent or not selected else 1


if __name__ == "__main__":
    raise SystemExit(main())
