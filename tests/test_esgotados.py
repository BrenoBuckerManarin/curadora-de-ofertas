"""Memória de cupom esgotado: avisos dos grupos, edição do Breno e o que vale agora."""
from __future__ import annotations

import json
import time
import unittest

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico

from curadora.banco import conectar
from curadora.cupons import cupons_da_mensagem, fora_de_perfume, melhor_cupom, registrar_cupons
from curadora.esgotados import (_inicio_do_dia, esgotados, guardar_loja_do_link, lista_para_normalizadora, lojas_fora,
                                quentes, registrar_aviso_da_equipe, registrar_sinais_de_grupo, registrar_sinal,
                                sinais_da_edicao, sinais_do_grupo, sombra_dos_45_min, tirou_do_post)
from curadora.ml import CardML
from curadora.ml_api import OfertaML, reputacao
from curadora.score import Curadora, preco_sem_pix, texto_confianca

CONHECIDOS = {"CHAPEU", "PEDALADA", "BICICLETA", "CANETA", "MODA10BELEZA", "ESGOTADO"}
ASAD_NORMAL = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197])]
AGORA = T0 + 20 * DIA
NORMALIZADO = ("*FRESCOR MARINHO*\n\n*Natura Kaiak 100ml*\n\nDe R$ 112 por *R$ 96* 🦸🏻‍♂️\n"
               "Cupom: *MODA10BELEZA* 🎟️\n\nhttps://meli.la/abc")


class TestSinaisDosGrupos(unittest.TestCase):
    def test_so_o_cupom_da_oracao_que_esgotou(self):
        # Mensagem real (grupo de promos): o CHAPEU esgotou; os outros três estão ATIVOS.
        sinais = sinais_do_grupo("cupom *CHAPEU* esgotou, ta ativo esses *PEDALADA* ou *BICICLETA* ou *CANETA*",
                                 CONHECIDOS)
        self.assertEqual([(c, t) for c, t, _ in sinais], [("CHAPEU", "esgotou")])

    def test_esgotando_e_acabou_de_nao_contam(self):
        for texto in ("Voyage gigantesco, corre que o cupom MODA10BELEZA tá esgotando",
                      "Cupom MODA10BELEZA: acabou de sair, aproveita",
                      "Use o cupom MODA10BELEZA antes que acabe"):
            self.assertEqual(sinais_do_grupo(texto, CONHECIDOS), [], texto)

    def test_so_vale_codigo_ja_visto_como_cupom(self):
        self.assertEqual(sinais_do_grupo("o cupom QUALQUERCOISA esgotou", CONHECIDOS), [])
        self.assertEqual(sinais_do_grupo("Cupom ESGOTADO, galera", CONHECIDOS), [])  # "ESGOTADO" não é cupom

    def test_cupom_que_voltou(self):
        self.assertEqual([(c, t) for c, t, _ in sinais_do_grupo("O MODA10BELEZA voltou!", CONHECIDOS)],
                         [("MODA10BELEZA", "ativo")])


class TestEdicaoDoBreno(unittest.TestCase):
    def test_cupom_tirado_e_esgotado_e_mantido_e_ativo(self):
        final = NORMALIZADO.replace("por *R$ 96*", "por *R$ 106*").replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        self.assertEqual(sinais_da_edicao(NORMALIZADO, final), [("MODA10BELEZA", "esgotou")])
        self.assertEqual(sinais_da_edicao(NORMALIZADO, NORMALIZADO), [("MODA10BELEZA", "ativo")])

    def test_cupom_que_o_breno_pos_a_mao_e_sinal_proprio(self):
        # 10/10 17:11: o post veio com ANACASTELA10 e ele trocou por MODA1010, refazendo o preço.
        final = NORMALIZADO.replace("MODA10BELEZA", "MODA1010").replace("R$ 96", "R$ 77")
        self.assertEqual(sinais_da_edicao(NORMALIZADO, final),
                         [("MODA10BELEZA", "esgotou"), ("MODA1010", "ativo"), ("MODA1010", "posto")])


class TestEstadoDosCupons(unittest.TestCase):
    def setUp(self):
        self.con = conectar(":memory:")

    def test_vale_o_sinal_mais_recente_e_o_parado_nao_expira(self):
        registrar_sinal(self.con, "MODA10BELEZA", T0, "esgotou", "grupo", "grupo X avisou", "m1")
        self.assertIn("MODA10BELEZA", esgotados(self.con, T0 + 60))
        registrar_sinal(self.con, "MODA10BELEZA", T0 + 120, "ativo", "grupo", "voltou", "m2")
        self.assertNotIn("MODA10BELEZA", esgotados(self.con, T0 + 180))
        registrar_sinal(self.con, "CHAPEU", T0, "esgotou", "grupo", "", "m3")
        # 03/11 (Breno: "completar 12 horas não reative o cupom automaticamente"): até 03/11, passou de 12h, esquecia
        self.assertEqual(esgotados(self.con, T0 + 13 * 3600)["CHAPEU"]["estado"], "esgotado")

    def test_o_que_o_breno_tirou_fica_suspenso_ate_um_sinal_novo(self):
        registrar_sinal(self.con, "MODA10BELEZA", T0, "esgotou", "breno", "você tirou", "e1")
        self.assertEqual(esgotados(self.con, T0 + 60)["MODA10BELEZA"]["motivo"], "suspenso: você tirou")
        self.assertIn("MODA10BELEZA", esgotados(self.con, T0 + 3 * DIA))
        registrar_sinal(self.con, "MODA10BELEZA", T0 + 3 * DIA + 180, "ativo", "breno", "você manteve", "e2")
        self.assertNotIn("MODA10BELEZA", esgotados(self.con, T0 + 3 * DIA + 240))

    def test_mesma_mensagem_nao_conta_duas_vezes(self):
        self.assertTrue(registrar_sinal(self.con, "X1234", T0, "esgotou", "breno", "", "e1"))
        self.assertFalse(registrar_sinal(self.con, "X1234", T0, "esgotou", "breno", "", "e1"))

    def test_aviso_do_grupo_entra_pelo_ao_vivo(self):
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts) VALUES ('CHAPEU', ?, ?)", (T0, T0))
        n = registrar_sinais_de_grupo(self.con, "cupom CHAPEU esgotou, ta ativo esses PEDALADA", T0 + 60, "Grupo X", "m9")
        self.assertEqual(n, 1)
        self.assertIn("Grupo X avisou", esgotados(self.con, T0 + 120)["CHAPEU"]["motivo"])

    def test_lista_da_normalizadora_inclui_codigo_com_data_vencida(self):
        # AGORA = T0 + 20 dias; cupom com a data de 2 dias antes no código
        import datetime as dt
        ontem = dt.date.fromtimestamp(AGORA - 2 * DIA)
        codigo = f"FULL{ontem:%d%m}"
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts) VALUES (?, ?, ?)", (codigo, AGORA, AGORA))
        lista = lista_para_normalizadora(self.con, AGORA)
        self.assertIn(codigo, lista)
        self.assertEqual(lista[codigo]["fonte"], "data")


class TestNotaComCupomEsgotado(unittest.TestCase):
    def test_cupom_esgotado_vira_preco_sem_cupom(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True)
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
        cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, limite, perfume_ts) "
                        "VALUES ('MODA10BELEZA', ?, ?, 10, 30, ?)", (AGORA, AGORA, AGORA))
        texto = "*Lattafa Asad 100ml*\nPor R$ 180\nCupom: MODA10BELEZA\nhttps://meli.la/a"
        self.assertEqual(cur.avaliar(texto, agora=AGORA).preco_efetivo, 180)   # regra confere: com cupom
        registrar_sinal(cur.con, "MODA10BELEZA", AGORA - 600, "esgotou", "breno", "você tirou do post do X às 10:00", "e1")
        av = cur.avaliar(texto, agora=AGORA)
        self.assertEqual(av.preco_efetivo, 200)
        self.assertIn("MODA10BELEZA", av.cupons_mortos)
        # 03/11: a retirada da Edição é suspensão, e o aviso diz isso
        self.assertIn("cupom MODA10BELEZA suspenso: você tirou do post do X às 10:00 (preço sem cupom)", av.alertas)
        self.assertFalse(any(a.startswith("preço depende de cupom") for a in av.alertas))


