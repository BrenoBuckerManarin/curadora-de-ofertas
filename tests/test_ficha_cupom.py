"""Ficha do cupom em modo sombra (30/10, estudo de cupons): onde o cupom vale, se está vivo, e os ganchos que só registram.

Os casos reais vêm da cópia do banco da VPS de 29/10 (tests/fixtures/ficha_cupom_casos.json): LOJAS2910, LOJAS1710,
TODOSITE2410, ESQFASHBEAUTY2110 e, da cópia com loja/post completados nos sinais de 09-11/10, MODA1110 e MODA1010.
As horas são de Brasília (UTC-3), convertidas para o instante exato.
"""
from __future__ import annotations

import calendar
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from apoio import FIXTURES

import pipeline
from curadora import esgotados
from curadora import ficha_cupom as F
from curadora.banco import conectar
from curadora.ml import CardML

CASOS = json.loads((FIXTURES / "ficha_cupom_casos.json").read_text(encoding="utf-8"))


def hora(dia: str, hhmmss: str) -> float:
    """'2026-10-29', '18:35:10' (Brasília) -> instante. Não depende do fuso da máquina."""
    if len(hhmmss) == 5:
        hhmmss += ":00"
    return calendar.timegm(time.strptime(f"{dia} {hhmmss}", "%Y-%m-%d %H:%M:%S")) + 3 * 3600


def loja(nome: str, oficial: bool = True) -> dict:
    return {"vendedor": nome, "oficial": oficial}


class BaseFicha(unittest.TestCase):
    """Banco em memória com o dia real do cupom e o registro do pipeline com os posts da rodada."""

    def setUp(self):
        F.limpar_cache()
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.registro = self.pasta / "pipeline.jsonl"
        self.registro.write_text("", encoding="utf-8")
        patcher = mock.patch.object(esgotados, "REGISTRO_PIPELINE", self.registro)
        patcher.start()
        self.addCleanup(patcher.stop)
        sombra = mock.patch.object(F, "REGISTRO_SOMBRA", self.pasta / "sombra.jsonl")
        sombra.start()
        self.addCleanup(sombra.stop)
        modo = mock.patch.object(F, "modo", return_value="sombra")
        modo.start()
        self.addCleanup(modo.stop)
        F._RODADA.__init__()
        F._ultimas_candidatas.clear()
        self.con = conectar(":memory:")
        for nome in CASOS["_lojas_oficiais"]:
            self.con.execute("INSERT INTO lojas (nome_norm, nome, oficial, vistas, ultima_vez) VALUES (?,?,1,1,0)",
                             (nome, nome))

    def carregar(self, codigo: str, ate: float | None = None, sem_refs: tuple = ()) -> dict:
        """Põe no banco o dia real do cupom (sinais do Breno até `ate`, se dado)."""
        caso = CASOS[codigo]
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, valor, minimo, limite, "
                         "perfume_ts, equipe_ts) VALUES (?,?,?,?,?,?,?,?,?,?)", caso["cupom"])
        for n, (ts, vendedor, oficial) in enumerate(caso["ofertas"]):
            self.con.execute("INSERT INTO ofertas (msg_id, ts, grupo, produto, base, chave, preco, cupons, vendedor, "
                             "loja_oficial) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f"{codigo}-{n}", ts, "grupo", "perfume", "perfume", "perfume", 100.0, json.dumps([codigo]),
                              vendedor, oficial))
        for ts, tipo, fonte, ref, loja_s, post, detalhe in caso["sinais"]:
            if (ate is not None and ts > ate) or ref in sem_refs:
                continue
            self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, detalhe, ref, loja, post) "
                             "VALUES (?,?,?,?,?,?,?,?)", (codigo, ts, tipo, fonte, detalhe or "", ref or "", loja_s, post))
        for n, (ts, chave, ml) in enumerate(caso["grupofinal"]):
            self.con.execute("INSERT INTO postagens (msg_id, ts, chave, dados) VALUES (?,?,?,?)",
                             (f"h-{codigo}-{n}", ts, chave, json.dumps({"cupons": [codigo], "ml": ml})))
        with self.registro.open("a", encoding="utf-8") as f:
            for post in caso["rodada"]:
                f.write(json.dumps({"cupom_quente": codigo, "link_afiliado": "https://" + post}) + "\n")
        self.con.commit()
        return caso

    def sinal(self, codigo: str, hhmmss: str, loja_chave: str) -> list:
        """O sinal do Breno daquela hora e loja (ts, tipo, fonte, ref, loja, post, detalhe)."""
        caso = CASOS[codigo]
        alvo = hora(caso["dia"], hhmmss)
        return next(s for s in caso["sinais"] if s[0] == alvo and s[4] == loja_chave)

    def vale(self, codigo, nome, quando, oficial=True):
        return F.vale_no_anuncio(self.con, codigo, loja(nome, oficial), quando)

    def linhas(self) -> list:
        arq = self.pasta / "sombra.jsonl"
        return [json.loads(l) for l in arq.read_text(encoding="utf-8").splitlines()] if arq.exists() else []


class TestClasseDaLoja(unittest.TestCase):
    def test_marca_revenda_comum(self):
        self.assertEqual(F.classe_da_loja(vendedor="Natura", oficial=True), "marca")
        self.assertEqual(F.classe_da_loja(vendedor="O Boticário", oficial=True), "marca")
        self.assertEqual(F.classe_da_loja(vendedor="Lipx", oficial=True), "revenda")
        self.assertEqual(F.classe_da_loja(vendedor="SEUPERFUME", oficial=False), "comum")
        self.assertEqual(F.classe_da_loja(vendedor="", oficial=True), "?")

    def test_excecoes_a_mao(self):
        self.assertEqual(F.classe_da_loja(vendedor="Armani Beauty", oficial=True), "marca")
        self.assertEqual(F.classe_da_loja(vendedor="Mercado Livre", oficial=True), "especial")
        self.assertEqual(F.classe_da_loja(vendedor="OUD", oficial=True), "mista")
        self.assertEqual(F.classe_da_loja(vendedor="Rocha Importados", oficial=True), "mista")

    def test_card_do_ml(self):
        self.assertEqual(F.classe_da_loja(CardML(ok=True, vendedor="KLASSEY", loja_oficial=True)), "revenda")
        self.assertEqual(F.classe_da_loja(CardML(ok=True, vendedor="Natura", loja_oficial=True)), "marca")
        self.assertEqual(F.classe_da_loja(CardML(ok=False)), "?")


