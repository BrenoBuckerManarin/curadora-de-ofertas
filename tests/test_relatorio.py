"""Relatório semanal: seções com dados de verdade e envio para o grupo configurado."""
from __future__ import annotations

import unittest

from apoio import CFG, DIA, T0, banco_com_historico

from curadora.alertas import SaidaWhatsApp
from curadora.banco import registrar_avaliacao
from curadora.postagens import registrar_postagem
from curadora.relatorio import montar
from curadora.score import Curadora, para_dict

AGORA = T0 + 40 * DIA
# Asad: ~R$200 no mês anterior, ~R$170 nesta semana (caiu); Yara estável.
HIST = ([("Lattafa Asad 100ml", 200 + d % 5, d) for d in range(5, 33)] +
        [("Lattafa Asad 100ml", 170 + d % 3, d) for d in range(34, 40)] +
        [("Lattafa Yara 100ml", 130 + d % 4, d) for d in range(5, 40)])


class TestRelatorio(unittest.TestCase):
    def setUp(self):
        self.con = banco_com_historico(HIST)
        self.cur = Curadora(CFG, self.con)

    def avaliar(self, texto: str, ts: int):
        av = self.cur.avaliar(texto, agora=ts, conferir_ml=False)
        registrar_avaliacao(self.con, ts, "ao vivo", "G", av.identidade.chave, av.nota, av.score, False, para_dict(av))
        return av

    def test_secoes_principais(self):
        av = self.avaliar("*Lattafa Asad 100ml*\nPor R$ 120\nhttps://meli.la/a", AGORA - DIA)
        texto = montar(self.con, CFG, agora=AGORA)
        self.assertIn("Relatório da semana", texto)
        self.assertIn("O que mais caiu", texto)
        self.assertIn("Lattafa Asad", texto.split("O que mais caiu")[1])
        self.assertIn("Top vendidos", texto)
        if av.nota in ("IMPERDÍVEL", "BOA"):
            self.assertIn("Melhores promos da semana", texto)

    def test_excelente_sem_post_aparece_e_postada_nao(self):
        registrar_postagem(self.con, self.cur, "*Lattafa Yara 100ml*\nPor R$ 99", AGORA - 3 * DIA, "h0", "GrupoFinal")
        av = self.avaliar("*Lattafa Asad 100ml*\nPor R$ 140\nhttps://meli.la/a", AGORA - DIA)
        self.assertEqual(av.nota, "IMPERDÍVEL")
        texto = montar(self.con, CFG, agora=AGORA)
        self.assertIn("Lattafa Asad", texto.split("passaram sem post")[1])
        registrar_postagem(self.con, self.cur, "*Lattafa Asad EDP 100ml*\nPor R$ 141", AGORA - DIA + 600, "h1", "GrupoFinal")
        texto = montar(self.con, CFG, agora=AGORA)
        self.assertNotIn("Lattafa Asad", texto.split("passaram sem post")[1])

    def test_envio_vai_para_o_grupo_sem_filtro_de_nota(self):
        enviados = []
        saida = SaidaWhatsApp("120363000@g.us", ["IMPERDÍVEL"], http=lambda url, p: enviados.append(p) or {"success": True})
        saida.enviar_texto("relatório")
        self.assertEqual(enviados, [{"chatId": "120363000@g.us", "message": "relatório"}])


if __name__ == "__main__":
    unittest.main()
