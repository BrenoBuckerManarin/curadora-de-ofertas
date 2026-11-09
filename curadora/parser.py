"""Lê o texto livre de uma promo (qualquer grupo) e extrai os campos comerciais."""
from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, field

DINHEIRO = r"R\$\s*(\d[\d.,]*)"
URL = re.compile(r"https?://\S+")
LINHA_OPERACIONAL = re.compile(
    r"(?i)(R\$|https?://|cupom|c[oó]digo|vendido|selecione|loja\b|link|compre|pegar promo|"
    r"frete|parcel|sem juros|\bpix\b|\bative\b|pesquise|\blimit[ea]d[oa]?\s+(?:a|de)\b|a partir de)"
)
VOLUME = re.compile(r"(?i)\b(\d{2,3})\s*ml\b")
PALAVRAS_PERFUME = re.compile(
    r"(?i)\b(edp|edt|parfum|perfume|eau\s+de|col[oô]nia|body\s*splash|body\s*spray|elixir|extrait|"
    r"desodorante\s+col[oô]nia)\b"
)
CONDICAO_CUPOM = re.compile(r"(?i)(a partir de|\blimit[ea]d?[oa]?\s+(?:a|de)\b|\blimite\b|m[ií]nim|acima de|compras? de|at[eé] R\$|cupom de R\$|R\$\s*[\d.,]+\s*off)")
EDITORIAL = re.compile(r"(?i)^(inspirad|dupe|contratipo|lembra|parecid|cheiro de|igual|um dos|o mais|o melhor)")
NAO_PERFUME = re.compile(
    r"(?i)\b(rel[oó]gio|m[aá]quina|aparador|barbeador|t[eê]nis|camis[ae]|bolsa|[oó]culos|fone|celular|smartwatch|"
    r"creme|hidrata[cç][aã]o|shampoo|condicionador|sabonete|protetor|facial|maquiagem|batom|escova|secador|"
    r"chapinha|cueca|carteira|mochila|garrafa|cabelo|barba|esmalte|s[eé]rum|t[oô]nico|"
    # 06/10: roupa e skincare que passavam ("Kit 3 Camisetas Polo Wear", "Loção CeraVe", "Principia Kit")
    r"camisetas?|camisas|shorts?|bermudas?|cal[cç]as?|blusas?|vestidos?|moletom|jaquetas?|chinelos?|"
    r"sand[aá]lias?|cuecas|hidratante|lo[cç][aã]o|principia|cerave|nivea|micelar|antitranspirante|roll[\s-]?on|"
    # 10/10: o pipeline mandou um kit de shampoo ("Kit Braé Stages Nutrition") e uma loção CeraVe "sem perfume"
    r"bra[eé]|wella|invigo|bozzano|gel\s+fixador|t-?shirts?|polo\s+(?:masculina|lacoste)|slim\s+fit|"
    r"m[aá]scara\s+capilar|"
    # 19/10: o pipeline mandou "Leave-in Uniq One Curls 230ml - Revlon" (o "230ml" fazia parecer perfume). Em 60
    # dias nada de cabelo foi ao GrupoFinal; nos grupos, 18 de 50.970 promos.
    r"leave[\s-]?in|curls|cachos|finalizador|creme\s+(?:de|para)\s+pentear)\b"
)
SEM_PERFUME = re.compile(r"(?i)\bsem\s+(?:perfume|fragr[aâ]ncia|cheiro)\b")
# Brinquedo, jogo, eletrônico etc.: os grupos de promo misturam tudo ("Loja oficial Lego no MELI!").
NAO_PERFUME_FORTE = re.compile(
    r"(?i)\b(lego|pe[cç]as|brinquedo|boneca|booster|pok[eé]mon|cartas|bundle|jogo|console|playstation|xbox|"
    r"nintendo|notebook|monitor|televis[aã]o|smart\s*tv|air\s*fryer|panela|cafeteira|liquidificador|aspirador|"
    r"fralda|ra[cç][aã]o|pneu|furadeira|ferramenta|cadeira|colch[aã]o|tablet|caixa de som|headset|mouse|teclado|"
    # 18/10: o pipeline mandou um "Copo Térmico Stanley Adventure Black 354ml" (o "354ml" fazia parecer perfume)
    r"copos?|canecas?|squeeze|t[eé]rmic[oa]s?|stanley|tumbler|marmitas?)\b"
)
# Frase de chamada, não o nome do produto. Voz de quem posta ("Meu favorito da Bidaya") é sempre chamada;
# já "inspirado", "lançamento", "alta fixação" aparecem também em título de anúncio do ML, então só contam
# em linha com cara de frase (vírgula ou ponto final) e sem volume.
VOZ_EDITORIAL = re.compile(r"(?i)\b(meu|minha|meus|minhas|favorit[oa]|todo mundo|mesmo criador|sempre na lista)\b")
FRASE_EDITORIAL = re.compile(
    r"(?i)\b(inspirad[oa]|lan[cç]amento|fixa[cç][aã]o|proje[cç][aã]o|vers[aã]o|nacional|contratipo|dupe|"
    r"cheiro|melhorad[oa]s?)\b"
)
MANCHETE = re.compile(r"(?i)(\breais\b|\bcaindo\b|\bpor\s+\d|\bpra\s+\d|\bs[oó]\s+\d|\d+,\d{2}\b)")
NAO_CUPOM = {"OFF", "OU", "ML", "PIX", "MELI", "APP", "USE", "NO", "DE", "E"}