class TestLOJAS2910(BaseFicha):
    """29/10: cupom de loja da marca. 51 de 54 posts de grupo eram de loja da marca; o Breno pôs no Boticário e na
    Natura às 18:33-18:34, tirou de 4 revendas às 18:35-18:39 (não valia lá) e, às 19:44, do Boticário e da Carolina
    Herrera, onde valia: aí acabou."""
    K, DIA = "LOJAS2910", "2026-10-29"

    def setUp(self):
        super().setUp()
        self.carregar(self.K)

    def test_revenda_fechada_por_concentracao_antes_do_breno(self):
        t = hora(self.DIA, "17:50")
        f = F.ficha_do_dia(self.con, self.K, t)
        self.assertEqual(f.concentrada(), "marca")
        vale, motivo = self.vale(self.K, "Lipx", t)
        self.assertEqual(vale, "não")
        self.assertIn("51 de 54 posts", motivo)
        self.assertEqual(self.vale(self.K, "Natura", t)[0], "sem prova")   # só o Breno abre a classe

    def test_com_conc_fecha_desligado_a_revenda_fica_sem_prova_ate_o_breno_tirar(self):
        # 30/10 (revisor): a opção de a concentração só deixar 'sem prova' (sonda) até o Breno tirar de uma loja fora
        # da marca. Fica atrás de CONC_FECHA (ligado: no replay ela custaria 4 erros nos LOJAS1010/LOJAS2910).
        with mock.patch.object(F, "CONC_FECHA", False):
            t = hora(self.DIA, "17:50")
            self.assertEqual(F.ficha_do_dia(self.con, self.K, t).classe_status("revenda")[0], "concentrada")
            self.assertEqual(self.vale(self.K, "Lipx", t)[0], "sem prova")
            vale, motivo = self.vale(self.K, "Vanvic", hora(self.DIA, "18:40"))   # ele tirou da Lipx às 18:35
            self.assertEqual(vale, "não")
            self.assertIn("o Breno tirou de revenda às 18:3", motivo)

    def test_marca_abre_quando_o_breno_poe(self):
        t = hora(self.DIA, "18:35")
        self.assertEqual(self.vale(self.K, "Carolina Herrera", t), ("sim", "classe aberta"))
        self.assertEqual(self.vale(self.K, "O Boticário", t), ("sim", "loja confirmada"))
        self.assertEqual(self.vale(self.K, "Lipx", t)[0], "não")
        self.assertEqual(F.decidir(self.con, self.K, loja("Natura"), t)["faixa"], 1)
        self.assertEqual(F.decidir(self.con, self.K, loja("Carolina Herrera"), t)["faixa"], 2)

    def test_retiradas_em_revenda_sao_fora_da_classe_e_o_cupom_nao_morre(self):
        for hhmmss, chave in (("18:35:10", "lipx"), ("18:35:36", "suffeinoficial"), ("18:36:13", "2540338692|lipx"),
                              ("18:39:00", "klassey")):
            s = self.sinal(self.K, hhmmss, chave)
            leitura, motivo = F.ler_retirada(self.con, self.K, chave, s[0], s[3])
            self.assertEqual(leitura, "fora da classe", (hhmmss, motivo))
        t = hora(self.DIA, "18:40")
        f = F.ficha_do_dia(self.con, self.K, t)
        self.assertFalse(f.esgotou)
        self.assertEqual(f.seg_fim, 0)
        self.assertEqual(self.vale(self.K, "Natura", t), ("sim", "loja confirmada"))
        self.assertTrue(F.vivo(self.con, self.K, loja("Natura"), t)[0])
        self.assertEqual(self.vale(self.K, "Lipx", t)[0], "não")

    def test_manteve_de_post_da_rodada_nao_confirma_a_loja(self):
        # 19:35 o Breno manteve o cupom no Rabanne que a própria rodada mandou: não vira "loja confirmada"
        f = F.ficha_do_dia(self.con, self.K, hora(self.DIA, "19:42"))
        self.assertEqual(f.loja_status("358625041|rabanne"), "")

    def test_tirar_da_loja_da_marca_confirmada_e_fim(self):
        s = self.sinal(self.K, "19:44:11", "oboticario")
        self.assertEqual(F.ler_retirada(self.con, self.K, "oboticario", s[0], s[3])[0], "fim")
        meio = F.ficha_do_dia(self.con, self.K, hora(self.DIA, "19:44:30"))
        self.assertEqual(meio.vale("1957713361|natura", "marca")[0], "não")   # suspenso: 1 'fim'
        self.assertFalse(meio.esgotou)
        s = self.sinal(self.K, "19:44:45", "carolinaherrera")
        self.assertEqual(F.ler_retirada(self.con, self.K, "carolinaherrera", s[0], s[3])[0], "fim")
        t = hora(self.DIA, "19:46")
        self.assertTrue(F.ficha_do_dia(self.con, self.K, t).esgotou)
        vale, motivo = self.vale(self.K, "Natura", t)
        self.assertEqual(vale, "não")
        self.assertIn("esgotou", motivo)


class TestLOJAS1710(BaseFicha):
    """17/10: o Breno PÔS à mão o LOJAS1710 num post da Lipx (revenda) às 16:09: o cupom volta e a classe revenda abre
    (a válvula do remendo LOJAS, agora pela regra geral: confirmação do Breno abre a classe)."""
    K, DIA = "LOJAS1710", "2026-10-17"

    def setUp(self):
        super().setUp()
        self.carregar(self.K)

    def test_por_a_mao_em_revenda_abre_a_classe(self):
        # 16:01-16:02 ele tirou da Lipx e da OUD sem nada confirmado no dia: 2 'fim', como hoje (esgotou)
        vale, motivo = self.vale(self.K, "KID'S LIFE", hora(self.DIA, "16:05"))
        self.assertEqual(vale, "não")
        self.assertIn("esgotou", motivo)
        t = hora(self.DIA, "16:10")
        self.assertEqual(self.vale(self.K, "KID'S LIFE", t), ("sim", "classe aberta"))
        self.assertEqual(self.vale(self.K, "Lipx", t), ("sim", "loja confirmada"))
        self.assertTrue(F.vivo(self.con, self.K, loja("KID'S LIFE"), t)[0])


