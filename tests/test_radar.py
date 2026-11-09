"""API do ML (token, renovação, ofertas) e radar, com respostas falsas no formato real da API."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoio import CFG, DIA, T0, banco_com_historico

from curadora.alertas import Alertador
from curadora.ml_api import ClienteML, SemCredencial
from curadora.radar import Radar
from curadora.score import Curadora

ASAD_NORMAL = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197, 200, 204])]
AGORA = T0 + 15 * DIA


class APIFalsa:
    """Responde como a API do ML; conta as chamadas."""

    def __init__(self, precos=(150.0, 189.9, 210.0)):
        self.chamadas: list[str] = []
        self.precos = precos
        self.token_vencido_uma_vez = False

    def __call__(self, metodo, url, cabecalhos, corpo=None):
        self.chamadas.append(f"{metodo} {url.split('api.mercadolibre.com')[-1]}")
        if url.endswith("/oauth/token"):
            return 200, {"access_token": "novo", "refresh_token": "r2", "expires_in": 21600}
        if self.token_vencido_uma_vez:
            self.token_vencido_uma_vez = False
            return 401, {"message": "invalid access token"}
        if "/products/search" in url:
            return 200, {"results": [{"id": "MLB1", "name": "Perfume Lattafa Asad Eau De Parfum 100ml"},
                                     {"id": "MLB2", "name": "Perfume Lattafa Asad Bourbon 100ml"}]}
        if "/products/MLB1/items" in url:
            return 200, {"results": [
                {"item_id": "MLB10", "price": self.precos[1], "original_price": 289.0, "seller_id": 7,
                 "official_store_id": 123, "shipping": {"free_shipping": True}},
                {"item_id": "MLB11", "price": self.precos[0], "original_price": None, "seller_id": 8,
                 "official_store_id": None, "shipping": {"free_shipping": False}},
                {"item_id": "MLB12", "price": self.precos[2], "seller_id": 9}]}
        if "/users/7" in url:
            return 200, {"nickname": "LIPX"}
        if "/users/" in url:
            return 200, {"nickname": "LOJA QUALQUER"}
        return 404, {}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self.tmp.name)
        (self.pasta / "ml-app.env").write_text("ML_CLIENT_ID=1\nML_CLIENT_SECRET=s\nML_REDIRECT_URI=https://x\n")
        (self.pasta / "ml-token.json").write_text(json.dumps({"access_token": "a", "refresh_token": "r",
                                                               "obtido_em": AGORA}))
        self.api = APIFalsa()
        self.cliente = ClienteML(self.pasta, http=self.api, relogio=lambda: AGORA + 60, intervalo=0)

    def tearDown(self):
        self.tmp.cleanup()


class TestClienteML(Base):
    def test_sem_credencial_avisa_o_que_fazer(self):
        with self.assertRaises(SemCredencial):
            ClienteML(Path(self.tmp.name) / "vazio", http=self.api).token()

    def test_ofertas_vem_ordenadas_com_loja_oficial(self):
        ofertas = self.cliente.ofertas("MLB1")
        self.assertEqual([o.preco for o in ofertas], [150.0, 189.9, 210.0])
        self.assertTrue(ofertas[1].loja_oficial)
        self.assertEqual(ofertas[1].preco_anterior, 289.0)

    def test_token_vencido_renova_e_tenta_de_novo(self):
        self.api.token_vencido_uma_vez = True
        self.assertEqual(len(self.cliente.ofertas("MLB1")), 3)
        self.assertIn("POST /oauth/token", self.api.chamadas)

    def test_acesso_emprestado_so_le_e_nunca_grava(self):
        arquivo = self.pasta / "token-da-coletora.json"
        original = json.dumps({"access_token": "da-coletora", "refresh_token": "nao-mexer", "fetched_at": AGORA})
        arquivo.write_text(original)
        cliente = ClienteML(self.pasta / "vazia", http=self.api, relogio=lambda: AGORA + 3600, intervalo=0,
                            emprestado=arquivo)
        self.assertTrue(cliente.configurado())
        self.assertEqual(len(cliente.ofertas("MLB1")), 3)
        self.assertNotIn("POST /oauth/token", self.api.chamadas)   # nunca renova
        self.assertEqual(arquivo.read_text(), original)            # nunca grava

    def test_acesso_emprestado_vencido_so_espera(self):
        arquivo = self.pasta / "token-da-coletora.json"
        arquivo.write_text(json.dumps({"access_token": "velho", "refresh_token": "r", "fetched_at": AGORA}))
        cliente = ClienteML(self.pasta, http=self.api, relogio=lambda: AGORA + 6 * 3600, intervalo=0, emprestado=arquivo)
        with self.assertRaises(SemCredencial):
            cliente.token()
        self.assertNotIn("POST /oauth/token", self.api.chamadas)

    def test_acesso_emprestado_recusado_pelo_ml_nao_renova(self):
        arquivo = self.pasta / "token-da-coletora.json"
        arquivo.write_text(json.dumps({"access_token": "x", "refresh_token": "r", "fetched_at": AGORA}))
        self.api.token_vencido_uma_vez = True
        cliente = ClienteML(self.pasta, http=self.api, relogio=lambda: AGORA + 60, intervalo=0, emprestado=arquivo)
        with self.assertRaises(SemCredencial):
            cliente.ofertas("MLB1")
        self.assertNotIn("POST /oauth/token", self.api.chamadas)

    def compartilhado(self, idade_s: float, **tok):
        arquivo = self.pasta / "token-da-coletora.json"
        arquivo.write_text(json.dumps({"access_token": "da-coletora", "refresh_token": "r", "fetched_at": AGORA,
                                       **tok}))
        cliente = ClienteML(self.pasta / "vazia", http=self.api, relogio=lambda: AGORA + idade_s, intervalo=0,
                            emprestado=arquivo, app_compartilhado=self.pasta / "ml-app.env")
        return cliente, arquivo

    def test_compartilhado_novo_so_usa(self):
        cliente, _ = self.compartilhado(3600)
        self.assertEqual(cliente.token(), "da-coletora")
        self.assertNotIn("POST /oauth/token", self.api.chamadas)

    def test_compartilhado_espera_a_coletora_renovar_primeiro(self):
        cliente, _ = self.compartilhado(5.1 * 3600)       # a coletora renova com 5h: a curadora ainda não entra
        self.assertEqual(cliente.token(), "da-coletora")
        self.assertNotIn("POST /oauth/token", self.api.chamadas)

    def test_compartilhado_vencido_renova_e_grava_no_formato_da_coletora(self):
        cliente, arquivo = self.compartilhado(8 * 3600)
        self.assertEqual(cliente.token(), "novo")
        gravado = json.loads(arquivo.read_text())
        self.assertEqual((gravado["access_token"], gravado["refresh_token"]), ("novo", "r2"))
        self.assertAlmostEqual(gravado["fetched_at"], AGORA + 8 * 3600)    # a coletora lê fetched_at
        self.assertEqual(cliente.token(), "novo")                          # não renova de novo
        self.assertEqual(self.api.chamadas.count("POST /oauth/token"), 1)

    def test_compartilhado_recusado_usa_o_que_a_coletora_acabou_de_gravar(self):
        cliente, arquivo = self.compartilhado(8 * 3600)

        def coletora_renovou_antes(metodo, url, cabecalhos, corpo=None):
            arquivo.write_text(json.dumps({"access_token": "da-coletora-nova", "refresh_token": "r9",
                                           "fetched_at": AGORA + 8 * 3600}))
            return 400, {"error": "invalid_grant"}
        cliente.http = coletora_renovou_antes
        self.assertEqual(cliente.token(), "da-coletora-nova")
        self.assertEqual(json.loads(arquivo.read_text())["refresh_token"], "r9")   # não estraga o da coletora

    def test_compartilhado_ml_recusou_acesso_renova(self):
        cliente, _ = self.compartilhado(2 * 3600)
        self.api.token_vencido_uma_vez = True
        self.assertEqual(len(cliente.ofertas("MLB1")), 3)
        self.assertEqual(self.api.chamadas.count("POST /oauth/token"), 1)

    def test_token_velho_renova_antes_de_usar(self):
        velho = ClienteML(self.pasta, http=self.api, relogio=lambda: AGORA + 6 * 3600, intervalo=0)
        self.assertEqual(velho.token(), "novo")
        self.assertEqual(json.loads((self.pasta / "ml-token.json").read_text())["refresh_token"], "r2")


class TestRadar(Base):
    def radar(self, precos=(150.0, 189.9, 210.0)):
        self.api.precos = precos
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))
        self.enviados: list[str] = []
        return Radar(cur, self.cliente, Alertador(cur.con, [lambda t, d: self.enviados.append(t)]),
                     nomes=["Lattafa Asad"], relogio=lambda: AGORA + 60)

    def test_acha_o_produto_certo_no_catalogo(self):
        alvos = self.radar().alvos()
        self.assertEqual([p for p, _ in alvos["Lattafa Asad"]], ["MLB1"])  # Asad Bourbon fica de fora

    def test_queda_de_preco_vira_alerta_sem_depender_de_grupo(self):
        radar = self.radar()
        alertas = radar.rodada()
        self.assertTrue(alertas)
        self.assertIn("radar do ML", self.enviados[0])
        self.assertEqual(radar.estatisticas["ofertas_lidas"], 3)

    def test_avalia_tambem_a_mais_barata_em_loja_oficial(self):
        radar = self.radar()
        radar.rodada()
        avaliadas = radar.con.execute("SELECT dados FROM avaliacoes WHERE origem='radar do ML'").fetchall()
        lojas = {json.loads(r["dados"])["loja"] for r in avaliadas}
        self.assertIn("Lipx", lojas)

    def _com_internacionais(self, quantas):
        radar = self.radar()
        originais = radar.cliente.ofertas

        def ofertas(produto_id, limite=50):
            lista = originais(produto_id, limite)
            for o in lista[:quantas]:
                o.internacional = True     # as mais baratas são de vendedor de fora do Brasil
            return lista
        radar.cliente.ofertas = ofertas
        radar.rodada()
        return [(d["preco"], d["internacional"], r["nota"]) for r in radar.con.execute(
            "SELECT nota, dados FROM avaliacoes WHERE origem='radar do ML'") for d in [json.loads(r["dados"])]]

    def test_loja_oficial_so_na_api_com_poucas_vendas_nao_vale(self):
        # 21/10 (Breno: "isso não é loja oficial"): PERFUMELATTAFA7, 804 vendas, com official_store_id na API.
        radar = self.radar()
        original = self.api.__call__

        def api(metodo, url, cabecalhos, corpo=None):
            if "/users/7" in url:
                self.api.chamadas.append(f"{metodo} /users/7")
                return 200, {"nickname": "PERFUMELATTAFA7",
                             "seller_reputation": {"level_id": "5_green", "transactions": {"total": 804}}}
            return original(metodo, url, cabecalhos, corpo)
        radar.cliente.http = api
        radar.rodada()
        dados = [json.loads(r["dados"]) for r in radar.con.execute("SELECT dados FROM avaliacoes WHERE origem='radar do ML'")]
        da_conta = [d for d in dados if "PERFUMELATTAFA7" in str(d.get("loja"))]
        self.assertTrue(da_conta)
        self.assertFalse(any((d.get("ml") or {}).get("oficial") for d in da_conta))
        self.assertFalse(any("loja oficial" in str(d.get("quem_vende")).lower() for d in da_conta))

    def test_a_mais_barata_de_fora_do_brasil_nao_concorre(self):
        vistas = self._com_internacionais(1)
        self.assertTrue(vistas)
        self.assertNotIn(150.0, [p for p, _, _ in vistas])        # a de R$150 era a internacional
        self.assertFalse(any(intl for _, intl, _ in vistas))

    def test_so_tem_oferta_internacional_nao_vira_promo_boa(self):
        vistas = self._com_internacionais(3)
        self.assertTrue(vistas)
        self.assertTrue(all(intl and nota == "NÃO VALE" for _, intl, nota in vistas))

    def test_precos_lidos_viram_historico_real(self):
        radar = self.radar()
        radar.rodada()
        n = radar.con.execute("SELECT COUNT(*) FROM precos_ml WHERE fonte='radar API'").fetchone()[0]
        self.assertEqual(n, 3)

    def test_preco_normal_nao_alerta(self):
        self.assertEqual(self.radar(precos=(199.0, 205.0, 210.0)).rodada(), [])

    def test_lista_extra_e_vigiada_aos_poucos_em_circulo(self):
        radar = self.radar()
        radar.extras = ["Natura Kaiak", "Natura Essencial", "Natura Homem", "O Boticário Malbec", "O Boticário Egeo", "Natura Luna"]
        self.assertEqual(radar.extras_da_vez(), ["Natura Kaiak", "Natura Essencial", "Natura Homem", "O Boticário Malbec"])
        self.assertEqual(radar.extras_da_vez(), ["O Boticário Egeo", "Natura Luna", "Natura Kaiak", "Natura Essencial"])
        radar.extras = []
        self.assertEqual(radar.extras_da_vez(), [])

    def test_sem_credencial_o_radar_so_espera(self):
        cur = Curadora(CFG, banco_com_historico(ASAD_NORMAL))
        radar = Radar(cur, ClienteML(Path(self.tmp.name) / "vazio", http=self.api), Alertador(cur.con, []),
                      nomes=["Lattafa Asad"])
        self.assertEqual(radar.rodada(), [])
        self.assertIn("configurar_ml.py", radar.ultimo_erro)


if __name__ == "__main__":
    unittest.main()
