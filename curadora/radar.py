"""Radar do ML: acha as melhores ofertas dos produtos vigiados direto na API, sem esperar os grupos.

Para cada produto da lista top:
  1. descobre o(s) produto(s) de catálogo do ML (aprendidos dos cards já conferidos ou pela busca da API);
  2. lê TODAS as ofertas do catálogo (todos os vendedores);
  3. guarda cada preço como histórico real (sem cupom);
  4. avalia a oferta mais barata e a mais barata em loja oficial; se for BOA/EXCELENTE, alerta.
"""
from __future__ import annotations

import time

from .alertas import Alertador
from .banco import (FONTE_API, produtos_catalogo, registrar_avaliacao, registrar_preco_ml, registrar_produto_catalogo,
                    registrar_vendedores)
from .identidade import Identificador
from .ml import CardML
from .ml_api import ClienteML, SemCredencial
from .parser import Promo
from .score import Curadora, para_dict

PRODUTOS_POR_NOME = 3     # ex.: 100ml e 50ml do mesmo perfume
# 21/10 (Breno, print do card "Asad Tradicional · loja oficial PERFUMELATTAFA7": "isso não é loja oficial"): a API marca
# o anúncio com loja oficial (official_store_id), mas a conta vende pouco (PERFUMELATTAFA7: 804 vendas; PERFUMELATTAFA6:
# 0) e o link do ML não mostra o selo. Loja oficial vinda só da API exige 5 mil vendas, o mesmo corte do pipeline.
OFICIAL_API_MIN_VENDAS = 5000


def oficial_so_na_api(rep: dict | None) -> bool:
    """A API diz "loja oficial", mas a conta tem menos de 5 mil vendas: não vale como loja oficial."""
    vendas = (rep or {}).get("vendas")
    return vendas is not None and vendas < OFICIAL_API_MIN_VENDAS
EXTRAS_POR_VOLTA = 4      # da lista extra (config "radar_extras"), quantos nomes por volta (a volta é de 15 min)


