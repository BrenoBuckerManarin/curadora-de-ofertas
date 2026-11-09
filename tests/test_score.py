"""Score: propriedades que precisam valer sempre, conferência no ML e ranking."""
from __future__ import annotations

import unittest

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico, html_com_card

from curadora.ml import CardML, ler_card
from curadora.ml_api import OfertaML, reputacao
from curadora.score import Curadora, ranquear, relatorio

ASAD_NORMAL = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197])]
AGORA = T0 + 20 * DIA


def promo(produto="Lattafa Asad 100ml", preco=200, extra="", link="https://meli.la/a"):
    return f"*{produto}*\nPor R$ {preco}\n{extra}\n{link}"


class TestPropriedadesDoScore(unittest.TestCase):
    def setUp(self):
        self.cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))

    def score(self, texto, **kw):
        return self.cur.avaliar(texto, agora=AGORA, **kw)

    def test_quanto_mais_barato_maior_o_score(self):
        scores = [self.score(promo(preco=p)).score for p in (240, 210, 200, 190, 175, 160, 140)]
        self.assertEqual(scores, sorted(scores))
        self.assertGreater(scores[-1], scores[0])

    def test_acima_do_normal_nao_vale_mesmo_sendo_top_em_loja_top(self):
        av = self.score(promo(preco=240, extra="Vendido por Loja Oficial Lipx no ML"))
        self.assertEqual(av.nota, "NÃO VALE")
        self.assertTrue(any("ACIMA" in a for a in av.alertas))

    def test_preco_normal_em_loja_top_e_so_ok(self):
        av = self.score(promo(preco=199, extra="Vendido por Loja Oficial Lipx no ML"))
        self.assertIn(av.nota, {"OK", "NÃO VALE"})

    def test_top_vendido_barato_em_loja_top_e_imperdivel(self):
        av = self.score(promo(preco=150, extra="Vendido por Loja Oficial Lipx no ML"))
        self.assertTrue(av.top_vendido)
        self.assertEqual(av.nota, "IMPERDÍVEL")

    def test_lista_top_vale_mais_que_produto_desconhecido(self):
        top = self.score(promo(preco=150)).componentes["relevancia"]
        outro = self.score(promo(produto="Perfume Qualquer Coisa Rara 100ml", preco=150)).componentes["relevancia"]
        self.assertGreater(top, outro)

    def test_janela_de_60_dias_ignora_preco_antigo(self):
        av = self.cur.avaliar(promo(preco=190), agora=T0 + 90 * DIA)
        self.assertEqual(av.historico.observacoes, 0)
        self.assertEqual(av.confianca, "baixa")

    def test_repost_no_mesmo_dia_conta_uma_vez(self):
        cur = Curadora(CFG, banco_com_historico([("Lattafa Asad 100ml", 200, 1)] * 6))
        self.assertEqual(cur.avaliar(promo(), agora=AGORA).historico.observacoes, 1)

    def test_nunca_compara_com_o_futuro(self):
        av = self.cur.avaliar(promo(), agora=T0 + 3 * DIA)
        self.assertEqual(av.historico.observacoes, 3)

    def test_nome_parcial_da_loja_top(self):
        av = self.score(promo(extra="Vendido por wathiq Verificado pelo mercado livre"))
        self.assertEqual(av.loja, "Wathiq Parfums")

    def test_score_fica_entre_0_e_100(self):
        for preco in (1, 50, 200, 5000):
            self.assertTrue(0 <= self.score(promo(preco=preco)).score <= 100)


