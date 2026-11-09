"""Memória de cupom esgotado (09/10/2026, pedido do Breno: errar menos e sair do loop da edição).

Cupom do ML acaba rápido: um código some dos grupos em ~1h30 (mediana) e cupom esgotado é o erro de preço que mais
dá trabalho na edição (03 a 09/10: 26 dos 47 preços que o Breno corrigiu eram cupom que ele tirou do post).
Dois sinais alimentam a memória:

  * a edição do Breno: cupom que estava no post da normalizadora e saiu da versão final dele = esgotado; cupom que
    ele manteve ou pôs = ativo. Backtest 03 a 09/10: "o Breno tirou nas últimas 12h" acertaria 19 dos 26 cupons
    tirados, sem nenhum alarme falso nos 35 que ele manteve;
  * os grupos: "cupom X esgotou / acabou / encerrou" marca o X; "o X voltou" desmarca. "Esgotando" (corre que
    acaba) não conta.

03/11 (Breno aprovou separar os dois, opção A): o que o Breno tirou na Edição é SUSPENSÃO preventiva (a Edição é
rascunho: tirar é precaução, não prova de que acabou); o aviso explícito ("esgotou" de grupo, "❌ Cupom X esgotado" da
equipe) e a saída da página oficial do ML são ESGOTAMENTO confirmado. Nos dois o cupom fica fora do preço, da rodada,
da sugestão e do post (a normalizadora tira o código), e nenhum dos dois volta sozinho com o tempo: até 03/11 o cupom
voltava 12h depois do último sinal. Só um sinal novo, posterior, tira o cupom do estado (ver esgotados). Quem usa: a
nota (preço sem o cupom), a sugestão de cupom, o painel e a normalizadora (lê a lista exportada e tira o cupom do post
antes de normalizar).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from .cupons import cupons_da_mensagem, cupons_vencidos, fora_de_perfume, trecho_do_cupom, validade_do_codigo

ESQUEMA_SINAIS = """
CREATE TABLE IF NOT EXISTS cupom_sinais (
    id      INTEGER PRIMARY KEY,
    codigo  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    tipo    TEXT NOT NULL,         -- 'esgotou' ou 'ativo'
    fonte   TEXT NOT NULL,         -- 'breno' (edição) ou 'grupo'
    detalhe TEXT NOT NULL DEFAULT '',
    ref     TEXT NOT NULL DEFAULT ''   -- de qual mensagem veio: a mesma mensagem não conta duas vezes
);
CREATE INDEX IF NOT EXISTS idx_cupom_sinais_ts ON cupom_sinais (ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cupom_sinais_ref ON cupom_sinais (codigo, tipo, ref);
-- Quem vendia o anúncio de cada post que passou pela normalizadora (para saber de que loja o Breno tirou o cupom).
CREATE TABLE IF NOT EXISTS lojas_dos_links (
    chave TEXT PRIMARY KEY,        -- chave do link do post (meli.la/XXXX)
    ts    INTEGER NOT NULL,
    loja  TEXT NOT NULL,           -- "id do vendedor|nome sem enfeite" (o que houver)
    nome  TEXT NOT NULL DEFAULT ''
);
"""
JANELA_S = 12 * 3600       # "fora desta loja" vale 12h; a lista da normalizadora guarda o parado há menos que isso
SUSPENSO, ESGOTADO = "suspenso", "esgotado"   # 03/11: os estados do cupom parado (ver esgotados)
INDICIO = "indício"                           # 04/11 (Astra): sumir da página oficial é indício, não aviso
QUENTE_S = 40 * 60       # cupom "quente": a equipe confirmou há menos de 40 min (cupom some dos grupos em ~1h30)
CUPOM_FRESCO_S = 3600      # cupom só segura preço se algum grupo usou na última hora (ou a equipe confirmou)
POSTO_MIN = 2             # em quantos posts o Breno precisa ter confirmado o cupom (pôs ou manteve) para valer
POSTO_S = 2 * 3600        # ... nas últimas 2 horas
CONHECIDO_S = 3 * 86400  # código citado num aviso de grupo só conta se foi visto como cupom nos últimos 3 dias

# "esgotou", "acabou", "encerrado", "não está funcionando"... mas não "esgotando", "acabou de chegar" nem "antes que acabe".
ESGOTOU = re.compile(r"(?i)\besgot(?:ou|ad[oa]s?|aram)\b|\bacab(?:ou|aram)\b(?!\s+de\b)|\bencerr(?:ou|ad[oa]s?|aram)\b"
                     r"|\bexpir(?:ou|ad[oa]s?)\b|\bn[aã]o\s+(?:est[aá]\s+|t[aá]\s+)?(?:funciona|funcionando|vale|valendo)\b"
                     r"|\bparou\s+de\s+funcionar\b|\bdesativad[oa]s?\b")
VOLTOU = re.compile(r"(?i)\bvolt(?:ou|aram)\b|\breativad[oa]s?\b|\brenovad[oa]s?\b")
# Separa as orações: "cupom CHAPEU esgotou, ta ativo esses PEDALADA ou BICICLETA" fala de dois grupos de cupons.
ORACOES = re.compile(r"[\n.;!?]+|,\s|\s(?:mas|por[eé]m|agora|use|usem|usa|troque|troca|substitu\w*)\s", re.I)
TOKEN = re.compile(r"[A-Za-z0-9]{4,30}")
PALAVRA_DE_AVISO = re.compile(r"ESGOT|ACAB|ENCERR|VOLT|ATIV|LIBERAD|EXPIR")


def _limpo(texto: str) -> str:
    return re.sub(r"[*_~`]", "", texto or "")


def sinais_do_grupo(texto: str, conhecidos: set[str]) -> list[tuple[str, str, str]]:
    """Cupons que um grupo diz que esgotaram (ou voltaram): [(codigo, 'esgotou'|'ativo', trecho)].

    Só vale código já visto como cupom nos grupos (`conhecidos`) e na MESMA oração da palavra: em "cupom CHAPEU
    esgotou, ta ativo esses PEDALADA ou BICICLETA" só o CHAPEU esgotou.
    """
    saida: list[tuple[str, str, str]] = []
    if not conhecidos:
        return saida
    for oracao in ORACOES.split(_limpo(texto)):
        if ESGOTOU.search(oracao):
            tipo = "esgotou"
        elif VOLTOU.search(oracao):
            tipo = "ativo"
        else:
            continue
        for tok in TOKEN.findall(oracao):
            codigo = tok.upper()
            if PALAVRA_DE_AVISO.search(codigo):
                continue  # "ESGOTADO" e "LIBERADOOO" já foram lidos como cupom em "Cupom ESGOTADO"
            if codigo in conhecidos and all(c != codigo for c, _, _ in saida):
                saida.append((codigo, tipo, oracao.strip()[:120]))
    return saida


def sinais_da_edicao(normalizado: str, final: str) -> list[tuple[str, str]]:
    """Compara o post da normalizadora com a versão final do Breno: cupom que saiu = esgotou; o que ficou = ativo."""
    antes = set(cupons_da_mensagem(normalizado))
    depois = set(cupons_da_mensagem(final))
    # "posto" (10/10): o cupom não vinha no post e o Breno pôs à mão. É o sinal forte de que ele está usando.
    return ([(c, "esgotou") for c in sorted(antes - depois)] + [(c, "ativo") for c in sorted(depois)]
            + [(c, "posto") for c in sorted(depois - antes)])


def registrar_sinal(con: sqlite3.Connection, codigo: str, ts: int, tipo: str, fonte: str, detalhe: str = "",
                    ref: str = "", loja: str = "", post: str = "") -> bool:
    cur = con.execute("INSERT OR IGNORE INTO cupom_sinais (codigo, ts, tipo, fonte, detalhe, ref, loja, post) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (codigo.upper(), int(ts), tipo, fonte, detalhe, ref, loja or None, post or None))
    return cur.rowcount > 0


# ── a loja de cada post ──────────────────────────────────────────────────────────────────────────────────────
def chaves_da_loja(card) -> tuple[str, str]:
    """Quem vende o anúncio: ("id do vendedor|nome sem enfeite", nome para mostrar). Vazio se não deu para saber."""
    if not (card and getattr(card, "ok", False)):
        return "", ""
    nome = str(getattr(card, "vendedor", "") or (getattr(card, "reputacao", None) or {}).get("nome") or "").strip()
    limpo = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", nome.lower()))
    partes = [str(getattr(card, "seller_id", "") or ""), limpo]
    return "|".join(p for p in partes if p), nome


def mesma_loja(a: str, b: str) -> bool:
    return bool(set((a or "").split("|")) & set((b or "").split("|")) - {""})


# 29/10 (Breno: "loja oficial é diferente do seller verificado: produto Natura da loja Natura tem cupom, Boticário da
# Boticário, Eudora da Eudora"): o cupom LOJAS<data> vale só na loja oficial da PRÓPRIA marca. Revenda com selo de loja
# oficial (Lipx, Klassey, Suffein) não tem. Hoje ele tirou o LOJAS2910 de Asad, Sabah e Club de Nuit (revendas) e o
# sistema entendeu "esgotou", tirando o cupom também da Natura e do Boticário, onde ele valia.
_EXTRA_MARCAS = frozenset({"oboticario", "boticario", "jpg", "jeanpaulgaultier", "carolinaherrera", "pacorabanne"})
_SUFIXO_LOJA = re.compile(r"(?:lojaoficial|oficial|official|brasil|brazil|store|loja|perfumes|perfumaria|parfums"
                          r"|cosmeticos)$")


def cupom_da_loja_da_marca(codigo: str) -> bool:
    return str(codigo or "").upper().startswith("LOJAS")


def _so_letras(texto) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(texto or "").lower()))


_marcas_cache: list = []


def _marcas() -> frozenset:
    if not _marcas_cache:
        try:
            from . import carregar_config
            marcas = carregar_config().get("marcas") or []
        except Exception:  # noqa: BLE001 - sem o config, fica só a lista fixa
            marcas = []
        _marcas_cache.append(frozenset({_so_letras(m) for m in marcas if m}) | _EXTRA_MARCAS)
    return _marcas_cache[0]


def loja_da_marca(loja: str) -> bool:
    """A loja (nome, ou chave "id|nome") é a loja oficial da própria marca (Natura, O Boticário, Eudora...)?"""
    nome = _so_letras(str(loja or "").split("|")[-1])
    sem = _SUFIXO_LOJA.sub("", nome)
    return bool(sem or nome) and ((sem or nome) in _marcas() or nome in _marcas())


def _inicio_do_dia(agora: float) -> float:
    return time.mktime(time.localtime(agora)[:3] + (0, 0, 0, 0, 0, -1))


_config_cache: dict = {}


def _lojas_marca_ligada() -> bool:
    """30/10: chave cupom_lojas_marca.ligado no config (ausente = ligada) para desligar a trava pelo nome LOJAS."""
    try:
        from . import ARQUIVO_CONFIG, carregar_config
        versao = Path(ARQUIVO_CONFIG).stat().st_mtime_ns
        if _config_cache.get("versao") != versao:
            ligada = (carregar_config().get("cupom_lojas_marca") or {}).get("ligado", True)
            _config_cache.update(versao=versao, ligada=bool(ligada))
        return _config_cache["ligada"]
    except Exception:  # noqa: BLE001 - sem o config, fica como estava (ligada)
        return True


def liberado_em_revenda(con: sqlite3.Connection, codigo: str, agora: float) -> bool:
    """30/10 (estudo de cupons): o Breno PÔS à mão o cupom num post de revenda hoje, então a trava pelo nome sai para
    esse código até o fim do dia (o LOJAS1710 de 17/10 valeu na Lipx e na Wathiq). Só o 'pôs' abre: o 'manteve' de um
    cupom que a rodada pôs abriria por engano (Klassey, 29/10 às 18:34)."""
    for (loja,) in con.execute("SELECT loja FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND tipo = 'posto' "
                               "AND ts >= ? AND ts <= ?", (codigo.upper(), _inicio_do_dia(agora), agora)):
        if loja and not loja_da_marca(loja):
            return True
    return False


def trava_da_marca(con: sqlite3.Connection, codigo: str, agora: float) -> bool:
    """O cupom vale só em loja da própria marca agora? (nome LOJAS*, chave ligada e a válvula não abriu hoje)"""
    return cupom_da_loja_da_marca(codigo) and _lojas_marca_ligada() and not liberado_em_revenda(con, codigo, agora)


def revenda_de_cupom_da_marca(con: sqlite3.Connection, codigo: str, loja: str, agora: float) -> bool:
    """Sinal de um cupom de loja da marca vindo de um post de revenda: quer dizer só "não vale nessa loja"."""
    return bool(loja) and not loja_da_marca(loja) and trava_da_marca(con, codigo, agora)


# 30/10 (estudo de cupons, 45 dias): o sinal mais forte de que o cupom acabou é nenhum grupo-fonte postar ele há um
# tempo. Sem post de grupo há até 15 min, o Breno tirou 31%; há mais de 1h, 86-92%. Na rodada de cupom, "nenhum grupo
# nos últimos 45 min" cortaria 17 envios postados: 16 ele tirou e 1 manteve. O post no GrupoFinal não renova (sai em mediana
# 29 min depois da edição).
FRESCOR_GRUPO_S = 45 * 60
FRESCOR_A_MAO_S = 40 * 60


def fresco(con: sqlite3.Connection, codigo: str, agora: float) -> bool:
    """Algum grupo-fonte postou o cupom nos últimos 45 min, ou o Breno pôs à mão nos últimos 40?"""
    r = con.execute("SELECT ultimo_ts FROM cupons WHERE codigo = ?", (codigo,)).fetchone()
    if r and r[0] and 0 <= agora - r[0] <= FRESCOR_GRUPO_S:
        return True
    if r and r[0] and r[0] > agora:
        # No replay, ultimo_ts pode ser de depois da decisão. Só vale evidência já disponível.
        for (cupons,) in con.execute("SELECT cupons FROM ofertas WHERE ts >= ? AND ts <= ?",
                                    (agora - FRESCOR_GRUPO_S, agora)):
            try:
                if codigo in json.loads(cupons or "[]"):
                    return True
            except (ValueError, TypeError):
                continue
    r = con.execute("SELECT max(ts) FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND tipo = 'posto' AND ts <= ?",
                    (codigo, agora)).fetchone()
    return bool(r and r[0] and agora - r[0] <= FRESCOR_A_MAO_S)


REGISTRO_PIPELINE = Path(__file__).resolve().parent.parent / "dados" / "pipeline.jsonl"
_rodada_cache: dict = {}


def posts_da_rodada(codigo: str) -> set[str]:
    """Os posts (meli.la/...) em que o pipeline mandou este cupom. O 'manteve' do Breno neles não confirma o cupom
    (30/10, estudo de cupons: em 29/10 às 18:31 ele manteve o LOJAS2910 que a rodada pôs na Lipx e na Suffein, e tirou
    4 min depois; esse 'manteve' esquentou o cupom e mandou o Club de Nuit da Klassey às 18:34)."""
    try:
        st = REGISTRO_PIPELINE.stat()
    except OSError:
        return set()
    versao = (str(REGISTRO_PIPELINE), st.st_mtime_ns, st.st_size)
    if _rodada_cache.get("versao") != versao:
        por: dict[str, set] = {}
        for linha in REGISTRO_PIPELINE.read_text(encoding="utf-8", errors="ignore").splitlines()[-3000:]:
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            c = str(r.get("cupom_quente") or "").upper()
            m = re.search(r"meli\.la/[A-Za-z0-9]+", str(r.get("link_afiliado") or ""))
            if c and m:
                por.setdefault(c, set()).add(m.group(0))
        _rodada_cache.update(versao=versao, por=por)
    return _rodada_cache["por"].get(str(codigo or "").upper(), set())


def confirmado_na_loja_da_marca(con: sqlite3.Connection, codigo: str, agora: float) -> float:
    """Hora (de hoje) em que o Breno pôs ou manteve o cupom num post de loja da marca, depois da última vez que tirou
    de um post de loja da marca. 0 se não houve. O 'manteve' de post em que a rodada pôs o cupom não conta."""
    ultimo = tirou = 0.0
    rodada = posts_da_rodada(codigo)
    for ts, tipo, loja, post in con.execute(
            "SELECT ts, tipo, loja, post FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND ts >= ? AND ts <= ? "
            "ORDER BY ts, id", (codigo.upper(), _inicio_do_dia(agora), agora)):
        if not loja or not loja_da_marca(loja):
            continue
        if tipo == "posto" or (tipo == "ativo" and not (post and post in rodada)):
            ultimo = ts
        elif tipo == "esgotou":
            tirou = ts
    return ultimo if ultimo > tirou else 0.0


def guardar_loja_do_link(con: sqlite3.Connection, chave: str, ts: int, loja: str, nome: str = "") -> None:
    if chave and loja:
        con.execute("INSERT OR REPLACE INTO lojas_dos_links (chave, ts, loja, nome) VALUES (?,?,?,?)",
                    (chave, int(ts), loja, nome))


def loja_do_link(con: sqlite3.Connection, chave: str) -> tuple[str, str]:
    r = con.execute("SELECT loja, nome FROM lojas_dos_links WHERE chave = ?", (chave,)).fetchone()
    return (r[0], r[1]) if r else ("", "")


def tirou_do_post(con: sqlite3.Connection, codigo: str, ts: int, produto: str, ref: str, loja: str = "",
                  nome: str = "", post: str = "", confirmado_ts: int = 0, desde: float = 0) -> tuple[str, bool]:
    """O Breno tirou o cupom de um post (a regra está em _tirou_do_post). 30/10 (Breno: "Ficha do cupom"): a ficha,
    em modo sombra, registra como leria essa retirada ('fim' ou 'fora da classe'); não muda o que é gravado."""
    tipo, novo = _tirou_do_post(con, codigo, ts, produto, ref, loja, nome, post, confirmado_ts, desde)
    if novo:
        try:
            from .ficha_cupom import sombra_da_retirada
            sombra_da_retirada(con, codigo, ts, loja or nome, post, ref, tipo)
        except Exception:  # noqa: BLE001 - a sombra nunca derruba a 2ª leitura
            pass
    return tipo, novo


def _tirou_do_post(con: sqlite3.Connection, codigo: str, ts: int, produto: str, ref: str, loja: str = "",
                   nome: str = "", post: str = "", confirmado_ts: int = 0, desde: float = 0) -> tuple[str, bool]:
    """O Breno tirou o cupom de um post. Devolve (tipo gravado, se é novidade): 'fora' ou 'esgotou'.

    11/10 (Breno: "não enviei alguns perfumes para a Edição pois o cupom não se aplicava neles e se eu removesse
    acabaria a rodada"; depois: "ideia 2 é boa sim"). Com o cupom da equipe valendo:
    - tirar de UM post quer dizer "não vale nessa loja" (tipo 'fora'): o cupom segue nos produtos das outras;
    - tirar de dois posts SEGUIDOS (sem manter nem pôr em nenhum no meio) = acabou (tipo 'esgotou', como antes).
      21/10 (Breno: "aprovo apenas B"): vale mesmo que os dois sejam da mesma loja. O ESQFASHBEAUTY2110 acabou por volta
      das 19h; ele tirou do Yara Moi e do Ferassa (os dois da Lipx) às 19:02–19:03, o sistema entendeu "não vale na
      Lipx" e o ritmo de cupom só parou às 19:14, com mais 4 posts no preço errado. Antes (11/10), dois posts da mesma
      loja contavam como um; agora, para o cupom voltar, basta ele manter em 2 posts novos.
    Cupom que não é o da equipe continua como sempre: tirou, esgotou. `confirmado_ts` é a hora da última edição
    dele com o cupom que ainda não foi gravada (as mantidas esperam 3 min). `desde`: quando a normalizadora publicou
    este post (as versões dele contam como uma retirada só; 0 = não se sabe, e toda retirada conta).
    """
    codigo = codigo.upper()
    ja = con.execute("SELECT tipo FROM cupom_sinais WHERE codigo = ? AND ref = ? AND fonte = 'breno' AND "
                     "tipo IN ('fora', 'esgotou')", (codigo, ref)).fetchone()
    if ja:
        return ja[0], False   # edição relida (reinício) ou a passada dos 3 min: já está gravada
    if post and desde:
        # 03/11 (relatório da Astra: a Edição é rascunho): versão nova do MESMO post ainda sem o cupom não é outra
        # retirada. Em 30/10, às 17:35, a 2ª versão do Club de Nuit, do Yara Elixir e do Sabah contou de novo, cada
        # uma como retirada nova. Volta a contar só se ele manteve ou pôs o cupom nesse post no meio. O mesmo link
        # pode voltar em outra postagem (outro dia, outra campanha): só contam as versões desta, a partir de `desde`
        # (quando a normalizadora publicou o post).
        ultimo = con.execute("SELECT tipo FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND post = ? AND "
                             "ts >= ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
                             (codigo, post, int(desde), int(ts))).fetchone()
        if ultimo and ultimo[0] in ("fora", "esgotou"):
            return ultimo[0], False
    hora = time.strftime("%H:%M", time.localtime(ts))
    tipo, detalhe = "esgotou", f"você tirou do post do {produto} às {hora}"
    if revenda_de_cupom_da_marca(con, codigo, loja or nome, ts):   # 29/10: ver cupom_da_loja_da_marca
        detalhe += f": cupom de loja da marca, não vale na revenda {nome or loja}"
        return "fora", registrar_sinal(con, codigo, ts, "fora", "breno", detalhe, ref, loja, post)
    if codigo in quentes(con, ts):
        # 01/11: só o sinal de hoje conta (um 'fora' de ontem não faz da 1ª retirada de hoje a "2ª seguida")
        anterior = con.execute(
            "SELECT tipo, ts, loja FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND ts <= ? AND ts >= ? AND "
            "ref != ? AND coalesce(post, '') != ? AND tipo IN ('ativo', 'posto', 'fora', 'esgotou') "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (codigo, int(ts), _inicio_do_dia(ts), ref, post or "\0")).fetchone()
        if anterior and anterior[0] == "fora" and confirmado_ts <= anterior[1]:
            detalhe = f"você tirou de 2 posts seguidos (o 2º: {produto} às {hora})"
        else:
            tipo = "fora"
            detalhe += f": não vale na loja {nome}" if nome else ": não vale nesse anúncio"
    return tipo, registrar_sinal(con, codigo, ts, tipo, "breno", detalhe, ref, loja, post)


def lojas_fora(con: sqlite3.Connection, agora: float | None = None, janela_s: float = JANELA_S) -> dict[str, list[str]]:
    """Lojas em que o cupom não vale: {codigo: [chaves da loja]}. Vale o último sinal do Breno para o par
    cupom + loja: se depois de tirar ele manteve ou pôs o cupom num post dessa loja, ela volta."""
    agora = agora if agora is not None else time.time()
    estado: dict[str, list[tuple[str, str]]] = {}
    avisos = dict(con.execute("SELECT codigo, MAX(ts) FROM cupom_sinais WHERE tipo = 'aviso' AND fonte = 'equipe' AND "
                              "ts > ? AND ts <= ? GROUP BY codigo", (agora - janela_s, agora)).fetchall())
    for codigo, tipo, loja, ts in con.execute(
            "SELECT codigo, tipo, loja, ts FROM cupom_sinais WHERE fonte = 'breno' AND loja IS NOT NULL AND ts > ? AND "
            "ts <= ? ORDER BY ts, id", (agora - janela_s, agora)):
        if codigo in avisos and ts < _inicio_do_dia(avisos[codigo]):
            continue   # 01/11: a retirada de outro dia não vale na campanha nova que a equipe avisou no GrupoFinal
        vistas = [(l, t) for l, t in estado.get(codigo, []) if not mesma_loja(l, loja)]
        estado[codigo] = vistas + [(loja, tipo)]
    return {c: lojas for c, vistas in estado.items()
            if (lojas := [l for l, t in vistas if t in ("fora", "esgotou")])}


def codigos_conhecidos(con: sqlite3.Connection, agora: float) -> set[str]:
    # 01/11: também o que a equipe avisou no GrupoFinal nos últimos 3 dias. O aviso não renova ultimo_ts (é o frescor de
    # grupo), e o MODA10 que volta depois de dias sem grupo nenhum ficava sem o "esgotou" da equipe
    return {r[0] for r in con.execute("SELECT codigo FROM cupons WHERE ultimo_ts >= ? OR equipe_ts >= ?",
                                      (agora - CONHECIDO_S, agora - CONHECIDO_S))}


def registrar_sinais_de_grupo(con: sqlite3.Connection, texto: str, ts: int, grupo: str, ref: str,
                              ignorar: set[str] | frozenset = frozenset()) -> int:
    """Chamado para cada mensagem dos grupos-fonte. Só olha a mensagem se ela fala de esgotar/voltar. `ignorar`: no
    GrupoFinal, os cupons que a própria mensagem anuncia com regra (o "acabou" deles vem por registrar_aviso_da_equipe; o
    "(o MODA1010 acabou)" ou o "não vale com outros cupons" na linha do cupom novo marcariam o novo)."""
    if not texto or not (ESGOTOU.search(texto) or VOLTOU.search(texto)):
        return 0
    n = 0
    hora = time.strftime("%H:%M", time.localtime(ts))
    for codigo, tipo, trecho in sinais_do_grupo(texto, codigos_conhecidos(con, ts) - set(ignorar)):
        verbo = "avisou" if tipo == "esgotou" else "disse que voltou"
        n += registrar_sinal(con, codigo, ts, tipo, "grupo", f"{grupo} {verbo} às {hora}: \"{trecho[:70]}\"", ref)
    return n


def _sai_do_estado(estado: str, parado: dict, tipo: str, fonte: str, ts: int) -> bool:
    """O sinal (`tipo`, `fonte`, `ts`), posterior a `parado` (a suspensão, o indício ou o esgotamento do cupom), tira
    esse estado? Cada estado tem a sua regra; um sinal que tira um não mexe nos outros."""
    if tipo == "ativo" and fonte == "grupo":
        return True   # um grupo avisou que o cupom voltou
    if estado == SUSPENSO:
        if fonte == "breno":
            return tipo in ("ativo", "posto")   # você pôs ou manteve o cupom num post
        # 01/11: o aviso da equipe de outro dia é campanha nova com o mesmo código (o MODA10 que você tirou às 22h não
        # segura o aviso das 9h); o do mesmo dia não desfaz a sua retirada
        return tipo == "aviso" and fonte == "equipe" and parado["desde"] < _inicio_do_dia(ts)
    if estado == INDICIO:
        # Sumiu da página oficial: indício. Sai quando o cupom volta para a página, quando você põe ou mantém (como desde
        # 13/10, pagina_cupons.fora_da_pagina) ou com um aviso da equipe.
        return ((fonte == "pagina" and tipo == "ativo") or (fonte == "breno" and tipo in ("ativo", "posto"))
                or (fonte == "equipe" and tipo == "aviso"))
    # ESGOTADO, o aviso explícito de grupo ou da equipe: manter um post que veio com o cupom não basta (só pôr à mão), e
    # a página voltar a mostrar o cupom não desfaz o aviso (04/11, Astra: página -> grupo -> página apagava o grupo)
    if fonte == "breno":
        return tipo == "posto"
    return tipo == "aviso" and fonte == "equipe"   # aviso novo da equipe no GrupoFinal


def _rotulo(e: dict, agora: float) -> str:
    """"suspenso: você tirou do post do X às 11:10"; de outro dia, "suspenso desde 01/11: ..." (o parado não expira)."""
    desde = "" if e["desde"] >= _inicio_do_dia(agora) else time.strftime(" desde %d/%m", time.localtime(e["desde"]))
    return f"{e['estado']}{desde}: {e['detalhe']}"


def esgotados(con: sqlite3.Connection, agora: float | None = None) -> dict[str, dict]:
    """Os cupons parados agora: {codigo: {"estado": "esgotado"|"suspenso"|"indício", "estados", "desde": ts, "motivo",
    "fonte", "detalhe"}}. O motivo começa pelo estado ("suspenso: você tirou do post do X às 11:10").

    03/11 (Breno aprovou a separação, opção A, "garantindo que completar 12 horas não reative o cupom automaticamente"
    e que "a retomada use sinais novos, posteriores à suspensão"). Vale a ordem dos sinais, sem prazo:
    - SUSPENSO: você tirou na Edição (1 retirada de cupom fora da rodada, ou 2 seguidas do cupom da rodada: ver
      tirou_do_post). Sai quando você põe ou mantém o cupom num post, quando um grupo avisa que voltou, ou com o aviso
      da equipe de outro dia (campanha nova);
    - ESGOTADO: aviso explícito, "esgotou" de grupo ou "esgotado" da equipe no GrupoFinal. Sai quando você PÕE o cupom à mão
      num post, quando um grupo avisa que voltou ou com um aviso novo da equipe;
    - INDÍCIO (04/11, Astra): o cupom sumiu da página oficial do ML, que pode estar atrasada. Não é aviso: sai quando o
      cupom volta para a página, quando você põe ou mantém, quando um grupo avisa que voltou ou com um aviso da equipe.
    Os três são guardados à parte e cada um sai só pela sua regra. Aparece o mais forte (esgotado, depois suspenso,
    depois indício); "estados" diz todos os que valem. 04/11 (Astra reproduziu): esgotou pela página -> esgotou pelo
    grupo -> voltou para a página apagava o aviso do grupo, porque só o primeiro motivo ficava guardado.
    Grupo repostar o cupom não tira nenhum: os grupos repostam cupom morto (o MODA10 de 01/11 teve 13 posts depois do
    fim). O "fora" (não vale nessa loja) não entra aqui: ver lojas_fora."""
    agora = agora if agora is not None else time.time()
    parados: dict[str, dict] = {}   # codigo -> {SUSPENSO: a última retirada, ESGOTADO e INDICIO: o primeiro sinal}
    for codigo, ts, tipo, fonte, detalhe, loja in con.execute(
            "SELECT codigo, ts, tipo, fonte, detalhe, loja FROM cupom_sinais WHERE ts <= ? ORDER BY ts, id", (agora,)):
        p = parados.setdefault(codigo, {})
        if tipo == "esgotou":
            if fonte == "breno" and revenda_de_cupom_da_marca(con, codigo, loja, agora):
                continue  # 29/10: tirou de revenda um cupom de loja da marca = "não vale nessa loja"
            estado = SUSPENSO if fonte == "breno" else INDICIO if fonte == "pagina" else ESGOTADO
            if estado == SUSPENSO or estado not in p:
                p[estado] = {"desde": ts, "fonte": fonte, "detalhe": detalhe}
            continue
        for estado in [e for e in p if _sai_do_estado(e, p[e], tipo, fonte, ts)]:
            del p[estado]
    saida = {}
    for codigo, p in parados.items():
        estados = [e for e in (ESGOTADO, SUSPENSO, INDICIO) if e in p]
        if estados:
            e = {"estado": estados[0], "estados": estados, **p[estados[0]]}
            saida[codigo] = {**e, "motivo": _rotulo(e, agora)}
    return saida


# 03/11 (Breno: "a regra dos 45 minutos fica só em observação"): a suspensão viraria esgotamento confirmado se, nos 45 min
# seguintes, nenhum grupo-fonte postou o cupom e o GrupoFinal não publicou com ele (você pondo ou mantendo já tira a
# suspensão). Só leitura, para medir: nada usa isto para decidir.
SOMBRA_S = 45 * 60


def sombra_dos_45_min(con: sqlite3.Connection, agora: float | None = None) -> dict[str, int]:
    """Dos cupons suspensos agora, os que a regra dos 45 min daria como esgotados: {codigo: hora em que daria}."""
    agora = agora if agora is not None else time.time()
    saida = {}
    for codigo, e in esgotados(con, agora).items():
        ate = e["desde"] + SOMBRA_S
        if e["estado"] != SUSPENSO or ate > agora:
            continue
        if con.execute("SELECT 1 FROM ofertas WHERE ts > ? AND ts <= ? AND cupons LIKE ? LIMIT 1",
                       (e["desde"], ate, f'%"{codigo}"%')).fetchone() or _no_grupofinal_com(con, codigo, e["desde"] + 1, ate):
            continue
        saida[codigo] = int(ate)
    return saida


# 01/11 (Breno: "quando chegar cupom no grupo, ativar a rodada"; em 21/10, do CAMISA10: "quando fazem isso é um gatilho
# de cupom"): o aviso de cupom da equipe no GrupoFinal liga a rodada sem esperar 2 posts. Em 01/11 o MODA10 foi avisado às
# 09:06, um grupo postou perfume com ele às 09:16 e a rodada só ligou às 09:24, depois de você pôr o cupom à mão em 2
# posts. Mas o aviso traz os cupons do dia de todas as seções, e de 06/10 a 01/11 vários que podiam valer em perfume
# pela seção nunca apareceram em perfume (MODA2310, MODA1510, HOJEPROMO20, os de 30% "acima de R$ 1" de 12/10): por isso
# o aviso só liga a rodada com algum sinal de que o cupom vale em perfume.
AVISO_MINIMO_MAX = 300      # compra mínima acima disso não é cupom para perfume (ESQTVSCEL2110: acima de R$ 1.199)
AVISO_PROVA_S = 3 * 3600    # o sinal de perfume vale desde 3h antes do aviso


# O fim do cupom no aviso da equipe. Diferente do ESGOTOU dos grupos, "não vale com outros cupons" ou "não vale no Full"
# é restrição, não fim; "não vale mais" e "não está funcionando" são fim.
FIM = re.compile(r"(?i)\besgot(?:ou|ad[oa]s?|aram)\b|\bacab(?:ou|aram)\b(?!\s+de\b)|\bencerr(?:ou|ad[oa]s?|aram)\b"
                 r"|\bexpir(?:ou|ad[oa]s?)\b|\bn[aã]o\s+(?:est[aá]\s+|t[aá]\s+)?(?:funciona|funcionando|valendo)\b"
                 r"|\bn[aã]o\s+vale\s+mais\b|\bparou\s+de\s+funcionar\b|\bdesativad[oa]s?\b")


def registrar_aviso_da_equipe(con: sqlite3.Connection, texto: str, ts: int, ref: str) -> list[str]:
    """Aviso de cupom da equipe no GrupoFinal: um sinal 'aviso' (fonte 'equipe') para cada cupom com regra que pode valer em
    perfume (cupons.fora_de_perfume) e que o aviso não dá como acabado. Devolve os códigos gravados agora.

    Se o aviso diz que o cupom acabou, no trecho dele ("🎟️ MODA10 | 10% OFF máx R$20 | ESGOTOU") ou no título acima do
    🎟️ ("❌ CUPOM ESGOTADO | 🎟️ MODA10 | ..."), não grava o aviso e grava o "esgotou", como o de um grupo: o leitor dos
    grupos lê oração por oração e não junta o código numa linha com a palavra em outra. Só grava quando não há dúvida de
    qual cupom acabou (um código só no trecho; pelo título, ou sem 🎟️, um só na mensagem): em "🎟️ MODA10 (o MODA1010
    acabou)" ninguém leva aviso nem "esgotou"."""
    hora = time.strftime("%H:%M", time.localtime(ts))
    novos = []
    codigos = {c: r for c, r in cupons_da_mensagem(texto).items() if not FIM.search(c)}   # "Cupom ESGOTADO" não é código
    partes = re.split(r"(?=🎟)", texto or "")
    titulo = _limpo(partes[0]) if len(partes) > 1 and not partes[0].startswith("🎟") else ""
    fim_no_titulo = bool(FIM.search(titulo))
    for codigo, regra in codigos.items():
        trecho = _limpo(trecho_do_cupom(texto, codigo))
        fim_no_trecho = bool(FIM.search(trecho))
        if fim_no_trecho or fim_no_titulo:
            no_trecho = [c for c in codigos if re.search(rf"(?<![A-Z0-9]){re.escape(c)}(?![A-Z0-9])", trecho)]
            sem_duvida = len(no_trecho) == 1 if fim_no_trecho and len(partes) > 1 else len(codigos) == 1
            if sem_duvida and codigo in codigos_conhecidos(con, ts):
                registrar_sinal(con, codigo, ts, "esgotou", "grupo", f"a equipe avisou no GrupoFinal às {hora} que acabou",
                                ref or f"grupofinal:{ts}")
            continue
        if not (regra.pct or regra.valor) or fora_de_perfume(texto, codigo):
            continue
        resumo = re.split(r"(?i)ative aqui", " ".join(trecho.replace("🎟️", " ").replace("🎟", " ").split()))[0].strip()
        if registrar_sinal(con, codigo, ts, "aviso", "equipe", f"a equipe avisou no GrupoFinal às {hora}: {resumo[:90]}",
                           ref or f"grupofinal:{ts}"):
            novos.append(codigo)
    return novos


def _no_grupofinal_com(con: sqlite3.Connection, codigo: str, desde: float, ate: float) -> bool:
    """Saiu no GrupoFinal, entre `desde` e `ate`, um post com o cupom (no texto do post; a sugestão de cupom não conta)."""
    for (dados,) in con.execute("SELECT dados FROM postagens WHERE ts >= ? AND ts <= ? AND dados LIKE ?",
                                (desde, ate, f'%"{codigo}"%')):
        try:
            if codigo in (json.loads(dados).get("cupons") or []):
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


def avisos_com_prova(con: sqlite3.Connection, agora: float) -> dict[str, int]:
    """Os avisos da equipe dos últimos 40 min com sinal de que o cupom vale em perfume, desde 3h antes do aviso: um
    grupo-fonte postou perfume com ele, você pôs ou manteve num post, ou saiu no GrupoFinal um post com ele. {codigo: hora
    do aviso}. Ficam de fora: compra mínima acima de R$ 300; regra que a conta não aplica (R$ OFF sem compra mínima,
    como o MODA1510 de 15/10: a rodada ligaria sem pôr o cupom em nenhum preço); % sem teto conhecido (leitura que
    falhou: o "limite R$35" do TODOSITE2810 de 28/10 não era lido e a rodada daria os 30% inteiros); e o LOJAS<data> com
    a trava da loja da marca, que segue a frente própria (pipeline.cupom_da_marca_agora): a rodada geral iria com os
    queridinhos de revenda, onde ele não entra."""
    saida = {}
    for codigo, ts in con.execute(
            "SELECT s.codigo, MAX(s.ts) FROM cupom_sinais s JOIN cupons c ON c.codigo = s.codigo WHERE s.tipo = 'aviso' "
            "AND s.fonte = 'equipe' AND s.ts > ? AND s.ts <= ? AND COALESCE(c.minimo, 0) <= ? AND ((COALESCE(c.pct, 0) > 0 "
            "AND COALESCE(c.limite, 0) > 0) OR (COALESCE(c.valor, 0) > 0 AND COALESCE(c.minimo, 0) > 0)) GROUP BY s.codigo",
            (agora - QUENTE_S, agora, AVISO_MINIMO_MAX)).fetchall():
        if trava_da_marca(con, codigo, agora):
            continue
        desde = ts - AVISO_PROVA_S
        if (con.execute("SELECT 1 FROM ofertas WHERE ts >= ? AND ts <= ? AND cupons LIKE ? LIMIT 1",
                        (desde, agora, f'%"{codigo}"%')).fetchone()
                or con.execute("SELECT 1 FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND tipo IN ('posto', "
                               "'ativo') AND ts >= ? AND ts <= ? LIMIT 1", (codigo, desde, agora)).fetchone()
                or _no_grupofinal_com(con, codigo, desde, agora)):
            saida[codigo] = ts
    return saida


def _chave_do_link(link: str | None) -> str:
    """A chave do post (meli.la/XXXX ou MLB123), a mesma de conferente.chave_link (que importa este módulo)."""
    m = re.search(r"meli\.la/([A-Za-z0-9]+)", link or "")
    if m:
        return "meli.la/" + m.group(1)
    m = re.search(r"MLB-?(\d{6,})", link or "", re.I)
    return f"MLB{m.group(1)}" if m else ""


def _grupofinal_conferido_depois(con: sqlite3.Connection, post: str, tirou: float) -> bool:
    """Sem parada, todo post no GrupoFinal vale. Depois de uma parada, só o post cuja oferta a curadora conferiu depois dela
    (04/11, Breno: "publicar depois da suspensão não prova que a oferta foi conferida depois dela")."""
    if not tirou:
        return True
    conferido = con.execute("SELECT ts FROM lojas_dos_links WHERE chave = ?", (post,)).fetchone() if post else None
    return bool(conferido and conferido[0] > tirou)


def _confirmacoes_do_grupofinal(con: sqlite3.Connection, posts: list[tuple[int, str, str]], tirou: float,
                           da_edicao: set, edicao_sem_link: bool) -> dict[str, int]:
    """Os posts no GrupoFinal que contam como confirmação do cupom: {postagem: hora do post}.

    04/11 (Breno: "um post sozinho não tira a suspensão. Pode contar como uma confirmação, sem duplicar a confirmação
    da mesma postagem na Edição. Publicar depois da suspensão não prova que a oferta foi conferida depois dela"):
    - cada postagem (o link; sem link, o produto) conta uma vez, e não conta se já contou pela Edição (`da_edicao`).
      Com confirmação da Edição, o post do GrupoFinal só soma se os dois lados têm o link e ele é outro: sem isso não dá
      para provar que é outra postagem (o erro de 10/10: ele manteve num post só e esse mesmo post saiu no GrupoFinal);
    - depois de uma parada (`tirou`), o post do GrupoFinal só conta se a curadora conferiu a oferta desse link depois dela
      (lojas_dos_links: a conferência de quando a promo entrou na normalizadora). Sem essa prova (post direto no GrupoFinal,
      ou promo conferida antes da parada), não conta. Sem parada, conta como antes.
    O post no GrupoFinal nunca tira a suspensão (ver esgotados): só soma para a rodada voltar."""
    saida: dict[str, int] = {}
    for ts, produto, post in posts:
        if not _grupofinal_conferido_depois(con, post, tirou):
            continue
        if da_edicao and (edicao_sem_link or not post):
            continue
        chave = post or f"produto:{produto}"
        if chave in da_edicao:
            continue
        saida[chave] = max(ts, saida.get(chave, 0))
    return saida


def quentes(con: sqlite3.Connection, agora: float | None = None) -> dict[str, dict]:
    """Cupons que a equipe está USANDO agora: {codigo: {"confirmado_em", "por"}}.

    Regra (11/10, 09:20; a de 10/10 exigia 2 posts com o cupom posto À MÃO e não pegou o MODA1110: o Breno pôs à
    mão em 1 e manteve em 2 que já vieram com ele, 3 posts no GrupoFinal em 3 minutos, e o pipeline não seguiu):
    - o cupom tem 2 confirmações ou mais nas últimas 2h, depois da última parada: postagens em que o Breno pôs à mão
      OU manteve, e posts no GrupoFinal com o cupom (04/11: somados, cada postagem uma vez; ver _confirmacoes_do_grupofinal).
      Uma só não basta: foi o erro do COMPRAPREFERIDA em 10/10 às 16:27;
    - a última confirmação (dele, ou post no GrupoFinal com o cupom) tem menos de 40 min;
    - não está esgotado nem vencido, e a regra dele é conhecida.
    Se ele tira o cupom de dois posts seguidos, para na hora (vira suspenso) e só volta com 2 posts novos.
    Tirar de um post só = "não vale nessa loja" (ver tirou_do_post e lojas_fora): o cupom segue nas outras.
    01/11: o aviso de cupom da equipe no GrupoFinal, com sinal de que o cupom vale em perfume (avisos_com_prova), vale como
    as 2 confirmações e como frescor por 40 min. O aviso de antes da última vez que ele tirou o cupom não conta.
    """
    agora = agora if agora is not None else time.time()
    usados = [r[0] for r in con.execute("SELECT DISTINCT codigo FROM cupom_sinais WHERE tipo IN ('posto', 'ativo') AND "
                                        "fonte = 'breno' AND ts > ? AND ts <= ?", (agora - POSTO_S, agora))]
    # 14/10 (Breno: "olha o grupo GrupoFinal, postaram o cupom lá, logo faz sentido valer"): posts no GrupoFinal com o cupom,
    # de quem for (de manhã a equipe postou 17 com o 20SITE sem passar pela Edição dele).
    produtos_no_grupofinal: dict[str, list[tuple[int, str, str]]] = {}
    for ts, chave, link, dados in con.execute("SELECT ts, chave, link, dados FROM postagens WHERE ts > ? AND ts <= ? AND "
                                              "dados LIKE '%cupons%'", (agora - POSTO_S, agora)):
        try:
            cupons = json.loads(dados).get("cupons") or []
        except (ValueError, TypeError):
            continue
        for codigo in cupons:
            produtos_no_grupofinal.setdefault(codigo, []).append((ts, chave or "", _chave_do_link(link)))
    avisados = avisos_com_prova(con, agora)
    if not usados and not produtos_no_grupofinal and not avisados:
        return {}
    mortos = esgotados(con, agora)
    saida = {}
    for codigo in sorted(set(usados) | set(produtos_no_grupofinal) | set(avisados)):
        if codigo in mortos or cupons_vencidos([codigo], agora):
            continue
        um = lambda sql, *p: con.execute(sql, (codigo, *p)).fetchone()
        # 03/11 (Breno: "a retomada use sinais novos, posteriores à suspensão, sem duplicar postagens"): conta só o que
        # veio depois da última parada, a sua retirada (fora a de revenda, 29/10) ou o "esgotou" de grupo, equipe ou
        # página; e cada postagem uma vez: duas versões do mesmo post são uma confirmação só.
        tirou = max((ts for ts, fonte, loja in con.execute(
            "SELECT ts, fonte, loja FROM cupom_sinais WHERE codigo = ? AND tipo = 'esgotou' AND ts <= ?", (codigo, agora))
            if not (fonte == "breno" and revenda_de_cupom_da_marca(con, codigo, loja, agora))), default=0)
        # 30/10: o 'manteve' de um post em que a própria rodada pôs o cupom não confirma (ver posts_da_rodada).
        rodada = posts_da_rodada(codigo)
        postagens, sem_link, ts_dele = set(), False, 0
        for ref, ts, tipo, post in con.execute(
                "SELECT ref, ts, tipo, post FROM cupom_sinais WHERE codigo = ? AND tipo IN ('posto', 'ativo') AND "
                "fonte = 'breno' AND ts > ? AND ts <= ?", (codigo, max(tirou, agora - POSTO_S), agora)):
            if tipo == "ativo" and post and post in rodada:
                continue
            postagens.add(post or ref)
            sem_link = sem_link or not post
            ts_dele = max(ts_dele, ts)
        grupofinal = _confirmacoes_do_grupofinal(con, produtos_no_grupofinal.get(codigo, []), tirou, postagens, sem_link)
        quantos = len(postagens) + len(grupofinal)
        aviso = avisados.get(codigo, 0) if avisados.get(codigo, 0) > tirou else 0   # 01/11: aviso da equipe no GrupoFinal
        if quantos < POSTO_MIN and not aviso:
            continue
        pos = um("SELECT 1 FROM cupom_sinais WHERE codigo = ? AND tipo = 'posto' AND fonte = 'breno' AND ts = ?",
                 ts_dele) if ts_dele else None
        # O frescor (40 min) é outra pergunta: o cupom ainda está em uso? O post no GrupoFinal conferido depois da parada
        # responde, mesmo que seja a postagem que já contou pela Edição.
        no_grupofinal = max((ts for ts, produto, post in produtos_no_grupofinal.get(codigo, [])
                        if ts > agora - QUENTE_S and _grupofinal_conferido_depois(con, post, tirou)), default=0)
        confirmado, por = max((ts_dele or 0, "você pôs à mão" if pos else "você manteve"),
                              (no_grupofinal, "post no GrupoFinal"), (aviso, "aviso de cupom no GrupoFinal"))
        if agora - confirmado >= QUENTE_S:
            continue
        if not (aviso or fresco(con, codigo, agora)):
            continue   # 30/10: nenhum grupo postou há 45 min e o Breno não pôs à mão há 40 (ver FRESCOR_GRUPO_S)
        if um("SELECT 1 FROM cupons WHERE codigo = ? AND (pct IS NOT NULL OR valor IS NOT NULL)"):
            saida[codigo] = {"confirmado_em": confirmado, "por": por}
    return saida


def lista_para_normalizadora(con: sqlite3.Connection, agora: float | None = None) -> dict[str, dict]:
    """O que a normalizadora tira do post: os cupons suspensos e os esgotados (03/11, opção A) e os com data no código
    que já passou ("FULL0910" no dia 10/10), entre os vistos há 3 dias. Do parado há mais de 12h, só o que um grupo ou
    o aviso da equipe usou nos últimos 3 dias: o resto não aparece nos posts. Ele continua parado (não volta com o
    tempo) e, se um grupo voltar a postar o código, volta para a lista."""
    agora = agora if agora is not None else time.time()
    conhecidos = codigos_conhecidos(con, agora)
    lista = {c: e for c, e in esgotados(con, agora).items() if c in conhecidos or e["desde"] > agora - JANELA_S}
    for (codigo,) in con.execute("SELECT codigo FROM cupons WHERE ultimo_ts >= ?", (agora - CONHECIDO_S,)):
        if codigo not in lista and cupons_vencidos([codigo], agora):
            data = validade_do_codigo(codigo, agora)
            lista[codigo] = {"desde": int(agora), "motivo": f"a data do código ({data:%d/%m}) já passou", "fonte": "data"}
    return lista