class Radar:
    def __init__(self, curadora: Curadora, cliente: ClienteML, alertador: Alertador,
                 nomes: list[str] | None = None, relogio=time.time):
        self.cur = curadora
        self.con = curadora.con
        self.cliente = cliente
        self.alertador = alertador
        self.nomes = nomes if nomes is not None else list(curadora.cfg.get("top_vendidos", []))
        # 11/10 (Breno: "consegue arrumar"): o pipeline ficava sem promo no fim do turno e sem Natura/Boticário,
        # porque só enxergava o que os grupos postavam. A lista extra é vigiada aos poucos (4 nomes por volta, para
        # não pesar na API do ML, que a coletora também usa) e NÃO conta como TOP vendido na nota.
        self.extras = [] if nomes is not None else list(curadora.cfg.get("radar_extras", []))
        self._proximo_extra = 0
        self.relogio = relogio
        self.ultimo_erro = ""
        self.estatisticas = {"rodadas": 0, "ofertas_lidas": 0, "alertas": 0}

    # ── quais produtos de catálogo vigiar ───────────────────────
    def _e_o_produto(self, nome_lista: str, nome_ml: str) -> bool:
        ident = self.cur.ident
        alvo, candidato = ident.analisar(nome_lista), ident.analisar(nome_ml)
        if Identificador.conflito(alvo, candidato):
            return False
        return ident.mesmo_produto_da_lista(self.cur.catalogo.chave(candidato), nome_lista)

    def extras_da_vez(self) -> list[str]:
        """Os próximos nomes da lista extra (roda em círculo)."""
        if not self.extras:
            return []
        inicio = self._proximo_extra % len(self.extras)
        self._proximo_extra = inicio + EXTRAS_POR_VOLTA
        return [self.extras[(inicio + i) % len(self.extras)] for i in range(min(EXTRAS_POR_VOLTA, len(self.extras)))]

    def alvos(self, buscar: bool = True, nomes: list[str] | None = None) -> dict[str, list[tuple[str, str]]]:
        """{nome da lista: [(produto_id, nome no ML), ...]}"""
        conhecidos = produtos_catalogo(self.con)
        resultado: dict[str, list[tuple[str, str]]] = {}
        for nome in (self.nomes if nomes is None else nomes):
            achados = [(r["produto_id"], r["nome"]) for r in conhecidos if self._e_o_produto(nome, r["nome"] or "")]
            if not achados and buscar:
                for p in self.cliente.buscar_produtos(nome):
                    if p["id"] and self._e_o_produto(nome, p["nome"]):
                        registrar_produto_catalogo(self.con, p["id"], self.cur.chave_de(p["nome"]), p["nome"], "busca API",
                                                   int(self.relogio()))
                        achados.append((p["id"], p["nome"]))
            resultado[nome] = achados[:PRODUTOS_POR_NOME]
        return resultado

    # ── uma volta pelo radar ────────────────────────────────────
    def rodada(self) -> list:
        alertadas = []
        try:
            alvos = self.alvos(nomes=self.nomes + self.extras_da_vez())
        except SemCredencial as exc:
            self.ultimo_erro = str(exc)
            return alertadas
        self.estatisticas["rodadas"] += 1
        for nome, produtos in alvos.items():
            for produto_id, nome_ml in produtos:
                try:
                    ofertas = self.cliente.ofertas(produto_id)
                    registrar_vendedores(self.cur.con, produto_id, ofertas, int(time.time()))
                except Exception as exc:  # um produto com problema não para o radar
                    self.ultimo_erro = f"{nome_ml}: {exc}"
                    continue
                agora = int(self.relogio())
                chave = self.cur.chave_de(nome_ml)
                self.estatisticas["ofertas_lidas"] += len(ofertas)
                cards = []
                for o in ofertas:
                    card = CardML(ok=True, item_id=o.item_id, produto_id=produto_id, titulo=nome_ml, preco=o.preco,
                                  preco_anterior=o.preco_anterior, loja_oficial=o.loja_oficial,
                                  url_produto=f"https://www.mercadolivre.com.br/p/{produto_id}",
                                  # 11/10: as 2 ofertas mais baratas do The Kingdom eram de fora do Brasil e o
                                  # card do radar aparecia como Boa (o grupo não posta compra internacional).
                                  internacional=o.internacional,
                                  # 31/10: quem vende vai junto no registro do preço (precos_ml.seller_id)
                                  seller_id=o.seller_id)
                    registrar_preco_ml(self.con, agora, chave, card, FONTE_API)
                    cards.append((o, card))
                self.con.commit()
                # Avalia a mais barata e a mais barata em loja oficial (podem ser a mesma).
                # 11/10: só as do Brasil concorrem (compra internacional o grupo não posta). No The Kingdom e no
                # Club de Nuit Intense as mais baratas eram de fora, e a oferta daqui nem chegava a ser avaliada.
                daqui = [c for c in cards if not c[0].internacional] or cards
                # 01/11 (Breno: "loja oficial vence revenda"): no empate de preço, a candidata é a loja oficial
                daqui = sorted(daqui, key=lambda c: (c[0].preco, not c[0].loja_oficial))
                candidatos = []
                if daqui:
                    candidatos.append(daqui[0])
                    oficial = next((c for c in daqui if c[0].loja_oficial), None)
                    if oficial and oficial is not daqui[0]:
                        candidatos.append(oficial)
                for oferta, card in candidatos:
                    rep = self.cliente.vendedor(oferta.seller_id)
                    card.vendedor, card.seller_id, card.reputacao = rep.get("nome", ""), oferta.seller_id, rep or None
                    card.loja_oficial = oferta.loja_oficial and not oficial_so_na_api(rep)
                    promo = Promo(texto=f"{nome_ml}\nPor R$ {oferta.preco:.2f}\n{card.url_produto}", produto=nome_ml,
                                  preco=oferta.preco, preco_de=oferta.preco_anterior, link=card.url_produto,
                                  marketplace="ml", vendedor=card.vendedor, loja_oficial=card.loja_oficial)
                    # A referência usa só o que veio ANTES desta volta: a oferta não é comparada com ela mesma.
                    # 31/10: o preço do card é o da API (sem o Pix); o Excelente compara só com a API do anúncio
                    av = self.cur.avaliar(promo, agora=agora, card=card, card_da_api=True)
                    if av is None:
                        continue
                    av.grupo = f"radar ML: {nome}"
                    alertou = self.alertador.talvez_alertar(av, "radar do ML", agora)
                    registrar_avaliacao(self.con, agora, "radar do ML", av.grupo, av.identidade.chave, av.nota,
                                        av.score, alertou, para_dict(av))
                    if alertou:
                        self.estatisticas["alertas"] += 1
                        alertadas.append(av)
        self.ultimo_erro = ""
        return alertadas

    def rodar(self, intervalo_s: float = 900, parar=lambda: False) -> None:
        while not parar():
            inicio = time.monotonic()
            try:
                self.rodada()
            except Exception as exc:
                self.ultimo_erro = f"{type(exc).__name__}: {exc}"
                print(f"[radar] erro: {self.ultimo_erro}", flush=True)
            restante = intervalo_s - (time.monotonic() - inicio)
            while restante > 0 and not parar():
                time.sleep(min(5.0, restante))
                restante -= 5.0