class TestRegrasDaNota(unittest.TestCase):
    """Regras de negócio que a nota precisa respeitar sempre."""

    def test_preco_normal_nunca_passa_de_nao_vale(self):
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL),
                       MLFalso({"https://meli.la/a": CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200,
                                                             vendedor="Lipx", loja_oficial=True,
                                                             mais_vendido=True, full=True)}))
        av = cur.avaliar(promo(preco=200), agora=AGORA)
        self.assertLessEqual(av.score, CFG["pesos"]["teto_sem_desconto_real"])
        self.assertEqual(av.nota, "NÃO VALE")

    def test_menor_preco_em_perfume_desejado_pode_ser_imperdivel(self):
        historico = [("Rabanne Invictus Victory 200ml", p, d) for d, p in enumerate([660, 648, 640, 655, 668, 600, 590, 650, 645, 670])]
        card = CardML(ok=True, titulo="Perfume Rabanne Invictus Victory EDP 200ml", preco=529,
                      vendedor="Rabanne", loja_oficial=True, full=True)
        cur = Curadora(CFG, banco_com_historico(historico), MLFalso({"https://meli.la/a": card}))
        av = cur.avaliar(promo(produto="Rabanne Invictus Victory 200ml", preco=529), agora=AGORA)
        self.assertFalse(av.top_vendido)
        self.assertEqual(av.nota, "IMPERDÍVEL", relatorio(av))

    def test_anuncio_de_outra_marca_usa_o_historico_dele(self):
        # 18/10 (Breno: "ele confundiu com o Asad, mas esse é diferente, outro perfume"): o grupo escreveu "Perfume
        # Arábe Asad Bourbon 100ml" a R$78,90; o anúncio era "... - Marca Isabelle La Belle". Saiu IMPERDÍVEL.
        titulo = "Perfume Arábe Asad Bourbon 100ml - Marca Isabelle La Belle | Fixação Intensa, Notas Amadeiradas"
        historico = ([("Lattafa Asad Bourbon 100ml", p, d) for d, p in enumerate([209, 205, 212, 208, 210, 207])]
                     + [(titulo, p, d) for d, p in enumerate([78.9, 79.9, 78.9, 77.9, 78.9, 79.5])])
        card = CardML(ok=True, titulo=titulo, preco=78.9, vendedor="TODA MENINA", loja_oficial=True, full=True)
        cur = Curadora(CFG, banco_com_historico(historico), MLFalso({"https://meli.la/a": card}))
        av = cur.avaliar(promo(produto="Perfume Arábe Asad Bourbon 100ml", preco=78.9), agora=AGORA)
        self.assertNotEqual(av.identidade.chave, "asad-bourbon|100")
        self.assertNotIn(av.nota, {"IMPERDÍVEL", "BOA"}, relatorio(av))
        self.assertTrue(any("outra marca (Isabelle La Belle)" in a for a in av.alertas), av.alertas)
        # o Asad Bourbon da Lattafa no mesmo preço baixo continua sendo achado
        card_lattafa = CardML(ok=True, titulo="Perfume Lattafa Asad Bourbon EDP 100ml", preco=150, vendedor="Lipx",
                              loja_oficial=True, full=True)
        cur = Curadora(CFG, banco_com_historico(historico), MLFalso({"https://meli.la/a": card_lattafa}))
        av = cur.avaliar(promo(produto="Perfume Arábe Asad Bourbon 100ml", preco=150), agora=AGORA)
        self.assertEqual(av.identidade.chave, "asad-bourbon|100")
        self.assertFalse(any("outra marca" in a for a in av.alertas))

    def test_loja_com_selo_e_desconto_pequeno_pode_ser_boa_mas_preco_normal_nao(self):
        # 06/10 (Breno): loja oficial/TOP confirmada é mais confiável; com desconto real pequeno já vale BOA.
        # Continua valendo a ordem de 24/09: no preço normal (< 3%) não é promo, nem em loja boa.
        for preco, nota in ((194, "BOA"), (199, "NÃO VALE")):   # ~3% abaixo e no preço normal
            card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=preco, vendedor="Lipx", loja_oficial=True,
                          mais_vendido=True, full=True)
            cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
            av = cur.avaliar(promo(preco=preco), agora=AGORA)
            self.assertEqual(av.nota, nota, relatorio(av))

    def test_loja_citada_pelo_grupo_nao_vale_para_outro_vendedor_em_destaque(self):
        # 09/10 (Victorioso): "Selecione a loja WATHIQ" a R$119, mas o link destaca outro vendedor a R$78.
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=130)           # vendedor não aparece
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
        av = cur.avaliar(promo(preco=180, extra="Selecione o vendedor: Loja Oficial WATHIQ PARFUMS"), agora=AGORA)
        self.assertTrue(any("destaca outro vendedor" in a for a in av.alertas))
        self.assertNotIn("loja TOP", " ".join(av.razoes))                         # não ganha os pontos da loja citada
        self.assertEqual(av.risco, "medio")                                       # −35% de vendedor desconhecido
        self.assertNotIn(av.nota, ("IMPERDÍVEL", "BOA"))

    def test_qualquer_loja_oficial_com_desconto_real_e_no_minimo_boa(self):
        # 06/10 (Breno): "lojas oficiais são todas que têm o selo"; Shakira/Polo Blue oficiais estavam em Médias.
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=192, vendedor="Intimabele", loja_oficial=True)
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
        self.assertEqual(cur.avaliar(promo(preco=192), agora=AGORA).nota, "BOA")   # ~4% abaixo, loja com selo

    def test_vendedor_que_o_ml_nao_mostra_fica_no_maximo_em_media(self):
        # 06/10 (Breno): "produtos pouco confiáveis como bons" — o ML abriu, mas não mostra quem vende.
        for card, nota in ((CardML(ok=True, titulo="Lattafa Asad 100ml", preco=160), "OK"),
                           (CardML(ok=True, titulo="Lattafa Asad 100ml", preco=160, vendedor="LOJA BOA",
                                   reputacao=reputacao("LOJA BOA", "5_green", "gold", 9000)), "IMPERDÍVEL")):
            cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
            with self.subTest(vendedor=card.vendedor or "desconhecido"):
                self.assertEqual(cur.avaliar(promo(preco=160), agora=AGORA).nota, nota)   # −20%

    def test_cupom_vencido_usa_o_preco_sem_cupom_do_ml(self):
        # 06/10: painel mostrava R$101 com MODA0610 vencido; abrindo o link, R$119.
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=220, vendedor="Loja X")
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
        for codigo in ("MODA1110", "MODA0910"):  # regra vista nos grupos: 20% até R$50
            cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, limite, perfume_ts) "
                            "VALUES (?, ?, ?, 20, 50, ?)", (codigo, AGORA, AGORA, AGORA))
        valido = cur.avaliar(promo(preco=180, extra="Cupom: MODA1110"), agora=AGORA)   # AGORA = 11/10
        vencido = cur.avaliar(promo(preco=180, extra="Cupom: MODA0910"), agora=AGORA)
        self.assertEqual((valido.preco_efetivo, vencido.preco_efetivo), (180, 220))
        self.assertTrue(any("cupom vencido: MODA0910" in a for a in vencido.alertas))

    def test_nota_respeita_o_degrau_de_desconto(self):
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))
        extra = "Vendido por Loja Oficial Lipx no ML"
        for preco, teto in ((192, 59), (182, 74)):   # ~4% e ~9% abaixo do normal
            self.assertLessEqual(cur.avaliar(promo(preco=preco, extra=extra), agora=AGORA).score, teto)

    def test_pouco_historico_no_mesmo_preco_nao_e_boa(self):
        # Visto no painel em 03/10: kit body splash a R$70,69 com 1-2 preços anteriores de R$71
        # aparecia como BOA com um "−3%" que não existia.
        kit = "Kit Body Splash Masculino Vibration E Blunn 200ml"
        cur = Curadora(CFG, banco_com_historico([(kit, 71, 1), (kit, 71, 2)]),
                       MLFalso({"https://meli.la/a": CardML(ok=True, titulo=kit, preco=70.69, vendedor="Primacial",
                                                             loja_oficial=True, mais_vendido=True)}))
        av = cur.avaliar(promo(produto=kit, preco=70.69), agora=AGORA)
        self.assertIsNone(av.pct_abaixo)
        self.assertLessEqual(av.score, CFG["pesos"]["tetos_por_desconto"][0][1])

    def test_sem_historico_nao_crava_imperdivel(self):
        cur = Curadora(CFG, banco_com_historico([]))
        av = cur.avaliar(promo(produto="Lattafa Asad 100ml", preco=60, extra="Vendido por Loja Oficial Lipx no ML"),
                         agora=AGORA)
        self.assertLessEqual(av.score, CFG["pesos"]["teto_sem_historico"])

    def test_promo_com_cupom_compara_com_precos_com_cupom(self):
        hist = [("Lattafa Asad 100ml", 150, d) for d in range(8)]  # com cupom (marcados abaixo)
        hist += [("Lattafa Asad 100ml", 170, d) for d in range(8, 16)]  # sem cupom
        con = banco_com_historico(hist)
        con.execute("UPDATE ofertas SET cupons='[\"X\"]' WHERE preco=150")
        cur = Curadora(CFG, con)
        com = cur.avaliar(promo(preco=150, extra="Cupom: *DESCONTO10*"), agora=AGORA)
        sem = cur.avaliar(promo(preco=150), agora=AGORA)
        self.assertEqual(com.historico.tipo, "com_cupom")
        self.assertEqual(sem.historico.tipo, "sem_cupom")
        self.assertGreater(sem.score, com.score)  # R$150 sem cupom é bem melhor que R$150 com cupom

    def test_de_inflado_e_marcado_e_nao_ganha_ponto(self):
        doze = ASAD_NORMAL + [("Lattafa Asad 100ml", 200, d) for d in (8, 9, 10, 11)]
        cur = Curadora(CFG, banco_com_historico(doze))  # em 90 dias nunca passou de R$205
        av = cur.avaliar(promo(preco=170, extra="De R$ 459 por R$ 170"), agora=AGORA)
        self.assertTrue(av.de_inflado)
        self.assertEqual(av.componentes["desconto"], 0)
        self.assertTrue(any("inflado" in a for a in av.alertas))

    def test_de_verdadeiro_nao_e_marcado(self):
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))
        av = cur.avaliar(promo(preco=150, extra="De R$ 205 por R$ 150"), agora=AGORA)
        self.assertFalse(av.de_inflado)

    def test_preco_visto_no_ml_vira_historico_sem_cupom(self):
        cur = Curadora(CFG, banco_com_historico([]))
        for d in range(5):
            cur.observar_preco_ml(CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=170 + d,
                                         item_id=f"MLB{d}"), "recomendacao", T0 + d * DIA)
        av = cur.avaliar(promo(preco=150), agora=AGORA)
        self.assertEqual(av.historico.tipo, "sem_cupom")
        self.assertEqual(av.historico.observacoes, 5)



