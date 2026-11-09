"""Manutenção da curadora na VPS: cópia de segurança e vigilante.

  python3 manutencao.py backup       cópia do banco (consistente mesmo com a curadora rodando), compactada;
                                      guarda as últimas 3
  python3 manutencao.py verificar    o painel responde? as mensagens estão sendo lidas? o disco aguenta?
                                      Se travou, reinicia o serviço. Avisa no grupo de alertas (se configurado).

Rodados pelos timers vps/curadora-backup.timer (todo dia 04:00) e vps/curadora-vigilante.timer (10 em 10 min).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from curadora import RAIZ, carregar_config
from curadora.alertas import SaidaWhatsApp
from curadora.aovivo import epoch
from curadora.banco import CAMINHO_PADRAO

PASTA_BACKUP = RAIZ / "backups"
GUARDAR = 3   # 29/10 (Breno: "Sim, 3 dias"): eram 7 (~1 GB) com a VPS em 88% de disco
TAP = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
PARADA_S = 30 * 60          # mensagens chegando e nenhuma avaliada há 30 min = travou
DISCO_MINIMO = 1.5e9        # bytes livres
ESTADO = RAIZ / "dados" / "vigilante.json"
AVISO_REPETIR_S = 6 * 3600


def backup(banco: Path = CAMINHO_PADRAO, pasta: Path = PASTA_BACKUP, guardar: int = GUARDAR) -> Path:
    """Cópia consistente (API de backup do SQLite) e compactada. Apaga as mais antigas."""
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"curadora-{time.strftime('%Y%m%d-%H%M')}.db.gz"
    temporario = pasta / "copia-em-andamento.db"
    origem = sqlite3.connect(banco)
    copia = sqlite3.connect(temporario)
    try:
        origem.backup(copia)
    finally:
        copia.close()
        origem.close()
    with temporario.open("rb") as entrada, gzip.open(destino, "wb", compresslevel=6) as saida:
        shutil.copyfileobj(entrada, saida)
    temporario.unlink()
    for antiga in sorted(pasta.glob("curadora-*.db.gz"))[:-guardar]:
        antiga.unlink()
    return destino


def _avisar(problemas: list[str], agora: float) -> None:
    """Avisa no grupo de alertas, sem repetir o mesmo problema mais de uma vez a cada 6h."""
    try:
        estado = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}
    except ValueError:
        estado = {}
    novos = [p for p in problemas if agora - estado.get(p, 0) > AVISO_REPETIR_S]
    if not novos:
        return
    saida = SaidaWhatsApp.do_config(carregar_config())
    texto = "⚠️ Curadora: " + " · ".join(novos)
    print(texto, flush=True)
    if saida:
        try:
            saida.notas = {"AVISO"}
            saida(texto, {"nota": "AVISO"})
        except Exception as exc:
            print(f"não consegui avisar no WhatsApp: {exc}", flush=True)
    estado.update({p: agora for p in novos})
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(estado), encoding="utf-8")


def ultima_promo_dos_grupos(tap: Path, grupos: set[str], ler_bytes: int = 3_000_000) -> int:
    """Horário da mensagem com preço mais recente dos grupos de perfume (lendo só o fim do arquivo)."""
    if not tap.exists():
        return 0
    with tap.open("rb") as fh:
        fh.seek(max(0, tap.stat().st_size - ler_bytes))
        linhas = fh.read().decode("utf-8", "replace").splitlines()[1:]
    for linha in reversed(linhas):
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if str(r.get("chatId", "")).split("@")[0] in grupos and not r.get("fromMe") and "R$" in (r.get("body") or ""):
            return epoch(r.get("timestamp"))
    return 0


def verificar(agora: float | None = None, abrir=urllib.request.urlopen, reiniciar=None) -> list[str]:
    agora = agora or time.time()
    problemas: list[str] = []
    try:
        with abrir("http://127.0.0.1:8765/entrar", timeout=10) as r:
            if r.status != 200:
                problemas.append(f"painel respondeu {r.status}")
    except Exception as exc:
        problemas.append(f"painel não responde ({type(exc).__name__})")

    # Promos de perfume chegando, mas nenhuma avaliada há muito tempo = leitura travada.
    # (Compara com a última mensagem dos GRUPOS DE PERFUME: à noite eles ficam quietos e outros grupos não.)
    try:
        con = sqlite3.connect(CAMINHO_PADRAO)
        ultima = con.execute("SELECT MAX(ts) FROM avaliacoes WHERE origem='ao vivo'").fetchone()[0] or 0
        con.close()
        chegou = ultima_promo_dos_grupos(TAP, set(carregar_config()["grupos_fonte"]))
        if chegou and chegou - ultima > PARADA_S and agora - chegou < PARADA_S:
            problemas.append(f"leitura dos grupos parada há {int((chegou - ultima) / 60)} min (reiniciei)")
            (reiniciar or _reiniciar)()
    except sqlite3.Error as exc:
        problemas.append(f"banco com problema ({exc})")

    livre = shutil.disk_usage("/").free
    if livre < DISCO_MINIMO:
        problemas.append(f"disco quase cheio ({livre / 1e9:.1f} GB livres)")
    if problemas:
        _avisar(problemas, agora)
    return problemas


def _reiniciar() -> None:
    subprocess.run(["systemctl", "--user", "restart", "curadora"], check=False, timeout=60)


def main() -> int:
    acao = sys.argv[1] if len(sys.argv) > 1 else ""
    if acao == "backup":
        print(f"cópia salva: {backup()}")
        return 0
    if acao == "verificar":
        problemas = verificar()
        print("tudo certo" if not problemas else "problemas: " + "; ".join(problemas))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
