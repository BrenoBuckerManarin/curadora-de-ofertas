"""👎 numa foto: ela sai do rodízio da normalizadora.

17/10 (Breno, sobre tirar do rodízio a foto que ele troca na Edição: "não faça, porque tem vez que troco foto só por
trocar; só se eu puser o emoji de negativo, o joinha ao contrário"). Um 👎 num post do Normalizador Promo ou da Edição
tira a foto daquele post do rodízio: o arquivo vai de approved_final/ para rejected/ na mesma pasta do banco (a
normalizadora não usa o que está em rejected/), e toda cópia da mesma foto (mesmo conteúdo) no banco vai junto. Nada é
apagado: tirar o 👎 devolve a foto. Registro em dados/fotos_rejeitadas.jsonl.

De onde vem a foto do post:
- Normalizador Promo: quem posta é a normalizadora, pela ponte (o que a ponte manda não aparece no tap). O log dela
  traz, para cada post, a linha "[normalizer_report] ... image_path=..." e logo depois a resposta da ponte com o
  "messageId" do post: é por ele que o 👎 acha a foto.
- Edição: a mensagem que o Breno encaminhou está no tap, com a foto (mediaPath).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import RAIZ

NORMALIZADOR = "120363000000000012"
EDICAO = "120363000000000001"
GRUPOS = {NORMALIZADOR: "Normalizador Promo", EDICAO: "Edição"}
BANCO = Path(os.path.expanduser("~/.hermes/perfume_image_bank/products"))
LOG_NORMALIZADORA = Path(os.path.expanduser("~/.hermes/run/perfume_manual_normalizer_15s.log"))
REGISTRO = RAIZ / "dados" / "fotos_rejeitadas.jsonl"
TIERS = ("approved_final", "high_quality", "acceptable")
IMG = {".jpg", ".jpeg", ".png", ".webp"}
REACAO = re.compile(r"\[Reaction:\s*(.*?)\s+to\s+(\S+?)\]")
FOTO_DO_RELATORIO = re.compile(r"image_path=(\S+)")
TAP_LIDO = 80_000_000      # a mensagem reagida na Edição é procurada nos últimos 80 MB do tap


def _reacao(r: dict) -> tuple[str, str]:
    """(emoji, id da mensagem reagida). Emoji vazio = a reação foi tirada."""
    m = REACAO.search(str(r.get("body") or ""))
    return (m.group(1).strip(), m.group(2)) if m else ("", "")


def _foto_no_log_da_normalizadora(alvo: str, log: Path) -> Path | None:
    if not log.exists():
        return None
    foto = None
    with log.open(encoding="utf-8", errors="replace") as fh:
        for linha in fh:
            if linha.startswith("[normalizer_report]"):
                m = FOTO_DO_RELATORIO.search(linha)
                foto = Path(m.group(1)) if m else None
            elif alvo in linha and linha.startswith("{"):
                try:
                    r = json.loads(linha)
                except ValueError:
                    continue
                if alvo == r.get("messageId") or alvo in (r.get("messageIds") or []):
                    return foto
    return None


def _foto_no_tap(alvo: str, tap: Path) -> Path | None:
    if not tap or not tap.exists():
        return None
    marca = alvo.encode()
    with tap.open("rb") as fh:
        fh.seek(max(0, tap.stat().st_size - TAP_LIDO))
        for bruto in fh:
            if marca not in bruto or b"mediaPath" not in bruto:
                continue
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if r.get("messageId") == alvo and r.get("mediaPath"):
                return Path(r["mediaPath"])
    return None


def foto_do_post(grupo: str, alvo: str, tap: Path | None) -> Path | None:
    if grupo == NORMALIZADOR:
        return _foto_no_log_da_normalizadora(alvo, LOG_NORMALIZADORA)
    if grupo == EDICAO:
        return _foto_no_tap(alvo, tap) if tap else None
    return None


def _digest(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def copias_no_banco(foto: Path, banco: Path | None = None) -> list[Path]:
    """As fotos do banco (aprovadas) com o mesmo conteúdo de `foto`. Só calcula o hash das do mesmo tamanho."""
    banco = banco or BANCO
    if not foto.exists():
        return []
    tamanho, alvo = foto.stat().st_size, _digest(foto)
    achadas = []
    for pasta in banco.iterdir() if banco.is_dir() else []:
        for tier in TIERS:
            d = pasta / tier
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.suffix.lower() in IMG and p.is_file() and p.stat().st_size == tamanho and _digest(p) == alvo:
                    achadas.append(p)
    return achadas


def _registrar(dados: dict) -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRO.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(dados, ts=int(time.time())), ensure_ascii=False) + "\n")


def _registros() -> list[dict]:
    if not REGISTRO.exists():
        return []
    saida = []
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines():
        try:
            saida.append(json.loads(linha))
        except ValueError:
            continue
    return saida


def retirar(fotos: list[Path]) -> list[list[str]]:
    movidas = []
    for p in fotos:
        destino = p.parent.parent / "rejected" / p.name
        destino.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not destino.exists():
            shutil.move(str(p), str(destino))
            movidas.append([str(p), str(destino)])
    return movidas


def devolver(movidas: list[list[str]]) -> int:
    n = 0
    for origem, destino in movidas:
        if Path(destino).exists() and not Path(origem).exists():
            Path(origem).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(destino, origem)
            n += 1
    return n


def reacao(r: dict, tap: Path | None = None) -> dict | None:
    """Trata uma reação vinda do tap. 👎 num post do Normalizador/Edição tira a foto dele do rodízio; tirar o 👎
    devolve. Qualquer outro emoji, ou outro grupo: nada."""
    grupo = str(r.get("chatId") or "").split("@")[0]
    if grupo not in GRUPOS:
        return None
    emoji, alvo = _reacao(r)
    if not alvo:
        return None
    quem = str(r.get("senderName") or r.get("senderId") or "")
    if emoji.startswith("👎"):
        if any(x.get("acao") == "retirada" and x.get("alvo") == alvo for x in _registros()):
            return None   # o mesmo 👎 chegou de novo
        foto = foto_do_post(grupo, alvo, tap)
        if not foto or not foto.exists():
            dados = {"acao": "sem_foto", "grupo": GRUPOS[grupo], "alvo": alvo, "quem": quem}
            _registrar(dados)
            print(f"[fotos] joinha negativo de {quem} no {GRUPOS[grupo]}: não achei a foto desse post", flush=True)
            return dados
        movidas = retirar(copias_no_banco(foto))
        dados = {"acao": "retirada", "grupo": GRUPOS[grupo], "alvo": alvo, "quem": quem, "foto": str(foto),
                 "movidas": movidas}
        _registrar(dados)
        print(f"[fotos] joinha negativo de {quem} no {GRUPOS[grupo]}: {len(movidas)} cópia(s) da foto fora do rodízio",
              flush=True)
        return dados
    if not emoji:
        retiradas = [x for x in _registros() if x.get("acao") == "retirada" and x.get("alvo") == alvo]
        ja = any(x.get("acao") == "devolvida" and x.get("alvo") == alvo for x in _registros())
        if retiradas and not ja:
            n = devolver(retiradas[-1].get("movidas") or [])
            dados = {"acao": "devolvida", "grupo": GRUPOS[grupo], "alvo": alvo, "quem": quem, "fotos": n}
            _registrar(dados)
            print(f"[fotos] joinha negativo tirado por {quem}: {n} foto(s) de volta ao rodízio", flush=True)
            return dados
    return None
