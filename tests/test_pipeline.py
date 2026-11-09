"""Pipeline: o texto que vai para o Gerador (cupom que sai leva o preço para o do ML)."""
from __future__ import annotations

import unittest

import apoio  # noqa: F401

import pipeline

ZAAD = ("ZAAD INFINITY DE 335 POR 211 🥷\n\n➡️ *Zaad Edp Infinity 95ml*\n\n✅ De R$ 335 por R$ 211\n"
        "🏷️ Cupom: TODOSITE0910\n\n🛒 Compre aqui: https://meli.la/TqV0zAq")


class TestLinkDoAnuncio(unittest.TestCase):
    """19/10: a página de catálogo troca de loja a cada sessão; o Gerador prende a loja quando recebe o anúncio."""

    def card(self, **kw):
        from curadora.ml import CardML
        return CardML(**dict(dict(ok=True, titulo="Lattafa Al Noble Wazeer", preco=162, item_id="MLB7153825372"), **kw))

    def test_troca_o_link_do_grupo_pelo_do_anuncio(self):
        texto = ("*Lattafa Al Noble Wazeer EDP 100ml*\nPor *R$ 162*\nResgate o cupom: https://meli.la/BteXCJy\n"
                 "https://meli.la/BteXCJy")
        novo = pipeline.com_link_do_anuncio(texto, self.card())
        self.assertNotIn("meli.la", novo)
        self.assertEqual(novo.count("https://produto.mercadolivre.com.br/MLB-7153825372"), 2)
        self.assertTrue(novo.startswith("*Lattafa Al Noble Wazeer EDP 100ml*\nPor *R$ 162*"))
        pagina = "*Wazeer*\nPor R$ 150\nhttps://www.mercadolivre.com.br/lattafa-al-noble-wazeer/p/MLB22788717"
        self.assertEqual(pipeline.com_link_do_anuncio(pagina, self.card()),
                         "*Wazeer*\nPor R$ 150\nhttps://produto.mercadolivre.com.br/MLB-7153825372")

    def test_sem_anuncio_conferido_fica_o_link_do_grupo(self):
        texto = "*Asad*\nPor R$ 150\nhttps://meli.la/abc"
        self.assertEqual(pipeline.com_link_do_anuncio(texto, self.card(ok=False)), texto)
        self.assertEqual(pipeline.com_link_do_anuncio(texto, self.card(item_id="")), texto)
        self.assertEqual(pipeline.com_link_do_anuncio(texto, None), texto)


class TestReviewNoFimDaRodada(unittest.TestCase):
    def test_grava_o_review_do_dia_e_nao_derruba_o_pipeline(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as pasta:
            feito = subprocess.CompletedProcess([], 0, stdout="REVIEW DA RODADA · teste\n", stderr="")
            with mock.patch.object(pipeline, "REVIEWS", Path(pasta)), \
                    mock.patch.object(pipeline.subprocess, "run", return_value=feito), mock.patch.object(pipeline, "log"):
                pipeline.guardar_review()
            arquivos = list(Path(pasta).glob("*.txt"))
            self.assertEqual(len(arquivos), 1)
            self.assertIn("REVIEW DA RODADA", arquivos[0].read_text(encoding="utf-8"))
            with mock.patch.object(pipeline, "REVIEWS", Path(pasta)), \
                    mock.patch.object(pipeline.subprocess, "run", side_effect=OSError("sem python")), \
                    mock.patch.object(pipeline, "log") as log:
                pipeline.guardar_review()   # não levanta
            self.assertIn("não saiu", log.call_args[0][0])


class TestTextoParaOGerador(unittest.TestCase):
    def test_cupom_que_sai_leva_o_preco_para_o_do_ml(self):
        # 09/10: o Breno tirou o TODOSITE0910 e postou a R$235, o preço do ML.
        texto = pipeline.texto_para_o_gerador(ZAAD, {"TODOSITE0910"}, 235.92, 211.0)
        self.assertNotIn("TODOSITE0910", texto)
        self.assertIn("De R$ 335 por R$ 235,92", texto)
        self.assertIn("POR 236", texto.splitlines()[0])
        self.assertIn("https://meli.la/TqV0zAq", texto)

    def test_cupom_quente_fica_e_o_preco_vira_o_de_agora_com_ele(self):
        # 10/10: o grupo postou a R$211 horas atrás; agora o ML está a R$236 e com o cupom quente sai R$200,53.
        texto = pipeline.texto_para_o_gerador(ZAAD, set(), 200.53, 211.0)
        self.assertIn("Cupom: TODOSITE0910", texto)
        self.assertIn("por R$ 200,53", texto)
        self.assertIn("POR 201", texto.splitlines()[0])

    def test_cupom_da_equipe_entra_antes_do_link_e_o_outro_sai(self):
        # 10/10: o Breno tirou o cupom que veio e pôs o MODA1010 à mão, com o preço refeito.
        texto = pipeline.texto_para_o_gerador(ZAAD, {"TODOSITE0910"}, 195.92, 211.0, por="MODA1010")
        linhas = texto.splitlines()
        self.assertNotIn("TODOSITE0910", texto)
        self.assertIn("por R$ 195,92", texto)
        self.assertEqual(texto.count("MODA1010"), 1)
        self.assertLess(linhas.index("🎟️ Cupom: MODA1010"), next(i for i, l in enumerate(linhas) if "https://" in l))

    def test_preco_com_dois_pontos_tambem_e_trocado(self):
        texto = pipeline.texto_para_o_gerador("*Asad 100ml*\nDe: ~R$ 230,00~ 🔥 *Por: R$ 97,92*\n🔗 https://meli.la/x",
                                              set(), 88.5)
        self.assertIn("*Por: R$ 88,50*", texto)

    def test_preco_com_negrito_depois_do_rs_tambem_e_trocado(self):
        # 21/10 (Breno: "por que essas 2 foram sem o cupom?"): o K Max veio com "Por: R$*168,00*"
        from curadora.parser import ler_promo
        texto = pipeline.texto_para_o_gerador("*Natura K Max 100ml* \n\nDe:R$~199,00~\nPor: R$*168,00* ‼️\n\n"
                                              "🔗https://meli.la/7pwsA8F", set(), 159.6, 168.0, por="CAMISA10")
        self.assertIn("Por: *R$ 159,60* ‼️", texto)
        self.assertEqual(ler_promo(texto).preco, 159.6)

    def test_preco_sem_por_antes_tambem_e_trocado(self):
        # 21/10: o Qaed Al Fursan Untamed veio com "✅ R$ 162" e o título "POR 162 REAIS"
        from curadora.parser import ler_promo
        texto = pipeline.texto_para_o_gerador(
            "PERFUME LATTAFA POR 162 REAIS 🥷\n\n➡️ **Perfume Qead Al Fursan Untamed Lattafa 100 ml Masculino**\n\n"
            "✅ R$ 162\n🏷️ Cupom: `ESQFASHBEAUTY2110`\n\n🛒 Compre aqui: https://meli.la/bVg4k0W",
            {"ESQFASHBEAUTY2110"}, 171.11, 162.0, por="CAMISA10")
        self.assertIn("✅ R$ 171,11", texto)
        self.assertEqual(ler_promo(texto).preco, 171.11)

    def test_promo_que_ja_ia_vai_com_o_cupom_mesmo_se_a_nota_com_ele_cair(self):
        # 21/10 (Breno: "não tá mandando os cupons em vários anúncios"): Bareeq Excelente sem o cupom, Não vale 42 com
        from types import SimpleNamespace
        from unittest import mock
        av = SimpleNamespace(card=SimpleNamespace(ok=True, preco=126.64), nota="IMPERDÍVEL", score=86,
                             promo=SimpleNamespace(cupons=[], preco=126.64))
        av2 = SimpleNamespace(cupom_quente={"codigo": "CAMISA10"}, promo=SimpleNamespace(preco=120.31),
                              nota="NÃO VALE", score=42)
        cur = SimpleNamespace(
            cupom_da_equipe=lambda ml, card=None: {"codigo": "CAMISA10", "preco": 120.31, "por": "você manteve",
                                                   "hora": "21:10"},
            avaliar=lambda texto, **k: av2)
        with mock.patch.object(pipeline, "log"):
            r = pipeline.com_cupom_da_equipe(cur, av, "*Bareeq 100ml*\nPor R$ 126,64\nhttps://meli.la/x")
        self.assertIs(r[0], av2)
        self.assertEqual((av2.nota, av2.score), ("IMPERDÍVEL", 86))
        # a que só iria com o cupom continua precisando ficar boa com ele
        av.nota, av.score = "NÃO VALE", 30
        av2.nota, av2.score = "NÃO VALE", 42
        with mock.patch.object(pipeline, "log"):
            self.assertIsNone(pipeline.com_cupom_da_equipe(cur, av, "*Bareeq 100ml*\nPor R$ 126,64\nhttps://meli.la/x"))

    def test_cupom_da_equipe_reavalia_com_o_mesmo_anuncio(self):
        # 21/10: o Durrat Al Aroos foi sem o CAMISA10 porque a reavaliação reabria a página de catálogo
        from types import SimpleNamespace
        from unittest import mock
        card = SimpleNamespace(ok=True, preco=126.35)
        av = SimpleNamespace(card=card, nota="BOA", score=60, promo=SimpleNamespace(cupons=[], preco=126.35))
        av2 = SimpleNamespace(cupom_quente={"codigo": "CAMISA10"}, promo=SimpleNamespace(preco=120.03), nota="BOA",
                              score=62)
        chamadas = []
        cur = SimpleNamespace(
            cupom_da_equipe=lambda ml, card=None: {"codigo": "CAMISA10", "preco": 120.03, "por": "você manteve",
                                                   "hora": "20:49"},
            avaliar=lambda texto, **k: (chamadas.append(k), av2)[1])
        with mock.patch.object(pipeline, "log"):
            r = pipeline.com_cupom_da_equipe(cur, av, "*Durrat 85ml*\nPor R$ 126,35\nhttps://meli.la/x")
        self.assertIs(r[0], av2)
        self.assertIs(chamadas[0].get("card"), card)
        # quando não entra, o log diz por quê
        av2.cupom_quente = None
        with mock.patch.object(pipeline, "log") as log:
            self.assertIsNone(pipeline.com_cupom_da_equipe(cur, av, "*Durrat 85ml*\nPor R$ 126,35\nhttps://meli.la/x"))
        self.assertIn("não valeu no texto", log.call_args[0][0])

    def test_sem_cupom_para_tirar_nada_muda(self):
        self.assertEqual(pipeline.texto_para_o_gerador(ZAAD, set(), None), ZAAD.strip())

    def test_cupom_suspenso_sai_do_texto_junto_com_o_desconto_dele(self):
        # 03/11 (Breno: "retirar o cupom não deixe o preço descontado sem sua condição"): no texto que a curadora manda,
        # o cupom suspenso sai e o preço vira o do ML.
        import time
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.esgotados import registrar_sinal
        from curadora.ml import CardML
        from curadora.score import Curadora
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True)
        cur = Curadora(CFG, banco_com_historico([("Lattafa Asad 100ml", 200, d) for d in range(8)]),
                       MLFalso({"https://meli.la/a": card}))
        agora = int(time.time())
        cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, limite, perfume_ts) "
                        "VALUES ('MODA10BELEZA', ?, ?, 10, 30, ?)", (agora, agora, agora))
        texto = "*Lattafa Asad 100ml*\nPor R$ 180\n🎟️ Cupom: MODA10BELEZA\nhttps://meli.la/a"
        self.assertEqual(pipeline.cupons_que_ficam(cur, cur.avaliar(texto, agora=agora)), ["MODA10BELEZA"])
        registrar_sinal(cur.con, "MODA10BELEZA", agora - 600, "esgotou", "breno", "você tirou do post do X", "e1")
        av = cur.avaliar(texto, agora=agora)
        ficam = pipeline.cupons_que_ficam(cur, av)
        tirar = set(av.promo.cupons) - set(ficam)
        novo = pipeline.texto_para_o_gerador(texto, tirar, pipeline.preco_do_texto(cur, av, ficam, tirar), av.promo.preco)
        self.assertEqual(novo, "*Lattafa Asad 100ml*\nPor R$ 200,00\nhttps://meli.la/a")


