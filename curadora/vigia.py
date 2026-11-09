"""Vigia do ML: reabre de tempos em tempos os links dos top vendidos para pegar queda de preço
antes de os grupos postarem.

A busca e a página do produto no ML pedem verificação de conta (testado em 02/10), e isso não
é contornado. O que abre normalmente é a página de afiliado de cada link `meli.la`. Então a vigia
usa os links que os grupos já postaram para cada produto da lista top: o card em destaque mostra
o preço e o vendedor de AGORA daquele anúncio. Cada página ainda traz ~20 recomendações, que
viram preço real no histórico.
"""
from __future__ import annotations

import time

from .banco import registrar_avaliacao
from .identidade import Identificador
from .parser import Promo
from .score import para_dict

LINKS_POR_PRODUTO = 3
DIAS_DE_LINKS = 14


MAIS_POSTADOS = 60        # além da lista top: os perfumes mais postados nos grupos nos últimos 14 dias
REGRAVAR_S = 6 * 3600     # mesma oferta, mesmo preço e nota: só regrava no painel a cada 6h
# 16/10 (Breno: "tinha que expandir o leque de produtos"; "faz a 1 e a 2"): a vigia acompanha também o que a EQUIPE
# postou no GrupoFinal (tabela postagens). Em 14 dias, 465 produtos com link; 313 nunca foram pelo pipeline na semana; 160
# foram postados 2+ vezes (já provaram que vendem). Aos poucos, para não pesar no ML (a curadora abre ~280 páginas/h):
# 20 por rodada de 10 min, em rodízio (uma volta a cada ~80 min), só das 14h às 23h (enche a fila do turno).
GRUPOFINAL_DIAS = 14
GRUPOFINAL_MIN_POSTS = 2
GRUPOFINAL_POR_RODADA = 20
GRUPOFINAL_HORAS = (14, 23)


