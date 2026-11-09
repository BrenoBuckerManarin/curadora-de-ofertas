#!/usr/bin/env python3
"""One-SKU pilot: consolidate Malbec Tradicional 100ml folders safely."""
from __future__ import annotations
import hashlib, json, shutil
from datetime import datetime, timezone
from pathlib import Path

BANK = Path.home()/'.hermes/perfume_image_bank'
PRODUCTS = BANK/'products'
CANONICAL = 'o-boticario-malbec-tradicional-100ml'
SOURCES = [
 'boticario-malbec-100ml',
 'desodorante-colonia-malbec-tradicional-fragrancia-amadeirada-o-boticario-100ml',
 'malbec-que-vale-ouro',
 'malbec-tradicional',
 'malbec-tradicional-100ml',
 'malbec-tradicional-desodorante-100ml',
 'o-boticario-malbec-100ml',
 'o-boticario-malbec-tradicional-100ml',
 'o-boticrio-malbec-tradicional-100ml',
 'perfume-boticario-malbec-100ml',
 'perfume-boticrio-malbec-100ml',
 'perfume-malbec-tradicional-100ml',
 'perfume-masculino-malbec-100ml',
 'perfume-o-boticrio-masculino-malbec-100ml',
]
IMAGE_EXT={'.jpg','.jpeg','.png','.webp'}
TIERS={'approved_final','candidate','raw','high_quality','acceptable','rejected','rejected_template_border_low_quality','wrong_volume_moved_to_100ml'}

def sha(p:Path)->str:
 h=hashlib.sha256(); h.update(p.read_bytes()); return h.hexdigest()

def main():
 dest=PRODUCTS/CANONICAL
 dest.mkdir(parents=True,exist_ok=True)
 manifest={'created_at':datetime.now(timezone.utc).isoformat(),'canonical':CANONICAL,'sources':SOURCES,'moves':[],'exact_duplicates':[]}
 active_hashes={}
 for p in dest.rglob('*'):
  if p.is_file() and p.suffix.lower() in IMAGE_EXT:
   active_hashes.setdefault(sha(p),p)
 for source in SOURCES:
  src=PRODUCTS/source
  if not src.exists(): continue
  for p in sorted(src.rglob('*')):
   if not p.is_file() or p.suffix.lower() not in IMAGE_EXT: continue
   rel=p.relative_to(src); tier=rel.parts[0] if rel.parts and rel.parts[0] in TIERS else 'raw'
   h=sha(p)
   if source==CANONICAL: continue
   if h in active_hashes:
    target=dest/'duplicates_archive'/source/tier/p.name
    kind='exact_duplicate'
    manifest['exact_duplicates'].append({'from':str(p),'same_as':str(active_hashes[h]),'to':str(target),'sha256':h})
   else:
    target=dest/tier/f'{source}__{p.name}'
    kind='active'
    active_hashes[h]=target
   target.parent.mkdir(parents=True,exist_ok=True)
   if target.exists(): target=target.with_name(f'{target.stem}__{h[:12]}{target.suffix}')
   shutil.move(str(p),str(target))
   manifest['moves'].append({'from':str(p),'to':str(target),'tier':tier,'kind':kind,'sha256':h})
  if source!=CANONICAL:
   legacy=dest/'legacy_metadata'/source
   legacy.mkdir(parents=True,exist_ok=True)
   for name in ('index.json','decisions.jsonl'):
    old=src/name
    if old.exists(): shutil.move(str(old),str(legacy/name))
   marker={'migrated_to':CANONICAL,'migrated_at':manifest['created_at'],'pilot':'malbec-tradicional-100ml'}
   (src/'MIGRATED_TO.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2)+'\n')
 # canonical index: simple durable identity; legacy metadata remains preserved
 idx=dest/'index.json'
 data={}
 if idx.exists():
  try:data=json.loads(idx.read_text())
  except Exception:data={}
 data.update({'product_key':CANONICAL,'canonical_guess':'O Boticário Malbec Tradicional 100ml','aliases':sorted(set(SOURCES)-{CANONICAL}),'updated_at':manifest['created_at']})
 idx.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
 out=BANK/'migration_manifests'/f'malbec-tradicional-100ml-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json'
 out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'canonical':str(dest),'sources':len(SOURCES),'moved':len(manifest['moves']),'exact_duplicates':len(manifest['exact_duplicates']),'manifest':str(out)},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
