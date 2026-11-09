#!/usr/bin/env python3
"""Expand migrated canonical folders with strict compatible live siblings."""
from __future__ import annotations
import importlib.util,json,sys
from pathlib import Path
HOME=Path.home(); SCRIPTS=HOME/'.hermes/scripts'; BANK=HOME/'.hermes/perfume_image_bank'; PRODUCTS=BANK/'products'
spec=importlib.util.spec_from_file_location('consolidate',SCRIPTS/'perfume_image_bank_consolidate_safe.py')
assert spec is not None and spec.loader is not None
c=importlib.util.module_from_spec(spec);sys.modules['consolidate']=c;spec.loader.exec_module(c)

def main():
 apply='--apply' in sys.argv
 selected={sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='--canonical'}
 manifests=sorted((BANK/'migration_manifests').glob('*-apply.json'))
 canonicals=set()
 for mf in manifests:
  try:
   d=json.loads(mf.read_text()); canonicals.update(g.get('canonical','') for g in d.get('groups',[]));
   if d.get('canonical'): canonicals.add(d['canonical'])
  except: pass
 canonicals.discard('')
 if selected:
  canonicals &= selected
 # A folder already selected as a canonical destination can never be consumed
 # as another canonical's source. Without this gate equivalent canonicals could
 # form reciprocal plans (A -> B and B -> A) in the same dry-run.
 live=[p for p in PRODUCTS.iterdir() if p.is_dir() and p.name not in canonicals and c.images(p) and not (p/'MIGRATED_TO.json').exists()]
 groups=[]; rejected=[]
 for canonical in sorted(canonicals):
  dest=PRODUCTS/canonical
  if not dest.is_dir(): continue
  src=[]
  for d in live:
   if d.name==canonical: continue
   ok,why=c.safe_pair(canonical,d.name)
   strict=c.pmn.image_bank_key_matches(c.pmn.product_key(canonical.replace('-',' ')),d.name) or c.pmn.image_bank_key_matches(c.pmn.product_key(d.name.replace('-',' ')),canonical)
   if ok and strict: src.append(d.name)
  if src: groups.append({'canonical':canonical,'sources':sorted(src),'folders':[canonical]+sorted(src),'image_count':len(c.images(dest))+sum(len(c.images(PRODUCTS/x)) for x in src)})
 # Never allow one source into two canonicals.
 owners={}
 for g in groups:
  for s in g['sources']: owners.setdefault(s,[]).append(g['canonical'])
 ambiguous={s:v for s,v in owners.items() if len(v)>1}
 for g in groups: g['sources']=[s for s in g['sources'] if s not in ambiguous]; g['folders']=[g['canonical']]+g['sources']
 groups=[g for g in groups if g['sources']]
 stamp=c.datetime.now(c.timezone.utc).isoformat(); summary={'created_at':stamp,'mode':'apply' if apply else 'dry-run','safe_groups':len(groups),'folders_to_merge':sum(len(g['sources']) for g in groups),'groups':groups,'ambiguous':ambiguous}
 if apply:
  aliases=json.loads(c.ALIASES.read_text()); moves=[];dups=[]
  for g in groups:
   m,d=c.apply_group(g,stamp);moves+=m;dups+=d
   aliases[g['canonical']]=[g['canonical']]
   aliases[c.pmn.product_key(g['canonical'].replace('-',' '))]=[g['canonical']]
   for key in g['sources']:
    aliases[key]=[g['canonical']];aliases[c.pmn.product_key(key.replace('-',' '))]=[g['canonical']]
  c.ALIASES.write_text(json.dumps(aliases,ensure_ascii=False,indent=2,sort_keys=True)+'\n');summary.update({'moved':len(moves),'exact_duplicates':len(dups),'moves':moves,'duplicates':dups})
 out=BANK/'migration_manifests'/f'safe-expansion-{c.datetime.now().strftime("%Y%m%d-%H%M%S")}-{summary["mode"]}.json';out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'mode':summary['mode'],'safe_groups':len(groups),'folders_to_merge':summary['folders_to_merge'],'ambiguous':ambiguous,'moved':summary.get('moved',0),'exact_duplicates':summary.get('exact_duplicates',0),'groups':groups,'manifest':str(out)},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