class TestPrecosDoEnvio(unittest.TestCase):
    """03/11 (relatório da Astra): cada envio guarda o preço do card, o "outros meios" e a regra do cupom usada, para
    conferir a ordem cupom x Pix com o mesmo momento (The Kingdom na Lipx: R$163,29 no Pix e R$192,11 nos outros)."""

    def test_guarda_os_dois_precos_e_a_regra_do_cupom(self):
        from types import SimpleNamespace
        from curadora.banco import conectar
        con = conectar(":memory:")
        con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite) "
                    "VALUES ('SITE031115', 1, 1, 15, 89, 20)")
        con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, valor, minimo) VALUES ('SITE200111', 1, 1, 20, 299)")
        card = SimpleNamespace(ok=True, preco=163.29, preco_outros=192.11, preco_cupom=None)
        av = SimpleNamespace(card=card, cupom_quente={"codigo": "SITE031115", "preco": 143.29},
                             promo=SimpleNamespace(cupons=["SITE031115", "SITE200111", "VELHO0110"]),
                             cupons_mortos={"VELHO0110": "vencido"})
        self.assertEqual(pipeline.precos_do_envio(SimpleNamespace(con=con), av), {
            "preco_card": 163.29, "preco_outros_meios": 192.11,
            "regras_cupom": [
                {"codigo": "SITE031115", "origem": "quente", "pct": 15.0, "valor": None, "minimo": 89.0, "limite": 20.0},
                {"codigo": "SITE200111", "origem": "oferta", "pct": None, "valor": 20.0, "minimo": 299.0, "limite": None},
                {"codigo": "VELHO0110", "origem": "oferta", "tirado": "vencido"}]})
        sem_cupom = SimpleNamespace(card=SimpleNamespace(ok=True, preco=99.9, preco_outros=None, preco_cupom=89.9),
                                    cupom_quente=None, promo=SimpleNamespace(cupons=[]), cupons_mortos={})
        self.assertEqual(pipeline.precos_do_envio(SimpleNamespace(con=con), sem_cupom),
                         {"preco_card": 99.9, "preco_cupom_ml": 89.9})
        self.assertEqual(pipeline.precos_do_envio(SimpleNamespace(con=con), SimpleNamespace(card=None, cupom_quente=None)),
                         {})

    def test_a_avaliacao_guardada_leva_o_outros_meios_do_card(self):
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.ml import CardML
        from curadora.score import Curadora, para_dict
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=177.9, preco_outros=209.3, vendedor="KIDS LIFE",
                      loja_oficial=True)
        cur = Curadora(CFG, banco_com_historico([("Lattafa Asad 100ml", 200, d) for d in range(8)]),
                       MLFalso({"https://meli.la/a": card}))
        av = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 177,90\nhttps://meli.la/a", agora=None)
        self.assertEqual((para_dict(av)["ml"]["preco"], para_dict(av)["ml"]["preco_outros"]), (177.9, 209.3))


class TestRepostarMaisBarato(unittest.TestCase):
    """10/10 à noite (Breno: "sim"): produto postado há menos de 8h só volta se estiver 8% mais barato."""

    def setUp(self):
        import time
        from curadora.banco import conectar
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                         ("p1", self.agora - 3600, "Grupo da Equipe", "Bareeq Al Dhahab 100ml", "bareeq-al-dhahab|100", 122.32))
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                         ("p0", self.agora - 9 * 3600, "Grupo da Equipe", "Asad 100ml", "asad|100", 150.0))

    def test_so_volta_antes_das_8h_se_cair_8_por_cento(self):
        from curadora.postagens import ainda_vale_o_post, ultimos_posts
        posts = ultimos_posts(self.con, self.agora)
        self.assertEqual(posts["bareeq-al-dhahab|100"][0], 122.32)
        self.assertNotIn("asad|100", posts)                                   # postado há 9h: já pode repetir
        self.assertTrue(ainda_vale_o_post(posts["bareeq-al-dhahab|100"], 118.41))   # 3% mais barato: segura
        self.assertFalse(ainda_vale_o_post(posts["bareeq-al-dhahab|100"], 105.0))   # 14% mais barato: pode ir
        self.assertFalse(ainda_vale_o_post(None, 105.0))

    def test_post_da_equipe_volta_depois_de_6h_e_o_do_pipeline_segue_8h(self):
        # 21/10 (Breno: "sim pros 2"): a equipe posta de dia; o público da noite é outro.
        import json, tempfile
        from pathlib import Path
        from curadora.postagens import ultimos_posts
        for msg, chave, horas in (("e1", "kaiak|100", 7), ("e2", "club-intense-nuit|105", 5), ("p2", "yara|100", 7)):
            self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                             (msg, self.agora - horas * 3600, "Grupo da Equipe", chave, chave, 100.0))
        with tempfile.TemporaryDirectory() as pasta:
            registro = Path(pasta) / "pipeline.jsonl"
            registro.write_text(json.dumps({"ts": self.agora - 7 * 3600 - 1800, "status": "enviado", "chave": "yara|100",
                                            "preco": 100.0}), encoding="utf-8")
            posts = ultimos_posts(self.con, self.agora, registro)
        self.assertNotIn("kaiak|100", posts)               # equipe, há 7h: já pode voltar
        self.assertIn("club-intense-nuit|105", posts)      # equipe, há 5h: ainda não
        self.assertIn("yara|100", posts)                   # veio do pipeline, há 7h: segue a regra das 8h

    def test_o_que_o_pipeline_mandou_tambem_conta(self):
        import json, tempfile
        from pathlib import Path
        from curadora.postagens import ultimos_posts
        with tempfile.TemporaryDirectory() as pasta:
            registro = Path(pasta) / "pipeline.jsonl"
            registro.write_text("\n".join(json.dumps(r) for r in (
                {"ts": self.agora - 600, "status": "enviado", "chave": "eros-flame|200", "preco": 563.64},
                {"ts": self.agora - 600, "status": "simulado", "chave": "eros|200", "preco": 400.0})), encoding="utf-8")
            posts = ultimos_posts(self.con, self.agora, registro)
        self.assertEqual(posts["eros-flame|200"][0], 563.64)
        self.assertNotIn("eros|200", posts)

    def test_painel_mostra_como_postado_o_que_o_pipeline_nao_mandaria(self):
        import json
        import painel
        def avaliacao(chave, preco):
            self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                             (self.agora - 60, "ao vivo", "g", chave, "IMPERDÍVEL", 94, json.dumps({"preco": preco})))
        avaliacao("bareeq-al-dhahab|100", 118.41)      # 3% abaixo do post de 1h atrás
        avaliacao("yara|100", 99.0)                    # nunca postado
        self.con.commit()
        cards = {c["chave"]: c for c in painel._cards(self.con)}
        self.assertEqual(cards["bareeq-al-dhahab|100"]["postada"], self.agora - 3600)
        self.assertEqual(cards["yara|100"]["postada"], 0)


class TestFotoDoAnuncio(unittest.TestCase):
    """10/10 à noite (Breno: "sim"): quando a normalizadora não tem foto do produto, vai a do anúncio do ML."""

    def test_pede_a_foto_grande_em_jpg(self):
        self.assertEqual(pipeline.url_grande("https://http2.mlstatic.com/D_Q_NP_608282-MLA91146351737_082025-O.webp"),
                         "https://http2.mlstatic.com/D_NQ_NP_2X_608282-MLA91146351737_082025-F.jpg")

    def test_a_promo_vai_para_a_caixa_com_o_caminho_da_foto(self):
        import json, tempfile
        from pathlib import Path
        caixa = pipeline.CAIXA
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.CAIXA = Path(pasta) / "caixa.jsonl"
            try:
                pipeline.para_a_normalizadora("*Asad*\nhttps://meli.la/a", 1, "/tmp/foto.jpg")
                pipeline.para_a_normalizadora("*Yara*\nhttps://meli.la/b", 2)
                linhas = [json.loads(l) for l in pipeline.CAIXA.read_text(encoding="utf-8").splitlines()]
            finally:
                pipeline.CAIXA = caixa
        self.assertEqual(linhas[0]["mediaPath"], "/tmp/foto.jpg")
        self.assertNotIn("mediaPath", linhas[1])

    def test_endereco_que_nao_e_do_ml_nao_e_baixado(self):
        self.assertEqual(pipeline.baixar_foto("", "x"), "")
        self.assertEqual(pipeline.baixar_foto("http://exemplo.com/a.jpg", "x"), "")


class TestEquipeNaAtiva(unittest.TestCase):
    """11/10 (Breno, 09:00: "chegou 3 e parou?"): no começo do turno ninguém postou no GrupoFinal ainda."""

    def test_edicao_recente_do_breno_conta_como_equipe_na_ativa(self):
        import json, tempfile, time
        from pathlib import Path
        from types import SimpleNamespace
        from curadora.banco import conectar
        con = conectar(":memory:")
        cur = SimpleNamespace(cfg={"conferente": {"grupo_edicao": "120363000000000001@g.us"}})
        tap = pipeline.TAP
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.TAP = Path(pasta) / "tap.jsonl"
            try:
                pipeline.TAP.write_text("", encoding="utf-8")
                self.assertFalse(pipeline.equipe_postando(con, cur))               # ninguém postou nem editou
                linhas = [{"chatId": "120363000000000001@g.us", "timestamp": int(time.time()) - 3600, "body": "velha"},
                          {"chatId": "outro@g.us", "timestamp": int(time.time()) - 60, "body": "outro grupo"}]
                pipeline.TAP.write_text("x\n" + "".join(json.dumps(l) + "\n" for l in linhas), encoding="utf-8")
                self.assertFalse(pipeline.equipe_postando(con, cur))               # edição de 1h atrás não conta
                with pipeline.TAP.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"chatId": "120363000000000001@g.us", "timestamp": int(time.time()) - 120, "body": "edição"}) + "\n")
                self.assertTrue(pipeline.equipe_postando(con, cur))                # editou há 2 min: está na ativa
            finally:
                pipeline.TAP = tap
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES ('p', ?, 'GrupoFinal', 'x', 'x|100', 10)",
                    (int(time.time()) - 60,))
        self.assertTrue(pipeline.equipe_postando(con))                             # post no GrupoFinal continua valendo


