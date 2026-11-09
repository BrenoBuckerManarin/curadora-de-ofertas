"""Banco SQLite: ofertas observadas, catálogo aprendido e lojas confirmadas no ML."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import RAIZ
from .identidade import Catalogo

CAMINHO_PADRAO = RAIZ / "dados" / "curadora.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ofertas (
    id           INTEGER PRIMARY KEY,
    msg_id       TEXT UNIQUE,
    ts           INTEGER NOT NULL,
    grupo        TEXT,
    produto      TEXT NOT NULL,
    base         TEXT NOT NULL,
    volume       TEXT,
    genero       TEXT,
    chave        TEXT NOT NULL,
    preco        REAL NOT NULL,
    preco_de     REAL,
    cupons       TEXT,
    link         TEXT,
    marketplace  TEXT,
    vendedor     TEXT,
    loja_oficial INTEGER,
    verificado   INTEGER,
    texto        TEXT
);
CREATE INDEX IF NOT EXISTS ix_ofertas_chave_ts ON ofertas(chave, ts);
CREATE INDEX IF NOT EXISTS ix_ofertas_ts ON ofertas(ts);

CREATE TABLE IF NOT EXISTS meta (
    nome  TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

-- Preços vistos no próprio ML (card em destaque e recomendações): preço real, sem cupom.
CREATE TABLE IF NOT EXISTS precos_ml (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    chave      TEXT NOT NULL,
    item_id    TEXT,
    titulo     TEXT,
    preco      REAL NOT NULL,
    vendedor   TEXT,
    oficial    INTEGER,
    url        TEXT,
    fonte      TEXT,
    foto       TEXT,
    seller_id  INTEGER  -- 01/11: quem vende (só o radar, pela API, preenche); os bancos antigos ganham pela migração
);
CREATE INDEX IF NOT EXISTS ix_precos_ml_chave_ts ON precos_ml(chave, ts);

-- Produto de catálogo do ML de cada chave (aprendido dos cards e da busca da API): é o que o radar vigia.
CREATE TABLE IF NOT EXISTS catalogo_ml (
    produto_id TEXT PRIMARY KEY,
    chave      TEXT NOT NULL,
    nome       TEXT,
    origem     TEXT,
    ts         INTEGER
);
CREATE INDEX IF NOT EXISTS ix_catalogo_ml_chave ON catalogo_ml(chave);

-- Alertas já enviados (não repetir a mesma oferta).
CREATE TABLE IF NOT EXISTS alertas (
    chave_alerta TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    nota         TEXT,
    score        INTEGER,
    origem       TEXT,
    texto        TEXT
);

-- Cada promo avaliada (ao vivo, vigia ou colada no painel), com o voto da equipe para calibrar.
CREATE TABLE IF NOT EXISTS avaliacoes (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    origem    TEXT,
    grupo     TEXT,
    chave     TEXT,
    nota      TEXT,
    score     INTEGER,
    alertado  INTEGER DEFAULT 0,
    voto      INTEGER DEFAULT 0,
    postada   INTEGER DEFAULT 0,
    dados     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_avaliacoes_ts ON avaliacoes(ts);

-- Lojas que o próprio ML mostrou com o selo "Loja oficial" no card em destaque.
CREATE TABLE IF NOT EXISTS lojas (
    nome_norm   TEXT PRIMARY KEY,
    nome        TEXT NOT NULL,
    oficial     INTEGER NOT NULL,
    vistas      INTEGER NOT NULL DEFAULT 1,
    ultima_vez  INTEGER NOT NULL
);
"""


