"""Ofertas conhecidas de cada perfume e a escolha da melhor (04/11, fase 1 do PLANO_MELHOR_OFERTA_2026-11-04.md).

Só calcula: nada aqui muda escolha, nota, envio ou painel. A troca automática depende de nova aprovação do Breno.

Com os ajustes aprovados em 04/11:
- Confiança em três categorias: loja oficial da marca, revenda com selo, vendedor sem selo. A escolha nunca troca a
  oferta por uma de categoria menor. A régua de 2% e R$2 só vale entre lojas da mesma categoria; para subir de
  categoria, basta o preço igual ou menor (o "loja oficial vence revenda" do Breno, 31/10).
- Preço confirmado x hipótese. Confirmado é o que a página (ou a API) mostrou num horário conhecido, sem cupom, ou com
  o cupom que a própria página do anúncio mostra. Cupom visto num post deste mesmo anúncio, com a regra conferindo, é
  "visto em post"; cupom só anunciado é "hipótese". Nenhum dos dois vence preço confirmado: aparecem como possíveis.
- Pix com Pix (o preço em destaque da página), API com API. Sem base comum, a comparação fica à parte.
- Identidade em três níveis: mesmo perfume, em dúvida (fica à parte, nunca é escolhida) e outro produto (descartado).
- Prazos: só compara leitura de até 2 h; a escolhida com leitura de mais de 30 min fica "reconferir antes de enviar".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from . import travas
from .esgotados import _so_letras, loja_da_marca
from .identidade import Identificador, sem_acento

FRESCOR_COMPARAR_S = 2 * 3600   # leitura mais velha que isso não é escolhida (aparece como "a reconferir")
FRESCOR_ENVIO_S = 30 * 60       # a escolhida com leitura mais velha que isso precisa ser reconferida antes de enviar
PCT_MIN, REAIS_MIN = 0.02, 2.0  # a mesma régua do comparador (comparador.prioridade), só entre a mesma categoria
OFICIAL_API_MIN_VENDAS = 5000   # o mesmo corte de radar.OFICIAL_API_MIN_VENDAS: selo só da API exige 5 mil vendas
REGRA_CONFERE = 0.03            # o preço do post bate com a regra do cupom (a mesma folga de score._preco_efetivo)

OFICIAL_DA_MARCA, REVENDA_COM_SELO, SEM_SELO = "loja oficial da marca", "revenda com selo", "vendedor sem selo"
CONFIANCA = {SEM_SELO: 1, REVENDA_COM_SELO: 2, OFICIAL_DA_MARCA: 3}
SELO_SO_API = "selo só pela API, sem registro das vendas nesse horário"
SEM_REGISTRO_DO_SELO = "sem registro do selo nesse horário"
SEM_REGISTRO_DA_REPUTACAO = "vendedor sem selo e sem registro das vendas nesse horário (o pipeline não mandaria)"
SEM_REGISTRO = (SELO_SO_API, SEM_REGISTRO_DO_SELO, SEM_REGISTRO_DA_REPUTACAO)
VELHA = "leitura velha, mais de 2 h antes: reconferir"

MESMO, DUVIDA, OUTRO = "mesmo perfume", "identidade em dúvida", "outro produto"

NO_ANUNCIO = "confirmado no anúncio"           # a página do anúncio mostra o preço com cupom
EM_POST = "visto em post deste anúncio"        # um post com o link deste anúncio trazia o cupom e a regra confere
HIPOTESE = "hipótese"                          # o cupom foi anunciado ou confirmado em outro anúncio
NAO_SE_APLICA = "não se aplica"                # esgotado, suspenso, indício, vencido ou fora da condição

ORIGENS_DE_POST = {"ao vivo", "reconferida", "post da equipe", "colada"}   # o texto avaliado é o do post do anúncio
FONTES = {"ao vivo": "grupo", "reconferida": "reconferência", "vigia do ML": "vigia", "radar do ML": "radar",
          "post da equipe": "post da equipe", "colada": "colada no painel"}
DECANT = re.compile(r"\b(amostra|amostras|decant|decants|miniatura|travel|tester|refil|refill)\b")
ID_PAGINA = re.compile(r"/(?:p|up)/(MLBU?\d+)")
ID_ITEM = re.compile(r"\b(MLBU?)-?(\d{6,})")

# Situação de cada anúncio examinado e da decisão
CANDIDATA, DESCARTADA, A_PARTE = "candidata", "descartada", "à parte"
TROCA_CONFIRMADA = "troca confirmada"          # alternativa melhor na mesma base de preço (página com página)
TROCA_NA_API = "melhor na API"                 # melhor só na API: a página ainda precisa confirmar
MANTEM = "mantém"                              # havia outras; nenhuma melhor pelas regras
SEM_ALTERNATIVA = "sem alternativa"            # nenhum outro anúncio do perfume conhecido no horário
DEPENDE_DO_CUPOM = "depende do cupom"          # a diferença só existe com cupom que não é confirmado
INCONCLUSIVA = "inconclusiva"                  # falta registro do momento (preço, selo ou identidade da referência)


def brl(v) -> str:
    if v is None:
        return "R$ ?"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hora(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def dia_hora(ts) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "?"


@dataclass
class Cupom:
    codigo: str
    nivel: str
    preco: float | None = None      # o preço com o cupom, na base do preço da observação
    evidencia: str = ""


@dataclass
class Observacao:
    """Um anúncio visto num horário, por uma fonte, com o preço e quem vende naquele momento."""
    anuncio: str                    # catálogo|vendedor, MLBU… (página /up/) ou o item
    ts: float
    fonte: str                      # grupo, vigia, reconferência, radar, post da equipe, API do catálogo, página...
    base: str                       # "pagina" (Pix em destaque), "api" (sem Pix) ou "vitrine" (sem conferência)
    chave: str = ""
    titulo: str = ""
    vendedor: str = ""
    seller_id: int | None = None
    catalogo: str = ""              # produto de catálogo do ML ("" = página /up/ ou desconhecido)
    oficial: bool | None = None     # selo de loja oficial; None = sem registro
    selo_de: str = "pagina"         # "pagina" ou "api" (selo só da API exige 5 mil vendas)
    reputacao: dict | None = None
    internacional: bool = False
    full: bool = False
    risco: str = ""
    produtos_vendedor: int | None = None
    conflito: str = ""
    preco: float | None = None      # página: o em destaque (Pix, quando há); API: o da API
    preco_outros: float | None = None
    cupons: list[Cupom] = field(default_factory=list)
    link: str = ""
    ref: str = ""
    nota: str = ""
    score: int | None = None

    def onde(self) -> str:
        if self.catalogo:
            return f"catálogo {self.catalogo}"
        if self.anuncio.startswith("MLBU"):
            return "página /up/ sem catálogo"
        return f"anúncio {self.anuncio}" if self.anuncio else "anúncio sem número"


# ── identidade do anúncio ────────────────────────────────────────

def catalogo_da_url(url: str) -> str:
    m = re.search(r"/p/(MLB\d+)", url or "")
    return m.group(1) if m else ""


def anuncio_de(url: str = "", vendedor: str = "", item_id: str = "", catalogo: str = "") -> str:
    """A chave do anúncio: catálogo|vendedor (a página de catálogo mostra uma loja por vez), MLBU… (página /up/, de
    uma loja só) ou o número do item. Item de catálogo com o vendedor conhecido vira catálogo|vendedor, para juntar a
    leitura da API com a da página."""
    catalogo = catalogo or catalogo_da_url(url)
    nome = _so_letras(vendedor)
    if catalogo and nome:
        return f"{catalogo}|{nome}"
    m = re.search(r"/up/(MLBU\d+)", url or "")
    if m:
        return m.group(1)
    if item_id:
        return str(item_id).replace("-", "")
    m = ID_ITEM.search(url or "")
    if m:
        return m.group(1) + m.group(2)
    return f"{catalogo}|?" if catalogo else (url or "")


# ── confiança ────────────────────────────────────────────────────

def categoria(o: Observacao) -> tuple[str, str]:
    """(categoria, motivo para a loja não servir). Motivo vazio = loja aceita pelos critérios de hoje.

    A loja da marca é a de esgotados.loja_da_marca (o nome da loja é o de uma marca do config). O selo que só a API
    mostra vale com 5 mil vendas (radar.oficial_so_na_api); abaixo disso a loja é julgada como sem selo."""
    if o.oficial:
        cat = OFICIAL_DA_MARCA if loja_da_marca(o.vendedor) else REVENDA_COM_SELO
        if o.selo_de == "api":
            vendas = (o.reputacao or {}).get("vendas")
            if vendas is None:
                return cat, SELO_SO_API
            if vendas < OFICIAL_API_MIN_VENDAS:
                return SEM_SELO, travas.vendedor_fraco(False, o.reputacao)
        return cat, ""
    if o.oficial is None:
        return SEM_SELO, SEM_REGISTRO_DO_SELO
    if not o.reputacao or o.reputacao.get("vendas") is None:
        return SEM_SELO, SEM_REGISTRO_DA_REPUTACAO
    return SEM_SELO, travas.vendedor_fraco(False, o.reputacao)


def inadequada(o: Observacao) -> str:
    """Motivo para tirar a loja antes de olhar o preço (as travas do pipeline), ou ""."""
    if o.internacional:
        return "compra internacional"
    if o.conflito:
        return f"o link é de outro produto ({o.conflito})"
    if o.risco:
        return f"vendedor de risco ({o.risco})"
    if o.produtos_vendedor == 1 and not o.oficial:
        return "loja de 1 produto"
    return ""


def identidade(ref_titulo: str, ref_chave: str, o: Observacao, ident: Identificador) -> tuple[str, str]:
    """O anúncio é o mesmo perfume da referência? (nível, motivo)."""
    a, b = ident.analisar(ref_titulo or ""), ident.analisar(o.titulo or "")
    titulo = sem_acento((o.titulo or "").lower())
    if DECANT.search(titulo) and not DECANT.search(sem_acento((ref_titulo or "").lower())):
        return OUTRO, "decant ou amostra"
    if a.kit != b.kit:
        return OUTRO, "kit x avulso"
    if a.body_splash != b.body_splash:
        return OUTRO, "body splash x perfume"
    marca = Identificador.outra_marca(a, b)
    if marca:
        return OUTRO, f"outra marca ({marca})"
    if a.marca and b.marca and a.marca != b.marca:
        return OUTRO, f"outra marca ({b.marca} x {a.marca})"
    conflito = Identificador.conflito(a, b)
    if conflito:
        return OUTRO, conflito
    concentracao = Identificador.concentracao_diferente(a, b)
    if concentracao:
        return DUVIDA, f"concentração diferente ({concentracao})"
    if not b.tokens:
        return DUVIDA, "o título do anúncio não diz o perfume"
    if o.chave and ref_chave and o.chave != ref_chave:
        return DUVIDA, f"outra chave da curadora ({o.chave})"
    return MESMO, ""


# ── cupom por anúncio ────────────────────────────────────────────

def cupons_da_avaliacao(dados: dict, origem: str) -> list[Cupom]:
    """O cupom de cada anúncio, pelo que a curadora registrou NAQUELE momento (a avaliação não é sobrescrita)."""
    ml = dados.get("ml") or {}
    saida: list[Cupom] = []
    if ml.get("preco_cupom"):
        saida.append(Cupom("cupom da página", NO_ANUNCIO, float(ml["preco_cupom"]),
                           "a página do anúncio mostra o preço com cupom"))
    mortos = dados.get("cupons_mortos") or {}
    quente = dados.get("cupom_quente") or {}
    no_texto = list(dados.get("cupons") or [])
    codigos = list(dict.fromkeys(no_texto + ([quente["codigo"]] if quente.get("codigo") else [])))
    pagina, grupo, efetivo = ml.get("preco"), dados.get("preco_grupo"), dados.get("preco")
    for codigo in codigos:
        if codigo in mortos:
            saida.append(Cupom(codigo, NAO_SE_APLICA, None, f"{codigo} {mortos[codigo]}"))
            continue
        preco = None
        if quente.get("codigo") == codigo and quente.get("preco"):
            preco = float(quente["preco"])
        elif codigo in no_texto and efetivo and pagina and float(efetivo) < float(pagina) - 0.005:
            preco = float(efetivo)   # a curadora calculou com a regra conhecida do cupom
        de_post = origem in ORIGENS_DE_POST and codigo in no_texto
        confere = bool(preco and grupo and abs(float(grupo) - preco) <= REGRA_CONFERE * preco)
        if de_post and confere:
            nivel, evidencia = EM_POST, f"o post deste anúncio trazia {codigo} a {brl(grupo)} e a regra confere"
        elif preco and quente.get("codigo") == codigo:
            nivel = HIPOTESE
            evidencia = f"{codigo} confirmado às {quente.get('hora') or '?'} por {quente.get('por') or '?'}, não neste anúncio"
        elif preco:
            nivel, evidencia = HIPOTESE, f"{codigo} pela regra conhecida, sem post deste anúncio que confirme"
        elif de_post and grupo and pagina:
            nivel = HIPOTESE
            evidencia = f"{codigo} no post, mas {brl(grupo)} do post não bate com {brl(pagina)} da página"
        else:
            nivel, evidencia = HIPOTESE, f"{codigo} sem regra conhecida"
        saida.append(Cupom(codigo, nivel, preco, evidencia))
    return saida


# ── observações a partir do que a curadora já guarda ─────────────

def observacao_da_avaliacao(av_id, ts, origem: str, chave: str, nota: str, score, dados: dict) -> Observacao | None:
    """A avaliação (de grupo, vigia, reconferência, radar ou post da equipe) como observação de página."""
    ml = dados.get("ml") or {}
    url = ml.get("url") or ""
    if not (ml.get("preco") and url):
        return None
    vendedor = ml.get("vendedor") or dados.get("loja") or ""
    oficial = ml.get("oficial")
    # o radar avalia pela API (preço sem Pix; o selo já vem com a regra das 5 mil vendas aplicada pelo radar)
    da_api = origem == "radar do ML"
    return Observacao(
        anuncio=anuncio_de(url, vendedor), ts=float(ts), fonte=FONTES.get(origem, origem),
        base="api" if da_api else "pagina", chave=chave or "", titulo=ml.get("titulo") or dados.get("produto") or "",
        vendedor=vendedor, catalogo=catalogo_da_url(url), oficial=None if oficial is None else bool(oficial),
        selo_de="api" if da_api else "pagina",
        reputacao=dados.get("reputacao"), internacional=bool(dados.get("internacional")), full=bool(ml.get("full")),
        risco=str(dados.get("risco_motivo") or dados.get("risco") or ""), produtos_vendedor=dados.get("produtos_vendedor"),
        conflito=str(dados.get("conflito") or ""), preco=float(ml["preco"]),
        preco_outros=float(ml["preco_outros"]) if ml.get("preco_outros") else None,
        cupons=cupons_da_avaliacao(dados, origem), link=dados.get("link") or url,
        ref=f"avaliação #{av_id} ({origem})", nota=nota or "", score=score)


def observacao_da_api(ts, chave: str, item_id: str, titulo: str, preco, vendedor: str, oficial, url: str = "",
                      seller_id=None, reputacao: dict | None = None, fonte: str = "radar (API)",
                      internacional: bool = False, full: bool = False, catalogo: str = "",
                      base: str = "api", ref: str = "") -> Observacao:
    """Uma oferta lida na API do catálogo (radar, comparador): preço sem Pix, selo da API."""
    catalogo = catalogo or catalogo_da_url(url)
    return Observacao(
        anuncio=anuncio_de(url, vendedor, item_id, catalogo), ts=float(ts), fonte=fonte, base=base, chave=chave or "",
        titulo=titulo or "", vendedor=vendedor or "", seller_id=seller_id, catalogo=catalogo,
        oficial=None if oficial is None else bool(oficial), selo_de="api", reputacao=reputacao,
        internacional=internacional, full=full, preco=float(preco) if preco else None, link=url,
        ref=ref or f"{fonte} {item_id}")


def observacao_do_preco_ml(ts, chave: str, item_id: str, titulo: str, preco, vendedor: str, oficial, url: str,
                           fonte: str, seller_id=None, reputacao: dict | None = None) -> Observacao:
    """Uma linha de precos_ml. "radar API" é leitura da API do catálogo; "recomendacao" e "destaque" são preços vistos
    nas vitrines das páginas do ML, sem conferir o anúncio: entram como "vitrine" (nunca escolhidos sem conferência).
    Atenção: precos_ml guarda uma linha por anúncio, dia e preço; o horário é o da primeira vez naquele dia."""
    if fonte == "radar API":
        return observacao_da_api(ts, chave, item_id, titulo, preco, vendedor, oficial, url, seller_id, reputacao,
                                 fonte="radar (API)", ref=f"precos_ml radar API {item_id}")
    o = observacao_da_api(ts, chave, item_id, titulo, preco, vendedor, oficial, url, seller_id, reputacao,
                          fonte=f"vitrine do ML ({fonte})", base="vitrine", ref=f"precos_ml {fonte} {item_id}")
    o.selo_de = "pagina"
    if not vendedor:
        o.oficial = None   # a vitrine sem o nome da loja não diz se há selo
    return o


# ── a escolha ────────────────────────────────────────────────────

@dataclass
class Examinada:
    obs: Observacao
    categoria: str
    situacao: str = CANDIDATA
    motivo: str = ""
    identidade: str = MESMO
    preco_comparado: float | None = None    # o preço usado na comparação com a referência (mesma base)
    base_comparada: str = ""
    diferenca: float | None = None          # referência - esta (positivo = esta é mais barata)


@dataclass
class Decisao:
    momento: str                            # "post", "envio" ou "simulação"
    ts: float
    chave: str
    produto: str
    referencia: Examinada
    outras: list[Examinada] = field(default_factory=list)
    escolhida: Examinada | None = None
    status: str = MANTEM
    pronta: bool = True                     # False = a escolhida precisa ser reconferida antes de um envio
    possiveis_com_cupom: list[tuple[Observacao, Cupom]] = field(default_factory=list)
    lacunas: list[str] = field(default_factory=list)
    inconclusiva: str = ""
    publicado: float | None = None          # o preço que a equipe publicou (post) ou o enviado (com cupom, se houver)

    @property
    def troca(self) -> bool:
        return self.escolhida is not None and self.escolhida is not self.referencia

    @property
    def economia(self) -> float | None:
        """Quanto a escolhida fica abaixo da referência, os dois na mesma base (Pix da página ou API)."""
        if not self.troca or self.escolhida.diferenca is None:
            return None
        return self.escolhida.diferenca

    @property
    def economia_sobre_publicado(self) -> float | None:
        """Quanto a escolhida (preço confirmado) fica abaixo do preço publicado, que pode ter cupom."""
        if not self.troca or self.publicado is None or self.escolhida.base_comparada != "pagina":
            return None
        return round(self.publicado - self.escolhida.preco_comparado, 2)

    def frase(self) -> str:
        """"Para este perfume, examinei estas alternativas, descartei estas pelos seguintes motivos e escolheria
        esta oferta" (o formato pedido pela Astra em 04/11)."""
        todas = [self.referencia] + self.outras
        frescos = sum(1 for e in todas if e.motivo != VELHA)
        linhas = [f"Para {self.produto or self.chave} ({self.chave}), às {hora(self.ts)} de "
                  f"{time.strftime('%d/%m', time.localtime(self.ts))}, examinei {len(todas)} anúncio(s), "
                  f"{frescos} com leitura das 2 h antes:"]
        papel = {"post": " [o publicado]", "envio": " [o enviado]"}.get(self.momento, " [o do card]")
        linhas.append(f"- {_resumo(self.referencia)}{papel}")
        # as candidatas e a escolhida sempre; das outras, as mais baratas, até MOSTRAR linhas
        ordem = sorted(self.outras, key=lambda e: (e is not self.escolhida, e.situacao != CANDIDATA, e.motivo == VELHA,
                                                   e.situacao == DESCARTADA, e.obs.preco or 9e9))
        for e in ordem[:MOSTRAR]:
            linhas.append(f"- {_resumo(e)}")
        if len(ordem) > MOSTRAR:
            linhas.append(f"- e mais {len(ordem) - MOSTRAR} (listados no registro)")
        descartadas = [e for e in self.outras if e.situacao == DESCARTADA]
        if descartadas:
            linhas.append("Descartei: " + _agrupar(descartadas) + ".")
        a_parte = [e for e in self.outras if e.situacao == A_PARTE]
        if a_parte:
            linhas.append("Deixei à parte: " + _agrupar(a_parte) + ".")
        if self.inconclusiva:
            linhas.append(f"Inconclusiva: {self.inconclusiva}.")
        elif self.escolhida is not None:
            e = self.escolhida
            base = "Pix da página" if e.base_comparada == "pagina" else "API, sem Pix" if e.base_comparada == "api" \
                else e.base_comparada
            verbo = "Escolheria" if self.troca else "Manteria"
            extra = "" if self.pronta else "; leitura de mais de 30 min: reconferir antes de enviar"
            if self.status == TROCA_NA_API:
                extra += "; melhor só na API: a página precisa confirmar"
            economia = ""
            if self.economia:
                economia = f", {brl(self.economia)} abaixo da referência na mesma base"
            if self.economia_sobre_publicado is not None and self.economia_sobre_publicado > 0:
                economia += f", {brl(self.economia_sobre_publicado)} abaixo do publicado ({brl(self.publicado)})"
            linhas.append(f"{verbo} {_quem(e.obs)} a {brl(e.preco_comparado)} ({base}, conferido às "
                          f"{hora(e.obs.ts)}{economia}{extra}).")
        if self.possiveis_com_cupom:
            linhas.append("Possível com cupom (não conta como confirmado): " + "; ".join(
                f"{_quem(o)} {brl(c.preco)} com {c.codigo} ({c.nivel}: {c.evidencia})"
                for o, c in self.possiveis_com_cupom) + ".")
        if self.lacunas:
            linhas.append("Lacunas: " + "; ".join(self.lacunas) + ".")
        return "\n".join(linhas)


