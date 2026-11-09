"""👎 numa foto: ela sai do rodízio da normalizadora (17/10, Breno: "só se eu puser o emoji de negativo")."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import apoio  # noqa: F401

from curadora import fotos_ruins as fr

NORM = fr.NORMALIZADOR + "@g.us"
EDIC = fr.EDICAO + "@g.us"


def reacao(chat: str, emoji: str, alvo: str, quem: str = "Breno Bucker Manarin") -> dict:
    return {"messageId": "R" + alvo, "chatId": chat, "senderName": quem, "mediaType": "reaction",
            "nativeType": "reactionMessage", "body": f"[Reaction: {emoji} to {alvo}]"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.antes = (fr.BANCO, fr.LOG_NORMALIZADORA, fr.REGISTRO)
        fr.BANCO = raiz / "products"
        fr.LOG_NORMALIZADORA = raiz / "normalizer.log"
        fr.REGISTRO = raiz / "fotos_rejeitadas.jsonl"
        self.foto = self.no_banco("the-kingdom-lattafa-100ml", "breno_edicao_final_1_aaa.jpg", b"FOTO-MASCULINA")
        self.copia = self.no_banco("lattafa-the-kingdom-edp-100ml", "copia.jpg", b"FOTO-MASCULINA")
        self.outra = self.no_banco("the-kingdom-lattafa-100ml", "feminina.jpg", b"FOTO-FEMININA")
        fr.LOG_NORMALIZADORA.write_text(
            "[2026-10-17T16:00:00-03:00] run\n"
            f"[normalizer_report] headline_source=x | image_path={self.foto} image_checked | image_tier=approved_final\n"
            '{"success": true, "messageId": "3EBREL"}\n'
            '{"success": true, "messageId": "3EBPOST", "messageIds": ["3EBPOST"]}\n'
            f"[normalizer_report] headline_source=x | image_path={self.outra} image_checked\n"
            '{"success": true, "messageId": "3EBOUTRO", "messageIds": ["3EBOUTRO"]}\n', encoding="utf-8")
        self.tap = raiz / "tap.jsonl"
        midia = raiz / "img_cache.jpg"
        midia.write_bytes(b"FOTO-MASCULINA")
        self.tap.write_text(json.dumps({"messageId": "ACEDIC", "chatId": EDIC, "mediaPath": str(midia),
                                        "body": "*CHAMADA*"}) + "\n", encoding="utf-8")

    def tearDown(self):
        fr.BANCO, fr.LOG_NORMALIZADORA, fr.REGISTRO = self.antes
        self.tmp.cleanup()

    def no_banco(self, pasta: str, nome: str, conteudo: bytes) -> Path:
        p = fr.BANCO / pasta / "approved_final" / nome
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(conteudo)
        return p


class TestJoinhaAoContrario(Base):
    def test_no_post_do_normalizador_a_foto_e_as_copias_saem_do_rodizio(self):
        dados = fr.reacao(reacao(NORM, "👎", "3EBPOST"), self.tap)
        self.assertEqual(dados["acao"], "retirada")
        self.assertFalse(self.foto.exists())
        self.assertFalse(self.copia.exists())                         # a cópia em outra pasta vai junto
        self.assertTrue((self.foto.parent.parent / "rejected" / self.foto.name).exists())
        self.assertTrue(self.outra.exists())                          # outra foto da pasta fica

    def test_tambem_pelo_aviso_do_normalizador_e_com_tom_de_pele(self):
        self.assertEqual(fr.reacao(reacao(NORM, "👎🏻", "3EBREL"), self.tap)["acao"], "retirada")
        self.assertFalse(self.foto.exists())

    def test_na_edicao_acha_a_foto_pela_mensagem_encaminhada(self):
        dados = fr.reacao(reacao(EDIC, "👎", "ACEDIC"), self.tap)
        self.assertEqual((dados["acao"], len(dados["movidas"])), ("retirada", 2))
        self.assertTrue(self.outra.exists())

    def test_tirar_o_joinha_devolve_a_foto(self):
        fr.reacao(reacao(NORM, "👎", "3EBPOST"), self.tap)
        dados = fr.reacao(reacao(NORM, "", "3EBPOST"), self.tap)
        self.assertEqual((dados["acao"], dados["fotos"]), ("devolvida", 2))
        self.assertTrue(self.foto.exists() and self.copia.exists())

    def test_outro_emoji_outro_grupo_ou_repetido_nao_mexe(self):
        self.assertIsNone(fr.reacao(reacao(NORM, "👍", "3EBPOST"), self.tap))
        self.assertIsNone(fr.reacao(reacao("120363000000000002@g.us", "👎", "3EBPOST"), self.tap))   # GrupoFinal
        self.assertTrue(self.foto.exists())
        fr.reacao(reacao(NORM, "👎", "3EBPOST"), self.tap)
        self.assertIsNone(fr.reacao(reacao(NORM, "👎", "3EBPOST"), self.tap))
        self.assertEqual(len(fr._registros()), 1)

    def test_post_sem_foto_conhecida_so_registra(self):
        self.assertEqual(fr.reacao(reacao(NORM, "👎", "3EBNAOEXISTE"), self.tap)["acao"], "sem_foto")
        self.assertTrue(self.foto.exists())


class TestNaEscuta(Base):
    def test_a_escuta_manda_a_reacao_e_nao_trata_como_promo(self):
        from apoio import CFG, MLFalso, banco_com_historico
        from curadora.alertas import Alertador
        from curadora.aovivo import Escuta
        from curadora.score import Curadora
        cur = Curadora(CFG, banco_com_historico([]), MLFalso({}))
        escuta = Escuta(self.tap, cur, Alertador(cur.con, []), CFG["grupos_fonte"])
        self.assertIsNone(escuta.processar_linha(json.dumps(reacao(NORM, "👎", "3EBPOST"))))
        self.assertFalse(self.foto.exists())
