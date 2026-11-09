"""Simulação contínua da rodada diurna, em sombra (04/11, fase 1 do PLANO_MELHOR_OFERTA_2026-11-04.md).

A cada 7 min do turno (08:00 às 17:50), o que a curadora mandaria naquele minuto, usando só o que existia até ali, com
as regras da fila do pipeline (pipeline.candidatas e pipeline.misturar, sem cópia): 8 h sem repetir, outro tamanho
só depois de 4 h, rodízio de marca e de faixa de preço, Natura e Boticário 1 a cada 5, impulso 1 a cada 6, a linha só
depois de 1 h, queridinhos e loja da marca na rodada de cupom, recusadas por 30 min, reserva e fundo.

Fila paralela (Astra, 04/11: "ela deve respeitar a própria fila simulada"): a fila começa com o histórico real até o
começo do turno e, dali em diante, só conta as escolhas da própria simulação. Os posts da equipe no turno não travam a
simulação: são o que se compara com ela. Já o que é informação (preços, conferências, cupons em uso, esgotados) vale
como estava no minuto, inclusive o que veio da equipe.

Sem efeito fora daqui: as conexões são somente leitura; não abre página do ML, não chama o gerador, não grava a ficha
nem as travas. A conferência do pipeline (abrir a página agora) é trocada pela última conferência registrada até o
minuto, e só vale a de até 2 h (o prazo aprovado em 04/11); a escolhida com leitura de mais de 30 min fica marcada
"reconferir antes de enviar".
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from . import momento, ofertas, travas
from .parser import parece_perfume
from .score import Curadora

PASSO_S = 7 * 60
INICIO, FIM = "08:00", "17:50"
RECUSADA_S = 30 * 60          # o mesmo de pipeline.RECUSADA_S
CONFERIDAS_POR_VEZ = 12       # uma_promo olha as 12 primeiras da fila
ID_ML = re.compile(r"MLB-?(\d{6,})")


@dataclass
class Vez:
    """O que a curadora mandaria num minuto do turno."""
    ts: float
    modo: str = "sem escolha"            # normal, reserva 6h, reserva 4h, fundo, sem escolha
    chave: str = ""
    produto: str = ""
    nota: str = ""
    score: int | None = None
    origem: str = ""
    posicao: int | None = None           # posição na fila (1 = a primeira)
    decisao: ofertas.Decisao | None = None
    puladas: list[tuple[str, str]] = field(default_factory=list)
    cupons_em_uso: list[str] = field(default_factory=list)
    cupom_da_marca: str = ""
    preco_efetivo: float | None = None   # o preço do card (com cupom, se a avaliação usou), como o pipeline registra

    def resumo(self) -> dict:
        d = self.decisao
        esc = d.escolhida if d is not None else None
        return {"ts": int(self.ts), "hora": time.strftime("%H:%M", time.localtime(self.ts)), "modo": self.modo,
                "chave": self.chave, "produto": self.produto, "nota": self.nota, "score": self.score,
                "origem": self.origem, "posicao": self.posicao, "cupons_em_uso": self.cupons_em_uso,
                "preco_efetivo": self.preco_efetivo,
                "cupom_da_marca": self.cupom_da_marca, "puladas": self.puladas[:8],
                "status": d.status if d is not None else "", "troca": bool(d and d.troca),
                "escolhida": None if esc is None else {
                    "vendedor": esc.obs.vendedor, "anuncio": esc.obs.anuncio, "categoria": esc.categoria,
                    "preco": esc.preco_comparado, "base": esc.base_comparada, "lida": int(esc.obs.ts),
                    "link": esc.obs.link},
                "referencia": None if d is None else {
                    "vendedor": d.referencia.obs.vendedor, "anuncio": d.referencia.obs.anuncio,
                    "preco": d.referencia.preco_comparado, "categoria": d.referencia.categoria},
                "pronta": bool(d and d.pronta), "economia": d.economia if d is not None else None,
                "frase": d.frase() if d is not None else ""}


def horarios_do_turno(dia: str, inicio: str = INICIO, fim: str = FIM, passo_s: int = PASSO_S) -> list[float]:
    """Os minutos do turno de `dia` (AAAA-MM-DD): 08:00, 08:07, 08:14... até `fim`."""
    t0 = time.mktime(time.strptime(f"{dia} {inicio}", "%Y-%m-%d %H:%M"))
    t1 = time.mktime(time.strptime(f"{dia} {fim}", "%Y-%m-%d %H:%M"))
    saida, t = [], t0
    while t <= t1:
        saida.append(t)
        t += passo_s
    return saida


@contextlib.contextmanager
def pipeline_no_horario(t: float, registro: momento.RegistroEmMemoria, fila_desde: float):
    """Dentro do processo da simulação: o relógio no minuto t, o registro do pipeline em memória (o real até o começo
    do turno mais as escolhas simuladas) e a normalizadora vista só até o começo do turno. Tudo volta ao sair."""
    import painel
    import pipeline
    from . import esgotados
    real = time.time
    trocas = [(pipeline, "REGISTRO", registro), (painel, "REGISTRO_PIPELINE", registro),
              (esgotados, "REGISTRO_PIPELINE", registro),
              (pipeline, "na_normalizadora",
               lambda cur, desde: momento.na_normalizadora_ate(pipeline.FEEDBACK, cur.chave_de, desde,
                                                                min(t, fila_desde - 1)))]
    antes = [(mod, nome, getattr(mod, nome)) for mod, nome, _ in trocas]
    for mod, nome, valor in trocas:
        setattr(mod, nome, valor)
    caches = [getattr(painel, "_cache_cards", None), getattr(esgotados, "_rodada_cache", None)]
    for c in caches:
        if isinstance(c, dict):
            c.clear()
    time.time = lambda: float(t)
    try:
        yield pipeline
    finally:
        time.time = real
        for mod, nome, valor in antes:
            setattr(mod, nome, valor)
        for c in caches:
            if isinstance(c, dict):
                c.clear()


class Simulacao:
    """Uma simulação por turno. Chame vez(t) com t sempre crescente."""

    def __init__(self, banco: Path | str, cfg: dict, ident, inicio_turno: float, registro: Path | str,
                 cupons: momento.CuponsNoTempo | None = None, comparador: Path | str | None = None):
        self.inicio = inicio_turno
        self.ident = ident
        self.con_fila = momento.conectar_leitura(banco)
        self.con_info = momento.conectar_leitura(banco)
        momento.no_horario(self.con_info, inicio_turno)
        if cupons is not None:
            cupons.ate(inicio_turno).aplicar(self.con_info)
        self.cur = Curadora(cfg, self.con_info, None)   # sem consultor do ML: não abre página nem grava preço
        self.cupons = cupons
        self.comparador = momento.conectar_leitura(comparador) if comparador and Path(comparador).exists() else None
        self.registro = momento.registro_ate(registro, inicio_turno, fila_desde=inicio_turno)
        self.recusadas: dict[str, dict[str, float]] = {"normal": {}, "reserva": {}, "fundo": {}}
        # pipeline.rodar: o que a rodada já tentou (mandou ou pulou) não volta na fila normal até o fim da rodada; a
        # lista recomeça quando entra um cupom novo em uso. Sem isso, as 12 primeiras (na rodada de cupom, os
        # queridinhos já mandados) ocupavam todas as vezes (1ª rodada do replay de 04/11).
        self.excluir: set[str] = set()
        self.em_uso: set[str] = set()
        self.vezes: list[Vez] = []

    def fechar(self) -> None:
        for con in (self.con_fila, self.con_info, self.comparador):
            if con is not None:
                con.close()

    # ── um minuto do turno ───────────────────────────────────────
    def vez(self, t: float) -> Vez:
        momento.no_horario(self.con_fila, t, fila_desde=self.inicio)
        momento.no_horario(self.con_info, t)
        if self.cupons is not None:
            self.cupons.ate(t).aplicar(self.con_info)
        v = Vez(ts=t)
        with pipeline_no_horario(t, self.registro, self.inicio) as pipeline:
            em_uso = set(pipeline.cupons_da_equipe_agora(self.cur))
            if em_uso - self.em_uso:   # cupom novo em uso: a lista é refeita com ele (pipeline.rodar)
                self.excluir.clear()
                self.recusadas["normal"].clear()
            self.em_uso = em_uso
            v.cupons_em_uso = sorted(em_uso)
            v.cupom_da_marca = pipeline.cupom_da_marca_agora(self.cur)
            for modo, janela, reserva, fundo in (("normal", pipeline.REPETIR_APOS_S, False, False),
                                                 ("reserva 6h", pipeline.RESERVA_S[0], True, False),
                                                 ("reserva 4h", pipeline.RESERVA_S[1], True, False),
                                                 ("fundo", pipeline.REPETIR_APOS_S, False, True)):
                antes = (pipeline._janela_repetir, pipeline._na_reserva, pipeline._no_fundo)
                pipeline._janela_repetir, pipeline._na_reserva, pipeline._no_fundo = janela, reserva, fundo
                try:
                    achou = self._uma(pipeline, t, modo, v)
                finally:
                    pipeline._janela_repetir, pipeline._na_reserva, pipeline._no_fundo = antes
                if achou:
                    break
        if v.decisao is not None:
            esc = v.decisao.escolhida
            # o preço em que "foi" é o efetivo do card (com o cupom, como o pipeline registra): é com ele que a regra
            # das 8 h compara o preço de depois. Na troca, o menor dos dois.
            preco = v.preco_efetivo
            if v.decisao.troca and esc is not None and esc.preco_comparado:
                preco = min(p for p in (preco, esc.preco_comparado) if p)
            ids = set()
            for x in ([v.decisao.referencia.obs] + ([esc.obs] if esc is not None else [])):
                ids |= {"MLB" + n for n in ID_ML.findall(f"{x.anuncio} {x.link}")} | ({x.catalogo} if x.catalogo else set())
            self.registro.acrescentar({"ts": int(t), "status": "enviado", "chave": v.chave, "produto": v.produto,
                                       "nota": v.nota, "score": v.score, "preco": preco, "ml_ids": sorted(ids),
                                       "simulado": True})
        self.vezes.append(v)
        return v

    def _produto(self, card: dict) -> str:
        if card.get("produto"):
            return card["produto"]
        av_id = (card.get("atual") or {}).get("id") or card.get("id")
        r = self.con_fila.execute("SELECT json_extract(dados, '$.produto') FROM avaliacoes WHERE id = ?", (av_id,)).fetchone()
        card["produto"] = (r[0] if r else "") or ""
        return card["produto"]

    def _uma(self, pipeline, t: float, modo: str, v: Vez) -> bool:
        # a fila normal guarda o que a rodada já tentou; reserva e fundo começam do zero (como promo_da_reserva e
        # promo_do_fundo, que chamam uma_promo com set())
        excluir = self.excluir if modo == "normal" else set()
        prioridade, lista = pipeline.candidatas(self.con_fila, self.cur, excluir)
        lista = pipeline.misturar(lista, self._produto, pipeline.ultimas_mandadas(pipeline.RECENTES_PARA_A_ORDEM),
                                  lambda produto: pipeline.marca_visivel(self.cur, produto), pipeline.linhas_recentes())
        lista = prioridade + lista
        recusadas = self.recusadas["fundo" if modo == "fundo" else "reserva" if modo.startswith("reserva") else "normal"]
        for posicao, card in enumerate(lista[:CONFERIDAS_POR_VEZ], 1):
            chave = card["chave"]
            excluir.add(chave)   # uma_promo: tentada nesta rodada (mandada ou pulada)
            if t - recusadas.get(chave, 0) < RECUSADA_S:
                continue
            ok, motivo, linha, dados = self._conferir(pipeline, card, t)
            if not ok:
                recusadas[chave] = t
                v.puladas.append((chave, motivo))
                continue
            ref = ofertas.observacao_da_avaliacao(linha["id"], linha["ts"], linha["origem"], linha["chave"],
                                                  linha["nota"], linha["score"], dados)
            outras = momento.observacoes_do_perfume(self.con_info, chave, t, self.comparador)
            v.decisao = ofertas.escolher(ref, outras, t, self.ident, momento="simulação",
                                         produto=dados.get("produto") or self._produto(card))
            v.modo, v.chave, v.produto = modo, chave, dados.get("produto") or self._produto(card)
            v.nota, v.score, v.origem, v.posicao = linha["nota"], linha["score"], linha["origem"], posicao
            v.preco_efetivo = float(dados.get("preco") or (dados.get("ml") or {}).get("preco") or 0) or None
            return True
        return False

    def _conferir(self, pipeline, card: dict, t: float):
        """A conferência do pipeline (pipeline.conferir) sem abrir a página: a última conferência registrada até t."""
        av_id = (card.get("atual") or {}).get("id") or card["id"]
        linha = self.con_fila.execute("SELECT id, ts, origem, chave, nota, score, dados FROM avaliacoes WHERE id = ?",
                                      (av_id,)).fetchone()
        if linha is None:
            return False, "sem a avaliação do card", None, None
        dados = json.loads(linha["dados"] or "{}")
        ml = dados.get("ml") or {}
        if t - card["ts"] > ofertas.FRESCOR_COMPARAR_S:
            return False, "sem conferência nas 2 h antes (o pipeline abriria a página agora)", linha, dados
        if not (ml.get("preco") and ml.get("url")):
            return False, "sem anúncio do ML conferido", linha, dados
        if dados.get("internacional"):
            return False, "compra internacional", linha, dados
        fraco = travas.vendedor_fraco(bool(ml.get("oficial")), dados.get("reputacao"))
        if fraco:
            return False, fraco, linha, dados
        if not parece_perfume(ml.get("titulo") or dados.get("produto") or ""):
            return False, "não é perfume", linha, dados
        ressalva = pipeline.ressalva_da_curadora(SimpleNamespace(
            conflito=dados.get("conflito") or "", risco=dados.get("risco") or "",
            risco_motivo=dados.get("risco_motivo") or "", produtos_vendedor=dados.get("produtos_vendedor"),
            loja_selo=dados.get("loja_selo")))
        if ressalva:
            return False, f"sairia com ressalva da curadora: {ressalva[:80]}", linha, dados
        # a mesma trava de 8 h da conferência, com o registro simulado e as postagens de antes do turno
        vistos = pipeline.anuncios_recentes(self.con_fila, self.cur)
        foi_por = [vistos[i] for i in {"MLB" + n for n in ID_ML.findall(ml.get("url") or "")} if i in vistos]
        ultimo = pipeline.ultimos_posts(self.con_fila, t, self.registro).get(linha["chave"])
        if ultimo and t - ultimo[1] < pipeline._janela_repetir:
            foi_por.append(ultimo[0])
        preco = float(dados.get("preco") or ml["preco"])
        if foi_por and min(foi_por) and not preco <= min(foi_por) * (1 - pipeline.MAIS_BARATO):
            return False, f"já foi há menos de 8h a R${min(foi_por):.2f}", linha, dados
        if not pipeline.aceitavel(SimpleNamespace(conflito=dados.get("conflito") or "", nota=linha["nota"],
                                                  score=linha["score"])):
            return False, f"a nota é {linha['nota']} {linha['score']}", linha, dados
        return True, "", linha, dados