class TestCupomQuente(unittest.TestCase):
    """10/10: o cupom que o Breno está pondo à mão nos posts vale o preço do ML menos o cupom, pela regra."""

    def setUp(self):
        self.card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True)
        self.cur = Curadora({**CFG, "cupom_quente": {"ligado": True}}, banco_com_historico(ASAD_NORMAL),
                            MLFalso({"https://meli.la/a": self.card}))
        self.con = self.cur.con
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('OFERTAMELI', ?, ?, 15, 79, 60, ?)", (AGORA, AGORA, AGORA))
        # o grupo postou o preço cheio e o cupom ao lado (como o Malbec Gold de 10/10)
        self.texto = "*Lattafa Asad 100ml*\nPor R$ 200\nCupom: OFERTAMELI\nhttps://meli.la/a"

    def postar_no_grupofinal(self, ts, cupons=("OFERTAMELI",)):
        import json
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, dados) VALUES (?,?,?,?,?,?,?)",
                         (f"h{ts}", ts, "Grupo da Equipe", "Outro Perfume 100ml", "outro|100", 99,
                          json.dumps({"cupons": list(cupons)})))

    def por_a_mao(self, ts, posts=2, codigo="OFERTAMELI"):
        for n in range(posts):
            for tipo in ("ativo", "posto"):
                registrar_sinal(self.con, codigo, ts + n, tipo, "breno", "você pôs à mão", f"edicao:{ts}-{n}")

    def test_sem_confirmacao_da_equipe_nao_e_quente(self):
        self.assertEqual(quentes(self.con, AGORA), {})
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertIsNone(av.cupom_quente)
        self.assertEqual(av.preco_efetivo, 200)

    def test_breno_pos_a_mao_em_dois_posts_e_o_preco_vira_ml_menos_cupom(self):
        self.por_a_mao(AGORA - 600)
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "você pôs à mão")
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertEqual(av.preco_efetivo, 170)                      # 200 − 15%
        self.assertEqual(av.cupom_quente["codigo"], "OFERTAMELI")
        self.assertTrue(any("QUENTE" in r for r in av.razoes))

    def postar_produto(self, ts, chave, cupons=("OFERTAMELI",)):
        import json
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, dados) VALUES (?,?,?,?,?,?,?)",
                         (f"h{ts}{chave}", ts, "Grupo da Equipe", chave, chave, 99, json.dumps({"cupons": list(cupons)})))

    def test_dois_produtos_postados_no_grupofinal_com_o_cupom_esquentam_sem_passar_pela_edicao(self):
        # 14/10 (Breno: "olha o grupo GrupoFinal, postaram o cupom lá, logo faz sentido valer")
        self.postar_produto(AGORA - 1800, "a|100")
        self.assertEqual(quentes(self.con, AGORA), {})                 # um produto só não basta
        self.postar_produto(AGORA - 600, "b|100")
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "post no GrupoFinal")
        self.assertEqual(self.cur.avaliar(self.texto, agora=AGORA).preco_efetivo, 170)

    def test_o_mesmo_produto_postado_duas_vezes_conta_uma(self):
        self.postar_produto(AGORA - 1800, "a|100")
        self.postar_produto(AGORA - 600, "a|100")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_depois_que_o_breno_tira_so_posts_novos_no_grupofinal_contam(self):
        self.postar_produto(AGORA - 1800, "a|100")
        self.postar_produto(AGORA - 1500, "b|100")
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 1200, "esgotou", "breno", "você tirou", "e9")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_post_no_grupofinal_velho_nao_segura(self):
        self.postar_produto(AGORA - 7000, "a|100")
        self.postar_produto(AGORA - 6000, "b|100")
        self.assertEqual(quentes(self.con, AGORA), {})                  # o último tem mais de 40 min

    def test_um_post_so_nao_esquenta(self):
        # O erro das 16:27 de 10/10: ele MANTEVE o COMPRAPREFERIDA num post só e o cupom esgotou 14 min depois.
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 600, "ativo", "breno", "você manteve", "e1")
        self.postar_no_grupofinal(AGORA - 500)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_um_post_a_mao_sozinho_tambem_nao(self):
        self.por_a_mao(AGORA - 400, posts=1)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_manteve_em_dois_posts_ja_vale(self):
        # 11/10 09:13-09:16 (MODA1110): pôs à mão em 1 e manteve em 2 que já vieram com o cupom; 3 posts no GrupoFinal.
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 600, "ativo", "breno", "você manteve", "e1")
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 400, "ativo", "breno", "você manteve", "e2")
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "você manteve")
        self.assertEqual(self.cur.cupom_da_equipe(200, AGORA)["preco"], 170)

    def test_post_no_grupofinal_com_o_cupom_renova_os_40_min(self):
        self.por_a_mao(AGORA - 50 * 60)
        self.assertEqual(quentes(self.con, AGORA), {})
        self.postar_no_grupofinal(AGORA - 600)
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "post no GrupoFinal")

    def test_desligado_no_config_nao_muda_o_preco(self):
        self.por_a_mao(AGORA - 600)
        desligada = Curadora({**CFG, "cupom_quente": {"ligado": False}}, self.con, MLFalso({"https://meli.la/a": self.card}))
        av = desligada.avaliar(self.texto, agora=AGORA)
        self.assertIsNone(av.cupom_quente)
        self.assertEqual(av.preco_efetivo, 200)
        self.assertIsNone(desligada.cupom_da_equipe(200, AGORA))

    def test_esfria_sozinho_depois_de_40_min_sem_confirmacao(self):
        self.por_a_mao(AGORA - 41 * 60)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_breno_tirou_para_na_hora_e_so_volta_com_dois_posts_novos(self):
        self.por_a_mao(AGORA - 600)
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 300, "esgotou", "breno", "você tirou", "e9")
        self.assertEqual(quentes(self.con, AGORA), {})
        self.assertEqual(self.cur.avaliar(self.texto, agora=AGORA).preco_efetivo, 200)
        self.por_a_mao(AGORA - 200, posts=1)
        self.assertEqual(quentes(self.con, AGORA), {})
        self.por_a_mao(AGORA - 100, posts=2)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_abaixo_do_minimo_do_cupom_nao_aplica(self):
        self.por_a_mao(AGORA - 600)
        self.con.execute("UPDATE cupons SET minimo = 250 WHERE codigo = 'OFERTAMELI'")
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertIsNone(av.cupom_quente)
        self.assertIsNone(self.cur.cupom_da_equipe(200, AGORA))

    def test_cupom_da_equipe_vale_mesmo_sem_o_grupo_ter_postado_com_ele(self):
        # 10/10 17:12: Al Noble Safeer a R$132,40 no ML, sem cupom no post; ele pôs o MODA1010 e postou a R$105.
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('MODAHOJE', ?, ?, 20, 89, 40, ?)", (AGORA, AGORA, AGORA))
        self.por_a_mao(AGORA - 300, codigo="MODAHOJE")   # (MODA1010 tem data no nome: só vale em 10/10)
        cupom = self.cur.cupom_da_equipe(132.40, AGORA)
        self.assertEqual((cupom["codigo"], cupom["preco"]), ("MODAHOJE", 105.92))
        self.assertEqual(self.cur.cupom_da_equipe(292.33, AGORA)["preco"], 252.33)   # 20% passa do teto de R$40
        self.assertIsNone(self.cur.cupom_da_equipe(80, AGORA))                        # abaixo do mínimo de R$89