MOSTRAR = 6   # anúncios listados na frase além da referência (o registro guarda todos)


def _quem(o: Observacao) -> str:
    if o.vendedor:
        return o.vendedor
    return f"loja sem nome ({o.anuncio})" if o.anuncio else "anúncio sem loja"


def _agrupar(examinadas: list[Examinada], ate: int = 5) -> str:
    """"Lipx/MLB69225417, SEMIKE/MLB72936612 (leitura velha…)": o mesmo motivo junta as lojas."""
    grupos: dict[str, list[str]] = {}
    for e in examinadas:
        quem = e.obs.vendedor or "loja sem nome"
        onde = e.obs.catalogo or e.obs.anuncio.split("|")[0]
        grupos.setdefault(e.motivo, []).append(f"{quem}/{onde}" if onde else quem)
    partes = []
    for motivo, quem in grupos.items():
        nomes = ", ".join(quem[:ate]) + (f" e mais {len(quem) - ate}" if len(quem) > ate else "")
        partes.append(f"{nomes} ({motivo})")
    return "; ".join(partes)


def _resumo(e: Examinada) -> str:
    o = e.obs
    base = "página (Pix)" if o.base == "pagina" else "API" if o.base == "api" else o.base
    cupons = [c for c in o.cupons if c.nivel != NAO_SE_APLICA and c.preco]
    com = f", {brl(cupons[0].preco)} com {cupons[0].codigo} ({cupons[0].nivel})" if cupons else ""
    cat = e.categoria if o.oficial is not None else "selo sem registro"
    return f"{_quem(o)} ({cat}, {o.onde()}): {brl(o.preco)} na {base} às {dia_hora(o.ts)}{com} — {o.fonte}"


