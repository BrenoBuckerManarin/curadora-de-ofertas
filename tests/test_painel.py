"""API do painel, chamando as funções direto com um banco em memória (sem servidor, sem rede)."""
from __future__ import annotations

import time
import unittest

from apoio import CFG, banco_com_historico

import painel
from curadora.score import Curadora, para_dict
from curadora.banco import registrar_avaliacao

ASAD = [("Lattafa Asad 100ml", p, -d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197], start=3)]


class TestApiPainel(unittest.TestCase):
    def setUp(self):
        painel.ESTADO["sem_ml"] = True
        self.agora = int(time.time())
        hist = [(prod, preco, 0) for prod, preco, _ in ASAD]
        self.con = banco_com_historico(hist)
        self.con.execute("UPDATE ofertas SET ts = ? - (id * 86400)", (self.agora,))
        self.con.commit()
        self.cur = Curadora(CFG, self.con)

    def registrar(self, texto: str, grupo: str, ts: int):
        av = self.cur.avaliar(texto, agora=ts)
        av.grupo = grupo
        return registrar_avaliacao(self.con, ts, "ao vivo", grupo, av.identidade.chave, av.nota, av.score,
                                   False, para_dict(av))

    def test_mesma_oferta_em_varios_grupos_vira_um_card(self):
        texto = "*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a"
        self.registrar(texto, "Grupo A", self.agora - 120)
        self.registrar(texto.replace("150", "151"), "Grupo B", self.agora - 60)
        feed = painel.api_feed(self.con, {"secao": ["excelentes"]})
        self.assertEqual(len(feed), 1)
        self.assertEqual(sorted(feed[0]["grupos"]), ["Grupo A", "Grupo B"])
        self.assertEqual(painel.api_secoes(self.con, {})["excelentes"], 1)

    def test_produto_so_volta_se_a_nota_melhorar(self):
        # Pedido do Breno em 04/10: o mesmo produto só aparece de novo com score melhor.
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora - 300)  # excelente
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 240\nhttps://meli.la/b", "G", self.agora - 200)  # pior: some
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 190\nhttps://meli.la/c", "G", self.agora - 100)  # pior: some
        secoes = painel.api_secoes(self.con, {})
        self.assertEqual(sum(secoes.values()), 1)
        card = painel.api_feed(self.con, {"secao": ["excelentes"]})[0]
        self.assertEqual((card["preco"], card["vista_de_novo"]), (150, 2))
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 140\nhttps://meli.la/d", "G", self.agora)        # melhor: volta
        todas = painel.api_feed(self.con, {"secao": ["todas"]})
        self.assertEqual(len(todas), 1)
        self.assertEqual(todas[0]["preco"], 140)
        self.assertEqual(todas[0]["antes"]["preco"], 150)

    def test_postada_no_grupofinal_desce_some_da_sugestao_e_fica_marcada(self):
        from curadora.postagens import registrar_postagem
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora - 600)
        self.registrar("*Lattafa Yara 100ml*\nPor R$ 120\nhttps://meli.la/y", "G", self.agora - 300)
        s = painel.api_sugestao(self.con, {})
        self.assertIn("Asad", s["itens"][0]["produto"])          # a melhor não postada
        self.assertIsNone(s["ultimo_post"])
        # A equipe posta o Asad no GrupoFinal (outro link, preço parecido): sai da sugestão e fica marcado.
        registrar_postagem(self.con, self.cur, "*Lattafa Asad EDP 100ml*\nPor *R$ 149* no Pix\nhttps://meli.la/h",
                           self.agora - 60, "m1", "Grupo da Equipe")
        s = painel.api_sugestao(self.con, {})
        self.assertNotIn("Asad", s["itens"][0]["produto"])
        self.assertEqual(s["ultimo_post"]["ts"], self.agora - 60)
        todas = painel.api_feed(self.con, {"secao": ["todas"], "ordem": ["melhores"]})
        self.assertIn("Asad", todas[-1]["produto"])               # foi para o fim
        self.assertEqual(todas[-1]["postada_em"], {"ts": self.agora - 60, "grupo": "Grupo da Equipe"})

    def test_melhores_primeiro_na_secao_ruim(self):
        # Pedido do Breno em 04/10: postam a cada ~7 min; às vezes tiram das Ruins, então as melhores vêm antes.
        agora = time.time()
        cards = [
            {"id": 1, "nota": "NÃO VALE", "score": 30, "ts": agora - 60, "postada": 0},          # recente, fraca
            {"id": 2, "nota": "NÃO VALE", "score": 44, "ts": agora - 3600, "postada": 0},        # a melhor das ruins
            {"id": 3, "nota": "NÃO VALE", "score": 44, "ts": agora - 20 * 3600, "postada": 0},   # boa, mas velha
            {"id": 4, "nota": "NÃO VALE", "score": 50, "ts": agora - 120, "postada": agora},     # já postada
        ]
        self.assertEqual([c["id"] for c in sorted(cards, key=painel._ordem_melhores(agora))], [2, 1, 3, 4])
        original, completar = painel._cards, painel._completar
        try:
            painel._cards = lambda con: cards
            painel._completar = lambda con, c, grupofinal=None: c
            self.assertEqual([c["id"] for c in painel.api_feed(self.con, {"secao": ["ruins"], "ordem": ["melhores"]})],
                             [2, 1, 3, 4])
            self.assertEqual([c["id"] for c in painel.api_feed(self.con, {"secao": ["ruins"]})], [1, 2, 3, 4])
        finally:
            painel._cards, painel._completar = original, completar

    def test_reconferencia_que_subiu_tira_o_card_de_excelentes(self):
        import json
        from curadora.ml import CardML
        from curadora.reconferir import Reconferidor

        class MLFalso:
            preco = 150.0

            def consultar(self, link, agora=None):
                return CardML(ok=True, titulo="Perfume Lattafa Asad Eau De Parfum 100ml", preco=self.preco,
                              vendedor="Lipx", loja_oficial=True)
        ml = MLFalso()
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora - 3600)
        self.assertEqual(painel.api_secoes(self.con, {})["excelentes"], 1)
        ler = lambda i: json.loads(self.con.execute("SELECT dados FROM avaliacoes WHERE id=?", (i,)).fetchone()["dados"])
        rec = Reconferidor(Curadora(CFG, self.con, ml), lambda: painel._cards(self.con), ler)
        self.assertEqual(rec.rodada(), 0)                       # mesmo preço: só atualiza o "conferido há"
        card = painel.api_feed(self.con, {"secao": ["excelentes"]})[0]
        self.assertGreater(card["ts"], self.agora - 60)
        self.assertNotIn("era", card)
        self.con.execute("UPDATE avaliacoes SET ts = ts - 3600")  # passou o tempo
        self.con.commit()
        ml.preco = 240.0                                        # subiu no ML
        self.assertEqual(rec.rodada(), 1)
        secoes = painel.api_secoes(self.con, {})
        self.assertEqual((secoes["excelentes"], secoes["ruins"]), (0, 1))
        card = painel.api_feed(self.con, {"secao": ["ruins"]})[0]
        self.assertEqual((card["era"]["preco"], card["preco"]), (150, 240))

    def test_cada_nota_cai_na_sua_secao(self):
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 240\nhttps://meli.la/b", "G", self.agora - 60)  # acima do normal
        self.assertEqual(painel.api_secoes(self.con, {})["ruins"], 1)
        self.assertEqual(painel.api_feed(self.con, {"secao": ["ruins"]})[0]["nota"], "NÃO VALE")
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora)       # melhorou
        secoes = painel.api_secoes(self.con, {})
        self.assertEqual((secoes["excelentes"], secoes["ruins"]), (1, 0))   # sai de Ruins e vai para Excelentes

    def test_card_traz_o_texto_original_para_copiar(self):
        texto = "*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a"
        self.registrar(texto, "G", self.agora)
        self.assertEqual(painel.api_feed(self.con, {"secao": ["excelentes"]})[0]["texto"], texto)

    def test_avaliar_texto_colado_e_votar(self):
        r = painel.api_avaliar(self.con, {"texto": "*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "ml": False})
        self.assertIn(r["nota"], {"IMPERDÍVEL", "BOA", "OK", "NÃO VALE"})
        self.assertEqual(painel.api_voto(self.con, {"id": r["id"], "voto": 1}), {"ok": True})
        secao = {v: k for k, v in painel.SECOES.items()}[r["nota"]]
        self.assertEqual(painel.api_feed(self.con, {"secao": [secao]})[0]["voto"], 1)
        self.assertEqual(painel.api_status(self.con, {})["votos_bons"], 1)

    def test_tudo_numa_resposta_so(self):
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora)
        d = painel.api_painel(self.con, {"secao": ["excelentes"]})
        self.assertEqual(set(d), {"status", "secoes", "feed", "sugestao"})
        self.assertEqual(len(d["feed"]), d["secoes"]["excelentes"])

    def test_copiar_nao_conta_e_repeticao_conta_uma_vez(self):
        # Visto na VPS em 03/10: copiar contava como 👍 e uma cópia marcava 7 repetições da mesma oferta.
        texto = "*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a"
        ids = [self.registrar(texto, "vigia", self.agora - n * 600) for n in range(7)]
        painel.api_postada(self.con, {"ids": ids})
        self.assertEqual(painel.api_status(self.con, {})["votos_bons"], 0)
        for i in ids:
            painel.api_voto(self.con, {"id": i, "voto": 1})
        self.assertEqual(painel.api_status(self.con, {})["votos_bons"], 1)

    def test_busca_em_todas_as_secoes_e_quando_apareceu(self):
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "G", self.agora - 3600)   # excelente
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", "vigia", self.agora)      # reconferida
        self.registrar("*Lattafa Asad 100ml*\nPor R$ 240\nhttps://meli.la/b", "G", self.agora)          # pior: some
        todas = painel.api_feed(self.con, {"secao": ["todas"]})
        self.assertEqual([i["nota"] for i in todas], ["IMPERDÍVEL"])
        juntada = next(i for i in todas if i["nota"] == "IMPERDÍVEL")
        self.assertEqual(juntada["primeiro_ts"], self.agora - 3600)  # apareceu há 1h, mesmo reconferida agora
        self.assertEqual(juntada["ts"], self.agora)

    def test_calibracao_pede_mais_votos_no_comeco(self):
        r = painel.api_calibracao(self.con, {})
        self.assertFalse(r["pronto"])
        self.assertEqual(r["minimo"], 20)
        self.assertIn("erro", painel.api_calibracao_aplicar(self.con, {}))

    def test_config_novo_vale_na_hora_sem_reiniciar(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from curadora import carregar_config, salvar_config
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "config.json"
            salvar_config(CFG, arquivo)
            cur = Curadora(carregar_config(arquivo), self.con)
            novo = json.loads(arquivo.read_text(encoding="utf-8"))
            novo["pesos"]["top_vendido"] = 99
            arquivo.write_text(json.dumps(novo), encoding="utf-8")
            os.utime(arquivo, (time.time() + 5, time.time() + 5))  # garante mtime diferente
            cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", agora=self.agora)
            self.assertEqual(cur.pesos["top_vendido"], 99)
            self.assertNotIn("_arquivo", json.loads(arquivo.read_text(encoding="utf-8")))

    def test_texto_que_nao_e_promo(self):
        self.assertIn("erro", painel.api_avaliar(self.con, {"texto": "bom dia pessoal"}))
        self.assertIn("erro", painel.api_avaliar(self.con, {"texto": ""}))

    def test_voto_invalido(self):
        self.assertIn("erro", painel.api_voto(self.con, {"id": 1, "voto": 5}))

    def test_historico_do_produto(self):
        chave = self.cur.chave_de("Lattafa Asad 100ml")
        pontos = painel.api_historico(self.con, {"chave": [chave]})["pontos"]
        self.assertEqual(len(pontos), len(ASAD))
        self.assertTrue(all(p["tipo"] == "sem_cupom" for p in pontos))


class TestTravadas(unittest.TestCase):
    """21/10 (Breno: "sim pros 2"): a Excelente que o pipeline não mandaria sai da seção, com o motivo no card."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from curadora.banco import conectar
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        for nome, arquivo in (("TRAVAS_PIPELINE", "pipeline_travas.json"), ("REGISTRO_PIPELINE", "pipeline.jsonl")):
            self.addCleanup(setattr, painel, nome, getattr(painel, nome))
            setattr(painel, nome, Path(pasta.name) / arquivo)
        painel._cache_cards.clear()
        painel._cache_travas.clear()

    def card(self, chave, preco=100.0, postada=0, nota="IMPERDÍVEL", score=85, **extra):
        import json
        dados = {"preco": preco, "produto": chave.split("|")[0].title(), "link": "https://meli.la/x", **extra}
        self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados, postada) "
                         "VALUES (?,?,?,?,?,?,?,?)", (self.agora - 300, "ao vivo", "g", chave, nota, score,
                                                      json.dumps(dados), postada))
        self.con.commit()

    def feed(self):
        painel._cache_cards.clear()
        painel._cache_travas.clear()
        return [(c["chave"], c.get("trava")) for c in painel.api_feed(self.con, {"secao": ["excelentes"]})]

    def test_card_com_chave_antiga_junta_com_o_novo_quando_reconferido(self):
        # 25/10: o Bareeq de 23/10 nasceu "bareeq-dhahab-parf" e seguia ao lado do "bareeq-dhahab" (mesmo preço)
        import json
        self.card("bareeq-dhahab-parf|100", preco=125.89, score=93)
        velho = self.con.execute("SELECT max(id) FROM avaliacoes").fetchone()[0]
        self.card("bareeq-dhahab|100", preco=125.89, score=93, postada=self.agora - 3600)
        self.assertEqual(sorted(k for k, _ in self.feed()), ["bareeq-dhahab-parf|100", "bareeq-dhahab|100"])
        self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                         (self.agora - 60, "reconferida", "", "bareeq-dhahab|100", "IMPERDÍVEL", 93,
                          json.dumps({"preco": 125.89, "reconferida_de": velho, "link": "https://meli.la/x"})))
        self.con.commit()
        self.assertEqual([k for k, _ in self.feed()], ["bareeq-dhahab|100"])
        card = [c for c in painel._cards(self.con) if c["chave"] == "bareeq-dhahab|100"][0]
        self.assertTrue(card["postada"])          # vale o "postada" do card novo

    def test_mesmo_anuncio_com_outro_nome_vira_um_card_so(self):
        # 25/10: "Natura Kaiak Tradicional" (kaiak|100) e "Kaiak Masculino Tradicional" (kaiak|100|m), mesmo anúncio OUD
        url = "https://www.mercadolivre.com.br/kaiak/up/MLBU2978815028"
        self.card("kaiak|100", preco=93.41, score=81, ml={"url": url + "?x=1"})
        self.card("kaiak|100|m", preco=93.41, score=88, ml={"url": url})
        self.card("kaiak|100|f", preco=95.15, score=82, ml={"url": "https://www.mercadolivre.com.br/kaiak-fem/up/MLBU1"})
        self.assertEqual(sorted(k for k, _ in self.feed()), ["kaiak|100", "kaiak|100|f"])
        self.card("kaiak|100|m", preco=79.90, score=90, ml={"url": url})   # outro preço: é outra oferta
        self.assertEqual(sorted(k for k, _ in self.feed()), ["kaiak|100", "kaiak|100|f", "kaiak|100|m"])

    def test_card_com_chave_antiga_e_sem_card_novo_so_troca_a_chave(self):
        import json
        self.card("elixir-lipx-yara|100", preco=188.91, score=83)
        velho = self.con.execute("SELECT max(id) FROM avaliacoes").fetchone()[0]
        self.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                         (self.agora - 60, "reconferida", "", "elixir-yara|100", "IMPERDÍVEL", 83,
                          json.dumps({"preco": 188.91, "reconferida_de": velho, "link": "https://meli.la/x"})))
        self.con.commit()
        self.assertEqual([k for k, _ in self.feed()], ["elixir-yara|100"])

    def test_vendedor_com_poucas_vendas_vai_para_o_fim_e_nao_conta_na_aba(self):
        self.card("travada|100", ml={"oficial": False}, reputacao={"vendas": 1163, "classe": "boa"})
        self.card("livre|100", ml={"oficial": True}, reputacao={"vendas": 300})        # loja oficial passa
        self.card("sem-dado|100")                                                       # sem dado: não afirma nada
        feed = self.feed()
        self.assertEqual(feed[-1], ("travada|100", "vendedor com 1,2 mil vendas"))
        self.assertEqual({k for k, t in feed if not t}, {"livre|100", "sem-dado|100"})
        self.assertEqual(painel.api_secoes(self.con, {})["excelentes"], 2)
        self.assertNotIn("travada|100", [c["chave"] for c in painel.api_sugestao(self.con, {})["itens"]])

    def test_motivo_do_pipeline_vale_no_mesmo_preco(self):
        from curadora import travas
        self.card("cupom|100", preco=149.0)
        travas.guardar(painel.TRAVAS_PIPELINE, "cupom|100", "sem o cupom 40SITETODO não vale", 149.0)
        self.assertEqual(self.feed(), [("cupom|100", "depende do cupom 40SITETODO")])
        travas.guardar(painel.TRAVAS_PIPELINE, "cupom|100", "sem o cupom 40SITETODO não vale", 180.0)
        self.assertEqual(self.feed(), [("cupom|100", None)])          # o preço mudou: vale conferir de novo

    def test_post_da_equipe_trava_6h_e_o_do_pipeline_8h(self):
        import json
        self.card("equipe|100", postada=self.agora - 2 * 3600)
        self.card("equipe-velho|100", postada=self.agora - 7 * 3600)
        self.card("pipeline|100", postada=self.agora - 7 * 3600)
        painel.REGISTRO_PIPELINE.write_text(json.dumps({"ts": self.agora - 7 * 3600 - 900, "status": "enviado",
                                                        "chave": "pipeline|100", "preco": 100.0}) + "\n",
                                            encoding="utf-8")
        travadas = dict(self.feed())
        self.assertTrue(travadas["equipe|100"].startswith("a equipe postou"))
        self.assertIsNone(travadas["equipe-velho|100"])                # passou das 6h: volta
        self.assertTrue(travadas["pipeline|100"].startswith("o pipeline mandou"))


if __name__ == "__main__":
    unittest.main()


class TestPrecoDeCostume(unittest.TestCase):
    """25/10 (Breno): o card mostra por quanto o produto costuma sair nos grupos (só informação)."""

    def setUp(self):
        from curadora.banco import conectar
        import painel
        self.painel = painel
        self.con = conectar(":memory:")
        self.agora = 1_792_000_000
        painel._cache_costume.clear()
        self.addCleanup(painel._cache_costume.clear)

    def oferta(self, dias_atras, preco, chave="kaiak|100|m", n=[0]):
        n[0] += 1
        self.con.execute("INSERT INTO ofertas (msg_id, ts, produto, base, chave, preco) VALUES (?,?,?,?,?,?)",
                         (f"m{n[0]}", self.agora - dias_atras * 86400 - 3600, "Kaiak", "kaiak", chave, preco))

    def test_mediana_do_menor_preco_de_cada_dia(self):
        for dia, precos in enumerate([(89, 99), (87, 115), (85,), (88, 92), (90,), (86, 300)]):
            for p in precos:
                self.oferta(dia + 1, p)
        self.oferta(3, 9.9)   # erro de leitura: fica fora
        self.assertEqual(self.painel.preco_de_costume(self.con, "kaiak|100|m", self.agora), 87.5)

    def test_com_menos_de_5_dias_nao_mostra(self):
        for dia in range(4):
            self.oferta(dia + 1, 90)
        self.assertIsNone(self.painel.preco_de_costume(self.con, "kaiak|100|m", self.agora))

    def test_so_30_dias(self):
        for dia in (1, 2, 3, 4, 40):
            self.oferta(dia, 90)
        self.assertIsNone(self.painel.preco_de_costume(self.con, "kaiak|100|m", self.agora))
