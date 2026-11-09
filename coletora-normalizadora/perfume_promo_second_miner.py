#!/usr/bin/env python3
"""Mine WhatsApp `promo` group for raw -> normalized perfume image pairs.

The target pattern is a later "second" media message for the same product/link, usually
Breno/fromMe or a corrected text-rich repost, after an earlier raw product image/post.
Copies only the second image into the product's candidate bucket; never auto-approves it.
"""
from __future__ import annotations
import json, re, shutil, hashlib
import unicodedata
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageOps, ImageDraw

TAP=Path('/home/ubuntu/.hermes/whatsapp/taps/messages.jsonl')
BANK=Path('/home/ubuntu/.hermes/perfume_image_bank')
PRODUCTS=BANK/'products'
REPORT_DIR=BANK/'promo_second_mining'
DEFAULT_PROMO_CHATS=['120363997955672313@g.us']
PROMO_CHATS=[x.strip() for x in __import__('os').environ.get('PROMO_CHAT_IDS', ','.join(DEFAULT_PROMO_CHATS)).split(',') if x.strip()]

STOP_LINES = re.compile(r'(?i)^(de:?|por:?|cupom|https?://|use o cupom|selecion|vendido|loja|🔥|🏷️|🎟️|✅|❌|$)')
BRANDS = ['Davidoff','Lattafa','Afnan','Armaf','O Boticário','Boticário','Natura','Eudora','Antonio Banderas','Banderas','Khadlaj','Maison Alhambra','Al Wataniah','Ralph Lauren','Armani','Giorgio Armani','Wathiq','Lacoste','Hugo Boss','Calvin Klein','Nautica','Malbec','Rabanne','Paco Rabanne','Lancôme','Lancome','Mercedes','French Avenue','Azzaro','Rayhaan']
PERFUME_TERMS = re.compile(r'(?i)(perfume|edt|edp|eau de|parfum|col[oô]nia|deo col[oô]nia|fragrance|lattafa|afnan|armaf|davidoff|botic[aá]rio|natura|eudora|banderas|khadlaj|maison alhambra|al wataniah|ralph lauren|giorgio armani|acqua di gi[oò]|rabanne|lanc[oô]me|mercedes|french avenue|azzaro)')
URL_RE=re.compile(r'https?://\S+')
PRICE_RE=re.compile(r'R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?')

def norm(s): return re.sub(r'\s+', ' ', (s or '').replace('*','').replace('`','').replace('~','').replace('_','')).strip()
def slugify(s):
    s=norm(s).lower(); s=re.sub(r'[^a-z0-9à-ÿ]+','-',s).strip('-')
    return (re.sub(r'-+','-',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode()).strip('-')[:90] or 'produto-perfume-no-identificado')

def guess_product(body):
    lines=[norm(x) for x in (body or '').splitlines()]
    candidates=[]
    for line in lines:
        if not line or STOP_LINES.search(line) or len(line)<4 or len(line)>130: continue
        score=0
        if re.search(r'(?i)\b\d{2,3}\s*ml\b', line): score+=4
        if re.search(r'(?i)\b(edp|edt|parfum|eau de|perfume|col[oô]nia)\b', line): score+=4
        if any(b.lower() in line.lower() for b in BRANDS): score+=3
        if score: candidates.append((score,line))
    if candidates:
        candidates.sort(key=lambda x:(x[0], len(x[1])), reverse=True); return candidates[0][1]
    if PERFUME_TERMS.search(body or ''):
        for line in lines:
            if line and not STOP_LINES.search(line) and 4 <= len(line) <= 90: return line
    return None

def ts_val(o):
    ts=o.get('timestamp')
    if isinstance(ts,dict): ts=ts.get('low') or 0
    return int(ts or 0)

def img_info(path):
    raw=path.read_bytes(); sha=hashlib.sha256(raw).hexdigest()
    with Image.open(path) as im: return {'width':im.width,'height':im.height,'bytes':path.stat().st_size,'sha256':sha,'format':im.format}