def _vale_trocar(ref: float, outro: float) -> bool:
    dif = ref - outro
    return dif >= REAIS_MIN and ref > 0 and dif / ref >= PCT_MIN


def ultimas_por_anuncio(observacoes: Iterable[Observacao], ate: float) -> dict[str, dict[str, Observacao]]:
    """Por anúncio, a última observação até `ate` em cada base (página, API, vitrine)."""
    saida: dict[str, dict[str, Observacao]] = {}
    for o in observacoes:
        if o.ts > ate or not o.anuncio:
            continue
        por_base = saida.setdefault(o.anuncio, {})
        atual = por_base.get(o.base)
        if atual is None or o.ts >= atual.ts:
            por_base[o.base] = o
    return saida


def _melhor_cupom_confirmado(o: Observacao) -> Cupom | None:
    return min((c for c in o.cupons if c.nivel == NO_ANUNCIO and c.preco), key=lambda c: c.preco, default=None)


def _cupom_provavel(o: Observacao) -> Cupom | None:
    return min((c for c in o.cupons if c.nivel in (NO_ANUNCIO, EM_POST) and c.preco), key=lambda c: c.preco,
               default=None)


def escolher(ref: Observacao, outras: Iterable[Observacao], agora: float, ident: Identificador, *,
             momento: str = "post", produto: str = "", ref_titulo: str = "", ref_api: Observacao | None = None,
             lacunas: Iterable[str] = (), publicado: float | None = None, frescor_s: float = FRESCOR_COMPARAR_S,
             envio_s: float = FRESCOR_ENVIO_S) -> Decisao:
    """A melhor oferta conhecida do perfume da referência (o anúncio publicado, o enviado ou o do card), com tudo o que
    foi examinado e o motivo de cada descarte. Só usa observações até `agora`."""
    cat_ref, motivo_ref = categoria(ref)
    exam_ref = Examinada(ref, cat_ref, preco_comparado=_preco_confirmado(ref), base_comparada=ref.base)
    d = Decisao(momento=momento, ts=agora, chave=ref.chave, produto=produto or ref.titulo, referencia=exam_ref,
                lacunas=list(lacunas), publicado=publicado)
    if ref.preco is None:
        d.inconclusiva = "o anúncio de referência não tem preço registrado nesse horário"
    elif motivo_ref in (SEM_REGISTRO_DO_SELO, SELO_SO_API):
        d.inconclusiva = f"a loja de referência: {motivo_ref}"
    titulo_ref = ref_titulo or ref.titulo
    ultimas = ultimas_por_anuncio(outras, agora)
    da_ref = ultimas.pop(ref.anuncio, {})
    if ref_api is None and ref.base == "pagina":
        ref_api = da_ref.get("api")   # a mesma loja lida na API: permite comparar API com API
    for anuncio, por_base in sorted(ultimas.items()):
        d.outras.append(_examinar(exam_ref, ref_api, por_base, agora, ident, titulo_ref, frescor_s, publicado))
    d.outras.sort(key=lambda e: (e.situacao != CANDIDATA, e.preco_comparado if e.preco_comparado is not None else 9e9))
    if d.inconclusiva:
        d.status = INCONCLUSIVA
        return d
    candidatas = [e for e in d.outras if e.situacao == CANDIDATA]
    # página com página primeiro; a troca só pela API aparece quando nenhuma página confirma
    candidatas = [e for e in candidatas if e.base_comparada == "pagina"] or candidatas
    if not d.outras:
        d.status, d.escolhida = SEM_ALTERNATIVA, exam_ref
    elif candidatas:
        menor = min(e.preco_comparado for e in candidatas)
        # dentro da régua do mais barato, vale a confiança maior; depois, a leitura mais recente
        empatadas = [e for e in candidatas if not _vale_trocar(e.preco_comparado, menor)]
        d.escolhida = max(empatadas, key=lambda e: (CONFIANCA[e.categoria], e.obs.ts))
        d.status = TROCA_CONFIRMADA if d.escolhida.base_comparada == "pagina" else TROCA_NA_API
    else:
        d.escolhida = exam_ref
        d.status = DEPENDE_DO_CUPOM if any(e.motivo.startswith("depende do cupom") for e in d.outras) else MANTEM
    d.pronta = agora - d.escolhida.obs.ts <= envio_s
    # cupom só anunciado ou visto em post: possível, nunca escolhido (mesma base de preço da escolhida)
    base_preco, base = d.escolhida.preco_comparado, d.escolhida.base_comparada or d.escolhida.obs.base
    # só anúncio do mesmo perfume, com preço comparável e confiança igual ou maior (o "mais cara" entra: com o cupom
    # ela poderia ficar abaixo; leitura velha e confiança menor não)
    aceitas = [exam_ref] + [e for e in d.outras if e.identidade == MESMO and e.preco_comparado is not None
                            and not e.motivo.startswith("confiança menor")]
    for e in aceitas:
        if e.obs.base != base:
            continue
        for c in e.obs.cupons:
            if c.nivel in (EM_POST, HIPOTESE) and c.preco and base_preco and _vale_trocar(base_preco, c.preco):
                d.possiveis_com_cupom.append((e.obs, c))
    return d


