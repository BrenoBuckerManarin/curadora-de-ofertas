"""Início de cupom percebido pelos grupos, em sombra (04/11, ajuste 6): o DESCONTOS de 04/11 é o caso de teste."""
from __future__ import annotations

import json
import time
import unittest

from apoio import CFG, FIXTURES

from curadora import cupom_antecipado as ca

h = lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))   # noqa: E731


class TestDescontos0411(unittest.TestCase):
    """Parado desde 03/10; aviso no FONTE GAMA #45 às 09:04:31 e o 1º perfume no Fonte Zeta | 133 às 09:07:55.
    A equipe postou com ele às 09:11:31 e ele ficou quente na curadora às 09:16:16 (2º post do GrupoFinal)."""

    @classmethod
    def setUpClass(cls):
        cls.caso = json.loads((FIXTURES / "cupom_descontos_0411.json").read_text(encoding="utf-8"))
        d = ca.Detector(CFG["marcas"], ultimo_perfume={"DESCONTOS": cls.caso["ultima_oferta_de_perfume_antes"]})
        cls.inicios = d.ver_todas(cls.caso["mensagens"])

    def test_percebe_as_09_07_55_pelo_aviso_e_o_primeiro_perfume(self):
        self.assertEqual(len(self.inicios), 1)
        i = self.inicios[0]
        self.assertEqual((i.codigo, h(i.ts)), ("DESCONTOS", "09:07:55"))
        self.assertEqual([(s.tipo, s.nome, h(s.ts)) for s in i.sinais],
                         [("aviso", "FONTE GAMA #45", "09:04:31"), ("perfume", "Fonte Zeta | 133", "09:07:55")])
        self.assertIn("10%", i.regra)
        self.assertIn("parado desde 03/10", i.frase())

    def test_antes_da_equipe_e_do_quente(self):
        primeiro_post, segundo_post = self.caso["posts_do_grupofinal_com_o_cupom"][:2]
        i = self.inicios[0]
        self.assertLess(i.ts, primeiro_post)               # 09:11:31
        self.assertEqual(round((primeiro_post - i.ts) / 60, 1), 3.6)
        self.assertLess(i.ts, segundo_post)                # 09:16:16: quando ficou quente na curadora


class TestRegra(unittest.TestCase):
    def msg(self, ts, grupo, texto):
        return {"ts": ts, "grupo": grupo, "nome": grupo, "grupofinal": False, "texto": texto}

    PERFUME = "*Perfume Lattafa Asad 100ml*\nDe R$ 300 por *R$ 180*\n🎟️ Cupom: *MODA10*\nhttps://meli.la/LFTXzyG"
    AVISO = "🚨 NOVO CUPOM NO ML 🎟️ MODA10 10% OFF acima de R$79 limitado a R$100"

    def test_um_grupo_com_um_post_so_nao_basta_dois_grupos_bastam(self):
        d = ca.Detector(CFG["marcas"])
        self.assertEqual(d.ver(self.msg(1000, "g1", self.PERFUME)), [])
        (i,) = d.ver(self.msg(1300, "g2", self.PERFUME))
        self.assertEqual(i.ts, 1300)

    def test_sinais_a_mais_de_15_min_nao_se_juntam(self):
        d = ca.Detector(CFG["marcas"])
        d.ver(self.msg(1000, "g1", self.AVISO))
        self.assertEqual(d.ver(self.msg(1000 + 16 * 60, "g2", self.PERFUME)), [])

    def test_cupom_que_ja_estava_ativo_nao_e_inicio(self):
        d = ca.Detector(CFG["marcas"], ultimo_perfume={"MODA10": 1000})
        self.assertEqual(d.ver(self.msg(2000, "g1", self.AVISO)), [])
        self.assertEqual(d.ver(self.msg(2100, "g2", self.PERFUME)), [])

    def test_o_grupofinal_nao_conta(self):
        d = ca.Detector(CFG["marcas"])
        d.ver(self.msg(1000, "g1", self.AVISO))
        self.assertEqual(d.ver({**self.msg(1100, "grupofinal", self.PERFUME), "grupofinal": True}), [])


if __name__ == "__main__":
    unittest.main()
