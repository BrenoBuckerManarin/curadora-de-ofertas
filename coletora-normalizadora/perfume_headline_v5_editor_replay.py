#!/usr/bin/env python3
"""V5 conservative editor replay: V2 -> KEEP/REWRITE critic -> restricted rewrite.

Offline only. Uses a temporal holdout so the current/future human correction is
never available to critic or rewriter. Production remains untouched.
"""
from __future__ import annotations
import argparse,json,os,re,subprocess,sys
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
HOME=Path.home(); SELF=Path(__file__).resolve()
PAIR_PATH=HOME/'.hermes/perfume_image_bank/logs/editorial_pair_eval_latest.json'
OUT=HOME/'.hermes/perfume_image_bank/logs/headline_v5_editor_replay_30.json'
OUT_MD=HOME/'.hermes/perfume_image_bank/logs/headline_v5_editor_replay_30.md'

def strict(s:str)->str:return re.sub(r'\s+',' ',re.sub(r'[*_`~]','',s or '')).strip().upper()
def plain(s:str)->str:
 import unicodedata
 x=''.join(c for c in unicodedata.normalize('NFKD',strict(s)) if not unicodedata.combining(c))
 return re.sub(r'[^A-Z0-9$+ ]+',' ',x).strip()
def lane(text:str)->str:
 h=plain(text)
 if re.search(r'R\$\s*\d|\b(?:POR|MENOS DE)\s+\d',h):return 'price'
 if re.search(r'\b(?:FIEL|LEMBRA|ESTILO|INSPIR|DUPE|CLONE|LEITURA)\b',h):return 'inspiration'
 if re.search(r'\b(?:KIT|TRIO|QUATRO|4 |CADA|UNIDADES?|SPLASHES)\b',h):return 'kit_value'
 if re.search(r'\b(?:NOITE|NOTURN|VERAO|CALOR|FRIO|ENCONTRO|TRABALHO|DIA)\b',h):return 'occasion'
 if re.search(r'\b(?:LUXO|COMPENSA|CUSTO|VALE|CARO|NICHO|GRIFE|DESCONTAO)\b',h):return 'value'
 if re.search(r'\b(?:CITRIC|FRUTAD|FLORAL|AMBAR|ALMISC|MADEIR|BAUNIL|OUD|TABACO|COURO|LAVANDA|ESPECI|CREMOS|DOCE|FRESCOR|AROMAT)\b',h):return 'olfactive'
 if re.search(r'\b(?:DESENCALHAR|CHEGAR JUNTO|PAQUERA|CASAR|CORAGEM|ABSOLUTAMENTE|ABAIXO DE 0|UNICA COMO|CHIFRE)\b',h):return 'premise_or_nameplay'
 return 'generic_or_other'
def parse(text:str):
 try:return json.loads(text)
 except Exception:
  blocks=re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}',text,re.S)
  return json.loads(blocks[-1]) if blocks else {}
def worker(row:dict)->dict:
 p=subprocess.run([sys.executable,str(SELF),'--worker',json.dumps(row,ensure_ascii=False)],text=True,capture_output=True,timeout=300)
 if p.returncode:return {'error':(p.stderr or p.stdout)[-1200:]}
 try:return json.loads(p.stdout.strip().splitlines()[-1])
 except Exception:return {'error':p.stdout[-1200:]}