class TestFrescorDoCupom(TestCupomQuente):
    """30/10 (estudo de cupons): o cupom da equipe só vale com post de grupo nos últimos 45 min ou com o Breno pondo à
    mão nos últimos 40; o 'manteve' de post em que a própria rodada pôs o cupom não confirma."""

    def grupo_postou_ha(self, minutos):
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'OFERTAMELI'", (AGORA - minutos * 60,))

    def manteve(self, ha_s, post, ref):
        registrar_sinal(self.con, "OFERTAMELI", AGORA - ha_s, "ativo", "breno", "você manteve", ref, "7|lipx", post)

    def test_sem_grupo_ha_45_min_nao_vale_mesmo_confirmado(self):
        self.grupo_postou_ha(50)
        self.manteve(600, "meli.la/a1", "e1")
        self.manteve(500, "meli.la/a2", "e2")
        self.assertEqual(quentes(self.con, AGORA), {})
        self.grupo_postou_ha(20)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_por_a_mao_ha_pouco_segura_sem_grupo(self):
        self.grupo_postou_ha(50)
        self.por_a_mao(AGORA - 600)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_post_no_grupofinal_nao_renova_o_frescor(self):
        self.grupo_postou_ha(50)
        self.postar_produto(AGORA - 900, "a|100")
        self.postar_produto(AGORA - 600, "b|100")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_manteve_de_post_da_rodada_nao_confirma(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from curadora import esgotados as e
        pasta = Path(tempfile.mkdtemp())
        registro = pasta / "pipeline.jsonl"
        registro.write_text("".join(json.dumps({"cupom_quente": "OFERTAMELI", "link_afiliado": f"https://meli.la/r{n}"})
                                    + "\n" for n in (1, 2)), encoding="utf-8")
        with mock.patch.object(e, "REGISTRO_PIPELINE", registro):
            self.manteve(600, "meli.la/r1", "e1")
            self.manteve(500, "meli.la/r2", "e2")
            self.assertEqual(quentes(self.con, AGORA), {})                  # só "manteve" do que a rodada pôs
            self.manteve(400, "meli.la/outro", "e3")
            self.assertEqual(quentes(self.con, AGORA), {})                  # 1 confirmação de verdade não basta
            registrar_sinal(self.con, "OFERTAMELI", AGORA - 300, "posto", "breno", "você pôs", "e4", "7|lipx", "meli.la/r2")
            self.assertIn("OFERTAMELI", quentes(self.con, AGORA))           # o "pôs à mão" conta sempre


class TestAvisoDaEquipeLigaARodada(TestCupomQuente):
    """01/11 (Breno: "quando chegar cupom no grupo, ativar a rodada"): o aviso de cupom da equipe no GrupoFinal liga a rodada
    sem esperar 2 posts, com algum sinal de que o cupom vale em perfume."""

    AVISO = ("*CUPOM MERCADO LIVRE*\n\n🎟️ *MELIMAIS0111*\n20% OFF máx de R$70 p/ Meli+\n\n🎟️ *OFERTAMELI*\n"
             "15% OFF máx R$60 em Moda & Bem-Estar\n\n_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")

    def avisar(self, ts, texto=AVISO, ref="aviso1"):
        registrar_cupons(self.con, texto, ts, em_promo_de_perfume=True, da_equipe=True)
        return registrar_aviso_da_equipe(self.con, texto, ts, ref)

    def grupo_postou_perfume(self, ts, codigo="OFERTAMELI"):
        self.con.execute("INSERT INTO ofertas (msg_id, ts, grupo, produto, base, chave, preco, cupons) "
                         "VALUES (?,?,?,?,?,?,?,?)", (f"g{ts}{codigo}", ts, "grupo", "Lattafa Yara 100ml", "lattafa yara",
                                                      "lattafa yara|100", 150, json.dumps([codigo])))

    def test_aviso_com_perfume_nos_grupos_liga_na_hora(self):
        # MODA10 em 01/11: aviso às 09:06, perfume com ele num grupo às 09:16; a rodada só ligou às 09:24
        self.assertEqual(self.avisar(AGORA - 600), ["OFERTAMELI"])          # o do Meli+ fica de fora
        self.assertEqual(quentes(self.con, AGORA), {})                       # ainda sem sinal de perfume
        self.grupo_postou_perfume(AGORA - 60)
        q = quentes(self.con, AGORA)
        self.assertEqual((q["OFERTAMELI"]["por"], q["OFERTAMELI"]["confirmado_em"]),
                         ("aviso de cupom no GrupoFinal", AGORA - 600))
        self.assertEqual(self.cur.cupom_da_equipe(200, AGORA)["preco"], 170)

    def test_aviso_sem_sinal_de_perfume_nao_liga(self):
        # MODA2310 (22/10): avisado, mas nenhum grupo postou perfume com ele e ninguém pôs nem manteve num post
        self.avisar(AGORA - 600)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_cupom_de_outra_secao_nao_liga_nem_vira_sugestao(self):
        livros = ("*CUPOM MERCADO LIVRE*\n🎟️ *LIVROS3110*\n15% OFF máx R$40 em Livros & Jogos\n"
                  "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        self.assertEqual(self.avisar(AGORA - 600, livros, "aviso2"), [])
        self.grupo_postou_perfume(AGORA - 60, "LIVROS3110")                 # nem com um grupo postando (por engano)
        self.assertEqual(quentes(self.con, AGORA), {})
        self.avisar(AGORA - 600)
        um = lambda c: self.con.execute("SELECT perfume_ts, equipe_ts FROM cupons WHERE codigo = ?", (c,)).fetchone()
        self.assertEqual(tuple(um("LIVROS3110")), (None, AGORA - 600))      # a regra fica guardada, sem "visto com perfume"
        self.assertEqual(tuple(um("MELIMAIS0111")), (None, AGORA - 600))
        # o do Meli+ daria R$ 40 nos R$ 200 e era sugerido no post de perfume; fica o que vale (R$ 30)
        self.assertEqual(melhor_cupom(self.con, 200, AGORA)["codigo"], "OFERTAMELI")

    def test_aviso_esfria_em_40_min(self):
        self.avisar(AGORA - 41 * 60)
        self.grupo_postou_perfume(AGORA - 40 * 60)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 2 * 60))      # 39 min depois do aviso ainda vale
        self.assertEqual(quentes(self.con, AGORA), {})
        self.por_a_mao(AGORA - 60, posts=1)            # passados os 40 min, volta a valer a regra dos 2 posts
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_compra_minima_alta_nao_liga(self):
        # ESQTVSCEL2110 (21/10): R$ 150 OFF acima de R$ 1.199, para TV e celular
        self.avisar(AGORA - 600)
        self.con.execute("UPDATE cupons SET minimo = 1199 WHERE codigo = 'OFERTAMELI'")
        self.grupo_postou_perfume(AGORA - 60)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_aviso_de_antes_de_voce_tirar_nao_religa(self):
        self.avisar(AGORA - 900)
        self.grupo_postou_perfume(AGORA - 800)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 700))
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 600, "esgotou", "breno", "você tirou", "e9")
        self.assertEqual(quentes(self.con, AGORA - 500), {})
        self.por_a_mao(AGORA - 300, posts=1)          # depois de tirar, 1 post só não religa: o aviso é de antes
        self.assertNotIn("OFERTAMELI", esgotados(self.con, AGORA))
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_aviso_nao_desfaz_o_que_voce_tirou(self):
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 900, "esgotou", "breno", "você tirou", "e9")
        self.avisar(AGORA - 600)
        self.grupo_postou_perfume(AGORA - 60)
        self.assertIn("OFERTAMELI", esgotados(self.con, AGORA))
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_aviso_desfaz_o_esgotou_de_grupo(self):
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 900, "esgotou", "grupo", "o grupo X avisou", "g1")
        self.avisar(AGORA - 600)
        self.grupo_postou_perfume(AGORA - 60)
        self.assertNotIn("OFERTAMELI", esgotados(self.con, AGORA))
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_voce_por_a_mao_num_post_depois_do_aviso_liga(self):
        self.por_a_mao(AGORA - 300, posts=1)
        self.assertEqual(quentes(self.con, AGORA), {})                       # sem o aviso, 1 post não basta
        self.avisar(AGORA - 600)
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "você pôs à mão")

    def test_um_post_no_grupofinal_com_o_cupom_e_sinal_a_sugestao_nao(self):
        self.avisar(AGORA - 600)
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, dados) VALUES (?,?,?,?,?,?,?)",
                         ("h1", AGORA - 300, "Grupo da Equipe", "Kaiak", "kaiak|100", 84,
                          json.dumps({"cupons": [], "cupom_sugerido": {"codigo": "OFERTAMELI"}})))
        self.assertEqual(quentes(self.con, AGORA), {})
        self.postar_produto(AGORA - 200, "a|100")
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "post no GrupoFinal")

    def test_aviso_vale_como_frescor(self):
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'OFERTAMELI'", (AGORA - 100 * 60,))
        self.grupo_postou_perfume(AGORA - 100 * 60)   # nenhum grupo há 100 min, mas o cupom já tinha saído em perfume
        self.avisar(AGORA - 600)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))
        self.assertEqual(quentes(self.con, AGORA - 600 + 40 * 60), {})       # acabou o aviso: volta a regra do frescor

    def test_cupom_quente_pelo_aviso_nao_sai_como_velho(self):
        # Revisão de 01/11: a rodada ligava pelo aviso, mas a nota dava o cupom como "velho" e a promo ia sem ele
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'OFERTAMELI'", (AGORA - 100 * 60,))
        self.grupo_postou_perfume(AGORA - 100 * 60)                    # nenhum grupo há mais de 1h
        self.assertIn("OFERTAMELI", self.cur.avaliar(self.texto, agora=AGORA).cupons_mortos)
        self.avisar(AGORA - 600)
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertEqual((av.preco_efetivo, av.cupom_quente["codigo"]), (170, "OFERTAMELI"))
        # cupom de campanha visto pela 1ª vez há mais de 2h (o MODA1010 de 10/10: grupos desde 13:39, aviso às 17:07)
        campanha = "MODA" + time.strftime("%d%m", time.localtime(AGORA))
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES (?, ?, ?, 15, 79, 60, ?)", (campanha, AGORA - 3 * 3600, AGORA - 60, AGORA - 60))
        texto = f"*Lattafa Asad 100ml*\nPor R$ 200\nCupom: {campanha}\nhttps://meli.la/a"
        self.assertIn(campanha, self.cur.avaliar(texto, agora=AGORA).cupons_mortos)
        self.avisar(AGORA - 300, self.AVISO.replace("OFERTAMELI", campanha), "aviso6")
        self.grupo_postou_perfume(AGORA - 120, campanha)
        self.assertEqual(self.cur.avaliar(texto, agora=AGORA).preco_efetivo, 170)

    def test_aviso_de_cupom_da_loja_da_marca_nao_liga_a_rodada_geral(self):
        # LOJAS<data> só vale na loja da marca: segue a frente própria (29/10); a rodada geral iria com os queridinhos
        # de revenda, onde ele não entra
        lojas = "LOJAS" + time.strftime("%d%m", time.localtime(AGORA))
        texto = (f"*CUPOM MERCADO LIVRE*\n🎟️ *{lojas}*\n10% OFF máx de R$20 em Lojas Oficiais\n"
                 "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        self.assertEqual(self.avisar(AGORA - 600, texto, "aviso4"), [lojas])
        self.grupo_postou_perfume(AGORA - 60, lojas)
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_desconto_em_reais_sem_compra_minima_nao_liga(self):
        # MODA1510 (15/10): "R$ 15 OFF, limite de R$ 20" sem mínimo; a conta não põe valor fixo sem mínimo em preço nenhum
        texto = "*CUPOM MERCADO LIVRE*\n🎟️ *MODAQUINZE*\nR$ 15 OFF, limite de R$ 20 em Moda & Bem-Estar\n"
        self.assertEqual(self.avisar(AGORA - 600, texto, "aviso5"), ["MODAQUINZE"])
        self.grupo_postou_perfume(AGORA - 60, "MODAQUINZE")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_aviso_do_dia_seguinte_desfaz_o_seu_esgotou_da_noite(self):
        # o MODA10 volta toda campanha: tirado às 22:10, avisado de novo às 09:06 do dia seguinte
        meia_noite = _inicio_do_dia(AGORA)
        manha = meia_noite + 9 * 3600 + 360
        registrar_sinal(self.con, "OFERTAMELI", meia_noite - 2 * 3600 + 600, "esgotou", "breno", "você tirou", "e9")
        self.assertIn("OFERTAMELI", lista_para_normalizadora(self.con, manha))
        self.avisar(manha)
        self.grupo_postou_perfume(manha + 600)
        self.assertNotIn("OFERTAMELI", lista_para_normalizadora(self.con, manha + 660))
        self.assertIn("OFERTAMELI", quentes(self.con, manha + 660))

    def tirar(self, ts, post, ref, desde=AGORA - 3600):
        return tirou_do_post(self.con, "OFERTAMELI", ts, "Produto", ref, f"7|{post[-1].lower()}", f"Loja {post[-1]}",
                             post, desde=desde)

    def manter(self, ts, post, ref):
        registrar_sinal(self.con, "OFERTAMELI", ts, "ativo", "breno", "você manteve", ref, f"7|{post[-1].lower()}", post)

    def test_30_10_a_segunda_versao_dos_mesmos_posts_nao_vira_outro_2_seguidos(self):
        # 03/11 (relatório da Astra: a Edição é rascunho). Em 30/10, 17:34-17:35: tirou do Yara Elixir e do Sabah, manteve
        # no Khamrah e no Asad, e a 2ª versão do Yara Elixir e do Sabah contou como um novo "2 seguidos"
        self.por_a_mao(AGORA - 1200)
        self.assertEqual(self.tirar(AGORA - 300, "meli.la/A", "e1"), ("fora", True))
        self.assertEqual(self.tirar(AGORA - 298, "meli.la/B", "e2"), ("esgotou", True))
        self.manter(AGORA - 295, "meli.la/C", "e3")
        self.manter(AGORA - 294, "meli.la/D", "e4")
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 290))           # manteve em 2 posts depois de tirar
        self.assertEqual(self.tirar(AGORA - 240, "meli.la/A", "e5"), ("fora", False))       # 2ª versão do A
        self.assertEqual(self.tirar(AGORA - 239, "meli.la/B", "e6"), ("esgotou", False))    # 2ª versão do B
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 200))

    def test_tirar_de_novo_depois_de_manter_no_mesmo_post_conta(self):
        self.por_a_mao(AGORA - 1200)
        self.assertEqual(self.tirar(AGORA - 300, "meli.la/A", "e1"), ("fora", True))
        self.manter(AGORA - 200, "meli.la/A", "e2")
        self.assertTrue(self.tirar(AGORA - 100, "meli.la/A", "e3")[1])

    def test_mesmo_link_em_outra_postagem_e_outra_retirada(self):
        # 03/11 (Astra reproduziu): ontem ele tirou o cupom de um post; hoje o mesmo link volta num post novo e ele tira
        # de novo. A retirada de hoje conta: as versões só se juntam dentro da mesma postagem
        registrar_sinal(self.con, "OFERTAMELI", AGORA - DIA, "esgotou", "breno", "você tirou ontem", "e0", "7|a",
                        "meli.la/A")
        self.assertEqual(self.tirar(AGORA - 100, "meli.la/A", "e1", desde=AGORA - 300), ("esgotou", True))
        self.assertEqual(self.tirar(AGORA - 50, "meli.la/A", "e2", desde=AGORA - 300), ("esgotou", False))  # 2ª versão
        # sem saber quando a postagem começou, toda retirada conta (como antes)
        self.assertTrue(tirou_do_post(self.con, "OFERTAMELI", AGORA - 10, "Produto", "e3", "7|a", "Loja A",
                                      "meli.la/A")[1])

    def test_fora_de_ontem_nao_faz_da_primeira_retirada_de_hoje_a_segunda_seguida(self):
        registrar_sinal(self.con, "OFERTAMELI", _inicio_do_dia(AGORA) - 3600, "fora", "breno", "você tirou ontem", "e0",
                        "7|lipx", "meli.la/ontem")
        self.avisar(AGORA - 600)
        self.grupo_postou_perfume(AGORA - 500)
        self.assertEqual(tirou_do_post(self.con, "OFERTAMELI", AGORA - 300, "Lattafa Yara", "e1", "8|outra", "Outra",
                                       "meli.la/hoje")[0], "fora")
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_aviso_que_diz_que_acabou_para_a_rodada_que_o_aviso_ligou(self):
        # 2ª revisão: "🎟️ CÓDIGO | regra | ESGOTOU" (o código numa linha e a palavra em outra) não parava a rodada
        self.avisar(AGORA - 900)
        self.grupo_postou_perfume(AGORA - 800)
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 700))
        # restrição não é fim do cupom
        self.assertEqual(self.avisar(AGORA - 650, "🎟️ *OFERTAMELI*\n15% OFF máx R$60, não vale com outros cupons", "r1"),
                         ["OFERTAMELI"])
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 640))
        # código falso ("ESGOTADO") ou nunca visto não vai para a lista da normalizadora
        self.avisar(AGORA - 630, "❌ 🎟️ *ESGOTADO*", "r2")
        self.avisar(AGORA - 620, "🎟️ *NUNCAVISTO99*\nESGOTOU!", "r3")
        self.assertEqual(self.avisar(AGORA - 600, "🎟️ *OFERTAMELI*\n15% OFF máx R$60 em Moda & Bem-Estar\nESGOTOU!", "fim1"),
                         [])
        self.assertEqual(sorted(esgotados(self.con, AGORA)), ["OFERTAMELI"])
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_esgotou_no_titulo_acima_do_ticket_para_a_rodada(self):
        # 2ª revisão: "❌ CUPOM ESGOTADO" acima do bloco do cupom religava a rodada por mais 40 min
        for n, titulo in enumerate(("❌ *CUPOM ESGOTADO*", "*ESGOTOU!* 😢", "⚠️ Pessoal, o cupom abaixo esgotou:")):
            with self.subTest(titulo=titulo):
                self.con.execute("DELETE FROM cupom_sinais")
                self.avisar(AGORA - 900)
                self.grupo_postou_perfume(AGORA - 800 - n)
                self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 700))
                fim = f"{titulo}\n\n🎟️ *OFERTAMELI*\n15% OFF máx R$60 em Moda & Bem-Estar"
                self.assertEqual(self.avisar(AGORA - 600, fim, f"fim{n}"), [])
                self.assertEqual(sorted(esgotados(self.con, AGORA)), ["OFERTAMELI"])
                self.assertEqual(quentes(self.con, AGORA), {})

    def test_cupom_novo_com_acabou_ou_restricao_na_linha_nao_vira_esgotado(self):
        # 2ª revisão: "(o MODA1010 acabou)" ou "(não vale com outros cupons)" na linha do cupom novo marcava o novo como
        # esgotado: o leitor dos grupos lê oração por oração
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite) "
                         "VALUES ('MODA1010', ?, ?, 10, 89, 20)", (AGORA - DIA, AGORA - DIA))
        textos = {"c1": "🎟️ *MODA10* (o MODA1010 acabou)\n10% OFF máx R$20 em Moda & Bem-Estar",
                  "c2": "🎟 10% OFF máx R$20 em Moda & Bem-Estar (não vale com outros cupons): *MODA10*"}
        for ref, texto in textos.items():
            registrar_cupons(self.con, texto, AGORA - 600, True, da_equipe=True)
            registrar_aviso_da_equipe(self.con, texto, AGORA - 600, ref)
            com_regra = {c for c, x in cupons_da_mensagem(texto).items() if x.pct or x.valor}
            registrar_sinais_de_grupo(self.con, texto, AGORA - 600, "Grupo da Equipe", ref, ignorar=com_regra)
        self.assertEqual(esgotados(self.con, AGORA), {})
        # no c1 não dá para saber quem acabou: ninguém leva aviso; no c2 a restrição não é fim: o MODA10 é aviso
        self.assertEqual([r[0] for r in self.con.execute("SELECT codigo FROM cupom_sinais WHERE tipo = 'aviso'")],
                         ["MODA10"])

    def test_sem_ticket_e_com_dois_codigos_o_fim_e_de_quem_a_oracao_diz(self):
        for codigo in ("MODA10", "CAMISA10"):
            self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite) "
                             "VALUES (?, ?, ?, 10, 79, 30)", (codigo, AGORA - 3600, AGORA - 600))
        texto = "Pessoal, o cupom MODA10 esgotou. Usem o cupom CAMISA10: 5% OFF acima de R$ 79, limite de R$ 30"
        registrar_aviso_da_equipe(self.con, texto, AGORA - 300, "m1")
        registrar_sinais_de_grupo(self.con, texto, AGORA - 300, "Grupo da Equipe", "m1")   # como a escuta faz
        self.assertEqual(sorted(esgotados(self.con, AGORA)), ["MODA10"])

    def test_esgotou_da_equipe_vale_para_codigo_que_volta_depois_de_dias(self):
        # 2ª revisão: o código sem data que volta depois de dias sem grupo (ultimo_ts velho) ficava sem o "esgotou"
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'OFERTAMELI'", (AGORA - 5 * DIA,))
        self.avisar(AGORA - 900)
        self.por_a_mao(AGORA - 800, posts=1)            # a prova: você pôs à mão em 1 post
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA - 700))
        registrar_sinais_de_grupo(self.con, "⚠️ Pessoal, o cupom *OFERTAMELI* esgotou!", AGORA - 600, "Grupo da Equipe",
                                  "fim2")
        self.assertIn("OFERTAMELI", esgotados(self.con, AGORA))
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_limite_sem_de_e_o_teto_e_porcentagem_sem_teto_nao_liga(self):
        # TODOSITE2810 (28/10 17:02): "30% OFF acima de R$99, limite R$35" era lido sem teto (30% inteiros no perfume)
        regra = cupons_da_mensagem("*CUPOM MERCADO LIVRE*\n🎟 30% OFF acima de R$99, limite R$35: *TODOSITE2810*\n")
        self.assertEqual((regra["TODOSITE2810"].pct, regra["TODOSITE2810"].minimo, regra["TODOSITE2810"].limite),
                         (30, 99, 35))
        self.assertEqual(self.avisar(AGORA - 600, "*CUPOM MERCADO LIVRE*\n🎟️ *SEMTETO*\n20% OFF em Moda & Bem-Estar\n",
                                     "a7"), ["SEMTETO"])
        self.grupo_postou_perfume(AGORA - 60, "SEMTETO")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_lojas_da_retirada_de_ontem_voltam_com_o_aviso_de_hoje(self):
        # 2ª revisão: a Lipx e a Klassey da rodada da noite seguiam "fora" na rodada do aviso da manhã
        meia_noite = _inicio_do_dia(AGORA)
        manha = meia_noite + 9 * 3600 + 360
        registrar_sinal(self.con, "OFERTAMELI", meia_noite - 2 * 3600 - 1200, "fora", "breno", "você tirou", "e1",
                        "1|lipx", "meli.la/p1")
        registrar_sinal(self.con, "OFERTAMELI", meia_noite - 2 * 3600 - 900, "esgotou", "breno", "você tirou", "e2",
                        "2|klassey", "meli.la/p2")
        self.assertEqual(sorted(lojas_fora(self.con, manha)["OFERTAMELI"]), ["1|lipx", "2|klassey"])
        self.avisar(manha)
        self.assertEqual(lojas_fora(self.con, manha + 60), {})

    def test_aviso_que_diz_que_esgotou_nao_liga_e_o_mesmo_aviso_nao_conta_duas_vezes(self):
        esgotou = "🎟️ *OFERTAMELI*\n15% OFF máx R$60 em Moda & Bem-Estar\nESGOTOU!"
        self.assertEqual(self.avisar(AGORA - 600, esgotou, "aviso3"), [])
        self.assertEqual(self.avisar(AGORA - 600), ["OFERTAMELI"])
        self.assertEqual(self.avisar(AGORA - 600), [])