class TestQueridinhosNaRodadaDeCupom(unittest.TestCase):
    """11/10 (Breno: "devia ter dado uma priorizada naquela listinha dos perfumes mais vendidos")."""

    def setUp(self):
        import json, time
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.cur = Curadora({**CFG, "cupom_quente": {"ligado": True}}, self.con, None)

        def avaliacao(chave, ha_s, nota, score, **dados):
            self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                             (self.agora - ha_s, "ao vivo", "g", chave, nota, score, json.dumps({"preco": 200, **dados})))
        # queridinho que nenhum grupo postou hoje (card de 30h atrás, nota Média: fora da lista normal)
        avaliacao("teste-asad|100", 30 * 3600, "OK", 45, top_vendido=True)
        avaliacao("teste-kit|?", 600, "NÃO VALE", 40, categoria="kit_body_splash")
        avaliacao("teste-comum|100", 600, "BOA", 70)
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('MODAHOJE', ?, ?, 15, 89, 40, ?)", (self.agora, self.agora, self.agora))
        self.con.commit()

    def chaves(self, lista):
        return [c["chave"] for c in lista if c["chave"].startswith("teste-")]

    def test_sem_cupom_a_ordem_e_a_normal_e_nao_ha_prioridade(self):
        prioridade, resto = pipeline.candidatas(self.con, self.cur, set())
        self.assertEqual(prioridade, [])
        self.assertEqual(self.chaves(resto), ["teste-comum|100"])

    def test_com_cupom_da_equipe_os_queridinhos_vao_primeiro(self):
        from curadora.esgotados import registrar_sinal
        for n in (1, 2):   # o Breno manteve o cupom em 2 posts: cupom da equipe valendo
            registrar_sinal(self.con, "MODAHOJE", self.agora - 60 * n, "ativo", "breno", "você manteve", f"e{n}")
        self.con.commit()
        prioridade, resto = pipeline.candidatas(self.con, self.cur, set())
        self.assertEqual(self.chaves(prioridade), ["teste-asad|100", "teste-kit|?"])     # a lista dele e os kits
        self.assertEqual(self.chaves(resto), ["teste-comum|100"])
        # o que esta rodada já tentou não volta para a frente
        prioridade, _ = pipeline.candidatas(self.con, self.cur, {"teste-asad|100"})
        self.assertEqual(self.chaves(prioridade), ["teste-kit|?"])


