"""Cupons do momento: leitura das regras e melhor cupom para um preço."""
from __future__ import annotations

import datetime as dt
import time
import unittest

import apoio  # noqa: F401

from curadora.banco import conectar
from curadora.cupons import cupons_da_mensagem, melhor_cupom, registrar_cupons

ANUNCIO = """🔥 Novos cupons no Mercado Livre!

```LOJASOFICIAIS03``` 🎟️
10% OFF acima de R$ 89
Limite de R$ 20

```MODA0310``` 🎟️
15% OFF acima de R$ 79
Limite de R$ 30"""

ANUNCIO_2 = "*CUPOM LIBERADO NO MERCADO LIVRE*\n\n 🎟️ *MODA0310*\n15% OFF acima de R$ 79\nLimite de R$ 30."
PROMO = "*Lattafa Asad 100ml*\nPor R$ 180\n🎟️ Cupom: *MODA0310*\nhttps://meli.la/a"
FIXO = "🔥 Novo cupom no Mercado Livre!\n✅ *R$20,00 OFF* em compras acima de R$60\n🎟 Cupom:\n```0F35T4S3CUP0M```"


class TestCupons(unittest.TestCase):
    def test_le_codigo_e_regra(self):
        c = cupons_da_mensagem(ANUNCIO_2)
        self.assertEqual((c["MODA0310"].pct, c["MODA0310"].minimo, c["MODA0310"].limite), (15, 79, 30))

    def test_regra_de_valor_fixo(self):
        c = cupons_da_mensagem(FIXO)
        self.assertEqual((c["0F35T4S3CUP0M"].valor, c["0F35T4S3CUP0M"].minimo), (20, 60))

    def test_anuncio_com_codigo_sozinho_na_linha(self):
        c = cupons_da_mensagem("🔥 Novo cupom no Mercado Livre!\n\n*MODA E BEM-ESTAR NO MELI*\n```MODA10BELEZA```\n\n"
                               "10% OFF, mínimo de R$ 89\nLimite de R$ 20\n\nAtive aqui: https://mercadolivre.com/sec/x")
        self.assertEqual(list(c), ["MODA10BELEZA"])
        self.assertEqual((c["MODA10BELEZA"].pct, c["MODA10BELEZA"].minimo, c["MODA10BELEZA"].limite), (10, 89, 20))

    def test_regra_separada_do_codigo_por_linha_em_branco(self):
        # 10/10: o OFERTAMELI ficou "regra desconhecida" porque o anúncio vem com linhas em branco no meio.
        for anuncio in ("🔥 Novo cupom no Mercado Livre!\n\n✅ *15% OFF* em compras acima de *R$ 79*, limitado a *R$ 60*\n\n"
                        "🎟️ Cupom:\n```OFERTAMELI```\n\n🛒 Resgate aqui: https://mercadolivre.com/sec/2F94Xkz",
                        "💛 *+1 CUPOM NOVO MELI* 💛\n\n👉🏻 _15% OFF compras acima de R$79 limitado a R$60_\n\n"
                        "🎟 Código: `OFERTAMELI`\n\nResgate Aqui: https://mercadolivre.com/sec/2Zx7hRw"):
            c = cupons_da_mensagem(anuncio)
            self.assertEqual((c["OFERTAMELI"].pct, c["OFERTAMELI"].minimo, c["OFERTAMELI"].limite), (15, 79, 60))

    def test_regra_da_equipe_vale_mais_que_a_dos_outros_grupos(self):
        # 10/10: MODA1010 saiu "limitado a R$ 30" num grupo e "máx R$40" no aviso da equipe (era R$ 40).
        con = conectar(":memory:")
        registrar_cupons(con, "✅ 20% OFF em moda, limitado a R$ 30 OFF\n🎟️ Cupom:\n```MODA1010```", 1000, True)
        registrar_cupons(con, "*CUPOM MERCADO LIVRE*\n🎟️ *MODA1010*\n20% OFF máx R$40 em Moda & Bem-Estar", 1100, True,
                         da_equipe=True)
        regra = lambda: tuple(con.execute("SELECT pct, minimo, limite FROM cupons WHERE codigo = 'MODA1010'").fetchone())
        self.assertEqual(regra(), (20, None, 40))
        # outro grupo repete o R$ 30 e traz o mínimo: completa o que faltava, não troca o teto
        registrar_cupons(con, "🎟 Cupom: MODA1010\n20% OFF acima de R$ 89, limitado a R$ 30", 1200, True)
        self.assertEqual(regra(), (20, 89, 40))
        # post normal da equipe (sem regra escrita) não mexe em nada
        registrar_cupons(con, "*Asad 100ml*\nPor *R$ 105*\nCupom: *MODA1010* 🎟️", 1300, True, da_equipe=True)
        self.assertEqual(regra(), (20, 89, 40))

    def test_desconto_da_promo_nao_vira_regra_do_cupom(self):
        # Numa promo comum, o "15% OFF" de um parágrafo acima é do preço, não do cupom.
        c = cupons_da_mensagem("*Lattafa Asad 100ml*\n15% OFF no Pix hoje\n\nPor R$ 180\n\n🎟️ Cupom: OFERTAMELI\n"
                               "https://meli.la/a mercado livre")
        self.assertIsNone(c["OFERTAMELI"].pct)

    def test_varios_cupons_nao_misturam_regras(self):
        c = cupons_da_mensagem(ANUNCIO + "\nmercado livre")
        self.assertEqual((c["LOJASOFICIAIS03"].pct, c["LOJASOFICIAIS03"].limite), (10, 20))
        self.assertEqual((c["MODA0310"].pct, c["MODA0310"].limite), (15, 30))

    def test_ignora_cupom_de_outra_loja(self):
        self.assertEqual(cupons_da_mensagem("🔥 Novo cupom no Magalu!\n✅ R$ 400 OFF\n🎟 Cupom: ```LU400```"), {})

    def test_cupom_de_valor_fixo_sem_minimo_nao_vira_desconto(self):
        from curadora.cupons import Regra
        self.assertEqual(Regra(valor=400).desconto(180), 0)
        self.assertEqual(Regra(valor=100, minimo=50).desconto(180), 180 * 0.35)

    def test_melhor_cupom_so_entre_os_ativos_para_perfume(self):
        con = conectar(":memory:")
        t = int(time.mktime(dt.datetime(2026, 10, 3, 10, 0).timetuple()))  # 03/10: o MODA0310 vale hoje
        registrar_cupons(con, ANUNCIO, t, em_promo_de_perfume=False)   # anúncio: ainda sem prova em perfume
        self.assertIsNone(melhor_cupom(con, 180, t + 1000))
        registrar_cupons(con, PROMO, t + 500, em_promo_de_perfume=True)      # visto numa promo de perfume
        m = melhor_cupom(con, 180, t + 1000)
        self.assertEqual((m["codigo"], m["desconto"], m["preco_final"]), ("MODA0310", 27.0, 153.0))
        self.assertEqual(melhor_cupom(con, 70, t + 1000), None)               # abaixo do mínimo de R$79
        self.assertIsNone(melhor_cupom(con, 180, t + 500 + 7 * 3600))       # passou de 6h: não está mais ativo

    def test_cupom_com_data_vence_no_dia_seguinte(self):
        from curadora.cupons import cupons_vencidos
        dia_6 = time.mktime(dt.datetime(2026, 10, 6, 19, 0).timetuple())
        self.assertEqual(cupons_vencidos(["MODA0610", "TODOSITE0410", "ESPECIALML", "FULL0510"], dia_6),
                         ["TODOSITE0410", "FULL0510"])                       # sem data no código: não dá para saber


