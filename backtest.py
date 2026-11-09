"""O que a curadora teria escolhido num período do histórico.

Uso:
  python backtest.py                 últimas 24h do histórico, sem abrir o ML
  python backtest.py --horas 6 --top 10
  python backtest.py --horas 3 --ml 8 confere no ML ao vivo as 8 melhores (preço/loja de AGORA)

Cada promo é avaliada só com o histórico anterior a ela (nunca olha o futuro).
"""
from __future__ import annotations

import argparse
import collections
import time

from curadora import carregar_config
from curadora.banco import conectar
from curadora.ml import ConsultorML
from curadora.score import Curadora, ranquear, relatorio


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=float, default=24)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--ml", type=int, default=0, help="conferir no ML as N melhores")
    args = ap.parse_args()

    con = conectar()
    cfg = carregar_config()
    cur = Curadora(cfg, con, ConsultorML(con) if args.ml else None)
    fim = con.execute("SELECT MAX(ts) FROM ofertas").fetchone()[0]
    linhas = con.execute("SELECT ts, texto, grupo FROM ofertas WHERE ts >= ? ORDER BY ts",
                         (fim - int(args.horas * 3600),)).fetchall()
    avs = []
    for r in linhas:
        av = cur.avaliar(r["texto"], agora=r["ts"], conferir_ml=False)
        if av:
            av.grupo = r["grupo"]
            avs.append(av)
    ranking = ranquear(avs)
    if args.ml:
        conferidas = []
        for av in ranking[:args.ml]:
            nova = cur.avaliar(av.promo, agora=int(time.time()))
            nova.grupo = av.grupo
            conferidas.append(nova)
        ranking = ranquear(conferidas) + ranking[args.ml:]

    notas = collections.Counter(a.nota for a in ranking)
    print(f"{len(avs)} promos em {args.horas:g}h -> {len(ranking)} ofertas únicas | "
          + " | ".join(f"{n}: {notas[n]}" for n in ("IMPERDÍVEL", "BOA", "OK", "NÃO VALE")))
    print()
    for av in ranking[:args.top]:
        print(f"[{av.grupo}]")
        print(relatorio(av))
        print()


if __name__ == "__main__":
    main()
