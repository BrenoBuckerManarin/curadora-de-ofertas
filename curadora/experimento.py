"""Experimento curadora × equipe (pedido do Breno em 04/10).

Durante uma semana, das 8h às 22h, a cada 7 minutos (8:00, 8:07, 8:14…) a curadora ANOTA o que ela postaria
naquele momento (não posta nada). A escolha é independente da equipe: a melhor promo vista nas últimas 6h que ela
mesma ainda não escolheu nas últimas 6h. No fim do dia, um relatório compara as escolhas dela com o que a equipe
postou no grupo dela (Grupo da Equipe) no mesmo horário.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from collections import Counter
from typing import Callable

from . import RAIZ, fila

ESQUEMA_EXPERIMENTO = """
CREATE TABLE IF NOT EXISTS experimento (
    slot_ts   INTEGER PRIMARY KEY,
    dia       TEXT NOT NULL,
    card_id   INTEGER,
    chave     TEXT,
    produto   TEXT,
    preco     REAL,
    nota      TEXT,
    score     INTEGER,
    pct       REAL,
    link      TEXT,
    anotado_ts INTEGER
);
"""
PADRAO = {"inicio": "2026-10-06", "fim": "2026-10-12", "ensaio": "2026-10-05",
          "hora_ini": "08:00", "hora_fim": "22:00", "intervalo_min": 7}
TOLERANCIA_S = 180          # se o serviço estava parado no horário, anota até 3 min depois; mais que isso, pula
NAO_REPETIR_S = 6 * 3600
ORDEM = {"IMPERDÍVEL": 3, "BOA": 2, "OK": 1, "NÃO VALE": 0}
NOME = {"IMPERDÍVEL": "Excelente", "BOA": "Boa", "OK": "Média", "NÃO VALE": "Ruim"}


def config(cfg: dict) -> dict:
    return {**PADRAO, **(cfg.get("experimento") or {})}


def _hm(texto: str) -> tuple[int, int]:
    h, m = texto.split(":")
    return int(h), int(m)


def slots_do_dia(dia: dt.date, c: dict) -> list[int]:
    """Horários do dia (hora local da VPS), de hora_ini a hora_fim, a cada intervalo_min."""
    h0, m0 = _hm(c["hora_ini"])
    h1, m1 = _hm(c["hora_fim"])
    t = dt.datetime(dia.year, dia.month, dia.day, h0, m0)
    fim = dt.datetime(dia.year, dia.month, dia.day, h1, m1)
    saida = []
    while t <= fim:
        saida.append(int(time.mktime(t.timetuple())))
        t += dt.timedelta(minutes=int(c["intervalo_min"]))
    return saida


def dia_ativo(dia: dt.date, c: dict) -> bool:
    ini = dt.date.fromisoformat(c.get("ensaio") or c["inicio"])
    return ini <= dia <= dt.date.fromisoformat(c["fim"])


class Experimento:
    def __init__(self, con: sqlite3.Connection, escolher: Callable[[set], dict | None], cfg: dict, relogio=time.time):
        self.con = con
        self.escolher = escolher
        self.c = config(cfg)
        self.relogio = relogio
        self.anotados_hoje = 0

    def tick(self) -> dict | None:
        agora = self.relogio()
        hoje = dt.date.fromtimestamp(agora)
        if not dia_ativo(hoje, self.c):
            return None
        passados = [s for s in slots_do_dia(hoje, self.c) if s <= agora]
        if not passados or agora - passados[-1] > TOLERANCIA_S:
            return None
        slot = passados[-1]
        if self.con.execute("SELECT 1 FROM experimento WHERE slot_ts = ?", (slot,)).fetchone():
            return None
        ja = {r[0] for r in self.con.execute("SELECT chave FROM experimento WHERE slot_ts >= ? AND chave IS NOT NULL",
                                             (slot - NAO_REPETIR_S,))}
        a = self.escolher(ja) or {}
        self.con.execute(
            "INSERT OR IGNORE INTO experimento (slot_ts, dia, card_id, chave, produto, preco, nota, score, pct, link, "
            "anotado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slot, hoje.isoformat(), a.get("id"), a.get("chave"), a.get("produto"), a.get("preco"), a.get("nota"),
             a.get("score"), a.get("pct_abaixo"), a.get("link"), int(agora)))
        self.con.commit()
        self.anotados_hoje += 1
        return a

    def rodar(self, parar=lambda: False) -> None:
        while not parar():
            try:
                self.tick()
            except Exception as exc:
                print(f"[experimento] erro: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(20)


def _linha(ts: int, produto: str, nota: str | None, score) -> str:
    rotulo = f"{NOME.get(nota, nota or '?')} {score}" if nota else "sem escolha"
    return f"{time.strftime('%H:%M', time.localtime(ts))} {(produto or '—')[:40]} ({rotulo})"


def enviadas_pelo_pipeline(desde: float, ate: float, registro=None) -> dict[str, list[int]]:
    """{chave: [quando o pipeline mandou ao Gerador]} (dados/pipeline.jsonl)."""
    import json
    registro = registro or (RAIZ / "dados" / "pipeline.jsonl")
    saida: dict[str, list[int]] = {}
    if not registro.exists():
        return saida
    for linha in registro.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "enviado" and r.get("chave") and desde <= r.get("ts", 0) < ate:
            saida.setdefault(r["chave"], []).append(int(r["ts"]))
            if r.get("link_afiliado"):  # o mesmo link no GrupoFinal, mesmo que o Breno tenha mudado o nome
                saida.setdefault(r["link_afiliado"].split("?")[0], []).append(int(r["ts"]))
    return saida


def relatorio(con: sqlite3.Connection, cfg: dict, dia: dt.date, grupo_equipe: str = "Grupo da Equipe",
              registro_pipeline=None) -> str:
    c = config(cfg)
    slots = slots_do_dia(dia, c)
    ini, fim = slots[0], slots[-1] + int(c["intervalo_min"]) * 60
    passo = int(c["intervalo_min"]) * 60
    cur = con.execute("SELECT * FROM experimento WHERE slot_ts >= ? AND slot_ts < ? ORDER BY slot_ts", (ini, fim)).fetchall()
    hum = con.execute("SELECT ts, produto, chave, link, nota, score, nota_antes, json_extract(dados, '$.pct_abaixo') AS pct "
                      "FROM postagens WHERE ts >= ? AND ts < ? ORDER BY ts", (ini, fim)).fetchall()
    # 09/10: posts que a equipe fez com promos mandadas pelo pipeline da curadora não são "da equipe": ficam fora.
    pipeline = enviadas_pelo_pipeline(ini - 3 * 3600, fim, registro_pipeline)
    do_pipeline = [h for h in hum if any(0 <= h["ts"] - t <= 3 * 3600 for t in
                                         pipeline.get(h["chave"], []) + pipeline.get((h["link"] or "").split("?")[0], []))]
    hum = [h for h in hum if h not in do_pipeline]
    dia1 = dt.date.fromisoformat(c["inicio"])
    n_dia = (dia - dia1).days + 1
    titulo = "ensaio" if n_dia < 1 else f"dia {n_dia} de {(dt.date.fromisoformat(c['fim']) - dia1).days + 1}"
    linhas = [f"🧪 *CURADORA × EQUIPE · Experimento {titulo}* ({dia.strftime('%d/%m')}, "
              f"{c['hora_ini']}–{c['hora_fim']}, a cada {c['intervalo_min']} min)", ""]

    def resumo(rotulo: str, notas: list, scores: list) -> str:
        cont = Counter(notas)
        media = sum(scores) / len(scores) if scores else 0
        partes = " · ".join(f"{NOME[n]} {cont[n]}" for n in ("IMPERDÍVEL", "BOA", "OK", "NÃO VALE") if cont[n])
        return f"{rotulo}: {len(notas)} · nota média *{media:.0f}* · {partes}"

    escolhas = [r for r in cur if r["nota"]]
    if do_pipeline:
        linhas.append(f"📮 {len(do_pipeline)} posts da equipe vieram do pipeline da curadora: ficam fora da comparação")
    linhas.append(resumo("📣 Equipe postou", [r["nota"] for r in hum], [r["score"] for r in hum]))
    linhas.append(resumo("🧠 Curadora escolheria", [r["nota"] for r in escolhas], [r["score"] for r in escolhas]))
    # Desconto real médio: a medida mais objetiva (preço contra o normal de 60 dias), além da nota.
    pct_hum = [h["pct"] for h in hum if h["pct"] is not None]
    pct_cur = [r["pct"] for r in escolhas if r["pct"] is not None]
    if pct_hum or pct_cur:
        media = lambda v: f"{sum(v) / len(v):.0f}%" if v else "—"
        linhas.append(f"📉 Desconto real médio (vs preço normal): equipe {media(pct_hum)} · curadora {media(pct_cur)}")
    vazios = len(cur) - len(escolhas)
    if vazios:
        linhas.append(f"   ({vazios} horário(s) sem nada fresco para escolher)")
    comuns = {r["chave"] for r in escolhas} & {r["chave"] for r in hum}
    linhas.append(f"🤝 Produtos que os dois postaram: {len(comuns)}")

    # Mesmo horário: cada post da equipe contra a escolha da curadora no intervalo de 7 min em que caiu.
    por_slot = {r["slot_ts"]: r for r in escolhas}
    pares = []
    for h in hum:
        s = por_slot.get(ini + (h["ts"] - ini) // passo * passo)
        if s and s["chave"] != h["chave"]:
            pares.append((s["score"] - h["score"], h, s))
    melhor_cur = sorted((p for p in pares if p[0] >= 15), key=lambda p: -p[0])[:5]
    melhor_hum = sorted((p for p in pares if p[0] <= -15), key=lambda p: p[0])[:3]
    if melhor_cur:
        linhas += ["", "🏆 *Onde a curadora teria escolhido melhor* (mesmo horário)"]
        linhas += [f"• equipe: {_linha(h['ts'], h['produto'], h['nota'], h['score'])}\n"
                   f"  curadora: {(s['produto'] or '')[:40]} ({NOME.get(s['nota'])} {s['score']})" for _, h, s in melhor_cur]
    if melhor_hum:
        linhas += ["", "👏 *Onde a equipe escolheu melhor*"]
        linhas += [f"• equipe: {_linha(h['ts'], h['produto'], h['nota'], h['score'])}\n"
                   f"  curadora: {(s['produto'] or '')[:40]} ({NOME.get(s['nota'])} {s['score']})" for _, h, s in melhor_hum]
    nao_vistos = [h for h in hum if h["nota_antes"] is None]
    if hum:
        linhas += ["", f"🔎 Posts da equipe que a curadora nem tinha visto antes: {len(nao_vistos)} de {len(hum)}"
                   + (" (ex.: " + ", ".join((h["produto"] or "")[:30] for h in nao_vistos[:3]) + ")" if nao_vistos else "")]
    if pares:
        ganhou = sum(1 for p in pares if p[0] > 0)
        linhas += ["", f"📊 No mesmo horário, a escolha da curadora teve nota maior em {ganhou} de {len(pares)} "
                       f"comparações ({ganhou / len(pares) * 100:.0f}%)."]
    edicao = fila.texto(fila.medir(ini, fim + 3600), "Edição de hoje (fila de exceções)")
    if edicao:
        linhas += ["", edicao]
    linhas += ["", "_Nota = a da curadora (0–100) no momento; os dois lados conferidos no ML na hora. "
                  "Ninguém postou nada pela curadora: é só simulação._"]
    return "\n".join(linhas)
