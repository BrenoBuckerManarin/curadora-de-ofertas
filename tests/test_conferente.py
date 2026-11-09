"""Segunda leitura: conferência do que entra na normalizadora, edição do Breno e fila de exceções."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico

from curadora import fila
from curadora.conferente import Conferente, LeitorJsonl, chave_link
from curadora.esgotados import esgotados, registrar_sinal
from curadora.ml import CardML
from curadora.score import Curadora

ASAD_NORMAL = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197])]
AGORA = T0 + 20 * DIA
NORMALIZADORA, EDICAO = "120363000000000012", "120363000000000001"
POST = "*O ASAD MAIS BARATO*\n\n*Lattafa Asad 100ml*\n\nDe R$ 299 por *R$ 180* 🦸🏻‍♂️\nCupom: *MODA10BELEZA* 🎟️\n\nhttps://meli.la/aB1"


def escrever(caminho: Path, linhas: list[dict]) -> None:
    with caminho.open("a", encoding="utf-8") as f:
        for l in linhas:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


class TestLeitorJsonl(unittest.TestCase):
    def test_le_so_linhas_completas_e_continua_de_onde_parou(self):
        pasta = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pasta)
        arq = pasta / "x.jsonl"
        arq.write_text('{"a": 1}\n{"a": 2}\n{"a": 3', encoding="utf-8")
        leitor = LeitorJsonl(arq)
        self.assertEqual(leitor.novas(), [])                       # começa do fim
        with arq.open("a", encoding="utf-8") as f:
            f.write('}\n{"a": 4}\n{"a"')
        self.assertEqual(leitor.novas(), [{"a": 4}])               # o pedaço do "a": 3 é descartado
        with arq.open("a", encoding="utf-8") as f:
            f.write(': 5}\n')
        self.assertEqual(leitor.novas(), [{"a": 5}])

    def test_chave_do_link_igual_nos_dois_lados(self):
        self.assertEqual(chave_link("https://meli.la/et6LS9w?x=1"), "meli.la/et6LS9w")
        self.assertEqual(chave_link("https://produto.mercadolivre.com.br/MLB-5957857754-perfume"), "MLB5957857754")


class TestConferente(unittest.TestCase):
    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.pasta)
        for nome in ("tap.jsonl", "feedback.jsonl", "caixa.jsonl"):
            (self.pasta / nome).write_text("", encoding="utf-8")
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200, vendedor="Lipx", loja_oficial=True, full=True)
        self.cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL), MLFalso({"https://meli.la/aB1": card}))
        self.cur.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct, limite, perfume_ts) "
                             "VALUES ('MODA10BELEZA', ?, ?, 10, 30, ?)", (AGORA, AGORA, AGORA))
        self.cur.con.commit()
        self.agora = AGORA
        cfg = dict(CFG, conferente={"grupo_normalizadora": NORMALIZADORA + "@g.us", "grupo_edicao": EDICAO + "@g.us"})
        self.conf = Conferente(self.cur, cfg, self.pasta / "tap.jsonl", self.pasta / "feedback.jsonl",
                               self.pasta / "caixa.jsonl", self.pasta / "saida.json", relogio=lambda: self.agora)

    def saida(self) -> dict:
        return json.loads((self.pasta / "saida.json").read_text(encoding="utf-8"))

    def test_confere_o_que_chega_na_normalizadora_e_exporta(self):
        escrever(self.pasta / "tap.jsonl", [{"messageId": "m1", "chatId": NORMALIZADORA + "@g.us", "body": POST,
                                             "timestamp": AGORA, "senderId": "breno"}])
        self.conf.rodada()
        c = self.saida()["conferencias"]["meli.la/aB1"]
        self.assertEqual((c["preco_ml"], c["preco_regra"]), (200, 180))   # 10% até R$30 sobre R$200
        self.assertIn("loja oficial", c["linha"])
        self.assertEqual(c["loja_oficial"], "Lipx")
        self.assertEqual(self.saida()["cupons_esgotados"], {})
        self.assertEqual(c["bloqueios"], [])

    def chega(self, corpo, mid="m1"):
        escrever(self.pasta / "tap.jsonl", [{"messageId": mid, "chatId": NORMALIZADORA + "@g.us", "body": corpo,
                                             "timestamp": self.agora, "senderId": "breno"}])
        self.conf.rodada()
        return self.saida()["conferencias"]["meli.la/aB1"]

    @staticmethod
    def normalizadora_ve_divergencia(conf: dict, final: float, cupons_no_post: int) -> bool:
        """A conta de curadora_conferencia na normalizadora: o preço final diverge do que a curadora vê (❌)?"""
        esperado = conf.get("preco_regra") if cupons_no_post else conf.get("preco_ml")
        return bool(final and esperado and abs(final - float(esperado)) > max(2.0, float(esperado) * 0.03))

    def test_cupom_que_sai_do_post_tem_o_preco_corrigido_ou_vai_para_revisao(self):
        """04/11 (Breno: "retirar o cupom suspenso dos próximos posts, garantindo que o preço também seja corrigido. Sem
        preço confirmado, sinalize para revisão")."""
        registrar_sinal(self.cur.con, "MODA10BELEZA", AGORA - 600, "esgotou", "breno", "você tirou do post do X às 11:03",
                        "e0")
        self.cur.con.commit()
        c = self.chega(POST)
        self.assertEqual(self.saida()["cupons_esgotados"]["MODA10BELEZA"]["estado"], "suspenso")   # ela tira o código
        self.assertEqual((c["preco_ml"], c["preco_regra"], c["tirados"], c["bloqueios"]),
                         (200, None, ["MODA10BELEZA"], []))
        # tirado o cupom (0 no post), o preço que ficasse com o desconto (R$ 180) diverge e o post vai com ❌; o preço do
        # card (R$ 200), que a normalizadora publica quando abre o anúncio, confere
        self.assertTrue(self.normalizadora_ve_divergencia(c, 180, 0))
        self.assertFalse(self.normalizadora_ve_divergencia(c, 200, 0))
        # sem o preço do ML nesta conferência, não há preço confirmado: revisar
        self.cur.ml.cards["https://meli.la/aB1"] = CardML(ok=False)
        self.assertEqual(self.chega(POST, "m3")["bloqueios"],
                         ["cupom MODA10BELEZA sai do post (suspenso: você tirou do post do X às 11:03) e a curadora não "
                          "tem o preço do ML sem ele: revisar"])

    def test_cupom_que_fica_suspenso_depois_da_conferencia_marca_a_conferencia(self):
        self.assertEqual(self.chega(POST)["bloqueios"], [])
        registrar_sinal(self.cur.con, "MODA10BELEZA", AGORA + 10, "esgotou", "grupo", "Grupo X avisou às 11:13", "g1")
        self.cur.con.commit()
        self.conf.exportar(AGORA + 40)
        c = self.saida()["conferencias"]["meli.la/aB1"]
        self.assertEqual(c["bloqueios"], ["cupom MODA10BELEZA passou a sair do post depois da conferência "
                                          "(esgotado: Grupo X avisou às 11:13): confira o preço sem ele"])
        self.assertEqual(c["preco_regra"], 180)   # a conta da conferência fica; quem marca o post é o aviso

    def test_loja_oficial_vai_com_o_nome_de_loja_e_so_com_o_selo(self):
        """18/10: a normalizadora não viu o vendedor do Fakhar Platin (verificação de conta); a curadora viu."""
        post = {"messageId": "w1", "chatId": NORMALIZADORA + "@g.us", "body": POST, "timestamp": AGORA}
        self.cur.ml.cards["https://meli.la/aB1"] = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200,
                                                          vendedor="WATHIQ_PARFUMS", loja_oficial=True)
        escrever(self.pasta / "tap.jsonl", [post])
        self.conf.rodada()
        self.assertEqual(self.saida()["conferencias"]["meli.la/aB1"]["loja_oficial"], "WATHIQ PARFUMS")
        self.cur.ml.cards["https://meli.la/aB1"] = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=200,
                                                          vendedor="LOJINHA", loja_oficial=False)
        escrever(self.pasta / "tap.jsonl", [dict(post, messageId="w2")])
        self.conf.rodada()
        self.assertEqual(self.saida()["conferencias"]["meli.la/aB1"]["loja_oficial"], "")

    def test_texto_que_nao_e_promo_avisa_que_nao_tera_conferencia(self):
        escrever(self.pasta / "tap.jsonl", [{"messageId": "m2", "chatId": NORMALIZADORA + "@g.us",
                                             "body": "olha esse https://meli.la/zZ9", "timestamp": AGORA}])
        self.conf.rodada()
        self.assertEqual(self.saida()["conferencias"]["meli.la/zZ9"].get("sem_conferencia"), True)

    def test_cupom_que_o_breno_tirou_vira_esgotado_quando_o_post_assenta(self):
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        final = POST.replace("por *R$ 180*", "por *R$ 200*").replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        escrever(self.pasta / "tap.jsonl", [{"messageId": "e1", "chatId": EDICAO + "@g.us", "body": final,
                                             "timestamp": AGORA, "senderId": "breno"}])
        self.conf.rodada()
        # 03/11 (a Edição é rascunho): a retirada espera os 3 min, como o "manteve" (até aqui valia na hora, desde 10/10)
        self.assertEqual(esgotados(self.cur.con, AGORA), {})
        self.assertFalse(self.cur.con.in_transaction)
        self.agora = AGORA + 200
        self.conf.rodada()
        motivo = self.saida()["cupons_esgotados"]["MODA10BELEZA"]["motivo"]
        self.assertIn("você tirou do post do Lattafa Asad 100ml", motivo)
        self.assertFalse(self.cur.con.in_transaction)

    def test_chamada_preenchida_pela_curadora_sai_quando_ele_troca_na_edicao(self):
        from curadora.chamadas import fila as fila_de_chamadas, importar, preencher
        prefs = self.pasta / "headline_preferences_v6.json"
        prefs.write_text(json.dumps({"manual_exact_sku_repertoire": {}}), encoding="utf-8")
        importar(self.cur.con, [{"chave": "lattafa-asad-100ml", "produto": "Lattafa Asad 100ml", "posts": 3,
                                 "chamadas": [{"texto": "O ASAD MAIS BARATO"}]}])
        preencher(self.cur.con, "teste", preferencias=prefs)
        self.conf.preferencias_chamadas = prefs
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        escrever(self.pasta / "tap.jsonl", [{"messageId": "e7", "chatId": EDICAO + "@g.us", "timestamp": AGORA,
                                             "body": POST.replace("O ASAD MAIS BARATO", "O LEÃO DA LATTAFA 🦁"),
                                             "senderId": "breno"}])
        self.agora = AGORA + 200
        self.conf.rodada()
        self.assertEqual(self.conf.estatisticas["chamadas_trocadas"], 1)
        self.assertEqual(fila_de_chamadas(self.cur.con)["resumo"]["recusada"], 1)
        self.assertEqual(json.loads(prefs.read_text(encoding="utf-8"))["manual_exact_sku_repertoire"], {})
        self.assertFalse(self.cur.con.in_transaction)

    def test_se_ele_reedita_com_o_cupom_de_volta_o_cupom_volta(self):
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        sem = POST.replace("por *R$ 180*", "por *R$ 200*").replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        escrever(self.pasta / "tap.jsonl", [{"messageId": "e1", "chatId": EDICAO + "@g.us", "body": sem,
                                             "timestamp": AGORA, "senderId": "breno"}])
        self.conf.rodada()
        self.assertEqual(esgotados(self.cur.con, AGORA), {})      # 03/11: rascunho, ainda não conta
        escrever(self.pasta / "tap.jsonl", [{"messageId": "e2", "chatId": EDICAO + "@g.us", "body": POST,
                                             "timestamp": AGORA + 60, "senderId": "breno"}])
        self.agora = AGORA + 60 + 200
        self.conf.rodada()
        self.assertEqual(esgotados(self.cur.con, self.agora), {})
        # tirou e pôs de volta no rascunho: a retirada nem é gravada (antes ficava um "esgotou" desfeito depois)
        self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM cupom_sinais WHERE tipo IN ('fora', 'esgotou')")
                         .fetchone()[0], 0)

    def test_03_11_duas_versoes_do_mesmo_post_sem_o_cupom_contam_uma_retirada(self):
        # 30/10 17:34-17:35: a 2ª versão do Club de Nuit (mais de 3 min depois da 1ª) contava como retirada nova
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        final = POST.replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        for mid, ts in (("e1", AGORA), ("e2", AGORA + 300)):
            escrever(self.pasta / "tap.jsonl", [{"messageId": mid, "chatId": EDICAO + "@g.us", "timestamp": ts,
                                                 "body": final.replace("O ASAD", "ASAD" if mid == "e2" else "O ASAD"),
                                                 "senderId": "breno"}])
            self.agora = ts
            self.conf.rodada()
            self.agora = ts + 200
            self.conf.rodada()
        self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM cupom_sinais WHERE tipo IN ('fora', 'esgotou')")
                         .fetchone()[0], 1)

    def test_03_11_o_mesmo_link_no_dia_seguinte_e_outra_postagem(self):
        # Astra: o link volta num post novo (outra campanha) e ele tira o cupom de novo; não é a versão de ontem
        final = POST.replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        for dia, mid in ((0, "e1"), (1, "e2")):
            ts = AGORA + dia * DIA
            self.agora = ts
            escrever(self.pasta / "feedback.jsonl", [{"ts": ts - 120, "link": "https://meli.la/aB1",
                                                      "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
            escrever(self.pasta / "tap.jsonl", [{"messageId": mid, "chatId": EDICAO + "@g.us", "body": final,
                                                 "timestamp": ts, "senderId": "breno"}])
            self.conf.rodada()
            self.agora = ts + 200
            self.conf.rodada()
        self.assertEqual([r[0] for r in self.cur.con.execute(
            "SELECT ref FROM cupom_sinais WHERE tipo IN ('fora', 'esgotou') ORDER BY ts")], ["edicao:e1", "edicao:e2"])

    def test_03_11_tirou_de_dois_posts_e_manteve_no_terceiro_logo_depois_nao_e_o_fim(self):
        # 30/10 17:34: tirou o OFICIAS3010 do Yara Elixir e do Sabah e, segundos depois, manteve no Khamrah e no Asad
        from curadora.esgotados import registrar_sinal
        for n in range(2):   # o cupom da equipe estava valendo (posto à mão em 2 posts)
            registrar_sinal(self.cur.con, "MODA10BELEZA", AGORA - 600 + n, "posto", "breno", "pôs", f"p{n}")
        posts = {}
        for n, link in enumerate(("https://meli.la/aB1", "https://meli.la/bC2", "https://meli.la/cD3")):
            posts[link] = POST.replace("https://meli.la/aB1", link)
            self.cur.ml.cards[link] = self.cur.ml.cards["https://meli.la/aB1"]
            escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": link, "published_copy": posts[link],
                                                      "display_product": "Lattafa Asad 100ml"}])
        sem = lambda link: posts[link].replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        escrever(self.pasta / "tap.jsonl", [
            {"messageId": "e1", "chatId": EDICAO + "@g.us", "body": sem("https://meli.la/aB1"), "timestamp": AGORA,
             "senderId": "breno"},
            {"messageId": "e2", "chatId": EDICAO + "@g.us", "body": sem("https://meli.la/bC2"), "timestamp": AGORA + 10,
             "senderId": "breno"},
            {"messageId": "e3", "chatId": EDICAO + "@g.us", "body": posts["https://meli.la/cD3"],
             "timestamp": AGORA + 20, "senderId": "breno"}])
        for self.agora in (AGORA + 5, AGORA + 185, AGORA + 215):
            self.conf.rodada()
        tipos = [r[0] for r in self.cur.con.execute("SELECT tipo FROM cupom_sinais WHERE ref LIKE 'edicao:%' ORDER BY ts, id")]
        self.assertEqual(tipos, ["fora", "fora", "ativo"])          # não acabou: ele seguiu usando no 3º post
        self.assertEqual(esgotados(self.cur.con, self.agora), {})

    def test_edicao_relida_nao_deixa_o_banco_travado(self):
        # 09/10, 20:31: depois de um reinício, a mesma edição relida (sinal já gravado) deixava a escrita aberta.
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 120, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        final = POST.replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        for _ in range(2):
            escrever(self.pasta / "tap.jsonl", [{"messageId": "e1", "chatId": EDICAO + "@g.us", "body": final,
                                                 "timestamp": AGORA, "senderId": "breno"}])
            self.agora = AGORA + 200
            self.conf.rodada()
            self.assertFalse(self.cur.con.in_transaction)


