#!/usr/bin/env python3
"""Report real perfume promos whose normalized image lookup misses but the bank has near folders.

Non-mutating. Produces suggested aliases for review before applying.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

TAP_FILE = Path(os.path.expanduser('~/.hermes/whatsapp/taps/messages.jsonl'))
NORMALIZER = Path(os.path.expanduser('~/.hermes/scripts/perfume_manual_normalizer.py'))
OUT_ROOT = Path('/tmp')
PERFUME_GROUPS = {
    '120363991092469570': '#84 FONTE ALFA | PERFUMES',
    '120363995307638674': 'FONTE EPSILON | 346',
    '120363997084299777': 'Fonte Zeta | 133',
    '120363999077277988': 'FONTE EPSILON | 348',
    '120363998926675391': 'FONTE BETA #27',
    '120363994228670851': 'Fonte Eta 03',
    '120363997452979005': 'Fonte Teta 3',
    '120363992376882261': 'FONTE GAMA #45',
    '120363993556613173': 'Fonte Delta #52',
}
STOP = {'perfume','arabe','edp','edt','eau','de','parfum','deo','colonia','masculino','feminino','original','ml','100ml','90ml','50ml','200ml'}


def load_pmn():
    spec = importlib.util.spec_from_file_location('pmn', NORMALIZER)
    if spec is None or spec.loader is None: raise RuntimeError('cannot load normalizer')
    pmn = importlib.util.module_from_spec(spec); spec.loader.exec_module(pmn)
    os.environ.setdefault('PERFUME_HEADLINE_LLM','0')
    pmn.jina_fetch = lambda link: ''
    return pmn


def msg_ts(raw: Any) -> int | None:
    if isinstance(raw, dict): raw = raw.get('low')
    try: ts = int(raw)
    except Exception: return None
    if ts > 10_000_000_000: ts //= 1000
    return ts


def source_media(msg: dict) -> str:
    if msg.get('mediaPath'): return str(msg.get('mediaPath'))
    urls = msg.get('mediaUrls') or []
    if isinstance(urls, list) and urls: return str(urls[0])
    return str(msg.get('mediaUrl') or '')


def load_samples(limit: int, since_hours: int) -> list[dict]:
    cutoff = int(dt.datetime.now().timestamp()) - since_hours * 3600
    rows=[]
    for line in TAP_FILE.read_text(encoding='utf-8', errors='replace').splitlines():
        try: msg=json.loads(line)
        except Exception: continue
        chat=str(msg.get('chatId') or '').replace('@g.us','').replace('@s.whatsapp.net','')
        if chat not in PERFUME_GROUPS or msg.get('fromMe'): continue
        body=(msg.get('body') or '').strip()
        if not body or 'http' not in body: continue
        ts=msg_ts(msg.get('timestamp'))
        if ts and ts < cutoff: continue
        if not re.search(r'(?i)\b(perfume|edp|edt|parfum|col[oô]nia|body splash|ml|lattafa|natura|botic[aá]rio|armani|azzaro|mawwal|french avenue|rayhaan|rasasi|armaf|rabanne|dior|mancera)\b', body):
            continue
        msg['_group_name']=PERFUME_GROUPS[chat]
        rows.append(msg)
    rows.sort(key=lambda m: msg_ts(m.get('timestamp')) or 0, reverse=True)
    return rows[:limit]


def token_set(key: str) -> set[str]:
    return {t for t in key.split('-') if len(t) > 2 and t not in STOP and not re.fullmatch(r'\d+', t)}


def approved_count(product_dir: Path) -> int:
    d=product_dir/'approved_final'
    if not d.is_dir(): return 0
    return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'})


def best_candidates(pmn, product_key: str, limit: int = 8) -> list[dict]:
    p_tokens=token_set(product_key)
    p_vol={t for t in product_key.split('-') if t.endswith('ml')}
    out=[]
    for d in pmn.IMAGE_BANK_PRODUCTS.iterdir() if pmn.IMAGE_BANK_PRODUCTS.exists() else []:
        if not d.is_dir(): continue
        cnt=approved_count(d)
        if not cnt: continue
        b_tokens=token_set(d.name)
        if not b_tokens: continue
        overlap=p_tokens & b_tokens
        if len(overlap) < 2: continue
        b_vol={t for t in d.name.split('-') if t.endswith('ml')}
        vol_bonus=2 if p_vol and b_vol and not p_vol.isdisjoint(b_vol) else 0
        score=(len(overlap)*10)+vol_bonus-abs(len(p_tokens)-len(b_tokens))
        if score < 15: continue
        out.append({'bank_key':d.name,'approved_count':cnt,'score':score,'overlap':sorted(overlap),'path':str(d)})
    out.sort(key=lambda r:(r['score'], r['approved_count']), reverse=True)
    return out[:limit]


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=160)
    ap.add_argument('--since-hours', type=int, default=168)
    args=ap.parse_args()
    pmn=load_pmn()
    stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    out=OUT_ROOT/f'perfume_image_lookup_misses_{stamp}'
    out.mkdir(parents=True, exist_ok=True)
    rows=[]
    for msg in load_samples(args.limit,args.since_hours):
        body=msg.get('body') or ''
        try:
            product=pmn.extract_product(body)
            product=pmn.canonicalize_known_product_name(product+' '+body) or pmn.canonicalize_known_product_name(product) or product
            key=pmn.product_key(product)
            lookup=pmn.image_lookup_keys(product)
            found=pmn.find_image(product, source_media(msg))
            if found: continue
            cand=best_candidates(pmn,key)
            rows.append({'messageId':msg.get('messageId'),'group':msg.get('_group_name'),'product':product,'product_key':key,'lookup_keys':lookup,'source_media':source_media(msg),'raw_head':' / '.join(body.splitlines()[:5]),'suggestions':cand})
        except Exception as e:
            rows.append({'messageId':msg.get('messageId'),'group':msg.get('_group_name'),'product':'<error>','error':repr(e),'raw_head':' / '.join(body.splitlines()[:5]),'suggestions':[]})
    (out/'misses.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n', encoding='utf-8')
    md=['# Perfume image lookup miss report — non-mutating','',f'- samples scanned: {args.limit}',f'- misses: {len(rows)}','']
    with_suggestions=sum(1 for r in rows if r.get('suggestions'))
    md.append(f'- misses with alias candidates: {with_suggestions}')
    md.append('')
    md.append('## Suggested aliases to review')
    for i,r in enumerate([x for x in rows if x.get('suggestions')][:80],1):
        best=r['suggestions'][0]
        md.append(f"\n### {i}. {r.get('product')}")
        md.append(f"- group: {r.get('group')}")
        md.append(f"- current key: `{r.get('product_key')}`")
        md.append(f"- best candidate: `{best['bank_key']}` score={best['score']} approved={best['approved_count']} overlap={','.join(best['overlap'])}")
        md.append(f"- suggested alias: `{r.get('product_key')}` -> `{best['bank_key']}`")
        md.append(f"- raw: {r.get('raw_head')}")
        if len(r['suggestions']) > 1:
            md.append('- alternatives: ' + ', '.join(f"`{c['bank_key']}`({c['score']})" for c in r['suggestions'][1:4]))
    md.append('')
    md.append('## Misses with no good candidate')
    for r in [x for x in rows if not x.get('suggestions')][:60]:
        md.append(f"- `{r.get('product_key')}` — {r.get('product')} — {r.get('group')}")
    (out/'summary.md').write_text('\n'.join(md)+'\n', encoding='utf-8')
    print(out)
    print(f'misses={len(rows)} with_suggestions={with_suggestions}')
    return 0

if __name__ == '__main__': raise SystemExit(main())
