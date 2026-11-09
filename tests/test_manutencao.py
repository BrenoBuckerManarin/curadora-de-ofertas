"""Cópia de segurança e vigilante."""
from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import apoio  # noqa: F401

import manutencao


class TestBackup(unittest.TestCase):
    def test_copia_consistente_compactada_e_guarda_so_as_ultimas(self):
        with tempfile.TemporaryDirectory() as pasta:
            pasta = Path(pasta)
            banco = pasta / "b.db"
            con = sqlite3.connect(banco)
            con.execute("CREATE TABLE t (x)")
            con.execute("INSERT INTO t VALUES (42)")
            con.commit()
            con.close()
            for n in range(9):
                with mock.patch("time.strftime", return_value=f"2026100{n}-0400"):
                    manutencao.backup(banco, pasta / "backups", guardar=7)
            copias = sorted((pasta / "backups").glob("curadora-*.db.gz"))
            self.assertEqual(len(copias), 7)
            restaurada = pasta / "r.db"
            restaurada.write_bytes(gzip.decompress(copias[-1].read_bytes()))
            lida = sqlite3.connect(restaurada)
            self.assertEqual(lida.execute("SELECT x FROM t").fetchone()[0], 42)
            lida.close()


class TestVigilante(unittest.TestCase):
    def test_ultima_promo_ignora_outros_grupos_e_mensagens_sem_preco(self):
        with tempfile.TemporaryDirectory() as pasta:
            tap = Path(pasta) / "m.jsonl"
            linhas = [{"chatId": "111@g.us", "timestamp": 1000, "body": "Perfume X\nPor R$ 10"},
                      {"chatId": "111@g.us", "timestamp": 2000, "body": "bom dia"},
                      {"chatId": "999@g.us", "timestamp": 3000, "body": "Bebê\nPor R$ 5"}]
            tap.write_text("\n" + "\n".join(json.dumps(l) for l in linhas) + "\n", encoding="utf-8")
            self.assertEqual(manutencao.ultima_promo_dos_grupos(tap, {"111"}), 1000)

    def test_painel_fora_do_ar_e_reportado(self):
        def falha(*a, **k):
            raise ConnectionRefusedError()
        with mock.patch.object(manutencao, "_avisar") as avisar, \
                mock.patch.object(manutencao, "ultima_promo_dos_grupos", return_value=0):
            problemas = manutencao.verificar(abrir=falha)
        self.assertTrue(any("painel não responde" in p for p in problemas))
        avisar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