class TestCupomDeSiteInteiro(BaseFicha):
    """24/10, TODOSITE2410: às 10h, 13 dos 14 posts de grupo eram de loja comum. É o normal de cupom de site inteiro:
    concentração em 'comum' (ou 'revenda') não fecha nada."""
    K, DIA = "TODOSITE2410", "2026-10-24"

    def test_concentracao_em_comum_nao_fecha_nada(self):
        self.carregar(self.K)
        t = hora(self.DIA, "10:01")
        f = F.ficha_do_dia(self.con, self.K, t)
        self.assertEqual((f.n_grupo["comum"], f.n_grupo["revenda"]), (13, 1))
        self.assertEqual(f.concentrada(), "")
        self.assertEqual(self.vale(self.K, "Lipx", t)[0], "sem prova")
        self.assertEqual(self.vale(self.K, "Natura", t)[0], "sem prova")

    def test_so_marca_concentra(self):
        f = F.Ficha("X", set(), True, False, 0)
        f.n_grupo.update(revenda=10)
        self.assertEqual(f.concentrada(), "")   # 100% em revenda: não fecha
        f.n_grupo.clear()
        f.n_grupo.update(marca=7, comum=1)
        self.assertEqual(f.concentrada(), "marca")
        f.n_grupo.update(comum=2)                # 7 de 10: abaixo de 80%
        self.assertEqual(f.concentrada(), "")


class TestESQFASHBEAUTY2110(BaseFicha):
    """21/10: o cupom acabou por volta das 19h; o Breno tirou de 2 posts da Lipx (loja confirmada) às 19:02-19:03."""
    K, DIA = "ESQFASHBEAUTY2110", "2026-10-21"

    def setUp(self):
        super().setUp()
        self.carregar(self.K)

    def test_dois_fim_seguidos_esgota(self):
        s = self.sinal(self.K, "19:02:20", "2540338692|lipx")
        self.assertEqual(F.ler_retirada(self.con, self.K, s[4], s[0], s[3])[0], "fim")
        t = hora(self.DIA, "19:02:40")
        self.assertEqual(self.vale(self.K, "KID'S LIFE", t)[0], "não")
        self.assertEqual(F.vivo(self.con, self.K, loja("KID'S LIFE"), t), (False, "suspenso (1 retirada onde valia)"))
        s = self.sinal(self.K, "19:03:04", "lipx")
        self.assertEqual(F.ler_retirada(self.con, self.K, s[4], s[0], s[3])[0], "fim")
        f = F.ficha_do_dia(self.con, self.K, hora(self.DIA, "19:04"))
        self.assertTrue(f.esgotou)
        self.assertIn("esgotou", f.vale("kidslife", "revenda")[1])

    def test_o_breno_confirmar_de_novo_desfaz(self):
        t = hora(self.DIA, "19:20")
        self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES "
                         "(?,?,?,?,?,?,?)", (self.K, t, "posto", "breno", "edicao:volta", "kidslife", "meli.la/volta"))
        self.con.commit()
        f = F.ficha_do_dia(self.con, self.K, t + 60)
        self.assertFalse(f.esgotou)
        self.assertEqual(f.vale("kidslife", "revenda"), ("sim", "loja confirmada"))


class TestRodada(BaseFicha):
    """Sonda (1 por loja por dia), disjuntor (2 sondas tiradas seguidas) e ritmo (3 até a 1ª decisão do Breno)."""
    K, DIA = "TESTE3010", "2026-10-30"

    def setUp(self):
        super().setUp()
        self.t0 = hora(self.DIA, "18:00")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct) VALUES (?,?,?,?)",
                         (self.K, self.t0 - 3600, self.t0, 10.0))
        for n, nome in enumerate(("natura", "oboticario")):   # o Breno pôs em 2 lojas da marca: vivo e marca aberta
            self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                             (self.K, self.t0 - 600 + n, "posto", "breno", f"edicao:{n}", nome, f"meli.la/b{n}"))
        self.con.execute("INSERT INTO ofertas (msg_id, ts, grupo, produto, base, chave, preco, cupons, vendedor, "
                         "loja_oficial) VALUES ('g1', ?, 'g', 'p', 'p', 'p', 100, ?, 'Natura', 1)",
                         (self.t0 - 300, json.dumps([self.K])))
        self.con.commit()
        self.r = F.Rodada()

    def envio(self, nome, post, minutos, oficial=True):
        t = self.t0 + minutos * 60
        d = F.decidir(self.con, self.K, loja(nome, oficial), t, self.r, post)
        self.r.anotar(self.K, t, F.loja_e_classe(self.con, loja(nome, oficial))[0], post, d["decisao"])
        return d

    def tirou(self, post, minutos, nome):
        self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                         (self.K, self.t0 + minutos * 60, "esgotou", "breno", f"edicao:t{post}", nome, post))
        self.con.commit()

    def test_sem_prova_vai_como_sonda_uma_vez_por_loja(self):
        d = self.envio("Lipx", "meli.la/s1", 1)
        self.assertEqual((d["vale"], d["decisao"], d["faixa"]), ("sem prova", "sonda", 3))
        self.assertEqual(self.envio("Lipx", "meli.la/s2", 2)["decisao"], "sem")
        self.assertEqual(self.envio("KLASSEY", "meli.la/s3", 3)["decisao"], "sonda")

    def test_disjuntor_depois_de_duas_sondas_tiradas(self):
        self.envio("Lipx", "meli.la/s1", 1)
        self.envio("SEUPERFUME", "meli.la/s2", 2, oficial=False)
        self.tirou("meli.la/s1", 3, "lipx")
        self.tirou("meli.la/s2", 4, "seuperfume")
        self.assertTrue(self.r.disjuntor(self.con, self.K, self.t0 + 300))
        d = self.envio("OUD", "meli.la/s3", 5)   # loja mista: sem prova, mas o disjuntor já desarmou
        self.assertEqual((d["vale"], d["decisao"]), ("sem prova", "sem"))
        self.assertIn("disjuntor", d["sonda_motivo"])
        self.assertEqual(self.envio("Mundo Make", "meli.la/s4", 6, oficial=False)["vale"], "não")   # comum fechou

    def test_ritmo_segura_o_quarto_ate_o_breno_decidir(self):
        for n, nome in enumerate(("Natura", "O Boticário", "Natura")):
            self.assertEqual(self.envio(nome, f"meli.la/r{n}", n + 1)["decisao"], "com")
        d = self.envio("Natura", "meli.la/r3", 4)
        self.assertEqual(d["decisao"], "segura")
        self.assertTrue(d["ritmo_segura"])
        self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                         (self.K, self.t0 + 270, "ativo", "breno", "edicao:r0", "natura", "meli.la/r0"))
        self.con.commit()
        self.assertEqual(self.envio("Natura", "meli.la/r4", 5)["decisao"], "com")


