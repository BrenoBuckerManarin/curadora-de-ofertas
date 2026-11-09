"""Regressões de segurança e medição encontradas na revisão de 30/10/2026."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apoio import conectar, CFG, T0, DIA, MLFalso
import ficha_relatorio as ficha
import pipeline
import review_rodada as review
from curadora.alertas import SaidaWhatsApp
from curadora.grupo import PROIBIDOS
from curadora.historico import historico_de_preco, tendencia
from curadora import esgotados
from curadora.ficha_cupom import Rodada
from curadora.score import Curadora, para_dict
from curadora.ml import CardML


class TestTravas(unittest.TestCase):
    def test_conflito_nunca_passa_nem_no_fundo(self):
        for fundo in (False, True):
            for score in (20, 80):
                av = SimpleNamespace(conflito="volume 100ml versus 50ml", nota="BOA", score=score)
                with patch.object(pipeline, "_no_fundo", fundo):
                    self.assertFalse(pipeline.aceitavel(av))
                self.assertIn("outro produto", pipeline.ressalva_da_curadora(av))

    def test_conferir_recusa_conflito_antes_de_tentar_cupom(self):
        av = SimpleNamespace(card=SimpleNamespace(ok=True, internacional=False, titulo="Perfume Asad 50ml"),
                             conflito="volume", identidade=SimpleNamespace(chave="asad|100"), nota="NÃO VALE",
                             score=20)
        cur = SimpleNamespace(avaliar=lambda *a, **k: av)
        card = {"id": 1, "preco": 100, "nota": "NÃO VALE", "texto_quente": "Asad 100ml https://meli.la/x"}
        with patch.object(pipeline, "para_dict", return_value={}), \
             patch.object(pipeline, "registrar_avaliacao"), \
             patch.object(pipeline, "vendedor_duvidoso", return_value=""):
            self.assertIsNone(pipeline.conferir(None, cur, card))
        self.assertIn("outro produto", pipeline.conferir.motivo)

    def test_todos_os_caminhos_bloqueiam_destinos_proibidos(self):
        for grupo in PROIBIDOS:
            for sufixo in ("", "@g.us"):
                pedidos = []
                saida = SaidaWhatsApp(grupo + sufixo, http=lambda *a: pedidos.append(a))
                saida.enviar_texto("relatório ou vigilante")
                saida("alvo", {"forcar": True})
                saida("promo", {"nota": "IMPERDÍVEL"})
                saida("promo", {"nota": "IMPERDÍVEL", "promo": "p", "avaliacao": "a"})
                self.assertEqual(pedidos, [])
                self.assertEqual(saida.enviados, 0)

    def test_destino_permitido_continua_funcionando(self):
        pedidos = []
        saida = SaidaWhatsApp("123456", http=lambda *a: pedidos.append(a) or {"success": True})
        saida.enviar_texto("relatório")
        saida("alvo", {"forcar": True})
        self.assertEqual(len(pedidos), 2)


class TestReview(unittest.TestCase):
    def test_troca_de_codigo_e_detectada(self):
        ciclo = {"before": {"copy": "Por R$100,00\nCupom AAA10"},
                 "after": {"copy": "Por R$100,00\nCupom BBB10"}}
        self.assertIn("cupom", review.correcoes(ciclo))
        ciclo["after"]["copy"] = "Por R$100,00\nCupom: *AAA10*"
        self.assertNotIn("cupom", review.correcoes(ciclo))

    def test_sem_ciclo_nao_e_acerto_confirmado(self):
        envios = [{"ts": 1000, "link_afiliado": "https://meli.la/a", "chave": "asad|100"}]
        itens = review.juntar(envios, [], [], [(1100, "https://meli.la/a", "asad|100")])
        self.assertEqual(itens[0]["situacao"], "postou sem edição identificada")

    def test_leitor_nao_descarta_primeira_linha_de_arquivo_pequeno(self):
        with tempfile.TemporaryDirectory() as pasta:
            p = Path(pasta) / "log.jsonl"
            p.write_text('{"ts": 1000}\n{"ts": 1001}\n', encoding="utf-8")
            self.assertEqual(len(review.ler_jsonl(str(p), 0, "ts", 60000000)), 2)


class TestJanelaDaFicha(unittest.TestCase):
    def setUp(self):
        self.con = conectar(":memory:")

    def sinal(self, ts, tipo):
        self.con.execute("INSERT INTO cupom_sinais(codigo, ts, tipo, fonte, ref, post) VALUES (?,?,?,?,?,?)",
                         ("TESTE10", ts, tipo, "breno", str(ts), "meli.la/a"))

    def test_post_antigo_e_post_apos_janela_nao_contam(self):
        for n, ts in enumerate((900, 1000 + ficha.JANELA_DECISAO_S)):
            self.con.execute("INSERT INTO postagens(msg_id, ts, link) VALUES (?,?,?)", (str(n), ts, "https://meli.la/a"))
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000))
        self.con.execute("INSERT INTO postagens(msg_id, ts, link) VALUES ('ok', 1100, 'https://meli.la/a')")
        self.assertTrue(ficha.postado(self.con, "meli.la/a", 1000))
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, 1100))

    def test_decisao_antiga_ou_de_outro_envio_nao_conta(self):
        self.sinal(950, "ativo")
        self.sinal(1200, "fora")
        self.assertEqual(ficha.decisao_do_breno(self.con, "TESTE10", "meli.la/a", 1000, 1100), "")
        self.assertEqual(ficha.decisao_do_breno(self.con, "TESTE10", "meli.la/a", 1100), "tirou")

    def test_sem_dados_nao_afirma_que_grupofinal_foi_sem_cupom(self):
        self.con.execute("INSERT INTO postagens(msg_id,ts,link,dados) VALUES ('h',1100,'https://meli.la/a','{}')")
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10"))
        self.con.execute('UPDATE postagens SET dados=?', (json.dumps({'cupons':['TESTE10']}),))
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10"))
        self.con.execute('UPDATE postagens SET dados=?', (json.dumps({'cupons':[]}),))
        self.assertTrue(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10"))

    def test_envios_do_mesmo_link_nao_duplicam_decisao(self):
        self.sinal(1300, "fora")
        envios = [{"tipo": "envio", "ts": ts, "post": "meli.la/a", "ficha": {
            "TESTE10": {"atual": "com", "decisao": "com", "faixa": 1}}} for ts in (1000, 1200)]
        texto = ficha.montar(envios, self.con, ficha.dia_de(1000))
        self.assertIn("2 envios, 1 com decisão sua", texto)
        self.assertIn("1 sem decisão", texto)

    def test_sonda_nao_herda_retirada_anterior_ao_envio(self):
        self.sinal(950, "fora")
        rodada = Rodada()
        self.assertIsNone(rodada._resultado(self.con, "TESTE10", "meli.la/a", 1300, 1000))
        self.sinal(1200, "fora")
        self.assertEqual(rodada._resultado(self.con, "TESTE10", "meli.la/a", 1300, 1000), (1200, True))


class TestPrecosECupons(unittest.TestCase):
    def setUp(self):
        self.con = conectar(":memory:")
        self.n = 0

    def preco(self, valor, ts, cupom=False):
        self.n += 1
        self.con.execute("INSERT INTO ofertas(msg_id,ts,chave,preco,cupons,produto,base) VALUES (?,?,?,?,?,'Asad','asad')",
                         (str(self.n), ts, "asad|100", valor, '["TESTE10"]' if cupom else '[]'))

    def test_media_e_ultimo_ignoram_leitura_absurda(self):
        for d, p in enumerate((200, 200, 200, 200, 0.5)):
            self.preco(p, T0 + d * DIA)
        h = historico_de_preco(self.con, "asad|100", T0+6*DIA, 60)
        self.assertEqual((h.mediana, h.media, h.ultimo, h.minimo), (200, 200, 200, 200))

    def test_tendencia_nao_pesa_reposts_como_novas_observacoes(self):
        for d in (0, 1, 2, 3, 4, 15, 16, 17):
            self.preco(200, T0+d*DIA)
        for n in range(100):
            self.preco(150, T0+17*DIA+n)
        t = tendencia(self.con, "asad|100", T0+18*DIA, 200, "sem_cupom")
        self.assertEqual(t["direcao"], "estavel")

    def test_tendencia_sem_cupom_nao_usa_minimo_com_cupom(self):
        for d in range(18):
            self.preco(200, T0+d*DIA)
            self.preco(150, T0+d*DIA, True)
        self.assertNotIn("ja_esteve", tendencia(self.con, "asad|100", T0+18*DIA, 200, "sem_cupom"))

    def test_frescor_nao_usa_futuro_mas_recupera_oferta_anterior(self):
        self.con.execute("INSERT INTO cupons(codigo,primeiro_ts,ultimo_ts) VALUES ('TESTE10',?,?)", (T0, T0+3600))
        self.assertFalse(esgotados.fresco(self.con, "TESTE10", T0+600))
        self.preco(180, T0+300, True)
        self.assertTrue(esgotados.fresco(self.con, "TESTE10", T0+600))

    def test_preco_recalculado_com_cupom_usa_historico_com_cupom(self):
        agora = T0+18*DIA
        for d in range(10):
            self.preco(200, T0+d*DIA)
            self.preco(180, T0+d*DIA, True)
        self.con.execute("INSERT INTO cupons(codigo,primeiro_ts,ultimo_ts,pct) VALUES ('TESTE10',?,?,10)",
                         (agora-300, agora-300))
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, loja_oficial=True, vendedor="Lipx")
        cur = Curadora({**CFG, "cupom_quente": {"ligado": False}}, self.con, MLFalso({"https://meli.la/x":card}))
        av = cur.avaliar("Lattafa Asad 100ml\nPor R$150\nCupom TESTE10\nhttps://meli.la/x", agora=agora)
        self.assertEqual(av.preco_efetivo, 180)
        self.assertEqual(av.historico.tipo, "com_cupom")
        self.assertEqual(para_dict(av)["historico"]["media"], 180)


class TestAjustesDaRevisao3110(unittest.TestCase):
    """31/10: o que os validadores acharam no pacote da Astra."""

    def setUp(self):
        self.con = conectar(":memory:")
        self.n = 0

    def preco(self, valor, ts):
        self.n += 1
        self.con.execute("INSERT INTO ofertas(msg_id,ts,chave,preco,cupons,produto,base) VALUES (?,?,?,?,'[]','Asad','asad')",
                         (str(self.n), ts, "asad|100", valor))

    def test_tendencia_nao_quebra_se_o_minimo_some_entre_as_leituras(self):
        for d in range(16):
            self.preco(200, T0 + d * DIA)
        self.preco(150, T0 + 16 * DIA)
        import curadora.historico as h
        real = h.precos_do_produto
        chamadas = []

        def falso(*a, **k):
            chamadas.append(1)
            linhas = real(*a, **k)
            return [r for r in linhas if r["preco"] != 150] if len(chamadas) == 4 else linhas

        with patch.object(h, "precos_do_produto", side_effect=falso):
            t = tendencia(self.con, "asad|100", T0 + 17 * DIA, 200, "sem_cupom")
        self.assertEqual(len(chamadas), 4)
        self.assertNotIn("ja_esteve", t)
        self.assertIn("ja_esteve", tendencia(self.con, "asad|100", T0 + 17 * DIA, 200, "sem_cupom"))

    def grupofinal(self, ts, link, chave, cupons):
        self.con.execute("INSERT INTO postagens(msg_id, ts, chave, link, dados) VALUES (?,?,?,?,?)",
                         (f"h{ts}", ts, chave, link, json.dumps({"cupons": cupons})))

    def test_grupofinal_com_outro_link_e_achado_pela_chave(self):
        # desde 20/10 o GrupoFinal sai com outro meli.la: o link do envio não aparece lá
        self.grupofinal(1000 + 1800, "https://meli.la/outro", "asad|100", [])
        self.assertTrue(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10", chave="asad|100"))
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10"))
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10", chave="kaiak|100"))

    def test_grupofinal_logo_depois_do_envio_e_da_equipe(self):
        self.grupofinal(1000 + 30, "https://meli.la/outro", "asad|100", [])
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10", chave="asad|100"))

    def test_grupofinal_pela_chave_com_o_cupom_nao_conta(self):
        self.grupofinal(1000 + 1800, "https://meli.la/outro", "asad|100", ["TESTE10"])
        self.assertFalse(ficha.postado(self.con, "meli.la/a", 1000, sem_cupom="TESTE10", chave="asad|100"))

    def test_relatorio_conta_poria_com_grupofinal_de_outro_link(self):
        self.grupofinal(1000 + 1800, "https://meli.la/outro", "asad|100", [])
        envio = {"tipo": "envio", "ts": 1000, "post": "meli.la/a", "chave": "asad|100", "produto": "Asad",
                 "ficha": {"TESTE10": {"atual": "sem", "decisao": "com", "faixa": 2, "classe": "revenda",
                                       "motivo": "classe aberta"}}}
        texto = ficha.montar([envio], self.con, ficha.dia_de(1000))
        # 31/10 (revisão colada): achado só pela chave é PROVÁVEL, e aparece separado do confirmado (mesmo link)
        self.assertIn("0 confirmados (mesmo link) + 1 prováveis (mesmo produto, outro link)", texto)
        self.assertIn("(provável) TESTE10", texto)
        self.assertEqual(ficha.postado(self.con, "meli.la/a", 1000, chave="asad|100"), "chave")
        self.con.execute("INSERT INTO postagens(msg_id, ts, chave, link, dados) VALUES ('l1', 1100, 'asad|100', "
                         "'https://meli.la/a', '{\"cupons\": []}')")
        self.assertEqual(ficha.postado(self.con, "meli.la/a", 1000, chave="asad|100"), "link")


if __name__ == "__main__":
    unittest.main()
