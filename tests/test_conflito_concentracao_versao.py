"""31/10 (auditoria da Astra + revisão de identidade): o conflito não via a versão que só o grupo escreveu ("Club de Nuit
Intense" com link do "Club de Nuit") e nada olhava a concentração (EDT x EDP). Medido em 14 dias (18 a 31/10, 237 mil
avaliações) antes de subir:
- a versão nos dois sentidos só pegou casos certos (kit com body spray x outro anúncio, Intense, Elixir): é conflito;
- a concentração diferente acertou 1 caso e barraria 3 Boas legítimas (o post escreveu a concentração errada): não é
  conflito; tira o Excelente e avisa ("confira o produto");
- "Extrato de perfume" (propaganda no ML do Brasil) e "Colônia" (os grupos chamam Deo Parfum de colônia) não dizem
  concentração: só o Island Dreams geraria 750 conflitos falsos em Boas que o Breno postava.
Pares de títulos reais."""
import unittest

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico
from curadora.identidade import Identificador
from curadora.ml import CardML
from curadora.score import Curadora


class Base(unittest.TestCase):
    def setUp(self):
        self.i = Identificador(CFG["marcas"], CFG["sinonimos"])

    def conflito(self, a, b):
        return Identificador.conflito(self.i.analisar(a), self.i.analisar(b))

    def concentracao(self, a, b):
        return Identificador.concentracao_diferente(self.i.analisar(a), self.i.analisar(b))


class TestConcentracaoDiferente(Base):
    def test_edt_x_edp_nos_dois_sentidos(self):
        # 31/10 ao vivo: o grupo escreveu "Edp" e o link era o Club de Nuit Intense Man (EDT)
        self.assertEqual(self.concentracao("Club De Nuit Intense Edp 105ml",
                                           "Club De Nuit Intense Armaf 105ml Edt Masculino"), "EDP x EDT")
        # 31/10: a Banderas vendia The Icon EDT (R$143,28) e havia The Icon EDP a R$137,83 na mesma chave
        self.assertEqual(self.concentracao("Banderas The Icon EDT 100ml", "Antonio Banderas The Icon Edp 100 Ml"), "EDT x EDP")
        self.assertEqual(self.concentracao("Antonio Banderas The Icon Edp 100 Ml",
                                           "Perfume Banderas The Icon Eau de Toilette 100 ml"), "EDP x EDT")

    def test_concentracao_nao_e_conflito(self):
        self.assertEqual(self.conflito("Banderas The Icon EDT 100ml", "Antonio Banderas The Icon Edp 100 Ml"), "")

    def test_woman_edp_no_link_de_um_man_edt_sem_genero_no_grupo(self):
        self.assertTrue(self.concentracao("Armaf Club de Nuit Intense EDT 105ml",
                                          "Perfume Armaf Club De Nuit Intense 105 ml Edp - Feminino"))

    def test_parfum_e_extrait(self):
        self.assertTrue(self.concentracao("Dior Sauvage Parfum 100ml", "Perfume Dior Sauvage Eau de Parfum 100ml"))
        self.assertTrue(self.concentracao("Armaf Club de Nuit Intense Man Extrait 105ml",
                                          "Armaf Club de Nuit Intense Man EDT 105ml"))

    def test_extrato_e_colonia_nao_dizem_concentracao(self):
        for a, b in [("Khadlaj Island Dreams EDP 100ml", "Spray De  Extrato De Perfume Unissex Khadlaj Island 100ml"),
                     ("Perfume Árabe Khadlaj Island Dreams EDP 100ml",
                      "Extrato De Perfume Árabe Island Khadlaj Luxo 100ml Unissex"),
                     ("Natura Essencial Sentir Colônia 100ml", "Deo Parfum Natura Essencial Sentir Masculino 100 mL"),
                     ("Kaiak Desodorante Colônia 100ml", "Natura Kaiak Colônia Eau De Toilette Classic Masculino 100ml")]:
            self.assertEqual(self.concentracao(a, b), "", (a, b))
            self.assertEqual(self.conflito(a, b), "", (a, b))

    def test_compativel(self):
        for a, b in [("Lattafa Asad 100ml", "Perfume Lattafa Asad 100ml Eau De Parfum Original"),
                     ("Natura Homem Identidad 100ml", "Deo Parfum Natura Homem Identidad 100ml"),
                     # "Parfum" no nome e EDP na concentração (vigia de 31/10)
                     ("Perfume Banderas The Icon Parfum EDP 100ml",
                      "The Icon The Perfume Antonio Banderas Perfume Masculino 100ml Eau de Parfum"),
                     # "Eau De Parf" cortado no fim do título
                     ("Bareeq Al Dhahab EDP 100ml", "Perfume Árabe Al Wataniah Bareeq Al Dhahab 100ml Eau De Parf"),
                     ("Rayhaan Elixir 100ml", "Perfume Arabe Rayhaan Elixir 100ml Alta Fixacao Edp")]:
            self.assertEqual(self.concentracao(a, b), "", (a, b))
            self.assertEqual(self.conflito(a, b), "", (a, b))