class TestOrigemDaPromo(TestConferente):
    """28/10 (Breno): de onde veio a promo, numa linha do relatório da normalizadora."""

    def vista(self, horas_atras: float, origem: str, grupo: str, preco: float) -> None:
        chave = self.cur.chave_de("Lattafa Asad 100ml")
        self.cur.con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, dados) "
                             "VALUES (?,?,?,?,?,?,?,?)", (int(AGORA - horas_atras * 3600), origem, grupo, chave, "BOA",
                                                          70, 0, json.dumps({"preco": preco})))

    def origem(self) -> str:
        escrever(self.pasta / "tap.jsonl", [{"messageId": "m1", "chatId": NORMALIZADORA + "@g.us", "body": POST,
                                             "timestamp": AGORA, "senderId": "breno"}])
        self.conf.rodada()
        return self.saida()["conferencias"]["meli.la/aB1"]["origem"]

    def test_grupo_que_postou_primeiro_e_quantos_mais(self):
        self.vista(3, "ao vivo", "FONTE BETA #27", 180)
        self.vista(2, "radar do ML", "", 181)
        self.vista(1, "ao vivo", "Outro Grupo", 179)
        o = self.origem()
        self.assertTrue(o.startswith("📍 Origem: FONTE BETA #27 ("), o)
        self.assertTrue(o.endswith("· +1 grupo"), o)

    def test_radar_antes_dos_grupos(self):
        self.vista(2, "radar do ML", "", 180)
        self.vista(1.5, "ao vivo", "FONTE BETA #27", 182)
        o = self.origem()
        self.assertTrue(o.startswith("📡 Origem: radar do ML ("), o)
        self.assertTrue(o.endswith(", 30 min antes do grupo FONTE BETA #27"), o)

    def test_radar_sozinho(self):
        self.vista(1, "vigia do ML", "", 180)
        self.assertTrue(self.origem().endswith("· nenhum grupo postou"))

    def test_outro_preco_nao_e_a_mesma_promo(self):
        self.vista(1, "ao vivo", "FONTE BETA #27", 230)
        self.assertEqual(self.origem(), "")

    def test_preco_parado_ha_dias_nao_e_descoberta_do_radar(self):
        # 31/10 (Khamrah): "📡 radar do ML (29/10 16:45), 1h51 antes do grupo" era o mesmo preço parado havia 2 dias
        self.vista(30, "radar do ML", "", 180)
        self.vista(28, "ao vivo", "FONTE BETA #27", 181)
        o = self.origem()
        self.assertTrue(o.startswith("📍 Origem: FONTE BETA #27 ("), o)