@dataclass
class Promo:
    texto: str
    produto: str = ""
    preco: float | None = None
    preco_de: float | None = None
    cupons: list[str] = field(default_factory=list)
    link: str = ""
    marketplace: str = ""
    vendedor: str = ""
    loja_oficial: bool = False
    verificado: bool = False
    vendas: str = ""
    no_pix: bool = False

    @property
    def valida(self) -> bool:
        return bool(self.produto) and self.preco is not None and self.preco > 0


def limpar(texto: str) -> str:
    """Remove a formatação do WhatsApp sem quebrar nomes como PERFUMES_BHZ."""
    t = re.sub(r"[*~`]", "", texto or "")
    t = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", "", t)
    return t.replace(" ", " ")


def valor(txt: str) -> float | None:
    s = txt.strip().rstrip(".,")
    if not s:
        return None
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 < v < 100_000 else None


def _precos(linhas: list[str]) -> tuple[float | None, float | None, bool]:
    de = por = None
    no_pix = False
    for linha in linhas:
        if CONDICAO_CUPOM.search(linha):
            continue
        m_de = re.search(r"(?i)\bde\s*:?\s*" + DINHEIRO, linha)
        m_por = re.search(r"(?i)\bpor\s*:?\s*" + DINHEIRO, linha)
        if m_de and de is None:
            de = valor(m_de.group(1))
        if m_por and por is None:
            por = valor(m_por.group(1))
            no_pix = no_pix or bool(re.search(r"(?i)\bpix\b", linha))
    if por is None:
        # Formatos sem "por": "✅ R$ 148". Usa o primeiro valor que não é o "de".
        for linha in linhas:
            if CONDICAO_CUPOM.search(linha):
                continue
            # "R$ 278 no Pix (De R$ 449)": tira o trecho do "de" e usa o valor que sobra.
            resto = re.sub(r"(?i)\bde\s*:?\s*" + DINHEIRO, " ", linha)
            m = re.search(DINHEIRO, resto)
            if m:
                por = valor(m.group(1))
                no_pix = bool(re.search(r"(?i)\bpix\b", linha))
                break
    if de is not None and por is not None and de <= por:
        de = None
    return de, por, no_pix


def _cupons(linhas: list[str]) -> list[str]:
    achados: list[str] = []
    for linha in linhas:
        if not re.search(r"(?i)cupo[mn]|c[oó]digo", linha):
            continue
        trecho = re.split(r"(?i)cupo[mn]s?|c[oó]digo", linha, maxsplit=1)[-1]
        for tok in re.findall(r"\b[A-Z0-9]{4,30}\b", trecho):
            if tok not in NAO_CUPOM and not tok.isdigit() and tok not in achados:
                achados.append(tok)
    return achados


def _marketplace(link: str) -> str:
    l = link.lower()
    if "meli.la" in l or "mercadolivre" in l or "mercadolibre" in l:
        return "ml"
    if "amzn" in l or "amazon" in l:
        return "amazon"
    if "shopee" in l or "shp.ee" in l:
        return "shopee"
    if "magalu" in l or "magazineluiza" in l:
        return "magalu"
    return "outro" if l else ""


