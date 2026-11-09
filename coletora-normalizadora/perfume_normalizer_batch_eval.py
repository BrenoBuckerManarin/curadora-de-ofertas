#!/usr/bin/env python3
"""Offline batch evaluation for the perfume manual normalizer.

Reads real WhatsApp tap messages from perfume source groups, runs normalize_copy +
image resolution without sending anything, and writes auditable artifacts.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

TAP_FILE = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
NORMALIZER = Path(os.path.expanduser("~/.hermes/scripts/perfume_manual_normalizer.py"))
OUT_ROOT = Path("/tmp")

PERFUME_GROUPS = {
    "120363991092469570": "#84 FONTE ALFA | PERFUMES",
    "120363995307638674": "FONTE EPSILON | 346",
    "120363997084299777": "Fonte Zeta | 133",
    "120363999077277988": "FONTE EPSILON | 348",
    "120363998926675391": "FONTE BETA #27",
    "120363994228670851": "Fonte Eta 03",
    "120363997452979005": "Fonte Teta 3",
    "120363992376882261": "FONTE GAMA #45",
    "120363993556613173": "Fonte Delta #52",
}

GENERIC_HEADLINE_RE = re.compile(
    r"(?i)^(pre[cç]inho|pre[cç][aã]o|barato|baixou|baixo|quem tava|pra quem|o estagiario|"
    r"novos cupons|momento|corre|ta muito|t[aá] compensando|n[aã]o pode faltar|esse esgota|"
    r"adocicado na medida|o pre[cç]o despencou|adm pegou|lan[cç]amento|dupe|inspirado)"
)


def load_normalizer(offline_jina: bool = True):
    spec = importlib.util.spec_from_file_location("pmn", NORMALIZER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {NORMALIZER}")
    pmn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pmn)
    os.environ.setdefault("PERFUME_HEADLINE_LLM", "0")
    if offline_jina:
        pmn.jina_fetch = lambda link: ""
    return pmn


def msg_ts(raw: Any) -> int | None:
    if isinstance(raw, dict):
        raw = raw.get("low")
    try:
        ts = int(raw)
    except Exception:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def load_samples(limit: int, since_hours: int, include_duplicates: bool) -> list[dict[str, Any]]:
    cutoff = int(dt.datetime.now().timestamp()) - since_hours * 3600
    rows: list[dict[str, Any]] = []
    if not TAP_FILE.exists():
        raise FileNotFoundError(TAP_FILE)
    for line in TAP_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        chat = str(msg.get("chatId") or "").replace("@g.us", "").replace("@s.whatsapp.net", "")
        if chat not in PERFUME_GROUPS or msg.get("fromMe"):
            continue
        body = (msg.get("body") or "").strip()
        if not body or "http" not in body:
            continue
        ts = msg_ts(msg.get("timestamp"))
        if ts is not None and ts < cutoff:
            continue
        if not re.search(r"(?i)\b(perfume|edp|edt|parfum|col[oô]nia|body splash|ml|lattafa|natura|botic[aá]rio|armani|azzaro|mawwal|french avenue|rayhaan|rasasi|armaf)\b", body):
            continue
        msg["_chat_norm"] = chat
        msg["_group_name"] = PERFUME_GROUPS[chat]
        rows.append(msg)
    # newest first, then optionally keep natural duplicates because they are the point
    rows.sort(key=lambda m: msg_ts(m.get("timestamp")) or 0, reverse=True)
    if include_duplicates:
        return rows[:limit]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for msg in rows:
        body = msg.get("body") or ""
        link = re.search(r"https?://\S+", body)
        key = (link.group(0).rstrip(".,;:!?)>").lower() if link else body[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(msg)
        if len(out) >= limit:
            break
    return out


def raw_product_guess(pmn, body: str) -> str:
    try:
        return pmn.extract_product(body)
    except Exception:
        return ""


def score_row(pmn, msg: dict[str, Any], copy: str, meta: dict[str, Any], image: str, report: str, error: str = "") -> tuple[list[str], dict[str, Any]]:
    flags: list[str] = []
    body = msg.get("body") or ""
    product = str(meta.get("product") or "")
    display_product = str(meta.get("display_product") or product)
    raw_headline = str(meta.get("raw_headline") or "")
    headline = str(meta.get("headline") or "")
    raw_por = meta.get("raw_por")
    final_por = meta.get("final_por")
    if error:
        flags.append("crash")
    if not copy.strip() and not error:
        flags.append("empty_copy")
    if not product or display_product.lower() == "produto":
        flags.append("product_missing")
    if product and raw_headline and product.casefold() == raw_headline.casefold() and GENERIC_HEADLINE_RE.search(raw_headline):
        flags.append("headline_as_product")
    if display_product and GENERIC_HEADLINE_RE.search(display_product):
        flags.append("generic_display_product")
    if raw_por and final_por:
        rv = pmn.money_value(raw_por)
        fv = pmn.money_value(final_por)
        if rv is not None and fv is not None and (fv < rv * 0.55 or fv > rv * 1.85):
            flags.append("price_changed_suspicious")
    if re.search(r"(?i)cupom", body) and meta.get("coupon_count", 0) == 0 and not re.search(r"(?i)cupom\s*:?\s*(r\$|off|pix|cupom\b)", body):
        flags.append("coupon_missed")
    if str(meta.get("headline_source") or "").startswith("fallback"):
        flags.append("headline_fallback")
    if image:
        if "/approved_final/" in image:
            if msg.get("mediaPath") and pmn.looks_like_same_image_base(image, msg.get("mediaPath") or ""):
                flags.append("same_photo_base")
        elif msg.get("mediaPath") and Path(image) == Path(msg.get("mediaPath")):
            flags.append("source_media_fallback")
        else:
            flags.append("non_final_image")
    else:
        flags.append("no_image")
    if "mesma foto-base" in report:
        flags.append("same_photo_base_reported")
    return flags, {
        "messageId": msg.get("messageId"),
        "group": msg.get("_group_name"),
        "timestamp": msg_ts(msg.get("timestamp")),
        "body": body,
        "incoming_media": msg.get("mediaPath") or "",
        "copy": copy,
        "report": report,
        "meta": meta,
        "image": image,
        "flags": flags,
        "error": error,
    }


def write_contact_sheet(rows: list[dict[str, Any]], out: Path, limit: int = 36) -> str:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return ""
    visual = [r for r in rows if r.get("image") and Path(r["image"]).exists()][:limit]
    if not visual:
        return ""
    thumb = 190
    label_h = 82
    cols = 4
    rows_n = (len(visual) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb, rows_n * (thumb + label_h)), (242, 242, 242))
    draw = ImageDraw.Draw(sheet)
    for i, r in enumerate(visual, 1):
        p = Path(r["image"])
        im = Image.open(p).convert("RGB")
        im.thumbnail((thumb, thumb))
        x = ((i - 1) % cols) * thumb
        y = ((i - 1) // cols) * (thumb + label_h)
        sheet.paste(im, (x + (thumb - im.width) // 2, y + (thumb - im.height) // 2))
        product = str((r.get("meta") or {}).get("display_product") or "")[:30]
        flags = ",".join(r.get("flags") or [])[:32]
        draw.text((x + 4, y + thumb + 2), f"{i} {product}\n{flags}\n{p.name[:28]}", fill=(0, 0, 0))
    out_path = out / "image_contact_sheet.jpg"
    sheet.save(out_path, quality=92)
    return str(out_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--since-hours", type=int, default=96)
    ap.add_argument("--online-jina", action="store_true", help="allow real Jina/ML fetches; default is offline for fast deterministic eval")
    ap.add_argument("--dedupe-input", action="store_true", help="dedupe exact links before evaluating; default keeps duplicates")
    args = ap.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / f"perfume_normalizer_eval_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    pmn = load_normalizer(offline_jina=not args.online_jina)
    samples = load_samples(args.limit, args.since_hours, include_duplicates=not args.dedupe_input)

    results: list[dict[str, Any]] = []
    for msg in samples:
        body = msg.get("body") or ""
        try:
            copy, meta = pmn.normalize_copy(body)
            image = pmn.find_image(meta.get("product") or meta.get("display_product") or "", msg.get("mediaPath") or "")
            report = pmn.normalizer_report(meta, image, msg.get("mediaPath") or "")
            flags, row = score_row(pmn, msg, copy, meta, image, report)
        except Exception as exc:
            flags, row = score_row(pmn, msg, "", {}, "", "", error=repr(exc))
        results.append(row)

    counts = collections.Counter(flag for r in results for flag in r.get("flags", []))
    groups = collections.Counter(r.get("group") for r in results)
    products = collections.Counter((r.get("meta") or {}).get("display_product") or "<error>" for r in results)
    contact = write_contact_sheet(results, out)

    (out / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    failures = [r for r in results if r.get("flags")]
    md = []
    md.append(f"# Perfume Normalizer Eval — {stamp}\n")
    md.append(f"- samples: {len(results)} real tap messages")
    md.append(f"- window: last {args.since_hours}h")
    md.append(f"- jina: {'online' if args.online_jina else 'offline/disabled'}")
    md.append(f"- contact_sheet: `{contact}`" if contact else "- contact_sheet: none")
    md.append("\n## Flag counts\n")
    for k, v in counts.most_common():
        md.append(f"- {k}: {v}")
    md.append("\n## Groups\n")
    for k, v in groups.most_common():
        md.append(f"- {k}: {v}")
    md.append("\n## Top products\n")
    for k, v in products.most_common(20):
        md.append(f"- {k}: {v}")
    md.append("\n## Review queue — first 30 flagged\n")
    for i, r in enumerate(failures[:30], 1):
        meta = r.get("meta") or {}
        md.append(f"\n### {i}. {meta.get('display_product') or '<error>'}")
        md.append(f"- group: {r.get('group')}")
        md.append(f"- flags: {', '.join(r.get('flags') or [])}")
        md.append(f"- image: `{r.get('image') or ''}`")
        md.append(f"- error: `{r.get('error') or ''}`")
        body_first = (r.get("body") or "").splitlines()[0:4]
        md.append("- raw head: " + " / ".join(body_first))
        copy_first = (r.get("copy") or "").splitlines()[0:6]
        md.append("- copy head: " + " / ".join(copy_first))
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(out)
    print(f"samples={len(results)} flags={dict(counts)} contact_sheet={contact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