class TestCardDoRadar(unittest.TestCase):
    """11/10: o Fakhar Platin e o The Kingdom (radar) eram pulados a manhã toda com "não abriu no ML"."""

    RADAR = "Perfume Arabe Fakhar Platin 100ml\nPor R$ 140.00\nhttps://www.mercadolivre.com.br/p/MLB52116223"

    def setUp(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from curadora.ml import CardML
        self.pasta = tempfile.TemporaryDirectory()
        self.antes = {n: getattr(pipeline, n) for n in ("LINKS_DO_CATALOGO", "TAP", "enviar_ao_gerador", "esperar_resposta",
                                                         "GERADOR_API")}
        self.completar = pipeline.painel._completar
        pipeline.LINKS_DO_CATALOGO = Path(self.pasta.name) / "links.json"
        pipeline.GERADOR_API = Path(self.pasta.name) / "sem_chave.json"   # na VPS a chave existe: aqui é o BotGerador
        pipeline.TAP = Path(self.pasta.name) / "tap.jsonl"
        pipeline.TAP.write_text("", encoding="utf-8")
        self.pedidos = []
        pipeline.enviar_ao_gerador = self.pedidos.append
        pipeline.esperar_resposta = lambda cur, produto, desde, prazo: {"body": "*Fakhar Platin*\n\nhttps://meli.la/LFTXzyG"}
        self.texto = self.RADAR
        pipeline.painel._completar = lambda con, card: {"texto": self.texto}
        self.pagina = CardML(ok=True, titulo="Perfume Lattafa Fakhar Platin Edp 100ml", preco=1128.8)
        self.abertos = []
        self.cur = SimpleNamespace(ml=SimpleNamespace(
            consultar=lambda link, agora=None: self.abertos.append(link) or self.pagina))
        self.card = {"id": 1, "chave": "fakhar-platin|100", "preco": 140.0, "nota": "BOA"}

    def tearDown(self):
        for nome, valor in self.antes.items():
            setattr(pipeline, nome, valor)
        pipeline.painel._completar = self.completar
        self.pasta.cleanup()

    def test_vale_o_preco_da_pagina_com_o_link_que_abre(self):
        novo = pipeline.pela_pagina(None, self.cur, self.card)
        self.assertEqual(novo["texto_quente"],
                         "Perfume Lattafa Fakhar Platin Edp 100ml\nPor R$ 1.128,80\nhttps://meli.la/LFTXzyG")
        self.assertEqual(novo["chave"], "fakhar-platin|100")
        self.assertEqual(self.abertos, ["https://meli.la/LFTXzyG"])     # abriu o link de afiliado, não o /p/MLB
        self.assertEqual(self.pedidos, [self.RADAR])

    def test_o_link_da_pagina_e_pedido_ao_gerador_uma_vez_so(self):
        pipeline.pela_pagina(None, self.cur, self.card)
        pipeline.pela_pagina(None, self.cur, self.card)
        self.assertEqual(len(self.pedidos), 1)
        self.assertEqual(len(self.abertos), 2)      # a página é aberta de novo: o preço é o de agora

    def test_promo_de_grupo_segue_como_estava(self):
        self.texto = "*Asad 100ml*\nPor R$ 150\nhttps://meli.la/TqV0zAq"
        self.assertIs(pipeline.pela_pagina(None, self.cur, self.card), self.card)
        self.assertEqual(self.pedidos, [])

    def test_na_simulacao_nao_fala_com_o_gerador(self):
        self.assertIsNone(pipeline.pela_pagina(None, self.cur, self.card, simular=True))
        self.assertEqual(self.pedidos, [])

    def test_pagina_que_nao_abre_nao_vai_e_o_link_e_pedido_de_novo_depois(self):
        self.pagina = type(self.pagina)(ok=False, erro="o ML pediu verificação de conta")
        self.assertIsNone(pipeline.pela_pagina(None, self.cur, self.card))
        self.assertEqual(pipeline.conferir.motivo, "não abriu no ML")
        pipeline.pela_pagina(None, self.cur, self.card)
        self.assertEqual(len(self.pedidos), 2)

    def test_gerador_sem_resposta_nao_vai_e_nao_insiste(self):
        # 14/10 (Breno: "ele tá insistindo no erro, esse link não vale mais"): o BotGerador só devolvia erro e o
        # pipeline pediu de novo 7 min depois. Agora a falha fica guardada por 6h.
        pipeline.esperar_resposta = lambda cur, produto, desde, prazo: None
        self.assertIsNone(pipeline.pela_pagina(None, self.cur, self.card))
        self.assertIsNone(pipeline.pela_pagina(None, self.cur, self.card))
        self.assertEqual(len(self.pedidos), 1)

    def test_com_a_api_o_link_da_pagina_vem_dela(self):
        antes = pipeline.gerar_pela_api
        pipeline.gerar_pela_api = lambda texto: {"body": texto, "link": "https://meli.la/RHec4w"}
        try:
            self.assertEqual(pipeline.link_da_pagina(self.cur, "MLB52116223", self.RADAR), "https://meli.la/RHec4w")
            self.assertEqual(pipeline.link_da_pagina(self.cur, "MLB52116223", self.RADAR), "https://meli.la/RHec4w")  # guardado
        finally:
            pipeline.gerar_pela_api = antes
        self.assertEqual(self.pedidos, [])

    def test_radar_com_risco_nem_pede_link_ao_gerador(self):
        # 14/10: Attracione Men a R$24,50 (58% abaixo, vendedor fraco) foi pedido ao Gerador na rodada de cupom
        pipeline.painel._completar = lambda con, card: {"texto": self.RADAR, "risco": "alto", "pct_abaixo": 58}
        self.assertIsNone(pipeline.pela_pagina(None, self.cur, self.card))
        self.assertEqual(self.pedidos, [])


class TestMisturaDeFaixas(unittest.TestCase):
    """11/10 (Breno: "misture mais com produtos tipo Natura, vende bem, preço 150, 130")."""

    def test_depois_de_uma_cara_vem_duas_de_preco_melhor(self):
        # "se vem um perfume acima de 350 era pra vir 2 no preço melhor"
        for ultimos in ([157, 412], [412, 130]):      # a 1ª e a 2ª vaga depois da cara
            ordem = sorted([467, 250, 180, 130], key=lambda p: pipeline.vez(p, ultimos))
            self.assertEqual(ordem, [180, 130, 250, 467])
        self.assertEqual(pipeline.vez(467, [130, 157]), 0)     # passadas as duas, a cara volta a poder ir
        self.assertEqual(pipeline.vez(467, []), 0)
        self.assertEqual(pipeline.vez(340, [157, 340]), 0)     # R$340 não é cara: a régua é R$350

    def test_natura_e_boticario_tem_a_frente_nas_vagas_de_preco_melhor(self):
        self.assertTrue(pipeline.sai_mais("Natura Homem Essence 100ml"))
        self.assertTrue(pipeline.sai_mais("Malbec Gold O Boticário Masculino 100ml"))
        self.assertFalse(pipeline.sai_mais("Perfume Natural Spray 100ml"))
        self.assertLess(pipeline.vez(130, [412, 90], True), pipeline.vez(120, [412, 90], False))
        self.assertGreater(pipeline.vez(250, [412, 90], True), pipeline.vez(120, [412, 90], False))   # só até R$200
        self.assertEqual(pipeline.vez(130, [90, 120], True), pipeline.vez(120, [90, 120], False))      # fora das vagas, nota

    def test_a_cada_cinco_promos_entra_uma_natura_ou_boticario(self):
        # regra do Breno (11/10): "a cada 5 promos, se não vier um Boticário ou Natura, colocar 1"
        lista = [{"produto": "Rayhaan Imperia 100ml", "preco": 178}, {"produto": "Versace Eros 200ml", "preco": 480},
                 {"produto": "Malbec Gold O Boticário 100ml", "preco": 194}, {"produto": "Natura Kaiak 100ml", "preco": 89}]
        nome = lambda c: c["produto"]
        ordem = lambda ultimas: [c["produto"].split()[0] for c in pipeline.misturar(lista, nome, ultimas)]
        outras = lambda n: [{"produto": f"Perfume Árabe {i} 100ml", "preco": 150} for i in range(n)]
        natura = {"produto": "Natura Homem 100ml", "preco": 130}
        self.assertEqual(ordem(outras(4)), ["Malbec", "Natura", "Rayhaan", "Versace"])        # 4 sem nenhuma: é a vez
        self.assertEqual(ordem(outras(3)), ["Rayhaan", "Versace", "Malbec", "Natura"])        # ainda não: vale a nota
        self.assertEqual(ordem([natura] + outras(3)), ["Rayhaan", "Versace", "Malbec", "Natura"])
        self.assertEqual(ordem([natura] + outras(4)), ["Malbec", "Natura", "Rayhaan", "Versace"])
        self.assertEqual(ordem([]), ["Rayhaan", "Versace", "Malbec", "Natura"])               # começo do turno
        self.assertTrue(pipeline.vez_de_natura_ou_boticario(outras(4)))
        self.assertFalse(pipeline.vez_de_natura_ou_boticario(outras(3) + [natura]))
        # depois de uma cara: até R$200 primeiro, Natura/Boticário na frente, e a cara por último
        self.assertEqual(ordem([{"produto": "Polo Blue 125ml", "preco": 412}]), ["Malbec", "Natura", "Rayhaan", "Versace"])

    def test_16_10_a_cada_seis_promos_entra_um_kit_de_body_splash_ou_um_vodka(self):
        # Breno (16/10): "dá um boost nos kits Primacial, o povo gosta; nos Vodka"; depois: "pode ser a cada 6"
        lista = [{"produto": "Rayhaan Imperia 100ml", "preco": 178}, {"produto": "Lattafa Asad 100ml", "preco": 150},
                 {"produto": "Paris Elysees Vodka Limited Edt 100ML", "preco": 69},
                 {"produto": "Kit Body Splash Barbarius + Enigma + Midtown 200ml", "preco": 93}]
        nome = lambda c: c["produto"]
        ordem = lambda ultimas: [c["produto"].split()[0] for c in pipeline.misturar(lista, nome, ultimas)]
        outras = lambda n: [{"produto": f"Perfume Árabe {i} 100ml", "preco": 150} for i in range(n)]
        kit = {"produto": "Kit Primacial Body Splash Blunn + Vibration", "preco": 70}
        self.assertEqual(pipeline.IMPULSO_A_CADA, 6)
        self.assertGreaterEqual(pipeline.RECENTES_PARA_A_ORDEM, 5)                 # a ordem olha as 5 últimas
        self.assertEqual(ordem(outras(5)), ["Paris", "Kit", "Rayhaan", "Lattafa"])     # 5 sem nenhum: é a vez
        self.assertEqual(ordem(outras(4)), ["Rayhaan", "Lattafa", "Paris", "Kit"])     # ainda não: vale a nota
        self.assertEqual(ordem(outras(4) + [kit]), ["Rayhaan", "Lattafa", "Paris", "Kit"])
        self.assertEqual(ordem([kit] + outras(5)), ["Paris", "Kit", "Rayhaan", "Lattafa"])
        self.assertEqual(ordem([]), ["Rayhaan", "Lattafa", "Paris", "Kit"])             # começo do turno
        # com as duas regras vencidas (4 sem Natura/Boticário e 5 sem kit/Vodka), o kit/Vodka vai primeiro
        lista.append({"produto": "Natura Kaiak 100ml", "preco": 89})
        self.assertEqual(ordem(outras(5)), ["Paris", "Kit", "Natura", "Rayhaan", "Lattafa"])
        self.assertEqual(ordem(outras(4)), ["Natura", "Rayhaan", "Lattafa", "Paris", "Kit"])   # só a de Natura

    def test_16_10_quem_e_kit_de_body_splash_ou_vodka(self):
        for sim in ("Kit Body Splash Barbarius + Enigma + Midtown 200ml", "Kit Primacial Body Splash Barbarius + Enigma",
                    "Body Splash Masculino Barbarius E Midtown 200ml Primacial", "Kit 4 Body Splash Enigma, Midtown",
                    "Paris Elysees Vodka Limited Edt 100ML", "Perfume Vodka Extreme Paris Elysees Masculino",
                    "Body Splash Primacial Blunn 200ml", "Kit Body Splash Infalível + Enigma + Banker + Adventus"):
            self.assertTrue(pipeline.impulsionado(sim), sim)
        for nao in ("Natura Kaiak 100ml", "Lattafa Asad 100ml", "Body Splash Natura Tododia 200ml",
                    "Perfume Enigma Feminino 100ml", "Paris Elysees Sexy Woman 100ml"):
            self.assertFalse(pipeline.impulsionado(nao), nao)
        self.assertTrue(pipeline.vez_do_impulso([{"produto": "Perfume Árabe 100ml"}] * 5))
        self.assertFalse(pipeline.vez_do_impulso([{"produto": "Perfume Árabe 100ml"}] * 4))
        self.assertFalse(pipeline.vez_do_impulso([{"produto": "Perfume Árabe 100ml"}] * 4 + [{"produto": "Vodka Man"}]))

    def test_le_os_precos_das_ultimas_mandadas(self):
        import json, tempfile, time
        from pathlib import Path
        registro = pipeline.REGISTRO
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.REGISTRO = Path(pasta) / "pipeline.jsonl"
            agora = int(time.time())
            linhas = [{"ts": agora - 7200, "status": "enviado", "preco": 999},          # de 2h atrás: não conta
                      {"ts": agora - 600, "status": "enviado", "preco": 130.5},
                      {"ts": agora - 400, "status": "gerador_sem_resposta", "preco": 50},
                      {"ts": agora - 200, "status": "enviado", "preco": 412.24},
                      {"ts": agora - 100, "status": "enviado", "preco": 157.76}]
            pipeline.REGISTRO.write_text("\n".join(json.dumps(l) for l in linhas) + "\n", encoding="utf-8")
            try:
                self.assertEqual(pipeline.ultimos_precos(), [412.24, 157.76])
            finally:
                pipeline.REGISTRO = registro


class TestFotoDoGrupo(unittest.TestCase):
    """11/10: quando a normalizadora não tem foto do produto, vai junto a foto do post do grupo de origem."""

    def setUp(self):
        import json, struct, tempfile, time
        from pathlib import Path
        from curadora.banco import conectar
        self.pasta = tempfile.TemporaryDirectory()
        raiz = Path(self.pasta.name)
        self.tap_antes = pipeline.TAP
        pipeline.TAP = raiz / "tap.jsonl"
        self.con = conectar(":memory:")
        self.agora = int(time.time())

        def png(nome, largura, altura):
            (raiz / nome).write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", largura, altura) + b"\0" * 5)
            return str(raiz / nome)

        def jpeg(nome, largura, altura):
            (raiz / nome).write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF" + b"\0" * 10
                                      + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, altura, largura) + b"\0" * 10)
            return str(raiz / nome)
        self.png, self.jpeg = png, jpeg
        self.linhas = []

        def msg(link, foto, chat="120363000000000010@g.us", ha_s=600):
            self.linhas.append({"chatId": chat, "body": f"*SONATY BLUE*\nPor R$ 158\n{link}", "mediaPath": foto,
                                "timestamp": self.agora - ha_s})
            pipeline.TAP.write_text("".join(json.dumps(l) + "\n" for l in self.linhas), encoding="utf-8")
        self.msg = msg
        # outro grupo postou o mesmo produto (mesma chave) com outro link
        self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                         (self.agora - 900, "ao vivo", "g", "sonaty-blue|100", "BOA", 64,
                          json.dumps({"link": "https://meli.la/2lpdMa", "preco": 158})))

    def tearDown(self):
        pipeline.TAP = self.tap_antes
        self.pasta.cleanup()

    def test_le_o_tamanho_de_png_e_jpeg_sem_biblioteca(self):
        from pathlib import Path
        self.assertEqual(pipeline.tamanho_da_imagem(Path(self.png("a.png", 1000, 800))), (1000, 800))
        self.assertEqual(pipeline.tamanho_da_imagem(Path(self.jpeg("b.jpg", 1024, 1024))), (1024, 1024))
        self.assertIsNone(pipeline.tamanho_da_imagem(Path(self.pasta.name) / "nao-existe.jpg"))

    def test_acha_a_foto_do_post_de_origem(self):
        foto = self.jpeg("origem.jpg", 1000, 1000)
        self.msg("https://meli.la/gij7RdR", foto)
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR"), foto)

    def test_vale_a_foto_de_outro_grupo_que_postou_o_mesmo_produto(self):
        self.msg("https://meli.la/gij7RdR", "")                       # o grupo de origem postou só texto
        foto = self.png("outro.png", 1100, 1100)
        self.msg("https://meli.la/2lpdMa", foto)
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR"), foto)

    def test_print_de_tela_miniatura_e_foto_dos_nossos_grupos_ficam_de_fora(self):
        self.msg("https://meli.la/gij7RdR", self.jpeg("print.jpg", 1080, 2200))      # print do app do ML
        self.msg("https://meli.la/gij7RdR", self.jpeg("mini.jpg", 320, 320))
        self.msg("https://meli.la/gij7RdR", self.jpeg("edicao.jpg", 1000, 1000), chat="120363000000000001@g.us")
        self.msg("https://meli.la/gij7RdR", str(self.pasta.name) + "/apagada.jpg")   # já saiu do cache
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR"), "")

    def post(self, link, foto, texto):
        import json
        self.linhas.append({"chatId": "120363000000000010@g.us", "body": texto + chr(10) + "Por R$ 199" + chr(10) + link,
                            "mediaPath": foto, "timestamp": self.agora - 300})
        pipeline.TAP.write_text("".join(json.dumps(l) + chr(10) for l in self.linhas), encoding="utf-8")

    def avaliacao(self, produto, link):
        import json
        self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                         (self.agora - 800, "ao vivo", "g", "club-intense-nuit|105", "BOA", 65,
                          json.dumps({"produto": produto, "link": link, "preco": 199})))

    def test_15_10_o_feminino_nao_pega_a_foto_do_post_do_masculino(self):
        """O Club de Nuit FEMININO foi ao grupo com a foto de um post do CLUB DE NUIT INTENSE MAN (mesma chave)."""
        fem = "Perfume Armaf Club De Nuit Intense 105 ml Edp - Feminino"
        self.avaliacao("Club De Nuit Intense Man 105ml", "https://meli.la/1man")
        self.post("https://meli.la/1man", self.jpeg("man.jpg", 1000, 1000), "*CLUB DE NUIT INTENSE MAN*")
        self.assertEqual(pipeline.foto_do_grupo(self.con, "club-intense-nuit|105", "https://meli.la/1fem", fem), "")
        sem_genero = self.jpeg("sem.jpg", 1100, 1100)    # a linha tem as duas versões: post sem gênero não prova nada
        self.avaliacao("Club De Nuit Intense Da Armaf Edt 105ml", "https://meli.la/1sem")
        self.post("https://meli.la/1sem", sem_genero, "*CLUB DE NUIT INTENSE*")
        self.assertEqual(pipeline.foto_do_grupo(self.con, "club-intense-nuit|105", "https://meli.la/1fem", fem), "")
        # sem o nome do produto (como antes), a maior foto: a do post sem gênero
        self.assertEqual(pipeline.foto_do_grupo(self.con, "club-intense-nuit|105", "https://meli.la/1fem"), sem_genero)

    def test_15_10_vale_o_post_do_mesmo_genero_e_o_proprio_post(self):
        fem = "Perfume Armaf Club De Nuit Intense 105 ml Edp - Feminino"
        self.avaliacao("Club De Nuit Intense Man 105ml", "https://meli.la/1man")
        self.post("https://meli.la/1man", self.jpeg("man.jpg", 1300, 1300), "*CLUB DE NUIT INTENSE MAN*")
        mulher = self.jpeg("mulher.jpg", 1000, 1000)
        self.avaliacao("Club De Nuit Intense Woman", "https://meli.la/rUviyq")
        self.post("https://meli.la/rUviyq", mulher, "*CLUB DE NUIT INTENSE WOMAN*")
        self.assertEqual(pipeline.foto_do_grupo(self.con, "club-intense-nuit|105", "https://meli.la/1fem", fem), mulher)
        proprio = self.jpeg("proprio.jpg", 1200, 1200)   # o post de onde a promo veio, mesmo sem gênero no texto
        self.post("https://meli.la/1fem", proprio, "*CLUB DE NUIT INTENSE*")
        self.assertEqual(pipeline.foto_do_grupo(self.con, "club-intense-nuit|105", "https://meli.la/1fem", fem), proprio)

    def test_15_10_linha_de_uma_versao_so_aceita_post_sem_genero(self):
        """Salvo só tem a versão masculina: o post "*SALVO EDP*" (sem gênero) continua valendo para o masculino."""
        foto = self.jpeg("salvo.jpg", 1000, 1000)
        self.post("https://meli.la/2lpdMa", foto, "*SONATY BLUE EDP*")
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR",
                                                "Perfume Masculino Sonaty Blue 100ml"), foto)
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR",
                                                "Perfume Feminino Sonaty Blue 100ml"), foto)

    def test_15_10_palavras_de_genero(self):
        self.assertEqual(pipeline.generos("Fragrância FEMININA, para ela"), {"f"})
        self.assertEqual(pipeline.generos("Bleu de Chanel Pour Homme"), {"m"})
        self.assertEqual(pipeline.generos("Kit masculino e feminino"), {"m", "f"})
        self.assertEqual(pipeline.generos("Le Male Elixir"), set())

    def test_entre_duas_fica_a_maior(self):
        self.msg("https://meli.la/gij7RdR", self.jpeg("pequena.jpg", 800, 800))
        maior = self.jpeg("maior.jpg", 1200, 1200)
        self.msg("https://meli.la/gij7RdR", maior)
        self.assertEqual(pipeline.foto_do_grupo(self.con, "sonaty-blue|100", "https://meli.la/gij7RdR"), maior)