class TestMODA1110(BaseFicha):
    """11/10: o MODA1110 acabou às 10h. Os grupos avisaram "esgotado" às 10:01 e o Breno tirou o cupom da OUD (09:59,
    10:00), do Mercado Livre (10:05, 10:06) e do Armani Beauty (10:07, 10:07, 10:58) sem confirmar nada no meio.
    Revenda e comum estavam abertas: lida sozinha, cada retirada era 'fora da classe' e o cupom seguia 'sim' e vivo até
    11:20 (revisor: 7 envios entre 10:23 e 11:03 sem uso nenhum depois). A regra no ar teria parado às 10:00."""
    K, DIA = "MODA1110", "2026-10-11"
    RALPH = {"vendedor": "Ralph Lauren Fragrance", "oficial": True, "seller_id": "480265022"}
    LIPX = {"vendedor": "Lipx", "oficial": True, "seller_id": "2540338692"}

    def setUp(self):
        super().setUp()
        self.carregar(self.K)

    def test_antes_de_acabar_vale_na_revenda(self):
        t = hora(self.DIA, "09:58")
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, self.LIPX, t), ("sim", "loja confirmada"))
        self.assertTrue(F.vivo(self.con, self.K, self.LIPX, t)[0])
        self.assertEqual(F.classe_da_loja(self.RALPH), "marca")   # 30/10: Ralph Lauren Fragrance é loja da marca

    def test_a_oud_sozinha_e_fora_da_classe(self):
        s = self.sinal(self.K, "09:59:19", "oud")
        self.assertEqual(F.ler_retirada(self.con, self.K, "oud", s[0], s[3])[0], "fora da classe")

    def test_retirada_depois_do_aviso_do_grupo_e_fim(self):
        s = self.sinal(self.K, "10:05:42", "mercadolivre")
        leitura, motivo = F.ler_retirada(self.con, self.K, "mercadolivre", s[0], s[3])
        self.assertEqual(leitura, "fim")
        self.assertIn("avisou que o cupom esgotou às 10:01", motivo)

    def test_varias_classes_seguidas_sem_confirmacao_e_fim_mesmo_sem_o_aviso(self):
        with mock.patch.object(F, "AVISO_FIM_S", -1):
            s = self.sinal(self.K, "10:05:42", "mercadolivre")
            leitura, motivo = F.ler_retirada(self.con, self.K, "mercadolivre", s[0], s[3])
            self.assertEqual(leitura, "fim")
            self.assertIn("2 classes diferentes seguidas (mista:oud, especial:mercadolivre)", motivo)
            self.assertTrue(F.ficha_do_dia(self.con, self.K, s[0] + 1).esgotou)   # as 2 da OUD contam também

    def test_esgotou_as_10_06_e_o_cupom_para(self):
        t = hora(self.DIA, "10:20")
        self.assertEqual(time.strftime("%H:%M:%S", time.gmtime(F.ficha_do_dia(self.con, self.K, t).esgotou - 3 * 3600)),
                         "10:06:04")
        for card in (self.RALPH, self.LIPX):
            d = F.decidir(self.con, self.K, card, t)
            self.assertEqual((d["vale"], d["decisao"]), ("não", "sem"))
            self.assertFalse(d["vivo"])


class TestManteveDeAntesDoFim(BaseFicha):
    """30/10 (revisor): um 'manteve' feito ANTES de uma retirada assenta 5 min depois; ele não pode desfazer o fim nem
    passar à frente da retirada (vale a última palavra pela hora). Caso real: FULL0910 (09/10) e MODA1010 (10/10)."""
    K, DIA = "TESTE3010", "2026-10-30"

    def setUp(self):
        super().setUp()
        self.t0 = hora(self.DIA, "18:00")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct) VALUES (?,?,?,?)",
                         (self.K, self.t0 - 3600, self.t0, 10.0))
        self.con.commit()

    def breno(self, segundos, tipo, loja_s, post):
        self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                         (self.K, self.t0 + segundos, tipo, "breno", f"edicao:{post}", loja_s, post))
        self.con.commit()

    def test_moda1010_o_manteve_das_17_27_nao_desfaz_o_fim_das_17_31(self):
        k, dia = "MODA1010", "2026-10-10"
        self.carregar(k)
        meio = F.ficha_do_dia(self.con, k, hora(dia, "17:33"))   # o manteve das 17:27:07 já assentou
        self.assertEqual(meio.seg_fim, 1)
        self.assertEqual(meio.vale("lipx", "revenda")[0], "não")
        self.assertTrue(F.ficha_do_dia(self.con, k, hora(dia, "17:35")).esgotou)   # 2 'fim' seguidos, como no ar

    def test_esgotou_continua_depois_que_o_manteve_assenta(self):
        self.breno(0, "posto", "natura", "meli.la/n1")
        self.breno(10, "posto", "oboticario", "meli.la/b1")
        self.breno(60, "ativo", "eudora", "meli.la/e1")
        self.breno(120, "esgotou", "natura", "meli.la/n2")
        self.breno(180, "esgotou", "oboticario", "meli.la/b2")
        f = F.ficha_do_dia(self.con, self.K, self.t0 + 60 + F.ASSENTA_S + 30)
        self.assertTrue(f.esgotou)
        self.assertEqual(f.vale("eudora", "marca")[0], "não")
        self.assertFalse(f.vivo(self.t0 + 60 + F.ASSENTA_S + 30, "marca")[0])

    def test_loja_tirada_depois_do_manteve_continua_tirada(self):
        self.breno(0, "posto", "natura", "meli.la/n1")          # a marca abre
        self.breno(60, "ativo", "klassey", "meli.la/k2")         # manteve na Klassey (assenta às 18:06)
        self.breno(120, "fora", "klassey", "meli.la/k3")         # tirou de outro post da Klassey às 18:02
        f = F.ficha_do_dia(self.con, self.K, self.t0 + 600)
        self.assertEqual(f.loja_status("klassey"), "-")
        self.assertEqual(f.vale("klassey", "revenda"), ("não", "o Breno tirou o cupom desta loja hoje"))
        self.assertEqual(f.classe_status("revenda")[0], "fechada")   # tirou de revenda valendo em marca, depois


