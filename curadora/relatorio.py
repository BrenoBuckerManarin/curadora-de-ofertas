"""Relatório semanal da curadora (pedido do Breno em 04/10), para o grupo da equipe.

Seções: resumo, melhores promos da semana, o que mais caiu/subiu de preço, top vendidos, Excelentes que
passaram sem post no grupo da equipe e cupons mais vistos. Texto pronto para WhatsApp (*negrito*).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import Counter

from . import fila
from .identidade import Identificador

SEMANA_S = 7 * 86400
ORIGENS_PROMO = ("ao vivo", "vigia do ML", "radar do ML", "reconferida")


def _brl(v: float) -> str:
    return "R$" + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _data(ts: float) -> str:
    return time.strftime("%d/%m", time.localtime(ts))


def melhores_da_semana(con: sqlite3.Connection, ini: float, fim: float, n: int = 5) -> list[dict]:
    """A melhor avaliação de cada produto na semana (Excelente/Boa), as n de maior nota."""
    marcas = ",".join("?" * len(ORIGENS_PROMO))
    linhas = con.execute(
        f"SELECT chave, ts, nota, score, dados FROM avaliacoes WHERE ts >= ? AND ts < ? AND origem IN ({marcas}) "
        "AND nota IN ('IMPERDÍVEL', 'BOA') AND chave != '' ORDER BY score DESC, ts DESC",
        (ini, fim, *ORIGENS_PROMO)).fetchall()
    vistos, saida = set(), []
    for r in linhas:
        if r["chave"] in vistos:
            continue
        vistos.add(r["chave"])
        d = json.loads(r["dados"])
        saida.append({"chave": r["chave"], "ts": r["ts"], "nota": r["nota"], "score": r["score"],
                      "produto": d.get("produto", ""), "preco": d.get("preco"), "pct": d.get("pct_abaixo")})
        if len(saida) >= n:
            break
    return saida


def variacoes(con: sqlite3.Connection, ini: float, fim: float, minimo: int = 5) -> list[dict]:
    """Mediana da semana contra a mediana dos 30 dias anteriores, por produto (só com dados suficientes)."""
    semana, antes, nome = {}, {}, {}
    for r in con.execute("SELECT chave, ts, preco, produto FROM ofertas WHERE ts >= ? AND ts < ? AND chave != ''",
                         (ini - 30 * 86400, fim)):
        if r["chave"].startswith("|"):
            continue
        (semana if r["ts"] >= ini else antes).setdefault(r["chave"], []).append(r["preco"])
        if r["ts"] >= ini:
            nome[r["chave"]] = r["produto"]
    saida = []
    for chave, precos in semana.items():
        anteriores = antes.get(chave, [])
        if len(precos) >= minimo and len(anteriores) >= minimo:
            m_sem, m_ant = statistics.median(precos), statistics.median(anteriores)
            saida.append({"chave": chave, "produto": nome[chave], "agora": m_sem, "antes": m_ant,
                          "pct": (m_sem - m_ant) / m_ant * 100, "n": len(precos)})
    return sorted(saida, key=lambda v: v["pct"])


def top_vendidos(con: sqlite3.Connection, cfg: dict, ini: float, fim: float) -> list[dict]:
    """Menor preço da semana de cada perfume da lista de top vendidos (volume mais comum), contra a mediana."""
    ident = Identificador(cfg["marcas"], cfg.get("sinonimos"))
    saida = []
    for nome in cfg.get("top_vendidos", []):
        base = ident.analisar(nome).base
        if not base:
            continue
        por_chave: dict[str, list] = {}
        for r in con.execute("SELECT chave, ts, preco FROM ofertas WHERE ts >= ? AND ts < ? AND chave LIKE ?",
                             (ini, fim, base + "|%")):
            por_chave.setdefault(r["chave"], []).append((r["preco"], r["ts"]))
        if not por_chave:
            continue
        chave, obs = max(por_chave.items(), key=lambda kv: len(kv[1]))
        menor, quando = min(obs)
        saida.append({"nome": nome, "volume": chave.split("|")[1], "menor": menor, "quando": quando,
                      "mediana": statistics.median(p for p, _ in obs), "n": len(obs)})
    return saida


def passaram_sem_post(con: sqlite3.Connection, ini: float, fim: float, n: int = 5) -> tuple[list[dict], float | None]:
    """Excelentes da semana sem post do mesmo produto no grupo da equipe (desde que a curadora lê o grupo)."""
    desde = con.execute("SELECT min(ts) FROM postagens").fetchone()[0]
    if desde is None:
        return [], None
    postados = {r[0] for r in con.execute("SELECT chave FROM postagens WHERE ts >= ?", (max(ini, desde) - 86400,))}
    # Primeira vez de cada produto como Excelente na semana: o que já era excelente antes de a curadora ler o
    # grupo pode ter sido postado sem ela ver (não dá para afirmar que "passou batido").
    primeira = {r[0]: r[1] for r in con.execute(
        "SELECT chave, min(ts) FROM avaliacoes WHERE ts >= ? AND ts < ? AND nota = 'IMPERDÍVEL' GROUP BY chave",
        (ini, fim))}
    exc = [m for m in melhores_da_semana(con, max(ini, desde), fim, 200)
           if m["nota"] == "IMPERDÍVEL" and m["chave"] not in postados and primeira.get(m["chave"], 0) >= desde]
    return exc[:n], desde


def montar(con: sqlite3.Connection, cfg: dict, agora: float | None = None) -> str:
    fim = agora or time.time()
    ini = fim - SEMANA_S
    linhas = [f"🧠 *CURADORA · Relatório da semana* ({_data(ini)} a {_data(fim)})", ""]

    # Conta PERFUMES (não avaliações: a vigia reconfere o mesmo produto o dia todo).
    melhor_nota: dict[str, int] = {}
    ordem = {"NÃO VALE": 0, "OK": 1, "BOA": 2, "IMPERDÍVEL": 3}
    for r in con.execute(
            f"SELECT chave, nota FROM avaliacoes WHERE ts >= ? AND ts < ? AND chave != '' "
            f"AND origem IN ({','.join('?' * len(ORIGENS_PROMO))})", (ini, fim, *ORIGENS_PROMO)):
        melhor_nota[r["chave"]] = max(melhor_nota.get(r["chave"], 0), ordem.get(r["nota"], 0))
    exc = sum(1 for v in melhor_nota.values() if v == 3)
    boas = sum(1 for v in melhor_nota.values() if v == 2)
    lidas = con.execute("SELECT count(*) FROM ofertas WHERE ts >= ? AND ts < ?", (ini, fim)).fetchone()[0]
    produtos = con.execute("SELECT count(DISTINCT chave) FROM ofertas WHERE ts >= ? AND ts < ?", (ini, fim)).fetchone()[0]
    posts = con.execute("SELECT count(*) FROM postagens WHERE ts >= ? AND ts < ?", (ini, fim)).fetchone()[0]
    linhas.append(f"📊 {lidas} promos lidas nos grupos · {produtos} perfumes diferentes")
    linhas.append(f"🔥 {exc} perfumes chegaram a Excelente · ✅ {boas} a Boa" + (f" · 📣 {posts} posts nossos" if posts else ""))

    melhores = melhores_da_semana(con, ini, fim)
    if melhores:
        linhas += ["", "🏆 *Melhores promos da semana*"]
        for m in melhores:
            pct = f" (−{m['pct']:.0f}%)" if m["pct"] else ""
            linhas.append(f"• {m['produto'][:55]} — {_brl(m['preco'])}{pct} · nota {m['score']} · {_data(m['ts'])}")

    var = variacoes(con, ini, fim)
    caiu = [v for v in var if v["pct"] <= -5][:5]
    subiu = [v for v in reversed(var) if v["pct"] >= 5][:3]
    if caiu:
        linhas += ["", "📉 *O que mais caiu* (mediana da semana × 30 dias antes)"]
        linhas += [f"• {v['produto'][:50]} — {_brl(v['antes'])} → {_brl(v['agora'])} (−{abs(v['pct']):.0f}%)" for v in caiu]
    if subiu:
        linhas += ["", "📈 *O que subiu*"]
        linhas += [f"• {v['produto'][:50]} — {_brl(v['antes'])} → {_brl(v['agora'])} (+{v['pct']:.0f}%)" for v in subiu]

    tops = top_vendidos(con, cfg, ini, fim)
    if tops:
        linhas += ["", "⭐ *Top vendidos: menor preço da semana*"]
        linhas += [f"• {t['nome']} {t['volume']}ml — {_brl(t['menor'])} em {_data(t['quando'])} "
                   f"(mediana da semana {_brl(t['mediana'])})" for t in tops]

    sem_post, desde = passaram_sem_post(con, ini, fim)
    if desde is not None:
        linhas += ["", "🎯 *Excelentes que passaram sem post*"
                   + (f" (desde {_data(desde)})" if desde > ini else "")]
        linhas += [f"• {m['produto'][:55]} — {_brl(m['preco'])} · nota {m['score']} · {_data(m['ts'])}"
                   for m in sem_post] or ["• nenhuma no período 👏"]

    cupons = con.execute("SELECT codigo, vezes FROM cupons WHERE perfume_ts >= ? ORDER BY vezes DESC LIMIT 3",
                         (ini,)).fetchall()
    if cupons:
        linhas += ["", "🎟 *Cupons mais vistos em perfume:* " + ", ".join(f"{c['codigo']} ({c['vezes']}×)" for c in cupons)]
    edicao = fila.texto(fila.medir(ini, fim), "Edição da semana (fila de exceções)")
    if edicao:
        linhas += ["", edicao]
    return "\n".join(linhas)
