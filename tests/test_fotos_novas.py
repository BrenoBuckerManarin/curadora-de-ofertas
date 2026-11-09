"""Coleta automática de fotos dos grupos (17/10, Breno: "sim, pode fazer com esses limites; aumente o limite pra 5")."""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fotos_novas as fn  # noqa: E402

GRUPO = "120363000000000011@g.us"


def imagem(caminho: Path, semente: int, lado: int = 1100, altura: int | None = None) -> Path:
    from PIL import Image
    rnd = random.Random(semente)
    im = Image.new("L", (16, 16))
    im.putdata([rnd.randint(0, 255) for _ in range(256)])
    im = im.resize((lado, altura or lado)).convert("RGB")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    im.save(caminho, quality=90)
    return caminho


class BancoFalso:
    def __init__(self, raiz: Path):
        self.raiz = raiz
        self.existentes: dict[str, list[Path]] = {}

    def fotos(self, titulo):
        return list(self.existentes.get(titulo, []))

    def pasta(self, titulo):
        return self.raiz / ("pasta-" + str(abs(hash(titulo)) % 1000))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.antes = (fn.REGISTRO, fn.ESTADO, fn.LINHAS, fn.texto_da_foto)
        fn.REGISTRO, fn.ESTADO, fn.LINHAS = r / "fotos_novas.jsonl", r / "estado.json", r / "linhas.json"
        fn.LINHAS.write_text(json.dumps({"fora": ["lattafa", "the", "perfume", "feminino", "masculino", "edp"],
                                         "familias": ["kingdom"]}), encoding="utf-8")
        fn.texto_da_foto = lambda caminho: "LATTAFA"
        self.banco = BancoFalso(r / "products")
        self.tap = r / "tap.jsonl"
        self.tap.write_text("", encoding="utf-8")
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE avaliacoes (ts INTEGER, origem TEXT, dados TEXT)")
        self.r = r
        self.n = 0

    def tearDown(self):
        fn.REGISTRO, fn.ESTADO, fn.LINHAS, fn.texto_da_foto = self.antes
        self.tmp.cleanup()

    def post(self, titulo: str, corpo: str, foto: Path):
        self.n += 1
        agora = int(time.time())
        cod = f"Lk{self.n}"
        self.con.execute("INSERT INTO avaliacoes VALUES (?,?,?)", (agora, "ao vivo", json.dumps(
            {"produto": corpo, "nota": "BOA", "link": f"https://meli.la/{cod}", "ml": {"titulo": titulo}})))
        with self.tap.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"messageId": f"M{self.n}", "chatId": GRUPO, "timestamp": agora, "mediaPath": str(foto),
                                "body": f"*{corpo}*\nPor R$ 150\nhttps://meli.la/{cod}"}) + "\n")

    def rodar(self):
        return fn.rodar(self.banco, tap=self.tap, con=self.con)


class TestColeta(Base):
    def test_foto_boa_de_produto_com_poucas_fotos_entra_como_aceitavel(self):
        titulo = "Perfume Lattafa Asad EDP Masculino 100ml"
        self.banco.existentes[titulo] = [imagem(self.r / "banco" / "a.jpg", 1)]
        self.post(titulo, "LATTAFA ASAD", imagem(self.r / "cache" / "nova.jpg", 2))
        c = self.rodar()
        self.assertEqual(c["guardadas"], 1)
        guardadas = list(self.banco.pasta(titulo).glob("acceptable/curadora_grupo_*"))
        self.assertEqual(len(guardadas), 1)
        idx = json.loads((self.banco.pasta(titulo) / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx["raw_items"][-1]["product_guess"], titulo)     # a origem fica registrada
        self.assertEqual(self.rodar()["posts"], 0)                          # a próxima volta só olha o que é novo

    def test_produto_com_5_fotos_pequena_torta_e_mesma_imagem_ficam_de_fora(self):
        cheio = "Perfume Lattafa Yara EDP Feminino 100ml"
        self.banco.existentes[cheio] = [imagem(self.r / "banco" / f"y{i}.jpg", 10 + i) for i in range(5)]
        self.post(cheio, "YARA", imagem(self.r / "cache" / "y.jpg", 20))
        pouco = "Perfume Lattafa Asad EDP Masculino 100ml"
        existente = imagem(self.r / "banco" / "a.jpg", 1)
        self.banco.existentes[pouco] = [existente]
        self.post(pouco, "ASAD", imagem(self.r / "cache" / "pequena.jpg", 3, lado=800))
        self.post(pouco, "ASAD", imagem(self.r / "cache" / "torta.jpg", 4, lado=1100, altura=1600))
        self.post(pouco, "ASAD", imagem(self.r / "cache" / "igual.jpg", 1))          # a mesma imagem do banco
        c = self.rodar()
        self.assertEqual(c["guardadas"], 0)
        self.assertEqual((c["já tem 5+ fotos"], c["menor que 1000px"], c["não é quadrada"],
                          c["mesma imagem que já tem"]), (1, 1, 1, 1))

    def test_genero_e_texto_de_promo(self):
        fem = "Perfume Lattafa The Kingdom Feminino 100ml"
        self.post(fem, "THE KINGDOM MASCULINO", imagem(self.r / "cache" / "m.jpg", 5))   # outro gênero
        self.post(fem, "THE KINGDOM", imagem(self.r / "cache" / "s.jpg", 6))             # linha nas duas versões
        asad = "Perfume Lattafa Asad EDP Masculino 100ml"
        self.post(asad, "ASAD", imagem(self.r / "cache" / "p.jpg", 7))
        fn.texto_da_foto = lambda caminho: "SÓ HOJE R$ 99,90 FRETE GRÁTIS" if caminho.name == "p.jpg" else ""
        c = self.rodar()
        self.assertEqual((c["outro gênero"], c["linha nas duas versões e o post não diz o gênero"],
                          c["texto de promo na foto"], c["guardadas"]), (1, 1, 1, 0))
        self.post(fem, "THE KINGDOM FEMININO", imagem(self.r / "cache" / "f.jpg", 8))     # agora diz o gênero
        self.assertEqual(self.rodar()["guardadas"], 1)

    def test_no_maximo_10_por_dia(self):
        for i in range(12):
            self.post(f"Perfume Lattafa Produto {i} EDP 100ml", f"PRODUTO {i}", imagem(self.r / "cache" / f"{i}.jpg", 100 + i))
        c = self.rodar()
        self.assertEqual(c["guardadas"], fn.MAX_POR_DIA)
        self.assertEqual(fn._registros_de_hoje(), fn.MAX_POR_DIA)