class TestMesmaVitrine(BaseFicha):
    """30/10 (revisor): o mesmo id de vendedor tem várias vitrines (480265022: Armani Beauty, Lancôme, Ralph Lauren... e
    o "Mercado Livre"; 1459493882: Beleza na Web, Carolina Herrera, Rabanne). A ficha compara a loja pelo nome."""
    K, DIA = "TESTE3010", "2026-10-30"

    def test_compara_pelo_nome(self):
        self.assertFalse(F.mesma_vitrine("480265022|armanibeauty", "480265022|mercadolivre"))
        self.assertTrue(F.mesma_vitrine("2540338692|lipx", "lipx"))
        self.assertTrue(F.mesma_vitrine("2540338692", "2540338692|lipx"))   # sem o nome, o id decide
        self.assertFalse(F.mesma_vitrine("", "lipx"))

    def test_por_no_armani_nao_confirma_o_mercado_livre_nem_a_beleza_na_web(self):
        t0 = hora(self.DIA, "18:00")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct) VALUES (?,?,?,?)",
                         (self.K, t0 - 3600, t0, 10.0))
        for n, (chave, post) in enumerate((("480265022|armanibeauty", "meli.la/a1"),
                                           ("1459493882|carolinaherrera", "meli.la/c1"))):
            self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                             (self.K, t0 + n, "posto", "breno", f"edicao:{n}", chave, post))
        self.con.commit()
        t = t0 + 60
        ml = {"vendedor": "Mercado Livre", "oficial": True, "seller_id": "480265022"}
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, ml, t),
                         ("sem prova", "loja especial: só vale a confirmação da própria loja"))
        bnw = {"vendedor": "Beleza na Web", "oficial": True, "seller_id": "1459493882"}
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, bnw, t)[0], "sem prova")
        self.assertEqual(F.ler_retirada(self.con, self.K, bnw, t)[0], "fora da classe")   # não suspende o cupom
        armani = {"vendedor": "Armani Beauty", "oficial": True, "seller_id": "480265022"}
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, armani, t), ("sim", "loja confirmada"))


class TestConcentracao(BaseFicha):
    """30/10 (revisor): sob concentração em 'marca', só o 'pôs' do Breno abre outra classe (como a válvula do LOJAS no
    ar, que só abre com 'posto'), não um 'manteve' de post que não é da rodada; com CONC_FECHA desligado, a classe fica
    'sem prova' (sonda) em vez de fechada."""
    K, DIA = "TESTE3010", "2026-10-30"

    def setUp(self):
        super().setUp()
        self.t0 = hora(self.DIA, "18:00")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, pct) VALUES (?,?,?,?)",
                         (self.K, self.t0 - 3600, self.t0, 10.0))
        for n in range(10):   # cupom de site inteiro que os grupos só postaram em loja da marca (SITE0110: 11 de 11)
            self.con.execute("INSERT INTO ofertas (msg_id, ts, grupo, produto, base, chave, preco, cupons, vendedor, "
                             "loja_oficial) VALUES (?,?,?,?,?,?,?,?,?,1)",
                             (f"g{n}", self.t0 - 1800 + n * 60, "g", "p", "p", "p", 100, json.dumps([self.K]),
                              ("Natura", "O Boticário")[n % 2]))
        self.breno(-600, "posto", "natura", "meli.la/n1")
        self.breno(-590, "posto", "oboticario", "meli.la/b1")
        self.r = F.Rodada()

    def breno(self, segundos, tipo, loja_s, post):
        self.con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                         (self.K, self.t0 + segundos, tipo, "breno", f"edicao:{post}", loja_s, post))
        self.con.commit()

    def test_concentrada_fecha_ou_vai_como_sonda(self):
        d = F.decidir(self.con, self.K, loja("Lipx"), self.t0, self.r, "meli.la/s1")
        self.assertEqual((d["vale"], d["decisao"]), ("não", "sem"))
        self.assertIn("10 de 10 posts de grupo são de loja marca", d["motivo"])
        with mock.patch.object(F, "CONC_FECHA", False):
            d = F.decidir(self.con, self.K, loja("Lipx"), self.t0, self.r, "meli.la/s1")
            self.assertEqual((d["vale"], d["decisao"], d["faixa"]), ("sem prova", "sonda", 3))

    def test_manteve_sob_concentracao_confirma_a_loja_mas_nao_abre_a_classe(self):
        self.breno(0, "ativo", "lipx", "meli.la/l1")   # post que o Breno manteve (não é da rodada)
        t = self.t0 + F.ASSENTA_S + 10
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, loja("Lipx"), t), ("sim", "loja confirmada"))
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, loja("KLASSEY"), t)[0], "não")
        self.assertEqual(F.ficha_do_dia(self.con, self.K, t).classe_status("revenda")[0], "fechada")

    def test_por_sob_concentracao_abre_a_classe(self):
        self.breno(0, "posto", "klassey", "meli.la/k1")
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, loja("Vanvic"), self.t0 + 10), ("sim", "classe aberta"))

    def test_sem_concentracao_o_manteve_abre_a_classe(self):
        self.con.execute("DELETE FROM ofertas")
        self.con.commit()
        self.breno(0, "ativo", "lipx", "meli.la/l1")
        t = self.t0 + F.ASSENTA_S + 10
        self.assertEqual(F.vale_no_anuncio(self.con, self.K, loja("KLASSEY"), t), ("sim", "classe aberta"))


class TestChaveNoConfig(unittest.TestCase):
    def test_sombra_e_o_padrao_e_desligada_desliga(self):
        import curadora
        self.addCleanup(F._config_cache.clear)
        for cfg, esperado in (({}, "sombra"), ({"ficha_cupom": {"modo": "desligada"}}, "desligada"),
                              ({"ficha_cupom": {"modo": "ligada"}}, "sombra")):   # 30/10: não existe modo ligada
            F._config_cache.clear()
            with mock.patch.object(curadora, "carregar_config", return_value=cfg):
                self.assertEqual(F.modo(), esperado, cfg)


