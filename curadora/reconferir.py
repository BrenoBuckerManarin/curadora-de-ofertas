"""Reconferidor: reabre no ML as promos de Excelentes e Boas (e o topo de Médias e Ruins) para não mostrar preço velho.

Pedido do Breno em 04/10 ("tem uns preços já bem desatualizados"). A cada rodada, cada card IMPERDÍVEL/BOA
conferido há mais de `idade_min_s` é avaliado de novo com o preço atual do ML. A avaliação nova é gravada
com `reconferida_de` = id do card; o painel usa a mais recente como a situação ATUAL daquele card
(subiu → desce de seção e mostra "era R$X"; igual → só atualiza o "conferido há"). Nunca alerta.
"""
from __future__ import annotations

import time
from typing import Callable

from .banco import registrar_avaliacao
from .score import para_dict

NOTAS_RECONFERIDAS = ("IMPERDÍVEL", "BOA")
IDADE_MIN_S = 15 * 60
TOPO_POR_SECAO = 10        # as 10 primeiras de Médias e de Ruins também são reconferidas
TOPO_FRESCO_S = 6 * 3600


def link_do_ml(link: str) -> bool:
    return bool(link) and ("meli.la" in link or "mercadolivre.com" in link or "mercadolibre.com" in link)


class Reconferidor:
    def __init__(self, curadora, listar_cards: Callable[[], list[dict]], ler_dados: Callable[[int], dict],
                 relogio=time.time, idade_min_s: float = IDADE_MIN_S, maximo_por_rodada: int = 60):
        self.cur = curadora
        self.listar_cards = listar_cards
        self.ler_dados = ler_dados
        self.relogio = relogio
        self.idade_min_s = idade_min_s
        self.maximo = maximo_por_rodada
        self.estatisticas = {"rodadas": 0, "reconferidas": 0, "mudaram": 0, "ultima": 0}
        self.ultimo_erro = ""

    def alvos(self) -> list[dict]:
        agora = self.relogio()
        todos = self.listar_cards()
        cards = [c for c in todos if c["nota"] in NOTAS_RECONFERIDAS and agora - c["ts"] >= self.idade_min_s]
        # Excelentes primeiro; dentro da seção, as conferidas há mais tempo.
        cards.sort(key=lambda c: (c["nota"] != "IMPERDÍVEL", c["ts"]))
        # Topo de Médias e Ruins (pedido do Breno em 04/10): no aperto a equipe posta de lá, então as que
        # aparecem primeiro no "Melhores primeiro" (não postadas, vistas nas últimas 6h) também são reconferidas.
        for nota in ("OK", "NÃO VALE"):
            topo = sorted((c for c in todos if c["nota"] == nota and not c.get("postada")
                           and agora - c["ts"] <= TOPO_FRESCO_S), key=lambda c: (-c["score"], -c["ts"]))
            cards += [c for c in topo[:TOPO_POR_SECAO] if agora - c["ts"] >= self.idade_min_s]
        return cards[:self.maximo]

    def reconferir(self, card: dict) -> bool:
        """Devolve True se o preço ou a nota mudaram."""
        dados = self.ler_dados(card["id"])
        link = dados.get("link", "")
        if not link_do_ml(link):
            return False
        texto = dados.get("texto") or f"*{dados.get('produto', '')}*\nPor R$ {dados.get('preco_grupo') or card['preco']}\n{link}"
        agora = int(self.relogio())
        av = self.cur.avaliar(texto, agora=agora, conferir_ml=True)
        if av is None or not (av.card and av.card.ok):
            return False  # não deu para abrir o ML agora: mantém como está e tenta na próxima rodada
        novo = para_dict(av)
        novo.update(reconferida_de=card["id"], preco_antes=card["preco"], nota_antes=card["nota"])
        registrar_avaliacao(self.cur.con, agora, "reconferida", card.get("grupo", ""), av.identidade.chave,
                            av.nota, av.score, False, novo)
        self.estatisticas["reconferidas"] += 1
        mudou = av.nota != card["nota"] or abs(av.preco_efetivo - card["preco"]) > 0.02 * max(card["preco"], 1)
        self.estatisticas["mudaram"] += int(mudou)
        return mudou

    def rodada(self) -> int:
        mudaram = 0
        for card in self.alvos():
            try:
                mudaram += int(self.reconferir(card))
            except Exception as exc:  # um link com problema não para a rodada
                self.ultimo_erro = f"{type(exc).__name__}: {exc}"
        self.estatisticas["rodadas"] += 1
        self.estatisticas["ultima"] = int(self.relogio())
        return mudaram

    def rodar(self, intervalo_s: float = 300, parar=lambda: False) -> None:
        while not parar():
            inicio = time.monotonic()
            try:
                self.rodada()
            except Exception as exc:
                self.ultimo_erro = f"{type(exc).__name__}: {exc}"
                print(f"[reconferir] erro: {self.ultimo_erro}", flush=True)
            restante = intervalo_s - (time.monotonic() - inicio)
            while restante > 0 and not parar():
                time.sleep(min(5.0, restante))
                restante -= 5.0