CDN_MASC = [("Armaf Club De Nuit Intense Masculino 105ml", 200 + d % 3, d) for d in range(6)]


class TestVersoesNoML(unittest.TestCase):
    """18/10 (Breno): preço do ML de outra versão (gênero oposto, ou 2x o preço) não entra na chave do produto."""

    def chaves(self, con):
        return dict(con.execute("SELECT item_id, chave FROM precos_ml").fetchall())

    def ver(self, cur, titulo, preco, item, dia=7):
        cur.observar_preco_ml(CardML(ok=True, titulo=titulo, preco=preco, item_id=item), "radar API", T0 + dia * DIA)

    def test_anuncio_do_genero_oposto_vai_para_a_outra_versao(self):
        con = banco_com_historico(CDN_MASC)
        cur = Curadora(CFG, con)
        principal = cur.chave_de("Armaf Club De Nuit Intense Masculino 105ml")
        self.ver(cur, "Perfume Club De Nuit Intense Da Armaf Edt 105ml Masculino", 205, "M")
        self.ver(cur, "Perfume Armaf Club De Nuit Intense 105 ml Edp - Feminino", 190, "F")
        self.ver(cur, "Perfume Club De Nuit Intense Armaf 105ml", 199, "S")            # sem gênero: é o produto
        self.assertEqual(self.chaves(con), {"M": principal, "F": principal + "|f", "S": principal})

    def test_anuncio_com_preco_dobrado_sai_do_historico(self):
        con = banco_com_historico(CDN_MASC)
        cur = Curadora(CFG, con)
        principal = cur.chave_de("Armaf Club De Nuit Intense Masculino 105ml")
        self.ver(cur, "Armaf Club De Nuit Intense Man Eau De Parfum 105ml", 520, "CARO")
        self.ver(cur, "Perfume Club De Nuit Intense Da Armaf Edt 105ml Masculino", 210, "OK")
        self.assertEqual(self.chaves(con), {"CARO": principal + "|fora", "OK": principal})

    def test_linha_sem_genero_dominante_nao_separa(self):
        unissex = [("Lattafa Khamrah Masculino 100ml", 190, d) for d in range(3)]
        unissex += [("Lattafa Khamrah Feminino 100ml", 191, d) for d in range(3, 6)]
        con = banco_com_historico(unissex)
        cur = Curadora(CFG, con)
        self.ver(cur, "Perfume Lattafa Khamrah Feminino 100ml", 195, "F")
        self.assertEqual(self.chaves(con), {"F": cur.chave_de("Lattafa Khamrah 100ml")})

    def test_reaprender_o_catalogo_nao_desfaz_a_separacao(self):
        from curadora.banco import aprender_catalogo
        con = banco_com_historico(CDN_MASC)
        cur = Curadora(CFG, con)
        self.ver(cur, "Perfume Armaf Club De Nuit Intense 105 ml Edp - Feminino", 190, "F")
        self.ver(cur, "Armaf Club De Nuit Intense Man Eau De Parfum 105ml", 520, "CARO")
        antes = self.chaves(con)
        aprender_catalogo(con, cur.ident)
        self.assertEqual(self.chaves(con), antes)


