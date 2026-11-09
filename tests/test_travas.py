"""Por que o pipeline não manda uma promo (21/10, Breno: "sim pros 2"): a regra do vendedor e os motivos gravados."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from curadora import travas


class TestVendedor(unittest.TestCase):
    def test_loja_oficial_e_vendedor_grande_passam(self):
        self.assertEqual(travas.vendedor_fraco(True, {"vendas": 10}), "")
        self.assertEqual(travas.vendedor_fraco(False, {"vendas": 26281, "classe": "otima"}), "")

    def test_poucas_vendas_reputacao_fraca_e_sem_dado_nao_passam(self):
        self.assertIn("1163 vendas", travas.vendedor_fraco(False, {"vendas": 1163, "classe": "boa"}))
        self.assertIn("vendedor fraco", travas.vendedor_fraco(False, {"vendas": 9000, "classe": "fraca"}))
        self.assertIn("sem número de vendas", travas.vendedor_fraco(False, None))

    def test_corte_de_10_mil_para_vendedor_comum(self):
        # 28/10 (Breno): vendedor sem loja oficial só com 10 mil+ vendas; loja oficial continua passando
        self.assertIn("6400 vendas", travas.vendedor_fraco(False, {"vendas": 6400, "classe": "boa"}))
        self.assertEqual(travas.vendedor_fraco(False, {"vendas": 10000, "classe": "boa"}), "")
        self.assertEqual(travas.vendedor_fraco(True, {"vendas": 5600, "classe": "boa"}), "")


class TestMotivosGravados(unittest.TestCase):
    def test_vale_2h_e_some_depois(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "pipeline_travas.json"
            agora = time.time()
            travas.guardar(caminho, "asad|100", "sem o cupom 40SITETODO não vale", 149.0, agora - 3 * 3600)
            travas.guardar(caminho, "yara|100", "não abriu no ML", 160.0, agora - 600)
            self.assertEqual(list(travas.recentes(caminho, agora)), ["yara|100"])
            self.assertEqual(travas.recentes(caminho, agora)["yara|100"]["preco"], 160.0)

    def test_arquivo_ausente_ou_estragado_nao_quebra(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "pipeline_travas.json"
            self.assertEqual(travas.recentes(caminho), {})
            caminho.write_text("{quebrado", encoding="utf-8")
            self.assertEqual(travas.recentes(caminho), {})
            travas.guardar(caminho, "asad|100", "não abriu no ML", 149.0)   # regrava por cima
            self.assertIn("asad|100", travas.recentes(caminho))


class TestCurto(unittest.TestCase):
    def test_motivos_do_pipeline_em_poucas_palavras(self):
        self.assertEqual(travas.curto("sem o cupom 40SITETODO não vale"), "depende do cupom 40SITETODO")
        self.assertEqual(travas.curto("vendedor sem loja oficial com 1163 vendas (mínimo 5000)"),
                         "vendedor com 1,2 mil vendas")
        self.assertEqual(travas.curto("vendedor sem loja oficial com 697 vendas (mínimo 5000)"), "vendedor com 697 vendas")
        self.assertEqual(travas.curto("já foi há menos de 8h a R$134.10 e agora está R$134.10"), "já foi hoje a R$134")
        self.assertEqual(travas.curto("conferida no ML, a nota caiu para OK 59 (R$149.00)"),
                         "conferida no ML: caiu para OK 59")
        self.assertEqual(travas.curto("algo novo que ninguém previu"), "algo novo que ninguém previu")

    def test_mil(self):
        self.assertEqual((travas.mil(697), travas.mil(1163), travas.mil(4000), travas.mil(267341)),
                         ("697", "1,2 mil", "4 mil", "267 mil"))


if __name__ == "__main__":
    unittest.main()
