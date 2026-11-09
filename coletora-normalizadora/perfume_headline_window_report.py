#!/usr/bin/env python3
"""Perfume headline quality per window: generic-headline share before vs after the
2026-09-26 fix (accept rc!=0 with valid JSON + Anthropic fallback).

No LLM. Reads perfume_image_bank/logs/normalizer_feedback.jsonl only.
"generic" = headline_source == fallback_rule (same metric used in the diagnosis).
Baselines are recomputed from the log every run, never hardcoded.
"""
import collections
import datetime as D
import json
from pathlib import Path

LOG = Path.home() / ".hermes/perfume_image_bank/logs/normalizer_feedback.jsonl"
CHANGE = D.datetime(2026, 9, 26, 19, 47)  # code change live in the 15s loop (mtime 19:46:53)
LLM_SOURCES_PREFIX = ("llm_",)


def load():
    rows = []
    for line in LOG.open(encoding="utf-8"):
        try:
            r = json.loads(line)
            r["_t"] = D.datetime.fromtimestamp(float(r["ts"]))
            rows.append(r)
        except Exception:
            continue
    return rows


def stats(rows):
    n = len(rows)
    src = collections.Counter(r.get("headline_source") or "?" for r in rows)
    gen = src.get("fallback_rule", 0)
    llm = sum(c for s, c in src.items() if s.startswith(LLM_SOURCES_PREFIX))
    models = collections.Counter(
        (r.get("headline_model") or "").split("/")[-1] for r in rows
        if str(r.get("headline_source") or "").startswith(LLM_SOURCES_PREFIX))
    partial = sum(1 for r in rows for a in (r.get("headline_attempts") or [])
                  if isinstance(a, dict) and a.get("status") == "response_partial_rc")
    causes = collections.Counter()
    for r in rows:
        if r.get("headline_source") != "fallback_rule":
            continue
        errs = " ".join(str(a.get("error") or "") for a in (r.get("headline_attempts") or []) if isinstance(a, dict))
        if "budget" in errs:
            causes["sem tempo (orçamento)"] += 1
        elif "rc=" in errs or "HTTP" in errs:
            causes["erro de provedor"] += 1
        else:
            causes["outro"] += 1
    return {"n": n, "gen": gen, "pct": (100 * gen / n) if n else 0.0, "llm": llm,
            "models": models, "partial": partial, "causes": causes}


def line(label, s):
    if not s["n"]:
        return f"• {label}: sem posts"
    return f"• {label}: {s['gen']}/{s['n']} genéricas ({s['pct']:.0f}%)"


def main():
    rows = load()
    now = D.datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base_old = [r for r in rows if D.datetime(2026, 9, 18) <= r["_t"] < D.datetime(2026, 9, 23)]
    base_bad = [r for r in rows if D.datetime(2026, 9, 25) <= r["_t"] < CHANGE]
    after = [r for r in rows if r["_t"] >= max(CHANGE, today0)]
    a, o, b = stats(after), stats(base_old), stats(base_bad)

    out = [f"Headlines Perfume — janela {now:%d/%m} (depois do ajuste de 26/09 19:47)", ""]
    out.append(line("Agora", a))
    out.append(line("Antes, 18–22/09 (reserva OpenCode ainda vivo)", o))
    out.append(line("Antes, 25–26/09 (reserva morto)", b))
    if a["n"]:
        out.append("")
        out.append(f"Agora: {a['llm']} headlines de LLM "
                   + (", ".join(f"{m or '?'} {c}" for m, c in a["models"].most_common()) or "nenhuma")
                   + f"; {a['partial']} respostas aproveitadas que antes iam pro lixo (rc≠0).")
        if a["causes"]:
            out.append("Genéricas agora por causa: " + ", ".join(f"{k} {v}" for k, v in a["causes"].most_common()))
        verdict = ("✅ melhorou" if a["pct"] < b["pct"] - 3 else
                   "➖ sem diferença clara" if a["pct"] <= b["pct"] + 3 else "⚠️ piorou")
        out.append(f"Veredito vs 25–26/09: {verdict}"
                   + (" (amostra pequena, <20 posts)" if a["n"] < 20 else ""))
    print("\n".join(out))


if __name__ == "__main__":
    main()
