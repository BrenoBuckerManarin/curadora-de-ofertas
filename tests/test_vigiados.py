"""Alerta por produto: "me avisa quando o Asad 100ml sair por até R$150"."""
from __future__ import annotations

import unittest

from apoio import CFG, DIA, T0, banco_com_historico

from curadora import vigiados
from curadora.alertas import Alertador
from curadora.score import Curadora

ASAD = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197])]
AGORA = T0 + 15 * DIA


class TestAlertaPorProduto(unittest.TestCase):
    def setUp(self):
        self.con = banco_com_historico(ASAD + [("Lattafa Asad Bourbon 100ml", 260, 1)])
        self.cur = Curadora(CFG, self.con)
        self.enviados: list[tuple[str, dict]] = []
        self.alertador = Alertador(self.con, [lambda t, d: self.enviados.append((t, d))])

    def chega(self, texto: str, ts: int = AGORA):
        av = self.cur.avaliar(texto, agora=ts, conferir_ml=False)
        self.alertador.talvez_alertar(av, "ao vivo", ts)
        return av

    def alvos(self):
        return [t for t, d in self.enviados if d.get("forcar")]

    def test_avisa_no_preco_alvo_mesmo_sem_ser_excelente(self):
        r = vigiados.adicionar(self.con, self.cur.ident, "Lattafa Asad 100ml", "190", "breno")
        self.assertTrue(r["ok"])
        self.chega("*Lattafa Asad 100ml*\nPor R$ 195\nhttps://meli.la/a")          # acima do alvo
        self.assertEqual(self.alvos(), [])
        av = self.chega("*Asad Lattafa EDP 100ml*\nPor R$ 189\nhttps://meli.la/b", AGORA + 60)
        self.assertNotEqual(av.nota, "IMPERDÍVEL")                                   # nota comum...
        self.assertEqual(len(self.alvos()), 1)                                       # ...mas o alvo avisa
        self.assertIn("ALVO ATINGIDO", self.alvos()[0])
        self.assertIn("breno", self.alvos()[0])

    def test_nao_repete_e_so_volta_se_cair_mais(self):
        vigiados.adicionar(self.con, self.cur.ident, "Lattafa Asad", "190")       # sem volume: qualquer tamanho
        self.chega("*Lattafa Asad 100ml*\nPor R$ 185\nhttps://meli.la/a")
        self.chega("*Lattafa Asad 100ml*\nPor R$ 184\nhttps://meli.la/b", AGORA + 60)   # mesma notícia
        self.assertEqual(len(self.alvos()), 1)
        self.chega("*Lattafa Asad 100ml*\nPor R$ 170\nhttps://meli.la/c", AGORA + 120)  # caiu mais 3%
        self.assertEqual(len(self.alvos()), 2)

    def test_outro_produto_ou_volume_nao_avisa(self):
        vigiados.adicionar(self.con, self.cur.ident, "Lattafa Asad 100ml", "300")
        self.chega("*Lattafa Asad Bourbon 100ml*\nPor R$ 250\nhttps://meli.la/a")  # flanker
        self.chega("*Lattafa Asad 30ml*\nPor R$ 80\nhttps://meli.la/b")            # outro volume
        self.assertEqual(self.alvos(), [])

    def test_removido_para_de_avisar_e_entrada_invalida(self):
        r = vigiados.adicionar(self.con, self.cur.ident, "Lattafa Asad 100ml", "190")
        vigiados.remover(self.con, r["id"])
        self.chega("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a")
        self.assertEqual(self.alvos(), [])
        self.assertIn("erro", vigiados.adicionar(self.con, self.cur.ident, "Lattafa", "100"))
        self.assertIn("erro", vigiados.adicionar(self.con, self.cur.ident, "Lattafa Asad", "abc"))


if __name__ == "__main__":
    unittest.main()