def worker_main(payload:str)->int:
 row=json.loads(payload); os.environ['PERFUME_CALIBRATION_CUTOFF_TS']=str(int(row.get('normalizer_ts') or 0))
 sys.path.insert(0,str(HOME/'.hermes/scripts')); import perfume_manual_normalizer as pmn
 product=row.get('product') or ''; baseline=(row.get('before') or {}).get('headline') or ''; source=(row.get('evidence') or {}).get('source_body') or ''
 ctx=pmn.load_editorial_context(product); ctx['_product']=product; ctx['_price_attractive']=False; ctx['_recent_molds']={}
 facts='\n'.join('- '+x for x in pmn.ranked_editorial_facts(product,source,ctx))
 pairs=pmn.breno_corrected_headline_pairs(8,product); contrasts='\n'.join(f'- {a} → {b}' for a,b in pairs) or '- nenhum comparável anterior'
 critic_prompt=f'''Você é um crítico conservador da headline V2 de Breno. NÃO reescreva. Decida apenas KEEP ou REWRITE.
KEEP é padrão: mantenha se curta, natural, específica, verdadeira e comercialmente defensável. Não exija novidade.
REWRITE somente com defeito claro: generic, catalog, wrong_angle, forced, overloaded, weak_commercial, factual_risk.
Uma linha factual boa não perde só porque existe dupe. Uma linha genérica não ganha só por ser segura.
Em árabe, sem trocadilho forçado. Considere pares históricos como decisões, não texto para copiar.

PRODUTO: {product}
FONTE: {source[:700]}
FATOS: {facts}
V2: {baseline}
PARES ANTERIORES: {contrasts}

Responda SOMENTE JSON:
{{"decision":"KEEP|REWRITE","defect":"none|generic|catalog|wrong_angle|forced|overloaded|weak_commercial|factual_risk","strongest_fact":"...","recommended_angle":"...","confidence":0.0,"why":"..."}}'''
 critic_raw,critic_err=pmn.run_headline_llm(critic_prompt); critic=parse(critic_raw) if critic_raw else {}
 decision=str(critic.get('decision') or 'KEEP').upper(); confidence=float(critic.get('confidence') or 0)
 # Conservative gate: low-confidence rewrites abstain and preserve V2.
 if decision!='REWRITE' or confidence<0.72:
  print(json.dumps({'final':baseline,'action':'KEEP','critic':critic,'critic_error':critic_err,'candidates':[]},ensure_ascii=False));return 0
 angle=str(critic.get('recommended_angle') or critic.get('strongest_fact') or '')
 rewrite_prompt=f'''Reescreva UMA headline de perfume seguindo um diagnóstico fechado. Não reavalie o ângulo.
Máximo 29 caracteres, CAIXA ALTA, natural no WhatsApp, sem frase de agência. Gere exatamente 4 opções realmente distintas.
Não invente fatos. Árabe: sem trocadilho/tradução forçada do nome. Emoji em no máximo uma opção.

PRODUTO: {product}
FATOS: {facts}
V2 COM DEFEITO: {baseline}
DEFEITO: {critic.get('defect')}
ÚNICO ÂNGULO: {angle}
MOTIVO: {critic.get('why')}

JSON apenas: {{"options":["...","...","...","..."]}}'''
 rewrite_raw,rewrite_err=pmn.run_headline_llm(rewrite_prompt); rewrite=parse(rewrite_raw) if rewrite_raw else {}
 candidates=[]
 for value in rewrite.get('options') or []:
  c=pmn.clean_headline(str(value),'')
  if not c or any(plain(c)==plain(x) for x in candidates):continue
  safe=dict(ctx);safe['_recent_molds']={}
  if pmn.headline_score(c,safe)>=60:candidates.append(c.strip('* '))
 if not candidates:
  print(json.dumps({'final':baseline,'action':'KEEP_NO_LEGAL_REWRITE','critic':critic,'critic_error':critic_err,'rewrite_error':rewrite_err,'candidates':[]},ensure_ascii=False));return 0
 cards='\n'.join(f'{chr(65+i)}) {x}' for i,x in enumerate([baseline]+candidates))
 final_prompt=f'''Você é o crítico conservador final. Compare a V2 original (A) com reescritas no ângulo já aprovado.
Troque somente se uma nova opção for CLARAMENTE superior em especificidade, força comercial e naturalidade, sem perder verdade.
Empate ou ganho pequeno = A. Não premie novidade, trocadilho ou palavra bonita por si só.
PRODUTO: {product}
FATOS: {facts}
DEFEITO DIAGNOSTICADO: {critic.get('defect')}
ÂNGULO: {angle}
OPÇÕES:
{cards}
JSON apenas: {{"winner":"A","confidence":0.0,"why":"..."}}'''
 final_raw,final_err=pmn.run_headline_llm(final_prompt); verdict=parse(final_raw) if final_raw else {}; win=str(verdict.get('winner') or 'A').upper()
 idx=ord(win)-65 if len(win)==1 else 0; allc=[baseline]+candidates; chosen=allc[idx] if 0<=idx<len(allc) else baseline
 print(json.dumps({'final':chosen,'action':'KEEP_AFTER_COMPARE' if chosen==baseline else 'REWRITE','critic':critic,'verdict':verdict,'critic_error':critic_err,'rewrite_error':rewrite_err,'final_error':final_err,'candidates':candidates},ensure_ascii=False));return 0
