"""Login do painel: convites, senhas, bloqueio por tentativas e sessões."""
from __future__ import annotations

import unittest

from apoio import T0

from curadora.acesso import Acesso, AcessoNegado
from curadora.banco import conectar


class TestAcesso(unittest.TestCase):
    def setUp(self):
        self.agora = T0
        self.acesso = Acesso(conectar(":memory:"), relogio=lambda: self.agora)

    def test_convite_cria_a_senha_e_ja_entra(self):
        token = self.acesso.convidar("Colega")
        sessao = self.acesso.aceitar_convite(token, "senha-boa-123")
        self.assertEqual(self.acesso.usuario_da_sessao(sessao).nome, "colega")
        self.assertTrue(self.acesso.entrar("colega", "senha-boa-123"))

    def test_senha_nunca_fica_guardada_em_texto(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        guardado = self.acesso.con.execute("SELECT hash FROM usuarios").fetchone()[0]
        self.assertNotIn("senha-boa-123", guardado)
        self.assertTrue(guardado.startswith("pbkdf2$"))

    def test_convite_vale_uma_vez_e_vence(self):
        token = self.acesso.convidar("colega")
        self.acesso.aceitar_convite(token, "senha-boa-123")
        with self.assertRaises(AcessoNegado):
            self.acesso.aceitar_convite(token, "outra-senha-456")
        vencido = self.acesso.convidar("victor")
        self.agora += 49 * 3600
        with self.assertRaises(AcessoNegado):
            self.acesso.aceitar_convite(vencido, "senha-boa-123")

    def test_senha_curta_e_recusada(self):
        with self.assertRaises(ValueError):
            self.acesso.aceitar_convite(self.acesso.convidar("colega"), "1234")

    def test_senha_errada_e_bloqueio_depois_de_5_tentativas(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        for _ in range(5):
            with self.assertRaises(AcessoNegado):
                self.acesso.entrar("colega", "errada", ip="1.2.3.4")
        with self.assertRaises(AcessoNegado) as ctx:
            self.acesso.entrar("colega", "senha-boa-123", ip="1.2.3.4")  # até a certa fica bloqueada
        self.assertIn("15 minutos", str(ctx.exception))
        self.agora += 16 * 60
        self.assertTrue(self.acesso.entrar("colega", "senha-boa-123", ip="1.2.3.4"))

    def test_usuario_inexistente_tem_a_mesma_mensagem(self):
        with self.assertRaises(AcessoNegado) as ctx:
            self.acesso.entrar("ninguem", "qualquer")
        self.assertEqual(str(ctx.exception), "nome ou senha incorretos")

    def test_sessao_vence_e_sair_encerra(self):
        sessao = self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        self.acesso.sair(sessao)
        self.assertIsNone(self.acesso.usuario_da_sessao(sessao))
        outra = self.acesso.entrar("colega", "senha-boa-123")
        self.agora += 31 * 86400
        self.assertIsNone(self.acesso.usuario_da_sessao(outra))

    def test_desativar_tira_o_acesso_na_hora(self):
        sessao = self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        self.acesso.desativar("colega")
        self.assertIsNone(self.acesso.usuario_da_sessao(sessao))
        with self.assertRaises(AcessoNegado):
            self.acesso.entrar("colega", "senha-boa-123")

    def test_esqueceu_a_senha_novo_convite_redefine(self):
        antiga = self.acesso.aceitar_convite(self.acesso.convidar("breno", admin=True), "senha-esquecida")
        nova = self.acesso.aceitar_convite(self.acesso.convidar("breno"), "senha-nova-456")  # convite comum
        self.assertIsNone(self.acesso.usuario_da_sessao(antiga))          # sessões antigas encerradas
        with self.assertRaises(AcessoNegado):
            self.acesso.entrar("breno", "senha-esquecida")
        self.assertTrue(self.acesso.usuario_da_sessao(nova).admin)        # continua admin

    def test_admin_vem_do_convite(self):
        sessao = self.acesso.aceitar_convite(self.acesso.convidar("breno", admin=True), "senha-boa-123")
        self.assertTrue(self.acesso.usuario_da_sessao(sessao).admin)

    # ── código de recuperação (20/10) ───────────────────────────
    def test_codigo_de_recuperacao_troca_a_senha_e_vale_uma_vez(self):
        antiga = self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-esquecida")
        codigo = self.acesso.novo_codigo("colega")
        # digitado do jeito que vier: minúsculas, espaços no lugar dos traços
        nova, outro = self.acesso.recuperar("Colega", codigo.lower().replace("-", " "), "senha-nova-456")
        self.assertEqual(self.acesso.usuario_da_sessao(nova).nome, "colega")
        self.assertIsNone(self.acesso.usuario_da_sessao(antiga))          # sai dos outros aparelhos
        self.assertTrue(self.acesso.entrar("colega", "senha-nova-456"))
        with self.assertRaises(AcessoNegado):
            self.acesso.entrar("colega", "senha-esquecida")
        with self.assertRaises(AcessoNegado):                              # o código usado não vale mais
            self.acesso.recuperar("colega", codigo, "outra-senha-789")
        self.acesso.recuperar("colega", outro, "outra-senha-789")          # o novo vale

    def test_codigo_tem_12_letras_claras_e_fica_so_como_hash(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        codigo = self.acesso.novo_codigo("colega")
        self.assertRegex(codigo, r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")
        guardado = self.acesso.con.execute("SELECT recuperacao FROM usuarios").fetchone()[0]
        self.assertTrue(guardado.startswith("pbkdf2$"))
        self.assertNotIn(codigo.replace("-", ""), guardado)
        self.assertNotEqual(self.acesso.novo_codigo("colega"), codigo)      # gerar de novo troca o código

    def test_codigo_errado_bloqueia_como_a_senha(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        codigo = self.acesso.novo_codigo("colega")
        for _ in range(5):
            with self.assertRaises(AcessoNegado):
                self.acesso.recuperar("colega", "AAAA-BBBB-CCCC", "senha-nova-456", ip="1.2.3.4")
        with self.assertRaises(AcessoNegado) as ctx:
            self.acesso.recuperar("colega", codigo, "senha-nova-456", ip="1.2.3.4")  # até o certo fica bloqueado
        self.assertIn("15 minutos", str(ctx.exception))
        self.agora += 16 * 60
        self.acesso.recuperar("colega", codigo, "senha-nova-456", ip="1.2.3.4")

    def test_sem_codigo_desativado_ou_inexistente_nao_recupera(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        with self.assertRaises(AcessoNegado) as ctx:                       # ainda não gerou código
            self.acesso.recuperar("colega", "AAAA-BBBB-CCCC", "senha-nova-456")
        self.assertEqual(str(ctx.exception), "nome ou código incorretos")
        codigo = self.acesso.novo_codigo("colega")
        with self.assertRaises(AcessoNegado) as ctx:
            self.acesso.recuperar("ninguem", codigo, "senha-nova-456")
        self.assertEqual(str(ctx.exception), "nome ou código incorretos")  # mesma mensagem: não revela quem existe
        self.acesso.desativar("colega")
        with self.assertRaises(AcessoNegado):
            self.acesso.recuperar("colega", codigo, "senha-nova-456")
        with self.assertRaises(AcessoNegado):
            self.acesso.novo_codigo("colega")

    def test_senha_nova_curta_nao_gasta_o_codigo(self):
        self.acesso.aceitar_convite(self.acesso.convidar("colega"), "senha-boa-123")
        codigo = self.acesso.novo_codigo("colega")
        with self.assertRaises(ValueError):
            self.acesso.recuperar("colega", codigo, "1234")
        self.acesso.recuperar("colega", codigo, "senha-nova-456")

    def test_banco_antigo_ganha_a_coluna_do_codigo(self):
        con = conectar(":memory:")
        con.execute("CREATE TABLE usuarios (nome TEXT PRIMARY KEY, hash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,"
                    " criado_em INTEGER NOT NULL, ativo INTEGER NOT NULL DEFAULT 1)")
        con.execute("INSERT INTO usuarios VALUES ('breno', 'x', 1, 0, 1)")
        acesso = Acesso(con, relogio=lambda: self.agora)
        self.assertIn("recuperacao", {r[1] for r in con.execute("PRAGMA table_info(usuarios)")})
        self.assertTrue(acesso.novo_codigo("breno"))


if __name__ == "__main__":
    unittest.main()
