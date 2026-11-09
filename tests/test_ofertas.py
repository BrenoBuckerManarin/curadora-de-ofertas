"""A escolha da melhor oferta conhecida de cada perfume (04/11, fase 1, só sombra): confiança em três categorias,
identidade em três níveis, cupom por anúncio, Pix com Pix e a frase "examinei, descartei, escolheria"."""
from __future__ import annotations

import json
import unittest

from apoio import CFG, FIXTURES

from curadora import comparador, ofertas as o, radar
from curadora.identidade import Identificador

IDENT = Identificador(CFG["marcas"], CFG.get("sinonimos"))
AGORA = 1_793_800_000
TITULO = "Perfume Árabe Masculino Asad Bourbon Lattafa Eau De Parfum 100ml"
CHAVE = "asad-bourbon|100"


def obs(anuncio, preco, vendedor="LOJA", oficial=True, ts=AGORA - 600, base="pagina", titulo=TITULO, chave=CHAVE,
        reputacao=None, cupons=(), selo_de="pagina", **extra):
    return o.Observacao(anuncio=anuncio, ts=ts, fonte="grupo", base=base, chave=chave, titulo=titulo,
                        vendedor=vendedor, oficial=oficial, selo_de=selo_de, reputacao=reputacao, preco=preco,
                        cupons=list(cupons), **extra)


def rep(vendas, classe="boa"):
    return {"vendas": vendas, "classe": classe, "nivel": "5_green"}


class TestReguas(unittest.TestCase):
    def test_a_regua_e_o_selo_da_api_sao_os_mesmos_do_comparador_e_do_radar(self):
        self.assertEqual((o.PCT_MIN, o.REAIS_MIN), (comparador.PRIORIDADE_PCT, comparador.PRIORIDADE_RS))
        self.assertEqual(o.OFICIAL_API_MIN_VENDAS, radar.OFICIAL_API_MIN_VENDAS)


class TestCategoria(unittest.TestCase):
    def test_loja_da_marca_revenda_com_selo_e_sem_selo(self):
        self.assertEqual(o.categoria(obs("a", 100, "O Boticário")), (o.OFICIAL_DA_MARCA, ""))
        self.assertEqual(o.categoria(obs("a", 100, "SEMIKE")), (o.REVENDA_COM_SELO, ""))
        self.assertEqual(o.categoria(obs("a", 100, "REVENDA", oficial=False, reputacao=rep(50_000))), (o.SEM_SELO, ""))

    def test_sem_selo_fraco_ou_sem_registro(self):
        cat, motivo = o.categoria(obs("a", 100, "X", oficial=False, reputacao=rep(800)))
        self.assertEqual(cat, o.SEM_SELO)
        self.assertIn("800 vendas", motivo)
        self.assertEqual(o.categoria(obs("a", 100, "X", oficial=False)), (o.SEM_SELO, o.SEM_REGISTRO_DA_REPUTACAO))
        self.assertEqual(o.categoria(obs("a", 100, "X", oficial=None)), (o.SEM_SELO, o.SEM_REGISTRO_DO_SELO))

    def test_selo_so_da_api_exige_5_mil_vendas(self):
        self.assertEqual(o.categoria(obs("a", 100, "PERFUMELATTAFA7", selo_de="api", reputacao=rep(6_000))),
                         (o.REVENDA_COM_SELO, ""))
        self.assertEqual(o.categoria(obs("a", 100, "PERFUMELATTAFA7", selo_de="api")), (o.REVENDA_COM_SELO, o.SELO_SO_API))
        cat, motivo = o.categoria(obs("a", 100, "PERFUMELATTAFA7", selo_de="api", reputacao=rep(804)))
        self.assertEqual(cat, o.SEM_SELO)   # 21/10: com 804 vendas o selo só da API não vale
        self.assertIn("804", motivo)


