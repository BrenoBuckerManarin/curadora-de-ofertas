#!/usr/bin/env python3
"""Create a perfume promo review bundle: validation JSON + image contact sheet + caption draft.

This is a human-in-the-loop tool. It must not publish. It marks ML validation
level honestly; if final price/coupon/Pix cannot be proven, caption draft uses
source values only as unverified notes or omits them.
"""
from __future__ import annotations

import argparse, json, re, subprocess, sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home()/'.hermes/perfume_review'
PY = '/home/ubuntu/.hermes/venvs/ml-validator/bin/python'
ML = '/home/ubuntu/.hermes/scripts/ml_offer_validator.py'
IMG = '/home/ubuntu/.hermes/scripts/perfume_image_resolver.py'

PRICE_RE = re.compile(r'R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?')
CUPOM_RE = re.compile(r'(?i)cupom\s*:?\s*\*?([A-Z0-9_-]{3,30}|\d+\s*%\s*OFF\s*AO\s*COMPRAR|R\$\s*\d+\s*AO\s*COMPRAR)')


def slugify(s: str) -> str:
    s=re.sub(r'[^a-zA-Z0-9À-ÿ]+','-',s.lower()).strip('-')
    return re.sub(r'-+','-',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode()).strip('-')[:80] or 'perfume'


def run_json(cmd: list[str]) -> dict:
    p=subprocess.run(cmd, text=True, capture_output=True, timeout=240)
    try:
        data=json.loads(p.stdout)
    except Exception:
        data={'ok':False,'error':'non_json_output','stdout':p.stdout[-2000:],'stderr':p.stderr[-2000:]}
    data['_exit_code']=p.returncode
    if p.stderr.strip(): data['_stderr']=p.stderr[-2000:]
    return data


def source_extract(text: str) -> dict:
    prices=PRICE_RE.findall(text or '')
    cup=None
    m=CUPOM_RE.search(text or '')
    if m: cup=m.group(1).strip('* ')
    return {'source_prices':prices,'source_coupon':cup}


def headline_options(title: str) -> list[str]:
    t=title.lower()
    if 'cool water' in t: return ['CLÁSSICO AQUÁTICO','FRESCOR CLÁSSICO','AQUÁTICO DE RESPEITO']
    if 'nashama' in t or 'caprice' in t: return ['FRESCOR E ELEGÂNCIA','AZUL NOTURNO','NOITE ELEGANTE']
    if 'botic' in t or 'clash' in t: return ['FRESCO MARCANTE','BOTICÁRIO NO PREÇO','FRESCOR NACIONAL']
    if 'banderas' in t: return ['AROMA ELEGANTE','CLÁSSICO ELEGANTE','ELEGÂNCIA ACESSÍVEL']
    if any(x in t for x in ['lattafa','afnan','al wataniah','khadlaj','maison alhambra']): return ['ÁRABE NO PREÇO','PERFUME ÁRABE FORTE','IMPORTADO NO PREÇO']
    return ['FRESCOR MARCANTE','PERFUME NO PREÇO','ACHADO PERFUMADO']


def make_caption(title: str, validation: dict, source: dict, url: str) -> str:
    h=headline_options(title)[0]
    lines=[f'*{h}*']
    trust=validation.get('trust_line')
    if trust: lines.append(trust)
    lines += ['', f'*{title}*', '']
    level=validation.get('validation_level')
    if validation.get('ok') and validation.get('best_price_verified'):
        old=validation.get('old_price_verified')
        best=validation.get('best_price_verified')
        pix=' no Pix' if validation.get('pix_evidence_blocks') else ''
        if old and old != best: lines.append(f'De {old} por *{best}*{pix} 🦸🏻‍♂️')
        else: lines.append(f'Por *{best}*{pix} 🦸🏻‍♂️')
        codes=validation.get('coupon_candidate_codes') or []
        if codes: lines.append(f'Cupom: *{codes[0]}* 🎟️')
    else:
        # Do not claim unverified final price. Draft keeps source only as review note below, not final caption line.
        lines.append('[PREÇO A VALIDAR NO ML]')
    lines += ['', url]
    return '\n'.join(lines)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--source-text', default='')
    ap.add_argument('--title')
    args=ap.parse_args()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base_title=args.title or 'perfume'
    run=ROOT/f'{stamp}-{slugify(base_title)}'
    run.mkdir(parents=True,exist_ok=True)
    (run/'source.txt').write_text(args.source_text or '',encoding='utf-8')
    validation=run_json([PY,ML,args.url])
    title=args.title or validation.get('title') or (validation.get('affiliate_landing_fallback') or {}).get('title') or 'Produto a validar'
    image=run_json([PY,IMG,'--title',title,'--url',args.url])
    src=source_extract(args.source_text or '')
    caption=make_caption(title,validation,src,args.url)
    notes=[]
    if not validation.get('ok'):
        notes.append('VALIDAÇÃO ML FINAL PENDENTE: não usar preço/cupom como final até sessão ML funcionar.')
    if src.get('source_prices'):
        notes.append('Preços da fonte detectados (hipótese): '+', '.join(src['source_prices']))
    if src.get('source_coupon'):
        notes.append('Cupom/texto da fonte detectado (hipótese): '+src['source_coupon'])
    if image.get('contact_sheet'):
        notes.append('Escolha humana obrigatória na contact sheet. No teste Cool Water, candidato 1 era imagem de perfil/hero errada; candidato 2 era produto correto.')
    (run/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'image_report.json').write_text(json.dumps(image,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'caption_draft.txt').write_text(caption,encoding='utf-8')
    (run/'notes.txt').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    report={'ok':True,'run_dir':str(run),'title':title,'validation_level':validation.get('validation_level'),'ml_ok':validation.get('ok'), 'contact_sheet':image.get('contact_sheet'),'caption_draft':str(run/'caption_draft.txt'),'notes':notes,'next_action':'Breno escolhe imagem por número e preço/cupom ficam pendentes até sessão ML real.'}
    (run/'bundle.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