def quality_hint(path):
    with Image.open(path) as im: w,h=im.size
    ratio=min(w,h)/max(w,h)
    if min(w,h)<350: return 'weak_low_resolution'
    if ratio<0.45: return 'weak_bad_crop_ratio'
    if ratio>0.75 and min(w,h)>=500: return 'strong_square_or_crop_ready'
    return 'candidate_crop_needed'

def load_events():
    arr=[]
    for line in TAP.open(errors='replace'):
        try:o=json.loads(line)
        except Exception: continue
        if o.get('chatId') not in PROMO_CHATS: continue
        mp=o.get('mediaPath') or ''; body=o.get('body') or ''; prod=guess_product(body)
        if not prod and not mp: continue
        e={**o,'ts':ts_val(o),'product_guess':prod,'product_key':slugify(prod or 'Produto perfume não identificado'), 'urls':URL_RE.findall(body), 'prices':PRICE_RE.findall(body)}
        arr.append(e)
    arr.sort(key=lambda x:x['ts']); return arr

def pair_events(events, require_media_exists=True):
    media=[]
    for ev in events:
        if not ev.get('mediaPath'): continue
        exists=Path(ev['mediaPath']).exists()
        if require_media_exists and not exists: continue
        ev=dict(ev); ev['media_exists']=exists; media.append(ev)
    pairs=[]
    for i,e in enumerate(media):
        if not e.get('product_guess'): continue
        if not e.get('fromMe') and not (e.get('prices') and e.get('urls')): continue
        candidates=[]; e_urls=set(e.get('urls') or [])
        for prev in media[:i]:
            dt=e['ts']-prev['ts']
            if dt < 0 or dt > 3*3600: continue
            same_key=(prev.get('product_key')==e.get('product_key') and e.get('product_key')!='produto-perfume-no-identificado')
            same_url=bool(set(prev.get('urls') or []).intersection(e_urls))
            blank_raw=(not (prev.get('body') or '').strip() and dt <= 15*60)
            if same_key or same_url or blank_raw:
                score=(10 if same_url else 0)+(8 if same_key else 0)+(5 if blank_raw else 0)+(4 if e.get('fromMe') else 0)+(3 if not prev.get('fromMe') else 0)-dt/3600
                candidates.append((score,prev))
        if candidates:
            candidates.sort(key=lambda x:x[0], reverse=True); prev=candidates[0][1]
            if prev.get('mediaPath')==e.get('mediaPath'): continue
            match='blank_raw_before' if not (prev.get('body') or '').strip() else ('url/key' if set(prev.get('urls') or []).intersection(e_urls) else 'key')
            pairs.append({'first':prev,'second':e,'delta_seconds':e['ts']-prev['ts'],'match':match})
    out=[]; seen=set()
    for p in pairs:
        mid=p['second'].get('messageId')
        if mid in seen: continue
        seen.add(mid); out.append(p)
    return out

