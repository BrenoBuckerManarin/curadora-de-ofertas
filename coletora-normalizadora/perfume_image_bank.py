#!/usr/bin/env python3
"""Search and review the local perfume image bank."""
from __future__ import annotations

import argparse, json, re, shutil
from pathlib import Path
from PIL import Image, ImageDraw

BANK=Path.home()/'.hermes/perfume_image_bank'
INDEX=BANK/'raw_index.jsonl'

def norm(s): return re.sub(r'\s+',' ',(s or '').lower()).strip()
def load():
    if not INDEX.exists(): return []
    return [json.loads(l) for l in INDEX.read_text(errors='ignore').splitlines() if l.strip()]
def score(e,q):
    q=norm(q); text=norm(' '.join([e.get('product_guess',''), e.get('body',''), e.get('product_key','')]))
    if not q: return int(e.get('source_priority') or 0) / 100
    sc=0
    for term in q.split():
        if term in text: sc+=1
    if q in text: sc+=5
    sc += int(e.get('source_priority') or 0) / 100
    return sc

def decision_file(product_key: str) -> Path:
    d=BANK/'products'/product_key
    d.mkdir(parents=True, exist_ok=True)
    return d/'decisions.jsonl'

def append_decision(product_key: str, decision: dict):
    f=decision_file(product_key)
    with f.open('a', encoding='utf-8') as out:
        out.write(json.dumps(decision, ensure_ascii=False)+'\n')

def load_decisions(product_key: str):
    f=decision_file(product_key)
    if not f.exists(): return []
    return [json.loads(l) for l in f.read_text(errors='ignore').splitlines() if l.strip()]

def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return sum(x != y for x,y in zip(a,b))

def quality_score(e: dict) -> float:
    img=e.get('image') or {}
    w=int(img.get('width') or 0); h=int(img.get('height') or 0)
    if not w or not h: return 0
    ratio=min(w,h)/max(w,h)
    px=w*h
    sc=0.0
    sc += min(px/(500*500), 3.0) * 10
    sc += ratio * 20
    sc += int(e.get('source_priority') or 0) / 5
    body=(e.get('body') or '').lower()
    if any(x in body for x in ['cupom','r$','http']): sc += 2
    if w < 350 or h < 350: sc -= 20
    if ratio < 0.55: sc -= 8
    return round(sc,2)