class TestSuspensaoEEsgotamento(TestCupomQuente):
    """03/11 (Breno aprovou a separação, opção A): a retirada da Edição é suspensão preventiva; o "esgotou" explícito
    (grupo, equipe) e a saída da página oficial são esgotamento confirmado. Nenhum dos dois volta sozinho com o tempo, só
    com um sinal novo, posterior; e a rodada volta só com postagens novas, cada uma contada uma vez."""

    avisar = TestAvisoDaEquipeLigaARodada.avisar
    grupo_postou_perfume = TestAvisoDaEquipeLigaARodada.grupo_postou_perfume
    manter = TestAvisoDaEquipeLigaARodada.manter

    def estado(self, ts, codigo="OFERTAMELI"):
        return (esgotados(self.con, ts).get(codigo) or {}).get("estado", "ativo")

    def sinal(self, ts, tipo, fonte, ref, codigo="OFERTAMELI"):
        registrar_sinal(self.con, codigo, ts, tipo, fonte, f"{fonte}: {tipo}", ref)

    def por(self, ts, post, ref):
        registrar_sinal(self.con, "OFERTAMELI", ts, "posto", "breno", "você pôs à mão", ref, "7|lipx", post)

    def test_retirada_e_suspensao_e_aviso_explicito_e_esgotamento(self):
        self.sinal(AGORA - 900, "esgotou", "breno", "e1")
        e = esgotados(self.con, AGORA)["OFERTAMELI"]
        self.assertEqual((e["estado"], e["fonte"], e["motivo"]), ("suspenso", "breno", "suspenso: breno: esgotou"))
        registrar_sinais_de_grupo(self.con, "⚠️ o cupom OFERTAMELI esgotou", AGORA - 600, "Grupo X", "g1")
        e = esgotados(self.con, AGORA)["OFERTAMELI"]
        self.assertEqual((e["estado"], e["fonte"]), ("esgotado", "grupo"))          # suspenso + aviso explícito
        self.assertTrue(e["motivo"].startswith("esgotado: Grupo X avisou às "), e["motivo"])
        self.sinal(AGORA - 300, "esgotou", "breno", "e2")
        self.assertEqual(esgotados(self.con, AGORA)["OFERTAMELI"]["desde"], AGORA - 600)   # tirar de novo não muda

    def test_esgotado_da_equipe_e_esgotamento_e_a_saida_da_pagina_e_indicio(self):
        self.avisar(AGORA - 900, "🎟️ *OFERTAMELI*\n15% OFF máx R$60 em Moda & Bem-Estar\nESGOTOU!", "fim1")
        self.assertEqual(self.estado(AGORA), "esgotado")
        self.assertIn("a equipe avisou no GrupoFinal", esgotados(self.con, AGORA)["OFERTAMELI"]["motivo"])
        self.sinal(AGORA - 600, "esgotou", "pagina", "p1", "MODA1310")
        self.assertEqual(self.estado(AGORA, "MODA1310"), "indício")   # 04/11 (Astra): página não é aviso

    def test_doze_horas_nao_reativam(self):
        self.sinal(AGORA - 900, "esgotou", "breno", "e1")
        self.sinal(AGORA - 900, "esgotou", "grupo", "g1", "CHAPEU")
        for depois in (12 * 3600 + 60, DIA, 3 * DIA, 30 * DIA):
            self.assertEqual((self.estado(AGORA + depois), self.estado(AGORA + depois, "CHAPEU")),
                             ("suspenso", "esgotado"), depois)
        self.assertEqual(quentes(self.con, AGORA + DIA), {})
        motivo = esgotados(self.con, AGORA + DIA)["OFERTAMELI"]["motivo"]
        self.assertEqual(motivo, time.strftime("suspenso desde %d/%m: breno: esgotou", time.localtime(AGORA - 900)))

    def test_suspensao_sai_com_voce_pondo_ou_mantendo_ou_com_grupo_dizendo_que_voltou(self):
        for n, (tipo, fonte) in enumerate((("ativo", "breno"), ("posto", "breno"), ("ativo", "grupo"))):
            t = AGORA - 3000 + n * 600
            self.sinal(t, "esgotou", "breno", f"t{n}")
            self.assertEqual(self.estado(t + 1), "suspenso")
            self.sinal(t + 60, tipo, fonte, f"s{n}")
            self.assertEqual(self.estado(t + 61), "ativo", (tipo, fonte))

    def test_grupo_repostar_ou_grupofinal_publicar_nao_tira_a_suspensao(self):
        self.sinal(AGORA - 3600, "esgotou", "breno", "e1")
        for n in range(5):
            self.grupo_postou_perfume(AGORA - 3000 + n * 300)   # os grupos repostam cupom morto (MODA10, 01/11: 13 posts)
        self.postar_produto(AGORA - 900, "a|100")
        self.postar_produto(AGORA - 600, "b|100")
        self.assertEqual(self.estado(AGORA), "suspenso")
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_esgotado_nao_sai_com_manteve_so_com_por_a_mao(self):
        registrar_sinais_de_grupo(self.con, "o cupom OFERTAMELI esgotou", AGORA - 900, "Grupo X", "g1")
        self.manter(AGORA - 600, "meli.la/a1", "e1")      # post que veio com o cupom de antes do aviso
        self.assertEqual(self.estado(AGORA), "esgotado")
        self.por(AGORA - 300, "meli.la/a2", "e2")
        self.assertEqual(self.estado(AGORA), "ativo")
        # o indício da página oficial sai também com o "manteve", como desde 13/10
        self.sinal(AGORA - 900, "esgotou", "pagina", "p1", "MODA1310")
        self.sinal(AGORA - 600, "ativo", "breno", "e3", "MODA1310")
        self.assertEqual(self.estado(AGORA, "MODA1310"), "ativo")

    def test_os_dois_estados_ficam_a_parte(self):
        self.sinal(AGORA - 900, "esgotou", "breno", "e1")
        registrar_sinais_de_grupo(self.con, "o cupom OFERTAMELI esgotou", AGORA - 800, "Grupo X", "g1")
        self.assertEqual(self.estado(AGORA), "esgotado")
        self.avisar(AGORA - 600)          # aviso do mesmo dia: tira o esgotamento do grupo, não a sua retirada
        self.assertEqual((self.estado(AGORA), esgotados(self.con, AGORA)["OFERTAMELI"]["desde"]), ("suspenso", AGORA - 900))

    def test_volta_para_a_pagina_nao_apaga_o_aviso_do_grupo(self):
        # 04/11 (Astra reproduziu): esgotou pela página -> esgotou pelo grupo -> voltou para a página. A volta só desfaz
        # o indício da página; o aviso explícito do grupo continua valendo.
        self.sinal(AGORA - 900, "esgotou", "pagina", "p1")
        registrar_sinais_de_grupo(self.con, "o cupom OFERTAMELI esgotou", AGORA - 600, "Grupo X", "g1")
        self.sinal(AGORA - 300, "ativo", "pagina", "p2")
        e = esgotados(self.con, AGORA)["OFERTAMELI"]
        self.assertEqual((e["estado"], e["fonte"]), ("esgotado", "grupo"))
        self.manter(AGORA - 200, "meli.la/a1", "e1")          # manter não desfaz o aviso explícito
        self.assertEqual(self.estado(AGORA), "esgotado")

    def test_saida_da_pagina_e_indicio_nao_aviso(self):
        # 04/11 (Astra: "desaparecer de uma página que pode estar desatualizada deveria ser identificado como indício")
        self.sinal(AGORA - 900, "esgotou", "pagina", "p1")
        e = esgotados(self.con, AGORA)["OFERTAMELI"]
        self.assertEqual((e["estado"], e["motivo"]), ("indício", "indício: pagina: esgotou"))
        self.sinal(AGORA - 600, "ativo", "pagina", "p2")       # voltou para a página
        self.assertEqual(self.estado(AGORA), "ativo")
        self.sinal(AGORA - 500, "esgotou", "pagina", "p3")
        self.manter(AGORA - 400, "meli.la/a1", "e1")           # o indício sai com o "manteve" (como desde 13/10)
        self.assertEqual(self.estado(AGORA), "ativo")
        # aviso explícito e indício juntos: aparece o aviso; a volta para a página tira só o indício
        registrar_sinais_de_grupo(self.con, "o cupom OFERTAMELI esgotou", AGORA - 300, "Grupo X", "g2")
        self.sinal(AGORA - 250, "esgotou", "pagina", "p4")
        self.assertEqual(esgotados(self.con, AGORA)["OFERTAMELI"]["estados"], ["esgotado", "indício"])
        self.sinal(AGORA - 200, "ativo", "pagina", "p5")
        self.assertEqual(esgotados(self.con, AGORA)["OFERTAMELI"]["estados"], ["esgotado"])

    def test_sinal_de_antes_da_suspensao_nao_tira(self):
        self.manter(AGORA - 1000, "meli.la/a1", "e1")
        self.avisar(AGORA - 950)
        self.sinal(AGORA - 900, "esgotou", "breno", "e2")
        self.assertEqual(self.estado(AGORA), "suspenso")

    def test_rodada_volta_so_com_duas_postagens_novas_e_versoes_contam_uma(self):
        self.manter(AGORA - 1500, "meli.la/a0", "e0")          # de antes da suspensão: não conta
        self.sinal(AGORA - 1200, "esgotou", "breno", "t1")
        self.manter(AGORA - 900, "meli.la/a1", "e1")           # tira a suspensão
        self.assertEqual(self.estado(AGORA), "ativo")
        self.manter(AGORA - 800, "meli.la/a1", "e2")           # 2ª versão do mesmo post: a mesma postagem
        self.assertEqual(quentes(self.con, AGORA), {})
        self.manter(AGORA - 700, "meli.la/a2", "e3")           # outra postagem: agora são 2
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_esgotou_de_grupo_tambem_reinicia_a_contagem_da_rodada(self):
        self.manter(AGORA - 1000, "meli.la/a1", "e1")          # de antes do "esgotou" do grupo
        registrar_sinais_de_grupo(self.con, "o cupom OFERTAMELI esgotou", AGORA - 900, "Grupo X", "g1")
        self.por(AGORA - 300, "meli.la/a2", "e2")
        self.assertEqual(self.estado(AGORA), "ativo")
        self.assertEqual(quentes(self.con, AGORA), {})          # só 1 postagem depois do "esgotou"
        self.por(AGORA - 200, "meli.la/a3", "e3")
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_normalizadora_tira_suspenso_e_esgotado_enquanto_o_codigo_circula(self):
        self.sinal(AGORA - 900, "esgotou", "breno", "e1")
        self.sinal(AGORA - 900, "esgotou", "grupo", "g1", "CHAPEU")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts) VALUES ('CHAPEU', ?, ?)",
                         (AGORA - 1000, AGORA - 1000))
        lista = lista_para_normalizadora(self.con, AGORA)
        self.assertEqual((lista["OFERTAMELI"]["estado"], lista["CHAPEU"]["estado"]), ("suspenso", "esgotado"))
        quatro_dias = AGORA + 4 * DIA
        self.assertNotIn("OFERTAMELI", lista_para_normalizadora(self.con, quatro_dias))   # nenhum grupo usa há 3 dias
        self.assertEqual(self.estado(quatro_dias), "suspenso")                           # ... mas continua parado
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'OFERTAMELI'", (quatro_dias - 60,))
        self.assertIn("OFERTAMELI", lista_para_normalizadora(self.con, quatro_dias))      # voltou a circular

    def publicar(self, ts, link, produto="a|100"):
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, link, dados) "
                         "VALUES (?,?,?,?,?,?,?,?)", (f"h{ts}{link}", ts, "Grupo da Equipe", produto, produto, 99,
                                                      f"https://{link}", json.dumps({"cupons": ["OFERTAMELI"]})))

    def conferida(self, link, ts):   # a promo desse link passou pela normalizadora e a curadora conferiu
        guardar_loja_do_link(self.con, link, ts, "7|lipx", "Lipx")

    def test_grupofinal_soma_uma_confirmacao_so_conferida_depois_e_sem_repetir_a_edicao(self):
        # 04/11 (Breno: "Pode contar como uma confirmação, sem duplicar a confirmação da mesma postagem na Edição.
        # Publicar depois da suspensão não prova que a oferta foi conferida depois dela.")
        self.sinal(AGORA - 3000, "esgotou", "breno", "t1")
        self.manter(AGORA - 900, "meli.la/a1", "e1")             # tira a suspensão: 1 confirmação
        self.assertEqual(self.estado(AGORA), "ativo")
        self.conferida("meli.la/a1", AGORA - 1000)
        self.publicar(AGORA - 600, "meli.la/a1")                # a mesma postagem da Edição: não conta de novo
        self.conferida("meli.la/b2", AGORA - 3500)
        self.publicar(AGORA - 500, "meli.la/b2", "b|100")       # publicado depois, conferido antes da suspensão
        self.publicar(AGORA - 450, "meli.la/d4", "d|100")       # direto no GrupoFinal: sem prova de conferência depois
        self.assertEqual(quentes(self.con, AGORA), {})
        self.conferida("meli.la/c3", AGORA - 800)
        self.publicar(AGORA - 400, "meli.la/c3", "c|100")       # conferido depois da suspensão: soma
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "post no GrupoFinal")

    def test_post_no_grupofinal_nao_tira_a_suspensao(self):
        self.sinal(AGORA - 3000, "esgotou", "breno", "t1")
        for n, link in enumerate(("meli.la/c3", "meli.la/c4")):
            self.conferida(link, AGORA - 800 + n)
            self.publicar(AGORA - 400 + n, link, f"c{n}|100")
        self.assertEqual((self.estado(AGORA), quentes(self.con, AGORA)), ("suspenso", {}))

    def test_frescor_pelo_grupofinal_depois_da_parada_so_com_oferta_conferida_depois(self):
        self.sinal(AGORA - 7000, "esgotou", "breno", "t1")
        self.por(AGORA - 3500, "meli.la/a1", "e1")
        self.por(AGORA - 3400, "meli.la/a2", "e2")             # 2 postagens, mas a última há mais de 40 min
        self.assertEqual(quentes(self.con, AGORA), {})
        self.conferida("meli.la/a1", AGORA - 7100)              # oferta conferida antes da parada
        self.publicar(AGORA - 300, "meli.la/a1")
        self.assertEqual(quentes(self.con, AGORA), {})
        self.conferida("meli.la/a2", AGORA - 3600)              # conferida depois da parada: renova os 40 min
        self.publicar(AGORA - 200, "meli.la/a2", "b|100")
        self.assertEqual(quentes(self.con, AGORA)["OFERTAMELI"]["por"], "post no GrupoFinal")

    def test_sem_parada_edicao_e_grupofinal_somam_cada_postagem_uma_vez(self):
        self.manter(AGORA - 900, "meli.la/a1", "e1")
        self.publicar(AGORA - 600, "meli.la/a1")                # a mesma postagem: continua 1
        self.assertEqual(quentes(self.con, AGORA), {})
        self.publicar(AGORA - 300, "meli.la/b2", "b|100")       # outra postagem, no GrupoFinal: 2
        self.assertIn("OFERTAMELI", quentes(self.con, AGORA))

    def test_regra_dos_45_min_so_observa(self):
        self.sinal(AGORA - 3600, "esgotou", "breno", "e1")
        self.assertEqual(sombra_dos_45_min(self.con, AGORA - 3600 + 600), {})            # ainda não passaram 45 min
        self.assertEqual(sombra_dos_45_min(self.con, AGORA), {"OFERTAMELI": AGORA - 3600 + 45 * 60})
        self.assertEqual(self.estado(AGORA), "suspenso")                                  # não muda nada
        self.grupo_postou_perfume(AGORA - 3000)                                            # um grupo usou nos 45 min
        self.assertEqual(sombra_dos_45_min(self.con, AGORA), {})