class TestIdentidade(unittest.TestCase):
    def nivel(self, titulo, chave=CHAVE):
        return o.identidade(TITULO, CHAVE, obs("a", 100, titulo=titulo, chave=chave), IDENT)

    def test_os_15_catalogos_reais_do_asad_bourbon_sao_o_mesmo_perfume(self):
        caso = json.loads((FIXTURES / "caso_bourbon_0411.json").read_text(encoding="utf-8"))
        nomes = [c["nome"] for c in caso["catalogo_ml"] if c["chave"] == CHAVE]
        self.assertEqual(len(nomes), 15)
        for nome in nomes:
            self.assertEqual(self.nivel(nome)[0], o.MESMO, nome)

    def test_outro_produto(self):
        for titulo, motivo in (("Perfume Arábe Asad Bourbon 100ml - Marca Isabelle La Belle", "outra marca"),
                               ("Decant Asad Bourbon Lattafa 100ml amostra", "decant"),
                               ("Kit Asad Bourbon Lattafa 100ml + Asad 100ml", "kit"),
                               ("Body Splash Asad Bourbon Lattafa 100ml", "body splash"),
                               ("Perfume Asad Bourbon Lattafa 50ml", "volume"),
                               ("O Boticário The Blend Bourbon Eau de Parfum 100ml", "outra marca")):
            nivel, porque = self.nivel(titulo)
            self.assertEqual(nivel, o.OUTRO, titulo)
            self.assertIn(motivo, porque, titulo)

    def test_em_duvida_fica_a_parte(self):
        self.assertEqual(self.nivel("Perfume Asad Bourbon Lattafa EDT 100ml")[0], o.DUVIDA)   # EDP x EDT
        nivel, porque = self.nivel("Perfume Assad Bourbon Lattafa 100ml", chave="assad-bourbon|100")
        self.assertEqual(nivel, o.DUVIDA)
        self.assertIn("outra chave", porque)


class TestCupomPorAnuncio(unittest.TestCase):
    def setUp(self):
        caso = json.loads((FIXTURES / "caso_bourbon_0411.json").read_text(encoding="utf-8"))
        self.av = {a["id"]: a for a in caso["avaliacoes"]}

    def cupom(self, av_id):
        a = self.av[av_id]
        return o.cupons_da_avaliacao(a["dados"], a["origem"])

    def test_kids_life_o_post_do_anuncio_trazia_o_cupom_e_a_regra_confere(self):
        (c,) = self.cupom(466169)   # grupo, 11:13: R$198 com DESCONTOS; página R$220,15
        self.assertEqual((c.codigo, c.nivel, c.preco), ("DESCONTOS", o.EM_POST, 198.13))

    def test_semike_o_cupom_foi_confirmado_em_outro_anuncio_e_hipotese(self):
        (c,) = self.cupom(465986)   # reconferência 11:02: o cupom "quente" veio de post no GrupoFinal de outro anúncio
        self.assertEqual((c.nivel, c.preco), (o.HIPOTESE, 134.1))
        self.assertIn("não neste anúncio", c.evidencia)
        (c,) = self.cupom(464483)   # grupo 09:19: R$197 com DESCONTOS, mas a página mostrava R$149
        self.assertEqual((c.nivel, c.preco), (o.HIPOTESE, None))
        self.assertIn("não bate", c.evidencia)

    def test_post_do_anuncio_com_preco_que_a_regra_nao_explica_e_hipotese(self):
        # 04/11 13:15, Club de Nuit: R$171 no post; a regra do DESCONTOS na página de R$197,20 dá R$177,48 (3,7%)
        dados = {"ml": {"preco": 197.20, "url": "x"}, "cupons": ["DESCONTOS"], "preco_grupo": 171.0, "preco": 177.48,
                 "cupom_quente": {"codigo": "DESCONTOS", "preco": 177.48, "por": "post no GrupoFinal", "hora": "13:11"}}
        (c,) = o.cupons_da_avaliacao(dados, "post da equipe")
        self.assertEqual(c.nivel, o.HIPOTESE)
        dados["preco_grupo"] = 176.0   # 0,8%: a regra confere
        (c,) = o.cupons_da_avaliacao(dados, "post da equipe")
        self.assertEqual(c.nivel, o.EM_POST)

    def test_cupom_da_pagina_e_confirmado_e_cupom_morto_nao_se_aplica(self):
        dados = {"ml": {"preco": 200.0, "preco_cupom": 185.0, "url": "x"}, "cupons": ["MODA10"],
                 "cupons_mortos": {"MODA10": "esgotado"}}
        niveis = {c.codigo: c.nivel for c in o.cupons_da_avaliacao(dados, "ao vivo")}
        self.assertEqual(niveis, {"cupom da página": o.NO_ANUNCIO, "MODA10": o.NAO_SE_APLICA})