def main()->int:
 ap=argparse.ArgumentParser();ap.add_argument('--worker');ap.add_argument('--count',type=int,default=30);ap.add_argument('--workers',type=int,default=5);args=ap.parse_args()
 if args.worker:return worker_main(args.worker)
 rows=(json.loads(PAIR_PATH.read_text(encoding='utf-8')).get('pairs') or [])[-args.count:];results:list[dict|None]=[None]*len(rows)
 with ThreadPoolExecutor(max_workers=args.workers) as pool:
  fs={pool.submit(worker,r):i for i,r in enumerate(rows)}
  for f in as_completed(fs):results[fs[f]]=f.result()
 out=[]
 for row,res in zip(rows,results):
  res=res or {'error':'empty'};base=(row.get('before') or {}).get('headline') or '';human=(row.get('after') or {}).get('headline') or '';v5=res.get('final') or ''
  expected='KEEP' if plain(base)==plain(human) else 'REWRITE';pred='KEEP' if str(res.get('action') or '').startswith('KEEP') else 'REWRITE'
  out.append({'product':row.get('product'),'baseline':base,'v5':v5,'human_final':human,'expected_action':expected,'predicted_action':pred,'diagnosis_correct':expected==pred,'baseline_exact':strict(base)==strict(human),'v5_exact':strict(v5)==strict(human) if v5 else False,'baseline_text_match':plain(base)==plain(human),'v5_text_match':plain(v5)==plain(human) if v5 else False,'baseline_lane':lane(base),'v5_lane':lane(v5),'human_lane':lane(human),'baseline_strategy_match':lane(base)==lane(human),'v5_strategy_match':lane(v5)==lane(human) if v5 else False,'title_exact':strict((row.get('before') or {}).get('title') or '')==strict((row.get('after') or {}).get('title') or ''),'error':res.get('error'),**{k:res.get(k) for k in ('action','critic','verdict','candidates','critic_error','rewrite_error','final_error')}})
 ok=[x for x in out if not x['error']];n=len(ok); rew=[x for x in ok if x['expected_action']=='REWRITE']; keep=[x for x in ok if x['expected_action']=='KEEP']
 s={'architecture':'v5-conservative-keep-rewrite-editor','requested':len(rows),'completed':n,'errors':len(rows)-n,'diagnosis_accuracy':round(sum(x['diagnosis_correct'] for x in ok)/n,4) if n else None,'keep_recall':round(sum(x['predicted_action']=='KEEP' for x in keep)/len(keep),4) if keep else None,'rewrite_recall':round(sum(x['predicted_action']=='REWRITE' for x in rew)/len(rew),4) if rew else None,'baseline_exact':round(sum(x['baseline_exact'] for x in ok)/n,4) if n else None,'v5_exact':round(sum(x['v5_exact'] for x in ok)/n,4) if n else None,'baseline_text_match':round(sum(x['baseline_text_match'] for x in ok)/n,4) if n else None,'v5_text_match':round(sum(x['v5_text_match'] for x in ok)/n,4) if n else None,'baseline_strategy_match':round(sum(x['baseline_strategy_match'] for x in ok)/n,4) if n else None,'v5_strategy_match':round(sum(x['v5_strategy_match'] for x in ok)/n,4) if n else None,'title_exact':round(sum(x['title_exact'] for x in ok)/n,4) if n else None,'temporal_holdout':True,'promoted':False}
 OUT.write_text(json.dumps({'summary':s,'rows':out},ensure_ascii=False,indent=2),encoding='utf-8')
 lines=['# V5 editor conservador — replay de 30 produtos','',f'```json\n{json.dumps(s,ensure_ascii=False,indent=2)}\n```','','| # | Produto | Ação esperada/predita | V2 | V5 | Final Breno |','|---:|---|---|---|---|---|']
 for i,x in enumerate(out,1):lines.append(f"| {i} | {x['product']} | {x['expected_action']}/{x['predicted_action']} | {x['baseline']} | {x['v5'] or 'ERRO'} | {x['human_final']} |")
 OUT_MD.write_text('\n'.join(lines)+'\n',encoding='utf-8');print(json.dumps(s,ensure_ascii=False));print(OUT);print(OUT_MD);return 0 if n==len(rows) else 2
if __name__=='__main__':raise SystemExit(main())