class TestSecaoDoAvisoDaEquipe(unittest.TestCase):
    """01/11: dos cupons do aviso da equipe no GrupoFinal, quais podem valer em perfume (avisos reais de 06/10 a 01/11)."""

    FIM = "\n_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1"
    AVISOS = {
        "01/11": ("*CUPOM MERCADO LIVRE*\n🎟️ *MELIMAIS0111*\n20% OFF máx de R$70 p/ Meli+\n🎟️ *MODA10*\n"
                  "10% OFF máx R$20 em Moda & Bem-Estar", {"MODA10"}),
        "31/10 09h": ("*CUPOM MERCADO LIVRE*\n🎟️ *50TODOSITE*  - 50/499\n🎟️ *OFFNOTECH*\n10% OFF acima de R$149 máx R$200\n"
                      "🎟️ *LIVROS3110*\n15% OFF máx R$40 em Livros & Jogos", set()),
        "31/10 12h": ("*CUPOM MERCADO LIVRE*\n🎟️ *TECH100*\nR$100 OFF em Celular, PC, TECH\n🎟️ *MODA3110*\n"
                      "15% OFF máx R$20 em Moda & Bem-Estar", {"MODA3110"}),
        "31/10 15h": ("*CUPOM MERCADO LIVRE*\n🎟️ *MELIPROMO3111*\n20% OFF máx de R$80 p/ Meli+", set()),
        "30/10": ("🚨🚨 *NOVO CUPONS MERCADO LIVRE*\n🎟️ *OFICIAS3010*\n*10% OFF* acima de R$ 89, limite de *R$ 30*\n"
                  "🎟️ *BEBES3010*\n*10% OFF* acima de R$ 89, limite de *R$ 30*\n🎟️ *DECOR3010*\n"
                  "*R$50 OFF* acima de R$ 399, limite de *R$ 50*", {"OFICIAS3010"}),
        # o FULL2510 só vale em anúncio com envio Full: a rodada poria o cupom nos outros
        "25/10": ("*CUPOM MERCADO LIVRE*\n🎟️ *MODA2510*\n20% OFF máx R$70 !!! em Moda & Bem-Estar\n🎟️ *FULL2510*\n"
                  "10% OFF máx R$20 em Entregas Full!", {"MODA2510"}),
        "14/10": ("*CUPOM MERCADO LIVRE*\n🎟️ *LOJAS1410*\n20% OFF máx R$50 em Lojas Oficiais\n🎟️ *TELEVISAO1410*\n"
                  "R$100 OFF acima de R$1299 em TVS e Celulares\n🎟️ *FERRAMENTAS1410*\n15% OFF máx R$60 em Auto & "
                  "Ferramentas", {"LOJAS1410"}),
        "08/10": ("*NOVOS CUPONS MERCADO LIVRE*\n\n🎟️ *15% OFF* , limite de R$ 50, acima de R$ 199 -\nCupom: *LIVROS0810*\n\n"
                  "🎟️ *15% OFF* acima de R$ 89, limitado a *R$ 40* -\nCupom: *TIENDAS0810*\n", {"TIENDAS0810"}),
        "06/10": ("*CUPOM MERCADO LIVRE*\n🎟️ *MODA0610*\n15% OFF máx R$30 em Moda & Bem-Estar\n🎟️ *CASA0610*\n"
                  "10% OFF máx. R$50 em itens Casa", {"MODA0610"}),
        "21/10": ("🚨🚨 *NOVO CUPOM MERCADO LIVRE*\n🎟️ *CAMISA10*\n*5% OFF* acima de R$ 79, limite de *R$ 30*", {"CAMISA10"}),
        # o SUPER15 saiu em 13 promos de perfume nos grupos e o Breno nunca usou; o LOJAS fica gravado (a trava da loja
        # da marca é que o tira da rodada geral)
        # 03/11 (relatório da Astra): sem o OFF o percentual não era lido e o aviso não virava sinal
        "03/11 18h": ("*CUPOM MERCADO LIVRE*\n\n🎟️  *SITE031115*\n15% acima de R$89 máx R$20 no Mercado Livre\n",
                      {"SITE031115"}),
        # o 100TODOSITE fica gravado, mas a mínima de R$999 o deixa fora da rodada (avisos_com_prova)
        "01/11 12h": ("*CUPOM MERCADO LIVRE*\n🎟️ *SITE200111* - R$20/299\n🎟️ *100TODOSITE* - R$100/999\n",
                      {"SITE200111", "100TODOSITE"}),
        "01/11 15h": ("*CUPOM MERCADO LIVRE*\n\n🎟️ *LOJAS0111*\n10% OFF máx R$40 em Lojas Oficiais\n\n🎟️ *SUPER15*\n"
                      "15% OFF máx R$30 em Supermercado\n", {"LOJAS0111"}),
        "sem 🎟️": ("*CUPOM MERCADO LIVRE*\nCupom: *ACHEINOMELI* 10% OFF acima de R$ 79, limite de R$ 30", {"ACHEINOMELI"}),
        "Mercado Pago": ("*CUPOM MERCADO LIVRE*\n🎟️ *PAGUEMP*\nR$ 20 OFF acima de R$ 99 pagando com Mercado Pago",
                         {"PAGUEMP"}),
        # 2ª revisão: o "_" do itálico conta como letra para o \b
        "itálico": ("*CUPOM MERCADO LIVRE*\n🎟️ *ENVIO0211*\n10% OFF máx R$20 em _Entregas Full_\n🎟️ *ITENS0211*\n"
                    "10% OFF máx R$50 em _itens Casa_\n🎟️ *MODA0211*\n10% OFF máx R$20 em _Moda & Bem-Estar_",
                    {"MODA0211"}),
        "outras seções": ("*CUPOM MERCADO LIVRE*\n🎟️ *PLUSDEZ*\n10% OFF máx R$30 p/ Meli Plus\n🎟️ *CARRODEZ*\n"
                          "10% OFF máx R$50 em Acessórios para Veículos\n🎟️ *BICHODEZ*\n10% OFF máx R$30 em Animais\n"
                          "🎟️ *FORADEZ*\n10% OFF máx R$30 em Compra Internacional", set()),
        # 🎟️ no fim da linha do código (formato de 10/10 08:12) e a seção na linha de baixo
        "🎟️ no fim": ("*🚨🚨 NOVO CUPOM MERCADO LIVRE*\nCupom *15% OFF* limitado a R$ 30 - *SUPER15* 🎟️\nem Supermercado",
                      set()),
        # o título que cita os códigos não é o trecho do SUPER15 (lá não aparece o "Supermercado")
        "título com os códigos": ("🚨 *CUPONS DE HOJE: LOJAS0111 E SUPER15*\n🎟️ *LOJAS0111*\n10% OFF máx R$40 em Lojas "
                                  "Oficiais\n🎟️ *SUPER15*\n15% OFF máx R$30 em Supermercado", {"LOJAS0111"}),
    }

    def test_so_o_que_pode_valer_em_perfume_vira_aviso(self):
        for dia, (texto, podem) in self.AVISOS.items():
            with self.subTest(dia=dia):
                con = conectar(":memory:")
                registrar_aviso_da_equipe(con, texto + self.FIM, AGORA, f"a{dia}")
                gravados = {r[0] for r in con.execute("SELECT codigo FROM cupom_sinais WHERE tipo = 'aviso'")}
                self.assertEqual(gravados, podem)

    def test_regra_acima_do_codigo_e_a_secao_de_cada_um(self):
        texto = self.AVISOS["08/10"][0] + self.FIM
        regras = cupons_da_mensagem(texto)
        self.assertEqual((regras["LIVROS0810"].minimo, regras["TIENDAS0810"].minimo), (199, 89))
        self.assertEqual((fora_de_perfume(texto, "LIVROS0810"), fora_de_perfume(texto, "TIENDAS0810")), ("livro", ""))


