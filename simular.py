"""Ensaio do modo ao vivo com mensagens reais: as últimas N horas chegam "agora", uma a uma.

Usa uma CÓPIA do banco sem as ofertas dessa janela (para a promo não ser comparada com ela mesma)
e mede o tempo entre a mensagem chegar e a curadora terminar (com consulta real ao ML nas candidatas).

Uso: python simular.py --horas 6 [--sem-ml]
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from curadora import RAIZ, carregar_config
from curadora.alertas import Alertador, saida_console
from curadora.aovivo import Escuta, epoch
from curadora.banco import CAMINHO_PADRAO, conectar
from curadora.ml import ConsultorML
from curadora.score import Curadora

MENSAGENS = RAIZ.parent / "dados" / "mensagens_grupos_perfume.jsonl.gz"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=float, default=6)
    ap.add_argument("--sem-ml", action="store_true")
    args = ap.parse_args()
    cfg = carregar_config()

    with gzip.open(MENSAGENS, "rt", encoding="utf-8") as fh:
        todas = [json.loads(l) for l in fh]
    fim = max(epoch(r.get("timestamp")) for r in todas)
    inicio = fim - int(args.horas * 3600)
    janela = sorted((r for r in todas if epoch(r.get("timestamp")) >= inicio), key=lambda r: epoch(r.get("timestamp")))

    tmp = Path(tempfile.mkdtemp(prefix="curadora_sim_"))
    banco = tmp / "sim.db"
    shutil.copy(CAMINHO_PADRAO, banco)
    con = conectar(banco)
    con.execute("DELETE FROM ofertas WHERE ts >= ?", (inicio,))
    con.execute("DELETE FROM alertas")
    con.commit()

    cur = Curadora(cfg, con, None if args.sem_ml else ConsultorML(con))
    atrasos: list[float] = []
    alertador = Alertador(con, [saida_console])
    tap = tmp / "messages.jsonl"
    tap.write_text("", encoding="utf-8")
    escuta = Escuta(tap, cur, alertador, cfg["grupos_fonte"])

    tempos: list[float] = []
    print(f"reproduzindo {len(janela)} mensagens das últimas {args.horas:g}h...\n", flush=True)
    for r in janela:
        r = dict(r, timestamp=int(time.time()))  # chega agora
        with tap.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        t0 = time.perf_counter()
        alertadas = escuta.processar_disponiveis()
        dt = time.perf_counter() - t0
        tempos.append(dt)
        if alertadas:
            atrasos.append(dt)
            print(f"   ⏱  alerta {dt:.2f}s depois de a mensagem chegar\n", flush=True)

    e = escuta.estatisticas
    print("=" * 60)
    print(f"mensagens: {e['lidas']} | promos de perfume: {e['promos']} | conferidas no ML: {e['conferidas_ml']} "
          f"| alertas: {e['alertas']}")
    if tempos:
        q = statistics.quantiles(tempos, n=20)
        print(f"tempo por mensagem: mediana {statistics.median(tempos)*1000:.0f} ms | p95 {q[18]*1000:.0f} ms "
              f"| máx {max(tempos):.2f} s")
    if atrasos:
        print(f"tempo até o alerta: mediana {statistics.median(atrasos):.2f} s | máx {max(atrasos):.2f} s")
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
