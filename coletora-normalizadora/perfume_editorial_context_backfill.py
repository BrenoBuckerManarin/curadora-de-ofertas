#!/usr/bin/env python3
"""Backfill editorial context for products missing olfactive rails.

Reads the gap report (gaps.jsonl), asks OpenCode Go (kimi-k2.5) for a compact
editorial microprofile per product, and merges into editorial_context.json.
Never overwrites existing entries. Source headlines from the tap are passed as
hints. Safe to re-run; skips done keys. Backup written before first merge.
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

CTX_FILE = Path.home() / ".hermes/perfume_image_bank/editorial_context.json"
GAPS = sorted(Path("/tmp").glob("perfume_editorial_context_gaps_*/gaps.jsonl"))[-1]
MODEL = "kimi-k2.5"
MIN_COUNT = 2  # skip one-off products; not worth a profile


def api_key() -> str:
    try:
        for raw in (Path.home() / ".hermes/.env").read_text().splitlines():
            if raw.startswith("OPENCODE_GO_API_KEY="):
                return raw.split("=", 1)[1].strip()
    except Exception:
        pass
    # No key is fine: call_llm degrades to the Hermes CLI path.
    return ""


KEY = api_key()
_HTTP_DEAD = False

PROMPT = """Você é especialista em perfumaria e marketing de achadinhos no WhatsApp brasileiro.
Produto: {product}
Headlines já usadas na fonte (pistas, podem ser ruins): {examples}

Gere o microperfil editorial. Responda SOMENTE JSON válido:
{{"olfactive_terms":["3-5 termos de família/notas em pt-br minúsculo"],
"safe_angles":["2-4 ângulos de venda seguros e específicos"],
"known_inspiration":"nome do perfume original se este for contratipo/inspiração CONHECIDA, senão string vazia",
"avoid_terms":["termos a evitar para este produto"],
"confidence":"high|medium|low"}}
Regras: known_inspiration só quando você tem certeza real (ex.: Club de Nuit->Creed Aventus). Se não conhecer o produto, confidence=low e termos genéricos plausíveis pela família do nome."""


def _call_via_cli(prompt: str) -> str:
    """Fallback via Hermes CLI (working subscription). Slower but alive."""
    import subprocess
    cmd = [
        "/home/ubuntu/.local/bin/hermes", "chat", "-Q", "--source", "tool",
        "--max-turns", "1", "--safe-mode", "-t", "", "-q", prompt,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (p.stdout or "").strip()
        # CLI prints a session_id header line before the answer.
        lines = [l for l in out.splitlines() if not l.startswith("session_id:")]
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"  ERR cli {e}", file=sys.stderr)
        return ""


def call_llm(product: str, examples: list[str]) -> dict | None:
    prompt = PROMPT.format(product=product, examples=", ".join(examples[:3]) or "(nenhuma)")
    content = ""
    global _HTTP_DEAD
    # OpenCode Go HTTP is the fast path (~2s). When that key is dead (401), fall
    # back to the Hermes CLI, which uses the working subscription (~11s/produto).
    if KEY and not _HTTP_DEAD:
        payload = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 6000,
        }).encode()
        req = urllib.request.Request(
            "https://opencode.ai/zen/go/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": "Hermes"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                d = json.load(resp)
            content = (d["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            if "401" in str(e) or "403" in str(e):
                _HTTP_DEAD = True
                print("  HTTP key dead -> switching to CLI for the rest of the run", file=sys.stderr)
            content = ""
    if not content:
        content = _call_via_cli(prompt)
    if not content:
        return None
    try:
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        try:
            obj = json.loads(content)
        except Exception:
            # kimi sometimes narrates before the JSON; grab the last {...} block.
            m = re.search(r"\{[^{}]*\"olfactive_terms\".*\}", content, re.S)
            if not m:
                return None
            obj = json.loads(m.group(0))
        if not isinstance(obj, dict) or "olfactive_terms" not in obj:
            return None
        # normalize
        out = {
            "olfactive_terms": [str(x).strip().lower() for x in obj.get("olfactive_terms") or [] if str(x).strip()][:6],
            "safe_angles": [str(x).strip() for x in obj.get("safe_angles") or [] if str(x).strip()][:5],
            "avoid_terms": [str(x).strip() for x in obj.get("avoid_terms") or [] if str(x).strip()][:6],
            "confidence": str(obj.get("confidence") or "low").lower(),
            "source": "backfill_cli_20260726",
        }
        insp = str(obj.get("known_inspiration") or "").strip()
        if insp:
            out["known_inspiration"] = insp
        return out
    except Exception as e:
        print(f"  ERR {e}", file=sys.stderr)
        return None


def main() -> int:
    ctx = json.loads(CTX_FILE.read_text()) if CTX_FILE.exists() else {}
    backup = CTX_FILE.with_suffix(f".json.bak-backfill-{time.strftime('%Y%m%d-%H%M')}")
    backup.write_text(json.dumps(ctx, ensure_ascii=False))
    gaps = []
    for line in GAPS.read_text().splitlines():
        try:
            g = json.loads(line)
        except Exception:
            continue
        key = g.get("product_key") or ""
        if not key or key == "produto" or key in ctx:
            continue
        if int(g.get("count") or 0) < MIN_COUNT:
            continue
        if not re.search(r"[a-z]{4}", key):
            continue
        gaps.append(g)
    print(f"gap file: {GAPS}\nto process: {len(gaps)} (existing ctx: {len(ctx)})")
    done = fail = 0
    for i, g in enumerate(gaps):
        key, product = g["product_key"], g.get("product") or g["product_key"]
        prof = call_llm(product, g.get("examples") or [])
        if prof:
            ctx[key] = prof
            done += 1
        else:
            fail += 1
        if (i + 1) % 10 == 0:
            CTX_FILE.write_text(json.dumps(ctx, ensure_ascii=False, indent=1, sort_keys=True))
            print(f"[{i+1}/{len(gaps)}] done={done} fail={fail}")
        time.sleep(0.4)
    CTX_FILE.write_text(json.dumps(ctx, ensure_ascii=False, indent=1, sort_keys=True))
    print(f"FINAL done={done} fail={fail} total_ctx={len(ctx)} backup={backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
