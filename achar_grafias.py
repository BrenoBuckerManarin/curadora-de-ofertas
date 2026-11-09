"""Procura no histórico erros de digitação que dividem o mesmo produto em dois (só lista; não grava nada).

Duas buscas:
  1. palavra parecida com uma MARCA (ex.: "lataffa" ~ lattafa): marca escrita errado vira palavra do produto;
  2. troca de UMA palavra que leva a um produto que já existe, mais comum (ex.: "khamah" -> "khamrah").

Revise a lista com cuidado: palavra parecida pode ser outro produto (Botica x Boticário, Ameer x Ameeri).
Os erros confirmados entram em GRAFIAS (curadora/identidade.py); depois rode `python reprocessar.py`
(mostra as junções) e `python reprocessar.py --aplicar`.

Uso:  python achar_grafias.py [--dias 120]
"""
from __future__ import annotations

import argparse
import collections
import difflib
import time

from curadora import carregar_config
from curadora.banco import CAMINHO_PADRAO, conectar
from curadora.identidade import sem_acento


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dias", type=int, default=120)
    ap.add_argument("--banco", default=str(CAMINHO_PADRAO))
    args = ap.parse_args()
    cfg = carregar_config()
    con = conectar(args.banco)
    chaves: collections.Counter = collections.Counter()
    exemplo: dict[str, str] = {}
    for r in con.execute("SELECT chave, produto FROM ofertas WHERE ts >= ?", (time.time() - args.dias * 86400,)):
        chaves[r["chave"]] += 1
        exemplo.setdefault(r["chave"], r["produto"])
    freq: collections.Counter = collections.Counter()
    for ch, n in chaves.items():
        for t in ch.split("|")[0].replace("kit:", "").split("-"):
            if t:
                freq[t] += n

    marcas = {w for m in cfg["marcas"] for w in sem_acento(m.lower()).split() if len(w) >= 4}
    print("== parecidas com MARCA ==")
    for t, n in freq.most_common():
        if t not in marcas and len(t) >= 4:
            achou = difflib.get_close_matches(t, marcas, n=1, cutoff=0.8)
            if achou:
                print(f"  {t:18} ~ marca {achou[0]:16} ({n} obs)")

    print("== troca de uma palavra que junta produtos ==")
    indice = {}
    for ch in chaves:
        base, _, vol = ch.partition("|")
        indice[(base.startswith("kit:"), vol, frozenset(base.replace("kit:", "").split("-")))] = ch
    comuns = [t for t in freq if len(t) >= 4]
    pares: collections.Counter = collections.Counter()
    ex = {}
    for (kit, vol, toks), ch in indice.items():
        for t in toks:
            if len(t) < 4 or freq[t] > 400:
                continue
            for u in difflib.get_close_matches(t, comuns, n=4, cutoff=0.8):
                alvo = None if u == t or u in toks else indice.get((kit, vol, (toks - {t}) | {u}))
                if alvo and chaves[alvo] >= chaves[ch]:
                    pares[(t, u)] += chaves[ch]
                    ex.setdefault((t, u), (exemplo[ch], exemplo[alvo]))
    for (t, u), n in pares.most_common(80):
        a, b = ex[(t, u)]
        print(f"  {t:16} -> {u:16} | {n:3d} obs  ex: {a[:38]!r} ~ {b[:38]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
