"""Servidor do painel de verdade (HTTP) com login exigido, como fica hospedado."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import apoio  # noqa: F401  (ajusta o caminho de importação)

import painel
from curadora.acesso import Acesso
from curadora.banco import conectar


class SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TestServidorComLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.banco = Path(cls.tmp.name) / "teste.db"
        painel.ESTADO.update(banco=cls.banco, exigir_login=True, sem_ml=True)
        cls.servidor = ThreadingHTTPServer(("127.0.0.1", 0), painel.Handler)
        cls.base = f"http://127.0.0.1:{cls.servidor.server_address[1]}"
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        con = conectar(cls.banco)
        cls.convite_admin = Acesso(con).convidar("breno", admin=True)
        cls.convite_equipe = Acesso(con).convidar("colega")
        con.close()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()
        painel.ESTADO.update(exigir_login=False)
        cls.tmp.cleanup()

    def pedir(self, caminho, dados=None, cookie="", origem=True):
        cabecalhos = {"Content-Type": "application/json"}
        if cookie:
            cabecalhos["Cookie"] = cookie
        if origem and dados is not None:
            cabecalhos["Origin"] = self.base
        corpo = json.dumps(dados).encode() if dados is not None else None
        req = urllib.request.Request(self.base + caminho, data=corpo, headers=cabecalhos)
        abrir = urllib.request.build_opener(SemRedirecionar).open
        try:
            with abrir(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def entrar_por_convite(self, convite):
        status, cab, _ = self.pedir("/api/convite", {"token": convite, "senha": "senha-boa-123"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cab["Set-Cookie"])
        return cab["Set-Cookie"].split(";")[0]

    def test_sem_login_vai_para_entrar_e_api_fecha(self):
        status, cab, _ = self.pedir("/")
        self.assertEqual((status, cab["Location"]), (302, "/entrar"))
        self.assertEqual(self.pedir("/api/painel")[0], 401)
        self.assertEqual(self.pedir("/entrar")[0], 200)

    def test_convite_admin_entra_e_convida(self):
        cookie = self.entrar_por_convite(self.convite_admin)
        status, _, corpo = self.pedir("/api/eu", cookie=cookie)
        self.assertEqual(json.loads(corpo), {"nome": "breno", "admin": True, "login": True})
        status, _, corpo = self.pedir("/api/convidar", {"nome": "victor"}, cookie=cookie)
        self.assertIn("/entrar?convite=", json.loads(corpo)["link"])

    def test_equipe_nao_faz_coisa_de_admin(self):
        cookie = self.entrar_por_convite(self.convite_equipe)
        self.assertEqual(self.pedir("/api/convidar", {"nome": "x"}, cookie=cookie)[0], 403)
        self.assertEqual(self.pedir("/api/usuarios", cookie=cookie)[0], 403)
        self.assertEqual(self.pedir("/api/painel?secao=boas", cookie=cookie)[0], 200)

    def test_senha_errada(self):
        status, _, corpo = self.pedir("/api/entrar", {"nome": "breno", "senha": "errada!!"})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", self.pedir("/api/entrar", {"nome": "breno", "senha": "errada!!"})[1])

    def test_pedido_de_outro_site_e_barrado(self):
        status, _, _ = self.pedir("/api/entrar", {"nome": "breno", "senha": "x"}, origem=False)
        self.assertEqual(status, 403)

    def test_convite_traz_codigo_e_esqueci_a_senha_usa_o_codigo(self):
        con = conectar(self.banco)
        convite = Acesso(con).convidar("victor")
        con.close()
        status, _, corpo = self.pedir("/api/convite", {"token": convite, "senha": "senha-boa-123"})
        codigo = json.loads(corpo)["codigo"]
        status, cab, corpo = self.pedir("/api/recuperar", {"nome": "victor", "codigo": codigo, "senha": "senha-nova-456"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cab["Set-Cookie"])
        self.assertNotEqual(json.loads(corpo)["codigo"], codigo)       # sai com um código novo
        self.assertEqual(self.pedir("/api/entrar", {"nome": "victor", "senha": "senha-nova-456"})[0], 200)
        status, cab, corpo = self.pedir("/api/recuperar", {"nome": "victor", "codigo": codigo, "senha": "outra-senha-789"})
        self.assertEqual((status, json.loads(corpo)["erro"]), (400, "nome ou código incorretos"))
        self.assertNotIn("Set-Cookie", cab)

    def test_logado_gera_codigo_novo(self):
        con = conectar(self.banco)
        convite = Acesso(con).convidar("joao")
        con.close()
        cookie = self.entrar_por_convite(convite)
        status, _, corpo = self.pedir("/api/recuperacao", {}, cookie=cookie)
        self.assertRegex(json.loads(corpo)["codigo"], r"^\w{4}-\w{4}-\w{4}$")
        self.assertEqual(self.pedir("/api/recuperacao", {})[0], 401)  # sem login, nada

    def test_cabecalhos_de_seguranca(self):
        _, cab, _ = self.pedir("/entrar")
        self.assertEqual(cab["X-Frame-Options"], "DENY")
        self.assertEqual(cab["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