class TestRessalvaDaCuradora(unittest.TestCase):
    """11/10: promo que a própria curadora marcaria com ressalva não vai pelo pipeline."""

    def av(self, **campos):
        from types import SimpleNamespace
        return SimpleNamespace(**{"risco": False, "risco_motivo": "", "produtos_vendedor": None, "loja_selo": False,
                                  **campos})

    def test_vendedor_de_um_produto_so_e_preco_muito_abaixo_de_todos(self):
        self.assertIn("loja de 1 produto", pipeline.ressalva_da_curadora(self.av(produtos_vendedor=1)))
        motivo = "25% abaixo de todos os outros 46 vendedores do ML sem vendedor confiável: confira se é original"
        self.assertEqual(pipeline.ressalva_da_curadora(self.av(risco=True, risco_motivo=motivo)), motivo)

    def test_loja_com_selo_e_vendedor_conhecido_passam(self):
        self.assertEqual(pipeline.ressalva_da_curadora(self.av(produtos_vendedor=1, loja_selo=True)), "")
        self.assertEqual(pipeline.ressalva_da_curadora(self.av(produtos_vendedor=7)), "")
        self.assertEqual(pipeline.ressalva_da_curadora(self.av()), "")     # vendedor não conferido: não afirma nada


class TestTextoArrumado(unittest.TestCase):
    """11/10: chamada do grupo virou nome de produto e o preço do grupo foi no lugar do conferido."""

    def test_chamada_com_cara_de_produto_sai_e_o_preco_vira_o_conferido(self):
        corpo = "*O PERFUME MAIS HYPADO VERSÃO 50ML*\n\n*Perfume Jequiti Cebolinha 50ml*\n\nPor *R$ 39* 🦸🏻‍♂️\n\nhttps://meli.la/dB5IxhS"
        self.assertEqual(pipeline.arrumar_resposta(corpo, "Perfume Jequiti Cebolinha 50ml", 44.9),
                         "*Perfume Jequiti Cebolinha 50ml*\n\nPor *R$ 44,90* 🦸🏻‍♂️\n\nhttps://meli.la/dB5IxhS")

    def test_no_formato_da_casa_o_nome_vai_para_a_primeira_linha(self):
        corpo = ("*200ML DA PRO ANO INTEIRO*\n_Loja Oficial *Banderas* no ML_\n\n*Perfume Banderas The Icon Parfum 200ml*"
                 "\n\nDe R$ 379 por *R$ 194* no Pix 🦸🏻‍♂️\n\nhttps://meli.la/7JHmvkF")
        novo = pipeline.arrumar_resposta(corpo, "Perfume Banderas The Icon Parfum 200ml", 194.49)
        self.assertEqual(novo.split("\n")[0], "*Perfume Banderas The Icon Parfum 200ml*")
        self.assertNotIn("ANO INTEIRO", novo)
        self.assertIn("_Loja Oficial *Banderas* no ML_", novo)
        self.assertIn("por *R$ 194* no Pix", novo)          # R$194,49 x R$194: é o mesmo preço

    def test_chamada_sem_cara_de_produto_e_texto_do_radar_ficam_como_estao(self):
        chamada = "*COMPARADO AO LA NUIT TRÉSOR 🥷*\n\n*Perfume Deo Colônia La Luna 100ml*\n\nPor *R$ 79* 🦸🏻‍♂️\n\nhttps://meli.la/x"
        self.assertEqual(pipeline.arrumar_resposta(chamada, "Perfume Deo Colônia La Luna 100ml", 79.0), chamada)
        radar = "*Perfume Lattafa Fakhar Platin Eau de Parfum Unissex 100ML*\n\nPor *R$ 149* 🦸🏻‍♂️\n\nhttps://meli.la/Nqdv3Gm"
        self.assertEqual(pipeline.arrumar_resposta(radar, "Perfume Lattafa Fakhar Platin Eau de Parfum Unissex 100ML", 149.04), radar)

    def test_com_cupom_no_texto_o_preco_nao_e_mexido_e_milhar_e_lido_certo(self):
        corpo = "*Polo Black 200ml*\n\nDe R$ 1.069 por *R$ 1.052* 🦸🏻‍♂️\nCupom: *X*\n\nhttps://meli.la/x"
        self.assertEqual(pipeline.arrumar_resposta(corpo, "Polo Black 200ml", None), corpo)
        self.assertIn("por *R$ 1.190,00*", pipeline.arrumar_resposta(corpo, "Polo Black 200ml", 1190.0))


class TestRodizioETamanhos(unittest.TestCase):
    """11/10 (Breno): vários Armani e Ralph Lauren seguidos; o mesmo perfume em tamanhos diferentes."""

    def test_marca_como_quem_le_o_grupo_enxerga(self):
        from types import SimpleNamespace
        cur = SimpleNamespace(ident=SimpleNamespace(analisar=lambda nome: SimpleNamespace(marca="")))
        for nome, marca in (("Perfume Polo 67 Ralph Lauren EDT 125ml", "ralph lauren"), ("Ralph's Club New York EDP 110ml", "ralph lauren"),
                            ("STRONGER WITH YOU PARFUM 100ml", "armani"), ("Armani Beauty Perfume Feminino My Way 30ml", "armani"),
                            ("Perfume Masculino Giorgio Armani Acqua di Giò 100ml", "armani"), ("HAWAS CHROME", "rasasi"),
                            ("Kaiak Aero Natura 100 ml", "natura"), ("Perfume Desconhecido 100ml", "")):
            self.assertEqual(pipeline.marca_visivel(cur, nome), marca, nome)

    def test_quem_repete_a_marca_das_ultimas_quatro_vai_para_depois(self):
        marca = lambda nome: "armani" if "Armani" in nome else "rayhaan" if "Rayhaan" in nome else ""
        lista = [{"produto": "Armani Code 125ml", "preco": 280}, {"produto": "Rayhaan Imperia 100ml", "preco": 178},
                 {"produto": "Mawwal Sama 100ml", "preco": 165}]
        ultimas = [{"produto": "Armani My Way 30ml", "preco": 289}, {"produto": "Nautica Voyage 100ml", "preco": 76},
                   {"produto": "Azzaro Chrome 100ml", "preco": 243}, {"produto": "Bareeq 100ml", "preco": 118}]
        ordem = [c["produto"].split()[0] for c in pipeline.misturar(lista, lambda c: c["produto"], ultimas, marca)]
        self.assertEqual(ordem, ["Rayhaan", "Mawwal", "Armani"])

    def test_familia_ignora_tamanho_e_embalagem(self):
        self.assertEqual(pipeline.familia("beauty-my-way|30"), pipeline.familia("beauty-my-way|90"))
        self.assertEqual(pipeline.familia("acqua-di-gio|100"), pipeline.familia("acqua-di-gio-refilavel|100"))
        self.assertNotEqual(pipeline.familia("acqua-di-gio|100"), pipeline.familia("acqua-di-gio-profondo|50"))
        self.assertNotEqual(pipeline.familia("|25"), pipeline.familia("|50"))
        self.assertEqual(pipeline.familia("kit:brae|60"), "kit:brae|60")

    def test_outro_tamanho_de_perfume_postado_ha_pouco_espera(self):
        import json, time
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        agora = int(time.time())
        con = conectar(":memory:")
        cur = Curadora(CFG, con, None)
        for chave, score in (("teste-way|90", 70), ("teste-outro|100", 65)):
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                        (agora - 300, "ao vivo", "g", chave, "BOA", score, json.dumps({"preco": 500})))
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                    ("p1", agora - 3600, "Grupo da Equipe", "Teste Way 30ml", "teste-way|30", 289.0))
        con.commit()
        _, lista = pipeline.candidatas(con, cur, set())
        chaves = [c["chave"] for c in lista if c["chave"].startswith("teste-")]
        self.assertEqual(chaves, ["teste-outro|100"])
        con.execute("UPDATE postagens SET ts = ?", (agora - 5 * 3600,))    # passadas as 4h, o outro tamanho volta
        con.commit()
        pipeline.painel._cache_cards.clear()
        _, lista = pipeline.candidatas(con, cur, set())
        self.assertIn("teste-way|90", [c["chave"] for c in lista])


class TestNaturaEBoticarioComNotaMaisBaixa(unittest.TestCase):
    def av(self, produto, nota, score):
        from types import SimpleNamespace
        return SimpleNamespace(nota=nota, score=score, promo=SimpleNamespace(produto=produto))

    def test_a_regua_do_pipeline_e_a_mesma_para_todas_as_marcas(self):
        # a exceção de nota 30 saiu: agora é a nota de Natura/Boticário que sobe na curadora
        self.assertFalse(pipeline.aceitavel(self.av("Natura Homem Elo 100ml", "NÃO VALE", 38)))
        self.assertTrue(pipeline.aceitavel(self.av("Natura Homem Elo 100ml", "OK", 58)))
        self.assertFalse(pipeline.aceitavel(self.av("Armani Code 125ml", "NÃO VALE", 38)))
        self.assertTrue(pipeline.aceitavel(self.av("Armani Code 125ml", "OK", 50)))
        self.assertTrue(pipeline.aceitavel(self.av("Armani Code 125ml", "BOA", 60)))

    def test_natura_a_preco_quase_normal_sobe_ate_media_e_nunca_vira_boa_por_isso(self):
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.ml import CardML
        from curadora.score import Curadora
        normal = [("Natura Homem Elo 100ml", p, d) for d, p in enumerate([143, 142, 144, 143, 145, 143, 141, 143, 144, 143])]
        normal += [("Rayhaan Imperia 100ml", p, d) for d, p in enumerate([143, 142, 144, 143, 145, 143, 141, 143, 144, 143])]
        cards = {"https://meli.la/n": CardML(ok=True, titulo="Natura Homem Elo Deo Parfum 100ml", preco=135.0),
                 "https://meli.la/r": CardML(ok=True, titulo="Rayhaan Imperia Eau De Parfum 100ml", preco=135.0)}
        cur = Curadora(CFG, banco_com_historico(normal), MLFalso(cards))
        agora = apoio.T0 + 15 * apoio.DIA
        natura = cur.avaliar("*Natura Homem Elo 100ml*\nPor R$ 135\nhttps://meli.la/n", agora=agora)
        outra = cur.avaliar("*Rayhaan Imperia 100ml*\nPor R$ 135\nhttps://meli.la/r", agora=agora)
        self.assertGreaterEqual(natura.score, outra.score + 20)   # as duas no mesmo preço e com o mesmo histórico
        self.assertLess(natura.score, 60)                         # sobe no máximo até Média
        self.assertLess(outra.score, 45)                          # a outra marca continua onde estava
        self.assertTrue(any("vende bem" in r for r in natura.razoes))
        self.assertFalse(any("vende bem" in r for r in outra.razoes))

    def test_kit_de_body_splash_e_vodka_nunca_ficam_ruins_no_preco_normal(self):
        # 15/10 (Breno: "kits de body splash são bem pedidos, não devem ser ruins"; "até os perfuminhos Vodka vendem bem")
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.ml import CardML
        from curadora.score import Curadora
        normal = [("Kit Body Splash Adventus e Enigma 200ml", p, d) for d, p in enumerate([70, 70, 71, 69, 70, 70, 70, 71])]
        normal += [("Vodka Man Paris Elysees 100ml", p, d) for d, p in enumerate([72, 73, 72, 72, 74, 72, 73, 72])]
        normal += [("Rayhaan Imperia 100ml", p, d) for d, p in enumerate([70, 70, 71, 69, 70, 70, 70, 71])]
        cards = {f"https://meli.la/{k}": CardML(ok=True, titulo=t, preco=p) for k, t, p in (
            ("k", "Kit Body Splash Adventus e Enigma 200ml", 76.0), ("v", "Vodka Man Paris Elysees 100ml", 70.0),
            ("c", "Kit Body Splash Adventus e Enigma 200ml", 90.0), ("r", "Rayhaan Imperia Eau De Parfum 100ml", 76.0))}
        cur = Curadora(CFG, banco_com_historico(normal), MLFalso(cards))
        agora = apoio.T0 + 15 * apoio.DIA
        nota = lambda nome, k, p: cur.avaliar(chr(10).join([f"*{nome}*", f"Por R$ {p}", f"https://meli.la/{k}"]), agora=agora)
        kit = nota("Kit Body Splash Adventus e Enigma 200ml", "k", 76)       # 8% acima do normal
        vodka = nota("Vodka Man Paris Elysees 100ml", "v", 70)              # um pouco abaixo do normal
        caro = nota("Kit Body Splash Adventus e Enigma 200ml", "c", 90)      # bem acima do normal
        outra = nota("Rayhaan Imperia 100ml", "r", 76)
        self.assertEqual((kit.nota, vodka.nota), ("OK", "OK"))
        self.assertGreaterEqual(min(kit.score, vodka.score), 50)
        self.assertEqual(caro.nota, "NÃO VALE")                       # mais de 10% acima do normal não é promo
        self.assertEqual(outra.nota, "NÃO VALE")                      # outra marca no mesmo caso continua Ruim


