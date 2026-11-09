#!/usr/bin/env python3
"""Index WhatsApp group perfume images into a lightweight local image bank.

Reads Hermes WhatsApp tap JSONL and media files already cached under
~/.hermes/image_cache. Does not copy raw images by default; writes metadata
pointers so the bank stays small.
"""
from __future__ import annotations

import argparse, hashlib, json, re, shutil
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

TAP = Path.home()/'.hermes/whatsapp/taps/messages.jsonl'
BANK = Path.home()/'.hermes/perfume_image_bank'
INDEX = BANK/'raw_index.jsonl'
PRODUCTS = BANK/'products'

PERFUME_TERMS = re.compile(r'(?i)(perfume|edt|edp|eau de|parfum|col[oô]nia|deo col[oô]nia|fragrance|lattafa|afnan|armaf|davidoff|botic[aá]rio|natura|eudora|banderas|khadlaj|maison alhambra|al wataniah|ralph lauren|giorgio armani|acqua di gi[oò])')
PRICE_RE = re.compile(r'R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?')
URL_RE = re.compile(r'https?://\S+')
CUPOM_RE = re.compile(r'(?i)cupom\s*:?\s*`?\*?([A-Z0-9_-]{3,30})')

STOP_LINES = re.compile(r'(?i)^(de:?|por:?|cupom|https?://|use o cupom|selecion|vendido|loja|🔥|🏷️|🎟️|✅|❌|$)')
BRANDS = ['Davidoff','Lattafa','Afnan','Armaf','O Boticário','Boticário','Natura','Eudora','Antonio Banderas','Banderas','Khadlaj','Maison Alhambra','Al Wataniah','Ralph Lauren','Armani','Giorgio Armani','Wathiq','Lacoste','Hugo Boss','Calvin Klein','Nautica','Malbec']

# Breno's perfume/promo source priorities. Higher = better source for visual bank.
GROUP_PRIORITY = {
    '120363997955672313@g.us': {'label': 'promo', 'priority': 100},
    # Disabled 2026-06-25: recent images carry @sopromosperfumes/link-do-grupo watermark.
    '120363993116679759@g.us': {'label': 'auto-perfume', 'priority': 80},
    '120363999570877129@g.us': {'label': 'automacoes', 'priority': 20},
}

# Sources whose images carry a competitor watermark. Kept in sync with
# perfume_source_group_miner.DISABLED_WATERMARK_SOURCES -- every writer into the
# bank must honour the same list, otherwise a blocked group simply enters
# through whichever miner forgot about it.
DISABLED_WATERMARK_SOURCES = {
    '120363997452979005@g.us',  # Fonte Teta 3: @sopromosperfumes tarja
    '120363997452979005',
}


