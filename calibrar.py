"""Mostra o quanto a curadora concorda com os seus 👍/👎 e promos postadas, e que ajuste melhoraria.

Uso:
  python calibrar.py                            usa o banco principal
  python calibrar.py --banco dados/simulacao.db
  python calibrar.py --aplicar                  grava o ajuste sugerido no config.json (guarda backup)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from curadora import carregar_config
from curadora.banco import conectar
from curadora.calibracao import MINIMO_VOTOS, calibrar, carregar_exemplos, gravar


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--banco", type=Path)
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()
    cfg = carregar_config()
    exemplos = carregar_exemplos(conectar(args.banco))
    print(f"Promos avaliadas por você: {len(exemplos)} "
          f"({sum(e.boa for e in exemplos)} boas, {sum(not e.boa for e in exemplos)} ruins)")
    r = calibrar(exemplos, cfg)
    if r is None:
        print(f"Preciso de pelo menos {MINIMO_VOTOS} para calibrar com segurança. Continue votando 👍/👎 no painel.")
        return
    print(f"Concordância hoje:        {r.concordancia_atual:.0%}")
    print(f"Com o melhor ajuste:      {r.concordancia_melhor:.0%}")
    if r.concordancia_melhor <= r.concordancia_atual + 0.02:
        print("A curadora já concorda com você o máximo que esses ajustes conseguem. Nada a mudar.")
    else:
        f = r.fatores
        print(f"Ajuste sugerido: relevância x{f['relevancia']}, loja x{f['loja']}, sinais do ML x{f['sinais_ml']}, "
              f"BOA a partir de {r.limite_boa}")
    if r.discordancias:
        print("\nOnde a curadora discorda de você hoje:")
        for produto, nota, voto in r.discordancias:
            print(f"  {voto}  {nota:<10}  {produto[:70]}")
    if args.aplicar and r.concordancia_melhor > r.concordancia_atual + 0.02:
        print(f"\nAjuste gravado em config.json (backup em {gravar(cfg, r)}).")


if __name__ == "__main__":
    main()
