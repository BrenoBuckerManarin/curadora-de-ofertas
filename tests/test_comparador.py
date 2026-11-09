"""Comparador de ofertas em modo sombra (04/11): só observa e registra; nada de escolha, nota ou envio muda."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

from apoio import CFG

from curadora import comparador as c
from curadora.banco import conectar
from curadora.identidade import Identificador
from curadora.ml import ESQUEMA_CACHE, CardML
from curadora.ml_api import OfertaML

AGORA = 1_793_800_000


def oferta(item, preco, seller, oficial=False, internacional=False):
    return OfertaML(item_id=item, preco=preco, preco_anterior=None, seller_id=seller, loja_oficial=oficial,
                    frete_gratis=True, internacional=internacional)


def rep(vendas, classe="boa", pais="BR", nome="Loja"):
    return {"nome": nome, "vendas": vendas, "classe": classe, "pais": pais, "nivel": "5_green", "medalha": ""}


class TestComparar(unittest.TestCase):
    """A conta, sem rede: o mesmo retrato do catálogo, preço da API com preço da API."""

    def setUp(self):
        self.reps = {1: rep(100000, nome="LIPX"), 2: rep(80000, nome="OUTRA OFICIAL"), 3: rep(500, "fraca"),
                     4: rep(20000, nome="REVENDA BOA"), 5: rep(800, nome="OFICIAL SO NA API"), 6: rep(90000, pais="CN")}

    def test_oficial_mais_barata_por_2_por_cento_e_2_reais_e_prioridade(self):
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True), oferta("MLB901", 180, 2, True)], self.reps, [])
        self.assertEqual((r["status"], r["tipo"], r["diferenca"], r["diferenca_pct"], r["prioridade"]),
                         (c.MELHOR_NA_API, "oficial x oficial", 20, 0.1, True))

    def test_diferenca_menor_fica_registrada_sem_prioridade(self):
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True), oferta("MLB901", 198, 2, True)], self.reps, [])
        self.assertEqual((r["status"], r["diferenca"], r["prioridade"]), (c.DIFERENCA_PEQUENA, 2.0, False))  # 1%
        r = c.comparar("MLB900", [oferta("MLB900", 60, 1, True), oferta("MLB901", 58.5, 2, True)], self.reps, [])
        self.assertEqual(r["status"], c.DIFERENCA_PEQUENA)                                                   # 2,5%, R$1,50

    def test_enviada_ja_era_a_menor(self):
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True), oferta("MLB901", 210, 2, True)], self.reps, [])
        self.assertEqual((r["status"], r["diferenca"]), (c.ENVIADA_MENOR, -10))

    def test_descartes_com_motivo_e_sem_selo_a_parte(self):
        ofertas = [oferta("MLB900", 200, 1, True), oferta("MLB905", 120, 5, True), oferta("MLB903", 130, 3),
                   oferta("MLB906", 140, 6, True), oferta("MLB907", 145, 7, True, internacional=True),
                   oferta("MLB904", 170, 4), oferta("MLB908", 175, 1)]
        r = c.comparar("MLB900", ofertas, self.reps, [])
        motivos = {d["item_id"]: d["motivo"] for d in r["descartes"]}
        self.assertIn("800 vendas", motivos["MLB905"])                    # oficial só na API: não vale como oficial
        self.assertIn("vendedor fraco", motivos["MLB903"])
        self.assertEqual(motivos["MLB906"], "compra internacional")       # país do vendedor
        self.assertEqual(motivos["MLB907"], "compra internacional")       # marca do ML
        self.assertEqual(motivos["MLB908"], "outro anúncio do mesmo vendedor")
        self.assertEqual((r["status"], r["motivo"]), (c.SEM_ALTERNATIVA, "nenhuma outra loja oficial válida no catálogo"))
        self.assertEqual((r["sem_selo_mais_barata"]["item_id"], r["sem_selo_mais_barata"]["diferenca"]), ("MLB904", 30))

    def test_sem_selo_mais_cara_sem_reputacao_nao_e_descarte(self):
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True), oferta("MLB910", 230, 99), oferta("MLB911", 120, 98)],
                       self.reps, [])
        self.assertEqual(r["sem_selo_mais_caras"], 1)                   # mais cara e sem selo: não disputa
        self.assertEqual([d["item_id"] for d in r["descartes"]], ["MLB911"])   # mais barata sem reputação: descarte
        self.assertEqual(r["descartes"][0]["motivo"], "sem a reputação do vendedor")

    def test_sem_selo_enviada_e_oficial_mais_barata(self):
        r = c.comparar("MLB904", [oferta("MLB904", 200, 4), oferta("MLB901", 190, 2, True)], self.reps, [])
        self.assertEqual((r["tipo"], r["status"], r["oficial_enviado"]), ("sem selo -> oficial", c.MELHOR_NA_API, False))

    def test_cupom_incerto_fica_a_parte(self):
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True), oferta("MLB901", 180, 2, True)], self.reps, ["MODA10"])
        self.assertEqual((r["status"], r["status_sem_cupom"], r["prioridade"]), (c.CUPOM_INCERTO, c.MELHOR_NA_API, False))
        self.assertIn("MODA10", r["motivo"])

    def test_anuncio_enviado_fora_do_catalogo_e_vendedor_unico(self):
        self.assertEqual(c.comparar("MLB999", [oferta("MLB900", 200, 1, True)], self.reps, [])["status"], c.INCONCLUSIVO)
        r = c.comparar("MLB900", [oferta("MLB900", 200, 1, True)], self.reps, [])
        self.assertEqual((r["status"], r["motivo"]), (c.SEM_ALTERNATIVA, "só o vendedor enviado no catálogo"))


class TestPaginas(unittest.TestCase):
    """As duas páginas abertas em seguida: o anúncio certo, o selo, o mesmo produto, Pix com Pix."""

    def setUp(self):
        self.ident = Identificador(CFG["marcas"], CFG.get("sinonimos"))
        self.env = CardML(ok=True, item_id="MLB900", titulo="Lattafa Asad 100ml", preco=199, preco_outros=210,
                          loja_oficial=True, vendedor="Lipx")
        self.alt = CardML(ok=True, item_id="MLB901", titulo="Perfume Lattafa Asad 100ml", preco=179,
                          preco_outros=189, loja_oficial=True, vendedor="Outra", preco_cupom=160)

    def test_confirmada_pix_com_pix_e_cupom_da_pagina_a_parte(self):
        p = c.conferir_paginas(self.env, self.alt, "MLB901", self.ident)
        self.assertEqual((p["status"], p["diferenca_pix"], p["diferenca_outros"]), (c.MELHOR_CONFIRMADA, 20, 21))
        self.assertEqual(p["cupom_da_pagina_melhor"], 160)          # guardado, não usado na conta

    def test_o_que_a_pagina_desmente(self):
        casos = {"outro anúncio": CardML(**{**asdict(self.alt), "item_id": "MLB777"}),
                 "selo": CardML(**{**asdict(self.alt), "loja_oficial": False}),
                 "volume diferente": CardML(**{**asdict(self.alt), "titulo": "Lattafa Asad 50ml"}),
                 "diferença no Pix": CardML(**{**asdict(self.alt), "preco": 198})}
        for trecho, alt in casos.items():
            p = c.conferir_paginas(self.env, alt, "MLB901", self.ident)
            self.assertEqual(p["status"], c.NAO_CONFIRMADA, trecho)
            self.assertIn(trecho, p["motivo"])

    def test_pagina_que_nao_abriu_nao_vira_erro_da_api(self):
        p = c.conferir_paginas(CardML(ok=False, erro="o ML pediu verificação de conta"), self.alt, "MLB901", self.ident)
        self.assertEqual(p["status"], c.MELHOR_NA_API)
        # a página do enviado com outra loja em destaque: sem conferência, não é erro da API
        p = c.conferir_paginas(self.env, self.alt, "MLB901", self.ident, item_env="MLB555")
        self.assertEqual((p["status"], p["motivo"]), (c.MELHOR_NA_API, "a página do anúncio enviado mostrou outra loja"))


class TestOrcamentoETrava(unittest.TestCase):
    def setUp(self):
        self.con = c.abrir(":memory:")
        self.t = AGORA
        self.orc = c.Orcamento(self.con, relogio=lambda: self.t, limites={"api": (2, 3), "pagina": (1, 1), "gerador": (1, 1)})

    def test_teto_por_hora_e_por_dia(self):
        for _ in range(2):
            self.assertTrue(self.orc.pode("api"))
            self.orc.registrar("api", "ok")
        self.assertFalse(self.orc.pode("api"))                   # 2 por hora
        self.t += 3601
        self.assertTrue(self.orc.pode("api"))
        self.orc.registrar("api", "ok")
        self.assertEqual(c._inicio_do_dia(self.t), c._inicio_do_dia(AGORA))
        self.assertFalse(self.orc.pode("api"))                   # 3 no dia

    def test_429_pausa_30_min_e_3_seguidos_param_o_dia(self):
        self.orc.registrar("api", "429")
        self.assertFalse(self.orc.pode("pagina"))
        self.t += c.PAUSA_S + 1
        self.assertTrue(self.orc.pode("pagina"))
        self.orc.registrar("pagina", "verificacao")
        self.orc.registrar("pagina", "verificacao")
        self.assertEqual(self.orc.pausado_ate(), c._inicio_do_dia(self.t) + 86400)
        self.orc.registrar("api", "ok")
        self.assertEqual(c.ler_estado(self.con, "falhas_seguidas"), "0")

    def test_uma_execucao_por_vez(self):
        pasta = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pasta, True)
        a, b = c.Trava(pasta / "comparador.lock"), c.Trava(pasta / "comparador.lock")
        self.assertTrue(a.pegar())
        self.assertFalse(b.pegar())
        a.soltar()
        self.assertTrue(b.pegar())
        b.soltar()


class APIFalsa:
    def __init__(self, respostas):
        self.respostas, self.chamadas, self.erro = respostas, [], None

    def get(self, caminho):
        self.chamadas.append(caminho)
        if self.erro:
            raise self.erro
        return self.respostas.get(caminho, {})


class PaginasFalsas:
    def __init__(self, cards):
        self.cards, self.abertas = cards, []

    def consultar(self, link, agora=None):
        self.abertas.append(link)
        return self.cards.get(link, CardML(ok=False, erro="não abriu"))


def usuario(nome, vendas, pais="BR"):
    return {"nickname": nome, "country_id": pais,
            "seller_reputation": {"level_id": "5_green", "power_seller_status": "platinum",
                                  "transactions": {"total": vendas}}}


class TestVolta(unittest.TestCase):
    """Uma volta inteira com API, páginas e gerador falsos."""

    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.pasta, True)
        self.t = AGORA
        self.con = c.abrir(":memory:")
        self.cur = conectar(":memory:")
        self.cur.executescript(ESQUEMA_CACHE)
        self.card(self.cur, "https://meli.la/env1", "MLB111", "MLB900", 199)
        self.cur.execute("INSERT INTO catalogo_ml (produto_id, chave, nome, origem, ts) VALUES ('MLB111', 'asad|100', "
                         "'Asad', 'destaque', ?)", (AGORA,))
        self.cur.commit()
        self.mudancas_antes = self.cur.total_changes
        self.registro = self.pasta / "pipeline.jsonl"
        self.api = APIFalsa({
            "/products/MLB111/items?limit=50": {"results": [
                {"item_id": "MLB900", "price": 200, "seller_id": 1, "official_store_id": 10},
                {"item_id": "MLB901", "price": 180, "seller_id": 2, "official_store_id": 20},
                {"item_id": "MLB902", "price": 150, "seller_id": 3}]},
            "/users/1": usuario("LIPX", 100000), "/users/2": usuario("OUTRA", 80000), "/users/3": usuario("FRACA", 500)})
        self.paginas = PaginasFalsas({
            "https://meli.la/env1": CardML(ok=True, item_id="MLB900", produto_id="MLB111", titulo="Lattafa Asad 100ml",
                                           preco=199, preco_outros=210, loja_oficial=True, vendedor="Lipx"),
            "https://meli.la/alt": CardML(ok=True, item_id="MLB901", produto_id="MLB111", titulo="Lattafa Asad 100ml",
                                          preco=179, preco_outros=189, loja_oficial=True, vendedor="Outra")})
        self.geradas = []
        self.comp = self.novo()

    def novo(self):
        def gerador(item, titulo):
            self.geradas.append(item)
            return "https://meli.la/alt"
        return c.Comparador(self.con, self.cur, self.api, self.paginas, gerador,
                            c.Orcamento(self.con, relogio=lambda: self.t), Identificador(CFG["marcas"], CFG.get("sinonimos")),
                            registro_pipeline=self.registro, relogio=lambda: self.t, dormir=lambda s: None, log=lambda *a: None)

    @staticmethod
    def card(con, link, produto, item, preco, oficial=True):
        dados = asdict(CardML(ok=True, item_id=item, produto_id=produto, titulo="Lattafa Asad 100ml", preco=preco,
                              loja_oficial=oficial, vendedor="Lipx"))
        con.execute("INSERT OR REPLACE INTO ml_cards (link, ts, dados) VALUES (?,?,?)",
                    (link, AGORA, json.dumps(dados, ensure_ascii=False)))

    def enviar(self, ts, **extra):
        linha = {"ts": ts, "status": "enviado", "chave": "asad|100", "produto": "Lattafa Asad 100ml",
                 "link_afiliado": "https://meli.la/env1", "preco_enviado": 199, **extra}
        with self.registro.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")

    def comparacoes(self):
        return self.con.execute("SELECT origem, status, motivo, economia_pix, persistiu FROM comparacoes ORDER BY id").fetchall()

    def test_envio_com_oficial_mais_barata_confirmada_nas_paginas(self):
        # um link de grupo antigo do mesmo anúncio não serve: a página de grupo troca a loja (ensaio de 04/11)
        self.card(self.cur, "https://meli.la/gc0EYs34sxTO", "MLB111", "MLB901", 181)
        self.cur.commit()
        self.mudancas_antes = self.cur.total_changes
        self.enviar(AGORA - 60)
        self.comp.rodada()
        (origem, status, motivo, economia, _), = self.comparacoes()
        self.assertEqual((origem, status, economia), ("envio", c.MELHOR_CONFIRMADA, 20))
        self.assertEqual(self.paginas.abertas, ["https://meli.la/env1", "https://meli.la/alt"])   # o enviado primeiro
        self.assertEqual(self.geradas, ["MLB901"])
        fontes = dict(self.con.execute("SELECT fonte, count(*) FROM observacoes GROUP BY fonte").fetchall())
        self.assertEqual(fontes, {"api": 3, "pagina": 2})
        consultas = dict(self.con.execute("SELECT tipo, count(*) FROM consultas GROUP BY tipo").fetchall())
        self.assertEqual(consultas, {"api": 4, "pagina": 2, "gerador": 1})     # catálogo + 3 vendedores
        self.assertEqual(self.cur.total_changes, self.mudancas_antes)        # o banco da curadora não muda
        # a mesma volta de novo não repete nada
        self.novo().rodada()
        self.assertEqual(len(self.comparacoes()), 1)
        self.assertEqual(len(self.api.chamadas), 4)

    def test_releitura_em_1_hora(self):
        self.enviar(AGORA - 60)
        self.comp.rodada()
        self.t += c.RELEITURA_S + 60
        self.api.respostas["/products/MLB111/items?limit=50"]["results"][1]["price"] = 199
        self.novo().rodada()
        self.assertEqual(self.comparacoes()[0][4], 0)                       # a diferença não se manteve

    def test_atrasada_nao_consulta_e_cupom_fica_a_parte(self):
        c.gravar_estado(self.con, "ultimo_envio", AGORA - 3600)             # a volta anterior foi há 1 h
        self.enviar(AGORA - 20 * 60)
        self.enviar(AGORA - 60, cupom_quente="MODA10")
        self.comp.rodada()
        status = [s for _, s, *_ in self.comparacoes()]
        self.assertEqual(status, [c.ATRASADA, c.CUPOM_INCERTO])
        self.assertEqual(self.paginas.abertas, [])                          # cupom: não abre página

    def test_orcamento_e_429_param_sem_perder_o_envio(self):
        self.enviar(AGORA - 60)
        self.api.erro = RuntimeError("ML 429 em /products/MLB111/items: too many requests")
        resumo = self.comp.rodada()
        self.assertTrue(any("429" in k for k in resumo))
        self.assertEqual(self.comparacoes(), [])                            # fica para a próxima volta
        self.assertGreater(self.comp.orc.pausado_ate(), AGORA)
        self.api.erro = None
        self.t += c.PAUSA_S + 1                       # depois da pausa o envio já passou dos 15 min: só registra
        self.novo().rodada()
        self.assertEqual(self.comparacoes()[0][1], c.ATRASADA)
        self.assertEqual(len(self.api.chamadas), 1)                          # nenhuma consulta depois do 429

    def test_amostra_do_grupofinal_so_pela_api(self):
        self.card(self.cur, "https://meli.la/h1", "MLB111", "MLB900", 199)
        self.cur.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, link, dados) VALUES "
                         "('h1', ?, 'GrupoFinal', 'Lattafa Asad 100ml', 'asad|100', 199, 'https://meli.la/h1', '{}')",
                         (AGORA - 60,))
        self.cur.commit()
        self.mudancas_antes = self.cur.total_changes
        self.comp.rodada()
        (origem, status, *_), = self.comparacoes()
        self.assertEqual((origem, status), ("grupofinal", c.MELHOR_NA_API))        # prioridade, mas sem abrir página
        self.assertEqual(self.paginas.abertas, [])

    def test_relatorio(self):
        self.enviar(AGORA - 60)
        self.comp.rodada()
        texto = c.relatorio(self.con, AGORA - 3600, AGORA + 3600)
        self.assertIn("confirmada nas páginas (Pix com Pix) | 1", texto)
        self.assertIn("Economia nas confirmadas: mediana R$ 20,00", texto)
        # 04/11 (Astra): a cobertura atual é dentro do catálogo consultado; válidas, inconclusivas e exemplos conferidos
        self.assertIn("Cobertura: só o catálogo consultado.", texto)
        self.assertIn("Comparações válidas (com pelo menos 1 outra loja oficial válida no catálogo): **1**. "
                      "Inconclusivas: **0**.", texto)
        self.assertIn("Exemplos conferidos nas páginas", texto)
        self.assertIn("enviado Pix R$ 199,00 (outros R$ 210,00) x Outra Pix R$ 179,00 (outros R$ 189,00)", texto)
        self.assertIn("https://meli.la/alt", texto)

    def test_acesso_da_api_so_le_o_token_emprestado(self):
        # 04/11 (Breno: "não contornar verificação do ML nem modificar a autenticação compartilhada")
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"CURADORA_ML_APP_COMPARTILHADO": str(self.pasta / "app.env")}):
            api, paginas, gerador, ident = c.dependencias_reais(self.con)
        self.assertEqual((api.emprestado, api.app_compartilhado), (c.TOKEN_EMPRESTADO, None))
        self.assertIs(paginas.con, self.con)                                # cache das páginas no banco do comparador
        self.assertIsNone(paginas.observador)                               # nada vai para o histórico da curadora

    def test_desligado_e_trava(self):
        from unittest import mock
        desligado = self.pasta / "comparador.desligado"
        desligado.write_text("", encoding="utf-8")
        with mock.patch.object(c, "DESLIGADO", desligado), mock.patch.object(c, "dependencias_reais") as dep:
            self.assertEqual(c.main(["--banco", str(self.pasta / "x.db")]), 0)
        dep.assert_not_called()
        trava = c.Trava(self.pasta / "comparador.lock")
        self.assertTrue(trava.pegar())
        with mock.patch.object(c, "DESLIGADO", self.pasta / "nao_existe"), \
                mock.patch.object(c, "TRAVA", self.pasta / "comparador.lock"), \
                mock.patch.object(c, "dependencias_reais") as dep:
            self.assertEqual(c.main(["--banco", str(self.pasta / "x.db")]), 0)
        dep.assert_not_called()                                             # outra execução em andamento
        trava.soltar()


if __name__ == "__main__":
    unittest.main()
