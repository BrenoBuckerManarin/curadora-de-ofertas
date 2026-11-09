"""A página oficial de cupons dos afiliados do ML como sinal de "esse cupom acabou" (13/10)."""
from __future__ import annotations

import json
import time
import unittest

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico

from curadora import pagina_cupons as pagina
from curadora.esgotados import esgotados, registrar_sinal
from curadora.ml import CardML
from curadora.score import Curadora

AGORA = T0 + 15 * DIA
HOJE = time.strftime("%d/%m/%Y", time.localtime(AGORA))
MES_QUE_VEM = time.strftime("%d/%m/%Y", time.localtime(AGORA + 30 * DIA))
ONTEM = time.strftime("%d/%m/%Y", time.localtime(AGORA - DIA))
ASAD = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197, 200, 204])]


def html(codigos: dict[str, str], atualizada: str = "13/10/2026 18:16") -> str:
    lista = [{"nome": c, "dia_inicio": "01/10/2026", "dia_fim": fim, "valor_desconto": "25,00%", "min_compra": "1",
              "desconto_max": "500", "is_mar_aberto": False} for c, fim in codigos.items()]
    return (f"<p>Atualizado em {atualizada}</p><script>const COUPONS = " + json.dumps(lista, indent=1)
            + ";\nconst X = 1;</script>")


def lote(n: int = 24, fim: str = MES_QUE_VEM) -> dict[str, str]:
    return {f"LISTA{i:02d}": fim for i in range(n)}


class Base(unittest.TestCase):
    def setUp(self):
        self.con = banco_com_historico(ASAD)
        pagina._cache.clear()

    def usado_nos_grupos(self, *codigos, ha_s=3600):
        for c in codigos:
            self.con.execute("INSERT OR REPLACE INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, minimo, limite) "
                             "VALUES (?,?,?,1,10,149,200)", (c, AGORA - ha_s, AGORA - ha_s))
        self.con.commit()

    def le(self, codigos, quando=AGORA, atualizada="13/10/2026 18:16"):
        return pagina.aplicar(self.con, pagina.ler(html(codigos, atualizada)), quando)


class TestLerAPagina(unittest.TestCase):
    def test_le_os_codigos_a_validade_e_a_hora_da_atualizacao(self):
        p = pagina.ler(html({**lote(), "MELHORDEHJ": "13/10/2026", "-": "31/10/2026"}))
        self.assertEqual(p["atualizada"], "13/10/2026 18:16")
        self.assertEqual(len(p["cupons"]), 25)                   # o "-" (sem código) fica de fora
        self.assertEqual(p["cupons"]["MELHORDEHJ"], {"fim": "2026-10-13", "inicio": "2026-10-01", "lista": False,
                                                     "regra": {"pct": 25.0, "minimo": 1.0, "limite": 500.0},
                                                     "categoria": "", "acao": "", "mar_aberto": False})

    def test_17_10_le_a_categoria_e_se_vale_no_site_todo(self):
        lista = [{"nome": c, "dia_inicio": "01/10/2026", "dia_fim": "31/10/2026", "valor_desconto": "25,00%",
                  "min_compra": "1", "desconto_max": "500", "is_mar_aberto": False} for c in lote()]
        lista.append({"nome": "OFFNOMELIHOJE", "acao": "Tech e Home Electronics", "dia_inicio": "17/10/2026",
                      "dia_fim": "17/10/2026", "valor_desconto": "10,00%", "min_compra": "149", "desconto_max": "200",
                      "container_url": "", "container_name": "", "is_mar_aberto": False,
                      "categoria": "Tecnologia e Eletrodomésticos"})
        p = pagina.ler("<p>Atualizado em 17/10/2026 14:57</p><script>const COUPONS = " + json.dumps(lista, indent=1)
                       + ";\nconst X = 1;</script>")
        c = p["cupons"]["OFFNOMELIHOJE"]
        self.assertEqual((c["categoria"], c["mar_aberto"], c["lista"]), ("Tecnologia e Eletrodomésticos", False, False))
        self.assertFalse(pagina._do_dia(c, "2026-10-17"))                      # é de Tecnologia: não vale para perfume
        self.assertTrue(pagina._do_dia(dict(c, mar_aberto=True), "2026-10-17"))
        self.assertTrue(pagina._do_dia(dict(c, categoria="Beleza e Cuidado Pessoal"), "2026-10-17"))

    def test_regra_que_nao_faz_sentido_fica_de_fora(self):
        # a página já trouxe "10000,00%" (MELIMAIS) e "R$ 0,25" (ACHEINOMELI)
        lista = [{"nome": "X" * 5, "valor_desconto": "10000,00%", "min_compra": "799", "desconto_max": "100"},
                 {"nome": "Y" * 5, "valor_desconto": "R$ 0,25", "min_compra": "1", "desconto_max": "500"}]
        self.assertEqual([pagina._regra(c) for c in lista], [{}, {}])

    def test_pagina_quebrada_ou_pela_metade_nao_serve(self):
        self.assertIsNone(pagina.ler("<html>manutenção</html>"))
        self.assertIsNone(pagina.ler("const COUPONS = [{\"nome\": \"X\"\n];"))
        self.assertIsNone(pagina.ler(html(lote(5))))            # poucos cupons: veio cortada