class TestTendencia(unittest.TestCase):
    def test_caindo_na_semana_e_ja_esteve_mais_barato(self):
        hist = [("Lattafa Asad 100ml", 200, d) for d in range(0, 10)]          # semanas anteriores
        hist += [("Lattafa Asad 100ml", p, d) for d, p in ((15, 170), (16, 165), (17, 150))]  # última semana
        cur = Curadora(CFG, banco_com_historico(hist))
        av = cur.avaliar(promo(preco=175), agora=T0 + 18 * DIA)
        self.assertEqual(av.tendencia["direcao"], "caindo")
        self.assertEqual(av.tendencia["ja_esteve"], 150)
        self.assertTrue(any("já esteve R$150" in a for a in av.alertas))

    def test_estavel_sem_aviso(self):
        cur = Curadora(CFG, banco_com_historico([("Lattafa Asad 100ml", 200, d) for d in range(0, 18)]))
        av = cur.avaliar(promo(preco=199), agora=T0 + 18 * DIA)
        self.assertEqual(av.tendencia.get("direcao"), "estavel")
        self.assertNotIn("ja_esteve", av.tendencia)


class TestAntifalsificacao(unittest.TestCase):
    def curadora(self, card):
        return Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))

    def test_classifica_reputacao_e_conta_sem_nome(self):
        r = reputacao("MB20250628193724", "5_green", None, 30)
        self.assertEqual((r["classe"], r["conta_sem_nome"], r["conta_desde"]), ("fraca", True, "06/2025"))
        self.assertEqual(reputacao("BJINFO", "5_green", "platinum", 9000)["classe"], "otima")
        self.assertEqual(reputacao("LOJA", "2_orange", None, 900)["classe"], "fraca")
        self.assertEqual(reputacao("LOJA", "5_green", None, 900)["classe"], "boa")

    def test_vendedor_fraco_e_preco_baixo_e_risco_alto(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=145, vendedor="MB20250628193724",
                      reputacao=reputacao("MB20250628193724", "1_red", None, 10))
        av = self.curadora(card).avaliar(promo(preco=145), agora=AGORA)
        self.assertEqual(av.risco, "alto")
        self.assertEqual(av.nota, "NÃO VALE")

    def test_bom_demais_sem_vendedor_conhecido_e_no_maximo_media(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=110, vendedor="LOJA NOVA")
        av = self.curadora(card).avaliar(promo(preco=110), agora=AGORA)
        self.assertEqual(av.risco, "medio")
        self.assertLessEqual(av.score, CFG["pesos"]["tetos_por_desconto"][0][1])

    def test_loja_oficial_nunca_entra_em_risco(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=110, vendedor="Lipx", loja_oficial=True)
        av = self.curadora(card).avaliar(promo(preco=110), agora=AGORA)
        self.assertEqual(av.risco, "")
        self.assertEqual(av.nota, "IMPERDÍVEL")

    def test_loja_de_um_produto_so_vira_aviso(self):
        # 06/10 (Breno): Z18PERFUMARIA tinha "1 Produto" na loja. A API do ML não conta os anúncios do vendedor
        # (403), então a curadora conta em quantos produtos de catálogo já o viu vendendo.
        from curadora.banco import registrar_vendedores
        from curadora.ml_api import OfertaML
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=160, vendedor="Z18PERFUMARIA", seller_id=190,
                      reputacao=reputacao("Z18PERFUMARIA", "5_green", None, 1269))
        cur = self.curadora(card)
        oferta = lambda sid: OfertaML("MLB1", 100, None, sid, False, False)
        for i in range(60):                                   # a curadora já viu ofertas de 60 produtos
            registrar_vendedores(cur.con, f"MLB{i}", [oferta(77)] + ([oferta(190)] if i == 0 else []), AGORA)
        av = cur.avaliar(promo(preco=160), agora=AGORA)
        self.assertEqual(av.produtos_vendedor, 1)
        self.assertTrue(any("loja de 1 produto" in a for a in av.alertas))
        self.assertEqual(av.nota, "IMPERDÍVEL")               # só aviso: a nota não muda (verde, 1269 vendas)
        registrar_vendedores(cur.con, "MLB5", [oferta(190)], AGORA)   # visto em outro perfume: some o aviso
        self.assertFalse(any("loja de 1 produto" in a for a in cur.avaliar(promo(preco=160), agora=AGORA).alertas))

    def test_vendedores_suspeitos_vistos_em_06_10(self):
        # Breno em 06/10: "lojas meio suspeitas, preço real bem abaixo, sellers com poucas vendas".
        casos = [
            (reputacao("AROMA10", "5_green", None, 90), 118, "medio"),           # verde, mas 90 vendas, −41%
            (reputacao("VJ2026091814281178", "5_green", None, 356), 140, "medio"),  # conta sem nome, −30%
            (reputacao("PERFUMARIARAQUEL", "4_light_green", None, 0), 152, "medio"),  # 0 vendas, −24%
            (reputacao("BJINFO", "5_green", "platinum", 9000), 118, "medio"),     # 14/10: medalha não basta a −41%
            (reputacao("LOJA BOA", "5_green", None, 4000), 150, ""),              # verde com muitas vendas, −25%
        ]
        for rep, preco, risco in casos:
            card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=preco, vendedor=rep["nome"], reputacao=rep)
            with self.subTest(vendedor=rep["nome"]):
                av = self.curadora(card).avaliar(promo(preco=preco), agora=AGORA)
                self.assertEqual(av.risco, risco, av.risco_motivo)
                if risco:
                    self.assertNotIn(av.nota, ("IMPERDÍVEL", "BOA"))

    def test_muito_abaixo_de_vendedor_pequeno_sem_medalha_e_risco(self):
        # 14/10 (Breno: "a promo do Asad não é confiável mesmo"): LILIBRITO, verde, 593 vendas, Asad 35% abaixo.
        casos = [
            (reputacao("LILIBRITO", "5_green", None, 593), 126, "medio"),
            (reputacao("CBCFGEHDA23648", "5_green", "platinum", 7258), 126, "medio"),   # medalha não basta
            (reputacao("LOJA GIGANTE", "5_green", "platinum", 80000), 126, ""),         # vendedor muito grande
            (reputacao("LILIBRITO", "5_green", None, 593), 150, ""),           # 25% abaixo: não é caso
        ]
        for rep, preco, risco in casos:
            card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=preco, vendedor=rep["nome"], reputacao=rep)
            with self.subTest(vendedor=rep["nome"], preco=preco):
                av = self.curadora(card).avaliar(promo(preco=preco), agora=AGORA)
                self.assertEqual(av.risco, risco, av.risco_motivo)
                if risco:
                    self.assertIn("abaixo do normal fora de loja oficial", av.risco_motivo)

    def test_vendedor_platinum_ganha_ponto_e_so_vendedor_muito_grande_tira_o_risco(self):
        rep = reputacao("BJINFO", "5_green", "platinum", 9000)
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=110, vendedor="BJINFO", reputacao=rep)
        av = self.curadora(card).avaliar(promo(preco=110), agora=AGORA)
        self.assertTrue(any("Platinum" in r for r in av.razoes))
        self.assertEqual(av.risco, "medio")                   # 14/10: 45% abaixo fora de loja oficial, 9 mil vendas
        rep = reputacao("GIGANTE", "5_green", "platinum", 120000)
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=110, vendedor="GIGANTE", reputacao=rep)
        self.assertEqual(self.curadora(card).avaliar(promo(preco=110), agora=AGORA).risco, "")

    def test_reputacao_descoberta_pela_api_para_promo_de_grupo(self):
        class APIFalsa:
            def configurado(self): return True
            def ofertas(self, pid): return [OfertaML("MLB9", 150, None, 77, False, True)]
            def vendedor(self, sid): return reputacao("BJINFO", "5_green", "gold", 5000)
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=150, vendedor="BJINFO", produto_id="MLB1", item_id="MLB9")
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}), cliente_api=APIFalsa())
        av = cur.avaliar(promo(preco=150), agora=AGORA)
        self.assertEqual(av.reputacao["medalha"], "gold")