class TestModoSombra(BaseFicha):
    """Os ganchos só registram: as decisões são as mesmas com a sombra ligada, desligada ou dando erro."""

    def test_desligada_nao_grava(self):
        with mock.patch.object(F, "modo", return_value="desligada"):
            self.assertIsNone(F.sombra_do_envio(self.con, chave="x", produto="x", link="https://meli.la/a", card=None,
                                                cupons_grupo=["ABC"], equipe=[], loja_da_marca_cupom="",
                                                atual={"cupom": "", "cupons_no_texto": []}))
            F.sombra_da_retirada(self.con, "ABC", time.time(), "lipx", "meli.la/a", "r", "esgotou")
        self.assertEqual(self.linhas(), [])

    def test_erro_vira_uma_linha_de_aviso(self):
        with mock.patch.object(F, "decidir", side_effect=RuntimeError("quebrou")):
            self.assertIsNone(F.sombra_do_envio(self.con, chave="x", produto="x", link="https://meli.la/a", card=None,
                                                cupons_grupo=["ABC"], equipe=[], loja_da_marca_cupom="",
                                                atual={"cupom": "", "cupons_no_texto": ["ABC"]}))
        linhas = self.linhas()
        self.assertEqual([l["tipo"] for l in linhas], ["aviso"])
        self.assertIn("quebrou", linhas[0]["erro"])

    def test_retirada_igual_com_e_sem_sombra_e_registrada(self):
        k, caso = "LOJAS2910", CASOS["LOJAS2910"]
        s = self.sinal(k, "18:35:10", "lipx")
        resultados = []
        for modo, quebra in (("sombra", False), ("desligada", False), ("sombra", True)):
            F.limpar_cache()
            self.con = conectar(":memory:")
            for nome in CASOS["_lojas_oficiais"]:
                self.con.execute("INSERT INTO lojas (nome_norm, nome, oficial, vistas, ultima_vez) VALUES (?,?,1,1,0)",
                                 (nome, nome))
            self.registro.write_text("", encoding="utf-8")
            self.carregar(k, ate=s[0] - 1)
            efeito = RuntimeError("quebrou") if quebra else None
            with mock.patch.object(F, "modo", return_value=modo), \
                    mock.patch.object(F, "ler_retirada", side_effect=efeito, wraps=None if quebra else F.ler_retirada):
                r = esgotados.tirou_do_post(self.con, k, s[0], "Asad", s[3], s[4], "Lipx", s[5])
            gravado = [tuple(x) for x in self.con.execute("SELECT codigo, ts, tipo, fonte, ref, loja, post FROM "
                                                          "cupom_sinais ORDER BY id")]
            resultados.append((r, gravado))
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(resultados[0], resultados[2])
        linhas = self.linhas()
        self.assertEqual([l["tipo"] for l in linhas], ["retirada", "aviso"])
        self.assertEqual((linhas[0]["gravado"], linhas[0]["ficha"], linhas[0]["diferente"]),
                         (resultados[0][0][0], "fora da classe", resultados[0][0][0] == "esgotou"))
        self.assertEqual(caso["dia"], "2026-10-29")

    def test_envio_avalia_tambem_os_codigos_que_o_breno_confirmou_hoje(self):
        # 30/10 (revisor): sem pedido do grupo nem da equipe, a ficha poria o LOJAS2910 na Natura às 18:36 (loja
        # confirmada, quente): entra com origem 'ficha', para o relatório contar "a ficha poria e você não pôs".
        self.carregar("LOJAS2910")
        t = hora("2026-10-29", "18:36")
        linha = F.sombra_do_envio(self.con, chave="x", produto="Kaiak", link="https://meli.la/novo", card=loja("Natura"),
                                  cupons_grupo=[], equipe=[], loja_da_marca_cupom="",
                                  atual={"cupom": "", "cupons_no_texto": []}, agora=t)
        d = linha["ficha"]["LOJAS2910"]
        self.assertEqual((d["origem"], d["atual"], d["decisao"], d["faixa"]), (["ficha"], "sem", "com", 1))
        # na Lipx a ficha não poria (classe revenda fechada às 18:35): não entra no registro
        linha = F.sombra_do_envio(self.con, chave="y", produto="Asad", link="https://meli.la/novo2", card=loja("Lipx"),
                                  cupons_grupo=[], equipe=[], loja_da_marca_cupom="",
                                  atual={"cupom": "", "cupons_no_texto": []}, agora=t)
        self.assertEqual(linha["ficha"], {})

    def test_pulo_e_candidatas_ficam_anotados(self):
        self.carregar("LOJAS2910")
        t = hora("2026-10-29", "18:36")
        pulo = F.sombra_do_pulo(self.con, chave="kaiak|100", produto="Kaiak", card=loja("Natura"), tirados=["LOJAS2910"],
                                sem_cupom={"nota": "OK", "score": 41}, agora=t)
        self.assertEqual((pulo["tipo"], pulo["ficha_mandaria"]), ("pulo", ["LOJAS2910"]))
        cands = [{"chave": "asad|100", "vendedor": "Lipx", "oficial": True, "queridinho": True},
                 {"chave": "212|100", "vendedor": "Carolina Herrera", "oficial": True},
                 {"chave": "kaiak|100", "vendedor": "Natura", "oficial": True}]
        foto = F.sombra_das_candidatas(self.con, cands, ["LOJAS2910"], t)
        self.assertEqual([c["faixa"]["LOJAS2910"] for c in foto["candidatas"]], [None, 2, 1])
        self.assertEqual(foto["ordem_da_ficha"], [2, 1, 0])
        self.assertIsNone(F.sombra_das_candidatas(self.con, cands, ["LOJAS2910"], t + 60))   # a mesma lista: não repete
        self.assertEqual([l["tipo"] for l in self.linhas()], ["pulo", "candidatas"])

    def test_rodada_que_liga_fica_anotada(self):
        self.carregar("LOJAS2910")
        F.sombra_da_rodada(self.con, ["LOJAS2910"], [], hora("2026-10-29", "18:35"))
        linha = self.linhas()[-1]
        self.assertEqual((linha["tipo"], linha["liga"]), ("rodada", ["LOJAS2910"]))
        self.assertEqual(linha["ficha"]["LOJAS2910"]["concentrada"], "marca")
        self.assertIn("LOJAS2910", F._RODADA.seq)


