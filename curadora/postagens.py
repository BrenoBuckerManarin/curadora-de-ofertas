"""O que a equipe postou no grupo dela (Grupo da Equipe): a resposta certa para medir a curadora.

Pedido do Breno em 04/10: o bot (Bender) entrou no grupo, então o tap passou a ver as postagens. Para cada uma:
  - marca como "postada" as avaliações do mesmo produto e preço (até 5%) das últimas 48h (some do "a postar");
  - guarda a nota que a curadora daria a ela e a melhor nota que a curadora já tinha dado antes do post.
Com isso dá para medir quanto do que a equipe posta a curadora já tinha apontado (e quanto ela deixa passar),
que é o caminho para a curadora substituir a coletora.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .parser import ler_promo, parece_perfume
from .score import para_dict

ESQUEMA_POSTAGENS = """
CREATE TABLE IF NOT EXISTS postagens (
    id          INTEGER PRIMARY KEY,
    msg_id      TEXT UNIQUE,
    ts          INTEGER NOT NULL,
    grupo       TEXT,
    produto     TEXT,
    chave       TEXT,
    preco       REAL,
    link        TEXT,
    nota        TEXT,      -- nota que a curadora dá ao post, no momento do post
    score       INTEGER,
    nota_antes  TEXT,      -- melhor nota que a curadora já tinha dado a esse produto/preço nas 48h antes
    score_antes INTEGER,
    avaliacoes  INTEGER,   -- quantas avaliações foram marcadas como postadas
    dados       TEXT
);
CREATE INDEX IF NOT EXISTS ix_postagens_ts ON postagens(ts);
-- "Pular" na sugestão do painel: o produto sai da sugestão por 2h (continua na lista normal).
CREATE TABLE IF NOT EXISTS pulos (chave TEXT NOT NULL, ts INTEGER NOT NULL, usuario TEXT);
"""
JANELA_S = 48 * 3600
MESMO_PRECO = 0.05


ID_ML = re.compile(r"MLB-?(\d{6,})")
REPETIR_APOS_S = 8 * 3600   # produto postado (ou mandado pelo pipeline) só volta depois de 8h...
MAIS_BARATO = 0.08          # ...ou antes, se estiver 8% ou mais abaixo do preço postado (Breno, 10/10 à noite)
# 21/10 (Breno: "sim pros 2"): o que a EQUIPE postou no GrupoFinal (o pipeline não tinha mandado) volta depois de 6h; o que
# veio do pipeline continua com 8h. A equipe posta de dia e o público da noite é outro: no dia 20, Kaiak Classic, Club
# de Nuit Intense e Eros Flame, postados pela equipe às 14h46–16h09, ficaram travados como Excelentes na rodada da noite.
REPETIR_EQUIPE_S = 6 * 3600
VEIO_DO_PIPELINE_S = 12 * 3600   # post no GrupoFinal até 12h depois de o pipeline mandar o mesmo produto = veio dele


def enviados_pelo_pipeline(registro: Path | None, desde: float) -> dict[str, list[int]]:
    """Por produto, as horas em que o pipeline mandou (dados/pipeline.jsonl)."""
    saida: dict[str, list[int]] = {}
    if registro is None or not registro.exists():
        return saida
    for linha in registro.read_text(encoding="utf-8").splitlines()[-600:]:
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "enviado" and r.get("chave") and r.get("ts", 0) >= desde:
            saida.setdefault(r["chave"], []).append(int(r["ts"]))
    return saida


def post_da_equipe(chave: str | None, ts: float | None, enviados: dict[str, list[int]]) -> bool:
    """O post no GrupoFinal foi da equipe (o pipeline não tinha mandado o produto nas 12h antes)?"""
    return bool(ts) and not any(0 <= ts - t <= VEIO_DO_PIPELINE_S for t in enviados.get(chave or "", ()))


def ultimos_posts(con: sqlite3.Connection, agora: float, registro: Path | None = None) -> dict[str, tuple[float, int]]:
    """Por produto: (menor preço, hora mais recente) do que foi postado no GrupoFinal nas últimas 8h. Com `registro`
    (dados/pipeline.jsonl) entram também as promos que o pipeline mandou e ainda estão na mão do Breno."""
    saida: dict[str, tuple[float, int]] = {}

    def junta(chave, preco, ts):
        if chave and preco:
            atual = saida.get(chave)
            saida[chave] = (min(preco, atual[0]), max(ts, atual[1])) if atual else (preco, ts)

    enviados = enviados_pelo_pipeline(registro, agora - REPETIR_APOS_S - VEIO_DO_PIPELINE_S)
    for chave, preco, ts in con.execute("SELECT chave, preco, ts FROM postagens WHERE ts >= ? AND ts <= ?",
                                        (agora - REPETIR_APOS_S, agora)):
        if agora - ts > REPETIR_EQUIPE_S and post_da_equipe(chave, ts, enviados):
            continue   # 21/10: post da equipe há mais de 6h já pode voltar
        junta(chave, preco, ts)
    if registro is not None and registro.exists():
        for linha in registro.read_text(encoding="utf-8").splitlines()[-400:]:
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            if r.get("status") == "enviado" and agora - REPETIR_APOS_S <= r.get("ts", 0) <= agora:
                junta(r.get("chave"), r.get("preco"), int(r["ts"]))
    return saida


def ainda_vale_o_post(ultimo: tuple[float, int] | None, preco: float | None) -> bool:
    """True = o produto já foi há menos de 8h e o preço de agora NÃO está 8% abaixo do que foi postado."""
    return bool(ultimo) and not (preco and preco <= ultimo[0] * (1 - MAIS_BARATO))


def ids_do_anuncio(card) -> set[str]:
    """Os números do anúncio no ML (item, produto de catálogo e o que vem na URL)."""
    if not (card and card.ok):
        return set()
    ids = {re.sub(r"\D", "", str(x)) for x in (card.item_id, card.produto_id) if x}
    return {i for i in ids if i} | set(ID_ML.findall(card.url_produto or ""))


def marcar_mesmo_anuncio(con: sqlite3.Connection, ids: set[str], preco: float, ts: int, por: str) -> int:
    """Marca como postados os cards do MESMO anúncio do ML que estão com outro nome.

    10/10: o Bareeq Al Dhahab foi postado às 16:39 e o card "Bareeq" (mesmo anúncio, nome mais curto num grupo)
    continuou no painel como Excelente não postado. O pipeline já não mandava de novo; o painel não sabia.
    """
    if not ids or not preco:
        return 0
    iguais = []
    for r in con.execute("SELECT id, json_extract(dados, '$.preco') AS preco, json_extract(dados, '$.ml.url') AS url, "
                         "json_extract(dados, '$.reconferida_de') AS ref "
                         "FROM avaliacoes WHERE ts BETWEEN ? AND ? AND nota != 'FORA' AND COALESCE(postada, 0) = 0 "
                         "AND dados LIKE '%MLB%'", (ts - JANELA_S, ts + 600)):
        if (r["url"] and r["preco"] and ids & set(ID_ML.findall(r["url"]))
                and abs(r["preco"] - preco) <= MESMO_PRECO * max(preco, r["preco"])):
            iguais.append((ts, por, r["id"]))
            if r["ref"]:
                # reconferência: é o preço de AGORA do card; quem diz se o card está postado é a avaliação
                # original dele (o card do Bareeq nasceu a R$110 e estava a R$118, o preço do post)
                iguais.append((ts, por, int(r["ref"])))
    con.executemany("UPDATE avaliacoes SET postada = ?, postada_por = ? WHERE id = ? AND COALESCE(postada, 0) = 0", iguais)
    return len(iguais)


def registrar_postagem(con: sqlite3.Connection, curadora, texto: str, ts: int, msg_id: str, grupo: str,
                       conferir_ml: bool = True) -> dict | None:
    promo = ler_promo(texto, curadora.cfg["marcas"])
    if not promo.valida or not parece_perfume(promo.produto, texto):
        return None
    # Conferido no ML na hora do post, como as escolhas da curadora (comparação justa no experimento, 05/10).
    av = curadora.avaliar(promo, agora=ts, conferir_ml=conferir_ml)
    if av is None:
        return None
    chave, preco = av.identidade.chave, av.preco_efetivo
    linhas = con.execute(
        "SELECT id, nota, score, json_extract(dados, '$.preco') AS preco FROM avaliacoes "
        "WHERE chave = ? AND ts BETWEEN ? AND ? AND nota != 'FORA'", (chave, ts - JANELA_S, ts + 600)).fetchall()
    casadas = [r for r in linhas if r["preco"] and abs(r["preco"] - preco) <= MESMO_PRECO * max(preco, r["preco"])]
    melhor = max(casadas, key=lambda r: r["score"], default=None)
    if casadas:
        con.executemany("UPDATE avaliacoes SET postada = ?, postada_por = ? WHERE id = ? AND (postada IS NULL OR postada = 0)",
                        [(ts, grupo, r["id"]) for r in casadas])
    marcar_mesmo_anuncio(con, ids_do_anuncio(av.card), preco, ts, grupo)
    con.execute(
        "INSERT OR IGNORE INTO postagens (msg_id, ts, grupo, produto, chave, preco, link, nota, score, nota_antes, "
        "score_antes, avaliacoes, dados) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (msg_id, ts, grupo, promo.produto, chave, preco, promo.link, av.nota, av.score,
         melhor["nota"] if melhor else None, melhor["score"] if melhor else None, len(casadas),
         json.dumps(para_dict(av), ensure_ascii=False)))
    con.commit()
    return {"produto": promo.produto, "chave": chave, "preco": preco, "nota": av.nota,
            "nota_antes": melhor["nota"] if melhor else None, "marcadas": len(casadas)}


def resumo(con: sqlite3.Connection, desde_ts: int) -> dict:
    """Quanto do que a equipe postou a curadora já tinha apontado antes (BOA ou IMPERDÍVEL)."""
    linhas = con.execute("SELECT nota, nota_antes FROM postagens WHERE ts >= ?", (desde_ts,)).fetchall()
    boas = {"IMPERDÍVEL", "BOA"}
    return {
        "postadas": len(linhas),
        "curadora_ja_tinha_visto": sum(1 for r in linhas if r["nota_antes"]),
        "curadora_apontou_antes": sum(1 for r in linhas if r["nota_antes"] in boas),
        "por_nota_antes": {n: sum(1 for r in linhas if (r["nota_antes"] or "não vista") == n)
                           for n in ("IMPERDÍVEL", "BOA", "OK", "NÃO VALE", "não vista")},
    }
