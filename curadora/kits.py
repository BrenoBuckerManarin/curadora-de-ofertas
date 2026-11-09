"""Kits: quanto sai o 100ml de perfume dentro do kit, comparado com o mesmo perfume avulso.

Só INFORMAÇÃO (pedido do Breno em 04/10): kit vende pela "ilusão" de levar mais, nem sempre é melhor; a nota não
muda. Exemplos: "Kit com 3 Floratta 30ml" = 90ml; "Combo Egeo Melancia: Colônia 90ml + Loção 400ml" = 90ml
(a loção não conta). Sem volume legível ("Kit 2 Itens") não calcula.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
import time

from .identidade import sem_acento

# Itens do kit que não são perfume: o volume deles não entra na conta.
NAO_E_PERFUME = re.compile(r"(locao|creme|hidratante|shampoo|gel|sabonete|oleo|condicionador|mascara|"
                           r"antitranspirante|desodorante(?!\s+colonia)|balm|esfoliante|refil de creme)\W*(\w+\W+){0,2}$")
QUANTIDADE = re.compile(r"\bkit\s+(?:com\s+)?(\d)\b|\b(\d)\s*(?:x|unidades|perfumes|frascos)\b")
VOLUME = re.compile(r"(\d{2,3})\s*ml\b")
# Palavras do nome do kit que não fazem parte do nome do perfume (para achar o avulso no histórico).
EXTRAS_DO_KIT = {"itens", "item", "presente", "mini", "miniatura", "locao", "hidratante", "desodorante", "creme",
                 "refil", "corporal", "gel", "sabonete", "deso", "coffret", "estojo", "combo", "caixa", "conjunto",
                 "nutritivo", "banho", "oleo", "mais", "unidades", "perfumes", "frascos"}


def ml_de_perfume(produto: str) -> float | None:
    s = sem_acento((produto or "").lower())
    volumes = []
    for m in VOLUME.finditer(s):
        antes = s[max(0, m.start() - 30):m.start()]
        if not NAO_E_PERFUME.search(antes):
            volumes.append(int(m.group(1)))
    if not volumes:
        return None
    q = QUANTIDADE.search(s)
    n = int(q.group(1) or q.group(2)) if q else 1
    return float(volumes[0] * n if len(volumes) == 1 and n > 1 else sum(volumes))


def info_kit(con: sqlite3.Connection, produto: str, preco: float, tokens, agora: float | None = None) -> dict | None:
    ml = ml_de_perfume(produto)
    if not ml or not preco:
        return None
    info = {"ml": ml, "preco_100ml": round(preco / ml * 100, 2), "avulso_100ml": None}
    nome = sorted(t for t in tokens if t not in EXTRAS_DO_KIT and not t.isdigit())
    if nome:
        desde = (agora or time.time()) - 60 * 86400
        por_volume: dict[str, list[float]] = {}
        for r in con.execute("SELECT chave, preco FROM ofertas WHERE ts >= ? AND chave LIKE ?",
                             (desde, "-".join(nome) + "|%")):
            vol = r["chave"].split("|")[1]
            if vol.isdigit():
                por_volume.setdefault(vol, []).append(r["preco"])
        if por_volume:
            vol, precos = max(por_volume.items(), key=lambda kv: len(kv[1]))
            if len(precos) >= 3:
                mediana = statistics.median(precos)
                info.update(avulso_100ml=round(mediana / int(vol) * 100, 2), avulso_volume=int(vol),
                            avulso_preco=round(mediana, 2))
    return info
