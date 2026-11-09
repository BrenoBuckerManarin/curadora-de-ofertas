"""Relatório diário do experimento curadora × equipe (o timer da VPS roda às 22:15 nos dias do experimento).

Uso:  python relatorio_experimento.py                     mostra o relatório de hoje (não envia)
      python relatorio_experimento.py --dia 2026-10-06    de outro dia
      python relatorio_experimento.py --enviar            manda no grupo dos avisos (whatsapp_alertas.grupo)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from curadora import RAIZ, carregar_config
from curadora.alertas import SaidaWhatsApp
from curadora.banco import CAMINHO_PADRAO, conectar
from curadora.experimento import config, dia_ativo, relatorio


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dia", default=dt.date.today().isoformat())
    ap.add_argument("--enviar", action="store_true")
    ap.add_argument("--banco", default=str(CAMINHO_PADRAO))
    args = ap.parse_args()
    cfg = carregar_config()
    dia = dt.date.fromisoformat(args.dia)
    sys.stdout.reconfigure(encoding="utf-8")
    if not dia_ativo(dia, config(cfg)):
        print(f"{dia} está fora do período do experimento: nada a fazer.")
        return 0
    nome = (cfg.get("grupo_postagens") or {}).get("nome", "grupo da equipe")
    texto = relatorio(conectar(args.banco), cfg, dia, nome)
    pasta = RAIZ / "dados" / "relatorios"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"experimento-{dia.isoformat()}.txt").write_text(texto + "\n", encoding="utf-8")
    print(texto)
    if args.enviar:
        saida = SaidaWhatsApp.do_config(cfg)
        if saida is None:
            print("\n(sem grupo em whatsapp_alertas.grupo: não enviado)")
            return 1
        saida.enviar_texto(texto)
        print("\nenviado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
