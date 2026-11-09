"""Modo ao vivo, vigia e alertas: simulando o arquivo de mensagens chegando, sem rede."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apoio import CFG, DIA, T0, MLFalso, banco_com_historico

from curadora.alertas import Alertador, SaidaWhatsApp, formatar_alerta
from curadora.aovivo import Escuta
from curadora.esgotados import esgotados
from curadora.ml import CardML
from curadora.score import Curadora
from curadora.vigia import Vigia

GRUPO = "120363000000000011"  # Fonte Delta #52
ASAD = [("Lattafa Asad 100ml", p, d) for d, p in enumerate([200, 198, 205, 202, 199, 201, 203, 197, 200, 204])]
AGORA = T0 + 15 * DIA


def mensagem(texto: str, ts: int, grupo: str = GRUPO, msg_id: str = "", from_me: bool = False) -> str:
    return json.dumps({"messageId": msg_id or f"id{ts}{hash(texto) % 1000}", "chatId": f"{grupo}@g.us",
                       "fromMe": from_me, "timestamp": ts, "body": texto, "hasMedia": False}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arquivo = Path(self.tmp.name) / "messages.jsonl"
        self.arquivo.write_text("", encoding="utf-8")
        self.enviados: list[str] = []
        self.ml = MLFalso({})
        self.cur = Curadora(CFG, banco_com_historico(ASAD), self.ml)
        self.alertador = Alertador(self.cur.con, [lambda texto, dados: self.enviados.append(texto)])
        self.escuta = Escuta(self.arquivo, self.cur, self.alertador, CFG["grupos_fonte"], relogio=lambda: AGORA + 5)

    def tearDown(self):
        self.tmp.cleanup()

    def chega(self, *linhas: str):
        with self.arquivo.open("a", encoding="utf-8") as fh:
            fh.writelines(linhas)


class TestAoVivo(Base):
    def test_promo_boa_vira_alerta_e_entra_no_historico(self):
        self.ml.cards["https://meli.la/asad"] = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=150,
                                                       vendedor="Lipx", loja_oficial=True)
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/asad", AGORA))
        alertas = self.escuta.processar_disponiveis()
        self.assertEqual(len(alertas), 1)
        self.assertIn("IMPERDÍVEL", self.enviados[0])
        self.assertIn("https://meli.la/asad", self.enviados[0])
        n = self.cur.con.execute("SELECT COUNT(*) FROM ofertas WHERE preco=150").fetchone()[0]
        self.assertEqual(n, 1)

    def test_mesma_oferta_em_outro_grupo_nao_repete_alerta(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA),
                   mensagem("*Asad Lattafa EDP 100ml*\nPor R$ 151\nhttps://meli.la/b", AGORA + 60,
                            grupo="120363000000000003"))
        self.escuta.processar_disponiveis()
        self.assertEqual(len(self.enviados), 1)

    def test_so_alerta_de_novo_se_o_preco_cair_de_verdade(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 151,90\nhttps://meli.la/a", AGORA),
                   mensagem("*Lattafa Asad 100ml*\nPor R$ 152,90\nhttps://meli.la/b", AGORA + 60),   # mesma notícia
                   mensagem("*Lattafa Asad 100ml*\nPor R$ 139,90\nhttps://meli.la/c", AGORA + 120))  # caiu 8%: avisa
        self.escuta.processar_disponiveis()
        self.assertEqual(len(self.enviados), 2)
        self.assertIn("139,90", self.enviados[1])

    def test_promo_ruim_nao_alerta_mas_tambem_e_conferida_no_ml(self):
        # Desde 04/10 toda promo do ML é conferida: Médias e Ruins também com vendedor e preço confirmados.
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 240\nhttps://meli.la/caro", AGORA))
        self.escuta.processar_disponiveis()
        self.assertEqual(self.enviados, [])
        self.assertEqual(self.ml.consultas, ["https://meli.la/caro"])

    def test_ignora_mensagens_proprias_e_de_outros_grupos(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA, from_me=True),
                   mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA, grupo="999"))
        self.escuta.processar_disponiveis()
        self.assertEqual(self.enviados, [])

    def test_continua_de_onde_parou_e_espera_linha_incompleta(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA))
        self.escuta.processar_disponiveis()
        with self.arquivo.open("a", encoding="utf-8") as fh:
            fh.write('{"messageId": "incompleta", "chatId"')  # bridge ainda escrevendo
        self.assertEqual(self.escuta.processar_disponiveis(), [])
        with self.arquivo.open("a", encoding="utf-8") as fh:
            fh.write(f': "{GRUPO}@g.us", "fromMe": false, "timestamp": {AGORA}, "body": "*Lattafa Yara 100ml*\\nPor R$ 90\\nhttps://meli.la/y"}}\n')
        self.escuta.processar_disponiveis()
        self.assertEqual(self.escuta.estatisticas["promos"], 2)  # nenhuma mensagem lida duas vezes

    def test_comecar_do_fim_pula_o_que_ja_existia(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA))
        self.escuta.comecar_do_fim()
        self.assertEqual(self.escuta.processar_disponiveis(), [])

    def test_mensagem_velha_nao_alerta(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA - 5 * 3600))
        self.escuta.processar_disponiveis()
        self.assertEqual(self.enviados, [])

    def test_catalogo_se_reaprende_de_tempos_em_tempos(self):
        self.assertTrue(self.escuta.reaprender_se_preciso(agora=AGORA))
        self.assertFalse(self.escuta.reaprender_se_preciso(agora=AGORA + 3600))       # cedo demais
        self.assertTrue(self.escuta.reaprender_se_preciso(agora=AGORA + 7 * 3600))    # passou o intervalo

    def test_linha_quebrada_nao_derruba(self):
        self.chega("isso não é json\n", mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA))
        self.escuta.processar_disponiveis()
        self.assertEqual(len(self.enviados), 1)


class TestPostagensDaEquipe(Base):
    GRUPOFINAL = "120363000000000002"

    def setUp(self):
        super().setUp()
        self.escuta.postagens = {"id": self.GRUPOFINAL, "nome": "Grupo da Equipe"}

    def test_post_da_equipe_marca_a_promo_como_postada_e_nao_vira_fonte(self):
        self.chega(mensagem("*Lattafa Asad 100ml*\nPor R$ 150\nhttps://meli.la/a", AGORA))
        self.escuta.processar_disponiveis()
        self.chega(mensagem("*ASAD PELA METADE*\n\n*Lattafa Asad EDP 100ml*\n\nPor *R$ 152* no Pix 🦸🏻\n"
                            "https://meli.la/grupofinal", AGORA + 600, grupo=self.GRUPOFINAL))
        self.escuta.processar_disponiveis()
        con = self.cur.con
        self.assertGreater(con.execute("SELECT postada FROM avaliacoes WHERE origem='ao vivo'").fetchone()[0], 0)
        p = con.execute("SELECT nota_antes, avaliacoes, grupo FROM postagens").fetchone()
        self.assertEqual((p["nota_antes"], p["avaliacoes"], p["grupo"]), ("IMPERDÍVEL", 1, "Grupo da Equipe"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ofertas").fetchone()[0],
                         len(ASAD) + 1)  # o post da equipe não entra no histórico (seria preço repetido)

    def test_01_11_aviso_de_cupom_da_equipe_vira_sinal_so_do_que_pode_valer_em_perfume(self):
        # 01/11 (Breno: "quando chegar cupom no grupo, ativar a rodada"): o aviso real do MODA10 das 09:06
        aviso = ("*CUPOM MERCADO LIVRE*\n\n🎟️ *MELIMAIS0111*\n20% OFF máx de R$70 p/ Meli+\n\n🎟️ *MODA10*\n"
                 "10% OFF máx R$20 em Moda & Bem-Estar\n\n_Ative aqui e utilize no produto desejado:_\n"
                 "https://meli.la/XV0IkK1")
        self.chega(mensagem(aviso, AGORA, grupo=self.GRUPOFINAL, msg_id="aviso1"))
        self.escuta.processar_disponiveis()
        con = self.cur.con
        self.assertEqual([tuple(r) for r in con.execute("SELECT codigo, tipo, fonte, ts, ref FROM cupom_sinais")],
                         [("MODA10", "aviso", "equipe", AGORA, "aviso1")])
        self.assertEqual({tuple(r) for r in con.execute("SELECT codigo, equipe_ts, perfume_ts FROM cupons")},
                         {("MODA10", AGORA, AGORA), ("MELIMAIS0111", AGORA, None)})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 0)
        # 15 min depois a equipe diz no GrupoFinal que acabou: para a rodada que o aviso ligou (com 2 códigos e sem 🎟️, quem
        # sabe de qual é o fim é o leitor dos grupos, oração por oração)
        self.chega(mensagem("⚠️ Pessoal, o cupom *MODA10* esgotou! Agora vale o cupom *MELIMAIS0111*", AGORA + 900,
                            grupo=self.GRUPOFINAL, msg_id="fim1"))
        self.escuta.processar_disponiveis()
        self.assertEqual(sorted(esgotados(con, AGORA + 960)), ["MODA10"])

    def test_01_11_aviso_com_titulo_que_parece_produto_tambem_e_aviso(self):
        # 2ª revisão: "*NOVOS CUPONS MERCADO LIVRE*" com "máx R$20" era lido como promo (produto e preço) e não ligava
        aviso = ("*NOVOS CUPONS MERCADO LIVRE*\n\n🎟️ *MODA10*\n10% OFF máx R$20 em Moda & Bem-Estar\n\n"
                 "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        # e o post da equipe com cupom (volume no nome; com ou sem a regra escrita) continua sendo post, não aviso
        post = ("*ASAD PELA METADE*\n\n*Lattafa Asad EDP 100ml*\n\nPor *R$ 152* no Pix 🦸🏻\nCupom: *CAMISA10* 🎟️\n"
                "https://meli.la/grupofinal")
        post_com_regra = ("*ASAD PELA METADE*\n\n*Lattafa Asad EDP 100ml*\n\nPor *R$ 152* no Pix 🦸🏻\n"
                          "Cupom: *JOGADOR* (5% OFF máx R$30) 🎟️\nhttps://meli.la/bTbyF1")
        # restrição na linha do cupom novo não o marca como esgotado (o leitor dos grupos ignora o cupom anunciado)
        restricao = "*CUPOM MERCADO LIVRE*\n🎟 10% OFF máx R$30 em Moda & Bem-Estar (não vale com outros cupons): *MODA20*"
        self.chega(mensagem(aviso, AGORA, grupo=self.GRUPOFINAL, msg_id="aviso2"),
                   mensagem(post, AGORA + 60, grupo=self.GRUPOFINAL, msg_id="post1"),
                   mensagem(post_com_regra, AGORA + 120, grupo=self.GRUPOFINAL, msg_id="post2"),
                   mensagem(restricao, AGORA + 180, grupo=self.GRUPOFINAL, msg_id="aviso3"))
        self.escuta.processar_disponiveis()
        con = self.cur.con
        self.assertEqual([tuple(r) for r in con.execute("SELECT codigo, tipo FROM cupom_sinais ORDER BY ts")],
                         [("MODA10", "aviso"), ("MODA20", "aviso")])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM postagens").fetchone()[0], 2)

    def test_01_11_banco_ocupado_no_aviso_le_a_linha_de_novo_sem_perder_nem_repetir(self):
        import sqlite3
        from unittest import mock
        from curadora import aovivo
        aviso = ("*CUPOM MERCADO LIVRE*\n\n🎟️ *MODA10*\n10% OFF máx R$20 em Moda & Bem-Estar\n\n"
                 "_Ative aqui e utilize no produto desejado:_\nhttps://meli.la/XV0IkK1")
        self.chega(mensagem(aviso, AGORA, grupo=self.GRUPOFINAL, msg_id="aviso1"))
        original, vezes = aovivo.registrar_aviso_da_equipe, []

        def ocupado(*a, **k):
            vezes.append(1)
            if len(vezes) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(*a, **k)
        with mock.patch.object(aovivo, "registrar_aviso_da_equipe", ocupado):
            self.escuta.processar_disponiveis()
            self.assertEqual(self.cur.con.execute("SELECT COUNT(*) FROM cupom_sinais").fetchone()[0], 0)
            self.escuta.processar_disponiveis()
            self.escuta.processar_disponiveis()
        self.assertEqual([tuple(r) for r in self.cur.con.execute("SELECT codigo, tipo FROM cupom_sinais")],
                         [("MODA10", "aviso")])

    def test_post_que_a_curadora_nao_viu(self):
        self.chega(mensagem("*Ck Be Calvin Klein EDT Unissex 200ml*\n\nPor *R$ 201* no Pix\nhttps://meli.la/x",
                            AGORA, grupo=self.GRUPOFINAL))
        self.escuta.processar_disponiveis()
        p = self.cur.con.execute("SELECT nota_antes, avaliacoes FROM postagens").fetchone()
        self.assertEqual((p["nota_antes"], p["avaliacoes"]), (None, 0))


class TestVigia(Base):
    def test_queda_de_preco_num_top_vira_alerta(self):
        self.cur.con.execute("UPDATE ofertas SET ts=?", (AGORA - DIA,))  # links recentes
        self.ml.cards["https://meli.la/x9"] = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=149,
                                                     vendedor="Lipx", loja_oficial=True, full=True)
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA)
        self.assertIn("https://meli.la/x9", vigia.alvos()["Lattafa Asad"])
        alertas = vigia.rodada()
        self.assertEqual(len(alertas), 1)
        self.assertIn("vigia do ML", self.enviados[0])

    def test_vigia_tambem_os_mais_postados(self):
        self.cur.con.execute("UPDATE ofertas SET ts=?", (AGORA - DIA,))
        cur2 = self.cur
        # Yara não está na lista vigiada; foi postado nos grupos, então entra pelo "mais postados".
        from curadora.banco import inserir_oferta
        from curadora.parser import ler_promo
        for n in range(3):
            p = ler_promo(f"*Lattafa Yara 100ml*\nPor R$ 150\nhttps://meli.la/y{n}", CFG["marcas"])
            inserir_oferta(cur2.con, f"y{n}", AGORA - DIA, "g", p, cur2.catalogo.chave(cur2.ident.analisar(p.produto)))
        cur2.con.commit()
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA)
        extras = vigia.alvos_mais_postados()
        self.assertEqual(list(extras.values()), [["https://meli.la/y2"]])  # 1 link, o mais recente

    def postar_no_grupofinal(self, produto, link, ts, msg):
        chave = self.cur.chave_de(produto)
        self.cur.con.execute("INSERT INTO postagens (msg_id, ts, grupo, produto, chave, preco, link) VALUES (?,?,?,?,?,?,?)",
                             (msg, ts, "Grupo da Equipe", produto, chave, 150, link))
        self.cur.con.commit()

    def test_16_10_vigia_o_que_a_equipe_postou_no_grupofinal(self):
        # Breno (16/10): "tinha que expandir o leque de produtos"
        self.postar_no_grupofinal("Lattafa Khamrah Qahwa 100ml", "https://meli.la/h1", AGORA - 3 * DIA, "a")
        self.postar_no_grupofinal("Lattafa Khamrah Qahwa 100ml", "https://meli.la/h2", AGORA - DIA, "b")    # o mais recente
        self.postar_no_grupofinal("Natura Kaiak Aero 100ml", "https://meli.la/k1", AGORA - DIA, "c")        # só 1 post
        self.postar_no_grupofinal("Afnan 9pm 100ml", "https://meli.la/n1", AGORA - 20 * DIA, "d")           # fora dos 14 dias
        self.postar_no_grupofinal("Afnan 9pm 100ml", "https://meli.la/n2", AGORA - 19 * DIA, "e")
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA, grupofinal_horas=(0, 24))
        self.assertEqual(vigia.alvos_do_grupofinal(set()), {"Lattafa Khamrah Qahwa 100ml": ["https://meli.la/h2"]})
        self.assertEqual(vigia.alvos_do_grupofinal({"https://meli.la/h2"}), {})    # o link já está nos outros alvos

    def test_16_10_vigia_do_grupofinal_em_rodizio_e_so_no_horario(self):
        import time as _time
        for i, nome in enumerate(("Lattafa Khamrah Qahwa 100ml", "Lattafa Yara Candy 100ml")):
            for j in range(2):
                self.postar_no_grupofinal(nome, f"https://meli.la/r{i}{j}", AGORA - DIA + j, f"r{i}{j}")
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA, grupofinal_por_rodada=1,
                      grupofinal_horas=(0, 24))
        vez = [list(vigia.alvos_do_grupofinal(set())) for _ in range(3)]
        self.assertEqual(vez[0], vez[2])
        self.assertNotEqual(vez[0], vez[1])
        hora = _time.localtime(AGORA).tm_hour
        fora = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA,
                     grupofinal_horas=((hora + 1) % 24, (hora + 2) % 24))
        self.assertEqual(fora.alvos_do_grupofinal(set()), {})

    def test_vigia_nao_regrava_oferta_igual(self):
        self.cur.con.execute("UPDATE ofertas SET ts=?", (AGORA - DIA,))
        self.ml.cards["https://meli.la/x9"] = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=199)
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA, mais_postados=0)
        vigia.rodada()
        vigia.rodada()  # mesma oferta, mesmo preço: não grava de novo
        n = self.cur.con.execute("SELECT COUNT(*) FROM avaliacoes WHERE origem='vigia do ML'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_link_de_outro_genero_e_ignorado(self):
        # Visto na VPS em 03/10: a vigia do "Attracione Men" trazia o Attracione Feminino.
        self.cur.con.execute("UPDATE ofertas SET ts=?", (AGORA - DIA,))
        for n in range(10):
            self.ml.cards[f"https://meli.la/x{n}"] = CardML(ok=True, titulo="Perfume Lattafa Asad Feminino 100ml",
                                                            preco=120)
        vigia = Vigia(self.cur, self.alertador, nomes=["Lattafa Asad Masculino"], relogio=lambda: AGORA)
        self.assertEqual(vigia.rodada(), [])
        n = self.cur.con.execute("SELECT COUNT(*) FROM avaliacoes WHERE origem='vigia do ML'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_link_que_virou_outro_produto_nao_alerta(self):
        self.cur.con.execute("UPDATE ofertas SET ts=?", (AGORA - DIA,))
        for n in range(10):
            self.ml.cards[f"https://meli.la/x{n}"] = CardML(ok=True, titulo="Lattafa Asad Bourbon 100ml", preco=120)
        Vigia(self.cur, self.alertador, nomes=["Lattafa Asad"], relogio=lambda: AGORA).rodada()
        self.assertEqual(self.enviados, [])


class TestWhatsApp(unittest.TestCase):
    def setUp(self):
        self.pedidos = []
        self.http = lambda url, payload: (self.pedidos.append((url, payload)), {"success": True})[1]
        # 21/10: com o pipeline rodando de verdade (na VPS, durante a rodada) quem fala no grupo é ele e o alerta fica
        # quieto (alertas.SaidaWhatsApp); o teste não pode depender disso.
        from curadora import alertas
        self.addCleanup(setattr, alertas.vitrine, "ativo", alertas.vitrine.ativo)
        alertas.vitrine.ativo = lambda cfg=None: False

    def test_sem_grupo_configurado_nao_manda_nada(self):
        self.assertIsNone(SaidaWhatsApp.do_config({"whatsapp_alertas": {"grupo": ""}}, self.http))

    def test_manda_so_excelente_e_so_para_o_grupo_configurado(self):
        saida = SaidaWhatsApp.do_config({"whatsapp_alertas": {"grupo": "1203630000"}}, self.http)
        saida("🔥 promo", {"nota": "IMPERDÍVEL"})
        saida("✅ promo", {"nota": "BOA"})  # BOA não vai para o grupo (padrão: só excelentes)
        self.assertEqual(len(self.pedidos), 1)
        url, payload = self.pedidos[0]
        self.assertTrue(url.endswith("/send"))
        self.assertEqual(payload, {"chatId": "1203630000@g.us", "message": "🔥 promo"})

    def test_recusa_da_ponte_vira_erro(self):
        recusa = lambda url, payload: {"success": False, "error": "x"}
        saida = SaidaWhatsApp("1203630000@g.us", http=recusa)
        with self.assertRaises(RuntimeError):
            saida("t", {"nota": "IMPERDÍVEL"})


class TestTextoDoAlerta(Base):
    def test_texto_curto_com_o_essencial(self):
        self.ml.cards["https://meli.la/a"] = CardML(ok=True, titulo="Perfume Lattafa Asad EDP 100ml", preco=150,
                                                    vendedor="Lipx", loja_oficial=True)
        av = self.cur.avaliar("*Lattafa Asad 100ml*\nPor R$ 150\nCupom: *CUPOM10*\nhttps://meli.la/a", agora=AGORA)
        texto = formatar_alerta(av, "ao vivo")
        self.assertIn("R$150,00", texto)
        self.assertIn("abaixo do normal", texto)
        self.assertIn("CUPOM10", texto)
        self.assertLessEqual(len(texto.splitlines()), 12)


if __name__ == "__main__":
    unittest.main()
