"""Chamadas prontas: sugestões, decisão do Breno e gravação na lista manual da normalizadora."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import apoio  # noqa: F401

from curadora.banco import conectar
from curadora.chamadas import apos_edicao, decidir, fila, importar, limpar, preencher, tamanho_visual

ITENS = [{"chave": "versace-eros-200ml", "produto": "Versace Eros EDT 200ml", "posts": 5,
          "contexto": {"notas": "menta, maçã verde, baunilha", "ia": ["VERSACE EM FRASCO DE 200ML"]},
          "chamadas": [{"texto": "*menta, maçã verde e baunilha*", "tipo": "notas", "base": "notas do anúncio"},
                       {"texto": "O DEUS GREGO DOS CHEIROSOS", "tipo": "nome", "base": "Eros"},
                       {"texto": "UMA CHAMADA COMPRIDA DEMAIS PARA CABER NA LINHA", "tipo": "nome"}]},
         {"chave": "cool-water-200ml", "produto": "Davidoff Cool Water EDT 200ml", "posts": 2,
          "chamadas": [{"texto": "ÁGUA FRESCA DE VERDADE 🌊", "tipo": "nome"}]}]


class TestChamadas(unittest.TestCase):
    def setUp(self):
        self.con = conectar(":memory:")
        self.pasta = tempfile.TemporaryDirectory()
        self.arquivo = Path(self.pasta.name) / "headline_preferences_v6.json"
        self.arquivo.write_text(json.dumps({"version": 2, "examples": [1, 2], "manual_exact_sku_repertoire": {
            "manual-b3-30": {"product": "Natura Kaiak Pulso 100ml", "headlines": ["*PULSO AQUÁTICO E VERDE*"]}}}),
            encoding="utf-8")
        self.assertEqual(importar(self.con, ITENS, 1000), 3)   # a comprida demais não entra

    def tearDown(self):
        self.pasta.cleanup()

    def lista(self):
        return json.loads(self.arquivo.read_text(encoding="utf-8"))["manual_exact_sku_repertoire"]

    def ids(self, chave):
        return [c["id"] for p in fila(self.con)["produtos"] if p["chave"] == chave for c in p["chamadas"]]

    def test_emoji_conta_como_um_e_o_texto_fica_no_formato(self):
        self.assertEqual(tamanho_visual("O HOMEM DE PRETO CHEIROSO 🤵🏻‍♂️"), 27)
        self.assertEqual(limpar("  *menta,   maçã* "), "MENTA, MAÇÃ")

    def test_fila_mostra_primeiro_o_produto_que_mais_aparece(self):
        f = fila(self.con)
        self.assertEqual([p["produto"] for p in f["produtos"]], ["Versace Eros EDT 200ml", "Davidoff Cool Water EDT 200ml"])
        self.assertEqual(f["resumo"], {"pendente": 3, "aprovada": 0, "recusada": 0})
        self.assertEqual(f["produtos"][0]["contexto"]["ia"], ["VERSACE EM FRASCO DE 200ML"])

    def test_importar_de_novo_nao_duplica_nem_desfaz_decisao(self):
        decidir(self.con, self.ids("cool-water-200ml")[0], "recusada", preferencias=self.arquivo)
        self.assertEqual(importar(self.con, ITENS, 2000), 0)
        self.assertEqual(fila(self.con)["resumo"], {"pendente": 2, "aprovada": 0, "recusada": 1})

    def test_aprovada_entra_na_lista_da_normalizadora_com_copia_do_arquivo(self):
        a, b = self.ids("versace-eros-200ml")
        r = decidir(self.con, a, "aprovada", por="breno", agora=5000, preferencias=self.arquivo)
        self.assertTrue(r["ok"] and r["gravada"])
        decidir(self.con, b, "aprovada", agora=5001, preferencias=self.arquivo)
        entrada = self.lista()["curadora:versace-eros-200ml"]
        self.assertEqual(entrada["product"], "Versace Eros EDT 200ml")
        self.assertEqual(entrada["headlines"], ["*MENTA, MAÇÃ VERDE E BAUNILHA*", "*O DEUS GREGO DOS CHEIROSOS*"])
        self.assertTrue(entrada["human_approved"])
        # o que já estava no arquivo continua igual, e ficou uma cópia de antes
        self.assertEqual(self.lista()["manual-b3-30"]["headlines"], ["*PULSO AQUÁTICO E VERDE*"])
        self.assertEqual(json.loads(self.arquivo.read_text(encoding="utf-8"))["examples"], [1, 2])
        copias = list(Path(self.pasta.name).glob("*.bak-curadora-*"))
        self.assertEqual(len(copias), 1)
        self.assertNotIn("curadora:versace-eros-200ml", json.loads(copias[0].read_text(encoding="utf-8"))["manual_exact_sku_repertoire"])

    def test_recusada_e_pendente_nao_sao_gravadas(self):
        a, b = self.ids("versace-eros-200ml")
        decidir(self.con, a, "recusada", preferencias=self.arquivo)
        self.assertNotIn("curadora:versace-eros-200ml", self.lista())

    def test_editar_antes_de_aprovar_grava_o_texto_dele(self):
        a = self.ids("cool-water-200ml")[0]
        r = decidir(self.con, a, "aprovada", texto="água fresca de verdade", preferencias=self.arquivo)
        self.assertEqual(r["chamada"], "ÁGUA FRESCA DE VERDADE")
        self.assertEqual(self.lista()["curadora:cool-water-200ml"]["headlines"], ["*ÁGUA FRESCA DE VERDADE*"])

    def test_desfazer_ou_trocar_o_texto_tira_a_antiga_da_normalizadora(self):
        a, b = self.ids("versace-eros-200ml")
        decidir(self.con, a, "aprovada", preferencias=self.arquivo)
        decidir(self.con, b, "aprovada", preferencias=self.arquivo)
        decidir(self.con, a, "aprovada", texto="MENTA E MAÇÃ VERDE NO EROS", preferencias=self.arquivo)
        self.assertEqual(self.lista()["curadora:versace-eros-200ml"]["headlines"],
                         ["*O DEUS GREGO DOS CHEIROSOS*", "*MENTA E MAÇÃ VERDE NO EROS*"])
        decidir(self.con, a, "recusada", preferencias=self.arquivo)
        decidir(self.con, b, "pendente", preferencias=self.arquivo)
        self.assertNotIn("curadora:versace-eros-200ml", self.lista())

    def test_texto_comprido_vazio_ou_repetido_e_recusado_sem_gravar(self):
        a, b = self.ids("versace-eros-200ml")
        self.assertIn("28", decidir(self.con, a, "aprovada", texto="X" * 29, preferencias=self.arquivo)["erro"])
        self.assertIn("vazia", decidir(self.con, a, "aprovada", texto="  ", preferencias=self.arquivo)["erro"])
        self.assertIn("já existe", decidir(self.con, a, "aprovada", texto="O deus grego dos cheirosos",
                                           preferencias=self.arquivo)["erro"])
        self.assertNotIn("curadora:versace-eros-200ml", self.lista())

    def test_preencher_poe_tudo_para_valer_de_uma_vez(self):
        # 10/10: "era pra você preencher repertórios de headlines" (sem aprovar uma a uma).
        r = preencher(self.con, "claude, a pedido do Breno", agora=7000, preferencias=self.arquivo)
        self.assertEqual(r, {"preenchidas": 3})
        self.assertEqual(fila(self.con)["resumo"], {"pendente": 0, "aprovada": 3, "recusada": 0})
        self.assertEqual(len(self.lista()["curadora:versace-eros-200ml"]["headlines"]), 2)
        self.assertEqual(self.lista()["curadora:cool-water-200ml"]["headlines"], ["*ÁGUA FRESCA DE VERDADE 🌊*"])

    def post(self, chamada, produto="Versace Eros EDT 200ml"):
        return f"*{chamada}*\n_Loja Oficial_\n\n*{produto}*\n\nPor *R$ 300* 🦸🏻‍♂️\n\nhttps://meli.la/x"

    def test_chamada_que_o_breno_troca_na_edicao_sai_do_repertorio(self):
        preencher(self.con, "claude", preferencias=self.arquivo)
        feito = apos_edicao(self.con, "Versace Eros EDT 200ml", self.post("O DEUS GREGO DOS CHEIROSOS"),
                            self.post("EROS NO PRECINHO 🚨"), preferencias=self.arquivo)
        self.assertEqual(feito, "trocada")
        self.assertEqual(self.lista()["curadora:versace-eros-200ml"]["headlines"], ["*MENTA, MAÇÃ VERDE E BAUNILHA*"])
        trocada = [c for p in fila(self.con)["produtos"] for c in p["chamadas"] if c["estado"] == "recusada"][0]
        self.assertEqual((trocada["chamada"], trocada["por"]), ("O DEUS GREGO DOS CHEIROSOS", "trocada por você na Edição"))

    def test_chamada_mantida_conta_uso_e_trocar_so_o_emoji_nao_e_trocar(self):
        preencher(self.con, "claude", preferencias=self.arquivo)
        com_emoji = self.post("ÁGUA FRESCA DE VERDADE 🌊", "Davidoff Cool Water EDT 200ml")
        sem_emoji = self.post("Água fresca de verdade", "Davidoff Cool Water EDT 200ml")
        self.assertEqual(apos_edicao(self.con, "Davidoff Cool Water EDT 200ml", com_emoji, sem_emoji,
                                     preferencias=self.arquivo), "mantida")
        self.assertEqual(self.lista()["curadora:cool-water-200ml"]["headlines"], ["*ÁGUA FRESCA DE VERDADE 🌊*"])
        usos = [c["usos"] for p in fila(self.con)["produtos"] for c in p["chamadas"] if "ÁGUA" in c["chamada"]]
        self.assertEqual(usos, [1])

    def test_mesmo_produto_guardado_com_dois_nomes_sai_junto(self):
        # Produto novo: a chamada fica guardada com o nome do grupo e com o do anúncio do ML ("chave" e "chave~2").
        importar(self.con, [{"chave": k, "produto": nome, "posts": 0, "chamadas": [{"texto": "O EROS QUE PEGA FOGO 🔥"}]}
                            for k, nome in (("eros-flame", "VERSACE EROS FLAME 200ml"),
                                            ("eros-flame~2", "Perfume Versace Eros Flame Edp 200ml Masculino"))])
        preencher(self.con, "claude", preferencias=self.arquivo)
        self.assertIn("curadora:eros-flame~2", self.lista())
        feito = apos_edicao(self.con, "Versace Eros Flame EDP Masculino 200ml", self.post("O EROS QUE PEGA FOGO 🔥"),
                            self.post("TANGERINA APIMENTADA"), preferencias=self.arquivo)
        self.assertEqual(feito, "trocada")
        self.assertNotIn("curadora:eros-flame", self.lista())
        self.assertNotIn("curadora:eros-flame~2", self.lista())

    def test_post_com_chamada_que_nao_e_daqui_nao_mexe_em_nada(self):
        preencher(self.con, "claude", preferencias=self.arquivo)
        antes = self.arquivo.read_text(encoding="utf-8")
        self.assertIsNone(apos_edicao(self.con, "Lattafa Asad 100ml", self.post("CHEIRO DE RIQUEZA ✨", "Lattafa Asad 100ml"),
                                      self.post("OUTRA COISA", "Lattafa Asad 100ml"), preferencias=self.arquivo))
        self.assertEqual(self.arquivo.read_text(encoding="utf-8"), antes)

    def test_sem_o_arquivo_da_normalizadora_a_decisao_fica_guardada_e_avisa(self):
        a = self.ids("cool-water-200ml")[0]
        r = decidir(self.con, a, "aprovada", preferencias=Path(self.pasta.name) / "nao-existe.json")
        self.assertTrue(r["ok"])
        self.assertFalse(r["gravada"])
        self.assertIn("ficou guardada", r["aviso"])
        # quando o arquivo aparece, a próxima aprovação leva as duas
        decidir(self.con, self.ids("versace-eros-200ml")[0], "aprovada", preferencias=self.arquivo)
        self.assertIn("curadora:cool-water-200ml", self.lista())


if __name__ == "__main__":
    unittest.main()
