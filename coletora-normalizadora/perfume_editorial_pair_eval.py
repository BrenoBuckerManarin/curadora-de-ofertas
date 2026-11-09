#!/usr/bin/env python3
"""Collect exact Normalizer -> last Edição editorial cycles and summarize changes.

Only feedback rows containing ``published_copy`` are trusted. Historical rows logged
source text instead of normalized copy and are deliberately excluded.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, re, shutil, time
from pathlib import Path

HOME = Path.home()
DEFAULT_FEEDBACK = HOME / '.hermes/perfume_image_bank/logs/normalizer_feedback.jsonl'
DEFAULT_TAP = HOME / '.hermes/whatsapp/taps/messages.jsonl'
DEFAULT_OUT = HOME / '.hermes/perfume_image_bank/logs/editorial_pair_eval_latest.json'
DEFAULT_CYCLES = HOME / '.hermes/perfume_image_bank/logs/editorial_comparison_cycles.jsonl'
DEFAULT_SUMMARY = HOME / '.hermes/perfume_image_bank/logs/editorial_comparison_summary.json'
DEFAULT_PREFERENCES = HOME / '.hermes/perfume_image_bank/headline_preferences_v6.json'
DEFAULT_EDITORIAL_CONTEXT = HOME / '.hermes/perfume_image_bank/editorial_context.json'
EDITION = '120363991547992305@g.us'
PMN_PATH = HOME / '.hermes/scripts/perfume_manual_normalizer.py'
MAX_EDITION_GAP_SECONDS = 2 * 60 * 60
spec = importlib.util.spec_from_file_location('pmn_editorial_eval', PMN_PATH)
assert spec and spec.loader
pmn = importlib.util.module_from_spec(spec); spec.loader.exec_module(pmn)


def epoch(value) -> int:
    if isinstance(value, dict): value = value.get('low', 0)
    try: return int(value)
    except Exception: return 0


def link(text: str) -> str:
    return pmn.extract_link(text or '') or ''


def clean_lines(text: str) -> list[str]:
    return [re.sub(r'[*_`~]', '', x).strip() for x in (text or '').splitlines() if x.strip()]


def headline(text: str) -> str:
    rows = clean_lines(text)
    if not rows or re.search(r'loja oficial|https?://', rows[0], re.I):
        return ''
    return rows[0]


def title(text: str) -> str:
    rows = clean_lines(text)
    for row in rows[1:]:
        if re.search(r'https?://|\b(?:de|por)\s+R\$|cupom|loja oficial', row, re.I): continue
        return row
    return ''


def title_components(text: str) -> dict:
    norm = pmn._headline_norm(text or '')
    volume = re.search(r'\b(\d{2,4})\s*ML\b', norm)
    concentration = re.search(r'\b(EXTRAIT DE PARFUM|DEO PARFUM|DESODORANTE COLONIA|DEO COLONIA|EDP|EDT|EDC|PARFUM|DESODORANTE|COLONIA)\b', norm)
    variant_vocab = {
        'black','gold','green','blue','elixir','intense','intenso','absolu','bourbon',
        'zanzibar','platin','platinum','sugar','fire','ice','brave','electric','voyage',
    }
    variants = sorted({token for token in pmn.product_key(text or '').split('-') if token in variant_vocab})
    return {'volume': volume.group(1) + 'ml' if volume else '', 'concentration': concentration.group(1) if concentration else '', 'variants': variants}


def classify_title(before: str, after: str) -> dict:
    if pmn._headline_norm(before) == pmn._headline_norm(after): return {'class':'unchanged','severity':0,'changes':[]}
    a, b = title_components(before), title_components(after); changes=[]; severity=1
    if a['volume'] and b['volume'] and a['volume'] != b['volume']: changes.append('wrong_volume'); severity=3
    elif not a['volume'] and b['volume']: changes.append('source_missing_volume_added_by_editor'); severity=max(severity,2)
    elif a['volume'] and not b['volume']: changes.append('editor_removed_volume'); severity=max(severity,2)
    if a['variants'] and b['variants'] and a['variants'] != b['variants']: changes.append('wrong_variant'); severity=3
    elif not a['variants'] and b['variants']: changes.append('source_missing_variant_added_by_editor'); severity=max(severity,2)
    elif a['variants'] and not b['variants']: changes.append('editor_removed_variant'); severity=max(severity,2)
    if a['concentration'] and b['concentration'] and a['concentration'] != b['concentration']: changes.append('wrong_concentration'); severity=3
    elif not a['concentration'] and b['concentration']: changes.append('source_missing_type_added_by_editor'); severity=max(severity,2)
    elif a['concentration'] and not b['concentration']: changes.append('editor_removed_type'); severity=max(severity,2)
    before_tokens=set(pmn.product_key(before).split('-')); after_tokens=set(pmn.product_key(after).split('-'))
    if not changes: changes.append('cleanup_only' if before_tokens == after_tokens else 'canonical_name_change'); severity=max(severity, 1 if changes[0]=='cleanup_only' else 2)
    return {'class':changes[0], 'severity':severity, 'changes':changes, 'before':a, 'after':b}


def headline_level(text: str, product: str, ctx: dict | None=None) -> str:
    h=(text or '').strip(); ctx=dict(ctx or {}); ctx['_product']=product; ctx.setdefault('_recent_molds', {})
    if not h: return 'missing'
    if pmn.headline_looks_like_catalog_title(h, product): return 'catalog_title'
    norm=pmn._headline_norm(h)
    if norm in {'AQUATICO', 'ORIENTAL', 'AMADEIRADO', 'AROMATICO', 'CITRICO', 'FLORAL', 'GOURMAND'}:
        return 'generic'
    generic=bool(re.search(r'\b(PRECINHO|BAIXOU|CORRE|IMPERDIVEL|PRESENCA|DIA A DIA|PRA VOCE|MUITO BOM|PRA APROVEITAR|ACIMA DA MEDIA|ABSURDO)\b', norm)) or pmn.headline_is_pure_descriptive(h)
    hook=pmn.headline_name_hook_score(h, product)
    inspiration=str(ctx.get('known_inspiration') or '')
    if generic: return 'generic'
    if hook >= 32 or (inspiration and any(t in norm for t in pmn._headline_norm(inspiration).split() if len(t)>=4)): return 'specific_memorable'
    return 'specific_weak'


def classify_headline(before: str, after: str, product: str, ctx: dict) -> dict:
    before_level=headline_level(before, product, ctx); after_level=headline_level(after, product, ctx)
    if pmn._headline_norm(before)==pmn._headline_norm(after): return {'class':'unchanged','severity':0,'before_level':before_level,'after_level':after_level,'changes':[]}
    changes=[]
    before_plain=re.sub(r'[^A-Z0-9 ]','',pmn._headline_norm(before)).strip()
    after_plain=re.sub(r'[^A-Z0-9 ]','',pmn._headline_norm(after)).strip()
    if before_plain == after_plain:
        return {'class':'emoji_or_punctuation_only','severity':0,'before_level':before_level,'after_level':after_level,'changes':['emoji_or_punctuation_only']}
    before_price=price_angle(before); after_price=price_angle(after)
    # Same coarse quality bucket can still hide a complete editorial-strategy
    # change. Token overlap keeps actual polishing separate from a new angle.
    bt=set(pmn.product_key(before).split('-')); at=set(pmn.product_key(after).split('-'))
    overlap=len(bt & at) / max(1, len(bt | at))
    if before_price != after_price:
        changes.append('strategy_price_added' if after_price else 'strategy_price_replaced')
    elif overlap < .34:
        changes.append('strategy_changed')
    if pmn.headline_name_hook_score(after, product)>=32 and pmn.headline_name_hook_score(before, product)<32: changes.append('name_hook_added')
    inspiration=str(ctx.get('known_inspiration') or '')
    if inspiration:
        tokens=[t for t in pmn._headline_norm(inspiration).split() if len(t)>=4]
        if any(t in pmn._headline_norm(after) for t in tokens) and not any(t in pmn._headline_norm(before) for t in tokens): changes.append('inspiration_added')
    if not changes and before_level in {'generic','specific_weak','catalog_title'} and after_level=='specific_memorable': changes.append('specific_to_memorable')
    if not changes:
        changes.append('minor_rewrite' if before_level==after_level else f'{before_level}_to_{after_level}')
    return {'class':changes[0],'severity':2 if changes[0] not in {'minor_rewrite'} else 1,'before_level':before_level,'after_level':after_level,'changes':changes,'token_overlap':round(overlap,3)}


def price_angle(text: str) -> bool:
    norm=pmn._headline_norm(text or '')
    return bool(re.search(r'(?:R\$\s*\d|\b(?:POR|MENOS DE|SAI POR)\s+(?:R\$\s*)?\d)', norm))


def price_skeleton(text: str) -> str:
    norm=pmn._headline_norm(text or '')
    norm=re.sub(r'R\$\s*\d+(?:[.,]\d+)?|(?<=\bPOR\s)\d+(?:[.,]\d+)?|(?<=\bDE\s)\d+(?:[.,]\d+)?', '<PRECO>', norm)
    return re.sub(r'[^A-Z<> ]+', '', norm).strip()


def classify_price_angle(before: str, after: str, product: str='') -> dict:
    """Keep factual price edits separate from editorial rejection of price."""
    b_price=price_angle(before); a_price=price_angle(after)
    unit=bool(re.search(r'\b(?:CADA|POR\s+MENOS\s+DE\s+\d+\s+CADA|\d+\s+(?:CADA|POR))\b', pmn._headline_norm(before+' '+after)))
    same_concept=b_price and a_price and price_skeleton(before)==price_skeleton(after)
    before_numbers=re.findall(r'\d+(?:[.,]\d+)?', before or '')
    after_numbers=re.findall(r'\d+(?:[.,]\d+)?', after or '')
    if unit and b_price and a_price:
        cls='unit_economics_approved'
    elif same_concept and before_numbers != after_numbers:
        cls='price_fact_corrected'
    elif same_concept or (b_price and a_price and pmn._headline_norm(before)==pmn._headline_norm(after)):
        cls='price_concept_approved'
    elif b_price and not a_price:
        cls='price_angle_replaced'
    elif not b_price and a_price:
        cls='price_angle_added'
    elif b_price and a_price:
        cls='price_angle_rewritten'
    else:
        cls='no_price_angle'
    return {'class':cls,'before_has_price':b_price,'after_has_price':a_price,'same_concept':same_concept,'unit_economics':unit}


def load_jsonl(path: Path) -> list[dict]:
    out=[]
    if not path.exists(): return out
    for raw in path.read_text(encoding='utf-8',errors='ignore').splitlines():
        try: out.append(json.loads(raw))
        except Exception: pass
    return out


def evidence(row: dict, product: str) -> dict:
    source=str(row.get('source_body') or '')
    inspiration=pmn.known_inspiration_from_text(source)
    facts=pmn.ranked_editorial_facts(product, source, {'known_inspiration':inspiration,'olfactive_terms':[],'safe_angles':[]})
    return {
        'source_body':source,
        'source_title':title(source),
        'known_inspiration':inspiration,
        'ranked_editorial_facts':facts,
        'normalizer_version':row.get('normalizer_version') or 'pre-versioned',
    }


def build(feedback_path: Path, tap_path: Path, hours: int) -> dict:
    cutoff=0 if hours<=0 else int(time.time())-hours*3600
    feedback=[x for x in load_jsonl(feedback_path) if int(x.get('ts') or 0)>=cutoff and x.get('published_copy') and x.get('link') and x.get('content_delivered')]
    feedback.sort(key=lambda x:int(x.get('ts') or 0))
    edition=[]
    for x in load_jsonl(tap_path):
        if x.get('chatId') != EDITION: continue
        body=x.get('body') or ''; u=link(body)
        if u: edition.append({'ts':epoch(x.get('timestamp')),'link':u,'body':body,'messageId':x.get('messageId'),'media_path':x.get('mediaPath') or ''})
    pairs=[]
    for row in feedback:
        start=int(row.get('ts') or 0); u=row['link']
        next_same=min([int(x.get('ts') or 0) for x in feedback if x.get('link')==u and int(x.get('ts') or 0)>start] or [2**63-1])
        finals=[x for x in edition if x['link']==u and start<=x['ts']<min(next_same,start+MAX_EDITION_GAP_SECONDS)]
        if not finals: continue
        final=max(finals,key=lambda x:x['ts']); before=row['published_copy']; after=final['body']
        product=row.get('display_product') or row.get('product') or title(before); ev=evidence(row,product)
        cycle_id=hashlib.sha256(f'{u}\0{start}'.encode()).hexdigest()[:20]
        before_media=str(row.get('image_path') or '')
        after_media=str(final.get('media_path') or '')
        def media_digest(value: str) -> str:
            p=Path(value)
            if not value or not p.is_file(): return ''
            try: return hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception: return ''
        def perceptual_digest(value: str) -> str:
            p=Path(value)
            if not value or not p.is_file(): return ''
            try:
                from PIL import Image
                im=Image.open(p).convert('L').resize((17,16),Image.Resampling.LANCZOS)
                px=[int(im.getpixel((x,y)) or 0) for y in range(16) for x in range(17)]  # type: ignore[arg-type]
                bits=sum(1<<i for i in range(256) if px[(i//16)*17+(i%16)]>px[(i//16)*17+(i%16)+1])
                return f'{bits:064x}'
            except Exception: return ''
        before_hash=media_digest(before_media); after_hash=media_digest(after_media)
        before_phash=perceptual_digest(before_media); after_phash=perceptual_digest(after_media)
        perceptual_distance=(int(before_phash,16)^int(after_phash,16)).bit_count() if before_phash and after_phash else None
        if before_hash and after_hash:
            image_change='unchanged' if before_hash==after_hash else 'changed'
            from perfume_round_guards import visually_similar_review
            if image_change == 'changed' and visually_similar_review(before_media, after_media, perceptual_distance):
                image_change = 'visually_similar_review'
        elif after_hash:
            image_change='added_in_edition'
        elif before_hash:
            image_change='edition_media_unavailable'
        else:
            image_change='unavailable'
        pairs.append({
            'cycle_id':cycle_id,'link':u,'product_key':pmn.product_key(product),'product':product,
            'normalizer_ts':start,'edition_final_ts':final['ts'],'edition_messageId':final['messageId'],
            'normalizer_version':ev['normalizer_version'],
            'before':{'headline':headline(before),'title':title(before),'copy':before,'title_parts':title_components(title(before))},
            'after':{'headline':headline(after),'title':title(after),'copy':after,'title_parts':title_components(title(after))},
            'image_change':{'class':image_change,'before_path':before_media,'before_sha256':before_hash,'before_dhash':before_phash,'after_path':after_media,'after_sha256':after_hash,'after_dhash':after_phash,'perceptual_distance':perceptual_distance,'exact_bytes':bool(before_hash and before_hash==after_hash)},
            'evidence':ev,
            'headline_change':classify_headline(headline(before),headline(after),product,ev),
            'price_angle':classify_price_angle(headline(before),headline(after),product),
            'title_change':classify_title(title(before),title(after)),
        })
    return {'generated_at':int(time.time()),'trusted_feedback_rows':len(feedback),'paired_cycles':len(pairs),'pairs':pairs}


def persist_cycles(new_pairs: list[dict], path: Path) -> list[dict]:
    merged={str(x.get('cycle_id')):x for x in load_jsonl(path) if x.get('cycle_id') and int(x.get('edition_final_ts') or 0)-int(x.get('normalizer_ts') or 0)<MAX_EDITION_GAP_SECONDS}
    for row in new_pairs: merged[row['cycle_id']]=row
    rows=sorted(merged.values(),key=lambda x:int(x.get('normalizer_ts') or 0))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
    return rows


def preference_category(row: dict) -> str:
    product=str(row.get('product') or '')
    folded=pmn.product_key(product).replace('-', ' ')
    ev=dict(row.get('evidence') or {})
    if re.search(r'\b(?:kit|combo|[2-9]\s*(?:unidades?|splashes?|perfumes?))\b', folded): return 'kit'
    if 'feromon' in folded: return 'premise'
    if pmn._arabic_perfume_product(product) and str(ev.get('known_inspiration') or '').strip(): return 'dupe'
    if pmn._arabic_perfume_product(product): return 'arabic_no_dupe'
    return 'national'


def persist_headline_preferences(rows: list[dict], path: Path) -> dict:
    """Merge trusted Edição cycles into the contrastive preference dataset.

    A cycle exists only when Breno posted the card in Edição. No Edição post means
    skipped/unknown and teaches nothing. Changed text creates a negative/positive
    pair; unchanged or cosmetic-only text is positive. Entries are idempotent by
    cycle id + headline + label, while manually collected blind labels are kept.
    """
    try: data=json.loads(path.read_text(encoding='utf-8'))
    except Exception: data={'version':1,'examples':[]}
    examples=list(data.get('examples') or [])
    # Rebuild automatic rows from the canonical cycles; preserve blind/manual data.
    examples=[x for x in examples if not str(x.get('source') or '').startswith('edition_cycle:')]
    auto=[]
    for row in rows:
        if not pmn.editorial_cycle_eligible(row):
            continue
        before=str((row.get('before') or {}).get('headline') or '').strip()
        after=str((row.get('after') or {}).get('headline') or '').strip()
        if not before or not after: continue
        cycle=str(row.get('cycle_id') or '')
        source=f'edition_cycle:{cycle}'
        base={'product':str(row.get('product') or ''),'category':preference_category(row),'source':source,
              'link':str(row.get('link') or ''),'normalizer_ts':int(row.get('normalizer_ts') or 0)}
        same=pmn._headline_norm(before)==pmn._headline_norm(after)
        cosmetic=(row.get('headline_change') or {}).get('class')=='emoji_or_punctuation_only'
        if same or cosmetic:
            auto.append({**base,'headline':after,'label':'approved'})
        else:
            auto.append({**base,'headline':before,'label':'rejected'})
            auto.append({**base,'headline':after,'label':'approved'})
    seen=set(); merged=[]
    for item in examples+auto:
        key=(str(item.get('source') or ''),pmn._headline_norm(str(item.get('headline') or '')),str(item.get('label') or ''))
        if key in seen: continue
        seen.add(key); merged.append(item)
    data['version']=max(2,int(data.get('version') or 1))
    data['updated_at']=int(time.time())
    data['automatic_cycle_examples']=len(auto)
    data['examples']=merged
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)
    return {'examples':len(merged),'automatic_cycle_examples':len(auto)}


def persist_human_editorial_facts(rows: list[dict], path: Path) -> dict:
    """Promote factual atoms from Breno's final headline, never the literal copy.

    Edição is trusted human evidence. Explicit dupe language becomes
    ``known_inspiration``; descriptive vocabulary becomes ``olfactive_terms``.
    Existing conflicting inspiration is preserved for review instead of being
    silently overwritten. This closes the learning loop without teaching the
    model to reuse Breno's headline verbatim.
    """
    try: data=json.loads(path.read_text(encoding='utf-8'))
    except Exception: data={}
    learned_inspirations=0; learned_terms=0; learned_dupe_strength=0; conflicts=[]
    for row in rows:
        if not pmn.editorial_cycle_eligible(row):
            continue
        product=str(row.get('product') or '').strip(); key=str(row.get('product_key') or pmn.product_key(product))
        final=str((row.get('after') or {}).get('headline') or '').strip()
        if not key or not final: continue
        entry=dict(data.get(key) or {})
        changed=False
        inspiration=pmn.known_inspiration_from_text(final)
        if inspiration:
            current=str(entry.get('known_inspiration') or '').strip()
            if not current:
                entry['known_inspiration']=inspiration; learned_inspirations+=1; changed=True
            elif pmn._headline_norm(current) != pmn._headline_norm(inspiration):
                # Same reference with cosmetic difference (case, "de grife",
                # trailing brand) is not a real conflict: keep the longer,
                # more specific existing value instead of flagging noise.
                if pmn._headline_norm(inspiration) in pmn._headline_norm(current) or pmn._headline_norm(current) in pmn._headline_norm(inspiration):
                    if len(pmn._headline_norm(inspiration)) > len(pmn._headline_norm(current)):
                        entry['known_inspiration'] = inspiration
                        changed = True
                else:
                    conflicts.append({'product_key':key,'existing':current,'candidate':inspiration})
        # Learn the editor's intensity choice as a product fact. Plain FIEL AO
        # does not prove a moderate dupe (it may be the very omission Breno is
        # correcting), but DEMAIS/D+ explicitly confirms a strong resemblance.
        if re.search(r'(?i)\bFIEL\s+(?:DEMAIS|D\+)\s+AO\b', final):
            if entry.get('dupe_strength') != 'strong':
                entry['dupe_strength']='strong'; learned_dupe_strength+=1; changed=True
        # Human-confirmed descriptive atoms are facts, not reusable prose.
        # Keep only vocabulary the normalizer already understands and skip the
        # reference perfume when this is an inspiration headline.
        if not inspiration:
            norm_tokens=set(re.findall(r'[a-zà-ÿ]+', pmn._headline_norm(final).lower()))
            terms=[]
            for term in sorted(pmn.DESCRIPTIVE_VOCAB):
                tn=pmn._headline_norm(str(term)).lower().strip()
                if tn and set(tn.split()) <= norm_tokens:
                    terms.append(str(term))
            if terms:
                existing=[str(x) for x in (entry.get('olfactive_terms') or [])]
                seen={pmn._headline_norm(x) for x in existing}
                additions=[x for x in terms if pmn._headline_norm(x) not in seen]
                if additions:
                    entry['olfactive_terms']=existing+additions; learned_terms+=len(additions); changed=True
        if changed:
            entry['human_fact_source']='edition_comparator'
            entry['human_fact_updated_at']=int(time.time())
            data[key]=entry
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=1)+'\n',encoding='utf-8')
    tmp.replace(path)
    return {'learned_inspirations':learned_inspirations,'learned_olfactive_terms':learned_terms,'learned_dupe_strength':learned_dupe_strength,'conflicts':conflicts}


def persist_edition_images(rows: list[dict]) -> dict:
    """Treat Breno's last Edição media for a paired link as the human final asset."""
    copied=[]; duplicates=[]; unavailable=[]
    bank=HOME/'.hermes/perfume_image_bank/products'
    for row in rows:
        key=str(row.get('product_key') or '').strip()
        image=dict(row.get('image_change') or {})
        source=Path(str(image.get('after_path') or ''))
        digest=str(image.get('after_sha256') or '')
        if not key or not digest or not source.is_file():
            unavailable.append(str(row.get('cycle_id') or '')); continue
        target_dir=bank/key/'approved_final'; target_dir.mkdir(parents=True,exist_ok=True)
        existing=[]
        for candidate in target_dir.iterdir():
            if not candidate.is_file(): continue
            try:
                if hashlib.sha256(candidate.read_bytes()).hexdigest()==digest: existing.append(str(candidate))
            except OSError: pass
        if existing:
            duplicates.append({'cycle_id':row.get('cycle_id'),'path':existing[0]}); continue
        suffix=source.suffix.lower() if source.suffix.lower() in {'.jpg','.jpeg','.png','.webp'} else '.jpg'
        target=target_dir/f"breno_edicao_final_{int(row.get('edition_final_ts') or time.time())}_{digest[:12]}{suffix}"
        shutil.copy2(source,target)
        copied.append({'cycle_id':row.get('cycle_id'),'product_key':key,'path':str(target),'sha256':digest})
    return {'copied':copied,'copied_count':len(copied),'duplicates_count':len(duplicates),'unavailable_count':len(unavailable)}


