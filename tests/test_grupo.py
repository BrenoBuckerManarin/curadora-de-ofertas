"""A curadora no grupo da equipe (13/10, Breno: "pense uma curadora, mas versão WhatsApp"): a promo limpa numa
mensagem e a avaliação curta embaixo, no lugar da coletora antiga."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import apoio  # noqa: F401

import pipeline
from curadora import alertas
from curadora import grupo as vitrine

GRUPO = "120363000000000014@g.us"
GRUPOFINAL = "120363000000000002@g.us"


def avaliacao(**mudancas):
    base = dict(
        nota="BOA", score=71, preco_efetivo=93.0, pct_abaixo=12.4, historico=SimpleNamespace(mediana=106.0),
        razoes=["12% abaixo do normal (mediana R$106, com cupom, 60d)", "preço confirmado no ML",
                "loja oficial confirmada no ML: Primacial", "TOP vendido: Sabah Al Ward"],
        alertas=[], cupom_quente=None, identidade=SimpleNamespace(chave="sabah-al-ward|100"),
        promo=SimpleNamespace(produto="Sabah Al Ward Al Wataniah EDP 100ml", cupons=[], preco=93.0, preco_de=None,
                              link="https://meli.la/EAJPuFo"),
        card=SimpleNamespace(ok=True, preco=93.0, loja_oficial=True, vendedor="Primacial"))
    base.update(mudancas)
    return SimpleNamespace(**base)


class Ponte:
    """A ponte do WhatsApp de mentira: guarda o que foi mandado."""

    def __init__(self, recusa: str = "", quebra: str = ""):
        self.pedidos, self.recusa, self.quebra = [], recusa, quebra

    def __call__(self, url: str, payload: dict) -> dict:
        caminho = "/" + url.rsplit("/", 1)[1]
        if caminho == self.quebra:
            raise OSError("a ponte caiu")
        self.pedidos.append((caminho, payload))
        return {"success": caminho != self.recusa}


class TestAvaliacaoCurta(unittest.TestCase):
    def test_nota_preco_conferido_motivos_e_de_onde_veio(self):
        self.assertEqual(vitrine.avaliacao(avaliacao(), "Fonte Zeta | 133"), "\n".join([
            "🧠 Curadora 🟢 Boa 71",
            "💰 R$ 93 · ML confirma ✅ · 12% abaixo do normal (R$ 106)",
            "👍 top vendido, loja oficial",
            "🛍️ Fonte Zeta | 133"]))

    def test_excelente_com_recorde(self):
        av = avaliacao(nota="IMPERDÍVEL", score=78, preco_efetivo=99.0, pct_abaixo=28.8,
                       historico=SimpleNamespace(mediana=139.0), card=SimpleNamespace(ok=True, preco=99.0),
                       razoes=["TOP vendido: Sabah Al Ward", "NOVO RECORDE: 4% abaixo do menor preço de 60 dias (R$103)"])
        linhas = vitrine.avaliacao(av, "radar do ML").splitlines()
        self.assertEqual(linhas[0], "🧠 Curadora 🔥 Excelente 78")
        self.assertEqual(linhas[1], "💰 R$ 99 · ML confirma ✅ · 29% abaixo do normal (R$ 139)")
        self.assertEqual(linhas[2], "👍 recorde de preço, top vendido")

    def test_com_cupom_mostra_o_preco_com_ele_e_o_do_ml(self):
        av = avaliacao(preco_efetivo=84.15, promo=SimpleNamespace(produto="x", cupons=["MODA1310"], link="l"),
                       card=SimpleNamespace(ok=True, preco=99.0))
        self.assertIn("💰 R$ 84,15 com o cupom MODA1310 (R$ 99 no ML)", vitrine.avaliacao(av))

    def test_media_no_preco_normal_diz_isso(self):
        av = avaliacao(nota="OK", score=55, preco_efetivo=135.0, pct_abaixo=1.0, historico=SimpleNamespace(mediana=136.4),
                       razoes=["Natura/Boticário vendem bem no grupo: a nota sobe (no máximo até Média)"])
        texto = vitrine.avaliacao(av, "FONTE GAMA #45")
        self.assertEqual(texto.splitlines()[0], "🧠 Curadora 🟡 Média 55")
        self.assertIn("no preço normal (R$ 136)", texto)
        self.assertNotIn("👍", texto)

    def test_marca_o_que_o_pipeline_ja_mandou(self):
        self.assertTrue(vitrine.avaliacao(avaliacao(), "Fonte Delta #52", do_pipeline=True)
                        .endswith("🛍️ Fonte Delta #52 · 🤖 já foi pelo pipeline"))

    def test_aviso_serio_aparece_e_os_outros_nao(self):
        av = avaliacao(alertas=["pouco histórico (2 obs.)", "preço SUBIU: grupo anunciou R$89.00, agora R$93.00 no ML"])
        texto = vitrine.avaliacao(av)
        self.assertIn("⚠️ preço SUBIU", texto)
        self.assertNotIn("pouco histórico", texto)


class TestPromoMontada(unittest.TestCase):
    """Achado do radar e alerta fora do horário do pipeline: não há post de origem, a curadora monta a promo."""

    def test_promo_limpa_com_loja_preco_e_link(self):
        self.assertEqual(vitrine.promo_montada(avaliacao()), "\n".join([
            "*Sabah Al Ward Al Wataniah EDP 100ml*", "🏬 Loja oficial Primacial", "", "✅ Por R$ 93", "",
            "🛒 https://meli.la/EAJPuFo"]))

    def test_com_o_de_e_com_cupom(self):
        av = avaliacao(card=SimpleNamespace(ok=True, preco=93.0, loja_oficial=False, vendedor="Zé"),
                       promo=SimpleNamespace(produto="Kit Body Splash", cupons=["MELISALVA"], preco=93.0, preco_de=209.0,
                                             link="https://meli.la/x"))
        self.assertEqual(vitrine.promo_montada(av), "\n".join([
            "*Kit Body Splash*", "", "✅ De R$ 209 por R$ 93", "🎟️ Cupom: MELISALVA", "", "🛒 https://meli.la/x"]))


class TestDeOndeVeio(unittest.TestCase):
    def test_grupo_de_origem_ou_achado_da_curadora(self):
        self.assertEqual(vitrine.fonte_do_card({"origem": "ao vivo", "grupo": "Fonte Zeta | 133"}), "Fonte Zeta | 133")
        self.assertEqual(vitrine.fonte_do_card({"origem": "radar do ML", "grupo": "radar ML: Sabah Al Ward"}), "radar do ML")
        self.assertEqual(vitrine.fonte_do_card({"origem": "reconferida", "grupo": "vigia: Perfume Vulcan Feu 100ml"}), "vigia do ML")
        self.assertEqual(vitrine.fonte_do_card(None), "")


class TestConfiguracao(unittest.TestCase):
    def test_desligada_enquanto_o_config_nao_ligar(self):
        self.assertFalse(vitrine.configuracao({"whatsapp_alertas": {"grupo": GRUPO}})["ligado"])
        self.assertTrue(vitrine.configuracao({"whatsapp_alertas": {"grupo": GRUPO}, "grupo_curadora": {"ligado": True}})["ligado"])

    def test_nunca_o_grupofinal_nem_os_grupos_do_fluxo(self):
        for proibido in vitrine.PROIBIDOS:
            v = vitrine.configuracao({"whatsapp_alertas": {"grupo": proibido}, "grupo_curadora": {"ligado": True}})
            self.assertFalse(v["ligado"])
            self.assertEqual(v["grupo"], "")

    def test_o_botao_de_desligar_vale_na_hora(self):
        antes = vitrine.DESLIGADO
        cfg = {"whatsapp_alertas": {"grupo": GRUPO}, "grupo_curadora": {"ligado": True}}
        with tempfile.TemporaryDirectory() as pasta:
            vitrine.DESLIGADO = Path(pasta) / "grupo.desligado"
            try:
                self.assertTrue(vitrine.configuracao(cfg)["ligado"])
                vitrine.DESLIGADO.touch()                       # pipelinectl grupo off
                self.assertFalse(vitrine.configuracao(cfg)["ligado"])
            finally:
                vitrine.DESLIGADO = antes

    def test_sem_grupo_dos_avisos_nao_liga(self):
        self.assertFalse(vitrine.configuracao({"grupo_curadora": {"ligado": True}})["ligado"])

    def test_o_passo_nunca_e_menor_que_30_segundos(self):
        v = vitrine.configuracao({"whatsapp_alertas": {"grupo": GRUPO}, "grupo_curadora": {"ligado": True, "intervalo_s": 2}})
        self.assertEqual(v["intervalo_s"], 30)

    def test_sem_pipeline_rodando_ela_nao_esta_ativa(self):
        antes = vitrine.ESTADO_PIPELINE
        with tempfile.TemporaryDirectory() as pasta:
            vitrine.ESTADO_PIPELINE = Path(pasta) / "pipeline.estado.json"
            try:
                self.assertFalse(vitrine.pipeline_rodando())
                vitrine.ESTADO_PIPELINE.write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
                self.assertFalse(vitrine.pipeline_rodando())      # processo que não existe mais
            finally:
                vitrine.ESTADO_PIPELINE = antes


class TestEnviar(unittest.TestCase):
    def test_duas_mensagens_a_promo_e_depois_a_avaliacao(self):
        ponte = Ponte()
        vitrine.enviar(GRUPO, "a promo", "a avaliação", http=ponte)
        self.assertEqual(ponte.pedidos, [("/send", {"chatId": GRUPO, "message": "a promo"}),
                                         ("/send", {"chatId": GRUPO, "message": "a avaliação"})])

    def test_com_foto_a_promo_vai_como_legenda(self):
        ponte = Ponte()
        with tempfile.TemporaryDirectory() as pasta:
            foto = Path(pasta) / "f.jpg"
            foto.write_bytes(b"x")
            vitrine.enviar(GRUPO, "a promo", "a avaliação", str(foto), http=ponte)
        self.assertEqual(ponte.pedidos[0], ("/send-media", {"chatId": GRUPO, "filePath": str(foto), "mediaType": "image",
                                                            "caption": "a promo"}))
        self.assertEqual(ponte.pedidos[1], ("/send", {"chatId": GRUPO, "message": "a avaliação"}))

    def test_foto_que_nao_vai_nao_segura_a_promo(self):
        for ponte in (Ponte(recusa="/send-media"), Ponte(quebra="/send-media")):
            with tempfile.TemporaryDirectory() as pasta:
                foto = Path(pasta) / "f.jpg"
                foto.write_bytes(b"x")
                vitrine.enviar(GRUPO, "a promo", "a avaliação", str(foto), http=ponte)
            self.assertEqual([p for p in ponte.pedidos if p[0] == "/send"],
                             [("/send", {"chatId": GRUPO, "message": "a promo"}),
                              ("/send", {"chatId": GRUPO, "message": "a avaliação"})])

    def test_foto_que_sumiu_do_cache_vai_so_o_texto(self):
        ponte = Ponte()
        vitrine.enviar(GRUPO, "a promo", "a avaliação", "/caminho/que/nao/existe.jpg", http=ponte)
        self.assertEqual([p[0] for p in ponte.pedidos], ["/send", "/send"])

    def test_no_grupofinal_nunca(self):
        ponte = Ponte()
        with self.assertRaises(ValueError):
            vitrine.enviar(GRUPOFINAL, "a promo", "a avaliação", http=ponte)
        with self.assertRaises(ValueError):
            vitrine.enviar("", "a promo", "a avaliação", http=ponte)
        self.assertEqual(ponte.pedidos, [])

    def test_promo_recusada_pela_ponte_nao_manda_avaliacao_solta(self):
        ponte = Ponte(recusa="/send")
        with self.assertRaises(RuntimeError):
            vitrine.enviar(GRUPO, "a promo", "a avaliação", http=ponte)
        self.assertEqual(len(ponte.pedidos), 1)


class ComVitrineLigada(unittest.TestCase):
    """Vitrine ligada, com registro em pasta temporária e a ponte de mentira."""

    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.antes = {"registro": vitrine.REGISTRO, "configuracao": vitrine.configuracao,
                      "foto": pipeline.foto_do_grupo, "log": pipeline.log}
        vitrine.REGISTRO = Path(self.pasta.name) / "grupo.jsonl"
        self.ligada = True
        vitrine.configuracao = lambda cfg=None: {"ligado": self.ligada, "grupo": GRUPO, "intervalo_s": 90, "foto": True}
        pipeline.foto_do_grupo = lambda con, chave, link, produto="": ""
        self.falas = []
        pipeline.log = self.falas.append
        self.ponte = Ponte()
        pipeline._recusadas_vitrine.clear()

    def tearDown(self):
        vitrine.REGISTRO, vitrine.configuracao = self.antes["registro"], self.antes["configuracao"]
        pipeline.foto_do_grupo, pipeline.log = self.antes["foto"], self.antes["log"]
        pipeline._recusadas_vitrine.clear()
        self.pasta.cleanup()


class TestParaAVitrine(ComVitrineLigada):
    def test_manda_a_promo_e_a_avaliacao_e_registra(self):
        card = {"grupo": "Fonte Zeta | 133", "origem": "ao vivo"}
        self.assertTrue(pipeline.para_a_vitrine(None, None, avaliacao(), "TRIO PRA ELES", card, False, self.ponte))
        self.assertEqual(self.ponte.pedidos[0][1]["message"], "TRIO PRA ELES")
        self.assertTrue(self.ponte.pedidos[1][1]["message"].startswith("🧠 Curadora 🟢 Boa 71"))
        self.assertIn("🛍️ Fonte Zeta | 133", self.ponte.pedidos[1][1]["message"])
        self.assertEqual(vitrine.mostradas(time.time() - 3600), {"sabah-al-ward|100": 93.0})
        self.assertEqual(vitrine.na_ultima_hora(), 1)

    def test_o_mesmo_produto_nao_aparece_duas_vezes_em_8h(self):
        pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, False, self.ponte)
        self.assertFalse(pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, True, self.ponte))
        self.assertFalse(pipeline.para_a_vitrine(None, None, avaliacao(preco_efetivo=88.0), "promo", {}, True, self.ponte))
        self.assertEqual(len(self.ponte.pedidos), 2)

    def test_volta_antes_se_estiver_8_por_cento_mais_barato(self):
        pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, False, self.ponte)
        self.assertTrue(pipeline.para_a_vitrine(None, None, avaliacao(preco_efetivo=85.0), "promo", {}, False, self.ponte))
        self.assertEqual(len(self.ponte.pedidos), 4)

    def test_desligada_nao_manda_nada(self):
        self.ligada = False
        self.assertFalse(pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, False, self.ponte))
        self.assertEqual(self.ponte.pedidos, [])

    def test_ponte_fora_do_ar_nao_derruba_o_pipeline(self):
        self.assertFalse(pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, True, Ponte(quebra="/send")))
        self.assertEqual(vitrine.mostradas(0), {})          # não foi: não conta como mostrada
        self.assertTrue(any("grupo da equipe: não foi" in f for f in self.falas))

    def test_trava_de_seguranca_por_hora(self):
        for n in range(vitrine.MAXIMO_POR_HORA):
            vitrine.registrar({"chave": f"p{n}|100", "preco": 100})
        self.assertFalse(pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, False, self.ponte))
        self.assertEqual(self.ponte.pedidos, [])

    def test_a_foto_do_post_de_origem_vai_junto(self):
        foto = Path(self.pasta.name) / "f.jpg"
        foto.write_bytes(b"x")
        pipeline.foto_do_grupo = lambda con, chave, link, produto="": str(foto)
        pipeline.para_a_vitrine(None, None, avaliacao(), "promo", {}, False, self.ponte)
        self.assertEqual(self.ponte.pedidos[0][0], "/send-media")
        self.assertEqual(self.ponte.pedidos[0][1]["caption"], "promo")

    def test_o_preco_da_promo_e_o_conferido_no_ml_quando_nao_ha_cupom(self):
        av = avaliacao(card=SimpleNamespace(ok=True, preco=97.5), promo=SimpleNamespace(preco=93.0))
        texto = "KIT\n\n✅ De R$ 209 por R$ 93 no Pix\n\n🛒 https://meli.la/EAJPuFo"
        self.assertIn("por R$ 97,50 no Pix", pipeline.texto_da_vitrine(texto, av, sem_cupom=True))
        self.assertEqual(pipeline.texto_da_vitrine(texto, av, sem_cupom=False), texto)


class TestUmaDaVitrine(ComVitrineLigada):
    def setUp(self):
        super().setUp()
        self.antes.update(candidatas=pipeline.candidatas, preparar=pipeline.preparar_para_a_vitrine)
        self.cards = [{"chave": "a|100", "preco": 100.0, "grupo": "G1"}, {"chave": "b|100", "preco": 200.0, "grupo": "G2"},
                      {"chave": "c|100", "preco": 300.0, "grupo": "G3"}]
        self.excluidos = []
        pipeline.candidatas = lambda con, cur, excluir: (self.excluidos.append(set(excluir)) or [],
                                                         [c for c in self.cards if c["chave"] not in excluir])
        self.recusa = set()
        self.conferidas = []

        def preparar(con, cur, card):
            self.conferidas.append(card["chave"])
            if card["chave"] in self.recusa:
                return None
            return avaliacao(identidade=SimpleNamespace(chave=card["chave"]), preco_efetivo=card["preco"]), "promo " + card["chave"]
        pipeline.preparar_para_a_vitrine = preparar
        self.mandar = lambda: pipeline.uma_da_vitrine(None, None, self.ponte)

    def tearDown(self):
        pipeline.candidatas, pipeline.preparar_para_a_vitrine = self.antes["candidatas"], self.antes["preparar"]
        super().tearDown()

    def test_uma_por_passo_na_ordem_da_nota_sem_repetir(self):
        for esperado in ("a|100", "b|100", "c|100"):
            self.assertTrue(self.mandar())
            self.assertEqual(self.ponte.pedidos[-2][1]["message"], "promo " + esperado)
        self.assertFalse(self.mandar())                      # acabou: não repete
        self.assertEqual(len(self.ponte.pedidos), 6)
        self.assertEqual(self.conferidas, ["a|100", "b|100", "c|100"])

    def test_recusada_na_conferencia_passa_para_a_proxima_e_nao_e_reconferida(self):
        self.recusa = {"a|100"}
        self.assertTrue(self.mandar())
        self.assertEqual(self.ponte.pedidos[0][1]["message"], "promo b|100")
        self.mandar()
        self.assertEqual(self.conferidas, ["a|100", "b|100", "c|100"])   # a|100 só foi ao ML uma vez
        self.assertIn("a|100", self.excluidos[-1])

    def test_o_que_o_pipeline_acabou_de_recusar_nao_e_conferido_de_novo(self):
        pipeline._recusadas["a|100"] = time.time()
        try:
            self.mandar()
        finally:
            pipeline._recusadas.pop("a|100", None)
        self.assertEqual(self.conferidas, ["b|100"])

    def test_no_maximo_tres_idas_ao_ml_por_passo(self):
        self.cards += [{"chave": f"x{n}|100", "preco": 50.0} for n in range(5)]
        self.recusa = {c["chave"] for c in self.cards}
        self.assertFalse(self.mandar())
        self.assertEqual(len(self.conferidas), pipeline.CONFERIDAS_POR_PASSO)

    def test_confere_em_silencio_e_o_log_volta_depois(self):
        self.mandar()
        self.assertFalse(pipeline._calado)
        self.assertEqual(len([f for f in self.falas if "no grupo da equipe" in f]), 1)

    def test_o_passo_devolve_o_intervalo_e_nunca_levanta_erro(self):
        mandar = pipeline.uma_da_vitrine
        try:
            pipeline.uma_da_vitrine = lambda con, cur: True
            self.assertEqual(pipeline.passo_da_vitrine(None, None), 90)
            pipeline.uma_da_vitrine = lambda con, cur: False
            self.assertEqual(pipeline.passo_da_vitrine(None, None), 45)
            pipeline.uma_da_vitrine = lambda con, cur: 1 / 0
            self.assertEqual(pipeline.passo_da_vitrine(None, None), 120)
            self.ligada = False
            self.assertEqual(pipeline.passo_da_vitrine(None, None), 60)
        finally:
            pipeline.uma_da_vitrine = mandar


class TestAlertaNoModeloDoGrupo(unittest.TestCase):
    """Fora do horário do pipeline o aviso das Excelentes continua, agora em duas mensagens (promo + avaliação)."""

    def setUp(self):
        self.ativo = alertas.vitrine.ativo
        alertas.vitrine.ativo = lambda cfg=None: False
        self.ponte = Ponte()
        self.saida = alertas.SaidaWhatsApp(GRUPO, ["IMPERDÍVEL"], http=self.ponte)
        self.dados = {"nota": "IMPERDÍVEL", "promo": "a promo limpa", "avaliacao": "🧠 Curadora 🔥 Excelente 78"}

    def tearDown(self):
        alertas.vitrine.ativo = self.ativo

    def test_o_aviso_vai_em_duas_mensagens(self):
        self.saida("texto antigo", self.dados)
        self.assertEqual([p[1]["message"] for p in self.ponte.pedidos], ["a promo limpa", "🧠 Curadora 🔥 Excelente 78"])

    def test_com_o_pipeline_rodando_o_aviso_fica_quieto(self):
        alertas.vitrine.ativo = lambda cfg=None: True
        self.saida("texto antigo", self.dados)
        self.assertEqual(self.ponte.pedidos, [])

    def test_alvo_de_preco_cadastrado_avisa_sempre(self):
        alertas.vitrine.ativo = lambda cfg=None: True
        self.saida("🎯 chegou no seu alvo", {"nota": "OK", "forcar": True})
        self.assertEqual([p[1]["message"] for p in self.ponte.pedidos], ["🎯 chegou no seu alvo"])

    def test_nota_que_nao_avisa_continua_sem_avisar(self):
        self.saida("texto", dict(self.dados, nota="BOA"))
        self.assertEqual(self.ponte.pedidos, [])


class TestAvaliarQuandoPedem(unittest.TestCase):
    """13/10 (Breno): "ter a função avaliar promos quando eu enviar uma promo no grupo"."""

    PROMO = "*Lattafa Asad 100ml*\nDe R$ 289 por R$ 165\nhttps://meli.la/asad"

    def setUp(self):
        from apoio import CFG, DIA, T0, MLFalso, banco_com_historico
        from curadora.aovivo import Escuta
        from curadora.ml import CardML
        from curadora.score import Curadora
        self.agora = T0 + 15 * DIA
        historico = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197, 200, 204])]
        self.ml = MLFalso({"https://meli.la/asad": CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=165,
                                                          vendedor="Lipx", loja_oficial=True)})
        cfg = {**CFG, "whatsapp_alertas": {"grupo": GRUPO}, "grupo_curadora": {"ligado": True}}
        self.cur = Curadora(cfg, banco_com_historico(historico), self.ml)
        self.tmp = tempfile.TemporaryDirectory()
        self.desligado = vitrine.DESLIGADO
        vitrine.DESLIGADO = Path(self.tmp.name) / "grupo.desligado"
        self.escuta = Escuta(Path(self.tmp.name) / "tap.jsonl", self.cur, alertas.Alertador(self.cur.con, []),
                             CFG["grupos_fonte"], relogio=lambda: self.agora + 5)
        self.ponte = Ponte()
        self.escuta.http_pedidos = self.ponte

    def tearDown(self):
        vitrine.DESLIGADO = self.desligado
        self.tmp.cleanup()

    def chega(self, texto, mid="AC01", ha_s=0, grupo=GRUPO, **extra):
        linha = json.dumps({"messageId": mid, "chatId": grupo, "timestamp": self.agora - ha_s, "body": texto,
                            "senderName": "Breno", **extra})
        return self.escuta.processar_linha(linha)

    def test_promo_mandada_no_grupo_recebe_a_avaliacao_em_resposta(self):
        self.chega(self.PROMO)
        self.assertEqual(len(self.ponte.pedidos), 1)
        caminho, payload = self.ponte.pedidos[0]
        self.assertEqual((caminho, payload["chatId"], payload["replyTo"]), ("/send", GRUPO, "AC01"))
        linhas = payload["message"].splitlines()
        self.assertTrue(linhas[0].startswith("🧠 Curadora "))
        self.assertIn("💰 R$ 165 · ML confirma ✅", linhas[1])
        self.assertIn("abaixo do normal (R$ 200)", linhas[1])

    def test_nao_vira_card_nem_historico(self):
        self.chega(self.PROMO)
        self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM avaliacoes").fetchone()[0], 0)
        self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM ofertas WHERE preco = 165").fetchone()[0], 0)

    def test_a_mesma_mensagem_so_e_respondida_uma_vez(self):
        self.chega(self.PROMO)
        self.chega(self.PROMO)                      # relida depois de um reinício
        self.assertEqual(len(self.ponte.pedidos), 1)
        self.chega(self.PROMO, mid="AC02")          # outra mensagem com a mesma promo: é outro pedido
        self.assertEqual(len(self.ponte.pedidos), 2)

    def test_conversa_reacao_e_mensagem_velha_ficam_sem_resposta(self):
        self.chega("tá rolando um cupom, olha lá", mid="AC10")
        self.chega("[Reaction: 😂 to 3EB08FBE78393BA4325B8F]", mid="AC11", mediaType="reaction")
        self.chega(self.PROMO, mid="AC12", ha_s=3600)
        self.chega("🧠 Curadora 🟢 Boa 71\n💰 R$ 165 · ML confirma ✅\nhttps://meli.la/asad", mid="AC13")
        self.assertEqual(self.ponte.pedidos, [])

    def test_so_o_link_sem_preco_pede_a_promo_inteira(self):
        self.chega("https://meli.la/asad", mid="AC20")
        self.assertIn("não consegui ler o produto e o preço", self.ponte.pedidos[0][1]["message"])

    def test_calada_no_grupo_nao_responde(self):
        vitrine.DESLIGADO.touch()                   # pipelinectl grupo off
        self.chega(self.PROMO)
        self.assertEqual(self.ponte.pedidos, [])

    def test_promo_em_grupo_fonte_continua_sendo_so_fonte(self):
        self.chega(self.PROMO, grupo="120363000000000011@g.us")
        self.assertEqual(self.ponte.pedidos, [])
        self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM avaliacoes").fetchone()[0], 1)

    def test_compra_internacional_e_vendedor_de_risco_aparecem_na_resposta(self):
        av = avaliacao(nota="NÃO VALE", score=0, risco=True, risco_motivo="25% abaixo de todos os outros vendedores: confira",
                       alertas=["12% ACIMA do normal (mediana R$106)", "cupom MODA1110 esgotado (você tirou às 10:07)"],
                       card=SimpleNamespace(ok=True, preco=93.0, internacional=True))
        texto = vitrine.avaliacao(av, completo=True)
        self.assertIn("⚠️ compra internacional: não pode postar", texto)
        self.assertIn("⚠️ 25% abaixo de todos os outros vendedores", texto)
        self.assertIn("⚠️ cupom MODA1110 esgotado", texto)
        self.assertNotIn("ACIMA do normal (mediana", texto)      # isso já está na linha do preço


if __name__ == "__main__":
    unittest.main()
