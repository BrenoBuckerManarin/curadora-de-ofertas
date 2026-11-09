"""Análise de uma rodada do pipeline (pedido do Breno em 09/10: "observe bem os erros e analise certinho").

Para cada promo que a curadora mandou: o que ela escolheu, quanto o Gerador demorou, o que a normalizadora publicou
(selo, alertas, foto, linha da curadora), a versão final do Breno (o que ele mudou), se foi para o GrupoFinal e o
preço no ML agora. No fim, os erros agrupados por tipo. Só lê; grava o texto em dados/relatorios/.

Uso:  python3 analisar_pipeline.py                 a rodada de hoje
      python3 analisar_pipeline.py --desde 20:00
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from curadora import RAIZ, carregar_config
from curadora.aovivo import epoch
from curadora.banco import conectar
from curadora.conferente import LINK, chave_link
from curadora.cupons import cupons_da_mensagem
from curadora.ml import ConsultorML

LOGS = Path(os.path.expanduser("~/.hermes/perfume_image_bank/logs"))
TAP = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
REGISTRO = RAIZ / "dados" / "pipeline.jsonl"
EDICAO, GRUPOFINAL = "120363000000000001", "120363000000000002"
POR = re.compile(r"[Pp]or\s+\*?R\$\s*([\d.]+(?:,\d{2})?)")


def hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"


def preco(copia: str) -> float | None:
    m = POR.search(copia or "")
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def headline(copia: str) -> str:
    return re.sub(r"[*_]", "", (copia or "").strip().splitlines()[0] if copia else "").strip()


def linhas_jsonl(caminho: Path, recuo: int = 0):
    if not caminho.exists():
        return
    with caminho.open("rb") as f:
        if recuo:
            f.seek(max(0, caminho.stat().st_size - recuo))
            f.readline()
        for bruto in f:
            try:
                yield json.loads(bruto)
            except ValueError:
                continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--desde", default="00:00")
    args = ap.parse_args()
    h, m = (int(x) for x in args.desde.split(":"))
    desde = dt.datetime.combine(dt.date.today(), dt.time(h, m)).timestamp()
    envios = [r for r in linhas_jsonl(REGISTRO) if r.get("ts", 0) >= desde and r.get("status") != "simulado"]
    if not envios:
        print("nenhum envio do pipeline no período")
        return 0
    chaves = {chave_link(r["link_afiliado"]): r for r in envios if r.get("link_afiliado")}

    normalizadas = {}
    for d in linhas_jsonl(LOGS / "normalizer_feedback.jsonl", 6_000_000):
        k = chave_link(d.get("link") or "")
        if k in chaves and (d.get("ts") or 0) >= desde:
            normalizadas[k] = d
    ciclos = {}
    for d in linhas_jsonl(LOGS / "editorial_comparison_cycles.jsonl", 6_000_000):
        k = chave_link(d.get("link") or "")
        if k in chaves:
            ciclos[k] = d
    edicoes, grupofinal = {}, {}
    for r in linhas_jsonl(TAP, 400_000_000):
        corpo = r.get("body") or ""
        achado = LINK.search(corpo)
        if not achado or epoch(r.get("timestamp")) < desde:
            continue
        k, grupo = chave_link(achado.group(0)), str(r.get("chatId") or "").split("@")[0]
        if k not in chaves:
            continue
        if grupo == EDICAO:
            edicoes[k] = (epoch(r.get("timestamp")), corpo, bool(r.get("mediaPath")))
        elif grupo == GRUPOFINAL and k not in grupofinal:
            grupofinal[k] = (epoch(r.get("timestamp")), str(r.get("senderName") or ""))

    con = conectar(RAIZ / "dados" / "curadora.db")
    ml = ConsultorML(con, ttl=300)
    erros = Counter()
    saida = [f"🔎 *Análise do pipeline* ({dt.date.today():%d/%m}, desde {args.desde}) · {len(envios)} envios", ""]
    for n, e in enumerate(envios, 1):
        k = chave_link(e.get("link_afiliado") or "")
        nd, ed, ci = normalizadas.get(k), edicoes.get(k), ciclos.get(k)
        saida.append(f"*{n}. {e['produto'][:50]}* · {hm(e['ts'])} · escolha: {e['nota']} {e['score']} a R${e['preco']:.2f}")
        if e.get("status") != "enviado":
            saida.append(f"   ✗ {e.get('status')}")
            erros["gerador sem resposta"] += 1
            saida.append("")
            continue
        if not nd:
            saida.append("   ✗ a normalizadora não publicou (sem registro)")
            erros["normalizadora não publicou"] += 1
            saida.append("")
            continue
        copia = nd.get("published_copy") or ""
        cupons = list(cupons_da_mensagem(copia))
        saida.append(f"   normalizadora: {nd.get('approval_grade')} em {nd.get('receipt_to_delivery_seconds') or '?'}s · "
                     f"R${preco(copia) or 0:.0f}{' · cupom ' + ','.join(cupons) if cupons else ''} · "
                     f"headline \"{headline(copia)[:40]}\"")
        for a in nd.get("approval_alerts") or []:
            saida.append(f"   ⚡ {a}")
            erros["alerta: " + re.sub(r"[\d.,]+", "N", a)[:60]] += 1
        if nd.get("curadora_cupom"):
            saida.append(f"   ℹ️ {nd['curadora_cupom']}")
        cur = nd.get("curadora") or {}
        if cur.get("linha"):
            saida.append(f"   🧠 {cur['linha']}")
        foto = str(nd.get("image_path") or "").split("products/")[-1].split("/")[0]
        saida.append(f"   foto da normalizadora: {foto or 'nenhuma'} ({(nd.get('image_decision') or {}).get('source_tier', '?')})")
        if ed:
            ts_ed, final, com_foto = ed
            mudou = []
            p0, p1 = preco(copia), preco(final)
            if p0 and p1 and abs(p0 - p1) > 0.5:
                mudou.append(f"preço R${p0:.0f}→R${p1:.0f}")
            c1 = set(cupons_da_mensagem(final))
            if set(cupons) - c1:
                mudou.append("tirou cupom " + ",".join(sorted(set(cupons) - c1)))
            if c1 - set(cupons):
                mudou.append("pôs cupom " + ",".join(sorted(c1 - set(cupons))))
            if headline(final) != headline(copia):
                mudou.append(f"headline → \"{headline(final)[:40]}\"")
            if ci:
                for campo, nome in (("title_change", "título"), ("image_change", "foto")):
                    classe = (ci.get(campo) or {}).get("class")
                    if classe and classe not in ("unchanged", "cleanup_only", "edition_media_unavailable", "unavailable"):
                        mudou.append(f"{nome}: {classe}")
            saida.append(f"   sua versão ({hm(ts_ed)}): " + ("; ".join(mudou) if mudou else "igual ✓"))
            for item in mudou:
                erros["você mudou: " + item.split(" ")[0]] += 1
        else:
            saida.append("   sua versão: não encontrei edição desse link")
        if k in grupofinal:
            saida.append(f"   GrupoFinal: postado às {hm(grupofinal[k][0])} por {grupofinal[k][1]}")
        else:
            saida.append("   GrupoFinal: não postado")
            erros["não foi para o GrupoFinal"] += 1
        card = ml.consultar(e["link_afiliado"])
        if card.ok and card.preco:
            final_p = preco(ed[1]) if ed else preco(copia)
            dif = f" (post: R${final_p:.0f})" if final_p else ""
            saida.append(f"   ML agora: R${card.preco:.2f}{dif}")
        saida.append("")
    saida.append("*Erros e mudanças, por tipo:*")
    saida += [f"• {k}: {v}" for k, v in erros.most_common()] or ["• nenhum"]
    texto = "\n".join(saida)
    pasta = RAIZ / "dados" / "relatorios"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"pipeline-{dt.date.today().isoformat()}.txt").write_text(texto + "\n", encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
