#!/usr/bin/env python3
"""Build the 12-product V6 mobile preference test. Offline; no delivery."""
from __future__ import annotations
import json,random,re,sys
from pathlib import Path
HOME=Path.home();sys.path.insert(0,str(HOME/'.hermes/scripts'))
import perfume_manual_normalizer as pmn
PAIR=HOME/'.hermes/perfume_image_bank/logs/editorial_pair_eval_latest.json'
OUT=HOME/'.hermes/perfume_image_bank/logs/headline_v6_mobile_preference_12.json'
MD=HOME/'.hermes/perfume_image_bank/logs/headline_v6_mobile_preference_12.md'
TARGETS=[
 ('Al Wataniah Eqaab EDP 100ml','dupe'),('Maison Alhambra Jean Lowe Vibe 100ml','dupe'),
 ('Vulcan Feu EDP 100ml','dupe'),('Vodka Extreme Paris Elysees Intense 100ml','dupe'),
 ('Bidaya Elliur EDP 100ml','arabic_no_dupe'),('Bidaya Ayat EDP 100ml','arabic_no_dupe'),
 ('Hawas Elixir Rasasi EDP 100ml','arabic_no_dupe'),('Malbec Pure Gold O Boticário Desodorante Colônia 100ml','national'),
 ('Quasar Ice Fougère Aldeídica Boticário Deo Colonia 100ml','national'),('Natura Una Tradicional Deo Parfum 75ml','national'),
 ('Attracione Men Com Feromônios Ativados 25ml','premise'),('Body Splash u002F4un De Luxo Alta Concentração 200ml','kit'),
]
def norm(s:str)->str:return pmn._headline_norm(s or '')
def parse(text:str):
 try:return json.loads(text)
 except Exception:
  bs=re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}',text,re.S);return json.loads(bs[-1]) if bs else {}
def main()->int:
 rows=json.loads(PAIR.read_text(encoding='utf-8')).get('pairs') or []
 built=[]
 for number,(wanted,category) in enumerate(TARGETS,1):
  row=next((r for r in reversed(rows) if norm(r.get('product'))==norm(wanted)),None)
  if not row:raise RuntimeError('missing '+wanted)
  product=row['product'];baseline=(row.get('before') or {}).get('headline') or '';source=(row.get('evidence') or {}).get('source_body') or ''
  ctx=pmn.load_editorial_context(product);ctx['_product']=product;ctx['_price_attractive']=False;ctx['_recent_molds']={}
  facts='\n'.join('- '+x for x in pmn.ranked_editorial_facts(product,source,ctx))
  prompt=f'''Crie três alternativas de headline para um teste cego de preferência do Breno.
Não copie a headline atual nem frases históricas. Cada alternativa deve usar estratégia diferente.
B = fato mais forte (dupe/nota/kit). C = outro ângulo factual. D = comercial ou criativa apenas se natural e comprovada.
Máximo 29 caracteres, CAIXA ALTA, sem cupom. No máximo um emoji em todo o trio.
Perfume árabe: sem trocadilho/tradução forçada do nome. Não invente.

PRODUTO: {product}
FONTE: {source[:650]}
FATOS:
{facts}
HEADLINE ATUAL (não copie): {baseline}
JSON apenas: {{"B":"...","C":"...","D":"..."}}'''
  raw,err=pmn.run_headline_llm(prompt);data=parse(raw) if raw else {}
  original=[('incumbent',baseline)]
  for key,role in [('B','strong_fact'),('C','alternate_fact'),('D','commercial_creative')]:
   val=pmn.clean_headline(str(data.get(key) or ''),'')
   if val and all(norm(val)!=norm(x[1]) for x in original):original.append((role,val.strip('* ')))
  # Backfill legal options if provider omitted one.
  if len(original)<4:
   for item in pmn.LAST_HEADLINE_OPTIONS:
    val=str(item.get('headline') or '')
    if not item.get('vetoed') and val and all(norm(val)!=norm(x[1]) for x in original):original.append(('backfill',val))
    if len(original)>=4:break
  original=original[:4]
  # Blind deterministic shuffle; mapping remains only in JSON.
  rng=random.Random(6200+number);rng.shuffle(original)
  opts=[]
  safety=dict(ctx);safety['_recent_molds']={}
  for i,(role,text) in enumerate(original):
   score=pmn.headline_score('*'+text+'*',ctx);safe_score=pmn.headline_score('*'+text+'*',safety)
   opts.append({'letter':chr(65+i),'text':text,'hidden_role':role,'filter_apt':safe_score>=60,'score':score})
  apt=[x for x in opts if x['filter_apt']]
  # Hidden top-1: incumbent wins ties; otherwise highest score.
  top=max(apt or opts,key=lambda x:(x['score'],x['hidden_role']=='incumbent'))
  built.append({'number':number,'product':product,'category':category,'options':opts,'hidden_top1':top['letter'],'source_error':err,'facts':facts})
 OUT.write_text(json.dumps({'status':'awaiting_breno_labels','instructions':'Reply e.g. 1 AB, 2 C, 3 N','items':built},ensure_ascii=False,indent=2),encoding='utf-8')
 lines=['# Teste V6 — preferência mobile','','Responda: `1 AB, 2 C, 3 N` (`N` = nenhuma). Marque **todas** que publicaria sem mudar.','']
 for x in built:
  lines += [f"## {x['number']}. {x['product']}"]+[f"**{o['letter']})** {o['text']}" for o in x['options']]+['']
 MD.write_text('\n'.join(lines),encoding='utf-8')
 print(json.dumps({'products':len(built),'options':sum(len(x['options']) for x in built),'json':str(OUT),'markdown':str(MD)},ensure_ascii=False))
 return 0
if __name__=='__main__':raise SystemExit(main())