class TestCupomDeCampanhaVelho(unittest.TestCase):
    """14/10 (Breno: "pegou uns cupons velhos para mim"): MODA1410 visto às 12:08 e repostado até as 18:21."""

    def setUp(self):
        import datetime as dt
        self.cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({}))
        self.moda = "MODA" + dt.date.fromtimestamp(AGORA).strftime("%d%m")

    def visto(self, codigo, primeiro_ha_s, ultimo_ha_s):
        self.cur.con.execute("INSERT OR REPLACE INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, minimo, limite) "
                             "VALUES (?,?,?,9,15,119,40)", (codigo, AGORA - primeiro_ha_s, AGORA - ultimo_ha_s))
        self.cur.con.commit()

    def test_cupom_de_campanha_que_apareceu_ha_mais_de_2h_e_velho_mesmo_usado_agora(self):
        self.visto(self.moda, 6 * 3600, 600)
        velhos = self.cur._cupons_velhos([self.moda], AGORA)
        self.assertIn("cupom de campanha visto pela 1ª vez", velhos[self.moda])

    def test_cupom_de_campanha_novo_vale(self):
        self.visto(self.moda, 3600, 300)
        self.assertEqual(self.cur._cupons_velhos([self.moda], AGORA), {})

    def test_o_breno_usando_o_cupom_vale_mais_que_a_idade(self):
        from curadora.esgotados import registrar_sinal
        self.visto(self.moda, 6 * 3600, 600)
        registrar_sinal(self.cur.con, self.moda, AGORA - 1200, "ativo", "breno", "você manteve no post", "e1")
        self.cur.con.commit()
        self.assertEqual(self.cur._cupons_velhos([self.moda], AGORA), {})

    def test_post_no_grupofinal_com_o_cupom_segura(self):
        # 14/10 (Breno: "postaram o cupom lá, logo faz sentido valer")
        import json
        self.visto(self.moda, 6 * 3600, 600)
        self.cur.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, dados) VALUES (?,?,?,?,?,?,?)",
                             ("h1", AGORA - 900, "Grupo da Equipe", "x", "x|100", 99, json.dumps({"cupons": [self.moda]})))
        self.assertEqual(self.cur._cupons_velhos([self.moda], AGORA), {})

    def test_cupom_sem_data_no_codigo_segue_a_regra_de_antes(self):
        self.visto("CLIENTEVIP", 6 * 3600, 600)
        self.assertEqual(self.cur._cupons_velhos(["CLIENTEVIP"], AGORA), {})


