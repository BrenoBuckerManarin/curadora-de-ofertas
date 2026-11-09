#!/usr/bin/env python3
"""Restore approved gate when exact-dedup archived approved bytes behind raw."""
import importlib.util,json,shutil
from datetime import datetime,timezone
from pathlib import Path
HOME=Path.home();BANK=HOME/'.hermes/perfume_image_bank';PRODUCTS=BANK/'products';SCRIPTS=HOME/'.hermes/scripts'
s=importlib.util.spec_from_file_location('pmn',SCRIPTS/'perfume_manual_normalizer.py');assert s and s.loader;m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
canon=set()
for mf in (BANK/'migration_manifests').glob('*.json'):
 try:d=json.loads(mf.read_text())
 except:continue
 if d.get('mode')=='dry-run':continue
 if d.get('canonical'):canon.add(d['canonical'])
 for g in d.get('groups',[]):canon.add(g.get('canonical',''))
canon.discard('');restored=[]
for key in sorted(canon):
 d=PRODUCTS/key;approved=d/'approved_final';active=[p for p in approved.glob('*') if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}] if approved.exists() else []
 if active:continue
 archived=[p for p in (d/'duplicates_archive').rglob('*') if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'} and 'approved_final' in p.parts] if (d/'duplicates_archive').exists() else []
 if not archived:continue
 best=m.best_image_candidate(archived);approved.mkdir(parents=True,exist_ok=True);target=approved/f'restored_approved_gate__{best.name}'
 shutil.copy2(best,target);restored.append({'canonical':key,'from':str(best),'to':str(target)})
out=BANK/'migration_manifests'/f'approved-gate-repair-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json';out.write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),'restored':restored},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'restored_count':len(restored),'restored':restored,'manifest':str(out)},ensure_ascii=False,indent=2))
