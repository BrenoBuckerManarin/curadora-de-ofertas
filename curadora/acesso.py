"""Login do painel: usuários, convites e sessões.

- Ninguém digita senha para outra pessoa: o admin gera um CONVITE (link de uso único, vale 48h)
  e a própria pessoa cria a senha ao abrir o link.
- Esqueceu a senha (pedido do Breno em 20/10): com o CÓDIGO DE RECUPERAÇÃO, que sai ao criar a senha
  ou é gerado na ⚙ do painel, a pessoa cria outra sozinha. O código vale uma vez; quem usa recebe outro.
- Senhas e códigos guardados só como hash PBKDF2-SHA256 (200 mil voltas, sal próprio).
- 5 tentativas erradas (por IP ou por nome, somando senha e código) bloqueiam por 15 minutos.
- Sessão de 30 dias num cookie HttpOnly; sair encerra a sessão.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

ESQUEMA_ACESSO = """
CREATE TABLE IF NOT EXISTS usuarios (
    nome      TEXT PRIMARY KEY,
    hash      TEXT NOT NULL,
    admin     INTEGER NOT NULL DEFAULT 0,
    criado_em INTEGER NOT NULL,
    ativo     INTEGER NOT NULL DEFAULT 1,
    recuperacao TEXT
);
CREATE TABLE IF NOT EXISTS sessoes (
    token   TEXT PRIMARY KEY,
    nome    TEXT NOT NULL,
    criada  INTEGER NOT NULL,
    expira  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS convites (
    token    TEXT PRIMARY KEY,
    nome     TEXT NOT NULL,
    admin    INTEGER NOT NULL DEFAULT 0,
    expira   INTEGER NOT NULL,
    usado_em INTEGER
);
"""
VOLTAS = 200_000
SESSAO_S = 30 * 86400
CONVITE_S = 48 * 3600
MAX_FALHAS = 5
BLOQUEIO_S = 15 * 60
SENHA_MIN = 8
# Código de recuperação: 12 letras e números sem os que se confundem (0/O, 1/I/L), em 3 grupos ("K7QM-4XTB-9WRH").
ALFABETO_CODIGO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


@dataclass
class Usuario:
    nome: str
    admin: bool


class AcessoNegado(Exception):
    pass


def _hash(senha: str, sal: bytes | None = None) -> str:
    sal = sal or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, VOLTAS)
    return f"pbkdf2${VOLTAS}${sal.hex()}${h.hex()}"


def _confere(senha: str, guardado: str) -> bool:
    try:
        _, voltas, sal, h = guardado.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), int(voltas))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def normalizar_nome(nome: str) -> str:
    return "".join(c for c in str(nome or "").strip().lower() if c.isalnum() or c in "._-")[:40]


def normalizar_codigo(codigo: str) -> str:
    """Aceita o código como a pessoa digitar: em minúsculas, com espaços ou sem os traços."""
    return "".join(c for c in str(codigo or "").upper() if c.isalnum())


def _novo_codigo() -> str:
    letras = "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(12))
    return "-".join(letras[i:i + 4] for i in range(0, 12, 4))


class Limitador:
    """Conta tentativas erradas por chave (IP ou nome) numa janela; em memória, reinicia com o serviço."""

    def __init__(self, relogio=time.time):
        self.relogio = relogio
        self._falhas: dict[str, list[float]] = {}
        self._trava = threading.Lock()

    def bloqueado(self, *chaves: str) -> bool:
        agora = self.relogio()
        with self._trava:
            return any(len([t for t in self._falhas.get(c, []) if agora - t < BLOQUEIO_S]) >= MAX_FALHAS
                       for c in chaves if c)

    def falhou(self, *chaves: str) -> None:
        agora = self.relogio()
        with self._trava:
            for c in chaves:
                if c:
                    self._falhas[c] = [t for t in self._falhas.get(c, []) if agora - t < BLOQUEIO_S] + [agora]

    def acertou(self, *chaves: str) -> None:
        with self._trava:
            for c in chaves:
                self._falhas.pop(c, None)


class Acesso:
    def __init__(self, con: sqlite3.Connection, relogio=time.time, limitador: Limitador | None = None):
        self.con = con
        self.con.executescript(ESQUEMA_ACESSO)
        self._migrar()
        self.relogio = relogio
        # O painel passa um limitador único do processo: as tentativas erradas valem entre pedidos.
        self.limitador = limitador or Limitador(relogio)

    def _migrar(self) -> None:
        """Banco de antes de 20/10: a tabela de usuários ainda não tinha a coluna do código de recuperação."""
        if "recuperacao" in {r[1] for r in self.con.execute("PRAGMA table_info(usuarios)")}:
            return
        try:
            self.con.execute("ALTER TABLE usuarios ADD COLUMN recuperacao TEXT")
            self.con.commit()
        except sqlite3.OperationalError:  # outro pedido acrescentou no mesmo instante
            pass

    # ── convites ────────────────────────────────────────────────
    def convidar(self, nome: str, admin: bool = False) -> str:
        nome = normalizar_nome(nome)
        if not nome:
            raise ValueError("nome inválido")
        token = secrets.token_urlsafe(24)
        self.con.execute("INSERT INTO convites (token, nome, admin, expira) VALUES (?,?,?,?)",
                         (token, nome, int(admin), int(self.relogio()) + CONVITE_S))
        self.con.commit()
        return token

    def ver_convite(self, token: str) -> sqlite3.Row | None:
        r = self.con.execute("SELECT * FROM convites WHERE token=?", (str(token or ""),)).fetchone()
        if not r or r["usado_em"] or r["expira"] < self.relogio():
            return None
        return r

    def aceitar_convite(self, token: str, senha: str) -> str:
        """Cria (ou redefine) a senha da pessoa do convite. Devolve o token de sessão."""
        convite = self.ver_convite(token)
        if not convite:
            raise AcessoNegado("convite inválido, já usado ou vencido: peça um novo")
        if len(senha or "") < SENHA_MIN:
            raise ValueError(f"a senha precisa ter pelo menos {SENHA_MIN} caracteres")
        agora = int(self.relogio())
        self.con.execute("""INSERT INTO usuarios (nome, hash, admin, criado_em, ativo) VALUES (?,?,?,?,1)
                            ON CONFLICT(nome) DO UPDATE SET hash=excluded.hash, ativo=1,
                              admin=MAX(admin, excluded.admin)""",
                         (convite["nome"], _hash(senha), convite["admin"], agora))
        self.con.execute("UPDATE convites SET usado_em=? WHERE token=?", (agora, token))
        self.con.execute("DELETE FROM sessoes WHERE nome=?", (convite["nome"],))  # senha nova: sai de tudo
        self.con.commit()
        return self._nova_sessao(convite["nome"])

    # ── recuperação (esqueceu a senha) ──────────────────────────
    def novo_codigo(self, nome: str) -> str:
        """Gera o código de recuperação da pessoa; o anterior deixa de valer. Mostrar uma vez só."""
        nome = normalizar_nome(nome)
        codigo = _novo_codigo()
        cur = self.con.execute("UPDATE usuarios SET recuperacao=? WHERE nome=? AND ativo=1",
                               (_hash(normalizar_codigo(codigo)), nome))
        if cur.rowcount != 1:
            raise AcessoNegado("usuário não encontrado")
        self.con.commit()
        return codigo

    def recuperar(self, nome: str, codigo: str, senha: str, ip: str = "") -> tuple[str, str]:
        """Com o código certo, troca a senha, encerra as outras sessões e devolve (sessão, código novo).
        Os erros contam no mesmo bloqueio do login."""
        nome = normalizar_nome(nome)
        if self.limitador.bloqueado(f"ip:{ip}", f"nome:{nome}"):
            raise AcessoNegado("muitas tentativas erradas: espere 15 minutos")
        if len(senha or "") < SENHA_MIN:  # antes de conferir: senha curta não gasta o código
            raise ValueError(f"a senha precisa ter pelo menos {SENHA_MIN} caracteres")
        r = self.con.execute("SELECT recuperacao FROM usuarios WHERE nome=? AND ativo=1", (nome,)).fetchone()
        guardado = r["recuperacao"] if r else None
        # Confere mesmo sem código guardado (com o hash falso), para não revelar quem tem código pelo tempo.
        ok = _confere(normalizar_codigo(codigo), guardado or _HASH_FALSO)
        if not (guardado and ok):
            self.limitador.falhou(f"ip:{ip}", f"nome:{nome}")
            raise AcessoNegado("nome ou código incorretos")
        self.limitador.acertou(f"nome:{nome}")
        self.con.execute("UPDATE usuarios SET hash=? WHERE nome=?", (_hash(senha), nome))
        self.con.execute("DELETE FROM sessoes WHERE nome=?", (nome,))  # senha nova: sai de tudo
        self.con.commit()
        return self._nova_sessao(nome), self.novo_codigo(nome)  # o código usado deixa de valer

    # ── login ───────────────────────────────────────────────────
    def entrar(self, nome: str, senha: str, ip: str = "") -> str:
        nome = normalizar_nome(nome)
        if self.limitador.bloqueado(f"ip:{ip}", f"nome:{nome}"):
            raise AcessoNegado("muitas tentativas erradas: espere 15 minutos")
        r = self.con.execute("SELECT hash FROM usuarios WHERE nome=? AND ativo=1", (nome,)).fetchone()
        # Confere mesmo sem usuário (com um hash falso) para não revelar quem existe pelo tempo de resposta.
        ok = _confere(senha or "", r["hash"] if r else _HASH_FALSO)
        if not (r and ok):
            self.limitador.falhou(f"ip:{ip}", f"nome:{nome}")
            raise AcessoNegado("nome ou senha incorretos")
        self.limitador.acertou(f"nome:{nome}")
        return self._nova_sessao(nome)

    def _nova_sessao(self, nome: str) -> str:
        token = secrets.token_urlsafe(32)
        agora = int(self.relogio())
        self.con.execute("INSERT INTO sessoes (token, nome, criada, expira) VALUES (?,?,?,?)",
                         (token, nome, agora, agora + SESSAO_S))
        self.con.execute("DELETE FROM sessoes WHERE expira < ?", (agora,))
        self.con.commit()
        return token

    def usuario_da_sessao(self, token: str | None) -> Usuario | None:
        if not token:
            return None
        r = self.con.execute("""SELECT u.nome, u.admin FROM sessoes s JOIN usuarios u ON u.nome = s.nome
                                WHERE s.token=? AND s.expira>=? AND u.ativo=1""",
                             (token, int(self.relogio()))).fetchone()
        return Usuario(r["nome"], bool(r["admin"])) if r else None

    def sair(self, token: str | None) -> None:
        self.con.execute("DELETE FROM sessoes WHERE token=?", (token or "",))
        self.con.commit()

    # ── administração ───────────────────────────────────────────
    def listar(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT nome, admin, ativo, criado_em FROM usuarios ORDER BY nome").fetchall()

    def desativar(self, nome: str) -> None:
        nome = normalizar_nome(nome)
        self.con.execute("UPDATE usuarios SET ativo=0 WHERE nome=?", (nome,))
        self.con.execute("DELETE FROM sessoes WHERE nome=?", (nome,))
        self.con.commit()


_HASH_FALSO = _hash("senha-que-ninguem-tem", b"\x00" * 16)
