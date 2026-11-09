"""O banco da curadora como estava num horário T, só para leitura (04/11, fase 1 do PLANO_MELHOR_OFERTA_2026-11-04.md).

Usado pelo replay e pela simulação da rodada diurna. Nada aqui grava no banco da curadora: a conexão é somente leitura
e as views e tabelas do horário ficam no espaço temporário da própria conexão (na memória).

Filtrar datas não basta (Astra, 04/11): algumas tabelas guardam só o último valor. Cada tabela tem uma política
(POLITICA); o que foi sobrescrito depois de T é reconstruído a partir de um registro de eventos ou fica de fora, como
"sem registro do momento". Nunca entra como se fosse conhecido em T.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Iterator

from .aovivo import epoch
from .banco import COLUNAS_NOVAS
from .cupons import ESQUEMA_CUPONS, cupons_da_mensagem, registrar_cupons
from .parser import VOLUME, ler_promo, parece_perfume

# tabela -> como entra no horário T
POLITICA = {
    "avaliacoes": "linhas com ts <= T; 'postada' marcada depois de T volta a 0 (na fila paralela, também a do turno)",
    "postagens": "ts <= T (na fila paralela da simulação: só o que veio antes do turno)",
    "pulos": "ts <= T (na fila paralela: só antes do turno; o 'Pular' é da pessoa que está postando)",
    "ofertas": "ts <= T (registro de eventos)",
    "precos_ml": "ts <= T (uma linha por anúncio, dia e preço: o horário é o da 1ª vez no dia)",
    "cupom_sinais": "ts <= T (registro de eventos)",
    "alertas": "ts <= T", "chamadas": "ts <= T", "experimento": "ts <= T",
    "cupons": "SOBRESCRITA (regra, último visto, aviso da equipe): reconstruída até T repassando as mensagens do tap "
              "pela mesma registrar_cupons; o que não está no tap fica sem regra",
    "catalogo_ml": "SOBRESCRITA (o ts muda quando o catálogo é visto de novo): só entra com ts <= T",
    "vendedores_produtos": "SOBRESCRITA: só entra com ts <= T",
    "ml_cards": "SOBRESCRITA (cache do card por link): só entra com ts <= T",
    "lojas_dos_links": "SOBRESCRITA: só entra com ts <= T",
    "lojas": "não usada na decisão", "vigiados": "não usada na decisão", "meta": "não usada na decisão",
    "usuarios": "não usada", "sessoes": "não usada", "convites": "não usada",
}
EVENTOS = ("ofertas", "precos_ml", "cupom_sinais", "alertas", "chamadas", "experimento")
SOBRESCRITAS = ("catalogo_ml", "vendedores_produtos", "ml_cards", "lojas_dos_links")
TAP = Path.home() / ".hermes" / "whatsapp" / "taps" / "messages.jsonl"


def conectar_leitura(caminho: Path | str) -> sqlite3.Connection:
    """Conexão somente leitura; o espaço temporário (views do horário) fica na memória."""
    con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA temp_store = MEMORY")
    return con


def _colunas(con: sqlite3.Connection, tabela: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA main.table_info({tabela})")]


def _existe(con: sqlite3.Connection, tabela: str) -> bool:
    return bool(con.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?", (tabela,)).fetchone())


def no_horario(con: sqlite3.Connection, ate: float, fila_desde: float | None = None) -> None:
    """Deixa a conexão vendo o banco como em `ate`. Com `fila_desde` (o começo do turno), postagens, pulos e as marcas
    de "postada" do turno somem: é a fila paralela da simulação, que só conta as escolhas da própria simulação."""
    t = int(ate)
    corte = min(t, int(fila_desde) - 1) if fila_desde is not None else t
    for (nome,) in con.execute("SELECT name FROM temp.sqlite_master WHERE type = 'view'").fetchall():
        con.execute(f"DROP VIEW temp.{nome}")   # a temp.cupons (CuponsNoTempo) é tabela: fica
    if _existe(con, "avaliacoes"):
        cols = []
        for c in _colunas(con, "avaliacoes"):
            if c == "postada":
                cols.append(f"CASE WHEN postada > {corte} THEN 0 ELSE postada END AS postada")
            elif c == "postada_por":
                cols.append(f"CASE WHEN postada > {corte} THEN NULL ELSE postada_por END AS postada_por")
            else:
                cols.append(c)
        con.execute(f"CREATE TEMP VIEW avaliacoes AS SELECT {', '.join(cols)} FROM main.avaliacoes WHERE ts <= {t}")
    for nome in ("postagens", "pulos"):
        if _existe(con, nome):
            con.execute(f"CREATE TEMP VIEW {nome} AS SELECT * FROM main.{nome} WHERE ts <= {corte}")
    for nome in EVENTOS + SOBRESCRITAS:
        if _existe(con, nome):
            con.execute(f"CREATE TEMP VIEW {nome} AS SELECT * FROM main.{nome} WHERE ts <= {t}")


def catalogos_conhecidos(con: sqlite3.Connection, chaves: Iterable[str], ate: float) -> dict[str, set[str]]:
    """Os produtos de catálogo de cada chave que se sabia existir em `ate`: o catalogo_ml com ts até ali (o ts dele é
    sobrescrito) e as URLs /p/ vistas até ali nas avaliações e nos preços. Use numa conexão em no_horario."""
    saida: dict[str, set[str]] = {k: set() for k in chaves}
    if not saida:
        return saida
    lista = sorted(saida)
    marcas = ",".join("?" * len(lista))
    for pid, chave in con.execute(f"SELECT produto_id, chave FROM catalogo_ml WHERE chave IN ({marcas}) AND ts <= ?",
                                  (*lista, int(ate))):
        saida[chave].add(pid)
    for chave, url in con.execute(f"SELECT chave, url FROM precos_ml WHERE chave IN ({marcas}) AND ts <= ?",
                                  (*lista, int(ate))):
        if url and "/p/MLB" in url:
            saida[chave].add(url.split("/p/")[1].split("?")[0].split("/")[0])
    return saida


# ── o registro do pipeline (dados/pipeline.jsonl) no horário ─────

class RegistroEmMemoria:
    """Faz as vezes do Path do pipeline.jsonl para as funções do pipeline e do painel: só leitura, em memória."""

    def __init__(self, linhas: list[str]):
        self.linhas = list(linhas)

    def exists(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8", errors: str | None = None) -> str:
        return "\n".join(self.linhas) + ("\n" if self.linhas else "")

    def stat(self):
        conteudo = self.read_text()
        return SimpleNamespace(st_mtime_ns=hash(conteudo) & 0x7FFFFFFFFFFFFFFF, st_size=len(conteudo))

    def open(self, *args, **kwargs):
        raise PermissionError("registro do horário: só leitura")

    def acrescentar(self, dados: dict) -> None:
        self.linhas.append(json.dumps(dados, ensure_ascii=False))


def registro_ate(caminho: Path | str, ate: float, fila_desde: float | None = None) -> RegistroEmMemoria:
    """As linhas do pipeline.jsonl até `ate` (na fila paralela, só as de antes do turno)."""
    corte = min(ate, fila_desde - 1) if fila_desde is not None else ate
    linhas = []
    caminho = Path(caminho)
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                ts = float(json.loads(linha).get("ts") or 0)
            except (ValueError, AttributeError):
                continue
            if ts <= corte:
                linhas.append(linha)
    return RegistroEmMemoria(linhas)


def na_normalizadora_ate(feedback: Path, chave_de, desde: float, ate: float) -> set[str]:
    """pipeline.na_normalizadora com o limite de cima: só o que passou pela normalizadora até `ate`."""
    saida = set()
    if not feedback.exists():
        return saida
    with feedback.open("rb") as f:
        f.seek(max(0, feedback.stat().st_size - 3_000_000))
        f.readline()
        for bruto in f:
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if desde <= (r.get("ts") or 0) <= ate:
                nome = r.get("display_product") or r.get("product") or ""
                if nome:
                    saida.add(chave_de(nome))
    return saida


# ── as mensagens dos grupos (tap) e os cupons no horário ─────────

def mensagens_do_tap(caminho: Path, grupos: dict[str, str], grupofinal: str = "",
                     desde: float = 0, ate: float | None = None) -> Iterator[dict]:
    """As mensagens dos grupos-fonte e do GrupoFinal, em ordem, com ts em segundos. Só o que interessa às contas: horário,
    grupo e texto (quem mandou não sai daqui)."""
    if not Path(caminho).exists():
        return
    with Path(caminho).open("rb") as f:
        for bruto in f:
            if b'"body"' not in bruto:
                continue
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            ts = epoch(r.get("timestamp"))
            if ts < desde:
                continue
            if ate is not None and ts > ate:
                break
            grupo = str(r.get("chatId", "")).split("@")[0]
            corpo = r.get("body") or ""
            if not corpo:
                continue
            if grupofinal and grupo == grupofinal:   # o GrupoFinal vale mesmo quando a mensagem é da própria conta (aovivo)
                yield {"ts": ts, "grupo": grupo, "nome": "GrupoFinal", "grupofinal": True, "texto": corpo,
                       "msg": str(r.get("messageId") or "")}
            elif grupo in grupos and not r.get("fromMe"):
                yield {"ts": ts, "grupo": grupo, "nome": grupos[grupo], "grupofinal": False, "texto": corpo,
                       "msg": str(r.get("messageId") or "")}


class CuponsNoTempo:
    """A tabela `cupons` como estava em T, refeita pela mesma registrar_cupons a partir das mensagens do tap (aovivo
    chama registrar_cupons para cada mensagem de grupo-fonte e para o aviso de cupom do GrupoFinal). As mensagens chegam
    em ordem: avance com `ate(T)` sempre para a frente."""

    def __init__(self, mensagens: Iterable[dict], marcas: list[str], ouvintes: Iterable = ()):
        self.ouvintes = list(ouvintes)   # quem mais quer ver cada mensagem, na ordem (ex.: o detector de início de cupom)
        self.mem = sqlite3.connect(":memory:")
        self.mem.executescript(ESQUEMA_CUPONS)
        for coluna, tipo in COLUNAS_NOVAS.get("cupons", {}).items():   # as mesmas colunas da migração do banco
            self.mem.execute(f"ALTER TABLE cupons ADD COLUMN {coluna} {tipo}")
        self.marcas = marcas
        self._fila = iter(mensagens)
        self._proxima: dict | None = None
        self.agora = 0.0
        self.lidas = 0

    def _registrar(self, m: dict) -> None:
        texto = m["texto"]
        if not cupons_da_mensagem(texto):
            return
        if m["grupofinal"]:
            promo = ler_promo(texto, self.marcas)
            com_regra = {c for c, x in cupons_da_mensagem(texto).items() if x.pct or x.valor}
            if promo.valida and not (com_regra and not VOLUME.search(promo.produto or "")):
                return   # post da equipe: é postagem, não aviso (aovivo.processar_linha)
            registrar_cupons(self.mem, texto, int(m["ts"]), em_promo_de_perfume=True, da_equipe=True)
            return
        promo = ler_promo(texto, self.marcas)
        e_perfume = promo.valida and parece_perfume(promo.produto, texto)
        registrar_cupons(self.mem, texto, int(m["ts"]), em_promo_de_perfume=e_perfume)

    def ate(self, t: float) -> "CuponsNoTempo":
        if t < self.agora:
            raise ValueError("CuponsNoTempo só anda para a frente")
        while True:
            if self._proxima is None:
                self._proxima = next(self._fila, None)
                if self._proxima is None:
                    break
            if self._proxima["ts"] > t:
                break
            self._registrar(self._proxima)
            for ouvinte in self.ouvintes:
                ouvinte(self._proxima)
            self.lidas += 1
            self._proxima = None
        self.agora = t
        return self

    def linhas(self) -> list[tuple]:
        return self.mem.execute("SELECT * FROM cupons").fetchall()

    def aplicar(self, con: sqlite3.Connection) -> None:
        """Põe a tabela do horário na conexão (temp.cupons esconde a main.cupons, que é a de agora)."""
        con.execute("DROP TABLE IF EXISTS temp.cupons")
        con.execute("CREATE TEMP TABLE cupons AS SELECT * FROM main.cupons WHERE 0")
        linhas = self.linhas()
        if linhas:
            marcas = ",".join("?" * len(linhas[0]))
            colunas = [r[1] for r in self.mem.execute("PRAGMA table_info(cupons)")]
            con.executemany(f"INSERT INTO temp.cupons ({', '.join(colunas)}) VALUES ({marcas})", linhas)


def hora(ts: float) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


# ── as ofertas conhecidas de um perfume no horário ───────────────

JANELA_OBS_S = 26 * 3600   # o que entra no exame: até 26 h antes (as de mais de 2 h aparecem como leitura velha)


def _partes(chave: str) -> tuple[frozenset, str]:
    """(palavras, volume): "kaiak|100|m" -> ({"kaiak"}, "100") (o gênero, quando separa, a identidade confere)."""
    base, _, resto = (chave or "").partition("|")
    return frozenset(t for t in base.removeprefix("kit:").split("-") if t), resto.split("|")[0]


def _uma_letra(a: str, b: str) -> bool:
    """"assad" x "asad": uma letra a mais, a menos ou trocada."""
    if a == b or abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    curto, longo = sorted((a, b), key=len)
    return any(longo[:i] + longo[i + 1:] == curto for i in range(len(longo)))


def irma(chave: str, outra: str) -> bool:
    """A outra chave pode ser o mesmo perfume escrito de outro jeito (a identidade decide depois): mesmo volume e todas
    as nossas palavras mais até 2 ("alta-asad-bourbon-fixacao"), ou uma palavra com uma letra diferente
    ("assad-bourbon"). A chave com MENOS palavras não entra: "asad" é outro perfume do "asad-bourbon"."""
    if outra == chave or chave.startswith("kit:") != outra.startswith("kit:"):
        return False
    a, va = _partes(chave)
    b, vb = _partes(outra)
    if not a or not b or va != vb or va in ("", "?"):
        return False
    if a == b:
        return True   # o mesmo nome com o gênero separado ("kaiak|100" x "kaiak|100|m")
    if a < b:
        return len(b - a) <= 2
    if len(a) == len(b) and len(a - b) == 1:
        (x,), (y,) = a - b, b - a
        return _uma_letra(x, y)
    return False


def chaves_irmas(con: sqlite3.Connection, chave: str, ate: float, janela_s: float = JANELA_OBS_S,
                 maximo: int = 12) -> list[str]:
    vistas = [r[0] for r in con.execute("SELECT DISTINCT chave FROM avaliacoes WHERE ts BETWEEN ? AND ? AND chave IS NOT "
                                        "NULL", (int(ate - janela_s), int(ate)))]
    return sorted(k for k in vistas if irma(chave, k))[:maximo]


def observacoes_do_perfume(con: sqlite3.Connection, chave: str, ate: float, comparador: sqlite3.Connection | None = None,
                           janela_s: float = JANELA_OBS_S, irmas: bool = True):
    """Os anúncios vistos do perfume (e das chaves irmãs) até `ate`: avaliações (grupo, vigia, reconferência, radar),
    preços do ML (radar e vitrine) e as leituras do comparador. Use numa conexão em no_horario(ate)."""
    from . import ofertas   # import tardio: ofertas não depende do banco
    chaves = [chave] + (chaves_irmas(con, chave, ate, janela_s) if irmas else [])
    marcas = ",".join("?" * len(chaves))
    desde = int(ate - janela_s)
    saida = []
    for r in con.execute(f"SELECT id, ts, origem, chave, nota, score, dados FROM avaliacoes WHERE chave IN ({marcas}) "
                         "AND ts BETWEEN ? AND ? AND nota != 'FORA'", (*chaves, desde, int(ate))):
        try:
            dados = json.loads(r[6] or "{}")
        except ValueError:
            continue
        o = ofertas.observacao_da_avaliacao(r[0], r[1], r[2], r[3], r[4], r[5], dados)
        if o is not None:
            saida.append(o)
    for r in con.execute(f"SELECT ts, chave, item_id, titulo, preco, vendedor, oficial, url, fonte, seller_id FROM precos_ml "
                         f"WHERE chave IN ({marcas}) AND ts BETWEEN ? AND ?", (*chaves, desde, int(ate))):
        if r[4]:
            saida.append(ofertas.observacao_do_preco_ml(*r))
    if comparador is not None:
        for r in comparador.execute(
                f"SELECT o.ts, c.chave, o.item_id, o.titulo, o.preco, o.vendedor, o.oficial, o.link, o.fonte, o.seller_id, "
                f"o.produto_id, o.internacional, o.full, o.preco_outros, o.preco_cupom FROM observacoes o JOIN comparacoes c "
                f"ON c.id = o.comparacao WHERE c.chave IN ({marcas}) AND o.ts BETWEEN ? AND ?", (*chaves, desde, int(ate))):
            if not r[4]:
                continue
            base = "pagina" if r[8] == "pagina" else "api"
            o = ofertas.observacao_da_api(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9],
                                          fonte=f"comparador ({r[8]})", internacional=bool(r[11]), full=bool(r[12]),
                                          catalogo=r[10] or "", base=base, ref=f"comparador {r[8]} {r[2]}")
            if base == "pagina":
                o.selo_de, o.preco_outros = "pagina", r[13]
                if r[14]:
                    o.cupons.append(ofertas.Cupom("cupom da página", ofertas.NO_ANUNCIO, float(r[14]),
                                                  "a página do anúncio mostra o preço com cupom"))
            saida.append(o)
    return saida
