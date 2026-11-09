#!/usr/bin/env python3
"""Audit evidence-engine output without treating legacy output as ground truth."""
from __future__ import annotations
import argparse, json, re
from collections import Counter
from pathlib import Path


def rendered_prices(copy: str):
    line = next((x for x in (copy or '').splitlines() if 'R$' in x and ('Por' in x or 'por' in x)), '')
    nums = [float(x.replace('.', '').replace(',', '.')) for x in re.findall(r'R\$\s*([0-9][0-9.,]*)', line)]
    return (nums[0], nums[1]) if len(nums) > 1 else ((None, nums[0]) if nums else (None, None))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('results_jsonl')
    ap.add_argument('--output', required=True)
    a = ap.parse_args()
    rows = [json.loads(x) for x in Path(a.results_jsonl).open()]
    counts = Counter(); audit=[]
    for row in rows:
        counts['total'] += 1
        meta = row.get('meta') or {}; shadow = meta.get('shadow_offer') or {}
        if not shadow:
            counts['missing_shadow'] += 1; continue
        identity = shadow.get('identity') or {}; decisions = shadow.get('decisions') or {}
        status = identity.get('status') or 'missing'
        counts['identity.' + status] += 1
        if identity.get('item_id'): counts['identity.with_item'] += 1
        for field in ('price','original_price','pix','installments','official_store'):
            d = decisions.get(field) or {}; counts[field + '.' + str(d.get('status') or 'missing')] += 1
        legacy_de, legacy_price = rendered_prices(row.get('copy') or '')
        shadow_price = shadow.get('price'); shadow_de = shadow.get('original_price')
        reasons=[]
        if status in ('ambiguous','product_only','conflict','missing'): reasons.append('identity_review')
        if (decisions.get('price') or {}).get('status') in ('campaign_claim','unverified_claim','corrected_from_exact_link','conflict'):
            reasons.append('price_review')
        if legacy_price is not None and shadow_price is not None and abs(legacy_price-float(shadow_price)) > 1.5: reasons.append('legacy_price_divergence')
        if (legacy_de is None) != (shadow_de is None) or (legacy_de is not None and shadow_de is not None and abs(legacy_de-float(shadow_de)) > 1.5): reasons.append('de_review')
        if bool(meta.get('pix_evidence')) != (shadow.get('price_kind') == 'pix'): reasons.append('pix_review')
        if bool(meta.get('trust')) != bool(shadow.get('official_store')): reasons.append('store_review')
        if reasons:
            audit.append({'product':meta.get('display_product') or identity.get('product_name'),'link':meta.get('link'),'reasons':sorted(set(reasons)),'identity':identity,'legacy':{'price':legacy_price,'original_price':legacy_de,'pix':bool(meta.get('pix_evidence')),'store':meta.get('trust')},'shadow':shadow,'body':row.get('body')})
    output={'metrics':dict(sorted(counts.items())),'review_count':len(audit),'review_queue':audit}
    Path(a.output).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'metrics':output['metrics'],'review_count':len(audit),'output':a.output},ensure_ascii=False))
if __name__=='__main__': main()