class TestVersaoNosDoisSentidos(Base):
    def test_versao_so_no_grupo(self):
        self.assertIn("versão", self.conflito("Armaf Club de Nuit Intense Man 105ml", "Armaf Club de Nuit Man 105ml"))
        self.assertIn("versão", self.conflito("Lattafa Yara Moi 100ml", "Perfume Lattafa Yara 100ml Feminino"))

    def test_versao_so_no_anuncio_continua(self):
        self.assertIn("versão", self.conflito("Armaf Club de Nuit Man 105ml", "Armaf Club de Nuit Intense Man 105ml"))

    def test_enfeites_que_o_anuncio_omite_nao_sao_outro_produto(self):
        self.assertEqual(self.conflito("Club de Nuit Intense Man Limited Edition Parfum 105ml",
                                       "Armaf Club de Nuit Intense Man Parfum 105ml"), "")
        # "Extrait" no grupo e "Extrato" no anúncio: o mesmo extrait
        self.assertEqual(self.conflito("CLUB DE NUIT INTENSE MAN EXTRAIT",
                                       "Extrato de perfume Armaf Club de Nuit Intense Masculino 70 mL 2025"), "")


class TestPontaAPonta(unittest.TestCase):
    """Antes: grupo "Sauvage Parfum" com link do Sauvage EDT saía IMPERDÍVEL 82; "Club de Nuit Intense" com link do Club
    de Nuit saía IMPERDÍVEL 92."""

    def avaliar(self, grupo, titulo):
        hist = [(grupo, 200 + (d % 3) * 5, d) for d in range(1, 15)]
        card = CardML(ok=True, titulo=titulo, preco=140, vendedor="Loja", loja_oficial=True)
        cur = Curadora(CFG, banco_com_historico(hist), MLFalso({"https://meli.la/a": card}))
        return cur.avaliar(f"*{grupo}*\nPor R$ 140\nhttps://meli.la/a", agora=T0 + 20 * DIA)

    def test_concentracao_trocada_nao_e_excelente_e_avisa(self):
        av = self.avaliar("Dior Sauvage Parfum 100ml", "Perfume Dior Sauvage Eau de Toilette 100ml")
        self.assertEqual(av.conflito, "")
        self.assertLessEqual(av.score, 74)
        self.assertNotEqual(av.nota, "NÃO VALE")
        self.assertTrue(any("concentração diferente entre o texto e o anúncio (PARFUM x EDT)" in a for a in av.alertas),
                        av.alertas)

    def test_versao_que_falta_no_anuncio_nao_vale(self):
        av = self.avaliar("Armaf Club de Nuit Intense Man 105ml", "Armaf Club de Nuit Man 105ml")
        self.assertIn("versão", av.conflito)
        self.assertEqual(av.nota, "NÃO VALE")

    def test_mesmo_produto_com_concentracao_so_no_anuncio_continua_bom(self):
        av = self.avaliar("Lattafa Asad 100ml", "Perfume Lattafa Asad 100ml Eau De Parfum Masculino")
        self.assertEqual(av.conflito, "")
        self.assertIn(av.nota, ("IMPERDÍVEL", "BOA"))
        self.assertFalse(any("concentração diferente" in a for a in av.alertas), av.alertas)


if __name__ == "__main__":
    unittest.main()