def summarize(rows: list[dict]) -> dict:
    rows = [row for row in rows if pmn.editorial_cycle_eligible(row)]
    def counts(field: str) -> dict:
        out={}
        for row in rows:
            key=str((row.get(field) or {}).get('class') or 'unknown'); out[key]=out.get(key,0)+1
        return out
    unchanged_head=sum((x.get('headline_change') or {}).get('class')=='unchanged' for x in rows)
    unchanged_title=sum((x.get('title_change') or {}).get('class')=='unchanged' for x in rows)
    n=len(rows)

    def exact_metrics(sample: list[dict]) -> dict:
        total=len(sample)
        h=sum(pmn._headline_norm((x.get('before') or {}).get('headline',''))==pmn._headline_norm((x.get('after') or {}).get('headline','')) for x in sample)
        t=sum(pmn._headline_norm((x.get('before') or {}).get('title',''))==pmn._headline_norm((x.get('after') or {}).get('title','')) for x in sample)
        both=sum(
            pmn._headline_norm((x.get('before') or {}).get('headline',''))==pmn._headline_norm((x.get('after') or {}).get('headline',''))
            and pmn._headline_norm((x.get('before') or {}).get('title',''))==pmn._headline_norm((x.get('after') or {}).get('title',''))
            for x in sample
        )
        return {
            'cycles':total,
            'headline_exact_rate':round(h/total,4) if total else None,
            'title_exact_rate':round(t/total,4) if total else None,
            'both_exact_rate':round(both/total,4) if total else None,
        }

    by_version={}
    for version in sorted({str(x.get('normalizer_version') or 'pre-versioned') for x in rows}):
        sample=[x for x in rows if str(x.get('normalizer_version') or 'pre-versioned')==version]
        by_version[version]=exact_metrics(sample[-30:])
    return {
        'generated_at':int(time.time()),'cycles':n,
        'headline_change_counts':counts('headline_change'),'title_change_counts':counts('title_change'),
        'image_change_counts':counts('image_change'),
        'price_angle_counts':counts('price_angle'),
        'headline_unchanged_rate':round(unchanged_head/n,4) if n else None,
        'title_unchanged_rate':round(unchanged_title/n,4) if n else None,
        'recent30_exact':exact_metrics(rows[-30:]),
        'exact_by_version_last30':by_version,
        # Human edits are directional preference/evidence, not a machine verdict
        # that the before-copy was objectively bad. Raise review only from usable
        # recent evidence instead of permanently flagging every mature dataset.
        'human_edits_are_directional':True,
        'comparable_image_cycles':sum((x.get('image_change') or {}).get('class') in {'unchanged','changed','added_in_edition'} for x in rows),
        'integrity_review_due':any((x.get('title_change') or {}).get('severity',0) >= 3 for x in rows[-30:]),
        'calibration_review_due':bool(n >= 30 and exact_metrics(rows[-30:])['headline_exact_rate'] is not None and exact_metrics(rows[-30:])['headline_exact_rate'] < 0.5),
    }


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--feedback',type=Path,default=DEFAULT_FEEDBACK); ap.add_argument('--tap',type=Path,default=DEFAULT_TAP); ap.add_argument('--hours',type=int,default=720); ap.add_argument('--out',type=Path,default=DEFAULT_OUT); ap.add_argument('--cycles',type=Path,default=DEFAULT_CYCLES); ap.add_argument('--summary',type=Path,default=DEFAULT_SUMMARY); ap.add_argument('--preferences',type=Path,default=DEFAULT_PREFERENCES); ap.add_argument('--editorial-context',type=Path,default=DEFAULT_EDITORIAL_CONTEXT)
    args=ap.parse_args(); result=build(args.feedback,args.tap,args.hours)
    rows=persist_cycles(result['pairs'],args.cycles); summary=summarize(rows)
    result['stored_cycles']=len(rows); result['summary']=summary; result['preferences']=persist_headline_preferences(rows,args.preferences); result['learned_facts']=persist_human_editorial_facts(rows,args.editorial_context); result['edition_images']=persist_edition_images(rows)
    args.out.parent.mkdir(parents=True,exist_ok=True); args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); args.summary.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='pairs'},ensure_ascii=False)); return 0

if __name__=='__main__': raise SystemExit(main())