def _preco_confirmado(o: Observacao) -> float | None:
    """O preço confirmado do anúncio: o da página ou da API; com cupom só se a própria página mostra o cupom."""
    if o.preco is None:
        return None
    cupom = _melhor_cupom_confirmado(o)
    return min(o.preco, cupom.preco) if cupom else o.preco


def _examinar(ref: Examinada, ref_api: Observacao | None, por_base: dict[str, Observacao], agora: float,
              ident: Identificador, titulo_ref: str, frescor_s: float, publicado: float | None = None) -> Examinada:
    fresco = lambda x: x is not None and agora - x.ts <= frescor_s   # noqa: E731
    pagina, api, vitrine = por_base.get("pagina"), por_base.get("api"), por_base.get("vitrine")
    recente = max((x for x in (pagina, api, vitrine) if x is not None), key=lambda x: x.ts)
    o = pagina if fresco(pagina) else api if fresco(api) else recente
    cat, motivo_loja = categoria(o)
    e = Examinada(o, cat)
    nivel, motivo_id = identidade(titulo_ref, ref.obs.chave, o, ident)
    e.identidade = nivel
    if nivel == OUTRO:
        e.situacao, e.motivo = DESCARTADA, f"outro produto: {motivo_id}"
        return e
    ruim = inadequada(o)
    if ruim:
        e.situacao, e.motivo = DESCARTADA, ruim
        return e
    if not (fresco(pagina) or fresco(api) or fresco(vitrine)):
        e.situacao, e.motivo = A_PARTE, VELHA
        return e
    if motivo_loja and motivo_loja != SELO_SO_API:
        e.situacao = A_PARTE if motivo_loja in SEM_REGISTRO else DESCARTADA
        e.motivo = motivo_loja
        return e
    if nivel == DUVIDA:
        e.situacao, e.motivo = A_PARTE, f"identidade em dúvida: {motivo_id}"
        return e
    # a base de preço: Pix da página com Pix da página; API com API
    if ref.obs.base == "pagina" and fresco(pagina):
        e.obs, e.base_comparada, ref_preco = pagina, "pagina", ref.preco_comparado
    elif fresco(api) and fresco(ref_api):
        e.obs, e.base_comparada, ref_preco = api, "api", _preco_confirmado(ref_api)
    elif ref.obs.base == "api" and fresco(api):
        e.obs, e.base_comparada, ref_preco = api, "api", ref.preco_comparado
    else:
        e.situacao = A_PARTE
        if fresco(api):
            e.obs, e.motivo = api, "só lida na API: sem base comum com a página (conferir a página)"
        elif fresco(vitrine):
            e.obs, e.motivo = vitrine, "só vista na vitrine do ML, sem conferência"
        else:
            e.motivo = "sem base de preço comum com a referência"
        return e
    if motivo_loja:   # selo só da API sem registro das vendas: não sobe nem desce sem conferir a página
        e.situacao, e.motivo = A_PARTE, motivo_loja
        return e
    e.preco_comparado = _preco_confirmado(e.obs)
    if e.preco_comparado is None or ref_preco is None:
        e.situacao, e.motivo = A_PARTE, "sem preço comparável"
        return e
    e.diferenca = round(ref_preco - e.preco_comparado, 2)
    nivel_ref, nivel_e = CONFIANCA[ref.categoria], CONFIANCA[e.categoria]
    if nivel_e < nivel_ref:
        mais_barata = f", {brl(e.diferenca)} mais barata" if e.diferenca > 0 else ""
        e.situacao, e.motivo = A_PARTE, f"confiança menor ({e.categoria} x {ref.categoria}){mais_barata}"
        return e
    if nivel_e > nivel_ref:
        melhor = e.preco_comparado <= ref_preco + 0.005
    else:
        melhor = _vale_trocar(ref_preco, e.preco_comparado)
    if not melhor:
        e.situacao = DESCARTADA
        if e.diferenca > 0:
            e.motivo = f"só {brl(e.diferenca)} mais barata (abaixo de 2% e R$2, mesma categoria)"
        else:
            e.motivo = f"mais cara ({brl(-e.diferenca)} a mais)" if e.diferenca < 0 else "mesmo preço"
        return e
    # a referência com cupom provável (na página ou no post do próprio anúncio) abaixo desta: não é troca certa
    cupom_ref = _cupom_provavel(ref.obs)
    if cupom_ref is not None and cupom_ref.preco < e.preco_comparado - 0.005 and ref.obs.base == e.base_comparada:
        e.situacao = A_PARTE
        e.motivo = (f"depende do cupom: com {cupom_ref.codigo} ({cupom_ref.nivel}) a referência sai "
                    f"{brl(cupom_ref.preco)}, abaixo desta")
        return e
    # o preço publicado com cupom (mesmo sem confirmação) abaixo desta: trocar subiria o preço do post. Nem a hipótese
    # vence o confirmado, nem o confirmado vence a hipótese: fica à parte
    com_cupom = [c for c in ref.obs.cupons if c.nivel != NAO_SE_APLICA]
    if publicado is not None and com_cupom and publicado < e.preco_comparado - 0.005 and e.base_comparada == "pagina":
        e.situacao = A_PARTE
        e.motivo = (f"depende do cupom: o publicado ({brl(publicado)} com {com_cupom[0].codigo}, "
                    f"{com_cupom[0].nivel}) está abaixo desta")
        return e
    return e
