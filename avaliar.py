"""Avalia uma promo colada e mostra o score com as razões.

Uso:
  python avaliar.py "texto da promo"
  python avaliar.py --arquivo promo.txt
  python avaliar.py --sem-ml --arquivo promo.txt    (não abre o link no Mercado Livre)
  (sem texto: cole a promo e termine com Ctrl+Z e Enter no Windows, ou Ctrl+D)
"""
from __future__ import annotations

import sys

from curadora import carregar_config
from curadora.banco import conectar
from curadora.ml import ConsultorML
from curadora.score import Curadora, relatorio


def main() -> None:
    args = sys.argv[1:]
    sem_ml = "--sem-ml" in args
    args = [a for a in args if a != "--sem-ml"]
    if args[:1] == ["--arquivo"] and len(args) > 1:
        texto = open(args[1], encoding="utf-8").read()
    elif args:
        texto = " ".join(args)
    else:
        texto = sys.stdin.read()
    con = conectar()
    curadora = Curadora(carregar_config(), con, None if sem_ml else ConsultorML(con))
    av = curadora.avaliar(texto)
    if av is None:
        print("Não consegui ler produto e preço dessa mensagem.")
        sys.exit(1)
    print(relatorio(av))


if __name__ == "__main__":
    main()
