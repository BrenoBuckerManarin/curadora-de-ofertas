"""31/10 (Breno: "loja oficial vence revenda"). Malbec Bleu, 16:58: foi a revenda PASSOSBARBARA a R$180 e, no mesmo
produto de catálogo do ML, a loja oficial O Boticário vendia a R$167,93."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apoio import conectar, CFG, MLFalso
import pipeline
from curadora.ml import CardML
from curadora.ml_api import OfertaML
from curadora.score import Curadora, oferta_oficial_melhor, resumo_do_catalogo, url_da_oferta

PRODUTO = "MLB19870"
REVENDA, OFICIAL = "MLB5550001", "MLB5550002"
TITULO = "Perfume Malbec Bleu Desodorante Colônia 100ml O Boticário"
TEXTO = "*Malbec Bleu 100ml*\n\nDe R$ 249,90 por R$ 180\n\nhttps://meli.la/KzdtJO"
URL_OFICIAL = url_da_oferta(PRODUTO, OFICIAL)
ANUNCIO_OFICIAL = f"https://produto.mercadolivre.com.br/MLB-{OFICIAL[3:]}"
# 01/11: o meli.la que a API do gerador devolve para o anúncio da oficial (abre a página social com o card dela)
LINK_OFICIAL, LINK_REVENDA = "https://meli.la/2u8zTHA", "https://meli.la/fmxQ5Uo"
REP = {1: {"nome": "PASSOSBARBARA", "vendas": 145000, "classe": "otima", "medalha": "platinum", "nivel": "5_green",
           "pais": "BR"},
       2: {"nome": "O Boticário", "vendas": 2000000, "classe": "otima", "medalha": "", "nivel": "5_green", "pais": "BR"}}


class APIFalsa:
    """A API do ML: ofertas do catálogo, reputação (consulta) e reputação já guardada (sem consulta)."""

    def __init__(self, ofertas, reputacoes, guardadas):
        self._ofertas, self.reputacoes, self.guardadas = ofertas, reputacoes, guardadas
        self.consultados: list[int] = []

    def configurado(self):
        return True

    def ofertas(self, produto_id, limite=50):
        return list(self._ofertas)

    def vendedor(self, seller_id):
        self.consultados.append(seller_id)
        return dict(self.reputacoes.get(seller_id) or {})

    def vendedor_guardado(self, seller_id):
        return dict(self.guardadas.get(seller_id) or {})


def oferta(item_id, preco, seller_id, oficial, internacional=False):
    return OfertaML(item_id=item_id, preco=preco, preco_anterior=249.9, seller_id=seller_id, loja_oficial=oficial,
                    frete_gratis=True, internacional=internacional)


class TestResumoDoCatalogo(unittest.TestCase):
    def test_guarda_anuncio_vendedor_e_loja_oficial(self):
        ofertas = [oferta(OFICIAL, 167.93, 2, True), oferta(REVENDA, 180.0, 1, False), oferta("MLB3", 185.0, 3, False)]
        r = resumo_do_catalogo(ofertas, ofertas[1], {2: REP[2]}.get)
        self.assertEqual((r["segundo"], r["oficial"], r["vendedores"]), (167.93, 167.93, 3))   # os preços como antes
        self.assertEqual(r["oficial_oferta"]["item_id"], OFICIAL)
        self.assertEqual((r["oficial_oferta"]["seller_id"], r["oficial_oferta"]["nome"]), (2, "O Boticário"))
        self.assertEqual(r["oficial_oferta"]["vendas"], 2000000)
        self.assertTrue(r["oficial_oferta"]["loja_oficial"])
        # sem a reputação guardada: o anúncio e o vendedor ficam, a reputação não (nenhuma consulta a mais)
        r = resumo_do_catalogo(ofertas, ofertas[0], {}.get)
        self.assertEqual(r["segundo_oferta"]["item_id"], REVENDA)
        self.assertNotIn("vendas", r["segundo_oferta"])
        self.assertIsNone(r["oficial_oferta"])

    def test_oficial_valida_e_a_mais_barata_entre_as_que_valem(self):
        """01/11: a oficial mais barata era de fora e a 2ª só da API (804 vendas); vale a 3ª. "oficial" (o preço)
        continua o da mais barata, qualquer uma."""
        ofertas = [oferta("MLB777", 150.0, 5, True, internacional=True), oferta("MLB778", 160.0, 6, True),
                   oferta(OFICIAL, 167.93, 2, True), oferta(REVENDA, 180.0, 1, False)]
        r = resumo_do_catalogo(ofertas, ofertas[3], {2: REP[2], 6: {"nome": "PERFUMELATTAFA7", "vendas": 804}}.get)
        self.assertEqual(r["oficial"], 150.0)
        self.assertEqual(r["oficial_oferta"]["item_id"], OFICIAL)
        # nenhuma oficial válida: sem oficial_oferta, mas o preço da mais barata fica
        r = resumo_do_catalogo(ofertas[:2] + ofertas[3:], ofertas[3], {6: {"vendas": 804}}.get)
        self.assertEqual((r["oficial"], r["oficial_oferta"]), (150.0, None))

    def test_catalogo_no_formato_antigo_nao_quebra(self):
        """01/11: avaliação guardada antes de 31/10 (só vendedores, segundo e oficial)."""
        card = CardML(ok=True, item_id=REVENDA, produto_id=PRODUTO, titulo=TITULO, preco=180.0,
                      catalogo={"vendedores": 3, "segundo": 120.0, "oficial": 100.0})
        self.assertIsNone(oferta_oficial_melhor(card))
        card.catalogo = None
        self.assertIsNone(oferta_oficial_melhor(card))

    def test_url_do_anuncio_na_pagina_do_produto(self):
        self.assertEqual(URL_OFICIAL, f"https://www.mercadolivre.com.br/p/{PRODUTO}?pdp_filters=item_id:{OFICIAL}")
        self.assertEqual(url_da_oferta("", "MLB-77"), "https://produto.mercadolivre.com.br/MLB-77")


class TestSellerIdNoRegistroDePrecos(unittest.TestCase):
    def test_radar_grava_quem_vende_cada_preco(self):
        import test_radar as tr
        from curadora.alertas import Alertador
        from curadora.radar import Radar
        base = tr.Base()
        base.setUp()
        self.addCleanup(base.tearDown)
        cur = Curadora(CFG, tr.banco_com_historico(tr.ASAD_NORMAL))
        Radar(cur, base.cliente, Alertador(cur.con, [lambda t, d: None]), nomes=["Lattafa Asad"],
              relogio=lambda: tr.AGORA + 60).rodada()
        linhas = {(r["item_id"], r["seller_id"]) for r in cur.con.execute("SELECT item_id, seller_id FROM precos_ml")}
        self.assertEqual(linhas, {("MLB10", 7), ("MLB11", 8), ("MLB12", 9)})

    def test_preco_da_pagina_sem_vendedor_fica_vazio(self):
        from curadora.banco import registrar_preco_ml
        con = conectar(":memory:")
        registrar_preco_ml(con, int(time.time()), "x|100", CardML(ok=True, item_id="MLB1", titulo="X", preco=10.0), "destaque")
        self.assertIsNone(con.execute("SELECT seller_id FROM precos_ml").fetchone()[0])


class TestRadarNoEmpate(unittest.TestCase):
    def test_empate_de_preco_a_candidata_e_a_loja_oficial(self):
        """01/11: revenda e loja oficial ao mesmo preço; a API devolve a revenda antes. Vai a oficial."""
        import test_radar as tr
        from curadora.alertas import Alertador
        from curadora.radar import Radar
        base = tr.Base()
        base.setUp()
        self.addCleanup(base.tearDown)
        base.api.precos = (189.9, 189.9, 210.0)   # MLB11 (revenda) e MLB10 (oficial, LIPX) empatam
        cur = Curadora(CFG, tr.banco_com_historico(tr.ASAD_NORMAL))
        radar = Radar(cur, base.cliente, Alertador(cur.con, [lambda t, d: None]), nomes=["Lattafa Asad"],
                      relogio=lambda: tr.AGORA + 60)
        originais = radar.cliente.ofertas
        radar.cliente.ofertas = lambda produto_id, limite=50: sorted(originais(produto_id, limite),
                                                                    key=lambda o: (o.preco, o.loja_oficial))
        self.assertFalse(radar.cliente.ofertas("MLB1")[0].loja_oficial)   # a revenda vem primeiro
        radar.rodada()
        lojas = [json.loads(r["dados"])["loja"] for r in cur.con.execute(
            "SELECT dados FROM avaliacoes WHERE origem='radar do ML'")]
        self.assertEqual(lojas, ["Lipx"])   # uma candidata só: a oficial é a mais barata e a oficial


class TestLojaOficialVenceRevenda(unittest.TestCase):
    def setUp(self):
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        self.logs: list[str] = []
        self.addCleanup(setattr, pipeline, "log", pipeline.log)
        pipeline.log = self.logs.append
        # 01/11: a trava de 8 h lê o registro do pipeline; nos testes, um arquivo temporário
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.addCleanup(setattr, pipeline, "REGISTRO", pipeline.REGISTRO)
        pipeline.REGISTRO = Path(pasta.name) / "pipeline.jsonl"
        # a API do gerador: o anúncio da oficial vira LINK_OFICIAL (o MLFalso abre o card dela); o resto, LINK_REVENDA
        self.gerados: list[str] = []
        self.addCleanup(setattr, pipeline, "gerar_pela_api", pipeline.gerar_pela_api)
        pipeline.gerar_pela_api = self.gerar

    def gerar(self, texto):
        self.gerados.append(texto)
        link = LINK_OFICIAL if ANUNCIO_OFICIAL in texto else LINK_REVENDA
        return {"body": pipeline.LINK.sub(link, texto), "link": link}

    def curadora(self, preco_revenda=180.0, preco_oficial=167.93, guardadas=None, reputacoes=None, pagina=None,
                 extras=()):
        """A revenda no link do grupo; a loja oficial no mesmo produto de catálogo (preço da API e da página)."""
        ofertas = sorted([oferta(OFICIAL, preco_oficial, 2, True), oferta(REVENDA, preco_revenda, 1, False), *extras],
                         key=lambda o: o.preco)
        self.api = APIFalsa(ofertas, REP if reputacoes is None else reputacoes, {2: REP[2]} if guardadas is None else guardadas)
        revenda = CardML(ok=True, item_id=REVENDA, produto_id=PRODUTO, titulo=TITULO, preco=preco_revenda,
                         vendedor="PASSOSBARBARA")
        oficial = CardML(ok=True, item_id=OFICIAL, produto_id=PRODUTO, titulo=TITULO, preco=preco_oficial,
                         vendedor="O Boticário", loja_oficial=True)
        if pagina:
            for campo, valor in pagina.items():
                setattr(oficial, campo, valor)
        self.ml = MLFalso({"https://meli.la/KzdtJO": revenda, LINK_REVENDA: revenda, LINK_OFICIAL: oficial,
                           URL_OFICIAL: oficial})
        cur = Curadora({**CFG, "cupom_quente": {"ligado": False}}, self.con, self.ml, self.api)
        if not self.con.execute("SELECT 1 FROM ofertas LIMIT 1").fetchone():
            chave = cur.chave_de("Malbec Bleu 100ml")
            for n, dias in enumerate(range(2, 40), 1):
                self.con.execute("INSERT INTO ofertas(msg_id,ts,chave,preco,cupons,produto,base) "
                                 "VALUES (?,?,?,?,'[]','Malbec Bleu','malbec bleu')",
                                 (str(n), self.agora - dias * 86400, chave, 239.9))
        return cur

    def revenda(self, cur):
        av = cur.avaliar(TEXTO, agora=self.agora)
        self.assertTrue(pipeline.aceitavel(av), (av.nota, av.score, av.alertas))
        return av

    def troca(self, cur, av, texto=TEXTO, sem_cupom=True, simular=False):
        return pipeline.pela_loja_oficial(self.con, cur, av, texto, sem_cupom=sem_cupom, simular=simular)

    def test_malbec_vai_a_loja_oficial(self):
        cur = self.curadora()
        av = self.revenda(cur)
        self.assertEqual(av.oferta_oficial["item_id"], OFICIAL)
        alerta = next(a for a in av.alertas if "loja oficial" in a)
        self.assertEqual(alerta, "loja oficial O Boticário a R$167,93 no mesmo produto (este é revenda)")
        self.assertLessEqual(len(alerta), 120)   # 01/11: o grupo da equipe corta em 120
        troca = self.troca(cur, av)
        self.assertIsNotNone(troca, self.logs)
        av2, texto, resposta = troca
        self.assertEqual((av2.card.item_id, av2.card.preco), (OFICIAL, 167.93))
        self.assertIn("R$ 167,93", texto)
        self.assertIn(ANUNCIO_OFICIAL, texto)
        self.assertNotIn("meli.la/KzdtJO", texto)
        self.assertEqual(av2.no_lugar_de["item_id"], REVENDA)
        self.assertIn("LOJA OFICIAL no lugar da revenda", self.logs[-1])
        # a conferência abriu o meli.la que a API do gerador fez para o anúncio dela, e é esse link que volta
        self.assertEqual(len(self.gerados), 1)
        self.assertIn(ANUNCIO_OFICIAL, self.gerados[0])
        self.assertEqual(self.ml.consultas[-1], LINK_OFICIAL)
        self.assertEqual(resposta["link"], LINK_OFICIAL)
        self.assertIn(LINK_OFICIAL, resposta["body"])
        self.assertIn("167,93", resposta["body"])
        self.assertNotIn(URL_OFICIAL, self.ml.consultas)

    def test_sem_a_api_do_gerador_nao_troca_nem_abre_o_ml(self):
        """01/11: sem o link de afiliado, a página /p/...?pdp_filters não tem o card em destaque: não consulta."""
        cur = self.curadora()
        av = self.revenda(cur)
        antes = list(self.ml.consultas)
        pipeline.gerar_pela_api = lambda texto: self.gerados.append(texto)   # devolve None
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("sem o link de afiliado do anúncio dela", self.logs[-1])
        self.assertEqual(self.ml.consultas, antes)
        pipeline.gerar_pela_api = lambda texto: {"body": texto, "link": ""}   # resposta sem link
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("sem o link de afiliado do anúncio dela", self.logs[-1])
        self.assertEqual(self.ml.consultas, antes)

    def test_simular_so_diz_o_que_faria(self):
        cur = self.curadora()
        av = self.revenda(cur)
        antes = list(self.ml.consultas)
        self.assertIsNone(self.troca(cur, av, simular=True))
        self.assertIn("trocaria pela oficial O Boticário a R$167.93 (não conferido)", self.logs[-1])
        self.assertEqual((self.gerados, self.ml.consultas), ([], antes))

    def enviar(self, cur, av):
        """uma_promo do começo ao fim, com o conferir e o envio falsos. Devolve (volta, saída)."""
        card = {"chave": av.identidade.chave, "id": 1, "preco": 180.0, "produto": "Malbec Bleu 100ml"}
        saida = {"registrar": [], "normalizadora": [], "fotos": [], "vitrine": []}
        trocas = dict(
            candidatas=lambda con, cur, excluir: ([], [card]), misturar=lambda lista, *a, **k: lista,
            pela_pagina=lambda con, cur, c, simular: c, conferir=lambda con, cur, c: (av, {"texto": TEXTO}),
            com_cupom_da_equipe=lambda cur, av, texto: None, cupons_que_ficam=lambda cur, av: [],
            sombra_das_candidatas=lambda *a, **k: None, sombra_da_ficha=lambda *a, **k: None,
            texto_da_api=lambda body, produto: body, produtos_com_foto=lambda cur: set(),
            foto_do_grupo=lambda con, chave, link, produto="": saida["fotos"].append(link) or "",
            baixar_foto=lambda *a, **k: "",
            para_a_normalizadora=lambda corpo, n, foto="": saida["normalizadora"].append(corpo) or "mid1",
            marcar_feita=lambda *a: None, marcar_mesmo_anuncio=lambda *a: None,
            registrar=lambda d: saida["registrar"].append(dict(d)),
            para_a_vitrine=lambda *a, **k: saida["vitrine"].append(k.get("link_do_grupo")),
            marca=lambda cur, p: "O Boticário")
        with mock.patch.multiple(pipeline, **trocas), mock.patch.object(pipeline.travas, "guardar"):
            volta = pipeline.uma_promo(self.con, cur, set(), "", 1, False)
        return volta, saida

    def test_malbec_pelo_envio_inteiro(self):
        """uma_promo: o link, o preço do texto e o registro são os da loja oficial; o link enviado é o conferido."""
        cur = self.curadora()
        volta, saida = self.enviar(cur, self.revenda(cur))
        self.assertEqual(volta["status"], "enviado")
        self.assertEqual(self.ml.consultas[-1], LINK_OFICIAL)
        self.assertEqual(len(self.gerados), 1)   # 01/11: a conferência; o envio usa o mesmo link
        self.assertIn(ANUNCIO_OFICIAL, self.gerados[0])
        self.assertIn("167,93", saida["normalizadora"][0])
        self.assertIn(LINK_OFICIAL, saida["normalizadora"][0])
        registro = saida["registrar"][-1]
        self.assertEqual(registro["preco"], 167.93)
        self.assertIn(OFICIAL, registro["ml_ids"])
        self.assertNotIn(REVENDA, registro["ml_ids"])
        self.assertEqual(registro["no_lugar_de"], {"item_id": REVENDA, "vendedor": "PASSOSBARBARA", "preco": 180.0})
        self.assertEqual(registro["preco_calculado"], 167.93)
        # 01/11: o que veio do grupo continua o do grupo; o link de afiliado é o que foi conferido
        self.assertEqual((registro["preco_anunciado"], registro["link_origem"]), (180.0, "https://meli.la/KzdtJO"))
        self.assertEqual(registro["link_afiliado"], LINK_OFICIAL)
        self.assertEqual(saida["fotos"], ["https://meli.la/KzdtJO"])    # a foto do post do grupo
        self.assertEqual(saida["vitrine"], ["https://meli.la/KzdtJO"])  # e a do grupo da equipe

    def test_oficial_falha_no_envio_vai_a_revenda(self):
        """01/11: a oficial mais cara na página ou que não abre: registro e texto da revenda, sem no_lugar_de."""
        for caso in ("mais cara", "não abre"):
            self.gerados.clear()
            cur = self.curadora(pagina={"preco": 185.0} if caso == "mais cara" else None)
            if caso == "não abre":
                del self.ml.cards[LINK_OFICIAL]
            volta, saida = self.enviar(cur, self.revenda(cur))
            self.assertEqual(volta["status"], "enviado", caso)
            registro = saida["registrar"][-1]
            self.assertNotIn("no_lugar_de", registro, caso)
            self.assertEqual(registro["preco"], 180.0, caso)
            self.assertIn(REVENDA, registro["ml_ids"], caso)
            self.assertNotIn(OFICIAL, registro["ml_ids"], caso)
            self.assertEqual(registro["link_afiliado"], LINK_REVENDA, caso)
            self.assertIn(LINK_REVENDA, saida["normalizadora"][0], caso)
            self.assertNotIn("167,93", saida["normalizadora"][0], caso)
            # a conferência da oficial e 1 só chamada para o envio da revenda
            self.assertEqual(len(self.gerados), 2, caso)
            self.assertIn(ANUNCIO_OFICIAL, self.gerados[0], caso)
            self.assertEqual(sum(ANUNCIO_OFICIAL not in t for t in self.gerados), 1, caso)

    def test_revenda_mais_barata_que_a_oficial_nao_troca(self):
        cur = self.curadora(preco_revenda=165.0, preco_oficial=167.93)
        av = cur.avaliar(TEXTO.replace("R$ 180", "R$ 165"), agora=self.agora)
        self.assertIsNone(av.oferta_oficial)
        self.assertIsNone(self.troca(cur, av))
        self.assertNotIn(LINK_OFICIAL, self.ml.consultas)
        self.assertEqual(self.gerados, [])

    def test_oficial_mais_cara_na_pagina_fica_a_revenda(self):
        cur = self.curadora(pagina={"preco": 185.0})   # a API dizia 167,93; a página, agora, 185
        av = self.revenda(cur)
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("mais que a revenda", self.logs[-1])

    def test_oficial_que_nao_abre_segue_a_original(self):
        cur = self.curadora()
        av = self.revenda(cur)
        del self.ml.cards[LINK_OFICIAL]
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("não abriu no ML", self.logs[-1])
        self.ml.consultar = mock.Mock(side_effect=TimeoutError())
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("erro ao abrir no ML (TimeoutError)", self.logs[-1])

    def test_banco_travado_sobe(self):
        """01/11: sqlite3.OperationalError não é engolido (o rodar() faz o rollback)."""
        import sqlite3
        cur = self.curadora()
        av = self.revenda(cur)
        self.ml.consultar = mock.Mock(side_effect=sqlite3.OperationalError("database is locked"))
        with self.assertRaises(sqlite3.OperationalError):
            self.troca(cur, av)

    def test_meli_la_da_api_abre_outro_anuncio_ou_outro_produto_segue_a_original(self):
        """O link de afiliado do anúncio dela abriu outro anúncio (ou outro produto): recua."""
        for pagina, motivo in (({"item_id": "MLB999"}, "outro anúncio"), ({"titulo": "Perfume Malbec Gold 100ml"},
                                                                          "outro produto")):
            cur = self.curadora(pagina=pagina)
            av = self.revenda(cur)
            self.assertIsNone(self.troca(cur, av), pagina)
            self.assertIn(motivo, self.logs[-1])
            self.assertEqual(self.ml.consultas[-1], LINK_OFICIAL)

    def test_trava_de_8h_tambem_no_anuncio_da_oficial(self):
        """01/11: o anúncio da oficial foi há 1 h a R$167,93: não troca. A R$200 (agora 8% ou mais abaixo), troca."""
        cur = self.curadora()
        av = self.revenda(cur)
        for preco, troca in ((167.93, False), (200.0, True)):
            pipeline.REGISTRO.write_text(json.dumps({"ts": time.time() - 3600, "status": "enviado", "ml_ids": [OFICIAL],
                                                     "preco": preco}) + "\n", encoding="utf-8")
            if troca:
                self.assertIsNotNone(self.troca(cur, av), self.logs)
            else:
                self.assertIsNone(self.troca(cur, av))
                self.assertIn("o anúncio dela já foi há menos de 8h a R$167.93", self.logs[-1])

    def test_duas_oficiais_a_primeira_de_fora_vale_a_segunda(self):
        """01/11: a oficial mais barata é internacional e a 2ª só da API com 804 vendas; a 3ª, válida e mais barata
        que a revenda, é a que entra no alerta e na troca."""
        guardadas = {2: REP[2], 6: {"nome": "PERFUMELATTAFA7", "vendas": 804}}
        cur = self.curadora(guardadas=guardadas, extras=(oferta("MLB777", 150.0, 5, True, internacional=True),
                                                         oferta("MLB778", 160.0, 6, True)))
        av = cur.avaliar(TEXTO, agora=self.agora)
        self.assertEqual(av.card.catalogo["oficial"], 150.0)
        self.assertEqual(av.oferta_oficial["item_id"], OFICIAL)
        self.assertTrue(pipeline.aceitavel(av), (av.nota, av.score, av.alertas))
        troca = self.troca(cur, av)
        self.assertIsNotNone(troca, self.logs)
        self.assertEqual(troca[0].card.item_id, OFICIAL)

    def test_texto_ainda_com_cupom_nao_troca(self):
        """01/11: se a avaliação dela ainda acha cupom no texto, não troca (o cupom seria da loja da revenda)."""
        cur = self.curadora()
        av = self.revenda(cur)
        com_cupom = TEXTO.replace("\n\nhttps", "\n\nUse o cupom MALBEC10\n\nhttps")
        self.assertIsNone(self.troca(cur, av, texto=com_cupom))
        self.assertIn("o texto ainda tem cupom", self.logs[-1])

    def test_com_cupom_no_texto_nao_troca(self):
        cur = self.curadora()
        av = self.revenda(cur)
        self.assertIsNone(self.troca(cur, av, sem_cupom=False))
        self.assertIn("cupom é da loja da revenda", self.logs[-1])
        self.assertNotIn(LINK_OFICIAL, self.ml.consultas)

    def test_escolhida_ja_oficial_nada_muda(self):
        cur = self.curadora()
        av = cur.avaliar(TEXTO.replace("https://meli.la/KzdtJO", URL_OFICIAL).replace("R$ 180", "R$ 167,93"),
                         agora=self.agora)
        self.assertTrue(av.card.loja_oficial)
        self.assertIsNone(av.oferta_oficial)
        self.assertIsNone(self.troca(cur, av))
        self.assertEqual(self.logs, [])

    def test_selo_so_da_api_sem_vendas_conhecidas_nao_troca_sozinho(self):
        """Sem a reputação guardada, a oferta aparece no alerta mas só troca com o selo na página E 5 mil vendas
        conferidas; se a API não diz as vendas, vai a revenda."""
        cur = self.curadora(guardadas={}, reputacoes={1: REP[1]})
        av = self.revenda(cur)
        self.assertFalse(av.oferta_oficial["vendas_conhecidas"])
        self.assertTrue(any(a.endswith("(este é revenda); selo só pela API") for a in av.alertas), av.alertas)
        self.assertIsNone(self.troca(cur, av))
        self.assertIn("vendas não conferidas", self.logs[-1])

    def test_selo_so_da_api_com_poucas_vendas_ou_sem_selo_na_pagina_nao_troca(self):
        # vendas já guardadas e abaixo de 5 mil: nem vira oferta (21/10, PERFUMELATTAFA7)
        poucas = dict(REP[2], vendas=804)
        cur = self.curadora(guardadas={2: poucas}, reputacoes={1: REP[1], 2: poucas})
        self.assertIsNone(self.revenda(cur).oferta_oficial)
        # vendas só conferidas na hora: 804 não troca
        cur = self.curadora(guardadas={}, reputacoes={1: REP[1], 2: poucas})
        self.assertIsNone(self.troca(cur, self.revenda(cur)))
        self.assertIn("só 804 vendas", self.logs[-1])
        # a página não mostra o selo
        cur = self.curadora(pagina={"loja_oficial": False})
        self.assertIsNone(self.troca(cur, self.revenda(cur)))
        self.assertIn("não mostra o selo", self.logs[-1])

    def test_vendas_desconhecidas_conferidas_na_hora_troca(self):
        cur = self.curadora(guardadas={})
        av = self.revenda(cur)
        self.assertFalse(av.oferta_oficial["vendas_conhecidas"])
        troca = self.troca(cur, av)
        self.assertIsNotNone(troca, self.logs)
        self.assertIn(2, self.api.consultados)   # a reputação da oficial foi pedida na conferência


if __name__ == "__main__":
    unittest.main()
