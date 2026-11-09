"""Alerta por produto (pedido do Breno em 04/10): "me avisa quando o Asad 100ml passar de R$150".

A equipe cadastra no painel o perfume e o preço-alvo. Toda promo avaliada (grupos ao vivo, vigia, radar) é
comparada com a lista: mesmo produto (mesmas palavras da linha; volume, se informado) por até o preço-alvo
→ aviso no grupo, mesmo que a nota não seja Excelente. Não repete o mesmo alvo por 12h, a não ser que o preço
caia mais 3%.
"""
from __future__ import annotations

import json
import sqlite3
import time

ESQUEMA_VIGIADOS = """
CREATE TABLE IF NOT EXISTS vigiados (
    id             INTEGER PRIMARY KEY,
    nome           TEXT NOT NULL,
    tokens         TEXT NOT NULL,     -- palavras da linha (json), como na identidade do produto
    volume         TEXT,              -- "" = qualquer volume
    kit            INTEGER DEFAULT 0,
    preco_alvo     REAL NOT NULL,
    criado_por     TEXT,
    criado_ts      INTEGER,
    ativo          INTEGER DEFAULT 1,
    alerta_ts      INTEGER,
    alerta_preco   REAL
);
"""
NAO_REPETIR_S = 12 * 3600


def adicionar(con: sqlite3.Connection, ident, nome: str, preco_alvo: float, usuario: str = "") -> dict:
    from .parser import valor
    nome = " ".join(str(nome or "").split())[:120]
    preco_alvo = valor(str(preco_alvo or "").replace("R$", "").strip())  # "150", "149,90", "1.024,90"
    if not preco_alvo or preco_alvo <= 0:
        return {"erro": "Preço-alvo inválido."}
    analise = ident.analisar(nome)
    if not analise.tokens:
        return {"erro": "Não reconheci o perfume. Escreva marca e nome, ex.: Lattafa Asad 100ml."}
    cur = con.execute(
        "INSERT INTO vigiados (nome, tokens, volume, kit, preco_alvo, criado_por, criado_ts) VALUES (?,?,?,?,?,?,?)",
        (nome, json.dumps(sorted(analise.tokens)), analise.volume, int(analise.kit), preco_alvo, usuario,
         int(time.time())))
    con.commit()
    return {"ok": True, "id": cur.lastrowid, "nome": nome, "volume": analise.volume, "preco_alvo": preco_alvo}


def remover(con: sqlite3.Connection, id_: int) -> dict:
    con.execute("UPDATE vigiados SET ativo = 0 WHERE id = ?", (int(id_),))
    con.commit()
    return {"ok": True}


def ativos(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM vigiados WHERE ativo = 1 ORDER BY criado_ts DESC").fetchall()


def casa(v, tokens: frozenset, volume: str, kit: bool) -> bool:
    return (set(json.loads(v["tokens"])) == set(tokens) and bool(v["kit"]) == bool(kit)
            and (not v["volume"] or v["volume"] == volume))


def alvos_atingidos(con: sqlite3.Connection, av, agora: int) -> list[sqlite3.Row]:
    """Alvos que esta avaliação atinge e que ainda não foram avisados (ou caíram mais 3% desde o aviso)."""
    if av is None or getattr(av, "conflito", ""):
        return []
    ident = av.identidade
    saida = []
    for v in ativos(con):
        if not casa(v, ident.tokens, ident.volume, ident.kit) or av.preco_efetivo > v["preco_alvo"]:
            continue
        if v["alerta_ts"] and agora - v["alerta_ts"] < NAO_REPETIR_S and av.preco_efetivo > v["alerta_preco"] * 0.97:
            continue
        saida.append(v)
    return saida


def marcar_avisado(con: sqlite3.Connection, v, agora: int, preco: float) -> None:
    con.execute("UPDATE vigiados SET alerta_ts = ?, alerta_preco = ? WHERE id = ?", (agora, preco, v["id"]))
    con.commit()


def formatar(av, v) -> str:
    p, h = av.promo, av.historico
    preco = f"R${av.preco_efetivo:.2f}".replace(".", ",")
    alvo = f"R${v['preco_alvo']:.2f}".replace(".", ",")
    linhas = ["🎯 CURADORA · ALVO ATINGIDO", f"*{p.produto[:80]}*", f"💰 {preco} (alvo {alvo})"]
    if av.pct_abaixo is not None and h.mediana:
        linhas.append(f"📊 {av.pct_abaixo:.0f}% abaixo do normal (R${h.mediana:.0f}) · nota {av.score}")
    if p.cupons:
        linhas.append("🎟️ cupom: " + " ou ".join(p.cupons[:2]))
    if av.risco:
        linhas.append(f"⚠️ risco: {av.risco_motivo}")
    if p.link:
        linhas.append(f"🔗 {p.link}")
    if v["criado_por"]:
        linhas.append(f"(alerta pedido por {v['criado_por']})")
    return "\n".join(linhas)