class TestFila(unittest.TestCase):
    def test_conta_quanto_o_breno_mexe_por_selo(self):
        pasta = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pasta)
        escrever(pasta / "normalizer_feedback.jsonl", [
            {"ts": T0, "link": "L1", "approval_grade": "✅ APROVADO", "curadora": {"linha": "x"}},
            {"ts": T0, "link": "L2", "approval_grade": "✅ APROVADO", "curadora_cupom": "cupom esgotado tirado"},
            {"ts": T0, "link": "L3", "approval_grade": "⚠️ APROVADO c/ ressalva"}])
        igual = {"headline_change": {"class": "unchanged"}, "title_change": {"class": "unchanged"},
                 "image_change": {"class": "unchanged"}}
        escrever(pasta / "editorial_comparison_cycles.jsonl", [
            dict(igual, link="L1", edition_final_ts=T0 + 60, before={"copy": "Por *R$ 96*"}, after={"copy": "Por *R$ 96*"}),
            dict(igual, link="L2", edition_final_ts=T0 + 60, before={"copy": "Por *R$ 96*\nCupom: X"},
                 after={"copy": "Por *R$ 106*"}),
            dict(igual, link="L3", edition_final_ts=T0 + 60, before={"copy": "Por *R$ 50*"}, after={"copy": "Por *R$ 50*"},
                 headline_change={"class": "strategy_changed"})])
        m = fila.medir(T0 - 10, T0 + 3600, pasta)
        self.assertEqual((m["total"]["posts"], m["total"]["intocados"]), (3, 1))
        self.assertEqual((m["por_selo"]["✅"]["posts"], m["por_selo"]["✅"]["intocados"], m["por_selo"]["✅"]["cupom"]), (2, 1, 1))
        texto = fila.texto(m, "Edição")
        self.assertIn("3 posts editados · você não mexeu em *33%*", texto)
        self.assertIn("cupom esgotado tirado sozinho: 1", texto)


