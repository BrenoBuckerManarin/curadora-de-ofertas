#!/usr/bin/env python3
"""Discover and consolidate globally identical high-confidence SKU folders."""
from __future__ import annotations
import importlib.util,json,re,sys
from collections import defaultdict
from pathlib import Path
HOME=Path.home();SCRIPTS=HOME/'.hermes/scripts';BANK=HOME/'.hermes/perfume_image_bank';PRODUCTS=BANK/'products'
spec=importlib.util.spec_from_file_location('c',SCRIPTS/'perfume_image_bank_consolidate_safe.py');assert spec and spec.loader;c=importlib.util.module_from_spec(spec);sys.modules['c']=c;spec.loader.exec_module(c)
BRANDS={
 'boticario':{'boticario','boticrio','oboticario'},'armani':{'armani'},'natura':{'natura'},'lattafa':{'lattafa'},'armaf':{'armaf'},'rasasi':{'rasasi'},
 'maison-alhambra':{'maison','alhambra'},'afnan':{'afnan'},'eudora':{'eudora'},'avon':{'avon'},'jequiti':{'jequiti'},'hinode':{'hinode'},
 'carolina-herrera':{'carolina','herrera'},'ralph-lauren':{'ralph','lauren'},'mawwal':{'mawwal'},'wataniah':{'wataniah'},'rabanne':{'rabanne','rabane'},
 'chanel':{'chanel'},'dior':{'dior'},'azzaro':{'azzaro'},'versace':{'versace'},'mercedes':{'mercedes'},'montblanc':{'montblanc'},'paco-rabanne':{'paco','rabanne'},
 'giorgio-beverly-hills':{'beverly','hills'},'paris-elysees':{'paris','elysees'},'primacial':{'primacial'},'barbours':{'barbour','barbours'},
 'jean-paul-gaultier':{'gaultier'},'antonio-banderas':{'banderas'},'calvin-klein':{'calvin','klein'},
 'jean-paul-gaultier-jpg':{'jpg'},'rayhaan':{'rayhaan'},'khadlaj':{'khadlaj'},'french-avenue':{'french','avenue'},
}
# NOTE (2026-08-02): gender words stay in the signature on purpose. See the
# matching comment in perfume_image_bank_consolidate_safe.py — for houses like
# Armaf Club de Nuit and JPG Scandal the gender word is the SKU itself.
DROP={'perfume','arabe','rabe','masculino','feminino','original','loja','oficial','deo','desodorante','colonia','eau','de','toilette','edp','edt','parfum','spray','importado','com','nf','o','da','do','das','dos','para'}

def raw_tokens(name):return [x for x in name.lower().split('-') if x]
def brand(name):
 t=set(raw_tokens(name));hits=[]
 for label,words in BRANDS.items():
  if t&words:hits.append(label)
 return sorted(set(hits))
def concentration(name):
 t=set(raw_tokens(name))
 if 'parfum' in t:return 'parfum'
 if 'edp' in t or ('eau' in t and 'parfum' in t):return 'edp'
 if 'edt' in t or ('eau' in t and 'toilette' in t):return 'edt'
 return ''
def signature(name):
 t=set(c.tokens(name));v=tuple(sorted(c.vols(t)))
 b=brand(name);conc=concentration(name)
 if len(b)!=1 or not v or not conc:return None
 kit='kit' if t&{'kit','combo','conjunto'} else 'unit'
 nums=tuple(sorted(c.counts(t))) if kit=='kit' else ()
 brand_words=set().union(*(BRANDS[x] for x in b))
 core=tuple(sorted(x for x in t if x not in DROP|brand_words and not x.endswith('ml') and not re.fullmatch(r'\d{1,2}',x)))
 if len(core)<2:return None
 return b[0],core,v,conc,kit,nums

def choose_canonical(folders):
 def rank(name):
  d=PRODUCTS/name;ap=sum(1 for p in c.images(d) if 'approved_final' in p.parts);total=len(c.images(d));return ap,total,1 if (d/'index.json').exists() else 0,-len(name),name
 return max(folders,key=rank)
def main():
 apply='--apply' in sys.argv; buckets=defaultdict(list)
 for d in PRODUCTS.iterdir():
  if not d.is_dir() or (d/'MIGRATED_TO.json').exists() or not c.images(d):continue
  sig=signature(d.name)
  if sig:buckets[sig].append(d.name)
 groups=[]
 for sig,names in buckets.items():
  if len(names)<2:continue
  canonical=choose_canonical(names);sources=sorted(x for x in names if x!=canonical)
  groups.append({'canonical':canonical,'sources':sources,'folders':sorted(names),'signature':sig,'image_count':sum(len(c.images(PRODUCTS/x)) for x in names)})
 groups.sort(key=lambda g:g['canonical']);stamp=c.datetime.now(c.timezone.utc).isoformat();summary={'created_at':stamp,'mode':'apply' if apply else 'dry-run','groups':groups,'safe_groups':len(groups),'folders_to_merge':sum(len(g['sources']) for g in groups)}
 if apply:
  aliases=json.loads(c.ALIASES.read_text());moves=[];dups=[]
  for g in groups:
   m,d=c.apply_group(g,stamp);moves+=m;dups+=d
   for key in g['folders']:
    aliases[key]=[g['canonical']];aliases[c.pmn.product_key(key.replace('-',' '))]=[g['canonical']]
  c.ALIASES.write_text(json.dumps(aliases,ensure_ascii=False,indent=2,sort_keys=True)+'\n');summary.update({'moved':len(moves),'exact_duplicates':len(dups),'moves':moves,'duplicates':dups})
 out=BANK/'migration_manifests'/f'global-exact-{c.datetime.now().strftime("%Y%m%d-%H%M%S")}-{summary["mode"]}.json';out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'mode':summary['mode'],'safe_groups':len(groups),'folders_to_merge':summary['folders_to_merge'],'moved':summary.get('moved',0),'exact_duplicates':summary.get('exact_duplicates',0),'groups':groups,'manifest':str(out)},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