class TestListaDoPainel(unittest.TestCase):
    """11/10: o que não devia estar na lista de Boas (prints do Breno)."""

    def setUp(self):
        import json, time
        import painel
        from curadora.banco import conectar
        self.painel, self.json = painel, json
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.registro = painel.REGISTRO_PIPELINE

    def tearDown(self):
        self.painel.REGISTRO_PIPELINE = self.registro

    def avaliacao(self, chave, ha_s=60, nota="BOA", score=60, origem="ao vivo", postada=0, **dados):
        cur = self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, postada, dados) "
                               "VALUES (?,?,?,?,?,?,?,?)", (self.agora - ha_s, origem, "g", chave, nota, score, postada,
                                                            self.json.dumps({"preco": 400, **dados})))
        self.con.commit()
        return cur.lastrowid

    def cards(self):
        return {c["chave"]: c for c in self.painel._cards(self.con)}

    def test_promo_que_o_pipeline_mandou_hoje_aparece_como_feita_mesmo_postada_ontem(self):
        import tempfile
        from pathlib import Path
        ontem = self.agora - 20 * 3600
        self.avaliacao("teste-gio|100", postada=ontem)
        self.avaliacao("teste-livre|100")
        with tempfile.TemporaryDirectory() as pasta:
            self.painel.REGISTRO_PIPELINE = Path(pasta) / "pipeline.jsonl"
            self.assertEqual(self.cards()["teste-gio|100"]["postada"], ontem)       # sem envio hoje: marca de ontem
            self.painel.REGISTRO_PIPELINE.write_text(self.json.dumps(
                {"ts": self.agora - 7200, "status": "enviado", "chave": "teste-gio|100", "preco": 400}) + "\n", encoding="utf-8")
            cards = self.cards()
        self.assertEqual(cards["teste-gio|100"]["postada"], self.agora - 7200)      # mandada há 2h: feita
        self.assertEqual(cards["teste-livre|100"]["postada"], 0)

    def test_promo_da_amazon_nao_entra_na_lista(self):
        self.avaliacao("teste-voyage|100", link="https://link.amazon/B0cxme0qI")
        self.avaliacao("teste-asad|100", link="https://meli.la/TqV0zAq")
        self.avaliacao("teste-sem-link|100")
        self.assertEqual(sorted(self.cards()), ["teste-asad|100", "teste-sem-link|100"])

    def test_reconferencia_que_descobre_que_nao_e_perfume_tira_o_card(self):
        primeiro = self.avaliacao("teste-kit|60", ha_s=3600, link="https://meli.la/inSA3Uh")
        self.assertIn("teste-kit|60", self.cards())
        self.avaliacao("teste-kit|60", ha_s=30, nota="FORA", score=0, origem="reconferida", reconferida_de=primeiro)
        self.assertNotIn("teste-kit|60", self.cards())

    def test_pelo_titulo_do_anuncio_no_ml_nao_e_perfume(self):
        from apoio import CFG, MLFalso
        from curadora.ml import CardML
        from curadora.score import Curadora
        cards = {"https://meli.la/a": CardML(ok=True, titulo="Kit Braé Stages Nutrition Shampoo, Cond, Másc + Leave-in", preco=131.59),
                 "https://meli.la/b": CardML(ok=True, titulo="Perfume Lattafa Asad Eau De Parfum 100ml", preco=150.0)}
        cur = Curadora(CFG, self.con, MLFalso(cards))
        kit = cur.avaliar("*Kit Stages Nutrition 60ml*\nPor R$ 131\nhttps://meli.la/a", agora=self.agora)
        self.assertEqual((kit.nota, kit.score), ("FORA", 0))
        asad = cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/b", agora=self.agora)
        self.assertNotEqual(asad.nota, "FORA")


class TestNotaDaCuradoraEm1110(unittest.TestCase):
    """11/10 à noite: piso de loja oficial só com 10% nas caras; nome genérico do grupo usa o título do ML."""

    def curadora(self, historico, cards):
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.score import Curadora
        return Curadora(dict(CFG, lojas_top=["Loja Teste"]), banco_com_historico(historico), MLFalso(cards))

    def test_piso_de_loja_oficial_exige_10_por_cento_nas_caras(self):
        from curadora.ml import CardML
        hist = [("Perfume Caro Teste 100ml", 500, d) for d in range(10)] + [("Perfume Barato Teste 100ml", 200, d) for d in range(10)]
        loja = dict(ok=True, vendedor="Loja Teste", loja_oficial=True)
        cards = {"https://meli.la/c5": CardML(titulo="Perfume Caro Teste 100ml", preco=475.0, **loja),      # 5% e R$475
                 "https://meli.la/c12": CardML(titulo="Perfume Caro Teste 100ml", preco=440.0, **loja),     # 12% e R$440
                 "https://meli.la/b5": CardML(titulo="Perfume Barato Teste 100ml", preco=190.0, **loja)}    # 5% e R$190
        cur = self.curadora(hist, cards)
        agora = apoio.T0 + 15 * apoio.DIA
        nota = lambda nome, preco, link: cur.avaliar(f"*{nome}*\nPor R$ {preco}\nhttps://meli.la/{link}", agora=agora)
        caro5, caro12, barato5 = nota("Perfume Caro Teste 100ml", 475, "c5"), nota("Perfume Caro Teste 100ml", 440, "c12"), nota("Perfume Barato Teste 100ml", 190, "b5")
        if not barato5.loja_selo:
            self.skipTest("a configuração de teste não reconhece a loja como oficial")
        self.assertGreaterEqual(barato5.score, 60)       # até R$350 o piso continua valendo com desconto pequeno
        self.assertLess(caro5.score, 60)                 # R$475 com 5%: não vira Boa só por ser loja oficial
        self.assertGreaterEqual(caro12.score, 60)        # R$440 com 12%: vira

    def test_nome_generico_do_grupo_usa_o_titulo_do_anuncio(self):
        from curadora.ml import CardML
        cards = {"https://meli.la/k": CardML(ok=True, titulo="Natura Kriska Miniatura Colônia 25ml", preco=37.8)}
        cur = self.curadora([], cards)
        av = cur.avaliar("*Perfume 25ml*\nPor R$ 37,80\nhttps://meli.la/k", agora=apoio.T0 + 15 * apoio.DIA)
        self.assertFalse(av.identidade.chave.startswith("|"))
        self.assertIn("kriska", av.identidade.chave)
        self.assertEqual(av.promo.produto, "Natura Kriska Miniatura Colônia 25ml")


class TestVendedorELojaOficial(unittest.TestCase):
    """18/10 (Breno: "vendedor com poucas vendas tem que evitar como padrão, dar uma boa preferência em lojas oficiais")."""

    def test_vendedor_comum_com_poucas_vendas_nao_vai(self):
        from curadora.ml import CardML
        self.assertEqual(pipeline.vendedor_duvidoso(CardML(ok=True, loja_oficial=True)), "")
        grande = CardML(ok=True, reputacao={"classe": "boa", "vendas": 12000, "nivel": "5_green"})
        self.assertEqual(pipeline.vendedor_duvidoso(grande), "")
        for rep in ({"classe": "fraca", "vendas": 18, "nivel": "5_green"},        # o Club de Nuit das 17:25
                    {"classe": "fraca", "vendas": 6000, "nivel": "2_orange"},     # muitas vendas, reputação ruim
                    {"classe": "boa", "vendas": 4334, "nivel": "5_green"},
                    {"classe": "desconhecida", "vendas": None}, None):
            self.assertTrue(pipeline.vendedor_duvidoso(CardML(ok=True, reputacao=rep)), rep)

    def test_loja_oficial_passa_na_frente_dentro_da_faixa(self):
        c = lambda chave, score: {"chave": chave, "score": score, "ts": 0}
        cards = [c("boa66-comum", 66), c("boa62-oficial", 62), c("media58-oficial", 58), c("boa61-comum", 61)]
        ordem = [x["chave"] for x in pipeline.na_ordem_da_novidade(cards, set(), set(),
                                                                   {"boa62-oficial", "media58-oficial"})]
        self.assertEqual(ordem, ["boa62-oficial", "boa66-comum", "boa61-comum", "media58-oficial"])

    def test_loja_oficial_vem_da_conferencia_mais_recente(self):
        import json, time
        from curadora.banco import conectar
        con = conectar(":memory:")
        agora = int(time.time())
        for chave, ts, dados in (("a|100", agora - 500, {"ml": {"oficial": False}}),
                                 ("a|100", agora - 100, {"ml": {"oficial": True}}),
                                 ("b|100", agora - 100, {"ml": {"oficial": False}}),
                                 ("c|100", agora - 100, {"preco": 10}),
                                 ("d|100", agora - 7200, {"ml": {"oficial": True}})):
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                        (ts, "ao vivo", "g", chave, "BOA", 60, json.dumps(dados)))
        self.assertEqual(pipeline.lojas_oficiais(con, {"a|100", "b|100", "c|100", "d|100"}, agora - 3600), {"a|100"})