class TestGanchoDoPipeline(BaseFicha):
    """uma_promo manda a mesma coisa com a sombra ligada, desligada ou quebrando."""

    def rodar(self, modo: str, quebrar: bool = False, pular: bool = False) -> dict:
        con = conectar(":memory:")
        card_ml = CardML(ok=True, item_id="MLB123", titulo="Lattafa Asad 100ml", preco=200.0, vendedor="Lipx",
                         loja_oficial=True, seller_id=7)
        av = SimpleNamespace(cupom_quente=None, card=card_ml, nota="BOA", score=80, preco_efetivo=180.0,
                             identidade=SimpleNamespace(chave="asad|100"),
                             promo=SimpleNamespace(cupons=["CUPOMX"], produto="Lattafa Asad 100ml",
                                                   link="https://meli.la/orig", preco=180.0))
        texto = "*Lattafa Asad 100ml*\nPor R$ 180\nCupom: CUPOMX\nhttps://meli.la/orig"
        card = {"chave": "asad|100", "id": 1, "preco": 180.0, "produto": "Lattafa Asad 100ml"}
        # pular: o CUPOMX sai do texto e, sem ele, a nota cai (o pipeline pula a promo: "sem o cupom não vale")
        cur = SimpleNamespace(cfg={"cupom_quente": {"ligado": True}}, con=con,
                              avaliar=lambda *a, **k: SimpleNamespace(nota="NÃO VALE", score=20))
        saida = {"registrar": [], "normalizadora": [], "vitrine": [], "travas": []}
        trocas = dict(
            candidatas=lambda con, cur, excluir: ([], [card]), misturar=lambda lista, *a, **k: lista,
            pela_pagina=lambda con, cur, c, simular: c, conferir=lambda con, cur, c: (av, {"texto": texto}),
            com_cupom_da_equipe=lambda cur, av, texto: None,
            cupons_que_ficam=lambda cur, av: [] if pular else ["CUPOMX"],
            gerar_pela_api=lambda t: {"body": t.replace("https://produto.mercadolivre.com.br/MLB-123",
                                                        "https://meli.la/NOSSO"), "link": "https://meli.la/NOSSO"},
            texto_da_api=lambda body, produto: body, produtos_com_foto=lambda cur: {"asad|100"},
            para_a_normalizadora=lambda corpo, n, foto="": saida["normalizadora"].append(corpo) or "mid1",
            marcar_feita=lambda *a: None, marcar_mesmo_anuncio=lambda *a: None,
            registrar=lambda d: saida["registrar"].append(dict(d)),
            para_a_vitrine=lambda *a, **k: saida["vitrine"].append(a[3]), marca=lambda cur, p: "Lattafa",
            log=lambda m: None)
        with mock.patch.multiple(pipeline, **trocas), mock.patch.object(F, "modo", return_value=modo), \
                mock.patch.object(pipeline.travas, "guardar", side_effect=lambda *a: saida["travas"].append(a)), \
                mock.patch.object(F, "decidir", side_effect=RuntimeError("quebrou") if quebrar else None,
                                  wraps=None if quebrar else F.decidir):
            saida["volta"] = pipeline.uma_promo(con, cur, set(), "", 1, False)
        return saida

    def test_mesma_saida_com_e_sem_sombra(self):
        ligada = self.rodar("sombra")
        linhas = self.linhas()
        self.assertEqual([l["tipo"] for l in linhas], ["envio"])
        self.assertEqual(linhas[0]["post"], "meli.la/NOSSO")
        self.assertEqual(linhas[0]["classe"], "revenda")
        self.assertEqual(linhas[0]["atual"]["cupons_no_texto"], ["CUPOMX"])
        self.assertEqual(linhas[0]["ficha"]["CUPOMX"]["atual"], "com")
        self.assertEqual(linhas[0]["ficha"]["CUPOMX"]["decisao"], "sem")   # ninguém confirmou o CUPOMX hoje
        desligada = self.rodar("desligada")
        self.assertEqual(len(self.linhas()), 1)
        quebrada = self.rodar("sombra", quebrar=True)
        self.assertEqual(self.linhas()[-1]["tipo"], "aviso")
        for outra in (desligada, quebrada):
            self.assertEqual(ligada["volta"], outra["volta"])
            self.assertEqual(ligada["normalizadora"], outra["normalizadora"])
            self.assertEqual(ligada["vitrine"], outra["vitrine"])
            self.assertEqual([{k: v for k, v in r.items() if k != "gerador_s"} for r in ligada["registrar"]],
                             [{k: v for k, v in r.items() if k != "gerador_s"} for r in outra["registrar"]])
        self.assertEqual(ligada["volta"]["status"], "enviado")
        # 30/10 (revisor): o registro diz se a promo passaria sem o cupom (aqui só foi medida com ele: 180 x 200 no ML)
        self.assertEqual(linhas[0]["atual"]["aceitavel_sem_cupom"]["aceitavel"], None)
        self.assertEqual((linhas[0]["atual"]["nota"], linhas[0]["atual"]["score"]), ("BOA", 80))

    def test_promo_pulada_por_sem_o_cupom_nao_vale_igual_com_e_sem_sombra(self):
        ligada = self.rodar("sombra", pular=True)
        self.assertEqual([l["tipo"] for l in self.linhas()], ["pulo"])
        self.assertEqual(self.linhas()[0]["tirados"], ["CUPOMX"])
        self.assertEqual(self.linhas()[0]["sem_cupom"]["nota"], "NÃO VALE")
        desligada = self.rodar("desligada", pular=True)
        quebrada = self.rodar("sombra", quebrar=True, pular=True)
        self.assertEqual(self.linhas()[-1]["tipo"], "aviso")
        for outra in (desligada, quebrada):
            self.assertIsNone(outra["volta"])
            self.assertEqual(ligada["travas"], outra["travas"])
            self.assertEqual(ligada["normalizadora"], outra["normalizadora"])
        self.assertIsNone(ligada["volta"])
        self.assertEqual(ligada["travas"][0][2], "sem o cupom CUPOMX não vale")


class TestCandidatasNaRodada(BaseFicha):
    """30/10 (revisor): numa rodada de cupom, o pipeline grava as candidatas com a faixa de cada uma na ficha."""

    def test_candidatas_com_a_loja_da_avaliacao(self):
        self.carregar("LOJAS2910")
        for av_id, vendedor in ((1, "Lipx"), (2, "Natura")):
            self.con.execute("INSERT INTO avaliacoes (id, ts, chave, dados) VALUES (?,?,?,?)",
                             (av_id, 0, f"p{av_id}", json.dumps({"ml": {"vendedor": vendedor, "oficial": True}})))
        lista = [{"chave": "p1", "id": 1}, {"chave": "p2", "atual": {"id": 2}}, {"chave": "p3"}]
        t = hora("2026-10-29", "18:36")
        with mock.patch.object(pipeline, "cupons_da_equipe_agora", return_value={"LOJAS2910"}), \
                mock.patch.object(pipeline, "cupom_da_marca_agora", return_value=""), \
                mock.patch.object(pipeline, "queridinhos", return_value={"p1"}), \
                mock.patch.object(F.time, "time", return_value=t):
            pipeline.sombra_das_candidatas(self.con, SimpleNamespace(), lista, [lista[0]])
        linha = self.linhas()[-1]
        self.assertEqual(linha["tipo"], "candidatas")
        self.assertEqual([(c["vendedor"], c["classe"], c["melhor_faixa"], c["queridinho"], c["prioridade"])
                          for c in linha["candidatas"]],
                         [("Lipx", "revenda", None, True, True), ("Natura", "marca", 1, False, False),
                          ("", "?", 3, False, False)])
        self.assertEqual(linha["ordem_da_ficha"], [1, 2, 0])

    def test_erro_vira_aviso(self):
        with mock.patch.object(pipeline, "cupons_da_equipe_agora", side_effect=RuntimeError("quebrou")):
            pipeline.sombra_das_candidatas(self.con, SimpleNamespace(), [{"chave": "p1"}], [])
        self.assertEqual([l["tipo"] for l in self.linhas()], ["aviso"])


