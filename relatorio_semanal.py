"""Relatório semanal da curadora: monta o texto, guarda em dados/relatorios/ e (com --enviar) manda no grupo
configurado em config.json -> whatsapp_alertas.grupo (o mesmo dos avisos).

Uso:  python relatorio_semanal.py            mostra o relatório (não envia)
      python relatorio_semanal.py --enviar   manda no grupo (é o que o timer da VPS roda, segunda 9h)
"""
from __future__ import annotations

import argparse
import sys
import time

from curadora import RAIZ, carregar_config
from curadora.alertas import SaidaWhatsApp
from curadora.banco import CAMINHO_PADRAO, conectar
from curadora.relatorio import montar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--enviar", action="store_true", help="manda no grupo do WhatsApp configurado")
    ap.add_argument("--banco", default=str(CAMINHO_PADRAO))
    args = ap.parse_args()
    cfg = carregar_config()
    texto = montar(conectar(args.banco), cfg)
    pasta = RAIZ / "dados" / "relatorios"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"semana-{time.strftime('%Y-%m-%d')}.txt").write_text(texto + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
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