def copy_second(pair):
    e=pair['second']; src=Path(e['mediaPath'])
    key=e['product_key']; d=PRODUCTS/key; cand=d/'candidate'; cand.mkdir(parents=True, exist_ok=True)
    ext=src.suffix.lower() or '.jpg'; name=f"promo_second_{datetime.fromtimestamp(e['ts']).strftime('%Y%m%d-%H%M%S')}_{src.stem}{ext}"
    dest=cand/name
    if not dest.exists(): shutil.copy2(src,dest)
    review=d/'second_500'; review.mkdir(exist_ok=True); rdest=review/(dest.stem+'_500.jpg')
    if not rdest.exists():
        with Image.open(dest) as im:
            im=ImageOps.exif_transpose(im).convert('RGB'); im.thumbnail((500,500), Image.Resampling.LANCZOS)
            bg=Image.new('RGB',(500,500),'white'); bg.paste(im,((500-im.width)//2,(500-im.height)//2)); bg.save(rdest, quality=92)
    rec={'ts':datetime.now().isoformat(),'decision':'promo_second_candidate','reason':'second/normalized WhatsApp promo message detected; copied for review, not auto-approved','product_key':key,'product_guess':e.get('product_guess'),'source_chat':e.get('chatId'),'first_message_id':pair['first'].get('messageId'),'second_message_id':e.get('messageId'),'first_time':datetime.fromtimestamp(pair['first']['ts']).isoformat(),'second_time':datetime.fromtimestamp(e['ts']).isoformat(),'delta_seconds':pair['delta_seconds'],'match':pair['match'],'source_media':str(src),'candidate_path':str(dest),'review_500':str(rdest),'quality_hint':quality_hint(dest),'second_from_me':e.get('fromMe'),'prices':e.get('prices'),'urls':e.get('urls')}
    with (d/'decisions.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(rec, ensure_ascii=False)+'\n')
    return rec

def make_sheet(records):
    if not records: return None
    REPORT_DIR.mkdir(parents=True, exist_ok=True); thumbs=[]
    for r in records:
        with Image.open(r['review_500']) as im: thumbs.append((r, im.convert('RGB').resize((220,220))))
    cols=3; rows=(len(thumbs)+cols-1)//cols; sheet=Image.new('RGB',(cols*360, rows*300),'white'); draw=ImageDraw.Draw(sheet)
    for n,(r,im) in enumerate(thumbs):
        x=(n%cols)*360; y=(n//cols)*300; sheet.paste(im,(x,y))
        draw.text((x+5,y+225),f"{n+1}. {r['product_key'][:30]}\n{r['quality_hint']}\n{Path(r['candidate_path']).name[:34]}",fill=(0,0,0))
    out=REPORT_DIR/f"promo_second_sheet_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"; sheet.save(out,quality=90); return str(out)

def main():
    events=load_events()
    metadata_pairs=pair_events(events, require_media_exists=False)
    pairs=pair_events(events, require_media_exists=True)
    records=[]; seen_sha=set(); errors=[]
    for p in pairs:
        try:
            info=img_info(Path(p['second']['mediaPath']))
            if info['sha256'] in seen_sha: continue
            seen_sha.add(info['sha256']); rec=copy_second(p); rec['image']=info; records.append(rec)
        except Exception as ex: errors.append({'error':str(ex),'second':p['second'].get('messageId')})
    missing_second_media=[p for p in metadata_pairs if not Path(p['second'].get('mediaPath') or '').exists()]
    REPORT_DIR.mkdir(parents=True, exist_ok=True); sheet=make_sheet(records)
    report={'ok':True,'promo_chats':PROMO_CHATS,'promo_events':len(events),'metadata_pairs_found':len(metadata_pairs),'pairs_with_media_available':len(pairs),'copied_candidates':len(records),'missing_second_media_count':len(missing_second_media),'sheet':sheet,'records':records,'missing_second_media':[{'product_key':p['second'].get('product_key'),'second_time':datetime.fromtimestamp(p['second']['ts']).isoformat(),'second_media':p['second'].get('mediaPath'),'second_message_id':p['second'].get('messageId'),'first_message_id':p['first'].get('messageId'),'match':p['match']} for p in missing_second_media], 'errors':errors}
    out=REPORT_DIR/f"promo_second_report_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"; out.write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
    print(json.dumps({'ok':True,'report':str(out),'sheet':sheet,'promo_events':len(events),'metadata_pairs_found':len(metadata_pairs),'pairs_with_media_available':len(pairs),'copied_candidates':len(records),'missing_second_media_count':len(missing_second_media),'products':sorted(set(r.get('product_key') for r in records if r.get('product_key')))}, ensure_ascii=False, indent=2))
if __name__=='__main__': main()
