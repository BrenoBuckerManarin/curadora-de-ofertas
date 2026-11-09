"""O banco como estava num horário T (04/11, fase 1): nada posterior nem sobrescrito entra como conhecido, e nada é
gravado no banco da curadora."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apoio import CFG

from curadora import momento as m
from curadora.banco import conectar

T = 1_793_800_000          # o horário da decisão
TURNO = T - 3 * 3600       # o começo do turno (fila paralela)


class TestNoHorario(unittest.TestCase):
    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.caminho = self.pasta / "curadora.db"
        con = conectar(self.caminho)
        av = lambda ts, chave, postada=0: con.execute(   # noqa: E731
            "INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, voto, postada, dados) "
            "VALUES (?, 'ao vivo', 'g', ?, 'BOA', 60, 0, 0, ?, '{}')", (ts, chave, postada))
        av(T - 600, "antes")
        av(T + 60, "depois")                       # visto 1 min depois da decisão
        av(T - 7200, "postada-depois", postada=T + 300)
        av(T - 7200, "postada-no-turno", postada=TURNO + 60)
        av(T - 7200, "postada-antes-do-turno", postada=TURNO - 60)
        for ts, chave in ((TURNO - 600, "post-antes-do-turno"), (TURNO + 600, "post-no-turno"), (T + 600, "post-depois")):
            con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES (?, ?, 'GrupoFinal', 'x', ?, 100)",
                        (chave, ts, chave))
        con.execute("INSERT INTO catalogo_ml VALUES ('MLB1', 'a|100', 'A', 'busca', ?)", (T - 86400,))
        con.execute("INSERT INTO catalogo_ml VALUES ('MLB2', 'a|100', 'A', 'recomendacao', ?)", (T + 3600,))   # ts reescrito
        con.execute("INSERT INTO catalogo_ml VALUES ('MLB3', 'a|100', 'A', 'recomendacao', ?)", (T + 3600,))
        con.execute("INSERT INTO precos_ml (ts, chave, item_id, titulo, preco, vendedor, oficial, url, fonte) VALUES "
                    "(?, 'a|100', 'MLB9', 'A', 100, 'X', 1, 'https://www.mercadolivre.com.br/a/p/MLB2', 'recomendacao')",
                    (T - 3600,))
        con.execute("INSERT INTO ofertas (msg_id, ts, grupo, produto, base, chave, preco, cupons) "
                    "VALUES ('o1', ?, 'g', 'x', 'x', 'a|100', 1, '[]')",
                    (T + 1,))
        con.commit()
        con.close()
        self.con = m.conectar_leitura(self.caminho)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def chaves(self, sql):
        return {r[0] for r in self.con.execute(sql)}

    def test_so_o_que_existia_ate_t(self):
        m.no_horario(self.con, T)
        self.assertNotIn("depois", self.chaves("SELECT chave FROM avaliacoes"))
        self.assertIn("antes", self.chaves("SELECT chave FROM avaliacoes"))
        self.assertNotIn("post-depois", self.chaves("SELECT chave FROM postagens"))
        self.assertEqual(self.con.execute("SELECT count(*) FROM ofertas").fetchone()[0], 0)

    def test_marca_de_postada_depois_de_t_volta_a_zero(self):
        m.no_horario(self.con, T)
        postada = dict(self.con.execute("SELECT chave, postada FROM avaliacoes"))
        self.assertEqual(postada["postada-depois"], 0)
        self.assertEqual(postada["postada-no-turno"], TURNO + 60)

    def test_fila_paralela_esquece_o_turno_da_equipe(self):
        m.no_horario(self.con, T, fila_desde=TURNO)
        self.assertEqual(self.chaves("SELECT chave FROM postagens"), {"post-antes-do-turno"})
        postada = dict(self.con.execute("SELECT chave, postada FROM avaliacoes"))
        self.assertEqual(postada["postada-no-turno"], 0)
        self.assertEqual(postada["postada-antes-do-turno"], TURNO - 60)

    def test_catalogo_com_ts_reescrito_so_entra_com_prova_ate_t(self):
        m.no_horario(self.con, T)
        self.assertEqual(self.chaves("SELECT produto_id FROM catalogo_ml"), {"MLB1"})
        # o MLB2 tem o ts reescrito para depois de T, mas uma URL dele foi vista antes de T: entra; o MLB3, não
        self.assertEqual(m.catalogos_conhecidos(self.con, ["a|100"], T), {"a|100": {"MLB1", "MLB2"}})

    def test_nao_grava_no_banco_da_curadora(self):
        m.no_horario(self.con, T)
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("INSERT INTO main.pulos (chave, ts) VALUES ('x', 1)")
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("UPDATE main.avaliacoes SET nota = 'FORA'")

    def test_cada_tabela_tem_politica(self):
        con = sqlite3.connect(self.caminho)
        tabelas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        con.close()
        self.assertFalse(tabelas - set(m.POLITICA), "tabela nova sem política de horário")


class TestChavesIrmas(unittest.TestCase):
    def test_o_mesmo_perfume_escrito_de_outro_jeito_e_nao_a_linha_base(self):
        self.assertTrue(m.irma("asad-bourbon|100", "alta-asad-bourbon-fixacao|100"))   # palavras do título
        self.assertTrue(m.irma("asad-bourbon|100", "assad-bourbon|100"))               # uma letra
        self.assertTrue(m.irma("kaiak|100", "kaiak|100|m"))                            # gênero separado
        self.assertFalse(m.irma("asad-bourbon|100", "asad|100"))                       # o Asad é outro perfume
        self.assertFalse(m.irma("asad-bourbon|100", "asad-bourbon|50"))                # outro tamanho
        self.assertFalse(m.irma("asad-bourbon|100", "a-asad-b-bourbon-c|100"))         # 3 palavras a mais
        self.assertFalse(m.irma("asad-bourbon|100", "kit:asad-bourbon|100"))


class TestRegistroNoHorario(unittest.TestCase):
    def test_so_linhas_ate_t_e_na_fila_paralela_so_antes_do_turno(self):
        pasta = Path(tempfile.mkdtemp())
        try:
            arq = pasta / "pipeline.jsonl"
            arq.write_text("\n".join(json.dumps({"ts": ts, "status": "enviado", "chave": c})
                                     for ts, c in ((TURNO - 60, "a"), (TURNO + 60, "b"), (T + 60, "c"))), encoding="utf-8")
            self.assertEqual([json.loads(x)["chave"] for x in m.registro_ate(arq, T).linhas], ["a", "b"])
            reg = m.registro_ate(arq, T, fila_desde=TURNO)
            self.assertEqual([json.loads(x)["chave"] for x in reg.linhas], ["a"])
            reg.acrescentar({"ts": T, "status": "enviado", "chave": "simulado"})
            self.assertIn("simulado", reg.read_text())
            with self.assertRaises(PermissionError):
                reg.open("a")
        finally:
            shutil.rmtree(pasta, ignore_errors=True)


class TestCuponsNoTempo(unittest.TestCase):
    """A tabela cupons guarda só a última regra e o último visto: refeita pelas mensagens até T."""

    def msg(self, ts, texto, grupofinal=False):
        return {"ts": ts, "grupo": "g1", "nome": "Grupo", "grupofinal": grupofinal, "texto": texto}

    def test_regra_trocada_depois_de_t_nao_vale_em_t(self):
        mensagens = [self.msg(T - 3600, "CUPOM MODA10: 10% OFF acima de R$79, limitado a R$100"),
                     self.msg(T + 3600, "CUPOM MODA10: 20% OFF limitado a R$40")]
        c = m.CuponsNoTempo(mensagens, CFG["marcas"])
        linha = dict(zip(("codigo", "primeiro_ts", "ultimo_ts", "vezes", "pct", "valor", "minimo", "limite"),
                         c.ate(T).linhas()[0]))
        self.assertEqual((linha["pct"], linha["limite"], linha["ultimo_ts"]), (10.0, 100.0, T - 3600))
        linha = c.ate(T + 7200).linhas()[0]
        self.assertEqual(linha[4], 20.0)
        with self.assertRaises(ValueError):
            c.ate(T)   # só anda para a frente

    def test_a_mensagem_do_proprio_minuto_ja_e_conhecida(self):
        c = m.CuponsNoTempo([self.msg(T, "CUPOM MODA10: 10% OFF limitado a R$100")], CFG["marcas"])
        self.assertEqual(len(c.ate(T).linhas()), 1)

    def test_aplicar_esconde_a_tabela_de_agora(self):
        pasta = Path(tempfile.mkdtemp())
        try:
            caminho = pasta / "c.db"
            con = conectar(caminho)
            con.execute("INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, limite) "
                        "VALUES ('MODA10', 1, ?, 9, 20, 40)", (T + 3600,))
            con.commit()
            con.close()
            leitura = m.conectar_leitura(caminho)
            c = m.CuponsNoTempo([self.msg(T - 3600, "CUPOM MODA10: 10% OFF limitado a R$100")], CFG["marcas"])
            m.no_horario(leitura, T)
            c.ate(T).aplicar(leitura)
            self.assertEqual(tuple(leitura.execute("SELECT pct, ultimo_ts FROM cupons").fetchone()), (10.0, T - 3600))
            self.assertEqual(leitura.execute("SELECT pct FROM main.cupons").fetchone()[0], 20.0)   # a de agora fica intacta
            # o horário seguinte: as views são refeitas e a tabela do horário é trocada, nessa ordem, sem erro
            m.no_horario(leitura, T + 60)
            c.ate(T + 60).aplicar(leitura)
            self.assertEqual(leitura.execute("SELECT count(*) FROM cupons").fetchone()[0], 1)
            leitura.close()
        finally:
            shutil.rmtree(pasta, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
