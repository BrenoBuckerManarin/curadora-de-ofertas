"""Leitura de promos: conjunto de referência (mensagens reais conferidas à mão) + casos difíceis."""
from __future__ import annotations

import json
import unittest

from apoio import FIXTURES, MARCAS

from curadora.parser import ler_promo, parece_perfume, valor


class TestConjuntoDeReferencia(unittest.TestCase):
    """36 mensagens reais (4 por grupo), sorteadas e conferidas campo a campo."""

    def test_todas_as_mensagens(self):
        golden = json.loads((FIXTURES / "golden_promos.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(golden), 36)
        for n, caso in enumerate(golden):
            esp = caso["esperado"]
            p = ler_promo(caso["texto"], MARCAS)
            with self.subTest(n=n, grupo=caso["grupo"]):
                self.assertEqual(p.valida and parece_perfume(p.produto), esp["perfume"])
                if not esp["perfume"]:
                    continue
                for campo in ("produto", "preco", "preco_de", "vendedor", "loja_oficial", "verificado", "cupons"):
                    self.assertEqual(getattr(p, campo), esp[campo], campo)


class TestCasosDificeis(unittest.TestCase):
    def test_manchete_com_volume_perde_para_linha_do_produto(self):
        p = ler_promo("AL WATANIAH 100ML CAINDO PRA 162 REAIS 🔥\n\n*Al Wataniah Bareeq Al Dhahab EDP 100ml*\n"
                      "De R$293 por R$162\nVendido por Loja Oficial Lipx no ML\nhttps://meli.la/a", MARCAS)
        self.assertEqual(p.produto, "Al Wataniah Bareeq Al Dhahab EDP 100ml")
        self.assertEqual(p.vendedor, "Lipx")

    def test_manchete_unica_tem_preco_cortado(self):
        p = ler_promo("PERFUME BENETTON 90ML POR 112,50 🥷\nR$ 112,50\nhttps://meli.la/a", MARCAS)
        self.assertEqual(p.produto, "PERFUME BENETTON 90ML")
        self.assertEqual(p.preco, 112.5)

    def test_frase_de_inspiracao_nao_vira_produto(self):
        p = ler_promo("Inspirado excelente no Y EDP.\n*Perfume Armaf Odyssey Homme EDP 100ml*\nPor R$187\n"
                      "https://meli.la/a", MARCAS)
        self.assertEqual(p.produto, "Perfume Armaf Odyssey Homme EDP 100ml")

    def test_loja_oficial_sem_nome(self):
        p = ler_promo("*Perfume Rabanne Invictus EDT 200ml*\nPor *R$538*\nLoja oficial no MELI!\nhttps://meli.la/a", MARCAS)
        self.assertEqual(p.vendedor, "")
        self.assertTrue(p.loja_oficial)

    def test_lista_de_cupons_nao_e_oferta(self):
        p = ler_promo("BOM DIA\nCupom: *AQUITEMCUPOM*\n15% OFF em compras a partir de R$ 79\nLimite de R$ 60\n"
                      "https://mercadolivre.com/sec/2MD7vPv", MARCAS)
        self.assertFalse(p.valida)

    def test_cupom_em_reais_nao_vira_preco_de(self):
        p = ler_promo("O Boticário The Blend Saffron Eau De Parfum 100ml\n_aplique o cupom de R$ 50 OFF_\n*Por R$309*\n"
                      "https://meli.la/a", MARCAS)
        self.assertEqual((p.preco, p.preco_de), (309.0, None))

    def test_limited_no_nome_nao_e_condicao_de_cupom(self):
        p = ler_promo("EDIÇÃO LIMITADA ASAD 🥷\n\n➡️ **Lattafa Asad Zanzibar Limited Masculino 100ml**\n\n✅ R$ 184\n\n"
                      "🛒 Compre aqui: https://meli.la/vZO8HcI", MARCAS)
        self.assertEqual(p.produto, "Lattafa Asad Zanzibar Limited Masculino 100ml")
        self.assertEqual(p.preco, 184.0)

    def test_preco_e_de_na_mesma_linha_sem_por(self):
        p = ler_promo("*Perfume Boticário The Blend Cardamom EDP 100ml*\n\n💰 R$ 278 no Pix (De ~R$ 449~)\n"
                      "🎟️ Cupom: QUEROGOL + Selecione Pix\n🏪 Loja: Boticário no ML\n\n🛒 https://meli.la/ngciV09", MARCAS)
        self.assertEqual((p.preco, p.preco_de), (278.0, 449.0))
        self.assertTrue(p.no_pix)
        self.assertEqual(p.vendedor, "Boticário")

    def test_marca_com_acento_no_nome_do_kit(self):
        # Visto na VPS em 03/10: não lia "Egeo Bomb Black Kit Presente 2 Itens O Boticário".
        p = ler_promo("*Egeo Bomb Black Kit Presente 2 Itens O Boticário*\n\nDe ~R$195~\nPor *R$138* 🔥\n\n"
                      "🎟️ Cupom: *MODA0310*\n\n👉 *Pegar promoção:* https://meli.la/abc", MARCAS)
        self.assertEqual(p.produto, "Egeo Bomb Black Kit Presente 2 Itens O Boticário")
        self.assertEqual(p.preco, 138.0)

    def test_marketplaces(self):
        self.assertEqual(ler_promo("Perfume X 100ml\nPor R$ 10\nhttps://amzn.to/abc", MARCAS).marketplace, "amazon")
        self.assertEqual(ler_promo("Perfume X 100ml\nPor R$ 10\nhttps://meli.la/abc", MARCAS).marketplace, "ml")

    def test_valores_em_reais(self):
        self.assertEqual(valor("1.024,90"), 1024.9)
        self.assertEqual(valor("1.024"), 1024.0)
        self.assertEqual(valor("299,00"), 299.0)
        self.assertEqual(valor("142.90"), 142.9)
        self.assertIsNone(valor(""))

    def test_titulo_em_negrito_vence_a_frase_de_chamada(self):
        # Visto na VPS em 04/10: o Natura virava "Le Male Elixir" e sumia da busca por Natura.
        casos = {
            "*NATURA HOMEM EVOLUTIO*\nO Le Male Elixir nacional.\n \n🔥🔥Por: *R$173*\n\n"
            "👉🏼 Link para compra: https://meli.la/QT3hxOl\n\nSelecione a loja BPSPERFUMARIA": "NATURA HOMEM EVOLUTIO",
            "*LATTAFA FAHAD*\nO lançamento mais importante da Lattafa, do mesmo criador do Myslf.\n \n~De: R$499~\n"
            "🔥🔥Por: *R$283*\n\n👉🏼 Link para compra: https://meli.la/rNhcxPd": "LATTAFA FAHAD",
            "*RAYHAAN OBSIDIAN*\nTodo mundo já sabe que é meu árabe favorito, inspirado no Dior Homme Parfum.\n \n"
            "🔥🔥Por: *R$244*\n\n👉🏼 Link para compra: https://meli.la/62Rq1l1": "RAYHAAN OBSIDIAN",
            "*RAYHAAN AZUL*\nDior Homme Cologne com fixação e projeção melhoradas. ✅\n\n🔥🔥Por: *R$285*\n\n"
            "👉🏼 Link para compra: https://meli.la/Ymr9Ibl": "RAYHAAN AZUL",
        }
        for texto, produto in casos.items():
            with self.subTest(produto=produto):
                self.assertEqual(ler_promo(texto, MARCAS).produto, produto)

    def test_linha_da_loja_nao_e_produto(self):
        p = ler_promo("KIT EGEO BOMB BLACK POR 159 🥷\n\n➡️ **Egeo Bomb Black Mini Kit 2 Itens**\n🏬 O Boticário\n\n"
                      "✅ R$ 159\n\n🛒 Compre aqui: https://meli.la/5m7ltha", MARCAS)
        self.assertEqual(p.produto, "Egeo Bomb Black Mini Kit 2 Itens")

    def test_brinquedo_e_cartas_nao_sao_perfume(self):
        lego = ("*Lego City #60465 (403 Peças) LEGO*\n\nDe ~R$499~\nPor *R$286* 🔥 no pix\n\n🎟️ Cupom: *BRINCAR*\n\n"
                "👉 *Pegar promoção:* https://meli.la/xewjR1e\n\nLoja oficial Lego no MELI!")
        pokemon = ("*Megaevolution Pitch Black Booster Bundle*\n\nPor *R$169* 🔥\n\n🎟️ Cupom: *BRINCAR*\n\n"
                   "👉 *Pegar promoção:* https://meli.la/oSKbhFw\n\nLoja oficial Pokémon no MELI!")
        for texto in (lego, pokemon):
            p = ler_promo(texto, MARCAS)
            self.assertFalse(parece_perfume(p.produto, texto), p.produto)
        self.assertFalse(parece_perfume("Creme Multirreparador Pele Sensível Cicaplast Baume B5+ La Roche-Posay 20ml"))

    def test_nao_perfume(self):
        self.assertFalse(parece_perfume("Relógio Masculino Saint Germain Houston 40 mm"))
        self.assertFalse(parece_perfume("Principia Kit Hidratação E Proteção Facial"))
        self.assertFalse(parece_perfume("Copo Térmico Stanley Adventure Black 354ml"))   # 18/10, foi pelo pipeline
        self.assertFalse(parece_perfume("Caneca Térmica Inox 473ml"))
        self.assertFalse(parece_perfume("Leave-in Uniq One Curls 230ml - Revlon"))   # 19/10, foi pelo pipeline
        self.assertFalse(parece_perfume("Leave In Truss Uso Obrigatório 260ml"))
        self.assertTrue(parece_perfume("Natura Essencial Mirra Deo Parfum 100ml"))
        self.assertTrue(parece_perfume("HIS CONFESSION"))
        self.assertTrue(parece_perfume("Kit Perfume Kaiak 100ml + Sabonete"))
        self.assertTrue(parece_perfume("Kit Egeo Dolce - Boticário (50ml + Creme 85g)"))
        self.assertFalse(parece_perfume("Kit Nutri Acid.Complex Shampoo 250ml + Condicionador 200ml"))
        self.assertFalse(parece_perfume("Kit Tododia Cereja Negra E Praliné Creme Nutritivo 400ml + Refil 400ml"))

    def test_sem_perfume_e_itens_de_cabelo_nao_sao_perfume(self):
        # 10/10: o pipeline mandou um kit de shampoo e uma loção "sem perfume" (a palavra estava lá para negar).
        for nome in ("Loção Hidratante Corporal Sem Perfume CeraVe 473ml", "Kit 6 Desodorantes Aerosol Bozzano Sem Perfume 90g",
                     "Kit Braé Stages Nutrition", "KIT WELLA INVIGO", "Gel Fixador Bozzano", "Polo Lacoste Slim OG",
                     "Daily T-shirt Insider"):
            self.assertFalse(parece_perfume(nome), nome)
        for nome in ("Body Spray Carolina Herrera 212 Men NYC Body 250ml", "Corporal Natura Homem Nós Desodorante 100ml",
                     "RAYHAAN AZUL", "Kit O Boticário Egeo Dolce (3 Itens)", "Perfume Ralph Lauren Polo Blue EDT Masculino 200ml"):
            self.assertTrue(parece_perfume(nome), nome)


if __name__ == "__main__":
    unittest.main()