def make_square(src: Path, dst: Path, size=500):
    im=Image.open(src).convert('RGB')
    bg=Image.new('RGB',(size,size),(246,246,246))
    im.thumbnail((size-30,size-30), Image.Resampling.LANCZOS)
    bg.paste(im,((size-im.width)//2,(size-im.height)//2))
    bg.save(dst, quality=92)
def sheet(items, out: Path, cols=4, thumb=190):
    rows=(len(items)+cols-1)//cols; label_h=58
    canvas=Image.new('RGB',(cols*thumb, rows*(thumb+label_h)),(238,238,238)); d=ImageDraw.Draw(canvas)
    for i,e in enumerate(items,1):
        x=((i-1)%cols)*thumb; y=((i-1)//cols)*(thumb+label_h)
        im=Image.open(e['final_500']).convert('RGB'); im.thumbnail((thumb,thumb), Image.Resampling.LANCZOS)
        canvas.paste(im,(x+(thumb-im.width)//2,y+(thumb-im.height)//2))
        txt=f"{i}. {e.get('product_guess','')[:28]}\n{e['image']['width']}x{e['image']['height']} {Path(e['media_path']).suffix}"
        d.text((x+5,y+thumb+3),txt,fill=(0,0,0))
    out.parent.mkdir(parents=True,exist_ok=True); canvas.save(out,quality=90)

def query_terms(q: str):
    return [t for t in norm(q).split() if len(t) > 1]

def matches_query(e: dict, q: str, loose: bool=False) -> bool:
    terms=query_terms(q)
    if not terms: return True
    text=norm(' '.join([e.get('product_guess',''), e.get('body',''), e.get('product_key','')]))
    if loose: return any(t in text for t in terms)
    return all(t in text for t in terms)

def cmd_search(args):
    events=load(); ranked=sorted([(score(e,args.query),e) for e in events if matches_query(e,args.query,args.loose)], key=lambda x:x[0], reverse=True)
    rows=[]
    for sc,e in ranked:
        if sc<=0: continue
        rows.append({'score':sc,'product':e.get('product_guess'),'key':e.get('product_key'),'media_path':e.get('media_path'),'prices':e.get('prices'),'coupon':e.get('coupon'),'chat_id':e.get('chat_id')})
        if len(rows)>=args.limit: break
    print(json.dumps({'ok':True,'query':args.query,'count':len(rows),'results':rows},ensure_ascii=False,indent=2))
def cmd_sheet(args):
    events=load(); ranked=[e for sc,e in sorted([(score(e,args.query),e) for e in events if matches_query(e,args.query,args.loose)], key=lambda x:(x[0], quality_score(x[1])), reverse=True) if sc>0]
    run=BANK/'sheets'/re.sub(r'[^a-zA-Z0-9]+','-',args.query.lower()).strip('-')
    finals=run/'final_500'; finals.mkdir(parents=True,exist_ok=True)
    chosen=[]; seen_paths=set(); seen_sha=set(); seen_ahash=[]; skipped_dupes=0
    for e in ranked:
        p=Path(e['media_path'])
        if not p.exists() or str(p) in seen_paths: continue
        img=e.get('image') or {}; sha=img.get('sha256'); ah=img.get('ahash')
        if args.dedupe:
            if sha and sha in seen_sha:
                skipped_dupes += 1; continue
            if ah and any(hamming(ah, old) <= args.distance for old in seen_ahash):
                skipped_dupes += 1; continue
        seen_paths.add(str(p))
        if sha: seen_sha.add(sha)
        if ah: seen_ahash.append(ah)
        try:
            out=finals/f"{len(chosen)+1:02d}_{p.stem}.jpg"; make_square(p,out)
            chosen.append({**e,'quality_score':quality_score(e),'final_500':str(out)})
        except Exception: pass
        if len(chosen)>=args.limit: break
    out=run/'contact_sheet.jpg'
    if chosen: sheet(chosen,out)
    (run/'sheet_items.json').write_text(json.dumps(chosen,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':bool(chosen),'query':args.query,'count':len(chosen),'skipped_dupes':skipped_dupes,'run_dir':str(run),'contact_sheet':str(out) if chosen else None,'items_json':str(run/'sheet_items.json')},ensure_ascii=False,indent=2))
def cmd_stats(args):
    events=load();
    from collections import Counter
    print(json.dumps({'ok':True,'events':len(events),'products':len(set(e.get('product_key') for e in events)),'top_products':Counter(e.get('product_key') for e in events).most_common(20)},ensure_ascii=False,indent=2))

def cmd_classify(args):
    items_path=Path(args.sheet_dir)/'sheet_items.json'
    if not items_path.exists():
        raise SystemExit(f'sheet_items.json not found: {items_path}')
    items=json.loads(items_path.read_text(encoding='utf-8'))
    idx=args.number-1
    if idx < 0 or idx >= len(items):
        raise SystemExit(f'number out of range 1..{len(items)}')
    item=items[idx]
    status=args.status
    if status not in {'good','bad','unknown'}:
        raise SystemExit('status must be good, bad, or unknown')
    decision={
        'status': status,
        'reason': args.reason,
        'query': args.query,
        'sheet_dir': str(args.sheet_dir),
        'number': args.number,
        'media_path': item.get('media_path'),
        'final_500': item.get('final_500'),
        'product_guess': item.get('product_guess'),
        'product_key': item.get('product_key'),
    }
    append_decision(item.get('product_key') or 'unknown-product', decision)
    # If approved, copy optimized 500x500 into approved/ for reuse.
    approved_path=None
    if status == 'good' and item.get('final_500'):
        d=BANK/'products'/(item.get('product_key') or 'unknown-product')/'approved'
        d.mkdir(parents=True, exist_ok=True)
        approved_path=d/f"approved_{Path(item['final_500']).name}"
        shutil.copy2(item['final_500'], approved_path)
        decision['approved_path']=str(approved_path)
    print(json.dumps({'ok':True,'decision':decision},ensure_ascii=False,indent=2))

def cmd_dedupe(args):
    events=load()
    exact={}
    for e in events:
        sha=(e.get('image') or {}).get('sha256')
        if sha: exact.setdefault(sha,[]).append(e)
    exact_dupes=[v for v in exact.values() if len(v)>1]

    near=[]
    by_product={}
    for e in events:
        by_product.setdefault(e.get('product_key'),[]).append(e)
    for key, items in by_product.items():
        for i in range(len(items)):
            ai=(items[i].get('image') or {}).get('ahash')
            for j in range(i+1,len(items)):
                aj=(items[j].get('image') or {}).get('ahash')
                dist=hamming(ai,aj)
                if dist <= args.distance:
                    near.append({'product_key':key,'distance':dist,'a':items[i].get('media_path'),'b':items[j].get('media_path'),'a_title':items[i].get('product_guess'),'b_title':items[j].get('product_guess')})
    report={'ok':True,'events':len(events),'exact_duplicate_groups':len(exact_dupes),'exact_duplicate_items':sum(len(x) for x in exact_dupes),'near_duplicate_pairs':len(near),'near_distance':args.distance,'exact_examples':[[{'product':e.get('product_guess'),'path':e.get('media_path'),'chat':e.get('chat_id')} for e in group[:5]] for group in exact_dupes[:10]],'near_examples':near[:20]}
    print(json.dumps(report,ensure_ascii=False,indent=2))

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('search'); s.add_argument('query'); s.add_argument('--limit',type=int,default=10); s.add_argument('--loose',action='store_true'); s.set_defaults(fn=cmd_search)
    sh=sub.add_parser('sheet'); sh.add_argument('query'); sh.add_argument('--limit',type=int,default=12); sh.add_argument('--dedupe',action='store_true',default=True); sh.add_argument('--no-dedupe',dest='dedupe',action='store_false'); sh.add_argument('--distance',type=int,default=4); sh.add_argument('--loose',action='store_true'); sh.set_defaults(fn=cmd_sheet)
    st=sub.add_parser('stats'); st.set_defaults(fn=cmd_stats)
    cl=sub.add_parser('classify'); cl.add_argument('sheet_dir'); cl.add_argument('number',type=int); cl.add_argument('status',choices=['good','bad','unknown']); cl.add_argument('--reason',default=''); cl.add_argument('--query',default=''); cl.set_defaults(fn=cmd_classify)
    dd=sub.add_parser('dedupe'); dd.add_argument('--distance',type=int,default=4,help='aHash Hamming distance for near-duplicates within same product'); dd.set_defaults(fn=cmd_dedupe)
    args=ap.parse_args(); args.fn(args)
if __name__=='__main__': main()
