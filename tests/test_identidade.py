"""Identidade do produto e o catálogo aprendido (gênero e volume)."""
from __future__ import annotations

import unittest

from apoio import CFG, MARCAS

from curadora.identidade import Catalogo, Identificador


class TestIdentificador(unittest.TestCase):
    def setUp(self):
        self.i = Identificador(MARCAS, CFG["sinonimos"])

    def chave(self, nome: str) -> str:
        return Catalogo().chave(self.i.analisar(nome)).chave

    def test_ordem_marca_e_palavras_genericas_nao_importam(self):
        self.assertEqual(self.chave("Armaf Club de Nuit Intense Man EDT 105ml"),
                         self.chave("Perfume Club De Nuit Intense 105 ml Armaf Masculino"))
        self.assertEqual(self.chave("Lattafa Asad Eau de Parfum 100ml"), self.chave("Asad 100 ML Original"))
        self.assertEqual(self.chave("Natura Essencial Sentir Deo Parfum 100ml"), self.chave("Perfume Natura Essencial Sentir EDP 100ml"))
        self.assertEqual(self.chave("Kaiak Tradicional Desodorante Colônia 100ml"), self.chave("Natura Kaiak 100ml"))
        # 21/10: "Lipx" é a loja, não parte do nome (o mesmo Yara Elixir foi duas vezes)
        self.assertEqual(self.chave("Perfume Arabe Yara Elixir Feminino Eau De Parfum 100ml Lipx"),
                         self.chave("Yara Elixir Perfume Árabe Feminino Edp 100ml - Lattafa"))
        self.assertEqual(self.chave("Perfume Yara Moi Lipx Eau De Parfum 100ml"), self.chave("Lattafa Yara Moi EDP 100ml"))

    def test_loja_e_palavras_soltas_nao_partem_o_perfume(self):
        # 25/10 (Breno: "a curadora achava que cada Yara era diferente")
        self.assertEqual(self.chave("Perfume Hawas Kobra Rasasi EDP 100ml Arabe Erian"), self.chave("Rasasi Hawas Kobra EDP 100ml"))
        self.assertEqual(self.chave("Perfume Árabe Al Wataniah Bareeq Al Dhahab 100ml Eau De Parf"),
                         self.chave("Al Wataniah Bareeq Al Dhahab EDP 100ml"))
        self.assertEqual(self.chave("Perfume Árabe Yara Tous Original Edp Para Feminino 100ml"), self.chave("Lattafa Yara Tous 100ml"))
        self.assertEqual(self.chave("Kaiak Ultra Natura Colônia Perfume Masculino 100ml Relançamento"),
                         self.chave("Natura Kaiak Ultra Masculino 100ml"))
        self.assertEqual(self.chave("Extrato De Perfume Árabe Island Khadlaj Luxo 100ml Unissex"),
                         self.chave("Spray De Extrato De Perfume Unissex Khadlaj Island 100ml"))
        self.assertEqual(self.chave("Club de Nuit Intense para Homens Armaf 105ml"), self.chave("Armaf Club De Nuit Intense Man 105ml"))

    def test_descricao_do_cheiro_nao_parte_o_perfume(self):
        # 28/10 (Breno: "esse preço nem deveria ser excelente"): o Asad "Amadeirado" virava outro produto
        self.assertEqual(self.chave("Perfume Árabe Asad Lattafa 100ml Edp Amadeirado Masculino Importado"),
                         self.chave("Lattafa Asad EDP 100ml"))
        self.assertEqual(self.chave("Rasasi Hawas Oriental Masculino 100ml"), self.chave("Rasasi Hawas EDP 100ml"))
        self.assertEqual(self.chave("Vulcan Feu Cítrico Frutado French Avenue 100ml"), self.chave("French Avenue Vulcan Feu 100ml"))
        # No body splash a nota é o nome da versão
        self.assertNotEqual(self.chave("Body Splash Primacial Blunn Cítrico 200ml"),
                            self.chave("Body Splash Primacial Blunn Amadeirado 200ml"))

    def test_versoes_continuam_separadas(self):
        yara = self.chave("Lattafa Yara EDP 100ml")
        self.assertNotEqual(yara, self.chave("Lattafa Yara Candy EDP 100ml"))
        self.assertNotEqual(yara, self.chave("Lattafa Yara Elixir EDP 100ml"))
        # o Kaiak K EDP não é o Kaiak tradicional (antes "para" separava por acaso; sem ele, juntava)
        self.assertNotEqual(self.chave("Natura Kaiak K EDP 100ml para masculino"), self.chave("Natura Kaiak Tradicional 100ml"))
        self.assertEqual(self.chave("Natura Kaiak K EDP 100ml para masculino"), self.chave("Perfume Natura Kaiak K EDP 100ml"))

    def test_flanker_volume_e_concentracao_separam(self):
        base = self.chave("Lattafa Asad 100ml")
        self.assertNotEqual(base, self.chave("Lattafa Asad Bourbon 100ml"))
        self.assertNotEqual(base, self.chave("Lattafa Asad Elixir 100ml"))
        self.assertNotEqual(base, self.chave("Lattafa Asad 50ml"))
        self.assertNotEqual(self.chave("Club de Nuit Intense Man 105ml"),
                            self.chave("Club de Nuit Intense Man Parfum 105ml"))

    def test_genero_vira_atributo_e_nao_palavra(self):
        a = self.i.analisar("Perfume Árabe Fakhar Rose Lattafa EDP 100ml Feminino")
        self.assertEqual(a.genero, "f")
        self.assertEqual(a.tokens, frozenset({"fakhar", "rose"}))

    def test_nomes_que_parecem_genero(self):
        self.assertEqual(self.i.analisar("Jean Paul Gaultier Le Male 125ml").tokens, frozenset({"male"}))
        self.assertEqual(self.i.analisar("Hawas For Him Rasasi 100ml").tokens, frozenset({"hawas"}))

    def test_nome_de_uma_letra(self):
        # Visto na VPS em 03/10: "Y Yves Saint Laurent Edt 100ml" ficava sem identidade.
        y = self.i.analisar("Y Yves Saint Laurent Edt 100ml")
        self.assertIn("y", y.tokens)
        self.assertEqual(self.chave("Y Yves Saint Laurent Edt 100ml"), self.chave("Perfume YSL Y EDT 100ml"))
        self.assertNotEqual(self.chave("Y Yves Saint Laurent Edt 100ml"), self.chave("Yves Saint Laurent Libre 100ml"))

    def test_linha_generica_mantem_a_marca(self):
        self.assertEqual(self.i.analisar("Dior Homme Intense 100ml").tokens, frozenset({"dior", "homme", "intense"}))
        self.assertNotEqual(self.chave("Dior Homme Intense 100ml"), self.chave("Azzaro Intense 100ml"))

    def test_sinonimos_de_grafia(self):
        self.assertEqual(self.chave("Al Wataniah Bareq Al Dahab 100ml"), self.chave("Bareeq Al Dhahab EDP 100ml"))

    def test_erros_de_digitacao_dos_grupos(self):
        # Pares reais do histórico (04/10): cada um dividia o mesmo produto em dois.
        for a, b in [("Asad Lataffa 100ml", "Lattafa Asad 100ml"),
                     ("Salvo Alhmabra 100ml", "Maison Alhambra Salvo EDP 100ml"),
                     ("L'eau Issey Miyake Pour Homme 125ml", "Perfume L'eau D'Issey Miyake Pour Homme 125ml"),
                     ("Mawwal Asir L’qalb 100ml", "MAWWAL ASIR L'QALB 100ml"),
                     ("OBoticário Quasar Ice 100ml", "O Boticário Quasar Ice 100ml"),
                     ("AFNAN SUPREMACY COLLECTOR’S EDITION 100ml", "Supremacy Collector's Edition 100ml"),
                     ("Lattafa Khamah 100ml", "Lattafa Khamrah EDP 100ml")]:
            with self.subTest(a=a):
                self.assertEqual(self.chave(a), self.chave(b))

    def test_palavra_parecida_que_e_outro_produto_nao_junta(self):
        self.assertNotEqual(self.chave("Botica 214 Dark Mint 90ml"), self.chave("Boticário Dark Mint 90ml"))
        self.assertNotEqual(self.chave("Lattafa Ameer 100ml"), self.chave("Al Wataniah Ameeri 100ml"))
        self.assertNotEqual(self.chave("L'Homme Nuit 100ml"), self.chave("Nuit 100ml"))  # L'Homme é nome

    def test_lista_top(self):
        cat = Catalogo()
        self.assertTrue(self.i.mesmo_produto_da_lista(cat.chave(self.i.analisar("Asad Lattafa EDP 100ml")), "Lattafa Asad"))
        self.assertFalse(self.i.mesmo_produto_da_lista(cat.chave(self.i.analisar("Sabah Al Ward Sugar 100ml")), "Sabah Al Ward"))
        self.assertTrue(self.i.mesmo_produto_da_lista(cat.chave(self.i.analisar("Perfume Attracione Men 100ml")), "Attracione Men"))


