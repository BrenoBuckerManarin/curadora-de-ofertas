#!/usr/bin/env python3
"""V4 contrastive editor: plan -> independent authors -> separate judge -> replay.

Offline only. Temporal cutoff prevents target/future corrections from leaking into
retrieval. The author uses the configured primary chain; the judge uses DeepSeek
HTTP explicitly so generation and selection are not the same editorial pass.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOME=Path.home(); SELF=Path(__file__).resolve()
PAIR_PATH=HOME/'.hermes/perfume_image_bank/logs/editorial_pair_eval_latest.json'
OUT=HOME/'.hermes/perfume_image_bank/logs/headline_v4_contrastive_replay_30.json'
OUT_MD=HOME/'.hermes/perfume_image_bank/logs/headline_v4_contrastive_replay_30.md'


def strict(text:str)->str:
    return re.sub(r'\s+',' ',re.sub(r'[*_`~]','',text or '')).strip().upper()

def plain(text:str)->str:
    import unicodedata
    s=''.join(c for c in unicodedata.normalize('NFKD',strict(text)) if not unicodedata.combining(c))
    return re.sub(r'[^A-Z0-9$+ ]+',' ',s).strip()

def lane(text:str)->str:
    h=plain(text)
    if re.search(r'R\$\s*\d|\b(?:POR|MENOS DE)\s+\d',h): return 'price'
    if re.search(r'\b(?:FIEL|LEMBRA|ESTILO|INSPIR|DUPE|CLONE|LEITURA)\b',h): return 'inspiration'
    if re.search(r'\b(?:KIT|TRIO|QUATRO|4 |CADA|UNIDADES?|SPLASHES)\b',h): return 'kit_value'
    if re.search(r'\b(?:NOITE|NOTURN|VERAO|CALOR|FRIO|ENCONTRO|TRABALHO|DIA)\b',h): return 'occasion'
    if re.search(r'\b(?:LUXO|COMPENSA|CUSTO|VALE|CARO|NICHO|GRIFE|DESCONTAO)\b',h): return 'value'
    if re.search(r'\b(?:CITRIC|FRUTAD|FLORAL|AMBAR|ALMISC|MADEIR|BAUNIL|OUD|TABACO|COURO|LAVANDA|ESPECI|CREMOS|DOCE|FRESCOR|AROMAT)\b',h): return 'olfactive'
    if re.search(r'\b(?:DESENCALHAR|CHEGAR JUNTO|PAQUERA|CASAR|CORAGEM|ABSOLUTAMENTE|ABAIXO DE 0|UNICA COMO|CHIFRE)\b',h): return 'premise_or_nameplay'
    return 'generic_or_other'

def parse_json(text:str):
    try:return json.loads(text)
    except Exception:
        blocks=re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}',text,re.S)
        return json.loads(blocks[-1]) if blocks else {}

def worker(row:dict)->dict:
    payload=json.dumps({'row':row},ensure_ascii=False)
    p=subprocess.run([sys.executable,str(SELF),'--worker',payload],text=True,capture_output=True,timeout=240)
    if p.returncode:return {'error':(p.stderr or p.stdout)[-1000:]}
    try:return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:return {'error':p.stdout[-1000:]}

def worker_main(payload:str)->int:
    data=json.loads(payload); row=data['row']; cutoff=int(row.get('normalizer_ts') or 0)
    os.environ['PERFUME_CALIBRATION_CUTOFF_TS']=str(cutoff)
    sys.path.insert(0,str(HOME/'.hermes/scripts'))
    import perfume_manual_normalizer as pmn
    product=row.get('product') or ''
    source=(row.get('evidence') or {}).get('source_body') or ''
    ctx=pmn.load_editorial_context(product); ctx['_product']=product; ctx['_price_attractive']=False; ctx['_recent_molds']={}
    facts='\n'.join('- '+x for x in pmn.ranked_editorial_facts(product,source,ctx))
    pairs=pmn.breno_corrected_headline_pairs(8,product)
    contrasts='\n'.join(f'- PERDEU: {a} | VENCEU: {b}' for a,b in pairs) or '- sem pares anteriores comparáveis'
    arabic=pmn._arabic_perfume_product(product)
    author_prompt=f'''Você é uma mesa editorial de perfume. Primeiro decida o PLANO; depois três autores independentes escrevem candidatos.
Não escolha vencedor. Não copie literalmente exemplos. Máximo 29 caracteres, caixa alta, no máximo um emoji coerente.
Perfume árabe: sem trocadilho/tradução forçada do nome. Nunca invente fatos.

PRODUTO: {product}
TEXTO-FONTE: {source[:700]}
FAMÍLIA: {'árabe' if arabic else 'não árabe'}
FATOS:
{facts}

PREFERÊNCIAS CONTRASTIVAS HISTÓRICAS (a esquerda perdeu, a direita venceu):
{contrasts}

PLANEJADOR: responda qual verdade vender, qual salto comercial é permitido e o que evitar.
AUTORES:
- factual: 3 candidatas específicas, humanas, sem catálogo.
- comercial: 3 candidatas sobre desejo, valor percebido ou diferencial comprovado.
- criativo: 3 candidatas com humor/imagem/jogo natural apenas quando sustentado.
As 9 precisam ser ideias diferentes, não paráfrases.
Responda SOMENTE JSON:
{{"plan":{{"angle":"...","allowed_leap":"...","avoid":["..."]}},"factual":["...","...","..."],"commercial":["...","...","..."],"creative":["...","...","..."]}}'''
    author_out,author_err=pmn.run_headline_llm(author_prompt)
    author=parse_json(author_out) if author_out else {}
    raw=[]
    for role in ('factual','commercial','creative'):
        for value in author.get(role) or []: raw.append((role,str(value)))
    candidates=[]
    for role,value in raw:
        clean=pmn.clean_headline(value,'')
        if not clean or any(plain(clean)==plain(x['headline']) for x in candidates):continue
        score=pmn.headline_score(clean,ctx)
        safety=dict(ctx); safety['_recent_molds']={}
        if pmn.headline_score(clean,safety)<60:continue
        candidates.append({'id':chr(65+len(candidates)),'role':role,'headline':clean.strip('* '),'score':score})
    if not candidates:
        print(json.dumps({'error':'no legal candidates','author_error':author_err,'author_raw':author_out[-1000:]},ensure_ascii=False))
        return 0
    cards='\n'.join(f"{x['id']}) {x['headline']}" for x in candidates)
    judge_prompt=f'''Você é o editor final contrastivo de Breno. Você NÃO escreveu as opções. Escolha por confronto, não por soma de palavras.
Prioridades: verdade; sacada comercial específica; naturalidade brasileira; memorabilidade; zero cara de catálogo.
Uma descrição segura porém intercambiável perde para uma ideia ligada à premissa real do produto.
Dupe comprovado é forte, mas não precisa vencer perfil/nota ou detalhe visual melhor.
Árabe não aceita trocadilho forçado com o nome. Não premie frase carregada só por conter dupe+preço+nota.

PRODUTO: {product}
PLANO: {json.dumps(author.get('plan') or {},ensure_ascii=False)}
FATOS:
{facts}
CONTRASTES REAIS:
{contrasts}
CANDIDATAS ANÔNIMAS:
{cards}

Faça torneio mental par a par. Responda SOMENTE JSON:
{{"winner":"A","runner_up":"B","confidence":0.0,"why":"...","loser_pattern":"..."}}'''
    judge_out,judge_err=pmn.run_headline_llm_http(judge_prompt,'deepseek-v4-flash')
    judge=parse_json(judge_out) if judge_out else {}
    win=str(judge.get('winner') or '').strip().upper()
    chosen=next((x for x in candidates if x['id']==win),None)
    if not chosen:
        chosen=max(candidates,key=lambda x:x['score'])
    print(json.dumps({'headline':chosen['headline'],'role':chosen['role'],'plan':author.get('plan') or {},'judge':judge,'candidates':candidates,'author_model':pmn.LAST_HEADLINE_MODEL,'author_error':author_err,'judge_error':judge_err,'context':{k:ctx.get(k) for k in ('known_inspiration','olfactive_terms','safe_angles')}},ensure_ascii=False))
    return 0

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--worker'); ap.add_argument('--count',type=int,default=30); ap.add_argument('--workers',type=int,default=5); args=ap.parse_args()
    if args.worker:return worker_main(args.worker)
    rows=(json.loads(PAIR_PATH.read_text(encoding='utf-8')).get('pairs') or [])[-args.count:]
    results: list[dict | None]=[None]*len(rows)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut={pool.submit(worker,row):i for i,row in enumerate(rows)}
        for f in as_completed(fut):results[fut[f]]=f.result()
    out=[]
    for row,res in zip(rows,results):
        res=res or {'error':'empty'}; base=(row.get('before') or {}).get('headline') or ''; human=(row.get('after') or {}).get('headline') or ''; v4=res.get('headline') or ''
        out.append({'product':row.get('product'),'baseline':base,'v4':v4,'human_final':human,'baseline_exact':strict(base)==strict(human),'v4_exact':strict(v4)==strict(human) if v4 else False,'baseline_text_match':plain(base)==plain(human),'v4_text_match':plain(v4)==plain(human) if v4 else False,'baseline_lane':lane(base),'v4_lane':lane(v4),'human_lane':lane(human),'baseline_strategy_match':lane(base)==lane(human),'v4_strategy_match':lane(v4)==lane(human) if v4 else False,'title_exact':strict((row.get('before') or {}).get('title') or '')==strict((row.get('after') or {}).get('title') or ''),'error':res.get('error'),**{k:res.get(k) for k in ('role','plan','judge','candidates','author_model','author_error','judge_error','context')}})
    ok=[x for x in out if not x['error']]; n=len(ok)
    s={'architecture':'v4-contrastive-plan-authors-independent-judge','requested':len(rows),'completed':n,'errors':len(rows)-n,'baseline_exact':round(sum(x['baseline_exact'] for x in ok)/n,4) if n else None,'v4_exact':round(sum(x['v4_exact'] for x in ok)/n,4) if n else None,'baseline_text_match':round(sum(x['baseline_text_match'] for x in ok)/n,4) if n else None,'v4_text_match':round(sum(x['v4_text_match'] for x in ok)/n,4) if n else None,'baseline_strategy_match':round(sum(x['baseline_strategy_match'] for x in ok)/n,4) if n else None,'v4_strategy_match':round(sum(x['v4_strategy_match'] for x in ok)/n,4) if n else None,'title_exact':round(sum(x['title_exact'] for x in ok)/n,4) if n else None,'temporal_holdout':True,'promoted':False}
    OUT.write_text(json.dumps({'summary':s,'rows':out},ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# V4 contrastiva — replay de 30 produtos','',f'```json\n{json.dumps(s,ensure_ascii=False,indent=2)}\n```','','| # | Produto | V2 | V4 | Final Breno | Estratégia |','|---:|---|---|---|---|---|']
    for i,x in enumerate(out,1):lines.append(f"| {i} | {x['product']} | {x['baseline']} | {x['v4'] or 'ERRO'} | {x['human_final']} | {x['baseline_lane']}→{x['v4_lane']} / alvo {x['human_lane']} |")
    OUT_MD.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(s,ensure_ascii=False)); print(OUT); print(OUT_MD)
    return 0 if n==len(rows) else 2

if __name__=='__main__':raise SystemExit(main())
