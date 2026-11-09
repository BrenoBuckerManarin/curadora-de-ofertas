"""A página oficial de cupons dos afiliados do Mercado Livre, como sinal de "esse cupom acabou".

13/10/2026: o gerente do ML passou o link à equipe e o Breno pediu para usar ("talvez resolva nossos problemas").
Medido no histórico público da página (1.006 atualizações desde julho) contra o que ele tira dos posts:

- ela traz os cupons de afiliado: os de lista de lojas (25-30%) e o cupom do dia que vale no site todo
  (MELHORDEHJ, PROMODEHOJE, OFERTADEHOJE, CLIENTEVIP...), e TIRA o cupom quando a verba ou a validade acaba. Nos
  dois casos em que ele tirou um desses de post (CUPOMEXCLUSIVO, OPORTUNIDADEML) a página já tinha tirado horas antes;
- ela NÃO traz os cupons de campanha (MODAddmm, FULLddmm, LOJASddmm, TODOSITE...): para esses continua valendo o
  que já existe (ele tirar do post, ou um grupo avisar);
- a porcentagem dela não é confiável (mostra 25% em cupom que o aviso da própria equipe dá como 22%), e ela atrasa
  (muda 3 a 7 vezes por dia). Por isso a curadora NÃO pega regra daqui, não cria cupom e não "ressuscita" cupom.

O que ela faz: cupom que os grupos estão usando e que SAIU da página conta como esgotado, até o Breno manter o
cupom num post ou um grupo avisar que voltou. Liga em config.json -> pagina_de_cupons.ligado.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request

from .banco import gravar_meta, ler_meta
from .esgotados import registrar_sinal
from .identidade import sem_acento

URL = "https://afiliadosmercadolivre.github.io/cupons-afiliadosmercadolivre/"
META = "pagina_cupons"
INTERVALO_S = 30 * 60
MINIMO_DE_CUPONS = 20            # página com menos que isso veio quebrada: não se mexe em nada
SAIDA_EM_MASSA = 15              # mais que isso saindo de uma vez ANTES do fim da validade é defeito da página
LEITURA_VELHA_S = 12 * 3600      # sem ler a página há mais que isso, a saída não vira aviso (não se sabe quando foi)
LEMBRAR_S = 14 * 86400           # por quanto tempo vale o "saiu da página" (os códigos voltam em outras campanhas)
VISTO_NOS_GRUPOS_S = 3 * 86400   # só avisa de cupom que os grupos usaram há pouco
_cache: dict[int, tuple[float, dict]] = {}


def baixar(url: str = URL) -> str:
    pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(pedido, timeout=25) as resp:
        return resp.read(3_000_000).decode("utf-8", "replace")


def _data(texto) -> str:
    m = re.fullmatch(r"\s*(\d{2})/(\d{2})/(\d{4})\s*", str(texto or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _numero(texto) -> float | None:
    m = re.search(r"\d[\d.]*(?:,\d+)?", str(texto or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _regra(c: dict) -> dict:
    """{"pct", "minimo", "limite"} só quando a regra faz sentido (a página já trouxe "10000,00%" e "R$ 0,25")."""
    texto = str(c.get("valor_desconto") or "")
    pct, minimo, limite = _numero(texto), _numero(c.get("min_compra")), _numero(c.get("desconto_max"))
    if "%" not in texto or pct is None or not 1 <= pct <= 60:
        return {}
    return {"pct": pct, "minimo": minimo, "limite": limite}


def ler(html: str) -> dict | None:
    """{"atualizada": "13/10/2026 18:16", "cupons": {codigo: {"fim": "2026-10-31"}}}, ou None se a página não veio
    como se espera."""
    m = re.search(r"const\s+COUPONS\s*=\s*(\[.*?\n\s*\]);", html or "", re.S)
    if not m:
        return None
    try:
        lista = json.loads(m.group(1))
    except ValueError:
        return None
    cupons = {}
    for c in lista if isinstance(lista, list) else []:
        codigo = str((c or {}).get("nome") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{4,30}", codigo):
            cupons[codigo] = {"fim": _data(c.get("dia_fim")), "inicio": _data(c.get("dia_inicio")),
                              "regra": _regra(c), "lista": str(c.get("container_name") or "").startswith("aff-list"),
                              "categoria": str(c.get("categoria") or "").strip(), "acao": str(c.get("acao") or "").strip(),
                              "mar_aberto": bool(c.get("is_mar_aberto"))}
    if len(cupons) < MINIMO_DE_CUPONS:
        return None
    atualizada = re.search(r"Atualizado em\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", html)
    return {"atualizada": atualizada.group(1) if atualizada else "", "cupons": cupons}


def _estado(con: sqlite3.Connection, fresco: bool = False) -> dict:
    agora = time.time()
    guardado = _cache.get(id(con))
    if guardado and not fresco and agora - guardado[0] < 60:
        return guardado[1]
    try:
        estado = json.loads(ler_meta(con, META, "") or "{}")
    except ValueError:
        estado = {}
    _cache[id(con)] = (agora, estado)
    return estado


def aplicar(con: sqlite3.Connection, pagina: dict, agora: float | None = None) -> dict:
    """Compara a página de agora com a da última leitura e registra quem saiu. Devolve o resumo do que mudou."""
    agora = agora if agora is not None else time.time()
    estado = _estado(con, fresco=True)
    cupons = {c: dict(d) for c, d in (estado.get("cupons") or {}).items()}
    estavam = {c for c, d in cupons.items() if not d.get("saiu")}
    hoje = time.strftime("%Y-%m-%d", time.localtime(agora))
    sairam = sorted(estavam - set(pagina["cupons"]))
    antes_do_fim = [c for c in sairam if (cupons[c].get("fim") or "9999") >= hoje]
    em_massa = len(antes_do_fim) > SAIDA_EM_MASSA
    recente = agora - (estado.get("lida_em") or 0) <= LEITURA_VELHA_S
    usados = {r[0] for r in con.execute("SELECT codigo FROM cupons WHERE ultimo_ts >= ?", (agora - VISTO_NOS_GRUPOS_S,))}
    avisados = []
    for codigo in sairam:
        if em_massa and codigo in antes_do_fim:
            continue   # a página veio sem dezenas de cupons ainda válidos: defeito dela, não se mexe neles
        cupons[codigo]["saiu"] = int(agora)
        if recente and codigo in usados and registrar_sinal(
                con, codigo, int(agora), "esgotou", "pagina",
                f"saiu da página oficial de cupons do ML (atualizada em {pagina['atualizada'] or '?'})",
                f"pagina:{pagina['atualizada']}:{int(agora) // 3600}"):
            avisados.append(codigo)
    # 03/11: o indício que a própria página deu (o cupom sumiu dela) não expira mais com o tempo (esgotados.esgotados);
    # ele sai quando o cupom volta para a página. A sua retirada e o aviso de grupo ou da equipe não saem com isso
    # (test_a_pagina_nunca_ressuscita_cupom; 04/11, Astra). 04/11: a volta é vista pelo sinal (o último da página para o
    # código é o "esgotou"), e não pelo estado da página, que esquece o "saiu" depois de 14 dias (LEMBRAR_S).
    ultimo_da_pagina = dict(con.execute(
        "SELECT codigo, tipo FROM cupom_sinais s WHERE fonte = 'pagina' AND id = "
        "(SELECT max(id) FROM cupom_sinais WHERE fonte = 'pagina' AND codigo = s.codigo)").fetchall())
    for codigo in sorted(c for c in pagina["cupons"] if ultimo_da_pagina.get(c) == "esgotou"):
        registrar_sinal(con, codigo, int(agora), "ativo", "pagina",
                        f"voltou para a página oficial de cupons do ML (atualizada em {pagina['atualizada'] or '?'})",
                        f"pagina-volta:{pagina['atualizada']}:{int(agora) // 3600}")
    for codigo, dados in pagina["cupons"].items():
        cupons[codigo] = {"fim": dados.get("fim") or "", "saiu": 0, "inicio": dados.get("inicio") or "",
                          "regra": dados.get("regra") or {}, "lista": bool(dados.get("lista")),
                          "categoria": dados.get("categoria") or "", "acao": dados.get("acao") or "",
                          "mar_aberto": bool(dados.get("mar_aberto")),
                          "visto": (cupons.get(codigo) or {}).get("visto") or int(agora)}
    # O cupom do dia precisa da regra na tabela de cupons (é dela que sai o preço com cupom); a do aviso da equipe vale mais.
    for codigo, d in cupons.items():
        r = d.get("regra") or {}
        if not d.get("saiu") and _do_dia(d, hoje) and r:
            con.execute("INSERT OR IGNORE INTO cupons (codigo, primeiro_ts, ultimo_ts, vezes, pct, minimo, limite) "
                        "VALUES (?,?,?,0,?,?,?)", (codigo, int(agora), int(agora), r["pct"], r["minimo"], r["limite"]))
            con.execute("UPDATE cupons SET pct = ?, valor = NULL, minimo = ?, limite = ? WHERE codigo = ? AND equipe_ts IS NULL",
                        (r["pct"], r["minimo"], r["limite"], codigo))
    cupons = {c: d for c, d in cupons.items() if not d.get("saiu") or agora - d["saiu"] <= LEMBRAR_S}
    gravar_meta(con, META, json.dumps({"lida_em": int(agora), "atualizada": pagina["atualizada"], "cupons": cupons}))
    con.commit()
    _cache.pop(id(con), None)
    return {"na_pagina": len(pagina["cupons"]), "sairam": [c for c in sairam if cupons.get(c, {}).get("saiu")],
            "avisados": avisados, "em_massa": em_massa}


def fora_da_pagina(con: sqlite3.Connection, agora: float, codigos: list[str]) -> dict[str, str]:
    """Dos `codigos`, os que saíram da página oficial e ninguém confirmou depois: {codigo: motivo}. Quem desfaz é o
    Breno (manteve ou pôs o cupom num post), um grupo avisando que voltou ou, desde 01/11, o aviso de cupom da equipe no
    GrupoFinal (é ele que liga a rodada: esgotados.avisos_com_prova)."""
    if not codigos:
        return {}
    cupons = _estado(con).get("cupons") or {}
    saiu = {c: cupons[c]["saiu"] for c in codigos
            if c in cupons and cupons[c].get("saiu") and 0 <= agora - cupons[c]["saiu"] <= LEMBRAR_S}
    if not saiu:
        return {}
    marcas = ",".join("?" * len(saiu))
    for codigo, ts in con.execute(f"SELECT codigo, MAX(ts) FROM cupom_sinais WHERE tipo IN ('ativo', 'posto', 'aviso') AND "
                                  f"ts <= ? AND codigo IN ({marcas}) GROUP BY codigo", [int(agora), *saiu]).fetchall():
        if ts and ts > saiu.get(codigo, 0):
            saiu.pop(codigo, None)
    # 04/11 (Astra): sumir da página é indício, não aviso de que acabou
    return {c: "indício: saiu da página oficial de cupons do ML em "
               + time.strftime("%d/%m às %H:%M", time.localtime(ts)) for c, ts in saiu.items()}


def _do_dia(d: dict, hoje: str) -> bool:
    """Cupom do dia: vale só hoje (início = fim = hoje), não é de lista de lojas e vale para perfume."""
    return d.get("inicio") == d.get("fim") == hoje and not d.get("lista") and vale_para_perfume(d)


# 17/10 (Breno: "ele mandou uma promo com um cupom que não está funcionando, da onde tirou isso?"): o OFFNOMELIHOJE era
# cupom do dia SÓ de "Tecnologia e Eletrodomésticos" (campos categoria/acao da página) e a curadora pôs no Yara. A página
# diz a categoria e se o cupom vale no site todo (is_mar_aberto). O cupom do dia só entra sozinho se valer no site todo
# ou numa categoria de beleza/perfumaria; sem isso (outra categoria, ou a página não diz) vale só com a confirmação de
# sempre: o Breno manter num post, ou 2 posts no GrupoFinal com ele (esgotados.quentes).
PARA_PERFUME = re.compile(r"beleza|perfum|cuidado pessoal|cosmet|fragranc")


def vale_para_perfume(d: dict) -> bool:
    if d.get("mar_aberto"):
        return True
    return bool(PARA_PERFUME.search(sem_acento(f"{d.get('categoria') or ''} {d.get('acao') or ''}".lower())))


CUPOM_DO_DIA_MAX_PCT = 20          # o cupom do dia é de 8% a 20%; mais que isso é leitura errada da página
LEITURA_RECENTE_S = 2 * 3600       # a página tem de ter sido lida há pouco (ela mesma atrasa)


def cupons_do_dia(con: sqlite3.Connection, agora: float, ate: str = "20:00") -> dict[str, dict]:
    """14/10 (Breno: "sim pode ligar"): o cupom do dia dos afiliados que a página oficial mostra AGORA, com regra
    que faz sentido (8-20%, com mínimo e teto), até a hora `ate` (pela página, ele costuma acabar entre 18h e 20h).
    {codigo: {"pct", "minimo", "limite", "desde"}}. Esgotado (alguém tirou, grupo avisou, saiu da página) fica de fora
    por quem chama (score._melhor_quente olha os esgotados)."""
    estado = _estado(con)
    if not estado or agora - (estado.get("lida_em") or 0) > LEITURA_RECENTE_S:
        return {}
    if time.strftime("%H:%M", time.localtime(agora)) >= (ate or "23:59"):
        return {}
    hoje = time.strftime("%Y-%m-%d", time.localtime(agora))
    saida = {}
    for codigo, d in (estado.get("cupons") or {}).items():
        r = d.get("regra") or {}
        if (not d.get("saiu") and _do_dia(d, hoje) and r and 5 <= r.get("pct", 0) <= CUPOM_DO_DIA_MAX_PCT
                and (r.get("minimo") or 0) >= 20 and (r.get("limite") or 0) > 0):
            saida[codigo] = dict(r, desde=d.get("visto") or estado.get("lida_em"))
    return saida


class Leitor:
    """Lê a página de tempos em tempos, dentro do serviço da curadora."""

    def __init__(self, con: sqlite3.Connection, baixar_=baixar, intervalo_s: float = INTERVALO_S):
        self.con, self.baixar, self.intervalo_s = con, baixar_, intervalo_s
        self.ultima: dict = {}

    def uma_vez(self, agora: float | None = None) -> dict | None:
        pagina = ler(self.baixar())
        if pagina is None:
            print("[cupons oficiais] a página não veio como se espera: nada foi mexido", flush=True)
            return None
        resumo = aplicar(self.con, pagina, agora)
        if resumo["em_massa"]:
            print("[cupons oficiais] a página veio sem dezenas de cupons ainda válidos: ignorei a saída deles", flush=True)
        if resumo["avisados"]:
            print(f"[cupons oficiais] saíram da página e contam como esgotados: {', '.join(resumo['avisados'])}", flush=True)
        self.ultima = dict(resumo, atualizada=pagina["atualizada"], lida_em=int(agora or time.time()))
        return resumo

    def rodar(self, parar=lambda: False) -> None:
        while not parar():
            try:
                self.uma_vez()
            except sqlite3.OperationalError as exc:
                self.con.rollback()
                print(f"[cupons oficiais] banco ocupado, tento na próxima: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001 - a página fora do ar não derruba nada
                print(f"[cupons oficiais] não consegui ler agora: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(self.intervalo_s)
