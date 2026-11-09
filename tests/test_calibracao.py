"""Calibração pelos votos: encontra o ajuste que concorda mais com o Breno, sem inventar."""
from __future__ import annotations

import unittest

from apoio import CFG

from curadora.calibracao import Exemplo, aplicar, calibrar
from curadora.score import nota_do_score


def ex(preco, relevancia, loja, boa, pct=15.0):
    return Exemplo("x", {"preco": preco, "relevancia": relevancia, "loja": loja, "sinais_ml": 0, "desconto": 0},
                   {"pct_abaixo": pct}, boa, "")


class TestCalibracao(unittest.TestCase):
    def test_poucos_votos_nao_calibra(self):
        self.assertIsNone(calibrar([ex(40, 0, 0, True)] * 5, CFG))

    def test_quem_vota_valorizando_loja_puxa_o_peso_da_loja(self):
        # O Breno aprova promos medianas em loja boa e reprova as mesmas sem loja.
        votos = [ex(40, 0, 15, True) for _ in range(12)] + [ex(58, 0, 0, False) for _ in range(12)]
        r = calibrar(votos, CFG)
        self.assertGreater(r.concordancia_melhor, r.concordancia_atual)
        self.assertGreater(r.fatores["loja"], 1.0)

    def test_quando_ja_concorda_nao_mexe(self):
        votos = [ex(55, 18, 15, True) for _ in range(12)] + [ex(15, 0, 0, False) for _ in range(12)]
        r = calibrar(votos, CFG)
        self.assertAlmostEqual(r.concordancia_atual, 1.0)
        self.assertEqual((r.fatores, r.limite_boa), ({"relevancia": 1.0, "loja": 1.0, "sinais_ml": 1.0}, 60))

    def test_respeita_o_teto_do_desconto(self):
        # Desconto de 2%: mesmo com muita loja, nunca vira BOA, então 👍 aí não convence a calibração.
        votos = [ex(10, 18, 15, True, pct=2) for _ in range(12)] + [ex(15, 0, 0, False) for _ in range(12)]
        r = calibrar(votos, CFG)
        self.assertLess(r.concordancia_melhor, 1.0)

    def test_aplicar_ajusta_pesos_e_limites(self):
        votos = [ex(40, 0, 15, True) for _ in range(12)] + [ex(58, 0, 0, False) for _ in range(12)]
        r = calibrar(votos, CFG)
        novo = aplicar(CFG, r)
        self.assertNotEqual(novo["pesos"]["loja_top"], CFG["pesos"]["loja_top"])
        self.assertEqual(novo["limites_nota"]["BOA"], r.limite_boa)
        self.assertEqual(CFG["pesos"]["loja_top"], 20)  # o config original não foi alterado
        self.assertEqual(nota_do_score(r.limite_boa, novo), "BOA")


if __name__ == "__main__":
    unittest.main()
