"""Fila de exceções (09/10/2026): quanto da edição do Breno ainda é necessária, por selo da normalizadora.

A meta é o Breno olhar só o ⚠️ e o ❌. Para isso o ✅ precisa ser confiável; esta conta mostra, nos posts que ele
editou, quantos ele deixou como vieram, separando pelo selo (✅/⚠️/❌) e pelo que ele mexeu (preço, headline,
imagem, título). Em 03 a 09/10, antes da segunda leitura da curadora: 51% dos posts sem mexer; no ✅, 61%.

Lê (só leitura) dois registros da coletora: o post de cada link com o selo (normalizer_feedback.jsonl) e a
comparação normalizadora × edição do Breno (editorial_comparison_cycles.jsonl).
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

LOGS = Path(os.path.expanduser("~/.hermes/perfume_image_bank/logs"))
POR = re.compile(r"[Pp]or\s+\*?R\$\s*([\d.]+(?:,\d{2})?)")
CUPOM = re.compile(r"(?i)\bcupo[mn]")
SEM_MUDANCA_HEADLINE = {"unchanged", "emoji_or_punctuation_only", None}
SEM_MUDANCA_TITULO = {"unchanged", "cleanup_only", None}
META_INTOCADOS = 0.9  # com 90% dos ✅ sem mexer, o ✅ pode ir direto


def _linhas(caminho: Path):
    if not caminho.exists():
        return
    with caminho.open(encoding="utf-8", errors="replace") as f:
        for linha in f:
            try:
                yield json.loads(linha)
            except ValueError:
                continue


def _selo(grade: str) -> str:
    return "✅" if "✅" in grade else "⚠️" if "⚠" in grade else "❌" if "❌" in grade else "sem selo"


def mudancas(ciclo: dict) -> dict[str, bool]:
    """O que o Breno mudou entre o post da normalizadora (before) e a versão final dele (after)."""
    antes, depois = (ciclo.get("before") or {}).get("copy") or "", (ciclo.get("after") or {}).get("copy") or ""
    a, b = POR.search(antes), POR.search(depois)
    return {"preço": bool(a and b and a.group(1) != b.group(1)),
            "cupom": bool(CUPOM.search(antes) and not CUPOM.search(depois)),
            "headline": (ciclo.get("headline_change") or {}).get("class") not in SEM_MUDANCA_HEADLINE,
            "título": (ciclo.get("title_change") or {}).get("class") not in SEM_MUDANCA_TITULO,
            "imagem": (ciclo.get("image_change") or {}).get("class") in ("changed", "added_in_edition")}


def medir(ini: float, fim: float, logs: Path = LOGS) -> dict:
    selos: dict[str, str] = {}
    automatico = Counter()
    for d in _linhas(logs / "normalizer_feedback.jsonl"):
        ts = d.get("ts") or 0
        if ini - 86400 <= ts < fim and d.get("link"):
            selos[d["link"]] = _selo(str(d.get("approval_grade") or ""))
        if ini <= ts < fim:
            automatico["posts"] += 1
            automatico["conferidos"] += bool(d.get("curadora"))
            automatico["cupom_tirado"] += bool(d.get("curadora_cupom"))
    total, por_selo = Counter(), {}
    for d in _linhas(logs / "editorial_comparison_cycles.jsonl"):
        if not ini <= (d.get("edition_final_ts") or 0) < fim:
            continue
        m = mudancas(d)
        intocado = not (m["preço"] or m["cupom"] or m["headline"] or m["título"] or m["imagem"])
        for c in (total, por_selo.setdefault(selos.get(d.get("link"), "sem selo"), Counter())):
            c["posts"] += 1
            c["intocados"] += intocado
            c.update({k: 1 for k, v in m.items() if v})
    return {"total": total, "por_selo": por_selo, "automatico": automatico}


def texto(m: dict, titulo: str) -> str:
    t = m["total"]
    if not t["posts"]:
        return ""
    pct = lambda c: f"{c['intocados'] / c['posts'] * 100:.0f}%"
    linhas = [f"🧾 *{titulo}*",
              f"{t['posts']} posts editados · você não mexeu em *{pct(t)}*"]
    for selo in ("✅", "⚠️", "❌"):
        c = m["por_selo"].get(selo)
        if c:
            mexeu = " · ".join(f"{k} {c[k]}" for k in ("preço", "cupom", "headline", "imagem", "título") if c[k])
            linhas.append(f"{selo} {c['posts']} posts: não mexeu em {pct(c)}" + (f" ({mexeu})" if mexeu else ""))
    a = m["automatico"]
    if a["conferidos"] or a["cupom_tirado"]:
        linhas.append(f"🧠 conferidos pela curadora: {a['conferidos']} de {a['posts']} · "
                      f"cupom esgotado tirado sozinho: {a['cupom_tirado']}")
    ok = m["por_selo"].get("✅")
    if ok and ok["posts"] >= 20:
        atingiu = ok["intocados"] / ok["posts"] >= META_INTOCADOS
        linhas.append("🎯 o ✅ já pode ir direto, sem passar por você" if atingiu else
                      f"🎯 meta: {META_INTOCADOS * 100:.0f}% dos ✅ sem mexer; aí o ✅ pode ir direto")
    return "\n".join(linhas)