def conectar(caminho: Path | str | None = None) -> sqlite3.Connection:
    caminho = caminho or CAMINHO_PADRAO
    if str(caminho) != ":memory:":
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False + WAL: o modo ao vivo e a vigia usam o banco em threads diferentes.
    # timeout 30 s: manutenção (recálculo, backup) pode segurar o banco por alguns segundos (06/10: 'database is locked').
    con = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    if str(caminho) != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    from .cupons import ESQUEMA_CUPONS
    from .postagens import ESQUEMA_POSTAGENS
    from .vigiados import ESQUEMA_VIGIADOS
    from .experimento import ESQUEMA_EXPERIMENTO
    from .esgotados import ESQUEMA_SINAIS
    from .chamadas import ESQUEMA_CHAMADAS
    con.executescript(ESQUEMA_CUPONS)
    con.executescript(ESQUEMA_POSTAGENS)
    con.executescript(ESQUEMA_VIGIADOS)
    con.executescript(ESQUEMA_EXPERIMENTO)
    con.executescript(ESQUEMA_VENDEDORES)
    con.executescript(ESQUEMA_SINAIS)
    con.executescript(ESQUEMA_CHAMADAS)
    _migrar(con)
    return con


COLUNAS_NOVAS = {
    # 31/10 (Breno: "loja oficial vence revenda"): o vendedor de cada preço, para saber depois quem vendia a opção
    # mais barata (na auditoria, 10 de 14 casos ficaram sem resposta; o nome da loja nem sempre vem na API).
    # 01/11: só o radar (API) preenche; o preço lido da página fica sem seller_id
    "precos_ml": {"foto": "TEXT", "seller_id": "INTEGER"},
    "avaliacoes": {"postada": "INTEGER DEFAULT 0",  # ts de quando foi copiada/enviada
                   "votado_por": "TEXT DEFAULT ''", "postada_por": "TEXT DEFAULT ''"},
    "cupons": {"equipe_ts": "INTEGER"},  # quando a equipe escreveu a regra do cupom (aviso no GrupoFinal)
    "chamadas": {"usos": "INTEGER DEFAULT 0"},
    "cupom_sinais": {"loja": "TEXT", "post": "TEXT"},  # 11/10: tirar o cupom de UM post = não vale nessa loja
}


def _migrar(con: sqlite3.Connection) -> None:
    """Colunas novas em bancos criados antes delas existirem.

    Várias partes (ao vivo, vigia, radar, painel) abrem o banco ao mesmo tempo ao ligar; se duas tentarem
    criar a mesma coluna, a segunda recebe "duplicate column" — isso é normal e ignorado (visto em 03/10).
    """
    for tabela, colunas in COLUNAS_NOVAS.items():
        existentes = {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}
        for coluna, tipo in colunas.items():
            if coluna in existentes:
                continue
            try:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    con.commit()


def ler_meta(con: sqlite3.Connection, nome: str, padrao: str = "") -> str:
    linha = con.execute("SELECT valor FROM meta WHERE nome=?", (nome,)).fetchone()
    return linha["valor"] if linha else padrao


