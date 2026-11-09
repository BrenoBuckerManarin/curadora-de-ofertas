"""Monta o histórico de preços a partir das mensagens dos grupos-fonte.

Uso: python importar_historico.py [arquivo.jsonl.gz]

1. Lê cada mensagem, extrai a promo e grava a oferta.
2. Aprende o catálogo (gênero que muda preço, volume dominante) e recalcula as chaves.
"""
from __future__ import annotations

import collections
import gzip
import json
import sys
from pathlib import Path

from curadora import RAIZ, carregar_config
from curadora.banco import aprender_catalogo, conectar, inserir_oferta
from curadora.identidade import Catalogo, Identificador
from curadora.parser import ler_promo, parece_perfume

PADRAO = RAIZ.parent / "dados" / "mensagens_grupos_perfume.jsonl.gz"


def epoch(ts) -> int:
    if isinstance(ts, dict):
        ts = ts.get("low") or 0
    ts = int(ts or 0)
    return ts // 1000 if ts > 10**12 else ts


def importar(arquivo: Path, con, cfg: dict) -> collections.Counter:
    grupos = cfg["grupos_fonte"]
    ident = Identificador(cfg["marcas"], cfg.get("sinonimos"))
    provisorio = Catalogo()
    cont: collections.Counter = collections.Counter()
    abrir = gzip.open if str(arquivo).endswith(".gz") else open  # .gz (cópia) ou .jsonl (arquivo vivo da VPS)
    with abrir(arquivo, "rt", encoding="utf-8", errors="replace") as fh:
        for linha in fh:
            try:
                r = json.loads(linha)
            except ValueError:
                cont["linha_invalida"] += 1  # a última linha pode estar sendo escrita agora
                continue
            grupo = str(r.get("chatId", "")).split("@")[0]
            if r.get("fromMe") or grupo not in grupos:
                cont["fora_dos_grupos_fonte"] += 1
                continue
            promo = ler_promo(r.get("body") or "", cfg["marcas"])
            if not promo.valida:
                cont["sem_produto_ou_preco"] += 1
                continue
            if not parece_perfume(promo.produto):
                cont["nao_perfume"] += 1
                continue
            analise = ident.analisar(promo.produto)
            if not analise.base or analise.base == "kit:":
                cont["produto_nao_identificado"] += 1  # ex.: "Perfume Masculino 100ml"
                continue
            novo = inserir_oferta(con, r.get("messageId") or "", epoch(r.get("timestamp")),
                                  grupos[grupo], promo, provisorio.chave(analise))
            cont["importadas" if novo else "ja_existiam"] += 1
    con.commit()
    catalogo = aprender_catalogo(con, ident)
    cont["linhas_com_genero_relevante"] = len(catalogo.generos_relevantes)
    cont["linhas_com_volume_dominante"] = len(catalogo.volumes)
    return cont


def main() -> None:
    arquivo = Path(sys.argv[1]) if len(sys.argv) > 1 else PADRAO
    con = conectar()
    cont = importar(arquivo, con, carregar_config())
    for k, v in cont.most_common():
        print(f"{k:30} {v}")
    linha = con.execute("SELECT COUNT(*) n, COUNT(DISTINCT chave) produtos FROM ofertas").fetchone()
    print(f"\nofertas no banco: {linha['n']} | produtos distintos: {linha['produtos']}")


if __name__ == "__main__":
    main()
