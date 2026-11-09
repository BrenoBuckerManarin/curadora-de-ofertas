#!/usr/bin/env python3
"""Validate Vitrine da Equipe WhatsApp promo before manual/auto send.

Usage:
  perfume_post_gate.py --image /path/img.jpg --caption /path/caption.txt [--allow-ml-packshot]

Exit 0 only when the post passes minimum editorial + image gates.
"""
from __future__ import annotations
import argparse, re, sys
from pathlib import Path
from PIL import Image

PRICE_RE = re.compile(r"R\$\s*\d+[\d.,]*")
URL_RE = re.compile(r"https?://\S+")


def fail(msgs):
    for m in msgs:
        print(f"FAIL: {m}")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--caption", required=True)
    ap.add_argument("--allow-ml-packshot", action="store_true", help="Allow weak ML packshot/fallback imagery; default blocks likely fallback paths")
    args = ap.parse_args()
    img = Path(args.image).expanduser()
    cap_path = Path(args.caption).expanduser()
    errors=[]
    if not img.exists(): errors.append(f"image missing: {img}")
    if not cap_path.exists(): errors.append(f"caption missing: {cap_path}")
    if errors: return fail(errors)
    text = cap_path.read_text(encoding="utf-8", errors="replace").strip()
    lines = [l.rstrip() for l in text.splitlines()]
    nonempty = [l for l in lines if l.strip()]
    if len(nonempty) < 4:
        errors.append("caption too short; expected headline, product, price/coupon, link")
    if nonempty:
        headline = nonempty[0]
        if not (headline.startswith("*") and headline.endswith("*") and len(headline) >= 4):
            errors.append("headline must be bold: *HEADLINE CURTA*")
        inner = headline.strip("*").strip()
        if len(inner) > 28:
            errors.append(f"headline too long for mobile ({len(inner)} chars > 28)")
        if inner != inner.upper():
            errors.append("headline should be uppercase")
    # Trust line optional, but if present directly under headline and starts with Loja/Vendido, must be italic.
    if len(nonempty) >= 2 and re.search(r"loja|vendido|oficial|verificada", nonempty[1], re.I):
        if not (nonempty[1].startswith("_") and nonempty[1].endswith("_")):
            errors.append("trust signal must be italic: _Loja Oficial *X* no ML_")
    # Product: first bold non-headline non-trust line.
    product_lines = [l for l in nonempty[1:] if not l.startswith("_") and not PRICE_RE.search(l) and not URL_RE.search(l) and "cupom" not in l.lower()]
    if not product_lines or not (product_lines[0].startswith("*") and product_lines[0].endswith("*")):
        errors.append("product title must be bold: *Produto limpo*")
    price_lines = [l for l in nonempty if PRICE_RE.search(l)]
    if not price_lines:
        errors.append("missing price line")
    elif not any(re.search(r"\*R\$\s*\d", l) for l in price_lines):
        errors.append("final price must be bold: *R$ Y*")
    coupon_lines = [l for l in nonempty if "cupom" in l.lower()]
    for l in coupon_lines:
        if "*" not in l:
            errors.append("coupon value must be bold: Cupom: *CODE* 🎟️")
    urls = [l for l in nonempty if URL_RE.search(l)]
    if not urls:
        errors.append("missing final link")
    elif nonempty[-1] != urls[-1]:
        errors.append("link must be alone on the final line")
    # Image hard checks.
    try:
        with Image.open(img) as im:
            if im.size != (500,500):
                errors.append(f"image must be 500x500, got {im.size}")
    except Exception as e:
        errors.append(f"cannot open image: {e}")
    s=str(img).lower()
    if not args.allow_ml_packshot:
        weak_markers=["mlstatic", "og_image", "raw.webp", "rayhaan_imperia_raw", "candidate", "quarantine"]
        if any(m in s for m in weak_markers):
            errors.append("image path looks like fallback/candidate/ML packshot; use approved_final or explicitly pass --allow-ml-packshot")
    if errors: return fail(errors)
    print("PASS: perfume post gate ok")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