class TestCupomDoDia(unittest.TestCase):
    """14/10 (Breno: "sim pode ligar"): o cupom do dia dos afiliados, pela página oficial, entra sozinho."""

    def setUp(self):
        import json, time as t
        from curadora import pagina_cupons
        self.pagina = pagina_cupons
        hoje = t.strftime("%Y-%m-%d", t.localtime(AGORA))
        self.cfg = {**CFG, "cupom_quente": {"ligado": True}, "cupom_do_dia": {"ligado": True, "ate": "23:59"}}
        self.cur = Curadora(self.cfg, banco_com_historico(ASAD_NORMAL), MLFalso({}))
        self.estado = {"lida_em": AGORA - 600, "atualizada": "x", "cupons": {
            "MELHORDEHJ": {"fim": hoje, "inicio": hoje, "saiu": 0, "lista": False, "visto": AGORA - 3600,
                           "regra": {"pct": 10.0, "minimo": 149.0, "limite": 200.0}, "mar_aberto": True},
            "PARAVC": {"fim": "2026-11-23", "inicio": "2026-10-12", "saiu": 0, "lista": True,
                       "regra": {"pct": 30.0, "minimo": 1.0, "limite": 500.0}}}}
        self.gravar()
        self.cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, minimo, limite) "
                             "VALUES ('MELHORDEHJ', ?, ?, 0, 10, 149, 200)", (AGORA - 3600, AGORA - 3600))
        self.cur.con.commit()

    def gravar(self):
        import json
        from curadora.banco import gravar_meta
        gravar_meta(self.cur.con, "pagina_cupons", json.dumps(self.estado))
        self.pagina._cache.clear()

    def test_o_cupom_do_dia_entra_nas_promos_acima_do_minimo(self):
        c = self.cur.cupom_da_equipe(200, agora=AGORA)
        self.assertEqual((c["codigo"], c["preco"]), ("MELHORDEHJ", 180.0))
        self.assertIn("cupom do dia", c["por"])
        self.assertIsNone(self.cur.cupom_da_equipe(120, agora=AGORA))           # abaixo de R$149
        self.assertEqual(self.cur._cupons_velhos(["MELHORDEHJ"], AGORA), {})     # nenhum grupo usou: não é velho

    def test_nao_decide_repeticao_nem_o_que_so_vale_com_cupom(self):
        self.assertIsNone(self.cur.cupom_da_equipe(200, agora=AGORA, so_da_equipe=True))

    def test_cupom_de_lista_de_lojas_nao_entra(self):
        self.assertNotIn("PARAVC", self.pagina.cupons_do_dia(self.cur.con, AGORA))

    def test_esgotado_ou_tirado_pelo_breno_para(self):
        from curadora.esgotados import registrar_sinal
        registrar_sinal(self.cur.con, "MELHORDEHJ", AGORA - 60, "esgotou", "breno", "você tirou do post", "e1")
        self.cur.con.commit()
        self.assertIsNone(self.cur.cupom_da_equipe(200, agora=AGORA))

    def test_pagina_lida_ha_muito_tempo_ou_depois_da_hora_nao_vale(self):
        self.estado["lida_em"] = AGORA - 3 * 3600
        self.gravar()
        self.assertIsNone(self.cur.cupom_da_equipe(200, agora=AGORA))
        self.estado["lida_em"] = AGORA - 600
        self.gravar()
        self.assertEqual(self.pagina.cupons_do_dia(self.cur.con, AGORA, ate="00:00"), {})

    def test_17_10_cupom_do_dia_de_outra_categoria_nao_entra(self):
        # Breno (17/10): "ele mandou uma promo com um cupom que não está funcionando" (OFFNOMELIHOJE no Yara)
        import time as t
        hoje = t.strftime("%Y-%m-%d", t.localtime(AGORA))
        base = {"fim": hoje, "inicio": hoje, "saiu": 0, "lista": False, "visto": AGORA - 3600,
                "regra": {"pct": 10.0, "minimo": 149.0, "limite": 200.0}}
        self.estado["cupons"] = {
            "OFFNOMELIHOJE": dict(base, categoria="Tecnologia e Eletrodomésticos", acao="Tech e Home Electronics",
                                  mar_aberto=False),
            "SEMCATEGORIA": dict(base, categoria="", acao="", mar_aberto=False),
            "BELEZAHOJE": dict(base, categoria="Beleza e Cuidado Pessoal", acao="Beauty", mar_aberto=False),
            "SITETODO": dict(base, categoria="Tecnologia", mar_aberto=True)}
        self.gravar()
        self.assertEqual(sorted(self.pagina.cupons_do_dia(self.cur.con, AGORA)), ["BELEZAHOJE", "SITETODO"])
        self.estado["cupons"] = {"OFFNOMELIHOJE": self.estado["cupons"]["OFFNOMELIHOJE"]}
        self.gravar()
        self.assertIsNone(self.cur.cupom_da_equipe(200, agora=AGORA))

    def test_desligado_no_config_nao_entra(self):
        cur = Curadora({**self.cfg, "cupom_do_dia": {"ligado": False}}, self.cur.con, MLFalso({}))
        self.assertIsNone(cur.cupom_da_equipe(200, agora=AGORA))


