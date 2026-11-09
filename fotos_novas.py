"""Coleta automática de fotos de produto dos grupos de promo para o banco da normalizadora.

17/10 (Breno: "temos que ver o que fazer pra ter mais fotos, melhores opções"; sobre os limites: "sim, pode fazer com
esses limites; na verdade aumente o limite pra 5"). A ponte só guarda as fotos dos grupos por ~1 dia, por isso a coleta
roda a cada 30 min (timer curadora-fotos-novas) e olha os posts novos desde a última volta.

Uma foto entra quando:
- o produto (título do anúncio no ML, da avaliação que a curadora fez do mesmo post) tem MENOS de 5 fotos no banco;
- tem 1000px+ no menor lado e é quase quadrada;
- não é a mesma imagem de uma foto que o produto já tem (comparação pela imagem, não pelo arquivo);
- o gênero do post não é o outro (e, nas linhas que existem nas duas versões, o post diz o mesmo gênero);
- a leitura do texto da imagem não acha preço, porcentagem, cupom, frete, @, site, loja ou perfumaria;
- e no máximo 10 fotos por dia no total.
Ela vai para <pasta do produto>/acceptable/ (não é aprovada pelo Breno: o post sai com ⚠️ "imagem curada sem status
approved_final"). Se o Breno postar a foto, ela vira aprovada pelo minerador da Edição; se der 👎, sai do rodízio.
Registro em dados/fotos_novas.jsonl. Uso: python3 fotos_novas.py [--simular]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
TAP = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
BANCO_DB = RAIZ / "dados" / "curadora.db"
REGISTRO = RAIZ / "dados" / "fotos_novas.jsonl"
ESTADO = RAIZ / "dados" / "fotos_novas.estado.json"
LINHAS = Path(os.path.expanduser("~/.hermes/perfume_image_bank/linhas_dois_generos.json"))
NOSSOS_GRUPOS = {"120363000000000013", "120363000000000012", "120363000000000001", "120363000000000002",
                 "120363000000000014"}   # Gerador, Normalizador, Edição, GrupoFinal, Vitrine da Equipe

LIMITE_FOTOS = 5
MAX_POR_DIA = 10
MIN_LADO = 1000
MAX_PROPORCAO = 1.25
MESMA_IMAGEM = 6          # diferença máxima (de 64) entre os dhash de duas fotos para serem a mesma imagem
PRIMEIRA_VOLTA_S = 3 * 3600
PROMO_NA_FOTO = re.compile(r"(?i)r\$|\d\s*%|\boff\b|cupom|frete|@|www|\.com|\bloja\b|perfumaria|desconto|\bpix\b")
MASC = {"masculino", "masculina", "man", "men", "homme", "him", "homem"}
FEM = {"feminino", "feminina", "woman", "women", "femme", "her", "mulher"}


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "").lower()) if not unicodedata.combining(c))


def genero(texto: str) -> set[str]:
    palavras = set(re.findall(r"[a-z]+", sem_acento(texto)))
    return ({"m"} if palavras & MASC else set()) | ({"f"} if palavras & FEM else set())


def _linhas() -> tuple[set, set]:
    try:
        d = json.loads(LINHAS.read_text(encoding="utf-8"))
        return set(d.get("fora") or []), set(d.get("familias") or [])
    except (OSError, ValueError):
        return set(), set()


def familia(nome: str, fora: set) -> str:
    toks = {t for t in re.findall(r"[a-z0-9]+", sem_acento(nome)) if t not in fora and len(t) > 1
            and not re.fullmatch(r"\d+(?:ml)?", t)}
    return "-".join(sorted(toks))


def dhash(caminho: Path) -> int | None:
    from PIL import Image
    try:
        with Image.open(caminho) as im:
            g = im.convert("L").resize((9, 8))
            px = list(g.get_flattened_data() if hasattr(g, "get_flattened_data") else g.getdata())
    except Exception:
        return None
    bits = 0
    for linha in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[linha * 9 + col] > px[linha * 9 + col + 1])
    return bits


def distancia(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def tamanho(caminho: Path) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(caminho) as im:
            return im.size
    except Exception:
        return None


def texto_da_foto(caminho: Path) -> str:
    try:
        return subprocess.run(["/usr/bin/tesseract", str(caminho), "stdout", "-l", "por+eng", "--psm", "11"],
                              text=True, capture_output=True, timeout=40).stdout or ""
    except Exception:
        return ""


class BancoDaNormalizadora:
    """O que a coleta precisa da normalizadora: as fotos que um produto já tem e a pasta onde gravar."""

    def __init__(self):
        sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
        import perfume_manual_normalizer as n   # noqa: E402
        self.n = n

    def fotos(self, titulo: str) -> list[Path]:
        n = self.n
        pastas = n.approved_final_candidates(titulo)
        return [p for d in pastas if d.is_dir() for p in d.iterdir()
                if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and n.bank_asset_identity_allowed(titulo, p, d.parent)]

    def pasta(self, titulo: str) -> Path | None:
        chave = self.n.resolve_bank_write_key(titulo)
        if not chave:
            return None
        pasta = self.n.IMAGE_BANK_PRODUCTS / chave
        for _ in range(3):   # pasta migrada: segue o MIGRATED_TO.json
            marca = pasta / "MIGRATED_TO.json"
            if not marca.exists():
                return pasta
            try:
                pasta = pasta.parent / json.loads(marca.read_text(encoding="utf-8"))["migrated_to"]
            except (OSError, ValueError, KeyError):
                return None
        return None


def _registros_de_hoje() -> int:
    if not REGISTRO.exists():
        return 0
    hoje = time.strftime("%Y-%m-%d")
    n = 0
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        n += r.get("acao") == "guardada" and time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0))) == hoje
    return n


def _registrar(dados: dict) -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRO.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(dados, ts=int(time.time())), ensure_ascii=False) + "\n")


def _epoch(t) -> float:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return 0.0
    return t / 1000 if t > 1e11 else t


def posts_novos(tap: Path, posicao: int) -> tuple[list[dict], int]:
    """Posts de grupo (não os nossos) com foto e link do ML, desde `posicao` (na primeira volta, as últimas 3h)."""
    if not tap.exists():
        return [], posicao
    fim = tap.stat().st_size
    no_meio = False
    if posicao <= 0 or posicao > fim:
        posicao = max(0, fim - 60_000_000)
        desde = time.time() - PRIMEIRA_VOLTA_S
        no_meio = posicao > 0
    else:
        desde = 0
    saida = []
    with tap.open("rb") as f:
        f.seek(posicao)
        if no_meio:
            f.readline()   # caiu no meio de uma linha: descarta o pedaço (retomando da última volta, não)
        for bruto in f:
            if b"mediaPath" not in bruto or b"meli.la" not in bruto:
                continue
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if (not r.get("mediaPath") or str(r.get("chatId") or "").split("@")[0] in NOSSOS_GRUPOS
                    or _epoch(r.get("timestamp")) < desde):
                continue
            saida.append(r)
        posicao = f.tell()
    return saida, posicao


def avaliacao_do_post(con, post: dict) -> dict | None:
    ts = _epoch(post.get("timestamp"))
    for cod in re.findall(r"meli\.la/([A-Za-z0-9]+)", post.get("body") or ""):
        row = con.execute("SELECT dados FROM avaliacoes WHERE origem = 'ao vivo' AND ts BETWEEN ? AND ? AND dados LIKE ? "
                          "ORDER BY ts DESC LIMIT 1", (ts - 900, ts + 3600, f"%meli.la/{cod}%")).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except ValueError:
                return None
    return None


def rodar(banco, simular: bool = False, tap: Path = TAP, con=None) -> dict:
    con = con or sqlite3.connect(f"file:{BANCO_DB}?mode=ro", uri=True, timeout=30)
    estado = {}
    if ESTADO.exists():
        try:
            estado = json.loads(ESTADO.read_text(encoding="utf-8"))
        except ValueError:
            estado = {}
    posts, posicao = posts_novos(tap, int(estado.get("posicao") or 0))
    fora, duas = _linhas()
    feitas = _registros_de_hoje()
    contagem = {"posts": len(posts), "guardadas": 0}
    vistas: list[int] = []   # dhash das fotos guardadas nesta volta
    for post in posts:
        if feitas >= MAX_POR_DIA:
            contagem["teto do dia"] = contagem.get("teto do dia", 0) + 1
            break
        motivo = None
        foto = Path(post["mediaPath"])
        dados = avaliacao_do_post(con, post)
        titulo = str(((dados or {}).get("ml") or {}).get("titulo") or (dados or {}).get("produto") or "")
        if not dados or not titulo or dados.get("nota") == "FORA":
            motivo = "sem avaliação do produto"
        elif not foto.exists() or not (tam := tamanho(foto)):
            motivo = "foto não abre"
        elif min(tam) < MIN_LADO:
            motivo = "menor que 1000px"
        elif max(tam) > MAX_PROPORCAO * min(tam):
            motivo = "não é quadrada"
        else:
            g_prod, g_post = genero(titulo), genero(post.get("body") or "")
            if g_prod and g_post - g_prod:
                motivo = "outro gênero"
            elif g_prod and familia(titulo, fora) in duas and not (g_post & g_prod):
                motivo = "linha nas duas versões e o post não diz o gênero"
        if not motivo:
            existentes = banco.fotos(titulo)
            if len(existentes) >= LIMITE_FOTOS:
                motivo = f"já tem {LIMITE_FOTOS}+ fotos"
            else:
                h = dhash(foto)
                ja = [x for x in (dhash(p) for p in existentes) if x is not None] + vistas
                if h is None or any(distancia(h, x) <= MESMA_IMAGEM for x in ja):
                    motivo = "mesma imagem que já tem"
                elif PROMO_NA_FOTO.search(texto_da_foto(foto)):
                    motivo = "texto de promo na foto"
        if motivo:
            contagem[motivo] = contagem.get(motivo, 0) + 1
            continue
        pasta = banco.pasta(titulo)
        if pasta is None:
            contagem["sem pasta no banco"] = contagem.get("sem pasta no banco", 0) + 1
            continue
        sha = hashlib.sha256(foto.read_bytes()).hexdigest()
        destino = pasta / "acceptable" / f"curadora_grupo_{int(_epoch(post.get('timestamp')))}_{sha[:12]}{foto.suffix.lower() or '.jpg'}"
        vistas.append(dhash(foto) or 0)
        feitas += 1
        contagem["guardadas"] += 1
        if simular:
            print(f"   [simulação] guardaria: {titulo[:60]} -> {destino.parent.parent.name}")
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(foto, destino)
        _guardar_origem(pasta, destino, sha, titulo, post.get("body") or "")
        _registrar({"acao": "guardada", "produto": titulo, "arquivo": str(destino), "grupo": post.get("chatId"),
                    "mensagem": post.get("messageId")})
    if not simular:
        ESTADO.write_text(json.dumps({"posicao": posicao}), encoding="utf-8")
    return contagem


def _guardar_origem(pasta: Path, destino: Path, sha: str, titulo: str, corpo: str) -> None:
    """A origem da foto no index.json (raw_items: não é aprovada), para a trava de gênero por foto."""
    idx = pasta / "index.json"
    dados = {"product_key": pasta.name, "canonical_guess": "", "approved": [], "raw_items": [], "updated_at": None}
    if idx.exists():
        try:
            dados.update(json.loads(idx.read_text(encoding="utf-8")))
        except ValueError:
            return
    itens = [e for e in (dados.get("raw_items") or []) if isinstance(e, dict)]
    itens.append({"media_path": str(destino), "body": corpo[:600], "product_guess": titulo,
                  "source_group_label": "curadora-grupo", "sha256": sha})
    dados["raw_items"] = itens[-300:]
    dados["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = pasta / "index.json.tmp-fotos-novas"
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, idx)


if __name__ == "__main__":
    c = rodar(BancoDaNormalizadora(), simular="--simular" in sys.argv)
    print(time.strftime("%H:%M"), "fotos novas:", c, flush=True)
