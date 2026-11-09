"""Rede de segurança sobre as mensagens reais: uma mudança no leitor não pode piorar nenhum grupo.

Usa 1 de cada 4 mensagens dos grupos-fonte (cerca de 25 mil). É pulado se o arquivo não existir.
Limites calibrados em 02/10/2026, um pouco abaixo do medido (lido 91–99%, identificado 98–100%).
"""
from __future__ import annotations

import collections
import gzip
import json
import unittest

from apoio import CFG, MARCAS, RAIZ

from curadora.identidade import Identificador
from curadora.parser import ler_promo, parece_perfume

ARQUIVO = RAIZ.parent / "dados" / "mensagens_grupos_perfume.jsonl.gz"
MIN_LIDO = 0.88
MIN_IDENTIFICADO = 0.95
MAX_PRECO_ABSURDO = 0.002


@unittest.skipUnless(ARQUIVO.exists(), "mensagens reais não disponíveis")
class TestQualidadeNasMensagensReais(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        grupos = CFG["grupos_fonte"]
        ident = Identificador(MARCAS, CFG["sinonimos"])
        cls.stats = collections.defaultdict(collections.Counter)
        with gzip.open(ARQUIVO, "rt", encoding="utf-8") as fh:
            for n, linha in enumerate(fh):
                if n % 4:
                    continue
                r = json.loads(linha)
                g = str(r.get("chatId", "")).split("@")[0]
                corpo = r.get("body") or ""
                if g not in grupos or r.get("fromMe") or "R$" not in corpo or "http" not in corpo:
                    continue
                s = cls.stats[grupos[g]]
                s["promos"] += 1
                p = ler_promo(corpo, MARCAS)
                if not p.valida or not parece_perfume(p.produto):
                    continue
                s["lidas"] += 1
                s["identificadas"] += bool(ident.analisar(p.produto).base)
                s["preco_absurdo"] += not (5 <= p.preco <= 5000)
                s["de_menor_que_por"] += bool(p.preco_de and p.preco_de <= p.preco)

    def test_todos_os_grupos_aparecem(self):
        self.assertEqual(set(self.stats), set(CFG["grupos_fonte"].values()))

    def test_leitura_por_grupo(self):
        for grupo, s in self.stats.items():
            with self.subTest(grupo=grupo):
                self.assertGreaterEqual(s["lidas"] / s["promos"], MIN_LIDO)
                self.assertGreaterEqual(s["identificadas"] / s["lidas"], MIN_IDENTIFICADO)

    def test_precos_coerentes(self):
        for grupo, s in self.stats.items():
            with self.subTest(grupo=grupo):
                self.assertLessEqual(s["preco_absurdo"] / s["lidas"], MAX_PRECO_ABSURDO)
                self.assertEqual(s["de_menor_que_por"], 0)


if __name__ == "__main__":
    unittest.main()
