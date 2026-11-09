"""O replay em leitura (04/11, fase 1): a nota sobre o preço publicado sem cupom em dobro, o modelo de custo de consultas
e o relatório."""
from __future__ import annotations

import copy
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apoio import CFG

import replay_melhor_oferta as r
from curadora import score
from curadora.banco import conectar
from curadora.ml import CardML

T = int(time.mktime(time.strptime("2026-11-04 09:16", "%Y-%m-%d %H:%M")))
TEXTO = "*Yara Lattafa EDP 100ml*\nDe R$ 249 por *R$ 164* no Pix\nCupom: *DESCONTOS*\nhttps://meli.la/LFTXzyG"


class TestNotaPublicada(unittest.TestCase):
    """09:16 de 04/11: Yara publicado a R$164 com DESCONTOS (10%, mín. R$79, até R$100) e página a R$182,66. O preço
    do post já tem o cupom: a nota sobre o publicado não pode aplicar o cupom de novo."""

    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.con = conectar(self.pasta / "c.db")
        self.con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, minimo, limite, perfume_ts) "
                         "VALUES ('DESCONTOS', ?, ?, 5, 10, 79, 100, ?)", (T - 86400, T - 60, T - 60))
        self.con.commit()
        self.dados = {"texto": TEXTO, "preco_grupo": 164.0, "reputacao": None, "internacional": False,
                      "ml": {"titulo": "Perfume Árabe Yara Lattafa EDP 100ml", "preco": 182.66, "oficial": True,
                             "vendedor": "KID'S LIFE", "url": "https://www.mercadolivre.com.br/x/up/MLBU1"}}
        quente = {"DESCONTOS": {"confirmado_em": T - 60, "por": "post no GrupoFinal"}}
        self.patch = mock.patch.object(score, "cupons_quentes", lambda con, agora: quente)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.con.close()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def historico_pedido(self, cur, dados):
        """O preço que a nota usou e se ela pediu o histórico com cupom (o argumento usa_cupom de referencia)."""
        pedidos, original = [], score.referencia

        def espia(con, chave, agora, dias, usa_cupom, minimo):
            pedidos.append(usa_cupom)
            return original(con, chave, agora, dias, usa_cupom, minimo)
        with mock.patch.object(score, "referencia", espia):
            x = r.nota_publicada(cur, dados, T)
        return x["preco"], pedidos[-1]

    def test_o_preco_publicado_com_o_historico_com_cupom(self):
        cfg = copy.deepcopy(CFG)
        cfg["cupom_quente"] = {**(cfg.get("cupom_quente") or {}), "ligado": True}
        cfg.pop("_arquivo", None)
        com_quente = score.Curadora(cfg, self.con, None)
        # com o cupom quente ligado, o cupom entraria em cima da página: R$182,66 - 10% (não é o publicado)
        self.assertEqual(r.nota_publicada(com_quente, self.dados, T)["preco"], 164.39)
        sem = r.curadora_sem_quente(cfg, self.con)
        # o publicado, comparado com o histórico com cupom (2ª rodada: ia para o sem cupom e o Yara saía IMPERDÍVEL)
        self.assertEqual(self.historico_pedido(sem, self.dados), (164.0, True))
        self.assertTrue(cfg["cupom_quente"]["ligado"])   # o config de quem chamou não muda
        self.assertNotIn("_preco_efetivo", vars(sem))     # o método da classe volta depois da conta

    def test_post_sem_cupom_usa_o_historico_sem_cupom(self):
        sem = r.curadora_sem_quente(CFG, self.con)
        dados = {**self.dados, "texto": TEXTO.replace("Cupom: *DESCONTOS*\n", ""), "preco_grupo": 164.0}
        self.assertEqual(self.historico_pedido(sem, dados), (164.0, False))

    def test_sem_texto_ou_preco_nao_avalia(self):
        sem = r.curadora_sem_quente(CFG, self.con)
        self.assertIsNone(r.nota_publicada(sem, {**self.dados, "texto": ""}, T))
        self.assertIsNone(r.nota_publicada(sem, {**self.dados, "preco_grupo": None}, T))


class TestCusto(unittest.TestCase):
    def test_cache_de_30_min_4_catalogos_e_busca_de_7_dias(self):
        c = r.Custo()
        cats = {f"MLB{i}" for i in range(6)}
        self.assertEqual(c.decisao(T, "a|100", cats, "post"), 4 + 1)       # 4 catálogos + a busca
        self.assertEqual(c.decisao(T + 600, "a|100", cats, "post"), 2)     # os 2 que ficaram de fora; o resto em cache
        self.assertEqual(c.decisao(T + 1200, "a|100", cats, "post"), 0)    # tudo lido há menos de 30 min
        self.assertEqual(c.decisao(T + 2400, "a|100", cats, "post"), 4)    # 30 min depois: lê de novo, sem busca

    def test_teto_por_hora_com_a_reserva_do_inicio_de_cupom(self):
        c = r.Custo()
        for i in range(60):
            c.decisao(T + i, f"k{i}|100", set(), "post")    # 60 buscas na mesma hora
        hora = time.strftime("%Y-%m-%d %H", time.localtime(T))
        self.assertIn(hora, c.resumo([])["horas_acima_do_teto"])          # 60 > 50
        self.assertNotIn(hora, c.resumo([T])["horas_acima_do_teto"])      # com início de cupom: 60 <= 65


class TestRelatorio(unittest.TestCase):
    def test_relatorio_minimo(self):
        texto = r.relatorio_md({"dias": ["2026-11-04"], "gerado": "04/11 18:00", "tempo_s": 1, "versao": {},
                                "posts": [], "vezes": [], "inicios_de_cupom": [],
                                "custos": {"modelo": "m", "por_dia": {}, "por_uso": {}, "pico_hora": 0,
                                           "horas_acima_do_teto": {}, "teto": {"hora": 50, "reserva_hora": 15}}})
        self.assertIn("Replay da melhor oferta", texto)


if __name__ == "__main__":
    unittest.main()