class TestConflitoComML(unittest.TestCase):
    def setUp(self):
        self.i = Identificador(MARCAS, CFG["sinonimos"])

    def conflito(self, promo: str, titulo_ml: str) -> str:
        return Identificador.conflito(self.i.analisar(promo), self.i.analisar(titulo_ml))

    def test_titulo_do_ml_com_palavras_de_seo_e_compativel(self):
        self.assertEqual(self.conflito("Lattafa Asad 100ml", "Perfume Lattafa Asad 100ml Eau De Parfum Original "
                                       "Árabe Original Com Nota Fiscal E Selo Original Lataffa"), "")
        self.assertEqual(self.conflito("Mawwal Saz Edp 100ml", "Perfume Mawwal Symphony SAZ Original Masculino EDP 100ml"), "")
        # Visto ao vivo em 02/10: "Deo Parfum" é só a concentração da Natura.
        self.assertEqual(self.conflito("Natura Homem Identidad 100ml", "Deo Parfum Natura Homem Identidad 100ml"), "")
        self.assertEqual(self.conflito("Perfume Natura Kaiak 100ml", "Kaiak Masculino Tradicional Natura Desodorante "
                                       "Colônia Perfume Clássico Original 100ml"), "")

    def outra(self, promo: str, titulo_ml: str) -> str:
        return Identificador.outra_marca(self.i.analisar(promo), self.i.analisar(titulo_ml))

    def test_anuncio_de_outra_marca(self):
        # 18/10: o grupo escreveu "Asad Bourbon"; o anúncio era a versão inspirada da Isabelle La Belle (R$79).
        isabelle = "Perfume Arábe Asad Bourbon 100ml - Marca Isabelle La Belle | Fixação Intensa, Notas Amadeiradas"
        self.assertEqual(self.i.analisar(isabelle).marca_declarada, "isabelle la belle")
        self.assertEqual(self.i.analisar("Kit Hidratante Corporal Angel 200ml Isabelle Labelle").marca_declarada,
                         "isabelle la belle")
        self.assertEqual(self.outra("Perfume Arábe Asad Bourbon 100ml", isabelle), "isabelle la belle")
        self.assertEqual(self.outra("Asad Bourbon Isabelle La Belle 100ml", isabelle), "")   # o grupo disse a marca
        # "Marca" com a marca do próprio perfume, ou palavra que não é marca, não muda nada
        self.assertEqual(self.i.analisar("Perfume Asad Bourbon 100ml Marca Lattafa Original").marca_declarada, "lattafa")
        self.assertEqual(self.outra("Asad Bourbon 100ml", "Perfume Asad Bourbon 100ml Marca Lattafa Original"), "")
        self.assertEqual(self.outra("Lattafa Asad Bourbon 100ml", "Perfume Lattafa Asad Bourbon EDP 100ml Masculino"), "")
        self.assertEqual(self.i.analisar("Perfume Deixe Sua Marca 100ml").marca_declarada, "")
        self.assertEqual(self.i.analisar("Asad 100ml Marca Original Importado").marca_declarada, "")

    def test_corporal_e_outro_produto(self):
        self.assertIn("versão", self.conflito("Natura Essencial Atrai 100ml", "Desodorante Corporal Natura Essencial Atrai 100ml"))

    def test_versao_volume_ou_genero_diferente_e_conflito(self):
        self.assertIn("versão", self.conflito("Club de Nuit Intense Man 105ml", "Armaf Club De Nuit Intense Man Parfum 105ml"))
        self.assertIn("volume", self.conflito("Lattafa Yara 100ml", "Perfume Lattafa Yara EDP 50ml"))
        self.assertIn("gênero", self.conflito("CH Men 100ml", "Carolina Herrera CH Feminino 100ml"))
        self.assertIn("não bate", self.conflito("Lattafa Asad 100ml", "Shampoo Condicionador Lattafa 100ml"))