def _vendedor(linhas: list[str]) -> tuple[str, bool, bool, str]:
    nome = ""
    oficial = verificado = False
    vendas = ""
    for linha in linhas:
        baixa = linha.lower()
        if "loja oficial" in baixa or "loja verificada" in baixa or "vendido por" in baixa \
                or "selecione a loja" in baixa or "verificado pelo" in baixa or "selecione vendedor" in baixa \
                or "selecione o vendedor" in baixa or re.match(r"(?i)\W*loja\s*:", linha):
            oficial = oficial or "loja oficial" in baixa
            verificado = verificado or "verificad" in baixa
            m = re.search(r"\(\s*(\+?\s*[\d.,]+\s*(?:mil|k|m)?)\s*vendas?\s*\)", linha, re.I)
            if m:
                vendas = m.group(1).replace(" ", "")
            if nome:
                continue
            m = (re.search(r"(?i)loja\s+oficial\s+(.+?)\s+(?:no|do|na)\s+(?:ml|meli|mercado\s*livre)\b", linha)
                 or re.search(r"(?i)loja\s*:\s*(?!loja\s+oficial)(.+?)\s+(?:no|do|na)\s+(?:ml|meli|mercado\s*livre)\b", linha)
                 or re.search(r"(?i)vendido\s+por\s+(?:loja\s+oficial\s+)?(.+?)(?:\s*\(|\s+verificad|\s+no\s+ml\b|$)", linha)
                 or re.search(r"(?i)selecione\s+a\s+loja\s+(.+)$", linha)
                 or re.search(r"(?i)selecione\s+(?:o\s+)?vendedor\s+(.+?)(?:\s+no\s+(?:ml|meli|mercado\s*livre)\b|$)", linha)
                 or re.search(r"(?i)loja\s+oficial\s+([^:!\n]+?)\s*[:!]*$", linha))
            if m:
                cand = re.sub(r"[^\w\s'&.-]", "", m.group(1)).strip(" .-")
                if cand and not re.fullmatch(r"(?i)((no|do|na)\s+)?(ml|meli|mercado\s*livre)?|loja oficial", cand):
                    nome = cand
    return nome, oficial, verificado, vendas


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


@functools.lru_cache(maxsize=8)
def _regex_marcas(marcas: tuple) -> re.Pattern:
    """Uma regex só com todas as marcas (sem acento), montada uma vez por lista de marcas."""
    return re.compile(r"\b(?:" + "|".join(re.escape(_sem_acento(m.lower())) for m in marcas) + r")\b")


def _pontua_linha_produto(linha: str, marcas: list[str]) -> int:
    if LINHA_OPERACIONAL.search(linha) or len(linha) < 4:
        return -99
    pts = 0
    if VOLUME.search(linha):
        pts += 3
    if PALAVRAS_PERFUME.search(linha):
        pts += 2
    baixa = _sem_acento(linha.lower())  # "O Boticário" tem que casar com a marca "o boticario"
    if marcas and _regex_marcas(tuple(marcas)).search(baixa):
        pts += 2
    if re.match(r"(?i)kit\b", linha):
        pts += 1
    letras = [c for c in linha if c.isalpha()]
    if letras and sum(c.isupper() for c in letras) / len(letras) > 0.8:
        # Manchete em caixa alta ("UM DOS MELHORES NACIONAIS"). Com volume ainda pode
        # ser o produto ("YSL Y EDT 100ml"), mas perde para uma linha normal.
        pts -= 1 if VOLUME.search(linha) else 3
    if MANCHETE.search(linha):
        pts -= 4  # "AL WATANIAH 100ML CAINDO PRA 162 REAIS"
    if EDITORIAL.match(linha):
        pts -= 5  # comparação editorial, não é o produto vendido
    elif len(linha.split()) >= 4 and (VOZ_EDITORIAL.search(linha) or (
            FRASE_EDITORIAL.search(linha) and not VOLUME.search(linha) and re.search(r",|[.!]\W*$", linha))):
        pts -= 4  # "Todo mundo já sabe que é meu árabe favorito, inspirado no Dior Homme Parfum."
    if len(linha.split()) >= 5 and re.search(r"[a-zà-ú]\.\W*$", linha):
        pts -= 2  # frase terminada em ponto: texto corrido, não um nome de produto
    if linha.rstrip().endswith((",", "!", "?")):
        pts -= 1
    return pts


TITULO_GENERICO = re.compile(r"(?i)\b(oferta|promo|cupom|desconto|baixou|rel[aâ]mpago|imperd[ií]vel|achad|queima|"
                             r"black friday|precinho|hoje|corre|aproveit|bom dia|boa noite|boa tarde)")


TITULO_DE_ANUNCIO = re.compile(r"(?i)\b(eau\s+de|edp|edt|parfum|feminino|masculino|unissex|kit|coffret|"
                               r"col[oô]nia|body\s*splash)\b|\s-\s")


def _titulo_ok(linha: str, max_palavras: int) -> bool:
    return bool(linha) and (not LINHA_OPERACIONAL.search(linha) and not EDITORIAL.match(linha)
                            and not MANCHETE.search(linha) and not VOZ_EDITORIAL.search(linha)
                            and not FRASE_EDITORIAL.search(linha) and len(linha.split()) <= max_palavras
                            and not linha.rstrip().endswith((",", "!", "?", ":")))


def _cara_de_frase(linha: str) -> bool:
    """"O clássico da Boticário", "Carro chefe da linha, DNA do Spicebomb": texto corrido, não título."""
    if VOLUME.search(linha) or TITULO_DE_ANUNCIO.search(linha):
        return False  # "Giorgio Armani Acqua di Gioia Intense - Eau de Parfum Feminino" é título do ML
    return sum(1 for w in linha.split()[1:] if w[:1].islower()) >= 2