class TestSaiuDaPagina(Base):
    def test_cupom_usado_nos_grupos_que_sai_da_pagina_conta_como_esgotado(self):
        self.usado_nos_grupos("OFERTADEHOJE")
        self.assertEqual(self.le({**lote(), "OFERTADEHOJE": HOJE})["avisados"], [])       # 1ª leitura: só anota
        resumo = self.le(lote(), AGORA + 3600, "13/10/2026 19:20")
        self.assertEqual(resumo["avisados"], ["OFERTADEHOJE"])
        morto = esgotados(self.con, AGORA + 3700)["OFERTADEHOJE"]
        self.assertEqual(morto["fonte"], "pagina")
        self.assertIn("saiu da página oficial de cupons do ML (atualizada em 13/10/2026 19:20)", morto["motivo"])
        self.assertEqual(self.le(lote(), AGORA + 7200)["avisados"], [])                    # não avisa de novo

    def test_cupom_que_os_grupos_nao_usam_nao_vira_aviso(self):
        self.le({**lote(), "DESCONHECIDO": HOJE})
        self.assertEqual(self.le(lote(), AGORA + 3600)["avisados"], [])
        self.assertEqual(esgotados(self.con, AGORA + 3700), {})

    def test_a_pagina_nunca_ressuscita_cupom(self):
        self.usado_nos_grupos("MODA1310")
        registrar_sinal(self.con, "MODA1310", AGORA, "esgotou", "breno", "você tirou do post", "e1")
        self.con.commit()
        self.le({**lote(), "MODA1310": HOJE}, AGORA + 600)       # a página (atrasada) ainda mostra o cupom
        self.assertIn("MODA1310", esgotados(self.con, AGORA + 700))

    def test_03_11_o_indicio_da_pagina_nao_expira_e_sai_quando_o_cupom_volta_para_ela(self):
        self.usado_nos_grupos("OFERTADEHOJE", "MODA1310")
        self.le({**lote(), "OFERTADEHOJE": HOJE, "MODA1310": HOJE})
        self.assertEqual(self.le(lote(), AGORA + 3600, "13/10/2026 19:20")["avisados"], ["MODA1310", "OFERTADEHOJE"])
        registrar_sinal(self.con, "MODA1310", AGORA + 3650, "esgotou", "breno", "você tirou do post", "e1")
        self.con.commit()
        self.assertEqual(esgotados(self.con, AGORA + 30 * 3600)["OFERTADEHOJE"]["estado"], "indício")    # não expira
        self.le({**lote(), "OFERTADEHOJE": HOJE, "MODA1310": HOJE}, AGORA + 31 * 3600, "14/10/2026 18:20")   # voltaram
        depois = esgotados(self.con, AGORA + 31 * 3600 + 60)
        self.assertNotIn("OFERTADEHOJE", depois)
        self.assertEqual(depois["MODA1310"]["estado"], "suspenso")            # a sua retirada não sai com a página

    def test_04_11_volta_para_a_pagina_depois_de_14_dias_tambem_conta(self):
        # a página esquece o "saiu" depois de 14 dias (LEMBRAR_S); a volta é vista pelo sinal, não pelo estado dela
        self.usado_nos_grupos("OFERTADEHOJE")
        registrar_sinal(self.con, "OFERTADEHOJE", AGORA - 16 * DIA, "esgotou", "pagina", "saiu da página", "p-antigo")
        self.con.commit()
        self.assertEqual(esgotados(self.con, AGORA)["OFERTADEHOJE"]["estado"], "indício")
        self.le({**lote(), "OFERTADEHOJE": HOJE}, AGORA)
        self.assertNotIn("OFERTADEHOJE", esgotados(self.con, AGORA + 60))
        self.le({**lote(), "OFERTADEHOJE": HOJE}, AGORA + 7200, "13/10/2026 20:16")   # continua na página: não regrava
        self.assertEqual(self.con.execute("SELECT count(*) FROM cupom_sinais WHERE codigo = 'OFERTADEHOJE' AND "
                                          "tipo = 'ativo'").fetchone()[0], 1)

    def test_dezenas_saindo_antes_do_fim_da_validade_e_defeito_da_pagina(self):
        self.usado_nos_grupos(*list(lote(40))[:20])
        self.le(lote(40))
        resumo = self.le(lote(22), AGORA + 3600)                 # 18 somem de uma vez, todos ainda válidos
        self.assertTrue(resumo["em_massa"])
        self.assertEqual((resumo["avisados"], resumo["sairam"]), ([], []))
        self.assertEqual(self.le(lote(40), AGORA + 7200)["avisados"], [])                 # a página voltou ao normal

    def test_dezenas_saindo_porque_a_validade_acabou_e_normal(self):
        vencem_hoje = {f"DIA{i:02d}": ONTEM for i in range(18)}
        self.usado_nos_grupos("DIA00", "DIA01")
        self.le({**lote(), **vencem_hoje})
        resumo = self.le(lote(), AGORA + 3600)
        self.assertFalse(resumo["em_massa"])
        self.assertEqual(resumo["avisados"], ["DIA00", "DIA01"])

    def test_depois_de_muito_tempo_sem_ler_nao_inventa_a_hora_em_que_acabou(self):
        self.usado_nos_grupos("OFERTADEHOJE", ha_s=600)
        self.le({**lote(), "OFERTADEHOJE": HOJE}, AGORA - 20 * 3600)
        resumo = self.le(lote(), AGORA)
        self.assertEqual(resumo["avisados"], [])                 # sem aviso de "esgotou agora"...
        self.assertIn("OFERTADEHOJE", pagina.fora_da_pagina(self.con, AGORA + 60, ["OFERTADEHOJE"]))   # ...mas fora