class TestAvisoComARegraAcimaDoCodigo(unittest.TestCase):
    """13/10: nos avisos com vários cupons e a regra ACIMA do código, cada cupom ficava com a regra do seguinte."""

    ACIMA = ("📢 *CUPONS NOVOS NO MERCADO LIVRE*‼️   \n\n🔥 _Ganhe 30% OFF, limite de R$500_  \n\n"
             "🎟️ Cupom: *SALVAMEUBOLSO* \n\n🔥 _Ganhe 25% OFF, limite de R$500_  \n\n🎟️ Cupom: *ACHEINOMELI* \n\n"
             "🔥 _Ganhe 22% OFF, limite de R$500_  \n\n🎟️ Cupom: *MUITOBARATO* \n\n"
             "🔥 _Ganhe 18% OFF, limite de R$500_  \n\n🎟️ Cupom: *MELHORDODIA* \n\n"
             "🔥 _Ganhe 10% OFF em compras acima de R$199_  \n\n🎟️ Cupom: *OPORTUNIDADEML* \n\n"
             "🔥 _Ganhe 08% OFF em compras acima de R$149_  \n\n🎟️ Cupom: *CUSTAPOUCO* \n\n"
             "👉 *Pesquise o produto e ative o cupom:* https://mercadolivre.com/sec/2Njqmk1\n\nAgora, no MELI!")
    ABAIXO = ("💛 *CUPONS ATIVOS NO MELI* 💛\n\n✨ Resgate Aqui: https://mercadolivre.com/sec/2Zx7hRw\n\n"
              "🎟 Código: `CUPOMEXCLUSIVO`\n👉🏻 _10% OFF compras acima de R$199 limitado a R$100_\n\n"
              "🎟 Código: `MELIOFFAQUI`\n👉🏻 _25% OFF compras acima de R$1 limitado a R$500_\n\n"
              "🎟 Código: `CORREAQUINOML`\n👉🏻 _30% OFF compras acima de R$1 limitado a R$500_\n\n"
              "✨ Resgate Aqui: https://mercadolivre.com/sec/2Zx7hRw")
    NA_LINHA = ("*NOVOS CUPONS MERCADO LIVRE*\n\n🎟️ *10% OFF*, acima de R$ 149, limite de R$ 200 - Cupom: *PROMODEHOJE*\n\n"
                "🎟️ *30% OFF*, acima de R$ 1, limite de R$ 500 - Cupom: *PARAVC*\n\n"
                "🎟️ *22% OFF*, acima de R$ 1, limite de R$ 500 - Cupom: *MELIOFFHJ*\n\n"
                "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")

    def test_cada_cupom_fica_com_a_regra_que_esta_acima_dele(self):
        from curadora.cupons import cupons_da_mensagem
        r = cupons_da_mensagem(self.ACIMA)
        self.assertEqual({c: (x.pct, x.limite, x.minimo) for c, x in r.items()}, {
            "SALVAMEUBOLSO": (30.0, 500.0, None), "ACHEINOMELI": (25.0, 500.0, None), "MUITOBARATO": (22.0, 500.0, None),
            "MELHORDODIA": (18.0, 500.0, None), "OPORTUNIDADEML": (10.0, None, 199.0), "CUSTAPOUCO": (8.0, None, 149.0)})

    def test_aviso_com_a_regra_abaixo_continua_como_era(self):
        from curadora.cupons import cupons_da_mensagem
        r = cupons_da_mensagem(self.ABAIXO)
        self.assertEqual({c: (x.pct, x.minimo, x.limite) for c, x in r.items()}, {
            "CUPOMEXCLUSIVO": (10.0, 199.0, 100.0), "MELIOFFAQUI": (25.0, 1.0, 500.0), "CORREAQUINOML": (30.0, 1.0, 500.0)})

    def test_aviso_com_a_regra_na_mesma_linha_continua_como_era(self):
        from curadora.cupons import cupons_da_mensagem
        r = cupons_da_mensagem(self.NA_LINHA)
        self.assertEqual({c: (x.pct, x.minimo, x.limite) for c, x in r.items()}, {
            "PROMODEHOJE": (10.0, 149.0, 200.0), "PARAVC": (30.0, 1.0, 500.0), "MELIOFFHJ": (22.0, 1.0, 500.0)})

    def test_promo_comum_com_um_cupom_nao_muda(self):
        from curadora.cupons import cupons_da_mensagem
        promo = ("ASAD 15% OFF NO PIX 🥷\n\n*Lattafa Asad 100ml*\nDe R$ 200 por R$ 150\n"
                 "🎟️ Cupom: MODA1110\n15% OFF acima de R$ 89, limite de R$ 40\n\nhttps://meli.la/asad")
        r = cupons_da_mensagem(promo)["MODA1110"]
        self.assertEqual((r.pct, r.minimo, r.limite), (15.0, 89.0, 40.0))


