"""Leitura do card em destaque do ML (com cards reais salvos) e o cache das consultas."""
from __future__ import annotations

import unittest

from apoio import T0, html_com_card

from curadora.banco import conectar
from curadora.ml import ConsultorML, ler_card, ler_recomendacoes


class TestLerCard(unittest.TestCase):
    def test_card_com_loja_oficial(self):
        c = ler_card(html_com_card("ml_card_loja_oficial.json"))
        self.assertTrue(c.ok)
        self.assertEqual(c.titulo, "Perfume Mawwal Symphony SAZ Original Masculino EDP 100ml")
        self.assertEqual(c.vendedor, "WATHIQ PARFUMS")
        self.assertTrue(c.loja_oficial)
        self.assertEqual((c.preco, c.preco_outros, c.preco_anterior), (228.05, 232.7, 459.9))
        self.assertTrue(c.full)
        self.assertTrue(c.mais_vendido)
        self.assertEqual((c.item_id, c.produto_id), ("MLB4614426137", "MLB53361124"))
        self.assertEqual(c.foto, "https://http2.mlstatic.com/D_Q_NP_602021-MLA97983986989_112025-O.webp")

    def test_card_sem_vendedor(self):
        c = ler_card(html_com_card("ml_card_sem_vendedor.json"))
        self.assertTrue(c.ok)
        self.assertEqual(c.vendedor, "")
        self.assertFalse(c.loja_oficial)
        self.assertEqual(c.preco, 177.65)

    def test_recomendacoes_da_pagina(self):
        recs = ler_recomendacoes(html_com_card("ml_card_loja_oficial.json", "ml_recomendacoes.json"))
        self.assertEqual(len(recs), 6)
        athar = next(r for r in recs if "Athar" in r.titulo)
        self.assertEqual(athar.vendedor, "WATHIQ PARFUMS")
        self.assertTrue(athar.loja_oficial)
        self.assertTrue(all(r.preco for r in recs))

    def test_pagina_sem_card(self):
        c = ler_card("<html>página de login</html>")
        self.assertFalse(c.ok)
        self.assertIn("não encontrado", c.erro)

    def test_card_quebrado(self):
        self.assertFalse(ler_card('<script>{"id":"card-featured","type":').ok)


class TestConsultor(unittest.TestCase):
    def setUp(self):
        self.chamadas: list[str] = []

        def buscar(link):
            self.chamadas.append(link)
            return html_com_card("ml_card_loja_oficial.json")
        self.ml = ConsultorML(conectar(":memory:"), buscar_html=buscar, ttl=1800)

    def test_cache_dentro_do_prazo(self):
        self.ml.consultar("https://meli.la/a", agora=T0)
        card = self.ml.consultar("https://meli.la/a", agora=T0 + 600)
        self.assertEqual(len(self.chamadas), 1)
        self.assertEqual(card.vendedor, "WATHIQ PARFUMS")

    def test_cache_vencido_consulta_de_novo(self):
        self.ml.consultar("https://meli.la/a", agora=T0)
        self.ml.consultar("https://meli.la/a", agora=T0 + 3600)
        self.assertEqual(len(self.chamadas), 2)

    def test_link_que_nao_e_do_ml_nao_consulta(self):
        self.assertFalse(self.ml.consultar("https://amzn.to/x").ok)
        self.assertEqual(self.chamadas, [])

    def test_observador_recebe_destaque_e_recomendacoes(self):
        vistos = []
        ml = ConsultorML(conectar(":memory:"),
                         buscar_html=lambda l: html_com_card("ml_card_loja_oficial.json", "ml_recomendacoes.json"))
        ml.observador = lambda card, fonte, ts: vistos.append(fonte)
        ml.consultar("https://meli.la/a", agora=T0)
        self.assertEqual(vistos.count("destaque"), 1)
        self.assertEqual(vistos.count("recomendacao"), 6)

    def test_falha_de_rede_vira_card_com_erro(self):
        def falha(link):
            raise TimeoutError()
        ml = ConsultorML(conectar(":memory:"), buscar_html=falha)
        card = ml.consultar("https://meli.la/b", agora=T0)
        self.assertFalse(card.ok)
        self.assertIn("TimeoutError", card.erro)


if __name__ == "__main__":
    unittest.main()
