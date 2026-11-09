#!/usr/bin/env python3
"""Consolidate only high-confidence alias-linked perfume image folders.

Dry-run by default. --apply moves images into one canonical folder per safe
component, archives exact duplicates, preserves metadata, writes rollback
manifests, updates aliases, and leaves MIGRATED_TO markers.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, re, shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME=Path.home(); BANK=HOME/'.hermes/perfume_image_bank'; PRODUCTS=BANK/'products'; ALIASES=BANK/'aliases.json'
PMN_PATH=HOME/'.hermes/scripts/perfume_manual_normalizer.py'
spec=importlib.util.spec_from_file_location('pmn_consolidate',PMN_PATH); pmn=importlib.util.module_from_spec(spec); spec.loader.exec_module(pmn)  # type: ignore
EXT={'.jpg','.jpeg','.png','.webp'}
TIERS={'approved_final','candidate','raw','high_quality','acceptable','rejected','rejected_template_border_low_quality','wrong_volume_moved_to_100ml'}
FILLER=set(pmn.CONCENTRATION_TOKENS)|{'o','da','do','das','dos','para','com','kit','combo','conjunto'}
VARIANTS=set(pmn.DISTINCTIVE_VARIANT_TOKENS)
BRAND_WORDS={'o','boticario','boticrio','natura','eudora','jequiti','avon','lattafa','afnan','rayhaan','maison','alhambra','al','wataniah','paris','elysees','hinode','armaf','rasasi','giorgio','armani','emporio','carolina','herrera','ralph','lauren','mawwal','barbour','barbours','jean','paul','gaultier','jpg'}
# NOTE (2026-08-02): gender words are NOT filler here. Dropping them looked
# attractive for "Scandal Intense For Him 150" vs "Scandal Intense 150ml", but
# the same rule made "Armaf Club de Nuit Woman" absorb the generic
# "armaf-club-nuit-105ml" in dry-run. For several houses the gender word IS the
# SKU (Club de Nuit Woman, Scandal feminino vs For Him). Leaving a few folders
# unmerged is cheaper than publishing the wrong bottle.
CATEGORY_WORDS={'perfume','arabe','rabe','masculino','feminino','original','loja','oficial','deo','desodorante','colonia','eau','de','toilette','edp','edt'}

def images(d:Path): return [p for p in d.rglob('*') if p.is_file() and p.suffix.lower() in EXT and 'duplicates_archive' not in p.parts]
def sha(p:Path):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def normalize_folder_key(name:str)->str:
 # Folder names drift between "-100", "-100-ml" and "-100ml" for the same SKU.
 out=re.sub(r'-(\d{2,3})(?:-ml|ml)(?=-|$)',r'-\1ml',name)
 # A bare 2-3 digit token is only read as a volume at the very END of the name
 # (…-scandal-intense-for-him-150). Anywhere else it may be a kit item count,
 # which is identity and must not be rewritten.
 out=re.sub(r'-(\d{2,3})$',r'-\1ml',out)
 return out
def tokens(k): return {x for x in pmn.product_key(normalize_folder_key(k).replace('-',' ')).split('-') if x}
def brand_equalize(a:set,b:set):
 """Replace brand-house synonyms in `a` by the sibling token present in `b`.

 Giorgio/Emporio/Armani label the same house; folders alternate them for the
 same SKU. Only tokens registered in the normalizer's brand map are touched.
 """
 out=set()
 for t in a:
  if t in b: out.add(t); continue
  sib=pmn.BRAND_HOUSE_EQUIVALENTS.get(t,set())&b
  out.add(next(iter(sorted(sib))) if sib else t)
 return out
def vols(t): return {x for x in t if re.fullmatch(r'\d{2,3}ml',x)}
def counts(t): return {x for x in t if re.fullmatch(r'\d{1,2}',x)}
def safe_pair(a,b):
 raw_a=set(x for x in normalize_folder_key(a).lower().split('-') if x); raw_b=set(x for x in normalize_folder_key(b).lower().split('-') if x)
 if ('parfum' in raw_a) != ('parfum' in raw_b): return False,'parfum_concentration_mismatch'
 ta,tb=tokens(a),tokens(b); va,vb=vols(ta),vols(tb)
 if not va or va!=vb: return False,'volume'
 if (ta&VARIANTS)!=(tb&VARIANTS): return False,'variant'
 ka,kb=bool(ta&{'kit','combo','conjunto'}),bool(tb&{'kit','combo','conjunto'})
 if ka!=kb: return False,'kit'
 if ka and counts(ta) and counts(tb) and counts(ta)!=counts(tb): return False,'kit_count'
 # Brand-house synonyms (Giorgio/Emporio/Armani) name the same house. Equalize
 # them BEFORE the identity comparison so folder drift on the brand word does
 # not read as a different product. Variant/volume/kit gates already ran.
 ta=brand_equalize(ta,tb)
 # After removing only brand/category/volume noise, the actual product identity
 # must be identical. This blocks generic Natura Homem -> Homem Absoluto and
 # Stronger With You Parfum -> plain EDT while still allowing brand order/SEO.
 ia={x for x in ta if x not in BRAND_WORDS|CATEGORY_WORDS and not x.endswith('ml')}
 ib={x for x in tb if x not in BRAND_WORDS|CATEGORY_WORDS and not x.endswith('ml')}
 if ia != ib: return False,'identity_core_mismatch'
 if ('parfum' in ta) != ('parfum' in tb): return False,'parfum_concentration_mismatch'
 ca={x for x in ta if x not in FILLER and not x.endswith('ml')}; cb={x for x in tb if x not in FILLER and not x.endswith('ml')}
 overlap=ca&cb
 if len(overlap)<2: return False,'weak_overlap'
 coverage=len(overlap)/max(1,min(len(ca),len(cb)))
 jacc=len(overlap)/max(1,len(ca|cb))
 if coverage<0.80 or jacc<0.55: return False,'identity_drift'
 return True,'safe'

def build_plan():
 payload=json.loads(ALIASES.read_text())
 graph=defaultdict(set); preferred=Counter(); rejected=[]
 for src,vals in payload.items():
  vals=[vals] if isinstance(vals,str) else list(vals or [])
  for alias_rank,dst in enumerate(vals):
   if src==dst: continue
   sd,dd=PRODUCTS/src,PRODUCTS/dst
   if not sd.is_dir() or not dd.is_dir() or not images(sd) or not images(dd): continue
   ok,why=safe_pair(src,dst)
   if not ok: rejected.append({'source':src,'target':dst,'reason':why}); continue
   graph[src].add(dst); graph[dst].add(src); preferred[dst]+=max(1,10-alias_rank)
 seen=set(); groups=[]
 for node in sorted(graph):
  if node in seen: continue
  stack=[node]; comp=[]; seen.add(node)
  while stack:
   x=stack.pop(); comp.append(x)
   for y in graph[x]:
    if y not in seen: seen.add(y); stack.append(y)
  live=[x for x in comp if images(PRODUCTS/x)]
  if len(live)<2: continue
  def rank(x):
   d=PRODUCTS/x; approved=len([p for p in images(d) if 'approved_final' in p.parts]); total=len(images(d))
   explicit=(1 if (d/'index.json').exists() else 0)
   return preferred[x],approved,total,explicit,-len(x),x
  canonical=max(live,key=rank)
  groups.append({'canonical':canonical,'sources':sorted(x for x in live if x!=canonical),'folders':sorted(live),'image_count':sum(len(images(PRODUCTS/x)) for x in live)})
 return groups,rejected

def apply_group(g,stamp):
 canonical=g['canonical']; dest=PRODUCTS/canonical; moved=[]; dups=[]
 tier_priority={'high_quality':6,'approved_final':5,'acceptable':4,'candidate':3,'raw':2,'rejected':0,'rejected_template_border_low_quality':0}
 known={}
 for p in images(dest):
  try:
   tier=next((part for part in p.relative_to(dest).parts if part in TIERS), 'raw')
   h=sha(p)
   current=known.get(h)
   if current is None or tier_priority.get(tier,1)>current[0]: known[h]=(tier_priority.get(tier,1),p)
  except: pass
 for source in g['sources']:
  src=PRODUCTS/source
  for p in sorted(images(src)):
   rel=p.relative_to(src); tier=rel.parts[0] if rel.parts and rel.parts[0] in TIERS else 'raw'; h=sha(p)
   existing=known.get(h)
   incoming_priority=tier_priority.get(tier,1)
   if existing is not None and existing[0] >= incoming_priority:
    target=dest/'duplicates_archive'/source/tier/p.name; kind='exact_duplicate'; dups.append({'from':str(p),'same_as':str(existing[1]),'to':str(target),'sha256':h})
   else:
    target=dest/tier/f'{source}__{p.name}'; kind='active'; known[h]=(incoming_priority,target)
   target.parent.mkdir(parents=True,exist_ok=True)
   if target.exists(): target=target.with_name(f'{target.stem}__{h[:12]}{target.suffix}')
   shutil.move(str(p),str(target)); moved.append({'from':str(p),'to':str(target),'tier':tier,'kind':kind,'sha256':h})
  legacy=dest/'legacy_metadata'/source; legacy.mkdir(parents=True,exist_ok=True)
  for name in ('index.json','decisions.jsonl'):
   old=src/name
   if old.exists():
    target=legacy/name
    if target.exists(): target=legacy/f'{stamp}__{name}'
    shutil.move(str(old),str(target))
  (src/'MIGRATED_TO.json').write_text(json.dumps({'migrated_to':canonical,'migrated_at':stamp,'mode':'safe-alias-scale'},ensure_ascii=False,indent=2)+'\n')
 idx=dest/'index.json'; data={}
 if idx.exists():
  try:data=json.loads(idx.read_text())
  except:data={}
 aliases=sorted(set(data.get('aliases') or [])|set(g['sources']))
 data.update({'product_key':canonical,'canonical_guess':data.get('canonical_guess') or canonical.replace('-',' ').title(),'aliases':aliases,'updated_at':stamp})
 idx.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
 return moved,dups

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--apply',action='store_true'); args=ap.parse_args()
 groups,rejected=build_plan(); stamp=datetime.now(timezone.utc).isoformat(); summary={'created_at':stamp,'mode':'apply' if args.apply else 'dry-run','safe_groups':len(groups),'folders_to_merge':sum(len(g['sources']) for g in groups),'images_seen':sum(g['image_count'] for g in groups),'groups':groups,'rejected_edges':rejected}
 if args.apply:
  aliases=json.loads(ALIASES.read_text()); all_moves=[]; all_dups=[]
  for g in groups:
   moved,dups=apply_group(g,stamp); all_moves+=moved; all_dups+=dups
   for key in g['folders']: aliases[key]=[g['canonical']]
  ALIASES.write_text(json.dumps(aliases,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
  summary.update({'moved':len(all_moves),'exact_duplicates':len(all_dups),'moves':all_moves,'duplicates':all_dups})
 out=BANK/'migration_manifests'/f'safe-consolidation-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{summary["mode"]}.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'mode':summary['mode'],'safe_groups':summary['safe_groups'],'folders_to_merge':summary['folders_to_merge'],'images_seen':summary['images_seen'],'rejected_edges':len(rejected),'moved':summary.get('moved',0),'exact_duplicates':summary.get('exact_duplicates',0),'manifest':str(out),'sample_groups':groups[:15]},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