class TestCatalogoAprendido(unittest.TestCase):
    @staticmethod
    def ofertas(base, volume, genero, preco, n):
        return [(base, volume, genero, preco)] * n

    def test_genero_so_separa_quando_muda_o_preco(self):
        cat = Catalogo.aprender(
            self.ofertas("ch", "100", "m", 650, 6) + self.ofertas("ch", "100", "f", 500, 6)
            + self.ofertas("essencial-oud", "100", "m", 140, 20) + self.ofertas("essencial-oud", "100", "f", 141, 30))
        self.assertIn("ch|100", cat.generos_relevantes)
        self.assertNotIn("essencial-oud|100", cat.generos_relevantes)

    def test_genero_padrao_pelo_preco_de_quem_nao_informa(self):
        cat = Catalogo.aprender(
            self.ofertas("kaiak", "100", "m", 159, 3) + self.ofertas("kaiak", "100", "f", 78, 3)
            + self.ofertas("kaiak", "100", "", 150, 50))
        self.assertEqual(cat.generos_relevantes["kaiak|100"], "m")
        i = Identificador(MARCAS)
        self.assertEqual(cat.chave(i.analisar("Natura Kaiak 100ml")).chave, "kaiak|100|m")
        self.assertEqual(cat.chave(i.analisar("Natura Kaiak Feminino 100ml")).chave, "kaiak|100|f")

    def test_genero_ambiguo_fica_marcado(self):
        cat = Catalogo.aprender(self.ofertas("ch", "100", "m", 650, 6) + self.ofertas("ch", "100", "f", 500, 6))
        ident = cat.chave(Identificador(MARCAS).analisar("Carolina Herrera CH 100ml"))
        self.assertEqual(ident.chave, "ch|100|?")
        self.assertTrue(any("gênero" in n for n in ident.notas))

    def test_volume_dominante_e_assumido(self):
        cat = Catalogo.aprender(self.ofertas("asad", "100", "", 170, 40) + self.ofertas("asad", "50", "", 120, 2))
        ident = cat.chave(Identificador(MARCAS).analisar("Lattafa Asad"))
        self.assertEqual(ident.chave, "asad|100")
        self.assertTrue(any("assumido 100ml" in n for n in ident.notas))

    def test_volume_dividido_nao_e_assumido(self):
        cat = Catalogo.aprender(self.ofertas("yara", "100", "", 170, 10) + self.ofertas("yara", "50", "", 120, 10))
        self.assertEqual(cat.chave(Identificador(MARCAS).analisar("Lattafa Yara")).chave, "yara|?")

    def test_catalogo_sobrevive_ida_e_volta_em_json(self):
        cat = Catalogo({"ch|100": ""}, {"asad": "100"})
        self.assertEqual(Catalogo.de_json(cat.para_json()).volumes, {"asad": "100"})


if __name__ == "__main__":
    unittest.main()