class TestFormatosDoAvisoDe0311(unittest.TestCase):
    """03/11 (relatório da Astra): formatos de aviso que o leitor perdia, sem afrouxar o que não é regra de cupom."""

    def regra(self, texto, codigo):
        r = cupons_da_mensagem(texto)[codigo]
        return (r.pct, r.valor, r.minimo, r.limite)

    def test_percentual_sem_off_seguido_da_compra_minima(self):
        # aviso da equipe das 18:07: a rodada só ligou às 18:36
        aviso = ("*CUPOM MERCADO LIVRE*\n\n🎟️  *SITE031115*\n15% acima de R$89 máx R$20 no Mercado Livre\n\n"
                 "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        self.assertEqual(self.regra(aviso, "SITE031115"), (15.0, None, 89.0, 20.0))
        self.assertEqual(self.regra("Cupom: SITEDEZ\n10% de desconto em compras acima de R$ 79", "SITEDEZ"),
                         (10.0, None, 79.0, None))

    def test_desconto_em_reais_com_a_minima_depois_da_barra(self):
        # aviso de 01/11 12:03
        aviso = ("*CUPOM MERCADO LIVRE*\n🎟️ *SITE200111* - R$20/299\n🎟️ *100TODOSITE* - R$100/999\n"
                 "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        self.assertEqual(self.regra(aviso, "SITE200111"), (None, 20.0, 299.0, None))
        self.assertEqual(self.regra(aviso, "100TODOSITE"), (None, 100.0, 999.0, None))

    def test_o_que_nao_e_regra_do_cupom_continua_fora(self):
        vazio = (None, None, None, None)
        self.assertEqual(self.regra("*Asad*\nPor R$ 180 com 15% no Pix\nCupom: ASADPIX", "ASADPIX"), vazio)
        self.assertEqual(self.regra("*Asad*\nPor R$ 180\n15% de desconto no Pix\nCupom: ASADPIX", "ASADPIX"), vazio)
        self.assertEqual(self.regra("*Kit*\n15% a partir de 2 unidades\nCupom: KITDOIS", "KITDOIS"), vazio)
        self.assertEqual(self.regra("*Asad*\nPor R$ 180 até 03/11\nCupom: DATA0311", "DATA0311"), vazio)
        self.assertEqual(self.regra("*Lenço*\nR$ 2,99/un\nCupom: LENCO10", "LENCO10"), vazio)
        # "50/499" sem o R$ não dá para separar de uma data; a mínima de R$499 já o deixaria fora do gatilho
        self.assertEqual(self.regra("🎟️ *50TODOSITE*  - 50/499", "50TODOSITE"), vazio)

    def test_o_desconto_do_outro_lado_do_ou_nao_e_deste_cupom(self):
        # 03/11 18:40, One Million: o R$18 era outro cupom, e o cadastro do SITE031115 ficou com 15% e R$18
        promo = ("ONE MILLION 100ML PELA METADE DO PREÇO 🥷\n\n✅ De R$ ~799,99~ por R$ 381,00\n"
                 "🏷️ Cupom: `R$ 18,00 OFF` ou `SITE031115`\n\n"
                 "🎟️ Resgate o cupom: adicione ambos os cupons para chegar ao valor final\n"
                 "🛒 Compre aqui: https://meli.la/uPsArY0")
        self.assertEqual(self.regra(promo, "SITE031115"), (None, None, None, None))
        self.assertEqual(self.regra("Cupom: CUPOMAA ou CUPOMBB 10% OFF acima de R$ 79", "CUPOMBB"), (10.0, None, 79.0, None))
        self.assertEqual(self.regra("Cupom: SITEDEZ (15% OFF) válido no app ou site, acima de R$ 99", "SITEDEZ"),
                         (15.0, None, 99.0, None))


if __name__ == "__main__":
    unittest.main()
