"""Score de curadoria: quão boa é esta promo, com as razões em português.

Fluxo de uma avaliação:
  texto -> Promo (parser) -> conferência no ML (opcional) -> identidade (catálogo)
        -> histórico de 60 dias -> componentes do score -> nota, razões e alertas
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import carregar_config
from dataclasses import dataclass, field

from .banco import (carregar_catalogo, chave_da_versao, lojas_oficiais_confirmadas, precos_altos,
                    produtos_do_vendedor, registrar_loja, registrar_preco_ml, registrar_produto_catalogo,
                    registrar_vendedores, versoes_por_chave)
from .cupons import Regra, cupons_vencidos, melhor_cupom, validade_do_codigo
from .esgotados import esgotados as cupons_esgotados
from .esgotados import CUPOM_FRESCO_S, QUENTE_S, chaves_da_loja, lojas_fora, mesma_loja
from .esgotados import confirmado_na_loja_da_marca, fresco, loja_da_marca, trava_da_marca
from .esgotados import quentes as cupons_quentes
from .historico import Historico, menor_preco_do_anuncio, referencia, tendencia, ultimo_preco_da_pagina
from .kits import info_kit
from .identidade import Identidade, Identificador, sem_acento
from .ml import CardML
from .parser import Promo, ler_promo, parece_perfume
from .pagina_cupons import cupons_do_dia, fora_da_pagina

NACIONAIS = ("natura", "boticario", "eudora", "jequiti", "avon", "granado", "kaiak", "malbec", "egeo", "quasar", "essencial")
ARABES = ("lattafa", "armaf", "al wataniah", "maison alhambra", "afnan", "rasasi", "french avenue", "rayhaan",
          "mawwal", "orientica", "al haramain", "khadlaj", "fragrance world", "paris corner", "swiss arabian", "ajmal")
ARABES_LINHAS = frozenset({"sabah", "asad", "fakhar", "yara", "kingdom", "bareeq", "wazeer", "khamrah", "hawas",
                           "turathi", "salvo", "nuit", "badee", "qaed", "ameer"})
GRIFES = ("armani", "azzaro", "ysl", "yves saint laurent", "dior", "versace", "rabanne", "jean paul gaultier",
          "chanel", "prada", "carolina herrera", "givenchy", "dolce gabbana", "calvin klein", "hugo boss",
          "montblanc", "bvlgari", "lacoste", "davidoff", "lancome", "mercedes benz")
PENALIDADE_CONFLITO = 40
MIN_PRODUTOS_VISTOS = 50  # só avisa "loja de 1 produto" depois de a curadora ter visto ofertas de 50+ produtos
VENDAS_CONFIAVEIS = 500  # vendedor sem selo com menos vendas que isso não é "confiável" para preço muito abaixo
VENDAS_LOJA_GRANDE = 50000  # 14/10: 30% ou mais abaixo do normal fora de loja oficial só não é risco acima disso
MEDIA_DE_QUEM_VENDE = 50   # 15/10: Natura, Boticário, body splash, Vodka: no preço normal (até 10% acima) são Média
CAMPANHA_S = 2 * 3600    # 14/10: cupom de campanha (data no código) visto pela 1ª vez há mais que isso é velho
JANELA_DE_DIAS = 90  # para julgar se o "de" anunciado é verdadeiro


@dataclass
class Avaliacao:
    promo: Promo
    identidade: Identidade
    categoria: str
    preco_efetivo: float
    historico: Historico
    card: CardML | None = None
    score: int = 0
    nota: str = ""
    confianca: str = ""
    razoes: list[str] = field(default_factory=list)
    alertas: list[str] = field(default_factory=list)
    top_vendido: bool = False
    loja: str = ""
    loja_confirmada: bool = False   # o ML mostrou quem vende (pode ser uma loja qualquer)
    loja_selo: bool = False         # loja oficial ou TOP confirmada no ML: só esta ganha o ✓ verde
    vendedor_ok: bool | None = None  # ML conferido: loja com selo ou reputação comprovada (None = não conferido)
    produtos_vendedor: int | None = None  # em quantos produtos de catálogo este vendedor foi visto
    loja_alegada_outra: bool = False  # o grupo cita uma loja, mas o link destaca outro vendedor
    conflito: str = ""
    componentes: dict[str, int] = field(default_factory=dict)
    pct_abaixo: float | None = None
    sem_historico: bool = False
    na_faixa: bool = False
    mesmo_preco_de_sempre: bool = False
    recorde: bool = False
    ts: int = 0
    pct_anunciado: float | None = None
    de_inflado: bool = False
    reputacao: dict | None = None
    risco: str = ""          # "", "medio" ou "alto"
    risco_motivo: str = ""
    tendencia: dict = field(default_factory=dict)
    kit_info: dict | None = None
    cupom_sugerido: dict | None = None
    cupons_mortos: dict = field(default_factory=dict)  # código -> por que não vale mais (vencido/esgotado)
    cupom_quente: dict | None = None  # cupom da promo confirmado pela equipe há pouco: {codigo, preco, hora, por}
    oferta_oficial: dict | None = None  # 31/10: a loja oficial do mesmo produto, igual ou mais barata que esta revenda
    grupo: str = ""

    @property
    def conferido_ml(self) -> bool:
        return bool(self.card and self.card.ok)

    def flags(self) -> dict:
        return {"pct_abaixo": self.pct_abaixo, "recorde": self.recorde, "sem_historico": self.sem_historico,
                "na_faixa": self.na_faixa, "mesmo_preco_de_sempre": self.mesmo_preco_de_sempre,
                "conflito": bool(self.conflito), "risco": self.risco, "loja_selo": self.loja_selo,
                "vendedor_ok": self.vendedor_ok, "internacional": bool(self.card and self.card.internacional)}


def vendedor_confiavel(rep: dict) -> bool:
    """Reputação comprovada no ML: medalha, ou verde com 500+ vendas e nome de loja."""
    if not rep or rep.get("conta_sem_nome"):
        return False
    return rep.get("classe") == "otima" or (rep.get("classe") == "boa" and (rep.get("vendas") or 0) >= VENDAS_CONFIAVEIS)


def _quem_vende(o, reputacoes) -> dict:
    """Uma oferta do catálogo em poucas linhas: anúncio, vendedor, loja oficial e, se já conhecida, a reputação."""
    info = {"item_id": o.item_id, "seller_id": o.seller_id, "preco": o.preco, "loja_oficial": bool(o.loja_oficial),
            "internacional": bool(getattr(o, "internacional", False))}
    rep = (reputacoes(o.seller_id) if reputacoes and o.seller_id else None) or {}
    if rep:
        info.update(nome=rep.get("nome", ""), vendas=rep.get("vendas"), medalha=rep.get("medalha", ""))
    return info


def resumo_do_catalogo(ofertas: list, oferta, reputacoes=None) -> dict:
    """Onde o anúncio do link fica entre os vendedores do mesmo produto no ML (ofertas em ordem de preço).

    "segundo" e "oficial" continuam sendo só os PREÇOS (texto_confianca e _risco_pelo_catalogo usam). 31/10 (auditoria:
    10 de 14 casos sem resposta sobre quem vendia a opção mais barata): "segundo_oferta" e "oficial_oferta" dizem o
    anúncio, o vendedor e se é loja oficial. `reputacoes(seller_id)` devolve a reputação só se já estiver guardada:
    nenhuma consulta a mais à API.

    01/11 (revisão): "oficial_oferta" é a oficial mais barata VÁLIDA (do Brasil e, com a reputação guardada, com 5 mil
    vendas ou mais); a mais barata podia ser de fora ou só da API e esconder outra oficial boa. "oficial" segue sendo o
    preço da oficial mais barata, qualquer uma."""
    from .radar import oficial_so_na_api   # import aqui: radar importa este módulo
    outros = [o for o in ofertas if o.seller_id != oferta.seller_id]
    oficiais = sorted((o for o in outros if o.loja_oficial), key=lambda o: o.preco)
    valida = next((info for info in (_quem_vende(o, reputacoes) for o in oficiais)
                   if not info["internacional"] and not oficial_so_na_api(info)), None)
    return {"vendedores": len({o.seller_id for o in ofertas}),
            "segundo": outros[0].preco if outros else None,
            "oficial": oficiais[0].preco if oficiais else None,
            "segundo_oferta": _quem_vende(outros[0], reputacoes) if outros else None,
            "oficial_oferta": valida}


def url_da_oferta(produto_id: str, item_id: str) -> str:
    """O anúncio dentro da página do produto, no formato que o ML abre (ou o link do item, sem o produto)."""
    if produto_id:
        return f"https://www.mercadolivre.com.br/p/{produto_id}?pdp_filters=item_id:{item_id}"
    numero = re.sub(r"\D", "", str(item_id or ""))
    return f"https://produto.mercadolivre.com.br/MLB-{numero}" if numero else ""


def preco_sem_pix(card) -> float | None:
    """O preço do card no mesmo critério da API dos outros vendedores (sem o desconto do Pix): o "ou R$ X em outros
    meios" quando o card mostra, senão o preço em destaque. 18/10: no Maleka o relatório dizia "14% abaixo do 2º
    vendedor" comparando o Pix da Luz Make Up (R$187,92) com o preço sem Pix da outra loja (R$217,90); igual com
    igual era 1% (R$216), e a loja do link trocava conforme quem abria. Valor menor que o destaque é parcela, não
    preço: fica o destaque."""
    if not card or not card.preco:
        return None
    if card.preco_outros and card.preco <= card.preco_outros <= card.preco * 1.35:
        return card.preco_outros
    return card.preco


def _milhares(n: int) -> str:
    if n >= 10000:
        return f"{n // 1000} mil"
    if n >= 1000:
        return f"{n / 1000:.1f} mil".replace(".", ",").replace(",0 mil", " mil")
    return str(n)


def texto_confianca(av: "Avaliacao") -> str:
    """Quem vende, em uma linha: "loja oficial Wathiq · Full" ou "KALITYUSABRF · Platinum · 52 mil vendas · Full"."""
    rep, card = av.reputacao or {}, av.card if av.card and av.card.ok else None
    partes = []
    if card and card.internacional:
        partes.append("🌎 internacional" + (f" ({rep['pais']})" if rep.get("pais") and rep["pais"] != "BR" else ""))
    if av.loja_selo:
        partes.append(f"loja oficial {av.loja}".strip())
    else:
        if rep.get("nome") and not rep.get("conta_sem_nome"):
            partes.append(str(rep["nome"])[:22])
        if rep.get("medalha") in ("platinum", "gold", "silver"):
            partes.append(str(rep["medalha"]).capitalize())
        if rep.get("vendas") is not None:
            partes.append(f"{_milhares(int(rep['vendas']))} vendas")
        if rep.get("classe") == "fraca":
            partes.append("reputação fraca")
        if rep.get("conta_sem_nome"):
            partes.append(f"conta sem nome de loja (desde {rep.get('conta_desde')})")
    if card and card.full:
        partes.append("Full")
    cat = (card.catalogo or {}) if card else {}
    base = preco_sem_pix(card)
    if cat.get("segundo") and base:
        pct = (cat["segundo"] - base) / cat["segundo"] * 100
        if pct >= 10:
            partes.append(f"{pct:.0f}% abaixo do 2º vendedor")
        elif pct <= -3:
            partes.append(f"outro vendedor tem por R${cat['segundo']:.0f}")
    return " · ".join(partes) or "vendedor não conferido"


def oferta_oficial_melhor(card) -> dict | None:
    """31/10 (Breno: "loja oficial vence revenda"). Malbec Bleu, 16:58: o link destacava uma revenda (PASSOSBARBARA,
    Platinum, 145 mil vendas) a R$180 e, no MESMO produto do ML, a loja oficial O Boticário vendia a R$167,93; foi a
    revenda. Se o anúncio do link não é de loja oficial e o catálogo tem loja oficial com preço igual ou menor, devolve
    essa oferta (o pipeline confere e manda ela). Igual com igual: o preço da API da oficial (sem o Pix) contra o preço
    sem o Pix do anúncio do link. Revenda mais barata que a oficial não troca (a oficial sairia mais cara).

    O selo aqui é o da API (official_store_id). oficial_so_na_api olha só "vendas": conhecidas e abaixo de 5 mil, não
    vale (21/10, PERFUMELATTAFA7). Sem a reputação guardada, "vendas" não vem e oficial_so_na_api diz que vale; por
    isso "vendas_conhecidas" vai junto: a oferta aparece no alerta, mas o pipeline só troca depois de abrir o anúncio e
    ver o selo na página E pelo menos 5 mil vendas na reputação (pipeline.pela_loja_oficial). A API sozinha não troca."""
    if not (card and card.ok) or card.loja_oficial or card.internacional:
        return None
    o = (card.catalogo or {}).get("oficial_oferta")
    base = preco_sem_pix(card)
    if not (o and o.get("item_id") and o.get("preco") and base) or o.get("internacional"):
        return None
    from .radar import oficial_so_na_api   # 21/10: "loja oficial" só na API, com menos de 5 mil vendas, não vale
    if oficial_so_na_api(o) or o["preco"] > base + 0.005:
        return None
    return dict(o, produto_id=card.produto_id, url=url_da_oferta(card.produto_id, o["item_id"]),
                vendas_conhecidas=o.get("vendas") is not None)


def norm_loja(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", sem_acento(str(s or "").lower()))


def categoria(ident: Identidade, produto: str) -> str:
    if ident.kit:
        return "kit"
    if ident.body_splash:
        return "body_splash"
    texto = sem_acento(produto.lower())
    alvo = f"{ident.marca} {texto}"
    if any(m in alvo for m in ARABES) or "arabe" in texto or ident.tokens & ARABES_LINHAS:
        return "arabe"
    if any(m in alvo for m in NACIONAIS):
        return "nacional"
    if any(m in alvo for m in GRIFES):
        return "grife"
    return "outro"


def casa_loja(nome: str, lojas: dict[str, str]) -> str:
    """'wathiq' casa com 'Wathiq Parfums'; 'O Boticário' com 'Boticário'."""
    nome = norm_loja(nome)
    if len(nome) < 3:
        return ""
    for chave, original in lojas.items():
        if nome == chave or (min(len(nome), len(chave)) >= 5 and (nome in chave or chave in nome)):
            return original
    return ""


class Curadora:
    def __init__(self, cfg: dict, con, consultor_ml=None, cliente_api=None):
        self.con = con
        self.ml = consultor_ml
        # API do ML (opcional): descobre quem vende cada anúncio e a reputação dele.
        self.api = cliente_api
        self._ofertas_cache: dict[str, tuple[float, list]] = {}
        self._versoes: tuple[float, dict, dict] = (float("-inf"), {}, {})
        self._aplicar_config(cfg)
        self.catalogo = carregar_catalogo(con)
        # True só na segunda leitura do que entra na normalizadora (conferente): ali quem decidiu postar com o
        # cupom foi uma pessoa, agora; a idade do cupom nos grupos não desmente o preço dela.
        self.sem_cupom_velho = False
        if consultor_ml is not None and hasattr(consultor_ml, "observador"):
            consultor_ml.observador = self.observar_preco_ml

    def _aplicar_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.pesos = cfg["pesos"]
        self.ident = Identificador(cfg["marcas"], cfg.get("sinonimos"))
        self.lojas_top = {norm_loja(x): x for x in cfg.get("lojas_top", [])}
        self.favoritos = [sem_acento(f.lower()) for f in cfg.get("favoritos_publico", [])]
        # 11/10 (Breno): "Natura e Boticário são produtos e marcas que vendem bem". Ver o fim de avaliar().
        vendem = [re.escape(sem_acento(m.lower())) for m in cfg.get("marcas_que_vendem", ["natura", "boticario"])]
        self.marcas_que_vendem = re.compile(r"\b(?:" + "|".join(vendem) + r")\b") if vendem else None
        arquivo = cfg.get("_arquivo")
        self._cfg_mtime = os.path.getmtime(arquivo) if arquivo and os.path.exists(arquivo) else None

    def recarregar_se_mudou(self) -> bool:
        """Se o config.json mudou (ex.: ajuste da calibração aplicado no painel), passa a usar o novo na hora."""
        arquivo = self.cfg.get("_arquivo")
        if not arquivo or self._cfg_mtime is None or not os.path.exists(arquivo):
            return False
        if os.path.getmtime(arquivo) == self._cfg_mtime:
            return False
        try:
            self._aplicar_config(carregar_config(Path(arquivo)))
        except (ValueError, KeyError):
            return False  # arquivo no meio de uma gravação ou quebrado: tenta na próxima
        return True

    def chave_de(self, nome: str) -> str:
        return self.catalogo.chave(self.ident.analisar(nome)).chave

    def _enriquecer_reputacao(self, card: CardML) -> None:
        """Para anúncios vistos pela página (grupos/vigia), pergunta à API quem vende e a reputação.

        O card traz o produto de catálogo e o anúncio; as ofertas do catálogo dizem o vendedor do anúncio.
        Sem acesso à API, ou se o anúncio não estiver entre as ofertas, segue sem reputação.
        """
        if not (self.api and card.ok and card.produto_id and card.item_id) or card.reputacao is not None:
            return
        try:
            if not self.api.configurado():
                return
            agora = time.time()
            guardado = self._ofertas_cache.get(card.produto_id)
            if not guardado or agora - guardado[0] > 15 * 60:
                guardado = (agora, self.api.ofertas(card.produto_id))
                self._ofertas_cache[card.produto_id] = guardado
                registrar_vendedores(self.con, card.produto_id, guardado[1], int(agora))
            oferta = next((o for o in guardado[1] if o.item_id == card.item_id), None)
            if oferta and oferta.seller_id:
                card.seller_id = oferta.seller_id
                card.reputacao = self.api.vendedor(oferta.seller_id) or None
            if oferta:
                card.catalogo = resumo_do_catalogo(guardado[1], oferta, getattr(self.api, "vendedor_guardado", None))
                pais = (card.reputacao or {}).get("pais") or "BR"
                card.internacional = bool(getattr(oferta, "internacional", False) or pais != "BR")
        except Exception:
            return  # reputação é um extra: nunca derruba a avaliação

    def observar_preco_ml(self, card: CardML, fonte: str, ts: int) -> None:
        """Todo preço visto no ML vira histórico real (sem cupom)."""
        if not card.ok or not card.preco or not card.titulo:
            return
        analise = self.ident.analisar(card.titulo)
        if not analise.base or not parece_perfume(card.titulo):
            return
        chave = self.catalogo.chave(analise).chave
        if abs(ts - self._versoes[0]) > 3600:   # quem é cada versão (gênero dos grupos, mediana): 1x por hora
            self._versoes = (ts, *versoes_por_chave(self.con, ts))
        _, dominante, medianas = self._versoes
        registrar_preco_ml(self.con, ts, chave_da_versao(chave, analise.genero, card.preco, dominante, medianas),
                           card, fonte)
        if card.produto_id:
            registrar_produto_catalogo(self.con, card.produto_id,
                                       chave_da_versao(chave, analise.genero, None, dominante, medianas),
                                       card.titulo, fonte, ts)
        if card.loja_oficial and card.vendedor:
            registrar_loja(self.con, norm_loja(card.vendedor), card.vendedor, True, ts)

    def _lojas_confiaveis(self) -> dict[str, str]:
        lojas = {norm_loja(x): x for x in self.cfg.get("lojas_oficiais_confiaveis", [])}
        lojas.update(lojas_oficiais_confirmadas(self.con))
        return lojas

    # ── preço ──────────────────────────────────────────────────
    def _cupons_mortos(self, promo: Promo, agora: float) -> dict[str, str]:
        """Cupons da promo que não valem mais: data do código já passou, suspenso (o Breno tirou na edição), esgotado
        (um grupo ou a equipe avisou) ou indício (sumiu da página oficial); ver esgotados.py."""
        if not promo.cupons:
            return {}
        mortos = {c: "vencido" for c in cupons_vencidos(promo.cupons, agora)}
        esgot = cupons_esgotados(self.con, agora)
        for c in promo.cupons:
            if c not in mortos and c in esgot:
                mortos[c] = esgot[c]["motivo"]   # 03/11: "suspenso: ..." ou "esgotado: ..."
        # 13/10: cupom de afiliado que saiu da página oficial do ML (acabou a verba ou a validade)
        mortos.update(fora_da_pagina(self.con, agora, [c for c in promo.cupons if c not in mortos]))
        if not self.sem_cupom_velho:
            mortos.update(self._cupons_velhos([c for c in promo.cupons if c not in mortos], agora))
        return mortos

    def _cupons_velhos(self, codigos: list[str], agora: float) -> dict[str, str]:
        """Cupom que nenhum grupo usa há mais de 1h e que a equipe não confirmou há pouco: não segura o preço.

        10/10 (Breno, olhando os Excelentes do painel): "esses cupons já estão velhos". O Asad Elixir estava
        Excelente a R$175 por um cupom visto pela última vez 18h antes (no ML, R$227). O pipeline já fazia essa
        conta ao escolher; o painel passou a fazer a mesma, para os dois dizerem a mesma coisa.
        """
        velhos = {}
        for c in codigos:
            # 29/10: cupom de loja da marca (LOJAS<data>) vale o dia todo na Natura/Boticário/Eudora; não é "velho"
            # enquanto você tiver confirmado ele hoje num post de loja da marca (e não tirou de um depois).
            if (trava_da_marca(self.con, c, agora) and confirmado_na_loja_da_marca(self.con, c, agora)
                    and fresco(self.con, c, agora)):   # 30/10: só enquanto algum grupo posta ou você pôs há pouco
                continue
            linha = self.con.execute("SELECT primeiro_ts, ultimo_ts FROM cupons WHERE codigo = ?", (c,)).fetchone()
            primeiro, visto = (linha[0], linha[1]) if linha else (None, None)
            # 14/10 (Breno: "pegou uns cupons velhos para mim"): os grupos repostam cupom de campanha horas depois de
            # ele acabar (o MODA1410 apareceu às 12:08 e ainda era repostado às 18:21). Medido em 14 dias, os de data no
            # código acabaram de 1 a 4 horas depois de aparecer (MODA1110 0,9h; LOJAS1010 1,2h; MODA1010 e FULL0910
            # 3,9h). Visto pela 1ª vez há mais de 2h é velho, mesmo que um grupo tenha usado agora.
            campanha = bool(primeiro) and validade_do_codigo(c, agora) is not None and agora - primeiro > CAMPANHA_S
            if not campanha and (not visto or agora - visto <= CUPOM_FRESCO_S):
                continue   # nunca registrado (não dá para saber) ou usado há pouco
            confirmado = self.con.execute("SELECT max(ts) FROM cupom_sinais WHERE codigo = ? AND fonte = 'breno' "
                                          "AND tipo IN ('ativo', 'posto') AND ts <= ?", (c, agora)).fetchone()[0] or 0
            if agora - confirmado <= QUENTE_S:
                continue   # você manteve ou pôs esse cupom num post nos últimos 40 min
            # 14/10 (Breno: "postaram o cupom lá [no GrupoFinal], logo faz sentido valer")
            if self.con.execute("SELECT 1 FROM postagens WHERE ts > ? AND ts <= ? AND dados LIKE ? LIMIT 1",
                                (agora - QUENTE_S, agora, f'%"{c}"%')).fetchone():
                continue
            # 01/11: o aviso de cupom da equipe no GrupoFinal nos últimos 40 min também confirma (é o que liga a rodada)
            if self.con.execute("SELECT 1 FROM cupom_sinais WHERE codigo = ? AND tipo = 'aviso' AND fonte = 'equipe' "
                                "AND ts > ? AND ts <= ? LIMIT 1", (c, agora - QUENTE_S, agora)).fetchone():
                continue
            if c in self._do_dia(agora):
                continue   # cupom do dia que a página oficial ainda mostra
            if campanha:
                velhos[c] = (f"velho: cupom de campanha visto pela 1ª vez às "
                             f"{time.strftime('%H:%M', time.localtime(primeiro))} (há {(agora - primeiro) / 3600:.0f}h); "
                             "esses costumam acabar em 1 a 4 horas")
                continue
            horas = (agora - visto) / 3600
            velhos[c] = f"velho: nenhum grupo usa há {'mais de 1h' if horas < 2 else f'{horas:.0f}h'}"
        return velhos

    def _cupom_quente(self, promo: Promo, card: CardML | None, mortos: dict, agora: float) -> dict | None:
        """Se a promo traz um cupom que a equipe está usando agora (esgotados.quentes) e a regra dele dá desconto
        sobre o preço do ML de agora, devolve {"codigo", "preco", "hora", "por"}."""
        if not (promo.cupons and card and card.ok and card.preco):
            return None
        return self._melhor_quente(float(card.preco), agora, [c for c in promo.cupons if c not in mortos], card)

    def cupom_da_equipe(self, preco_ml: float | None, agora: float | None = None,
                        card: CardML | None = None, so_da_equipe: bool = False) -> dict | None:
        """O cupom que o Breno está pondo à mão agora e que mais baixa este preço, mesmo que o post do grupo não
        traga o cupom. Só o pipeline usa: a promo já chega para ele com o cupom e o preço refeito. Com o `card`,
        fica de fora o cupom que ele tirou de um post dessa mesma loja. `so_da_equipe`: sem o cupom do dia (para
        decidir repetição e o que só vale com cupom, que continuam só com o cupom da equipe)."""
        if not preco_ml:
            return None
        return self._melhor_quente(float(preco_ml), agora or time.time(), None, card, do_dia=not so_da_equipe)

    def _do_dia(self, agora: float) -> dict[str, dict]:
        """O cupom do dia da página oficial (config cupom_do_dia; desligado se ausente)."""
        c = self.cfg.get("cupom_do_dia") or {}
        if not c.get("ligado"):
            return {}
        try:
            return cupons_do_dia(self.con, agora, c.get("ate") or "20:00")
        except Exception:  # noqa: BLE001 - sem a página, segue sem o cupom do dia
            return {}

    def cupons_fora_da_loja(self, card: CardML | None, agora: float | None = None) -> set[str]:
        """Cupons que não valem na loja deste anúncio: o Breno tirou de um post dela (esgotados.tirou_do_post)."""
        loja, _ = chaves_da_loja(card)
        if not loja:
            return set()
        return {c for c, lojas in lojas_fora(self.con, agora or time.time()).items()
                if any(mesma_loja(loja, outra) for outra in lojas)}

    @staticmethod
    def loja_da_marca_do_card(card: CardML | None) -> bool:
        """29/10: o anúncio é da loja oficial da própria marca (não revenda com selo)? Ver esgotados.loja_da_marca."""
        return bool(card and card.ok and card.loja_oficial and loja_da_marca(card.vendedor))

    def _melhor_quente(self, preco_ml: float, agora: float, so_estes: list[str] | None,
                       card: CardML | None = None, do_dia: bool = True) -> dict | None:
        # Chave geral em config cupom_quente.ligado (ausente = desligado). Quem decide o que está quente é
        # esgotados.quentes: o Breno pôs à mão em 2 posts ou mais e ainda não tirou (ou 2 posts no GrupoFinal com ele).
        if not (self.cfg.get("cupom_quente") or {}).get("ligado"):
            return None
        melhor = None
        fora = self.cupons_fora_da_loja(card, agora)
        valendo = dict(cupons_quentes(self.con, agora))
        da_marca = self.loja_da_marca_do_card(card)
        # 29/10 (Breno): cupom de loja da marca só entra em anúncio da loja da marca; nela, vale enquanto você tiver
        # confirmado hoje num post de loja da marca, mesmo depois dos 40 min do "quente".
        valendo = {c: q for c, q in valendo.items() if da_marca or not trava_da_marca(self.con, c, agora)}
        if da_marca:
            mortos_marca = cupons_esgotados(self.con, agora)
            for (codigo,) in self.con.execute("SELECT codigo FROM cupons WHERE codigo LIKE 'LOJAS%' AND ultimo_ts >= ?",
                                              (agora - 24 * 3600,)):
                confirmado = confirmado_na_loja_da_marca(self.con, codigo, agora)
                if (codigo not in valendo and confirmado and codigo not in mortos_marca
                        and not cupons_vencidos([codigo], agora) and trava_da_marca(self.con, codigo, agora)
                        and fresco(self.con, codigo, agora)):   # 30/10: frescor
                    valendo[codigo] = {"confirmado_em": confirmado, "por": "cupom da loja da marca"}
        if do_dia:
            # 14/10 (Breno: "sim pode ligar"): o cupom do dia dos afiliados, enquanto a página oficial mostra
            mortos = cupons_esgotados(self.con, agora)
            for codigo, d in self._do_dia(agora).items():
                if codigo not in valendo and codigo not in mortos:
                    valendo[codigo] = {"confirmado_em": d.get("desde") or agora, "por": "cupom do dia (página oficial do ML)"}
        for codigo, q in valendo.items():
            if codigo in fora or (so_estes is not None and codigo not in so_estes):
                continue
            preco = self._preco_com_cupom_conhecido([codigo], preco_ml)
            if preco is not None and preco < preco_ml and (melhor is None or preco < melhor["preco"]):
                melhor = {"codigo": codigo, "preco": preco, "por": q["por"],
                          "hora": time.strftime("%H:%M", time.localtime(q["confirmado_em"]))}
        return melhor

    def _preco_efetivo(self, promo: Promo, card: CardML | None, av_razoes: list, av_alertas: list,
                       agora: float | None = None, mortos: dict | None = None) -> float:
        agora = agora or time.time()
        if mortos is None:
            mortos = self._cupons_mortos(promo, agora)
        # Cupom com data no código ("MODA0610") vale só até esse dia: vencido, o preço com cupom não existe mais.
        # Cupom esgotado (09/10) idem: o preço com ele também não existe mais.
        vencidos = [c for c, m in mortos.items() if m == "vencido"]
        cupom_valido = bool(promo.cupons) and len(mortos) < len(promo.cupons)
        if vencidos:
            av_alertas.append(f"cupom vencido: {', '.join(vencidos)}" + ("" if cupom_valido else " (preço sem cupom)"))
        for c, m in mortos.items():
            if m != "vencido":
                av_alertas.append(f"cupom {c} {m}" + ("" if cupom_valido else " (preço sem cupom)"))
        if not (card and card.ok and card.preco):
            return promo.preco
        anunciado, ml = promo.preco, float(card.preco)
        if promo.cupons and not cupom_valido:
            return ml
        if promo.cupons:
            if ml > anunciado * 1.03:
                # 06/10 (Breno): "preços divergentes". O preço que vale é o que se vê ao abrir o link; o cupom só
                # entra se a regra dele é conhecida (vista nos grupos) e explica a diferença.
                validos = [c for c in promo.cupons if c not in mortos]
                calculado = self._preco_com_cupom_conhecido(validos, ml)
                if calculado is not None and calculado <= anunciado * 1.03:
                    # 31/10 (revisão): a NOTA fica com o número do grupo, que é o que o histórico com cupom guarda
                    # (R$112, e não R$112,62); o TEXTO publicado nunca fica abaixo da conta (pipeline.preco_do_texto).
                    av_razoes.append(f"no ML sem cupom: R${ml:.2f}; com cupom: R${anunciado:.2f} (regra do cupom confere)")
                    return anunciado
                if calculado is not None:
                    av_alertas.append(f"grupo diz R${anunciado:.2f} com cupom; pela regra do cupom sai R${calculado:.2f}")
                    return calculado
                av_alertas.append(f"grupo diz R${anunciado:.2f} com cupom {' ou '.join(validos)} (regra desconhecida): "
                                  f"valendo o preço do ML, R${ml:.2f}")
                return ml
            if ml < anunciado * 0.98:
                av_razoes.append(f"no ML está ainda mais barato: R${ml:.2f}")
            return min(anunciado, ml)
        if ml > anunciado * 1.03:
            av_alertas.append(f"preço SUBIU: grupo anunciou R${anunciado:.2f}, agora R${ml:.2f} no ML")
        elif ml < anunciado * 0.98:
            av_razoes.append(f"no ML está ainda mais barato: R${ml:.2f}")
        else:
            av_razoes.append("preço confirmado no ML")
        return ml

    def _preco_com_cupom_conhecido(self, codigos: list[str], ml: float) -> float | None:
        """Preço do ML com o melhor desses cupons, se a regra de algum foi vista nos grupos ("10% até R$20")."""
        if not codigos:
            return None
        marcas = ",".join("?" * len(codigos))
        precos = []
        for r in self.con.execute(f"SELECT pct, valor, minimo, limite FROM cupons WHERE codigo IN ({marcas}) "
                                  "AND (pct IS NOT NULL OR valor IS NOT NULL)", codigos):
            desconto = Regra(r[0], r[1], r[2], r[3]).desconto(ml)
            if desconto > 0:
                precos.append(round(ml - desconto, 2))
        return min(precos) if precos else None

    def _curva(self, pct: float) -> float:
        """Pontos de preço por % abaixo do normal, interpolando a curva do config."""
        pontos = self.pesos["curva_preco"]
        if pct <= pontos[0][0]:
            return pontos[0][1] + (pct - pontos[0][0]) * 1.0  # abaixo de −10%: continua caindo
        for (x0, y0), (x1, y1) in zip(pontos, pontos[1:]):
            if pct <= x1:
                return y0 + (y1 - y0) * (pct - x0) / (x1 - x0)
        return pontos[-1][1]

    def _preco(self, av: Avaliacao) -> int:
        """Preço é o principal. Sem desconto real (pct < mínimo), a promo não pode passar de NÃO VALE."""
        teto, h, preco = self.pesos["preco_max"], av.historico, av.preco_efetivo
        janela = self.cfg["janela_dias"]
        ref = {"com_cupom": "preços com cupom", "sem_cupom": "preços sem cupom"}.get(h.tipo, "todos os preços")
        if h.observacoes >= self.cfg["min_observacoes"] and h.mediana:
            pct = (h.mediana - preco) / h.mediana * 100
            if h.observacoes < 8 and pct > 0:
                pct *= self.pesos["fator_pouco_historico"]  # pouca amostra: desconto vale menos
            av.pct_abaixo = pct
            pts = self._curva(pct)
            if pct >= 0.5:
                av.razoes.append(f"{pct:.0f}% abaixo do normal (mediana R${h.mediana:.0f}, {ref}, {janela}d)")
            elif pct > -5:
                av.razoes.append(f"no preço normal (mediana R${h.mediana:.0f}, {ref})")
            else:
                av.alertas.append(f"{-pct:.0f}% ACIMA do normal (mediana R${h.mediana:.0f}, {ref})")
            if h.minimo and preco <= h.minimo and h.observacoes >= 5:
                folga = (1 - preco / h.minimo) * 100
                if folga >= 5:
                    av.recorde = True
                    pts += self.pesos["recorde_com_folga"]
                    av.razoes.append(f"NOVO RECORDE: {folga:.0f}% abaixo do menor preço de {janela} dias (R${h.minimo:.0f})")
                else:
                    pts += self.pesos["menor_preco_janela"]
                    av.razoes.append(f"menor preço em {janela} dias (antes R${h.minimo:.0f})")
                av.pct_abaixo = max(pct, self.pesos["pct_minimo_para_ser_promo"])
            return round(max(-15, min(teto, pts)))
        k = av.kit_info or {}
        if k.get("avulso_100ml"):
            # Kit quase nunca se repete igual (sem histórico próprio): compara o 100ml de perfume do kit com o
            # mesmo perfume avulso (pedido do Breno em 04/10: kit é o que o público gosta, tem que valer mais).
            pct = (k["avulso_100ml"] - k["preco_100ml"]) / k["avulso_100ml"] * 100
            av.pct_abaixo = pct
            av.razoes.append(f"kit: o 100ml de perfume sai R${k['preco_100ml']:.0f}, avulso R${k['avulso_100ml']:.0f} "
                             f"({abs(pct):.0f}% {'mais barato' if pct >= 0 else 'mais caro'})")
            return round(max(-15, min(teto, self._curva(pct))))
        # Sem histórico suficiente: faixa de preço da categoria, com peso menor e teto na nota.
        # Não há % de desconto real para mostrar: pct_abaixo fica vazio.
        faixa = self.cfg["faixas_categoria"].get(av.categoria) or self.cfg["faixas_categoria"]["outro"]
        av.alertas.append(f"pouco histórico ({h.observacoes} obs.): comparado com a faixa de {av.categoria}")
        av.sem_historico = True
        if h.observacoes and h.minimo and preco >= h.minimo * 0.95:
            av.mesmo_preco_de_sempre = True
            av.alertas.append(f"mesmo preço das {h.observacoes} vez(es) que apareceu (R${h.minimo:.0f})")
        if preco <= faixa["excelente"]:
            av.razoes.append(f"preço excelente para {av.categoria} (até R${faixa['excelente']})")
            av.na_faixa = True
            return round(teto * 0.6)
        if preco <= faixa["bom"]:
            av.razoes.append(f"preço bom para {av.categoria} (até R${faixa['bom']})")
            av.na_faixa = True
            return round(teto * 0.35)
        return round(teto * 0.1)

    # ── relevância para o público ──────────────────────────────
    def _relevancia(self, av: Avaliacao) -> int:
        ident = av.identidade
        for nome in self.cfg.get("top_vendidos", []):
            if self.ident.mesmo_produto_da_lista(ident, nome):
                av.top_vendido = True
                av.razoes.append(f"TOP vendido: {nome}")
                return self.pesos["top_vendido"]
        if "kit_body_splash" in self.cfg.get("top_categorias", []) and ident.kit and ident.body_splash:
            av.top_vendido = True
            av.razoes.append("TOP vendido: kit body splash")
            return self.pesos["top_vendido"]
        if ident.kit:
            av.razoes.append("kit: o público gosta")
            return self.pesos.get("kit", 12)
        texto = sem_acento(av.promo.produto.lower())
        fav = next((f for f in self.favoritos if re.search(r"\b" + re.escape(f) + r"\b", texto)), "")
        if fav:
            av.razoes.append(f"queridinho do público: {fav}")
            return self.pesos["favorito_publico"]
        return 0

    # ── reputação e risco de falsificado ───────────────────────
    def _pontos_reputacao(self, av: Avaliacao, rep: dict | None) -> int:
        """Vendedor sem selo de loja oficial: a reputação no ML pesa um pouco e acende alertas."""
        if not rep:
            return 0
        if rep.get("conta_sem_nome"):
            av.alertas.append(f"conta sem nome de loja (criada em {rep['conta_desde']})")
        classe = rep.get("classe")
        if classe == "otima":
            vendas = f", {rep['vendas']} vendas" if rep.get("vendas") else ""
            av.razoes.append(f"vendedor {rep['medalha'].capitalize()} no ML{vendas}")
            return self.pesos.get("vendedor_otimo", 4)
        if classe == "boa":
            av.razoes.append("vendedor com reputação verde no ML")
            return self.pesos.get("vendedor_bom", 1)
        if classe == "fraca":
            av.alertas.append("vendedor com reputação fraca ou poucas vendas no ML")
        return 0

    def _avaliar_risco(self, av: Avaliacao) -> None:
        """Perfume é muito falsificado: preço bom demais vindo de quem não tem reputação acende o risco.

        alto  -> nota no máximo NÃO VALE (reputação fraca e 25%+ abaixo do normal)
        médio -> nota no máximo OK (40%+ abaixo do normal sem vendedor confiável conhecido)
        Loja oficial ou TOP confirmada no ML nunca entra em risco.
        """
        if av.loja_selo:
            return
        if av.pct_abaixo is None:
            self._risco_pelo_catalogo(av)
            if av.risco:
                av.alertas.insert(0, av.risco_motivo)
            return
        pct, rep = av.pct_abaixo, av.reputacao or {}
        vendas = rep.get("vendas")
        # 06/10 (Breno): "lojas meio suspeitas, preço bem abaixo, poucas vendas". O ML pinta de verde até quem
        # tem 90 vendas; confiável de verdade = medalha, ou verde com 500+ vendas e nome de loja.
        poucas = vendas is not None and vendas < VENDAS_CONFIAVEIS
        confiavel = (rep.get("classe") == "otima" or (rep.get("classe") == "boa" and not poucas)) \
            and not rep.get("conta_sem_nome")
        if rep.get("classe") == "fraca" and pct >= 25:
            av.risco = "alto"
            av.risco_motivo = f"{pct:.0f}% abaixo do normal vindo de vendedor com reputação fraca: risco de falsificado"
        elif rep.get("classe") == "fraca" and pct >= 15:
            av.risco = "medio"
            av.risco_motivo = f"{pct:.0f}% abaixo do normal, vendedor com reputação fraca ou poucas vendas: confira"
        elif rep.get("conta_sem_nome") and pct >= 15:
            av.risco = "medio"
            av.risco_motivo = (f"{pct:.0f}% abaixo do normal, conta sem nome de loja (criada em "
                               f"{rep.get('conta_desde')}): confira se é original")
        elif poucas and pct >= 20:
            av.risco = "medio"
            av.risco_motivo = f"{pct:.0f}% abaixo do normal, vendedor com só {vendas} vendas: confira se é original"
        elif (not rep and av.card is not None and av.card.ok and pct >= 25
              and not ((av.promo.loja_oficial or av.promo.verificado) and not av.loja_alegada_outra)):
            # Abriu no ML, mas sem dados do vendedor (e o grupo nem diz que é loja oficial/verificada).
            av.risco = "medio"
            av.risco_motivo = f"{pct:.0f}% abaixo do normal e não deu para conferir o vendedor: confira antes de postar"
        elif rep and pct >= 30 and (vendas or 0) < VENDAS_LOJA_GRANDE:
            # 14/10 (Breno: "a promo do Asad não é confiável mesmo"; "muitas promos duvidosas"): no turno saíram Asad a
            # R$126 de vendedor verde com 593 vendas, Asad Elixir 33% abaixo de "CBCFGEHDA23648" (Platinum, 7 mil
            # vendas) e Silver Scent 40% abaixo de vendedor com 3,8 mil vendas, todos fora de loja oficial. Em 14 dias,
            # das promos 30%+ abaixo fora de loja oficial ele postou 1 de 14. A medalha não basta: só loja oficial
            # (que nem chega aqui) ou vendedor muito grande.
            av.risco = "medio"
            av.risco_motivo = (f"{pct:.0f}% abaixo do normal fora de loja oficial"
                               + (f" ({vendas} vendas)" if vendas is not None else "") + ": confira se é original")
        elif pct >= 40 and not confiavel:
            av.risco = "medio"
            av.risco_motivo = (f"bom demais para ser verdade: {pct:.0f}% abaixo do normal sem vendedor confiável "
                               "conhecido; confira se é original")
        else:
            self._risco_pelo_catalogo(av)
        if av.risco:
            av.alertas.insert(0, av.risco_motivo)

    def _risco_pelo_catalogo(self, av: Avaliacao) -> None:
        """09/10 (Breno: "a curadora acha preços ótimos, mas não sei se são confiáveis"): o ML mostra todos os
        vendedores do mesmo produto. Muito abaixo de TODOS os outros, sem selo nem reputação comprovada, é o
        padrão de falsificado; com vendedor confiável (Victorioso: Platinum, 52 mil vendas, Full) é achado."""
        card = av.card
        cat = (card.catalogo or {}) if card and card.ok else {}
        base = preco_sem_pix(card)
        if not (cat.get("segundo") and base):
            return
        pct = (cat["segundo"] - base) / cat["segundo"] * 100
        if pct < 10:
            return
        if av.loja_selo or vendedor_confiavel(av.reputacao or {}):
            av.razoes.append(f"{pct:.0f}% abaixo do 2º vendedor mais barato do ML (R${cat['segundo']:.0f}), "
                             "com vendedor confiável")
        elif pct >= 25:
            av.risco = "medio"
            av.risco_motivo = (f"{pct:.0f}% abaixo de todos os outros {cat['vendedores'] - 1} vendedores do ML "
                               f"(2º: R${cat['segundo']:.0f}) sem vendedor confiável: confira se é original")

    # ── loja ───────────────────────────────────────────────────
    def _loja(self, av: Avaliacao) -> int:
        promo, card, p = av.promo, av.card, self.pesos
        confiaveis = self._lojas_confiaveis()
        if card and card.ok and card.vendedor:
            # O ML mostrou o vendedor real: vale mais que o texto do grupo.
            av.loja, av.loja_confirmada = card.vendedor, True
            av.reputacao = card.reputacao
            top = casa_loja(card.vendedor, self.lojas_top)
            if top:
                av.loja, av.loja_selo, av.vendedor_ok = top, True, True
                av.razoes.append(f"loja TOP: {top} (confirmado no ML)")
                return p["loja_top"]
            if card.loja_oficial:
                av.loja_selo = av.vendedor_ok = True
                av.razoes.append(f"loja oficial confirmada no ML: {card.vendedor}")
                return p["loja_oficial_confiavel"]
            if promo.loja_oficial:
                av.alertas.append(f"grupo diz loja oficial, mas o ML mostra {card.vendedor} sem selo")
            av.vendedor_ok = vendedor_confiavel(card.reputacao or {})
            pts = 0
            conf = casa_loja(card.vendedor, confiaveis)
            if conf:
                av.razoes.append(f"loja conhecida: {conf} (sem selo neste anúncio)")
                pts = p["loja_verificada_alegada"]
            return pts + self._pontos_reputacao(av, card.reputacao)
        if card and card.ok:
            # O ML abriu o anúncio mas não mostra quem vende (a página só mostra o vendedor quando é loja oficial):
            # sem selo e sem reputação comprovada, não passa de Média (06/10, Breno).
            av.reputacao = card.reputacao
            av.vendedor_ok = vendedor_confiavel(card.reputacao or {})
            if (card.preco and promo.preco and abs(card.preco - promo.preco) > promo.preco * 0.03
                    and (promo.vendedor or promo.loja_oficial or promo.verificado)):
                # 09/10 (Victorioso): o grupo diz "selecione a loja WATHIQ" a R$119, mas o link destaca OUTRO vendedor
                # a R$78. A loja citada não é a do anúncio em destaque: não ganha os pontos nem o "loja oficial".
                av.alertas.append(f"o ML destaca outro vendedor (R${card.preco:.2f}); a loja citada pelo grupo "
                                  f"({promo.vendedor or 'loja oficial'}) é outra oferta")
                av.loja_alegada_outra = True
                return self._pontos_reputacao(av, card.reputacao)
        # Sem vendedor do ML: usa o que o grupo diz, com peso menor.
        fator = p.get("fator_nao_confirmado", 0.75)
        top = casa_loja(promo.vendedor, self.lojas_top)
        if top:
            av.loja = top
            av.razoes.append(f"loja TOP: {top} (dito pelo grupo)")
            return round(p["loja_top"] * fator)
        conf = casa_loja(promo.vendedor, confiaveis)
        if conf and (promo.loja_oficial or promo.verificado):
            av.loja = conf
            av.razoes.append(f"loja oficial confiável: {conf} (dito pelo grupo)")
            return round(p["loja_oficial_confiavel"] * fator)
        if promo.loja_oficial:
            av.loja = promo.vendedor or "loja oficial"
            av.razoes.append(f"diz ser loja oficial{': ' + promo.vendedor if promo.vendedor else ''} (não confirmado)")
            return p["loja_oficial_alegada"]
        if promo.verificado:
            av.loja = promo.vendedor or "verificada"
            av.razoes.append("loja verificada (dito pelo grupo)")
            return p["loja_verificada_alegada"]
        av.loja = promo.vendedor
        return 0

    # ── sinais extras do ML e desconto ─────────────────────────
    def _sinais_ml(self, av: Avaliacao) -> int:
        card, pts = av.card, 0
        if not (card and card.ok):
            return 0
        if card.mais_vendido:
            pts += self.pesos.get("mais_vendido", 3)
            av.razoes.append("selo MAIS VENDIDO no ML")
        if card.full:
            pts += self.pesos.get("full", 2)
            av.razoes.append("enviado pelo FULL")
        if card.preco_cupom and card.preco and card.preco_cupom < card.preco * 0.99:
            # Só informação (10/10): é o cupom do próprio anúncio, que o ML mostra no card; não é código de grupo.
            av.razoes.append(f"o anúncio mostra R${card.preco_cupom:.2f} com cupom (cupom do próprio anúncio no ML)")
        return pts

    def _desconto_anunciado(self, av: Avaliacao) -> int:
        de = (av.card.preco_anterior if av.card and av.card.ok and av.card.preco_anterior else None) or av.promo.preco_de
        if not de or de <= av.preco_efetivo:
            return 0
        pct = (1 - av.preco_efetivo / de) * 100
        av.pct_anunciado = round(pct, 1)
        # "De" falso: muito acima de qualquer preço visto em 90 dias. O histórico é quase todo de preços já em
        # promoção (os grupos não postam o preço cheio), então a margem é larga: 1,5x o maior visto, com 10+
        # observações (marca ~23% dos "de" reais, só os gritantes).
        precos = precos_altos(self.con, av.identidade.chave, av.ts - JANELA_DE_DIAS * 86400, av.ts)
        if len(precos) >= 10 and de > max(precos) * 1.5:
            av.de_inflado = True
            av.alertas.append(f"'de R${de:.0f}' inflado: em {JANELA_DE_DIAS} dias nunca passou de "
                              f"R${max(precos):.0f} (o desconto real é o de cima, não os {pct:.0f}%)")
            return 0
        teto = self.pesos["desconto_anunciado_max"]
        if pct >= 40:
            av.razoes.append(f"desconto anunciado {pct:.0f}% (o 'de' costuma ser inflado)")
            return teto
        return round(teto * 0.6) if pct >= 25 else 0

    # ── avaliação ──────────────────────────────────────────────
    def avaliar(self, texto_ou_promo, agora: int | None = None, conferir_ml: bool = True,
                card: CardML | None = None, card_da_api: bool = False) -> Avaliacao | None:
        """`card`: dados do ML já obtidos por outro caminho (ex.: o radar pela API); dispensa abrir o link.
        `card_da_api`: o preço do card veio da API do ML (sem o desconto do Pix), não da página."""
        promo = texto_ou_promo if isinstance(texto_ou_promo, Promo) else ler_promo(texto_ou_promo, self.cfg["marcas"])
        if not promo.valida:
            return None
        agora = agora or int(time.time())
        self.recarregar_se_mudou()
        if card is None and conferir_ml and self.ml and promo.link:
            card = self.ml.consultar(promo.link, agora)
        if card is not None:
            self._enriquecer_reputacao(card)
        razoes: list[str] = []
        alertas: list[str] = []
        if card and not card.ok:
            alertas.append(f"não deu para conferir no ML: {card.erro}")
        if not parece_perfume(promo.produto, promo.texto):
            alertas.append("isso não parece um perfume")

        analise = self.ident.analisar(promo.produto)
        conflito = concentracao = ""
        if card and card.ok:
            analise_ml = self.ident.analisar(card.titulo)
            if not analise.base and analise_ml.base and parece_perfume(card.titulo):
                # 11/10: o grupo escreveu só "Perfume 50ml" (52 leituras em 2 dias viravam o produto sem nome
                # "|50", todos misturados no mesmo histórico). O nome está no título do anúncio do ML.
                promo.produto = card.titulo
                analise = analise_ml
            elif Identificador.outra_marca(analise, analise_ml) and parece_perfume(card.titulo):
                # 18/10 (Breno: "ele confundiu com o Asad, mas esse é diferente, outro perfume"): o grupo escreveu
                # "Asad Bourbon" e o anúncio era "... - Marca Isabelle La Belle", a versão inspirada (R$79). Saiu
                # IMPERDÍVEL, "62% abaixo" do Asad Bourbon da Lattafa (R$209). O produto é o do anúncio.
                alertas.append(f"o anúncio é de outra marca ({Identificador.outra_marca(analise, analise_ml).title()}): "
                               "comparado com o histórico desse anúncio, não com o do original")
                promo.produto = card.titulo
                analise = analise_ml
            else:
                conflito = Identificador.conflito(analise, analise_ml)
                concentracao = "" if conflito else Identificador.concentracao_diferente(analise, analise_ml)
                if not analise.volume and analise_ml.volume and not conflito:
                    analise = self.ident.analisar(f"{promo.produto} {analise_ml.volume}ml")
        if not analise.base:
            alertas.append("não identifiquei o produto: sem histórico para comparar")
        ident = self.catalogo.chave(analise)
        alertas.extend(ident.notas)

        mortos = self._cupons_mortos(promo, agora)
        if card and card.ok and card.vendedor and not self.loja_da_marca_do_card(card):
            for c in promo.cupons:   # 29/10: cupom de loja da marca não vale em revenda (Lipx, Klassey...)
                if trava_da_marca(self.con, c, agora) and c not in mortos:
                    mortos[c] = f"de loja da marca: não vale na revenda {card.vendedor}"
        quente = self._cupom_quente(promo, card, mortos, agora)
        if quente:
            # Cupom QUENTE (10/10): a equipe confirmou há pouco e este produto foi postado com ele. Vale o preço
            # do ML de agora menos o cupom, pela regra dele (não o número do grupo, que pode ser de horas atrás).
            preco = quente["preco"]
            razoes.append(f"cupom {quente['codigo']} QUENTE (confirmado às {quente['hora']}, {quente['por']}): "
                          f"R${card.preco:.2f} no ML, R${preco:.2f} com ele")
        else:
            preco = self._preco_efetivo(promo, card, razoes, alertas, agora, mortos)
        preco_ml = card.preco if (card and card.ok and card.preco) else None
        # O preço recalculado com cupom também compara com histórico COM cupom,
        # mesmo quando já difere do valor anunciado pelo grupo.
        usa_cupom = bool(quente) or (any(c not in mortos for c in promo.cupons)
                                     and (preco < preco_ml if preco_ml else abs(preco - promo.preco) < 0.01))
        historico = referencia(self.con, ident.chave, agora, self.cfg["janela_dias"], usa_cupom,
                               self.cfg["min_observacoes"])
        av = Avaliacao(promo=promo, identidade=ident, categoria=categoria(ident, promo.produto),
                       preco_efetivo=preco, historico=historico,
                       card=card, razoes=razoes, alertas=alertas, conflito=conflito, ts=agora, cupons_mortos=mortos,
                       cupom_quente=quente)
        if ident.kit:  # quanto sai o 100ml de perfume no kit x avulso (usado no preço quando o kit não tem histórico)
            av.kit_info = info_kit(self.con, promo.produto, preco, ident.tokens, agora)
        av.componentes = {
            "preco": self._preco(av),
            "relevancia": self._relevancia(av),
            "loja": self._loja(av),
            "sinais_ml": self._sinais_ml(av),
            "desconto": self._desconto_anunciado(av),
        }
        if card and card.ok and card.internacional:
            pais = (card.reputacao or {}).get("pais") or ""
            av.alertas.insert(0, "COMPRA INTERNACIONAL: vendedor de fora do Brasil"
                                 + (f" ({pais})" if pais and pais != "BR" else "") + "; não pode ser postado")
        if conflito:
            av.componentes["conflito"] = -PENALIDADE_CONFLITO
            av.alertas.insert(0, f"LINK APONTA PARA OUTRO PRODUTO: {conflito} (ML: {card.titulo[:70]})")
        vivos = [c for c in promo.cupons if c not in mortos]
        if vivos:
            av.alertas.append("preço depende de cupom: " + ", ".join(vivos))
        if not ident.volume or ident.volume == "?":
            if not ident.kit:
                av.alertas.append("volume (ml) não informado: comparação pode misturar tamanhos")
        self._avaliar_risco(av)
        if card and card.seller_id and not av.loja_selo:
            n, total = produtos_do_vendedor(self.con, card.seller_id)
            if total >= MIN_PRODUTOS_VISTOS:
                av.produtos_vendedor = n
                if n == 1:
                    # 06/10 (Breno, Z18PERFUMARIA: "1 Produto" na loja): só aviso, a nota não muda.
                    av.alertas.append("loja de 1 produto: vendedor só visto vendendo este perfume; confira")
        av.tendencia = tendencia(self.con, ident.chave, agora, av.preco_efetivo,
                                 "com_cupom" if usa_cupom else "sem_cupom")
        if av.tendencia.get("ja_esteve"):
            av.alertas.append(f"já esteve R${av.tendencia['ja_esteve']:.0f} há {av.tendencia['ha_dias']} dia(s): "
                              "pode cair mais")
        if not promo.cupons:
            # Só informação: o cupom pode não valer para este vendedor, então não mexe na nota.
            av.cupom_sugerido = melhor_cupom(self.con, av.preco_efetivo, agora,
                                             excluir=set(cupons_esgotados(self.con, agora)))
            if av.cupom_sugerido:
                av.razoes.append(f"com o cupom {av.cupom_sugerido['codigo']} (visto nos grupos) pode sair "
                                 f"R${av.cupom_sugerido['preco_final']:.2f}: conferir no carrinho")
        total = sum(av.componentes.values())
        teto, motivo = teto_da_nota(self.pesos, av.flags())
        if total > teto and motivo:
            av.alertas.append(motivo)
        av.score = round(max(0, min(100, total, teto)))
        limite_boa = ((self.cfg.get("limites_nota") or LIMITES_NOTA).get("BOA", 60))
        # 11/10: o piso valia com 3% de desconto para qualquer preço. Medido no dia: das promos de até R$350 que
        # foram ao pipeline só por ele (menos de 10% de desconto), o Breno postou 14 de 20; das de R$350 ou mais,
        # 3 de 10 (Polo, Armani e Lancôme de R$400+ com 4 a 8%). As caras precisam de 10% para virar Boa pelo piso.
        minimo_piso = self.pesos["pct_minimo_para_ser_promo"]
        if av.preco_efetivo >= self.pesos.get("piso_caro_a_partir_de", 350):
            minimo_piso = max(minimo_piso, self.pesos.get("pct_minimo_piso_caro", 10))
        if (av.loja_selo and av.pct_abaixo is not None and av.pct_abaixo >= minimo_piso
                and not av.risco and not av.conflito and not (card and card.internacional) and av.score < limite_boa):
            # 06/10 (Breno): loja oficial é mais confiável; com desconto real, já é BOA.
            av.score = limite_boa
            av.razoes.append("loja oficial com desconto real: no mínimo BOA")
        bonus = self.pesos.get("marca_que_vende", 20)
        if (bonus and self.marcas_que_vendem and av.score < limite_boa - 1
                and self.marcas_que_vendem.search(sem_acento(promo.produto.lower()))
                and av.pct_abaixo is not None and av.pct_abaixo >= -10
                and not av.risco and not av.conflito and not (card and card.internacional)):
            # 11/10 (Breno: "por que você diria que são ruins? são marcas que vendem bem"). A nota mede o desconto;
            # Natura e Boticário a preço normal ou com desconto pequeno ficavam em "Ruim 3x" (19 das 26 do dia).
            # Sobem 20 pontos, no máximo até Média: Boa continua sendo só com desconto de verdade.
            # 15/10 (Breno: "kits de body splash são bem pedidos, não devem ser ruins"; "até os perfuminhos Vodka vendem
            # bem"): quem vende bem nunca fica Ruim no preço normal ou até 10% acima dele: no mínimo Média 50.
            sobe = bonus if av.pct_abaixo >= 0 else 0
            av.score = min(limite_boa - 1, max(MEDIA_DE_QUEM_VENDE, av.score + sobe))
            av.razoes.append("vende bem no grupo: a nota sobe (no máximo até Média)")
        limite_exc = (self.cfg.get("limites_nota") or LIMITES_NOTA).get("IMPERDÍVEL", 75)
        if av.score >= limite_exc and card and card.ok and card.item_id and card.preco:
            # 31/10 (Breno: "tem ctz que isso é uma promo excelente?"; Astra: "considerar os últimos 7-14 dias da mesma
            # loja/anúncio"). O Khamrah 100ml saiu Excelente 83 a R$186,72 na Wathiq; o mesmo anúncio estava a R$177,38
            # no dia anterior. Excelente só se o preço não estiver mais de 3% acima do menor preço do anúncio em 7 dias
            # (sem cupom vale o do anúncio, não o número do grupo; com cupom, o preço com ele); senão, no máximo Boa
            # (continua indo, sem o selo de Excelente). Em 24-30/10: 16 de 123 Excelentes do pipeline cairiam para Boa,
            # 11 delas postadas (Kaiak Sonar a R$94,95 com o anúncio a R$75,96 dias antes, Yara Moi +15%...).
            # A régua "costuma sair nos grupos" foi medida e descartada: derrubaria 133 de 448 envios, 85 postados.
            pago = av.preco_efetivo if usa_cupom else float(card.preco)
            # a chave vem do título do anúncio, como o preço do ML é gravado (observar_preco_ml). 31/10 22h: API com
            # API e página com página (o preço da API vem sem o Pix; o radar do Khamrah na Wathiq caiu para Boa sem
            # subida nenhuma, comparando os R$186,72 da API com os R$177,38 da página).
            chave_anuncio = self.chave_de(card.titulo)
            menor = menor_preco_do_anuncio(self.con, chave_anuncio, card.item_id, agora, pago, api=card_da_api)
            onde = ""
            if card_da_api and not (menor and pago > menor * 1.03):
                # O radar só vê a API. Se a página do anúncio foi vista nas últimas 24 h, a página de agora contra a da
                # semana pega o fim do desconto do Pix (Club de Nuit Intense: API de 214,99 para 189,99; página de
                # 151,99 para 189,99).
                pagina = ultimo_preco_da_pagina(self.con, chave_anuncio, card.item_id, agora)
                menor_pagina = pagina and menor_preco_do_anuncio(self.con, chave_anuncio, card.item_id, agora, pagina)
                if menor_pagina and pagina > menor_pagina * 1.03:
                    pago, menor, onde = pagina, menor_pagina, f" na página, com o Pix (hoje R${pagina:.2f})"
            if menor and pago > menor * 1.03:
                av.score = min(av.score, limite_exc - 1)
                av.alertas.append(f"o mesmo anúncio custava R${menor:.2f} nos últimos 7 dias{onde}: hoje não é Excelente")
        if concentracao:
            # 31/10 (auditoria da Astra): o texto diz uma concentração e o anúncio outra ("The Icon EDT" com link do "The
            # Icon Edp" saía Excelente). Pode ser o post que escreveu errado (3 Boas legítimas em 14 dias) ou outro
            # produto (1), e o preço foi comparado com o histórico do que o texto diz: não bloqueia, mas no máximo Boa.
            av.score = min(av.score, limite_exc - 1)
            av.alertas.append(f"concentração diferente entre o texto e o anúncio ({concentracao}): confira o produto")
        av.oferta_oficial = oferta_oficial_melhor(card)
        if av.oferta_oficial:
            o = av.oferta_oficial
            # 01/11: curto (o grupo da equipe corta o alerta em 120 caracteres)
            preco_br = f"{o['preco']:.2f}".replace(".", ",")
            av.alertas.append(f"loja oficial{' ' + o['nome'] if o.get('nome') else ''} a R${preco_br} no mesmo produto "
                              "(este é revenda)" + ("" if o["vendas_conhecidas"] else "; selo só pela API"))
        n = av.historico.observacoes
        av.confianca = "alta" if n >= 8 else "média" if n >= self.cfg["min_observacoes"] else "baixa"
        av.nota = nota_do_score(av.score, self.cfg)
        if card and card.ok and card.titulo and not parece_perfume(card.titulo):
            # 11/10: kit de shampoo (Braé), camisa polo e camiseta ficavam na lista de Boas porque o nome que o
            # grupo deu era curto; o título do anúncio no ML diz o que é. "FORA" não aparece no painel nem vai
            # pelo pipeline (que ainda mantinha o card vivo, reconferindo a cada meia hora).
            av.nota, av.score = "FORA", 0
            av.alertas.append("pelo título do anúncio no ML, isso não é perfume")
        return av


LIMITES_NOTA = {"IMPERDÍVEL": 75, "BOA": 60, "OK": 45}


def nota_do_score(score: float, cfg: dict | None = None) -> str:
    limites = (cfg or {}).get("limites_nota") or LIMITES_NOTA
    for nota in ("IMPERDÍVEL", "BOA", "OK"):
        if score >= limites[nota]:
            return nota
    return "NÃO VALE"


def teto_da_nota(pesos: dict, f: dict) -> tuple[float, str]:
    """Até onde a nota pode ir, pelo desconto real e pelo risco. Usada na avaliação e na calibração.

    `f`: pct_abaixo, recorde, sem_historico, na_faixa, mesmo_preco_de_sempre, conflito, risco.
    """
    if f.get("conflito"):
        return 20, ""
    if f.get("internacional"):
        # 09/10 (Breno): compra internacional não pode ser postada no grupo.
        return pesos["teto_sem_desconto_real"], "compra internacional: o grupo não posta (nota limitada a NÃO VALE)"
    if f.get("risco") == "alto":
        return pesos["teto_sem_desconto_real"], "risco de falsificado: nota limitada a NÃO VALE"
    teto, motivo = _teto_pelo_desconto(pesos, f)
    if f.get("risco") == "medio" and teto > pesos["tetos_por_desconto"][0][1]:
        return pesos["tetos_por_desconto"][0][1], "risco de falsificado: nota limitada a MÉDIA"
    if f.get("vendedor_ok") is False and teto > pesos["tetos_por_desconto"][0][1]:
        return pesos["tetos_por_desconto"][0][1], "vendedor sem selo e sem reputação comprovada: no máximo MÉDIA"
    return teto, motivo


def _teto_pelo_desconto(pesos: dict, f: dict) -> tuple[float, str]:
    if f.get("sem_historico"):
        # Sem base de comparação: a faixa da categoria libera no máximo BOA; se o preço é o mesmo
        # das poucas vezes que o produto apareceu, no máximo OK; fora da faixa, NÃO VALE.
        if not f.get("na_faixa"):
            return pesos["teto_sem_desconto_real"], ""
        if f.get("mesmo_preco_de_sempre"):
            return pesos["tetos_por_desconto"][0][1], ""
        return pesos["teto_sem_historico"], ""
    pct = f.get("pct_abaixo")
    if pct is None or pct < pesos["pct_minimo_para_ser_promo"]:
        # Ordem do Breno (24/09): favorito ou loja boa no preço normal não é promo.
        return pesos["teto_sem_desconto_real"], "sem desconto real: nota limitada a NÃO VALE"
    if not f.get("recorde"):
        # O desconto real define até onde a nota pode ir: 3-7% no máximo OK, 7-12% no máximo BOA.
        # Loja oficial/TOP confirmada (pedido do Breno em 06/10: mais confiável, preço ok já vale): um degrau
        # acima: 3-7% pode chegar a BOA, 7%+ sem teto. Preço normal (< 3%) continua NÃO VALE.
        tetos = (pesos.get("tetos_por_desconto_loja_selo", [[7, 74]]) if f.get("loja_selo")
                 else pesos.get("tetos_por_desconto", []))
        for limite, teto in tetos:
            if pct < limite:
                return teto, f"desconto de só {pct:.0f}%: nota limitada"
    return 100, ""


def ranquear(avaliacoes: list[Avaliacao]) -> list[Avaliacao]:
    """Junta a mesma oferta repostada por vários grupos e ordena da melhor para a pior.

    Mesma oferta = mesmo produto com preços a até 2% um do outro.
    """
    por_produto: dict[str, list[Avaliacao]] = {}
    for av in avaliacoes:
        if av is not None:
            por_produto.setdefault(av.identidade.chave, []).append(av)
    escolhidas: list[Avaliacao] = []
    for grupo in por_produto.values():
        grupo.sort(key=lambda a: a.preco_efetivo)
        atual: list[Avaliacao] = []
        for av in grupo:
            if atual and av.preco_efetivo > atual[0].preco_efetivo * 1.02:
                escolhidas.append(max(atual, key=lambda a: (a.score, a.loja_confirmada)))
                atual = []
            atual.append(av)
        escolhidas.append(max(atual, key=lambda a: (a.score, a.loja_confirmada)))
    return sorted(escolhidas, key=lambda a: (-a.score, a.preco_efetivo))


def para_dict(av: Avaliacao) -> dict:
    """Tudo que o painel precisa mostrar de uma avaliação (JSON simples)."""
    p, h, c = av.promo, av.historico, av.card
    return {
        "produto": p.produto, "texto": p.texto, "preco": round(av.preco_efetivo, 2), "preco_grupo": p.preco,
        "preco_de": p.preco_de,
        "cupons": p.cupons, "link": p.link, "nota": av.nota, "score": av.score, "confianca": av.confianca,
        "pct_abaixo": None if av.pct_abaixo is None else round(av.pct_abaixo, 1),
        "loja": av.loja, "loja_confirmada": av.loja_confirmada, "loja_selo": av.loja_selo,
        "top_vendido": av.top_vendido,
        "categoria": av.categoria, "chave": av.identidade.chave, "conflito": av.conflito,
        "razoes": av.razoes, "alertas": av.alertas, "componentes": av.componentes, "grupo": av.grupo,
        "flags": av.flags(), "pct_anunciado": av.pct_anunciado, "de_inflado": av.de_inflado,
        "reputacao": av.reputacao, "risco": av.risco, "risco_motivo": av.risco_motivo, "tendencia": av.tendencia,
        "cupom_sugerido": av.cupom_sugerido, "kit_info": av.kit_info, "cupons_mortos": av.cupons_mortos,
        "cupom_quente": av.cupom_quente, "oferta_oficial": av.oferta_oficial,
        "catalogo": c.catalogo if c and c.ok else None, "quem_vende": texto_confianca(av),
        "internacional": bool(c and c.ok and c.internacional),
        "produtos_vendedor": av.produtos_vendedor, "vendedor_ok": av.vendedor_ok,
        "historico": {"tipo": h.tipo, "observacoes": h.observacoes, "dias": h.dias, "mediana": h.mediana,
                      "minimo": h.minimo, "p25": h.p25, "media": h.media},
        "ml": None if not (c and c.ok) else {"titulo": c.titulo, "preco": c.preco, "vendedor": c.vendedor,
                                              "oficial": c.loja_oficial, "full": c.full,
                                              "mais_vendido": c.mais_vendido, "url": c.url_produto,
                                              "foto": c.foto, "preco_cupom": c.preco_cupom,
                                              # 03/11: o "outros meios" da hora, para a conta cupom x Pix
                                              "preco_outros": c.preco_outros},
    }


def relatorio(av: Avaliacao) -> str:
    p, h = av.promo, av.historico
    ml = "conferido no ML" if av.conferido_ml else "não conferido no ML"
    linhas = [
        f"{av.nota}  |  score {av.score}/100  |  confiança {av.confianca}  |  {ml}",
        f"Produto: {p.produto}",
        f"Preço: R${av.preco_efetivo:.2f}" + (f" (grupo: R${p.preco:.2f})" if abs(av.preco_efetivo - p.preco) > 0.01 else "")
        + (f" | de R${p.preco_de:.2f}" if p.preco_de else ""),
        f"Loja: {av.loja or '-'}{' (confirmada)' if av.loja_confirmada else ''}   Categoria: {av.categoria}"
        f"   Chave: {av.identidade.chave}",
    ]
    if av.card and av.card.ok:
        linhas.append(f"ML: {av.card.titulo[:90]}")
    if h.observacoes:
        tipo = {"com_cupom": "com cupom", "sem_cupom": "sem cupom"}.get(h.tipo, "todos")
        linhas.append(f"Histórico ({tipo}): {h.observacoes} preços em {h.dias} dias | mín R${h.minimo:.0f} | "
                      f"p25 R${h.p25:.0f} | mediana R${h.mediana:.0f} | último R${h.ultimo:.0f}")
    linhas += [f"  + {r}" for r in av.razoes]
    linhas += [f"  ! {a}" for a in av.alertas]
    return "\n".join(linhas)