class TestNaNota(Base):
    PROMO = "*Lattafa Asad 100ml*\nPor R$ 150\n🎟️ Cupom: PROMODEHOJE\nhttps://meli.la/asad"

    def setUp(self):
        super().setUp()
        self.usado_nos_grupos("PROMODEHOJE", ha_s=300)
        self.ml = MLFalso({"https://meli.la/asad": CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=165)})
        self.cur = Curadora(CFG, self.con, self.ml)
        self.le({**lote(), "PROMODEHOJE": HOJE}, AGORA - 7200)
        self.le(lote(), AGORA - 3600)                            # saiu da página há 1h

    def test_o_cupom_que_saiu_da_pagina_nao_segura_o_preco(self):
        av = self.cur.avaliar(self.PROMO, agora=AGORA)
        self.assertIn("PROMODEHOJE", av.cupons_mortos)
        self.assertEqual(av.preco_efetivo, 165)                  # vale o preço do ML, sem o cupom
        # 04/11 (Astra): sumir da página é indício, não "esgotado"
        self.assertTrue(any(a.startswith("cupom PROMODEHOJE indício: saiu da página oficial de cupons") for a in av.alertas))

    def test_o_breno_manter_o_cupom_num_post_vale_mais_que_a_pagina(self):
        registrar_sinal(self.con, "PROMODEHOJE", AGORA - 600, "ativo", "breno", "você manteve no post", "e9")
        self.con.commit()
        self.assertEqual(pagina.fora_da_pagina(self.con, AGORA, ["PROMODEHOJE"]), {})
        self.assertNotIn("PROMODEHOJE", self.cur.avaliar(self.PROMO, agora=AGORA).cupons_mortos)

    def test_01_11_o_aviso_da_equipe_no_grupofinal_vale_mais_que_a_pagina(self):
        # o aviso liga a rodada de cupom (esgotados.avisos_com_prova): o código que saiu da página em outra campanha não
        # pode deixar a rodada ir sem o cupom
        registrar_sinal(self.con, "PROMODEHOJE", AGORA - 600, "aviso", "equipe", "a equipe avisou no GrupoFinal", "a1")
        self.con.commit()
        self.assertEqual(pagina.fora_da_pagina(self.con, AGORA, ["PROMODEHOJE"]), {})

    def test_se_o_cupom_volta_para_a_pagina_deixa_de_estar_fora(self):
        self.le({**lote(), "PROMODEHOJE": MES_QUE_VEM}, AGORA - 60)
        self.assertEqual(pagina.fora_da_pagina(self.con, AGORA, ["PROMODEHOJE"]), {})

    def test_04_11_fora_da_pagina_e_indicio(self):
        # 04/11 (Astra): sumir da página (que pode estar atrasada) é indício, não aviso de que acabou
        self.assertTrue(pagina.fora_da_pagina(self.con, AGORA, ["PROMODEHOJE"])["PROMODEHOJE"].startswith(
            "indício: saiu da página oficial de cupons do ML em "))

    def test_depois_de_14_dias_o_codigo_pode_ser_de_outra_campanha(self):
        self.assertEqual(pagina.fora_da_pagina(self.con, AGORA + 15 * DIA, ["PROMODEHOJE"]), {})

    def test_cupom_que_nunca_passou_pela_pagina_nao_muda(self):
        self.assertEqual(pagina.fora_da_pagina(self.con, AGORA, ["MODA1310", "FULL1310"]), {})


class TestLeitor(Base):
    def test_pagina_fora_do_ar_nao_mexe_em_nada(self):
        self.usado_nos_grupos("OFERTADEHOJE")
        self.le({**lote(), "OFERTADEHOJE": HOJE})
        leitor = pagina.Leitor(self.con, baixar_=lambda: "<html>502 Bad Gateway</html>")
        self.assertIsNone(leitor.uma_vez(AGORA + 3600))
        self.assertEqual(esgotados(self.con, AGORA + 3700), {})

    def test_leitura_normal(self):
        self.usado_nos_grupos("OFERTADEHOJE")
        self.le({**lote(), "OFERTADEHOJE": HOJE})
        leitor = pagina.Leitor(self.con, baixar_=lambda: html(lote(), "13/10/2026 21:16"))
        self.assertEqual(leitor.uma_vez(AGORA + 3600)["avisados"], ["OFERTADEHOJE"])
        self.assertEqual(leitor.ultima["atualizada"], "13/10/2026 21:16")


if __name__ == "__main__":
    unittest.main()
