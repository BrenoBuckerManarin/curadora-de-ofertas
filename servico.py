"""Roda a curadora sem parar: modo ao vivo (mensagens dos grupos) + vigia do ML.

Não publica nada em grupo nenhum: só gera alertas (console e dados/alertas.jsonl).

Uso:
  python servico.py --arquivo ~/.hermes/whatsapp/taps/messages.jsonl --do-fim
  python servico.py --arquivo teste.jsonl --uma-vez --sem-vigia
Opções:
  --do-fim          ignora o que já está no arquivo e lê só o que chegar daqui em diante
  --uma-vez         processa o que houver e sai (para testes)
  --sem-ml          não abre links no ML
  --sem-vigia       não roda a vigia
  --intervalo-vigia segundos entre rodadas da vigia (padrão 600)
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path

from curadora import RAIZ, carregar_config
from curadora.alertas import Alertador, SaidaArquivo, saida_console
from curadora.aovivo import Escuta, arquivo_padrao
from curadora.banco import conectar
from curadora.ml import ConsultorML
from curadora.score import Curadora
from curadora.vigia import Vigia


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arquivo", type=Path, default=arquivo_padrao())
    ap.add_argument("--do-fim", action="store_true")
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--sem-ml", action="store_true")
    ap.add_argument("--sem-vigia", action="store_true")
    ap.add_argument("--intervalo-vigia", type=float, default=600)
    args = ap.parse_args()

    cfg = carregar_config()
    saidas = [saida_console, SaidaArquivo(RAIZ / "dados" / "alertas.jsonl")]

    con = conectar()
    ml = None if args.sem_ml else ConsultorML(con)
    cur = Curadora(cfg, con, ml)
    alertador = Alertador(con, saidas)
    escuta = Escuta(args.arquivo, cur, alertador, cfg["grupos_fonte"])
    if args.do_fim:
        escuta.comecar_do_fim()

    parar = threading.Event()
    if not args.sem_vigia and not args.sem_ml and not args.uma_vez:
        # Conexão e consultor próprios; o cache dura menos que o intervalo, para cada rodada ver o preço novo.
        con_v = conectar()
        cur_v = Curadora(cfg, con_v, ConsultorML(con_v, ttl=max(60, int(args.intervalo_vigia) - 60)))
        vigia = Vigia(cur_v, Alertador(con_v, saidas))
        threading.Thread(target=vigia.rodar, kwargs={"intervalo_s": args.intervalo_vigia, "parar": parar.is_set},
                         daemon=True, name="vigia").start()
        print(f"[vigia] vigiando {len(vigia.nomes)} produtos a cada {args.intervalo_vigia:.0f}s", flush=True)

    print(f"[ao vivo] lendo {args.arquivo}", flush=True)
    try:
        if args.uma_vez:
            escuta.processar_disponiveis()
        else:
            escuta.rodar(parar=parar.is_set)
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        print(f"[ao vivo] {escuta.estatisticas}", flush=True)


if __name__ == "__main__":
    main()