class TestCupomDaLojaDaMarca(unittest.TestCase):
    """29/10 (Breno): cupom LOJAS<data> vale só na loja oficial da própria marca (Natura da Natura, Boticário do
    Boticário); revenda com selo de loja oficial (Lipx, Klassey, Suffein) não tem."""

    def setUp(self):
        self.revenda = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True)
        self.marca = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lattafa", loja_oficial=True)
        self.cur = Curadora({**CFG, "cupom_quente": {"ligado": True}}, banco_com_historico(ASAD_NORMAL),
                            MLFalso({"https://meli.la/r": self.revenda, "https://meli.la/m": self.marca}))
        self.con = self.cur.con
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('LOJASHOJE', ?, ?, 10, 79, 30, ?)", (AGORA - 7 * 3600, AGORA - 600, AGORA))

    def texto(self, link):
        return f"*Lattafa Asad 100ml*\nPor R$ 200\nCupom: LOJASHOJE\nhttps://meli.la/{link}"

    def confirmar_na_marca(self, ts):
        for n, tipo in enumerate(("ativo", "posto")):
            registrar_sinal(self.con, "LOJASHOJE", ts, tipo, "breno", "você pôs à mão", f"edicao:m{ts}", "1957713361|natura")

    def test_loja_da_marca_x_revenda(self):
        from curadora.esgotados import loja_da_marca
        for loja in ("Natura", "1957713361|natura", "O Boticário", "208339111|oboticario", "Eudora", "Jean Paul Gaultier"):
            self.assertTrue(loja_da_marca(loja), loja)
        for loja in ("Lipx", "2540338692|lipx", "SUFFEIN OFICIAL", "suffeinoficial", "KLASSEY", "Wathiq Parfums", ""):
            self.assertFalse(loja_da_marca(loja), loja)

    def test_tirar_de_revenda_e_nao_vale_nessa_loja(self):
        from curadora.esgotados import tirou_do_post
        tipo, _ = tirou_do_post(self.con, "LOJASHOJE", AGORA - 60, "Lattafa Asad", "edicao:x1", "2540338692|lipx", "Lipx")
        self.assertEqual(tipo, "fora")
        self.assertNotIn("LOJASHOJE", esgotados(self.con, AGORA))

    def test_esgotou_antigo_de_revenda_nao_mata_o_cupom(self):
        # Os 3 "esgotou" de 29/10 18:35-18:39 (Suffein, Lipx, Klassey) já gravados
        for n, loja in enumerate(("suffeinoficial", "2540338692|lipx", "klassey")):
            registrar_sinal(self.con, "LOJASHOJE", AGORA - 300 + n, "esgotou", "breno", "você tirou", f"edicao:e{n}", loja)
        self.assertNotIn("LOJASHOJE", esgotados(self.con, AGORA))
        registrar_sinal(self.con, "LOJASHOJE", AGORA - 100, "esgotou", "breno", "você tirou", "edicao:n1", "1957713361|natura")
        self.assertIn("LOJASHOJE", esgotados(self.con, AGORA))   # tirou de loja da marca: acabou mesmo

    def test_na_loja_da_marca_vale_depois_dos_40_min_e_na_revenda_nunca(self):
        self.confirmar_na_marca(AGORA - 2 * 3600)          # confirmado na Natura há 2h: já não é "quente"
        self.assertEqual(quentes(self.con, AGORA), {})
        av = self.cur.avaliar(self.texto("m"), agora=AGORA)
        self.assertEqual(av.cupom_quente["codigo"], "LOJASHOJE")
        self.assertEqual(av.preco_efetivo, 180)                                         # 200 − 10%
        self.assertEqual(self.cur.cupom_da_equipe(200, AGORA, card=self.marca)["codigo"], "LOJASHOJE")
        av = self.cur.avaliar(self.texto("r"), agora=AGORA)
        self.assertIsNone(av.cupom_quente)
        self.assertIn("não vale na revenda", av.cupons_mortos["LOJASHOJE"])
        self.assertIsNone(self.cur.cupom_da_equipe(200, AGORA, card=self.revenda))

    def test_valvula_por_a_mao_em_revenda_libera_no_dia(self):
        # 30/10: o LOJAS1710 de 17/10 valeu na Lipx e na Wathiq; o Breno pôs à mão em 2 da Lipx
        from curadora.esgotados import trava_da_marca
        self.confirmar_na_marca(AGORA - 600)
        registrar_sinal(self.con, "LOJASHOJE", AGORA - 300, "ativo", "breno", "você manteve", "edicao:r0", "2540338692|lipx")
        self.assertTrue(trava_da_marca(self.con, "LOJASHOJE", AGORA))          # "manteve" não abre
        registrar_sinal(self.con, "LOJASHOJE", AGORA - 200, "posto", "breno", "você pôs", "edicao:r1", "2540338692|lipx")
        self.assertFalse(trava_da_marca(self.con, "LOJASHOJE", AGORA))
        self.assertEqual(self.cur.cupom_da_equipe(200, AGORA, card=self.revenda)["codigo"], "LOJASHOJE")
        self.assertNotIn("LOJASHOJE", self.cur.avaliar(self.texto("r"), agora=AGORA).cupons_mortos)

    def test_chave_desligada_tira_a_trava_pelo_nome(self):
        from unittest import mock
        from curadora import esgotados as e
        with mock.patch.object(e, "_lojas_marca_ligada", return_value=False):
            self.assertFalse(e.trava_da_marca(self.con, "LOJASHOJE", AGORA))
            self.assertNotIn("LOJASHOJE", self.cur.avaliar(self.texto("r"), agora=AGORA).cupons_mortos)

    def test_sem_frescor_a_loja_da_marca_tambem_para(self):
        self.confirmar_na_marca(AGORA - 2 * 3600)
        self.con.execute("UPDATE cupons SET ultimo_ts = ? WHERE codigo = 'LOJASHOJE'", (AGORA - 50 * 60,))
        self.assertIsNone(self.cur.cupom_da_equipe(200, AGORA, card=self.marca))

    def test_sem_confirmacao_numa_loja_da_marca_nao_vale(self):
        av = self.cur.avaliar(self.texto("m"), agora=AGORA)
        self.assertIsNone(av.cupom_quente)
        self.assertIsNone(self.cur.cupom_da_equipe(200, AGORA, card=self.marca))