def norm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').replace('*','').replace('`','').replace('~','')).strip()


def slugify(s: str) -> str:
    s = norm(s).lower()
    s = re.sub(r'[^a-z0-9à-ÿ]+', '-', s).strip('-')
    s = unicodedata.normalize('NFKD', s).encode('ascii','ignore').decode()
    s = re.sub(r'-+', '-', s).strip('-')
    return s[:90] or 'unknown-product'


_PMN_PATH = Path.home() / '.hermes/scripts/perfume_manual_normalizer.py'
_pmn = None


def resolve_key(title: str) -> str:
    """Canonical bank folder for `title`, falling back to a plain slug.

    Keeps the miner from minting a second folder for a SKU that already owns
    one (2026-08-02 fragmentation audit).
    """
    global _pmn
    try:
        if _pmn is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location('pmn_group_miner', _PMN_PATH)
            assert spec and spec.loader
            _pmn = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_pmn)
        return _pmn.resolve_bank_write_key(title) or slugify(title)
    except Exception:
        return slugify(title)


def guess_product(body: str) -> str | None:
    lines=[norm(x) for x in (body or '').splitlines()]
    candidates=[]
    for line in lines:
        if not line or STOP_LINES.search(line):
            continue
        if len(line) < 4 or len(line) > 120:
            continue
        # Prefer lines with volume/concentration/perfume brand-ish facts.
        score=0
        if re.search(r'(?i)\b\d{2,3}\s*ml\b', line): score += 3
        if re.search(r'(?i)\b(edp|edt|parfum|eau de|perfume|col[oô]nia)\b', line): score += 3
        if any(b.lower() in line.lower() for b in BRANDS): score += 2
        if score:
            candidates.append((score,line))
    if candidates:
        candidates.sort(key=lambda x:(x[0], len(x[1])), reverse=True)
        return candidates[0][1]
    # fallback first meaningful line if perfume terms in body
    if PERFUME_TERMS.search(body or ''):
        for line in lines:
            if line and not STOP_LINES.search(line) and 4 <= len(line) <= 80:
                return line
    return None


def image_info(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        with Image.open(path) as im:
            w,h=im.size
            # simple average hash for near-duplicate detection
            small = im.convert('L').resize((8,8), Image.Resampling.LANCZOS)
            vals = list(small.getdata())
            avg = sum(vals) / len(vals)
            ahash = ''.join('1' if v >= avg else '0' for v in vals)
            return {'width':w,'height':h,'format':im.format,'bytes':path.stat().st_size,'sha256':sha256,'ahash':ahash}
    except Exception:
        return None


def iter_events(tap: Path):
    if not tap.exists(): return
    for line_no,line in enumerate(tap.read_text(errors='ignore').splitlines(),1):
        try: d=json.loads(line)
        except Exception: continue
        mp=d.get('mediaPath') or ''
        if not mp: continue
        p=Path(mp)
        if not p.exists(): continue
        body=d.get('body') or ''
        # Watermarked sources must never enter the bank. perfume_source_group_miner
        # already refuses them, but this miner read every group and let the same
        # photos in through the back door: on 2026-08-02, 139 of 228 folders
        # awaiting curation traced back to "Fonte Teta 3", whose images
        # carry the @sopromosperfumes tarja. One blocklist, honoured by every
        # writer, or the bank keeps refilling with unusable material.
        if str(d.get('chatId') or '') in DISABLED_WATERMARK_SOURCES:
            continue
        group_meta = GROUP_PRIORITY.get(d.get('chatId'), {'label': 'unknown', 'priority': 50})
        # Broaden the bank: priority perfume/promo groups are valuable even when
        # the message text is terse and lacks explicit perfume keywords.
        if not PERFUME_TERMS.search(body) and group_meta['priority'] < 80:
            continue
        info=image_info(p)
        if not info: continue
        title=guess_product(body) or 'Produto perfume não identificado'
        yield {
            'line_no': line_no,
            'chat_id': d.get('chatId'),
            'source_group_label': group_meta['label'],
            'source_priority': group_meta['priority'],
            'sender_id': d.get('senderId'),
            'message_id': d.get('id') or d.get('messageId'),
            'timestamp': d.get('timestamp'),
            'media_path': str(p),
            'body': body,
            'product_guess': title,
            'product_key': resolve_key(title),
            'prices': PRICE_RE.findall(body),
            'coupon': (CUPOM_RE.search(body).group(1) if CUPOM_RE.search(body) else None),
            'urls': URL_RE.findall(body),
            'image': info,
            'indexed_at': datetime.now(timezone.utc).isoformat(),
        }


def write_index(events: list[dict[str,Any]]):
    BANK.mkdir(parents=True, exist_ok=True)
    with INDEX.open('w', encoding='utf-8') as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False)+'\n')
    by_product=defaultdict(list)
    for e in events: by_product[e['product_key']].append(e)
    PRODUCTS.mkdir(parents=True, exist_ok=True)
    for key, items in by_product.items():
        d=PRODUCTS/key
        d.mkdir(parents=True, exist_ok=True)
        # Copy media into the bank so the index never dangles when the
        # transient WhatsApp image_cache rotates (2026-07-08 Maktub Gold loss).
        raw_dir=d/'raw'
        for it in items:
            src=Path(it.get('media_path') or '')
            try:
                if src.exists() and PRODUCTS not in src.parents:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    dst=raw_dir/f"{(it.get('image') or {}).get('sha256','nosha')[:16]}{src.suffix.lower() or '.jpg'}"
                    if not dst.exists():
                        shutil.copy2(src, dst)
                    it['media_path']=str(dst)
            except Exception:
                pass
        meta={
            'product_key': key,
            'canonical_guess': Counter(i['product_guess'] for i in items).most_common(1)[0][0],
            'count': len(items),
            'approved': [],
            'rejected': [],
            'raw_items': items,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        (d/'index.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--tap', default=str(TAP))
    ap.add_argument('--bank', default=str(Path.home()/'.hermes/perfume_image_bank'))
    ap.add_argument('--limit', type=int)
    args=ap.parse_args()
    globals()['BANK']=Path(args.bank); globals()['INDEX']=BANK/'raw_index.jsonl'; globals()['PRODUCTS']=BANK/'products'
    events=list(iter_events(Path(args.tap)))
    if args.limit: events=events[:args.limit]
    write_index(events)
    product_counts=Counter(e['product_key'] for e in events)
    chat_counts=Counter(e['chat_id'] for e in events)
    summary={
        'ok': True,
        'events_indexed': len(events),
        'products': len(product_counts),
        'bank_dir': str(BANK),
        'index': str(INDEX),
        'top_products': product_counts.most_common(20),
        'top_chats': chat_counts.most_common(10),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
