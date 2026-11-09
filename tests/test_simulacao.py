"""Simulação da rodada diurna (04/11, fase 1, sombra): a fila do pipeline (sem cópia), a fila paralela, nada depois
do minuto e nada gravado."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apoio import CFG

import painel
import pipeline
from curadora import momento, ofertas, simulacao
from curadora.banco import conectar
from curadora.identidade import Identificador

IDENT = Identificador(CFG["marcas"], CFG.get("sinonimos"))
TURNO = time.mktime(time.strptime("2026-11-04 08:00", "%Y-%m-%d %H:%M"))
T1 = TURNO + 30 * 60
T2 = T1 + 7 * 60


def dados(produto, preco, catalogo, vendedor="SEMIKE", oficial=True):
    return {"produto": produto, "preco": preco, "preco_grupo": preco, "link": f"https://meli.la/{catalogo}",
            "loja": vendedor, "loja_selo": oficial, "reputacao": None, "internacional": False, "conflito": "",
            "risco": "", "cupons": [], "ml": {"url": f"https://www.mercadolivre.com.br/x/p/{catalogo}", "preco": preco,
                                              "oficial": oficial, "vendedor": vendedor, "titulo": produto}}


class TestHorarios(unittest.TestCase):
    def test_turno_de_7_em_7_minutos(self):
        h = simulacao.horarios_do_turno("2026-11-04")
        self.assertEqual(time.strftime("%H:%M", time.localtime(h[0])), "08:00")
        self.assertEqual(time.strftime("%H:%M", time.localtime(h[1])), "08:07")
        self.assertLessEqual(time.strftime("%H:%M", time.localtime(h[-1])), "17:50")
        self.assertEqual(len(h), 85)


class TestFilaParalela(unittest.TestCase):
    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.banco = self.pasta / "curadora.db"
        con = conectar(self.banco)
        av = lambda ts, chave, nota, score, d: con.execute(   # noqa: E731
            "INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, voto, postada, dados) "
            "VALUES (?, 'ao vivo', 'Grupo', ?, ?, ?, 0, 0, 0, ?)", (ts, chave, nota, score, json.dumps(d)))
        av(TURNO + 5 * 60, "asad|100", "BOA", 70, dados("Perfume Lattafa Asad 100ml", 150.0, "MLB111"))
        av(TURNO + 5 * 60, "yara|100", "BOA", 68, dados("Perfume Lattafa Yara 100ml", 160.0, "MLB222"))
        av(TURNO + 5 * 60, "9pm|100", "BOA", 66, dados("Perfume Afnan 9pm 100ml", 200.0, "MLB333"))
        # visto 1 min depois de T1, e melhor que todos: não pode aparecer em T1
        av(T1 + 60, "khamrah|100", "IMPERDÍVEL", 95, dados("Perfume Lattafa Khamrah 100ml", 120.0, "MLB444"))
        # a equipe postou o Asad no turno: na fila paralela isso não trava a simulação
        con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco) VALUES ('p1', ?, 'GrupoFinal', "
                    "'Asad', 'asad|100', 150)", (TURNO + 20 * 60,))
        con.commit()
        con.close()
        self.registro = self.pasta / "pipeline.jsonl"
        # o pipeline mandou o Yara 1 h antes do turno: as 8 h valem
        self.registro.write_text(json.dumps({"ts": TURNO - 3600, "status": "enviado", "chave": "yara|100",
                                             "produto": "Yara", "preco": 160.0, "ml_ids": ["MLB222"]}) + "\n",
                                 encoding="utf-8")
        self.antes = (self.banco.stat().st_size, self.banco.stat().st_mtime_ns)
        self.sim = simulacao.Simulacao(self.banco, CFG, IDENT, TURNO, self.registro)

    def tearDown(self):
        self.sim.fechar()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_a_fila_paralela_e_o_que_existia_no_minuto(self):
        v1 = self.sim.vez(T1)
        self.assertEqual((v1.modo, v1.chave), ("normal", "asad|100"))   # o post da equipe no turno não trava
        self.assertEqual(v1.decisao.status, ofertas.SEM_ALTERNATIVA)
        # o Khamrah (visto 1 min depois) não existia para a vez das 08:30
        self.assertEqual(self.sim.con_fila.execute("SELECT count(*) FROM avaliacoes WHERE chave = 'khamrah|100'")
                         .fetchone()[0], 0)
        v2 = self.sim.vez(T2)
        # rodízio de marca (pipeline.misturar): acabou de ir um Lattafa, o Afnan passa na frente do Khamrah
        self.assertEqual(v2.chave, "9pm|100")
        v3 = self.sim.vez(T2 + 7 * 60)
        self.assertEqual(v3.chave, "khamrah|100")   # o Yara continua travado: foi pelo pipeline 1 h antes do turno
        self.assertNotIn("yara|100", [v.chave for v in self.sim.vezes])
        self.assertIn("asad|100", self.sim.registro.read_text())

    def test_o_preco_registrado_e_o_efetivo_do_card(self):
        """04/11, 1ª rodada do replay: o kit de R$151,70 na página e R$139,53 com cupom foi escolhido de 16:31 a 17:20
        porque a simulação registrava o preço da página, e o card (com cupom) parecia 8% mais barato a cada vez."""
        con = conectar(self.banco)
        d = dados("Kit Body Splash Cereja 200ml", 139.53, "MLB555", vendedor="O Boticário")
        d["ml"]["preco"] = 151.70
        con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, voto, postada, dados) "
                    "VALUES (?, 'ao vivo', 'Grupo', 'kit:cereja|200', 'IMPERDÍVEL', 99, 0, 0, 0, ?)",
                    (TURNO + 5 * 60, json.dumps(d)))
        con.commit()
        con.close()
        v1, v2 = self.sim.vez(T1), self.sim.vez(T2)
        self.assertEqual(v1.chave, "kit:cereja|200")
        self.assertNotEqual(v2.chave, "kit:cereja|200")
        self.assertIn('"preco": 139.53', self.sim.registro.read_text())

    def test_conferencia_de_mais_de_2_h_nao_e_escolhida(self):
        """Média de 3 h atrás: o painel ainda mostra (6 h), o pipeline abriria a página; a simulação não pode."""
        pasta = Path(tempfile.mkdtemp())
        try:
            banco = pasta / "c.db"
            con = conectar(banco)
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, voto, postada, dados) "
                        "VALUES (?, 'ao vivo', 'Grupo', 'asad|100', 'OK', 58, 0, 0, 0, ?)",
                        (T1 - 3 * 3600, json.dumps(dados("Perfume Lattafa Asad 100ml", 150.0, "MLB111"))))
            con.commit()
            con.close()
            sim = simulacao.Simulacao(banco, CFG, IDENT, TURNO, self.registro)
            try:
                v = sim.vez(T1)
            finally:
                sim.fechar()
            self.assertEqual(v.modo, "sem escolha")
            self.assertTrue(any("sem conferência nas 2 h" in m for _, m in v.puladas))
        finally:
            shutil.rmtree(pasta, ignore_errors=True)

    def _so_com(self, linhas, registro_linhas=()):
        pasta = Path(tempfile.mkdtemp())
        banco = pasta / "c.db"
        con = conectar(banco)
        for ts, chave, nota, score, d in linhas:
            con.execute("INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, voto, postada, dados) "
                        "VALUES (?, 'ao vivo', 'Grupo', ?, ?, ?, 0, 0, 0, ?)", (ts, chave, nota, score, json.dumps(d)))
        con.commit()
        con.close()
        registro = pasta / "pipeline.jsonl"
        registro.write_text("".join(json.dumps(x) + "\n" for x in registro_linhas), encoding="utf-8")
        return pasta, simulacao.Simulacao(banco, CFG, IDENT, TURNO, registro)

    def test_reserva_e_fundo_quando_nao_ha_promo_nova_boa(self):
        # a única Boa foi pelo pipeline há 7 h: as 8 h travam, a reserva de 6 h deixa repetir
        pasta, sim = self._so_com(
            [(T1 - 600, "asad|100", "BOA", 70, dados("Perfume Lattafa Asad 100ml", 150.0, "MLB111"))],
            [{"ts": T1 - 7 * 3600, "status": "enviado", "chave": "asad|100", "produto": "Asad", "preco": 150.0}])
        try:
            v = sim.vez(T1)
            self.assertEqual((v.modo, v.chave), ("reserva 6h", "asad|100"))
        finally:
            sim.fechar()
            shutil.rmtree(pasta, ignore_errors=True)
        # só uma Ruim de nota 30, nova: vai pelo fundo da fila
        pasta, sim = self._so_com(
            [(T1 - 600, "yara|100", "NÃO VALE", 30, dados("Perfume Lattafa Yara 100ml", 250.0, "MLB222"))])
        try:
            v = sim.vez(T1)
            self.assertEqual((v.modo, v.chave), ("fundo", "yara|100"))
        finally:
            sim.fechar()
            shutil.rmtree(pasta, ignore_errors=True)

    def test_a_rodada_guarda_o_que_ja_tentou_e_passa_para_as_seguintes(self):
        """pipeline.rodar: uma_promo só olha as 12 primeiras da fila e o que ela tentou não volta na rodada. 12 Boas
        de compra internacional (puladas na conferência) na frente de 1 Boa boa: na vez seguinte, a 13ª."""
        marcas = ["Lattafa", "Afnan", "Armaf", "Rasasi", "Al Haramain", "Maison Alhambra", "Zimaya", "Ajmal",
                  "Swiss Arabian", "Nabeel", "Ard Al Zaafaran", "French Avenue"]
        linhas = []
        for i, marca in enumerate(marcas):
            d = dados(f"Perfume {marca} Linha{i} 100ml", 150.0, f"MLB9{i:02d}")
            d["internacional"] = True
            linhas.append((T1 - 600, f"linha{i}|100", "BOA", 80 - i, d))
        linhas.append((T1 - 600, "boa|100", "BOA", 65, dados("Perfume Natura Kaiak Boa 100ml", 120.0, "MLB777")))
        pasta, sim = self._so_com(linhas)
        try:
            v1 = sim.vez(T1)
            # as 12 são puladas na fila normal (e de novo na reserva, que recomeça do zero, como no pipeline)
            self.assertEqual(len({c for c, m in v1.puladas if m == "compra internacional"}), 12)
            self.assertEqual(v1.modo, "sem escolha")
            v2 = sim.vez(T2)
            self.assertEqual((v2.modo, v2.chave), ("normal", "boa|100"))
        finally:
            sim.fechar()
            shutil.rmtree(pasta, ignore_errors=True)
        # com um cupom novo em uso, a lista recomeça (pipeline.rodar): as 12 voltam na frente e a 13ª não sai
        pasta, sim = self._so_com(linhas)
        try:
            sim.vez(T1)
            with mock.patch.object(pipeline, "cupons_da_equipe_agora", lambda cur: {"MODA10"}):
                v2 = sim.vez(T2)
            self.assertNotEqual(v2.chave, "boa|100")
            self.assertEqual(v2.cupons_em_uso, ["MODA10"])
        finally:
            sim.fechar()
            shutil.rmtree(pasta, ignore_errors=True)

    def test_nada_e_gravado_e_tudo_volta_ao_normal(self):
        real, reg_pipeline, reg_painel = time.time, pipeline.REGISTRO, painel.REGISTRO_PIPELINE
        self.sim.vez(T1)
        self.assertIs(time.time, real)
        self.assertIs(pipeline.REGISTRO, reg_pipeline)
        self.assertIs(painel.REGISTRO_PIPELINE, reg_painel)
        self.assertEqual((self.banco.stat().st_size, self.banco.stat().st_mtime_ns), self.antes)
        self.assertNotIn("simulado", self.registro.read_text(encoding="utf-8"))

    def test_resumo_traz_a_frase(self):
        r = self.sim.vez(T1).resumo()
        self.assertEqual((r["hora"], r["chave"], r["status"]), ("08:30", "asad|100", ofertas.SEM_ALTERNATIVA))
        self.assertIn("examinei 1 anúncio", r["frase"])


if __name__ == "__main__":
    unittest.main()
