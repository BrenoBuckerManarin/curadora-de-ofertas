"""Kits (só informação), reconferência do topo de Médias/Ruins e "Pular" na sugestão (04/10)."""
from __future__ import annotations

import time
import unittest

from apoio import CFG, DIA, T0, banco_com_historico

import painel
from curadora.banco import registrar_avaliacao
from curadora.kits import info_kit, ml_de_perfume
from curadora.reconferir import Reconferidor
from curadora.score import Curadora, para_dict


class TestKits(unittest.TestCase):
    def test_ml_de_perfume_no_kit(self):
        casos = {"Kit com 3 Boticário Perfume Floratta 30ml": 90, "Combo Egeo Melancia: Colônia 90ml + Loção 400ml": 90,
                 "Kit Egeo Dolce - Boticário (50ml + Creme 85g)": 50, "Kit 2 Perfumes Kaiak 100ml": 200,
                 "Kit Malbec Desodorante Colônia 100ml + Shampoo 200ml": 100,
                 "Kit Body Splash Malbec 200ml + Desodorante 75ml": 200, "Egeo Bomb Black Mini Kit 2 Itens": None}
        for nome, ml in casos.items():
            with self.subTest(nome=nome):
                self.assertEqual(ml_de_perfume(nome), ml)

    def test_compara_com_o_avulso_e_nao_mexe_na_nota(self):
        con = banco_com_historico([("Natura Kaiak 100ml", p, d) for d, p in enumerate([150, 152, 148, 151, 149])])
        cur = Curadora(CFG, con)
        info = info_kit(con, "Kit 2 Perfumes Kaiak 100ml", 260, cur.ident.analisar("Kit 2 Perfumes Kaiak 100ml").tokens,
                        T0 + 6 * DIA)
        self.assertEqual((info["ml"], info["preco_100ml"], info["avulso_100ml"]), (200, 130, 150))
        av = cur.avaliar("*Kit 2 Perfumes Kaiak 100ml*\nPor R$ 260\nhttps://meli.la/k", agora=T0 + 6 * DIA,
                         conferir_ml=False)
        self.assertEqual(av.kit_info["preco_100ml"], 130)

    def test_kit_vale_mais(self):
        # Pedido do Breno em 04/10: kit é o que o público gosta. Sem histórico próprio, compara com o avulso
        # (100ml do kit 13% mais barato = desconto real) e ganha ponto de relevância.
        con = banco_com_historico([("Natura Kaiak 100ml", p, d) for d, p in enumerate([150, 152, 148, 151, 149])])
        cur = Curadora(CFG, con)
        kit = cur.avaliar("*Kit 2 Perfumes Kaiak 100ml*\nPor R$ 260\nhttps://meli.la/k", agora=T0 + 6 * DIA,
                          conferir_ml=False)
        self.assertAlmostEqual(kit.pct_abaixo, 13.3, places=1)
        self.assertFalse(kit.sem_historico)
        self.assertEqual(kit.componentes["relevancia"], CFG["pesos"].get("kit", 12))
        self.assertEqual(kit.nota, "OK")                                      # 13% mais barato: Média
        bom = cur.avaliar("*Kit 2 Perfumes Kaiak 100ml*\nPor R$ 240\nhttps://meli.la/k", agora=T0 + 6 * DIA,
                          conferir_ml=False)                    # 20% mais barato que o avulso: Boa
        self.assertEqual(bom.nota, "BOA")
        caro = cur.avaliar("*Kit 2 Perfumes Kaiak 100ml*\nPor R$ 330\nhttps://meli.la/k", agora=T0 + 6 * DIA,
                           conferir_ml=False)                    # 100ml mais caro que o avulso: não é promo
        self.assertEqual(caro.nota, "NÃO VALE")


class TestMediasERuins(unittest.TestCase):
    def setUp(self):
        self.agora = int(time.time())
        hist = [("Lattafa Asad 100ml", p, 0) for p in (200, 198, 205, 202, 199, 201, 203, 197)]
        self.con = banco_com_historico(hist)
        self.con.execute("UPDATE ofertas SET ts = ? - (id * 86400)", (self.agora,))
        self.con.commit()
        self.cur = Curadora(CFG, self.con)

    def registrar(self, texto: str, ts: int):
        av = self.cur.avaliar(texto, agora=ts, conferir_ml=False)
        return registrar_avaliacao(self.con, ts, "ao vivo", "G", av.identidade.chave, av.nota, av.score, False,
                                   para_dict(av))

    def test_topo_das_ruins_tambem_e_reconferido(self):
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 240\nhttps://meli.la/a", self.agora - 3600)   # ruim, fresca
        rec = Reconferidor(self.cur, lambda: painel._cards(self.con), lambda i: {}, relogio=lambda: self.agora)
        self.assertEqual([c["nota"] for c in rec.alvos()], ["NÃO VALE"])

    def test_pular_tira_da_sugestao(self):
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", self.agora - 600)
        s = painel.api_sugestao(self.con, {})
        self.assertEqual(len(s["itens"]), 1)
        painel.api_pular(self.con, {"chave": s["itens"][0]["chave"]})
        self.assertEqual(painel.api_sugestao(self.con, {})["itens"], [])


if __name__ == "__main__":
    unittest.main()