class TestQuemVende(unittest.TestCase):
    def curadora(self, ofertas, rep):
        class APIFalsa:
            def configurado(self): return True
            def ofertas(self, pid): return ofertas
            def vendedor(self, sid): return rep
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=ofertas[0].preco, produto_id="MLB1",
                      item_id=ofertas[0].item_id, full=True)
        return Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}),
                        cliente_api=APIFalsa())

    def ofertas(self, preco):
        # Victorioso (09/10): o link destaca um vendedor a R$78; o 2º mais barato está a R$120.
        return [OfertaML("MLB9", preco, None, 1, False, True), OfertaML("MLB8", 120, None, 2, False, True),
                OfertaML("MLB7", 134.9, None, 3, True, True)]

    def test_confiavel_muito_abaixo_dos_outros_e_achado(self):
        cur = self.curadora(self.ofertas(78), reputacao("KALITYUSABRF", "5_green", "platinum", 52219))
        av = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 78\nhttps://meli.la/a", agora=AGORA)
        self.assertFalse(av.risco)
        c = av.card.catalogo
        self.assertEqual((c["vendedores"], c["segundo"], c["oficial"]), (3, 120, 134.9))
        # 31/10: e quem vende cada uma (o anúncio e o vendedor)
        self.assertEqual((c["segundo_oferta"]["item_id"], c["oficial_oferta"]["seller_id"]), ("MLB8", 3))
        self.assertEqual(texto_confianca(av), "KALITYUSABRF · Platinum · 52 mil vendas · Full · 35% abaixo do 2º vendedor")

    def test_sem_reputacao_muito_abaixo_dos_outros_e_risco(self):
        # Perto do preço normal de 60 dias (as regras antigas não pegam), mas 27% abaixo de todos os outros
        # vendedores do ML, de um vendedor sem medalha com 300 vendas: padrão de falsificado.
        ofertas = [OfertaML("MLB9", 190, None, 1, False, False), OfertaML("MLB8", 260, None, 2, False, True),
                   OfertaML("MLB7", 270, None, 3, True, True)]
        cur = self.curadora(ofertas, reputacao("LOJINHA", "5_green", None, 300))
        av = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 190\nhttps://meli.la/a", agora=AGORA)
        self.assertEqual(av.risco, "medio")
        self.assertIn("abaixo de todos os outros 2 vendedores", av.risco_motivo)

    def test_compara_sem_o_desconto_do_pix(self):
        # 18/10 (Maleka): o card mostra R$187,92 no Pix e "ou R$216 em outros meios"; a API dá os outros vendedores
        # sem Pix (2º: R$217,90). O relatório dizia "14% abaixo do 2º vendedor"; igual com igual é 1%.
        class APIFalsa:
            def configurado(self): return True
            def ofertas(self, pid): return [OfertaML("MLB9", 216, None, 1, True, True),
                                            OfertaML("MLB8", 217.9, None, 2, False, True)]
            def vendedor(self, sid): return reputacao("LUZ MAKE UP", "5_green", "platinum", 90000)
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=187.92, preco_outros=216, produto_id="MLB1",
                      item_id="MLB9", full=True)
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}), cliente_api=APIFalsa())
        av = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 187,92\nhttps://meli.la/a", agora=AGORA)
        self.assertEqual(av.card.catalogo["segundo"], 217.9)
        self.assertNotIn("abaixo do 2º vendedor", texto_confianca(av))
        self.assertFalse(any("2º vendedor" in r for r in av.razoes))

    def test_parcela_nao_vira_preco_sem_pix(self):
        self.assertEqual(preco_sem_pix(CardML(ok=True, preco=187.92, preco_outros=216)), 216)
        self.assertEqual(preco_sem_pix(CardML(ok=True, preco=200, preco_outros=21.6)), 200)   # "10x de R$21,60"
        self.assertEqual(preco_sem_pix(CardML(ok=True, preco=200)), 200)
        self.assertIsNone(preco_sem_pix(None))


class TestInternacional(unittest.TestCase):
    def test_compra_internacional_nao_pode_ser_postada(self):
        # 09/10 (Breno): "o Fierce era internacional, não poderia ser postado". O ML marca o anúncio com cbt_item.
        class APIFalsa:
            def configurado(self): return True
            def ofertas(self, pid): return [OfertaML("MLB9", 150, None, 1, False, True, internacional=True),
                                            OfertaML("MLB8", 200, None, 2, False, True)]
            def vendedor(self, sid): return dict(reputacao("VERSUREUSABRF", "5_green", "platinum", 5600), pais="US")
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=150, produto_id="MLB1", item_id="MLB9", full=True)
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}), cliente_api=APIFalsa())
        av = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", agora=AGORA)
        self.assertTrue(av.card.internacional)
        self.assertEqual(av.nota, "NÃO VALE")          # 25% abaixo do normal, mas não pode ir para o grupo
        self.assertTrue(av.alertas[0].startswith("COMPRA INTERNACIONAL"))
        self.assertTrue(texto_confianca(av).startswith("🌎 internacional (US)"))


