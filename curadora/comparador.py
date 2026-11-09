"""Comparador de ofertas em modo sombra (04/11/2026).

Aprovação do Breno: "implementar, testar e instalar o comparador em modo sombra, com backup, fora do horário protegido
e sem alterar escolhas, notas ou envios". Plano: PLANO_COMPARADOR_DE_OFERTAS.md; escopo e orçamento:
DIAGNOSTICO_COMPARADOR_2026-11-03.md.

Para cada envio do pipeline (e uma amostra dos posts no GrupoFinal, só pela API), lê no ML o catálogo do produto e compara o
anúncio enviado com as outras ofertas do mesmo produto, todas lidas no mesmo momento. Só observa e registra:
- lê o banco da curadora só para leitura e o registro do pipeline; escreve só no banco próprio (dados/comparador.db);
- não muda escolha, nota, texto, envio, fila nem painel, e não reinicia nada;
- API do ML com o acesso emprestado da coletora: só lê o token, nunca renova (não mexe na autenticação compartilhada);
- não contorna a verificação de conta do ML: se ela aparece, para;
- orçamento com teto por hora e por dia; para 30 min com 429 ou verificação e o resto do dia depois de 3 seguidos;
- uma execução por vez (trava de arquivo); desliga com o arquivo dados/comparador.desligado.

Garantias pedidas pelo Breno (04/11):
- cada observação fica guardada com preço, vendedor, condições e horário (tabela observacoes; nada é apagado);
- o anúncio original e a alternativa são lidos no mesmo retrato da API e, na conferência, nas páginas abertas em
  seguida. Comparação feita depois do envio não prova que havia preço melhor naquele momento: o atraso fica guardado,
  e a de mais de 15 min fica de fora da conta (status "atrasada");
- cupom incerto fica à parte (status "cupom_incerto"), sem contar como ganho ou perda;
- loja sem selo fica registrada à parte, com os critérios de confiança de hoje (travas.vendedor_fraco);
- diferença de 2% e R$ 2 juntos dá prioridade para abrir as páginas; as menores também são registradas.
Troca automática não existe aqui: depende de nova aprovação.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from .ml_api import OfertaML, SemCredencial, reputacao
from .radar import oficial_so_na_api
from .travas import vendedor_fraco

try:   # trava de arquivo: fcntl na VPS (Linux); msvcrt no Windows (testes)
    import fcntl
except ImportError:   # pragma: no cover - só no Windows
    fcntl = None
    import msvcrt

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
BANCO = DADOS / "comparador.db"
BANCO_CURADORA = DADOS / "curadora.db"
REGISTRO_PIPELINE = DADOS / "pipeline.jsonl"
DESLIGADO = DADOS / "comparador.desligado"
TRAVA = DADOS / "comparador.lock"
# O acesso emprestado da coletora (o mesmo de vps/curadora.service). O comparador só LÊ este arquivo, pelo ClienteML:
# sem o app compartilhado, ele nunca renova o token.
TOKEN_EMPRESTADO = Path.home() / ".hermes" / "secrets" / "ml-api-token.json"

PRIORIDADE_PCT, PRIORIDADE_RS = 0.02, 2.0                         # os dois juntos: prioridade para abrir as páginas
LIMITES = {"api": (30, 300), "pagina": (20, 150), "gerador": (10, 60)}   # teto por hora e por dia
PAUSA_S = 30 * 60                       # com 429 ou verificação de conta, para 30 min
FALHAS_PARA_O_DIA = 3                   # 3 seguidas: para até o fim do dia
ATRASO_MAX_S = 15 * 60                  # comparação de mais de 15 min depois do envio não prova nada sobre ele
RELEITURA_S = 3600                      # a alternativa mais barata ainda está 1 h depois?
ENVIOS_POR_VOLTA = 6
GRUPOFINAL_POR_VOLTA, GRUPOFINAL_POR_DIA = 2, 50  # amostra dos posts no GrupoFinal: só API, sem abrir página
RELEITURAS_POR_VOLTA = 3
REPUTACOES_NOVAS_POR_COMPARACAO = 6     # vendedores consultados na API por comparação (os guardados não contam)
INTERVALO_API_S, INTERVALO_PAGINA_S = 2.0, 10.0
VENDEDOR_GUARDADO_S = 7 * 86400
ANUNCIO = "https://produto.mercadolivre.com.br/MLB-{}"

MELHOR_CONFIRMADA = "melhor_confirmada"   # as páginas confirmam: oficial 2% e R$2 mais barata, Pix com Pix
NAO_CONFIRMADA = "nao_confirmada"         # a API dizia mais barata e as páginas não confirmaram (motivo)
MELHOR_NA_API = "melhor_na_api"           # 2% e R$2 mais barata na API; páginas não conferidas (motivo)
DIFERENCA_PEQUENA = "diferenca_pequena"   # mais barata, abaixo de 2% ou de R$2 (registrada, sem abrir página)
ENVIADA_MENOR = "enviada_menor"           # nenhuma oficial válida mais barata que a enviada
SEM_ALTERNATIVA = "sem_alternativa"       # nenhuma outra loja oficial válida no catálogo
CUPOM_INCERTO = "cupom_incerto"           # envio com cupom: à parte, nem ganho nem perda
INCONCLUSIVO = "inconclusivo"             # com motivo (sem catálogo, falha, orçamento...)
ATRASADA = "atrasada"                     # mais de 15 min depois do envio: não consulta nada

ESQUEMA = """
CREATE TABLE IF NOT EXISTS comparacoes (
    id                INTEGER PRIMARY KEY,
    origem            TEXT NOT NULL,          -- 'envio' (pipeline) ou 'grupofinal' (amostra, só API)
    ref               TEXT NOT NULL UNIQUE,
    ts_ref            INTEGER NOT NULL,       -- hora do envio ou do post
    ts                INTEGER NOT NULL,       -- hora da leitura do catálogo
    chave             TEXT, produto TEXT, link TEXT, produto_id TEXT, item_enviado TEXT,
    seller_enviado    INTEGER, oficial_enviado INTEGER,
    preco_api_enviado REAL, preco_texto REAL,
    cupom             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL, motivo TEXT NOT NULL DEFAULT '',
    tipo              TEXT,                   -- 'oficial x oficial' ou 'sem selo -> oficial'
    item_melhor       TEXT, seller_melhor INTEGER, preco_api_melhor REAL,
    diferenca         REAL, diferenca_pct REAL,
    economia_pix      REAL,                   -- só nas confirmadas na página
    sem_selo_dif      REAL,                   -- loja sem selo mais barata (à parte)
    pagina            TEXT,                   -- JSON da conferência das páginas
    ts_releitura      INTEGER, persistiu INTEGER,
    dados             TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_comparacoes_ts ON comparacoes(ts_ref);
-- Cada oferta vista, com preço, vendedor, condições e horário. Nada é apagado: é o histórico.
CREATE TABLE IF NOT EXISTS observacoes (
    id            INTEGER PRIMARY KEY,
    ts            INTEGER NOT NULL,
    comparacao    INTEGER,
    fonte         TEXT NOT NULL,             -- 'api' (catálogo) ou 'pagina' (card do link)
    produto_id    TEXT, item_id TEXT, seller_id INTEGER, vendedor TEXT,
    oficial       INTEGER, internacional INTEGER, frete_gratis INTEGER, full INTEGER,
    preco         REAL, preco_anterior REAL, preco_outros REAL, preco_cupom REAL,
    titulo        TEXT, link TEXT
);
CREATE INDEX IF NOT EXISTS ix_observacoes_item ON observacoes(item_id, ts);
CREATE TABLE IF NOT EXISTS vendedores (seller_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, dados TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consultas (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, tipo TEXT NOT NULL, resultado TEXT NOT NULL, ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_consultas_ts ON consultas(ts);
CREATE TABLE IF NOT EXISTS estado (nome TEXT PRIMARY KEY, valor TEXT NOT NULL);
"""


def _sem_hifen(item) -> str:
    return str(item or "").replace("-", "")


def _inicio_do_dia(agora: float) -> float:
    return time.mktime(time.localtime(agora)[:3] + (0, 0, 0, 0, 0, -1))


def abrir(caminho: Path | str = BANCO) -> sqlite3.Connection:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(caminho), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    return con


def abrir_curadora_so_leitura(caminho: Path = BANCO_CURADORA) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    return con


def ler_estado(con, nome: str, padrao: str = "") -> str:
    r = con.execute("SELECT valor FROM estado WHERE nome = ?", (nome,)).fetchone()
    return r[0] if r else padrao


def gravar_estado(con, nome: str, valor) -> None:
    con.execute("INSERT OR REPLACE INTO estado (nome, valor) VALUES (?, ?)", (nome, str(valor)))


# ── uma execução por vez ────────────────────────────────────────
class Trava:
    """Trava exclusiva num arquivo. Solta sozinha se o processo morrer (é do sistema, não um arquivo-sinal)."""

    def __init__(self, caminho: Path = TRAVA):
        self.caminho = Path(caminho)
        self.arquivo = None

    def pegar(self) -> bool:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        arquivo = open(self.caminho, "a+")
        try:
            arquivo.seek(0)
            if fcntl:
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:   # pragma: no cover - só no Windows
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            arquivo.close()
            return False
        self.arquivo = arquivo
        return True

    def soltar(self) -> None:
        if not self.arquivo:
            return
        try:
            if fcntl:
                fcntl.flock(self.arquivo.fileno(), fcntl.LOCK_UN)
            else:   # pragma: no cover - só no Windows
                self.arquivo.seek(0)
                msvcrt.locking(self.arquivo.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.arquivo.close()
        self.arquivo = None


# ── orçamento de consultas ──────────────────────────────────────
class Orcamento:
    """Teto por hora e por dia de cada tipo de consulta, e a pausa depois de 429 ou verificação de conta."""

    def __init__(self, con, relogio=time.time, limites: dict | None = None):
        self.con = con
        self.relogio = relogio
        self.limites = limites or LIMITES

    def pausado_ate(self) -> float:
        return float(ler_estado(self.con, "pausa_ate", "0") or 0)

    def usado(self, tipo: str, desde: float) -> int:
        return self.con.execute("SELECT count(*) FROM consultas WHERE tipo = ? AND ts >= ?", (tipo, int(desde))).fetchone()[0]

    def pode(self, tipo: str, quantas: int = 1) -> bool:
        agora = self.relogio()
        if agora < self.pausado_ate():
            return False
        por_hora, por_dia = self.limites[tipo]
        return (self.usado(tipo, agora - 3600) + quantas <= por_hora
                and self.usado(tipo, _inicio_do_dia(agora)) + quantas <= por_dia)

    def registrar(self, tipo: str, resultado: str, ms: int = 0) -> None:
        agora = self.relogio()
        self.con.execute("INSERT INTO consultas (ts, tipo, resultado, ms) VALUES (?,?,?,?)",
                         (int(agora), tipo, resultado, int(ms)))
        if resultado in ("429", "verificacao"):
            seguidas = int(ler_estado(self.con, "falhas_seguidas", "0") or 0) + 1
            gravar_estado(self.con, "falhas_seguidas", seguidas)
            ate = _inicio_do_dia(agora) + 86400 if seguidas >= FALHAS_PARA_O_DIA else agora + PAUSA_S
            gravar_estado(self.con, "pausa_ate", int(ate))
        elif resultado == "ok":
            gravar_estado(self.con, "falhas_seguidas", 0)
        self.con.commit()


# ── a comparação (sem rede: testável) ───────────────────────────
def classe_do_vendedor(oferta, rep: dict | None) -> tuple[str, str]:
    """('oficial' | 'sem_selo' | 'fora', motivo). Oficial "só na API" (menos de 5 mil vendas) não conta como oficial;
    sem selo, valem os critérios de confiança de hoje (travas.vendedor_fraco, o mesmo do pipeline e do painel)."""
    rep = rep or {}
    if getattr(oferta, "internacional", False) or rep.get("pais") not in (None, "", "BR"):
        return "fora", "compra internacional"
    if oferta.loja_oficial and not oficial_so_na_api(rep):
        return "oficial", ""
    if not rep:
        return "fora", "sem a reputação do vendedor"
    motivo = vendedor_fraco(False, rep)
    return ("fora", motivo) if motivo else ("sem_selo", "")


def prioridade(dif: float, pct: float) -> bool:
    return dif >= PRIORIDADE_RS and pct >= PRIORIDADE_PCT


def _diferenca(base: float, outro: float) -> tuple[float, float]:
    dif = round(base - outro, 2)
    return dif, (round(dif / base, 4) if base else 0.0)


def _resumo(o, reps: dict, motivo: str = "", classe: str = "") -> dict:
    rep = reps.get(o.seller_id) or {}
    r = {"item_id": o.item_id, "seller_id": o.seller_id, "vendedor": rep.get("nome") or o.vendedor or "",
         "preco": o.preco, "oficial_api": bool(o.loja_oficial), "frete_gratis": bool(o.frete_gratis),
         "internacional": bool(o.internacional), "vendas": rep.get("vendas")}
    if classe:
        r["classe"] = classe
    if motivo:
        r["motivo"] = motivo
    return r


def comparar(item_enviado: str, ofertas: list, reps: dict, cupons: list[str]) -> dict:
    """Compara o anúncio enviado com as outras ofertas do MESMO retrato do catálogo (preço da API com preço da API).

    O que conta como "melhor" é loja oficial válida mais barata: oficial x oficial quando a enviada é oficial (a disputa
    que o pipeline não faz) e sem selo -> oficial quando não é (a regra de hoje). Loja sem selo mais barata fica à
    parte. Envio com cupom vai para "cupom incerto": a análise de preço fica guardada, mas não conta."""
    item_enviado = _sem_hifen(item_enviado)
    enviado = next((o for o in ofertas if _sem_hifen(o.item_id) == item_enviado), None)
    if enviado is None:
        return {"status": INCONCLUSIVO, "motivo": "o anúncio enviado não está nas ofertas do catálogo",
                "vendedores_no_catalogo": len({o.seller_id for o in ofertas})}
    classe_env, motivo_env = classe_do_vendedor(enviado, reps.get(enviado.seller_id))
    validas, descartes, mais_caras = [], [], 0
    for o in ofertas:
        if o is enviado:
            continue
        if o.seller_id is not None and o.seller_id == enviado.seller_id:
            descartes.append(_resumo(o, reps, "outro anúncio do mesmo vendedor"))
            continue
        if not o.loja_oficial and not reps.get(o.seller_id) and o.preco > enviado.preco:
            mais_caras += 1   # sem selo e mais cara que a enviada: não disputa (a reputação nem é consultada)
            continue
        classe, motivo = classe_do_vendedor(o, reps.get(o.seller_id))
        if classe == "fora":
            descartes.append(_resumo(o, reps, motivo))
        else:
            validas.append((classe, o))
    validas.sort(key=lambda x: x[1].preco)
    oficiais = [o for c, o in validas if c == "oficial"]
    sem_selo = [o for c, o in validas if c == "sem_selo"]
    r = {"classe_enviado": classe_env, "motivo_enviado": motivo_env, "preco_api_enviado": enviado.preco,
         "seller_enviado": enviado.seller_id, "oficial_enviado": classe_env == "oficial",
         "tipo": "oficial x oficial" if classe_env == "oficial" else "sem selo -> oficial",
         "vendedores_no_catalogo": len({o.seller_id for o in ofertas}), "sem_selo_mais_caras": mais_caras,
         "alternativas": [_resumo(o, reps, "", c) for c, o in validas], "descartes": descartes, "prioridade": False}
    if sem_selo and sem_selo[0].preco < enviado.preco:
        dif, pct = _diferenca(enviado.preco, sem_selo[0].preco)
        r["sem_selo_mais_barata"] = {**_resumo(sem_selo[0], reps, "", "sem_selo"), "diferenca": dif, "pct": pct}
    if oficiais:
        melhor = oficiais[0]
        dif, pct = _diferenca(enviado.preco, melhor.preco)
        r.update(item_melhor=melhor.item_id, seller_melhor=melhor.seller_id, preco_api_melhor=melhor.preco,
                 diferenca=dif, diferenca_pct=pct, prioridade=prioridade(dif, pct))
        status = MELHOR_NA_API if r["prioridade"] else DIFERENCA_PEQUENA if dif > 0 else ENVIADA_MENOR
        motivo = "página ainda não conferida" if status == MELHOR_NA_API else ""
    else:
        status = SEM_ALTERNATIVA
        motivo = ("só o vendedor enviado no catálogo" if r["vendedores_no_catalogo"] <= 1
                  else "nenhuma outra loja oficial válida no catálogo")
    if cupons:
        # 04/11 (Breno: "separar cupons incertos, sem contabilizá-los como ganho ou perda")
        r.update(status_sem_cupom=status, prioridade=False)
        status, motivo = CUPOM_INCERTO, f"envio com cupom ({', '.join(cupons)}): o preço enviado depende dele"
    r.update(status=status, motivo=motivo)
    return r


def _pix(card) -> float | None:
    """O preço do Pix na página: o em destaque (quando a página mostra "outros meios", o destaque é o do Pix)."""
    return card.preco


def _outros(card) -> float | None:
    return card.preco_outros or card.preco


def conferir_paginas(card_env, card_alt, item_alt: str, ident=None, item_env: str = "") -> dict:
    """As páginas do anúncio enviado e da alternativa, abertas em seguida: o anúncio certo, o selo, o mesmo produto e
    o preço, Pix com Pix e "outros meios" com "outros meios". O "com cupom" da página fica à parte (não conta).
    A página do enviado que mostra outra loja deixa a comparação sem conferir (não é erro da API)."""
    r = {"pix_enviado": _pix(card_env) if card_env.ok else None, "pix_melhor": _pix(card_alt) if card_alt.ok else None,
         "outros_enviado": _outros(card_env) if card_env.ok else None,
         "outros_melhor": _outros(card_alt) if card_alt.ok else None,
         "cupom_da_pagina_enviado": getattr(card_env, "preco_cupom", None),
         "cupom_da_pagina_melhor": getattr(card_alt, "preco_cupom", None),
         "vendedor_pagina_melhor": getattr(card_alt, "vendedor", ""), "selo_pagina_melhor": bool(card_alt.loja_oficial)}
    if not card_env.ok or not card_env.preco:
        return {**r, "status": MELHOR_NA_API, "motivo": f"a página do anúncio enviado não abriu ({card_env.erro or 'sem preço'})"}
    if item_env and card_env.item_id and _sem_hifen(card_env.item_id) != _sem_hifen(item_env):
        return {**r, "status": MELHOR_NA_API, "motivo": "a página do anúncio enviado mostrou outra loja"}
    if not card_alt.ok or not card_alt.preco:
        return {**r, "status": MELHOR_NA_API, "motivo": f"a página da alternativa não abriu ({card_alt.erro or 'sem preço'})"}
    if card_alt.item_id and _sem_hifen(card_alt.item_id) != _sem_hifen(item_alt):
        return {**r, "status": NAO_CONFIRMADA, "motivo": "a página da alternativa mostrou outro anúncio"}
    if card_alt.internacional:
        return {**r, "status": NAO_CONFIRMADA, "motivo": "a página da alternativa é compra internacional"}
    if not card_alt.loja_oficial:
        return {**r, "status": NAO_CONFIRMADA, "motivo": "a página da alternativa não mostra o selo de loja oficial"}
    if ident is not None:
        a, b = ident.analisar(card_env.titulo or ""), ident.analisar(card_alt.titulo or "")
        conflito = ident.conflito(a, b) or ident.concentracao_diferente(a, b) or ("kit x avulso" if a.kit != b.kit else "")
        if conflito:
            return {**r, "status": NAO_CONFIRMADA, "motivo": f"produto diferente: {conflito}"}
    dif, pct = _diferenca(r["pix_enviado"], r["pix_melhor"])
    dif_outros, _ = _diferenca(r["outros_enviado"], r["outros_melhor"])
    r.update(diferenca_pix=dif, pct_pix=pct, diferenca_outros=dif_outros)
    if prioridade(dif, pct):
        return {**r, "status": MELHOR_CONFIRMADA, "motivo": ""}
    por_cento = f"{pct * 100:.1f}".replace(".", ",")
    return {**r, "status": NAO_CONFIRMADA, "motivo": f"nas páginas a diferença no Pix é de {_brl(dif)} ({por_cento}%)"}


def _ofertas_da_resposta(resultados: list) -> list[OfertaML]:
    """As ofertas de uma página de /products/{id}/items, como ml_api.ClienteML.ofertas (que só lê a 1ª página)."""
    ofertas = []
    for r in resultados or []:
        if not isinstance(r.get("price"), (int, float)):
            continue
        ofertas.append(OfertaML(
            item_id=str(r.get("item_id") or ""), preco=float(r["price"]),
            preco_anterior=r.get("original_price") if isinstance(r.get("original_price"), (int, float)) else None,
            seller_id=r.get("seller_id"), loja_oficial=bool(r.get("official_store_id")),
            frete_gratis=bool((r.get("shipping") or {}).get("free_shipping")),
            internacional=("cbt_item" in (r.get("tags") or [])
                           or "cbt_fulfillment" in ((r.get("shipping") or {}).get("tags") or []))))
    return sorted(ofertas, key=lambda o: o.preco)


class ParouDeConsultar(Exception):
    """Orçamento esgotado, pausa por 429/verificação ou acesso vencido: a volta para aqui, sem forçar nada."""


# ── a volta do comparador ───────────────────────────────────────
class Comparador:
    def __init__(self, con, con_cur, api, paginas, gerador, orcamento: Orcamento, ident=None,
                 registro_pipeline: Path = REGISTRO_PIPELINE, relogio=time.time, dormir=time.sleep, log=print):
        self.con, self.cur, self.api, self.paginas, self.gerador = con, con_cur, api, paginas, gerador
        self.orc, self.ident, self.registro = orcamento, ident, Path(registro_pipeline)
        self.relogio, self.dormir, self.log = relogio, dormir, log
        self._ultima = {"api": 0.0, "pagina": 0.0}
        self.resumo = Counter()

    # consultas, sempre pelo orçamento
    def _esperar(self, tipo: str, intervalo: float) -> None:
        falta = intervalo - (time.monotonic() - self._ultima[tipo])
        if self._ultima[tipo] and falta > 0:
            self.dormir(falta)
        self._ultima[tipo] = time.monotonic()

    def _api(self, caminho: str) -> dict:
        if not self.orc.pode("api"):
            raise ParouDeConsultar("orçamento da API esgotado ou em pausa")
        self._esperar("api", INTERVALO_API_S)
        inicio = time.monotonic()
        try:
            dados = self.api.get(caminho)
        except SemCredencial as exc:
            raise ParouDeConsultar(f"sem acesso à API ({exc})") from exc
        except Exception as exc:  # 4xx/5xx, rede
            codigo = re.search(r"\bML (\d{3})\b", str(exc))
            resultado = "429" if codigo and codigo.group(1) == "429" else f"erro {codigo.group(1)}" if codigo else "erro"
            self.orc.registrar("api", resultado, int((time.monotonic() - inicio) * 1000))
            if resultado == "429":
                raise ParouDeConsultar("a API do ML pediu para esperar (429)") from exc
            raise
        self.orc.registrar("api", "ok", int((time.monotonic() - inicio) * 1000))
        return dados or {}

    def _pagina(self, link: str):
        if not self.orc.pode("pagina"):
            raise ParouDeConsultar("orçamento de páginas esgotado ou em pausa")
        self._esperar("pagina", INTERVALO_PAGINA_S)
        inicio = time.monotonic()
        card = self.paginas.consultar(link, int(self.relogio()))
        verificacao = "verificação" in (card.erro or "")
        self.orc.registrar("pagina", "verificacao" if verificacao else "ok" if card.ok else "erro",
                           int((time.monotonic() - inicio) * 1000))
        if verificacao:
            raise ParouDeConsultar("o ML pediu verificação de conta: parei sem contornar")
        return card

    def _link_gerado(self, item_id: str, titulo: str) -> str:
        if not self.gerador or not self.orc.pode("gerador"):
            return ""
        inicio = time.monotonic()
        try:
            link = self.gerador(item_id, titulo) or ""
        except Exception:  # noqa: BLE001 - sem o link, a página não é conferida
            link = ""
        self.orc.registrar("gerador", "ok" if link else "erro", int((time.monotonic() - inicio) * 1000))
        return link

    # leitura do catálogo e dos vendedores
    def ler_catalogo(self, produto_id: str, item_enviado: str = "") -> list[OfertaML]:
        ofertas = _ofertas_da_resposta(self._api(f"/products/{produto_id}/items?limit=50").get("results"))
        if item_enviado and len(ofertas) >= 50 and _sem_hifen(item_enviado) not in {_sem_hifen(o.item_id) for o in ofertas}:
            ofertas += _ofertas_da_resposta(self._api(f"/products/{produto_id}/items?limit=50&offset=50").get("results"))
        return sorted(ofertas, key=lambda o: o.preco)

    def reputacao(self, seller_id, novas: list) -> dict:
        if not seller_id:
            return {}
        r = self.con.execute("SELECT ts, dados FROM vendedores WHERE seller_id = ?", (seller_id,)).fetchone()
        if r and self.relogio() - r[0] < VENDEDOR_GUARDADO_S:
            return json.loads(r[1])
        if novas[0] >= REPUTACOES_NOVAS_POR_COMPARACAO:
            return {}
        novas[0] += 1
        try:
            u = self._api(f"/users/{seller_id}")
        except ParouDeConsultar:
            raise
        except Exception:  # noqa: BLE001 - sem a reputação, a oferta fica de fora (sem selo) ou sem o corte de 5 mil
            return {}
        rep = u.get("seller_reputation") or {}
        dados = reputacao(str(u.get("nickname") or ""), rep.get("level_id"), rep.get("power_seller_status"),
                          (rep.get("transactions") or {}).get("total"))
        dados["pais"] = str(u.get("country_id") or "")
        self.con.execute("INSERT OR REPLACE INTO vendedores (seller_id, ts, dados) VALUES (?,?,?)",
                         (seller_id, int(self.relogio()), json.dumps(dados, ensure_ascii=False)))
        return dados

    def reputacoes(self, ofertas: list[OfertaML], item_enviado: str) -> dict:
        """A do vendedor enviado e as das ofertas que podem ganhar dele: oficiais até o preço dele e as sem selo mais
        baratas, das mais baratas para cima (as guardadas há menos de 7 dias não gastam consulta)."""
        enviado = next((o for o in ofertas if _sem_hifen(o.item_id) == _sem_hifen(item_enviado)), None)
        teto = enviado.preco if enviado else float("inf")
        candidatas = [o for o in ofertas if o is not enviado and o.preco <= teto]
        # as oficiais primeiro: são elas que decidem a comparação (e o corte de 5 mil vendas precisa da reputação)
        alvo = ([enviado] if enviado else []) + sorted(candidatas, key=lambda o: (not o.loja_oficial, o.preco))
        novas, reps = [0], {}
        for o in alvo:
            if o.seller_id and o.seller_id not in reps:
                reps[o.seller_id] = self.reputacao(o.seller_id, novas)
        return reps

    def observar(self, ts: int, comparacao, produto_id: str, ofertas: list[OfertaML], reps: dict) -> None:
        self.con.executemany(
            "INSERT INTO observacoes (ts, comparacao, fonte, produto_id, item_id, seller_id, vendedor, oficial, "
            "internacional, frete_gratis, preco, preco_anterior) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, comparacao, "api", produto_id, o.item_id, o.seller_id, (reps.get(o.seller_id) or {}).get("nome", ""),
              int(bool(o.loja_oficial)), int(bool(o.internacional)), int(bool(o.frete_gratis)), o.preco,
              o.preco_anterior) for o in ofertas])

    def observar_card(self, ts: int, comparacao, card, link: str) -> None:
        self.con.execute(
            "INSERT INTO observacoes (ts, comparacao, fonte, produto_id, item_id, seller_id, vendedor, oficial, "
            "internacional, full, preco, preco_anterior, preco_outros, preco_cupom, titulo, link) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, comparacao, "pagina", card.produto_id, card.item_id, card.seller_id, card.vendedor,
             int(bool(card.loja_oficial)), int(bool(card.internacional)), int(bool(card.full)), card.preco,
             card.preco_anterior, card.preco_outros, card.preco_cupom, card.titulo, link))

    # o anúncio de cada envio ou post
    def anuncio_do_link(self, *links) -> dict:
        """produto_id, item_id e título do anúncio do link, pelo card que a curadora já abriu (banco dela, só leitura)."""
        for link in links:
            if not link:
                continue
            r = self.cur.execute("SELECT dados FROM ml_cards WHERE link = ?", (link,)).fetchone()
            if r:
                try:
                    d = json.loads(r[0])
                except ValueError:
                    continue
                if d.get("ok") and (d.get("produto_id") or d.get("item_id")):
                    return d
        return {}

    def link_da_alternativa(self, item_id: str, titulo: str) -> str:
        """O link meli.la do ANÚNCIO da alternativa, pelo gerador (o mesmo da troca para a loja oficial no pipeline):
        ele prende a loja. 04/11 (ensaio): o link de grupo do mesmo anúncio abria a página do produto, e o ML mostrou
        outra loja em destaque. Nada de abrir página do produto direto: lá o ML pede verificação."""
        return self._link_gerado(item_id, titulo)

    def _registrar(self, origem: str, ref: str, ts_ref: float, ts: float, base: dict, r: dict) -> int | None:
        cur = self.con.execute(
            "INSERT OR IGNORE INTO comparacoes (origem, ref, ts_ref, ts, chave, produto, link, produto_id, item_enviado, "
            "seller_enviado, oficial_enviado, preco_api_enviado, preco_texto, cupom, status, motivo, tipo, item_melhor, "
            "seller_melhor, preco_api_melhor, diferenca, diferenca_pct, sem_selo_dif, dados) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (origem, ref, int(ts_ref), int(ts), base.get("chave"), base.get("produto"), base.get("link"),
             base.get("produto_id"), base.get("item_id"), r.get("seller_enviado"),
             None if "oficial_enviado" not in r else int(r["oficial_enviado"]), r.get("preco_api_enviado"),
             base.get("preco_texto"), ", ".join(base.get("cupons") or []), r["status"], r.get("motivo", ""),
             r.get("tipo"), r.get("item_melhor"), r.get("seller_melhor"), r.get("preco_api_melhor"), r.get("diferenca"),
             r.get("diferenca_pct"), (r.get("sem_selo_mais_barata") or {}).get("diferenca"),
             json.dumps({k: v for k, v in r.items() if k in ("alternativas", "descartes", "sem_selo_mais_barata",
                                                            "status_sem_cupom", "vendedores_no_catalogo",
                                                            "sem_selo_mais_caras", "classe_enviado", "motivo_enviado")},
                        ensure_ascii=False)))
        self.con.commit()
        return cur.lastrowid if cur.rowcount else None

    def comparar_um(self, origem: str, ref: str, ts_ref: float, base: dict, conferir_pagina: bool) -> str:
        """Uma comparação completa. `base`: chave, produto, link, produto_id, item_id, titulo, cupons, preco_texto."""
        agora = self.relogio()
        if self.con.execute("SELECT 1 FROM comparacoes WHERE ref = ?", (ref,)).fetchone():
            return "já comparado"
        if agora - ts_ref > ATRASO_MAX_S:
            self._registrar(origem, ref, ts_ref, agora, base,
                            {"status": ATRASADA, "motivo": f"{int((agora - ts_ref) // 60)} min depois do envio"})
            return ATRASADA
        if not base.get("produto_id") or not base.get("item_id"):
            self._registrar(origem, ref, ts_ref, agora, base, {"status": INCONCLUSIVO, "motivo": "sem produto de catálogo"})
            return INCONCLUSIVO
        try:
            ofertas = self.ler_catalogo(base["produto_id"], base["item_id"])
            reps = self.reputacoes(ofertas, base["item_id"])
        except ParouDeConsultar:
            raise
        except Exception as exc:  # noqa: BLE001 - a falha fica registrada, a volta segue
            self._registrar(origem, ref, ts_ref, agora, base,
                            {"status": INCONCLUSIVO, "motivo": f"falha na leitura do catálogo ({type(exc).__name__})"})
            return INCONCLUSIVO
        lido = int(self.relogio())
        r = comparar(base["item_id"], ofertas, reps, base.get("cupons") or [])
        if not ofertas:
            r.update(status=INCONCLUSIVO, motivo="o catálogo não tem ofertas")
        comparacao = self._registrar(origem, ref, ts_ref, lido, base, r)
        self.observar(lido, comparacao, base["produto_id"], ofertas, reps)
        self.con.commit()
        if conferir_pagina and r.get("prioridade") and comparacao:
            self.conferir(comparacao, base, r)
        return self.con.execute("SELECT status FROM comparacoes WHERE id = ?", (comparacao,)).fetchone()[0] \
            if comparacao else r["status"]

    def conferir(self, comparacao: int, base: dict, r: dict) -> None:
        """As duas páginas, uma logo depois da outra (o anúncio enviado primeiro)."""
        if "meli.la/" not in (base.get("link") or ""):
            self._atualizar_pagina(comparacao, {"status": MELHOR_NA_API, "motivo": "sem o link do anúncio enviado"})
            return
        if not self.orc.pode("pagina", 2):
            self._atualizar_pagina(comparacao, {"status": MELHOR_NA_API, "motivo": "orçamento de páginas: não conferida"})
            return
        link_alt = self.link_da_alternativa(r["item_melhor"], base.get("titulo") or base.get("produto") or "")
        if not link_alt:
            self._atualizar_pagina(comparacao, {"status": MELHOR_NA_API, "motivo": "sem link que abra a alternativa"})
            return
        card_env = self._pagina(base["link"])
        ts_env = int(self.relogio())
        self.observar_card(ts_env, comparacao, card_env, base["link"])
        card_alt = self._pagina(link_alt)
        ts_alt = int(self.relogio())
        self.observar_card(ts_alt, comparacao, card_alt, link_alt)
        p = conferir_paginas(card_env, card_alt, r["item_melhor"], self.ident, base.get("item_id", ""))
        p.update(ts_enviado=ts_env, ts_melhor=ts_alt, link_melhor=link_alt)
        self._atualizar_pagina(comparacao, p)

    def _atualizar_pagina(self, comparacao: int, p: dict) -> None:
        economia = p.get("diferenca_pix") if p["status"] == MELHOR_CONFIRMADA else None
        self.con.execute("UPDATE comparacoes SET status = ?, motivo = ?, pagina = ?, economia_pix = ? WHERE id = ?",
                         (p["status"], p.get("motivo", ""), json.dumps(p, ensure_ascii=False), economia, comparacao))
        self.con.commit()

    # de onde vem o trabalho
    def envios_novos(self, desde: float) -> list[dict]:
        if not self.registro.exists():
            return []
        envios = []
        with self.registro.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 3_000_000))
            for linha in f.read().decode("utf-8", "ignore").splitlines():
                try:
                    e = json.loads(linha)
                    ts = float(e.get("ts") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if ts > desde and e.get("status") == "enviado":
                    envios.append(e)
        return sorted(envios, key=lambda e: float(e["ts"]))

    def base_do_envio(self, e: dict) -> dict:
        d = self.anuncio_do_link(e.get("link_afiliado"), e.get("link_origem"))
        produto_id, item_id = d.get("produto_id") or "", d.get("item_id") or ""
        if not (produto_id and item_id):
            ids = [_sem_hifen(i) for i in e.get("ml_ids") or []]
            catalogo = {i for i in ids if self.cur.execute("SELECT 1 FROM catalogo_ml WHERE produto_id = ?",
                                                         (i,)).fetchone()}
            produto_id = produto_id or next(iter(sorted(catalogo)), "")
            item_id = item_id or next((i for i in ids if i not in catalogo), "")
        cupons = [c for c in [e.get("cupom_quente")] if c]
        cupons += [r.get("codigo") for r in e.get("regras_cupom") or [] if r.get("codigo") and not r.get("tirado")
                   and r.get("codigo") not in cupons]
        return {"chave": e.get("chave"), "produto": e.get("produto"), "link": e.get("link_afiliado") or "",
                "produto_id": _sem_hifen(produto_id), "item_id": _sem_hifen(item_id), "titulo": d.get("titulo", ""),
                "cupons": cupons, "preco_texto": e.get("preco_enviado") or e.get("preco")}

    def posts_do_grupofinal(self, desde: float, limite: int) -> list[tuple]:
        return self.cur.execute("SELECT msg_id, ts, chave, produto, link, dados FROM postagens WHERE ts > ? AND "
                                "link LIKE '%meli.la/%' ORDER BY ts LIMIT ?", (int(desde), limite)).fetchall()

    def releituras(self) -> list[tuple]:
        return self.con.execute(
            "SELECT id, produto_id, item_enviado, item_melhor FROM comparacoes WHERE status IN (?, ?) AND "
            "ts_releitura IS NULL AND ts <= ? ORDER BY ts LIMIT ?",
            (MELHOR_CONFIRMADA, MELHOR_NA_API, int(self.relogio() - RELEITURA_S), RELEITURAS_POR_VOLTA)).fetchall()

    def reler(self, comparacao: int, produto_id: str, item_enviado: str, item_melhor: str) -> None:
        ofertas = self.ler_catalogo(produto_id, item_enviado)
        lido = int(self.relogio())
        self.observar(lido, comparacao, produto_id, ofertas, {})
        por_item = {_sem_hifen(o.item_id): o for o in ofertas}
        env, alt = por_item.get(_sem_hifen(item_enviado)), por_item.get(_sem_hifen(item_melhor))
        persistiu = None if not (env and alt) else int(prioridade(*_diferenca(env.preco, alt.preco)))
        self.con.execute("UPDATE comparacoes SET ts_releitura = ?, persistiu = ? WHERE id = ?", (lido, persistiu, comparacao))
        self.con.commit()

    def rodada(self) -> Counter:
        """Uma volta: releituras de 1 h, envios novos e a amostra do GrupoFinal. Para (sem erro) no orçamento ou na pausa."""
        agora = self.relogio()
        try:
            for comparacao, produto_id, item_enviado, item_melhor in self.releituras():
                try:
                    self.reler(comparacao, produto_id, item_enviado, item_melhor)
                except ParouDeConsultar:
                    raise
                except Exception:  # noqa: BLE001 - a releitura que falhou fica sem resposta (não tenta para sempre)
                    self.con.execute("UPDATE comparacoes SET ts_releitura = ? WHERE id = ?", (int(self.relogio()), comparacao))
                self.resumo["releituras"] += 1
            desde = float(ler_estado(self.con, "ultimo_envio", "0") or 0)
            if not desde:   # 1ª volta: começa 15 min atrás e guarda já (uma pausa logo de cara não perde o envio)
                desde = agora - ATRASO_MAX_S
                gravar_estado(self.con, "ultimo_envio", desde)
                self.con.commit()
            for e in self.envios_novos(desde)[:ENVIOS_POR_VOLTA]:
                ts = float(e["ts"])
                status = self.comparar_um("envio", f"envio:{ts:.3f}:{e.get('chave')}", ts, self.base_do_envio(e), True)
                gravar_estado(self.con, "ultimo_envio", ts)
                self.con.commit()
                self.resumo[f"envio {status}"] += 1
            # Amostra do GrupoFinal (04/11, Breno: "somente em leitura"): só a API, sem abrir página; os posts mais recentes
            # (até 2 por volta e 50 por dia). Os outros da volta ficam fora da amostra, sem registro.
            hoje = self.con.execute("SELECT count(*) FROM comparacoes WHERE origem = 'grupofinal' AND ts >= ?",
                                    (int(_inicio_do_dia(agora)),)).fetchone()[0]
            desde_h = float(ler_estado(self.con, "ultimo_grupofinal", "0") or 0) or agora - ATRASO_MAX_S
            posts = self.posts_do_grupofinal(desde_h, 500)
            if posts:
                gravar_estado(self.con, "ultimo_grupofinal", max(p[1] for p in posts))
                self.con.commit()
            recentes = [p for p in posts if agora - p[1] <= ATRASO_MAX_S][-GRUPOFINAL_POR_VOLTA:]
            for msg_id, ts, chave, produto, link, dados in recentes:
                if hoje >= GRUPOFINAL_POR_DIA:
                    self.resumo["grupofinal: amostra do dia completa"] += 1
                    break
                d = self.anuncio_do_link(link)
                try:
                    cupons = list(json.loads(dados or "{}").get("cupons") or [])
                except ValueError:
                    cupons = []
                base = {"chave": chave, "produto": produto, "link": link, "produto_id": _sem_hifen(d.get("produto_id")),
                        "item_id": _sem_hifen(d.get("item_id")), "titulo": d.get("titulo", ""), "cupons": cupons}
                status = self.comparar_um("grupofinal", f"grupofinal:{msg_id}", ts, base, False)
                hoje += 1
                self.resumo[f"grupofinal {status}"] += 1
        except ParouDeConsultar as exc:
            self.resumo[f"parou: {exc}"] += 1
        self.con.commit()
        return self.resumo


# ── relatório ───────────────────────────────────────────────────
def _brl(v) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v is not None else "?"


ROTULOS = {MELHOR_CONFIRMADA: "oficial mais barata, confirmada nas páginas (Pix com Pix)",
           NAO_CONFIRMADA: "a API dizia mais barata e as páginas não confirmaram",
           MELHOR_NA_API: "oficial mais barata só na API (páginas não conferidas)",
           DIFERENCA_PEQUENA: "oficial mais barata por menos de 2% ou de R$ 2",
           ENVIADA_MENOR: "a enviada já era a oficial mais barata (ou empate)",
           SEM_ALTERNATIVA: "sem outra loja oficial válida para comparar",
           CUPOM_INCERTO: "cupom incerto (à parte: nem ganho nem perda)",
           INCONCLUSIVO: "inconclusivo", ATRASADA: "comparação atrasada (mais de 15 min)"}


VALIDAS = (MELHOR_CONFIRMADA, NAO_CONFIRMADA, MELHOR_NA_API, DIFERENCA_PEQUENA, ENVIADA_MENOR)   # 1+ oficial comparada
INCONCLUSIVAS = (SEM_ALTERNATIVA, INCONCLUSIVO, ATRASADA)
COBERTURA = ("**Cobertura: só o catálogo consultado.** Cada comparação usa as ofertas do catálogo do ML do produto "
             "enviado (o mesmo cadastro; a 1ª página de 50 ofertas e a 2ª quando o anúncio enviado não está na 1ª). "
             "Outros cadastros do mesmo perfume e produtos novos ainda não entram: a busca ampla é etapa futura do plano. "
             "\"Melhor\" aqui quer dizer melhor dentro desse catálogo, não do ML inteiro.")


def _exemplo(ts_ref, produto, status, motivo, p_env, p_alt, dif, pagina) -> str:
    hora = time.strftime("%d/%m %H:%M", time.localtime(ts_ref))
    if pagina:
        p = json.loads(pagina)
        if p.get("pix_enviado") is not None:
            return (f"- {hora} · {produto} · enviado Pix {_brl(p.get('pix_enviado'))} (outros {_brl(p.get('outros_enviado'))})"
                    f" x {p.get('vendedor_pagina_melhor') or 'alternativa'} Pix {_brl(p.get('pix_melhor'))} "
                    f"(outros {_brl(p.get('outros_melhor'))}) · {ROTULOS.get(status, status)}"
                    + (f": {motivo}" if motivo else "") + (f" · {p['link_melhor']}" if p.get("link_melhor") else ""))
    return (f"- {hora} · {produto} · API: enviado {_brl(p_env)} x oficial {_brl(p_alt)} (diferença {_brl(dif)}) · "
            f"{ROTULOS.get(status, status)}" + (f": {motivo}" if motivo else ""))


def relatorio(con, desde: float, ate: float) -> str:
    linhas = [f"# Comparador em sombra: {time.strftime('%d/%m %H:%M', time.localtime(desde))} a "
              f"{time.strftime('%d/%m %H:%M', time.localtime(ate))}", "",
              "Só observação: nenhuma escolha, nota ou envio foi alterado. Preço da API com preço da API no mesmo retrato "
              "do catálogo; as páginas conferem as diferenças de 2% e R$ 2. Checkout, frete por CEP e estoque não "
              "estão comprovados. Comparação feita depois do envio não prova que havia preço melhor naquele momento.",
              "", COBERTURA, ""]
    for origem, nome in (("envio", "Envios do pipeline"), ("grupofinal", "Amostra do GrupoFinal (só API)")):
        linhas_o = con.execute("SELECT status, motivo, tipo, economia_pix, diferenca, diferenca_pct, sem_selo_dif, persistiu, "
                               "ts - ts_ref FROM comparacoes WHERE origem = ? AND ts_ref BETWEEN ? AND ?",
                               (origem, int(desde), int(ate))).fetchall()
        por_status = Counter(s for s, *_ in linhas_o)
        validas = sum(por_status[s] for s in VALIDAS)
        inconclusivas = sum(por_status[s] for s in INCONCLUSIVAS)
        linhas += [f"## {nome}: {len(linhas_o)}", "",
                   f"Comparações válidas (com pelo menos 1 outra loja oficial válida no catálogo): **{validas}**. "
                   f"Inconclusivas: **{inconclusivas}**. Cupom incerto, à parte: **{por_status[CUPOM_INCERTO]}**.", "",
                   "| Resultado | Quantos |", "|---|---:|"]
        for s in ROTULOS:
            if por_status[s]:
                linhas.append(f"| {ROTULOS[s]} | {por_status[s]} |")
        conf = [x for x in linhas_o if x[0] == MELHOR_CONFIRMADA and x[3] is not None]
        if conf:
            eco = sorted(x[3] for x in conf)
            linhas += ["", f"Economia nas confirmadas: mediana {_brl(statistics.median(eco))}, total {_brl(sum(eco))} "
                           f"em {len(eco)}."]
        tipos = Counter(x[2] for x in linhas_o if x[0] in (MELHOR_CONFIRMADA, MELHOR_NA_API, DIFERENCA_PEQUENA))
        if tipos:
            linhas.append("Por tipo (oficial mais barata): " + "; ".join(f"{t}: {n}" for t, n in tipos.most_common()))
        sem_selo = sorted(x[6] for x in linhas_o if x[6])
        if sem_selo:
            linhas.append(f"Loja sem selo mais barata (à parte, critérios de confiança de hoje): {len(sem_selo)}; "
                          f"mediana {_brl(statistics.median(sem_selo))}.")
        rel = [x[7] for x in linhas_o if x[7] is not None]
        if rel:
            linhas.append(f"Ainda mais barata 1 h depois: {sum(rel)} de {len(rel)}.")
        motivos = Counter(f"{x[0]}: {x[1]}" for x in linhas_o if x[0] in (INCONCLUSIVO, NAO_CONFIRMADA) and x[1])
        if motivos:
            linhas.append("Motivos (inconclusivo e não confirmada): " + "; ".join(f"{m} ({n})" for m, n in motivos.most_common(8)))
        atrasos = sorted(x[8] for x in linhas_o if x[0] != ATRASADA and x[8] is not None)
        if atrasos:
            linhas.append(f"Atraso entre o envio e a leitura: mediana {statistics.median(atrasos) / 60:.1f} min.")
        conferidos = con.execute(
            "SELECT ts_ref, produto, status, motivo, preco_api_enviado, preco_api_melhor, diferenca, pagina FROM comparacoes "
            "WHERE origem = ? AND ts_ref BETWEEN ? AND ? AND pagina IS NOT NULL AND pagina LIKE '%pix_enviado%' "
            "ORDER BY (status = ?) DESC, diferenca DESC LIMIT 10", (origem, int(desde), int(ate), MELHOR_CONFIRMADA)).fetchall()
        if conferidos:
            linhas += ["", "Exemplos conferidos nas páginas (para auditar):"] + [_exemplo(*x) for x in conferidos]
        so_api = con.execute(
            "SELECT ts_ref, produto, status, motivo, preco_api_enviado, preco_api_melhor, diferenca, NULL FROM comparacoes "
            f"WHERE origem = ? AND ts_ref BETWEEN ? AND ? AND status IN ({','.join('?' * len(VALIDAS))}) AND "
            "(pagina IS NULL OR pagina NOT LIKE '%pix_enviado%') ORDER BY diferenca DESC LIMIT 5",
            (origem, int(desde), int(ate), *VALIDAS)).fetchall()
        if so_api:
            linhas += ["", "Exemplos de comparações válidas só pela API:"] + [_exemplo(*x) for x in so_api]
        linhas.append("")
    consultas = con.execute("SELECT tipo, resultado, count(*) FROM consultas WHERE ts BETWEEN ? AND ? GROUP BY 1, 2",
                            (int(desde), int(ate))).fetchall()
    linhas += ["## Consultas", ""] + [f"- {t} {r}: {n}" for t, r, n in sorted(consultas)]
    return "\n".join(linhas)


# ── execução ────────────────────────────────────────────────────
def dependencias_reais(con):
    """API (acesso emprestado, sem renovar), páginas (cache próprio) e o gerador de links do pipeline."""
    from . import carregar_config
    from .identidade import Identificador
    from .ml import ConsultorML
    from .ml_api import ClienteML
    api = ClienteML(emprestado=TOKEN_EMPRESTADO)
    api.app_compartilhado = None   # só lê o token da coletora: nunca renova nem grava nada na autenticação
    paginas = ConsultorML(con, ttl=60)   # cache no banco do comparador; nada vai para o histórico da curadora
    cfg = carregar_config()
    ident = Identificador(cfg["marcas"], cfg.get("sinonimos"))

    def gerador(item_id: str, titulo: str) -> str:
        sys.path.insert(0, str(RAIZ))
        import pipeline   # o gerador de links do pipeline (o mesmo da troca para a loja oficial); só o link
        numero = re.sub(r"\D", "", item_id)
        resposta = pipeline.gerar_pela_api(f"{titulo}\n{ANUNCIO.format(numero)}")
        return (resposta or {}).get("link") or ""

    return api, paginas, gerador, ident


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Comparador de ofertas em modo sombra (só observa e registra)")
    ap.add_argument("--relatorio", action="store_true", help="imprime o relatório (nada é consultado)")
    ap.add_argument("--dias", type=float, default=14)
    ap.add_argument("--banco", default=str(BANCO))
    ap.add_argument("--banco-curadora", default=str(BANCO_CURADORA), help="lido só para leitura")
    ap.add_argument("--registro", default=str(REGISTRO_PIPELINE), help="o registro dos envios do pipeline (só lido)")
    ap.add_argument("--ensaio", type=int, default=0,
                    help="teste antes de instalar: compara os N envios e os 2 posts do GrupoFinal mais recentes, de qualquer "
                         "hora (só para conferir o caminho: não vale como medição), num banco de ensaio (--banco)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = abrir(args.banco)
    if args.relatorio:
        agora = time.time()
        print(relatorio(con, agora - args.dias * 86400, agora))
        con.close()
        return 0
    if DESLIGADO.exists():
        print("comparador desligado (dados/comparador.desligado): nada feito")
        con.close()
        return 0
    trava = Trava(TRAVA)
    if not trava.pegar():
        print("outra execução do comparador está em andamento: saí sem fazer nada")
        con.close()
        return 0
    con_cur = None
    try:
        con_cur = abrir_curadora_so_leitura(Path(args.banco_curadora))
        api, paginas, gerador, ident = dependencias_reais(con)
        comp = Comparador(con, con_cur, api, paginas, gerador, Orcamento(con), ident, registro_pipeline=Path(args.registro))
        if args.ensaio:
            global ATRASO_MAX_S
            ATRASO_MAX_S = 7 * 86400
            envios = comp.envios_novos(0)[-args.ensaio:]
            gravar_estado(con, "ultimo_envio", float(envios[0]["ts"]) - 1 if envios else time.time())
            ultimos = con_cur.execute("SELECT ts FROM postagens WHERE link LIKE '%meli.la/%' ORDER BY ts DESC LIMIT 3").fetchall()
            gravar_estado(con, "ultimo_grupofinal", ultimos[-1][0] - 1 if ultimos else time.time())
            con.commit()
        resumo = comp.rodada()
        print(time.strftime("%d/%m %H:%M:%S"), "comparador:", dict(resumo) or "nada novo", flush=True)
    finally:
        trava.soltar()
        if con_cur is not None:
            con_cur.close()
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
