#!/usr/bin/env python3
"""Generate a non-mutating curation report for perfume approved_final images."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path('/home/ubuntu/.hermes/perfume_image_bank/products')
OUT = Path('/tmp/perfume_image_curation_report_20260704')
SUSPECT_NAME_RE = re.compile(r'(so_promos|sopromos|vitrine|normalizador|approved_corrected|approved_source|line\d+)', re.I)
WATERMARK_SOURCE_RE = re.compile(r'(so_promos|sopromos)', re.I)


def load_index(product_dir: Path) -> dict:
    p = product_dir / 'index.json'
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def classify(path: Path, idx: dict) -> tuple[list[str], str]:
    reasons=[]
    name=str(path)
    if WATERMARK_SOURCE_RE.search(name):
        reasons.append('source-name-suggests-watermark/channel-origin')
    if 'approved_corrected' in path.name:
        reasons.append('approved_corrected-manual-key-move')
    if 'approved_source_' in path.name:
        reasons.append('approved_source-from-forwarded-group')
    if 'vitrine' in name.lower():
        reasons.append('vitrine-source-review')
    # index raw item corrected_from_key suggests product-key correction; useful, but higher mismatch risk.
    raw_items=idx.get('raw_items') or []
    for item in raw_items:
        if item.get('media_path') == str(path) or item.get('sha256','') in path.name:
            cf=item.get('corrected_from_key')
            if cf:
                reasons.append(f'corrected_from_key:{cf[:60]}')
            body=(item.get('body') or '').lower()
            key=idx.get('product_key','').replace('-',' ')
            if key and body and not all(tok in body for tok in key.split()[:2] if len(tok)>3):
                reasons.append('index-body/product-key-weak-match')
    if not reasons and SUSPECT_NAME_RE.search(name):
        reasons.append('source-name-review')
    action='review'
    if any('watermark' in r for r in reasons) or any('corrected_from_key' in r for r in reasons):
        action='likely_quarantine_after_visual_check'
    return reasons, action


def make_sheet(rows: list[dict], out: Path, title: str, limit: int = 60) -> str:
    rows=rows[:limit]
    if not rows:
        return ''
    thumb=180; label_h=85; cols=5
    sheet=Image.new('RGB',(cols*thumb, ((len(rows)+cols-1)//cols)*(thumb+label_h)), (242,242,242))
    d=ImageDraw.Draw(sheet)
    for i,r in enumerate(rows,1):
        p=Path(r['path'])
        try:
            im=Image.open(p).convert('RGB')
        except Exception:
            continue
        im.thumbnail((thumb,thumb))
        x=((i-1)%cols)*thumb; y=((i-1)//cols)*(thumb+label_h)
        sheet.paste(im,(x+(thumb-im.width)//2,y+(thumb-im.height)//2))
        label=f"{i} {r['product_key'][:28]}\n{Path(r['path']).name[:32]}\n{';'.join(r['reasons'])[:45]}"
        d.text((x+3,y+thumb+2),label,fill=(0,0,0))
    path=out/f'{title}.jpg'
    sheet.save(path,quality=92)
    return str(path)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_rows=[]; suspects=[]
    for approved_dir in ROOT.glob('*/approved_final'):
        product_dir=approved_dir.parent
        idx=load_index(product_dir)
        for p in approved_dir.iterdir():
            if not p.is_file() or p.suffix.lower() not in {'.jpg','.jpeg','.png','.webp'}:
                continue
            try:
                im=Image.open(p)
                wh=im.size
            except Exception:
                wh=(0,0)
            reasons, action=classify(p,idx)
            row={'product_key':product_dir.name,'path':str(p),'file':p.name,'size':p.stat().st_size,'width':wh[0],'height':wh[1],'reasons':reasons,'action':action}
            all_rows.append(row)
            if reasons:
                suspects.append(row)
    suspects.sort(key=lambda r:(0 if r['action'].startswith('likely') else 1, r['product_key'], r['file']))
    (OUT/'suspects.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in suspects)+'\n',encoding='utf-8')
    likely=[r for r in suspects if r['action'].startswith('likely')]
    review=[r for r in suspects if not r['action'].startswith('likely')]
    sheet1=make_sheet(likely,OUT,'likely_quarantine_contact_sheet',60)
    sheet2=make_sheet(review,OUT,'review_contact_sheet',60)
    counts=Counter(reason.split(':')[0] for r in suspects for reason in r['reasons'])
    md=[]
    md.append('# Perfume image-bank curation report — non-mutating')
    md.append('')
    md.append(f'- approved_final scanned: {len(all_rows)}')
    md.append(f'- suspects: {len(suspects)}')
    md.append(f'- likely quarantine after visual check: {len(likely)}')
    md.append(f'- review only: {len(review)}')
    md.append(f'- likely sheet: `{sheet1}`')
    md.append(f'- review sheet: `{sheet2}`')
    md.append('')
    md.append('## Reason counts')
    for k,v in counts.most_common(): md.append(f'- {k}: {v}')
    md.append('')
    md.append('## Top likely quarantine candidates')
    for i,r in enumerate(likely[:80],1):
        md.append(f"{i}. `{r['path']}`")
        md.append(f"   - product: `{r['product_key']}`")
        md.append(f"   - reasons: {', '.join(r['reasons'])}")
        md.append(f"   - action: {r['action']}")
    md.append('')
    md.append('## Policy recommendation')
    md.append('- Quarantine immediately only after visual confirmation of watermark/wrong product.')
    md.append('- Do not delete hard; move to `quarantine/<reason>/<product>/...` for rollback.')
    md.append('- Keep same-photo-base product-correct images; normalizer should avoid using them as replacements when incoming media is same base.')
    (OUT/'summary.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    print(OUT)
    print(f'scanned={len(all_rows)} suspects={len(suspects)} likely={len(likely)} review={len(review)}')
    print(sheet1)
    print(sheet2)

if __name__=='__main__': main()
