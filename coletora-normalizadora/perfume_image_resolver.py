#!/usr/bin/env python3
"""Perfume image resolver MVP.

Given a product title and optional final ML/meli.la URL, collect image candidates
from affiliate landing/product metadata and lightweight web image search, then
create a numbered 500x500 contact sheet for Breno to choose.

Conservative MVP: does not auto-publish and does not claim image correctness.
"""
from __future__ import annotations

import argparse, hashlib, html, json, re, sys
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageDraw, ImageFont

ROOT = Path.home()/'.hermes/perfume_images'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36'
IMG_RE = re.compile(r'https?://[^"\'<>\s]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?', re.I)


def slugify(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9À-ÿ]+','-',s.lower()).strip('-')
    s = re.sub(r'-+','-',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode()).strip('-') or 'perfume'
    return s[:80]


def fetch(url: str, timeout=25) -> requests.Response | None:
    try:
        return requests.get(url, timeout=timeout, allow_redirects=True, headers={'User-Agent':UA,'Accept-Language':'pt-BR,pt;q=0.9,en;q=0.8'})
    except Exception:
        return None


def collect_from_url(url: str) -> list[dict]:
    out=[]
    r=fetch(url)
    if not r or not r.text: return out
    text=html.unescape(r.text)
    soup=BeautifulSoup(text,'lxml')
    for sel,kind in [('meta[property="og:image"]','og_image'),('meta[name="twitter:image"]','twitter_image'),('meta[name="image"]','meta_image')]:
        for tag in soup.select(sel):
            u=tag.get('content')
            if u: out.append({'url':u,'source':kind,'reason':'metadata image'})
    # common ML hi-res image URLs embedded in pages
    for u in IMG_RE.findall(text):
        if 'mlstatic.com' in u and any(x in u.lower() for x in ['_np_','_nq_','-o.','-f.','-t.','webp','jpg']):
            out.append({'url':u.replace('&amp;','&'),'source':'mlstatic_embedded','reason':'embedded mlstatic image'})
    # product card image src attributes
    for img in soup.find_all('img'):
        for attr in ['src','data-src','data-original']:
            u=img.get(attr)
            if u and u.startswith('http'):
                out.append({'url':u,'source':'img_tag','reason':img.get('alt') or 'image tag'})
    return out


def collect_duckduckgo(title: str, limit=8) -> list[dict]:
    # Lightweight HTML search. This is best-effort and intentionally marked web_candidate.
    queries=[f'{title} perfume', f'{title} official image', f'{title} 100ml']
    out=[]
    for q in queries:
        r=fetch('https://duckduckgo.com/html/?q='+quote_plus(q), timeout=20)
        if not r: continue
        text=html.unescape(r.text)
        # DDG image HTML is weak; still catches proxied/result image URLs sometimes.
        for u in IMG_RE.findall(text):
            if not any(bad in u.lower() for bad in ['sprite','favicon','logo']):
                out.append({'url':u,'source':'web_search_candidate','reason':q})
        if len(out)>=limit: break
    return out[:limit]


def normalize_image_url(u: str) -> str:
    # Prefer larger ML image where URL pattern supports it.
    u = u.replace('D_Q_NP_2X_','D_NQ_NP_2X_')
    return u


def download_image(cand: dict, dest: Path) -> dict | None:
    u=normalize_image_url(cand['url'])
    try:
        r=requests.get(u, timeout=25, headers={'User-Agent':UA,'Referer':'https://www.mercadolivre.com.br/'})
        if r.status_code>=400 or len(r.content)<3000: return None
        im=Image.open(BytesIO(r.content)).convert('RGB')
        w,h=im.size
        if w<180 or h<180: return None
        name=hashlib.sha1(u.encode()).hexdigest()[:10]+'.jpg'
        path=dest/name
        im.save(path, quality=92)
        score=0
        if min(w,h)>=500: score+=25
        if max(w,h)>=900: score+=15
        ratio=min(w,h)/max(w,h)
        if ratio>0.75: score+=15
        if 'mlstatic' in u: score+=20
        if cand.get('source') in {'og_image','twitter_image','meta_image'}: score+=15
        return {**cand,'url':u,'path':str(path),'width':w,'height':h,'score':score}
    except Exception:
        return None


def make_square(src: Path, dst: Path, size=500):
    im=Image.open(src).convert('RGB')
    # Contain, not crop: product fidelity beats aesthetics in MVP.
    bg=Image.new('RGB',(size,size),(246,246,246))
    im.thumbnail((size-30,size-30), Image.Resampling.LANCZOS)
    x=(size-im.width)//2; y=(size-im.height)//2
    bg.paste(im,(x,y))
    bg.save(dst, quality=94)


def contact_sheet(items: list[dict], path: Path, thumb=220, cols=3):
    rows=(len(items)+cols-1)//cols
    label_h=54
    sheet=Image.new('RGB',(cols*thumb, rows*(thumb+label_h)),(235,235,235))
    draw=ImageDraw.Draw(sheet)
    for idx,it in enumerate(items):
        r=idx//cols; c=idx%cols
        x=c*thumb; y=r*(thumb+label_h)
        im=Image.open(it['final_500']).convert('RGB')
        im.thumbnail((thumb,thumb), Image.Resampling.LANCZOS)
        sheet.paste(im,(x+(thumb-im.width)//2,y+(thumb-im.height)//2))
        label=f"{idx+1}. {it['source']} score {it['score']}\n{it['width']}x{it['height']}"
        draw.text((x+6,y+thumb+4),label,fill=(0,0,0))
    sheet.save(path, quality=92)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--title', required=True)
    ap.add_argument('--url')
    ap.add_argument('--limit', type=int, default=8)
    args=ap.parse_args()
    slug=slugify(args.title)
    run=ROOT/slug/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    cand_dir=run/'candidates'; final_dir=run/'final_500'
    cand_dir.mkdir(parents=True, exist_ok=True); final_dir.mkdir(parents=True, exist_ok=True)
    candidates=[]
    if args.url:
        candidates += collect_from_url(args.url)
    candidates += collect_duckduckgo(args.title, limit=8)
    # de-dupe
    seen=set(); uniq=[]
    for c in candidates:
        u=normalize_image_url(c.get('url','')).split('#')[0]
        if not u or u in seen: continue
        seen.add(u); c['url']=u; uniq.append(c)
    downloaded=[]
    for c in uniq[:40]:
        d=download_image(c,cand_dir)
        if d: downloaded.append(d)
    downloaded=sorted(downloaded,key=lambda x:x.get('score',0),reverse=True)[:args.limit]
    for i,it in enumerate(downloaded,1):
        out=final_dir/f'{i:02d}_final_500.jpg'
        make_square(Path(it['path']),out)
        it['final_500']=str(out)
        it['number']=i
    sheet=run/'contact_sheet.jpg'
    if downloaded: contact_sheet(downloaded,sheet)
    report={'ok':bool(downloaded),'title':args.title,'url':args.url,'run_dir':str(run),'contact_sheet':str(sheet) if downloaded else None,'candidates':downloaded,'created_at':datetime.now(timezone.utc).isoformat(),'notes':['MVP: escolha humana obrigatória; não prova fidelidade visual sozinho.','Produto correto > estética; imagens web são candidatas, não verdade.']}
    (run/'image_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if downloaded else 2

if __name__=='__main__':
    raise SystemExit(main())
