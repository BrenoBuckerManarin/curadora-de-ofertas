"""Experimento curadora × equipe: horários de 7 em 7 min, escolha independente e relatório do dia."""
from __future__ import annotations

import datetime as dt
import time
import unittest

from apoio import CFG

from curadora.banco import conectar
from curadora.experimento import Experimento, relatorio, slots_do_dia

DIA = dt.date(2026, 10, 6)
CFG_EXP = {**CFG, "experimento": {"inicio": "2026-10-06", "fim": "2026-10-12", "ensaio": "2026-10-05",
                                  "hora_ini": "08:00", "hora_fim": "22:00", "intervalo_min": 7}}


def ts(h: int, m: int, s: int = 0) -> int:
    return int(time.mktime(dt.datetime(2026, 10, 6, h, m, s).timetuple()))


class TestExperimento(unittest.TestCase):
    def setUp(self):
        self.con = conectar(":memory:")
        self.cards = [{"id": 1, "chave": "wazeer|100", "produto": "Al Noble Wazeer 100ml", "preco": 122.0,
                       "nota": "IMPERDÍVEL", "score": 90, "pct_abaixo": 19, "link": "https://meli.la/w"},
                      {"id": 2, "chave": "kaiak|100", "produto": "Kaiak 100ml", "preco": 84.0, "nota": "BOA",
                       "score": 70, "pct_abaixo": 20, "link": "https://meli.la/k"}]
        self.agora = ts(8, 0, 10)
        self.exp = Experimento(self.con, self.escolher, CFG_EXP, relogio=lambda: self.agora)

    def escolher(self, excluir: set):
        return next((c for c in self.cards if c["chave"] not in excluir), None)

    def test_horarios_de_7_em_7_minutos(self):
        slots = slots_do_dia(DIA, CFG_EXP["experimento"])
        self.assertEqual(time.strftime("%H:%M", time.localtime(slots[1])), "08:07")
        self.assertEqual(time.strftime("%H:%M", time.localtime(slots[-1])), "22:00")
        self.assertEqual(len(slots), 121)

    def test_anota_uma_vez_por_horario_sem_repetir_produto(self):
        self.assertEqual(self.exp.tick()["chave"], "wazeer|100")
        self.assertIsNone(self.exp.tick())                    # mesmo horário: não anota de novo
        self.agora = ts(8, 7, 5)
        self.assertEqual(self.exp.tick()["chave"], "kaiak|100")  # não repete o Wazeer nas 6h seguintes
        self.agora = ts(8, 14, 0) + 260                      # serviço parado no horário: pula, não anota atrasado
        self.assertIsNone(self.exp.tick())
        self.agora = ts(22, 30)                              # fora do horário
        self.assertIsNone(self.exp.tick())

    def test_relatorio_compara_no_mesmo_horario(self):
        self.exp.tick()                                          # 08:00 curadora: Wazeer (90)
        self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, nota, score, nota_antes) "
                         "VALUES ('h1', ?, 'Grupo da Equipe', 'CK Be 200ml', 'be-ck|200', 201, 'OK', 50, 'OK')",
                         (ts(8, 3),))
        self.con.commit()
        texto = relatorio(self.con, CFG_EXP, DIA)
        self.assertIn("dia 1 de 7", texto)
        self.assertIn("Equipe postou: 1", texto)
        self.assertIn("Curadora escolheria: 1", texto)
        self.assertIn("Onde a curadora teria escolhido melhor", texto)
        self.assertIn("1 de 1", texto)

    def test_post_que_veio_do_pipeline_nao_conta_como_da_equipe(self):
        import json, tempfile
        from pathlib import Path
        for msg, h, chave in (("h1", 8, "be-ck|200"), ("h2", 9, "kaiak|100")):
            self.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, nota, score, nota_antes) "
                             "VALUES (?, ?, 'Grupo da Equipe', ?, ?, 100, 'OK', 50, 'OK')", (msg, ts(h, 3), chave, chave))
        self.con.commit()
        registro = Path(tempfile.mkdtemp()) / "pipeline.jsonl"
        registro.write_text(json.dumps({"ts": ts(8, 50), "chave": "kaiak|100", "status": "enviado"}) + "\n",
                            encoding="utf-8")
        texto = relatorio(self.con, CFG_EXP, DIA, registro_pipeline=registro)
        self.assertIn("Equipe postou: 1", texto)
        self.assertIn("📮 1 posts da equipe vieram do pipeline", texto)


if __name__ == "__main__":
    unittest.main()