class TestEscolha(unittest.TestCase):
    def escolher(self, ref, *outras, **kw):
        return o.escolher(ref, list(outras), AGORA, IDENT, produto="Asad Bourbon", **kw)

    def test_mesma_categoria_precisa_de_2_por_cento_e_2_reais(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        d = self.escolher(ref, obs("b", 197.0, "SEMIKE"))          # 1,5%: não troca
        self.assertEqual((d.status, d.troca), (o.MANTEM, False))
        self.assertIn("abaixo de 2% e R$2", d.outras[0].motivo)
        d = self.escolher(obs("ref", 60.0, "KID'S LIFE"), obs("b", 58.6, "SEMIKE"))   # 2,3% mas R$1,40
        self.assertFalse(d.troca)
        d = self.escolher(ref, obs("b", 195.0, "SEMIKE"))          # 2,5% e R$5
        self.assertEqual((d.status, d.escolhida.obs.vendedor, d.economia), (o.TROCA_CONFIRMADA, "SEMIKE", 5.0))

    def test_subir_de_categoria_basta_preco_igual_e_nunca_desce(self):
        ref = obs("ref", 200.0, "PASSOSBARBARA", oficial=False, reputacao=rep(30_000))
        d = self.escolher(ref, obs("b", 200.0, "O Boticário"))     # oficial da marca, mesmo preço: troca
        self.assertEqual(d.escolhida.categoria, o.OFICIAL_DA_MARCA)
        d = self.escolher(ref, obs("b", 201.0, "O Boticário"))     # mais cara: não troca
        self.assertFalse(d.troca)
        oficial = obs("ref", 200.0, "O Boticário")
        d = self.escolher(oficial, obs("b", 150.0, "PASSOSBARBARA", oficial=False, reputacao=rep(30_000)))
        self.assertFalse(d.troca)                                  # 25% mais barata, mas confiança menor
        self.assertEqual(d.outras[0].situacao, o.A_PARTE)
        self.assertIn("confiança menor", d.outras[0].motivo)
        self.assertIn("R$ 50,00 mais barata", d.outras[0].motivo)
        d = self.escolher(oficial, obs("b", 150.0, "SEMIKE"))      # um degrau abaixo (revenda com selo): também não
        self.assertFalse(d.troca)
        self.assertIn("confiança menor", d.outras[0].motivo)

    def test_entre_duas_melhorias_vale_a_mais_barata_e_no_empate_a_de_confianca_maior(self):
        ref = obs("ref", 200.0, "REVENDA", oficial=False, reputacao=rep(30_000))
        d = self.escolher(ref, obs("a", 150.0, "SEMIKE"), obs("b", 190.0, "O Boticário"))
        self.assertEqual(d.escolhida.obs.vendedor, "SEMIKE")
        d = self.escolher(ref, obs("a", 150.0, "SEMIKE"), obs("b", 151.0, "O Boticário"))
        self.assertEqual(d.escolhida.obs.vendedor, "O Boticário")   # R$1 de diferença: vale a loja da marca

    def test_lojas_inadequadas_saem_antes_do_preco(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        d = self.escolher(ref, obs("a", 100.0, "FORA", internacional=True),
                          obs("b", 100.0, "FRACA", oficial=False, reputacao=rep(300, "fraca")),
                          obs("c", 100.0, "RISCO", risco="preço 60% abaixo de todos"))
        self.assertFalse(d.troca)
        self.assertEqual({e.situacao for e in d.outras}, {o.DESCARTADA})
        motivos = " ".join(e.motivo for e in d.outras)
        for trecho in ("internacional", "fraco", "risco"):
            self.assertIn(trecho, motivos)

    def test_leitura_velha_so_api_e_identidade_em_duvida_ficam_a_parte(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        d = self.escolher(ref, obs("a", 100.0, "SEMIKE", ts=AGORA - 3 * 3600),
                          obs("b", 100.0, "LIPX", base="api", selo_de="api", reputacao=rep(90_000)),
                          obs("c", 100.0, "OUTRA", titulo="Perfume Asad Bourbon Lattafa EDT 100ml"))
        self.assertEqual((d.status, d.troca), (o.MANTEM, False))
        motivos = {e.obs.vendedor: (e.situacao, e.motivo) for e in d.outras}
        self.assertEqual(motivos["SEMIKE"][0], o.A_PARTE)
        self.assertIn("leitura velha", motivos["SEMIKE"][1])
        self.assertIn("só lida na API", motivos["LIPX"][1])
        self.assertIn("identidade em dúvida", motivos["OUTRA"][1])

    def test_pagina_velha_nao_compara_mesmo_com_leitura_nova_em_outra_base(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        velha = obs("b", 150.0, "SEMIKE", ts=AGORA - 3 * 3600)                     # página de 3 h atrás
        nova = obs("b", 160.0, "SEMIKE", base="vitrine", ts=AGORA - 600)           # a mesma loja, vista na vitrine agora
        d = self.escolher(ref, velha, nova)
        self.assertFalse(d.troca)
        self.assertIn("só vista na vitrine", d.outras[0].motivo)

    def test_api_com_api_vira_melhor_na_api_e_pagina_vence_api(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        ref_api = obs("ref", 230.0, "KID'S LIFE", base="api", selo_de="api", reputacao=rep(90_000))
        alt_api = obs("b", 180.0, "LIPX", base="api", selo_de="api", reputacao=rep(90_000))
        d = self.escolher(ref, ref_api, alt_api)
        self.assertEqual((d.status, d.escolhida.obs.vendedor, d.economia), (o.TROCA_NA_API, "LIPX", 50.0))
        d = self.escolher(ref, ref_api, alt_api, obs("c", 190.0, "SEMIKE"))
        self.assertEqual((d.status, d.escolhida.obs.vendedor), (o.TROCA_CONFIRMADA, "SEMIKE"))

    def test_hipotese_de_cupom_nao_vence_preco_confirmado(self):
        ref = obs("ref", 200.0, "KID'S LIFE")
        alt = obs("b", 205.0, "SEMIKE", cupons=[o.Cupom("DESCONTOS", o.HIPOTESE, 184.5, "aviso geral")])
        d = self.escolher(ref, alt)
        self.assertFalse(d.troca)
        self.assertEqual([(x.vendedor, c.preco) for x, c in d.possiveis_com_cupom], [("SEMIKE", 184.5)])

    def test_cupom_da_pagina_conta_como_confirmado(self):
        alt = obs("b", 210.0, "SEMIKE", cupons=[o.Cupom("cupom da página", o.NO_ANUNCIO, 190.0)])
        d = self.escolher(obs("ref", 200.0, "KID'S LIFE"), alt)
        self.assertEqual((d.status, d.escolhida.preco_comparado), (o.TROCA_CONFIRMADA, 190.0))

    def test_publicado_com_cupom_abaixo_da_alternativa_depende_do_cupom(self):
        """04/11 13:15, Club de Nuit: a equipe publicou R$171 com DESCONTOS (a regra não explica: hipótese) e a
        alternativa confirmada saía R$189,99. Trocar subiria o preço do post: fica à parte, não é troca."""
        ref = obs("ref", 197.20, "KLASSEY", cupons=[o.Cupom("DESCONTOS", o.HIPOTESE, 177.48, "GrupoFinal, outro anúncio")])
        d = self.escolher(ref, obs("b", 189.99, "BELTRAO_PRODUTOS"), publicado=171.0)
        self.assertEqual((d.status, d.troca), (o.DEPENDE_DO_CUPOM, False))
        self.assertIn("o publicado (R$ 171,00 com DESCONTOS", d.outras[0].motivo)
        d = self.escolher(ref, obs("b", 165.0, "BELTRAO_PRODUTOS"), publicado=171.0)   # abaixo do publicado: troca
        self.assertEqual((d.status, d.economia_sobre_publicado), (o.TROCA_CONFIRMADA, 6.0))

    def test_radar_e_leitura_da_api(self):
        dados = {"ml": {"url": "https://www.mercadolivre.com.br/p/MLB1", "preco": 150.0, "oficial": True,
                        "vendedor": "SEMIKE", "titulo": TITULO}, "reputacao": rep(90_000)}
        x = o.observacao_da_avaliacao(1, AGORA, "radar do ML", CHAVE, "BOA", 60, dados)
        self.assertEqual((x.base, x.selo_de), ("api", "api"))
        self.assertEqual(o.categoria(x), (o.REVENDA_COM_SELO, ""))

    def test_referencia_com_cupom_provavel_abaixo_da_alternativa_depende_do_cupom(self):
        ref = obs("ref", 200.0, "KID'S LIFE", cupons=[o.Cupom("DESCONTOS", o.EM_POST, 180.0)])
        d = self.escolher(ref, obs("b", 190.0, "SEMIKE"))
        self.assertEqual((d.status, d.troca), (o.DEPENDE_DO_CUPOM, False))
        self.assertIn("depende do cupom", d.outras[0].motivo)

    def test_sem_alternativa_inconclusiva_e_nada_depois_do_horario(self):
        d = self.escolher(obs("ref", 200.0, "KID'S LIFE"))
        self.assertEqual(d.status, o.SEM_ALTERNATIVA)
        self.assertIn("examinei 1 anúncio", d.frase())
        self.assertEqual(self.escolher(obs("ref", None, "KID'S LIFE"), obs("b", 100.0)).status, o.INCONCLUSIVA)
        self.assertEqual(self.escolher(obs("ref", 200.0, "X", oficial=None), obs("b", 100.0)).status, o.INCONCLUSIVA)
        depois = obs("b", 100.0, "SEMIKE", ts=AGORA + 60)   # visto depois da decisão: não existe para ela
        self.assertEqual(self.escolher(obs("ref", 200.0, "KID'S LIFE"), depois).status, o.SEM_ALTERNATIVA)

    def test_escolhida_com_leitura_de_mais_de_30_min_e_reconferir(self):
        d = self.escolher(obs("ref", 200.0, "KID'S LIFE"), obs("b", 150.0, "SEMIKE", ts=AGORA - 40 * 60))
        self.assertTrue(d.troca)
        self.assertFalse(d.pronta)
        self.assertIn("reconferir antes de enviar", d.frase())


class TestCasoAsadBourbon(unittest.TestCase):
    """04/11 11:14: a equipe publicou a KID'S LIFE (R$198 com DESCONTOS; página R$220,15) e a curadora conhecia a
    SEMIKE a R$149 (página conferida às 11:02). Só o que existia até 11:14 entra."""

    @classmethod
    def setUpClass(cls):
        caso = json.loads((FIXTURES / "caso_bourbon_0411.json").read_text(encoding="utf-8"))
        cls.post_ts = caso["post_ts"]
        post = caso["post_da_equipe"][0]
        cls.ref = o.observacao_da_avaliacao("post", post["ts"], "post da equipe", post["chave"], post["nota"],
                                            post["score"], post["dados"])
        cls.outras = [x for x in (o.observacao_da_avaliacao(a["id"], a["ts"], a["origem"], a["chave"], a["nota"],
                                                            a["score"], a["dados"]) for a in caso["avaliacoes"]) if x]
        cls.outras += [o.observacao_do_preco_ml(p["ts"], p["chave"], p["item_id"], p["titulo"], p["preco"],
                                                p["vendedor"], p["oficial"], p["url"], p["fonte"], p["seller_id"])
                       for p in caso["precos_ml"]]
        cls.d = o.escolher(cls.ref, cls.outras, cls.post_ts, IDENT, momento="post", produto=post["produto"],
                           publicado=post["preco"])

    def test_escolheria_a_semike_a_149_confirmado(self):
        d = self.d
        self.assertEqual(self.ref.anuncio, "MLBU3398845320")
        self.assertEqual((d.status, d.escolhida.obs.vendedor, d.escolhida.preco_comparado),
                         (o.TROCA_CONFIRMADA, "SEMIKE", 149.0))
        self.assertEqual(d.escolhida.categoria, o.REVENDA_COM_SELO)   # SEMIKE e KID'S LIFE: revendas com selo
        self.assertEqual(d.referencia.categoria, o.REVENDA_COM_SELO)
        self.assertEqual((d.economia, d.economia_sobre_publicado), (71.15, 49.13))
        self.assertTrue(d.pronta)   # a página da SEMIKE foi conferida às 11:02
        # as outras leituras do próprio anúncio publicado (os posts de grupo da KID'S LIFE) não são alternativa
        self.assertNotIn(self.ref.anuncio, {e.obs.anuncio for e in d.outras})

    def test_o_desconto_da_semike_fica_como_hipotese(self):
        possiveis = [(x.vendedor, c.codigo, c.nivel, c.preco) for x, c in self.d.possiveis_com_cupom]
        self.assertIn(("SEMIKE", "DESCONTOS", o.HIPOTESE, 134.1), possiveis)

    def test_o_que_foi_visto_depois_do_post_nao_entra(self):
        depois = [x for x in self.outras if x.ts > self.post_ts]
        self.assertTrue(depois)   # há leituras depois das 11:14 no arquivo
        examinadas = {e.obs.ref for e in self.d.outras}
        self.assertFalse(examinadas & {x.ref for x in depois if x.ref not in
                                       {y.ref for y in self.outras if y.ts <= self.post_ts}})
        self.assertTrue(all(e.obs.ts <= self.post_ts for e in self.d.outras))

    def test_lipx_velha_e_the_blend_outro_produto(self):
        por_loja = {}
        for e in self.d.outras:
            por_loja.setdefault(e.obs.vendedor, []).append(e)
        lipx = por_loja["Lipx"]
        self.assertTrue(all(e.situacao == o.A_PARTE and "leitura velha" in e.motivo for e in lipx))
        blend = [e for e in self.d.outras if "blend" in (e.obs.chave or "")]
        self.assertTrue(blend)
        self.assertTrue(all(e.situacao == o.DESCARTADA and "outro produto" in e.motivo for e in blend))

    def test_a_frase(self):
        frase = self.d.frase()
        for trecho in ("examinei", "[o publicado]", "Escolheria SEMIKE a R$ 149,00", "Pix da página",
                       "conferido às 11:02", "R$ 49,13 abaixo do publicado", "Possível com cupom", "134,10",
                       "hipótese", "Descartei", "Deixei à parte", "leitura velha"):
            self.assertIn(trecho, frase)


if __name__ == "__main__":
    unittest.main()