class TestCaixaAoReligar(TestConferente):
    """11/10: ao religar, só as promos da caixa dos últimos 5 min são conferidas (eram todas as do dia)."""

    def test_promo_velha_da_caixa_nao_e_conferida_de_novo(self):
        velha = POST.replace("aB1", "vElha1")
        escrever(self.pasta / "caixa.jsonl", [
            {"messageId": "curadora-1", "timestamp": AGORA - 3600, "body": velha},
            {"messageId": "curadora-2", "timestamp": AGORA - 20, "body": POST}])
        cfg = dict(CFG, conferente={"grupo_normalizadora": NORMALIZADORA + "@g.us", "grupo_edicao": EDICAO + "@g.us"})
        conf = Conferente(self.cur, cfg, self.pasta / "tap.jsonl", self.pasta / "feedback.jsonl",
                          self.pasta / "caixa.jsonl", self.pasta / "saida.json", relogio=lambda: self.agora)
        conf.rodada()
        conferencias = self.saida()["conferencias"]
        self.assertIn("meli.la/aB1", conferencias)
        self.assertNotIn("meli.la/74K7du", conferencias)
        self.assertEqual(self.cur.ml.consultas, ["https://meli.la/aB1"])


class TestCupomDaEquipeTiradoDeUmPost(TestConferente):
    """11/10: com o cupom da equipe valendo, tirar de um post = não vale nessa loja (a rodada continua)."""

    def test_o_conferente_guarda_a_loja_e_nao_esgota_o_cupom(self):
        from curadora.esgotados import lojas_fora, quentes, registrar_sinal
        con = self.cur.con
        for n in (1, 2):   # ele manteve o cupom em 2 posts de outra loja
            registrar_sinal(con, "MODA10BELEZA", AGORA - 300 - n, "ativo", "breno", "você manteve", f"x{n}",
                            "outraloja", f"meli.la/x{n}")
        con.commit()
        escrever(self.pasta / "tap.jsonl", [{"messageId": "m1", "chatId": NORMALIZADORA + "@g.us", "body": POST,
                                             "timestamp": AGORA - 120, "senderId": "bot"}])
        escrever(self.pasta / "feedback.jsonl", [{"ts": AGORA - 100, "link": "https://meli.la/aB1",
                                                  "published_copy": POST, "display_product": "Lattafa Asad 100ml"}])
        final = POST.replace("por *R$ 180*", "por *R$ 200*").replace("Cupom: *MODA10BELEZA* 🎟️\n", "")
        escrever(self.pasta / "tap.jsonl", [{"messageId": "e1", "chatId": EDICAO + "@g.us", "body": final,
                                             "timestamp": AGORA, "senderId": "breno"}])
        self.conf.rodada()
        self.assertEqual(lojas_fora(con, AGORA), {})                 # 03/11: rascunho, ainda não conta
        self.assertFalse(con.in_transaction)
        self.agora = AGORA + 200     # a versão assentou: não vale nessa loja, e não vira esgotado
        self.conf.rodada()
        self.assertEqual(esgotados(con, self.agora), {})
        self.assertEqual(lojas_fora(con, self.agora), {"MODA10BELEZA": ["lipx"]})
        self.assertIn("MODA10BELEZA", quentes(con, self.agora))
        self.assertEqual(self.saida()["cupons_esgotados"], {})
        self.assertFalse(con.in_transaction)


if __name__ == "__main__":
    unittest.main()