def _produto(linhas: list[str], marcas: list[str], negritos: list[str], setas: list[str] = (),
             titulo: str = "") -> str:
    melhor, melhor_pts = "", 1
    for linha in linhas:
        if linha.startswith(("🏬", "🏪")):
            continue  # "🏬 O Boticário": é a loja, não o produto
        limpa = re.sub(r"^[^\w]+", "", linha).strip()
        pts = _pontua_linha_produto(limpa, marcas)
        if pts > melhor_pts:
            melhor, melhor_pts = limpa, pts
    # Formato Fonte Zeta: "➡️ **Mini Fragrâncias Privée EDP (3 Itens)**" — a seta marca o produto.
    seta = next((s for s in setas if _titulo_ok(s, 16) and not TITULO_GENERICO.search(s)), "")
    if seta:
        melhor = seta
    # Formato "*CLASH*" + "O clássico da Boticário": o título em negrito na 1ª linha é o produto,
    # a linha de baixo é a chamada.
    elif titulo and _titulo_ok(titulo, 6) and not TITULO_GENERICO.search(titulo) and (
            not melhor or _cara_de_frase(melhor)):
        melhor = titulo
    if not melhor:
        # Formato FONTE GAMA: o nome vem sozinho em negrito, sem volume ("*HIS CONFESSION*").
        melhor = next((l for l in negritos if _titulo_ok(l, 6)), "")
    # Manchete usada como produto por falta de opção: corta o "POR 112,50 🥷".
    melhor = re.sub(r"(?i)\s+(por|pra|caindo|s[oó])\b.*$", "", melhor) if MANCHETE.search(melhor) else melhor
    return re.sub(r"\s+", " ", melhor)[:140].strip(" -,")


def parece_perfume(produto: str, texto: str = "") -> bool:
    """Os grupos também postam relógio, skincare, Lego etc. Sinal de perfume vence a lista negativa.

    Com o `texto` da mensagem, olha também a loja ("Loja oficial Pokémon no MELI!")."""
    if SEM_PERFUME.search(produto):
        return False  # "Loção Hidratante Sem Perfume", "Desodorante Sem Perfume": a palavra está lá para negar
    if PALAVRAS_PERFUME.search(produto):
        return True
    # Kit de perfume com brinde: "Kit Egeo Dolce - Boticário (50ml + Creme 85g)" — o que vem antes do "+" manda.
    principal = produto.split("+")[0]
    if (re.search(r"(?i)\b(kit|combo|presente)\b", produto) and "+" in produto and VOLUME.search(principal)
            and not NAO_PERFUME.search(principal) and not NAO_PERFUME_FORTE.search(principal)):
        return True
    if not produto or NAO_PERFUME.search(produto) or NAO_PERFUME_FORTE.search(produto):
        return False  # "Creme ... Cicaplast 20ml": ter ml não faz virar perfume
    if VOLUME.search(produto):
        return True
    return not (texto and NAO_PERFUME_FORTE.search(texto) and not (VOLUME.search(texto) or PALAVRAS_PERFUME.search(texto)))


def ler_promo(texto: str, marcas: list[str] | None = None) -> Promo:
    marcas = marcas or []
    limpo = limpar(texto)
    linhas = [l.strip() for l in limpo.splitlines() if l.strip()]
    negritos = [limpar(m.group(1)).strip() for m in re.finditer(r"(?m)^\s*\*{1,2}([^*\n]+)\*{1,2}\s*$", texto or "")]
    setas = [limpar(m.group(1)).strip() for m in
             re.finditer(r"(?m)^\s*(?:➡️|➡|👉|▶️|▶)\s*\*{1,2}([^*\n]+)\*{1,2}\s*$", texto or "")]
    primeira = next((l for l in (texto or "").splitlines() if l.strip()), "")
    m_titulo = re.fullmatch(r"\s*\*{1,2}([^*\n]+)\*{1,2}\s*", primeira)
    titulo = limpar(m_titulo.group(1)).strip() if m_titulo else ""
    links = URL.findall(limpo)
    de, por, no_pix = _precos(linhas)
    nome, oficial, verificado, vendas = _vendedor(linhas)
    link = links[0].rstrip(").,") if links else ""
    return Promo(
        texto=texto,
        produto=_produto(linhas, marcas, negritos, setas, titulo),
        preco=por,
        preco_de=de,
        cupons=_cupons(linhas),
        link=link,
        marketplace=_marketplace(link),
        vendedor=nome,
        loja_oficial=oficial,
        verificado=verificado,
        vendas=vendas,
        no_pix=no_pix,
    )