class TestGeradorQueNaoResponde(unittest.TestCase):
    """18/10: o Gerador não respondia para o Mawwal Rayhan e o pipeline tentava de novo a cada rodada."""

    def test_produto_sem_resposta_do_gerador_fica_fora_por_6h(self):
        import json, tempfile, time
        from pathlib import Path
        agora = time.time()
        registro = pipeline.REGISTRO
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.REGISTRO = Path(pasta) / "pipeline.jsonl"
            try:
                linhas = [{"chave": "rayhan|100", "status": "gerador_sem_resposta", "ts": agora - 3600},
                          {"chave": "antigo|100", "status": "gerador_sem_resposta", "ts": agora - 7 * 3600},
                          {"chave": "foi|100", "status": "enviado", "ts": agora - 600}]
                pipeline.REGISTRO.write_text("\n".join(json.dumps(l) for l in linhas) + "\n", encoding="utf-8")
                self.assertEqual(pipeline.gerador_nao_respondeu(agora - pipeline.GERADOR_FALHOU_S), {"rayhan|100"})
            finally:
                pipeline.REGISTRO = registro

    def test_resposta_com_nome_so_de_marca_e_reconhecida(self):
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        cur = Curadora(CFG, conectar(":memory:"), None)
        resposta = ("*ÁRABE AROMÁTICO*\n\n*Perfume Árabe Mawwal Rayhan EDP 100ml*\n\nDe R$ 489 por *R$ 185* 🦸\n\n"
                    "https://meli.la/hQu2E2o")
        self.assertTrue(pipeline.mesma_promo(cur, "Perfume Árabe Mawwal Rayhan EDP 100ml", resposta))
        self.assertFalse(pipeline.mesma_promo(cur, "Perfume Árabe Mawwal Malek EDP 100ml", resposta))


if __name__ == "__main__":
    unittest.main()


class TestReserva(unittest.TestCase):
    """14/10 (Breno: "mesmo com pouca promoção eu preciso enviar algo, mesmo uma promo de 6 horas atrás")."""

    def setUp(self):
        import json, time
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.cur = Curadora(CFG, self.con, None)

        def avaliacao(chave, nota, score, postada_ha_h):
            self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados, postada) "
                             "VALUES (?,?,?,?,?,?,?,?)", (self.agora - 600, "ao vivo", "g", chave, nota, score,
                                                          json.dumps({"preco": 200}), self.agora - postada_ha_h * 3600))
        avaliacao("teste-foi-ha-7h|100", "BOA", 70, 7)
        avaliacao("teste-foi-ha-5h|100", "IMPERDÍVEL", 90, 5)
        avaliacao("teste-foi-ha-1h|100", "IMPERDÍVEL", 95, 1)
        self.con.commit()
        # 21/10: as três vieram do pipeline (mandadas 30 min antes do post); o post da equipe tem teste próprio
        import tempfile
        from pathlib import Path
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.addCleanup(setattr, pipeline, "REGISTRO", pipeline.REGISTRO)
        pipeline.REGISTRO = Path(pasta.name) / "pipeline.jsonl"
        pipeline.REGISTRO.write_text("".join(json.dumps({"ts": self.agora - h * 3600 - 1800, "status": "enviado",
                                                         "chave": f"teste-foi-ha-{h}h|100", "preco": 200}) + "\n"
                                             for h in (7, 5, 1)), encoding="utf-8")

    def chaves(self, janela):
        pipeline._janela_repetir = janela
        try:
            _, lista = pipeline.candidatas(self.con, self.cur, set())
        finally:
            pipeline._janela_repetir = pipeline.REPETIR_APOS_S
        return [c["chave"] for c in lista if c["chave"].startswith("teste-")]

    def test_fora_da_reserva_nada_que_o_pipeline_mandou_ha_menos_de_8h_volta(self):
        self.assertEqual(self.chaves(pipeline.REPETIR_APOS_S), [])

    def test_fora_da_reserva_post_da_equipe_volta_depois_de_6h(self):
        # 21/10 (Breno: "sim pros 2"): o pipeline não tinha mandado, então o post foi da equipe
        pipeline.REGISTRO.write_text("", encoding="utf-8")
        self.assertEqual(self.chaves(pipeline.REPETIR_APOS_S), ["teste-foi-ha-7h|100"])

    def test_na_reserva_primeiro_as_de_6h_depois_as_de_4h_e_nunca_as_recentes(self):
        self.assertEqual(self.chaves(6 * 3600), ["teste-foi-ha-7h|100"])
        self.assertEqual(self.chaves(4 * 3600), ["teste-foi-ha-5h|100", "teste-foi-ha-7h|100"])

    def test_a_reserva_tenta_6h_e_depois_4h_e_volta_a_regra_normal(self):
        janelas = []
        antes = pipeline.uma_promo
        pipeline.uma_promo = lambda con, cur, excluir, marca, n, simular: (janelas.append(
            (pipeline._janela_repetir, pipeline._na_reserva)) or None)
        try:
            self.assertIsNone(pipeline.promo_da_reserva(self.con, self.cur, 1))
        finally:
            pipeline.uma_promo = antes
        self.assertEqual(janelas, [(6 * 3600, True), (4 * 3600, True)])
        self.assertEqual((pipeline._janela_repetir, pipeline._na_reserva), (pipeline.REPETIR_APOS_S, False))


class TestFundoDaFila(unittest.TestCase):
    """15/10 (Breno: "mesmo promos ruins têm que ir; se tiver uma melhor, obviamente postar a melhor")."""

    def setUp(self):
        import json, time
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.cur = Curadora(CFG, self.con, None)
        for chave, nota, score in (("teste-boa|100", "BOA", 70), ("teste-media45|100", "OK", 45),
                                   ("teste-ruim30|100", "NÃO VALE", 30), ("teste-ruim10|100", "NÃO VALE", 10)):
            self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                             (self.agora - 600, "ao vivo", "g", chave, nota, score, json.dumps({"preco": 100})))
        self.con.commit()

    def chaves(self, fundo):
        pipeline._no_fundo = fundo
        try:
            pri, lista = pipeline.candidatas(self.con, self.cur, set())
        finally:
            pipeline._no_fundo = False
        return [c["chave"] for c in pri + lista if c["chave"].startswith("teste-")]

    def test_fora_do_fundo_so_a_regua_de_sempre(self):
        self.assertEqual(self.chaves(False), ["teste-boa|100"])

    def test_no_fundo_as_que_sobraram_da_melhor_para_a_pior_e_nunca_abaixo_de_20(self):
        self.assertEqual(self.chaves(True), ["teste-media45|100", "teste-ruim30|100"])

    def test_no_fundo_a_nota_baixa_e_aceita_e_a_regra_volta_depois(self):
        from types import SimpleNamespace
        ruim = SimpleNamespace(nota="NÃO VALE", score=30)
        self.assertFalse(pipeline.aceitavel(ruim))
        pipeline._no_fundo = True
        try:
            self.assertTrue(pipeline.aceitavel(ruim))
            self.assertFalse(pipeline.aceitavel(SimpleNamespace(nota="FORA", score=0)))
        finally:
            pipeline._no_fundo = False

    def test_o_fundo_so_e_tentado_com_a_flag_e_ela_volta(self):
        vistos = []
        antes = pipeline.uma_promo
        pipeline.uma_promo = lambda con, cur, excluir, marca, n, simular: vistos.append(pipeline._no_fundo) or None
        try:
            self.assertIsNone(pipeline.promo_do_fundo(self.con, self.cur, 1))
        finally:
            pipeline.uma_promo = antes
        self.assertEqual((vistos, pipeline._no_fundo), ([True], False))


class TestMenosRepeticao(unittest.TestCase):
    """15/10 (Breno: "tem algumas repetições… será que tá olhando muito as mesmas coisas?")."""

    def test_o_que_foi_nas_ultimas_24h_vai_para_o_fim_da_faixa(self):
        import json, tempfile, time
        from pathlib import Path
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        agora = int(time.time())
        con = conectar(":memory:")
        cur = Curadora(CFG, con, None)
        for chave, nota, score, extra in (("teste-de-ontem|100", "BOA", 80, {}), ("teste-nova|100", "BOA", 70, {}),
                                          ("teste-media|100", "OK", 55, {}),
                                          ("teste-queridinho|100", "BOA", 75, {"top_vendido": True})):
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                        (agora - 600, "ao vivo", "g", chave, nota, score, json.dumps({"preco": 100, **extra})))
        con.commit()
        antes = pipeline.REGISTRO
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.REGISTRO = Path(pasta) / "pipeline.jsonl"
            pipeline.REGISTRO.write_text("".join(json.dumps({"chave": k, "status": "enviado", "preco": 150,
                                                             "ts": agora - 20 * 3600}) + chr(10)
                                                 for k in ("teste-de-ontem|100", "teste-queridinho|100")), encoding="utf-8")
            try:
                _, lista = pipeline.candidatas(con, cur, set())
            finally:
                pipeline.REGISTRO = antes
        # o queridinho que foi ontem não perde a vez; o comum que foi ontem vai para o fim das Boas
        self.assertEqual([c["chave"] for c in lista if c["chave"].startswith("teste-")],
                         ["teste-queridinho|100", "teste-nova|100", "teste-de-ontem|100", "teste-media|100"])


class TestNovidadeNaFrente(unittest.TestCase):
    """16/10 (Breno: "tem uma certa repetição… tinha que expandir o leque de produtos"; "faz a 1 e a 2")."""

    def test_quem_foi_nos_ultimos_3_dias_perde_a_vez_para_um_novo_de_ate_5_pontos_abaixo(self):
        c = lambda chave, score, ts=0: {"chave": chave, "score": score, "ts": ts}
        cards = [c("boa60-repetida", 60), c("media56-nova", 56), c("media54-nova", 54), c("boa70-repetida", 70),
                 c("boa70-de-ontem", 70), c("boa62-nova", 62)]
        repetidos = {"boa60-repetida", "boa70-repetida", "boa70-de-ontem"}
        ordem = [x["chave"] for x in pipeline.na_ordem_da_novidade(cards, {"boa70-de-ontem"}, repetidos)]
        self.assertEqual(ordem, ["boa70-repetida",    # Boa 70 de 2 dias atrás: 64,5 continua na faixa Boa
                                 "boa62-nova",        # 8 pontos abaixo: não é "parecida", vai depois da repetida
                                 "boa70-de-ontem",    # foi nas últimas 24h: fim da faixa (como antes)
                                 "media56-nova",      # Média nova passa na frente da Boa 60 repetida (54,5)
                                 "boa60-repetida",
                                 "media54-nova"])     # 6 pontos abaixo: não passa

    def test_postado_no_grupofinal_nos_ultimos_3_dias_tambem_conta(self):
        import json, tempfile, time
        from pathlib import Path
        from apoio import CFG
        from curadora.banco import conectar
        from curadora.score import Curadora
        agora = int(time.time())
        con = conectar(":memory:")
        cur = Curadora(CFG, con, None)
        for chave, nota, score in (("teste-grupofinal|100", "BOA", 60), ("teste-nova|100", "OK", 57),
                                   ("teste-velha|100", "BOA", 61)):
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                        (agora - 600, "ao vivo", "g", chave, nota, score, json.dumps({"preco": 100})))
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                    ("m1", agora - 2 * 86400, "Grupo da Equipe", "Perfume Teste GrupoFinal", "teste-grupofinal|100", 100))
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                    ("m2", agora - 4 * 86400, "Grupo da Equipe", "Perfume Teste Velha", "teste-velha|100", 100))
        con.commit()
        antes = pipeline.REGISTRO
        with tempfile.TemporaryDirectory() as pasta:
            pipeline.REGISTRO = Path(pasta) / "pipeline.jsonl"
            try:
                _, lista = pipeline.candidatas(con, cur, set())
            finally:
                pipeline.REGISTRO = antes
        # o postado há 4 dias não conta; o de 2 dias atrás (60 - 5,5 = 54,5) fica atrás da Média 57 nova
        self.assertEqual([x["chave"] for x in lista if x["chave"].startswith("teste-")],
                         ["teste-velha|100", "teste-nova|100", "teste-grupofinal|100"])




