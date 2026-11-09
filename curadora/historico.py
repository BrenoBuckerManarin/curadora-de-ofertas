"""Preço "normal" de um produto: estatísticas dos últimos N dias.

Há duas referências, porque o preço com cupom fica de 6% a 19% abaixo do preço sem
cupom (medido em 02/10 nos 10 mais vendidos). Comparar uma promo sem cupom com a
mediana misturada tiraria nota dela, e uma com cupom ganharia nota demais.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from .banco import FONTE_API, precos_do_produto


@dataclass
class Historico:
    observacoes: int = 0
    dias: int = 0
    mediana: float | None = None
    minimo: float | None = None
    p25: float | None = None
    ultimo: float | None = None
    tipo: str = "todos"
    media: float | None = None


def historico_de_preco(con, chave: str, ate_ts: int, janela_dias: int, tipo: str = "todos") -> Historico:
    """Só olha para trás (antes de `ate_ts`), para nunca comparar a promo com ela mesma."""
    linhas = precos_do_produto(con, chave, ate_ts - janela_dias * 86400, ate_ts, tipo)
    if not linhas:
        return Historico(tipo=tipo)
    centro = statistics.median(r["preco"] for r in linhas)
    # A mesma oferta é repostada por vários grupos no mesmo dia: conta uma vez.
    unicos: dict[tuple[str, int], float] = {}
    ultimo_valido = None
    for r in linhas:
        p = r["preco"]
        if p < centro * 0.35 or p > centro * 3:
            continue  # erro de leitura ou produto diferente com o mesmo nome
        dia = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        unicos[(dia, round(p))] = p
        ultimo_valido = p
    valores = sorted(unicos.values())
    if not valores:
        return Historico(tipo=tipo)
    p25 = statistics.quantiles(valores, n=4)[0] if len(valores) >= 4 else valores[0]
    return Historico(
        observacoes=len(valores),
        dias=len({d for d, _ in unicos}),
        mediana=statistics.median(valores),
        minimo=valores[0],
        p25=p25,
        ultimo=ultimo_valido,
        media=statistics.mean(valores),
        tipo=tipo,
    )


def tendencia(con, chave: str, ate_ts: int, preco_atual: float | None = None, tipo: str = "todos") -> dict:
    """Como o preço vem andando: mediana dos últimos 7 dias contra a dos 8–60 dias antes.

    direcao: "caindo" (5%+ abaixo), "subindo" (5%+ acima) ou "estavel".
    ja_esteve: menor preço dos últimos 14 dias, se foi 8%+ abaixo do preço atual (talvez valha esperar).
    """
    # Mesma amostra limpa e sem repostagens da referência de preço; cupom separado.
    semana = historico_de_preco(con, chave, ate_ts, 7, tipo)
    antes = historico_de_preco(con, chave, ate_ts - 7 * 86400, 53, tipo)
    saida: dict = {}
    if semana.observacoes >= 3 and antes.observacoes >= 5:
        med_semana, med_antes = semana.mediana, antes.mediana
        variacao = (med_semana / med_antes - 1) * 100
        saida = {"variacao_pct": round(variacao, 1),
                 "direcao": "caindo" if variacao <= -5 else "subindo" if variacao >= 5 else "estavel"}
    recente = historico_de_preco(con, chave, ate_ts, 14, tipo)
    if preco_atual and recente.minimo is not None and recente.minimo <= preco_atual * 0.92:
        linhas = precos_do_produto(con, chave, ate_ts - 14 * 86400, ate_ts, tipo)
        # 31/10: as duas leituras não são atômicas; se a linha do mínimo sumir entre elas, fica sem o aviso.
        menor = next((r for r in reversed(linhas) if r["preco"] == recente.minimo), None)
        if menor is not None:
            saida.update(ja_esteve=round(menor["preco"], 2), ha_dias=max(1, int((ate_ts - menor["ts"]) / 86400)))
    return saida


ANUNCIO_DIAS = 7


def menor_preco_do_anuncio(con, chave: str, item_id: str, ate_ts: float, preco_atual: float,
                           api: bool = False) -> float | None:
    """O menor preço do MESMO anúncio (item MLB) visto no ML nos 7 dias antes de `ate_ts`, sem a leitura de agora
    (10 min). Procura pela chave do produto (usa o índice) e pelas versões dela ("chave|f"). Leitura abaixo de metade
    do preço atual é descartada (erro de leitura). None se o anúncio não foi visto.
    `api`: o preço atual veio da API do ML (radar, sem o Pix) e só se compara com leituras da API; senão, só com as da
    página (com o Pix). 31/10: o radar do Khamrah na Wathiq caiu para Boa comparando os R$186,72 da API, parados a
    semana toda, com os R$177,38 da página (186,72 menos os 5% do Pix)."""
    if not (chave and item_id and preco_atual):
        return None
    r = con.execute("SELECT MIN(preco) FROM precos_ml WHERE chave >= ? AND chave < ? AND item_id = ? AND ts >= ? "
                    "AND ts < ? AND preco >= ? AND (COALESCE(fonte, '') = ?) = ?",
                    (chave, chave + "\uffff", item_id, ate_ts - ANUNCIO_DIAS * 86400, ate_ts - 600,
                     preco_atual * 0.5, FONTE_API, bool(api))).fetchone()
    return r[0] if r and r[0] else None


def ultimo_preco_da_pagina(con, chave: str, item_id: str, ate_ts: float, horas: int = 24) -> float | None:
    """O último preço do anúncio visto na PÁGINA do ML (com o Pix) nas `horas` antes de `ate_ts`; None se não houver.
    31/10: o radar só lê a API (sem o Pix). No Club de Nuit Intense a API até caiu (214,99 para 189,99), mas a página
    subiu de 151,99 para 189,99: o desconto do Pix acabou e quem paga no Pix paga 25% a mais."""
    if not (chave and item_id):
        return None
    r = con.execute("SELECT preco FROM precos_ml WHERE chave >= ? AND chave < ? AND item_id = ? AND ts >= ? AND ts <= ? "
                    "AND COALESCE(fonte, '') != ? ORDER BY ts DESC LIMIT 1",
                    (chave, chave + "\uffff", item_id, ate_ts - horas * 3600, ate_ts, FONTE_API)).fetchone()
    return r[0] if r and r[0] else None


def referencia(con, chave: str, ate_ts: int, janela_dias: int, com_cupom: bool, minimo: int) -> Historico:
    """Referência do mesmo tipo da promo (com ou sem cupom); se tiver pouco dado, usa a misturada."""
    h = historico_de_preco(con, chave, ate_ts, janela_dias, "com_cupom" if com_cupom else "sem_cupom")
    if h.observacoes >= minimo:
        return h
    return historico_de_preco(con, chave, ate_ts, janela_dias, "todos")