class Vigia:
    def __init__(self, curadora, alertador, nomes: list[str] | None = None,
                 links_por_produto: int = LINKS_POR_PRODUTO, relogio=time.time, mais_postados: int = MAIS_POSTADOS,
                 grupofinal_por_rodada: int = GRUPOFINAL_POR_RODADA, grupofinal_horas: tuple[int, int] = GRUPOFINAL_HORAS):
        self.cur = curadora
        self.con = curadora.con
        self.alertador = alertador
        self.nomes = nomes if nomes is not None else list(curadora.cfg.get("top_vendidos", []))
        self.links_por_produto = links_por_produto
        self.relogio = relogio
        self.mais_postados = mais_postados
        self._gravado: dict[str, tuple[float, str, float]] = {}  # link -> (preço, nota, quando)
        self.grupofinal_por_rodada = grupofinal_por_rodada
        self.grupofinal_horas = grupofinal_horas
        self._vez_grupofinal = 0   # rodízio: de onde a próxima fatia do GrupoFinal começa

    def alvos_mais_postados(self) -> dict[str, list[str]]:
        """Os perfumes que os grupos mais postaram nos últimos 14 dias (fora a lista top), 1 link cada.

        É o "radar maior" sem depender da API: pega queda de preço de muito mais produtos.
        """
        if not self.mais_postados:
            return {}
        agora = int(self.relogio())
        tokens_top = [self.cur.ident.analisar(n).tokens for n in self.nomes]
        resultado: dict[str, list[str]] = {}
        for r in self.con.execute(
                """SELECT chave, base, COUNT(*) n,
                          (SELECT o2.link FROM ofertas o2 WHERE o2.chave = o.chave AND o2.marketplace='ml'
                             AND o2.link != '' ORDER BY o2.ts DESC LIMIT 1) link,
                          (SELECT o3.produto FROM ofertas o3 WHERE o3.chave = o.chave ORDER BY o3.ts DESC LIMIT 1) produto
                   FROM ofertas o
                   WHERE ts >= ? AND marketplace='ml' AND base NOT LIKE 'kit:%' AND base != '' AND chave NOT LIKE '%|?%'
                   GROUP BY chave ORDER BY n DESC LIMIT ?""",
                (agora - DIAS_DE_LINKS * 86400, self.mais_postados + len(self.nomes) * 3)):
            tokens = frozenset(t for t in r["base"].split("-") if t)
            if not r["link"] or any(tokens == t for t in tokens_top):
                continue  # a lista top já é vigiada com mais links
            resultado[r["produto"]] = [r["link"]]
            if len(resultado) >= self.mais_postados:
                break
        return resultado

    def alvos_do_grupofinal(self, ja: set[str]) -> dict[str, list[str]]:
        """O que a equipe postou no GrupoFinal 2+ vezes em 14 dias, 1 link cada (o do post mais recente), fora os links que
        já estão nos outros alvos; uma fatia de 20 por rodada, em rodízio, só no horário (ver GRUPOFINAL_POR_RODADA)."""
        hora = time.localtime(self.relogio()).tm_hour
        if not self.grupofinal_por_rodada or not (self.grupofinal_horas[0] <= hora < self.grupofinal_horas[1]):
            return {}
        agora = int(self.relogio())
        candidatos = []
        for r in self.con.execute(
                """SELECT chave, COUNT(*) n, MAX(ts) ultimo,
                          (SELECT p2.link FROM postagens p2 WHERE p2.chave = p.chave AND p2.link LIKE '%meli.la/%'
                             ORDER BY p2.ts DESC LIMIT 1) link,
                          (SELECT p3.produto FROM postagens p3 WHERE p3.chave = p.chave ORDER BY p3.ts DESC LIMIT 1) produto
                   FROM postagens p
                   WHERE ts >= ? AND chave IS NOT NULL AND chave != '' AND chave NOT LIKE '%|?%'
                   GROUP BY chave HAVING n >= ? ORDER BY n DESC, ultimo DESC""",
                (agora - GRUPOFINAL_DIAS * 86400, GRUPOFINAL_MIN_POSTS)):
            if r["link"] and r["produto"] and r["link"] not in ja:
                candidatos.append((r["produto"], r["link"]))
        if not candidatos:
            return {}
        inicio = self._vez_grupofinal % len(candidatos)
        fatia = (candidatos[inicio:] + candidatos[:inicio])[:self.grupofinal_por_rodada]
        self._vez_grupofinal = (inicio + len(fatia)) % len(candidatos)
        return {nome: [link] for nome, link in fatia}

    def _vale_gravar(self, link: str, preco: float, nota: str, agora: float) -> bool:
        anterior = self._gravado.get(link)
        if anterior and anterior[0] == preco and anterior[1] == nota and agora - anterior[2] < REGRAVAR_S:
            return False
        self._gravado[link] = (preco, nota, agora)
        return True

    def alvos(self) -> dict[str, list[str]]:
        """Para cada produto vigiado, os links ML mais recentes, de vendedores diferentes quando possível."""
        agora = int(self.relogio())
        tokens_alvo = {nome: self.cur.ident.analisar(nome).tokens for nome in self.nomes}
        resultado: dict[str, list[str]] = {nome: [] for nome in self.nomes}
        vendedores: dict[str, set] = {nome: set() for nome in self.nomes}
        linhas = self.con.execute(
            """SELECT base, link, vendedor FROM ofertas
               WHERE marketplace='ml' AND link != '' AND ts >= ? AND base NOT LIKE 'kit:%'
               ORDER BY ts DESC""",
            (agora - DIAS_DE_LINKS * 86400,),
        )
        for r in linhas:
            tokens = frozenset(t for t in r["base"].split("-") if t)
            for nome, alvo in tokens_alvo.items():
                lista = resultado[nome]
                if alvo and tokens == alvo and len(lista) < self.links_por_produto and r["link"] not in lista:
                    vend = (r["vendedor"] or "").lower()
                    if vend and vend in vendedores[nome] and len(lista) < self.links_por_produto - 1:
                        continue  # prefere variar o vendedor
                    lista.append(r["link"])
                    vendedores[nome].add(vend)
        return resultado

    def rodada(self) -> list:
        """Confere todos os alvos uma vez. Devolve as avaliações que viraram alerta."""
        alertadas = []
        if self.cur.ml is None:
            return alertadas
        alvos = self.alvos()
        alvos.update(self.alvos_mais_postados())
        ja = {link for links in alvos.values() for link in links}
        for nome, links in self.alvos_do_grupofinal(ja).items():
            alvos.setdefault(nome, links)   # mesmo nome já vigiado: fica o que já estava
        for nome, links in alvos.items():
            for link in links:
                agora = int(self.relogio())
                card = self.cur.ml.consultar(link, agora)
                if not card.ok or not card.preco:
                    continue
                # O link pode ter virado outro produto (ou o grupo errou o nome): confere com o nome da lista,
                # inclusive o gênero ("Attracione Men" não é o Attracione Feminino).
                if Identificador.conflito(self.cur.ident.analisar(nome), self.cur.ident.analisar(card.titulo)):
                    continue
                promo = Promo(texto=f"{card.titulo}\nPor R$ {card.preco}\n{link}", produto=card.titulo,
                              preco=float(card.preco), link=link, marketplace="ml", vendedor=card.vendedor,
                              loja_oficial=card.loja_oficial)
                av = self.cur.avaliar(promo, agora=agora, conferir_ml=True)
                if av is None or av.conflito:
                    continue
                av.grupo = f"vigia: {nome}"
                alertou = self.alertador.talvez_alertar(av, "vigia do ML", agora)
                # Sem mudança de preço ou nota, não enche o banco: o painel já mostra essa oferta.
                if alertou or self._vale_gravar(link, av.preco_efetivo, av.nota, agora):
                    registrar_avaliacao(self.con, agora, "vigia do ML", av.grupo, av.identidade.chave, av.nota,
                                        av.score, alertou, para_dict(av))
                if alertou:
                    alertadas.append(av)
        return alertadas

    def rodar(self, intervalo_s: float = 600, parar=lambda: False) -> None:
        while not parar():
            inicio = time.monotonic()
            try:
                self.rodada()
            except Exception as exc:
                print(f"[vigia] erro: {type(exc).__name__}: {exc}", flush=True)
            restante = intervalo_s - (time.monotonic() - inicio)
            while restante > 0 and not parar():
                time.sleep(min(5.0, restante))
                restante -= 5.0
