"""Ficha do cupom, em MODO SOMBRA (30/10/2026, estudo de cupons de 45 dias; Breno: "Ficha do cupom").

O que a ficha responde, por código e por dia, só com os sinais do próprio dia (sem olhar o nome do código):
  * ONDE o cupom vale (escopo): por loja e por classe de loja;
  * SE ele ainda vale (estado): vivo, suspenso ou esgotado.

Classes de loja: 'marca' (loja oficial da própria marca: Natura na Natura, Boticário no Boticário), 'revenda' (selo de
loja oficial, mas não é a marca: Lipx, Klassey, Suffein) e 'comum' (sem selo). Exceções à mão em EXCECOES.

Fontes, na ordem da hora: posts dos grupos-fonte (ofertas), posts no GrupoFinal (postagens) e as decisões do Breno na
Edição (cupom_sinais: pôs, manteve, tirou). Aviso de "esgotou" de grupo: a retirada do Breno nos 30 min seguintes é
'fim' (AVISO_FIM_S).

MODO SOMBRA: a ficha só CALCULA e REGISTRA (dados/ficha_cupom_sombra.jsonl) o que faria. Nenhuma decisão do que está
no ar muda; ficha_relatorio.py compara depois com as decisões do Breno. Chave no config: ficha_cupom.modo =
"sombra" (padrão) ou "desligada".

Replay das 18 rodadas de 10 a 29/10 com este módulo e a LOJA CERTA de cada envio (estudo_cupons/replay/r12, 30/10:
loja do lojas_dos_links com o id do vendedor, selo da tabela lojas): o cupom iria em 39 envios certos e 13 errados (a
lógica antiga: 40 e 40; a fase Agora: 37 e 19; o estudo simulado: 39 e 17), perdendo 1 cupom bom. Por decisão: faixa 1
9 certos e 1 errado, faixa 2 25 e 6, SONDA 5 e 6 (sem sonda: 34 e 7, com 6 bons perdidos). O r10 dizia "a sonda acerta
12" porque 23 envios estavam sem vendedor e caíam em sonda; no replay do revisor (veredicto pelo sinal no post) a sonda
acerta 4 e erra 6. Fora da rodada: 13 dos 48 cupons que o Breno pôs à mão voltariam; "cupom novo sem ninguém pedir":
10 envios, 1 sem uso depois (eram 17 e 8 antes da leitura das retiradas em várias classes, quase tudo MODA1110).
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

from .cupons import cupons_vencidos
from .esgotados import chaves_da_loja, loja_da_marca, mesma_loja, posts_da_rodada

DIA_S = 86400
CLASSES = ("marca", "revenda", "comum")
# 30/10 (estudo): fatia de cada classe nos posts de grupo com cupom, em 45 dias. Concentração só conta se a fatia de
# hoje for 3x a normal: 80% em 'marca' (normal 20%) é sinal; 80% em 'comum' (normal 49%) é o dia a dia.
FATIA_NORMAL = {"marca": 0.20, "revenda": 0.31, "comum": 0.49}
CONC_POSTS = 8            # posts de grupo com vendedor conhecido para falar em concentração
CONC_FATIA = 0.80         # ... e a fatia da classe
CONC_LIFT = 3.0           # ... e 3x a fatia normal
CONC_SO_MARCA = True      # 30/10 (revisor): só a concentração em 'marca' fecha as outras (cupom de site inteiro não)
# 30/10 (revisor da sombra): concentração em 'marca' FECHA revenda e comum ('não') ou só as deixa 'sem prova' (sonda)
# até o Breno tirar de uma delas? Nos 45 dias ela só pesou em envio nos LOJAS1010 e LOJAS2910 (cupom vivo): 4 erros
# evitados, nenhum bom perdido; deixar 'sem prova' (False) mandaria esses 4 como sonda (replay r12: 39/13 vira 39/17).
# Os cupons de site que também concentraram (SITE0110, MODA1410, OFICIAIS1909) não tiveram nenhum uso do Breno nem do
# GrupoFinal no dia. O 'pôs' do Breno numa revenda reabre na hora (válvula). O relatório conta à parte o 'sem' que veio só
# da concentração.
CONC_FECHA = True
ASSENTA_S = 5 * 60        # 'manteve' só confirma depois de 5 min sem ser desfeito
EM_USO_S = 2 * 3600       # o "quente" de hoje: 2 confirmações nas últimas 2h (esgotados.quentes)...
EM_USO_N = 2
QUENTE_S = 40 * 60        # ... a última há menos de 40 min
FRESCOR_GRUPO_S = 45 * 60  # ... e algum grupo postou há até 45 min (ou o Breno pôs à mão há até 40)
FRESCOR_A_MAO_S = 40 * 60
CLASSE_CONF_S = 3 * 3600  # vivo pela classe: o Breno confirmou NESTA classe nas últimas 3h e um grupo usou nela há 45 min
FIM_SEGUIDOS = 2          # 2 retiradas 'fim' seguidas = esgotou; 1 = suspenso até o Breno confirmar de novo
# 30/10 (revisor, MODA1110 em 11/10): o Breno tirou de OUD, Mercado Livre e Armani entre 09:59 e 10:58 sem confirmar
# nada no meio; cada uma lida sozinha era 'fora da classe' e o cupom seguia vivo até 11:20. Retiradas seguidas em 2 ou
# mais classes (ou exceções) diferentes, sem confirmação no meio, contam todas como 'fim' QUANDO o cupom já vale em
# revenda ou comum (cupom de site: não tem classe). Cupom só da marca (LOJAS) não entra: tirar de uma revenda e de uma
# comum é o esperado. As 4 do LOJAS2910 (todas em revenda, só a marca aberta) seguem 'fora da classe'.
FIM_VARIAS_CLASSES = True
AVISO_FIM_S = 30 * 60     # retirada até 30 min depois de um grupo avisar "esgotou" é 'fim' (MODA1110: aviso às 10:01)
SONDAS_POR_LOJA = 1       # 'sem prova': no máximo 1 post por loja por código por dia
DISJUNTOR = 2             # 2 sondas tiradas seguidas encerram as sondas do código no dia
RITMO_MAX = 3             # no máximo 3 envios com o cupom numa rodada até a 1ª decisão do Breno sobre ela
RODADA_PAUSA_S = 45 * 60  # sem envio com o código há 45 min, a próxima é outra rodada

# 30/10 (estudo, lista à mão): Armani Beauty é a loja da marca; o vendedor "Mercado Livre" é especial; OUD e Rocha
# Importados vendem de tudo (mista). Nas especiais e mistas só vale a confirmação da própria loja, nunca a da classe.
EXCECOES = {"armanibeauty": "marca", "ralphlaurenfragrance": "marca", "calvinkleinfragrancias": "marca",
            "mercadolivre": "especial", "oud": "mista", "rochaimportados": "mista"}
SO_A_PROPRIA_LOJA = ("mista", "especial")
FAIXAS = {"loja confirmada": 1, "classe aberta": 2, "sem prova": 3}

REGISTRO_SOMBRA = Path(__file__).resolve().parent.parent / "dados" / "ficha_cupom_sombra.jsonl"


def _so_letras(texto) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(texto or "").lower()))


def _hora(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "-"


def _inicio_do_dia(agora: float) -> float:
    return time.mktime(time.localtime(agora)[:3] + (0, 0, 0, 0, 0, -1))


def _partes_da_loja(chave) -> tuple[str, str]:
    """("id do vendedor", "nome sem enfeite") de uma chave de loja "id|nome", "nome" ou "id"."""
    partes = [p for p in str(chave or "").split("|") if p]
    if len(partes) >= 2:
        return partes[0], _so_letras(partes[-1])
    if partes and partes[0].isdigit():
        return partes[0], ""
    return "", _so_letras(partes[0]) if partes else ""


def mesma_vitrine(a, b) -> bool:
    """A mesma loja para a ficha: o mesmo NOME (o id só decide quando falta o nome de um dos lados).

    30/10 (revisor): esgotados.mesma_loja junta por id OU nome, e um id de vendedor tem várias vitrines: o 480265022 é
    Armani Beauty, Lancôme, Ralph Lauren, Azzaro... e também o "Mercado Livre"; o 1459493882 é Beleza na Web, Carolina
    Herrera e Rabanne; o 1957713361 é Natura e Avon. Pôr no Armani Beauty não confirma o cupom no Mercado Livre."""
    ia, na = _partes_da_loja(a)
    ib, nb = _partes_da_loja(b)
    if na and nb:
        return na == nb
    return bool(ia and ia == ib)


# ── config ───────────────────────────────────────────────────────────────────────────────────────────────────
_config_cache: dict = {}


def modo() -> str:
    """'sombra' (padrão, também quando a chave falta ou o config não abre) ou 'desligada'."""
    try:
        from . import ARQUIVO_CONFIG, carregar_config
        versao = Path(ARQUIVO_CONFIG).stat().st_mtime_ns
        if _config_cache.get("versao") != versao:
            valor = str((carregar_config().get("ficha_cupom") or {}).get("modo") or "sombra").strip().lower()
            _config_cache.update(versao=versao, modo="desligada" if valor == "desligada" else "sombra")
        return _config_cache["modo"]
    except Exception:  # noqa: BLE001 - sem o config, fica no padrão
        return "sombra"


# ── classe da loja ───────────────────────────────────────────────────────────────────────────────────────────
def classe_da_loja(card=None, vendedor: str = "", oficial=None, seller_id=None) -> str:
    """'marca' | 'revenda' | 'comum' | 'mista' | 'especial' | '?' (sem vendedor).

    `card` pode ser o CardML, um dict {"vendedor", "oficial"} ou nada (aí valem vendedor/oficial). Sem saber do selo
    (oficial None), nome de marca conta como 'marca' e o resto fica '?'."""
    if card is not None:
        if isinstance(card, dict):
            vendedor = card.get("vendedor") or vendedor
            oficial = card.get("oficial", card.get("loja_oficial", oficial))
        elif getattr(card, "ok", True):
            vendedor = getattr(card, "vendedor", "") or vendedor
            oficial = getattr(card, "loja_oficial", oficial)
        else:
            return "?"
    nome = _so_letras(str(vendedor or "").split("|")[-1])
    if not nome:
        return "?"
    if nome in EXCECOES:
        return EXCECOES[nome]
    if oficial is None:
        return "marca" if loja_da_marca(nome) else "?"
    if oficial and loja_da_marca(nome):
        return "marca"
    return "revenda" if oficial else "comum"


def _classe_da_chave(con, loja: str, memo: dict) -> str:
    """Classe da loja de um sinal do Breno ("id|nome"): o selo vem da tabela lojas (as que o ML mostrou como oficiais)."""
    if not loja:
        return "?"
    if loja not in memo:
        nome = _so_letras(loja.split("|")[-1])
        oficial = None
        if nome:
            r = con.execute("SELECT oficial FROM lojas WHERE nome_norm = ?", (nome,)).fetchone()
            oficial = bool(r[0]) if r else (None if loja_da_marca(nome) else False)
        memo[loja] = classe_da_loja(vendedor=nome, oficial=oficial)
    return memo[loja]


def loja_e_classe(con, card) -> tuple[str, str]:
    """(chave da loja "id|nome", classe) de um card, dict {"vendedor", "oficial", "seller_id"} ou chave de loja."""
    if card is None:
        return "", "?"
    if isinstance(card, str):
        return card, _classe_da_chave(con, card, {})
    if isinstance(card, dict):
        nome = str(card.get("vendedor") or "")
        partes = [str(card.get("seller_id") or ""), _so_letras(nome)]
        return "|".join(p for p in partes if p), classe_da_loja(card)
    return chaves_da_loja(card)[0], classe_da_loja(card)


# ── eventos do dia (com cache curto) ───────────────────────────────────────────────────────────────────────────
_cache: dict = {}
CACHE_S = 60
CACHE_MAX = 64


def limpar_cache() -> None:
    _cache.clear()


def _versao(con) -> tuple:
    return tuple(con.execute(f"SELECT max(id) FROM {t}").fetchone()[0] for t in ("cupom_sinais", "ofertas", "postagens"))


def _eventos_do_dia(con, codigo: str, dia0: float) -> dict:
    """Tudo do dia sobre o código (inclusive depois de `agora`: quem lê filtra pela hora)."""
    chave = (id(con), codigo, dia0)
    versao = _versao(con)
    item = _cache.get(chave)
    if item and item["con"] is con and item["versao"] == versao and time.monotonic() - item["em"] < CACHE_S:
        return item["dados"]
    padrao = f'%"{codigo}"%'
    memo: dict = {}
    ev = []
    for ts, vendedor, oficial in con.execute("SELECT ts, vendedor, loja_oficial FROM ofertas WHERE ts >= ? AND ts < ? "
                                             "AND cupons LIKE ?", (dia0, dia0 + DIA_S, padrao)):
        cl = classe_da_loja(vendedor=vendedor, oficial=bool(oficial)) if vendedor else "?"
        ev.append({"f": "g", "ts": ts, "cl": cl, "loja": _so_letras(vendedor)})
    for ts, chave_produto, dados in con.execute("SELECT ts, chave, dados FROM postagens WHERE ts >= ? AND ts < ? AND "
                                                "dados LIKE ?", (dia0, dia0 + DIA_S, padrao)):
        try:
            cupons = json.loads(dados).get("cupons") or []
        except (ValueError, TypeError, AttributeError):
            continue
        if codigo in cupons:
            ev.append({"f": "h", "ts": ts, "chave": str(chave_produto or "")})
    por_ref: dict = {}
    for ts, tipo, fonte, ref, loja, post in con.execute(
            "SELECT ts, tipo, fonte, ref, loja, post FROM cupom_sinais WHERE codigo = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts, id", (codigo, dia0, dia0 + DIA_S)):
        if fonte == "grupo":
            if tipo == "esgotou":
                ev.append({"f": "w", "ts": ts})
            continue
        if fonte != "breno":
            continue
        e = por_ref.setdefault(ref or f"{post}:{ts}", {"f": "b", "ts": ts, "tipos": set(), "loja": loja or "",
                                                        "post": post or "", "ref": ref or ""})
        e["tipos"].add(tipo)
        e["loja"] = e["loja"] or loja or ""
    for e in por_ref.values():
        e["v"] = ("pôs" if "posto" in e["tipos"] else "tirou" if e["tipos"] & {"esgotou", "fora"} else "manteve")
        e["cl"] = _classe_da_chave(con, e["loja"], memo)
        ev.append(e)
    ev.sort(key=lambda e: (e["ts"], e["f"]))
    r = con.execute("SELECT ultimo_ts, pct, valor FROM cupons WHERE codigo = ?", (codigo,)).fetchone()
    dados = {"eventos": ev, "ultimo_ts": (r[0] if r else 0) or 0, "regra": bool(r and (r[1] or r[2]))}
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[chave] = {"con": con, "versao": versao, "em": time.monotonic(), "dados": dados}
    return dados


# ── a ficha de um código num dia ─────────────────────────────────────────────────────────────────────────────
class Ficha:
    """Estado de UM cupom num dia, alimentado evento a evento (como seria ao vivo).

    30/10 (revisor): vale a última palavra do Breno PELA HORA. Um 'manteve' só assenta 5 min depois; se nesse meio
    tempo ele tirou o cupom de outro post, o 'manteve' (mais velho) não desfaz o fim nem passa à frente da retirada."""

    def __init__(self, codigo: str, rodada: set, regra: bool, vencido: bool, ultimo_ts: float):
        self.codigo = codigo
        self.rodada = rodada            # posts em que o pipeline mandou o cupom: o 'manteve' neles não confirma
        self.regra, self.vencido, self.ultimo_ts = regra, vencido, ultimo_ts
        self.agora = 0.0
        self.n_grupo: Counter = Counter()   # classe -> posts de grupo hoje
        self.ult_grupo = 0.0
        self.ult_grupo_cl: dict = {}
        self.lojas: list = []           # [(chave da loja, '+'|'-', ts)]: confirmada / o Breno tirou dela
        self.conf_cl: dict = {}         # classe -> última confirmação do Breno nela hoje
        self.pos_cl: dict = {}          # classe -> último 'pôs' do Breno nela (só ele abre classe sob concentração)
        self.confs: list = []           # (ts, chave, tipo): confirmações para o "quente" (pôs, manteve, GrupoFinal)
        self.ult_pos = 0.0
        self.fora_cl: dict = {}         # classe -> hora em que o Breno tirou dela valendo em outra
        self.tirou_cl: dict = {}        # classe -> última retirada do Breno nela (fim ou fora)
        self.fora_seq: list = []        # [(ts, classe ou exceção)]: 'fora da classe' seguidas desde a última confirmação
        self.seg_fim = 0
        self.ult_fim = 0.0
        self.esgotou = 0.0
        self.aviso_grupo = 0.0
        self.retiradas: list = []       # (ts, loja, classe, leitura, motivo)
        self.pend: list = []            # mantidos esperando os 5 min
        self.ult_no_post: dict = {}     # post -> ref do último sinal do Breno nele

    # ── escrita ──
    def ver(self, e: dict) -> None:
        self._assentar(e["ts"])
        self.agora = max(self.agora, e["ts"])
        if e["f"] == "g":
            self.ult_grupo = e["ts"]
            if e["cl"] in CLASSES:
                self.n_grupo[e["cl"]] += 1
                self.ult_grupo_cl[e["cl"]] = e["ts"]
        elif e["f"] == "h":
            self.confs.append((e["ts"], "h:" + e["chave"], "grupofinal"))
        elif e["f"] == "w":
            self.aviso_grupo = e["ts"]
        elif e["f"] == "b":
            if e["post"]:
                self.ult_no_post[e["post"]] = e["ref"]
            if e["v"] == "pôs":
                self._confirmar(e, "pôs")
            elif e["v"] == "manteve":
                if not (e["post"] and e["post"] in self.rodada):   # 30/10: o 'manteve' da rodada não confirma
                    self.pend.append(e)
            else:
                self.retirada(e)

    def _assentar(self, t: float) -> None:
        resto = []
        for e in self.pend:
            if t - e["ts"] >= ASSENTA_S:
                if not e["post"] or self.ult_no_post.get(e["post"]) == e["ref"]:
                    self._confirmar(e, "manteve")
            else:
                resto.append(e)
        self.pend = resto

    def _confirmar(self, e: dict, tipo: str) -> None:
        """Confirmação do Breno com a hora DELA (um 'manteve' chega aqui 5 min depois de feito)."""
        ts = e["ts"]
        if e["loja"]:
            self.lojas.append((e["loja"], "+", ts))   # a loja vale pela hora: um '-' mais novo continua valendo
        if ts < self.ult_fim:
            # 30/10 (revisor, FULL0910 em 09/10: manteve às 18:58, fim às 19:00, e às 19:03 a suspensão sumia): o
            # 'manteve' de ANTES de um fim só confirma a loja; não desfaz o fim, não abre classe nem esquenta o cupom.
            return
        if e["cl"] in CLASSES:
            self.conf_cl[e["cl"]] = max(self.conf_cl.get(e["cl"], 0), ts)
            if tipo == "pôs":
                self.pos_cl[e["cl"]] = max(self.pos_cl.get(e["cl"], 0), ts)
        self.confs.append((ts, e["ref"] or e["post"], tipo))
        if tipo == "pôs":
            self.ult_pos = max(self.ult_pos, ts)
        self.fora_seq = [x for x in self.fora_seq if x[0] > ts]   # só as retiradas DEPOIS dela seguem na sequência
        self.seg_fim, self.esgotou = 0, 0.0

    @staticmethod
    def _chave_da_classe(cl: str, loja: str) -> str:
        """A classe (marca/revenda/comum) ou, nas exceções, a própria loja ('mista:oud'); '' se não dá para saber."""
        if cl in CLASSES:
            return cl
        if cl in SO_A_PROPRIA_LOJA:
            return f"{cl}:{_partes_da_loja(loja)[1] or loja}"
        return ""

    def _provado_fora_da_marca(self) -> bool:
        """O cupom já vale em revenda ou comum (classe aberta): não é cupom só de loja da marca (o LOJAS)."""
        return any(self.classe_status(c)[0] == "aberta" for c in CLASSES if c != "marca")

    def _ler(self, e: dict) -> tuple[str, str, int]:
        """(leitura, motivo, quantos 'fim' conta) de uma retirada AGORA, sem gravar."""
        cl, loja = e.get("cl", "?"), e.get("loja", "")
        ts = e.get("ts") or self.agora
        if self.loja_status(loja) == "+":
            return "fim", "tirou de uma loja em que o cupom estava confirmado", 1
        if cl in CLASSES and self.classe_status(cl)[0] == "aberta":
            return "fim", f"tirou de uma loja {cl}, classe em que o cupom estava confirmado", 1
        if self.seg_fim >= 1:
            return "fim", "o cupom já tinha falhado onde valia e falhou de novo", 1
        if self.aviso_grupo and 0 <= ts - self.aviso_grupo <= AVISO_FIM_S:
            return "fim", f"um grupo avisou que o cupom esgotou às {_hora(self.aviso_grupo)}", 1
        abertas = [c for c in CLASSES if c != cl and self.classe_status(c)[0] == "aberta"]
        outras = [l for l, s, _ in self._lojas_atuais() if s == "+" and not mesma_vitrine(l, loja)]
        if (loja or cl != "?") and (abertas or outras):
            chave = self._chave_da_classe(cl, loja)
            antes = sorted({k for _, k in self.fora_seq if k})
            if FIM_VARIAS_CLASSES and chave and antes and chave not in antes and self._provado_fora_da_marca():
                return "fim", (f"tirou em {len(antes) + 1} classes diferentes seguidas ({', '.join(antes + [chave])}) "
                               "sem confirmação no meio: o cupom acabou"), 1 + len(self.fora_seq)
            onde = f"classe {'/'.join(abertas)}" if abertas else "outra loja"
            return "fora da classe", f"o cupom não estava provado aqui ({cl}) e vale em {onde}", 0
        return "fim", "o cupom não estava provado em lugar nenhum (como hoje)", 1

    def ler_retirada(self, e: dict) -> tuple[str, str]:
        """Como a ficha lê o Breno tirar o cupom de um post AGORA (sem gravar): ('fim'|'fora da classe', motivo)."""
        return self._ler(e)[:2]

    def retirada(self, e: dict) -> str:
        leitura, motivo, conta = self._ler(e)
        cl, loja = e.get("cl", "?"), e.get("loja", "")
        if loja:
            self.lojas.append((loja, "-", e["ts"]))
        if cl in CLASSES:
            self.tirou_cl[cl] = e["ts"]
        if leitura == "fora da classe":
            if cl in CLASSES and any(self.classe_status(c)[0] == "aberta" for c in CLASSES if c != cl):
                self.fora_cl[cl] = e["ts"]
            self.fora_seq.append((e["ts"], self._chave_da_classe(cl, loja)))
        else:
            self.fora_seq = []
            self.seg_fim += conta
            self.ult_fim = e["ts"]
            if self.seg_fim >= FIM_SEGUIDOS and not self.esgotou:
                self.esgotou = e["ts"]
        self.retiradas.append((e["ts"], loja, cl, leitura, motivo))
        return leitura

    # ── leitura ──
    def _lojas_atuais(self) -> list:
        """A última palavra do Breno em cada loja, pela hora: [(loja, '+'|'-', ts)]."""
        atual: list = []
        for loja, s, ts in sorted(self.lojas, key=lambda x: x[2]):   # sorted é estável: empate fica na ordem de chegada
            atual = [x for x in atual if not mesma_vitrine(x[0], loja)] + [(loja, s, ts)]
        return atual

    def loja_status(self, loja: str) -> str:
        """'+' (confirmada), '-' (o Breno tirou dela) ou '': a última palavra PELA HORA, não pela ordem de chegada."""
        if not loja:
            return ""
        ultimo = None
        for n, (l, s, ts) in enumerate(self.lojas):
            if mesma_vitrine(l, loja) and (ultimo is None or (ts, n) >= ultimo[0]):
                ultimo = ((ts, n), s)
        return ultimo[1] if ultimo else ""

    def concentrada(self) -> str:
        tot = sum(self.n_grupo[c] for c in CLASSES)
        if tot < CONC_POSTS:
            return ""
        for c in CLASSES:
            fatia = self.n_grupo[c] / tot
            if fatia >= CONC_FATIA and fatia >= CONC_LIFT * FATIA_NORMAL[c] and (c == "marca" or not CONC_SO_MARCA):
                return c
        return ""

    def _texto_conc(self, conc: str) -> str:
        tot = sum(self.n_grupo[c] for c in CLASSES)
        return f"{self.n_grupo[conc]} de {tot} posts de grupo são de loja {conc} ({self.n_grupo[conc] * 100 // tot}%)"

    def classe_status(self, cl: str) -> tuple[str, str]:
        """('aberta'|'fechada'|'concentrada'|'?', motivo). Só o Breno abre uma classe; vale a última palavra dele.

        Concentração dos grupos em 'marca' fecha as outras classes (CONC_FECHA; com False ficam 'concentrada', sem
        prova, até o Breno tirar de uma delas). 30/10 (revisor): sob concentração, só o 'pôs' do Breno abre a classe
        (como a válvula do LOJAS no ar, que só abre com 'posto'); um 'manteve' confirma só aquela loja."""
        if cl not in CLASSES:
            return "?", "loja sem classe"
        fora = self.fora_cl.get(cl, 0)
        conc = self.concentrada()
        if conc and conc != cl:
            pos = self.pos_cl.get(cl, 0)
            if pos and pos > fora:
                return "aberta", f"o Breno pôs em {cl} às {_hora(pos)}"
            if fora:
                return "fechada", f"o Breno tirou de {cl} às {_hora(fora)} valendo em outra classe"
            if CONC_FECHA:
                return "fechada", f"{self._texto_conc(conc)}: cupom de {conc}"
            tirou = {c: t for c, t in self.tirou_cl.items() if c != conc}
            if tirou:
                c, t = max(tirou.items(), key=lambda x: x[1])
                return "fechada", f"{self._texto_conc(conc)} e o Breno tirou de {c} às {_hora(t)}: cupom de {conc}"
            return "concentrada", f"{self._texto_conc(conc)}: pode ser cupom só de {conc}"
        conf = self.conf_cl.get(cl, 0)
        if conf and conf > fora:
            return "aberta", f"o Breno confirmou em {cl} às {_hora(conf)}"
        if fora:
            return "fechada", f"o Breno tirou de {cl} às {_hora(fora)} valendo em outra classe"
        return "?", f"nenhuma confirmação do Breno em {cl} hoje"

    def vale(self, loja: str, cl: str) -> tuple[str, str]:
        if self.esgotou:
            return "não", f"esgotou: tirado de 2 posts seguidos onde valia (às {_hora(self.esgotou)})"
        if self.seg_fim >= 1:
            return "não", f"suspenso: tirado onde valia às {_hora(self.ult_fim)}; volta quando o Breno confirmar"
        ls = self.loja_status(loja)
        if ls == "-":
            return "não", "o Breno tirou o cupom desta loja hoje"
        if ls == "+":
            return "sim", "loja confirmada"
        if cl in SO_A_PROPRIA_LOJA:
            return "sem prova", f"loja {cl}: só vale a confirmação da própria loja"
        if cl not in CLASSES:
            return "sem prova", "loja desconhecida"
        st, motivo = self.classe_status(cl)
        if st == "aberta":
            return "sim", "classe aberta"
        if st == "fechada":
            return "não", f"classe {cl} fechada: {motivo}"
        return "sem prova", motivo

    def fresco(self, agora: float) -> bool:
        g = max(self.ult_grupo, self.ultimo_ts if self.ultimo_ts <= agora else 0)
        return bool((g and agora - g <= FRESCOR_GRUPO_S) or (self.ult_pos and agora - self.ult_pos <= FRESCOR_A_MAO_S))

    def vivo(self, agora: float, cl: str = "?") -> tuple[bool, str]:
        if self.esgotou:
            return False, "esgotou"
        if self.seg_fim >= 1:
            return False, "suspenso (1 retirada onde valia)"
        if self.vencido:
            return False, "a data do código já passou"
        if not self.regra:
            return False, "regra do cupom desconhecida"
        desde = max(self.ult_fim, agora - EM_USO_S)
        confs = [c for c in self.confs if desde < c[0] <= agora]
        breno = {c[1] for c in confs if c[2] != "grupofinal"}
        grupofinal = {c[1] for c in confs if c[2] == "grupofinal"}
        if len(breno) >= EM_USO_N or len(grupofinal) >= EM_USO_N:
            ult = max(c[0] for c in confs)
            if agora - ult < QUENTE_S and self.fresco(agora):
                return True, f"quente: confirmado às {_hora(ult)}"
        if cl in CLASSES:
            c, g = self.conf_cl.get(cl, 0), self.ult_grupo_cl.get(cl, 0)
            if c and c > self.ult_fim and agora - c <= CLASSE_CONF_S and g and agora - g <= FRESCOR_GRUPO_S:
                return True, f"o Breno confirmou em {cl} às {_hora(c)} e um grupo usou em {cl} às {_hora(g)}"
        if len(breno) < EM_USO_N and len(grupofinal) < EM_USO_N:
            return False, "não está em uso (menos de 2 confirmações nas últimas 2h)"
        if not self.fresco(agora):
            return False, "sem frescor (nenhum grupo há 45 min e o Breno não pôs à mão há 40)"
        return False, "a última confirmação passou de 40 min"

    def resumo(self, agora: float) -> dict:
        st = {c: self.classe_status(c) for c in CLASSES}
        return {"abertas": {c: m for c, (s, m) in st.items() if s == "aberta"},
                "fechadas": {c: m for c, (s, m) in st.items() if s == "fechada"},
                "concentrada": self.concentrada(), "grupos": {c: self.n_grupo[c] for c in CLASSES},
                "lojas_confirmadas": [l for l, s, _ in self._lojas_atuais() if s == "+"],
                "lojas_fora": [l for l, s, _ in self._lojas_atuais() if s == "-"],
                "esgotou": _hora(self.esgotou) if self.esgotou else "", "suspenso": self.seg_fim >= 1 and not self.esgotou,
                "vivo": self.vivo(agora)[1], "aviso_de_grupo": _hora(self.aviso_grupo) if self.aviso_grupo else ""}


def _ficha(con, codigo: str, agora: float, sem_ref: str = "") -> Ficha:
    codigo = str(codigo or "").upper()
    dados = _eventos_do_dia(con, codigo, _inicio_do_dia(agora))
    f = Ficha(codigo, posts_da_rodada(codigo), dados["regra"], bool(cupons_vencidos([codigo], agora)),
              dados["ultimo_ts"])
    for e in dados["eventos"]:
        if e["ts"] > agora:
            break
        if sem_ref and e.get("ref") == sem_ref:
            continue
        f.ver(e)
    f._assentar(agora)
    f.agora = agora
    return f


def ficha_do_dia(con, codigo: str, agora: float | None = None) -> Ficha:
    """A ficha do código hoje, até `agora` (o banco é lido uma vez e guardado por pouco tempo)."""
    return _ficha(con, codigo, time.time() if agora is None else agora)


def vale_no_anuncio(con, codigo: str, card, agora: float | None = None) -> tuple[str, str]:
    """('sim'|'sem prova'|'não', motivo) para o cupom no anúncio deste card (ou loja)."""
    agora = time.time() if agora is None else agora
    loja, cl = loja_e_classe(con, card)
    return ficha_do_dia(con, codigo, agora).vale(loja, cl)


def vivo(con, codigo: str, card, agora: float | None = None) -> tuple[bool, str]:
    agora = time.time() if agora is None else agora
    return ficha_do_dia(con, codigo, agora).vivo(agora, loja_e_classe(con, card)[1])


def ler_retirada(con, codigo: str, loja, agora: float | None = None, ref: str = "") -> tuple[str, str]:
    """O Breno tirou o cupom de um post desta loja agora: ('fim'|'fora da classe', motivo). Lê a ficha de antes da
    retirada (sem o sinal `ref`, se ele já foi gravado)."""
    agora = time.time() if agora is None else agora
    chave, cl = loja_e_classe(con, loja)
    return _ficha(con, codigo, agora, sem_ref=ref).ler_retirada({"cl": cl, "loja": chave, "ts": agora})


# ── a rodada: faixa, sonda, disjuntor e ritmo ─────────────────────────────────────────────────────────────────
class Rodada:
    """O que a ficha faria ao longo de uma rodada de cupom (sondas e ritmo). Em memória; recarrega o dia do registro."""

    def __init__(self):
        self.sondas: dict = {}    # (codigo, dia0) -> [{"loja", "post", "ts"}]
        self.seq: dict = {}       # codigo -> {"inicio", "ultimo", "posts", "com"}
        self.carregado = 0.0

    def ligar(self, codigo: str, agora: float) -> None:
        self.seq[codigo] = {"inicio": agora, "ultimo": agora, "posts": [], "com": 0}

    def _seq(self, codigo: str, agora: float) -> dict:
        s = self.seq.get(codigo)
        if not s or agora - s["ultimo"] > RODADA_PAUSA_S or _inicio_do_dia(s["ultimo"]) != _inicio_do_dia(agora):
            self.ligar(codigo, agora)
            s = self.seq[codigo]
        return s

    def _resultado(self, con, codigo: str, post: str, agora: float, desde: float):
        r = con.execute("SELECT tipo, ts FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND post = ? AND ts >= ? AND ts <= ? "
                        "ORDER BY ts DESC, id DESC LIMIT 1", (codigo, post, desde, agora)).fetchone() if post else None
        return (r[1], r[0] in ("esgotou", "fora")) if r else None

    def disjuntor(self, con, codigo: str, agora: float) -> bool:
        res = sorted(r for r in (self._resultado(con, codigo, s["post"], agora, s["ts"])
                                 for s in self.sondas.get((codigo, _inicio_do_dia(agora)), [])) if r)
        seguidas = 0
        for _, tirada in res:
            seguidas = seguidas + 1 if tirada else 0
        return seguidas >= DISJUNTOR

    def sonda(self, con, codigo: str, loja: str, agora: float) -> tuple[bool, str]:
        feitas = self.sondas.get((codigo, _inicio_do_dia(agora)), [])
        # 30/10: aqui (é cota, não prova) o mesmo vendedor conta como a mesma loja, por id OU nome (esgotados.mesma_loja):
        # 1 sonda por vendedor. Com a vitrine pelo nome, o ARQUIBANCADA10 ganharia 1 sonda a mais (Benetton, 480265022,
        # depois da do Armani), e o Breno tirou.
        if loja and sum(1 for s in feitas if mesma_loja(s["loja"], loja) or s["loja"] == loja) >= SONDAS_POR_LOJA:
            return False, "já houve sonda nesta loja hoje"
        if self.disjuntor(con, codigo, agora):
            return False, f"disjuntor: {DISJUNTOR} sondas tiradas seguidas hoje"
        return True, "sonda (1ª nesta loja hoje)"

    def ritmo(self, con, codigo: str, agora: float) -> tuple[bool, str]:
        """(segura?, motivo): já foram 3 com o cupom nesta rodada e o Breno ainda não decidiu nenhum post dela."""
        s = self._seq(codigo, agora)
        if s["com"] < RITMO_MAX:
            return False, ""
        posts = sorted(set(s["posts"]))
        if posts:
            marcas = ",".join("?" * len(posts))
            r = con.execute(f"SELECT 1 FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' AND ts >= ? AND ts <= ? "
                            f"AND post IN ({marcas}) LIMIT 1", (codigo, s["inicio"], agora, *posts)).fetchone()
            if r:
                return False, ""
        return True, f"ritmo: {s['com']} envios com o cupom e o Breno ainda não decidiu nenhum"

    def anotar(self, codigo: str, agora: float, loja: str, post: str, decisao: str) -> None:
        s = self._seq(codigo, agora)
        s["ultimo"] = agora
        if post:
            s["posts"].append(post)
        if decisao in ("com", "sonda"):
            s["com"] += 1
        if decisao == "sonda":
            self.sondas.setdefault((codigo, _inicio_do_dia(agora)), []).append(
                {"loja": loja or post, "post": post, "ts": agora})

    def carregar(self, caminho: Path, agora: float) -> None:
        """Refaz o dia a partir do registro da sombra (o pipeline reiniciou no meio da rodada)."""
        self.carregado = _inicio_do_dia(agora)
        try:
            with open(caminho, "rb") as f:   # só o fim do arquivo: o registro não é rodado e cresce todo dia
                f.seek(max(0, f.seek(0, 2) - 4_000_000))
                linhas = f.read().decode("utf-8", errors="ignore").splitlines()[-5000:]
        except OSError:
            return
        for linha in linhas:
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            if r.get("tipo") == "rodada":
                for codigo in r.get("liga") or []:
                    if _inicio_do_dia(r["ts"]) == self.carregado:
                        self.ligar(codigo, r["ts"])
            if r.get("tipo") != "envio" or _inicio_do_dia(r.get("ts") or 0) != self.carregado:
                continue
            for codigo, d in (r.get("ficha") or {}).items():
                self.anotar(codigo, r["ts"], r.get("loja") or "", r.get("post") or "", d.get("decisao") or "")


def decidir(con, codigo: str, card, agora: float | None = None, rodada: Rodada | None = None,
            post: str = "", sondar: bool = True) -> dict:
    """O que a ficha faria com o cupom neste envio: decisao 'com' | 'sonda' | 'sem' | 'segura' (ritmo).
    `sondar=False`: 'sem prova' fica sem o cupom (código que ninguém pediu neste envio: a ficha não sonda com ele)."""
    agora = time.time() if agora is None else agora
    codigo = str(codigo or "").upper()
    loja, cl = loja_e_classe(con, card)
    f = ficha_do_dia(con, codigo, agora)
    vale, motivo = f.vale(loja, cl)
    esta_vivo, por_que = f.vivo(agora, cl)
    d = {"vale": vale, "motivo": motivo, "vivo": esta_vivo, "vivo_motivo": por_que, "classe": cl,
         "faixa": FAIXAS.get(motivo if vale == "sim" else vale) if vale != "não" else None,
         "sonda": False, "ritmo_segura": False}
    if not esta_vivo or vale == "não":
        d["decisao"] = "sem"
    elif vale == "sim":
        d["decisao"] = "com"
    elif rodada is None or not sondar:
        d["decisao"] = "sem"
    else:
        pode, d["sonda_motivo"] = rodada.sonda(con, codigo, loja or post, agora)
        d["sonda"] = pode
        d["decisao"] = "sonda" if pode else "sem"
    if d["decisao"] in ("com", "sonda") and rodada is not None:
        segura, m = rodada.ritmo(con, codigo, agora)
        if segura:
            d.update(decisao="segura", ritmo_segura=True, ritmo_motivo=m)
    return d


# ── modo sombra: só registra ─────────────────────────────────────────────────────────────────────────────────
_RODADA = Rodada()


def _gravar(linha: dict) -> None:
    REGISTRO_SOMBRA.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRO_SOMBRA, "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False, default=str) + "\n")


def aviso(onde: str, exc: BaseException) -> None:
    """Uma linha de aviso no registro; nunca levanta erro."""
    try:
        _gravar({"tipo": "aviso", "ts": int(time.time()), "onde": onde, "erro": f"{type(exc).__name__}: {exc}"[:300]})
    except Exception:  # noqa: BLE001 - a sombra nunca derruba nada
        pass


def _rodada_do_dia(agora: float) -> Rodada:
    if _RODADA.carregado != _inicio_do_dia(agora):
        _RODADA.__init__()
        _RODADA.carregar(REGISTRO_SOMBRA, agora)
    return _RODADA


def _post(link: str) -> str:
    m = re.search(r"meli\.la/[A-Za-z0-9]+", str(link or ""))
    return m.group(0) if m else ""


def sombra_do_envio(con, *, chave: str, produto: str, link: str, card, cupons_grupo, equipe, loja_da_marca_cupom: str,
                    atual: dict, queridinho: bool | None = None, agora: float | None = None) -> dict | None:
    """30/10 (Breno: ficha do cupom): o pipeline decidiu um envio; registra o que a ficha faria com cada cupom
    considerado. Devolve a linha gravada (ou None). Nunca levanta erro e não mexe em nada do envio."""
    try:
        if modo() != "sombra":
            return None
        agora = time.time() if agora is None else agora
        loja, cl = loja_e_classe(con, card)
        post = _post(link)
        no_texto = {str(c).upper() for c in (atual.get("cupons_no_texto") or [])}
        considerados = ({str(c).upper() for c in cupons_grupo or []} | {str(c).upper() for c in equipe or []}
                        | ({loja_da_marca_cupom.upper()} if loja_da_marca_cupom else set()) | no_texto)
        rodada = _rodada_do_dia(agora)
        ficha = {}
        for codigo in sorted(c for c in considerados if c):
            d = decidir(con, codigo, card, agora, rodada, post)
            d["atual"] = "com" if codigo in no_texto else "sem"
            d["origem"] = [o for o, s in (("grupo", cupons_grupo), ("equipe", equipe),
                                          ("loja da marca", [loja_da_marca_cupom])) if codigo in {str(x).upper() for x in s or []}]
            ficha[codigo] = d
        # 30/10 (revisor): também os códigos que o Breno confirmou hoje (ou que o GrupoFinal postou nas últimas 2h) e que
        # ninguém pediu neste envio. Só entram no registro se a ficha os poria ('com', ou 'segura' pelo ritmo), sem
        # sonda: é o "cupom novo sem ninguém pedir" do estudo, que o relatório conta como "a ficha poria e você não pôs".
        for codigo in sorted(codigos_do_dia(con, agora) - set(ficha)):
            d = decidir(con, codigo, card, agora, rodada, post, sondar=False)
            if d["decisao"] != "sem":
                ficha[codigo] = dict(d, atual="sem", origem=["ficha"])
        for codigo, d in ficha.items():
            rodada.anotar(codigo, agora, loja, post, d["decisao"])
        linha = {"tipo": "envio", "ts": int(agora), "chave": chave, "produto": str(produto or "")[:80], "link": link,
                 "post": post, "vendedor": _vendedor(card), "loja": loja, "classe": cl, "queridinho": queridinho,
                 "cupons": {"grupo": sorted({str(c).upper() for c in cupons_grupo or []}),
                            "equipe": sorted({str(c).upper() for c in equipe or []}),
                            "loja_da_marca": loja_da_marca_cupom or ""},
                 "atual": atual, "ficha": ficha}
        _gravar(linha)
        return linha
    except Exception as exc:  # noqa: BLE001 - a sombra nunca derruba nada
        aviso("envio", exc)
        return None


def codigos_do_dia(con, agora: float) -> set:
    """Códigos que o Breno pôs ou manteve hoje, ou que o GrupoFinal postou nas últimas 2h: os que a ficha pode achar vivos."""
    dia0 = _inicio_do_dia(agora)
    codigos = {str(r[0]).upper() for r in con.execute(
        "SELECT DISTINCT codigo FROM cupom_sinais WHERE fonte = 'breno' AND tipo IN ('posto', 'ativo') AND ts >= ? "
        "AND ts <= ?", (dia0, agora)) if r[0]}
    for (dados,) in con.execute("SELECT dados FROM postagens WHERE ts >= ? AND ts <= ? AND dados LIKE '%cupons%'",
                                (max(dia0, agora - EM_USO_S), agora)):
        try:
            codigos.update(str(c).upper() for c in json.loads(dados).get("cupons") or [] if c)
        except (ValueError, TypeError, AttributeError):
            continue
    return codigos


def sombra_do_pulo(con, *, chave: str, produto: str, card, tirados, sem_cupom: dict | None,
                   agora: float | None = None) -> dict | None:
    """30/10 (revisor): o pipeline PULOU a promo porque, sem o cupom que saiu do texto, ela não vale. Registra o que a
    ficha diria desses cupons nesta loja (sem sonda e sem mexer no ritmo: a promo não foi). Nunca levanta erro."""
    try:
        if modo() != "sombra":
            return None
        agora = time.time() if agora is None else agora
        loja, cl = loja_e_classe(con, card)
        ficha = {str(c).upper(): decidir(con, c, card, agora, None, "", sondar=False) for c in tirados or [] if c}
        linha = {"tipo": "pulo", "ts": int(agora), "chave": chave, "produto": str(produto or "")[:80],
                 "vendedor": _vendedor(card), "loja": loja, "classe": cl, "tirados": sorted(ficha),
                 "sem_cupom": sem_cupom, "ficha": ficha,
                 "ficha_mandaria": sorted(c for c, d in ficha.items() if d["decisao"] == "com")}
        _gravar(linha)
        return linha
    except Exception as exc:  # noqa: BLE001
        aviso("pulo", exc)
        return None


_ultimas_candidatas: dict = {}
CANDIDATAS_S = 10 * 60   # a mesma lista de candidatas só é gravada de novo depois de 10 min


def sombra_das_candidatas(con, candidatas: list, codigos, agora: float | None = None) -> dict | None:
    """30/10 (revisor): numa rodada de cupom, a faixa de cada candidata (1 loja confirmada, 2 classe aberta, 3 sem
    prova, None não vale ou não está vivo) para cada código, e a ordem em que a ficha as poria (melhor faixa;
    queridinhos primeiro dentro da faixa; depois a ordem de hoje). `candidatas`: [{"chave", "vendedor", "oficial",
    "queridinho", "prioridade"}] na ordem do pipeline. Só registra; nunca levanta erro."""
    try:
        if modo() != "sombra" or not candidatas or not codigos:
            return None
        agora = time.time() if agora is None else agora
        codigos = sorted({str(c).upper() for c in codigos if c})
        assinatura = (tuple(codigos), tuple(str(c.get("chave")) for c in candidatas))
        if (_ultimas_candidatas.get("assinatura") == assinatura
                and agora - _ultimas_candidatas.get("ts", 0) < CANDIDATAS_S):
            return None
        fichas = {c: ficha_do_dia(con, c, agora) for c in codigos}
        lista = []
        for n, c in enumerate(candidatas):
            loja, cl = loja_e_classe(con, {"vendedor": c.get("vendedor") or "", "oficial": c.get("oficial")})
            faixas, vales = {}, {}
            for codigo, f in fichas.items():
                vale, motivo = f.vale(loja, cl)
                vivo_ = f.vivo(agora, cl)[0]
                faixas[codigo] = FAIXAS.get(motivo if vale == "sim" else vale) if vale != "não" and vivo_ else None
                vales[codigo] = vale
            melhor = min((x for x in faixas.values() if x), default=None)
            lista.append({"n": n, "chave": c.get("chave"), "vendedor": c.get("vendedor") or "", "classe": cl,
                          "queridinho": bool(c.get("queridinho")), "prioridade": bool(c.get("prioridade")),
                          "faixa": faixas, "vale": vales, "melhor_faixa": melhor})
        ordem = sorted(lista, key=lambda x: (x["melhor_faixa"] or 9, not x["queridinho"], x["n"]))
        linha = {"tipo": "candidatas", "ts": int(agora), "codigos": codigos, "candidatas": lista,
                 "ordem_da_ficha": [x["n"] for x in ordem]}
        _ultimas_candidatas.update(assinatura=assinatura, ts=agora)
        _gravar(linha)
        return linha
    except Exception as exc:  # noqa: BLE001
        aviso("candidatas", exc)
        return None


def _vendedor(card) -> str:
    if isinstance(card, dict):
        return str(card.get("vendedor") or "")
    return str(getattr(card, "vendedor", "") or "") if card is not None else ""


def sombra_da_rodada(con, liga, desliga, agora: float | None = None) -> None:
    """A rodada de cupom ligou ou desligou: anota a ficha de cada código nesse momento."""
    try:
        if modo() != "sombra":
            return
        agora = time.time() if agora is None else agora
        rodada = _rodada_do_dia(agora)
        for codigo in liga or []:
            rodada.ligar(str(codigo).upper(), agora)
        fichas = {str(c).upper(): ficha_do_dia(con, c, agora).resumo(agora) for c in set(liga or []) | set(desliga or [])}
        _gravar({"tipo": "rodada", "ts": int(agora), "liga": sorted(str(c).upper() for c in liga or []),
                 "desliga": sorted(str(c).upper() for c in desliga or []), "ficha": fichas})
    except Exception as exc:  # noqa: BLE001
        aviso("rodada", exc)


def sombra_da_retirada(con, codigo: str, ts: float, loja: str, post: str, ref: str, gravado: str) -> None:
    """O Breno tirou o cupom de um post (esgotados.tirou_do_post): como a ficha leria x o que foi gravado."""
    try:
        if modo() != "sombra":
            return
        codigo = str(codigo or "").upper()
        leitura, motivo = ler_retirada(con, codigo, loja, ts, ref)
        depois = _ficha(con, codigo, ts)
        _gravar({"tipo": "retirada", "ts": int(ts), "codigo": codigo, "loja": loja, "post": post, "ref": ref,
                 "classe": loja_e_classe(con, loja)[1], "gravado": gravado, "ficha": leitura, "motivo": motivo,
                 "diferente": (gravado == "esgotou") != (leitura == "fim"),
                 "ficha_depois": "esgotou" if depois.esgotou else "suspenso" if depois.seg_fim else "segue"})
    except Exception as exc:  # noqa: BLE001
        aviso("retirada", exc)