class TestSemCupomMedido(unittest.TestCase):
    """30/10 (revisor): aceitavel_sem_cupom só com o que o envio já mediu (sem abrir o ML nem avaliar de novo)."""

    def av(self, efetivo, nota="BOA", score=70):
        return SimpleNamespace(card=CardML(ok=True, preco=200.0), preco_efetivo=efetivo, nota=nota, score=score)

    def test_avaliacao_no_preco_do_ml(self):
        self.assertEqual(pipeline._sem_cupom_medido(self.av(200.0), None, {}),
                         {"nota": "BOA", "score": 70, "aceitavel": True, "medido": "envio"})
        # o envio foi com o cupom da equipe; a conferência (antes dele) estava no preço do ML
        self.assertEqual(pipeline._sem_cupom_medido(self.av(170.0), self.av(200.0, "OK", 30), {})["medido"], "conferida")
        self.assertIs(pipeline._sem_cupom_medido(self.av(200.0, "OK", 30), None, {})["aceitavel"], False)

    def test_so_com_cupom_e_nao_medido(self):
        self.assertIs(pipeline._sem_cupom_medido(self.av(170.0), self.av(200.0), {"_so_com_cupom": True})["aceitavel"],
                      False)
        self.assertIsNone(pipeline._sem_cupom_medido(self.av(170.0), self.av(170.0), {})["aceitavel"])


class TestRelatorio(unittest.TestCase):
    """ficha_relatorio.py: junta a sombra com as decisões do Breno (manteve/pôs = bom, tirou = errado)."""

    def test_placar_por_cupom_e_retirada_lida_diferente(self):
        import ficha_relatorio
        con = conectar(":memory:")
        t = hora("2026-10-29", "18:30")
        for n, (tipo, post) in enumerate((("esgotou", "meli.la/a"), ("ativo", "meli.la/b"), ("ativo", "meli.la/c"),
                                          ("posto", "meli.la/c"))):
            con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                        ("LOJAS2910", t + 300 + n, tipo, "breno", f"edicao:{post}", "x", post))
        envio = lambda post, atual, decisao: {"tipo": "envio", "ts": t, "post": post, "produto": "Asad",
                                              "vendedor": "Lipx", "ficha": {"LOJAS2910": {
                                                  "atual": atual, "decisao": decisao, "vivo": True, "classe": "revenda",
                                                  "motivo": "classe revenda fechada"}}}
        linhas = [envio("meli.la/a", "com", "sem"), envio("meli.la/b", "com", "com"), envio("meli.la/c", "sem", "com"),
                  {"tipo": "retirada", "ts": t + 400, "codigo": "LOJAS2910", "loja": "lipx", "classe": "revenda",
                   "gravado": "esgotou", "ficha": "fora da classe", "motivo": "vale em marca", "diferente": True,
                   "ficha_depois": "segue"}]
        texto = ficha_relatorio.montar(linhas, con, "2026-10-29")
        self.assertIn("hoje (no ar):  1 certos, 1 errados (50% certos); 1 que você pôs à mão", texto)
        self.assertIn("com a ficha:   2 certos, 0 errados (100% certos)", texto)
        self.assertIn("cupons errados que a ficha EVITARIA (1)", texto)
        self.assertIn("a ficha leria diferente: 1", texto)
        self.assertIn("a ficha leria \"não vale nessa classe", texto)

    def test_placar_pela_decisao_da_ficha_e_a_ficha_poria_e_voce_nao_pos(self):
        # 30/10 (revisor): faixa 1, faixa 2, sonda e ritmo separados; e o cupom que a ficha poria num post que foi para
        # o GrupoFinal sem ele (antes ficava escondido em "sem decisão"); e a promo pulada que a ficha mandaria com o cupom.
        import ficha_relatorio
        con = conectar(":memory:")
        t = hora("2026-10-29", "18:30")
        for n, (tipo, post) in enumerate((("ativo", "meli.la/a"), ("esgotou", "meli.la/b"), ("esgotou", "meli.la/d"))):
            con.execute("INSERT INTO cupom_sinais (codigo, ts, tipo, fonte, ref, loja, post) VALUES (?,?,?,?,?,?,?)",
                        ("LOJAS2910", t + 300 + n, tipo, "breno", f"edicao:{post}", "x", post))
        con.execute("INSERT INTO postagens (msg_id, ts, chave, link, dados) VALUES ('h1', ?, 'kaiak', "
                    "'https://meli.la/c', '{\"cupons\": []}')", (t + 200,))
        envio = lambda post, atual, decisao, faixa: {
            "tipo": "envio", "ts": t, "post": post, "produto": "Kaiak", "vendedor": "Natura",
            "ficha": {"LOJAS2910": {"atual": atual, "decisao": decisao, "faixa": faixa, "vivo": True, "classe": "marca",
                                    "motivo": "loja confirmada"}}}
        linhas = [envio("meli.la/a", "com", "com", 1), envio("meli.la/b", "com", "sonda", 3),
                  envio("meli.la/c", "sem", "com", 2), envio("meli.la/d", "com", "segura", 1),
                  envio("meli.la/e", "sem", "com", 1),   # não foi para o GrupoFinal: não conta como "poria"
                  {"tipo": "envio", "ts": t, "post": "meli.la/f", "produto": "Asad", "vendedor": "Lipx",
                   "ficha": {"LOJAS2910": {"atual": "com", "decisao": "sem", "vale": "não", "vivo": True,
                                           "classe": "revenda", "motivo": "classe revenda fechada: 9 de 9 posts de "
                                                                         "grupo são de loja marca (100%): cupom de marca"}}},
                  {"tipo": "pulo", "ts": t, "produto": "Asad", "vendedor": "Lipx", "classe": "revenda",
                   "ficha_mandaria": ["LOJAS2910"], "sem_cupom": {"nota": "OK", "score": 41}},
                  {"tipo": "candidatas", "ts": t, "candidatas": [{"melhor_faixa": 1}, {"melhor_faixa": None}]}]
        texto = ficha_relatorio.montar(linhas, con, "2026-10-29")
        self.assertIn("com o cupom, faixa 1 (loja confirmada): 1 / 0 / 1 / 0", texto)
        self.assertIn("com o cupom, faixa 2 (classe aberta): 0 / 0 / 0 / 1", texto)
        self.assertIn("sonda (faixa 3: sem prova, 1 por loja): 0 / 1 / 0 / 0", texto)
        self.assertIn("segurado pelo ritmo (3 até você decidir): 0 / 1 / 0 / 0", texto)
        self.assertIn("classe fechada só pela concentração dos grupos em marca: 0 / 0 / 1 / 0", texto)
        self.assertIn("A ficha poria o cupom e você não pôs (o post foi para o GrupoFinal sem ele): 1", texto)
        self.assertIn("a ficha mandaria com o cupom: 1", texto)
        self.assertIn("Candidatas nas rodadas de cupom: 1 fotos (faixa 1 50%, não vale 50%)", texto)


if __name__ == "__main__":
    unittest.main()
