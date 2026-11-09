#!/usr/bin/env python3
"""Temporal 30-product replay for the personalized perfume headline challenger.

No messages are sent. Each product is generated in an isolated worker with a
calibration cutoff at that cycle's timestamp, preventing the target correction
(or any future correction) from leaking into retrieval examples.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOME = Path.home()
PAIRS = HOME / ".hermes/perfume_image_bank/logs/editorial_pair_eval_latest.json"
OUT_JSON = HOME / ".hermes/perfume_image_bank/logs/headline_challenger_replay_30.json"
OUT_MD = HOME / ".hermes/perfume_image_bank/logs/headline_challenger_replay_30.md"
SELF = Path(__file__).resolve()


def norm(text: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).upper()
    return re.sub(r"[^A-Z0-9$+ ]+", " ", value).strip()


def strict_norm(text: str) -> str:
    """Approval metric: preserve words, punctuation and emoji; ignore WA markup/space only."""
    return re.sub(r"\s+"," ",re.sub(r"[*_`~]","",text or "")).strip().upper()


def lane(text: str) -> str:
    h = norm(text)
    if re.search(r"R\$\s*\d|\b(?:POR|MENOS DE)\s+\d", h):
        return "price"
    if re.search(r"\b(?:FIEL|LEMBRA|ESTILO|INSPIR|DUPE|CLONE|LEITURA)\b", h):
        return "inspiration"
    if re.search(r"\b(?:KIT|TRIO|QUATRO|4 |CADA|UNIDADES?|SPLASHES)\b", h):
        return "kit_value"
    if re.search(r"\b(?:NOITE|NOTURN|VERAO|CALOR|FRIO|ENCONTRO|TRABALHO|DIA)\b", h):
        return "occasion"
    if re.search(r"\b(?:LUXO|COMPENSA|CUSTO|VALE|CARO|NICHO|GRIFE)\b", h):
        return "value"
    if re.search(r"\b(?:CITRIC|FRUTAD|FLORAL|AMBAR|ALMISC|MADEIR|BAUNIL|OUD|TABACO|COURO|LAVANDA|ESPECI|CREMOS|DOCE|FRESCOR|AROMAT)\b", h):
        return "olfactive"
    if re.search(r"\b(?:DESENCALHAR|CHEGAR JUNTO|PAQUERA|CASAR|CORAGEM|ABSOLUTAMENTE|ABAIXO DE 0)\b", h):
        return "premise_or_nameplay"
    return "generic_or_other"


def token_overlap(a: str, b: str) -> float:
    aa={x for x in norm(a).split() if len(x)>=3}; bb={x for x in norm(b).split() if len(x)>=3}
    return len(aa & bb)/max(1,len(aa | bb))


def worker(row: dict) -> dict:
    source_body=(row.get("evidence") or {}).get("source_body") or ""
    source_head=next((re.sub(r"[*_`~]","",x).strip() for x in source_body.splitlines() if x.strip()),"")
    payload=json.dumps({
        "product":row.get("product") or "",
        "raw":source_head,
        "body":source_body,
        "cutoff":int(row.get("normalizer_ts") or 0),
    },ensure_ascii=False)
    run=subprocess.run(
        [sys.executable,str(SELF),"--worker",payload],
        text=True,capture_output=True,timeout=150,env={**os.environ,"PERFUME_HEADLINE_OPTIONS":"8"},
    )
    if run.returncode:
        return {"error":(run.stderr or run.stdout)[-500:]}
    try:
        return json.loads(run.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error":run.stdout[-500:]}


def worker_main(payload: str) -> int:
    data=json.loads(payload)
    os.environ["PERFUME_CALIBRATION_CUTOFF_TS"]=str(data["cutoff"])
    os.environ["PERFUME_HEADLINE_OPTIONS"]="8"
    os.environ["PERFUME_HEADLINE_ARCHITECTURE"]="v3"
    sys.path.insert(0,str(HOME/".hermes/scripts"))
    import perfume_manual_normalizer as pmn
    headline=pmn.llm_headline(data["product"],data["raw"],data["body"],{"attractive":False})
    print(json.dumps({
        "headline":headline.strip("* "),
        "source":pmn.LAST_HEADLINE_SOURCE,
        "reason":pmn.LAST_HEADLINE_REASON,
        "model":pmn.LAST_HEADLINE_MODEL,
        "options":pmn.LAST_HEADLINE_OPTIONS,
        "context":{k:pmn.load_editorial_context(data["product"]).get(k) for k in ("known_inspiration","olfactive_terms","safe_angles")},
    },ensure_ascii=False))
    return 0


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--worker")
    ap.add_argument("--count",type=int,default=30)
    ap.add_argument("--workers",type=int,default=5)
    args=ap.parse_args()
    if args.worker:
        return worker_main(args.worker)
    data=json.loads(PAIRS.read_text(encoding="utf-8"))
    rows=data.get("pairs") or []
    # Last N distinct cycles with an actual human final. This is a temporal
    # holdout replay, not an approval claim for skipped/unpaired promotions.
    sample=rows[-args.count:]
    results: list[dict | None]=[None]*len(sample)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(worker,row):i for i,row in enumerate(sample)}
        for future in as_completed(futures):
            results[futures[future]]=future.result()
    report=[]
    for row,res in zip(sample,results):
        res=res or {"error":"empty result"}
        before=(row.get("before") or {}).get("headline") or ""
        human=(row.get("after") or {}).get("headline") or ""
        challenger=res.get("headline") or ""
        report.append({
            "product":row.get("product"),"link":row.get("link"),
            "baseline":before,"challenger":challenger,"human_final":human,
            "baseline_exact":strict_norm(before)==strict_norm(human),
            "challenger_exact":strict_norm(challenger)==strict_norm(human) if challenger else False,
            "baseline_editorial_text_match":norm(before)==norm(human),
            "challenger_editorial_text_match":norm(challenger)==norm(human) if challenger else False,
            "baseline_lane":lane(before),"challenger_lane":lane(challenger),"human_lane":lane(human),
            "baseline_lane_match":lane(before)==lane(human),
            "challenger_lane_match":lane(challenger)==lane(human) if challenger else False,
            "baseline_overlap":round(token_overlap(before,human),3),
            "challenger_overlap":round(token_overlap(challenger,human),3) if challenger else 0,
            "title_exact":norm((row.get("before") or {}).get("title") or "")==norm((row.get("after") or {}).get("title") or ""),
            "model":res.get("model"),"reason":res.get("reason"),"error":res.get("error"),
            "options":res.get("options") or [],"context":res.get("context") or {},
        })
    ok=[x for x in report if not x.get("error")]
    n=len(ok)
    summary={
        "architecture":"headline-v3-personalized-lanes-title-v3",
        "requested":len(sample),"completed":n,"errors":len(report)-n,
        "baseline_headline_exact":round(sum(x["baseline_exact"] for x in ok)/n,4) if n else None,
        "challenger_headline_exact":round(sum(x["challenger_exact"] for x in ok)/n,4) if n else None,
        "baseline_editorial_text_match":round(sum(x["baseline_editorial_text_match"] for x in ok)/n,4) if n else None,
        "challenger_editorial_text_match":round(sum(x["challenger_editorial_text_match"] for x in ok)/n,4) if n else None,
        "baseline_strategy_match":round(sum(x["baseline_lane_match"] for x in ok)/n,4) if n else None,
        "challenger_strategy_match":round(sum(x["challenger_lane_match"] for x in ok)/n,4) if n else None,
        "baseline_mean_token_overlap":round(sum(x["baseline_overlap"] for x in ok)/n,4) if n else None,
        "challenger_mean_token_overlap":round(sum(x["challenger_overlap"] for x in ok)/n,4) if n else None,
        "title_exact":round(sum(x["title_exact"] for x in ok)/n,4) if n else None,
        "target_exact":0.8,
        "temporal_holdout":True,
    }
    OUT_JSON.write_text(json.dumps({"summary":summary,"rows":report},ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# Replay offline — 30 produtos","",f"```json\n{json.dumps(summary,ensure_ascii=False,indent=2)}\n```","","| # | Produto | Antes | Challenger | Final Breno | Estratégia |","|---:|---|---|---|---|---|"]
    for i,x in enumerate(report,1):
        strategy=f"{x['baseline_lane']}→{x['challenger_lane']} / alvo {x['human_lane']}"
        lines.append(f"| {i} | {x['product']} | {x['baseline']} | {x['challenger'] or 'ERRO'} | {x['human_final']} | {strategy} |")
    OUT_MD.write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False))
    print(OUT_JSON)
    print(OUT_MD)
    return 0 if n==len(sample) else 2


if __name__=="__main__":
    raise SystemExit(main())
