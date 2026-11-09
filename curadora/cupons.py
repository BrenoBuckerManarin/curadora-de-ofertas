"""Cupons do momento: os grupos anunciam o dia todo; a curadora guarda a regra de cada um.

"Cupom: `MODA0310` 15% OFF acima de R$ 79 Limite de R$ 20"  -> 15%, mínimo R$79, desconto até R$20
"R$20,00 OFF em compras acima de R$60 Cupom: X"             -> R$20 fixo, mínimo R$60

Um cupom é considerado ATIVO para perfume se foi visto nas últimas 6h junto de uma promo de perfume
(prova de que vale para perfume). É informação para quem vai postar: não muda a nota, porque o cupom
pode não valer para aquele vendedor ou categoria.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

ESQUEMA_CUPONS = """
CREATE TABLE IF NOT EXISTS cupons (
    codigo      TEXT PRIMARY KEY,
    primeiro_ts INTEGER NOT NULL,
    ultimo_ts   INTEGER NOT NULL,
    vezes       INTEGER NOT NULL DEFAULT 1,
    pct         REAL,
    valor       REAL,
    minimo      REAL,
    limite      REAL,
    perfume_ts  INTEGER            -- última vez visto junto de uma promo de perfume
);
"""
ATIVO_S = 6 * 3600
DESCONTO_MAX = 0.35        # nenhum cupom de grupo derruba mais que 35% (evita leitura errada virar promo)
CODIGO = re.compile(r"\b[A-Z0-9]{4,30}\b")
VOLUME = re.compile(r"\d+(?:ML|L)")   # 31/10: "30ML" é o frasco, não cupom
NAO_E_CUPOM = {"OFF", "PIX", "MELI", "FULL", "LOJA", "ATIVE", "AQUI", "CUPOM", "CUPONS", "NOVO", "NOVOS", "LIMITE", "MERCADO", "LIVRE", "PERFUME", "PERFUMES", "LINK", "RESGATE", "ATIVO", "ATIVOS"}
DINHEIRO = r"R\$\s*([\d.]+(?:,\d{1,2})?)"


@dataclass
class Regra:
    pct: float | None = None
    valor: float | None = None
    minimo: float | None = None
    limite: float | None = None

    def desconto(self, preco: float) -> float:
        if self.minimo and preco < self.minimo:
            return 0.0
        if self.pct:
            d = preco * self.pct / 100
            d = min(d, self.limite) if self.limite else d
        elif self.valor and self.minimo:                  # valor fixo sem mínimo conhecido: não arrisca
            d = self.valor
        else:
            return 0.0
        return min(d, preco * DESCONTO_MAX)


def _valor(txt: str) -> float | None:
    try:
        return float(txt.replace(".", "").replace(",", "."))
    except ValueError:
        return None


# 03/11 (relatório da Astra): o aviso da equipe das 18:07 dizia "15% acima de R$89 máx R$20", sem o OFF, e o
# percentual não era lido. Sem OFF, o % só vale seguido de uma compra mínima em reais ("acima de R$89", "a partir de
# R$99", "em compras acima de R$79"): "15% no Pix", "15% de desconto no Pix" e "15% a partir de 2 unidades" ficam fora.
PCT_SEM_OFF = re.compile(r"(\d{1,2})\s*%\s*(?:de\s+desconto\s+)?(?:(?:em|nas?)\s+compras?\s+)?(?:acima|a\s+partir)\s+de\s*R\$",
                         re.I)
# 01/11 12:03: "SITE200111 - R$20/299" (R$ 20 OFF acima de R$ 299). Só com o R$ na frente: "03/11" é data.
VALOR_BARRA_MINIMO = re.compile(DINHEIRO + r"\s*/\s*(?:R\$\s*)?([\d.]+(?:,\d{1,2})?)(?![\d/])")


def ler_regra(trecho: str) -> Regra:
    t = re.sub(r"[*_~`]", "", trecho)
    r = Regra()
    m = re.search(r"(\d{1,2})\s*%\s*OFF", t, re.I) or PCT_SEM_OFF.search(t)
    if m:
        r.pct = float(m.group(1))
    else:
        m = re.search(DINHEIRO + r"\s*OFF", t, re.I)
        if m:
            r.valor = _valor(m.group(1))
        else:
            m = VALOR_BARRA_MINIMO.search(t)
            valor, minimo = (_valor(m.group(1)), _valor(m.group(2))) if m else (None, None)
            if valor and minimo and valor < minimo:
                r.valor, r.minimo = valor, minimo
    m = re.search(r"(?:acima de|a partir de|m[ií]nimo de|compras? de)\s*" + DINHEIRO, t, re.I)
    if m:
        r.minimo = _valor(m.group(1))
    # 01/11: "limite R$35" sem o "de" (aviso da equipe de 28/10, TODOSITE2810) também é o teto
    m = re.search(r"(?:limite(?:\s+de)?:?|limitad[oa] a|at[eé]|m[aá]x(?:imo)?\.?(?: de)?)\s*" + DINHEIRO, t, re.I)
    if m:
        r.limite = _valor(m.group(1))
    return r


OUTRA_LOJA = re.compile(r"(?i)magalu|magazine luiza|shopee|amazon|aliexpress|shein|americanas|casas bahia|kabum")
DO_ML = re.compile(r"(?i)mercado ?livre|\bmeli\b|mercadolivre")
SOZINHO = re.compile(r"^\s*(?:```|`)([A-Z0-9]{4,30})(?:```|`)\s*(?:🎟️?)?\s*$")


def _codigos_da_linha(bruta: str, limpa: str, proxima: str) -> list[str]:
    m = SOZINHO.match(bruta)                              # anúncio: ```MODA10BELEZA``` sozinho na linha
    if m and m.group(1) not in NAO_E_CUPOM and not m.group(1).isdigit():
        return [m.group(1)]
    if not re.search(r"(?i)cupo[mn]|🎟", limpa):
        return []
    # \b: o código pode começar por "CUPOM" ("Código: CUPOMEXCLUSIVO" virava "EXCLUSIVO"; CUPOMDEHOJE, "DEHOJE")
    trecho = re.split(r"(?i)\bcupo[mn]s?\b", limpa, maxsplit=1)[-1]
    codigos = [c for c in CODIGO.findall(trecho) if c not in NAO_E_CUPOM and not c.isdigit()
               and not VOLUME.fullmatch(c)]
    if not codigos and CODIGO.fullmatch(proxima) and proxima not in NAO_E_CUPOM and not proxima.isdigit():
        codigos = [proxima]                               # "Cupom:" e o código na linha de baixo
    return codigos


CARA_DE_REGRA = re.compile(r"(?i)acima de|a partir de|m[ií]nimo|limite|limitad[oa]|m[aá]x")


def cupons_da_mensagem(texto: str) -> dict[str, Regra]:
    """Códigos de cupom do Mercado Livre citados na mensagem, cada um com a regra escrita junto dele."""
    texto = texto or ""
    if OUTRA_LOJA.search(texto) and not DO_ML.search(texto):
        return {}
    brutas = texto.splitlines()
    limpas = [re.sub(r"[*_~`]", "", l) for l in brutas]
    por_linha = [_codigos_da_linha(b, l, limpas[i + 1].strip() if i + 1 < len(limpas) else "")
                 for i, (b, l) in enumerate(zip(brutas, limpas))]
    achados: dict[str, Regra] = {}

    def tem_regra(linha: str) -> bool:
        regra = ler_regra(linha)
        return bool(regra.pct or regra.valor)

    def vizinha(i: int, passo: int) -> str:
        """A linha não vazia mais próxima de `i` (até 4 linhas), acima (passo -1) ou abaixo (+1)."""
        for j in range(i + passo, i + 5 * passo, passo):
            if not 0 <= j < len(limpas):
                break
            if limpas[j].strip():
                return "" if por_linha[j] else limpas[j]
        return ""

    # 13/10: aviso de vários cupons com a regra ACIMA de cada código ("🔥 Ganhe 30% OFF, limite de R$500" e, na linha
    # de baixo, "🎟️ Cupom: SALVAMEUBOLSO"; depois "Ganhe 25% OFF..." e "Cupom: ACHEINOMELI"...). Lendo a regra abaixo,
    # cada cupom ficava com a regra do SEGUINTE (o de 30% virou 25%, o de 18% virou "10% acima de R$199"), e a regra
    # guardada mudava conforme o grupo que tinha falado por último. É esse o formato quando o 1º código tem regra
    # logo acima e o último não tem regra nem na linha dele nem abaixo.
    com_codigo = [i for i, codigos in enumerate(por_linha) if codigos]
    regra_acima = (len(com_codigo) >= 2 and not tem_regra(limpas[com_codigo[0]]) and tem_regra(vizinha(com_codigo[0], -1))
                   and not tem_regra(limpas[com_codigo[-1]]) and not tem_regra(vizinha(com_codigo[-1], 1)))
    for i, codigos in enumerate(por_linha):
        if not codigos:
            continue
        if regra_acima and not tem_regra(limpas[i]):
            bloco = []
            for j in range(i - 1, max(-1, i - 6), -1):
                if por_linha[j]:
                    break
                if limpas[j].strip():
                    bloco.insert(0, limpas[j])
                    if len(bloco) == 2:
                        break
            regra = ler_regra(" ".join(bloco))
            for codigo in codigos:
                if codigo not in achados or regra.pct or regra.valor:
                    achados[codigo] = regra
            continue
        # A regra vem na mesma linha ou logo abaixo ("10% OFF, mínimo de R$ 89 / Limite de R$ 20"),
        # até o próximo cupom: assim um anúncio com vários cupons não mistura as regras. 03/11: "Cupom: `R$ 18,00 OFF`
        # ou `SITE031115`" são dois cupons diferentes; o desconto do outro lado do "ou" não é deste código.
        linha = limpas[i]
        if re.search(r"(?i)\s+ou\s+", linha):
            linha = " ".join(p for p in re.split(r"(?i)\s+ou\s+", linha)
                             if any(c in p for c in codigos) or not tem_regra(p))
        bloco = [linha]
        for j in range(i + 1, min(i + 5, len(limpas))):
            if por_linha[j] and por_linha[j] != codigos:
                break
            bloco.append(limpas[j])
        regra = ler_regra(" ".join(bloco))
        if not (regra.pct or regra.valor):
            # Sem regra na linha nem abaixo: procura acima ("✅ R$ 20 OFF acima de R$ 60" e depois o código).
            pulou_branco, achadas = False, 0
            for j in range(i - 1, max(-1, i - 6), -1):
                if por_linha[j] and por_linha[j] != codigos:
                    break
                if not limpas[j].strip():
                    # 10/10 (OFERTAMELI): o anúncio vem com linha em branco entre a regra e o código.
                    pulou_branco = True
                    continue
                # Depois de uma linha em branco só vale linha com cara de regra de cupom (mínimo ou limite): numa
                # promo comum, o "15% OFF no Pix" de dois parágrafos acima não é a regra do cupom.
                if pulou_branco and not CARA_DE_REGRA.search(limpas[j]):
                    break
                bloco.insert(0, limpas[j])
                achadas += 1
                if achadas == 2:
                    break
            regra = ler_regra(" ".join(bloco))
        for codigo in codigos:
            if codigo not in achados or regra.pct or regra.valor:   # o mesmo código em 2 linhas: fica a que tem regra
                achados[codigo] = regra
    return achados


# 01/11 (gatilho do aviso da equipe): o aviso de cupom no GrupoFinal traz os cupons do dia de todas as seções do ML ("15% OFF
# máx R$40 em Livros & Jogos", "R$100 OFF em Celular, PC, TECH", "20% OFF máx de R$80 p/ Meli+"). De 06/10 a 01/11
# foram 29 avisos: LIVROS3110, BEBES3010, OFFNOTECH, MELIPROMO3111... nunca apareceram em promo de perfume nos grupos,
# mas viravam sugestão de cupom no post de perfume. O SUPER15 (01/11 15:14, "em Supermercado") saiu em 13 promos de
# perfume nos grupos e você nunca usou. Também fica de fora o que só vale para parte dos perfumes pelo envio ou pelo
# comprador ("em Entregas Full", "p/ Meli+"): a rodada poria o cupom em anúncio onde ele não entra. Cada cupom do aviso
# vem num trecho que começa com 🎟️.
SECAO_FORA_DE_PERFUME = re.compile(
    r"livro|\bjogos\b|\bgames?\b|\bcasa\b|decora|\bmoveis\b|\btvs?\b|televis|celular|smartphone|\bpc\b|\btech\b|"
    r"notebook|informatica|eletro|ferrament|\bauto\b|automotiv|\bbebes?\b|infantil|brinquedo|\bpets?\b|petshop|"
    r"reforma|limpeza|constru|esporte|supermercado|\bmercado\b(?!\s*(?:livre|pago))|farmacia|meli\s*\+|meli\s*mais|assinante|"
    r"\bfull\b|meli\s*plus|veiculo|animais|internacional")
CODIGO_FORA_DE_PERFUME = re.compile(r"livro|casa|decor|tvs|televis|celular|tech|ferrament|bebe|petshop|reforma|limpeza|"
                                    r"melimais|games|full")


def trecho_do_cupom(texto: str, codigo: str) -> str:
    """O pedaço do aviso que fala deste cupom: do 🎟️ dele até o próximo (a mensagem toda, se não houver 🎟️). Um título
    que cita os códigos ("🚨 CUPONS DE HOJE: LOJAS0111 E SUPER15") não é o trecho de nenhum deles."""
    achar = re.compile(rf"(?<![A-Z0-9]){re.escape(codigo)}(?![A-Z0-9])")
    partes = re.split(r"(?=🎟)", texto or "")
    for parte in sorted(partes, key=lambda p: not p.startswith("🎟")):
        if achar.search(re.sub(r"[*_~`]", "", parte)):
            return parte
    return texto or ""


def fora_de_perfume(texto: str, codigo: str) -> str:
    """No aviso da equipe, o cupom é de outra seção do ML (livros, casa, TV...) ou só de parte dos perfumes (Full,
    Meli+)? Devolve a palavra que mostrou isso, ou "" (pode valer em perfume: Moda & Bem-Estar, Lojas Oficiais, site
    todo, ou sem seção escrita)."""
    # Com um cupom só na mensagem, ela toda fala dele: o 🎟️ no fim da linha do código (aviso de 10/10 08:12) deixaria a
    # seção da linha de baixo fora do trecho
    trecho = texto or "" if list(cupons_da_mensagem(texto)) == [codigo] else trecho_do_cupom(texto, codigo)
    trecho = re.sub(r"https?://\S+", " ", trecho.lower())
    trecho = re.sub(r"[*_~`]", " ", trecho)   # "_Entregas Full_": o "_" conta como letra para o \b
    trecho = "".join(c for c in unicodedata.normalize("NFKD", trecho) if not unicodedata.combining(c))
    achou = SECAO_FORA_DE_PERFUME.search(trecho) or CODIGO_FORA_DE_PERFUME.search(codigo.lower())
    return achou.group(0) if achou else ""


REGRA_DA_EQUIPE_S = 12 * 3600


def registrar_cupons(con: sqlite3.Connection, texto: str, ts: int, em_promo_de_perfume: bool,
                     da_equipe: bool = False) -> int:
    """Guarda os cupons da mensagem e a regra de cada um.

    10/10: o MODA1010 saiu como "até R$ 30" num grupo e "máx R$ 40" em outro e no aviso da própria equipe (era
    R$ 40). A regra que a EQUIPE escreve (`da_equipe`, aviso de cupom no GrupoFinal) vale por 12h: nesse tempo os
    outros grupos só completam o que falta (o mínimo, por exemplo), não trocam.
    """
    achados = cupons_da_mensagem(texto)
    if da_equipe:
        achados = {c: r for c, r in achados.items() if r.pct or r.valor}
        for codigo, r in achados.items():
            # 01/11: cupom de outra seção do ML (livros, casa, Meli+...) não vira "visto com perfume" (sugestão de cupom)
            perfume_ts = None if fora_de_perfume(texto, codigo) else ts
            con.execute(
                """INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, valor, minimo, limite, perfume_ts,
                                       equipe_ts)
                   VALUES (?,?,?,1,?,?,?,?,?,?)
                   ON CONFLICT(codigo) DO UPDATE SET
                     pct = excluded.pct, valor = excluded.valor, minimo = COALESCE(excluded.minimo, minimo),
                     limite = COALESCE(excluded.limite, limite), equipe_ts = excluded.equipe_ts""",
                (codigo, ts, ts, r.pct, r.valor, r.minimo, r.limite, perfume_ts, ts))
        return len(achados)
    trava = ts - REGRA_DA_EQUIPE_S   # regra escrita pela equipe depois disso: os grupos só completam
    for codigo, r in achados.items():
        con.execute(
            """INSERT INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, valor, minimo, limite, perfume_ts)
               VALUES (?,?,?,1,?,?,?,?,?)
               ON CONFLICT(codigo) DO UPDATE SET
                 ultimo_ts = MAX(ultimo_ts, excluded.ultimo_ts), vezes = vezes + 1,
                 pct = CASE WHEN COALESCE(equipe_ts, 0) > ? THEN COALESCE(pct, excluded.pct)
                            ELSE COALESCE(excluded.pct, pct) END,
                 valor = CASE WHEN COALESCE(equipe_ts, 0) > ? THEN COALESCE(valor, excluded.valor)
                              ELSE COALESCE(excluded.valor, valor) END,
                 minimo = CASE WHEN COALESCE(equipe_ts, 0) > ? THEN COALESCE(minimo, excluded.minimo)
                               ELSE COALESCE(excluded.minimo, minimo) END,
                 limite = CASE WHEN COALESCE(equipe_ts, 0) > ? THEN COALESCE(limite, excluded.limite)
                               ELSE COALESCE(excluded.limite, limite) END,
                 perfume_ts = MAX(COALESCE(perfume_ts, 0), COALESCE(excluded.perfume_ts, 0))""",
            (codigo, ts, ts, r.pct, r.valor, r.minimo, r.limite, ts if em_promo_de_perfume else None,
             trava, trava, trava, trava))
    return len(achados)


def melhor_cupom(con: sqlite3.Connection, preco: float, agora: int, excluir: set | None = None) -> dict | None:
    """O cupom ativo para perfume que mais baixa este preço, com a regra conhecida. `excluir`: esgotados."""
    melhor = None
    for c in con.execute("""SELECT codigo, pct, valor, minimo, limite, ultimo_ts FROM cupons
                            WHERE perfume_ts >= ? AND (pct IS NOT NULL OR valor IS NOT NULL)""",
                         (agora - ATIVO_S,)):
        if cupons_vencidos([c[0]], agora) or c[0] in (excluir or ()):
            continue  # "MODA0410" no dia 05/10: já venceu; ou esgotou (esgotados.py)
        d = Regra(c[1], c[2], c[3], c[4]).desconto(preco)
        if d >= 1 and (melhor is None or d > melhor["desconto"]):
            melhor = {"codigo": c[0], "desconto": round(d, 2), "preco_final": round(preco - d, 2),
                      "visto_ha_min": max(0, int((agora - c[5]) / 60))}
    return melhor


def validade_do_codigo(codigo: str, agora: float) -> "dt.date | None":
    """Cupom do ML com a data no fim do código ("MODA0610" = 06/10): vale só até esse dia. None = sem data."""
    import datetime as dt
    m = re.search(r"(\d{2})(\d{2})$", codigo or "")
    if not m:
        return None
    dia, mes = int(m.group(1)), int(m.group(2))
    hoje = dt.date.fromtimestamp(agora)
    try:
        data = dt.date(hoje.year, mes, dia)
    except ValueError:
        return None
    if (data - hoje).days > 180:      # "3112" visto em janeiro: é do ano passado
        data = data.replace(year=hoje.year - 1)
    elif (hoje - data).days > 180:    # "0201" visto em dezembro: é do ano que vem
        data = data.replace(year=hoje.year + 1)
    return data


def cupons_vencidos(codigos: list[str], agora: float) -> list[str]:
    """Os códigos com data que já passou. Só conta como vencido o que tem data no código."""
    import datetime as dt
    hoje = dt.date.fromtimestamp(agora)
    return [c for c in codigos if (v := validade_do_codigo(c, agora)) is not None and v < hoje]
