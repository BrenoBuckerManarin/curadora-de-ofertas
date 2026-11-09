"""31/10: correções da revisão crítica (dados reais de 16 a 30/10). Cada teste reproduz um caso real."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apoio import conectar, CFG, T0, MLFalso
import pipeline
from curadora.chamadas import apos_edicao, decidir
from curadora.cupons import cupons_da_mensagem
from curadora.ml import CardML
from curadora.score import Curadora

RAIZ = Path(__file__).resolve().parent.parent


class TestSemCaractereDeControle(unittest.TestCase):
    def test_nenhum_py_tem_caractere_de_controle(self):
        """13/10: um backspace (\\x08) entrou no lugar de \\b em cupons.py e o leitor de cupons passou a ler palavras do
        título como cupom por 18 dias. O heredoc do Git Bash troca \\\\b por \\b: este teste pega a próxima vez."""
        controle = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
        arquivos = subprocess.run(["git", "-C", str(RAIZ), "ls-files", "*.py"], capture_output=True, text=True).stdout.split()
        arquivos += [str(p.relative_to(RAIZ)) for p in RAIZ.glob("tests/*.py")]
        achados = []
        for nome in sorted(set(arquivos)):
            p = RAIZ / nome
            if not p.exists():
                continue
            for n, linha in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if controle.search(linha):
                    achados.append(f"{nome}:{n}")
        self.assertEqual(achados, [])


class TestLeitorDeCupons(unittest.TestCase):
    def test_nina_rouge_real_nao_vira_cupom(self):
        # Fonte Zeta, 30/10 17:54: a curadora registrou NINA, ROUGE e 30ML como cupons
        texto = ("NINA ROUGE 30ML COM CUPOM 🥷\n\n➡️ **Perfume Nina Ricci Nina Rouge Eau de Toilette 30 ml Feminino**\n\n"
                 "✅ De R$ ~369~ por R$ 240\n🏷️ Cupom: `SITEMELIMAIS15`\n\n🛒 Compre aqui: https://meli.la/QpIgqaT")
        self.assertEqual(list(cupons_da_mensagem(texto)), ["SITEMELIMAIS15"])

    def test_codigo_que_comeca_por_cupom_continua(self):
        self.assertEqual(list(cupons_da_mensagem("Código: CUPOMEXCLUSIVO")), ["CUPOMEXCLUSIVO"])
        self.assertEqual(list(cupons_da_mensagem("Use o cupom CUPOMLIBERADO")), ["CUPOMLIBERADO"])
        self.assertEqual(list(cupons_da_mensagem("🎟️ Cupom: *MODA10*")), ["MODA10"])

    def test_volume_nao_e_cupom(self):
        self.assertEqual(list(cupons_da_mensagem("Cupom: 100ML")), [])
        self.assertEqual(list(cupons_da_mensagem("BANDERAS SEDUCTION 100ML COM CUPOM\nCupom: OFERTANOML")),
                         ["OFERTANOML"])


class TestPrecoComCupomNaoFicaAbaixoDaConta(unittest.TestCase):
    """24-27/10: 3 de 69 envios com cupom foram publicados abaixo da conta da regra (até R$0,92); o código deixava
    até 3% (R$5,40 em R$180)."""

    def setUp(self):
        self.con = conectar(":memory:")
        agora = T0 + 30 * 86400
        self.agora = agora
        self.con.execute("INSERT INTO cupons(codigo, primeiro_ts, ultimo_ts, pct) VALUES ('TESTE10', ?, ?, 10)",
                         (agora - 600, agora - 600))

    def avaliar(self, anunciado, ml):
        card = CardML(ok=True, titulo="Lattafa Asad 100ml", preco=ml, loja_oficial=True, vendedor="Lipx")
        self.cur = cur = Curadora({**CFG, "cupom_quente": {"ligado": False}}, self.con, MLFalso({"https://meli.la/x": card}))
        preco = f"{anunciado:.2f}".replace(".", ",")
        av = cur.avaliar(f"Lattafa Asad 100ml\nPor R${preco}\nCupom TESTE10\nhttps://meli.la/x", agora=self.agora)
        self.assertTrue(av.conferido_ml)   # sem o card a conta nem é feita (o teste não provaria nada)
        return av

    def test_nota_continua_com_o_numero_do_grupo(self):
        # verificador: com a conta (R$112,62) a promo perdia o "menor preço" contra o próprio repost (R$112) e 6
        # posts publicados cairiam de nota. A nota compara o número do grupo com o histórico do grupo.
        av = self.avaliar(180, 204.44)          # regra: 204,44 - 10% = 184,00
        self.assertAlmostEqual(av.preco_efetivo, 180.0, places=2)

    def test_texto_ao_gerador_leva_o_preco_da_conta(self):
        av = self.avaliar(180, 204.44)
        self.assertAlmostEqual(pipeline.preco_do_texto(self.cur, av, ["TESTE10"], set()), 184.0, places=2)
        texto = pipeline.texto_para_o_gerador("Asad 100ml\nPor R$ 180\nCupom TESTE10", set(),
                                              pipeline.preco_do_texto(self.cur, av, ["TESTE10"], set()), 180.0)
        self.assertIn("R$ 184,00", texto)
        self.assertNotIn("R$ 180", texto)

    def test_grupo_acima_da_regra_continua_o_do_grupo(self):
        av = self.avaliar(329.99, 359.99)       # regra: 323,99; o grupo diz 329,99
        self.assertAlmostEqual(av.preco_efetivo, 329.99, places=2)
        self.assertIsNone(pipeline.preco_do_texto(self.cur, av, ["TESTE10"], set()))

    def test_texto_sem_mudanca_nos_outros_casos(self):
        av = self.avaliar(180, 200.0)           # regra: 180,00 (igual ao grupo)
        self.assertIsNone(pipeline.preco_do_texto(self.cur, av, ["TESTE10"], set()))   # cupom fica, grupo certo
        self.assertEqual(pipeline.preco_do_texto(self.cur, av, [], {"TESTE10"}), 200.0)  # cupom sai: preço do ML
        self.assertIsNone(pipeline.preco_do_texto(self.cur, av, [], set()))            # sem cupom nenhum

    def test_arrumar_resposta_nao_deixa_preco_abaixo_do_conferido(self):
        # identidad 26/10: conferido R$160,20 e o texto dizia R$159
        self.assertIn("160,20", pipeline.arrumar_resposta("Identidad 100ml\npor R$ 159", "Identidad 100ml", 160.20))
        self.assertIn("R$ 161", pipeline.arrumar_resposta("Identidad 100ml\npor R$ 161", "Identidad 100ml", 160.20))
        # centavos cortados são o estilo da casa: R$160 para R$160,20 fica
        self.assertIn("R$ 160\n", pipeline.arrumar_resposta("Identidad 100ml\npor R$ 160\n", "Identidad 100ml", 160.20))


class TestPrecoEnviado(unittest.TestCase):
    """31/10 (revisão colada): o registro do envio guarda o preço que foi de fato no texto, além do da nota."""

    def test_formatos_reais(self):
        self.assertEqual(pipeline.preco_no_texto("Lattafa\n\nPor *R$ 149,04* 🦸🏻‍♂️\n\nhttps://meli.la/x"), 149.04)
        self.assertEqual(pipeline.preco_no_texto("De R$ 379 por *R$ 194* no Pix"), 194.0)
        self.assertEqual(pipeline.preco_no_texto("Por: R$*168,00*"), 168.0)
        self.assertEqual(pipeline.preco_no_texto("POR R$1.099,90"), 1099.90)
        self.assertIsNone(pipeline.preco_no_texto("✅ R$ 162 no Pix"))


class TestKitComOutroNome(unittest.TestCase):
    """31/10 (Breno: "pode já montar as 3 correções"): o GrupoFinal postou o Kit Floratta Blue a R$96,81 às 13:46 e o
    pipeline mandou o mesmo kit, com outro nome ("+ Hidratante 75ml"), a R$113,90 às 16:09."""

    def test_mesmo_kit_ignora_acompanhamento_e_tamanho(self):
        self.assertEqual(pipeline.mesmo_kit("kit:blue-floratta-hidratante|30"), "kit:blue-floratta")
        self.assertEqual(pipeline.mesmo_kit("kit:blue-floratta|?"), "kit:blue-floratta")
        self.assertNotEqual(pipeline.mesmo_kit("kit:kaiak-urbano|100"), pipeline.mesmo_kit("kit:kaiak|100"))
        self.assertEqual(pipeline.mesmo_kit("tous-yara|100"), "")

    def candidatas(self, preco_do_kit):
        import time
        from curadora.banco import conectar
        agora = int(time.time())
        con = conectar(":memory:")
        cur = Curadora(CFG, con, None)
        for chave, score, preco in (("kit:teste-floratta-hidratante|30", 70, preco_do_kit), ("teste-outro|100", 65, 150.0)):
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, dados) VALUES (?,?,?,?,?,?,?)",
                        (agora - 300, "ao vivo", "g", chave, "BOA", score, json.dumps({"preco": preco})))
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?,?,?,?,?,?)",
                    ("h1", agora - 3600, "Grupo da Equipe", "Kit Teste Floratta", "kit:teste-floratta|?", 96.81))
        con.commit()
        _, lista = pipeline.candidatas(con, cur, set())
        return [c["chave"] for c in lista if "teste" in c["chave"]]

    def test_mesmo_kit_postado_ha_pouco_e_mais_caro_nao_vai(self):
        self.assertEqual(self.candidatas(113.90), ["teste-outro|100"])

    def test_mesmo_kit_8_por_cento_mais_barato_pode_ir(self):
        self.assertIn("kit:teste-floratta-hidratante|30", self.candidatas(85.0))


class TestMesmaLinhaNaHora(unittest.TestCase):
    """31/10 (Breno: "pode já montar as 3 correções"): Yara Tous 15:32 e Yara Elixir 16:18; Essencial Exclusivo 15:45
    e Essencial Sentir 16:05; dois kits Vibration + Blunn 15:49 e 16:13."""

    def test_linha_dos_casos_de_hoje(self):
        self.assertEqual(pipeline.linha("tous-yara|100"), {"yara"})
        self.assertEqual(pipeline.linha("elixir-yara|100"), {"yara"})
        self.assertEqual(pipeline.linha("essencial-exclusivo|100") & pipeline.linha("essencial-sentir|100"), {"essencial"})
        self.assertEqual(pipeline.linha("kit:blunn-body-fero-infalivel-splash-vibration|?")
                         & pipeline.linha("kit:bergamota-blunn-body-limao-primacial-splash-vibration|200"),
                         {"blunn", "vibration"})
        self.assertEqual(pipeline.linha("black-vip|100"), set())   # só versão: não tem linha, não segura nada

    def test_mesma_linha_da_ultima_hora_vai_para_depois(self):
        lista = [{"chave": "elixir-yara|100", "produto": "Yara Elixir", "preco": 188},
                 {"chave": "asad|100", "produto": "Asad", "preco": 158}]
        nome = lambda c: c["produto"]
        self.assertEqual([c["chave"] for c in pipeline.misturar(lista, nome, [], None, {"yara"})],
                         ["asad|100", "elixir-yara|100"])
        self.assertEqual([c["chave"] for c in pipeline.misturar(lista, nome, [], None, set())],
                         ["elixir-yara|100", "asad|100"])
        # só muda a ordem: sozinha, a mesma linha vai assim mesmo
        self.assertEqual([c["chave"] for c in pipeline.misturar(lista[:1], nome, [], None, {"yara"})], ["elixir-yara|100"])

    def test_linhas_da_ultima_hora_vem_do_registro(self):
        import time
        agora = time.time()
        with tempfile.TemporaryDirectory() as pasta:
            registro = Path(pasta) / "pipeline.jsonl"
            registro.write_text("\n".join(json.dumps(r) for r in (
                {"ts": agora - 7200, "status": "enviado", "chave": "khamrah|100", "preco": 150},
                {"ts": agora - 1800, "status": "enviado", "chave": "tous-yara|100", "preco": 154},
                {"ts": agora - 600, "status": "gerador_sem_resposta", "chave": "asad|100", "preco": 158})) + "\n",
                encoding="utf-8")
            with patch.object(pipeline, "REGISTRO", registro):
                self.assertEqual(pipeline.linhas_recentes(), {"yara"})


class TestExcelenteSoComPrecoBom(unittest.TestCase):
    """31/10 (Breno: "tem ctz que isso é uma promo excelente?"; Astra: "considerar os últimos 7-14 dias da mesma
    loja/anúncio"): o Khamrah 100ml saiu Excelente 83 a R$186,72 na Wathiq contra a mediana de 60 dias, mas o mesmo
    anúncio estava a R$177,38 no dia anterior."""

    TITULO = "Perfume Lattafa Khamrah Eau de Parfum 100ml"

    def setUp(self):
        import time
        self.agora = int(time.time())
        self.con = conectar(":memory:")
        for n, dias in enumerate(range(2, 58), 1):
            self.con.execute("INSERT INTO ofertas(msg_id,ts,chave,preco,cupons,produto,base) VALUES (?,?,?,?,'[]','Khamrah','khamrah')",
                             (str(n), self.agora - dias * 86400, "khamrah|100", 260.0))

    def curadora(self, card=None):
        return Curadora({**CFG, "cupom_quente": {"ligado": False}}, self.con, MLFalso({"https://meli.la/k": card} if card else {}))

    def card(self, preco, item_id="MLB4911"):
        return CardML(ok=True, titulo=self.TITULO, preco=preco, item_id=item_id, loja_oficial=True, vendedor="Wathiq Parfums")

    def visto(self, preco, horas, item_id="MLB4911", fonte="destaque"):
        """O anúncio visto na página do ML (conferência) há `horas`, gravado como na produção (com o Pix)."""
        self.curadora().observar_preco_ml(self.card(preco, item_id), fonte, self.agora - int(horas * 3600))

    def visto_na_api(self, preco, horas, item_id="MLB4911"):
        """O anúncio lido pelo radar na API do ML há `horas` (sem o Pix), gravado como o radar grava."""
        from curadora.banco import FONTE_API, registrar_preco_ml
        registrar_preco_ml(self.con, self.agora - int(horas * 3600), self.curadora().chave_de(self.TITULO),
                           self.card(preco, item_id), FONTE_API)

    def avaliar(self, preco):
        av = self.curadora(self.card(preco)).avaliar(f"Lattafa Khamrah 100ml\nPor R${preco:.2f}\nhttps://meli.la/k"
                                                     .replace(".", ",", 1), agora=self.agora)
        self.assertTrue(av.conferido_ml)
        return av

    def avaliar_pelo_radar(self, preco):
        """Como o radar avalia: o card já vem da API e não abre a página."""
        av = self.curadora().avaliar(f"Lattafa Khamrah 100ml\nPor R${preco:.2f}\nhttps://meli.la/k".replace(".", ",", 1),
                                     agora=self.agora, card=self.card(preco), card_da_api=True)
        self.assertTrue(av.conferido_ml)
        return av

    def test_radar_compara_so_com_a_api(self):
        # 31/10 22h, o caso real (MLB6600806802): a API leu R$186,72 a semana toda e a página R$177,38 (5% do Pix),
        # a última vez 20 h antes; o radar caía para Boa 74 sem subida nenhuma
        for horas in (20.5, 44, 68):
            self.visto_na_api(186.72, horas + 0.5)
            self.visto(177.38, horas, fonte="recomendacao")
        av = self.avaliar_pelo_radar(186.72)
        self.assertGreaterEqual(av.score, 75)
        self.assertFalse(any("o mesmo anúncio custava" in a for a in av.alertas), av.alertas)

    def test_radar_ve_o_fim_do_pix_na_pagina(self):
        # 31/10, Club de Nuit Intense (MLB4515906491): a API caiu de 214,99 para 189,99, mas a página subiu de 151,99
        # para 189,99 (o desconto do Pix acabou: no Pix, 25% a mais)
        self.visto_na_api(214.99, 120)
        self.visto_na_api(189.99, 50)
        self.visto(151.99, 52, fonte="recomendacao")
        self.visto(189.99, 5, fonte="recomendacao")
        av = self.avaliar_pelo_radar(189.99)
        self.assertEqual(av.score, 74)
        self.assertTrue(any("o mesmo anúncio custava R$151.99 nos últimos 7 dias na página, com o Pix (hoje R$189.99)"
                            in a for a in av.alertas), av.alertas)

    def test_radar_com_pagina_velha_fica_so_na_api(self):
        self.visto_na_api(189.99, 50)
        self.visto(151.99, 52, fonte="recomendacao")   # página vista só há mais de 24 h: não diz como está hoje
        av = self.avaliar_pelo_radar(189.99)
        self.assertGreaterEqual(av.score, 75)

    def test_radar_com_subida_na_api_cai_para_boa(self):
        self.visto_na_api(177.38, 26)
        self.visto_na_api(186.72, 3)
        av = self.avaliar_pelo_radar(186.72)
        self.assertEqual(av.score, 74)
        self.assertTrue(any("o mesmo anúncio custava R$177.38" in a for a in av.alertas), av.alertas)

    def test_pagina_nao_usa_o_preco_da_api(self):
        self.visto_na_api(150.0, 26)       # a API mais barata que a página (raro): não entra na conta da página
        self.visto(186.72, 3)
        av = self.avaliar(186.72)
        self.assertGreaterEqual(av.score, 75)
        self.assertFalse(any("o mesmo anúncio custava" in a for a in av.alertas), av.alertas)

    def test_registro_guarda_api_e_pagina_no_mesmo_dia(self):
        # até 31/10 o mesmo preço visto pela API e pela página no mesmo dia virava uma linha só (de quem viu primeiro)
        self.visto(186.72, 0.5)
        self.visto_na_api(186.72, 0.4)
        self.visto(186.72, 0.3)                       # mesma fonte, mesmo dia, mesmo preço: não repete
        self.visto(186.72, 0.2, fonte="recomendacao")  # também é da página
        self.visto_na_api(186.72, 0.1)
        fontes = sorted(r[0] for r in self.con.execute("SELECT fonte FROM precos_ml WHERE item_id = 'MLB4911'"))
        self.assertEqual(fontes, ["destaque", "radar API"])

    def test_anuncio_mais_caro_que_ontem_nao_e_excelente(self):
        import curadora.score as score
        self.visto(177.38, 26)
        self.visto(186.72, 3)
        with patch.object(score, "menor_preco_do_anuncio", return_value=None):
            sem_regra = self.avaliar(186.72)
        self.assertGreaterEqual(sem_regra.score, 75)              # antes: Excelente (60 dias a R$260)
        av = self.avaliar(186.72)
        self.assertEqual(av.score, 74)                            # Boa: continua indo, sem o selo
        self.assertEqual(av.nota, "BOA")
        self.assertTrue(any("o mesmo anúncio custava R$177.38 nos últimos 7 dias" in a for a in av.alertas), av.alertas)

    def test_ate_3_por_cento_acima_continua_excelente(self):
        self.visto(177.38, 26)
        av = self.avaliar(182.0)                                  # +2,6%
        self.assertGreaterEqual(av.score, 75)
        self.assertFalse(any("o mesmo anúncio custava" in a for a in av.alertas), av.alertas)

    def test_so_conta_o_mesmo_anuncio_nos_ultimos_7_dias(self):
        self.visto(150.0, 26, item_id="MLB9999")                  # outro anúncio do mesmo perfume
        self.visto(150.0, 8 * 24)                                 # o mesmo anúncio, mas há 8 dias
        self.visto(177.38, 0.1)                                   # leitura de agora (6 min): é a própria promo
        self.visto(80.0, 26)                                      # menos da metade: erro de leitura
        av = self.avaliar(186.72)
        self.assertGreaterEqual(av.score, 75)
        self.assertFalse(any("o mesmo anúncio custava" in a for a in av.alertas), av.alertas)

    def test_menor_preco_do_anuncio_acha_a_versao_gravada(self):
        from curadora.historico import menor_preco_do_anuncio
        self.visto(177.38, 26)
        gravadas = [r[0] for r in self.con.execute("SELECT chave FROM precos_ml")]
        chave = self.curadora().chave_de(self.TITULO)
        self.assertTrue(gravadas and all(g.startswith(chave) for g in gravadas), (gravadas, chave))
        self.assertEqual(menor_preco_do_anuncio(self.con, chave, "MLB4911", self.agora, 186.72), 177.38)
        self.assertIsNone(menor_preco_do_anuncio(self.con, chave, "MLB4911", self.agora - 2 * 86400, 186.72))


class TestFalhaDoGeradorNaoEEnvio(unittest.TestCase):
    def test_so_o_enviado_conta_como_ja_mandado(self):
        with tempfile.TemporaryDirectory() as pasta:
            registro = Path(pasta) / "pipeline.jsonl"
            registro.write_text("\n".join(json.dumps(r) for r in (
                {"ts": 1000, "chave": "bareeq|100", "status": "gerador_sem_resposta"},
                {"ts": 1000, "chave": "khamrah|100", "status": "enviado"},
                {"ts": 1000, "chave": "antigo|100"})) + "\n", encoding="utf-8")
            with patch.object(pipeline, "REGISTRO", registro):
                self.assertEqual(pipeline.ja_enviadas(0), {"khamrah|100", "antigo|100"})
                self.assertEqual(pipeline.gerador_nao_respondeu(0), {"bareeq|100"})   # o bloqueio de 6 h continua


class TestChamadaTrocadaEmVariasLinhas(unittest.TestCase):
    """22/10 21:15: o Breno trocou 'DOCE, FLORAL E SOLAR' no Fakhar Gold Extrait e nada foi gravado; a chamada voltou
    em 30/10 18:47 no 'Lattafa Fakhar Gold 100ml'."""

    def setUp(self):
        self.con = conectar(":memory:")
        self.pasta = tempfile.TemporaryDirectory()
        self.arquivo = Path(self.pasta.name) / "headline_preferences_v6.json"
        self.arquivo.write_text(json.dumps({"manual_exact_sku_repertoire": {}}), encoding="utf-8")
        for chave, produto, chamada in (("extrait-fakhar-gold|100", "Fakhar Gold Extrait EDP 100ml", "DOCE, FLORAL E SOLAR"),
                                        ("fakhar-gold|100", "Lattafa Fakhar Gold 100ml", "DOCE, FLORAL E SOLAR ☀️"),
                                        ("kaiak|100", "Natura Kaiak 100ml", "DOCE, FLORAL E SOLAR")):
            self.con.execute("INSERT INTO chamadas(chave, produto, chamada, estado, criada_ts, gravada_ts) "
                             "VALUES (?, ?, ?, 'aprovada', 1, 1)", (chave, produto, chamada))
        self.con.commit()

    def tearDown(self):
        self.pasta.cleanup()

    def estados(self):
        return dict(self.con.execute("SELECT chave, estado FROM chamadas"))

    def test_troca_recusa_so_a_mesma_versao(self):
        # "Fakhar Gold" sem "Extrait" fica: pode ser outra versão (revisão de 31/10: proteger as versões)
        feito = apos_edicao(self.con, "Lattafa Fakhar Gold Extrait EDP", "*DOCE, FLORAL E SOLAR ☀️*\n\nresto",
                            "*BAUNILHA DOURADA*\n\nresto", agora=2000, preferencias=self.arquivo)
        self.assertEqual(feito, "trocada")
        self.assertEqual(self.estados(), {"extrait-fakhar-gold|100": "recusada", "fakhar-gold|100": "aprovada",
                                          "kaiak|100": "aprovada"})

    def mesma_chamada(self, *linhas):
        self.con.execute("DELETE FROM chamadas")
        for chave, produto in linhas:
            self.con.execute("INSERT INTO chamadas(chave, produto, chamada, estado, criada_ts, gravada_ts) "
                             "VALUES (?, ?, 'DOCE, FLORAL E SOLAR', 'aprovada', 1, 1)", (chave, produto))

    def test_fakhar_gold_nao_tira_do_fakhar_black(self):
        self.mesma_chamada(("fakhar-gold|100", "Lattafa Fakhar Gold 100ml"), ("black-fakhar|100", "Lattafa Fakhar Black 100ml"))
        apos_edicao(self.con, "Perfume Fakhar Gold EDP 100ml", "*DOCE, FLORAL E SOLAR*\n\nx", "*OUTRA*\n\nx",
                    agora=2000, preferencias=self.arquivo)
        self.assertEqual(self.estados(), {"fakhar-gold|100": "recusada", "black-fakhar|100": "aprovada"})

    def test_body_splash_nao_tira_do_perfume(self):
        self.mesma_chamada(("kaiak|100", "Natura Kaiak EDT 100ml"), ("kit:kaiak-body|200", "Natura Kaiak Body Splash 200ml"))
        apos_edicao(self.con, "Kaiak Body Splash 200ml", "*DOCE, FLORAL E SOLAR*\n\nx", "*OUTRA*\n\nx",
                    agora=2000, preferencias=self.arquivo)
        self.assertEqual(self.estados(), {"kaiak|100": "aprovada", "kit:kaiak-body|200": "recusada"})

    def test_genero_oposto_nao_e_tocado(self):
        self.mesma_chamada(("atrai|100|m", "Natura Essencial Atrai Masculino 100ml"),
                           ("atrai|100|f", "Natura Essencial Atrai Feminino 100ml"))
        apos_edicao(self.con, "Essencial Atrai Feminino EDP", "*DOCE, FLORAL E SOLAR*\n\nx", "*OUTRA*\n\nx",
                    agora=2000, preferencias=self.arquivo)
        self.assertEqual(self.estados(), {"atrai|100|m": "aprovada", "atrai|100|f": "recusada"})

    def test_mantida_segue_como_antes(self):
        # verificador: contar uso pelas palavras do nome somava uso em outra versão (Body Spray x EDP); o "manteve"
        # só conta com nome exato ou mesma chave, como antes de 31/10
        feito = apos_edicao(self.con, "Lattafa Fakhar Gold Extrait EDP", "*DOCE, FLORAL E SOLAR*\n\nresto",
                            "*DOCE, FLORAL E SOLAR*\n\noutro resto", agora=2000, preferencias=self.arquivo)
        self.assertIsNone(feito)
        self.assertEqual(set(self.estados().values()), {"aprovada"})
        feito = apos_edicao(self.con, "Lattafa Fakhar Gold 100ml", "*DOCE, FLORAL E SOLAR*\n\nresto",
                            "*DOCE, FLORAL E SOLAR*\n\noutro resto", agora=2000, preferencias=self.arquivo)
        self.assertEqual(feito, "mantida")
        usos = dict(self.con.execute("SELECT chave, COALESCE(usos, 0) FROM chamadas"))
        self.assertEqual(usos, {"extrait-fakhar-gold|100": 0, "fakhar-gold|100": 1, "kaiak|100": 0})

    def test_salvo_com_uma_palavra_em_comum(self):
        # 17/10 18:04: 'BERGAMOTA E AMBROXAN' trocada no Salvo; a mesma chamada estava no Sauvage (o original)
        self.con.execute("DELETE FROM chamadas")
        for chave, produto in (("cur:christian-sauvage|100", "Sauvage Masculino de Christian Dior Eau de Parfum"),
                               ("cur:lote5:arabe-salvo-100ml", "Perfume Arabe Masculino Salvo Eau de Parfum 100ml")):
            self.con.execute("INSERT INTO chamadas(chave, produto, chamada, estado, criada_ts, gravada_ts) "
                             "VALUES (?, ?, 'BERGAMOTA E AMBROXAN', 'aprovada', 1, 1)", (chave, produto))
        feito = apos_edicao(self.con, "Maison Alhambra Salvo EDP Masculino 100ml", "*BERGAMOTA E AMBROXAN*\n\nx",
                            "*O DUPE DO SAUVAGE*\n\nx", agora=2000, preferencias=self.arquivo)
        self.assertEqual(feito, "trocada")
        self.assertEqual(self.estados(), {"cur:christian-sauvage|100": "aprovada", "cur:lote5:arabe-salvo-100ml": "recusada"})

    def test_troca_com_nome_exato_tira_tambem_o_mesmo_produto(self):
        self.con.execute("UPDATE chamadas SET produto = 'Lattafa Fakhar Gold Extrait EDP' WHERE chave = 'extrait-fakhar-gold|100'")
        self.con.execute("INSERT INTO chamadas(chave, produto, chamada, estado, criada_ts, gravada_ts) VALUES "
                         "('cur:lote9:fakhar gold extrait', 'Perfume Árabe Lattafa Fakhar Gold Extrait EDP Masculino 100ml', "
                         "'DOCE, FLORAL E SOLAR', 'aprovada', 1, 1)")
        apos_edicao(self.con, "Lattafa Fakhar Gold Extrait EDP", "*DOCE, FLORAL E SOLAR*\n\nx", "*OUTRA*\n\nx",
                    agora=2000, preferencias=self.arquivo)
        self.assertEqual(self.estados(), {"extrait-fakhar-gold|100": "recusada", "cur:lote9:fakhar gold extrait": "recusada",
                                          "fakhar-gold|100": "aprovada", "kaiak|100": "aprovada"})

    def test_produto_sem_relacao_nao_e_tocado(self):
        feito = apos_edicao(self.con, "Perfume Malbec 100ml", "*DOCE, FLORAL E SOLAR*\n\nresto",
                            "*OUTRA*\n\nresto", agora=2000, preferencias=self.arquivo)
        self.assertIsNone(feito)
        self.assertEqual(set(self.estados().values()), {"aprovada"})


if __name__ == "__main__":
    unittest.main()