class TestConferenciaNoML(unittest.TestCase):
    def curadora(self, card: CardML):
        return Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))

    def test_loja_oficial_confirmada_vale_mais_que_a_dita_pelo_grupo(self):
        card = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=180, vendedor="Lipx", loja_oficial=True)
        confirmado = self.curadora(card).avaliar(promo(preco=180, extra="Vendido por Lipx"), agora=AGORA)
        so_grupo = self.curadora(CardML(ok=False, erro="x")).avaliar(promo(preco=180, extra="Vendido por Lipx"), agora=AGORA)
        self.assertTrue(confirmado.loja_confirmada)
        self.assertGreater(confirmado.componentes["loja"], so_grupo.componentes["loja"])

    def test_loja_qualquer_aparece_mas_sem_selo(self):
        # Pedido do Breno (03/10): "MB20250628193724" não pode ganhar o ✓ verde.
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=150, vendedor="MB20250628193724", loja_oficial=False)
        av = self.curadora(card).avaliar(promo(preco=150), agora=AGORA)
        self.assertEqual(av.loja, "MB20250628193724")
        self.assertFalse(av.loja_selo)
        oficial = self.curadora(CardML(ok=True, titulo="Lattafa Asad 100ml", preco=150, vendedor="Lipx",
                                       loja_oficial=True)).avaliar(promo(preco=150), agora=AGORA)
        self.assertTrue(oficial.loja_selo)

    def test_grupo_diz_loja_oficial_mas_ml_nao_mostra_selo(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=180, vendedor="LOJA QUALQUER", loja_oficial=False)
        av = self.curadora(card).avaliar(promo(preco=180, extra="Loja oficial no ML"), agora=AGORA)
        self.assertEqual(av.componentes["loja"], 0)
        self.assertTrue(any("sem selo" in a for a in av.alertas))

    def test_preco_subiu_usa_o_preco_do_ml(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=230)
        av = self.curadora(card).avaliar(promo(preco=160), agora=AGORA)
        self.assertEqual(av.preco_efetivo, 230)
        self.assertTrue(any("SUBIU" in a for a in av.alertas))

    def test_cupom_so_vale_se_a_regra_conhecida_explica_o_preco(self):
        # 06/10 (Breno): "preços divergentes" — painel R$113 com cupom, abrindo o link R$162.
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200)
        cur = self.curadora(card)
        texto = promo(preco=165, extra="Cupom: *DESCONTO20*")
        self.assertEqual(cur.avaliar(texto, agora=AGORA).preco_efetivo, 200)          # regra desconhecida: ML
        cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, limite, perfume_ts) "
                        "VALUES ('DESCONTO20', ?, ?, 20, 40, ?)", (AGORA, AGORA, AGORA))
        self.assertEqual(cur.avaliar(texto, agora=AGORA).preco_efetivo, 165)          # 20% até R$40: confere
        av = cur.avaliar(promo(preco=120, extra="Cupom: *DESCONTO20*"), agora=AGORA)
        self.assertEqual(av.preco_efetivo, 160)                                       # não explica: R$200 − R$40

    def test_link_de_outro_produto_derruba_o_score(self):
        card = CardML(ok=True, titulo="Perfume Lattafa Asad Bourbon EDP 100ml", preco=150)
        av = self.curadora(card).avaliar(promo(preco=150), agora=AGORA)
        self.assertTrue(av.conflito)
        self.assertEqual(av.nota, "NÃO VALE")

    def test_volume_vem_do_titulo_do_ml(self):
        card = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=199)
        av = self.curadora(card).avaliar(promo(produto="Lattafa Asad", preco=199), agora=AGORA)
        self.assertEqual(av.identidade.chave, "asad|100")
        self.assertGreater(av.historico.observacoes, 0)

    def test_card_real_salvo(self):
        card = ler_card(html_com_card("ml_card_loja_oficial.json"))
        cur = Curadora(CFG, banco_com_historico([]), MLFalso({"https://meli.la/a": card}))
        av = cur.avaliar(promo(produto="Mawwal Saz Edp 100ml", preco=216), agora=AGORA)
        self.assertEqual(av.loja, "Wathiq Parfums")
        self.assertTrue(av.loja_confirmada)
        self.assertEqual(av.preco_efetivo, 228.05)  # sem cupom: vale o preço atual do ML
        self.assertIn("selo MAIS VENDIDO no ML", av.razoes)

    def test_loja_confirmada_fica_registrada(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=180, vendedor="Klassey", loja_oficial=True)
        cur = self.curadora(card)
        cur.avaliar(promo(preco=180), agora=AGORA)
        self.assertIn("klassey", cur._lojas_confiaveis())


class TestRanking(unittest.TestCase):
    def test_mesma_oferta_em_varios_grupos_vira_uma(self):
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))
        avs = [cur.avaliar(promo(preco=p), agora=AGORA) for p in (160, 161, 160.5)]
        avs.append(cur.avaliar(promo(produto="Lattafa Yara 100ml", preco=150), agora=AGORA))
        ranking = ranquear(avs)
        self.assertEqual(len(ranking), 2)
        self.assertEqual(ranking, sorted(ranking, key=lambda a: -a.score))


if __name__ == "__main__":
    unittest.main()