def gravar_meta(con: sqlite3.Connection, nome: str, valor: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (nome, valor) VALUES (?, ?)", (nome, valor))
    con.commit()


def inserir_oferta(con: sqlite3.Connection, msg_id: str, ts: int, grupo: str, promo, ident) -> bool:
    a = ident.analise
    cur = con.execute(
        """INSERT OR IGNORE INTO ofertas
           (msg_id, ts, grupo, produto, base, volume, genero, chave, preco, preco_de, cupons, link,
            marketplace, vendedor, loja_oficial, verificado, texto)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (msg_id, ts, grupo, promo.produto, a.base, a.volume, a.genero, ident.chave, promo.preco,
         promo.preco_de, json.dumps(promo.cupons), promo.link, promo.marketplace, promo.vendedor,
         int(promo.loja_oficial), int(promo.verificado), promo.texto),
    )
    return cur.rowcount > 0


# 18/10 (Breno: "pode fazer o 2"): preço visto no ML de OUTRA versão do produto não entra na chave dele. O catálogo
# junta masculino e feminino quando custam parecido (o unissex vem rotulado de qualquer jeito), e o radar punha o
# "Club de Nuit Intense 105 ml Edp - Feminino" e um "Man Eau De Parfum" de R$500 no histórico do Club de Nuit Intense
# masculino (em 60 dias: 5 mil anúncios de gênero oposto em 22 chaves; 577 com preço 2x+ em 48). Só os preços do ML:
# as ofertas dos grupos ficam como estão.
VERSAO_JANELA_S = 60 * 86400
VERSAO_MIN_GENERO = 5      # ofertas de grupo com gênero escrito, para saber qual domina
VERSAO_DOMINANCIA = 0.8
VERSAO_MIN_PRECOS = 5      # ofertas de grupo sem cupom, para a mediana
VERSAO_FORA = 2.0          # anúncio a 2x+ a mediana dos grupos sai do histórico (outra versão ou preço fora)


def versoes_por_chave(con: sqlite3.Connection, agora: float | None = None) -> tuple[dict[str, str], dict[str, float]]:
    """Em 60 dias de ofertas de grupo: o gênero que domina cada chave (80%+ das que dizem o gênero, mínimo 5) e a
    mediana sem cupom (mínimo 5)."""
    desde = int((agora if agora is not None else time.time()) - VERSAO_JANELA_S)
    generos: dict[str, Counter] = defaultdict(Counter)
    precos: dict[str, list[float]] = defaultdict(list)
    for chave, genero, preco, cupons in con.execute(
            "SELECT chave, genero, preco, cupons FROM ofertas WHERE ts >= ?", (desde,)):
        if genero:
            generos[chave][genero] += 1
        if cupons == "[]" and preco:
            precos[chave].append(preco)
    dominante = {}
    for chave, cont in generos.items():
        genero, n = cont.most_common(1)[0]
        total = sum(cont.values())
        if total >= VERSAO_MIN_GENERO and n / total >= VERSAO_DOMINANCIA:
            dominante[chave] = genero
    medianas = {chave: statistics.median(ps) for chave, ps in precos.items() if len(ps) >= VERSAO_MIN_PRECOS}
    return dominante, medianas


def chave_da_versao(chave: str, genero: str, preco: float | None, dominante: dict[str, str],
                    medianas: dict[str, float]) -> str:
    """A chave de um preço visto no ML: a do produto; a da outra versão (chave + "|f" ou "|m") quando o título diz o
    gênero oposto ao que domina nos grupos; ou chave + "|fora" quando o preço é 2x+ a mediana dos grupos."""
    dom = dominante.get(chave)
    if genero and dom and genero != dom:
        return f"{chave}|{genero}"
    med = medianas.get(chave)
    if preco and med and preco >= VERSAO_FORA * med:
        return f"{chave}|fora"
    return chave


def aprender_catalogo(con: sqlite3.Connection, identificador) -> Catalogo:
    """Aprende as regras de gênero/volume com o histórico e recalcula todas as chaves."""
    linhas = [(r["base"], r["volume"] or "", r["genero"] or "", r["preco"])
              for r in con.execute("SELECT base, volume, genero, preco FROM ofertas")]
    catalogo = Catalogo.aprender(linhas)
    con.execute("INSERT OR REPLACE INTO meta (nome, valor) VALUES ('catalogo', ?)", (catalogo.para_json(),))
    novas = []
    for r in con.execute("SELECT id, produto FROM ofertas"):
        novas.append((catalogo.chave(identificador.analisar(r["produto"])).chave, r["id"]))
    con.executemany("UPDATE ofertas SET chave=? WHERE id=?", novas)
    dominante, medianas = versoes_por_chave(con)
    ml = []
    for r in con.execute("SELECT id, titulo, preco FROM precos_ml"):
        analise = identificador.analisar(r["titulo"] or "")
        ml.append((chave_da_versao(catalogo.chave(analise).chave, analise.genero, r["preco"], dominante, medianas),
                   r["id"]))
    con.executemany("UPDATE precos_ml SET chave=? WHERE id=?", ml)
    cat = []
    for r in con.execute("SELECT produto_id, nome FROM catalogo_ml"):
        analise = identificador.analisar(r["nome"] or "")
        cat.append((chave_da_versao(catalogo.chave(analise).chave, analise.genero, None, dominante, medianas),
                    r["produto_id"]))
    con.executemany("UPDATE catalogo_ml SET chave=? WHERE produto_id=?", cat)
    con.commit()
    return catalogo


def carregar_catalogo(con: sqlite3.Connection) -> Catalogo:
    linha = con.execute("SELECT valor FROM meta WHERE nome='catalogo'").fetchone()
    return Catalogo.de_json(linha["valor"]) if linha else Catalogo()


def precos_do_produto(con: sqlite3.Connection, chave: str, desde_ts: int, ate_ts: int,
                      tipo: str = "todos") -> list[sqlite3.Row]:
    """`tipo`: "com_cupom" (anunciados com cupom), "sem_cupom" (anunciados sem cupom + vistos no ML) ou "todos"."""
    filtro = {"com_cupom": " AND cupons != '[]'", "sem_cupom": " AND cupons = '[]'"}.get(tipo, "")
    sql = f"SELECT ts, preco FROM ofertas WHERE chave=? AND ts>=? AND ts<?{filtro}"
    if tipo in ("sem_cupom", "todos"):
        sql += " UNION ALL SELECT ts, preco FROM precos_ml WHERE chave=? AND ts>=? AND ts<?"
        args: tuple = (chave, desde_ts, ate_ts, chave, desde_ts, ate_ts)
    else:
        args = (chave, desde_ts, ate_ts)
    return con.execute(f"SELECT ts, preco FROM ({sql}) ORDER BY ts", args).fetchall()


def precos_altos(con: sqlite3.Connection, chave: str, desde_ts: int, ate_ts: int) -> list[float]:
    """Todos os preços vistos do produto na janela (grupos + ML), para saber até onde ele já custou."""
    return [r[0] for r in con.execute(
        "SELECT preco FROM ofertas WHERE chave=? AND ts>=? AND ts<? UNION ALL "
        "SELECT preco FROM precos_ml WHERE chave=? AND ts>=? AND ts<?",
        (chave, desde_ts, ate_ts, chave, desde_ts, ate_ts))]


# 31/10: o preço da API do ML (radar) vem sem o desconto do Pix; o da página ("destaque", "recomendacao") vem com ele,
# de 5% a 20% mais barato no mesmo anúncio (91 pares medidos). Ao comparar o mesmo anúncio no tempo, uma fonte não se
# mistura com a outra.
FONTE_API = "radar API"


def registrar_preco_ml(con: sqlite3.Connection, ts: int, chave: str, card, fonte: str) -> None:
    """Guarda um preço real visto no ML. Mesmo item no mesmo dia e preço não se repete na mesma fonte (API ou página).
    Até 31/10 a fonte não contava: o preço visto pelas duas no mesmo dia ficava numa linha só, com o rótulo de quem viu
    primeiro."""
    dia = ts - ts % 86400
    existe = con.execute(
        "SELECT 1 FROM precos_ml WHERE chave=? AND item_id=? AND preco=? AND ts>=? AND (COALESCE(fonte, '') = ?) = ? "
        "LIMIT 1",
        (chave, card.item_id, card.preco, dia, FONTE_API, fonte == FONTE_API),
    ).fetchone()
    if existe:
        return
    con.execute(
        "INSERT INTO precos_ml (ts, chave, item_id, titulo, preco, vendedor, oficial, url, fonte, foto, seller_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, chave, card.item_id, card.titulo, card.preco, card.vendedor, int(card.loja_oficial), card.url_produto, fonte,
         card.foto, getattr(card, "seller_id", None)),
    )


def registrar_produto_catalogo(con: sqlite3.Connection, produto_id: str, chave: str, nome: str, origem: str,
                               ts: int) -> None:
    con.execute("""INSERT INTO catalogo_ml (produto_id, chave, nome, origem, ts) VALUES (?,?,?,?,?)
                   ON CONFLICT(produto_id) DO UPDATE SET ts = excluded.ts""",
                (produto_id, chave, nome, origem, ts))
    con.commit()


def produtos_catalogo(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT produto_id, chave, nome FROM catalogo_ml ORDER BY ts DESC").fetchall()


def foto_do_produto(con: sqlite3.Connection, chave: str) -> str:
    """Foto mais recente que o ML mostrou para este produto (de qualquer anúncio)."""
    linha = con.execute("SELECT foto FROM precos_ml WHERE chave=? AND foto != '' ORDER BY ts DESC LIMIT 1",
                        (chave,)).fetchone()
    return linha["foto"] if linha else ""


def registrar_avaliacao(con: sqlite3.Connection, ts: int, origem: str, grupo: str, chave: str, nota: str,
                        score: int, alertado: bool, dados: dict) -> int:
    cur = con.execute(
        "INSERT INTO avaliacoes (ts, origem, grupo, chave, nota, score, alertado, dados) VALUES (?,?,?,?,?,?,?,?)",
        (ts, origem, grupo, chave, nota, score, int(alertado), json.dumps(dados, ensure_ascii=False)),
    )
    con.commit()
    return cur.lastrowid


def registrar_alerta(con: sqlite3.Connection, chave_alerta: str, ts: int, nota: str, score: int,
                     origem: str, texto: str) -> None:
    con.execute("INSERT OR REPLACE INTO alertas (chave_alerta, ts, nota, score, origem, texto) VALUES (?,?,?,?,?,?)",
                (chave_alerta, ts, nota, score, origem, texto))
    con.commit()


def registrar_loja(con: sqlite3.Connection, nome_norm: str, nome: str, oficial: bool, ts: int) -> None:
    con.execute(
        """INSERT INTO lojas (nome_norm, nome, oficial, vistas, ultima_vez) VALUES (?,?,?,1,?)
           ON CONFLICT(nome_norm) DO UPDATE SET
             oficial = MAX(oficial, excluded.oficial), vistas = vistas + 1, ultima_vez = excluded.ultima_vez""",
        (nome_norm, nome, int(oficial), ts),
    )
    con.commit()


def lojas_oficiais_confirmadas(con: sqlite3.Connection) -> dict[str, str]:
    return {r["nome_norm"]: r["nome"] for r in con.execute("SELECT nome_norm, nome FROM lojas WHERE oficial=1")}


# ── quem vende o quê (para o aviso "loja de 1 produto") ──────────
ESQUEMA_VENDEDORES = """
CREATE TABLE IF NOT EXISTS vendedores_produtos (
    seller_id  INTEGER NOT NULL,
    produto_id TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (seller_id, produto_id)
);
"""


def registrar_vendedores(con: sqlite3.Connection, produto_id: str, ofertas, ts: int) -> None:
    """Toda vez que a curadora vê as ofertas de um produto de catálogo no ML, anota quem vende."""
    linhas = [(o.seller_id, produto_id, ts) for o in ofertas if getattr(o, "seller_id", None)]
    if linhas:
        con.executemany("INSERT OR REPLACE INTO vendedores_produtos (seller_id, produto_id, ts) VALUES (?,?,?)", linhas)
        con.commit()


def produtos_do_vendedor(con: sqlite3.Connection, seller_id: int) -> tuple[int, int]:
    """(produtos em que este vendedor foi visto, produtos que a curadora já consultou no total)."""
    n = con.execute("SELECT count(*) FROM vendedores_produtos WHERE seller_id = ?", (seller_id,)).fetchone()[0]
    total = con.execute("SELECT count(DISTINCT produto_id) FROM vendedores_produtos").fetchone()[0]
    return n, total