class TestGeradorPelaApi(unittest.TestCase):
    """24/10 (Breno): o link de afiliado vem da API do gerador; o BotGerador (WhatsApp) fica de reserva."""

    def setUp(self):
        import json, tempfile
        from pathlib import Path
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        for nome in ("GERADOR_API", "log", "_api_sem_chave_avisado"):
            self.addCleanup(setattr, pipeline, nome, getattr(pipeline, nome))
        self.addCleanup(setattr, pipeline.urllib.request, "urlopen", pipeline.urllib.request.urlopen)
        self.addCleanup(setattr, pipeline.time, "sleep", pipeline.time.sleep)
        self.esperas = []
        pipeline.time.sleep = self.esperas.append
        self.logs = []
        pipeline.log = self.logs.append
        pipeline.GERADOR_API = Path(pasta.name) / "gerador_api.json"
        pipeline.GERADOR_API.write_text(json.dumps({"url": "https://api.teste/process", "token": "t"}), encoding="utf-8")
        self.pedidos, self.respostas = [], []

        class Resposta:
            def __init__(self, corpo):
                self.corpo = corpo

            def read(self):
                return self.corpo

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            self.pedidos.append(json.loads(req.data))
            r = self.respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return Resposta(json.dumps({"detectedStore": "MERCADOLIVRE", "processedText": r}).encode())
        pipeline.urllib.request.urlopen = urlopen

    @staticmethod
    def erro(codigo, cabecalhos=None):
        import urllib.error
        return urllib.error.HTTPError("https://api.teste/process", codigo, "erro", cabecalhos or {}, None)

    def test_troca_o_link_e_devolve_o_texto(self):
        self.respostas = [ZAAD.replace("https://meli.la/TqV0zAq", "https://meli.la/JffjKj")]
        r = pipeline.gerar_pela_api(ZAAD)
        self.assertEqual(r["link"], "https://meli.la/JffjKj")
        self.assertIn("Zaad Edp Infinity 95ml", r["body"])
        self.assertNotIn("2W5jQYb", r["body"])
        self.assertEqual(self.pedidos[0]["generatorSlug"], "gerador-grupofinal-perfumes-api")

    def test_link_do_anuncio_vira_meli_la(self):
        texto = "*Yara*\n\nPor R$ 150\n\nhttps://produto.mercadolivre.com.br/MLB-4208287546"
        self.respostas = ["*Yara*\n\nPor R$ 150\n\nhttps://meli.la/DW1bcg"]
        self.assertEqual(pipeline.gerar_pela_api(texto)["link"], "https://meli.la/DW1bcg")

    def test_o_mesmo_link_duas_vezes_vale(self):
        texto = "*Asad*\nResgate: https://produto.mercadolivre.com.br/MLB-111\n\nhttps://produto.mercadolivre.com.br/MLB-111"
        self.respostas = ["*Asad*\nResgate: https://meli.la/gIwETw\n\nhttps://meli.la/gIwETw"]
        self.assertEqual(pipeline.gerar_pela_api(texto)["link"], "https://meli.la/gIwETw")

    def test_dois_links_diferentes_vai_pelo_whatsapp(self):
        texto = "*Asad*\nResgate o cupom: https://meli.la/5NXgiB\n\nhttps://produto.mercadolivre.com.br/MLB-111"
        self.respostas = ["*Asad*\nResgate o cupom: https://meli.la/7vJe8L\n\nhttps://meli.la/gIwETw"]
        self.assertIsNone(pipeline.gerar_pela_api(texto))

    def test_link_de_outro_afiliado_ou_de_fora_vai_pelo_whatsapp(self):
        texto = "*Asad*\n\nhttps://meli.la/yXNMUn"
        for corpo in ("*Asad*\n\nhttps://meli.la/yXNMUn", "*Asad*\n\nhttp://meli.la/yXNMUn",
                      "*Asad*\nhttps://chat.whatsapp.com/abc\nhttps://meli.la/5uTOlC",
                      "*Asad*\nhttps://mercadolivre.com/sec/OUTRO9\nhttps://meli.la/5uTOlC"):
            self.respostas = [corpo]
            self.assertIsNone(pipeline.gerar_pela_api(texto), corpo)

    def test_nao_trocou_o_link_nao_insiste(self):
        self.respostas = [ZAAD]
        self.assertIsNone(pipeline.gerar_pela_api(ZAAD))
        self.assertEqual(len(self.pedidos), 1)

    def test_falha_de_rede_e_depois_responde(self):
        self.respostas = [TimeoutError(), ZAAD.replace("https://meli.la/TqV0zAq", "https://meli.la/ARHlfE")]
        self.assertEqual(pipeline.gerar_pela_api(ZAAD)["link"], "https://meli.la/ARHlfE")
        self.assertEqual(self.esperas, [3.0])

    def test_limite_de_taxa_espera_o_que_a_api_pede(self):
        for retry, espera in (("3.5", 3.5), ("Sat, 24 Oct 2026 18:00:00 GMT", 5.0), (None, 5.0), ("90", 5.0)):
            self.esperas.clear()
            self.respostas = [self.erro(429, {"Retry-After": retry} if retry else {}),
                              ZAAD.replace("https://meli.la/TqV0zAq", "https://meli.la/opIddH")]
            self.assertEqual(pipeline.gerar_pela_api(ZAAD)["link"], "https://meli.la/opIddH")
            self.assertEqual(self.esperas, [espera])

    def test_recusa_nao_tenta_de_novo(self):
        self.respostas = [self.erro(403)]
        self.assertIsNone(pipeline.gerar_pela_api(ZAAD))
        self.assertEqual(len(self.pedidos), 1)

    def test_duas_falhas_vai_pelo_whatsapp_sem_pausar_a_api(self):
        self.respostas = [self.erro(502), OSError()]
        self.assertIsNone(pipeline.gerar_pela_api(ZAAD))
        self.respostas = [ZAAD.replace("https://meli.la/TqV0zAq", "https://meli.la/oac33N")]
        self.assertEqual(pipeline.gerar_pela_api(ZAAD)["link"], "https://meli.la/oac33N")   # a próxima já tenta
        self.assertEqual(self.esperas, [3.0])

    def test_chave_ausente_ou_errada_nao_chama(self):
        import json
        for conteudo in (None, "[1, 2]", json.dumps({"url": "api.teste/x", "token": "t"}), json.dumps({"url": "https://a"}),
                         "não é json"):
            pipeline._api_sem_chave_avisado = False
            if conteudo is None:
                pipeline.GERADOR_API.unlink(missing_ok=True)
            else:
                pipeline.GERADOR_API.write_text(conteudo, encoding="utf-8")
            self.assertIsNone(pipeline.gerar_pela_api(ZAAD), conteudo)
        self.assertEqual(self.pedidos, [])

    def test_texto_da_api_poe_o_nome_em_cima_e_tira_o_vendedor(self):
        corpo = ("ELITE ÁRABE DUPE DO LE MALE ELIXIR 🥷\n\n➡️ **Perfume Árabe The Kingdom Lattafa EDP 100ml**\n"
                 "🛒Vendido por _*seu perfume (+100k vendas) Verificado pelo ML*_☑️\nSelecione a loja LATTAFA\n\n"
                 "✅ De R$ ~299~ por R$ 183 no Pix\n\n🛒 Compre aqui: https://meli.la/qwmbSB")
        novo = pipeline.texto_da_api(corpo, "Perfume Árabe The Kingdom Lattafa EDP 100ml")
        self.assertTrue(novo.startswith("➡️ **Perfume Árabe The Kingdom"))
        self.assertIn("ELITE ÁRABE", novo)
        self.assertNotIn("Vendido por", novo)
        self.assertNotIn("Selecione a loja", novo)
        self.assertIn("por R$ 183 no Pix", novo)
        so_chamada = pipeline.texto_da_api("DOIS CLÁSSICOS EM SPLASHES\n\nPor R$ 90\n\nhttps://meli.la/N", "Kit Splash Vodka")
        self.assertTrue(so_chamada.startswith("*Kit Splash Vodka*\n\nDOIS CLÁSSICOS"))

    def test_preco_do_ml_entra_nos_formatos_dos_grupos(self):
        for texto in ("🔥🔥Por: *R$ 236,88*", "por:R$*329,99*", "Por: R$*168,00*", "por:*R$ 113,30*", "De R$ 300 por *R$ 250*"):
            self.assertIn("299,90", pipeline.arrumar_resposta(texto, "X", 299.90), texto)


class TestReservaIntercalada(unittest.TestCase):
    """25/10 (Breno: "era melhor outro produto pra variar, talvez uma intercalada")."""

    def setUp(self):
        self.chamadas = []
        for nome in ("promo_da_reserva", "promo_do_fundo"):
            self.addCleanup(setattr, pipeline, nome, getattr(pipeline, nome))
        self.reserva, self.fundo = {"produto": "Kit Cebolinha", "reserva": True}, {"produto": "Média nova", "fundo": True}
        pipeline.promo_da_reserva = lambda con, cur, n, simular=False: self.chamadas.append("reserva") or self.reserva
        pipeline.promo_do_fundo = lambda con, cur, n, simular=False: self.chamadas.append("fundo") or self.fundo

    def test_sem_repetida_antes_tenta_a_reserva(self):
        self.assertIs(pipeline.promo_de_reserva_ou_fundo(None, None, 1, False), self.reserva)
        self.assertEqual(self.chamadas, ["reserva"])

    def test_depois_de_uma_repetida_vai_um_produto_novo(self):
        self.assertIs(pipeline.promo_de_reserva_ou_fundo(None, None, 1, True), self.fundo)
        self.assertEqual(self.chamadas, ["fundo"])

    def test_se_o_fundo_esta_vazio_repete_assim_mesmo(self):
        self.fundo = None
        self.assertIs(pipeline.promo_de_reserva_ou_fundo(None, None, 1, True), self.reserva)
        self.assertEqual(self.chamadas, ["fundo", "reserva"])


class TestFocoNaLojaDaMarca(unittest.TestCase):
    """29/10 (Breno: "como a gente sabe que está rolando cupom nessas lojas, focar em enviar produtos que valem")."""

    def setUp(self):
        import time
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.banco import registrar_avaliacao
        from curadora.esgotados import registrar_sinal
        from curadora.score import Curadora
        self.agora = time.time()
        self.cur = Curadora({**CFG, "cupom_quente": {"ligado": True}}, banco_com_historico([]), MLFalso({}))
        self.con = self.cur.con
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, minimo, limite, perfume_ts) "
                         "VALUES ('LOJASHOJE', ?, ?, 10, 79, 30, ?)", (self.agora - 3600, self.agora - 600, self.agora))
        self.ids = {}
        for nome, vendedor in (("natura", "Natura"), ("lipx", "Lipx")):
            self.ids[nome] = registrar_avaliacao(self.con, int(self.agora), "ao vivo", "g", f"{nome}|100", "BOA", 70,
                                                 False, {"ml": {"vendedor": vendedor, "oficial": True}})
        self.registrar_sinal = registrar_sinal

    def test_sem_confirmacao_na_loja_da_marca_nao_ha_foco(self):
        self.assertEqual(pipeline.cupom_da_marca_agora(self.cur), "")

    def test_confirmado_na_loja_da_marca_a_natura_vai_na_frente(self):
        self.registrar_sinal(self.con, "LOJASHOJE", int(self.agora) - 1800, "posto", "breno", "pôs à mão", "edicao:1",
                             "1957713361|natura")
        self.assertEqual(pipeline.cupom_da_marca_agora(self.cur), "LOJASHOJE")
        cards = [{"id": self.ids["natura"], "chave": "natura|100"}, {"id": self.ids["lipx"], "chave": "lipx|100"}]
        self.assertEqual(pipeline.da_loja_da_marca(self.con, cards), {id(cards[0])})