class TestCupomVelho(unittest.TestCase):
    """10/10: cupom que nenhum grupo usa há mais de 1h não segura o preço do card (Asad Elixir Excelente a R$175
    por um cupom visto 18h antes, com R$227 no ML)."""

    def setUp(self):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True)
        self.cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/a": card}))
        self.con = self.cur.con
        self.texto = "*Lattafa Asad 100ml*\nPor R$ 170\nCupom: OFERTAMELI\nhttps://meli.la/a"

    def cupom_visto(self, ha_segundos):
        self.con.execute("INSERT OR REPLACE INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('OFERTAMELI', ?, ?, 15, 79, 60, ?)", (AGORA - 90000, AGORA - ha_segundos, AGORA - ha_segundos))

    def test_cupom_usado_ha_pouco_segura_o_preco(self):
        self.cupom_visto(20 * 60)
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertEqual((av.preco_efetivo, av.cupons_mortos), (170, {}))

    def test_cupom_que_nenhum_grupo_usa_ha_horas_vira_preco_do_ml(self):
        self.cupom_visto(18 * 3600)
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertEqual(av.preco_efetivo, 200)
        self.assertIn("velho: nenhum grupo usa há 18h", av.cupons_mortos["OFERTAMELI"])
        self.assertTrue(any("velho" in a and "preço sem cupom" in a for a in av.alertas))

    def test_cupom_que_o_breno_manteve_ha_pouco_nao_e_velho(self):
        self.cupom_visto(3 * 3600)
        registrar_sinal(self.con, "OFERTAMELI", AGORA - 600, "ativo", "breno", "você manteve", "e1")
        self.assertEqual(self.cur.avaliar(self.texto, agora=AGORA).preco_efetivo, 170)

    def test_segunda_leitura_da_normalizadora_nao_usa_essa_regra(self):
        self.cupom_visto(3 * 3600)
        self.cur.sem_cupom_velho = True
        self.assertEqual(self.cur.avaliar(self.texto, agora=AGORA).preco_efetivo, 170)

    def test_cupom_nunca_registrado_fica_como_antes(self):
        av = self.cur.avaliar(self.texto, agora=AGORA)
        self.assertEqual(av.cupons_mortos, {})


class TestMesmoAnuncio(unittest.TestCase):
    """10/10: Bareeq Al Dhahab postado às 16:39; o card "Bareeq" (mesmo anúncio, outro nome) seguia como não postado."""

    def test_card_do_mesmo_anuncio_com_outro_nome_vira_postado(self):
        import json
        from curadora.postagens import ids_do_anuncio, marcar_mesmo_anuncio
        con = conectar(":memory:")
        def avaliacao(chave, preco, url):
            return con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                               (AGORA - 3600, "ao vivo", "g", chave, "IMPERDÍVEL", 78,
                                json.dumps({"preco": preco, "ml": {"url": url}}))).lastrowid
        mesmo = avaliacao("bareeq|100", 118.41, "https://www.mercadolivre.com.br/perfume-bareeq/p/MLB63400720")
        caro = avaliacao("bareeq|100", 160.0, "https://www.mercadolivre.com.br/perfume-bareeq/p/MLB63400720")
        outro = avaliacao("asad|100", 118.0, "https://produto.mercadolivre.com.br/MLB-999999999-asad")
        card = CardML(ok=True, titulo="Bareeq Al Dhahab", preco=118.41, produto_id="MLB63400720",
                      url_produto="https://www.mercadolivre.com.br/x/p/MLB63400720")
        # o card nasceu a R$110 (fora dos 5%) e a reconferência dele está a R$118,41: marca a original também
        nasceu = avaliacao("bareeq|100", 110.0, "https://www.mercadolivre.com.br/perfume-bareeq/p/MLB63400720")
        con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                    (AGORA - 600, "reconferência", "g", "bareeq|100", "IMPERDÍVEL", 78, json.dumps(
                        {"preco": 118.41, "reconferida_de": nasceu, "ml": {"url": "https://www.mercadolivre.com.br/x/p/MLB63400720"}})))
        marcar_mesmo_anuncio(con, ids_do_anuncio(card), 118.41, AGORA, "Grupo da Equipe")
        postadas = {r[0]: r[1] for r in con.execute("SELECT id, COALESCE(postada, 0) FROM avaliacoes")}
        self.assertEqual((postadas[mesmo], postadas[caro], postadas[outro], postadas[nasceu]), (AGORA, 0, 0, AGORA))


class TestCupomNaoValeNaLoja(unittest.TestCase):
    """11/10 (Breno: "não enviei alguns perfumes para a Edição pois o cupom não se aplicava neles e se eu removesse
    acabaria a rodada"): tirar o cupom da equipe de UM post = não vale nessa loja; de dois seguidos = acabou."""

    def setUp(self):
        from curadora.esgotados import chaves_da_loja, tirou_do_post
        self.con = conectar(":memory:")
        self.cur = Curadora({**CFG, "cupom_quente": {"ligado": True}}, self.con, None)
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('MODAHOJE', ?, ?, 15, 89, 40, ?)", (AGORA - 900, AGORA - 900, AGORA - 900))
        for n in (1, 2):   # ele manteve o cupom em 2 posts: cupom da equipe valendo
            registrar_sinal(self.con, "MODAHOJE", AGORA - 600 - n, "ativo", "breno", "você manteve", f"e{n}",
                            "7|lipx", f"meli.la/p{n}")
        self.armani = CardML(ok=True, titulo="Armani Code 125ml", preco=500, vendedor="Armani Beauty", seller_id=11)
        self.lipx = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", seller_id=7)
        self.tirou = lambda ha_s, ref, card, post, **mais: tirou_do_post(
            self.con, "MODAHOJE", AGORA - ha_s, "Perfume", ref, *chaves_da_loja(card), post=post, **mais)

    def test_tirar_de_um_post_so_tira_o_cupom_daquela_loja(self):
        self.assertEqual(self.tirou(60, "e3", self.armani, "meli.la/p3"), ("fora", True))
        self.assertIn("MODAHOJE", quentes(self.con, AGORA))                    # a rodada continua
        self.assertEqual(lista_para_normalizadora(self.con, AGORA), {})        # a normalizadora não tira o cupom
        self.assertIsNone(self.cur.cupom_da_equipe(500, AGORA, self.armani))   # nessa loja vai sem o cupom
        self.assertEqual(self.cur.cupom_da_equipe(200, AGORA, self.lipx)["preco"], 170)
        # outro anúncio do mesmo vendedor, lido só com o id: também fica sem
        outro = CardML(ok=True, titulo="Armani My Way 50ml", preco=400, seller_id=11)
        self.assertIsNone(self.cur.cupom_da_equipe(400, AGORA, outro))
        motivo = self.con.execute("SELECT detalhe FROM cupom_sinais WHERE tipo = 'fora'").fetchone()[0]
        self.assertIn("não vale na loja Armani Beauty", motivo)

    def test_tirar_de_dois_posts_seguidos_acaba_a_rodada(self):
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        outra = CardML(ok=True, titulo="Boss Bottled 100ml", preco=400, vendedor="Mercado Livre", seller_id=99)
        self.assertEqual(self.tirou(30, "e4", outra, "meli.la/p4"), ("esgotou", True))
        self.assertEqual(quentes(self.con, AGORA), {})
        self.assertIn("2 posts seguidos", esgotados(self.con, AGORA)["MODAHOJE"]["motivo"])

    def test_dois_posts_seguidos_da_mesma_loja_tambem_acabam(self):
        # 21/10 (Breno: "aprovo apenas B"): Yara Moi e Ferassa, os dois da Lipx, às 19:02–19:03; o cupom tinha acabado
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        outro = CardML(ok=True, titulo="Armani My Way 50ml", preco=400, vendedor="Armani Beauty", seller_id=11)
        self.assertEqual(self.tirou(30, "e4", outro, "meli.la/p4"), ("esgotou", True))
        self.assertEqual(quentes(self.con, AGORA), {})

    def test_se_ele_manteve_em_um_post_no_meio_nao_sao_seguidos(self):
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        registrar_sinal(self.con, "MODAHOJE", AGORA - 45, "ativo", "breno", "você manteve", "e4", "7|lipx", "meli.la/p4")
        outra = CardML(ok=True, titulo="Boss Bottled 100ml", preco=400, vendedor="Mercado Livre", seller_id=99)
        self.assertEqual(self.tirou(30, "e5", outra, "meli.la/p5")[0], "fora")
        self.assertIn("MODAHOJE", quentes(self.con, AGORA))

    def test_edicao_com_o_cupom_que_ainda_espera_os_3_min_tambem_conta(self):
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        outra = CardML(ok=True, titulo="Boss Bottled 100ml", preco=400, vendedor="Mercado Livre", seller_id=99)
        self.assertEqual(self.tirou(30, "e5", outra, "meli.la/p5", confirmado_ts=AGORA - 45)[0], "fora")

    def test_reeditar_o_mesmo_post_nao_conta_como_segundo(self):
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        self.assertEqual(self.tirou(30, "e3b", self.armani, "meli.la/p3")[0], "fora")
        self.assertEqual(self.tirou(30, "e3b", self.armani, "meli.la/p3"), ("fora", False))   # relida: não repete
        self.assertIn("MODAHOJE", quentes(self.con, AGORA))

    def test_cupom_que_nao_e_o_da_equipe_continua_esgotando_na_hora(self):
        from curadora.esgotados import tirou_do_post
        self.assertEqual(tirou_do_post(self.con, "OUTROCUPOM", AGORA - 60, "Perfume", "e9", "11|armanibeauty",
                                       "Armani Beauty", "meli.la/p9")[0], "esgotou")
        self.assertIn("OUTROCUPOM", esgotados(self.con, AGORA))

    def test_se_depois_ele_mantem_num_post_da_loja_ela_volta(self):
        self.tirou(60, "e3", self.armani, "meli.la/p3")
        registrar_sinal(self.con, "MODAHOJE", AGORA - 20, "ativo", "breno", "você manteve", "e4", "11|armanibeauty",
                        "meli.la/p4")
        self.assertEqual(self.cur.cupom_da_equipe(500, AGORA, self.armani)["preco"], 460)   # 15% até R$40


if __name__ == "__main__":
    unittest.main()
