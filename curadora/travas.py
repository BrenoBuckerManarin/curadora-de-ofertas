"""Por que o pipeline não manda uma promo agora (21/10, Breno: "sim" para o painel mostrar o motivo e tirar a Excelente
travada da seção). A regra do vendedor e os motivos que o pipeline viu ao conferir no ML ficam aqui: o pipeline decide e
o painel explica com a mesma conta."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# 18/10 (Breno: "dar uma boa preferência em lojas oficiais"): quem não é loja oficial só vai com 5.000+ vendas e
# reputação que não seja fraca. 28/10 (Breno: "de preferência lojas que têm pelo menos 10 mil vendas"; escolheu o corte
# para vendedor comum): 10.000+. Loja oficial continua passando.
VENDAS_MINIMAS = 10000
VALE_S = 2 * 3600     # o motivo que o pipeline viu ao conferir no ML vale 2h no painel (e só para o mesmo preço)
GUARDA_S = 24 * 3600


def vendedor_fraco(oficial: bool, rep: dict | None) -> str:
    """O motivo para não mandar por causa de quem vende ("" se é loja oficial ou vendedor grande)."""
    if oficial:
        return ""
    rep = rep or {}
    vendas = rep.get("vendas")
    if rep.get("classe") == "fraca":
        return f"vendedor fraco ({vendas if vendas is not None else '?'} vendas, nível {rep.get('nivel') or '?'})"
    if vendas is None:
        return "vendedor sem loja oficial e sem número de vendas"
    if vendas < VENDAS_MINIMAS:
        return f"vendedor sem loja oficial com {vendas} vendas (mínimo {VENDAS_MINIMAS})"
    return ""


def guardar(caminho: Path, chave: str, motivo: str, preco: float | None, agora: float | None = None) -> None:
    """O pipeline pulou `chave` por `motivo` ao conferir no ML agora. Falha aqui nunca derruba o pipeline."""
    agora = agora or time.time()
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
    except (OSError, ValueError):
        dados = {}   # arquivo estragado: recomeça
    try:
        dados = {k: v for k, v in (dados if isinstance(dados, dict) else {}).items()
                 if isinstance(v, dict) and agora - v.get("ts", 0) <= GUARDA_S}
        dados[chave] = {"ts": int(agora), "motivo": motivo, "preco": preco}
        tmp = caminho.with_name(caminho.name + ".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, caminho)
    except (OSError, ValueError, TypeError, AttributeError):
        pass


def recentes(caminho: Path, agora: float | None = None) -> dict[str, dict]:
    """Os motivos que o pipeline gravou nas últimas 2h, por produto."""
    agora = agora or time.time()
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(dados, dict):
        return {}
    return {k: v for k, v in dados.items() if isinstance(v, dict) and agora - v.get("ts", 0) <= VALE_S}


def mil(n: float) -> str:
    """1163 -> "1,2 mil"; 267341 -> "267 mil"; 697 -> "697"."""
    if n < 1000:
        return f"{n:g}"
    if n < 10000:
        return f"{n / 1000:.1f} mil".replace(".", ",").replace(",0 mil", " mil")
    return f"{n / 1000:.0f} mil"


_CURTOS = (
    (r"sem o cupom (.+?) não vale", lambda m: f"depende do cupom {m.group(1)}"),
    (r"nem com o cupom", lambda m: "nem com o cupom da equipe fica Boa"),
    (r"vendedor sem loja oficial com (\d+) vendas", lambda m: f"vendedor com {mil(int(m.group(1)))} vendas"),
    (r"vendedor fraco", lambda m: "vendedor fraco"),
    (r"sem número de vendas", lambda m: "vendedor sem número de vendas"),
    (r"compra internacional", lambda m: "compra internacional"),
    (r"^não é perfume", lambda m: "não é perfume"),
    (r"ressalva da curadora: (.+)", lambda m: f"ressalva: {m.group(1)[:38]}"),
    (r"já foi há menos de 8h a R\$([\d.]+)", lambda m: f"já foi hoje a R${float(m.group(1)):.0f}"),
    (r"a nota caiu para (\S+ \d+)", lambda m: f"conferida no ML: caiu para {m.group(1)}"),
    (r"radar com risco", lambda m: "risco ou preço bom demais"),
)


def curto(motivo: str) -> str:
    """O motivo em poucas palavras, para o selo do card."""
    for padrao, texto in _CURTOS:
        achado = re.search(padrao, motivo or "")
        if achado:
            return texto(achado)
    return (motivo or "")[:45]
