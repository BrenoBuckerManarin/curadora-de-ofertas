#!/usr/bin/env python3
"""Deterministic WhatsApp Perfume Manual Normalizer.

Dedicated narrow pipeline:
- watches only one WhatsApp group configured in PERFUME_NORMALIZER_GROUP
- reads raw promo messages from the WhatsApp tap jsonl
- validates link/price-ish facts via Jina when possible
- returns only the normalized WhatsApp copy to the same group
- no Hermes agent, no skills, no session history, no browser
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from perfume_identity_guard import gender as identity_gender, preserve_gender, compatible as identity_compatible, headline_gender_conflict, gender_safe_headline, fakhar_variant_conflict
from perfume_identity_guard import file_digest, fingerprint, human_final_records
from perfume_round_guards import (kit_signature, kit_identity_matches, image_identity_hold,
                                  headline_budget, remaining_headline_seconds)

from perfume_offer_engine import Evidence as ShadowEvidence
from perfume_offer_engine import SourceOffer as ShadowSourceOffer
from perfume_offer_engine import collect_source_prices as shadow_collect_source_prices
from perfume_offer_engine import reconcile_offer as shadow_reconcile_offer

TAP_FILE = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
STATE_FILE = Path(os.path.expanduser("~/.hermes/whatsapp/taps/.perfume_manual_normalizer_state.json"))
GUARDED_SENDER = os.path.expanduser("~/.hermes/scripts/whatsapp_send_guarded.py")
RULES_FILE = Path("/home/ubuntu/brain/ops/perfume-normalizer-runtime-rules.md")
IMAGE_BANK_PRODUCTS = Path(os.path.expanduser("~/.hermes/perfume_image_bank/products"))
IMAGE_BANK_ALIASES = Path(os.path.expanduser("~/.hermes/perfume_image_bank/aliases.json"))
IMAGE_PREFERENCES_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/image_preferences.json"))
IMAGE_USAGE_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/image_rotation_usage.json"))
EDITORIAL_CONTEXT_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/editorial_context.json"))
PREPARED_IMAGE_DIR = Path(os.path.expanduser("~/.hermes/perfume_image_bank/prepared_send"))
IMAGE_TIER_DIRS = ("high_quality", "approved_final", "acceptable")
REJECTED_IMAGE_DIRS = ("rejected", "rejected_template_border_low_quality")
_IMAGE_METADATA_CACHE: dict[tuple, list[str]] = {}
_IMAGE_ROTATION_LOCK = threading.Lock()
_IMAGE_RESERVED: set[str] = set()
DEFAULT_GROUP = ""  # set via env/config once the group JID is known
CONFIG_FILE = Path(os.path.expanduser("~/.hermes/whatsapp/perfume_manual_normalizer.env"))
ML_CARD_PROBE = Path(os.path.expanduser("~/.hermes/scripts/ml_social_card_probe.js"))
ML_CARD_PROBE_DIR = Path(os.path.expanduser("~/.hermes/runtime/ml_card_probe"))
MAX_AGE_SECONDS = 6 * 60 * 60
LAST_HEADLINE_SOURCE = ""
LAST_HEADLINE_REASON = ""
# Discarded-options telemetry. Without this, a bad published headline is
# indistinguishable between "the LLM produced five duds" and "the LLM produced
# a good one and the ranker killed it" — opposite diagnoses, opposite fixes.
LAST_HEADLINE_OPTIONS: list[dict] = []
LAST_HEADLINE_JUDGE_REASON = ""
LAST_HEADLINE_MODEL = ""
# Requests run in parallel; state is snapshotted at the end of each request for
# diagnostics. Do not serialize the whole headline lane: that created queue
# delays larger than the LLM deadline and published title-only cards.
_HEADLINE_LLM_SLOTS = threading.BoundedSemaphore(3)
_HEADLINE_DIAGNOSTICS = threading.local()


def headline_diagnostics() -> dict:
    """Request-local snapshot taken before the headline lane is released."""
    return dict(getattr(_HEADLINE_DIAGNOSTICS, "value", {}))


def _capture_headline_diagnostics() -> None:
    _HEADLINE_DIAGNOSTICS.value = {
        "attempts": list(getattr(_HEADLINE_DIAGNOSTICS, "attempts", [])),
        "source": LAST_HEADLINE_SOURCE,
        "reason": LAST_HEADLINE_REASON,
        "options": list(LAST_HEADLINE_OPTIONS),
        "judge_reason": LAST_HEADLINE_JUDGE_REASON,
        "model": LAST_HEADLINE_MODEL,
    }

RAW_HEADLINE_BLOCKLIST = {
    "quem tava esperando chegou", "pra quem tava esperando", "tava sumido", "preco", "precinho",
    "ta valendo muito", "oportunidade demais", "nao deixe passar", "adm ta de olho nesse",
    "baixa", "baixou", "promo relampago", "corre", "imperdivel",
}

BRAND_ALIASES = {
    "boticario": "O Boticário",
    "o boticario": "O Boticário",
    "natura": "Natura",
    "carolina herrera": "Carolina Herrera",
    "ch": "Carolina Herrera",
    "azzaro": "Azzaro",
    "ralph lauren": "Ralph Lauren",
    "lipx": "Lipx",
    "kid's life": "KID'S LIFE",
    "kids life": "KID'S LIFE",
    "klassey": "Klassey",
    "beleza na web": "Beleza na Web",
    "vanvic": "Vanvic",
    "rabanne": "Rabanne",
    "paco rabanne": "Rabanne",
    "rasasi": "Rasasi",
    "banderas": "Banderas",
    "antonio banderas": "Banderas",
}

DISTINCTIVE_FAMILY_VARIANT_TOKENS = {
    "black", "gold", "pure", "magnetic", "icon", "bleu", "blue", "ultra", "signature",
    "aero", "urbe", "oceano", "pulso", "aventura", "k", "max", "kmax",
    "sentir", "unico", "único", "exclusivo", "oud", "atrai", "intenso", "tradicional",
    "sagaz", "dom", "essence", "coragio", "coragem", "evolutio", "neo", "verum", "absoluto",
    "red", "flores", "secretas", "rose", "flower", "cardamom", "bourbon",
    "bomb", "vanilla", "choc", "voyage", "safran", "malibu", "elixir",
}

CANONICAL_FAMILY_EXPANSIONS = {
    # family token -> (brand/prefix expansions, default variant aliases)
    "malbec": (["o-boticario", "boticario"], ["tradicional"]),
    "kaiak": (["natura"], []),
    "essencial": (["natura"], []),
    "egeo": (["o-boticario", "boticario"], []),
    "zaad": (["o-boticario", "boticario"], []),
    "floratta": (["o-boticario", "boticario"], []),
    "coffee": (["o-boticario", "boticario"], []),
    "quasar": (["o-boticario", "boticario"], []),
    "the-blend": (["o-boticario", "boticario"], []),
    "club-6": (["eudora"], []),
}
PRODUCT_NOISE_TOKENS = {
    # Category/concentration/SEO tokens: useful in copy, weak for image lookup.
    "perfume", "deo", "de", "eau", "toilette", "parfum", "colonia", "colônia", "edp", "edt", "para",
    "compartilhavel", "compartilhável", "compartilhvel", "unissex", "spray", "vaporizador",
    "masculino", "feminino", "original", "importado", "importada",
    # Seller/store tokens must never become product identity. They caused misses
    # like "Árabe Asad Bourbon Lipx 100ml" -> no approved_final.
    "loja", "oficial", "vendido", "vendedor", "verificado", "verificada", "mercado",
    "livre", "meli", "ml", "lipx", "lumen", "lúmen", "vanvic", "kids", "life",
    "phtech", "rocha", "importados", "seu", "perfumaria", "universo", "presentes",
    "gicadex", "ravistoresp", "emporium", "fragrance", "sieno", "klassey",
    # 2026-08-02: "Vulcan Sable French Avenue Veoquiero EDP 100ml" keyed as
    # vulcan-sable-french-avenue-veoquiero-100ml and missed vulcan-sable-100ml
    # (3 approved). Seller name in the MIDDLE of the title, so the trailing-only
    # _SELLER_NOISE_RE never caught it.
    "veoquiero",
    # 2026-08-02: marketplaces/lojas que vazavam para o título público
    # ("Perfume Asad 100ml Shopee" -> "Asad Shopee 100ml"). Nome de canal de
    # venda nunca é identidade do produto.
    "shopee", "amazon", "magalu", "magazine", "americanas", "shein",
    "aliexpress", "netshoes", "centauro", "casasbahia",
    # 2026-07-07: logistics/assurance junk that leaked into bank keys, e.g.
    # "arabe-rayhaan-wolf-com-nf-e-100ml". Never product identity.
    "nf", "nfe", "com", "lacrado", "lacrada", "novo", "nova", "pronta", "entrega",
    "envio", "imediato", "imediata", "garantia", "autentico", "autêntico", "selado",
    # 2026-08-02: "desodorante" is a product CATEGORY (deo colônia / desodorante
    # colônia), never identity. "Clash Desodorante Colônia 100ml" keyed as
    # clash-desodorante-100ml and matched nothing, while o-boticario-clash-100ml
    # held 7 approved photos. "deo" was already stripped; this is the same word.
    "desodorante",
}

_SELLER_NOISE_TOKENS = sorted(
    (
        re.escape(x) for x in PRODUCT_NOISE_TOKENS
        if x not in {"perfume", "deo", "de", "eau", "parfum", "colonia", "colônia", "edp", "edt", "masculino", "feminino", "original", "arabe", "árabe"}
    ),
    key=len,
    reverse=True,
)
# "ml" doubles as a seller token (Mercado Livre) and as the volume unit. Since
# this regex is anchored at the END of the title, a perfectly normal seller
# title written as "Afnan 9am Dive Edp 100 ML" had its unit eaten, the volume
# regex (\d+\s*ml) then found nothing, and the published name became "Afnan 9am
# Dive 100 EDP" — an orphan number with no volume. Volume is SKU identity and
# must survive (Breno, 2026-08-02). Require that the trailing seller token is
# NOT preceded by a number.
_SELLER_NOISE_RE = re.compile(
    r"(?i)(?<!\d)(?<!\d\s)\b(?:" + "|".join(_SELLER_NOISE_TOKENS) + r")\b$"
)


def sanitize_product_text(product: str) -> str:
    """Remove seller/store contamination while preserving perfume identity."""
    text = re.sub(r"\s+", " ", strip_wa(product or "")).strip(" -–—,")
    # Recurring ML catalog typo: "Tuma da Mônica". Leaving it untouched splits
    # one SKU into a fake key, misses approved_final folders and then makes the
    # headline model repeat the typo publicly.
    text = re.sub(r"(?i)\btuma\s+da\s+m[oô]nica\b", "Turma da Mônica", text)
    text = re.sub(r"(?i)\bárebe\b", "Árabe", text)
    # Duplicate house slogan, not the distinct Lattafa Pride product line.
    if re.search(r"(?i)\blattafa\s+pride\s+of\s+lattafa\b", text):
        text = re.sub(r"(?i)\blattafa\s+pride\s+of\s+lattafa\b", "Lattafa", text)
    # Remove dangling separator before a concentration, not kit separators.
    text = re.sub(r"\s+/\s+(?=(?:EDP|EDT|EDC)\b)", " ", text)
    if not text:
        return ""
    text = re.sub(r"(?i)\b(?:loja|vendido\s+por|vendedor|selecione\s+(?:a\s+)?loja)\b.*$", "", text).strip(" -–—,")
    text = re.sub(r"(?i)\b(?:no\s+ML|na\s+ML|no\s+Mercado\s+Livre|Mercado\s+Livre|no\s+MELI|na\s+MELI|MELI)\b", "", text)
    # Marketplace names anywhere in the title, not just at the end. The
    # trailing-only _SELLER_NOISE_RE left "Lattafa Asad Shopee 100ml" and
    # "Egeo Magalu 90ml" intact because the volume came after the channel name
    # (Breno, 2026-08-02). A sales channel is never product identity.
    text = re.sub(
        r"(?i)\s*\b(?:shopee|amazon|magalu|magazine\s+luiza|americanas|shein|"
        r"aliexpress|netshoes|centauro|casas\s+bahia)\b\s*",
        " ", text,
    )
    text = re.sub(r"(?i)\b(?:Loja\s+Oficial|Oficial)\b", "", text)
    # Marketplace SEO/legal noise should never leak into the public product title.
    text = re.sub(r"(?i)\b(?:com\s+nota\s+fiscal|nota\s+fiscal|com\s+nf\s*-?\s*e?|c\s*/\s*nf\s*-?\s*e?)\b", "", text)
    text = re.sub(r"(?i)\bnf\s*-?\s*e?\b", "", text)
    # Escaped catalog separators and launch/batch years are listing metadata,
    # not SKU identity. If they survive here they poison title, context and bank
    # keys (Asad Elixir became asad-elixir-100ml-2026 and missed its context).
    # The escaped slash is sometimes glued to the next token (`u002F4un`), so a
    # trailing word boundary does not exist. Strip the escape itself and retain
    # the quantity that follows.
    text = re.sub(r"(?i)\bu?002f|\bc\s*[\\/]+", " ", text)
    text = re.sub(r"(?i)\b20\d{2}\b", " ", text)
    # Arabic script, promo-noise words and common typos leak from source titles.
    text = re.sub(r"[\u0600-\u06FF\u0750-\u077F]+", "", text)
    text = re.sub(r"(?i)\b(?:lacrad[oa]?|lacrado(?:\s+e\s+original)?|original\s+lacrado|100%\s*original|pronta\s+entrega|envio\s+(?:imediato|r[aá]pido)|frete\s+gr[aá]tis|promo[cç][aã]o|oferta|barato|lan[cç]amento|genu[ií]no)\b", "", text)
    # Price slogans appended by sellers are offer copy, never product identity.
    text = re.sub(
        r"(?i)\s*\bde\s*(?:r\$\s*)?\d+(?:[.,]\d{1,2})?\s+"
        r"(?:pra|para|por)\s*(?:r\$\s*)?\d+(?:[.,]\d{1,2})?.*$",
        "", text,
    )
    text = _SELLER_NOISE_RE.sub("", text).strip(" -–—,")
    text = re.sub(r"\s+", " ", text).strip(" -–—,")
    # Dangling connectives left by noise removal ("Kmax 100ml Oferta no Meli"
    # -> "Kmax 100ml no") polluted product keys with junk suffixes like
    # natura-kmax-100ml-no, breaking image-bank lookup (51 of 151 misses on
    # 2026-07-09 audit). Strip trailing stopwords repeatedly.
    while True:
        new = re.sub(r"(?i)\s+(?:no|na|nos|nas|em|de|da|do|das|dos|com|por|para|pra|a|o|e)$", "", text)
        if new == text:
            break
        text = new
    return text.strip(" -–—,")


def load_env_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_FILE.exists():
        for raw in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def normal_chat_id(value: str) -> str:
    cid = (value or "").strip()
    if cid and "@" not in cid and cid.isdigit():
        cid += "@g.us"
    return cid.lower()


def config_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or load_env_config().get(name) or default


def target_group() -> str:
    return normal_chat_id(config_value("PERFUME_NORMALIZER_GROUP", DEFAULT_GROUP))


def browser_social_card_probe(link: str | None, expected_product: str = "") -> dict:
    """Render the exact ML social featured card in real Chrome; screenshots expire in 24h."""
    if not link or "meli.la/" not in link or not ML_CARD_PROBE.exists():
        return {}
    ML_CARD_PROBE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 60 * 60
    for old in ML_CARD_PROBE_DIR.glob("*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except Exception:
            pass
    env = os.environ.copy()
    # Durable dependency path: /tmp was erased during disk cleanup, silently
    # disabling the real-Chrome card probe when Node could no longer import Playwright.
    env["NODE_PATH"] = "/home/ubuntu/.hermes/ml-card-probe-deps/node_modules"
    env["ML_CARD_PROBE_DIR"] = str(ML_CARD_PROBE_DIR)
    attempts: list[dict] = []
    best: dict = {}
    for attempt in range(1, 3):
        started = time.monotonic()
        try:
            run = subprocess.run(
                ["xvfb-run", "-a", "node", str(ML_CARD_PROBE), link],
                text=True, capture_output=True, timeout=110, env=env,
            )
            lines = [x for x in (run.stdout or "").splitlines() if x.strip()]
            data = json.loads(lines[-1]) if lines else {}
            if not isinstance(data, dict):
                data = {}
            missing = [name for name in ("title", "current_price", "screenshot") if not data.get(name)]
            identity_mismatch = bool(
                data.get("title") and expected_product and expected_product != "Produto"
                and (
                    (_tokens_overlap(expected_product, str(data["title"])) < 0.45
                     and identity_token_conflict(expected_product, str(data["title"])))
                    or source_variant_lost(expected_product, str(data["title"]))
                    or (product_variant_conflict(expected_product, str(data["title"]))
                        and not same_product_except_volume(expected_product, str(data["title"])))
                )
            )
            status = "identity_conflict" if identity_mismatch else ("ok" if data.get("ok") and not missing else (
                "probe_error" if data.get("error") else "page_not_rendered"
            ))
            attempts.append({
                "attempt": attempt,
                "seconds": round(time.monotonic() - started, 4),
                "status": status,
                "missing": missing,
                "returncode": run.returncode,
                "error": str(data.get("error") or "")[:300],
            })
            # Keep the richest exact-card evidence even when it is partial.
            if sum(bool(data.get(k)) for k in ("title", "current_price", "seller", "official_store")) > sum(
                bool(best.get(k)) for k in ("title", "current_price", "seller", "official_store")
            ):
                best = data
            if status == "ok":
                data["_probe_attempts"] = attempts
                return data
        except subprocess.TimeoutExpired:
            attempts.append({"attempt": attempt, "seconds": round(time.monotonic() - started, 4), "status": "probe_timeout", "missing": [], "returncode": None, "error": "timeout"})
        except (json.JSONDecodeError, ValueError) as exc:
            attempts.append({"attempt": attempt, "seconds": round(time.monotonic() - started, 4), "status": "parser_error", "missing": [], "returncode": None, "error": str(exc)[:300]})
        except Exception as exc:
            attempts.append({"attempt": attempt, "seconds": round(time.monotonic() - started, 4), "status": "probe_error", "missing": [], "returncode": None, "error": f"{type(exc).__name__}: {exc}"[:300]})
    best["_probe_attempts"] = attempts
    best["_probe_failed"] = True
    return best


def browser_card_commercially_authoritative(card: dict) -> bool:
    """Trust commercial facts visibly rendered on the initial featured card.

    A later click may land on account-verification, but that does not erase the
    title/price already visible in the retained screenshot. Seller/store still
    require their own scoped badge/name evidence.
    """
    if not card:
        return False
    return bool(
        str(card.get("title") or "").strip()
        and isinstance(card.get("current_price"), (int, float))
        and str(card.get("screenshot") or "").strip()
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen_ids": [], "last_position": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def msg_ts(raw) -> int | None:
    if isinstance(raw, dict):
        raw = raw.get("low")
    try:
        ts = int(raw)
    except Exception:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def collect_new_messages(group: str) -> list[dict]:
    state = load_state()
    seen = set(state.get("seen_ids", []))
    last_pos = int(state.get("last_position", 0) or 0)
    out: list[dict] = []
    if not TAP_FILE.exists():
        return out
    with TAP_FILE.open(encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size < last_pos:
            last_pos = 0
            seen = set()
        f.seek(last_pos)
        for line in f:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = str(msg.get("messageId") or "")
            if not mid or mid in seen:
                continue
            chat = normal_chat_id(str(msg.get("chatId") or ""))
            if chat != group:
                continue
            if msg.get("fromMe"):
                seen.add(mid)
                continue
            ts = msg_ts(msg.get("timestamp"))
            if ts and time.time() - ts > MAX_AGE_SECONDS:
                seen.add(mid)
                continue
            body = (msg.get("body") or "").strip()
            # Do not mark empty incoming messages as seen before handling them.
            # Baileys sometimes emits an undecryptable placeholder with body="";
            # the dedicated group should get a small actionable diagnostic instead
            # of silently swallowing Breno's test.
            out.append(msg)
            # 2026-07-08: do NOT add to seen here. Marking messages seen before
            # they are processed silently dropped promos whenever main() only
            # handled part of the batch. mark_processed() persists after send.
        end_pos = f.tell()
    # Only advance the file cursor when there is nothing pending: pending
    # messages must be re-readable on the next run (seen_ids dedupes the ones
    # already handled). Otherwise a crash mid-batch would lose promos.
    if not out:
        state["last_position"] = end_pos
    state["seen_ids"] = list(seen)[-2000:]
    save_state(state)
    return out


def mark_processed(mid: str) -> None:
    """Persist a message id as seen only after it was actually handled."""
    if not mid:
        return
    state = load_state()
    seen = state.get("seen_ids", [])
    if mid not in seen:
        seen.append(mid)
    state["seen_ids"] = seen[-2000:]
    save_state(state)


def strip_wa(text: str) -> str:
    return re.sub(r"[_*~`]", "", text or "")


def money_norm(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().replace(" ", "")
    s = s.replace("R$", "").replace("r$", "")
    if not s:
        return None
    # Breno wants promo copy without cents. Drop decimal cents instead of
    # printing ugly ML/Jina values like R$ 298,32.
    m = re.match(r"^([0-9]{1,3}(?:\.[0-9]{3})*|[0-9]+)(?:,[0-9]{1,2})?$", s)
    if m:
        s = m.group(1)
    return "R$ " + s


def money_value(raw: str | None) -> float | None:
    if not raw:
        return None
    s = str(raw).replace("R$", "").replace("r$", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def extract_link(body: str) -> str | None:
    m = re.search(r"https?://\S+", body or "")
    return m.group(0).rstrip(".,;)>") if m else None


def canonicalize_known_product_name(product: str) -> str:
    """Collapse marketplace SEO titles into the real searchable perfume name."""
    source = strip_wa(product or "")
    raw = sanitize_product_text(product)
    lo = source.casefold()
    volume = ""
    # Capture volume before sanitize_product_text can drop a standalone "ml".
    m_vol = re.search(r"(?i)\b(\d{2,3})\s*ml\b", source)
    if m_vol:
        volume = f" {m_vol.group(1)}ml"
    if "his confession" in lo:
        return f"Lattafa His Confession EDP{volume or ' 100ml'}"
    # ML occasionally scrambles this title as "Lattafa Elixir Asad Elixir Masc".
    # The repeated Elixir and gender abbreviation are marketplace noise; preserve
    # the exact Asad Elixir flanker and make it resolve to its curated bank folder.
    if re.search(r"(?i)\b(?:lattafa|lataffa)\b", source) and re.search(r"(?i)\basad\b", source) and re.search(r"(?i)\belixir\b", source):
        return f"Lattafa Asad Elixir EDP{volume or ' 100ml'}"
    if "eudora" in lo and "pulse boost" in lo:
        return f"Eudora Pulse Boost{volume or ' 100ml'}"

    if re.search(r"(?i)\bkhamrah\b", source) and not re.search(
        r"(?i)\b(qahwa|dukhan|waha|smoke|coffee)\b", source
    ):
        return f"Lattafa Khamrah EDP{volume or ' 100ml'}"
    if "azzaro" in lo and "the most wanted" in lo:
        concentration = "Parfum" if "parfum" in lo else "EDP" if "edp" in lo or "eau de parfum" in lo else "EDT" if "edt" in lo or "eau de toilette" in lo else ""
        variant = "Intense" if "intense" in lo else ""
        return re.sub(r"\s+", " ", f"Azzaro The Most Wanted {variant} {concentration}{volume}").strip()
    # ML/source titles duplicate the brand or prepend editorial copy:
    # "DEFINIÇÃO DE RAIZ, Azzaro Pour Homme 50ml" and
    # "... Azzaro - Azzaro Pour Homme 50 Ml" are the same traditional SKU.
    if "azzaro" in lo and "pour homme" in lo and not re.search(
        r"(?i)\b(chrome|sport|wanted|intense|elixir|parfum|night|wild)\b", source
    ):
        return f"Azzaro Pour Homme{volume or ''}".strip()
    return ""


def strip_non_identity_attributes(text: str) -> str:
    """Remove catalog usage attributes without touching SKU/variant/volume."""
    value = strip_wa(text or "")
    value = re.sub(
        r"(?i)\b(?:para\s+(?:masculino|feminino)|unissex|compartilh[aá]?vel|compartilhvel)\b",
        " ", value,
    )
    # Standalone marketplace connector ("Khamrah By EDP") is title noise, not
    # a flanker. Keeping it produced key ``khamrah-by-100ml`` and hid the exact
    # approved ``lattafa-khamrah-100ml`` bank folder. This does not erase the
    # actual brand token in names such as By Kilian.
    value = re.sub(r"(?i)\b(?:uso\s+adulto|spray|vaporizador|by)\b", " ", value)
    # Marketplace promo SEO glued to titles: "... Milestone Oferta Exclusiva
    # 105ml". Never identity, and it poisons product_key/image lookup.
    value = re.sub(
        r"(?i)\b(?:oferta\s+(?:exclusiva|especial|imperd[ií]vel)|"
        r"promo(?:[cç][aã]o)?\s+(?:exclusiva|especial)|"
        r"frete\s+gr[aá]tis|envio\s+r[aá]pido|pronta\s+entrega|"
        r"lan[cç]amento|novidade|super\s+oferta|mega\s+oferta)\b",
        " ", value,
    )
    value = re.sub(
        r"(?i)\b(?:masculino|feminino|importad[oa]|original|com\s+selo(?:\s+hologr[aá]fico)?)\b",
        " ", value,
    )
    return re.sub(r"\s+", " ", value).strip(" -–—,")


def canonical_perfume_type(text: str) -> str:
    """Return an explicitly evidenced concentration/presentation in house style.

    This is deliberately lexical: it may canonicalize a phrase present in the
    title, but it must not infer concentration from brand or product family.
    Longer/more specific forms win so ``Deo Parfum`` is not reduced to
    ``Parfum`` and ``Desodorante Colônia`` is not reduced to ``Colônia``.
    """
    value = strip_wa(text or "")
    patterns = (
        (r"\bextrait\s+de\s+parfum\b", "Extrait de Parfum"),
        (r"\bdeo(?:dorante)?\s+parfum\b", "Deo Parfum"),
        (r"\bdesodorante\s+col[oô]nia\b", "Desodorante Colônia"),
        (r"\bdeo\s+col[oô]nia\b", "Deo Colonia"),
        (r"\beau\s+de\s+parfum\b|\bedp\b", "EDP"),
        (r"\beau\s+de\s+toilette\b|\bedt\b", "EDT"),
        (r"\beau\s+de\s+cologne\b|\bedc\b", "EDC"),
        (r"\bparfum\b", "Parfum"),
        (r"\bdesodorante\b", "Desodorante"),
        (r"\bcol[oô]nia\b", "Colônia"),
    )
    for pattern, canonical in patterns:
        if re.search(pattern, value, re.I):
            return canonical
    return ""


def preserve_source_variant_identity(product: str, source: str) -> str:
    """An exact card may omit a flanker, but cannot silently erase source identity."""
    source_tokens = set(product_key(source).split('-'))
    target_tokens = set(product_key(product).split('-'))
    source_variants = source_tokens & DISTINCTIVE_VARIANT_TOKENS
    target_variants = target_tokens & DISTINCTIVE_VARIANT_TOKENS
    missing = source_variants - target_variants
    noise = PRODUCT_NOISE_TOKENS | KNOWN_BRAND_TOKENS | DISTINCTIVE_VARIANT_TOKENS
    core = lambda tokens: {t for t in tokens - noise if t and not re.fullmatch(r'\d+(?:ml)?', t)}
    if (not missing or target_variants - source_variants
            or not core(source_tokens) or core(source_tokens) != core(target_tokens)
            or product_variant_conflict(source, product)):
        return product
    qualifier = ' '.join(sorted(missing))
    return re.sub(r'(?i)(\b\d{2,3}\s*ml\b)', qualifier + r' \1', product, count=1) if re.search(r'(?i)\b\d{2,3}\s*ml\b', product) else product + ' ' + qualifier


def source_variant_lost(source: str, listing: str) -> bool:
    """An explicit source flanker may not disappear in the exact-link listing."""
    source_variants = set(product_key(source).split('-')) & DISTINCTIVE_VARIANT_TOKENS
    listing_variants = set(product_key(listing).split('-')) & DISTINCTIVE_VARIANT_TOKENS
    return bool(source_variants - listing_variants)


def preserve_source_house(product: str, source: str) -> str:
    """Keep an explicitly supplied perfume house when marketplace SEO omits it."""
    houses = (
        (r'\bal\s+wataniah\b', 'Al Wataniah'), (r'\bmaison\s+alhambra\b', 'Maison Alhambra'),
        (r'\bfragrance\s+world\b', 'Fragrance World'), (r'\bfrench\s+avenue\b', 'French Avenue'),
        (r'\bo\s+botic[aá]rio\b|\boboticario\b', 'O Boticário'),
        (r'\blattafa\b', 'Lattafa'), (r'\barmaf\b', 'Armaf'), (r'\bafnan\b', 'Afnan'),
        (r'\brayhaan\b', 'Rayhaan'), (r'\brasasi\b', 'Rasasi'),
        (r'\bnatura\b', 'Natura'), (r'\beudora\b', 'Eudora'),
    )
    for pattern, canonical in houses:
        if re.search(pattern, source, re.I) and not re.search(pattern, product, re.I):
            if re.match(r'(?i)^perfume\b', product):
                return re.sub(r'(?i)^perfume\b', f'Perfume {canonical}', product, count=1).strip()
            return f"{canonical} {product}".strip()
    return product


def compact_product_name(product: str, preserve_house: bool = False) -> str:
    """Short display name, preserving SKU identity while removing marketplace SEO.

    ``preserve_house`` is used only when an exact listing title or an exact-link
    human edit is the authority. Historical display style may omit a redundant
    national house (e.g. ``Malbec Signature``), but an authoritative title that
    explicitly carries ``Natura``/``O Boticário`` must not lose that identity.
    """
    raw = strip_non_identity_attributes(canonicalize_known_product_name(product) or sanitize_product_text(product))
    # Marketplace attribute labels are not part of a fragrance's identity.
    raw = re.sub(r"(?i)\bmarca\s+linha\b", "", raw)
    raw = re.sub(r"(?i)\b(?:presente\s+perfeito|alta\s+fixa[cç][aã]o|alta\s+concentra[cç][aã]o|original)\b", "", raw)
    if re.search(r"(?i)\bhair\s+mist\b", raw):
        raw = re.sub(r"(?i)^\s*(?:perfume\s+)?para\s+os\s+cabelos\s+", "", raw)
    raw = re.sub(r"(?i)\bkit\s+(\d{1,2})\s+(?=infantil\b)", r"Kit \1 un. ", raw)
    # A kit is not one bottle: preserve each component's own size instead of
    # extracting the first volume and deleting all the rest.
    if re.search(r"(?i)\b(?:kit|combo)\b", raw) and len(re.findall(r"(?i)\b\d{2,3}\s*ml\b", raw)) > 1:
        raw = re.sub(r"(?i)\b(\d{2,3})\s*ml\b", r"\1ml", raw)
        return re.sub(r"\s+", " ", raw).strip(" -–—,")
    raw = re.sub(r"(?i)\b[áa]rebe\b", "Árabe", raw)
    raw = re.sub(r"(?i)\balhmabra\b", "Alhambra", raw)
    raw = re.sub(r"(?i)\beua\s+de\b", "Eau de", raw)
    raw = re.sub(r"(?i)\beau\s+de\s+alhambra\b", "Alhambra", raw)
    # Truncated marketplace concentration still carries unambiguous identity.
    raw = re.sub(r"(?i)\beau\s+de\s+parf\b", "EDP", raw)
    raw = re.sub(r"(?i)\b(\d{2,3})\s*m\b", r"\1ml", raw)
    # Marketplace/editorial launch labels and truncated assurance words are not
    # SKU identity. Breno repeatedly removes these in Edição.
    raw = re.sub(r"(?i)\brelan[cç]amento(?:\s+20\d{2})?\b", "", raw)
    raw = re.sub(r"(?i)\b20\d{2}\b", "", raw)
    raw = re.sub(r"(?i)\bu?002f\b", "", raw)
    raw = re.sub(r"(?i)\bc\s*[\\/]+\s*", "", raw)
    raw = re.sub(r"(?i)\blacrad(?:o|a)?\b", "", raw)
    # Common ML SEO tails observed in Edição. These describe quality/profile or
    # contain broken attribute joins; none is SKU identity. Keep this list
    # narrow so real flankers remain untouched.
    raw = re.sub(r"(?i)\b(?:genu[ií]no|alta\s+fixa[cç][aã]o|body\s+blz\s+[uú]nica)\b", " ", raw)
    raw = re.sub(
        r"(?i)\b(?:c[ií]tricos?|frescos?|aqu[aá]ticos?|amadeirados?|arom[aá]ticos?|especiados?|florais?|frutados?)"
        r"(?:\s+(?:c[ií]tricos?|frescos?|aqu[aá]ticos?|amadeirados?|arom[aá]ticos?|especiados?|florais?|frutados?|toques?))*\s*$",
        " ", raw,
    )
    # ML assurance tail, not a flanker: "Lattafa Asad E Selo Lataffa EDP".
    # Strip the whole conjunction-tail before it leaks into the public title.
    raw = re.sub(r"(?i)\s+e\s+selo(?:\s+original)?(?:\s+lattafa|\s+lataffa)?\b.*?(?=\b(?:EDP|EDT|EDC|\d{2,3}\s*ml)\b|$)", " ", raw)
    if not raw:
        return "Produto"

    concentration = canonical_perfume_type(raw)

    volume = ""
    m_vol = re.search(r"(?i)\b(\d{2,3})\s*ml\b", raw)
    if m_vol:
        volume = f"{m_vol.group(1)}ml"

    name = raw
    # ``Le Parfum`` can be the exact flanker name (JPG Le Beau Le Parfum), not
    # merely a concentration token. Protect it during generic type stripping.
    name = re.sub(r"(?i)\ble\s+parfum\b", "__LE_PARFUM_VARIANT__", name)
    # Seller promo prose is not SKU identity. Preserve the volume captured
    # above, but strip tails such as "DE 199 PRA 75 😱" before display and
    # product-key generation can treat those prices as model/volume tokens.
    name = re.sub(
        r"(?i)\s*\bde\s*(?:r\$\s*)?\d+(?:[.,]\d{1,2})?\s+"
        r"(?:pra|para|por)\s*(?:r\$\s*)?\d+(?:[.,]\d{1,2})?.*$",
        "", name,
    )
    # Normalize/removes marketplace SEO words, but don't lose the product identity.
    name = re.sub(
        r"(?i)\b(?:extrait\s+de\s+parfum|deo(?:dorante)?\s+parfum|"
        r"desodorante\s+col[oô]nia|deo\s+col[oô]nia|eau\s+de\s+parfum|"
        r"eau\s+de\s+toilette|eau\s+de\s+cologne|edp|edt|edc|parfum|"
        r"desodorante|col[oô]nia)\b",
        "", name,
    )
    name = name.replace("__LE_PARFUM_VARIANT__", "Le Parfum")
    # ML attribute junk appended to catalog titles ("Volume da unidade 90 mL").
    name = re.sub(r"(?i)\bvolume\s+da\s+unidade\b(\s*\d{1,4}\s*m?l?)?", "", name)
    # Gender phrases first, INCLUDING conjunctions: "Perfume Masculino E
    # Feminino Ck Be" must not survive as "E Feminino Ck Be" (CkBe 2026-07-10).
    name = re.sub(r"(?i)\bperfume\s+(?:masculino|feminino)(?:\s+e\s+(?:masculino|feminino))?(?:\s+original)?\b", "", name)
    name = re.sub(r"(?i)\b(?:para\s+)?(?:masculino|feminino)\s+e\s+(?:masculino|feminino)\b", "", name)
    name = re.sub(r"(?i)\bpara\s+(?:masculino|feminino)\b", "", name)
    name = re.sub(r"(?i)^\s*e\s+(?=[A-Za-zÀ-ÿ])", "", name)
    name = re.sub(r"(?i)^\s*(?:masculina|masculino|feminina|feminino)\s+", "", name)
    name = re.sub(r"(?i)\bmasc\b", "", name)
    # Olfactive-family SEO is description, not part of the SKU name. Keep real
    # flanker tokens; remove only a trailing descriptor immediately before type.
    name = re.sub(
        r"(?i)\b(?:amadeirado|c[ií]trico|arom[aá]tico|especiado|floral|frutado)"
        r"(?:\s+(?:amadeirado|c[ií]trico|arom[aá]tico|especiado|floral|frutado))*"
        r"(?=\s+\d{2,3}\s*ml\b|\s*$)",
        "", name,
    )
    name = re.sub(r"(?i)\b(perfume\s+masculino\s+original|perfume\s+feminino\s+original|perfume\s+masculino|perfume\s+feminino|original)\b", "", name)
    name = re.sub(r"(?i)\bperfume\b", "", name)
    name = re.sub(r"\(\s*\)", "", name)
    name = re.sub(r"(?i)\s*[-–—]?\s*\bcompartilh[aá]vel\b", "", name)

    without_arabe = re.sub(r"(?i)\b[áa]rabe\b", "", name)
    without_arabe = re.sub(r"\s+", " ", without_arabe).strip(" -–—,")
    # Breno usually drops "Perfume Árabe"/"Árabe" from the public title;
    # keep it only when removing it would leave a naked/generic short name.
    # 2026-07-26: "Kit Body Splash Árabe 200ml" passed the length test but the
    # remainder is 100% generic category words, so the title became an
    # unidentifiable "Kit Body Splash 200ml". Length is not enough: require a
    # distinctive (non-category) token to survive.
    _generic_title_tokens = {
        "kit", "combo", "conjunto", "body", "splash", "mist", "hair",
        "perfume", "colonia", "colônia", "deo", "desodorante", "spray",
        "feminino", "masculino", "unissex", "un", "und", "unidades", "itens",
        "de", "e", "com", "ml",
    }
    _rest_tokens = {
        re.sub(r"[^a-zà-ÿ]", "", w.casefold())
        for w in re.sub(r"(?i)\b\d+\s*(?:ml|un|und)?\b", " ", without_arabe).split()
    }
    _distinctive = {t for t in _rest_tokens if t and t not in _generic_title_tokens}
    if len(without_arabe) >= 12 and _distinctive:
        name = without_arabe
    # House-style (other editors, 2026-07-09): well-known national brands are
    # dropped when the line name is distinctive on its own — "Kaiak Aero
    # 100ml", "Egeo Spicy Vibe Colônia 90ml", not "Zaad Tradicional - O
    # Boticário EDP 95ml". Keep the brand when removing it would leave a
    # short/naked name (e.g. "Natura Homem" must not become "Homem").
    without_brand = re.sub(r"(?i)\s*[-–—]?\s*\b(?:o\s+botic[aá]rio|botic[aá]rio|natura)\b\s*[-–—]?\s*", " ", name)
    without_brand = re.sub(r"\s+", " ", without_brand).strip(" -–—,")
    # Recent Edição pairs establish that NATURA is needed for generic family
    # names such as Homem and Essencial ("Homem Coragio" and "Essencial
    # Exclusivo" were both restored to Natura). Distinctive lines such as Kaiak
    # can still use the shorter house style.
    must_keep_natura = bool(re.search(r"(?i)\bnatura\s+(?:homem|essencial)\b", name))
    if (without_brand != name and not preserve_house and not must_keep_natura
            and len(without_brand) >= 10 and len(without_brand.split()) >= 2):
        name = without_brand
    name = re.sub(r"(?i)\b\d{2,3}\s*ml\b", "", name)
    name = re.sub(r"\(\s*\)", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—,")

    # Exact-item titles can repeat both a whole phrase and its last token after
    # catalog joins ("French Avenue Royal Blend French Avenue Royal Blend Nero
    # Nero"). Collapse only adjacent duplicate sequences, preserving legitimate
    # non-adjacent repetitions and names such as Club de Nuit.
    _name_tokens = name.split()
    for _span in range(min(6, len(_name_tokens) // 2), 0, -1):
        _i = 0
        while _i + 2 * _span <= len(_name_tokens):
            _left = [t.casefold() for t in _name_tokens[_i:_i + _span]]
            _right = [t.casefold() for t in _name_tokens[_i + _span:_i + 2 * _span]]
            if _left == _right:
                del _name_tokens[_i + _span:_i + 2 * _span]
            else:
                _i += 1
    name = " ".join(_name_tokens)

    # Word-safe truncation ("...Enigma + Midto" case 2026-07-09): never cut
    # mid-word; allow a bit more room before trimming at a word boundary.
    base_name = name.strip(" -–—,")
    if len(base_name) > 62:
        base_name = base_name[:62]
        base_name = re.sub(r"\s+\S*$", "", base_name).strip(" -–—,+&")
    # Only add generic "Perfume" when the product name would be too naked/short.
    # Breno's standard is not to prefix every title with "Perfume".
    if base_name and len(base_name) < 10 and not re.search(r"(?i)\b(edp|edt|parfum|col[oô]nia|body|kit)\b", base_name):
        parts = ["Perfume", base_name]
    else:
        parts = [base_name]
    if concentration:
        parts.append(concentration)
    if volume:
        parts.append(volume)
    display = " ".join(p for p in parts if p)
    display = re.sub(r"\s+", " ", display).strip()[:72]
    # Deterministic display casing: ML API titles flip-flop between
    # "Volume Da Unidade" and "Volume da unidade"; capitalize fully-lowercase
    # words but never touch acronyms/mixed-case tokens (EDP, ml, McQueen).
    display = " ".join(w.capitalize() if w.islower() and w != "ml" else w for w in display.split(" "))
    display = re.sub(r"(?i)\bExtrait\s+De\s+Parfum\b", "Extrait de Parfum", display)
    # Sellers SHOUT titles ("PRADA L'HOMME"). If most letters are uppercase,
    # retitle word-by-word, preserving acronym tokens (EDP/EDT/ML/CK/YSL...)
    # and volume tokens.
    letters = [ch for ch in display if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
        keep = {"EDP", "EDT", "EDC", "ML", "CK", "YSL", "DKNY", "CH", "NF"}
        def _fix(w: str) -> str:
            if w.upper() in keep or re.fullmatch(r"\d+(?:ml|ML)", w):
                return w if not w.isupper() or w.upper() in keep else w
            return "'".join(p.capitalize() for p in w.lower().split("'")) if w.isupper() else w
        display = " ".join(_fix(w) for w in display.split(" "))
    return preserve_gender(display, product) if display else "Produto"


def sane_price_pair(de: str | None, por: str | None) -> tuple[str | None, str | None]:
    """Never publish nonsense like 'De R$ 299 por R$ 409'."""
    if de and por:
        de_value = money_value(de)
        por_value = money_value(por)
        if de_value is not None and por_value is not None and de_value <= por_value:
            return None, por
    return de, por


def meaningful_de_price(de: str | None, por: str | None, proved: bool = False) -> str | None:
    """Only show 'De R$ X' when the checked discount is worth highlighting.

    If ML/Jina says R$134 -> R$114, publishing the smaller 'de' makes the post
    weaker/noisier than just 'Por R$114'. Keep 'De' for meaningful delta.
    """
    if not de or not por:
        return None
    de_value = money_value(de)
    por_value = money_value(por)
    if de_value is None or por_value is None or de_value <= por_value:
        return None
    delta = de_value - por_value
    discount_pct = delta / de_value
    # Evidence proves the number, not its editorial usefulness. A tiny anchor
    # still pollutes the post even when ML really shows it. Breno's floor is
    # approximately 10% AND R$10; below either threshold publish only Por.
    if proved:
        return de if delta >= 10 and discount_pct >= 0.10 else None
    ratio = de_value / max(0.01, por_value)
    if delta < 30 and ratio < 1.25:
        return None
    return de


PRODUCT_CUE_RE = re.compile(
    r"(?i)\b(\d{2,3}\s*ml|kit|presente|perfume|body spray|edp|edt|parfum|"
    r"eau\s+de\s+(?:toilette|parfum|cologne)|col[oô]nia|"
    r"o botic[aá]rio|natura|lattafa|carolina herrera|azzaro|ralph lauren|malbec|"
    r"armaf|club de nuit|al wataniah|maison alhambra|french avenue|hugo boss|khadlaj|afnan)\b"
)


def looks_like_product_line(line: str) -> bool:
    lo = line.lower()
    if re.search(r"https?://|cupom|loja|vendido|vendedor|^de\b|\bpor\b|link|compre|selecione", lo):
        return False
    if re.search(r"(?i)\b(pre[cç]o|promo|baixou|imperd[ií]vel|bom no pre[cç]o)\b", line):
        return False
    # Editorial phrases from curated groups are useful as headline context, but
    # must never become product keys or approved-image folders.
    if re.search(r"(?i)^(perfeito pra noite|esse aqui|perfume de tanta qualidade|mais maduro|inspirado no|o litr[aã]o|melhor baunilha|pai do|sumiu|valor ta surreal)", line):
        return False
    return bool(PRODUCT_CUE_RE.search(line))


def looks_like_editorial_product_contamination(line: str) -> bool:
    """Sales-copy phrases that mention perfume terms but are not product names."""
    clean = strip_wa(line or "").strip()
    if not clean:
        return False
    return bool(re.search(
        r"(?i)\b("
        r"^um\s+dos\s+melhores|brabo da linha|brabo da noite|é\s+o\s+(?:intense|parfum|edp|edt)|e\s+o\s+(?:intense|parfum|edp|edt)|"
        r"pra\s+(?:noite|balada|date|trabalho)|cheiro\s+de|cara\s+de\s+caro|"
        r"esse\s+(?:aqui|daqui)|qualidade\s+absurda|entrega\s+demais|"
        r"^ideia\s+de\s+presente|^presente\s+pro|^j[aá]\s+separa|"
        r"^voc[eê]\s+e\s+o\s+moz[aã]o|^eleg[aâ]ncia\s+em\s+forma|"
        r"^do\s+saltinho|^melhor\s+inspirado"
        r")\b",
        clean,
    )) or bool(re.search(
        # Dupe/inspiration headlines name the REFERENCE perfume, never the product
        # being sold ("DUPE DO ONE MILLION PARFUM, o mais desejado" for a Fakhar
        # Gold listing). Because they carry parfum/edp/ml tokens they used to pass
        # looks_like_product_line(), hijacking both the image lookup and the card
        # price check (product mismatch -> "preço não validado").
        r"(?i)^\s*(?:o\s+)?(?:dupe|clone|inspirado|inspirada|vers[aã]o|similar|"
        r"parecido|igualzinho|lembra)\b[^\n]*\b(?:d[oae]|ao|no|na)\b",
        clean,
    )) or bool(re.search(
        # Sales verbs / comparative claims. Real listing titles are noun phrases
        # ("Kit Presente Egeo Dolce 2 Itens"); they never say what the product
        # *does for you*. "Kit Egeo Compensa Mais Que Comprar Separado" passed
        # looks_like_product_line() because of "Kit"+"Egeo" and became the title.
        # Measured: only 1 of ~3000 bank folders contains any of these verbs.
        r"(?i)\b(compensa|vale\s+(?:mais|a\s+pena)|sai\s+(?:mais|por)|"
        r"comprar\s+separado|mais\s+vantagem|melhor\s+que|"
        r"aproveit\w+|economiz\w+|garante|te\s+deixa|vai\s+durar|"
        r"pela\s+metade\s+do\s+pre[cç]o)\b",
        clean,
    ))


def product_variant_conflict(raw_product: str, jina_product: str) -> bool:
    """Prefer ML title when raw text names a different variant of the same item.

    Examples: Sabah Al Ward text attached to a Sabah listing; Gold vs Black;
    Sugar vs original. These mismatches pick a beautiful but wrong bottle.
    """
    if not raw_product or not jina_product:
        return False
    if not identity_compatible(raw_product, jina_product, DISTINCTIVE_VARIANT_TOKENS):
        return True
    raw_key = product_key(raw_product)
    jina_key = product_key(jina_product)
    raw_tokens = set(raw_key.split("-"))
    jina_tokens = set(jina_key.split("-"))
    if len(raw_tokens & jina_tokens) < 2:
        return False
    variant_tokens = {
        "ward", "sugar", "black", "gold", "platin", "platinum", "intense", "elixir",
        "bourbon", "verde", "blue", "bleu", "tradicional", "traditional", "x", "hair", "mist",
        "legend", "pulso", "evolutio", "absolu", "sable",
        "asad", "eclaire", "fakhar", "angham", "rose",
    }
    raw_variants = raw_tokens & variant_tokens
    jina_variants = jina_tokens & variant_tokens
    # Functional product types are not harmless missing flankers: Good Girl
    # perfume and Good Girl Hair Mist can share the stiletto silhouette while
    # being different SKUs. If only one side says hair/cabelo/mist, block.
    functional_tokens = {"hair", "mist", "cabelo", "cabelos"}
    if bool(raw_tokens & functional_tokens) != bool(jina_tokens & functional_tokens):
        return True
    # Volume is a hard SKU attribute, not a flanker. When both sides state a
    # volume and they disagree, the ad and our title are different products —
    # advertising 50ml for a 100ml listing misprices the offer for the customer.
    # Stronger With You (2026-07-26) shipped as "50ml" while the rendered card
    # said "Edt 100ml". Missing volume on one side stays enrichment.
    raw_vols = set(re.findall(r"(\d{2,3})\s*ml", " ".join(raw_tokens)))
    jina_vols = set(re.findall(r"(\d{2,3})\s*ml", " ".join(jina_tokens)))
    if raw_vols and jina_vols and not (raw_vols & jina_vols):
        return True
    # A missing flanker on one side is enrichment, not conflict: "Fakhar 100ml"
    # + exact card "Fakhar Gold 100ml" is the card telling us the real SKU.
    # Block only when both sides explicitly name INCOMPATIBLE variant sets
    # (Gold vs Black, Asad vs Eclaire). A plain set inequality is wrong here
    # because {fakhar} != {fakhar, gold} while still being a subset.
    return bool(raw_variants and jina_variants
                and not raw_variants.issubset(jina_variants)
                and not jina_variants.issubset(raw_variants))


def same_product_except_volume(raw_product: str, listing_product: str) -> bool:
    """True only when an exact listing corrects the volume of the same SKU family.

    This is deliberately narrower than fuzzy identity: both sides must state
    different volumes and their remaining canonical tokens must be identical.
    Missing EDP/gender/original wording is harmless enrichment; flankers, kits,
    quantities, functional types and different product names remain conflicts.
    """
    if not raw_product or not listing_product:
        return False
    raw_vols = set(re.findall(r"(?i)\b(\d{2,3})\s*ml\b", raw_product))
    listing_vols = set(re.findall(r"(?i)\b(\d{2,3})\s*ml\b", listing_product))
    if not raw_vols or not listing_vols or raw_vols & listing_vols:
        return False

    def concentrations(text: str) -> set[str]:
        clean = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
        # Normalize full concentrations before tokenizing: the word 'parfum'
        # inside Eau de Parfum must not also authorize a pure Parfum SKU.
        clean = re.sub(r"\beau\s+de\s+parfum\b", "edp", clean)
        clean = re.sub(r"\beau\s+de\s+toilette\b", "edt", clean)
        clean = re.sub(r"\bextrait\s+de\s+parfum\b", "extrait", clean)
        return set(re.findall(r"\b(edp|edt|parfum|extrait|cologne)\b", clean))

    raw_concentrations = concentrations(raw_product)
    listing_concentrations = concentrations(listing_product)
    if (raw_concentrations and listing_concentrations
            and not raw_concentrations & listing_concentrations):
        return False
    raw_genders = set(re.findall(r"(?i)\b(masculino|feminino|masculina|feminina)\b", raw_product))
    listing_genders = set(re.findall(r"(?i)\b(masculino|feminino|masculina|feminina)\b", listing_product))
    normalize_gender = lambda values: {"m" if v.lower().startswith("mascul") else "f" for v in values}
    if (raw_genders and listing_genders
            and normalize_gender(raw_genders) != normalize_gender(listing_genders)):
        return False

    def identity_tokens(text: str) -> set[str]:
        tokens = set(product_key(text).split("-"))
        cleaned = {
            token for token in tokens
            if token and not re.fullmatch(r"\d{2,3}ml", token)
            and token not in {
                "perfume", "arabe", "original", "masculino", "feminino",
                "edp", "edt", "parfum", "eau", "de", "toilette",
            }
        }
        # The house now labels products simply "Rabanne" while older/source
        # titles still say "Paco Rabanne". Paco is not a flanker or SKU token;
        # keeping it made a pure 50ml→100ml correction look like another product.
        if "rabanne" in cleaned:
            cleaned.discard("paco")
        return cleaned

    raw_identity = identity_tokens(raw_product)
    listing_identity = identity_tokens(listing_product)
    return len(raw_identity) >= 2 and raw_identity == listing_identity


def extract_product(body: str) -> str:
    clean = strip_wa(body)
    lines = [x.strip(" -•\t") for x in clean.splitlines() if x.strip()]

    def _generic_only(line: str) -> bool:
        # "Perfume 100ml", "Desodorante Corporal 100ml": kit component lines
        # made only of generic words + volume are NOT a product name
        # (Kit Natura published as "100ml", 2026-07-09).
        toks = [t for t in re.findall(r"[A-Za-zÀ-ÿ]{3,}", line)]
        generic = {"perfume", "desodorante", "corporal", "shower", "gel", "body", "splash",
                   "spray", "colônia", "colonia", "masculino", "feminino", "original", "edp", "edt", "parfum"}
        return bool(toks) and all(t.lower() in generic for t in toks)

    # Some curated source groups use line 1 as the product and line 2 as an
    # editorial phrase. Prefer first line when it has hard product cues; this
    # prevents phrases like "Esse aqui é muito perfume" from becoming product.
    if lines and not looks_like_editorial_product_contamination(lines[0]) and looks_like_product_line(lines[0]) and re.search(r"(?i)\b(\d{2,3}\s*ml|edp|edt|parfum|eau\s+de\s+(?:toilette|parfum|cologne)|body splash|body spray|col[oô]nia)\b", lines[0]):
        return sanitize_product_text(lines[0])[:110]
    # Kit promos: first line is the kit name ("Kit Natura Essencial Clássico")
    # and the volume lives in the component bullets. The kit name IS the
    # product even without an ml token.
    if lines and re.search(r"(?i)^kit\b", lines[0]) and not looks_like_editorial_product_contamination(lines[0]):
        return sanitize_product_text(lines[0])[:110]
    # Otherwise prefer explicit product lines after the first raw headline.
    for line in lines[1:]:
        if looks_like_product_line(line) and not looks_like_editorial_product_contamination(line) and not _generic_only(line):
            return sanitize_product_text(line)[:110]
    return "Produto"


def kit_image_identity(body: str, product: str) -> str:
    """Keep kit components/cardinality for image lookup without polluting display title."""
    if not re.search(r"(?i)\b(?:kit|combo|conjunto)\b", f"{product}\n{body}"):
        return ""
    components: list[str] = []
    for raw in strip_wa(body or "").splitlines()[1:]:
        line = raw.strip(" -•\t")
        if not line:
            continue
        if re.search(r"(?i)^(?:de\b|por\b|cupom\b|https?://|loja\b|peg(?:ar|ue)|compre)", line):
            break
        if re.search(r"(?i)\b\d{2,3}\s*ml\b", line) and not looks_like_editorial_product_contamination(line):
            components.append(sanitize_product_text(line))
    if len(components) < 2:
        return ""
    return f"{product} {len(components)} itens " + " ".join(components)


def extract_product_from_jina(jina: str) -> str:
    if not jina:
        return ""
    m = re.search(r"(?im)^Title:\s*(.+)$", jina)
    if not m:
        m = re.search(r"(?im)^#\s+(.+)$", jina)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", strip_wa(m.group(1))).strip()
    title = re.sub(r"(?i)\s*\|\s*Mercado Livre.*$", "", title)
    title = re.sub(r"(?i)\s+-\s*Mercado Livre.*$", "", title)
    title = sanitize_product_text(title)
    return title[:110] if looks_like_product_line(title) else ""


def extract_prices(body: str) -> tuple[str | None, str | None, str | None]:
    clean = strip_wa(body)
    de = None
    por = None
    parcel = None
    m = re.search(r"(?i)de\s*:?[\s~]*R\$?\s*([0-9][0-9.,]*)", clean)
    if m:
        de = money_norm(m.group(1))
    # prefer lines with por
    m = re.search(r"(?i)por\s*:?[\s~*]*R\$?\s*([0-9][0-9.,]*)", clean)
    if not m:
        m = re.search(r"(?i)R\$\s*([0-9][0-9.,]*)\s*(?:no pix|via pix)", clean)
    if m:
        por = money_norm(m.group(1))
    m = re.search(r"(?i)\b(em\s+\d{1,2}x(?:\s+de\s+R\$\s*[0-9][0-9.,]*)?(?:\s+sem juros)?)", clean)
    if m:
        parcel = re.sub(r"\s+", " ", m.group(1)).strip()
    return de, por, parcel


def extract_cupons(body: str) -> list[str]:
    """Extract whole coupon tokens without eating a legitimate ``CUPOM`` prefix.

    The old regex restarted inside alternatives such as ``CUPOMLIBERADO`` and
    produced ``LIBERADO``; because results were truncated to two entries, the
    intact token found later was discarded.  Strip only the line's leading
    label, then parse complete alternatives from the remaining payload.
    """
    clean = strip_wa(body)
    ignored = {"CUPOM", "CUPONS", "OFF", "PIX", "ANUNCIO", "ANÚNCIO", "PAGAMENTO"}
    found: list[str] = []
    coupon_lines = [ln.strip() for ln in clean.splitlines() if re.search(r"(?i)\bcupom(?:s)?\b", ln)]
    for line in coupon_lines:
        payload = re.sub(
            r"(?i)^\s*(?:use|usar|aplique|aplicar)?\s*(?:o\s+)?cupons?\s*(?::|-)?\s*",
            "",
            line,
            count=1,
        )
        # Codes are complete alphanumeric tokens.  A leading CUPOM is data when
        # attached to the token (CUPOMVAI/CUPOMLIBERADO), never another label.
        for raw in re.findall(r"(?<![A-Z0-9_-])([A-Z0-9][A-Z0-9_-]{2,24})(?![A-Z0-9_-])", payload, re.I):
            code = raw.upper()
            if code in ignored or code in {"USE", "USAR", "APLIQUE", "APLICAR", "OU", "DE"}:
                continue
            if code not in found:
                found.append(code)
    return found[:2]


def canonical_brand(s: str) -> str:
    base = strip_wa(s).lower()
    base = base.replace("á", "a").replace("ã", "a").replace("â", "a").replace("ó", "o")
    for needle, brand in sorted(BRAND_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        pat = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
        if re.search(pat, base):
            return brand
    # fallback first title token before no ML
    s2 = re.sub(r"(?i)loja oficial|no ml|mercado livre|loja|oficial", "", strip_wa(s)).strip()
    return re.sub(r"\s+", " ", s2).strip().title()[:40]


def product_brand(product: str, jina_text: str = "") -> str:
    # Do not scan full ML/Jina page for brand aliases; generic page text causes false hits.
    return canonical_brand(product)


def extract_ml_product_url(jina_text: str, link: str | None = None) -> str | None:
    if not jina_text:
        return link
    m = re.search(r"\[Ir para produto\]\((https://www\.mercadolivre\.com\.br/[^\s)]+)\)", jina_text)
    if m:
        return m.group(1)
    m = re.search(r"https://www\.mercadolivre\.com\.br/[^\s)]+/(?:p/)?MLB[0-9A-Z]+[^\s)]*", jina_text)
    return m.group(0) if m else link


def ml_api_item_id(product_url: str | None) -> str | None:
    if not product_url:
        return None
    # On ML social/Jina pages the real item often appears as wid=MLB123...;
    # the /p/MLB... in the path is a catalog product id, never a listing id.
    m = re.search(r"[?&#]wid=(MLB-?\d{6,})\b", product_url, re.I)
    if not m:
        m = re.search(r"(?<!/p/)\b(MLB-?\d{8,})\b", product_url, re.I)
    return m.group(1).replace("-", "").upper() if m else None


def resolve_link_offer(link: str, html_text: str = "", expected_product: str | None = None) -> dict:
    """Resolve the exact commercial listing selected by a promo link."""
    text = html.unescape(html_text or "").replace("\\/", "/")
    mt = re.search(r'(?:og:title"\s*content="|"type":"og:title","content":")([^"]{8,240})', html_text or "")
    og_title = html.unescape(mt.group(1)) if mt else ""
    page_product_ids = list(dict.fromkeys(re.findall(r"/p/(MLB\d{6,10})\b", text)))
    product_id = page_product_ids[0] if page_product_ids else None
    candidates: list[tuple[str, str, str | None]] = []
    direct = ml_api_item_id(link)
    if direct:
        candidates.append((direct, "direct_item_url", None))
    for mm in re.finditer(r"pdp_filters=item_id(?:%3A|:)(MLB\d{8,})", text, re.I):
        candidates.append((mm.group(1).upper(), "pdp_filters", None))
    # Keep each wid bound to the /p/ id from ITS OWN href. Using the first /p/
    # on a social page made Quasar Ice inherit Nautica Voyage's catalog.
    for mm in re.finditer(r'href="[^"]*?/p/(MLB\d{6,10})[^"]*?[?&]wid=(MLB\d{8,})', text, re.I):
        candidates.append((mm.group(2).upper(), "product_href_wid", mm.group(1).upper()))
    for mm in re.finditer(r'"(?:selected_item_id|selectedItemId|item_id)"\s*:\s*"(MLB\d{8,})"', text, re.I):
        candidates.append((mm.group(1).upper(), "selected_item_state", None))
    seen: set[tuple[str, str | None]] = set()
    rejected: list[dict] = []
    for item_id, source, candidate_pid in candidates:
        sig = (item_id, candidate_pid)
        if sig in seen:
            continue
        seen.add(sig)
        try:
            payload = ml_api_item_payload(item_id)
        except Exception:
            # Public /items/{id} is policy-walled in this environment. The
            # authenticated catalog listing endpoint is an equally strong
            # identity proof: membership under product_id means this item is an
            # offer of that exact catalog product, and supplies price/seller/store.
            payload = {}
            search_pids = [candidate_pid] if candidate_pid else page_product_ids[:12]
            for pid in [p for p in search_pids if p]:
                listing = _ml_api_get(f"https://api.mercadolibre.com/products/{pid}/items?limit=50")
                rows = listing.get("results", []) if isinstance(listing, dict) else []
                row = next((r for r in rows if r.get("item_id") == item_id), None)
                if not row:
                    continue
                prod = _ml_api_get(f"https://api.mercadolibre.com/products/{pid}")
                pname = str(prod.get("name") or "") if isinstance(prod, dict) else ""
                if og_title and pname and _tokens_overlap(og_title, pname) < 0.5:
                    continue
                payload = dict(row)
                payload["catalog_product_id"] = pid
                payload["title"] = pname
                break
            if not payload and source == "pdp_filters":
                # Featured/pdp item can disappear from the catalog listing API
                # before the social card expires. The card itself still binds
                # item_id + title + seller + typed prices. Resolve its product
                # only when one page /p/ product name strongly matches that card.
                facts = extract_exact_item_facts(html_text, item_id)
                ftitle = str(facts.get("title") or "")
                scored = []
                if ftitle:
                    for pid in page_product_ids[:12]:
                        prod = _ml_api_get(f"https://api.mercadolibre.com/products/{pid}")
                        pname = str(prod.get("name") or "") if isinstance(prod, dict) else ""
                        score = _tokens_overlap(ftitle, pname) if pname else 0.0
                        distinctive_match = bool(
                            _distinctive_identity_tokens(ftitle)
                            & _distinctive_identity_tokens(pname)
                        )
                        if (score >= 0.55 and distinctive_match
                                and not product_variant_conflict(ftitle, pname)):
                            scored.append((score, pid, pname))
                if scored:
                    _, matched_pid, matched_name = max(scored)
                    payload = {
                        "catalog_product_id": matched_pid,
                        "title": matched_name,
                        "price": facts.get("current_price"),
                        "_exact_card_fallback": True,
                    }
                elif (ftitle and expected_product
                      and not product_variant_conflict(expected_product, ftitle)):
                    payload = {
                        "catalog_product_id": None,
                        "title": ftitle,
                        "price": facts.get("current_price"),
                        "_exact_card_fallback": True,
                    }
            if not payload:
                rejected.append({"item_id": item_id, "source": source, "reason": "not_in_exact_catalog_page"})
                continue
        item_product = str(payload.get("catalog_product_id") or "")
        item_title = str(payload.get("title") or "")
        if candidate_pid and item_product and item_product != candidate_pid:
            rejected.append({"item_id": item_id, "source": source, "reason": "different_product_id"})
            continue
        if og_title and item_title and _tokens_overlap(og_title, item_title) < 0.5:
            rejected.append({"item_id": item_id, "source": source, "reason": "title_mismatch"})
            continue
        if (expected_product and item_title
                and (_tokens_overlap(expected_product, item_title) < 0.45
                     or product_variant_conflict(expected_product, item_title))):
            # Social pages contain many valid recommendation wids. A candidate
            # being a real API item is not enough: it must also be the product
            # the forwarded message/link intends (Sceptre selected Wazeer).
            rejected.append({"item_id": item_id, "source": source, "reason": "expected_product_mismatch"})
            continue
        return {
            "product_id": item_product or candidate_pid or product_id or None,
            "item_id": item_id,
            "item_id_source": source,
            "confidence": "exact_card" if payload.get("_exact_card_fallback") else "exact",
            "item_payload": payload,
            "rejected_candidates": rejected,
        }
    return {
        "product_id": None,
        "item_id": None,
        "item_id_source": None,
        "confidence": "unknown",
        "rejected_candidates": rejected,
    }


def extract_exact_item_facts(html_text: str, item_id: str) -> dict:
    """Extract typed price facts from the single card containing item_id."""
    if not html_text or not item_id:
        return {}
    text = html.unescape(html_text).replace("\\/", "/")
    cards: list[str] = []
    title_starts = [m.start() for m in re.finditer(r'"title":\{"text":"', text)]
    for occ in re.finditer(re.escape(item_id), text, re.I):
        starts = [p for p in title_starts if p <= occ.start()]
        if not starts:
            continue
        start = starts[-1]
        nexts = [p for p in title_starts if p > start]
        end = nexts[0] if nexts else min(len(text), start + 9000)
        card = text[start:end]
        if item_id in card:
            cards.append(card)
    if not cards:
        # Rendered HTML fallback: some social cards omit the structured JSON but
        # still bind wid, title, seller and aria-label prices in one poly-card.
        for chunk in re.split(r"(?is)<div\s+class=[\"']poly-card\b", text):
            if not re.search(r"(?i)[?&](?:amp;)?wid=" + re.escape(item_id) + r"\b", chunk):
                continue
            tm = re.search(r"(?is)poly-component__title[^>]*>(.*?)</a>", chunk)
            sm = re.search(r"(?is)poly-component__seller[^>]*>\s*Por\s+([^<]{2,80})", chunk)
            now = re.search(r'aria-label="Agora:\s*([\d.]+) reais(?: com (\d{1,2}) centavos)?', chunk, re.I)
            before = re.search(r'aria-label="Antes:\s*([\d.]+) reais(?: com (\d{1,2}) centavos)?', chunk, re.I)
            def aria_num(m):
                return (float(m.group(1).replace(".", "")) + float(m.group(2) or 0) / 100.0) if m else None
            current_value = aria_num(now)
            original_value = aria_num(before)
            pix = bool(re.search(r"(?i)\bno\s+pix\b", chunk))
            inst = re.search(r"(?i)\b(\d{1,2})x\b.{0,160}?sem\s+juros", re.sub(r"<[^>]+>", " ", chunk), re.S)
            title_text = re.sub(r"<[^>]+>", " ", tm.group(1)).strip() if tm else None
            seller_text = html.unescape(sm.group(1)).strip(" -–—,.") if sm else None
            return {
                "item_id": item_id,
                "product_id": None,
                "title": html.unescape(title_text) if title_text else None,
                "current_price": current_value,
                "original_price": original_value,
                "pix_price": current_value if pix else None,
                "other_methods_price": None,
                "installments": f"{int(inst.group(1))}x" if inst else None,
                "installments_no_interest": bool(inst),
                "seller_name": seller_text,
                "price_modalities": ([{"kind": "pix" if pix else "regular", "amount": current_value,
                                      "item_id": item_id, "source": "exact_item_html_card"}]
                                     if current_value is not None else []),
            }
        return {}
    # Prefer the card with a structured price object and action URL to this item.
    card = next((c for c in cards if '"price":{' in c and item_id in c), cards[0])
    current = re.search(r'"current_price":\{"value":\s*([\d.]+)', card)
    title = re.search(r'"title":\{"text":"([^"]+)', card)
    card_pid = re.search(r'/p/(MLB\d{6,10})[^"\s]*?[?&]wid=' + re.escape(item_id), card, re.I)
    previous = re.search(r'"previous_price":\{"value":\s*([\d.]+)', card)
    label = re.search(r'"discount_label":\{"text":"([^"]+)', card)
    seller = re.search(r'"seller":\{"text":"Por\s+([^"{]+)', card)
    inst = re.search(r'"installments":\{"text":"([^"]*?(\d{1,2})x[^"]*)"\s*,\s*"no_interest":\s*(true|false)', card)
    other = re.search(r'"installments":\{"text":"ou\s+\{price_total\}\s+em outros meios".*?"price":\{"value":\s*([\d.]+)', card, re.S)
    def num(match):
        try:
            return float(match.group(1)) if match else None
        except (TypeError, ValueError):
            return None
    current_value = num(current)
    pix = bool(label and re.search(r"(?i)\bno\s+pix\b", label.group(1)))
    modalities = []
    if current_value is not None:
        modalities.append({"kind": "pix" if pix else "regular", "amount": current_value, "item_id": item_id, "source": "exact_item_card"})
    other_value = num(other)
    if other_value is not None:
        modalities.append({"kind": "regular", "amount": other_value, "item_id": item_id, "source": "exact_item_card"})
    return {
        "item_id": item_id,
        "product_id": card_pid.group(1).upper() if card_pid else None,
        "title": title.group(1).strip() if title else None,
        "current_price": current_value,
        "original_price": num(previous),
        "pix_price": current_value if pix else None,
        "other_methods_price": other_value,
        "installments": (f"{int(inst.group(2))}x" if inst and inst.group(3) == "true" else None),
        "installments_no_interest": bool(inst and inst.group(3) == "true"),
        "seller_name": seller.group(1).strip() if seller else None,
        "price_modalities": modalities,
    }


def ml_api_item_ids_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    ids: list[str] = []
    ids.extend(m.group(1).replace("-", "").upper() for m in re.finditer(r"(?i)\bwid=(MLB-?\d{6,})\b", text))
    ids.extend(m.group(1).replace("-", "").upper() for m in re.finditer(r"(?i)\b(MLB-?\d{9,})\b", text))
    seen: set[str] = set()
    return [x for x in ids if x and not (x in seen or seen.add(x))]


_ML_ITEM_CACHE: dict[str, dict] = {}


def ml_api_item_payload(item_id: str) -> dict:
    if item_id in _ML_ITEM_CACHE:
        return _ML_ITEM_CACHE[item_id]
    req = Request(f"https://api.mercadolibre.com/items/{item_id}", headers={"User-Agent": "Hermes"})
    with urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    _ML_ITEM_CACHE[item_id] = data
    return data


def ml_api_item_title(product_url: str | None) -> str:
    """Real item title from the ML API — cleanest product-name source.

    Only trusts the URL-derived item id (wid). Text-scraped ids from social
    pages can point at recommendation cards, so they are not used here.
    """
    item_id = ml_api_item_id(product_url)
    if not item_id:
        return ""
    try:
        title = str(ml_api_item_payload(item_id).get("title") or "").strip()
    except Exception:
        return ""
    return title if title and looks_like_product_line(title) else ""


def ml_api_official_store(product_url: str | None, jina_text: str = "") -> tuple[bool, str]:
    item_id = ml_api_item_id(product_url)
    item_ids = ([item_id] if item_id else []) + [x for x in ml_api_item_ids_from_text(jina_text) if x != item_id]
    for iid in item_ids[:8]:
        try:
            data = ml_api_item_payload(iid)
            store_id = data.get("official_store_id")
            if not store_id:
                continue
            brand = data.get("official_store_name") or ""
            if brand:
                return True, canonical_brand(str(brand))
            seller_id = data.get("seller_id")
            if seller_id:
                try:
                    req = Request(f"https://api.mercadolibre.com/users/{seller_id}", headers={"User-Agent": "Hermes"})
                    with urlopen(req, timeout=8) as resp:
                        seller = json.loads(resp.read().decode("utf-8", "ignore"))
                    nick = seller.get("nickname") or ""
                    return True, canonical_brand(str(nick)) if nick else ""
                except Exception:
                    return True, ""
            return True, ""
        except Exception:
            continue
    return False, ""


# ---------------------------------------------------------------------------
# ML Catalog API (authenticated). /items and /sites/search are policy-walled
# for third-party apps, but the catalog endpoints work:
#   /products/search, /products/{id}, /products/{id}/items
# Flow: short link -> social/item HTML -> catalog product id (/p/MLBxxxx) +
# item id (wid=MLBxxxx) -> catalog lookup for canonical name, live price,
# original_price and official_store_id of the exact linked listing.
# ---------------------------------------------------------------------------
ML_TOKEN_FILE = Path(os.path.expanduser("~/.hermes/secrets/ml-api-token.json"))
ML_APP_ENV = Path(os.path.expanduser("~/.hermes/secrets/ml-api-app.env"))
ML_CATALOG_CACHE_FILE = Path(os.path.expanduser("~/.hermes/cache/ml_catalog_cache.json"))
ML_CATALOG_CACHE_SCHEMA = 10
ML_CATALOG_TTL_SECONDS = 10 * 60  # social selection/buy-box rotates; 3h was unsafe
_ML_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _ml_app_creds() -> dict:
    creds: dict = {}
    try:
        for line in ML_APP_ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    except Exception:
        pass
    return creds


def ml_access_token(force_refresh: bool = False) -> str:
    """Access token with self-refresh (offline_access refresh token, 6h TTL)."""
    try:
        tok = json.loads(ML_TOKEN_FILE.read_text())
    except Exception:
        return ""
    age = time.time() - float(tok.get("fetched_at") or 0)
    if not force_refresh and tok.get("access_token") and age < 5 * 60 * 60:
        return str(tok["access_token"])
    creds = _ml_app_creds()
    refresh = tok.get("refresh_token")
    if not (creds.get("ML_CLIENT_ID") and creds.get("ML_CLIENT_SECRET") and refresh):
        return str(tok.get("access_token") or "")
    try:
        from urllib.parse import urlencode

        body = urlencode({
            "grant_type": "refresh_token",
            "client_id": creds["ML_CLIENT_ID"],
            "client_secret": creds["ML_CLIENT_SECRET"],
            "refresh_token": refresh,
        }).encode()
        req = Request("https://api.mercadolibre.com/oauth/token", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=12) as resp:
            new_tok = json.loads(resp.read().decode("utf-8", "ignore"))
        if new_tok.get("access_token"):
            new_tok["fetched_at"] = time.time()
            ML_TOKEN_FILE.write_text(json.dumps(new_tok))
            os.chmod(ML_TOKEN_FILE, 0o600)
            return str(new_tok["access_token"])
    except Exception:
        pass
    return str(tok.get("access_token") or "")


def _ml_api_get(url: str) -> dict | list | None:
    token = ml_access_token()
    if not token:
        return None
    for attempt in (0, 1):
        try:
            req = Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "Hermes"})
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:  # refresh once on auth failure
            if attempt == 0 and "401" in str(exc):
                token = ml_access_token(force_refresh=True)
                continue
            return None
    return None


def _ml_fetch_html(link: str) -> str:
    try:
        req = Request(link, headers={"User-Agent": _ML_BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _ml_catalog_cache() -> dict:
    try:
        data = json.loads(ML_CATALOG_CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ml_catalog_cache_put(key: str, value: dict) -> None:
    try:
        cache = _ml_catalog_cache()
        cache[key] = {"at": time.time(), "data": value}
        ML_CATALOG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ML_CATALOG_CACHE_FILE.write_text(json.dumps(cache))
    except Exception:
        pass


def _fmt_brl(value: float | None) -> str | None:
    if value is None:
        return None
    txt = f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    txt = re.sub(r",00$", "", txt)
    return f"R$ {txt}"


def _tokens_overlap(a: str, b: str) -> float:
    ta = {t for t in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(t) > 2}
    tb = {t for t in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _distinctive_identity_tokens(text: str) -> set[str]:
    """Identity words that cannot be satisfied by generic perfume catalog prose."""
    generic = {
        "perfume", "arabe", "arab", "original", "masculino", "feminino",
        "unissex", "eau", "parfum", "toilette", "edp", "edt", "deo",
        "colonia", "100ml", "50ml", "75ml", "90ml", "ml",
    }
    return {
        token for token in product_key(text or "").split("-")
        if len(token) >= 4 and token not in generic and not token.isdigit()
    }


def offer_relation(source: dict, evidence: dict) -> str:
    """Classify whether two records describe one commercial offer.

    Product identity is deliberately weaker than offer identity. Price, Pix,
    installments, seller and official-store status may cross sources only when
    the listing item_id is identical, or when an exact-link resolver produced
    the evidence for a source whose item_id was initially unknown.
    """
    sid = str(source.get("item_id") or "")
    eid = str(evidence.get("item_id") or "")
    if sid and eid:
        return "same_offer" if sid == eid else "different_offer"
    if evidence.get("exact_link") and eid and not sid:
        return "same_offer"
    spid = str(source.get("product_id") or "")
    epid = str(evidence.get("product_id") or "")
    if spid and epid and spid == epid:
        return "same_product_only"
    return "unknown"


def resolve_offer_evidence(source: dict, evidences: list[dict]) -> dict:
    """Merge commercial facts only from evidence tied to the same offer.

    Returns the source facts plus an audit trail. Product-only/catalog evidence
    remains useful for title, availability and price-range checks, but cannot
    contribute price, De, Pix, installments, seller or store.
    """
    commercial = ("price", "original_price", "pix", "installments", "seller_id",
                  "seller_name", "official_store_id", "store_name")
    resolved = dict(source)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for evidence in evidences:
        relation = offer_relation(source, evidence)
        audit = {"source": evidence.get("source") or "unknown", "relation": relation}
        if relation != "same_offer":
            rejected.append(audit)
            continue
        fields = []
        conflicts = []
        for key in commercial:
            value = evidence.get(key)
            if value is None or value == "":
                continue
            # Price correction is stricter than enrichment. Matching evidence
            # confirms the source; a conflicting value is audited but does not
            # replace it unless the caller explicitly grants override authority
            # (exact listing API policy decides that before this join).
            if key in ("price", "original_price") and resolved.get(key) is not None:
                try:
                    same_value = abs(float(resolved[key]) - float(value)) < 1.5
                except (TypeError, ValueError):
                    same_value = resolved[key] == value
                if same_value:
                    fields.append(f"{key}_confirmed")
                    continue
                if not evidence.get("allow_price_override"):
                    conflicts.append(key)
                    continue
            resolved[key] = value
            fields.append(key)
        audit["fields"] = fields
        if conflicts:
            audit["conflicts"] = conflicts
        accepted.append(audit)
        if not source.get("item_id") and evidence.get("item_id"):
            source = dict(source, item_id=evidence["item_id"])
            resolved["item_id"] = evidence["item_id"]
    resolved["evidence_accepted"] = accepted
    resolved["evidence_rejected"] = rejected
    return resolved


def ml_catalog_lookup(link: str | None, source_de: str | None = None, source_por: str | None = None,
                      expected_product: str | None = None) -> dict:
    """Resolve a promo link to live catalog facts for the exact listing.

    Returns {} on any failure — callers must treat this as optional validation,
    never as a hard dependency. Never invents official-store status.
    """
    if not link:
        return {}
    # Cache identity must include resolver schema and source prices. Caching by
    # short link alone reused an old Eclaire resolution when that same social
    # link freshly selected Asad — the auxiliary layer overruled the real link.
    cache_key = "|".join((
        f"v{ML_CATALOG_CACHE_SCHEMA}", link,
        str(money_value(source_de) or ""), str(money_value(source_por) or ""),
        expected_product or "",
    ))
    cache = _ml_catalog_cache()
    hit = cache.get(cache_key)
    if hit and time.time() - float(hit.get("at") or 0) < ML_CATALOG_TTL_SECONDS:
        return dict(hit.get("data") or {})
    html_text = _ml_fetch_html(link)
    if not html_text:
        return {}
    # og:title = featured product name; used to verify we picked the right card.
    m = re.search(r'(?:og:title"\s*content="|"type":"og:title","content":")([^"]{8,240})', html_text)
    og_title = html.unescape(m.group(1)) if m else ""
    # Resolve product context and the selected listing as one identity step.
    # The resolver validates every candidate against item API catalog_product_id
    # and title before it is allowed to feed commercial fields.
    identity = resolve_link_offer(link, html_text, expected_product=expected_product)
    product_id = identity.get("product_id")
    item_id = identity.get("item_id")
    item_id_source = identity.get("item_id_source")
    exact_item_facts = extract_exact_item_facts(html_text, item_id) if identity.get("confidence") in ("exact", "exact_card") and item_id else {}
    unescaped = html.unescape(html_text)
    featured = {}

    def _feat_amt(seg: str, label: str) -> float | None:
        mm = re.search(label + r":\s*([\d.]+) reais(?: com (\d{1,2}) centavos)?", seg)
        if not mm:
            return None
        try:
            return float(mm.group(1).replace(".", "")) + float(mm.group(2) or 0) / 100.0
        except Exception:
            return None

    # Anchor for the featured-card scan. Preferred: the promoted item id from
    # the pdp_filters anchor. Some social pages (Attar Al Wesal 2026-07-09)
    # have NO pdp_filters at all — fall back to a distinctive token from
    # og:title (the featured product's name). The featured card renders first
    # in the page, so the first anchored window with a price is the card.
    anchor_pat = re.escape(item_id) if item_id else ""
    if not anchor_pat and og_title:
        _toks = [t for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", og_title)
                 if t.lower() not in {"perfume", "masculino", "feminino", "original", "importado", "kit"}]
        if _toks:
            anchor_pat = re.escape(_toks[0])
    if anchor_pat:
        # The anchor appears several times (anchor link, tracking, and the
        # rendered price card). Scan a window around EACH occurrence — the
        # aria-label prices live near a later occurrence than the anchor.
        for occ in re.finditer(anchor_pat, unescaped, re.I):
            seg = unescaped[max(0, occ.start() - 4000): occ.end() + 4000]
            if product_id:
                # Same-catalog guard (Alpine/Lipx 2026-07-10): recommendation
                # cards can be the SAME product name under a DIFFERENT catalog
                # page (/p/MLB66812580 Lipx vs featured /p/MLB22930391) with
                # their own Pix price. Wide windows may contain both ids, so
                # judge by the /p/ id NEAREST to this anchor occurrence.
                _best = None
                for _pm2 in re.finditer(r"/p/(MLB\d{6,})", seg):
                    _dist = abs(_pm2.start() - min(4000, occ.start()))
                    if _best is None or _dist < _best[0]:
                        _best = (_dist, _pm2.group(1))
                if _best and _best[1] != product_id:
                    continue
            price = _feat_amt(seg, "Agora") or _feat_amt(seg, "(?:Preço|Por)")
            if price:
                # Seller must come from the SAME polycard as the promoted
                # listing. On these social pages ALL prices sit in one HTML
                # block and ALL sellers in another, so positional/window
                # matching is a guess (Rasasi picked FINKÈ, Malbec got Pétalo
                # by luck, JPG named the brand with no real card — 2026-07-09).
                # The reliable key is the polycard JSON itself: each card is
                # brand→title→seller→price(current)→price(strikethrough "De").
                # Match the card whose STRIKETHROUGH price equals the source's
                # "De" AND whose title matches the product. Unique match wins;
                # otherwise omit — a missing store line beats a wrong seller.
                m_seller = None
                _prod_toks = {t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", og_title or "")
                              if t.lower() not in {"perfume", "masculino", "feminino", "original", "importado"}}
                _need = max(1, min(2, len(_prod_toks) - 1))
                _de_v = money_value(source_de) if source_de else None
                _por_v = money_value(source_por) if source_por else None
                if _prod_toks:
                    _titles = [(t.start(), t.group(1)) for t in re.finditer(r'"title":\{"text":"([^"]{8,140})"', unescaped)]
                    _cands = []  # (title_score, price_match, seller_match_obj)
                    for sm in re.finditer(r'"seller":\{"text":"Por ([^"{]{2,40})', unescaped):
                        prev = [t for t in _titles if t[0] < sm.start()]
                        if not prev:
                            continue
                        card_title = prev[-1][1].lower()
                        tscore = sum(1 for t in _prod_toks if t in card_title)
                        if tscore < _need:
                            continue
                        # Prices in this card's JSON, right after the seller.
                        # These are raw JSON floats ("261.79"): dot = decimal,
                        # so parse directly — money_value() would read the dot
                        # as a thousands separator (261.79 -> 26179).
                        _amts = []
                        for x in re.findall(r'"(?:amount|value|fraction)":\s*"?([\d.]+)', unescaped[sm.start():sm.start() + 900]):
                            try:
                                _amts.append(float(x))
                            except ValueError:
                                pass
                        pmatch = 0
                        # Source De is often rounded (card 214.90 published as
                        # "De 214"), so match on whole reais, not cents.
                        if _de_v and any(abs(a - _de_v) < 1.5 for a in _amts):
                            pmatch += 2
                        if _por_v and any(abs(a - _por_v) < 1.5 for a in _amts):
                            pmatch += 1
                        _cands.append((tscore, pmatch, sm))
                    if _cands:
                        # Best by (price match, then title match). Accept only a
                        # UNIQUE top card — ties mean we cannot tell which seller.
                        _cands.sort(key=lambda x: (-x[1], -x[0]))
                        top = _cands[0]
                        unique = sum(1 for c in _cands if c[0] == top[0] and c[1] == top[1]) == 1
                        # Require a price signal (De/Por match): a card without
                        # any price key may be a different offer of the same
                        # product (Mawwal Malek 2026-07-10: single Malek card
                        # "Por Mawwal" passed, but the buy-box seller of the
                        # /p/ catalog listing was NOT the official store —
                        # Breno saw no badge). Brand-only pages (JPG) have many
                        # same-brand cards and no price key must never resolve
                        # to a store. No price key => omit, even for a single
                        # matching card.
                        if unique and top[1] >= 1:
                            m_seller = top[2]
                m_inst = re.search(
                    r'poly-price__installments">(?:em\s*)?(\d{1,2})x\s*<[^>]*aria-label="([\d.]+) reais(?: com (\d{1,2}) centavos)?',
                    seg,
                )
                installments = ""

                def _card_bounded(text: str, start: int, size: int = 2000) -> str:
                    # A polycard JSON window must END at the next card's title:
                    # a raw N-char slice bleeds into the neighbour card and
                    # steals its installments (Mawwal Malek 2026-07-10: Malek
                    # card is 12x com juros, but neighbour Fada card's '6x sem
                    # juros' sat within 2000 chars — both priced ~R$199,90, so
                    # n*value≈price validation could not catch it either).
                    tail = text[start:start + size]
                    nxt = tail.find('"title":{"text":"', 1)
                    return tail[:nxt] if nxt > 0 else tail

                if m_seller:
                    # Preferred source: the matched polycard's own JSON carries
                    # '"installments":{"text":"... 10x ... sem juros",
                    # "no_interest":true}' (CH 212 case 2026-07-09: HTML
                    # poly-price missed it). Trust no_interest flag directly.
                    _card_tail = _card_bounded(unescaped, m_seller.start())
                    mj = re.search(r'"installments":\{"text":"[^"]*?(\d{1,2})x[^"]*"(?:[^}]*?"no_interest":\s*(true|false))?', _card_tail)
                    if mj and (mj.group(2) == "true" or (mj.group(2) is None and "sem juros" in (mj.group(0) or ""))):
                        installments = f"{int(mj.group(1))}x"
                if not installments and m_seller is None:
                    # The ±4000-char featured window contains NEIGHBOUR cards
                    # by construction (Mawwal 2026-07-10: the '6x sem juros'
                    # found here belonged to a Michael Kors card). Without a
                    # matched seller card there is no safe way to attribute an
                    # installments plan inside this window — publish nothing.
                    # The page-wide fallback below (exact title-token + price
                    # key, card-bounded) remains the only alternative source.
                    pass
                if not installments and m_inst and m_seller is not None:
                    # HTML poly-price fallback ONLY with an attributed card
                    # (m_seller). Unattributed: the ±4000 window holds
                    # neighbour cards, and same-brand siblings can share the
                    # exact price (Mawwal Fada 6x@33.32 = R$199,90 = Malek's
                    # price, 2026-07-10) so n*value≈price cannot disambiguate.
                    try:
                        _iv = float(m_inst.group(2).replace(".", "")) + float(m_inst.group(3) or 0) / 100.0
                        # Publish ONLY interest-free installments: n*value must
                        # equal the card price (±3%). Cards also show juros
                        # plans (Botica 12x R$19,79 on R$199,90 = 19% juros)
                        # which Breno does not want published.
                        if 0.97 <= (int(m_inst.group(1)) * _iv) / price <= 1.03:
                            installments = f"{int(m_inst.group(1))}x"
                    except Exception:
                        pass
                featured = {
                    "price": price,
                    "original_price": _feat_amt(seg, "Antes"),
                    "official_store_html": bool(re.search(r"(?i)\bloja oficial\b", seg)),
                    "seller_name": (m_seller.group(1).strip() if m_seller else ""),
                    "installments": installments,
                }
                if not item_id:
                    # og:title-anchored path: recover the listing id from the
                    # featured card window itself.
                    m_id = re.search(r"MLB-?(\d{8,})", seg)
                    if m_id:
                        item_id = "MLB" + m_id.group(1)
                        item_id_source = "featured_window_scrape"
                break
    if product_id and not item_id:
        # Fallback: only trust a wid that lives in the SAME href as the
        # featured product id — a global first-wid grab picks a
        # recommendation card (Kaidoxstoree false attribution on Musamam).
        m = re.search(
            rf'href="https://www\.mercadolivre\.com\.br/[^"]*?/p/{product_id}[^"]*?wid=(MLB\d{{8,}})',
            unescaped,
        )
        item_id = m.group(1) if m else None
        if item_id:
            item_id_source = "wid_href"
    def _exact_card_only_result() -> dict:
        if identity.get("confidence") != "exact_card" or not item_id or not exact_item_facts:
            return {}
        exact_badged_store = extract_ml_official_store_from_html(
            html_text, "", expected_product or "", str(item_id)
        )
        return {
            "product_id": product_id,
            "item_id": item_id,
            "item_id_source": item_id_source,
            "offer_identity_confidence": "exact_card",
            "offer_identity_rejected": identity.get("rejected_candidates") or [],
            "name": exact_item_facts.get("title") or expected_product,
            "product_status": None,
            "source": "exact_item_card",
            "price": exact_item_facts.get("current_price"),
            "original_price": exact_item_facts.get("original_price"),
            "price_source": "exact_item_card",
            "exact_item_facts": exact_item_facts,
            "price_modalities": exact_item_facts.get("price_modalities") or [],
            "pix_price": exact_item_facts.get("pix_price"),
            "other_methods_price": exact_item_facts.get("other_methods_price"),
            "installments": exact_item_facts.get("installments"),
            "seller_name": exact_item_facts.get("seller_name"),
            # Card-scoped proof: title + seller + official badge + exact wid all
            # live in the same featured card. Keep separate from numeric
            # official_store_id; do not fabricate an API id.
            "official_store_html_exact": bool(exact_badged_store),
            "store_name": exact_badged_store or None,
        }

    if not product_id:
        return _exact_card_only_result()
    prod = _ml_api_get(f"https://api.mercadolibre.com/products/{product_id}")
    if not isinstance(prod, dict) or not prod.get("name"):
        return _exact_card_only_result()
    name = str(prod["name"]).strip()
    _generic_identity_tokens = {
        "perfume", "arabe", "arab", "maison", "alhambra", "edp", "edt", "eau", "parfum",
        "masculino", "feminino", "unissex", "original", "100ml", "50ml", "75ml", "ml",
    }
    _expected_distinctive = {
        t for t in product_key(expected_product or "").split("-")
        if len(t) >= 4 and t not in _generic_identity_tokens and not t.isdigit()
    }
    _catalog_distinctive = {
        t for t in product_key(name).split("-")
        if len(t) >= 4 and t not in _generic_identity_tokens and not t.isdigit()
    }
    if (identity.get("confidence") == "exact_card" and expected_product
            and ((_expected_distinctive and _catalog_distinctive
                  and not (_expected_distinctive & _catalog_distinctive))
                 or product_variant_conflict(expected_product, name))):
        # The page-wide /p/ catalog id can belong to a recommendation while the
        # bounded promoted card proves another product. Never let that catalog
        # context overwrite the exact card (Sceptre Bronzite -> Alpine Homme).
        return _exact_card_only_result()
    # Guard: social pages list many recommendation cards. Only trust the pick
    # when catalog name matches the page's featured title.
    if og_title and _tokens_overlap(name, og_title) < 0.5:
        return _exact_card_only_result()
    out: dict = {
        "product_id": product_id,
        "item_id": item_id,
        "item_id_source": item_id_source,
        "offer_identity_confidence": identity.get("confidence"),
        "offer_identity_rejected": identity.get("rejected_candidates") or [],
        "name": name,
        "product_status": prod.get("status"),
        "source": "ml_catalog_api",
    }
    listings = _ml_api_get(
        f"https://api.mercadolibre.com/products/{product_id}/items?limit=50"
    )
    results = listings.get("results", []) if isinstance(listings, dict) else []
    prices = sorted(r["price"] for r in results if isinstance(r.get("price"), (int, float)))
    if prices:
        out["min_price"] = prices[0]
        out["max_price"] = prices[-1]
    target = next((r for r in results if r.get("item_id") == item_id), None)
    if target is not None and item_id_source in ("wid_href", "featured_window_scrape") and source_por:
        # Source-price sanity for href-derived wids (Nautica Voyage
        # 2026-07-10): the /p/ href carried a wid of a MORE EXPENSIVE offer
        # (R$138.95) while the source's R$98 was a real live offer of the same
        # catalog product. A wid scraped from markup is weaker evidence than
        # the seller's own stated price matching a live offer — publishing a
        # HIGHER price than the source is never an acceptable "adjustment".
        _spv = money_value(source_por)
        if _spv and isinstance(target.get("price"), (int, float)) and target["price"] > _spv * 1.02:
            if any(isinstance(r.get("price"), (int, float)) and abs(r["price"] - _spv) < 1.5 for r in results):
                target = None  # keep source price; wid was a different offer
    if target:
        out["price"] = target.get("price")
        out["original_price"] = target.get("original_price")
        out["official_store_id"] = target.get("official_store_id")
        out["seller_id"] = target.get("seller_id")
        out["free_shipping"] = bool((target.get("shipping") or {}).get("free_shipping"))
        out["price_source"] = "catalog_exact_item"
        if exact_item_facts:
            out["exact_item_facts"] = exact_item_facts
            out["price_modalities"] = exact_item_facts.get("price_modalities") or []
            if exact_item_facts.get("pix_price") is not None:
                out["pix_price"] = exact_item_facts["pix_price"]
                out["other_methods_price"] = exact_item_facts.get("other_methods_price")
            if exact_item_facts.get("installments_no_interest") and exact_item_facts.get("installments"):
                out["installments"] = exact_item_facts["installments"]
        if out.get("official_store_id") and target.get("seller_id"):
            # Stale-flag guard (Sieno case 2026-07-09): items can carry an
            # official_store_id whose brand is OFFLINE/dead. Require the
            # seller's brand record to be live before trusting the flag.
            brands = _ml_api_get(f"https://api.mercadolibre.com/users/{target['seller_id']}/brands")
            brand = None
            if isinstance(brands, dict):
                for b in brands.get("brands") or []:
                    if b.get("official_store_id") == out["official_store_id"]:
                        brand = b
                        break
            if brand and str(brand.get("status") or "").lower() not in ("offline", "inactive", "disabled"):
                if brand.get("fantasy_name") or brand.get("name"):
                    out["store_name"] = str(brand.get("fantasy_name") or brand.get("name"))
                else:
                    seller = _ml_api_get(f"https://api.mercadolibre.com/users/{target['seller_id']}")
                    if isinstance(seller, dict) and seller.get("nickname"):
                        out["store_name"] = str(seller["nickname"])
            else:
                out.pop("official_store_id", None)
                out["official_store_stale"] = True
    elif featured.get("price"):
        # Promoted listing is NOT among the catalog's returned items (paging,
        # policy wall). The social page's own featured card still shows the
        # real campaign price/original — use it instead of silently falling
        # back to buy-box/no-price. Sanity: must sit within (or near) the
        # catalog's price band.
        fp = float(featured["price"])
        lo = float(out.get("min_price") or 0) or None
        hi = float(out.get("max_price") or 0) or None
        if (lo is None or fp >= lo * 0.80) and (hi is None or fp <= hi * 1.05):
            _spv2 = money_value(source_por) if source_por else None
            _sdv2 = money_value(source_de) if source_de else None
            _fdv2 = float(featured["original_price"]) if featured.get("original_price") else None
            same_campaign_de = (
                _sdv2 is not None and _fdv2 is not None and abs(_sdv2 - _fdv2) < 1.5
            )
            if (_spv2 and fp > _spv2 * 1.02 and results and not same_campaign_de
                    and any(isinstance(r.get("price"), (int, float)) and abs(r["price"] - _spv2) < 1.5 for r in results)):
                # Never adjust the price UP over a verifiable source price
                # (Nautica 2026-07-10: card showed the R$138,95 offer while the
                # source's R$98 was a real live catalog offer). Keep the
                # source's price; drop card-derived extras that describe the
                # wrong offer.
                out["wrong_offer_card"] = True
            else:
                out["price"] = fp
                if featured.get("original_price"):
                    out["original_price"] = float(featured["original_price"])
                out["price_source"] = "exact_item_card" if identity.get("confidence") == "exact_card" else "social_featured_card"
                if identity.get("confidence") == "exact_card" and exact_item_facts:
                    out["exact_item_facts"] = exact_item_facts
                    out["price_modalities"] = exact_item_facts.get("price_modalities") or []
                    out["pix_price"] = exact_item_facts.get("pix_price")
                    out["other_methods_price"] = exact_item_facts.get("other_methods_price")
                    out["seller_name"] = exact_item_facts.get("seller_name")
                if featured.get("installments"):
                    out["installments"] = featured["installments"]
            if featured.get("official_store_html"):
                out["official_store_hint_html"] = True
                if identity.get("confidence") == "exact_card" and featured.get("seller_name"):
                    # Exact promoted card binds seller + official badge + item id;
                    # carry that proof into commercial mode even when /loja/<slug>
                    # cannot be resolved (OUD/Clash case).
                    out["official_store_html_exact"] = True
                    out["store_name"] = str(featured["seller_name"]).strip()
            # Confirm official-store status OUTSIDE the social card (Lipx
            # rule): the card names the seller ("Por KLASSEY ✔"); the seller's
            # /loja/<slug> page carries official_store_id when it is a real
            # official store. This is item-independent, public, and cheap.
            sname = str(featured.get("seller_name") or "").strip()
            _card_owns_offer = True
            if product_id and not target and identity.get("confidence") != "exact_card":
                # Catalog buy-box guard (Azzaro Chrome Legend 2026-07-10): a
                # /p/ destination without exact-item verification lands on the
                # buy box, which may belong to another seller than the social
                # card ("Por Azzaro ✔" was real, but the buy box was ordinary
                # sellers at R$207-219). Verify by PRICE IDENTITY against the
                # catalog offers: the card price must match an offer owned by
                # an official store; matching an offer WITHOUT official store
                # => the destination offer is not the brand's => omit.
                _card_owns_offer = False
                _fpv = float(featured.get("price") or 0)
                if _fpv and results:
                    _pm = [r for r in results if isinstance(r.get("price"), (int, float)) and abs(r["price"] - _fpv) < 1.5]
                    if _pm and all(r.get("official_store_id") for r in _pm):
                        _card_owns_offer = True
                if not _card_owns_offer and results:
                    # Buy-box proxy (Le Beau 2026-07-10): the campaign/Pix card
                    # price may not appear among catalog list prices at all.
                    # The buyer still lands on the buy box ≈ the cheapest
                    # offers. If ALL of the cheapest offers (within 3% of the
                    # minimum) are official stores, the destination is an
                    # official store regardless of which one wins. Azzaro
                    # stays correctly omitted: its cheapest offers had no
                    # official_store_id.
                    _priced = [r for r in results if isinstance(r.get("price"), (int, float))]
                    if _priced:
                        _min = min(r["price"] for r in _priced)
                        _cheap = [r for r in _priced if r["price"] <= _min * 1.03]
                        if _cheap and all(r.get("official_store_id") for r in _cheap):
                            _card_owns_offer = True
            if sname and not out.get("official_store_id") and _card_owns_offer:
                # /loja/ slugs vary: "Rocha Importados" lives at
                # rocha-importados (hyphenated), not rochaimportados
                # (2026-07-09). Try hyphenated first, then collapsed.
                base = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", sname).encode("ascii", "ignore").decode().lower()).strip()
                slugs = []
                if base:
                    slugs.append(base.replace(" ", "-"))
                    collapsed = base.replace(" ", "")
                    if collapsed not in slugs:
                        slugs.append(collapsed)
                for slug in slugs:
                    store_html = _ml_fetch_html(f"https://www.mercadolivre.com.br/loja/{slug}")
                    m_sid = re.search(r'official_store_id\\?"?\s*:\s*(\d{3,10})', store_html or "")
                    if m_sid and re.search(r"(?i)\bloja oficial\b", store_html):
                        out["official_store_id"] = int(m_sid.group(1))
                        out["store_name"] = sname
                        out["official_store_confirmed_via"] = "loja_page"
                        break
    # Installments are accepted only from extract_exact_item_facts(), whose
    # block is bounded by the resolved item_id. No page-wide/title-price
    # fallback: siblings can share names and prices (Mawwal Malek/Fada).
    if (identity.get("confidence") == "exact_card" and item_id
            and exact_item_facts.get("seller_name") and not out.get("official_store_id")):
        exact_badged_store = extract_ml_official_store_from_html(
            html_text, str(exact_item_facts.get("seller_name") or ""),
            expected_product or str(exact_item_facts.get("title") or ""), str(item_id)
        )
        if exact_badged_store:
            out["official_store_html_exact"] = True
            out["store_name"] = exact_badged_store
    if not out.get("official_store_id") and not product_id:
        # Page-wide official-store fallback: ONLY for direct item links (no
        # catalog /p/ destination). On catalog pages the buyer lands on the
        # buy box, which a same-brand polycard cannot vouch for (Azzaro
        # 2026-07-10). Original motivation (Montblanc/ERIAN) also involved a
        # /p/ page, so that case now correctly omits too — melhor faltar
        # linha do que loja mentirosa.
        _ptoks2 = {t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", og_title or "")
                   if t.lower() not in {"perfume", "masculino", "feminino", "original", "importado"}}
        _sd = money_value(source_de) if source_de else None
        _sp = money_value(source_por) if source_por else None
        if _ptoks2 and (_sd is not None or _sp is not None):
            _t3 = [(t.start(), t.group(1)) for t in re.finditer(r'"title":\{"text":"([^"]{8,140})"', unescaped)]
            _matches = []
            for sm in re.finditer(r'"seller":\{"text":"Por ([^"{]{2,40})', unescaped):
                prev = [t for t in _t3 if t[0] < sm.start()]
                if not prev or sum(1 for t in _ptoks2 if t in prev[-1][1].lower()) < len(_ptoks2):
                    continue
                _tail = unescaped[sm.start():sm.start() + 2000]
                _nxt = _tail.find('"title":{"text":"', 1)
                if _nxt > 0:
                    _tail = _tail[:_nxt]
                if "icon_cockade" not in _tail[:400]:
                    continue
                _amts = [float(x) for x in re.findall(r'"value":\s*([\d.]+)', _tail) if x.replace(".", "").isdigit()]
                if not any(a is not None and any(abs(v - a) < 1.5 for v in _amts) for a in (_sd, _sp) if a is not None):
                    continue
                _matches.append(sm.group(1).strip())
            if len(set(_matches)) == 1:
                sname = _matches[0]
                base = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", sname).encode("ascii", "ignore").decode().lower()).strip()
                for slug in ([base.replace(" ", "-")] + ([base.replace(" ", "")] if base.replace(" ", "") != base.replace(" ", "-") else [])):
                    store_html = _ml_fetch_html(f"https://www.mercadolivre.com.br/loja/{slug}")
                    m_sid = re.search(r'official_store_id\\?"?\s*:\s*(\d{3,10})', store_html or "")
                    if m_sid and re.search(r"(?i)\bloja oficial\b", store_html):
                        out["official_store_id"] = int(m_sid.group(1))
                        out["store_name"] = sname
                        out["official_store_confirmed_via"] = "loja_page_pagewide_card"
                        break
    _ml_catalog_cache_put(cache_key, out)
    return out


def extract_ml_seller(jina_text: str) -> str:
    """Extract seller/store name from the ML featured product card."""
    if not jina_text:
        return ""
    block = re.split(r"(?i)\nPara você\n|\n## Quem viu|affiliate.profile.recommendations|\nMAIS VENDIDO ", jina_text, maxsplit=1)[0]
    # ML social/card pages commonly render: "Por Rocha Importados   R$214,99".
    m = re.search(r"(?i)\bPor\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{2,60}?)\s+(?:~~\s*)?R\$", block)
    if not m:
        m = re.search(r"(?i)\bVendido\s+por\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{2,60})(?:\n|\s{2,}|$)", block)
    if not m:
        # ML affiliate/social cards can render the featured seller as:
        # "Por KID'S LIFE \nFrete grátis..." with no price on the same line.
        # This is still only seller extraction; extract_trust uses it for a
        # trust line only when the source explicitly says "Loja Oficial no ML".
        m = re.search(r"(?i)\bPor\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &'_.-]{2,60}?)(?:\s*\n|\s{2,}|$)", block)
    if not m:
        return ""
    seller = re.sub(r"\s+", " ", m.group(1)).strip(" -–—,.")
    seller = re.sub(r"(?i)\s*(?:frete\s+gr[aá]tis|mais\s+vendidos?|r\$|pre[cç]o).*$", "", seller).strip(" -–—,.")
    if re.search(r"(?i)^(?:ser\s+sua\s+primeira\s+compra|sua\s+primeira\s+compra|primeira\s+compra)\b", seller):
        return ""
    return seller[:50]


def extract_ml_official_store_name(jina_text: str) -> str:
    """Extract explicit 'Loja oficial X' from ML/Jina page text."""
    if not jina_text:
        return ""
    block = re.split(r"(?i)\nPara você\n|\n## Quem viu|affiliate.profile.recommendations|\nMAIS VENDIDO ", jina_text, maxsplit=1)[0]
    patterns = [
        r"(?i)\bLoja\s+oficial\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{1,60}?)(?:\s*\+\s*[0-9]|\n|\s{2,}|$)",
        r"(?i)\bVendido\s+por\s+Loja\s+Oficial\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{1,60}?)(?:\s*\+\s*[0-9]|\n|\s{2,}|$)",
        r"(?i)\bSelecione\s+a\s+loja\s+oficial\s+([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{1,60}?)(?:\n|\s{2,}|$)",
    ]
    for pat in patterns:
        m = re.search(pat, block)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -–—,.")
        name = re.sub(r"(?i)\s*(?:no\s+ML|mercado\s+livre|\+.*)$", "", name).strip(" -–—,.")
        if name and name.lower() not in {"no", "ml", "mercado livre"}:
            return canonical_brand(name)
    return ""


def ml_raw_html(link: str | None) -> str:
    if not link:
        return ""
    try:
        req = Request(link, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=12) as resp:
            raw = resp.read(400_000)
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def extract_ml_official_store_from_html(html_text: str, expected_seller: str = "", expected_product: str = "",
                                        expected_item_id: str = "") -> str:
    if not html_text:
        return ""
    matches: list[tuple[str, str, bool, str]] = []
    # ML social cards render title + seller + official badge in the same poly-card.
    # Parse per card; a broad regex across the whole page can pair the featured
    # product title with an unrelated official recommendation below it.
    for chunk in re.split(r"(?is)<div\s+class=[\"']poly-card\b", html_text):
        if not re.search(r"(?is)aria-label=[\"'](?:Loja oficial|Tienda oficial)[\"']", chunk):
            continue
        title_m = re.search(r"(?is)class=[\"'][^\"']*poly-component__title[^\"']*[\"'][^>]*>\s*(.*?)\s*</a>", chunk)
        seller_m = re.search(r"(?is)Por\s+([^<]{2,80}?)\s*<svg[^>]+aria-label=[\"'](?:Loja oficial|Tienda oficial)[\"']", chunk)
        if not (title_m and seller_m):
            continue
        card_title = re.sub(r"<[^>]+>", " ", title_m.group(1))
        card_title = re.sub(r"\s+", " ", card_title).strip()
        name = re.sub(r"\s+", " ", seller_m.group(1)).strip(" -–—,.")
        is_featured = bool(re.search(r"(?is)(/home/card-featured|reco_item_pos=0|card-featured)", chunk))
        wid_m = re.search(r"(?i)[?&](?:amp;)?wid=(MLB\d{8,})", chunk)
        card_item_id = wid_m.group(1).upper() if wid_m else ""
        if name:
            matches.append((canonical_brand(html.unescape(name)), card_title, is_featured, card_item_id))
    # Fallback for compact snippets where only seller+badge are available.
    for m in re.finditer(r"(?is)Por\s+([^<]{2,80}?)\s*<svg[^>]+aria-label=[\"'](?:Loja oficial|Tienda oficial)[\"']", html_text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -–—,.")
        if name:
            matches.append((canonical_brand(html.unescape(name)), "", False, ""))
    if matches:
        # Strong proof on social pages requires the featured card to bind the
        # official badge and seller to the exact resolved listing id (wid).
        if expected_item_id:
            expected_iid = expected_item_id.replace("-", "").upper()
            for name, _title, is_featured, card_item_id in matches:
                if is_featured and card_item_id == expected_iid:
                    return name
        if expected_seller:
            exp = canonical_brand(expected_seller).lower()
            for name, _title, _is_featured, _item_id in matches:
                if name.lower() == exp:
                    return name
        expected_tokens = set(product_key(expected_product).split("-")) if expected_product else set()
        expected_tokens = {tok for tok in expected_tokens if len(tok) >= 4}
        if expected_tokens:
            # If this is an ML social/affiliate page with a featured CTA card,
            # only the featured card can prove the promoted product's official
            # store. Recommendations below often repeat the same product/family
            # with a different seller; accepting them creates false official lines.
            has_featured_page = bool(re.search(r"(?is)(/home/card-featured|card-featured|Ir\s+para\s+produto)", html_text))
            for name, card_title, is_featured, _item_id in matches:
                title_tokens = {tok for tok in product_key(card_title).split("-") if len(tok) >= 4}
                overlap = len(expected_tokens & title_tokens)
                if is_featured and overlap >= 2:
                    return name
            if has_featured_page:
                return ""
            for name, card_title, is_featured, _item_id in matches:
                title_tokens = {tok for tok in product_key(card_title).split("-") if len(tok) >= 4}
                if len(expected_tokens & title_tokens) >= min(3, max(1, len(expected_tokens))):
                    return name
        return ""
    # Plain text fallback only when it matches the expected seller; raw ML HTML
    # contains unrelated official-store recommendations.
    m = re.search(r"(?is)Loja\s+oficial\s+([^<\n]{2,80})", html_text)
    if m and expected_seller:
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -–—,.")
        name = re.sub(r"(?i)\s*(?:no\s+ML|mercado\s+livre|\+.*)$", "", name).strip(" -–—,.")
        if name and canonical_brand(name).casefold() == canonical_brand(expected_seller).casefold():
            return canonical_brand(name)
    return ""


def format_official_store_trust(store: str) -> str:
    # Preserve exact seller identities; product-brand canonicalization is not valid here.
    raw_store = strip_wa(store or "")
    raw_store = re.sub(r"(?i)^\s*loja\s+oficial\s+", "", raw_store)
    raw_store = re.sub(r"(?i)\s+(?:no\s+(?:ML|MELI)|mercado\s+livre)\s*$", "", raw_store)
    raw_store = re.sub(r"\s+", " ", raw_store).strip(" -–—,._")
    # Marketplace APIs often shout seller names in all caps. Preserve the exact
    # seller identity, but present it as normal title case: "WATHIQ PARFUMS" ->
    # "Wathiq Parfums", "KID'S LIFE" -> "Kid's Life".
    letters = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]", "", raw_store)
    if letters and letters == letters.upper():
        raw_store = raw_store.title()
        raw_store = re.sub(r"'S\b", "'s", raw_store)
    store = raw_store or canonical_brand(store)
    suffix = " no ML" if len(f"Loja Oficial {store} no ML") <= 33 else ""
    return f"_Loja Oficial *{store}*{suffix}_"


def rendered_card_store_trust(browser_card: dict) -> str | None:
    """Publish an exact-card verified seller badge as Loja Oficial.

    Breno's commercial rule treats ML's verified seller badge as sufficient for
    the public Loja Oficial line. The badge and seller must still be scoped to
    the same featured card or exact PDP seller block; page-wide badges and
    recommendation cards remain invalid.
    """
    if browser_card.get("official_store") and browser_card.get("seller"):
        return format_official_store_trust(str(browser_card["seller"]))
    if browser_card.get("pdp_seller_badged") and browser_card.get("pdp_seller"):
        return format_official_store_trust(str(browser_card["pdp_seller"]))
    return None


def extract_trust(body: str, product: str, jina_text: str = "") -> str | None:
    text = strip_wa(body + "\n" + (jina_text or ""))
    official_store = extract_ml_official_store_name(jina_text)
    if official_store:
        return format_official_store_trust(official_store)
    # only official line gets trust line. Verificada/vendedor is not the requested trust line.
    m = re.search(r"(?i)loja\s+oficial\s+([A-Za-zÀ-ÿ0-9 &_.-]{2,45}?)(?:\s+no\s+ML|\s+ML\b|\s+no\s+mercado\s+livre|\s+mercado\s+livre|$)", text)
    if m:
        brand = canonical_brand(m.group(1))
        if brand and brand.lower() not in {"no", "ml", "mercado livre"}:
            return format_official_store_trust(brand)
    # Source copy may say "Selecione a loja oficial Rabanne" without "no ML".
    m = re.search(r"(?i)(?:selecione\s+a\s+)?loja\s+oficial\s+([A-Za-zÀ-ÿ0-9 &_.-]{2,45})(?:\n|$)", text)
    if m:
        brand = canonical_brand(m.group(1))
        if brand and brand.lower() not in {"no", "ml", "mercado livre"}:
            return format_official_store_trust(brand)
    # Do not infer official status from generic lines like "Loja Verificada no ML"
    # or "Loja <seller> no ML". Breno wants a trust line only when the source/ML
    # explicitly says loja oficial, or the ML API exposes official_store_id.
    # Source often says only "Loja Oficial no ML". Do NOT infer brand from the
    # product (Rasasi/Hawas etc.); use the actual ML seller/card when available.
    if re.search(r"(?i)\b(?:loja|vendido\s+por)\s+oficial\s+no\s+ML\b", text):
        seller = extract_ml_seller(jina_text)
        if seller:
            return format_official_store_trust(seller)
    direct_url = extract_ml_product_url(jina_text, extract_link(body))
    seller = extract_ml_seller(jina_text)
    # 2026-07-08 (Lipx false positive): the social/affiliate featured card can
    # carry the poly_cockade badge labeled "Loja oficial" for sellers that are
    # NOT official stores on the real item page. Social-page HTML evidence is
    # therefore only a HINT; it must be confirmed on the item page itself.
    social_hint = ""
    for html_url in [extract_link(body), direct_url]:
        if not html_url:
            continue
        page_html = ml_raw_html(html_url)
        offer = resolve_link_offer(html_url, page_html, expected_product=product)
        exact_item_id = str(offer.get("item_id") or "")
        store = extract_ml_official_store_from_html(page_html, seller, product, exact_item_id)
        if not store:
            continue
        is_social_html = bool(re.search(r"(?is)(affiliate-profile|/social/|card-featured)", page_html))
        if is_social_html:
            # The featured card's wid, seller and official badge are scoped to
            # one exact offer. Loose/unbound social badges remain hints only.
            if exact_item_id:
                return format_official_store_trust(store)
            social_hint = store
            continue
        return format_official_store_trust(store)
    if social_hint and direct_url:
        item_html = ml_raw_html(direct_url)
        if item_html and not re.search(r"(?is)(affiliate-profile|/social/|card-featured)", item_html):
            confirmed = extract_ml_official_store_from_html(item_html, social_hint, product)
            if confirmed:
                return format_official_store_trust(confirmed)
        # Second confirmation path: Jina markdown of the item page. Raw item
        # HTML is often JS-gated; Jina renders it. Still item-page evidence,
        # never the social card itself, so it cannot re-introduce the Lipx bug.
        item_jina = jina_fetch(direct_url)
        if item_jina and not re.search(r"(?i)(Perfil Social|/social/|affiliate-profile)", item_jina):
            m = re.search(r"(?i)\bloja\s+oficial\b[^\n]{0,80}", strip_wa(item_jina))
            if m and canonical_brand(social_hint).lower() in m.group(0).lower():
                return format_official_store_trust(social_hint)
            jina_store = extract_ml_official_store_name(item_jina)
            if jina_store and canonical_brand(jina_store).lower() == canonical_brand(social_hint).lower():
                return format_official_store_trust(jina_store)
    ok, api_brand = ml_api_official_store(direct_url, jina_text)
    if ok:
        return format_official_store_trust(api_brand or product_brand(product, jina_text))
    # Do NOT infer official store from seller/card text like "Por Natura" or
    # "Por O Boticário". ML social pages mix cards/recommendations and this
    # caused false official-store claims. Official trust requires explicit
    # "Loja oficial ..." text or ML API official_store_id above.
    return None


JINA_CACHE_DIR = Path(os.path.expanduser("~/.hermes/perfume_image_bank/.jina_cache"))
JINA_CACHE_TTL = 15 * 60  # same-promo reruns within 15min reuse the page


def jina_fetch(link: str | None) -> str:
    if not link:
        return ""
    # 2026-07-07: short cache. The normalizer can hit the same link 2-3x per
    # promo (price + product + store). Caching keeps validation consistent
    # within one promo and cuts Jina latency/failures.
    cache_path = None
    try:
        import hashlib as _hashlib

        JINA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = JINA_CACHE_DIR / (_hashlib.sha256(link.encode("utf-8")).hexdigest()[:24] + ".md")
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < JINA_CACHE_TTL:
            cached = cache_path.read_text(encoding="utf-8", errors="replace")
            if cached.strip():
                return cached
    except Exception:
        cache_path = None
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "18", "-H", "Accept: text/markdown", f"https://r.jina.ai/{link}"],
            text=True, capture_output=True, timeout=22,
        )
        out = r.stdout if r.returncode == 0 else ""
        if out.strip() and cache_path is not None:
            try:
                cache_path.write_text(out, encoding="utf-8")
            except Exception:
                pass
        return out
    except Exception:
        return ""


def jina_price(jina: str) -> str | None:
    if not jina:
        return None
    if re.search(r"(?i)(Perfil Social|/social/|affiliate-profile|Her[oó]i das Promo)", jina):
        return None
    # Conservative: first R$ near price markers; ML markdown often includes visible current price.
    for pat in [r"(?i)(?:pre[cç]o|por|agora|pix)[^\n]{0,80}R\$\s*([0-9][0-9.,]*)", r"R\$\s*([0-9][0-9.,]*)"]:
        m = re.search(pat, jina)
        if m:
            return money_norm(m.group(1))
    return None


def social_primary_offer_prices(jina: str) -> tuple[str | None, str | None, str | None]:
    """Extract only the featured card above 'Ir para produto' on ML social pages."""
    if not re.search(r"(?i)(Perfil Social|/social/|affiliate-profile|Her[oó]i das Promo)", jina or ""):
        return None, None, None
    # Trust only the first featured block that has the explicit CTA. Never parse
    # recommendation cards under "Quem viu este produto também comprou".
    block = re.split(r"(?i)\nPara você\n|\n## Quem viu|affiliate.profile.recommendations|\nMAIS VENDIDO ", jina, maxsplit=1)[0]
    if "Ir para produto" not in block:
        return None, None, None
    m = re.search(r"(?i)\bPor\s+[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{2,60}?\s+R\$\s*([0-9][0-9.,]*)", block)
    if m:
        return None, money_norm(m.group(1)), None
    m = re.search(r"~~\s*R\$\s*([0-9][0-9.,]*)\s*~~.{0,180}?R\$\s*([0-9][0-9.,]*)[^\n]*(?:no Pix|Pix).{0,120}?ou\s+R\$\s*([0-9][0-9.,]*)\s+em(?:\s+outros\s+meios|\s+\d{1,2}x)", block, re.I | re.S)
    if m:
        return money_norm(m.group(1)), money_norm(m.group(2)), money_norm(m.group(3))
    m = re.search(r"~~\s*R\$\s*([0-9][0-9.,]*)\s*~~.{0,180}?R\$\s*([0-9][0-9.,]*)", block, re.I | re.S)
    if m:
        return money_norm(m.group(1)), money_norm(m.group(2)), None
    m = re.search(r"R\$\s*([0-9][0-9.,]*)[^\n]*(?:no Pix|Pix)", block, re.I)
    if m:
        return None, money_norm(m.group(1)), None
    m = re.search(r"(?i)\bPor\s+[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &_.-]{2,60}?\s+R\$\s*([0-9][0-9.,]*)", block)
    if m:
        return None, money_norm(m.group(1)), None
    # Featured-card social pages often show a plain product price before
    # "Ir para produto" with no Pix marker. Count it as link/product validation;
    # normalize_copy will avoid overwriting lower coupon prices with this value.
    prices = re.findall(r"R\$\s*([0-9][0-9.,]*)", block)
    if prices:
        return None, money_norm(prices[0]), None
    return None, None, None


def jina_offer_prices(jina: str) -> tuple[str | None, str | None, str | None]:
    """Extract ML offer prices from direct product page or trusted featured card."""
    if not jina:
        return None, None, None
    social_prices = social_primary_offer_prices(jina)
    if social_prices[0] or social_prices[1]:
        return social_prices
    # If a meli.la link resolves to a social/profile page but no trusted featured
    # CTA block is found, do not use recommendation-card prices as validation.
    if re.search(r"(?i)(Perfil Social|/social/|affiliate-profile|Her[oó]i das Promo)", jina):
        return None, None, None
    m = re.search(r"~~\s*R\$\s*([0-9][0-9.,]*)\s*~~.{0,120}?R\$\s*([0-9][0-9.,]*)[^\n]*(?:no Pix|Pix).{0,120}?ou\s+R\$\s*([0-9][0-9.,]*)\s+em(?:\s+outros\s+meios|\s+\d{1,2}x)", jina, re.I | re.S)
    if m:
        return money_norm(m.group(1)), money_norm(m.group(2)), money_norm(m.group(3))
    m = re.search(r"~~\s*R\$\s*([0-9][0-9.,]*)\s*~~.{0,120}?R\$\s*([0-9][0-9.,]*)", jina, re.I | re.S)
    if m:
        return money_norm(m.group(1)), money_norm(m.group(2)), None
    m = re.search(r"R\$\s*([0-9][0-9.,]*)[^\n]*(?:no Pix|Pix)", jina, re.I)
    if m:
        return None, money_norm(m.group(1)), None
    return None, None, None


def lightweight_final_price_check(raw_por: str | None, jina_por: str | None, jina_other: str | None, cupons: list[str], pix_evidence: bool) -> dict:
    """Fast, non-checkout validation for coupon/Pix final prices.

    This deliberately does not claim checkout verification. It only says whether
    the editorial price is mathematically plausible from the public ML/Pix price
    plus coupon. Breno wants the copy to use the calculated/editorial final price,
    while the report names this as the lightweight layer.
    """
    if not raw_por:
        return {"status": "none", "label": "sem preço final editorial"}
    rv = money_value(raw_por)
    jv = money_value(jina_por)
    ov = money_value(jina_other)
    if rv is None:
        return {"status": "none", "label": "preço editorial ilegível"}
    if cupons and jv is not None and rv < jv:
        discount = (1 - (rv / jv)) * 100 if jv else 0
        base = "preço Pix público" if pix_evidence else "preço público ML/Jina"
        if discount > 35:
            severity = "implausible_final_light" if discount > 50 else "high_coupon_discount_light"
            return {
                "status": severity,
                "label": f"camada leve: preço editorial {raw_por} exige {discount:.1f}% OFF sobre {base} {jina_por}; confirmar no checkout",
                "base_price": jina_por,
                "editorial_price": raw_por,
                "inferred_discount_pct": round(discount, 1),
            }
        return {
            "status": "probable_final_light",
            "label": f"camada leve: usei preço editorial {raw_por}; compatível com cupom sobre {base} {jina_por} ({discount:.1f}% OFF inferido); não é checkout",
            "base_price": jina_por,
            "editorial_price": raw_por,
            "inferred_discount_pct": round(discount, 1),
        }
    if jv is not None and abs(rv - jv) <= 1.0:
        return {
            "status": "listing_match_light",
            "label": f"camada leve: preço editorial bate com ML/Jina público ({raw_por}); não é checkout",
            "base_price": jina_por,
            "editorial_price": raw_por,
        }
    if cupons and jv is not None and rv >= jv:
        return {
            "status": "conflict_light",
            "label": f"camada leve: cupom informado, mas preço editorial {raw_por} não ficou menor que preço público {jina_por}; precisa checkout",
            "base_price": jina_por,
            "editorial_price": raw_por,
        }
    if (ov is not None and pix_evidence and jv is not None and jv < ov
            and abs(rv - ov) <= 1.0):
        return {
            "status": "pix_listing_light",
            "label": f"camada leve: Pix público vantajoso confirmado ({jina_por} vs {jina_other}); cupom/final não confirmado em checkout",
            "base_price": jina_por,
            "other_methods_price": jina_other,
            "editorial_price": raw_por,
        }
    return {"status": "unverified_light", "label": "camada leve: sem evidência pública suficiente; não é checkout"}


def availability_ok(jina: str) -> bool:
    lo = (jina or "").lower()
    if not lo:
        return True
    return not any(x in lo for x in ["esgotado", "indisponível", "indisponivel", "anúncio pausado", "produto não encontrado"])


# One rendered emoji can cost several codepoints: 🤌🏻 is base+skin-tone (2) and
# 🤵🏻‍♂️ is a ZWJ sequence (5). Measuring headlines with len() therefore charged
# Breno's own signature emoji up to 5 characters of a 26-29 char budget, so his
# approved "BAUNILHA COM FAVA É O DOM 🤌🏻" (25 visible chars + 1 glyph) measured
# 28 and was REJECTED outright, while a plain 🍃 (1 codepoint) passed. That
# silently biased the automation toward simple emoji and against his favourite.
_EMOJI_CLUSTER_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    r"[\U0001F3FB-\U0001F3FF]?\uFE0F?"
    r"(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    r"[\U0001F3FB-\U0001F3FF]?\uFE0F?)*"
)


def editorial_cycle_eligible(row: dict) -> bool:
    # Explicit Breno correction: Waha was a different product, so that audit row
    # must not influence learning/repertoire/statistics. Cebolinha's "CÍTLICO"
    # is intentional character wordplay (R -> L) and remains eligible.
    return str(row.get('cycle_id') or '') != '0748bb169f7088edfb94'


def headline_visual_len(text: str) -> int:
    """Length as WhatsApp renders it: every emoji cluster counts as one glyph."""
    return len(_EMOJI_CLUSTER_RE.sub("\u0001", text or ""))


def clean_headline(text: str, raw_headline: str = "") -> str:
    h = strip_wa(text or "").strip().strip('"').strip("'")
    h = re.sub(r"\s+", " ", h)
    h = h.splitlines()[0].strip() if h else ""
    h = re.sub(r"(?i)^headline\s*:\s*", "", h).strip()
    h = h.strip("* ").upper()
    bad = {
        "BAIXOU", "PROMOCAO", "PROMOÇÃO", "IMPERDIVEL", "IMPERDÍVEL", "PRECAO", "PREÇÃO",
        "PERFUME BOM NO PREÇO", "PERFUME NO PREÇO", "AROMÁTICO MARCANTE", "FRESCO MARCANTE",
        "NATURA NO PREÇO", "BOTICÁRIO NO PREÇO", "IMPORTADO PRA APROVEITAR",
        "PERFUME PRA REPETIR ELOGIO", "PERFUME PARA REPETIR ELOGIO", "AROMA ELEGANTE",
        "CHEIRO DE CAMISA BEM PASSADA", "CHEIRO DE ALFAFA", "ALFAFA", "DIFERENTÃO",
        "ARABE VERDE DIFERENTAO", "ÁRABE VERDE DIFERENTÃO", "PERFUME TOP",
        # Replaced manually by Breno in recent Edição cycles: generic molds,
        # not reusable fallback copy.
        "NACIONAL COM CARA DE CARO", "ÁRABE COM CARA DE CARO",
        "ACHADO PRA SAIR DO ÓBVIO", "ARABE OPULENTO", "ÁRABE OPULENTO",
        "BOTICÁRIO COM CHEIRO DE CARO", "CHEIRO DE PERFUME CARO",
        "KIT PRESENTE PRA APROVEITAR", "LUXO DE DUBAI",
        "INTENSO PARA NOITE", "CHEIRO DE DUBAI NO PREÇO",
        "ÁRABE DE FIXAÇÃO BRUTA", "TROPICAL CHIQUE",
    }
    # WhatsApp card width is brutal on mobile: ~30 chars in bold uppercase can
    # already wrap, and a trailing emoji often leaks to a second line. Keep the
    # runtime stricter than the LLM prompt.
    _vlen = headline_visual_len(h)
    if not h or _vlen > 29:
        # 32 still wrapped on mobile. Keep a tight 29-char ceiling: this admits
        # Breno's approved "OS SAGAZES APROVEITAM O PREÇO" (29) without reopening
        # the long-copy failure. Emoji headlines retain the stricter 26 below.
        return ""
    # Truncated/ellipsis salvage junk ("O AZULZINHO DO...", "NÃO É ... NÃO")
    # must never be published.
    if "..." in h or "…" in h:
        return ""
    # Any emoji on a long headline risks wrapping to a second line on mobile.
    # Emoji itself is welcome (Breno: coherent aroma emojis like 🍃/🌊 are good);
    # only the length+emoji combination is rejected. Measured visually so a
    # skin-toned or ZWJ emoji is not charged 2-5 characters of the budget.
    # Ceiling raised 26 -> 27 on evidence: Breno published and approved
    # "BAUNILHA COM FAVA É O DOM 🤌🏻" (visual 27) himself on 2026-08-02, so 26 was
    # rejecting real, proven-good copy. The old limit was set while emoji still
    # inflated len(), which double-counted the risk.
    if _vlen > 27 and re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", h):
        return ""
    # Numeric R$ is syntactically valid; headline_score() is the commercial gate
    # and vetoes it unless this exact offer was classified as exceptional.
    # Keeping R$ blocked here made the new tested policy impossible to execute.
    if re.search(r"https?://|\b(cupom|pix|meli\.la)\b", h, re.I):
        return ""

    raw = strip_wa(raw_headline or "").strip().strip("* ").upper()
    if raw and h == raw:
        return ""
    if raw:
        # Do not disguise source-copying as a new headline. Exact equality was
        # already blocked, but "O FAMOSO QUASAR PAU PRA TODA OBRA..." became
        # "QUASAR PRA TODA OBRA". Reject any copied contiguous 3-word phrase;
        # sharing only the product name remains allowed.
        raw_words = re.findall(r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ0-9]+", raw)
        h_words = re.findall(r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ0-9]+", h)
        raw_trigrams = {tuple(raw_words[i:i + 3]) for i in range(max(0, len(raw_words) - 2))}
        h_trigrams = {tuple(h_words[i:i + 3]) for i in range(max(0, len(h_words) - 2))}
        if raw_trigrams & h_trigrams:
            return ""
    lexical = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\ufe0f\u200d]", "", h).strip()
    if h in bad or lexical in bad:
        return ""
    # Meta-commentary about the generator/QC is never customer-facing copy.
    # It leaked as "SEM NOTA INVENTADA: CK BE" after an LLM retry.
    if re.search(r"\b(NOTA\s+INVENTADA|SEM\s+NOTA\s+INVENTADA|HEADLINE|LLM|FALLBACK|REPROVAD[AO])\b", h):
        return ""
    if re.search(r"\bN[ÓO]S\s+DEIXA\b", h):
        return ""
    # Renewal once promoted the grammatically broken "DOIS EGEO" beside the
    # human-approved "DOIS EGEOS". Machine review is not human approval.
    if re.search(r"\bDOIS\s+EGEO\b(?!S)", h):
        return ""
    return f"*{h}*"


def headline_is_just_product_name(headline: str, product: str) -> bool:
    """Reject labels masquerading as editorial headlines."""
    def core(text: str) -> set[str]:
        norm = _headline_norm(strip_wa(text or "").strip("* ")).lower()
        tokens = set(re.findall(r"[a-z0-9]+", norm))
        noise = {
            "perfume", "colonia", "deo", "parfum", "edp", "edt", "eau", "de",
            "homem", "masculino", "feminino", "unissex", "original", "arabe",
            "natura", "boticario", "lattafa", "jequiti", "eudora", "versace",
            "carolina", "herrera", "banderas", "ml",
        }
        return {t for t in tokens if t not in noise and not re.fullmatch(r"\d{2,3}(?:ml)?", t)}

    h_core = core(headline)
    p_core = core(product)
    return bool(h_core and h_core <= p_core)


def headline_looks_like_catalog_title(headline: str, product: str) -> bool:
    """Hard gate for marketplace labels masquerading as editorial copy."""
    h = strip_wa(headline or "").strip("* ")
    if not h:
        return True
    # Pure marketplace taxonomy is never editorial copy. It may have no useful
    # token overlap with the exact SKU, so the generic title-echo check below
    # cannot catch lines such as "PERFUME MASCULINO 100ML".
    if re.fullmatch(
        r"(?i)(?:PERFUME|FRAGR[ÂA]NCIA|COL[ÔO]NIA)?\s*"
        r"(?:MASCULIN[OA]|FEMININ[OA]|UNISSEX)\s+\d{2,3}\s*ML",
        h.strip(),
    ):
        return True
    if headline_is_just_product_name(h, product):
        words = re.findall(r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ0-9]+", _headline_norm(h))
        # Short name hooks can be legitimate copy ("NATURA EXCLUSIVO",
        # "DARK MINT DA BOTICA").  Block the long catalog-label shape.
        if len(words) >= 3:
            return True
    # Concentration is spec-sheet language. Volume can be a legitimate commercial
    # angle when the rest adds meaning (Breno: "O ELEGANTE GRANDÃO 200ML"); pure
    # title+volume labels were already caught by headline_is_just_product_name.
    if re.search(r"(?i)\b(?:EDP|EDT|EAU DE|PARFUM)\b", h):
        return True
    return False


def recent_approved_headlines(limit: int = 18) -> list[str]:
    """Small style memory from Breno's latest corrected posts."""
    report = Path.home() / ".hermes/perfume_image_bank/breno_corrections/breno_corrections_scan.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for item in reversed(data.get("items") or []):
        h = strip_wa(str(item.get("headline") or "")).strip().strip("* ")
        if not h:
            continue
        ch = clean_headline(h, "")
        if ch and ch not in out:
            out.append(ch)
        if len(out) >= limit:
            break
    return list(reversed(out))


def rejected_headline_examples(limit: int = 10) -> list[str]:
    """Headlines Breno rejected (✍️ = bad headline, 👎 = reject).

    This closes a loop that existed socially but not technically: the reaction
    was logged and then ignored. Feeding these back as NEGATIVE examples is
    strictly different from reusing approved copy — it teaches the model what
    to avoid, it does not recycle content.
    """
    path = Path.home() / ".hermes/perfume_image_bank/headline_rejected.json"
    out: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in reversed(data.get("rejected") or []):
            h = str(item.get("headline") or "").strip().strip("* ")
            reason = str(item.get("reason") or "").strip()
            if not h:
                continue
            entry = f"{h} ({reason})" if reason else h
            if entry not in out:
                out.append(entry)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _arabic_perfume_product(product: str) -> bool:
    """Conservative product-family signal used only for editorial calibration."""
    norm = _headline_norm(product).lower()
    markers = (
        "lattafa", "arabe", "rasasi", "alhambra", "wataniah", "afnan",
        "armaf", "bidaya", "rayhaan", "asdaaf", "maison alhambra",
        "al wataniah", "fragrance world", "french avenue",
        # Known Arabic families often arrive with the house omitted by ML.
        "asad", "shagaf", "khamrah", "fakhar", "yara", "qaed al fursan",
        "hawas", "turathi", "club de nuit",
    )
    return any(marker in norm for marker in markers)


def breno_corrected_headline_pairs(limit: int = 12, product: str = "") -> list[tuple[str, str]]:
    """(nossa headline, headline corrigida pelo Breno) para o mesmo link.

    Este é o sinal de autoria mais confiável que existe: quando o Breno REESCREVE
    uma headline que NÓS publicamos, a versão final é inequivocamente dele. Isso
    resolve o problema que forçou headline_voice.json a ser curado à mão — lá o
    senderName é sempre Breno e repasse não se distingue de autoria. Aqui a
    autoria é provada pela edição: só entra o par em que o texto MUDOU em relação
    ao que o normalizador publicou naquele mesmo link.
    """
    report = Path.home() / ".hermes/perfume_image_bank/breno_corrections/breno_corrections_scan.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        data = {"items": []}
    feedback: dict[str, str] = {}
    log = Path.home() / ".hermes/perfume_image_bank/logs/normalizer_feedback.jsonl"
    try:
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            link = str(obj.get("link") or "")
            # Rows before 2026-08-14 logged the collector/source body as if it were
            # our normalized card. They are historically useful but invalid for
            # Normalizer -> Edição learning. Trust only exact published_copy rows.
            published_copy = str(obj.get("published_copy") or "").strip()
            head = next(
                (x.strip().strip("*") for x in published_copy.splitlines() if x.strip()),
                "",
            )
            if link and head:
                feedback[link] = head
    except Exception:
        pass

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    # Primary source: cycle-safe pairs produced by the comparator. Each row has
    # our exact published card and Breno's LAST Edição version in that cycle.
    cycles = Path.home() / ".hermes/perfume_image_bank/logs/editorial_comparison_cycles.jsonl"
    try:
        cycle_rows: list[dict] = []
        for raw in cycles.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                cycle_rows.append(json.loads(raw))
            except Exception:
                continue
        target_is_arabic = _arabic_perfume_product(product) if product else None
        # Retrieve corrections semantically close to THIS product instead of
        # feeding the model whichever edits happened most recently. Recency is
        # still the tie-breaker, but family/name overlap is the primary signal.
        target_tokens = {
            t for t in product_key(product).split('-')
            if len(t) >= 3 and t not in PRODUCT_NOISE_TOKENS
        }

        def _pair_relevance(row: dict) -> tuple[float, int]:
            row_tokens = {
                t for t in product_key(str(row.get("product") or "")).split('-')
                if len(t) >= 3 and t not in PRODUCT_NOISE_TOKENS
            }
            overlap = len(target_tokens & row_tokens) / max(1, len(target_tokens | row_tokens))
            return overlap, int(row.get("normalizer_ts") or 0)

        if config_value("PERFUME_HEADLINE_ARCHITECTURE", "v2").strip().lower() == "v3":
            cycle_rows.sort(key=_pair_relevance, reverse=True)
        else:
            cycle_rows.reverse()
        _calibration_cutoff = int(os.environ.get("PERFUME_CALIBRATION_CUTOFF_TS", "0") or 0)
        for row in cycle_rows:
            if not editorial_cycle_eligible(row):
                continue
            # Offline replay uses a temporal holdout: a product generated at T
            # may retrieve only corrections that existed before T. Production
            # leaves the cutoff unset and sees the full trusted history.
            if _calibration_cutoff and int(row.get("normalizer_ts") or 0) >= _calibration_cutoff:
                continue
            # Do not teach an Arabic offer with the editor's name-play patterns
            # from Brazilian lines (DOM, ABSOLUTO, QUASAR etc.), or vice versa.
            # Breno explicitly separates those registers.
            if target_is_arabic is not None:
                row_product = str(row.get("product") or "")
                if _arabic_perfume_product(row_product) != target_is_arabic:
                    continue
            a = str((row.get("before") or {}).get("headline") or "").strip()
            b = str((row.get("after") or {}).get("headline") or "").strip()
            if not a or not b or _headline_norm(a) == _headline_norm(b):
                continue
            norm = _headline_norm(b)
            if norm in seen:
                continue
            seen.add(norm)
            pairs.append((a, b))
            if len(pairs) >= limit:
                return pairs
    except Exception:
        pass
    # Backfill curado a partir do histórico do grupo Edição (mesmo link, primeira
    # versão = nossa, última = dele). O feedback log só passou a gravar
    # "headline" em 2026-07-26, então sem isto não haveria sinal nenhum hoje.
    backfill = Path.home() / ".hermes/perfume_image_bank/breno_headline_rewrites.json"
    try:
        bf = json.loads(backfill.read_text(encoding="utf-8"))
        for row in bf.get("pares") or []:
            a = str(row.get("nossa") or "").strip()
            b = str(row.get("corrigida") or "").strip()
            if not a or not b:
                continue
            norm = _headline_norm(b)
            if norm in seen:
                continue
            seen.add(norm)
            pairs.append((a, b))
    except Exception:
        pass

    for item in reversed(data.get("items") or []):
        if str(item.get("decision") or "").startswith("quarantined"):
            continue
        if str(item.get("decision") or "") == "normalizer_echo_not_human_edit":
            continue
        # Precisa ter havido edição de verdade neste link.
        if int(item.get("versions_for_link") or 0) < 2:
            continue
        link = str(item.get("link") or "")
        ours = feedback.get(link, "")
        theirs = str(item.get("headline") or "").strip().strip("*")
        if not theirs or not ours:
            continue
        if _headline_norm(ours) == _headline_norm(theirs):
            continue
        norm = _headline_norm(theirs)
        if norm in seen:
            continue
        seen.add(norm)
        pairs.append((ours, theirs))
        if len(pairs) >= limit:
            break
    return pairs


def breno_labeled_headline_preferences(product: str, limit: int = 8) -> list[dict]:
    """Blind mobile labels: explicit approved/rejected options, not inferred edits."""
    path = Path.home() / ".hermes/perfume_image_bank/headline_preferences_v6.json"
    try:
        examples = list((json.loads(path.read_text(encoding="utf-8")) or {}).get("examples") or [])
    except Exception:
        return []
    target_arabic = _arabic_perfume_product(product)
    target_key = product_key(product)
    target = {t for t in target_key.split("-") if len(t) >= 3 and t not in PRODUCT_NOISE_TOKENS}

    # Exact blind labels are authoritative. In particular, do not hide a product
    # with four rejections by padding it with unrelated approved headlines.
    exact = [x for x in examples if product_key(str(x.get("product") or "")) == target_key]
    if exact:
        return exact[:limit]

    # Listings often add/remove brand and concentration words. Treat the nearest
    # strongly-overlapping tested SKU as exact instead of mixing other products.
    tested_products = {str(x.get("product") or "") for x in examples}
    nearest_product = ""
    nearest_overlap = 0.0
    for tested_product in tested_products:
        tested_tokens = {
            t for t in product_key(tested_product).split("-")
            if len(t) >= 3 and t not in PRODUCT_NOISE_TOKENS
        }
        overlap = len(target & tested_tokens) / max(1, min(len(target), len(tested_tokens)))
        if overlap > nearest_overlap:
            nearest_product, nearest_overlap = tested_product, overlap
    if nearest_product and nearest_overlap >= 0.60:
        return [x for x in examples if str(x.get("product") or "") == nearest_product][:limit]

    product_lower = target_key.replace("-", " ")
    ctx = load_editorial_context(product)
    if re.search(r"\b(?:kit|combo|[2-9]\s*(?:unidades?|splashes?|perfumes?))\b", product_lower):
        target_category = "kit"
    elif "feromon" in product_lower:
        target_category = "premise"
    elif target_arabic and str(ctx.get("inspiration") or "").strip():
        target_category = "dupe"
    elif target_arabic:
        target_category = "arabic_no_dupe"
    else:
        target_category = "national"

    def relevance(row: dict) -> tuple[float, int]:
        row_product = str(row.get("product") or "")
        if str(row.get("category") or "") != target_category:
            return (-1.0, 0)
        tokens = {t for t in product_key(row_product).split("-") if len(t) >= 3 and t not in PRODUCT_NOISE_TOKENS}
        overlap = len(target & tokens) / max(1, len(target | tokens))
        return (overlap, 1 if str(row.get("label")) == "approved" else 0)

    ranked = [x for x in examples if relevance(x)[0] >= 0]
    ranked.sort(key=relevance, reverse=True)
    positives = [x for x in ranked if x.get("label") == "approved"][: max(1, limit // 2)]
    negatives = [x for x in ranked if x.get("label") == "rejected"][: max(1, limit // 2)]
    return (positives + negatives)[:limit]


def approved_headline_examples(limit: int = 6) -> list[str]:
    """Breno's OWN headlines, used as voice calibration in the prompt.

    Reads the hand-curated headline_voice.json instead of deriving from
    breno_corrections_scan.json. The scan is unusable as a voice reference:
    it records everything Breno posts in the Edição group, which includes
    forwards of a colleague's posts sent only to harvest photos. senderName is
    Breno either way, so authorship and relay are indistinguishable there.

    Measured 2026-07-25: the prompt was injecting 6 "tone that works" examples,
    ZERO of them written by Breno — it was teaching the model someone else's
    voice ("É DE NICHO RAPAZIADA") while Breno's real register is descriptive
    and informative ("AMADEIRADO COM ÂMBAR", "FIEL AO SILVER MOUNTAIN").
    """
    path = Path.home() / ".hermes/perfume_image_bank/headline_voice.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        voice = [str(x).strip() for x in (data.get("voz_breno") or []) if str(x).strip()]
    except Exception:
        voice = []
    if not voice:
        return []
    banned = {
        _headline_norm(x.split(" (")[0]).strip()
        for x in rejected_headline_examples(50)
    }
    out: list[str] = []
    for h in voice:
        if _headline_norm(h).strip() in banned or h in out:
            continue
        out.append(h)
    # Rotate so the prompt does not anchor on the same six every single run.
    if len(out) > limit:
        offset = int(time.time() // 600) % len(out)
        out = (out + out)[offset:offset + limit]
    return out[:limit]


def cultural_headline_patterns(limit: int = 4) -> str:
    """Curated cultural-hook PATTERNS from headline_voice.json.

    Breno's best corrections are mechanisms ("translate the name into a
    concrete cultural image"), not reusable phrases. Feeding the mechanism
    with his real examples teaches adaptation to new SKUs without inviting
    verbatim reuse — the same contract as TOM QUE FUNCIONA.
    """
    path = Path.home() / ".hermes/perfume_image_bank/headline_voice.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        patterns = list((data.get("padroes_culturais") or {}).get("exemplos") or [])
    except Exception:
        return ""
    lines = []
    for item in patterns[:limit]:
        trigger = str(item.get("gatilho") or "").strip()
        mechanism = str(item.get("mecanismo") or "").strip()
        examples = ", ".join(str(x) for x in (item.get("exemplos_reais") or [])[:3])
        if trigger and mechanism:
            lines.append(f"- QUANDO: {trigger} | COMO: {mechanism} | Exemplos dele (não copiar): {examples}")
    return "\n".join(lines)


def identity_token_conflict(a: str, b: str) -> bool:
    """True when two product names carry different distinctive identity tokens.

    Guards the human-approved-name reuse path: "Malbec Tradicional 100ml" must
    never inherit the approved display name of "Kaiak Tradicional 100ml" just
    because they share generic tokens (tradicional / 100ml).
    """
    noise = PRODUCT_NOISE_TOKENS | {
        "tradicional", "traditional", "masculino", "feminino", "unissex",
        "homens", "mulheres", "homem", "mulher", "kit",
    }

    def core(name: str) -> set[str]:
        parts = [p for p in product_key(name).split("-") if p]
        return {
            p for p in parts
            if p not in noise
            and not re.fullmatch(r"\d{2,3}ml", p)
            and len(p) >= 3
        }

    ca, cb = core(a), core(b)
    if not ca or not cb:
        return False
    return not (ca & cb)


def approved_headline_for_product(product: str) -> str:
    """Reuse Breno-approved headline for the same product when available.

    This is a global guardrail: once Breno corrects/approves a final post, the
    normalizer should not ask the LLM to reinvent a worse headline for that exact
    product on the next recurring offer.
    """
    key = product_key(product)
    lookup_keys = set(image_lookup_keys(product)) | {key}
    report = Path.home() / ".hermes/perfume_image_bank/breno_corrections/breno_corrections_scan.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for item in reversed(data.get("items") or []):
        if str(item.get("decision") or "").startswith("quarantined"):
            continue
        # Our own published output echoed back is not a human decision.
        if str(item.get("decision") or "") == "normalizer_echo_not_human_edit":
            continue
        item_key = str(item.get("product_key") or "")
        item_product = str(item.get("product") or "")
        item_keys = set(image_lookup_keys(item_product)) | {item_key, product_key(item_product)}
        if (not (lookup_keys & item_keys)
                or not identity_compatible(product, item_product, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                or product_variant_conflict(product, item_product)
                or identity_token_conflict(product, item_product)):
            continue
        h = clean_headline(str(item.get("headline") or ""), "")
        if h:
            return h
    return ""


def approved_product_name_for_product(product: str) -> str:
    """Return Breno's latest approved display name for this exact product key.

    This changes only the WhatsApp label; offer identity, price joins and image
    lookup continue using the independently validated product.
    """
    key = product_key(product)
    lookup_keys = set(image_lookup_keys(product)) | {key}
    report = Path.home() / ".hermes/perfume_image_bank/breno_corrections/breno_corrections_scan.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for item in reversed(data.get("items") or []):
        if str(item.get("decision") or "") not in {"breno_final_human_approved", "skipped_duplicate"}:
            continue
        # Echoes of our own output must never rename a future product.
        if str(item.get("decision") or "") == "normalizer_echo_not_human_edit":
            continue
        if not str(item.get("senderName") or "").casefold().startswith("breno"):
            continue
        item_product = str(item.get("product") or "").strip()
        # Older correction mining sometimes stored the first bold headline as
        # `product`. Editorial phrases such as "É O PARFUM" must never rename a
        # SKU to "Perfume É O Parfum" or become image identity.
        item_identity_tokens = [
            token for token in product_key(item_product).split("-")
            if len(token) >= 3 and not re.fullmatch(r"\d{2,3}ml", token)
        ]
        if not looks_like_product_line(item_product) or not item_identity_tokens:
            continue
        item_key = str(item.get("product_key") or "")
        item_keys = set(image_lookup_keys(item_product)) | {item_key, product_key(item_product)}
        if (item_product and (lookup_keys & item_keys)
                and not product_variant_conflict(product, item_product)
                and not identity_token_conflict(product, item_product)):
            approved_display = strip_non_identity_attributes(sanitize_product_text(item_product))
            return re.sub(r"\s+", " ", approved_display).strip(" -–—,")[:72]
    return ""


def approved_product_name_for_link(link: str | None, product: str) -> str:
    """Latest human-final title for this exact offer link, guarded by identity.

    A social ML title can use a commercial alias (``Perfume Sedutor Sabah``)
    while Breno's final card establishes the canonical SKU (``Sabah Al Ward``).
    Same-link, same-volume and shared identity are stronger than fuzzy catalog
    prose, but incompatible variant/concentration/volume remains blocked.
    """
    if not link:
        return ""
    path = Path.home() / ".hermes/perfume_image_bank/logs/editorial_comparison_cycles.jsonl"
    try:
        rows = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if editorial_cycle_eligible(row) and str(row.get("link") or "") == link:
                rows.append(row)
        rows.sort(key=lambda x: int(x.get("edition_final_ts") or 0), reverse=True)
        for row in rows:
            candidate = str((row.get("after") or {}).get("title") or "").strip()
            if not candidate or not looks_like_product_line(candidate):
                continue
            if product_variant_conflict(product, candidate) or identity_token_conflict(product, candidate):
                continue
            pv = set(re.findall(r"(?i)\b\d{2,3}\s*ml\b", product))
            cv = set(re.findall(r"(?i)\b\d{2,3}\s*ml\b", candidate))
            if pv and cv and pv != cv:
                continue
            # This is Breno's final title for this exact link. Identity,
            # variant and volume guards above already passed; running it back
            # through compact_product_name would erase deliberate editorial
            # choices (e.g. Deo Parfum, Feminino, Perfume Capilar) and make the
            # machine "correct" the editor after loading his correction.
            return re.sub(r"\s+", " ", strip_wa(candidate)).strip(" -–—,")[:72]
    except Exception:
        pass
    return ""


def editorial_title_authority(
    link: str | None,
    product: str,
    exact_title: str = "",
    exact_offer: bool = False,
) -> tuple[str, str]:
    """Resolve display-title authority without allowing fuzzy identity drift.

    The order is intentionally explicit and different from image lookup:
    exact linked item/card > latest human-final title for that exact link >
    trusted human SKU history > lexical cleanup. A currently proven exact item
    must correct stale same-link history (especially volume/concentration), while
    a catalog recommendation cannot rename the product. Human link history is
    accepted only after the existing variant/volume guards pass.
    """
    if exact_offer and exact_title and looks_like_product_line(exact_title):
        return compact_product_name(exact_title, preserve_house=True), "exact_item"
    linked = approved_product_name_for_link(link, product)
    if linked:
        return linked, "human_link"
    historical = approved_product_name_for_product(product)
    if historical:
        return compact_product_name(historical), "human_sku"
    return compact_product_name(product), "lexical"


def _context_keys_for_product(product: str) -> list[str]:
    keys = image_lookup_keys(product)
    pk = product_key(product)
    if pk not in keys:
        keys.insert(0, pk)
    # Same exact Al Wataniah SKU appears with the house before or after the
    # fragrance name. Context is canonicalized house-first; source titles often
    # arrive name-first.
    for key in list(keys):
        if key.startswith("shagaf-al-ward-al-wataniah-"):
            keys.insert(0, key.replace("shagaf-al-ward-al-wataniah-", "al-wataniah-shagaf-al-ward-", 1))
        # Maktub/Bidaya sources reorder brand and fragrance and may include
        # "Unisex". Keep one canonical context rather than duplicating facts
        # across several spelling-order keys.
        if key.startswith("maktub-la-vie-bidaya-"):
            volume = re.search(r"(\d{2,3}ml)$", key)
            if volume:
                keys.insert(0, f"arabe-bidaya-maktub-la-vie-bidaya-{volume.group(1)}")
        # Source titles often inject category words ("Árabe", "Extrait") into
        # the middle of an otherwise exact SKU. They are copy/concentration
        # metadata, not a reason to miss the curated editorial context.
        if "-arabe-" in key:
            keys.append(key.replace("-arabe-", "-"))
        if "-extrait-" in key:
            keys.append(key.replace("-extrait-", "-"))
        if key.startswith("bidaya-ayat-arabe-"):
            keys.insert(0, key.replace("bidaya-ayat-arabe-", "bidaya-arabe-ayat-", 1))
        if key.startswith("hawas-elixir-") and key.endswith("-rasasi"):
            keys.insert(0, "rasasi-" + key.removesuffix("-rasasi"))
        if key.startswith("arabe-vulcan-feu-citrico-frutado-"):
            keys.insert(0, "french-avenue-" + key.removeprefix("arabe-"))
    # Exact card spelling observed for the same 90ml Fougère SKU. Recover only
    # its human-curated context, not conflicting legacy Fiji backfills.
    if pk in {"o-boticario-botica-214-fiji-fougere-90ml", "botica-214-fiji-fougere-90ml"}:
        keys.insert(0, "botica-214-fiji-paradise-fougere-90ml")
    return [k for k in keys if k]


def editorial_identity_keys(product: str) -> set[str]:
    """Reuse exact approved-bank identity resolution for editorial retrieval.

    Read-only: no bank writes or loose cross-flanker context merges.
    """
    keys = {product_key(product)}
    for directory in approved_final_candidates(product):
        if not directory.is_dir():
            continue
        bank_key = directory.parent.name
        if fakhar_variant_conflict(product, bank_key.replace('-', ' ')):
            continue
        keys.add(bank_key)
        keys.add(product_key(bank_key.replace("-", " ")))
        if bank_key.startswith("arabe-"):
            keys.add(product_key(bank_key.removeprefix("arabe-").replace("-", " ")))
    return keys


def _load_editorial_context_base(product: str) -> dict:
    """Load cached olfactive/editorial rails for headline generation."""
    try:
        data = json.loads(EDITORIAL_CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    try:
        aliases = json.loads(IMAGE_BANK_ALIASES.read_text(encoding="utf-8")) if IMAGE_BANK_ALIASES.exists() else {}
    except Exception:
        aliases = {}
    # Reviewed exact-SKU human facts supersede contradictory speculative backfills.
    # No literal headline learning, no cross-flanker fuzzy matches.
    bank_keys = sorted(editorial_identity_keys(product))
    try:
        reviewed = json.loads(EDITORIAL_CONTEXT_FILE.with_name('editorial_reviewed_facts.json').read_text(encoding='utf-8'))
    except Exception:
        reviewed = {}
    def context_allowed(key: str, ctx: dict) -> bool:
        if (not identity_compatible(product, key, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                or fakhar_variant_conflict(product, key.replace('-', ' '))):
            return False
        wanted_gender = product_gender_key(product)
        context_gender = product_gender_key(str(ctx.get('gender') or '') + ' ' + ' '.join(ctx.get('safe_angles') or []))
        return not (wanted_gender and context_gender and wanted_gender != context_gender)

    for key in [product_key(product)] + bank_keys:
        if isinstance(reviewed.get(key), dict) and context_allowed(key, reviewed[key]):
            return dict(reviewed[key])
    # Prefer exact human-curated bank identity over old loose lexical aliases.
    for key in bank_keys:
        ctx = data.get(key)
        if isinstance(ctx, dict) and ctx.get("human_fact_source") and context_allowed(key, ctx):
            return ctx
    for key in list(dict.fromkeys(_context_keys_for_product(product) + bank_keys)):
        for cand in [key] + list(aliases.get(key) or []):
            if not identity_compatible(product, cand, DISTINCTIVE_VARIANT_TOKENS, strict=True):
                continue
            ctx = data.get(cand)
            if isinstance(ctx, dict) and context_allowed(cand, ctx):
                return ctx
    return {}


def load_editorial_context(product: str) -> dict:
    """Latest explicit human inspiration correction overrides stale backfills.

    Read-only overlay; no promotion of unedited model copy or cross-SKU facts.
    """
    ctx = dict(_load_editorial_context_base(product))
    keys = editorial_identity_keys(product)
    latest = None
    try:
        for raw in EDITORIAL_CYCLES_FILE.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            rp = str(row.get('product') or '')
            if (not editorial_cycle_eligible(row) or product_key(rp) not in keys
                    or not identity_compatible(product, rp, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                    or product_variant_conflict(product, rp)
                    or fakhar_variant_conflict(product, rp)):
                continue
            before = str((row.get('before') or {}).get('headline') or '')
            after = str((row.get('after') or {}).get('headline') or '')
            inspiration = known_inspiration_from_text(after)
            if not inspiration or before == after:
                continue
            timestamp = int(row.get('edition_final_ts') or 0)
            if latest is None or timestamp > latest[0]:
                latest = (timestamp, inspiration, row.get('cycle_id', ''))
    except (OSError, ValueError, TypeError):
        pass
    if latest:
        previous = str(ctx.get('known_inspiration') or '')
        if previous and _headline_norm(previous) != _headline_norm(latest[1]):
            # Contradictory speculative notes/avoid lists cannot survive their
            # contradicted inspiration. Keep the current explicit fact only.
            ctx = {'superseded_inspiration': previous}
        ctx.update(known_inspiration=latest[1], human_fact_source=latest[2],
                   inspiration_authority='human_edited_cycle')
    return ctx


def headline_inspiration_conflict(headline: str, ctx: dict) -> bool:
    reference = str(ctx.get('known_inspiration') or '')
    claim = known_inspiration_from_text(headline)
    if not reference or not claim:
        return False
    stop = {'JEAN', 'PAUL', 'GAULTIER', 'CREED', 'DE', 'GRIFE', 'NO', 'DO', 'DA'}
    expected = set(_headline_norm(reference).split()) - stop
    actual = set(_headline_norm(claim).split()) - stop
    return bool(expected and actual and not expected.intersection(actual))


def format_editorial_context(ctx: dict) -> str:
    if not ctx:
        return "(sem contexto editorial local; seja conservador e não invente inspiração/notas)"

    def vals(name: str) -> str:
        v = ctx.get(name) or []
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return ", ".join(str(x) for x in v if str(x).strip())
        return ""

    lines = [f"- confiança: {ctx.get('confidence') or 'não definida'}"]
    for field, label in [
        ("safe_angles", "ângulos seguros"),
        ("olfactive_terms", "descritores olfativos"),
        ("known_inspiration", "inspiração/dupe conhecido"),
        ("dupe_strength", "intensidade do dupe confirmada pelo editor"),
        ("proof_points", "provas sociais verificadas"),
        ("social_proof", "provas sociais verificadas"),
        ("approved_headlines", "headlines aprovadas"),
        ("avoid_terms", "evitar"),
    ]:
        text = vals(field)
        if text:
            lines.append(f"- {label}: {text}")
    return "\n".join(lines)


def context_approved_headline(product: str) -> str:
    ctx = load_editorial_context(product)
    for h in ctx.get("approved_headlines") or []:
        cleaned = clean_headline(str(h), "")
        if cleaned:
            return cleaned
    return ""


BAD_HEADLINE_TERMS = {
    "DIFERENTÃO", "DIFERENTAO", "ALFAFA", "TOPZERA", "BRABO", "BRABA",
    "ABSURDO", "IMPERDÍVEL", "IMPERDIVEL", "PREÇÃO", "PRECAO",
    # Empty-praise family: these say nothing about the product and would fit
    # any listing on the marketplace. "CUSTO BENEFICIO DEMAIS" shipped on the
    # Azzaro Sport (2026-07-25) because the blocklist only banned ugly slang,
    # never vacuous copy.
    "CUSTO BENEFICIO", "CUSTO BENEFÍCIO", "VALE MUITO A PENA",
    "OPORTUNIDADE DEMAIS", "NAO PODE PERDER", "NÃO PODE PERDER",
    "APROVEITA DEMAIS", "PRECO BOM DEMAIS", "PREÇO BOM DEMAIS",
    "QUALIDADE DEMAIS", "SIMPLESMENTE PERFEITO",
    # Ad-agency swagger formulas. Breno replaced every one of these with
    # informative copy (2026-07-25): "SEM PEDIR LICENÇA" alone shipped twice in
    # 8 minutes on different products — a tic, not creativity.
    "SEM PEDIR LICENCA", "SEM PEDIR LICENÇA", "SEM FAZER DRAMA",
    "MODE CHEFE", "MODO CHEFE", "DIRETO AO PONTO", "SEM FRESCURA",
    "CHEGANDO CHEGANDO", "PODE CHAMAR DE", "AVISA QUE CHEGOU",
}


def _headline_norm(text: str) -> str:
    table = str.maketrans("ÁÀÂÃÉÊÍÓÔÕÚÜÇ", "AAAAEEIOOOUUC")
    return text.upper().translate(table)


HEADLINE_AUDIT_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/headline_audit.jsonl"))
HEADLINE_USAGE_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/headline_repertoire_usage.json"))
EDITORIAL_CYCLES_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/logs/editorial_comparison_cycles.jsonl"))
_HEADLINE_USAGE_LOCK = threading.Lock()


_HEADLINE_RESERVED: set[str] = set()
_HEADLINE_RESERVED_LOCK = threading.Lock()


def reserve_headline(headline: str) -> None:
    """Claim a headline for this run so parallel batch siblings avoid it."""
    try:
        with _HEADLINE_RESERVED_LOCK:
            _HEADLINE_RESERVED.add(_headline_norm(headline.strip("* ")))
    except Exception:
        pass


def headline_recently_used(headline: str, window: int = 40) -> bool:
    """True when this exact headline already went out in the recent audit log.

    Uses the append-only headline_audit.jsonl written by main(). Cheap tail
    read; failure means "not recently used" so behavior degrades to plain reuse.
    Also consults the in-process reservation set: with 3 parallel workers the
    audit log is only written at SEND time, so two same-brand promos in one
    batch both saw "not used" and went out with identical headlines
    (2x "AZZARO DE PRESENÇA", 2026-07-10 17:57).
    """
    with _HEADLINE_RESERVED_LOCK:
        if _headline_norm(headline.strip("* ")) in _HEADLINE_RESERVED:
            return True
    try:
        if not HEADLINE_AUDIT_FILE.exists():
            return False
        target = _headline_norm(headline.strip("* "))
        lines = HEADLINE_AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-window:]
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if _headline_norm(str(row.get("headline") or "").strip("* ")) == target:
                return True
    except Exception:
        pass
    return False


def _headline_price(price_context: dict | None) -> Decimal | None:
    if not isinstance(price_context, dict):
        return None
    for name in ("current_price", "final_price", "price", "por"):
        value = price_context.get(name)
        if value is None:
            continue
        try:
            return Decimal(str(value).replace("R$", "").replace(",", ".").strip())
        except Exception:
            continue
    return None


def _repertoire_price_allowed(headline: str, price_context: dict | None) -> tuple[bool, str]:
    text = _headline_norm(headline)
    price = _headline_price(price_context)
    # Plain "MENOS DE 100" is established commercial copy. "ABAIXO DE 0",
    # however, is an approved Quasar Ice semantic hook, not a negative price;
    # require an explicit currency marker for the ABAIXO form.
    less_than = re.search(
        r"(?:MENOS DE\s*(?:R\$\s*)?|ABAIXO DE\s*R\$\s*)(\d+(?:[,.]\d+)?)",
        text,
    )
    if less_than:
        limit = Decimal(less_than.group(1).replace(",", "."))
        if price is None or not price < limit:
            return False, f"price claim requires current price < {limit}"
    explicit = re.search(r"(?:R\$\s*|\bA\s+)(\d+(?:[,.]\d+)?)", text)
    if explicit and not less_than:
        claimed = Decimal(explicit.group(1).replace(",", "."))
        if price is None or abs(price - claimed) > Decimal("1.00"):
            return False, f"price claim {claimed} does not match current offer"
    return True, "no unsupported current-price claim"


def _load_headline_usage() -> dict:
    try:
        data = json.loads(HEADLINE_USAGE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_repertoire_usage(key: str, headline: str) -> None:
    try:
        usage = _load_headline_usage()
        entries = usage.setdefault(key, [])
        entries.append({"headline": headline, "ts": int(time.time())})
        usage[key] = entries[-20:]
        HEADLINE_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = HEADLINE_USAGE_FILE.with_name(HEADLINE_USAGE_FILE.name + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, HEADLINE_USAGE_FILE)
    except Exception:
        pass


def same_product_tokens_reordered(left: str, right: str) -> bool:
    """Accept the same SKU despite token order and cosmetic catalog descriptors.

    Flanker/model words, gender, concentration, volume and kit quantity remain
    hard identity. Only generic words such as perfume/desodorante/colônia and
    their placement may differ.
    """
    left_gender, right_gender = identity_gender(left), identity_gender(right)
    if left_gender and right_gender and left_gender != right_gender:
        return False
    cosmetic = {"perfume", "desodorante", "colonia", "spray", "original"}
    left_tokens = [t for t in product_key(left).split("-") if t not in cosmetic]
    right_tokens = [t for t in product_key(right).split("-") if t not in cosmetic]
    return bool(left_tokens and sorted(left_tokens) == sorted(right_tokens))


def eligible_repertoire_candidates(product: str, price_context: dict | None = None) -> list[str]:
    """Retrieve exact-SKU, offer-compatible final headlines; never promote generation."""
    key = product_key(product)
    identity_keys = editorial_identity_keys(product)
    fact_context = load_editorial_context(product)
    candidates: list[str] = []
    price_rejected = 0
    try:
        for raw in EDITORIAL_CYCLES_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not editorial_cycle_eligible(row):
                continue
            after = row.get("after") or {}
            raw_candidate = str(after.get("headline") or "")
            row_product = str(row.get("product") or (row.get("before") or {}).get("product") or after.get("product") or "")
            if (not raw_candidate or not row_product or
                    (product_key(row_product) not in identity_keys
                     and not same_product_tokens_reordered(product, row_product))):
                continue
            if (product_variant_conflict(product, row_product) or identity_token_conflict(product, row_product)
                    or fakhar_variant_conflict(product, row_product)):
                continue
            if not identity_compatible(product, row_product, DISTINCTIVE_VARIANT_TOKENS, strict=True):
                continue
            if headline_inspiration_conflict(raw_candidate, fact_context):
                continue
            if headline_gender_conflict(raw_candidate, product):
                continue
            allowed, _ = _repertoire_price_allowed(raw_candidate, price_context)
            if not allowed:
                price_rejected += 1
                continue
            headline = clean_headline(raw_candidate, "")
            if headline and headline not in candidates:
                candidates.append(headline)
    except Exception:
        return []
    candidates.reverse()
    # Breno-authorized manual seeds supplement, but do not rewrite, the human
    # Edição history. They remain subject to the same exact-SKU, fact/conflict,
    # price and distinctness gates below.
    manual_path = Path.home() / ".hermes/perfume_image_bank/headline_preferences_v6.json"
    try:
        manual = json.loads(manual_path.read_text(encoding="utf-8"))
        for entry in (manual.get("manual_exact_sku_repertoire") or {}).values():
            if not isinstance(entry, dict):
                continue
            manual_product = str(entry.get("product") or "")
            if (not manual_product
                    or (product_key(manual_product) not in identity_keys
                        and not same_product_tokens_reordered(product, manual_product))
                    or product_variant_conflict(product, manual_product)
                    or identity_token_conflict(product, manual_product)
                    or fakhar_variant_conflict(product, manual_product)
                    or not identity_compatible(product, manual_product, DISTINCTIVE_VARIANT_TOKENS, strict=True)):
                continue
            for raw_candidate in entry.get("headlines") or []:
                headline = clean_headline(str(raw_candidate), "")
                allowed, _ = _repertoire_price_allowed(headline, price_context)
                if headline and allowed and not headline_gender_conflict(headline, product) and headline not in candidates:
                    candidates.append(headline)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    from perfume_renewal_review import reviewed_candidates
    candidates.extend(reviewed_candidates(product, identity_keys, fact_context))
    return distinct_repertoire(candidates)


def distinct_repertoire(candidates: list[str]) -> list[str]:
    """Conservative lexical near-duplicate filter, not a semantic taste judge."""
    result = []
    signatures = []
    for headline in candidates:
        words = set(re.findall(r'[A-Z0-9]+', _headline_norm(headline))) - {'O','A','E','DE','DO','DA','COM','UM','UMA','PRA','PARA'}
        if not words:
            continue
        if any(len(words & old) / max(1, len(words | old)) >= 0.75 for old in signatures):
            continue
        signatures.append(words)
        result.append(headline)
    return result


def repertoire_exploration_interval(size: int, base_rate: float) -> int:
    # Grow toward five independent options; mature pools still renew.
    rate = max(base_rate, 0.5) if size < 5 and base_rate > 0 else base_rate
    return max(1, round(1.0 / rate)) if rate > 0 else 0


def approved_headline_repertoire(product: str, price_context: dict | None = None) -> str:
    key = product_key(product)
    candidates = eligible_repertoire_candidates(product, price_context)
    # One genuinely approved headline is still safer than invoking the LLM.
    # Reuse is the publication default; repertoire maturity is measured separately.
    if not candidates:
        return ""
    usage = _load_headline_usage().get(key) or []
    last_used = {_headline_norm(str(x.get('headline') or '')): i for i, x in enumerate(usage) if isinstance(x, dict)}
    selected = min(candidates, key=lambda h: last_used.get(_headline_norm(h), -1))
    global LAST_HEADLINE_REASON
    LAST_HEADLINE_REASON = f"selected from {len(candidates)} eligible distinct finals; target=5; least_recently_used; price_guard=applied"
    return selected


def fallback_pool_pick(options: list[str]) -> str:
    """Pick a deterministic-fallback headline avoiding recent repetition.

    The 25-promo batch showed the same generic fallback (ÁRABE OPULENTO etc.)
    going out 3x in one day. Rotate through a pool using the audit log; only
    repeat when every option was already used recently.
    """
    for opt in options:
        if not headline_recently_used(opt.strip("* ")):
            return opt
    return options[0]


def recent_sent_headlines(limit: int = 12) -> list[str]:
    """Last headlines actually sent (audit log), for structural anti-repetition."""
    out: list[str] = []
    try:
        if not HEADLINE_AUDIT_FILE.exists():
            return out
        for line in reversed(HEADLINE_AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]):
            try:
                h = str(json.loads(line).get("headline") or "").strip("* ")
            except Exception:
                continue
            if h and h not in out:
                out.append(h)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def headline_name_hook_score(headline: str, product: str) -> int:
    """Reward small, natural hooks tied to the product's own name/theme."""
    hn = _headline_norm(headline)
    pn = _headline_norm(product)
    # Breno's current rule: Arabic names identify the SKU but are not an
    # editorial wordplay source. The old generic token-overlap reward promoted
    # empty lines such as "ELIXIR PRA VARIAR" merely for repeating Elixir.
    if _arabic_perfume_product(product):
        return 0
    themes = [
        (("ICE", "POLAR", "GLACIAL", "FROZEN"), ("GELO", "GELAD", "CONGEL", "FRIO", "ICE", "ABAIXO DE 0", "ABAIXO DE ZERO", "❄️", "🧊")),
        (("FLAME", "FIRE", "FEU"), ("FOGO", "CHAMA", "QUENTE", "PEGANDO FOGO", "🔥")),
        (("GOLD", "OURO", "GOLDEN"), ("OURO", "DOURAD", "VALE OURO", "✨")),
        (("OCEANO", "OCEAN", "AQUA", "AQUATIC"), ("MAR", "ONDA", "OCEANO", "AQUÁT", "AQUAT", "🌊")),
        (("NIGHT", "NOIR", "NOTURNO"), ("NOITE", "NOTURN", "ESCURO", "🌙")),
        (("VINTAGE", "RADIO"), ("VINTAGE", "RÁDIO", "RADIO", "RETRÔ", "RETRO", "📻")),
        (("MILLION", "MILHÃO", "MILHAO"), ("MILHÃO", "MILHAO", "MILHÕES", "MILHOES", "💰")),
    ]
    for product_marks, headline_marks in themes:
        if any(mark in pn for mark in product_marks):
            if any(mark in hn or mark in headline for mark in headline_marks):
                # Prefer a product-specific execution ("QUASAR ABAIXO DE 0")
                # over a generic use of the same theme ("GELO CONTRA O CALORÃO").
                # The 30-point ranker override margin makes the larger bonus
                # necessary rather than decorative.
                distinctive = {
                    t for t in re.findall(r"[A-Z0-9]+", pn)
                    if len(t) >= 4 and t not in {
                        "PERFUME", "COLONIA", "PARFUM", "HOMEM", "NATURA", "BOTICARIO"
                    }
                }
                return 80 if any(t in hn for t in distinctive) else 45
            return 0
    # Wordplay on the product's OWN name (Breno, 2026-08-02). His approved
    # "ABSOLUTAMENTE ABSOLUTO" (Homem Absoluto) and "BAUNILHA COM FAVA É O DOM"
    # (Homem Dom) reuse a distinctive name token in a NEW grammatical role, and
    # the theme table above could never see it — it only knows a fixed list of
    # motifs. Telling the LLM to do this in the prompt was not enough (measured:
    # Homem Absoluto still came back as "MADEIRA NACIONAL"), so the ranker has
    # to reward it too.
    #
    # This is strictly different from echoing the title: the naked-echo case is
    # already penalised elsewhere (-40/-140 for a token subset, -200 for
    # headline_is_just_product_name). Here the headline must ALSO bring
    # substance the product name does not have.
    _stop = {
        "perfume", "colonia", "deo", "parfum", "edp", "edt", "eau", "kit",
        "natura", "boticario", "lattafa", "jequiti", "eudora", "homem",
        "masculino", "feminino", "unissex", "original", "arabe", "body",
        "splash", "com", "que", "para", "pra", "sem", "mais", "dos", "das",
    }
    _p_tokens = {
        t for t in re.findall(r"[a-z0-9]+", pn.lower())
        if len(t) >= 3 and t not in _stop and not re.fullmatch(r"\d+m?l?", t)
    }
    _h_tokens = [t for t in re.findall(r"[a-z0-9]+", hn.lower()) if len(t) >= 3]
    _h_set = set(_h_tokens)
    _shared = _p_tokens & _h_set
    if _shared:
        # Substance = words the headline adds that are NOT in the product name.
        _added = {t for t in _h_set if t not in _p_tokens and t not in _stop}
        # Breno's 2026-08-28 edits exposed a ranker inversion: natural clauses
        # built from the product name ("TRADICIONAL QUE É ESSENCIAL",
        # "ESSENCIAL DOCE QUE ATRAI", "O NACIONAL QUE TEM DOM") lost to
        # mechanical note/profile labels. QUE turns a reused name token into a
        # proposition instead of merely echoing the catalog title. Require two
        # distinctive title tokens or real added substance so empty title-like
        # lines do not earn the bonus. Arabic names remain excluded above.
        if "QUE" in hn and (len(_shared) >= 2 or bool(_added)):
            return 65
        # Derivation/alliteration on the shared token IS the wordplay and needs
        # no further substance: "ABSOLUTAMENTE ABSOLUTO" adds exactly one word,
        # and requiring two rejected Breno's own approved headline (measured
        # 2026-08-02). "O MALBEC DO VERDE" is the other shape — same token in a
        # new role, carried by a short phrase.
        _derived = any(
            t != s and len(t) > len(s) and t.startswith(s[:max(4, len(s) - 2)])
            for s in _shared for t in _h_set
        )
        if _derived:
            return 45
        if len(_added) >= 2:
            return 35
        # One added word still forms copy when the headline reads as a phrase
        # (connective present) rather than a bare title echo.
        if _added and re.search(r"\b(NO|NA|DO|DA|PRA|PRO|COM|É|E|QUE|SEM)\b", hn):
            return 32
    return 0


DESCRIPTIVE_VOCAB = {
    "citrico", "fresco", "amadeirado", "aromatico", "especiado", "ambar",
    "doce", "floral", "marcante", "intenso", "suave", "adocicado",
    "amargo", "verde", "aquatico", "oriental", "atalcado", "frutado",
    "almiscarado", "couro", "madeiras", "madeira", "baunilha", "lavanda",
    "hortela", "licoroso", "resinoso",
}


def headline_is_pure_descriptive(headline: str, extra_terms: "list | tuple | set | None" = None) -> bool:
    """True when a headline is made only of scent adjectives.

    NOTE (Breno, 2026-07-25): an olfactive headline is NOT bad in itself —
    "CÍTRICO, FRESCO E AMADEIRADO" is legitimate copy. The real failure was
    frequency: the ranker got addicted to that shape and shipped it over and
    over. So this predicate only *classifies*; the penalty is applied by
    repetition, not by category.
    """
    vocab = set(DESCRIPTIVE_VOCAB)
    for t in (extra_terms or []):
        vocab.add(_headline_norm(str(t)).lower())
    tokens = {
        t for t in re.findall(r"[a-z0-9]+", _headline_norm(headline).lower())
        if len(t) >= 3 and t not in {"com", "que", "para", "pra", "sem", "mais", "perfume", "colonia"}
    }
    return len(tokens) >= 2 and tokens <= vocab


def recent_descriptive_count(window: int = 10) -> int:
    """How many of the last published headlines were pure scent descriptions.

    This is the anti-rut signal: one olfactive headline is a fine choice, three
    in a row is a tic. Reading the audit log keeps the judgement empirical
    instead of banning a whole category up front.
    """
    try:
        if not HEADLINE_AUDIT_FILE.exists():
            return 0
        lines = HEADLINE_AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-window:]
    except Exception:
        return 0
    n = 0
    for line in lines:
        try:
            h = str(json.loads(line).get("headline") or "").strip("* ")
        except Exception:
            continue
        if h and headline_is_pure_descriptive(h):
            n += 1
    return n


def headline_mold(headline: str) -> str:
    """Classify the STRUCTURAL template of a headline, not its content.

    Measured 2026-08-02 over the last 120 published headlines: "PRA" appeared 30
    times and "PRA PRESENTEAR" alone 10 times. Each line was individually fine;
    read in sequence in the group they are monotonous. headline_score() already
    guards against defects but was blind to sameness of FORM, so the generator
    settled into three safe templates and filled in variables.

    Content-level anti-repetition (recent_set / headline_recently_used) cannot
    catch this: "PRA PRESENTEAR" and "PRA VIAJAR" are different strings but the
    same mold.
    """
    h = _headline_norm(strip_wa(headline or "").strip("* ")).upper()
    if not h:
        return ""
    if re.search(r"\bFIEL AO?\b|\bFIEL DEMAIS\b|\bFIEL D\+ AO\b", h):
        return "FIEL_AO"
    if re.search(r"\b(DUPE|CLONE|CONTRATIPO|INSPIRADO NO?|VERSAO D[EO]|LEMBRA O?)\b", h):
        return "DUPE"
    if re.search(r"R\$|\bPOR \d|\bMENOS DE \d|\bPRECINHO\b|\bPRECO\b", h):
        return "PRECO"
    if re.search(r"\b(PRA|PARA|PRO)\b", h):
        return "PRA_X"
    if re.search(r"\bKIT\b|\bCOMBO\b|\bCONJUNTO\b", h):
        return "KIT"
    if re.search(r"\b(VOCE|VC|TE|SEU|SUA)\b", h):
        return "SEGUNDA_PESSOA"
    # Two or more bare adjectives joined by E/COM, no verb: "AROMATICO NACIONAL
    # SAGAZ", "MARCANTE E ELEGANTE".
    if re.search(r"\b(E|COM)\b", h):
        return "ADJ_ADJ"
    return "OUTRO"


def recent_headline_molds(window: int = 10) -> dict:
    """How often each mold appeared in the last published headlines.

    Same empirical approach as recent_descriptive_count(): read the audit log
    instead of banning categories up front. A mold used once is a good choice;
    the same mold four times in ten is a tic.
    """
    counts: dict[str, int] = {}
    try:
        if not HEADLINE_AUDIT_FILE.exists():
            return counts
        lines = HEADLINE_AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-window:]
    except Exception:
        return counts
    for line in lines:
        try:
            h = str(json.loads(line).get("headline") or "").strip("* ")
        except Exception:
            continue
        mold = headline_mold(h)
        if mold:
            counts[mold] = counts.get(mold, 0) + 1
    return counts


EDITORIAL_CONTEXT_FILE = Path(os.path.expanduser("~/.hermes/perfume_image_bank/editorial_context.json"))
_GENERIC_OLFACTIVE_CACHE: set | None = None


def generic_olfactive_terms() -> set:
    """Olfactive terms so common across the catalog that they carry no signal.

    Measured 2026-07-25 over 448 products: "amadeirado" describes 60% of them,
    "especiado" 35%, "fresco" 29%. A headline built from these is technically
    accurate and completely interchangeable between products — which is exactly
    the "fraco" Breno kept rejecting. Specific notes (bergamota, couro,
    baunilha) still differentiate and stay valuable.

    Computed from the live catalog and cached per process, so it stays honest
    as the catalog grows instead of freezing today's numbers into a constant.
    """
    global _GENERIC_OLFACTIVE_CACHE
    if _GENERIC_OLFACTIVE_CACHE is not None:
        return _GENERIC_OLFACTIVE_CACHE
    generic: set = set()
    try:
        data = json.loads(EDITORIAL_CONTEXT_FILE.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        total = 0
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            total += 1
            for term in set(entry.get("olfactive_terms") or []):
                key = _headline_norm(str(term)).lower().strip()
                if key:
                    counts[key] = counts.get(key, 0) + 1
        if total >= 50:
            cutoff = total * 0.15
            generic = {t for t, n in counts.items() if n >= cutoff}
    except Exception:
        generic = set()
    _GENERIC_OLFACTIVE_CACHE = generic
    return generic


def headline_score(cleaned: str, ctx: dict) -> int:
    if headline_inspiration_conflict(cleaned, ctx):
        return -1000
    if headline_gender_conflict(cleaned, str(ctx.get('_product') or '')):
        return -1000
    h = cleaned.strip("* ").upper()
    hn = _headline_norm(h)
    # Observed rejected angles (Breno, current paired cycles): category praise
    # and collection filler carry no product-specific argument. Keep genuine
    # name play such as 'UM GENTLEMAN BEM CHEIROSO' outside this narrow veto.
    plain = re.sub(r"[^A-Z0-9 ]", "", unicodedata.normalize("NFKD", hn).encode("ascii", "ignore").decode()).strip()
    if re.fullmatch(r"(?:UM A MAIS NA COLECAO|UM ARABE PRA COLECAO|UM ARABE BEM CHEIROSO|FOUGERE BEM CHEIROSO)", plain):
        return -200
    # Flat plateau instead of a sharp peak at 24 chars. The old curve
    # (100 - abs(len-24)) taxed every short, sharp headline and rewarded
    # padding: the LLM learned to stretch copy with one more olfactive note
    # until it hit 24. Anything in the comfortable mobile range scores equal.
    _len = headline_visual_len(h)
    if 14 <= _len <= 28:
        score = 100
    elif _len < 14:
        score = 100 - 2 * (14 - _len)
    else:
        score = 100 - 3 * (_len - 28)
    product_text = str(ctx.get("_product") or "")
    score += headline_name_hook_score(h, product_text)
    # A headline must add an angle, not merely abbreviate the product title.
    # "TURMA DA MÔNICA EM 25ML" passed the old guards because it was short and
    # factual, but it adds nothing to "Jequiti Turma da Mônica Cebolinha 25ml".
    _headline_tokens = {
        t for t in re.findall(r"[a-z0-9]+", _headline_norm(h).lower())
        if len(t) >= 3 and t not in {"perfume", "colonia", "desodorante", "jequiti", "natura", "boticario"}
    }
    _product_tokens = {
        t for t in re.findall(r"[a-z0-9]+", _headline_norm(product_text).lower())
        if len(t) >= 3 and t not in {"perfume", "colonia", "desodorante", "jequiti", "natura", "boticario"}
    }
    if len(_headline_tokens) >= 2 and _headline_tokens <= _product_tokens:
        # A name-only headline that REARRANGES the title into a phrase is not
        # the same failure as abbreviating it. "CLUB DE NUIT NO SILLAGE" reads
        # as copy (Breno approved it 2026-07-25); "TURMA DA MÔNICA EM 25ML"
        # does not — it is the title with a preposition glued on, and the
        # volume token is the giveaway. Only soften when the headline forms a
        # phrase AND drops the spec-sheet tokens (volume/concentration).
        _has_connective = bool(re.search(r"\b(NO|NA|DO|DA|PRA|PRO|COM|É|E)\b", h))
        _carries_spec = bool(re.search(r"\b\d{2,4}\s*ML\b|\bEDP\b|\bEDT\b", h))
        score -= 40 if (_has_connective and not _carries_spec) else 140
    # Olfactive headlines are Breno's OWN dominant register — "AMADEIRADO COM
    # ÂMBAR", "LAVANDA, COURO E MADEIRAS" are his, and he confirmed the style is
    # good (2026-07-25). Only a genuine streak is discouraged, and gently: this
    # must never push the ranker back toward ad-agency swagger, which he
    # rewrites every single time.
    if headline_is_pure_descriptive(h, ctx.get("olfactive_terms") or []):
        _recent_desc = ctx.get("_recent_descriptive")
        if _recent_desc is None:
            _recent_desc = recent_descriptive_count(10)
        if _recent_desc >= 5:
            score -= 60
        elif _recent_desc >= 4:
            score -= 25
    # 2026-08-02 (Vulcan Sable): every option tied at 100 — "VULCAN SABLE PRA
    # USAR", "PERFUME PRA SUA ROTINA", "SEU PRÓXIMO PERFUME" — so the ranker was
    # blind and the generator's first pick won by default. A headline made only
    # of filler ("um cheiro novo", "pra usar", "sua rotina") carries no angle:
    # no dupe, no olfactive note, no benefit, no occasion. Penalise emptiness so
    # a real angle can outrank it instead of merely tying.
    _hu = _headline_norm(h).upper()
    # Semantic emoji guard. A fruit glyph is a factual olfactive claim, not a
    # generic decoration for anything "tropical". The old prompt explicitly
    # mapped 🥭 to manga/tropical, which let an abacaxi perfume leave the
    # comparator wearing a mango. Require the named fruit in verified context.
    _emoji_evidence = _headline_norm(" ".join(
        [str(ctx.get("known_inspiration") or "")]
        + [str(x) for x in (ctx.get("olfactive_terms") or [])]
        + [str(x) for x in (ctx.get("safe_angles") or [])]
    )).lower()
    if "🥭" in h and "manga" not in _emoji_evidence:
        score -= 240
    if "🍍" in h and "abacaxi" not in _emoji_evidence:
        score -= 240

    # Breno uses intensity as information in proven strong dupes. High-confidence
    # inspiration context should therefore prefer FIEL DEMAIS (or compact FIEL
    # D+ when the reference is long), rather than flattening every dupe to FIEL AO.
    # `confidence` says the inspiration fact is reliable; it does NOT say how
    # close the scent is. Only the explicit strength learned from Breno's Edição
    # may force DEMAIS/D+. Liquid Brun, for example, has high-confidence Althaïr
    # inspiration but Breno still chose plain "FIEL AO LUXUOSO ALTHAÏR".
    _strong_dupe = bool(ctx.get("known_inspiration")) and str(ctx.get("dupe_strength") or "").lower() == "strong"
    _fiel_intensive = bool(re.search(r"\bFIEL (?:DEMAIS|D\+) AO\b", _hu))
    _fiel_plain = bool(re.search(r"\bFIEL AO\b", _hu)) and not _fiel_intensive
    if _strong_dupe and _fiel_intensive:
        score += 55
    elif _strong_dupe and _fiel_plain:
        score -= 55
    # A broken discount percentage belongs in the price line, not the headline.
    # Normalizer -> Edição evidence on 2026-08-23: Breno rewrote mechanical
    # spreadsheet molds such as "41% MENOS NO BOTTLED" and "42% MENOS NO
    # SAFEER" into natural commercial hooks. Psychological milestones are the
    # exception: 50% OFF and 75% OFF are instantly legible buying arguments.
    # Other percentages merely advertise the calculation already visible in
    # De/Por, so veto them deterministically.
    _discount_pcts = [int(x) for x in re.findall(r"\b(\d{1,2})\s*%", _hu)]
    if _discount_pcts and any(x not in {50, 75} for x in _discount_pcts):
        score -= 220
    # A raw note plus an intensity adjective and price is the same catalog mold
    # wearing a commercial hat ("TUBEROSA INTENSA A R$169"). Veto this specific
    # extracted-note template without banning olfactive headlines or natural
    # product/threshold hooks.
    if re.fullmatch(r"[A-Z0-9 ]+\s+INTENS[AO]\s+A\s+R\$\s*\d+(?:[.,]\d+)?", _hu):
        score -= 180
    _filler_only = re.fullmatch(
        r"[\s\W]*(?:UM |UMA |O |A |SEU |SUA |MEU |MINHA )?"
        r"(?:NOVO |NOVA |PROXIMO |PROXIMA |PRIMEIRO )?"
        r"(?:PERFUME|CHEIRO|FRAGRANCIA|AROMA|COLONIA)"
        r"(?:\s+(?:PRA|PARA|NO|NA|DE|DO|DA)\s+"
        r"(?:USAR|VOCE|SUA|SEU|TODO|TODA|DIA|DIA A DIA|ROTINA|SEMPRE|AGORA|HOJE))*"
        r"[\s\W]*",
        _hu,
    )
    if _filler_only:
        score -= 120
    else:
        # Demographic/category words plus generic use-language are not an angle.
        # This batch generated seven interchangeable Shagaf lines because
        # "feminino" was incorrectly treated as useful product information.
        _generic_demographic_tokens = {
            "perfume", "cheiro", "fragrancia", "aroma", "colonia",
            "feminino", "masculino", "unissex", "direto", "dia", "todo",
            "usar", "pensar", "gosta", "rotina", "sempre", "voce", "pra",
            "para", "sem", "um", "uma", "no", "na", "de", "do", "da", "e",
        }
        _content_tokens = set(re.findall(r"[a-z0-9]+", _hu.lower()))
        if _content_tokens and _content_tokens <= _generic_demographic_tokens:
            score -= 120
        # Empty lifestyle slogans also carry no perfume-specific angle. They were
        # repeatedly reused across unrelated SKUs and survived at the 100-point
        # plateau ("CHEIROSO SEM COMPLICAÇÃO", 2026-08-09).
        if re.fullmatch(
            r"(?:CHEIROSO SEM COMPLICA(?:CAO|R)|CHEIRO FACIL DE USAR|PRESENCA BEM ARRUMADA)",
            _hu,
        ):
            score -= 160
        # Category alignment matters even when the phrase is elegant. Breno
        # liked the Gentleman hook but corrected "UM GENTLEMAN BEM VESTIDO" to
        # "UM GENTLEMAN BEM CHEIROSO": this is perfume, not menswear. Keep the
        # successful identity hook and reject the clothing payoff.
        if re.search(r"\bBEM VESTID[OA]\b", _hu):
            score -= 140
        # Generic-use tails add nothing on their own: "X PRA USAR", "X NO DIA A
        # DIA", "X PRA SUA ROTINA" are the product name plus a shrug.
        _generic_use_tail = r"\b(PRA USAR|PARA USAR|PRA SAIR|PARA SAIR|NO DIA A DIA|PRA SUA ROTINA|PRA TODO DIA|PARA TODO DIA|PRA VOCE|PRA VARIAR|PARA VARIAR|PRA REVEZAR|PARA REVEZAR)(?:\s*[^\w\s]+)?\s*$"
        if re.search(_generic_use_tail, _hu):
            score -= 55
        # Measured Normalizer→Edição misses: these tails survive by borrowing
        # the product-name bonus, but the payoff itself can be pasted onto any
        # SKU. Penalise the empty mold even when a brand/name precedes it.
        if re.search(
            r"\b(?:EM DESTAQUE|PRA REFORCAR|PARA REFORCAR|PRA SE ARRUMAR|PARA SE ARRUMAR)(?:\s*[^\w\s]+)?\s*$",
            _hu,
        ):
            score -= 130
        # 2026-08-29 Edição: "MALBEC PRA SAIR" was replaced by a concrete
        # commercial hook. A product name does not make the interchangeable
        # "X PRA SAIR" tail informative; apply an extra penalty so the name-hook
        # bonus cannot rescue this empty mold. Emoji at the end must not bypass
        # the rule (the live failure ended in the male-tuxedo sequence).
        if re.search(r"\b(?:PRA|PARA) SAIR(?:\s*[^\w\s]+)?\s*$", _hu):
            score -= 70
        # Agency slogans that add no product fact. Name overlap must not rescue
        # them: "ABSOLUTO SEM MEIA MEDIDA" tied with the factual amadeirado angle.
        if re.search(r"\bSEM MEIA MEDIDA\b", _hu):
            score -= 90
        # Interchangeable campaign language: it can be pasted onto any SKU and
        # carries no verified note, inspiration, benefit or occasion.
        if re.search(
            r"\b(?:SEM MODERACAO|SEM COMPLICACAO|FORA DO COMUM|SAIR DA MESMICE|"
            r"PRA VARIAR|PARA VARIAR|UM ELIXIR DIFERENTE|TROQUE O CHEIRO|"
            r"ESCOLHA FEMININA DIARIA|TODO DIA COM CHEIRO BOM|SEM PENSAR MUITO)\b",
            _hu,
        ):
            score -= 120
        # Measured in the first 20 trustworthy Normalizer→Edição cycles: Breno
        # rewrote these generic demographic/presence shells into named facts,
        # product hooks or concrete notes. Penalise the mold, not the words in a
        # substantive phrase such as "AURA DE LUXO E PRESENÇA".
        if re.fullmatch(
            r"(?:NACIONAL DE PRESENCA|PERFUME FEMININO (?:PRA HOJE|VERSATIL)|"
            r"ARABE MASCULINO VERSATIL|SEU CHEIRO,? SUA PRESENCA)",
            _hu,
        ):
            score -= 140
        # Reward a concrete angle: named dupe, olfactive note, or occasion.
        _has_angle = bool(re.search(
            r"\b(DUPE|CLONE|PARECIDO COM|IGUAL AO|LEMBRA|FIEL AO|FIEL DEMAIS AO|FIEL D\+ AO|VERSAO DE)\b", _hu
        )) or headline_is_pure_descriptive(h, ctx.get("olfactive_terms") or [])
        if _has_angle:
            score += 20
    # 77 paired Normalizador -> Edição cycles showed the recurring weak shape:
    # an abstract compliment with no product anchor ("PRESENÇA NA ROTINA",
    # "VERSÁTIL PRA ELE", "MARCANTE PRA SAIR BEM"). Penalise only when the line
    # has neither a verified inspiration/note nor a semantic hook in the name.
    _generic_abstract = bool(re.search(
        r"\b(?:VERSATIL|PRESENCA|MARCANTE|PRA ELE|PARA ELE|PRA SAIR BEM|SEU ESTILO|"
        r"ROTINA|PRA TODO DIA|PRA SAIR|PRESENCA FORTE|SEU CHEIRO)\b",
        _hu,
    ))
    _known_inspiration = bool(str(ctx.get("known_inspiration") or "").strip())
    _specific_terms = {
        _headline_norm(str(t)).upper() for t in (ctx.get("olfactive_terms") or [])
        if _headline_norm(str(t)).lower().strip() not in generic_olfactive_terms()
    }
    _uses_specific_term = any(t and t in _hu for t in _specific_terms)
    if _generic_abstract and not _known_inspiration and not _uses_specific_term and headline_name_hook_score(h, product_text) == 0:
        score -= 70
    # A generic word is acceptable only when the same line also carries a
    # verified anchor. This catches interchangeable abstractions that happen
    # to contain the product name but add no usable fact.
    if _generic_abstract and not (
        _known_inspiration and any(w in _hu for w in _headline_norm(str(ctx.get("known_inspiration") or "")).split() if len(w) >= 4)
    ) and headline_name_hook_score(h, product_text) == 0 and not _uses_specific_term:
        score -= 35
    # If this exact SKU has a human-confirmed inspiration, an interchangeable
    # shell is not a safe fallback. This closes the Glacier Ultra/Ultra Male and
    # Perseus/Pegasus misses without forcing inspiration onto unknown products.
    _inspiration_words = [
        w for w in _headline_norm(str(ctx.get("known_inspiration") or "")).split()
        if len(w) >= 4
    ]
    _mentions_inspiration = bool(_inspiration_words and any(w in _hu for w in _inspiration_words))
    _empty_generic_headline = bool(re.search(
        r"\b(?:UM PERFUME BEM CHEIROSO|UM PERFUME ARABE|PERFUME PRA SE ARRUMAR|"
        r"CHEIRO PRA MARCAR PRESENCA|ASSINATURA NACIONAL)\b",
        _hu,
    ))
    if _known_inspiration and _empty_generic_headline and not _mentions_inspiration:
        score -= 220
    # A concrete noun is not automatically a useful angle. "UM FRASCO ÁRABE"
    # merely names the container and can describe every perfume in the category;
    # Breno's correction to "UM CLÁSSICO ÁRABE" adds reputation/positioning.
    # Penalise the empty container angle, but never auto-award "clássico": that
    # status still needs source/editorial evidence rather than scorer invention.
    if re.search(r"(?i)\b(?:um|esse|o)\s+frasco\s+(?:árabe|arabe)\b", h):
        score -= 100
    price_urgency = bool(re.search(
        r"\bPRE[ÇC]O\b|N[AÃ]O PERDE|APROVEITA AGORA|🚨|R\$\s*\d|\bPOR\s+\d{2,3}\b|\bMENOS DE\s+\d{2,3}\b",
        h,
    ))
    if price_urgency:
        # Numeric/urgency price copy is a strong trigger only when the upstream
        # commercial gate marked this exact full-size offer exceptional. Without
        # that evidence it is not merely weak copy — it is an unsupported claim.
        _kit_unit_price = float(ctx.get("_kit_unit_price") or 0)
        _headline_numbers = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", h)]
        _is_proven_unit_price = bool(
            _kit_unit_price > 0
            and any(abs(x - _kit_unit_price) < 0.011 for x in _headline_numbers)
            and re.search(r"(?i)\b(?:CADA|POR\s+(?:UNIDADE|SPLASH|PERFUME)|/\s*(?:UN|SPLASH))\b", h)
        )
        score += 24 if ctx.get("_price_attractive") or _is_proven_unit_price else -170
    # Identity guard: never sell an árabe/importado as "NACIONAL" (and never
    # call a national line árabe). 2026-07-08 batch: Rayhaan Wolf (Lattafa
    # house style) went out as "NACIONAL COM CARA DE CARO".
    # 2026-07-25: this guard was silently DEAD — _headline_norm() returns
    # UPPERCASE while every marker below is lowercase, so `is_arab` and
    # `is_national` were always False and no provenance lie was ever blocked.
    # Comparing on a lowercased copy revives it.
    product_norm = _headline_norm(str(ctx.get("_product") or "")).lower()
    arab_markers = ("lattafa", "arabe", "rasasi", "alhambra", "wataniah", "afnan", "armaf", "bidaya", "rayhaan", "asdaaf", "al ", "oud")
    national_markers = ("boticario", "natura", "eudora", "quasar", "malbec", "kaiak", "egeo", "essencial")
    is_arab = any(m in product_norm for m in arab_markers)
    is_national = any(m in product_norm for m in national_markers)
    if is_arab and not is_national and re.search(r"\bNACIONAL\b|BOTICARIO|NATURA\b", _headline_norm(h).upper() or h):
        score -= 300
    if is_national and not is_arab and ("ARABE" in _headline_norm(h).upper() or "DUBAI" in h):
        score -= 300
    if "IMPORTADO" in _headline_norm(h).upper() and "importado" not in product_norm:
        # Provenance claim without proof in the product name (Nautica Voyage
        # "FRUTADO FRESCO IMPORTADO" 2026-07-10 — user: "falsa nem é importado").
        score -= 300
    for term in BAD_HEADLINE_TERMS:
        if _headline_norm(term) in hn:
            score -= 80
    for term in ctx.get("avoid_terms") or []:
        tn = _headline_norm(str(term).strip())
        if tn and tn in hn:
            score -= 100
    # Hint bonus, weighted by how much the term actually differentiates.
    # A catalog-wide filler ("amadeirado": 60% of products) is worth far less
    # than a specific note ("bergamota", "couro"). Both are still capped, so no
    # headline can win by stacking descriptors.
    _generic = generic_olfactive_terms()
    _hint_bonus = 0
    _specific_hit = False
    for term in (ctx.get("safe_angles") or []) + (ctx.get("olfactive_terms") or []):
        tn = _headline_norm(str(term).strip())
        if not tn:
            continue
        if any(part in hn for part in tn.split() if len(part) >= 4):
            if tn.lower().strip() in _generic:
                _hint_bonus += 6
            else:
                _hint_bonus += 18
                _specific_hit = True
    score += min(_hint_bonus, 18)
    # Naming the known inspiration is the single most valuable thing a headline
    # can do here (Breno rewrote CLUB DE NUIT NO SILLAGE -> FIEL AO SILVER
    # MOUNTAIN). Reward it well above any descriptor combination.
    _insp = _headline_norm(str(ctx.get("known_inspiration") or ""))
    _insp_matched = False
    if _insp:
        _insp_words = [w for w in _insp.split() if len(w) >= 4]
        if _insp_words and any(w in hn for w in _insp_words):
            score += 45
            _insp_matched = True
    if _specific_hit:
        score += 8
    if any(x in hn for x in ["INSPIRADO", "DUPE", "CONTRATIPO"]):
        score += 14 if ctx.get("known_inspiration") else -15
    if re.search(r"\b(?:INSPIRADO|DUPE|CONTRATIPO|FIEL AO|FIEL DEMAIS|FIEL D\+ AO)\b", _hu) and not ctx.get("known_inspiration"):
        score -= 300
    known_inspiration = _headline_norm(str(ctx.get("known_inspiration") or ""))
    if known_inspiration:
        inspiration_tokens = {
            t for t in re.findall(r"[a-z0-9]+", known_inspiration.lower())
            if len(t) >= 3 and t not in {"carolina", "herrera", "parfums", "perfume"}
        }
        matched = inspiration_tokens & _headline_tokens
        # A proven inspiration is usually the strongest buying shortcut. Reward
        # naming it materially; the old +14 generic "INSPIRADO" bonus let empty
        # name puns beat "212 VIP MEN" for Vodka Man (2026-07-19).
        if matched:
            score += 45 + 12 * min(2, len(matched))
    # Title echo is a HARD veto, not a style preference. headline_is_just_product_name()
    # already existed and was never consulted by the scorer: "9PM EDP DE 100ML"
    # scored a clean 100 while flagged just_name=True. It stayed invisible only
    # because the generator's favourite always won; giving the ranker teeth
    # (2026-08-02) surfaced it immediately. Breno rewrites every one of these
    # ("YSL Y EDT 100ML" -> "UM MARCO DA PERFUMARIA NO PREÇO").
    if headline_looks_like_catalog_title(h, str(ctx.get("_product") or "")):
        score -= 200
    # Anti-monotony of FORM (2026-08-02). Measured over 120 published headlines:
    # PRA_X 25%, ADJ_ADJ 20% — 45% of the feed in two templates, while the
    # content-level anti-repetition saw no problem because every string differed.
    # Penalise a mold already worn out in the last 10 posts so a different angle
    # can outrank it. Dupe/inspiration molds carry real buying information and
    # are Breno's own favourite register, so they decay far more gently.
    _mold = headline_mold(h)
    if _mold and _mold != "OUTRO":
        _mold_counts = ctx.get("_recent_molds")
        if _mold_counts is None:
            _mold_counts = recent_headline_molds(10)
            ctx["_recent_molds"] = _mold_counts
        _used = _mold_counts.get(_mold, 0)
        # Monotony is a defect of FORM. A headline that carries a concrete FACT —
        # a named inspiration or a specific (non-generic) olfactive note — is not
        # filler even when its shape repeats, and it is Breno's own dominant
        # register. Measured 2026-08-02: "ALMÍSCAR COM BAUNILHA" (his own
        # correction, score 146) was dropped to 91 by the ADJ_ADJ penalty and
        # lost to the vaguer "BAUNILHA QUE PEDE ELOGIO" (126). Substance must
        # outrank shape, so factual headlines decay far more gently.
        # Substance detection must not depend on ctx["olfactive_terms"] being
        # populated: in real audit rows it is frequently empty, which silently
        # disabled this protection and let "ALMÍSCAR COM BAUNILHA" lose again.
        # Read the notes out of the headline itself — a descriptive line built
        # from terms OUTSIDE the generic catalog-wide set (amadeirado, fresco,
        # cítrico, âmbar, aromático, especiado) is carrying a real fact.
        _generic_set = generic_olfactive_terms()
        _named_notes = {
            t for t in re.findall(r"[a-z0-9]+", _headline_norm(h).lower())
            if len(t) >= 4 and t in DESCRIPTIVE_VOCAB and t not in _generic_set
        }
        _informative = (
            _mold in {"FIEL_AO", "DUPE"}
            or _specific_hit
            or bool(_insp and _insp_matched)
            or bool(_named_notes)
        )
        if _used >= 4:
            score -= 20 if _informative else 55
        elif _used == 3:
            score -= 10 if _informative else 30
        elif _used == 2:
            score -= 0 if _informative else 12
    # Emoji bonus: Breno uses one in ~33% of his own rewrites (30 of 92 measured
    # 2026-08-02) while the automation shipped 0% of 120. The old bonus only
    # recognised four hardcoded glyphs, so aroma emojis he actually writes
    # (🍃 🌊 🍊 💪 🤵🏻‍♂️) earned nothing.
    # A/B with the real model (2026-08-02, 20 calls): Luna DOES generate emoji
    # options — "MENTA COM MADEIRA 🍃", "AQUÁTICO PRA REFRESCAR 🌊" — but at +10
    # they scored 110 vs 100 and still lost, because beating the generator's
    # favourite requires a 30-point margin. The bonus was mathematically unable
    # to decide anything. Then the PROMPT was tightened to actually request
    # emoji options, and the balance inverted: measured 2026-08-02 over 14 real
    # Luna calls, the published rate was 64% at bonus=0 and 71% at bonus=35 —
    # the generator alone had become the driver and the bonus was irrelevant.
    # So the prompt asks for a MINORITY of emoji options (Breno's target is
    # 30-40%) and the bonus is a nudge for a coherent emoji, never enough to
    # rescue weak copy or outrank a named dupe (+45).
    # Final calibration (Breno asked for ~30%): sweep over cached real options
    # gives 14% at 0-15, 21% at 20-25, 28% at 30. A live run at bonus=30 with a
    # looser prompt reached 50%, because the generator itself offered more emoji
    # options. A later real six-card round shipped 0/6 from the Normalizer while
    # Breno's finals used 3/6; a later 15-card live batch overshot badly after the
    # option quota was fixed, so keep the exact 30-40% candidate quota and reduce
    # the ranking nudge. Emoji decorates the winner; it must not choose the winner.
    if re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", h):
        score += int(config_value("PERFUME_HEADLINE_EMOJI_BONUS", "20") or 20)
    return score


def llm_judge_headline(product: str, options: list[str], ctx: dict) -> tuple[str, str]:
    """Second-pass LLM judge: pick the best among veto-surviving options.

    Rationale (2026-07-25 refactor): headline_score() is excellent at detecting
    defects (invented origin, banned terms, repetition, note-listing) and poor
    at ranking two *good* headlines — it can only compare arithmetic proxies.
    So the scorer now acts as a VETO filter and the LLM does the final pick.
    The judge never sees which option the generator preferred, so it cannot
    rubber-stamp its own favourite.

    Returns (headline, reason). Empty headline means "judge unavailable" and the
    caller must fall back to arithmetic ranking.
    """
    if config_value("PERFUME_HEADLINE_JUDGE", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return "", ""
    if len(options) < 2:
        return "", ""
    shuffled = list(options)
    random.shuffle(shuffled)
    listing = "\n".join(f"{i + 1}. {h.strip('* ')}" for i, h in enumerate(shuffled))
    prompt = f"""Você é editor-chefe de um canal brasileiro de promoções de perfume no WhatsApp.
Escolha a MELHOR headline da lista para o produto abaixo. Não reescreva nenhuma; apenas escolha.

CRITÉRIO, nesta ordem:
1. INFORMA algo útil? (imita qual perfume, como cheira, o que a pessoa ganha) — informação vence atitude sempre.
2. Inspiração comprovada é um argumento forte, não uma obrigação: não force dupe quando outra opção factual expressar melhor esta oferta.
3. Descrever o perfil olfativo é ótimo — não penalize por ser "descritiva demais".
4. Entre opções igualmente verdadeiras, prefira a ancorada neste produto/flanker à que serviria para qualquer perfume da mesma categoria.
5. Preço simples não vence automaticamente nome, dupe, nota ou reputação comprovada; precisa ser realmente excepcional e formar uma boa história.
6. Rejeite frase de efeito vazia, trocadilho forçado com o nome, e swagger de publicidade.

Rejeite mentalmente as que forem só elogio vazio ou que serviriam pra qualquer perfume.

PRODUTO: {product}

CONTEXTO COMPROVADO:
{format_editorial_context(ctx)}

VARIEDADE: entre opções igualmente boas e comprovadas, prefira um argumento diferente das headlines recentes abaixo. Novidade nunca justifica uma frase pior ou inventada.
{' | '.join(recent_sent_headlines()[-5:])}

OPÇÕES:
{listing}

Responda SOMENTE JSON: {{"escolha": <número>, "motivo": "até 10 palavras"}}
"""
    out, err = run_headline_llm(prompt)
    if not out:
        return "", f"judge unavailable: {err[:80]}"
    try:
        try:
            data = json.loads(out)
        except Exception:
            blocks = re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}', out)
            data = json.loads(blocks[-1]) if blocks else None
        if isinstance(data, dict):
            idx = int(data.get("escolha") or 0)
            if 1 <= idx <= len(shuffled):
                return shuffled[idx - 1], str(data.get("motivo") or "escolhido pelo juiz")[:180]
    except Exception:
        pass
    return "", "judge returned unparseable choice"


def best_clean_headline(candidates: list[str], raw_headline: str, ctx: dict, recent_set: set[str]) -> str:
    clean: list[str] = []
    for cand in candidates:
        ch = clean_headline(cand, raw_headline)
        if not ch or ch in clean:
            continue
        if ch.strip("*").upper() in recent_set or headline_recently_used(ch.strip("* ")):
            continue
        clean.append(ch)
    if not clean:
        for cand in candidates:
            ch = clean_headline(cand, raw_headline)
            if ch and ch not in clean:
                clean.append(ch)
    if not clean:
        return ""
    ranked = sorted(clean, key=lambda x: headline_score(x, ctx), reverse=True)
    # Scorer as VETO, LLM as judge. Anything below the veto threshold carries a
    # hard penalty (invented provenance, banned term, pure note-listing,
    # product-name echo) and must never reach the judge. Survivors are all
    # "legal", so the arithmetic ordering among them carries little signal.
    _VETO_FLOOR = 60
    # The veto is a SAFETY gate and must ignore the mold-diversity penalty.
    # Measured 2026-08-02: a legal "PERFUME PRA NOITE" fell below 60 purely
    # because PRA_X was worn out, every option got vetoed, and the code fell
    # back to ranked[0] — which was the raw product title "9PM EDP DE 100ML".
    # Monotony must never be able to promote a title echo. Style penalties
    # therefore only reorder survivors; they never decide legality.
    _ctx_safety = dict(ctx)
    _ctx_safety["_recent_molds"] = {}
    survivors = [h for h in ranked if headline_score(h, _ctx_safety) >= _VETO_FLOOR]
    global LAST_HEADLINE_OPTIONS, LAST_HEADLINE_JUDGE_REASON
    LAST_HEADLINE_OPTIONS = [
        {"headline": h.strip("* "), "score": headline_score(h, ctx), "vetoed": h not in survivors}
        for h in ranked
    ]
    LAST_HEADLINE_JUDGE_REASON = ""
    # One-call design (Breno, 2026-07-25): the generator already returns both
    # five options AND its favourite in the same JSON. A second sequential LLM
    # judge doubled latency while trying to rediscover subjective taste it does
    # not possess. Honour the generator's first candidate when it survives all
    # deterministic safety/identity vetoes. Only fall back to arithmetic ranking
    # when the chosen favourite is illegal or missing.
    preferred = clean[0]
    # One-call design preserved, but the ranker is no longer decorative.
    # Measured 2026-08-02: 124 of 538 audited headlines shipped as
    # `llm_generator_choice` with reason "passed deterministic vetoes" — the
    # generator's option 1 won by default in every legal case, so headline_score
    # only ever removed defects and never chose. When another survivor beats the
    # favourite by a MATERIAL margin (roughly one worn-out-mold penalty), the
    # scorer now overrides. Small differences still defer to the generator,
    # which is better at subjective taste than arithmetic.
    # Calibrated 2026-08-02 against the last 80 audited option sets, AFTER the
    # veto was made blind to style penalties. The margin must sit below the
    # worn-out-mold penalty (max 55) or the override can never fire: at 60 it
    # changed 1 pick in 80 (inert). Sweep: 20 -> 16 changes, 30 -> 13, 40 -> 7.
    # 30 buys the full diversity gain (PRA_X 25% -> 20%) while still deferring
    # to the generator in 67 of 80 cases.
    # Arabic headlines need a firmer factual preference. The generator tends to
    # promote generic campaign copy/name-play even when a concrete note/profile
    # option scores 20 points higher (observed 2026-08-22: "ACHADO PRA SAIR DO
    # ÓBVIO" beat "DA LAVANDA AO AMADEIRADO"). Brazilian name-play remains a
    # valid house style, so keep the more deferential margin there.
    _OVERRIDE_MARGIN = 15 if _arabic_perfume_product(str(ctx.get("_product") or "")) else 30
    if preferred in survivors:
        _pref_score = headline_score(preferred, ctx)
        _best = survivors[0]
        # Do not let additive bonuses force a stuffed comparison+price line over
        # the generator's cleaner factual favourite. Observed on Reyna: the
        # scorer promoted "ORIANA CREMOSA A R$149" by 126 points over the
        # cleaner floral-frutado angle that Breno ultimately kept. Inspiration
        # and price are each valid; cramming both into one short headline is not
        # automatically better editorial judgment.
        def _stacks_inspiration_and_price(value: str) -> bool:
            insp = _headline_norm(str(ctx.get("known_inspiration") or ""))
            words = [w for w in insp.split() if len(w) >= 4]
            norm = _headline_norm(value)
            return bool(words and any(w in norm for w in words) and re.search(r"R\$\s*\d|\bPOR\s+\d", norm))

        if (
            _best != preferred
            and not (_stacks_inspiration_and_price(_best) and not _stacks_inspiration_and_price(preferred))
            and headline_score(_best, ctx) - _pref_score >= _OVERRIDE_MARGIN
        ):
            LAST_HEADLINE_JUDGE_REASON = (
                f"scorer override: better angle/mold beat generator favourite "
                f"by {headline_score(_best, ctx) - _pref_score} pts"
            )
            return _best
        LAST_HEADLINE_JUDGE_REASON = "generator favourite passed deterministic vetoes"
        return preferred
    if survivors:
        LAST_HEADLINE_JUDGE_REASON = "generator favourite vetoed; safest survivor selected"
        return survivors[0]
    # Never publish an option we just classified as illegal. Returning empty
    # invokes the retry path; the former ranked[0] fallback shipped catalog
    # filler such as "UM ÁRABE DE 100ML" after vetoing every candidate.
    LAST_HEADLINE_JUDGE_REASON = "all generated options vetoed; retry required"
    return ""


def _headline_llm_chain() -> list[tuple[str, str]]:
    """Ordered (provider, model) attempts: primary then fallback subscription."""
    chain = [(config_value("PERFUME_HEADLINE_PROVIDER", "").strip(), config_value("PERFUME_HEADLINE_MODEL", "").strip())]
    fb_provider = config_value("PERFUME_HEADLINE_FALLBACK_PROVIDER", "").strip()
    fb_model = config_value("PERFUME_HEADLINE_FALLBACK_MODEL", "").strip()
    if fb_provider or fb_model:
        chain.append((fb_provider, fb_model))
    return chain


def _opencode_go_key() -> str:
    try:
        for raw in (Path.home() / ".hermes/.env").read_text(encoding="utf-8").splitlines():
            if raw.startswith("OPENCODE_GO_API_KEY="):
                return raw.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def run_headline_llm_http(prompt: str, model_override: str = "") -> tuple[str, str]:
    """Direct OpenCode Go chat-completions call: ~10s vs ~40s via hermes CLI."""
    key = _opencode_go_key()
    if not key:
        return "", "no OPENCODE_GO_API_KEY"
    model = (model_override or config_value("PERFUME_HEADLINE_MODEL", "deepseek-v4-flash")).strip() or "deepseek-v4-flash"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 12000,  # reasoning model: thinking eats budget before content; 4000 truncated mid-thought and forced salvage-garbage headlines (2026-07-08 fiasco)
        "temperature": 0.9,
        # Medium is the current quality/latency compromise for editorial copy:
        # Flash is inexpensive, while high historically pushed requests toward
        # 60-70s. Breno explicitly preferred more headline intelligence (2026-07-14).
        "reasoning_effort": config_value("PERFUME_HEADLINE_REASONING", "medium"),
    }).encode("utf-8")
    req = Request(
        "https://opencode.ai/zen/go/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Hermes"},
    )
    try:
        timeout = remaining_headline_seconds(float(config_value("PERFUME_HEADLINE_HTTP_TIMEOUT", "30")))
        if timeout <= 0:
            return "", "headline total budget exhausted"
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        out = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if out:
            return out, ""
        return "", "http: empty completion"
    except Exception as exc:
        return "", f"http {model}: {exc}"


def _run_headline_llm_http_race(prompt: str) -> tuple[str, str]:
    """Use one HTTP call, or race primary/alternate when explicitly configured."""
    import concurrent.futures as _fut
    errs: list[str] = []
    alt = config_value("PERFUME_HEADLINE_RACE_MODEL", "").strip()
    if not alt:
        return run_headline_llm_http(prompt)
    # NO `with` block: exiting the context manager calls shutdown(wait=True),
    # which blocks until the losing model finishes.
    ex = _fut.ThreadPoolExecutor(max_workers=2)
    from contextvars import copy_context
    futures = [
        ex.submit(copy_context().run, run_headline_llm_http, prompt),
        ex.submit(copy_context().run, run_headline_llm_http, prompt, alt),
    ]
    try:
        for f in _fut.as_completed(futures):
            try:
                out, err = f.result()
            except Exception as exc:
                out, err = "", f"race: {exc}"
            if out:
                return out, ""
            errs.append(err)
        return "", "; ".join(e for e in errs if e)[:200]
    finally:
        ex.shutdown(wait=False)


def run_headline_llm(prompt: str) -> tuple[str, str]:
    """Keep provider failures per request, even when the final route is fallback."""
    started = time.monotonic()
    budget = remaining_headline_seconds(9999)
    minimum_useful = float(config_value("PERFUME_HEADLINE_MIN_USEFUL_SECONDS", "20"))
    if budget < minimum_useful:
        error = f"headline call skipped: insufficient useful budget ({budget:.3f}s < {minimum_useful:.3f}s)"
        attempts = getattr(_HEADLINE_DIAGNOSTICS, "attempts", None)
        if attempts is None:
            attempts = []
            _HEADLINE_DIAGNOSTICS.attempts = attempts
        attempts.append({"seconds": 0.0, "budget_before": budget, "response_chars": 0,
                         "status": "skipped_budget", "error": error})
        return "", error
    try:
        _HEADLINE_DIAGNOSTICS.accepted_rc = None
        out, error = _run_headline_llm_impl(prompt)
    except Exception as exc:
        out, error = "", f"{type(exc).__name__}: provider invocation failed"
    attempts = getattr(_HEADLINE_DIAGNOSTICS, "attempts", None)
    if attempts is None:
        attempts = []
        _HEADLINE_DIAGNOSTICS.attempts = attempts
    accepted_rc = getattr(_HEADLINE_DIAGNOSTICS, "accepted_rc", None)
    attempts.append({"seconds": round(time.monotonic() - started, 3),
                     "budget_before": budget, "response_chars": len(out),
                     "status": ("response_partial_rc" if out and accepted_rc not in (None, 0)
                                else "response" if out else "error"),
                     "error": error[:240]})
    return out, error


def _headline_partial_json_ok(out: str) -> bool:
    """True when a non-zero hermes exit still carries a complete headline JSON.

    Covers the three headline prompts (generator/retry: "options", generator
    also "headline"; judge: "escolha"). Kill switch:
    PERFUME_HEADLINE_ACCEPT_PARTIAL_JSON=0 restores strict rc==0.
    """
    if config_value("PERFUME_HEADLINE_ACCEPT_PARTIAL_JSON", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if re.search(r"(?i)^error: http \d{3}", out):
        return False
    try:
        data = json.loads(out)
    except Exception:
        blocks = re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}', out)
        try:
            data = json.loads(blocks[-1]) if blocks else None
        except Exception:
            return False
    if not isinstance(data, dict):
        return False
    options = data.get("options")
    if isinstance(options, list) and len([x for x in options if x]) >= 3:
        return True
    return isinstance(data.get("escolha"), (int, str)) and str(data.get("escolha")).strip() != ""


def _run_headline_llm_impl(prompt: str) -> tuple[str, str]:
    """Run the headline prompt: direct HTTP first, then the CLI provider chain.

    Returns (stdout, error). stdout non-empty means a usable answer from some
    provider; error carries the last failure for the audit report.

    Also records LAST_HEADLINE_MODEL so the audit can distinguish a primary-model
    answer from a fallback one. Without it, "quality dropped" is unattributable:
    the audit said `llm_ranked` whether Luna or DeepSeek wrote the line.
    """
    global LAST_HEADLINE_MODEL
    if remaining_headline_seconds(1) <= 0:
        return "", "headline total budget exhausted"
    if config_value("PERFUME_HEADLINE_DIRECT_HTTP", "1").strip().lower() in {"1", "true", "yes", "on"}:
        out, http_err = _run_headline_llm_http_race(prompt)
        if out:
            LAST_HEADLINE_MODEL = f"http:{config_value('PERFUME_HEADLINE_MODEL', 'deepseek-v4-flash').strip()}"
            return out, ""
        last_err = http_err
    else:
        last_err = "direct HTTP disabled; using Hermes provider adapter"
    for provider, model in _headline_llm_chain():
        cmd = [config_value("PERFUME_HERMES_CLI", "/home/ubuntu/.local/bin/hermes"), "chat", "-Q", "--source", "tool", "--max-turns", "1", "--safe-mode", "-t", "", "-q", prompt]
        if model:
            cmd[2:2] = ["--model", model]
        if provider:
            cmd[2:2] = ["--provider", provider]
        try:
            timeout = remaining_headline_seconds(float(config_value("PERFUME_HEADLINE_TIMEOUT", "90")))
            if timeout <= 0:
                return "", "headline total budget exhausted"
            r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        except Exception as exc:
            last_err = f"{provider or 'default'}/{model or 'default'}: exception {exc}"
            continue
        out = (r.stdout or "").strip()
        accepted_rc = r.returncode
        if r.returncode != 0 and out and _headline_partial_json_ok(out):
            # hermes -Q exits 1 when the turn hits --max-turns 1 (model also
            # emitted a tool call) even though stdout already holds the full
            # JSON answer. Shadow 2026-09-26: 8/12 Luna answers were valid
            # 5-option JSON with rc=1 and were being discarded.
            pass
        elif r.returncode != 0 or not out or re.search(r"(?i)^error: http \d{3}", out):
            last_err = f"{provider or 'default'}/{model or 'default'}: rc={r.returncode} {(r.stderr or out or '')[:120]}"
            continue
        _HEADLINE_DIAGNOSTICS.accepted_rc = accepted_rc
        LAST_HEADLINE_MODEL = f"{provider or 'default'}/{model or 'default'}"
        return out, ""
    LAST_HEADLINE_MODEL = ""
    return "", last_err or "no provider configured"


def _distinctive_facts_note(ctx: dict) -> str:
    """Tell the model how much real material it has, and how to behave without it.

    Measured 2026-07-25: with a known inspiration the model produced "SOSPIRO
    VIBRATO NO MAWWAL"; without one it fell back to "MALIBU DA RASASI" — a
    paraphrase of the product name, which Breno rejects. The failure mode is
    forcing a hook when no distinguishing fact exists, so make that explicit
    and authorise short neutral copy instead of manufactured wit.
    """
    insp = str(ctx.get("known_inspiration") or "").strip()
    generic = generic_olfactive_terms()
    specific = [
        str(t).strip() for t in (ctx.get("olfactive_terms") or [])
        if _headline_norm(str(t)).lower().strip() not in generic
    ]
    if insp:
        return (
            f"FATO FORTE DISPONÍVEL: este perfume é conhecido por lembrar {insp}. "
            "Pelo menos 2 das 5 opções devem NOMEAR essa referência."
        )
    if specific:
        return (
            f"NOTAS QUE DIFERENCIAM: {', '.join(specific[:4])}. "
            "Prefira estas a termos genéricos como amadeirado/fresco/especiado, "
            "que descrevem a maioria do catálogo e não dizem nada."
        )
    return (
        "SEM FATO DISTINTIVO CONFIÁVEL: não há inspiração conhecida nem nota "
        "específica registrada. NÃO invente e NÃO force trocadilho com o nome — "
        "headline que só reordena o nome do produto é pior que headline simples. "
        "Prefira copy curta, honesta e direta sobre uso ou versatilidade."
    )


def known_inspiration_from_text(text: str) -> str:
    """Extract only an explicit inspiration claim already present in source text."""
    clean = strip_wa(text or "")
    match = re.search(
        r"(?i)\b(?:"
        r"lembra(?:\s+(?:muito|demais))?(?:\s+[oa])?(?:\s+(?:luxuos[oa]|ic[oô]nic[oa]))?|"
        r"inspirad[oa]\s+(?:no|na|em)|"
        r"no\s+estilo(?:\s+do|\s+da)?|"
        r"dupe\s+(?:do|da)(?:\s+(?:luxuos[oa]|ic[oô]nic[oa]))?|"
        r"fiel(?:\s+demais)?\s+(?:ao|[àa])"
        r")\s+"
        r"([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 +'&.-]{1,44})",
        clean,
    )
    if not match:
        return ""
    value = re.split(r"[;|\n]|\s{2,}", match.group(1), maxsplit=1)[0]
    # Editorial intensifiers describe the reference; they are not part of its
    # canonical name ("lembra o de grife Sedley" -> "Sedley").
    value = re.sub(r"(?i)^(?:o|a)?\s*(?:de\s+grife|luxuos[oa]|ic[oô]nic[oa])\s+", "", value.strip())
    return value.strip(" .,-")[:45]


def ranked_editorial_facts(product: str, body: str, ctx: dict) -> list[str]:
    """Ordered fact stack with strong, generic, and prohibited rails separated."""
    def _fact_values(value) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return [str(x).strip() for x in (value or []) if str(x).strip()]

    facts: list[str] = []
    inspiration = str(ctx.get("known_inspiration") or "").strip()
    if not inspiration:
        inspiration = known_inspiration_from_text(body)
    if inspiration:
        facts.append(f"PRIORIDADE 1 — inspiração comprovada: {inspiration}")
    pn = _headline_norm(product)
    semantic = []
    for marker, label in [
        ("ICE", "frio/gelo/abaixo de zero"), ("GOLD", "ouro/dourado"),
        ("NIGHT", "noite/noturno"), ("MILLION", "milhão/riqueza"),
        ("OCEAN", "mar/oceano"), ("AQUA", "água/mar"), ("VOYAGE", "viagem"),
        ("NÓS", "nós/união/assinatura"), ("EVOLUT", "evolução/evoluído"),
        ("ABSOLUTO", "absoluto/absolutamente"), ("DOM", "dom/talento"),
        ("BRAVE", "bravo/coragem"), ("SUGAR", "doce/açúcar"),
        ("SPORT", "esporte/academia"), ("STARWALKER", "limpeza/elegância"),
        ("EXCLUSIVO", "exclusividade/mais que essencial"),
    ]:
        # Match name tokens, not accidental substrings (Spice is not ICE,
        # Kingdom is not DOM). EVOLUT intentionally covers Evolut/Evolutio.
        pattern = r"\bEVOLUT\w*\b" if marker == "EVOLUT" else r"\b" + re.escape(_headline_norm(marker)) + r"\b"
        if re.search(pattern, pn):
            semantic.append(label)
    if semantic:
        facts.append("PRIORIDADE 2 — gancho semântico do nome: " + ", ".join(semantic))
    generic = generic_olfactive_terms()
    specific = [str(x).strip() for x in (ctx.get("olfactive_terms") or []) if _headline_norm(str(x)).lower().strip() not in generic]
    if specific:
        facts.append("PRIORIDADE 3 — notas específicas: " + ", ".join(specific[:4]))
    safe = _fact_values(ctx.get("safe_angles"))
    concrete = [
        x for x in safe
        if _headline_norm(x).lower().strip() not in generic_olfactive_terms()
        and not re.search(r"(?i)\b(?:vers[aá]til|presen[cç]a|seu estilo|pra ele|para ele|rotina)\b", x)
    ]
    if concrete:
        facts.append("PRIORIDADE 4 — benefício/ocasião concreta: " + ", ".join(concrete[:4]))
    proof = _fact_values(ctx.get("proof_points") or ctx.get("social_proof"))
    if proof:
        facts.append("PRIORIDADE 5 — prova social comprovada: " + ", ".join(proof[:3]))
    generic = [x for x in safe if x not in concrete]
    if generic:
        facts.append("GENÉRICO — só use se vier ancorado: " + ", ".join(generic[:4]))
    facts.append(
        "PROIBIDO/SEM EVIDÊNCIA — não inventar dupe, nota, reputação, origem, "
        "benefício, ocasião, prova social ou gênero"
    )
    if not facts:
        facts.append("SEM FATO FORTE — use verdade curta; não fabrique trocadilho")
    return facts


def llm_headline(product: str, raw_headline: str, body: str = "", price_context: dict | None = None) -> str:
    global LAST_HEADLINE_SOURCE, LAST_HEADLINE_REASON
    LAST_HEADLINE_SOURCE = "llm_disabled"
    LAST_HEADLINE_REASON = "PERFUME_HEADLINE_LLM off"
    if config_value("PERFUME_HEADLINE_LLM", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    LAST_HEADLINE_SOURCE = "llm_attempt"
    LAST_HEADLINE_REASON = ""
    recent = recent_approved_headlines()
    recent_style = " | ".join(h.strip("*") for h in recent[-12:])
    sent_style = " | ".join(recent_sent_headlines()[-10:])
    editorial_context = load_editorial_context(product)
    if not editorial_context.get("known_inspiration"):
        explicit_inspiration = known_inspiration_from_text(f"{raw_headline}\n{body}")
        if explicit_inspiration:
            editorial_context["known_inspiration"] = explicit_inspiration
    editorial_context["_product"] = product
    editorial_context["_price_attractive"] = bool((price_context or {}).get("attractive"))
    editorial_context_text = format_editorial_context(editorial_context)
    fact_priority_text = "\n".join("- " + x for x in ranked_editorial_facts(product, body, editorial_context))
    _is_arabic_offer = _arabic_perfume_product(product)
    _personalized_v3 = config_value("PERFUME_HEADLINE_ARCHITECTURE", "v2").strip().lower() == "v3"
    _pairs = breno_corrected_headline_pairs(24, product=product)
    if len(_pairs) > 8:
        if _personalized_v3:
            # V3 retrieval is relevance-ranked; keep the closest examples.
            _pairs = _pairs[:8]
        else:
            # Preserve the production-v2 rotation until the challenger proves a
            # higher approval rate in shadow replay/live cycles.
            _off = int(time.time() // 900) % len(_pairs)
            _pairs = (_pairs + _pairs)[_off:_off + 8]
    corrections_block = "\n".join(f"- {a}  →  {b}" for a, b in _pairs) or "- nenhuma registrada"
    _labeled = breno_labeled_headline_preferences(product, 8)
    labeled_block = "\n".join(
        f"- {str(x.get('label')).upper()}: {x.get('headline')}"
        for x in _labeled
    ) or "- nenhum rótulo explícito semelhante"
    # Tell the generator which templates the feed is already saturated with, so
    # it can avoid them up front instead of relying only on the scorer penalty.
    _molds_now = recent_headline_molds(10)
    _worn = [k for k, v in sorted(_molds_now.items(), key=lambda kv: -kv[1]) if v >= 3 and k != "OUTRO"]
    _recent_molds_note = (
        ", ".join(_worn) + " — evite repetir estes, busque outro ângulo"
        if _worn else "nenhum saturado; fique à vontade"
    )
    # Number of options is configurable so 5-vs-8 can be decided with measured
    # data instead of intuition. More options = a wider pool for the ranker, at
    # the cost of latency and of the model padding the list with filler.
    _n_options = max(3, min(10, int(config_value("PERFUME_HEADLINE_OPTIONS", "5") or 5)))
    _options_slots = ",".join(['"..."'] * _n_options)
    # Keep 30-40% of the candidate pool emoji-bearing: 3/8 in production, 2/5
    # when running the smaller configuration. This gives the ranker enough good
    # emoji options without turning every card into carnival.
    _n_options_emoji = max(1, min(_n_options - 1, round(_n_options * 0.375)))
    _n_options_sem_emoji = _n_options - _n_options_emoji
    # Match the published money formatter: it drops cents instead of rounding
    # them up. A R$226.xx card must never produce R$227 in the headline.
    _headline_price = int(float((price_context or {}).get("final_price", 0) or 0))
    _kit_match = re.search(r"(?i)\b(?:kit\s*(?:com\s*)?)?(\d{1,2})\s*(?:un|unidades?|splashes|perfumes?)\b", product)
    _kit_qty = int(_kit_match.group(1)) if _kit_match else 0
    _kit_unit_instruction = ""
    if _kit_qty >= 2 and _headline_price > 0:
        _unit_price = float((price_context or {}).get("final_price", 0) or 0) / _kit_qty
        editorial_context["_kit_unit_price"] = _unit_price
        _unit_text = f"{_unit_price:.2f}".replace(".", ",")
        _kit_unit_instruction = (
            f" KIT COM {_kit_qty} UNIDADES: economia unitária comprovada de R$ {_unit_text} por unidade. "
            "Este é um ângulo comercial forte e simples; gere uma opção com o valor por unidade."
        )
    price_angle_instruction = (
        f"Preço final excepcional e validado para este full-size: R$ {_headline_price}, desconto de aproximadamente {(price_context or {}).get('discount_pct', 0):.0f}%. "
        "Preço numérico é permitido e pode ser a melhor headline quando for o argumento de compra mais forte. "
        "Combine-o com nome curto, perfil ou benefício somente quando isso virar uma frase natural (ex.: FAKHAR POR MENOS DE 170); "
        "um limiar psicológico factual também vale (ex.: MENOS DE 100 NO VOYAGE). Não use cupom."
        if editorial_context["_price_attractive"] else
        "Preço não foi classificado como excepcional: não use número, desconto ou urgência de preço na headline, "
        "exceto economia unitária factual de kit quando a instrução seguinte fornecer o cálculo."
    )
    price_angle_instruction += _kit_unit_instruction
    name_play_instruction = (
        "PERFUME ÁRABE: NÃO faça trocadilho, tradução ou jogo com o nome do perfume. "
        "O nome pode aparecer apenas como identidade factual. Priorize, nesta ordem: inspiração/dupe comprovado, "
        "perfil ou nota específica, benefício/ocasião comprovada e preço excepcional. Se não houver fato forte, "
        "escreva uma verdade simples; nunca invente uma sacada com o nome."
        if _is_arabic_offer else
        "PERFUME NÃO ÁRABE: jogo com o nome é permitido quando natural e informativo, especialmente em linhas "
        "brasileiras; nunca force a piada nem deixe o nome vencer um fato mais forte."
    )
    _generation_brief = (
        """Crie 8 opções INDEPENDENTES e realmente diferentes entre si. Não entregue oito paráfrases da mesma ideia. Cubra uma TRILHA por opção; se não houver evidência para uma trilha, substitua por outra variação factual, sem inventar:
1) inspiração/dupe nomeado
2) perfil olfativo específico
3) premissa comercial explícita — feromônios podem render humor de paquera; kit pode render quantidade/economia
4) benefício ou identidade concreta do comprador
5) situação/ocasião comprovada
6) valor percebido/luxo/custo-benefício com apoio da oferta ou marca
7) jogo natural com nome apenas quando a REGRA DE FAMÍLIA permitir; para árabe, outra trilha factual
8) escolha livre que não repita as anteriores
Preço excepcional pode entrar em UMA trilha quando autorizado. Não empilhe preço + dupe + nota."""
        if _personalized_v3 else
        """Crie opções INDEPENDENTES e realmente diferentes entre si. Pelo menos 2 opções DEVEM ser ancoradas no produto; não conte apenas 'pra ele', 'presença' ou 'versátil' como âncora. Explore conforme a evidência: inspiração/dupe, perfil olfativo específico, benefício concreto, ocasião concreta e preço excepcional quando autorizado."""
    )
    existing_pool = eligible_repertoire_candidates(product, price_context)
    variety_brief = (
        f"REPERTÓRIO DESTE SKU: {len(existing_pool)} opções distintas elegíveis; objetivo de 4–5 boas opções. "
        + " | ".join(existing_pool[:5])
        + "\nGere candidatos para ampliar os argumentos disponíveis, não apenas trocar palavras ou emoji das frases acima. "
        "Use somente fatos comprovados. Se só houver um argumento seguro, não invente outro para preencher quota. "
        "Um candidato gerado ainda não é uma headline aprovada; prefira qualidade a completar cinco."
    )
    prompt = f"""Você escreve headlines brasileiras para promoções de perfume no WhatsApp.

{variety_brief}

OBJETIVO: primeiro escolha o argumento específico comprovado do produto/oferta; depois escreva uma frase natural. Se a transformação em benefício parecer fabricada, prefira a informação concreta. Uma combinação curta de notas confirmadas pode ser a melhor headline; não acrescente 'sem culpa', 'pra coleção' ou ocasião inventada para fazê-la parecer criativa.

ESCOLHA DO ARGUMENTO: quando houver inspiração comprovada, gere uma opção que a nomeie, sem obrigar todas as opções a usar dupe. Outra opção deve explorar um fato diferente disponível. Para kits, use o custo por unidade somente quando quantidade e preço estiverem demonstrados no contexto; custo unitário não comprova desconto. Prova social exige evidência. Nunca invente experiência pessoal do editor, compra própria, aprovação popular ou esgotamento. Correções humanas são exemplos de direção, não frases para copiar.

{_generation_brief}

VARIANTES DE DUPE que o editor usa (não só "FIEL AO"):
- NO ESTILO 212 VIP MEN — quando lembra sem ser cópia fiel
- SOSPIRO + GOD OF FIRE 🤌🏻 — soma de DOIS perfumes conhecidos, com o sinal +
- DUPE NACIONAL DO VIBRATO — quando a origem nacional é o argumento
- FIEL AO PACIFIC CHILL — quando a semelhança é forte e direta

REGRA DE FAMÍLIA: {name_play_instruction}

JOGO COM O NOME DO PRODUTO: esta técnica vale SOMENTE quando a REGRA DE FAMÍLIA acima permitir. Quando permitida e o nome render uma volta natural, ela pode ser uma boa escolha. O editor escreveu ABSOLUTAMENTE ABSOLUTO (Homem Absoluto), BAUNILHA COM FAVA É O DOM (Homem Dom) e QUASAR ABAIXO DE 0 (Quasar Ice). Não aplique esse mecanismo a perfumes árabes.

NOTAS: uma combinação curta de notas confirmadas é válida quando diferencia o produto; não force sensação, ocasião ou benefício sem evidência. Linguagem sensorial natural também vale. Categoria vazia ('árabe', 'unissex', 'bem cheiroso') não substitui o argumento específico disponível.

HEADLINE RECEBIDA: trate-a como pista editorial, não como decoração. Se ela já
enxerga um jogo verdadeiro com o nome do produto, preserve a IDEIA sem copiar a
frase literalmente e gere pelo menos 2 variações desse jogo. Não troque uma
sacada específica por frases genéricas de ocasião. Ex.: se o produto traz
BILLION/MILLION e a recebida fala em bilionários, explore milhão/bilhão/riqueza
em pelo menos 2 opções antes de sugerir "pra noite" ou "bem arrumado".


MALÍCIA ÚTIL:
- Situação concreta vence abstrações como "seu estilo", "versátil", "dia ativo" e "pra todo dia".
- A imagem precisa vender PERFUME. Se o gancho de identidade for bom, conclua no território de cheiro: "UM GENTLEMAN BEM CHEIROSO" comunica melhor que "UM GENTLEMAN BEM VESTIDO", que parece anúncio de roupa. Preserve a sacada e corrija apenas o payoff quando ele sair da categoria.
- Jogo com nome/marca precisa valorizar o comprador ou explicar a oferta. Se for só piada solta, descarte; se amarrar com a nota ou o benefício, é das melhores saídas.
- Origem nacional/árabe pode ser um diferencial quando for verdadeira e relevante.
- Antes de responder, pergunte: "isso informa algo ou cria uma imagem concreta?" Se não, reescreva.

CALIBRAÇÃO DOS CICLOS NORMALIZADOR → EDIÇÃO:
- Preserve identidade específica, mas em perfume árabe não transforme o nome em trocadilho. ASAD, FAKHAR ou SABAH podem identificar o produto; o argumento comercial deve vir de inspiração, perfil, nota, benefício comprovado ou preço excepcional.
- Preço na headline não ganha por existir. Use-o só quando for realmente a melhor história; perfil, dupe, reputação comprovada ou jogo inteligente com o nome vencem um simples "X A R$Y".
- Percentual quebrado não vira headline: "41% MENOS NO BOTTLED", "42% MENOS NO SAFEER" e "43% DE DESCONTO" soam como planilha. O De/Por já mostra essa economia. Exceção: marcos psicológicos redondos como "50% OFF" e "75% OFF" são comerciais e podem ser o próprio gancho quando verdadeiros.
- Nota olfativa também não vira comercial só por receber INTENSO + preço. "TUBEROSA INTENSA A R$169" é ficha técnica colada à etiqueta. Use a nota apenas se ela criar sensação/imagem natural; caso contrário, escolha outro ângulo comprovado.
- Prova social como "FAVORITO DO GRUPO" é forte somente quando o contexto/histórico fornecer essa evidência. Nunca invente preferência do grupo.
- Tente deixar pelo menos 2 opções ancoradas no produto específico (nome, flanker, dupe ou nota distintiva), não apenas na categoria demográfica.
- Em perfumes não árabes, quando houver volta natural com o nome (ABSOLUTO→ABSOLUTAMENTE, EVOLUT→EVOLUÍDO, BRAVE→BRAVO, DOM→DOM), gere uma opção desse tipo. Em perfumes árabes, esta regra NÃO se aplica.
- Emoji é acabamento, não resgate: 30-40% das opções devem receber UM emoji semanticamente coerente. Nunca use emoji para maquiar "precinho", "imperdível" ou outra frase intercambiável.
- Ordem para árabes: inspiração comprovada > perfil/nota específica > benefício ou ocasião concreta > preço excepcional > prova social comprovada; sem jogo semântico do nome.
- Ordem para brasileiros/não árabes: inspiração comprovada > jogo semântico natural do nome/flanker > nota específica > benefício ou ocasião concreta > prova social comprovada.
- "MARCANTE", "VERSÁTIL", "PRESENÇA", "PRA ELE", "PRA SAIR BEM", "ROTINA" e "SEU ESTILO" são abstrações intercambiáveis: penalize-as e só use com uma âncora específica verificável.
- Transforme, não copie: use NÓS como união/assinatura; SPORT como academia; STARWALKER como limpeza elegante; EXCLUSIVO como identidade do flanker. Estes são padrões de raciocínio, não frases prontas.

Depois escolha a melhor. Responda SOMENTE JSON válido:
{{"options":[{_options_slots}],"headline":"...","reason":"até 10 palavras"}}

FORMATO:
- CAIXA ALTA, máximo 29 caracteres.
- EMOJI: EXATAMENTE {_n_options_emoji} das {_n_options} opções devem terminar com UM emoji que reforce uma nota COMPROVADA ou a cena — 🌊 aquático, 🌬️ fresco/arejado, 🍃 menta/verde, 🍊 cítrico, 🥭 SOMENTE manga comprovada, 🍍 SOMENTE abacaxi comprovado, 🤌🏻 nicho/refinado, 🔥 intenso, 🤵🏻‍♂️ elegante, 💪 forte, 🍬 doce, 🌹 floral. "Tropical" ou "frutado" sozinho NÃO autoriza escolher manga/abacaxi: sem a fruta exata no contexto, não use emoji de fruta. As outras {_n_options_sem_emoji} opções vêm OBRIGATORIAMENTE sem emoji. Meta editorial: 30-40% das headlines publicadas, nunca zero numa rodada longa. Nunca decorativo, nunca mais de um, só em headline curta (até 26 caracteres), e jamais 🔥 como coringa genérico.
- Sem cupom ou link. Preço numérico só quando a instrução PREÇO declarar a oferta excepcional.

VARIE A FORMA: as headlines saem em sequência no grupo, uma atrás da outra. Se as {_n_options} opções tiverem todas a mesma estrutura, o feed fica monótono. Evite que mais de 2 opções compartilhem o mesmo molde (todas "PRA ALGUMA COISA", ou todas "ADJETIVO E ADJETIVO"). Moldes recentes já muito usados: {_recent_molds_note}

LEIS APRENDIDAS COM O EDITOR (ele reescreve TODA vez que violamos):
- Superlativo vago é proibido: ABSURDO, MUITOO, MENOR PREÇO JÁ VISTO, IMPERDÍVEL, TÁ MUITO BARATO. Troque sempre por nota olfativa concreta ou dupe nomeado. Ex.: ele corrigiu "BAUNILHA ABSURDA" → "ALMÍSCAR COM BAUNILHA", "MENOR PREÇO JÁ VISTO" → "BAUNILHA COM FAVA É O DOM".
- INTENSIDADE DO DUPE É OBRIGATÓRIA: com inspiração forte/comprovada (confiança alta), escreva "FIEL DEMAIS AO X"; se passar do limite visual, compacte para "FIEL D+ AO X". Use "FIEL AO X" simples somente quando a semelhança for moderada ou a confiança não for alta. Nunca use "INSPIRADO NO X" quando a fidelidade estiver comprovada. O editor corrigiu "FIEL AO AVENTUS" → "FIEL DEMAIS AO AVENTUS" e "CLUB DE NUIT NO SILLAGE" → "FIEL D+ AO SILVER MOUNTAIN". Não omita essa intensidade só para variar o feed.
- Corte "É O"/"É A" no início e diminutivos: "É O MALBEC DO VERDE" → "O MALBEC DO VERDE", "VERSÃO DOCINHA DO SABAH" → "VERSÃO DOCE DO SABAH".
- NUNCA devolva o nome/título do produto como headline. Se sair "YSL Y EDT 100ML" ou "FAKHAR GOLD", você falhou: ele reescreveu esses para "UM MARCO DA PERFUMARIA NO PREÇO" e "FIEL AO ONE MILLION PARFUM".
- SUBSTANTIVO ÚTIL: não confunda concretude com informação. "UM FRASCO ÁRABE POR R$118" só descreve o recipiente e serve para qualquer perfume; o editor corrigiu para "UM CLÁSSICO ÁRABE POR R$118", que posiciona reputação e valor. Prefira categoria, status ou característica do perfume a embalagem genérica — mas só diga CLÁSSICO, ÍCONE ou QUERIDINHO quando contexto/histórico sustentar; não invente reputação.

VERDADE:
- Não invente origem, notas, inspiração, reputação, ocasião, benefício ou prova social. Sem fato distintivo, prefira verdade curta a sacada forçada.
- Em kits, a cena deve continuar literal e natural: quem vai na bolsa/mochila é o produto, não um homem. Não force portabilidade ou ocasião sem evidência; prefira o benefício simples ou os componentes comprovados.
- Quando houver "inspiração/dupe conhecido" no contexto, pelo menos 2 opções devem nomeá-lo — é o atalho de compra mais forte que existe.
- Nunca chame árabe de nacional, nem nacional de árabe. Nunca diga "importado" sem prova.

TOM QUE FUNCIONA (referência de registro, NÃO copie estas):
{chr(10).join('- ' + x for x in approved_headline_examples(6)) or "- ESSE É O BÁSICO QUE FUNCIONA"}

PADRÕES CULTURAIS DO EDITOR (adapte o MECANISMO a este produto; jamais reaproveite a frase):
{cultural_headline_patterns() or "- nenhum padrão registrado"}

REESCRITAS REAIS DO EDITOR (esquerda: o que erramos → direita: como ele corrigiu).
Estude o PADRÃO da correção, não copie as frases:
{corrections_block}

RÓTULOS CEGOS EXPLÍCITOS DO BRENO (APPROVED ele publicaria sem mudar; REJECTED ele não publicaria).
Use como preferência contrastiva e NÃO como frase para copiar:
{labeled_block}

JÁ REPROVADAS PELO EDITOR — não escreva nada nesse espírito:
{chr(10).join('- ' + x for x in rejected_headline_examples(8)) or "- nenhuma"}

NÃO REPITA aprovadas/enviadas: {recent_style or "nenhuma"} | {sent_style or "nenhuma"}
CONTEXTO LOCAL: {editorial_context_text[:900] or "nenhum"}
HIERARQUIA DE FATOS (use o primeiro disponível; não deixe um nível fraco vencer um forte):
{fact_priority_text}
{_distinctive_facts_note(editorial_context)}
PREÇO: {price_angle_instruction}
PRODUTO: {product}
HEADLINE RECEBIDA: {raw_headline or ""}
TEXTO: {strip_wa(body or "")[:450]}
"""
    if not _personalized_v3:
        # Same provider/ranker and factual retrieval, without the repeated multi-page
        # brief that consumed the live deadline before a useful candidate arrived.
        prompt = f"""Escreva {_n_options} headlines brasileiras para WhatsApp. JSON somente:
{{"options":[{_options_slots}],"headline":"melhor opção","reason":"justificativa curta"}}
Máximo 28 caracteres, CAIXA ALTA, uma ideia por opção. Até um emoji coerente em {_n_options_emoji} opções curtas; nenhuma fruta sem a fruta exata comprovada.
Não devolva título de catálogo nem copie a headline recebida ou as frases de exemplo.
{_generation_brief}
{name_play_instruction}
Priorize inspiração comprovada, depois fato específico ou jogo natural permitido. Dupe forte: FIEL DEMAIS/FIEL D+; moderado: FIEL AO. Não invente intensidade, notas, ocasião, origem, reputação, experiência do editor, fixação ou projeção.
Não use abstrações intercambiáveis (PRESENÇA, ELEGANTE, VERSÁTIL), luxo genérico, COM CARA/CHEIRO DE CARO, FIXAÇÃO BRUTA, TROPICAL CHIQUE ou ACHADO PRA SAIR DO ÓBVIO. Não invente urgência. Kit: componentes/quantidade reais; economia por unidade só quando fornecida.
{price_angle_instruction}
SKU: {product}
FATOS DO SKU: {editorial_context_text[:1200]}
{fact_priority_text}
{_distinctive_facts_note(editorial_context)}
FONTE: {strip_wa(body or '')[:450]}
HEADLINE RECEBIDA: {raw_headline}
EXEMPLOS DE DIREÇÃO (não copiar):
{corrections_block}
PREFERÊNCIAS CONTRASTIVAS:
{labeled_block}
{variety_brief}
RECENTES (não repetir): {recent_style} | {sent_style}
MOLDES SATURADOS: {_recent_molds_note}
"""
    out, err = run_headline_llm(prompt)
    if not out:
        LAST_HEADLINE_SOURCE = "llm_error"
        LAST_HEADLINE_REASON = err[:160]
        return ""  # make_headline owns the single bounded retry
    recent_set = {h.strip("*").upper() for h in recent_approved_headlines(30)}
    try:
        try:
            data = json.loads(out)
        except Exception:
            # Reasoning models may prefix prose; grab the last JSON object in the text.
            blocks = re.findall(r'\{(?:[^{}]|\{[^{}]*\})*\}', out)
            data = json.loads(blocks[-1]) if blocks else None
        if isinstance(data, dict):
            candidates = [str(data.get("headline") or "")]
            candidates.extend(str(x.get('headline') or '') if isinstance(x, dict) else str(x)
                              for x in (data.get("options") or []) if x)
            picked = best_clean_headline(candidates, raw_headline, editorial_context, recent_set)
            if picked:
                # The judge's reason describes the PUBLISHED headline. The
                # generator's `reason` describes whichever option the generator
                # liked, which is often not the one that shipped — reporting it
                # as the rationale was quietly false.
                if LAST_HEADLINE_JUDGE_REASON:
                    if LAST_HEADLINE_JUDGE_REASON.startswith("generator favourite passed"):
                        LAST_HEADLINE_SOURCE = "llm_generator_choice"
                    elif LAST_HEADLINE_JUDGE_REASON.startswith("generator favourite vetoed"):
                        LAST_HEADLINE_SOURCE = "llm_alternative_selected"
                    else:
                        LAST_HEADLINE_SOURCE = "llm_ranked"
                    LAST_HEADLINE_REASON = LAST_HEADLINE_JUDGE_REASON
                else:
                    LAST_HEADLINE_SOURCE = "llm_ranked"
                    LAST_HEADLINE_REASON = str(data.get("reason") or "ranked by editorial context")[:180]
                return picked
    except Exception:
        pass
    # Backward compatibility if a provider ignores the JSON instruction.
    candidates = re.findall(r'"headline"\s*:\s*"([^"]+)"', out)
    candidates.extend(re.findall(r'"([A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9 ,!?.🤌🏻🧕🏻🌙✨-]{6,42})"', out))
    candidates.append(out)
    picked = best_clean_headline(candidates, raw_headline, editorial_context, recent_set)
    if picked:
        LAST_HEADLINE_SOURCE = "llm_salvaged_ranked"
        LAST_HEADLINE_REASON = "provider ignored JSON; salvaged and ranked a clean headline"
        return picked
    LAST_HEADLINE_SOURCE = "llm_rejected"
    LAST_HEADLINE_REASON = "no clean non-repeated LLM option"
    return ""  # make_headline owns the single bounded retry


def llm_headline_retry(product: str, raw_headline: str, body: str = "", recent_set: set[str] | None = None) -> str:
    global LAST_HEADLINE_SOURCE, LAST_HEADLINE_REASON
    if config_value("PERFUME_HEADLINE_LLM", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    recent_set = recent_set or {h.strip("*").upper() for h in recent_approved_headlines(30)}
    prompt = f"""A primeira headline falhou/repetiu. Gere 5 opções MAIS CRIATIVAS, mas todas honestas e ancoradas no produto quando houver evidência.
Responda SOMENTE JSON: {{"options":["...","...","...","...","..."]}}

Regras: CAIXA ALTA, até 28 caracteres, sem preço/cupom/link; pelo menos 2 opções devem usar nome/flanker, inspiração/nota/benefício comprovado. Não use conhecimento geral para inventar dupe, nota, reputação, origem ou ocasião. Evite abstrações intercambiáveis como PRESENÇA, VERSÁTIL, PRA ELE, ROTINA e PRA SAIR BEM.
Produto: {product}
Headline recebida: {raw_headline or ""}
Texto: {strip_wa(body or "")[:500]}
Contexto do SKU exato (não transferir fatos de outras variantes):
{format_editorial_context(load_editorial_context(product))}
"""
    out, err = run_headline_llm(prompt)
    if not out:
        LAST_HEADLINE_SOURCE = "llm_retry_error"
        LAST_HEADLINE_REASON = f"retry: {err[:150]}"
        return ""
    candidates: list[str] = []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            candidates.extend(str(x.get('headline') or '') if isinstance(x, dict) else str(x)
                              for x in (data.get("options") or []) if x)
    except Exception:
        pass
    candidates.extend(re.findall(r'"([A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9 ,!?.🤌🏻🧕🏻🌙✨-]{6,42})"', out))
    candidates.append(out)
    for cand in candidates:
        cleaned = clean_headline(cand, raw_headline)
        if (cleaned and enforce_headline_output(cleaned)
                and not headline_looks_like_catalog_title(cleaned, product)
                and not headline_inspiration_conflict(cleaned, load_editorial_context(product))
                and cleaned.strip("*").upper() not in recent_set):
            LAST_HEADLINE_SOURCE = "llm_retry"
            LAST_HEADLINE_REASON = "retry selected clean non-repeated option"
            return cleaned
    LAST_HEADLINE_SOURCE = "llm_retry_rejected"
    LAST_HEADLINE_REASON = "retry returned no clean non-repeated option"
    return ""


def enforce_headline_output(headline: str) -> str:
    """Final publishing gate shared by LLM, reuse and deterministic fallbacks."""
    norm = _headline_norm(headline)
    if (norm in {'AQUATICO', 'ORIENTAL', 'AMADEIRADO', 'AROMATICO', 'CITRICO', 'FLORAL', 'GOURMAND', 'IMPORTADO DE PRESENCA'}
            or re.fullmatch(r'PERFUME\s+(?:ARABE|IMPORTADO|MASCULINO|FEMININO)(?:\s+\d+\s*ML)?\s*[^A-Z0-9]*', norm)):
        return ''
    cleaned = clean_headline(headline)
    if cleaned and headline_visual_len(strip_wa(cleaned)) <= 28:
        return cleaned
    # Remove only a decorative emoji to salvage an otherwise valid full phrase.
    plain = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\ufe0f\u200d]", "", headline or "")
    cleaned = clean_headline(plain)
    if cleaned and headline_visual_len(strip_wa(cleaned)) <= 28:
        return cleaned
    # A failed validator must not manufacture its own explicitly banned fallback.
    return ""


def publishing_fallback_headline(product: str, raw_headline: str = "", body: str = "") -> str:
    """Always publish a usable fallback after repertoire + Luna are exhausted."""
    # Never republish the incoming editorial angle after a model failure.
    # In particular, source claims about fixation are not product evidence.
    ctx = load_editorial_context(product)
    inspiration = str(ctx.get("known_inspiration") or known_inspiration_from_text(body)).strip()
    if inspiration:
        candidate = enforce_headline_output(f"FIEL AO {inspiration}")
        if candidate:
            return candidate
    source = _headline_norm(raw_headline + " " + body)
    if "DIAS QUENTES" in source and re.search(r"FRESC|CITRIC|LIMAO|MACA", source):
        return "*FRESCOR PRA DIAS QUENTES*"
    terms = [str(t).strip() for t in (ctx.get("olfactive_terms") or []) if str(t).strip()]
    # Prefer two grounded cues to a one-word taxonomy label.
    for index, term in enumerate(terms):
        for other in terms[index + 1:]:
            if _headline_norm(term) == _headline_norm(other):
                continue
            candidate = enforce_headline_output(f"{term} E {other}")
            if candidate:
                return candidate
    for term in terms:
        if len(_headline_norm(term).split()) >= 2:
            candidate = enforce_headline_output(term)
            if candidate:
                return candidate
    if re.search(r"(?i)\bbody\s+(?:splash|spray)\b", product):
        return "*PRA PERFUMAR APÓS O BANHO*"
    brand = product_brand(product)
    if brand:
        candidate = enforce_headline_output(f"{brand.upper()} PRA APROVEITAR")
        if candidate:
            return candidate
    return "*PERFUME PRA APROVEITAR*"


def make_headline(product: str, raw_headline: str, body: str = "", price_context: dict | None = None) -> str:
    """Isolate editorial exceptions from commercial facts and image processing."""
    _HEADLINE_DIAGNOSTICS.attempts = []
    _HEADLINE_DIAGNOSTICS.value = {
        "source": "", "reason": "", "model": "", "options": [],
        "judge_reason": "", "attempts": [],
    }
    _HEADLINE_DIAGNOSTICS.approved_final_cache = {}
    _HEADLINE_DIAGNOSTICS.approved_final_cache_active = True
    try:
        return _make_headline_bounded(product, raw_headline, body, price_context)
    except Exception as exc:
        # Never let a provider/parser/context failure discard validated offer facts.
        _HEADLINE_DIAGNOSTICS.value = {
            "source": "editorial_pending", "reason": f"editorial exception: {type(exc).__name__}",
            "model": "", "options": [], "judge_reason": "",
            "attempts": list(_HEADLINE_DIAGNOSTICS.attempts),
        }
        return ""
    finally:
        _HEADLINE_DIAGNOSTICS.approved_final_cache_active = False
        _HEADLINE_DIAGNOSTICS.approved_final_cache = {}


def _make_headline_bounded(product: str, raw_headline: str, body: str = "", price_context: dict | None = None) -> str:
    # One shared allowance for generator, judge, retry and provider fallbacks.
    # The factual/local fallback remains available when the allowance is spent.
    global LAST_HEADLINE_SOURCE, LAST_HEADLINE_REASON, LAST_HEADLINE_OPTIONS, LAST_HEADLINE_JUDGE_REASON, LAST_HEADLINE_MODEL
    total = float(config_value("PERFUME_HEADLINE_TOTAL_BUDGET", "60"))
    # Queue only the model section. Evidence, title resolution, official-store
    # validation and image work still use the intake's parallel workers. The
    # budget starts after admission: waiting behind another generation must not
    # consume a request's LLM allowance and manufacture a fallback.
    queued = time.monotonic()
    with _HEADLINE_LLM_SLOTS:
        # Clear process-global telemetry only after this request owns the narrow
        # headline lane. Otherwise one completed offer can leak its diagnosis.
        LAST_HEADLINE_SOURCE = ""
        LAST_HEADLINE_REASON = ""
        LAST_HEADLINE_OPTIONS = []
        LAST_HEADLINE_JUDGE_REASON = ""
        LAST_HEADLINE_MODEL = ""
        llm_queue_seconds = time.monotonic() - queued
        with headline_budget(total):
            # Reserve a bounded retry for candidates rejected at the actual output gate.
            with headline_budget(total * 0.75):
                candidate = _make_headline_impl(product, raw_headline, body, price_context)
            final = enforce_headline_output(candidate)
            if LAST_HEADLINE_SOURCE.startswith("fallback") and (
                headline_is_pure_descriptive(final)
                or _headline_norm(final) in {'AQUATICO', 'ORIENTAL', 'AMADEIRADO', 'AROMATICO', 'CITRICO', 'FLORAL', 'GOURMAND'}
                or _headline_norm(final) == _headline_norm(raw_headline)
                or re.search(r"\b(?:FIXACAO BRUTA|PROJECAO DE IMPORTADO|ELOGIO GARANTIDO)\b", _headline_norm(final))
            ):
                final = publishing_fallback_headline(product, raw_headline, body)
            if final:
                LAST_HEADLINE_REASON = (LAST_HEADLINE_REASON + f"; llm_queue_seconds={llm_queue_seconds:.3f}").strip("; ")
                _capture_headline_diagnostics()
                return final
            # A model invocation with only the outer reserve left repeatedly
            # consumed another 14-16 seconds merely to report budget exhaustion.
            # Keep enough wall time for a real retry; otherwise publish the local
            # factual fallback immediately and record why the retry was skipped.
            retry_budget = remaining_headline_seconds(9999)
            if retry_budget >= 20:
                retry = llm_headline_retry(product, raw_headline, body)
            else:
                retry = ""
                LAST_HEADLINE_REASON = (
                    LAST_HEADLINE_REASON
                    + f"; retry_skipped_budget remaining={retry_budget:.3f}s"
                ).strip("; ")
            final = enforce_headline_output(retry)
            if final:
                LAST_HEADLINE_SOURCE = "llm_final_gate_retry"
                LAST_HEADLINE_REASON = (LAST_HEADLINE_REASON + f"; llm_queue_seconds={llm_queue_seconds:.3f}").strip("; ")
                _capture_headline_diagnostics()
                return final
            final = publishing_fallback_headline(product, raw_headline, body)
            LAST_HEADLINE_SOURCE = "fallback_rule"
            prior_reason = LAST_HEADLINE_REASON.strip("; ")
            LAST_HEADLINE_REASON = (
                (prior_reason + "; " if prior_reason else "")
                + "LLM and exact-SKU repertoire produced no valid headline; factual fallback published"
                + f"; llm_queue_seconds={llm_queue_seconds:.3f}"
            )
            _capture_headline_diagnostics()
            return final


def _make_headline_impl(product: str, raw_headline: str, body: str = "", price_context: dict | None = None) -> str:
    global LAST_HEADLINE_SOURCE, LAST_HEADLINE_REASON
    # Reuse is fully opt-in. Both per-product corrections and editorial-context
    # seeds must stay off together; otherwise a disabled approved-reuse setting
    # still bypasses the LLM through `editorial_context_reuse`.
    reuse_enabled = config_value("PERFUME_REUSE_APPROVED_HEADLINE", "").strip().lower() in {"1", "true", "yes", "on"}
    exploration_due = False
    exceptional_offer = False
    if reuse_enabled:
        try:
            exploration_rate = min(1.0, max(0.0, float(config_value("PERFUME_HEADLINE_EXPLORATION_RATE", "0.20"))))
        except (TypeError, ValueError):
            exploration_rate = 0.20
        # Explore every Nth historical appearance instead of hashing only the
        # SKU (which made one product explore forever and another never). The
        # current offer is not yet in comparator history, so retries are stable.
        prior_appearances = 0
        try:
            wanted_keys = editorial_identity_keys(product)
            for raw in EDITORIAL_CYCLES_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                row = json.loads(raw)
                row_product = str(row.get("product") or "")
                if editorial_cycle_eligible(row) and row_product and product_key(row_product) in wanted_keys:
                    prior_appearances += 1
        except Exception:
            prior_appearances = 0
        pool_size = len(eligible_repertoire_candidates(product, price_context))
        if pool_size < 5:
            from perfume_editorial_renewal import enqueue
            try:
                enqueue(product, body, price_context, eligible_repertoire_candidates(product, price_context))
            except OSError as exc:
                print(f'editorial renewal enqueue failed: {exc}', file=sys.stderr)
        interval = repertoire_exploration_interval(pool_size, exploration_rate)
        exploration_due = bool(interval and (prior_appearances + 1) % interval == 0)
        exceptional_offer = bool(isinstance(price_context, dict) and price_context.get("attractive"))
        # Selection and usage commit share a lock: publication always reuses an
        # eligible approved headline before considering any exploratory LLM pass.
        with _HEADLINE_USAGE_LOCK:
            repertoire = approved_headline_repertoire(product, price_context)
            if repertoire:
                if exploration_due and pool_size >= 5:
                    try:
                        from perfume_editorial_renewal import enqueue
                        enqueue(product, body, price_context, eligible_repertoire_candidates(product, price_context))
                    except Exception as exc:
                        print(f'editorial renewal enqueue failed: {type(exc).__name__}', file=sys.stderr)
                _record_repertoire_usage(product_key(product), repertoire)
                reserve_headline(repertoire)
                LAST_HEADLINE_SOURCE = "approved_repertoire"
                return repertoire
        if exceptional_offer and exploration_due:
            LAST_HEADLINE_SOURCE = "llm_exploration"
            LAST_HEADLINE_REASON = (
                f"repertoire bypassed for deterministic renewal appearance "
                f"{prior_appearances + 1}; interval={interval}"
            )
        elif exceptional_offer:
            LAST_HEADLINE_SOURCE = "llm_exceptional_offer"
            LAST_HEADLINE_REASON = "repertoire bypassed for exceptional current commercial offer"
    approved = ""
    if reuse_enabled and not exploration_due and not exceptional_offer:
        approved = approved_headline_for_product(product)
    if approved and (headline_is_just_product_name(approved, product)
                     or headline_inspiration_conflict(approved, load_editorial_context(product))):
        approved = ""
    if approved:
        LAST_HEADLINE_SOURCE = "breno_approved_reuse"
        LAST_HEADLINE_REASON = "reused latest Breno-approved headline for this product"
        return approved
    # Editorial-context seeds are legacy guardrails, not the creative default.
    # Let the current LLM/repertoire produce a fresh product-aware angle first;
    # only reuse these broad seeds when generation is unavailable or unusable.
    contextual = context_approved_headline(product) if reuse_enabled else ""
    if contextual and (headline_is_just_product_name(contextual, product)
                       or headline_inspiration_conflict(contextual, load_editorial_context(product))):
        contextual = ""
    # Guardrail for awkward LLM-but-technically-clean headline seen in prod.
    # "Camisa branca" reads odd/literal in a perfume promo feed.
    if re.search(r"(?i)\btommy\s+hilfiger\b", product):
        LAST_HEADLINE_SOURCE = "guardrail_brand_rule"
        LAST_HEADLINE_REASON = "blocked awkward Tommy/camisa-branca angle"
        return "*TOMMY CLÁSSICO PRA TODO DIA*"
    llm = (llm_headline(product, raw_headline, body, price_context)
           if price_context else llm_headline(product, raw_headline, body))
    if llm and not headline_looks_like_catalog_title(llm, product):
        return llm
    if contextual and not headline_is_pure_descriptive(contextual) and not re.search(r"\b(?:PRESENCA|ACIMA DA MEDIA|PRA APROVEITAR)\b", _headline_norm(contextual)):
        LAST_HEADLINE_SOURCE = "editorial_context_fallback"
        LAST_HEADLINE_REASON = "LLM unavailable/unusable; reused legacy editorial_context seed"
        return contextual
    if not LAST_HEADLINE_SOURCE.startswith("llm"):
        LAST_HEADLINE_REASON = LAST_HEADLINE_REASON or "LLM unavailable"
    LAST_HEADLINE_SOURCE = "fallback_rule"
    LAST_HEADLINE_REASON = LAST_HEADLINE_REASON or "LLM produced no usable headline; using deterministic rule"
    p = product.lower()
    b = (body or "").lower()
    fallback_ctx = load_editorial_context(product)
    inspiration = str(fallback_ctx.get('known_inspiration') or known_inspiration_from_text(body))
    if inspiration:
        for prefix in ('INSPIRADO NO ', 'FIEL AO '):
            candidate = clean_headline(prefix + inspiration, raw_headline)
            if candidate and headline_visual_len(candidate.strip('*')) <= 28:
                return candidate
    # Deterministic fallback examples from Breno: short, product-aware, WhatsApp-native.
    # Avoid generic discount headlines unless there is no useful product cue.
    if "secret absolu" in p or ("banderas" in p and "secret" in p):
        return "*ESSE É SEGREDO* 🤫"
    if "nocturno elixir" in p or ("rayhaan" in p and "nocturno" in p):
        return "*NOTURNO SOFISTICADO* 🌙"
    if "pisa" in p and "lattafa" in p:
        return "*ME SINTO NA ITÁLIA* 🤌🏻"
    if "1 million" in p or "one million" in p:
        return "*CHEIRO DE MILHÕES* ✨"
    if "vulcan feu" in p or "god of fire" in b:
        return "*INSPIRADO GOD OF FIRE*"
    if "salvo intense" in p:
        return "*INTENSO PARA NOITE* 🌙"
    if "botica 214" in p and "sevilla" in p:
        return "*BOTICÁRIO LUXUOSO* 👌🏻"
    if "alpine homme" in p or ("maison alhambra" in p and "alpine" in p):
        return "*AMADEIRADO AROMÁTICO LUXUOSO*"
    if "hawas" in p and "rasasi" in p and not any(v in p for v in ('elixir', 'ice', 'malibu', 'black')):
        return "*FRESCOR AQUÁTICO POTENTE*"
    if "lab 8" in p and "strategy" in p:
        return "*AMBARADO LUXUOSO*"
    if "pacific aura" in p or ("rayhaan" in p and "pacific" in p):
        return "*INSPIRADO NO PACIFIC CHILL*"
    if "the kingdom" in p and "lattafa" in p:
        return "*INSPIRADO NO LE MALE ELIXIR*"
    if "effervescent" in p and "lab 8" in p:
        return "*INSPIRADO NO SOSPIRO VIBRATO*"
    if "club de nuit intense" in p or ("armaf" in p and "intense" in p):
        return "*CONTRATIPO CREED AVENTUS*"
    if "clash" in p:
        return "*FRESCOR AZUL DO BOTICÁRIO*"
    if "polo 67" in p or ("ralph lauren" in p and "67" in p):
        return "*PREÇÃO NO RALPH LAUREN*"
    if "luxuos" in b and "amadeir" in b:
        return "*AMADEIRADO LUXUOSO*"
    if "luxuos" in b and ("ambar" in b or "âmbar" in b):
        return "*AMBARADO LUXUOSO*"
    if "pacific chill" in b:
        return "*INSPIRADO NO PACIFIC CHILL*"
    if "le male elixir" in b:
        return "*INSPIRADO NO LE MALE ELIXIR*"
    if "creed aventus" in b:
        return "*CONTRATIPO CREED AVENTUS*"
    if "mercedes" in p and "club black" in p:
        return "*CLUB BLACK DE RESPEITO*"
    if "yara" in p and "lattafa" in p and ("feminino" in p or "feminin" in body.lower() or "pra elas" in raw_headline.lower()):
        return "*YARA PRA ELAS*"
    if "zanzibar" in p and "asad" in p:
        return "*O AZULZINHO É DIFERENTE*"
    if "vulcan sable" in p or ("sable" in p and "french avenue" in p):
        return "*INSPIRADO NO SAND DANCE*"
    if "ultra bleu" in p and "malbec" in p:
        return "*O QUERIDINHO DOS MALBEC*"
    if "bareeq" in p or "dhahab" in p:
        return "*LUXO DE DUBAI*"
    if "essencial" in p and "oud" in p:
        return "*OUD NACIONAL INTENSO*"
    if "boss" in p and "elixir" in p:
        return "*BOSS ESCURO E ELEGANTE 🤌🏻*"
    if "natura homem" in p:
        return "*NACIONAL COM CARA DE CARO*"
    if "fakhar" in p and "platin" in p:
        return "*PREÇO BAIXO NO PLATINADO*"
    if "polo green" in p or ("ralph lauren" in p and "green" in p):
        return "*RALPH LAUREN VERDIN*"
    if "malbec black" in p:
        return "*UM DOS MAIORES SUCESSOS*"
    if "pure gold" in p and "malbec" in p:
        return "*MALBEC PURO OURO*"
    if "quasar fire" in p:
        return "*PARA OS HOMENS CALIENTES*"
    if "floratta red" in p and ("kit" in p or "presente" in p):
        return "*BAIXOU!! KIT PARA AS DIVAS*"
    if "asad" in p and "lattafa" in p:
        return "*PEGADA NOTURNA ÁRABE*"
    if "kit" in p or "presente" in p:
        return "*KIT PRESENTE PRA APROVEITAR*"
    if "body spray" in p:
        return "*PRA PERFUMAR SEM MISÉRIA*"
    if "malbec" in p:
        return "*MALBEC QUE VALE OURO*" if "gold" in p or "ouro" in p else "*MALBEC DE RESPEITO*"
    if "essencial" in p and "tradicional" in p and "natura" in p:
        return "*NATURA ESSENCIAL CLÁSSICO*"
    if "botic" in p or "zaad" in p or "floratta" in p:
        return fallback_pool_pick([
            "*BOTICÁRIO COM CHEIRO DE CARO*",
            "*O QUERIDINHO DO BOTICÁRIO*",
            "*BOTICÁRIO ACIMA DA MÉDIA*",
            "*NACIONAL QUE IMPRESSIONA*",
            "*BOTICÁRIO DE ELOGIO GARANTIDO*",
        ])
    if "natura" in p:
        return fallback_pool_pick([
            "*NACIONAL COM CARA DE CARO*",
            "*NATURA ACIMA DA MÉDIA*",
            "*O QUERIDINHO DA NATURA*",
            "*NACIONAL QUE IMPRESSIONA*",
            "*NATURA DE ELOGIO GARANTIDO*",
        ])
    if ("árabe" in p or "arabe" in p or "lattafa" in p or "mawwal" in p or "french avenue" in p
            or "rayhaan" in p or "al wataniah" in p or "maison alhambra" in p or "armaf" in p or "afnan" in p):
        return fallback_pool_pick([
            "*ÁRABE OPULENTO 🧕🏻*",
            "*CHEIRO DE DUBAI NO PREÇO*",
            "*ÁRABE DE FIXAÇÃO BRUTA*",
            "*LUXO ÁRABE ACESSÍVEL ✨*",
            "*PROJEÇÃO DE IMPORTADO CARO*",
            "*ÁRABE PRA CHAMAR ATENÇÃO*",
        ])
    if re.search(r"(?i)the most wanted", product):
        return "*O MAIS PROCURADO DA AZZARO*"
    if re.search(r"(?i)azzaro", product):
        return "*AZZARO PRA NOITE*"
    if re.search(r"(?i)(carolina herrera|ralph lauren|paco rabanne|rabanne|yves saint laurent|montblanc|boss)", product):
        return "*DESIGNER COM CHEIRO DE CARO 🤌🏻*"
    # Last-resort headline must still be product-aware. Never return the dead
    # generic "PERFUME PRA REPETIR ELOGIO" again.
    if re.search(r"(?i)(noite|noturno|intense|intenso|elixir)", product + " " + body):
        return "*INTENSO PARA NOITE* 🌙"
    if re.search(r"(?i)(fresh|fresco|blue|azul|ocean|aqua|kaiak|sport)", product + " " + body):
        return "*FRESCOR PRA USAR MUITO*"
    if re.search(r"(?i)(gold|ouro|million|milh[oõ]es)", product + " " + body):
        return "*CHEIRO DE MILHÕES* ✨"
    if re.search(r"(?i)(natura|botic[aá]rio|eudora)", product):
        return fallback_pool_pick([
            "*NACIONAL COM CARA DE CARO*",
            "*NACIONAL QUE IMPRESSIONA*",
            "*NACIONAL DE ELOGIO GARANTIDO*",
        ])
    if re.search(r"(?i)(edp|parfum|importado|designer)", product + " " + body):
        return fallback_pool_pick([
            "*CHEIRO DE PERFUME CARO 🤌🏻*",
            "*ELEGÂNCIA NO FRASCO*",
            "*PERFUME DE PRESENÇA*",
        ])
    return "*ACHADO PRA SAIR DO ÓBVIO*"


def build_shadow_offer(body: str, received_product: str, link: str | None, raw_de: str | None,
                       raw_por: str | None, source_pix: bool, coupons: list[str],
                       source_installments: str | None, catalog: dict) -> dict:
    """Adapter from legacy collectors into the pure shadow reconciler."""
    collected_de, collected_por = shadow_collect_source_prices(body)
    claimed_seller_match = re.search(
        r"(?im)\b(?:selecione|escolha)\s+(?:(?:a|o)\s+)?(?:loja|vendedor)?\s*[:\-]?\s*([A-Za-zÀ-ÿ0-9_. -]{2,45})\s*$",
        strip_wa(body or ""),
    )
    source = ShadowSourceOffer(
        product=received_product,
        link=link or "",
        price=collected_por if collected_por is not None else (Decimal(str(money_value(raw_por))) if money_value(raw_por) is not None else None),
        original_price=collected_de if collected_de is not None else (Decimal(str(money_value(raw_de))) if money_value(raw_de) is not None else None),
        says_pix=source_pix,
        installments=source_installments,
        coupons=tuple(coupons),
        claimed_store=claimed_seller_match.group(1).strip() if claimed_seller_match else None,
    )
    item_id = catalog.get("item_id")
    product_id = catalog.get("product_id")
    confidence = "exact_card" if catalog.get("offer_identity_confidence") == "exact_card" else "exact"
    facts = catalog.get("exact_item_facts") or {}
    evidence: list[ShadowEvidence] = []
    if item_id and catalog.get("offer_identity_confidence") in ("exact", "exact_card"):
        # For API-confirmed exact items, the item API title outranks a scraped
        # card title (which can be a neighboring recommendation). For exact_card
        # fallback, the bounded card title is the identity evidence.
        identity_name = (
            (catalog.get("name") or facts.get("title") or received_product)
            if confidence == "exact"
            else (facts.get("title") or catalog.get("name") or received_product)
        )
        evidence.append(ShadowEvidence(
            "product_name", identity_name,
            "exact_item_card" if confidence == "exact_card" else "item_api",
            item_id=item_id, product_id=product_id, seller_id=catalog.get("seller_id"),
            card_role="featured", confidence=confidence,
        ))
        if isinstance(catalog.get("price"), (int, float)):
            evidence.append(ShadowEvidence("price", Decimal(str(catalog["price"])), "item_api",
                item_id=item_id, product_id=product_id, confidence=confidence, price_kind="regular"))
        exact_original = catalog.get("original_price")
        if not isinstance(exact_original, (int, float)):
            exact_original = facts.get("original_price")
        if isinstance(exact_original, (int, float)):
            evidence.append(ShadowEvidence("original_price", Decimal(str(exact_original)), "exact_item_card",
                item_id=item_id, product_id=product_id, confidence=confidence))
        if isinstance(catalog.get("pix_price"), (int, float)):
            evidence.append(ShadowEvidence("pix_price", Decimal(str(catalog["pix_price"])), "exact_item_card",
                item_id=item_id, product_id=product_id, confidence=confidence, price_kind="pix"))
        if catalog.get("installments"):
            base = facts.get("current_price")
            evidence.append(ShadowEvidence("installments", catalog["installments"], "exact_item_card",
                item_id=item_id, product_id=product_id, confidence=confidence,
                price_base=Decimal(str(base)) if isinstance(base, (int, float)) else None,
                no_interest=bool(facts.get("installments_no_interest"))))
        store_item_source = str(catalog.get("item_id_source") or "")
        # product_href_wid is stable when resolve_link_offer has already validated
        # that exact featured-card wid against the item API/catalog product. The
        # prior exclusion made commercial mode erase proven Wathiq official-store
        # evidence for ordinary social links, even while legacy resolution had
        # accepted the same item_id + official_store_id + store_name.
        store_identity_stable = store_item_source in {
            "direct_item_url", "pdp_filters", "selected_item_state", "product_href_wid"
        }
        if catalog.get("official_store_id") and catalog.get("store_name") and store_identity_stable:
            evidence.append(ShadowEvidence("official_store", catalog["store_name"], "official_store_id",
                item_id=item_id, product_id=product_id, seller_id=catalog.get("seller_id"), confidence=confidence))
        elif (confidence == "exact_card" and catalog.get("official_store_html_exact")
              and catalog.get("store_name")):
            evidence.append(ShadowEvidence("official_store", catalog["store_name"], "exact_featured_card_badge",
                item_id=item_id, product_id=product_id, seller_id=catalog.get("seller_id"), confidence=confidence))
        elif re.search(r"(?i)\bloja\s+(?:verificada|oficial)\b", strip_wa(body or "")) and facts.get("seller_name"):
            # Breno's publishing rule: a seller verified on the exact promoted
            # card may supply the trust line when the source also explicitly
            # labels the offer as verified/official. Seller name alone is not enough.
            evidence.append(ShadowEvidence("official_store", facts["seller_name"], "verified_exact_seller",
                item_id=item_id, product_id=product_id, seller_id=catalog.get("seller_id"), confidence=confidence))
    return shadow_reconcile_offer(source, evidence).to_dict()


def screenshot_offer_facts(media_path: str, expected_product: str) -> dict:
    """Read an attached ML screenshot; ignore product art and recommendation cards."""
    path = Path(media_path or "")
    if not path.is_file() or not Path("/usr/bin/tesseract").exists():
        return {}
    # ML app screenshots are phone-portrait captures. Skip ordinary square
    # product artwork before spawning Tesseract; this saves up to ~3s per promo.
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.height < im.width * 1.20:
                return {}
    except Exception:
        return {}
    try:
        proc = subprocess.run(
            ["/usr/bin/tesseract", str(path), "stdout", "-l", "por+eng", "--psm", "6"],
            text=True, capture_output=True, timeout=15,
        )
        text = proc.stdout or ""
    except Exception:
        return {}
    # Do not read a WhatsApp conversation screenshot as an ML offer card. That
    # can steal a price from the message above the intended promo.
    ml_ui_evidence = bool(re.search(
        r"(?i)buscar\s+no\s+mercado\s+livre|ir\s+para\s+produto|linha\s+de\s+cr[eé]dito|"
        r"quem\s+viu\s+este\s+produto|chegar[aá].{0,20}gr[aá]tis|\bFULL\b", text
    ))
    whatsapp_ui = bool(re.search(r"(?i)normalizador\s+promo|\bBender\b|✅\s*APROVADO|\bMensagem\b", text))
    if not ml_ui_evidence or whatsapp_ui:
        return {}
    featured = re.split(
        r"(?i)quem\s+viu|tamb[eé]m\s+comprou|para\s+voc[eê]\s+mais\s+vendidos",
        text, maxsplit=1,
    )[0]
    if not featured.strip():
        return {}
    ocr_norm = product_key(featured).replace("-", " ")
    expected_tokens = {
        t for t in product_key(expected_product).split("-")
        if len(t) >= 5 and t not in CONCENTRATION_TOKENS and not t.endswith("ml")
    }
    matched = {t for t in expected_tokens if t in ocr_norm}
    if not matched or (len(expected_tokens) >= 3 and len(matched) < 2):
        return {}

    def amount(raw: str) -> float | None:
        return money_value("R$ " + raw)

    coupon_price = None
    m_coupon = re.search(r"(?i)R(?:\$|S)\s*([0-9]{1,4}(?:[.,][0-9]{2})?)m?\s*com\s+cupom", featured)
    if m_coupon:
        coupon_price = amount(m_coupon.group(1))
    prices: list[float] = []
    for m in re.finditer(r"(?i)R(?:\$|S)\s*([0-9]{1,4}(?:[.,][0-9]{2})?)m?", featured):
        if re.search(r"\d{1,2}x\s*$", featured[max(0, m.start() - 8):m.start()], re.I):
            continue
        value = amount(m.group(1))
        if value is not None and value >= 20:
            prices.append(value)
    previous_price = None
    public_price = next((v for v in prices if coupon_price is None or abs(v - coupon_price) > 0.02), None)
    if coupon_price is None and len(prices) >= 2:
        first, second = prices[0], prices[1]
        # ML card order: crossed-out/list price, then live price. Installment
        # values were already excluded above.
        if second < first and second >= first * 0.30:
            previous_price, public_price = first, second
    final_price = coupon_price if coupon_price is not None else public_price
    if final_price is None:
        return {}
    return {
        "matched_tokens": sorted(matched),
        "public_price": public_price,
        "previous_price": previous_price,
        "coupon_price": coupon_price,
        "final_price": final_price,
        "source": "attached_screenshot_ocr",
    }


def fetch_initial_offer_evidence(link: str | None, expected_product: str = "") -> tuple[dict, str, dict]:
    """Overlap independent reads; preserve both sources and validation rules."""
    from concurrent.futures import ThreadPoolExecutor
    timings = {}
    def measured(name, fn):
        started = time.monotonic()
        try:
            return fn(link)
        finally:
            timings[name] = round(time.monotonic() - started, 4)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        browser_fn = (
            (lambda current_link: browser_social_card_probe(current_link, expected_product))
            if expected_product else browser_social_card_probe
        )
        browser = pool.submit(measured, "browser_seconds", browser_fn)
        jina = pool.submit(measured, "jina_seconds", jina_fetch)
        card, text = browser.result(), jina.result()
    timings["initial_evidence_wall_seconds"] = round(time.monotonic() - started, 4)
    if isinstance(card, dict) and card.get("_probe_attempts"):
        timings["browser_probe_attempts"] = card["_probe_attempts"]
    return card, text, timings


def completed_batch_indices(futures: dict):
    """Yield ready promos without holding them behind a slower earlier input."""
    from concurrent.futures import as_completed
    indices = {future: index for index, future in futures.items()}
    for future in as_completed(indices):
        yield indices[future]


def headline_identity_product(canonical: str, received: str) -> str:
    """Prefer clean sender SKU over noisy card title only after strict agreement."""
    if (received
            and identity_compatible(canonical, received, DISTINCTIVE_VARIANT_TOKENS, strict=True)
            and not product_variant_conflict(canonical, received)
            and not identity_token_conflict(canonical, received)
            and not fakhar_variant_conflict(canonical, received)):
        return received
    return canonical


def normalize_copy(body: str, incoming_media: str = "") -> tuple[str, dict]:
    product = extract_product(body)
    received_product = product
    raw_headline = strip_wa(body).splitlines()[0].strip() if body.strip() else ""
    de, por, parcel = extract_prices(body)
    raw_parcel = parcel
    screenshot_card = screenshot_offer_facts(incoming_media, product)
    if screenshot_card:
        por = _fmt_brl(screenshot_card.get("final_price"))
        if screenshot_card.get("previous_price") is not None:
            de = _fmt_brl(screenshot_card.get("previous_price"))
        elif screenshot_card.get("coupon_price") is not None and screenshot_card.get("public_price") is not None:
            de = _fmt_brl(screenshot_card.get("public_price"))
    raw_de, raw_por = de, por
    cupons = extract_cupons(body)
    link = extract_link(body)
    browser_card, initial_jina, stage_timings = fetch_initial_offer_evidence(link, received_product)
    card_title = str(browser_card.get("title") or "")
    if card_title and received_product != "Produto" and (
        (
            _tokens_overlap(received_product, card_title) < 0.45
            and identity_token_conflict(received_product, card_title)
        )
        or (
            product_variant_conflict(received_product, card_title)
            and not same_product_except_volume(received_product, card_title)
        )
    ):
        # Never let a recommendation/hidden first card rewrite the promoted item.
        # A volume-only mismatch is allowed: the exact linked card outranks the
        # source caption and must keep its price/store evidence.
        browser_card = {}
        card_title = ""
    if not screenshot_card and card_title:
        screenshot_card = screenshot_offer_facts(incoming_media, card_title)
        if screenshot_card:
            por = _fmt_brl(screenshot_card.get("final_price"))
            if screenshot_card.get("coupon_price") is not None and screenshot_card.get("public_price") is not None:
                de = _fmt_brl(screenshot_card.get("public_price"))
            raw_de, raw_por = de, por
    if card_title:
        # The bounded card rendered by real Chrome is the promoted offer. Do not
        # let page-wide Jina/catalog parsing replace it with a recommendation
        # further down the social page (Invictus 100ml R$431 was rewritten as an
        # unrelated 50ml/R$789 card despite the retained screenshot being right).
        card_clean = sanitize_product_text(
            re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", card_title)
        )
        product = canonicalize_known_product_name(card_clean) or card_clean
    if (card_title and received_product != "Produto"
            and product_variant_conflict(received_product, card_title)
            and not same_product_except_volume(received_product, card_title)):
        raise RuntimeError(
            f"Produto da mensagem ({received_product}) conflita com o card visual do link "
            f"({card_title}); nao normalizei."
        )
    if (card_title
            and re.fullmatch(r"(?i)(?:kit\s+)?3\s+body\s+splash(?:es)?", received_product.strip())
            and all(token in card_title.casefold() for token in ("barbarius", "enigma", "midtown"))):
        # Generic "Kit 3 Body Splash" has no variant identity and previously
        # borrowed a Barbour's female trio. The exact card names all variants.
        product = "Kit Body Splash Barbarius + Enigma + Midtown 200ml Primacial"
        received_product = product
    jina = initial_jina
    jina_product = extract_product_from_jina(jina)
    if not card_title and jina_product and (
        product == "Produto"
        or not looks_like_product_line(product)
        or (not re.search(r"(?i)\b\d{2,3}\s*ml\b", product) and re.search(r"(?i)\b\d{2,3}\s*ml\b", jina_product))
        # If the ML product title disagrees with a variant token from the raw
        # text, prefer the page title. This avoids Sabah vs Sabah Al Ward, Gold
        # vs Black, Sugar vs original, etc. Photo matching depends on this.
        or product_variant_conflict(product, jina_product)
    ):
        product = jina_product
    product = canonicalize_known_product_name(product + " " + body) or canonicalize_known_product_name(product) or product
    if not browser_card and not availability_ok(jina):
        raise RuntimeError("Oferta parece indisponivel/esgotada no ML/Jina; nao normalizei.")
    # 2026-07-07: social/affiliate pages are the weakest price source. When the
    # page is social AND exposes a direct product URL ("Ir para produto"), fetch
    # the real item page and prefer its prices. Falls back silently to the old
    # social-card extraction when the second fetch fails.
    price_page = jina
    price_source_hint = ""
    resolved_item_url = link
    is_social_page = bool(re.search(r"(?i)(Perfil Social|/social/|affiliate-profile|Her[oó]i das Promo)", jina or ""))
    if is_social_page:
        direct_url = str(browser_card.get("product_url") or "") or extract_ml_product_url(jina, None)
        if direct_url and direct_url != link:
            direct_jina = jina_fetch(direct_url)
            if direct_jina and not re.search(r"(?i)(Perfil Social|/social/|affiliate-profile)", direct_jina):
                direct_prices = jina_offer_prices(direct_jina)
                if direct_prices[0] or direct_prices[1]:
                    price_page = direct_jina
                    resolved_item_url = direct_url
                    price_source_hint = "ML item direto (resolvido do social)"
                    if not availability_ok(direct_jina):
                        raise RuntimeError("Oferta parece indisponivel/esgotada no ML/Jina; nao normalizei.")
                    # Item page product title beats social-card title too.
                    direct_product = extract_product_from_jina(direct_jina)
                    if not card_title and direct_product and looks_like_product_line(direct_product):
                        if product == "Produto" or not looks_like_product_line(product) or product_variant_conflict(product, direct_product):
                            product = direct_product
    jina_de, jina_por, jina_other = jina_offer_prices(price_page)
    # ML Catalog API: authoritative facts for the exact listing (canonical
    # name, live price, original_price, official_store_id). Optional layer —
    # empty dict on any failure, and the Jina/HTML flow continues untouched.
    # When Chrome found a bounded featured card, resolve its direct product URL
    # rather than asking the social page resolver to rediscover the same card
    # among dozens of recommendations.
    catalog_lookup_link = str(browser_card.get("product_url") or "") or link
    catalog = ml_catalog_lookup(catalog_lookup_link, source_de=raw_de, source_por=raw_por,
                                expected_product=card_title or received_product)
    catalog_exact_offer = catalog.get("price_source") in ("catalog_exact_item", "exact_item_card")
    catalog_name = str(catalog.get("name") or "")
    exact_card_title = str((catalog.get("exact_item_facts") or {}).get("title") or "")
    # For product identity, the title inside the card bounded by the selected
    # item_id outranks catalog taxonomy and page-wide Jina. ML catalog labeled
    # the Sabah item as "Sabah Al Ward", while its exact card (and the sent
    # product) said Sabah; recommendations also contained both variants.
    identity_title = exact_card_title or catalog_name
    identity_correction = ""
    if (identity_title and received_product != "Produto"
            and not product_variant_conflict(received_product, identity_title)):
        # An exact card/catalog title may specialize an otherwise valid but
        # generic source name. "Malbec 100ml" is not wrong text, but it is not
        # enough identity to choose an image when the exact card says
        # "Malbec Tradicional 100ml". Keeping the generic source here allowed
        # approved photos from Malbec Bleu/X to enter the candidate set.
        received_tokens = set(product_key(received_product).split("-"))
        identity_tokens = set(product_key(identity_title).split("-"))
        received_variants = received_tokens & DISTINCTIVE_VARIANT_TOKENS
        identity_variants = identity_tokens & DISTINCTIVE_VARIANT_TOKENS
        exact_specialization = bool(
            catalog.get("offer_identity_confidence") in ("exact", "exact_card")
            and identity_variants
            and not received_variants
        )
        if exact_specialization:
            identity_clean = sanitize_product_text(
                re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", identity_title)
            )
            product = canonicalize_known_product_name(identity_clean) or identity_clean
        else:
            product = received_product
        if exact_card_title:
            catalog_name = exact_card_title
    if (identity_title and received_product != "Produto"
            and catalog.get("offer_identity_confidence") in ("exact", "exact_card")
            and product_variant_conflict(received_product, identity_title)):
        if same_product_except_volume(received_product, identity_title):
            source_volume = re.search(r"(?i)\b(\d{2,3})\s*ml\b", received_product)
            listing_volume = re.search(r"(?i)\b(\d{2,3})\s*ml\b", identity_title)
            identity_clean = sanitize_product_text(
                re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", identity_title)
            )
            product = canonicalize_known_product_name(identity_clean) or identity_clean
            catalog_name = exact_card_title or identity_title
            source_volume_value = source_volume.group(1) if source_volume else "?"
            listing_volume_value = listing_volume.group(1) if listing_volume else "?"
            identity_correction = (
                f"volume da fonte corrigido pelo item exato: "
                f"{source_volume_value}ml -> {listing_volume_value}ml"
            )
        else:
            raise RuntimeError(
                f"Produto da mensagem ({received_product}) conflita com o produto do link "
                f"({identity_title}); nao normalizei."
            )
    # Image lookup must never depend solely on the catalog title: catalog names
    # carry seller junk ("... Arabe Veoquiero") that breaks strict token match
    # against curated bank folders. Keep the pre-catalog product as an image
    # lookup alternative. (2026-07-08 fiasco batch: Vulcan Sable had an
    # approved_final photo but the catalog title hid it.)
    pre_catalog_product = product
    # Kit guard: catalog often resolves a kit promo to the single-item product
    # page (Kit Egeo -> Egeo 90ml avulso). A kit/non-kit mismatch means the
    # WHOLE catalog record (name AND price AND store) belongs to another
    # product — discard it entirely. Caught live: kit promo R$135 nearly
    # published with the single-item price R$119.
    if catalog_name:
        raw_is_kit = bool(re.search(r"(?i)\bkit\b", body or ""))
        cat_is_kit = bool(re.search(r"(?i)\bkit\b", catalog_name))
        if raw_is_kit != cat_is_kit:
            catalog = {}
            catalog_name = ""
    if catalog_name:
        # Glue "100 ml" -> "100ml" so the volume survives sanitize (bare "ml"
        # is a seller-noise token) and matches image-bank slug conventions.
        cat_clean = sanitize_product_text(re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", re.sub(r"(?i)\bvolume\s+da\s+unidade\b(\s*\d{1,4}\s*m?l?)?", "", catalog_name)))
        if (cat_clean and looks_like_product_line(cat_clean)
                and not product_variant_conflict(cat_clean, product)
                and (received_product == "Produto" or not looks_like_product_line(received_product))):
            product = canonicalize_known_product_name(cat_clean) or cat_clean
    if catalog.get("product_status") and catalog["product_status"] not in ("active",):
        raise RuntimeError("Catálogo ML diz que o produto não está ativo; nao normalizei.")
    catalog_price = _fmt_brl(catalog.get("price") if isinstance(catalog.get("price"), (int, float)) else None)
    catalog_de = _fmt_brl(catalog.get("original_price") if isinstance(catalog.get("original_price"), (int, float)) else None)
    if catalog_price and catalog_exact_offer:
        # PRICE AUTHORITY: only the API record for the exact listing may replace
        # the received offer. A social featured card and catalog buy-box are
        # *validation context*, never an edit authority: both can describe a
        # different offer (Mawwal/Azzaro/Nautica/Alpine 2026-07-10). This keeps
        # the pipeline source-centric instead of accumulating exceptions.
        #
        # Exact listing price from the API beats page scraping — EXCEPT when
        # the source claims a lower Pix price that is a plausible Pix discount
        # off the API list price (70–99%). The API only exposes list price;
        # overriding would erase a real Pix deal (Musamam case: list 332,80,
        # Pix 266 = exactly 80%).
        cv = money_value(catalog_price)
        rv = money_value(raw_por)
        # Source price BELOW the API list price in the 70–99% band is usually a
        # legitimate campaign/Pix price the API does not expose. Keep it and
        # use the API for the "De" + sanity. Below 70% -> distrust the source.
        pix_price_plausible = (
            rv is not None and cv is not None and 0.70 * cv <= rv < 0.99 * cv
            # Fakhar Platin case (2026-07-09): a plausible band is NOT
            # evidence. Only keep the lower source price when the source text
            # explicitly ties it to Pix; otherwise the API price is the truth.
            and bool(re.search(rf"(?i){re.escape(re.sub(r'[^0-9.,]', '', str(raw_por or '')))}\s*[^\n]{{0,40}}\b(?:no\s+pix|via\s+pix)\b", strip_wa(body or "")))
        )
        # Coupon promos can be legitimately below API price; keep the lower
        # coupon price. Otherwise the API price is the truth.
        if not (cupons and rv is not None and cv is not None and rv < cv) and not pix_price_plausible:
            jina_por = catalog_price
            dv = money_value(catalog_de) if catalog_de else None
            if dv is not None and cv is not None and dv > cv:
                jina_de = catalog_de
            _psrc = str(catalog.get("price_source") or "")
            if _psrc == "catalog_exact_item":
                price_source_hint = "API ML (anúncio exato do link)"
            elif _psrc == "social_featured_card":
                price_source_hint = "card destacado da página social (validado na faixa do catálogo)"
            else:
                price_source_hint = "buy-box do catálogo ML (NÃO é o anúncio exato do link)"
        elif pix_price_plausible:
            # Keep the source Pix price; validate/publish the "De" from the
            # API original price when it exists.
            dv = money_value(catalog_de) if catalog_de else None
            if dv is not None and rv is not None and dv > rv:
                jina_de = catalog_de
            jina_por = raw_por
            price_source_hint = "Pix da fonte + tabela validada na API ML"
    elif catalog.get("min_price") and raw_por:
        # No exact listing matched, but the catalog price floor still catches
        # wrong-variant prices (ex: decant 5ml price on a 100ml promo).
        rv = money_value(raw_por)
        if rv is not None and rv < float(catalog["min_price"]) * 0.55:
            raise RuntimeError(
                f"Preço da promo ({raw_por}) está muito abaixo do piso de catálogo "
                f"(R$ {catalog['min_price']}); provável variante errada. Nao normalizei."
            )
    # ML API title is the cleanest product-name source: no SEO suffixes, no
    # seller noise, no social-card contamination. Prefer it whenever the item
    # id is resolvable and the API title doesn't conflict-with/lose a variant.
    card_confirms_received = bool(
        card_title and received_product != "Produto"
        and _tokens_overlap(received_product, card_title) >= 0.45
        and not product_variant_conflict(received_product, card_title)
    )
    api_title = ml_api_item_title(resolved_item_url) or (ml_api_item_title(link) if resolved_item_url != link else "")
    if api_title:
        # Preserve catalog volume before sanitize_product_text removes standalone
        # "ml" as seller noise. Without this, "The Secret 100 Ml" became
        # "The Secret 100" and missed the approved 100ml image-bank key.
        api_clean = sanitize_product_text(
            re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", api_title)
        )
        api_identity_overlap = _tokens_overlap(product, api_clean) if api_clean else 0.0
        api_agrees_with_card = bool(
            card_title and api_clean
            and _tokens_overlap(card_title, api_clean) >= 0.45
            and not product_variant_conflict(card_title, api_clean)
        )
        if api_clean and (
            product == "Produto"
            or not looks_like_product_line(product)
            or (
                api_identity_overlap >= 0.45
                and not product_variant_conflict(product, api_clean)
                and (not card_confirms_received or api_agrees_with_card)
            )
            or (
                api_identity_overlap >= 0.45
                and not card_confirms_received
                and product_variant_conflict(product, api_clean)
            )
        ):
            product = canonicalize_known_product_name(api_clean) or api_clean
    # Final identity invariant: when the received product and the rendered exact
    # card agree, no page-wide Jina/catalog/API recommendation may leave a third
    # product in the output. Live failure: Khamrah Dukhan + matching card shipped
    # as Opera Rouge while a catalog heuristic separately pointed to Yara Tous.
    if card_confirms_received and card_title:
        card_clean = sanitize_product_text(
            re.sub(r"(?i)\b(\d{1,4})\s*ml\b", r"\1ml", card_title)
        )
        if card_clean:
            product = canonicalize_known_product_name(card_clean) or card_clean
    # A social page can prove that the link/product exists, but cannot rewrite
    # an incoming offer: featured cards and recommendations routinely describe
    # another seller, price, or campaign. Only a resolved non-social item page
    # is allowed to supply Jina price/Pix evidence.
    if is_social_page and price_page is jina:
        jina_de, jina_por, jina_other = None, None, None
        price_page = ""  # cannot borrow a Pix proof from a social/rec card
        price_source_hint = ""
    # The rendered featured card is the exact intent shown for this shortlink in
    # this browser session. It can refresh the public tier; a lower coupon price
    # remains plausible only and is never called checkout-confirmed.
    card_commercially_authoritative = browser_card_commercially_authoritative(browser_card)
    if card_commercially_authoritative:
        card_current = _fmt_brl(browser_card.get("current_price"))
        card_previous = _fmt_brl(browser_card.get("previous_price"))
        card_other = _fmt_brl(browser_card.get("other_methods_price"))
        cv, rv = money_value(card_current), money_value(raw_por)
        jina_por, jina_de, jina_other = card_current, card_previous, card_other
        if cv is not None and (rv is None or (not cupons and rv > cv * 1.01) or (cupons and rv >= cv)):
            por = card_current
        price_page = "card visual Chrome"
        price_source_hint = "card social exato renderizado no Chrome"
    if jina_por and raw_por and not browser_card:
        jv = money_value(jina_por)
        rv = money_value(raw_por)
        if jv is not None and rv is not None and (jv < rv * 0.60 or jv > rv * 1.80):
            jina_de, jina_por, jina_other = None, None, None
    price_checked = bool(jina_de or jina_por)
    price_source = (price_source_hint or "ML/Jina direto") if price_checked else "não validado"
    # "no Pix" is a claim that paying with Pix gives THIS discount. A loose
    # "pix" anywhere in the source ("Selecione Pix na tela...") is not that
    # (Coragio/Nautica false positives 2026-07-09). Require the Pix marker
    # adjacent to the actual published price value.
    def _pix_tied_to(value: str | None, text: str) -> bool:
        num = re.sub(r"[^0-9.,]", "", str(value or ""))
        if not num or not text:
            return False
        return bool(re.search(rf"{re.escape(num)}[^\n]{{0,40}}\b(?:no\s+pix|via\s+pix)\b", text, re.I))

    pix_evidence = _pix_tied_to(raw_por, strip_wa(body or ""))
    _exact_pix = catalog.get("pix_price")
    _exact_facts = catalog.get("exact_item_facts") or {}
    if (pix_evidence and catalog_exact_offer
            and _exact_facts.get("current_price") is not None
            and _exact_facts.get("pix_price") is None
            and _exact_facts.get("installments_no_interest")
            and _exact_facts.get("installments")):
        # The exact promoted card explicitly presents the current tier as
        # regular/interest-free installments, with no Pix label. Correct stale
        # source copy such as "R$79 no Pix" instead of preserving a modality
        # contradicted by the same item (Egeo Spicy Vibe: 3x, no Pix).
        pix_evidence = False
    if (not pix_evidence and isinstance(_exact_pix, (int, float))
            and money_value(raw_por) is not None
            and abs((money_value(raw_por) or 0) - float(_exact_pix)) < 1.5):
        # Typed Pix evidence from the block bounded by this exact item_id.
        pix_evidence = True
    elif (not pix_evidence and isinstance(_exact_pix, (int, float))
          and money_value(raw_por) is not None and cupons
          and catalog_exact_offer
          and catalog.get("official_store_id")
          and (money_value(raw_por) or 0) < float(_exact_pix)):
        # Same official-store item proves a Pix price, while the supplied coupon
        # explains a lower final value. This recovers Liquid Brun R$274 from its
        # public R$299.92 Pix tier without extending the rule to unrelated/
        # non-official catalog offers (the old Nautica contamination path).
        pix_evidence = True
    if card_commercially_authoritative and browser_card.get("pix"):
        _cv, _pv = browser_card.get("current_price"), money_value(por)
        if isinstance(_cv, (int, float)) and _pv is not None and (
                abs(_pv - float(_cv)) < 1.5 or (cupons and _pv < float(_cv))):
            pix_evidence = True
    if card_commercially_authoritative and browser_card.get("installments") and not browser_card.get("pix"):
        parcel = str(browser_card["installments"])
    if jina_por:
        # ML social/card price often excludes the coupon from Breno's promo. Use
        # it as validation, but do not overwrite a lower coupon price from the
        # source message.
        jina_por_value = money_value(jina_por)
        raw_por_value = money_value(raw_por)
        _cat_min = money_value(str(catalog.get("min_price"))) if catalog.get("min_price") is not None else None
        if (raw_por_value is not None and jina_por_value is not None
                and jina_por_value > raw_por_value * 1.02
                and _cat_min is not None and raw_por_value >= _cat_min * 0.98):
            # Never adjust UP over the source price when the catalog proves a
            # live offer at (or below) it (Nautica 2026-07-10: card/Pix showed
            # the R$138,95 offer's R$111,16 while R$98 was a real offer).
            # The card's De/Pix describe the WRONG offer — discard them too.
            jina_de, jina_other = None, None
            price_page = ""
        else:
            coupon_final_proved = bool(
                screenshot_card.get("coupon_price") is not None
                and screenshot_card.get("final_price") is not None
            )
            source_claims_lower_coupon = bool(
                cupons and raw_por and jina_por_value is not None
                and raw_por_value is not None and jina_por_value > raw_por_value
            )
            # Third-party copy may advertise a post-coupon price, but the public
            # card does not prove that personalized final. Publish the public
            # tier and keep the coupon line unless the attached screenshot itself
            # shows the coupon-applied value.
            if not (source_claims_lower_coupon and cupons):
                por = jina_por
    elif not por:
        por = jina_price(jina)
    if not pix_evidence and por and price_page:
        # Page fallback must prove a Pix-specific DISCOUNT, not just that Pix
        # exists as a payment method (every ML page mentions Pix; Coragio/
        # Nautica false positives 2026-07-09). Require the "ou R$ Y em outros
        # meios" structure with Y strictly above the Pix price.
        por_number = por.replace("R$ ", "").replace("R$", "").strip()
        escaped_por = re.escape(por_number)
        m_pix = re.search(
            rf"R\$\s*{escaped_por}\s*[^\n]{{0,40}}\bno\s+Pix\b.{{0,120}}?ou\s+R\$\s*([0-9][0-9.,]*)\s+em(?:\s+outros\s+meios|\s+\d{1,2}x)",
            price_page, re.I | re.S)
        if m_pix:
            other_v = money_value(m_pix.group(1))
            por_v = money_value(por)
            pix_evidence = bool(other_v is not None and por_v is not None and other_v > por_v)
    # If validation replaced the published price, the source's Pix claim no
    # longer describes the published value — re-verify against it or drop.
    if pix_evidence and money_value(por) != money_value(raw_por):
        pix_evidence = _pix_tied_to(por, strip_wa(body or "")) or _pix_tied_to(por, price_page or "")
    if not pix_evidence and not browser_card and jina_por and jina_other:
        # Structural Pix inheritance is fallback-only. An exact rendered card
        # explicitly lacking Pix outranks Jina's ambiguous price tiers (Sillage
        # 2026-07-19: coupon + credit-line prices were misread as Pix).
        # the OFFER is Pix-discounted ("R$ 127,92 no Pix ou R$ 159,90") — a
        # coupon applied on top yields a price BELOW the public Pix price, so
        # the published value is still a Pix price even though the literal
        # number never appears next to "no Pix" anywhere.
        _jp, _jo, _pv = money_value(jina_por), money_value(jina_other), money_value(por)
        if _jp is not None and _jo is not None and _pv is not None and _jo > _jp and _pv <= _jp * 1.01:
            pix_evidence = True
    lightweight_price = lightweight_final_price_check(raw_por, jina_por, jina_other, cupons, pix_evidence)
    if jina_de:
        de = jina_de
    de, por = sane_price_pair(de, por)
    # A crossed "De" captured from a dynamic social card can be technically
    # visible yet commercially absurd (Liquid Brun: R$950 over a R$306,81 public
    # tier, while another session showed R$429,90). Do not manufacture a 3x
    # anchor: when the alleged previous price exceeds 2x the same rendered
    # card's current price, omit "De" and keep the real final/coupon price.
    _card_current_v = (
        money_value(_fmt_brl(browser_card.get("current_price")))
        if card_commercially_authoritative else None
    )
    _de_v = money_value(de)
    if (_de_v is not None and _card_current_v is not None
            and _card_current_v > 0 and _de_v > _card_current_v * 2):
        de = None
    # A crossed-out/source "De" is not proof. Publish it only when the exact
    # rendered card, exact item/catalog, or direct item page exposes an original
    # price. Hawas Verde arrived with De R$499 while its exact card showed only
    # R$294,99; carrying R$499 forward manufactured a discount anchor.
    _original_price_proved = bool(
        jina_de
        or browser_card.get("previous_price")
        or (catalog_exact_offer and isinstance(catalog.get("original_price"), (int, float)))
        or (screenshot_card.get("coupon_price") is not None and screenshot_card.get("public_price") is not None)
    )
    if de and not _original_price_proved:
        de = None

    # Offer-identity join: keep every layer, but merge commercial fields only
    # when they resolve to the same listing. Catalog/product-only evidence is
    # retained in the audit trail and can still validate title/range/status.
    _source_offer = {
        "source": "received_message",
        "item_id": catalog.get("item_id") if catalog.get("offer_identity_confidence") in ("exact", "exact_card") else None,
        "product_id": catalog.get("product_id"),
        "price": money_value(raw_por),
        "original_price": money_value(raw_de),
        "pix": _pix_tied_to(raw_por, strip_wa(body or "")),
    }
    _offer_evidences: list[dict] = []
    if catalog:
        _offer_evidences.append({
            "source": "ml_exact_offer" if catalog_exact_offer else "ml_catalog_context",
            "item_id": catalog.get("item_id"),
            "product_id": catalog.get("product_id"),
            "exact_link": catalog_exact_offer,
            "allow_price_override": (
                catalog_exact_offer
                and money_value(por) is not None
                and isinstance(catalog.get("price"), (int, float))
                and abs((money_value(por) or 0) - float(catalog["price"])) < 1.5
            ),
            "price": catalog.get("price"),
            "original_price": catalog.get("original_price"),
            "pix": (
                True if isinstance(catalog.get("pix_price"), (int, float))
                and money_value(por) is not None
                and abs((money_value(por) or 0) - float(catalog["pix_price"])) < 1.5
                else None
            ),
            "installments": (
                catalog.get("installments")
                if catalog_exact_offer
                and isinstance((catalog.get("exact_item_facts") or {}).get("current_price"), (int, float))
                and money_value(por) is not None
                and abs((money_value(por) or 0) - float(catalog["exact_item_facts"]["current_price"])) < 1.5
                else None
            ),
            "seller_id": catalog.get("seller_id") if catalog_exact_offer else None,
            "official_store_id": catalog.get("official_store_id") if catalog_exact_offer else None,
            "store_name": catalog.get("store_name") if catalog_exact_offer else None,
        })
    if price_page and price_page is not jina and catalog.get("item_id"):
        _offer_evidences.append({
            "source": "exact_item_html",
            "item_id": catalog.get("item_id"),
            "product_id": catalog.get("product_id"),
            "exact_link": True,
            "price": money_value(jina_por),
            "original_price": money_value(jina_de),
            "pix": pix_evidence,
        })
    offer_resolution = resolve_offer_evidence(_source_offer, _offer_evidences)
    shadow_offer = build_shadow_offer(
        body, received_product, link, raw_de, raw_por,
        pix_evidence, cupons, parcel, catalog,
    )
    engine_mode = config_value("PERFUME_OFFER_ENGINE_MODE", "shadow").strip().lower()
    if engine_mode in ("identity_store", "commercial"):
        shadow_identity = shadow_offer.get("identity") or {}
        if shadow_identity.get("status") == "conflict":
            raise RuntimeError(
                "Motor novo detectou conflito entre o produto recebido e a oferta do link; nao normalizei."
            )
    if engine_mode == "commercial" and (shadow_offer.get("identity") or {}).get("status") in ("exact_item", "exact_card"):
        # Full authority requires an exact linked offer. While identity remains
        # ambiguous, retain the legacy commercial decision as a temporary
        # fallback rather than losing validated Pix/De coverage.
        shadow_price = shadow_offer.get("price")
        shadow_original = shadow_offer.get("original_price")
        por = _fmt_brl(float(shadow_price)) if isinstance(shadow_price, (int, float)) else None
        de = _fmt_brl(float(shadow_original)) if isinstance(shadow_original, (int, float)) else None
        pix_evidence = shadow_offer.get("price_kind") == "pix"
        parcel = shadow_offer.get("installments") or None
    # Final rendered-card authority: later catalog/shadow reconciliation must not
    # restore a stale source price above the offer visibly available right now.
    if (card_commercially_authoritative
            and isinstance(browser_card.get("current_price"), (int, float))):
        _card_now = float(browser_card["current_price"])
        _published_now = money_value(por)
        _raw_now = money_value(raw_por)
        if (_published_now is None or _published_now > _card_now * 1.01
                or (not cupons and abs((_published_now or 0) - _card_now) > 0.02)):
            por = _fmt_brl(_card_now)
        _card_previous = browser_card.get("previous_price")
        _por_final = money_value(por)
        if isinstance(_card_previous, (int, float)) and float(_card_previous) > _card_now * 1.01:
            de = _fmt_brl(float(_card_previous))
        elif (cupons and _por_final is not None and _por_final < _card_now
              and _card_now >= _por_final * 1.10
              and (_card_now - _por_final) >= 10.0):
            # Milestone case 2026-07-19: no crossed-out anchor on the public
            # card, but the card's own current price is the real pre-coupon
            # price and the published coupon price undercuts it meaningfully
            # (>=10% and >=R$10). The card price IS visible evidence — use it
            # as De. Small percentage or cash gaps stay De-less.
            de = _fmt_brl(_card_now)
            # The rendered card price is visible public evidence for this De.
            _original_price_proved = True
        else:
            # The exact rendered card has no crossed-out anchor and no coupon
            # undercut worth anchoring; never restore a stale source/API "De".
            de = None
        # Anti-noise: a meaningful De needs both relative and absolute distance.
        # H Marine rendered as De 78 -> 71: the hidden 78.99 passed 10%, but a
        # visible R$7 gap is still noise. Require >=10% AND >=R$10.
        _de_v = money_value(de)
        if (_de_v is not None and _por_final is not None
                and (_de_v < _por_final * 1.10 or (_de_v - _por_final) < 10.0)):
            de = None
        if browser_card.get("pix") and money_value(por) is not None and (
                abs((money_value(por) or 0) - _card_now) < 1.5
                or (cupons and (money_value(por) or 0) < _card_now)):
            pix_evidence = True
            parcel = None
        elif (browser_card.get("installments")
              and (not cupons
                   or abs((money_value(por) or 0) - _card_now) < 1.5
                   or (cupons and money_value(por) is not None
                       and (money_value(por) or 0) < _card_now))):
            # Coupon exception (Egeo Bomb 2026-07-19): a lower coupon price with
            # the rendered card showing explicit installments must keep them —
            # same rule already applied in the publication gate below. The
            # Essencial Atrai guard remains: a card with NO modality never
            # inherits installments from catalog/shadow.
            pix_evidence = False
            parcel = str(browser_card["installments"])
        else:
            # Catalog installments describe the public card tier, not a lower
            # post-coupon final. Essencial Atrai inherited "5x" from shadow data
            # while the rendered card had no modality and R$131 was a coupon
            # price. Keep only installments explicitly supplied by Breno.
            parcel = raw_parcel
    # A matching attached screenshot is the visible condition Breno is asking
    # us to publish. It outranks a different server-side ML session.
    if screenshot_card:
        por = _fmt_brl(screenshot_card.get("final_price"))
        if screenshot_card.get("previous_price") is not None:
            de = _fmt_brl(screenshot_card.get("previous_price"))
        elif screenshot_card.get("coupon_price") is not None and screenshot_card.get("public_price") is not None:
            de = _fmt_brl(screenshot_card.get("public_price"))
        else:
            de = None
        pix_evidence = False
        parcel = None
        price_checked = True
        price_source = "print anexado (OCR local; produto conferido)"
    _confirmed_price = any(
        "price_confirmed" in (entry.get("fields") or [])
        for entry in offer_resolution.get("evidence_accepted") or []
    )
    if _confirmed_price:
        price_checked = True
        price_source = "API ML (mesmo item_id; preço confirmado)"
        lightweight_price = {
            "status": "confirmed_exact_light",
            "label": "camada leve: preço confirmado no mesmo item_id da oferta; não é checkout",
        }
    # Preserve original gender even when an exact catalog title omits it.
    # Explicit disagreement was rejected by the offer identity guards above.
    product = preserve_gender(product, received_product)
    product = preserve_source_variant_identity(product, received_product)
    product = preserve_source_house(product, received_product)
    display_product, title_authority = editorial_title_authority(
        link,
        product,
        exact_title=identity_title,
        exact_offer=catalog_exact_offer,
    )
    display_product = preserve_gender(display_product, product)
    display_product = preserve_source_house(display_product, received_product)
    # The rendered Chrome card is the store authority: title, seller and badge
    # come from the same visible .poly-card and the screenshot is retained for audit.
    # Source prose is not store proof; session/API seller data without a visible
    # badge in this exact card is also insufficient.
    rendered_store_trust = rendered_card_store_trust(browser_card)
    if rendered_store_trust:
        trust = rendered_store_trust
    elif is_social_page:
        trust = None
    elif engine_mode in ("identity_store", "commercial"):
        # The staged engine is already the sole store authority. Do not execute
        # legacy extract_trust first and then overwrite it: that path refetched
        # the short link/item/Jina and added tens of seconds with zero effect.
        shadow_store = shadow_offer.get("official_store")
        trust = format_official_store_trust(str(shadow_store)) if shadow_store else None
    elif offer_resolution.get("official_store_id"):
        # Precedence (Cheiromarcante case 2026-07-09): the API/brand-confirmed
        # store_name is the truth. Source-text/extract_trust names come from
        # seller copy and can name a DIFFERENT store than the actual listing
        # (source said "Cheiromarcante", real store = WATHIQ PARFUMS).
        if offer_resolution.get("store_name"):
            trust = format_official_store_trust(str(offer_resolution["store_name"]))
        elif (named := extract_trust(body, product, jina)):
            trust = named
        else:
            m_store = re.search(r"(?i)loja\s+oficial\s+([A-Za-zÀ-ÿ0-9 &_.-]{2,45}?)(?:\s+no\s+ML|\n|_|$)", strip_wa(body or ""))
            trust = format_official_store_trust(m_store.group(1)) if m_store else None
    else:
        # A source/page saying "Loja Oficial" is only a claim. Without an
        # official_store_id joined to this exact offer it must not be printed
        # (batch replay found The Most Wanted labelled "Selvagem Essence").
        trust = None
    _price_discount_pct = 0.0
    if isinstance(lightweight_price, dict) and lightweight_price.get("status") == "probable_final_light":
        _price_discount_pct = float(lightweight_price.get("inferred_discount_pct") or 0)
    elif _original_price_proved and money_value(de) and money_value(por):
        _dv, _pv = money_value(de) or 0, money_value(por) or 0
        if _dv > _pv:
            _price_discount_pct = (1 - (_pv / _dv)) * 100
    _headline_pv = money_value(por) or 0
    _full_size = bool(re.search(r"(?i)\b(?:8\d|9\d|1\d\d|200)\s*ml\b", product or ""))
    # Numeric price headlines are allowed when the commercial fact itself is a
    # strong hook: validated full-size offer with >=45% discount. The former 35%
    # gate was too permissive: the 2026-08-29 Edição replaced 39%-off numeric
    # headlines with product/profile hooks. Price remains eligible for a proven
    # historic low, while ordinary discounts stay in the price line.
    # the isolated tests at R$139/R$142/R$177/R$229 (2026-07-25): "price under
    # 100 only" was too restrictive and threw away a legitimate buying trigger.
    # The LLM may still prefer inspiration/profile/occasion; this merely makes
    # the factual price angle eligible.
    _price_threshold = (int(_headline_pv // 10) + 1) * 10 if _headline_pv > 0 else 0
    _exceptional_price = bool(
        price_checked and _full_size and _headline_pv > 0
        and _price_discount_pct >= 45
    )
    # "Preço como história" (Breno, Davidoff Zino): when the SOURCE itself
    # claims a historic-low / all-time-low price, the commercial fact is strong
    # even below the generic 35% bar. This is evidence-based, not a style choice.
    _historic_low = bool(re.search(
        r"(?i)\bmenor\s+valor\s+hist[óo]rico|menor\s+pre[çc]o\s+(?:hist[óo]rico|j[áa]\s+visto)|"
        r"pre[çc]o\s+m[aí]s\s+baixo\s+j[áa]|all[-\s]?time\s+low\b",
        body or "",
    ))
    price_context = {
        "attractive": _exceptional_price or (price_checked and _historic_low),
        "discount_pct": round(_price_discount_pct, 1),
        "final_price": round(_headline_pv, 2),
        "threshold": _price_threshold,
    }
    _headline_started = time.monotonic()
    # Marketplace cards may prepend campaign/seller noise (for example
    # "PRIDE OF LATTAFA") that breaks exact-SKU repertoire lookup. The sender's
    # identity is preferred for editorial retrieval only when strict identity,
    # variant, volume and gender guards agree; display title remains card-owned.
    headline_product = headline_identity_product(product, received_product)
    headline = enforce_headline_output(make_headline(headline_product, raw_headline, body, price_context))
    # Last publication boundary catches historical reuse and any fallback path.
    # The Sentir masculine case reused a human-edited feminine angle verbatim.
    headline = gender_safe_headline(headline, product)
    headline_diag = headline_diagnostics()
    stage_timings["headline_seconds"] = round(time.monotonic() - _headline_started, 4)
    reserve_headline(headline)

    lines: list[str] = [headline]
    if trust:
        lines += [trust, ""]
    else:
        lines.append("")
    lines.append(f"*{display_product}*")
    lines.append("")
    # Final publication gate: source copy alone cannot prove a crossed-out De.
    # Later reconciliation must not restore it after the evidence check above.
    if not _original_price_proved:
        de = None
    display_de = meaningful_de_price(de, por, proved=_original_price_proved)
    editorial_de = money_norm(display_de)
    editorial_por = money_norm(por)
    published_price_line = ""
    if editorial_por:
        if editorial_de:
            price_line = f"De {editorial_de} por *{editorial_por}*"
        else:
            price_line = f"Por *{editorial_por}*"
        if pix_evidence:
            price_line += " no Pix"
        elif parcel:
            parcel_text = str(parcel).strip()
            parcel_prefix = "em " if re.fullmatch(r"\d{1,2}x", parcel_text, re.I) else ""
            price_line += f" {parcel_prefix}{parcel_text}"
        elif offer_resolution.get("installments") and (
            abs((money_value(por) or 0) - float(offer_resolution.get("price") or 0)) < 1.5
            or (
                cupons
                and money_value(por) is not None
                and offer_resolution.get("price") is not None
                and (money_value(por) or 0) < float(offer_resolution.get("price") or 0)
            )
        ):
            # Exact-card installments may accompany a lower coupon campaign
            # price. Coupons rotate/expire quickly and Breno performs the final
            # price check before publishing; preserving the card's installment
            # count is more useful than suppressing it merely because the coupon
            # changes the total. Never inherit from a different item.
            price_line += f" em {offer_resolution['installments']}"
        price_line += " 🦸🏻‍♂️"
        published_price_line = price_line
        lines.append(price_line)
    if cupons:
        if len(cupons) == 1:
            lines.append(f"Cupom: *{cupons[0]}* 🎟️")
        else:
            lines.append("Cupom: " + " ou ".join(f"*{c}*" for c in cupons) + " 🎟️")
    if link:
        lines += ["", link]
    kit_identity = kit_image_identity(body, display_product)
    return "\n".join(lines).strip(), {
        "stage_timings": stage_timings,
        "product": product,
        "source_product": received_product,
        "link": link,
        "price": por,
        "image_identity": product,
        "trust": trust or "",
        "trust_kind": (
            "official_store" if str(trust or "").startswith("_Loja Oficial")
            else "none"
        ),
        "headline": headline,
        "headline_identity": headline_product,
        "raw_headline": raw_headline,
        "raw_de": raw_de,
        "raw_por": raw_por,
        "display_product": display_product,
        "final_de": de,
        "final_por": por,
        "published_de": editorial_de,
        "published_price_line": published_price_line,
        "price_checked": price_checked,
        "pix_evidence": pix_evidence,
        "price_source": price_source,
        "browser_card_commercially_authoritative": card_commercially_authoritative,
        "lightweight_price": lightweight_price,
        "price_changed": (raw_de != de) or (raw_por != por),
        "coupon_count": len(cupons),
        "headline_source": headline_diag.get("source", ""),
        "headline_reason": headline_diag.get("reason", ""),
        "headline_attempts": headline_diag.get("attempts", []),
        "headline_model": headline_diag.get("model", ""),
        "headline_options": headline_diag.get("options", []),
        "headline_judge_reason": headline_diag.get("judge_reason", ""),
        "catalog_hit": bool(catalog),
        "catalog_item_matched": bool(catalog.get("price") is not None),
        "catalog_official_store": bool(catalog.get("official_store_id")),
        "catalog_name": catalog.get("name") or "",
        "identity_correction": identity_correction,
        "title_authority": title_authority,
        "offer_item_id": offer_resolution.get("item_id") or "",
        "offer_evidence_accepted": offer_resolution.get("evidence_accepted") or [],
        "offer_evidence_rejected": offer_resolution.get("evidence_rejected") or [],
        "price_modalities": catalog.get("price_modalities") or [],
        "shadow_offer": shadow_offer,
        "shadow_mode": True,
        "browser_card": browser_card,
        "screenshot_card": screenshot_card,
        # Image identity gets every exact-title layer. The editorial product may
        # be shortened ("Perfume SALVO EDP") or reordered by ML
        # ("Hawas For Him Malibu Rasasi"), while the bank keeps the canonical
        # brand/name/volume slug. find_image remains strict about variants.
        "image_products": ([kit_identity] if kit_identity else [p for p in dict.fromkeys([
            # Prefer the sender's specific identity before a marketplace title
            # that may collapse a flanker to a generic family (Natura Essencial /
            # Natura Homem). The send loop still rejects any source identity that
            # conflicts with the canonical exact-link product.
            # The approved editorial title may restore a trusted volume omitted
            # by both the terse source line and ML's card title (Qaed Al Fursan:
            # source/card had no volume, approved title had 90ml). It is still
            # checked by the same strict flanker/gender/volume guards downstream.
            display_product, received_product, product, pre_catalog_product, identity_title,
            catalog_name, api_title, jina_product,
        ]) if p]),
    }


@lru_cache(maxsize=2048)
def _same_image_base_cached(path_a: str, stat_a: tuple, path_b: str, stat_b: tuple) -> bool:
    """Cached visual comparison keyed by path plus file stat fingerprint."""
    try:
        if stat_a[0] == stat_b[0]:
            import hashlib
            ha = hashlib.sha256(Path(path_a).read_bytes()).hexdigest()
            hb = hashlib.sha256(Path(path_b).read_bytes()).hexdigest()
            if ha == hb:
                return True
    except Exception:
        pass
    try:
        from PIL import Image, ImageChops, ImageStat

        im_a = Image.open(path_a).convert("RGB").resize((256, 256))
        im_b = Image.open(path_b).convert("RGB").resize((256, 256))
        diff = ImageChops.difference(im_a, im_b)
        mean_diff = sum(ImageStat.Stat(diff).mean) / 3
        return mean_diff < 35
    except Exception:
        return False


def looks_like_same_image_base(path_a: str, path_b: str) -> bool:
    """Best-effort visual duplicate check for reporting honesty.

    The normalizer may replace a WhatsApp card/screenshot with a curated 500x500
    approved_final crop derived from the same underlying product photo. That is
    a valid asset swap, but the report must not imply Breno got a visually new
    photo. Keep this as a soft/reporting check only; selection still depends on
    approved_final.
    """
    if not path_a or not path_b:
        return False
    # Identical path is the strongest possible "same image" signal. Returning
    # False here made the bank's own copy of the incoming photo look like a
    # different composition, so find_image could hand back the very photo Breno
    # sent — the one swap he explicitly forbids (2026-07-26).
    if Path(path_a) == Path(path_b):
        return True
    try:
        sa = Path(path_a).stat()
        sb = Path(path_b).stat()
        return _same_image_base_cached(
            str(Path(path_a)), (sa.st_size, sa.st_mtime_ns),
            str(Path(path_b)), (sb.st_size, sb.st_mtime_ns),
        )
    except Exception:
        return False


def classify_image_decision(image_path: str = "", incoming_media: str = "") -> dict:
    """Describe image identity, approval tier and replacement separately.

    A same-composition higher-resolution asset can be useful for delivery, but
    it is not a visually new approved replacement. Keeping these dimensions
    separate prevents the report from turning every image outcome into one
    misleading boolean.
    """
    result = {
        "present": bool(image_path),
        "approved_asset": False,
        "same_composition": False,
        "visually_different": False,
        "replacement_approved": False,
        "delivery_allowed": bool(image_path),
        "source_tier": "missing",
        "reason": "foto ausente",
    }
    if not image_path:
        return result
    pstr = str(image_path)
    if "/approved_final/" in pstr or "/prepared_send/" in pstr:
        result["approved_asset"] = True
        result["source_tier"] = "approved_final"
    elif any(f"/{tier}/" in pstr for tier in ("high_quality", "acceptable")):
        result["source_tier"] = "non_final_curated"
    else:
        result["source_tier"] = "external_or_incoming"
    if incoming_media:
        result["same_composition"] = bool(
            Path(image_path) == Path(incoming_media)
            or looks_like_same_image_base(image_path, incoming_media)
        )
        result["visually_different"] = not result["same_composition"]
    else:
        result["visually_different"] = True
    result["replacement_approved"] = bool(result["approved_asset"] and result["visually_different"])
    if result["replacement_approved"]:
        result["reason"] = "imagem approved_final visualmente diferente"
    elif result["approved_asset"] and result["same_composition"]:
        result["reason"] = "foto recebida reutilizada; sem troca visual por outra imagem aprovada do banco"
    elif result["source_tier"] == "non_final_curated":
        result["reason"] = "imagem curada sem status approved_final"
    else:
        result["reason"] = "imagem externa/fallback sem approved_final"
    return result


def normalizer_report(meta: dict, image_path: str = "", incoming_media: str = "") -> str:
    """Gera relatório de aprovação enxuto para o chat + log técnico interno."""
    # --- dados internos ---
    raw_price = " / ".join(x for x in [meta.get("raw_de"), meta.get("raw_por")] if x) or "não veio claro"
    published_price = meta.get("published_price_line") or ""
    if published_price:
        final_price = strip_wa(published_price).replace("  🦸🏻‍♂️", "").strip()
    else:
        final_price = " / ".join(x for x in [meta.get("final_de"), meta.get("final_por")] if x) or "não definido"

    image_decision = classify_image_decision(image_path, incoming_media)
    image_checked = bool(image_decision["replacement_approved"])
    image_flag = "" if image_checked else str(image_decision["reason"])

    price_checked = bool(meta.get("price_checked"))
    coupons = meta.get("coupon_count") or 0
    headline_source = meta.get("headline_source") or "desconhecida"
    headline_fallback = (str(headline_source).startswith("fallback")
                         or headline_source in {"editorial_pending", "safe_fallback", "editorial_context_fallback"})
    headline_missing = not strip_wa(str(meta.get("headline") or "")).strip()
    trust = meta.get("trust") or ""
    card = meta.get("browser_card") or {}
    light = meta.get("lightweight_price") or {}

    # --- log técnico (vai apenas para stderr/log, não para o chat) ---
    log_lines = [
        f"headline_source={headline_source}",
        f"headline_reason={meta.get('headline_reason') or ''}",
        f"price_in={raw_price} price_out={final_price}",
        f"price_source={meta.get('price_source') or 'ML/Jina'} checked={price_checked}",
        f"trust={trust}",
        f"image_path={image_path} image_checked={image_checked}",
        f"image_tier={image_decision['source_tier']} same_composition={image_decision['same_composition']} delivery_allowed={image_decision['delivery_allowed']}",
        f"coupons={coupons}",
    ]
    if meta.get("identity_correction"):
        log_lines.append(f"identity_correction={meta['identity_correction']}")
    if card:
        card_prices = " / ".join(x for x in [
            _fmt_brl(card.get("previous_price")),
            _fmt_brl(card.get("current_price")),
            _fmt_brl(card.get("other_methods_price")),
        ] if x)
        modality = "Pix" if card.get("pix") else str(card.get("installments") or "sem modalidade")
        badge = "selo azul" if card.get("official_store") else "sem selo"
        log_lines.append(f"card={card.get('title') or ''} prices={card_prices} {modality} {badge}")
    if isinstance(light, dict) and light.get("label"):
        log_lines.append(f"lightweight={light['label']}")
    print("[normalizer_report] " + " | ".join(log_lines), file=sys.stderr)

    # --- pontuação de aprovação ---
    light_status = str(light.get("status") or "") if isinstance(light, dict) else ""
    price_warning = light_status in {"high_coupon_discount_light", "implausible_final_light", "conflict_light"}
    checks = {
        "headline": not headline_fallback and not headline_missing,
        "preço": price_checked,
        "preço final": not price_warning,
        "imagem": image_checked,
    }
    score = sum(checks.values())
    total = len(checks)
    # Snapshot the actual checks while transient evidence is still available.
    meta["approval_checks"] = dict(checks)
    meta["approval_failed_checks"] = [name for name, passed in checks.items() if not passed]
    meta["image_decision"] = dict(image_decision)
    meta["approval_lightweight_status"] = light_status
    commercial_ok = bool(checks["preço"] and checks["preço final"])
    meta["approval_dimensions"] = {
        "comercial": "aprovado" if commercial_ok else "revisar",
        "editorial": "aprovado" if checks["headline"] else "fallback/pendente",
        "imagem": "aprovada" if checks["imagem"] else "pendente",
    }
    if score == total:
        grade = "✅ APROVADO"
    elif commercial_ok:
        grade = "⚠️ APROVADO c/ ressalva"
    else:
        grade = "❌ REVISAR"

    # alertas visíveis apenas quando há problema
    alerts = []
    if headline_missing:
        alerts.append("headline ausente")
    if headline_source == "safe_fallback":
        alerts.append("headline fallback seguro")
    elif headline_source == "editorial_pending":
        alerts.append("headline pendente de revisão; genérica bloqueada")
    elif headline_fallback:
        alerts.append("headline fallback")
    if not price_checked:
        card = meta.get("browser_card") or {}
        if card and not meta.get("browser_card_commercially_authoritative", True):
            alerts.append("preço da fonte preservado; card ML bloqueado/incompleto")
        else:
            alerts.append("preço não validado")
    if price_warning:
        discount = light.get("inferred_discount_pct") if isinstance(light, dict) else None
        base_price = money_norm(str(light.get("base_price") or "")) if isinstance(light, dict) else None
        if isinstance(discount, (int, float)) and base_price:
            alerts.append(f"preço final exige {discount:.0f}% OFF sobre {base_price}; confirme no checkout")
        elif light_status == "conflict_light":
            alerts.append("preço final não fecha com o cupom e o valor público; confirme no checkout")
        else:
            alerts.append("preço final fora da faixa esperada; confirme no checkout")

    if image_flag:
        alerts.append(image_flag.strip())
    meta["approval_grade"] = grade
    meta["approval_alerts"] = list(alerts)
    # Cupom é opcional: ausência não reduz aprovação. Quando existe, continua
    # registrado no log técnico e validado pelo fluxo editorial.

    if score == total:
        correction = str(meta.get("identity_correction") or "").strip()
        suffix = f"\nℹ️ {correction}" if correction else ""
        return "\n\n---\n✅ APROVADO" + suffix

    # com ressalva ou revisar: mostra contexto + alertas
    headline_txt = meta.get("headline") or ""
    product_txt = meta.get("display_product") or meta.get("product") or ""
    loja_txt = trust
    price_txt = ""
    if meta.get("final_por"):
        price_txt = money_norm(str(meta["final_por"])) or ""
        if meta.get("final_de"):
            _de_txt = money_norm(str(meta["final_de"])) or ""
            price_txt = f"{_de_txt} → {price_txt}"
        if card and card.get("pix"):
            price_txt += " Pix"
    context_parts = [x for x in [product_txt, loja_txt, price_txt] if x]
    context_line = " · ".join(context_parts)

    lines = [f"*{grade}*"]
    dimensions = meta.get("approval_dimensions") or {}
    if dimensions:
        commercial_mark = "✅" if dimensions.get("comercial") == "aprovado" else "❌"
        editorial_mark = "✅" if dimensions.get("editorial") == "aprovado" else "⚠️"
        image_mark = "✅" if dimensions.get("imagem") == "aprovada" else "⚠️"
        lines.append(
            f"{commercial_mark} comercial · {editorial_mark} headline · {image_mark} imagem"
        )
    if headline_txt:
        lines.append(headline_txt)
    if context_line:
        lines.append(context_line)
    if alerts:
        lines.append("⚡ " + " · ".join(alerts))

    return "\n\n---\n" + "\n".join(lines)


def send_text(chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(text)
        return True
    r = subprocess.run([GUARDED_SENDER, "--chat", chat_id, "--text", text], text=True, capture_output=True, timeout=30)
    if r.returncode != 0:
        print((r.stdout or r.stderr).strip(), file=sys.stderr)
        return False
    print((r.stdout or "").strip())
    return True


from perfume_identity_memo import scoped_identity_memo

@scoped_identity_memo
def product_key(product: str) -> str:
    source = strip_wa(product or "").strip()
    identity_source = source
    # Editorial prefixes frequently ride on the product line:
    # "DEFINIÇÃO DE RAIZ, Azzaro Pour Homme 50ml". A suffix with a real
    # volume and product-shaped text is safer than carrying the prose into ID.
    comma_prefix = source.rsplit(",", 1)[0].strip() if "," in source else ""
    comma_suffix = source.rsplit(",", 1)[-1].strip() if "," in source else ""
    editorial_prefix = bool(re.search(
        r"(?i)\b(defini[cç][aã]o|pre[cç]o|promo(?:[cç][aã]o)?|imperd[ií]vel|corre+|barat[oa]|"
        r"raiz|respeito|pra\s+quem|para\s+quem|cheiro\s+de|menor\s+pre[cç]o|vale\s+muito)\b",
        comma_prefix,
    ))
    if (comma_suffix and editorial_prefix
            and re.search(r"(?i)\b\d{2,3}\s*m(?:l)?\b", comma_suffix)
            and looks_like_product_line(comma_suffix)):
        identity_source = comma_suffix
    # Marketplace SEO often repeats the brand around a dash:
    # "... Azzaro - Azzaro Pour Homme 50 Ml". Use the product-shaped suffix
    # only when it shares a meaningful token with the prefix.
    dash_parts = re.split(r"\s+[-–—]\s+", identity_source)
    if len(dash_parts) > 1:
        suffix = dash_parts[-1].strip()
        prefix_tokens = set(re.findall(r"(?i)[a-zà-ÿ0-9]+", " ".join(dash_parts[:-1]).casefold()))
        suffix_tokens = set(re.findall(r"(?i)[a-zà-ÿ0-9]+", suffix.casefold()))
        shared = {t for t in prefix_tokens & suffix_tokens if len(t) >= 4}
        if shared and re.search(r"(?i)\b\d{2,3}\s*m(?:l)?\b", suffix) and looks_like_product_line(suffix):
            identity_source = suffix
    # Glue volume before sanitization; standalone "ml" is intentionally a
    # noise token elsewhere and used to disappear from API titles.
    identity_source = re.sub(r"(?i)\b(\d{2,3})\s*m(?:l)?\b", r"\1ml", identity_source)
    s = sanitize_product_text(strip_non_identity_attributes(identity_source)).lower()
    # Product extraction occasionally carries marketplace/price suffixes from a
    # malformed source line. They are offer metadata, never product identity.
    s = re.sub(r"(?i)\s+no\s+(shopee|mercado\s*livre|meli|amazon)\b.*$", "", s)
    s = re.sub(r"(?i)\s+em\s+\d+x\s+de\s+r\$?\s*[\d.,]+.*$", "", s)
    trans = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
    s = s.translate(trans)
    s = re.sub(r"\b(\d{2,3})\s*m\b", r"\1ml", s)
    s = re.sub(r"\b(\d{2,3})\s+ml\b", r"\1ml", s)
    key = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    raw_parts = [p for p in key.split("-") if p]
    parts = [p for p in raw_parts if p.endswith("ml") or p not in PRODUCT_NOISE_TOKENS]
    # Collapse adjacent duplicate marketplace tokens ("azzaro-azzaro").
    deduped: list[str] = []
    for part in parts:
        if not deduped or deduped[-1] != part:
            deduped.append(part)
    return "-".join(deduped)


def gender_and_brand_order_lookup_keys(key: str) -> list[str]:
    """Safe lookup variants for common imported naming drift.

    Handles cases like:
    - Club de Nuit Intense Man 105ml -> Club de Nuit Intense Men 105ml
    - Armaf Club Nuit Intense 105ml -> Club Nuit Intense Armaf 105ml

    This is intentionally narrow: it preserves variant/flanker tokens such as
    iconic/sillage/milestone/urban-elixir and only reorders the known Armaf
    Club Nuit family, so we do not cross-pollinate sibling perfumes.
    """
    parts = [p for p in key.split("-") if p]
    if not parts:
        return []
    out: list[str] = []

    def add(candidate_parts: list[str]) -> None:
        cand = "-".join(p for p in candidate_parts if p)
        if cand and cand != key:
            out.append(cand)

    # man/men is a constant typo/translation drift in source titles.
    for src, dst in [("man", "men"), ("men", "man")]:
        if src in parts:
            add([dst if p == src else p for p in parts])

    # Specific Armaf Club Nuit family: bank folders alternate brand position and
    # include/remove "de" via product_key noise. Keep all other tokens intact.
    if "armaf" in parts and "club" in parts and "nuit" in parts:
        vol = next((p for p in parts if re.fullmatch(r"\d{2,3}ml", p)), "")
        core = [p for p in parts if p not in {"armaf"} and not re.fullmatch(r"\d{2,3}ml", p)]
        if core:
            add(core + ["armaf"] + ([vol] if vol else []))
            add(["armaf"] + core + (["edt", vol] if vol else ["edt"]))
            add(["armaf"] + core + (["edp", vol] if vol else ["edp"]))
            # If source says Man, try Men before/after brand too.
            if "man" in core:
                men_core = ["men" if p == "man" else p for p in core]
                add(men_core + ([vol] if vol else []))
                add(men_core + ["armaf"] + ([vol] if vol else []))
                add(["armaf"] + men_core + ([vol] if vol else []))
    # Brand position drift, generalized. Source titles put the brand anywhere:
    # "Perfume Liz Flora O Boticário 100ml" -> liz-flora-o-boticario-100ml,
    # while the bank folder is boticario-liz-flora-100ml. Same SKU, different
    # word order, zero matches -> we published the source photo with a hand and
    # a competitor watermark while a curated image sat in the bank (2026-07-26).
    # Reordering is safe because the token SET is preserved: no flanker is
    # added or dropped, only the brand moves.
    _brand_groups = (
        ("o", "boticario"), ("o", "boticrio"), ("boticario",), ("boticrio",),
        ("natura",), ("eudora",), ("jequiti",), ("avon",),
        ("lattafa",), ("afnan",), ("rayhaan",), ("maison", "alhambra"),
        ("al", "wataniah"), ("paris", "elysees"), ("hinode",),
    )
    _vol = next((p for p in parts if re.fullmatch(r"\d{2,3}ml", p)), "")
    _conc = [p for p in parts if p in {"edp", "edt"}]
    for _brand in _brand_groups:
        if not all(b in parts for b in _brand):
            continue
        _rest = [
            p for p in parts
            if p not in _brand
            and not re.fullmatch(r"\d{2,3}ml", p)
            and p not in {"edp", "edt"}
        ]
        if not _rest:
            continue
        _tail = _conc + ([_vol] if _vol else [])
        add(list(_brand) + _rest + _tail)
        add(_rest + list(_brand) + _tail)
        if len(_brand) > 1:
            # "o boticario" folders often drop the leading article.
            _short = [b for b in _brand if b != "o"]
            if _short:
                add(_short + _rest + _tail)
                add(_rest + _short + _tail)
    seen: set[str] = set()
    return [k for k in out if k and not (k in seen or seen.add(k))]


def image_lookup_keys(product: str) -> list[str]:
    """Return strict image-bank lookup keys for a product name.

    Runtime is allowed to auto-use only curated approved_final assets. Candidate,
    raw_items and source_examples are collection material, not final publishing
    assets.
    """
    key = product_key(product)
    canonical_key = product_key(canonicalize_known_product_name(product))
    keys = [canonical_key, key] if canonical_key and canonical_key != key else [key]
    # Narrow marketplace-title repairs. These words are not globally safe to
    # discard ("kit" and "clássico" can be real SKU identity), but the exact
    # families below are already curated under shorter canonical folders.
    key_tokens = set(key.split("-"))
    if {"cebolinha", "turma", "monica", "jequiti"} <= key_tokens:
        cebolinha = [
            token for token in key.split("-")
            if token not in {"perfumes", "miniatura"}
        ]
        keys.extend([
            "-".join(cebolinha),
            "colonia-turma-da-monica-cebolinha-jequiti-25ml",
            "jequiti-cebolinha-25ml",
        ])
    if {"kit", "presente", "malbec", "tradicional"} <= key_tokens:
        # ML currently calls the single Malbec Tradicional listing a "kit
        # presente ... clássico original" without naming any second component.
        # Do not route it to kit-malbec-4-itens: cardinality remains protected.
        keys.extend([
            "o-boticario-malbec-tradicional-100ml",
            "malbec-tradicional-100ml",
        ])
    try:
        alias_payload = json.loads(IMAGE_BANK_ALIASES.read_text(encoding="utf-8")) if IMAGE_BANK_ALIASES.exists() else {}
        for lookup_alias_key in dict.fromkeys([key, canonical_key]):
            alias_values = alias_payload.get(lookup_alias_key) if isinstance(alias_payload, dict) and lookup_alias_key else None
            if isinstance(alias_values, str):
                keys.append(alias_values)
            elif isinstance(alias_values, list):
                keys.extend(str(x) for x in alias_values if x)
        # Backward-compatible alias lookup: old alias keys may include seller,
        # category, or gender tokens that product_key now strips globally. Compare
        # canonicalized alias keys too, so one cleanup improves old mappings.
        if isinstance(alias_payload, dict):
            for alias_key, alias_values in alias_payload.items():
                if alias_key == key:
                    continue
                if product_key(str(alias_key)) != key:
                    continue
                if isinstance(alias_values, str):
                    keys.append(alias_values)
                elif isinstance(alias_values, list):
                    keys.extend(str(x) for x in alias_values if x)
    except Exception:
        pass
    if key.startswith("perfume-"):
        no_perfume_key = key.replace("perfume-", "", 1)
        keys.append(no_perfume_key)
    base_family_keys = list(keys)
    for base_key in base_family_keys:
        keys.extend(family_expanded_lookup_keys(base_key))
    # "Árabe" is product identity for many clones in the bank (arabe-salvo-100ml)
    # but some source/Jina titles include/remove it inconsistently. Search both.
    if key.startswith("arabe-"):
        keys.append(key.replace("arabe-", "", 1))
    elif "-arabe-" not in key and not key.startswith("arabe-"):
        keys.append("arabe-" + key)
    # Gender/marketing filler tokens are NOT identity: "Pacific Aura Para
    # Homens 100ml" must find bank folder rayhaan-pacific-aura-100ml
    # (2026-07-09 miss). Add de-filled variants of every key collected so far.
    _filler = re.compile(r"-(para-homens|para-mulheres|para-homem|para-mulher|masculino|masculina|masc|feminino|feminina|fem|unissex|homens|mulheres)(?=-|$)")
    for k in list(keys):
        stripped = re.sub(r"-{2,}", "-", _filler.sub("", k)).strip("-")
        if stripped and stripped != k:
            keys.append(stripped)
        # Marketplace titles sometimes repeat a variant around the product name
        # ("Lattafa Elixir Asad Elixir Masc"). Repeated identical tokens and
        # abbreviated gender are not SKU identity; removing them lets the strict
        # token-set matcher see the existing Asad Elixir folder without dropping
        # any unique flanker token.
        seen_tokens: set[str] = set()
        deduped = "-".join(
            token for token in stripped.split("-")
            if token and not (token in seen_tokens or seen_tokens.add(token))
        )
        if deduped and deduped != stripped:
            keys.append(deduped)
    if key.startswith("o-boticario-"):
        keys.append(key.replace("o-boticario-", "", 1))
        # Historical bank typo: some folders were indexed as "o-boticrio-*".
        keys.append(key.replace("o-boticario-", "o-boticrio-", 1))
    # Common indexed variants: "asad-100ml" and "lattafa-asad-edp-100ml" can
    # coexist with the literal extracted product "lattafa-asad-100ml".
    parts = [p for p in key.split("-") if p]
    if len(parts) >= 3 and parts[-1].endswith("ml"):
        # Dropping the head token is only safe when it is a BRAND prefix.
        # "malbec-tradicional-100ml" -> "tradicional-100ml" made Malbec and
        # Kaiak share a lookup key and cross-contaminated the approved display
        # name (2026-07-26 incident). Never emit a key whose head is a generic
        # variant/concentration word.
        head_rest = parts[1]
        if (head_rest not in DISTINCTIVE_FAMILY_VARIANT_TOKENS
                and head_rest not in DISTINCTIVE_VARIANT_TOKENS
                and not re.fullmatch(r"\d{2,3}ml", head_rest)):
            keys.append("-".join(parts[1:]))
        keys.append("-".join(parts[:-1] + ["edp", parts[-1]]))
        keys.append("-".join(parts[:-1] + ["edt", parts[-1]]))
    # Some extraction paths emit volume as a bare number when concentration comes
    # after it, e.g. "Banderas The Icon Elixir 100 EDP" ->
    # "banderas-the-icon-elixir-100". The bank stores these as 100ml and often
    # as edp-100ml/edt-100ml. Treat a terminal 2-3 digit token as volume for
    # lookup only; do not change product_key itself to avoid wider side effects.
    if len(parts) >= 3 and re.fullmatch(r"\d{2,3}", parts[-1] or ""):
        ml_part = parts[-1] + "ml"
        base_parts = parts[:-1] + [ml_part]
        keys.append("-".join(base_parts))
        keys.append("-".join(parts[:-1] + ["edp", ml_part]))
        keys.append("-".join(parts[:-1] + ["edt", ml_part]))
        keys.append("-".join(base_parts[1:]))
    keys.extend(gender_and_brand_order_lookup_keys(key))
    # Brand abbreviation <-> full name. The bank stores the SAME SKU under both
    # spellings depending on the source title: ysl-y-edt-100ml (1 approved photo,
    # same-base as the promo) vs yves-saint-laurent-y-100ml (3 approved photos,
    # all visually different). Without this expansion the sibling folder is
    # invisible and the report says "no photo" while the asset exists.
    # Breno confirmed 2026-08-02: the miss was search, not missing material.
    keys.extend(brand_alias_lookup_keys(list(keys)))
    # Volume spelling drift in FOLDER names: historical folders were created
    # from source titles as "-100", "-100-ml" or "-100ml" for the same SKU
    # (vibe-tropical-rayhaan-100 held the only approved photo while the -100ml
    # sibling was empty, 2026-08-02). Emit every spelling for lookup only;
    # product_key itself stays canonical.
    for base in list(keys):
        match = re.search(r"-(\d{2,3})ml$", base)
        if not match:
            continue
        stem = base[: match.start()]
        number = match.group(1)
        keys.append(f"{stem}-{number}")
        keys.append(f"{stem}-{number}-ml")
    seen: set[str] = set()
    return [k for k in keys if k and not (k in seen or seen.add(k))]


def _key_has_prefix(parts: list[str], prefix: str) -> bool:
    prefix_parts = [p for p in prefix.split("-") if p]
    if not prefix_parts or len(parts) < len(prefix_parts):
        return False
    return parts[:len(prefix_parts)] == prefix_parts


# Brand abbreviation <-> full-name spellings that denote the SAME house. Only
# unambiguous pairs belong here: swapping one for the other must never change
# which SKU is meant. Concentration, flanker and volume are untouched, so this
# widens WHERE we look, never WHAT counts as a match (the variant guard in
# image_bank_key_matches still decides that).
BRAND_ALIAS_FORMS: tuple[tuple[str, ...], ...] = (
    ("ysl", "yves-saint-laurent"),
    ("ch", "carolina-herrera"),
    ("pdm", "parfums-de-marly"),
    ("jpg", "jean-paul-gaultier"),
    ("ck", "calvin-klein"),
    ("ysl-y", "yves-saint-laurent-y"),
    ("dg", "dolce-gabbana"),
    ("mfk", "maison-francis-kurkdjian"),
    ("tf", "tom-ford"),
    ("il", "issey-miyake"),
    ("aba", "antonio-banderas"),
    ("banderas", "antonio-banderas"),
    ("boticario", "o-boticario"),
    ("armani", "giorgio-armani"),
)


def brand_alias_lookup_keys(keys: list[str]) -> list[str]:
    """Expand brand abbreviation <-> full name on the HEAD of each key.

    Only the leading brand token is swapped. Rewriting mid-key tokens would let
    "y" or "blue" collide across houses; the head is the one position where the
    token is reliably the brand.
    """
    expanded: list[str] = []
    for key in keys:
        if not key:
            continue
        for forms in BRAND_ALIAS_FORMS:
            for form in forms:
                if key != form and not key.startswith(f"{form}-"):
                    continue
                rest = key[len(form):]
                for other in forms:
                    if other == form:
                        continue
                    candidate = f"{other}{rest}"
                    if candidate and candidate != key:
                        expanded.append(candidate)
    seen: set[str] = set()
    return [k for k in expanded if not (k in seen or seen.add(k))]


def family_expanded_lookup_keys(key: str) -> list[str]:
    """Canonical family expansions for recurring national lines.

    Fixes short source titles like "Malbec 100ml" -> O Boticário Malbec without
    opening fuzzy matching for dangerous flankers. Variant words present in the
    source are preserved; default variants are used only when the source has no
    distinctive variant token.
    """
    parts = [p for p in key.split("-") if p]
    if not parts:
        return []
    # Kit identity is cardinality, not decoration. Never let a kit/combo source
    # expand to the bare family/default SKU (Malbec 4 itens -> single bottle).
    if "kit" in parts or "combo" in parts or "conjunto" in parts or "itens" in parts:
        return []
    volume = next((p for p in parts if re.fullmatch(r"\d{2,3}ml", p)), "")
    source_variants = [p for p in parts if p in DISTINCTIVE_FAMILY_VARIANT_TOKENS]
    # Any leftover token that is not family/brand/volume/noise is a flanker we
    # simply have not catalogued yet (2026-07-26: "Malbec Vert" was not in
    # DISTINCTIVE_FAMILY_VARIANT_TOKENS, so it silently expanded to the default
    # "tradicional" SKU). Unknown tokens must SUPPRESS defaults, never be
    # ignored — an uncatalogued flanker is exactly the dangerous case.
    _all_family_tokens = {t for fam in CANONICAL_FAMILY_EXPANSIONS for t in fam.split("-")}
    _all_prefix_tokens = {
        t for _, (prefixes, _d) in CANONICAL_FAMILY_EXPANSIONS.items()
        for prefix in prefixes for t in prefix.split("-")
    }
    unknown_tokens = [
        p for p in parts
        if p not in _all_family_tokens
        and p not in _all_prefix_tokens
        and p not in PRODUCT_NOISE_TOKENS
        and not re.fullmatch(r"\d{2,3}ml", p)
        and p not in source_variants
    ]
    out: list[str] = []
    for family, (prefixes, default_variants) in CANONICAL_FAMILY_EXPANSIONS.items():
        family_parts = family.split("-")
        if not all(fp in parts for fp in family_parts):
            continue
        current_has_prefix = any(_key_has_prefix(parts, prefix) for prefix in prefixes)
        variant_sets: list[list[str]] = []
        if source_variants:
            variant_sets.append(list(source_variants))
        elif unknown_tokens:
            # Uncatalogued flanker present: expand brand only, never defaults.
            variant_sets.append(list(unknown_tokens))
        else:
            # Bare family products like "Malbec 100ml" usually mean the default
            # traditional SKU. Also include the bare family key before default.
            variant_sets.extend([[], list(default_variants or [])])
        for prefix in prefixes:
            prefix_parts = prefix.split("-")
            base = [] if current_has_prefix else prefix_parts
            base += family_parts
            for variants in variant_sets:
                cand_parts = base + variants
                if volume:
                    cand_parts += [volume]
                cand = "-".join(cand_parts)
                if cand and cand != key:
                    out.append(cand)
        # Unprefixed default variant sibling, e.g. malbec-tradicional-100ml.
        if not source_variants and not unknown_tokens:
            for variants in [list(default_variants or [])]:
                if variants:
                    cand_parts = family_parts + variants + ([volume] if volume else [])
                    cand = "-".join(cand_parts)
                    if cand and cand != key:
                        out.append(cand)
    seen: set[str] = set()
    return [k for k in out if k and not (k in seen or seen.add(k))]


CONCENTRATION_TOKENS = {"edp", "edt", "eau", "de", "deo", "colonia", "parfum", "perfume", "arabe", "masculino", "feminino", "original", "loja", "oficial", "lipx"}


def product_concentration_key(value: str) -> str:
    """Preserve EDT/EDP/Parfum boundaries outside product_key noise stripping."""
    s = strip_wa(value or "").casefold().replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    if re.search(r"\beau\s+de\s+parfum\b", s) or re.search(r"\bedp\b", s):
        return "edp"
    if re.search(r"\beau\s+de\s+toilette\b", s) or re.search(r"\bedt\b", s):
        return "edt"
    # Some folders were slugged from titles that lost the "eau de" prefix
    # (armani-beauty-stronger-with-you-toilette-100ml). Bare "toilette" is still
    # an explicit concentration: without this it read as UNQUALIFIED and a
    # Parfum product could borrow the Toilette bottle (2026-08-02).
    if re.search(r"\btoilette\b", s):
        return "edt"
    if re.search(r"\bparfum\b", s):
        return "parfum"
    return ""


def product_gender_key(value: str) -> str:
    """Read original identity, including translated gender and explicit unisex."""
    return identity_gender(value)
BRAND_ALIAS_TOKENS = {
    "montblanc": {"mont", "blanc"},
    "mont": {"montblanc"},
    "blanc": {"montblanc"},
    "oboticario": {"boticario", "boticrio", "o"},
    "boticario": {"oboticario", "boticrio", "o"},
    "boticrio": {"oboticario", "boticario", "o"},
    "jp": {"jean", "paul", "gaultier"},
    "jean": {"jp", "gaultier"},
    "rabanne": {"rabane"},
    "rabane": {"rabanne"},
}
# Sub-house tokens that identify the SAME commercial brand house. Bank folders
# were created from source titles that alternate the house prefix for the very
# same SKU (giorgio-armani-stronger-with-you-100ml has 24 curated photos while
# emporio-armani-stronger-with-you-100ml sat empty, 2026-08-02). Treating them
# as interchangeable BRAND tokens is safe because volume, kit cardinality and
# flanker gates still run afterwards.
BRAND_HOUSE_EQUIVALENTS = {
    # NOTE: "beauty" is deliberately NOT an equivalent. It is a retail line
    # label that co-occurs with concentration variants (Armani Beauty ... Parfum
    # vs EDT), so accepting it as a brand stand-in let a plain EDT product match
    # the Parfum folder in validation.
    "armani": {"giorgio", "emporio"},
    "giorgio": {"armani", "emporio"},
    "emporio": {"armani", "giorgio"},
}
for _house_token, _siblings in BRAND_HOUSE_EQUIVALENTS.items():
    BRAND_ALIAS_TOKENS.setdefault(_house_token, set()).update(_siblings)


def brand_token_equivalents(token: str) -> set[str]:
    """All tokens that may stand in for `token` when comparing brand words."""
    return {token} | BRAND_ALIAS_TOKENS.get(token, set())


# Brand words that a bank folder is allowed to OMIT. Many curated folders were
# created from short source titles ("le-beau-le-125ml") while the live promo
# carries the full house name ("Jean Paul Gaultier Le Beau Le 125ml"). The
# subset test then failed and a curated approved_final photo stayed invisible
# (2026-08-02). Dropping a brand is only safe when the folder does not name a
# DIFFERENT house — that guard is enforced in image_bank_key_matches.
KNOWN_BRAND_TOKENS = {
    "o", "boticario", "boticrio", "oboticario", "natura", "eudora", "jequiti",
    "avon", "lattafa", "afnan", "rayhaan", "maison", "alhambra", "al",
    "wataniah", "paris", "elysees", "hinode", "armaf", "rasasi", "giorgio",
    "armani", "emporio", "carolina", "herrera", "ralph", "lauren", "mawwal",
    "barbour", "barbours", "jean", "paul", "gaultier", "jpg", "banderas",
    "antonio", "calvin", "klein", "versace", "azzaro", "dior", "chanel",
    "montblanc", "mont", "blanc", "rabanne", "rabane", "paco", "mercedes",
    "benz", "khadlaj", "french", "avenue", "nautica", "ysl", "hugo", "boss",
    "elizabeth", "arden", "shakira", "primacial",
}
# Distinctive flankers/variants that must not be silently borrowed from a bank
# folder when the live product name does not include them. This keeps tolerance
# for harmless concentration words (EDP/EDT) but blocks Asad -> Asad Bourbon.
DISTINCTIVE_VARIANT_TOKENS = {
    "bourbon", "elixir", "zanzibar", "ward", "black", "gold", "platinum", "platin",
    "bleu", "tradicional", "traditional", "x", "hair", "mist",
    "intense", "intenso", "absolu", "absolut", "sable", "blue", "verde", "rouge",
    "qahwa", "dukhan",
    # 2026-07-26: "intensely" is a distinct Armani flanker (Stronger With You
    # Intensely). Only "intense" was listed, so a plain "Stronger With You 50ml"
    # ad borrowed the Intensely bottle photo — visibly the wrong product.
    "intensely", "powerfully", "refil", "corporal",
    "white", "noir", "sport", "extreme", "ultra", "legend", "royal", "king", "queen",
    "sugar", "vanilla", "tobacco", "summer", "rebel",
    # Product families/kit variants that must never be inferred from brand alone.
    "invictus", "victory", "icon", "premium",
    # Versace/brand flankers: prevent generic Pour Homme from borrowing Eros Energy/Fraîche.
    "eros", "energy", "fraiche", "fraîche", "fresh",
    # 2026-07-07: Eros EDT was matching versace-eros-flame-100ml because "flame"
    # was missing here. Sibling flankers seen in the bank (Quasar Fire/Ice/Brave,
    # Eros Flame) must never be borrowed across variants.
    "flame", "fire", "ice", "brave", "rush", "aqua", "oceano", "evolutio",
    # Family names are variants too. Without these, token overlap on brand +
    # volume let Le Beau borrow Le Male Le Parfum and Acqua di Giò EDT borrow
    # Profondo (live validation 2026-07-11).
    "beau", "male", "profondo", "profumo", "paradise", "garden",
    # Maison Alhambra Philos has visually distinct flankers. Messenger (green)
    # must never borrow Pura (purple), even though brand/family/volume overlap.
    "messenger", "pura",
    # Essencial flankers must survive aliases, fuzzy lookup and editorial reuse.
    "oud", "mirra", "exclusivo", "atrai", "supreme", "unico",
}


def image_bank_key_matches(product_key_value: str, bank_key: str) -> bool:
    """Strict-but-tolerant approved_final lookup.

    Product extraction often drops concentration words (EDT/EDP), while curated
    image-bank folders keep them. Match only when every meaningful product token
    from the extracted key is present in the bank folder. This fixes generic
    concentration mismatches without falling back to raw/candidate images or
    broad fuzzy matching.
    """
    product_gender = product_gender_key(product_key_value)
    bank_gender = product_gender_key(bank_key)
    if product_gender and bank_gender and product_gender != bank_gender:
        return False
    product_tokens = [p for p in product_key_value.split("-") if p and p not in CONCENTRATION_TOKENS]
    bank_tokens = set(p for p in bank_key.split("-") if p and p not in CONCENTRATION_TOKENS)
    # Bank folders sometimes store volume as bare digits (rasasi-hawas-malibu-100).
    # Normalize both sides so 100 == 100ml.
    bank_tokens |= {t + "ml" for t in list(bank_tokens) if re.fullmatch(r"\d{2,3}", t)}
    # Need brand/name/ml at minimum. Avoid matching weak keys like "sport-100ml".
    if len(product_tokens) < 3:
        return False
    product_set = set(product_tokens)
    kit_markers = {"kit", "combo", "conjunto"}
    # Cardinality is identity. A single bottle may be a token subset of a kit
    # folder ("natura-essencial-tradicional-100ml" vs
    # "kit-natura-essencial-classico-tradicional-100ml"), but it must never
    # borrow the kit photo. Apply the same symmetric gate used by fuzzy lookup.
    if bool(product_set & kit_markers) != bool(bank_tokens & kit_markers):
        return False
    if product_set & kit_markers and bank_tokens & kit_markers:
        product_counts = {t for t in product_set if re.fullmatch(r"\d{1,2}", t)}
        bank_counts = {t for t in bank_tokens if re.fullmatch(r"\d{1,2}", t)}
        # Brand position/prefix may differ in historical folders, but kit type,
        # family, variant and item count must be identical.
        brand_fillers = {"o", "boticario", "boticrio"}
        product_kit_core = product_set - brand_fillers
        bank_kit_core = bank_tokens - brand_fillers
        if product_counts and product_counts == bank_counts and product_kit_core == bank_kit_core:
            return True
    # Brand-house drift: the same SKU is stored under Giorgio/Emporio/Armani
    # Beauty depending on which source title created the folder. A product token
    # is satisfied by any of its registered brand equivalents; every other token
    # still needs a literal match, so flankers cannot slip through.
    used_brand_substitution = False
    dropped_brand_tokens: set[str] = set()
    for token in product_set:
        if token in bank_tokens:
            continue
        if brand_token_equivalents(token) & bank_tokens:
            used_brand_substitution = True
            continue
        if token in KNOWN_BRAND_TOKENS:
            # The folder simply omits the house name. Record it; validated below.
            dropped_brand_tokens.add(token)
            continue
        return False
    if dropped_brand_tokens:
        # The remaining product identity must still be substantial, otherwise a
        # bare "armani-100ml" would match everything.
        core_tokens = {
            t for t in product_set
            if t not in KNOWN_BRAND_TOKENS and not t.endswith("ml")
        }
        if len(core_tokens) < 2:
            return False
        # And the folder must not belong to a DIFFERENT house. If it names any
        # brand that is neither the product's brand nor a registered synonym,
        # this is a same-name product from another maker: refuse.
        allowed_brands: set[str] = set()
        for token in product_set & KNOWN_BRAND_TOKENS:
            allowed_brands |= brand_token_equivalents(token)
        foreign_brands = (bank_tokens & KNOWN_BRAND_TOKENS) - allowed_brands
        if foreign_brands:
            return False
        used_brand_substitution = True
    if used_brand_substitution:
        # Brand substitution loosens identity, so it may not ALSO cross a
        # concentration line. "parfum"/"extrait" are separate SKUs (Stronger
        # With You Parfum is a different bottle from the EDT), and they are
        # stripped as generic concentration words above — re-check them on the
        # raw keys before accepting a substituted match.
        line_tokens = {"parfum", "extrait"}
        product_raw = {p for p in product_key_value.split("-") if p}
        bank_raw = {p for p in bank_key.split("-") if p}
        if (product_raw & line_tokens) != (bank_raw & line_tokens):
            return False
    extra_variants = (bank_tokens - product_set) & DISTINCTIVE_VARIANT_TOKENS
    if extra_variants:
        return False
    # Fail closed for generic family names. "Natura Essencial 100ml" and
    # "Natura Homem 100ml" do not identify Oud, Coragio, Nós, Tradicional, etc.
    # Exact/source-specific alternatives are checked separately by the caller.
    product_identity = {
        t for t in product_set
        if t not in KNOWN_BRAND_TOKENS and not t.endswith("ml")
    }
    bank_extra_identity = {
        t for t in bank_tokens - product_set
        if t not in KNOWN_BRAND_TOKENS and not t.endswith("ml")
    }
    if bank_extra_identity and len(product_identity) <= 1:
        return False
    return True


def image_bank_fuzzy_score(product_key_value: str, bank_key: str) -> int:
    """Conservative fallback score for approved_final folders.

    Used only after exact/alias lookup fails. It can recover cases where the
    source omits/splits a brand token (Montblanc vs Mont Blanc) but refuses
    ambiguous weak matches and variant/flanker drift.
    """
    product_gender = product_gender_key(product_key_value)
    bank_gender = product_gender_key(bank_key)
    if product_gender and bank_gender and product_gender != bank_gender:
        return 0
    product_tokens = [p for p in product_key_value.split("-") if p and p not in CONCENTRATION_TOKENS]
    bank_tokens = [p for p in bank_key.split("-") if p and p not in CONCENTRATION_TOKENS]
    # The 3-token floor exists to stop weak 2-token noise from matching anything,
    # but it also killed legitimate short keys: "LATTAFA KHAMRAH" -> lattafa-khamrah
    # (2 tokens) scored 0 against lattafa-khamrah-100ml, which holds 10 approved
    # photos (2026-08-02 audit: this pattern was ~40% of real misses). A 2-token
    # key is safe when BOTH tokens are distinctive -- i.e. neither is a bare
    # volume/number -- because the overlap and coverage gates below still apply.
    def _distinctive(tokens: list[str]) -> int:
        return len([t for t in tokens if not t.endswith("ml") and not re.fullmatch(r"\d{1,3}", t)])

    _floor_ok = (
        len(product_tokens) >= 3 and len(bank_tokens) >= 3
    ) or (
        _distinctive(product_tokens) >= 2 and _distinctive(bank_tokens) >= 2
    )
    if not _floor_ok:
        return 0
    product_set, bank_set = set(product_tokens), set(bank_tokens)
    kit_markers = {"kit", "combo", "conjunto"}
    if (product_set & kit_markers) or (bank_set & kit_markers):
        if not (product_set & kit_markers and bank_set & kit_markers):
            return 0
        product_counts = {t for t in product_set if re.fullmatch(r"\d{1,2}", t)}
        bank_counts = {t for t in bank_set if re.fullmatch(r"\d{1,2}", t)}
        if product_counts and product_counts != bank_counts:
            return 0
    product_core = {t for t in product_set if not t.endswith("ml")}
    bank_core = {t for t in bank_set if not t.endswith("ml")}
    if not product_core or not bank_core:
        return 0
    product_vol = {t for t in product_set if t.endswith("ml")}
    bank_vol = {t for t in bank_set if t.endswith("ml")}
    bank_vol |= {t + "ml" for t in bank_set if re.fullmatch(r"\d{2,3}", t)}
    if product_vol and bank_vol and product_vol.isdisjoint(bank_vol):
        return 0
    product_variants = product_set & DISTINCTIVE_VARIANT_TOKENS
    bank_variants = bank_set & DISTINCTIVE_VARIANT_TOKENS
    if product_variants != bank_variants:
        return 0

    def expanded(tokens: set[str]) -> set[str]:
        out = set(tokens)
        for token in list(tokens):
            out.update(BRAND_ALIAS_TOKENS.get(token, set()))
        return out

    product_exp = expanded(product_core)
    bank_exp = expanded(bank_core)
    overlap = product_exp & bank_exp
    if len(overlap) < 2:
        return 0
    # Sharing house + concentration is not product identity. If BOTH sides also
    # carry different model-name tokens, they are sibling SKUs and fuzzy lookup
    # must fail closed. Live failure: Billion Night and Max Gordon both had
    # Paris/Elysees/Intense/100ml, scoring 87 and swapping bottles.
    product_unique = {
        t for t in product_core - bank_exp
        if t not in KNOWN_BRAND_TOKENS
    }
    bank_unique = {
        t for t in bank_core - product_exp
        if t not in KNOWN_BRAND_TOKENS
    }
    if product_unique and bank_unique:
        return 0
    # A generic house/family query may not borrow an arbitrary named sibling.
    # Live failures: "Natura Essencial" selected Essencial Oud, and generic
    # "Natura Homem" selected Homem Coragio. When the query has at most one
    # non-brand identity token, any extra model/flanker token in the bank is
    # ambiguity, not enrichment. A more specific source alternative (e.g.
    # Tradicional) is evaluated separately and can still resolve exactly.
    product_identity = {t for t in product_core if t not in KNOWN_BRAND_TOKENS}
    if bank_unique and len(product_identity) <= 1:
        return 0
    short_core = min(len(product_core), len(bank_core))
    coverage = len(overlap) / max(1, short_core)
    if coverage < 0.67:
        return 0
    score = int(coverage * 100)
    score += 20 * len(product_vol & bank_vol)
    score -= 8 * abs(len(product_core) - len(bank_core))
    return max(score, 0)


def image_bank_metadata_keys(product_dir: Path) -> list[str]:
    """Canonical product keys stored inside a bank folder's curated metadata."""
    idx = product_dir / "index.json"
    try:
        stat = idx.stat()
    except OSError:
        return []
    cache_key = (str(product_dir), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    cached = _IMAGE_METADATA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return []
    values: list[str] = []
    for field in ("product_key", "canonical_guess", "product_guess"):
        value = data.get(field) if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            values.append(value)
    if isinstance(data, dict):
        for bucket in ("approved", "raw_items"):
            for item in data.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                value = item.get("product_guess") or item.get("canonical_guess")
                if isinstance(value, str) and value.strip():
                    values.append(value)
                # Legacy collector versions occasionally indexed the first bold
                # headline as product_guess (for example "PERFUME DE QUEM BATE
                # BOLA") while the same human-approved record kept the actual
                # SKU in its body.  The headline repertoire can still recognize
                # that SKU, but image lookup could not.  Recover the product from
                # the auditable source body instead of trusting the bad folder
                # name; explicit identity conflicts remain blocked downstream.
                body = item.get("body")
                if isinstance(body, str) and body.strip():
                    body_product = extract_product(body)
                    if body_product:
                        values.append(body_product)
    keys = [product_key(v) for v in values]
    seen_keys: set[str] = set()
    result = [k for k in keys if k and not (k in seen_keys or seen_keys.add(k))]
    _IMAGE_METADATA_CACHE[cache_key] = result
    return result


def audit_image_bank_miss(product: str, key: str, scored: list[tuple[int, Path, str]]) -> None:
    """Silent evidence for improving aliases; never creates chat noise."""
    try:
        audit = Path(os.path.expanduser("~/.hermes/perfume_image_bank/missing_lookup_audit.jsonl"))
        payload = {
            "ts": int(time.time()),
            "product": product,
            "key": key,
            "lookup_keys": image_lookup_keys(product),
            "top_candidates": [
                {"score": score, "folder": path.name, "matched_key": matched_key}
                for score, path, matched_key in scored[:3]
            ],
        }
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def approved_final_candidates(product: str) -> list[Path]:
    """Resolve approved image-bank folders without rescanning for every headline gate.

    A headline request asks for the same exact-SKU identity several times. The old
    path repeated the complete bank scan on each call, consuming much of the LLM
    budget before the provider was contacted. Cache only inside one headline
    request: bank changes remain immediately visible to every later request.
    """
    key = str(product or "").strip()
    active = bool(getattr(_HEADLINE_DIAGNOSTICS, "approved_final_cache_active", False))
    cache = getattr(_HEADLINE_DIAGNOSTICS, "approved_final_cache", {}) if active else {}
    if key in cache:
        return list(cache[key])
    resolved = tuple(_approved_final_candidates_impl(product))
    if active:
        cache[key] = resolved
        _HEADLINE_DIAGNOSTICS.approved_final_cache = cache
    return list(resolved)


def resolve_bank_write_key(product: str) -> str:
    """Canonical bank folder a NEW photo for `product` must be written into.

    Single source of truth for every writer (miners, vitrine snapshot, manual
    curation). Without it each script slugged the promo title on its own and
    happily created yet another folder for a SKU that already had one — that is
    how the bank grew to 226 fragmented SKUs / 329 redundant folders
    (measured 2026-08-02). Resolution order:

      1. aliases.json (durable migration map, honouring concentration);
      2. an existing live folder already reachable by image lookup;
      3. the raw product_key (genuinely new product).

    Never returns a folder marked MIGRATED_TO.json, and never crosses an
    explicit concentration boundary (EDP folder for an EDT product).
    """
    key = product_key(product)
    if not key:
        return key
    wanted_concentration = product_concentration_key(product)

    def usable(folder: str) -> bool:
        if not folder:
            return False
        target = IMAGE_BANK_PRODUCTS / folder
        if not target.is_dir() or (target / "MIGRATED_TO.json").exists():
            return False
        folder_concentration = product_concentration_key(folder)
        if wanted_concentration and folder_concentration and folder_concentration != wanted_concentration:
            return False
        return True

    def approved_count(folder: str) -> int:
        total = 0
        for tier in IMAGE_TIER_DIRS:
            tier_dir = IMAGE_BANK_PRODUCTS / folder / tier
            if tier_dir.is_dir():
                total += sum(
                    1 for p in tier_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                )
        return total

    def rank(folder: str) -> tuple[int, int]:
        """Concentration match first, then curated weight.

        Weight matters: several live folders may describe the same SKU, and the
        new photo must land where the curated set already is — otherwise the
        writer keeps feeding a folder the reader never picks (Natura Homem
        Coragião had 12 approved photos in one folder while writes went to an
        empty sibling).
        """
        conc = 2 if wanted_concentration and product_concentration_key(folder) == wanted_concentration else 1
        return conc, approved_count(folder)

    candidates: list[str] = []
    try:
        payload = json.loads(IMAGE_BANK_ALIASES.read_text(encoding="utf-8")) if IMAGE_BANK_ALIASES.exists() else {}
        if isinstance(payload, dict):
            for alias_key, alias_values in payload.items():
                if alias_key != key and product_key(str(alias_key).replace("-", " ")) != key:
                    continue
                if isinstance(alias_values, str):
                    candidates.append(alias_values)
                elif isinstance(alias_values, list):
                    candidates.extend(str(x) for x in alias_values if x)
    except Exception:
        pass
    ranked = [(rank(c), c) for c in candidates if usable(c)]
    if ranked and max(r[0] for r, _ in ranked) >= 2:
        return sorted(ranked, key=lambda row: row[0], reverse=True)[0][1]

    # No alias hit: reuse a folder the READER already resolves to, so writer and
    # reader can never disagree about where this product lives.
    existing: list[tuple[tuple[int, int], str]] = []
    try:
        for tier_dir in _approved_final_candidates_impl(product):
            folder = tier_dir.parent
            if usable(folder.name):
                existing.append((rank(folder.name), folder.name))
    except Exception:
        pass
    if existing:
        best = max(r for r, _ in existing)
        if best[0] >= 2 or not wanted_concentration:
            return sorted([n for r, n in existing if r == best])[0]

    if ranked:
        return sorted(ranked, key=lambda row: row[0], reverse=True)[0][1]
    return key


def _tier_dir_has_image(tier_dir: Path) -> bool:
    """True when a tier folder actually holds at least one usable image.

    Existence of the directory is not enough: consolidation leaves empty
    shells behind, and an empty shell must never count as a lookup hit.
    """
    if not tier_dir.is_dir():
        return False
    try:
        for p in tier_dir.iterdir():
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                return True
    except OSError:
        return False
    return False


_HUMAN_IMAGE_HASH_CACHE: dict[tuple, set[str]] = {}


def human_final_hashes_for_product(product: str) -> set[str]:
    """Gender evidence from the exact card → human-final image join, read-only."""
    try:
        cycles_stamp = fingerprint(EDITORIAL_CYCLES_FILE)
        feedback_stamp = fingerprint(EDITORIAL_CYCLES_FILE.with_name('normalizer_feedback.jsonl'))
        cache_key = (product, cycles_stamp, feedback_stamp)
        if cache_key in _HUMAN_IMAGE_HASH_CACHE:
            return _HUMAN_IMAGE_HASH_CACHE[cache_key]
        records = human_final_records(cycles_stamp, feedback_stamp)
    except (OSError, ValueError, TypeError):
        return set()
    result: set[str] = set()
    key = product_key(product)
    wanted_gender = product_gender_key(product)
    for row, source_product, hashes in records:
        if not editorial_cycle_eligible(row) or product_gender_key(source_product) != wanted_gender:
            continue
        if (not identity_compatible(product, source_product, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                or product_variant_conflict(product, source_product)):
            continue
        source_key = product_key(source_product)
        if key != source_key and not image_bank_key_matches(key, source_key):
            continue
        after_title = str((row.get('after') or {}).get('title') or '')
        if after_title and product_variant_conflict(product, after_title):
            continue
        result.update(hashes)
    if len(_HUMAN_IMAGE_HASH_CACHE) >= 256:
        _HUMAN_IMAGE_HASH_CACHE.clear()
    _HUMAN_IMAGE_HASH_CACHE[cache_key] = result
    return result


def bank_folder_genders(folder: Path) -> set[str]:
    genders = {product_gender_key(folder.name)} - {''}
    try:
        data = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
        values = [data.get(k, '') for k in ('product_key', 'canonical_guess', 'product_guess')]
        values += [v.get('product_guess') or v.get('canonical_guess') or ''
                   for bucket in ('approved', 'raw_items') for v in (data.get(bucket) or [])
                   if isinstance(v, dict)]
        genders.update(product_gender_key(v) for v in values)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return genders - {''}


@lru_cache(maxsize=4)
def _historical_approved_folder_genders(feedback_mtime_ns: int) -> dict[str, set[str]]:
    """Recover missing legacy metadata only from previously successful live use."""
    result: dict[str, set[str]] = {}
    feedback = IMAGE_BANK_PRODUCTS.parent / 'logs' / 'normalizer_feedback.jsonl'
    try:
        with feedback.open(encoding='utf-8', errors='ignore') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                image_path = str(row.get('image_path') or '')
                if not row.get('approved') or '/approved_final/' not in image_path:
                    continue
                gender = product_gender_key(str(row.get('display_product') or row.get('product') or ''))
                if gender:
                    result.setdefault(Path(image_path).parent.parent.name, set()).add(gender)
    except OSError:
        pass
    return result


def historical_approved_folder_genders(folder: Path) -> set[str]:
    feedback = IMAGE_BANK_PRODUCTS.parent / 'logs' / 'normalizer_feedback.jsonl'
    try:
        mtime = feedback.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return _historical_approved_folder_genders(mtime).get(folder.name, set())


def bank_kit_asset_allowed(product: str, media: Path, folder: Path) -> bool:
    """Generic kit aliases need exact, hash-linked component evidence per asset."""
    if not kit_signature(product)[0] and not kit_signature(folder.name)[0]:
        return True
    if kit_identity_matches(product, folder.name):
        return True
    try:
        data = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
        digest = file_digest(media)
        for bucket in ('approved', 'raw_items'):
            for item in data.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                linked = item.get('sha256') == digest or str(item.get('media_path') or '') == str(media)
                if not linked:
                    continue
                title = str(item.get('product_guess') or item.get('canonical_guess') or '')
                identity = kit_image_identity(str(item.get('body') or ''), title) or title
                if kit_identity_matches(product, identity):
                    return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False


def bank_asset_linked_identities(media: Path, folder: Path) -> list[str]:
    """Return SKU identities linked to one asset, including legacy source bodies."""
    try:
        data = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
        digest = file_digest(media)
        values: list[str] = []
        for bucket in ('approved', 'raw_items'):
            for item in data.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                linked = item.get('sha256') == digest or str(item.get('media_path') or '') == str(media)
                if not linked:
                    continue
                title = str(item.get('product_guess') or item.get('canonical_guess') or '').strip()
                body_product = extract_product(str(item.get('body') or '')).strip()
                values.extend(v for v in (body_product, title) if v)
        return list(dict.fromkeys(values))
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def bank_asset_identity_allowed(product: str, media: Path, folder: Path | None = None) -> bool:
    folder = folder or media.parent.parent
    if (image_identity_hold(media) or not bank_kit_asset_allowed(product, media, folder)
            or fakhar_variant_conflict(product, folder.name.replace('-', ' '))):
        return False
    linked_identities = bank_asset_linked_identities(media, folder)
    if any(identity_gender(identity) in {'masculino', 'feminino'}
           and identity_gender(product) != identity_gender(identity)
           for identity in linked_identities):
        return False
    folder_compatible = identity_compatible(product, folder.name, DISTINCTIVE_VARIANT_TOKENS, strict=True)
    linked_compatible = any(
        identity_compatible(product, identity, DISTINCTIVE_VARIANT_TOKENS, strict=True)
        and not product_variant_conflict(product, identity)
        and image_bank_key_matches(product_key(product), product_key(identity))
        for identity in linked_identities
    )
    if not folder_compatible and not linked_compatible:
        return False
    from perfume_round_guards import REVIEWED_ASSET_IDENTITIES
    verified_identity = REVIEWED_ASSET_IDENTITIES.get(file_digest(media), '')
    if verified_identity:
        return (product_key(product) == product_key(verified_identity)
                and identity_compatible(product, verified_identity, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                and (not product_concentration_key(product)
                     or product_concentration_key(product) == product_concentration_key(verified_identity)))
    wanted = product_gender_key(product)
    evidenced_genders = bank_folder_genders(folder) or historical_approved_folder_genders(folder)
    if not wanted or evidenced_genders == {wanted}:
        return True
    # Human-approved legacy assets often omitted gender while preserving an exact
    # SKU in the linked body. Omission is not conflict; a recorded opposite gender
    # still fails closed.
    if linked_compatible and not evidenced_genders:
        return True
    return file_digest(media) in human_final_hashes_for_product(product)


def _approved_final_candidates_impl(product: str) -> list[Path]:
    key = product_key(product)
    wanted_gender = product_gender_key(product)
    def safe_folder(folder: Path) -> bool:
        # Every route (direct key, alias, metadata and fuzzy) shares this gate.
        # Compare RAW identity: product_key intentionally loses gender/type.
        if fakhar_variant_conflict(product, folder.name.replace('-', ' ')):
            return False
        if not identity_compatible(product, folder.name, DISTINCTIVE_VARIANT_TOKENS, strict=True):
            # A small legacy set lives under headline-derived folder names. Only
            # admit such a folder when a concrete asset is hash/path-linked to a
            # source body that independently identifies this exact SKU.
            if not any(bank_asset_identity_allowed(product, p, folder)
                       for tier in IMAGE_TIER_DIRS if (folder / tier).is_dir()
                       for p in (folder / tier).iterdir()
                       if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}):
                return False
        if kit_signature(product)[0] or kit_signature(folder.name)[0]:
            return any(bank_asset_identity_allowed(product, p, folder)
                       for tier in IMAGE_TIER_DIRS if (folder / tier).is_dir()
                       for p in (folder / tier).iterdir()
                       if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'})
        if wanted_gender and not product_gender_key(folder.name):
            evidenced_genders = bank_folder_genders(folder) or historical_approved_folder_genders(folder)
            # Keep historically productive unqualified folders available when
            # there is no contrary metadata; exact known-wrong assets are
            # rejected individually by bank_asset_identity_allowed.
            if evidenced_genders and evidenced_genders != {wanted_gender}:
                return any(bank_asset_identity_allowed(product, p, folder) for tier in IMAGE_TIER_DIRS
                           if (folder / tier).is_dir() for p in (folder / tier).iterdir()
                           if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'})
        return True
    dirs: list[Path] = []
    seen: set[Path] = set()
    for cand in image_lookup_keys(product):
        for tier in IMAGE_TIER_DIRS:
            path = IMAGE_BANK_PRODUCTS / cand / tier
            if path not in seen and safe_folder(path.parent):
                dirs.append(path)
                seen.add(path)
        # Canonical-key folders sometimes coexist with an unqualified sibling:
        # natura-homem-coragio-100ml <-> natura-homem-coragio. Include the
        # sibling so alternate approved photos are considered before reporting
        # "same photo-base".
        if cand.endswith("-100ml") or cand.endswith("-90ml") or cand.endswith("-125ml") or cand.endswith("-150ml") or cand.endswith("-200ml"):
            no_volume = re.sub(r"-\d{2,3}ml$", "", cand)
            if no_volume and no_volume != cand:
                for tier in IMAGE_TIER_DIRS:
                    path = IMAGE_BANK_PRODUCTS / no_volume / tier
                    if path not in seen and safe_folder(path.parent):
                        dirs.append(path)
                        seen.add(path)

    if IMAGE_BANK_PRODUCTS.is_dir():
        for product_dir in sorted(IMAGE_BANK_PRODUCTS.iterdir()):
            if not product_dir.is_dir():
                continue
            folder_gender = product_gender_key(product_dir.name)
            if wanted_gender and folder_gender and wanted_gender != folder_gender:
                continue
            if image_bank_key_matches(key, product_dir.name) and safe_folder(product_dir):
                for tier in IMAGE_TIER_DIRS:
                    path = product_dir / tier
                    if path not in seen:
                        dirs.append(path)
                        seen.add(path)
        # Consolidation (2026-08-02) merged 427 folders into canonical ones and
        # left the sources behind as empty shells. An exact-key hit on a shell
        # used to satisfy the lookup and stop the search, so dior-sauvage-edt-100ml
        # (0 photos) shadowed the folder that actually holds the bottle. Only
        # folders that really carry an image should count as a resolved hit;
        # otherwise fall through to the fuzzy pass below.
        if not any(_tier_dir_has_image(d) for d in dirs):
            # Exact identity can live only inside a legacy index because old
            # collectors named the directory after the promotional headline.
            # Resolve that deterministic evidence before fuzzy clustering; the
            # latter may see many empty/raw duplicate folders and manufacture
            # ambiguity where the hash-linked approved asset is unambiguous.
            for product_dir in sorted(IMAGE_BANK_PRODUCTS.iterdir()):
                if not product_dir.is_dir():
                    continue
                metadata_keys = image_bank_metadata_keys(product_dir)
                if (any(image_bank_key_matches(key, metadata_key) for metadata_key in metadata_keys)
                        and safe_folder(product_dir)):
                    for tier in IMAGE_TIER_DIRS:
                        path = product_dir / tier
                        if path not in seen:
                            dirs.append(path)
                            seen.add(path)
            if any(_tier_dir_has_image(d) for d in dirs):
                return dirs
            scored: list[tuple[int, Path, str]] = []
            for product_dir in sorted(IMAGE_BANK_PRODUCTS.iterdir()):
                if not product_dir.is_dir():
                    continue
                folder_gender = product_gender_key(product_dir.name)
                if wanted_gender and folder_gender and wanted_gender != folder_gender:
                    continue
                candidate_keys = [product_dir.name] + image_bank_metadata_keys(product_dir)
                scored_keys = [
                    (
                        120 if image_bank_key_matches(key, candidate_key)
                        else image_bank_fuzzy_score(key, candidate_key),
                        candidate_key,
                    )
                    for candidate_key in candidate_keys
                ]
                score, matched_key = max(scored_keys, default=(0, product_dir.name))
                if score <= 0 or not safe_folder(product_dir):
                    continue
                if any(_tier_dir_has_image(product_dir / tier) for tier in IMAGE_TIER_DIRS):
                    scored.append((score, product_dir, matched_key))
            scored.sort(key=lambda x: x[0], reverse=True)
            clusters: dict[str, list[tuple[int, Path, str]]] = {}
            for row in scored:
                cluster_key = product_key(row[2].replace("-", " ")) or row[2]
                clusters.setdefault(cluster_key, []).append(row)
            ranked_clusters = sorted(
                ((max(r[0] for r in rows), cluster_key, rows) for cluster_key, rows in clusters.items()),
                key=lambda x: x[0], reverse=True,
            )
            # Ambiguity is between different canonical SKUs, not duplicate folder
            # spellings for the same SKU (de/EDP/attribute/order differences).
            #
            # 2026-08-02: the 18-point margin alone was too blunt. "LATTAFA
            # KHAMRAH" scored 100 on lattafa-khamrah-100ml (10 approved photos)
            # and 92 on the lattafa-khamrah-qahwa flanker, so the gate declared
            # ambiguity and returned nothing -- even though the top cluster is an
            # EXACT identity match and the runner-up only ranks high because it
            # contains every token of the query plus a flanker word. When the
            # best cluster's tokens equal the query's tokens and no rival can say
            # the same, that is not ambiguity: it is the answer.
            def _identity_tokens(k: str) -> frozenset[str]:
                return frozenset(
                    t for t in k.split("-")
                    if t and t not in CONCENTRATION_TOKENS and not t.endswith("ml")
                    and not re.fullmatch(r"\d{1,3}", t)
                )

            _query_ident = _identity_tokens(key)
            _exact_clusters = [
                c for c in ranked_clusters
                if _query_ident and _identity_tokens(c[1]) == _query_ident
            ]
            _exact_identity_win = (
                len(_exact_clusters) == 1
                and _exact_clusters[0] is ranked_clusters[0]
            )
            # Source titles routinely drop the brand: "FAKHAR GOLD" for
            # lattafa-fakhar-gold, "9PM REBEL" for afnan-9pm-rebel. The query's
            # tokens are then a strict SUBSET of the folder's, differing only by
            # a known brand word. That is still an exact identity match -- the
            # extra token adds no flanker, it names the house. Accept it only
            # when exactly one cluster qualifies, so Khamrah-vs-Khamrah-Qahwa
            # (which differs by a real flanker) keeps being refused.
            if not _exact_identity_win and _query_ident:
                _brand_only = []
                for c in ranked_clusters:
                    ident = _identity_tokens(c[1])
                    if not _query_ident < ident:
                        continue
                    extra = ident - _query_ident
                    if extra and extra <= KNOWN_BRAND_TOKENS:
                        _brand_only.append(c)
                if len(_brand_only) == 1 and _brand_only[0] is ranked_clusters[0]:
                    _exact_identity_win = True
            if ranked_clusters and (
                len(ranked_clusters) == 1
                or _exact_identity_win
                or ranked_clusters[0][0] - ranked_clusters[1][0] >= 18
            ):
                for _score, product_dir, _matched_key in ranked_clusters[0][2]:
                    for tier in IMAGE_TIER_DIRS:
                        path = product_dir / tier
                        if path not in seen:
                            dirs.append(path)
                            seen.add(path)
            else:
                audit_image_bank_miss(product, key, scored)
    # product_key intentionally strips generic concentration words, but folders
    # with an explicit conflicting concentration must never compete. Unknown
    # concentration remains eligible for historical folders that omit EDT/EDP.
    wanted_concentration = product_concentration_key(product)
    if wanted_concentration:
        def _has_images(tier_dir: Path) -> bool:
            if not tier_dir.is_dir():
                return False
            return any(
                p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                for p in tier_dir.iterdir()
            )

        exact_concentration_dirs = [
            d for d in dirs
            if product_concentration_key(d.parent.name) == wanted_concentration
        ]
        # An EDT-named folder that holds no usable image must not shadow the
        # unqualified folder that actually has the curated photos. Ralph Lauren
        # Polo Sport had 3 approved images in ralph-lauren-polo-sport-100ml
        # while the empty ...-edt-100ml sibling won the filter and the promo
        # shipped imageless (2026-08-02).
        # Unqualified folders hold the same SKU under a spelling that simply
        # omits EDT/EDP; they are NOT a conflicting concentration. Keeping them
        # alongside the exact matches costs nothing (find_image still ranks and
        # the variant guard still applies) and fixes the case where the
        # EDT-named folder is non-empty but holds only the photo Breno just
        # sent: ysl-y-edt-100ml had 1 same-base image while
        # yves-saint-laurent-y-100ml held 3 visually different approved photos,
        # and the promo shipped reusing his own picture (2026-08-02).
        unqualified_dirs = [d for d in dirs if not product_concentration_key(d.parent.name)]
        if any(_has_images(d) for d in exact_concentration_dirs):
            merged = list(exact_concentration_dirs)
            merged.extend(d for d in unqualified_dirs if d not in exact_concentration_dirs)
            dirs = merged
        else:
            # Unqualified folders only — never fall back to a CONFLICTING
            # concentration (an EDT product may not borrow an EDP bottle).
            dirs = unqualified_dirs
    return dirs


def first_file_in_dir(path: Path) -> str:
    if not path.is_dir():
        return ""
    candidates = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    return str(best_image_candidate(candidates)) if candidates else ""


def image_quality_score(path: Path) -> tuple[int, int]:
    """Prefer real/high-res images; punish template borders and low quality."""
    score = 0
    pstr = str(path)
    if any(f"/{name}/" in pstr for name in REJECTED_IMAGE_DIRS):
        return (-999999, 0)
    if "/high_quality/" in pstr:
        score += 5000
    elif "/approved_final/" in pstr:
        score += 2500
    elif "/acceptable/" in pstr:
        score += 800
    try:
        from PIL import Image, ImageChops

        im = Image.open(path).convert("RGB")
        pixels = im.width * im.height
        score += min(im.width, 1600) + min(im.height, 1600)
        if pixels >= 900_000:
            score += 1800
        elif pixels < 250_000:
            score -= 2500
        corner = im.getpixel((0, 0))
        corner_rgb = tuple(int(x or 0) for x in corner[:3]) if isinstance(corner, tuple) else (int(corner or 0), int(corner or 0), int(corner or 0))
        corner_lightness = sum(corner_rgb) / 3
        if corner_lightness > 225:
            score -= 800
        bg = Image.new("RGB", im.size, corner_rgb)
        bbox = ImageChops.difference(im, bg).convert("L").point(lambda px: 255 if px > 18 else 0).getbbox()  # type: ignore[operator]
        if bbox:
            left, top, right, bottom = bbox
            margin = min(left, top, im.width - right, im.height - bottom)
            if 8 <= margin <= 35 and corner_lightness > 220:
                score -= 5000
            if margin <= 2:
                score += 600
    except Exception:
        pass
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    # Human taste outranks automatic sharpness metrics. Low-priority assets stay
    # approved and usable as last resort, but lose to compatible alternatives.
    try:
        prefs = json.loads(IMAGE_PREFERENCES_FILE.read_text(encoding="utf-8")) if IMAGE_PREFERENCES_FILE.exists() else {}
        relative = str(path.relative_to(IMAGE_BANK_PRODUCTS.parent))
        if relative in set(prefs.get("low_priority") or []):
            score -= 10000
    except Exception:
        pass
    return score, size


_IMAGE_WATERMARK_CACHE: dict[str, tuple[bool, str]] = {}


def image_explicit_watermark(path: Path) -> tuple[bool, str]:
    """Reuse completed OCR across processes, never stale approval decisions."""
    import perfume_ocr_cache as ocr_cache
    try:
        cache_key = ocr_cache.fingerprint(path)
    except OSError:
        return False, ""
    if cache_key in _IMAGE_WATERMARK_CACHE:
        return _IMAGE_WATERMARK_CACHE[cache_key]
    cached = ocr_cache.get(cache_key)
    if cached is not None:
        _IMAGE_WATERMARK_CACHE[cache_key] = cached
        return cached
    result = (False, "")
    try:
        proc = subprocess.run(
            ["/usr/bin/tesseract", str(path), "stdout", "-l", "por+eng", "--psm", "11"],
            text=True, capture_output=True, timeout=12,
        )
        if proc.returncode != 0:
            return result  # Failed OCR is not reusable negative evidence.
        text = re.sub(r"\s+", " ", proc.stdout or "").strip()
        marker = re.search(r"(?i)(@[a-z0-9_.]{4,}|https?://\S+|www\.\S+|[a-z0-9_-]+\.(?:com|com\.br)/\S*)", text)
        if marker:
            result = (True, marker.group(1))
        # A miner may replace the file while OCR is running.
        if ocr_cache.fingerprint(path) == cache_key:
            ocr_cache.put(cache_key, result)
            _IMAGE_WATERMARK_CACHE[cache_key] = result
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def image_quality_tier(path: Path) -> tuple[str, str]:
    try:
        stat = path.stat()
        return _cached_image_quality_tier(str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except OSError:
        return _uncached_image_quality_tier(path)


@__import__('functools').lru_cache(maxsize=256)
def _cached_image_quality_tier(path: str, mtime: int, ctime: int, size: int) -> tuple[str, str]:
    # Process-local: no stale persistent approval; changed/moved files miss cache.
    return _uncached_image_quality_tier(Path(path))


def _uncached_image_quality_tier(path: Path) -> tuple[str, str]:
    """Return bank tier: high_quality, acceptable, or rejected with reason."""
    try:
        from PIL import Image, ImageChops, ImageFilter, ImageStat

        im = Image.open(path).convert("RGB")
        size = path.stat().st_size
        if im.width * im.height < 250_000 or size < 35_000:
            return "rejected", "baixa resolução/compressão"
        has_watermark, watermark = image_explicit_watermark(path)
        if has_watermark:
            return "rejected", f"watermark explícita: {watermark}"
        corner = im.getpixel((0, 0))
        corner_rgb = tuple(int(x or 0) for x in corner[:3]) if isinstance(corner, tuple) else (int(corner or 0), int(corner or 0), int(corner or 0))
        corner_lightness = sum(corner_rgb) / 3
        bg = Image.new("RGB", im.size, corner_rgb)
        diff = ImageChops.difference(im, bg).convert("L")
        bbox = diff.point(lambda px: 255 if px > 18 else 0).getbbox()  # type: ignore[operator]
        if bbox:
            left, top, right, bottom = bbox
            margin = min(left, top, im.width - right, im.height - bottom)
            if 8 <= margin <= 40 and corner_lightness > 215:
                return "rejected", "moldura/template claro"
        gray = im.convert("L")
        sharpness = sum(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean)
        if im.width * im.height >= 900_000 and size >= 80_000 and sharpness >= 10:
            return "high_quality", "fonte pesada/nítida"
        return "acceptable", "aprovada aceitável"
    except Exception as exc:
        return "rejected", f"erro ao avaliar: {exc}"


def best_image_candidate(paths: list[Path]) -> Path:
    def ranked(path: Path):
        score, size = image_quality_score(path)
        name = path.name.lower()
        breno = 1 if name.startswith("breno_final_") else 0
        auto = 1 if "final_auto" in name else 0
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        # Explicit Promo Edição approval is editorial preference, not a weak
        # tie-breaker. A newer Breno-approved composition must beat an older
        # technically sharper source (Khamrah case); quality ranks within the
        # same human/automatic class. This also prevents a future source miner
        # from silently undoing Breno's chosen photo.
        #
        # BUT recency must not promote a DEGRADED COPY of a photo we already
        # have in better resolution. Blue Seduction (2026-07-26): a 500x500
        # re-share outranked the 1254x1254 approved asset purely for being
        # newer, and the promo shipped visibly soft. Recency expresses a choice
        # of COMPOSITION, not a decision to lose pixels — so within the same
        # human class, quantize resolution first and let mtime break ties among
        # comparable-resolution photos.
        res_band = 0
        try:
            from PIL import Image
            with Image.open(path) as im:
                res_band = min(im.width, im.height) // 200
        except Exception:
            res_band = 0
        # The human/automatic class itself must not survive a big resolution
        # gap. Golden Secret (2026-07-26): a 500x500 breno_final outranked an
        # 1149x1149 approved source purely for being human-tagged — but that
        # 500px file IS our own downscaled output that came back through the
        # Edição group, so "human choice" was an artifact, not a preference.
        # Only let the human flag win when it is not a clearly worse copy.
        return res_band, breno, mtime if breno else 0, score, -auto, size
    winner = sorted(paths, key=ranked, reverse=True)[0]
    # Recency chooses composition, never a degraded copy of that composition.
    # Restrict upgrades to the same visual base; do not overturn human choice
    # merely because a different composition has a few extra pixels.
    def resolution(path: Path) -> tuple[int, int]:
        try:
            from PIL import Image
            with Image.open(path) as im:
                return min(im.size), im.width * im.height
        except Exception:
            return (0, 0)
    winner_resolution = resolution(winner)
    upgrades = [p for p in paths if resolution(p) > winner_resolution
                and looks_like_same_image_base(str(p), str(winner))]
    return max(upgrades, key=lambda p: (resolution(p), image_quality_score(p))) if upgrades else winner


def approved_source_candidate(media_path: str) -> str:
    """Use a higher-quality sibling source only when an approved_final exists.

    The bank often has a 500x500 approved_final plus the same approved visual in
    candidate/ at 1200x1200. This keeps the approval gate, but prepares the send
    image from the fuller source when it is clearly the same captured asset.
    """
    src = Path(media_path)
    if "/approved_final/" not in str(src) or not src.exists():
        return media_path
    candidate_dir = src.parent.parent / "candidate"
    if not candidate_dir.is_dir():
        return media_path
    try:
        from PIL import Image

        src_im = Image.open(src)
        src_pixels = src_im.width * src_im.height
        src_stem = src.stem
        # Stem/hash relationship OR visual same-base match. Stem-only missed
        # richer sources whose filenames don't share the hash (Breno request:
        # always pick the best-quality version of the same approved photo).
        candidates = []
        for p in candidate_dir.iterdir():
            if not (p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}):
                continue
            if p.stem in src_stem or src_stem in p.stem or p.stem[-12:] in src_stem:
                candidates.append(p)
            elif looks_like_same_image_base(str(p), str(src)):
                candidates.append(p)
        if not candidates:
            return media_path
        richer: list[Path] = []
        for cand in candidates:
            try:
                im = Image.open(cand)
                if im.width * im.height > src_pixels and cand.stat().st_size >= src.stat().st_size:
                    richer.append(cand)
            except Exception:
                continue
        return str(best_image_candidate(richer)) if richer else media_path
    except Exception:
        return media_path


def source_image_identity_proved(product: str, media: Path, source_product: str = "") -> bool:
    """Reject known contradictions; missing gender is not evidence of a mistake."""
    from perfume_round_guards import REVIEWED_ASSET_IDENTITIES
    digest = file_digest(media)
    verified = REVIEWED_ASSET_IDENTITIES.get(digest, '')
    if verified:
        return (identity_compatible(product, verified, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                and product_key(product) == product_key(verified)
                and not fakhar_variant_conflict(product, verified))
    if fakhar_variant_conflict(product, source_product):
        return False
    wanted, source = identity_gender(product), identity_gender(source_product)
    if wanted and source and wanted != source:
        return False
    return True


def recent_exact_source_image(product: str, *, link: str = "", max_lines: int = 6000) -> str:
    """Return a clean recent promo image for the exact SKU, without promoting it."""
    if not TAP_FILE.is_file():
        return ""
    try:
        from collections import deque
        with TAP_FILE.open(encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=max_lines)
    except OSError:
        return ""
    candidates: list[Path] = []
    wanted_key = product_key(product)
    wanted_link = extract_link(link or product)
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except Exception:
            continue
        media_values = event.get("mediaUrls") or event.get("media_urls") or []
        if isinstance(media_values, str):
            media_values = [media_values]
        media_candidates = [
            event.get("mediaPath"), event.get("media_path"),
            *(media_values if isinstance(media_values, list) else []),
        ]
        media = next((Path(str(value)) for value in media_candidates if value), Path(""))
        body = str(event.get("body") or "")
        event_link = extract_link(body)
        if not media.is_file() or not body or "meli.la/" not in body.lower():
            continue
        # Source copy sometimes puts a marketing sentence where extract_product
        # expects the title. Evaluate individual lines too; token order is not
        # identity ("Obsidian Rayhaan" == "Rayhaan Obsidian").
        title_candidates = [extract_product(body)] + [
            strip_wa(line).strip(" -•\t") for line in body.splitlines()
            if strip_wa(line).strip(" -•\t")
        ]
        def _base_identity(text: str) -> frozenset[str]:
            return frozenset(t for t in product_key(text).split("-")
                             if t and not re.fullmatch(r"\d{1,3}(?:ml)?", t)
                             and t not in CONCENTRATION_TOKENS)
        wanted_identity = _base_identity(product)
        source_product = next((title for title in title_candidates
                               if wanted_identity and _base_identity(title) == wanted_identity), "")
        same_offer = bool(wanted_link and event_link and wanted_link == event_link)
        if not source_product and same_offer:
            # A Breno-edited copy can deliberately shorten marketing/filler
            # words ("árabe", "original", "perfume", "beauty", "garantia")
            # while retaining the same offer link.  The link is stronger than
            # lexical equality, but it is not permission to ignore hard gates.
            source_product = next((title for title in title_candidates
                                   if len(_base_identity(title) & wanted_identity) >= 2), "")
        if not source_product:
            continue
        # A shared link and a matching family name do not prove that an
        # unlabeled photo shows the gendered variant requested by the card.
        if not source_image_identity_proved(product, media, source_product):
            continue
        # product_key intentionally drops dimensions; retain the hard gates.
        compatible = identity_compatible(product, source_product, DISTINCTIVE_VARIANT_TOKENS, strict=True)
        if not compatible and not same_offer:
            continue
        if not compatible:
            # Same-link recovery may relax only lexical filler. Never relax
            # explicit product/variant conflicts, concentration, volume or kit.
            if (product_variant_conflict(product, source_product)
                    or identity_token_conflict(product, source_product)):
                continue
        wanted_conc, source_conc = product_concentration_key(product), product_concentration_key(source_product)
        if wanted_conc and source_conc and wanted_conc != source_conc:
            continue
        def _volume(text: str) -> str:
            match = re.search(r"(?i)\b(\d{2,3})\s*ml\b", text or "")
            return match.group(1) if match else ""
        wanted_volume, source_volume = _volume(product), _volume(source_product)
        if wanted_volume and source_volume and wanted_volume != source_volume:
            continue
        wanted_kit, wanted_parts, _ = kit_signature(product)
        source_kit, source_parts, _ = kit_signature(source_product)
        if wanted_kit != source_kit:
            continue
        if wanted_kit and wanted_parts != source_parts:
            # Same offer links may have harmless editorial omissions (for
            # example, dropping "Beauty"), but never accept a different kit
            # cardinality or an individual item in place of a kit.
            if not same_offer or len(wanted_parts & source_parts) < 2:
                continue
        if image_quality_tier(media)[0] == "rejected":
            continue
        candidates.append(media)
    return str(best_image_candidate(candidates)) if candidates else ""


def _image_rotation_key(path: Path) -> str:
    """Stable identity for an approved composition, independent of filename."""
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return str(path)


def _rotated_image_choice(product: str, paths: list[Path]) -> Path:
    """Choose the least-recently-used approved photo, then quality within ties.

    Variety is deliberately subordinate to the approved/SKU gate: when every
    photo has been used, the oldest one wins. State is advisory; a corrupt or
    unwritable history must never block delivery.
    """
    if len(paths) <= 1:
        chosen = paths[0]
    else:
        try:
            with _IMAGE_ROTATION_LOCK:
                data = json.loads(IMAGE_USAGE_FILE.read_text(encoding="utf-8")) if IMAGE_USAGE_FILE.exists() else {}
                rows = data if isinstance(data, dict) else {}
                product_rows = rows.get(product_key(product), {})
                if not isinstance(product_rows, dict):
                    product_rows = {}
                candidates = [p for p in paths if _image_rotation_key(p) not in _IMAGE_RESERVED]
                if not candidates:
                    candidates = paths
                chosen = min(
                    candidates,
                    key=lambda p: (
                        float(product_rows.get(_image_rotation_key(p), 0) or 0),
                        -image_quality_score(p)[0],
                        -p.stat().st_size,
                    ),
                )
                digest = _image_rotation_key(chosen)
                _IMAGE_RESERVED.add(digest)
                product_rows[digest] = time.time()
                rows[product_key(product)] = product_rows
                IMAGE_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = IMAGE_USAGE_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(IMAGE_USAGE_FILE)
                return chosen
        except Exception:
            chosen = best_image_candidate(paths)
    with _IMAGE_ROTATION_LOCK:
        _IMAGE_RESERVED.add(_image_rotation_key(chosen))
    return chosen


def find_image(product: str, incoming_media: str = "", *, timings=None, link: str = "") -> str:
    """Find a different approved image for the product.

    If Breno sends a promo with media, do not "replace" it with an approved_final
    asset derived from the same photo-base. That looked technically correct in
    the report but was operationally useless: same photo wearing a fake mustache.
    """
    def measured(name, fn, *args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            if timings is not None:
                timings[name] = round(timings.get(name, 0) + time.monotonic() - started, 4)

    approved_medias: list[Path] = []
    for approved_dir in measured('image_identity_scan_seconds', approved_final_candidates, product):
        if not approved_dir.is_dir():
            continue
        approved_medias.extend(
            p for p in approved_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and bank_asset_identity_allowed(product, p)
        )
    if approved_medias:
        if timings is not None:
            timings['image_candidates_checked'] = timings.get('image_candidates_checked', 0) + len(approved_medias)
        usable = [p for p in approved_medias if measured('image_quality_seconds', image_quality_tier, p)[0] != "rejected"]
        if not usable:
            # Fail closed: an approved-folder label cannot resurrect an asset
            # rejected for watermark, low quality or a hard visual defect.
            return ""
        if incoming_media:
            same_base, different_base = [], []
            for p in usable:
                same = measured('image_comparison_seconds', looks_like_same_image_base, str(p), incoming_media)
                (same_base if same else different_base).append(p)
            if different_base:
                # HARD RULE (Breno, 2026-07-26): the published photo must be
                # VISUALLY DIFFERENT from the one he sent, even if the different
                # one is technically worse. Reusing his own photo is not a swap
                # at all, so it can never be traded for extra sharpness.
                # Resolution is the SECOND criterion and only ranks *within*
                # this different-base set (see best_image_candidate).
                return str(measured('image_ranking_seconds', _rotated_image_choice, product, different_base))
            if same_base:
                best_same = measured('image_ranking_seconds', best_image_candidate, same_base)
                try:
                    from PIL import Image
                    approved_im = Image.open(best_same)
                    incoming_im = Image.open(incoming_media)
                    approved_pixels = approved_im.width * approved_im.height
                    incoming_pixels = incoming_im.width * incoming_im.height
                    approved_size = best_same.stat().st_size
                    incoming_size = Path(incoming_media).stat().st_size
                    # Same photo-base is still useful when Breno approved a cleaner
                    # or higher-resolution version. This is not a visual novelty;
                    # it is an asset upgrade. Avoid only pointless same-base swaps.
                    if approved_pixels >= incoming_pixels * 1.15 or approved_size >= incoming_size * 1.20:
                        return str(best_same)
                except Exception:
                    pass
            # If the only approved assets are the same photo-base and not better,
            # keep the received image: matching an approved asset proves identity,
            # and a good received photo remains publishable. Do not report this as
            # a visual replacement; it is the same approved base.
            if same_base and Path(incoming_media).is_file():
                return incoming_media
            return ""
        return str(measured('image_ranking_seconds', _rotated_image_choice, product, usable)) if usable else ""
    # Legacy compatibility: some old indexes have an explicit approved bucket.
    # Do NOT auto-use raw_items/candidate/source_examples here; they are not
    # approved_final and caused weak images to leak into the normalizer.
    legacy_medias: list[Path] = []
    for cand in image_lookup_keys(product):
        idx = IMAGE_BANK_PRODUCTS / cand / "index.json"
        if not idx.exists():
            continue
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in data.get("approved") or []:
            # Legacy indexes used both object entries and plain path strings.
            # A single old string must not crash the entire WhatsApp queue.
            if isinstance(item, str):
                media = item
            elif isinstance(item, dict):
                media = item.get("media_path") or item.get("path") or item.get("final_500") or ""
            else:
                continue
            if (media and Path(media).is_file()
                    and bank_asset_identity_allowed(product, Path(media), idx.parent)
                    and image_quality_tier(Path(media))[0] != "rejected"
                    and not looks_like_same_image_base(media, incoming_media)):
                legacy_medias.append(Path(media))
    if legacy_medias:
        return str(best_image_candidate(legacy_medias))
    # The bank may lag behind the source tap. Prefer a clean exact-SKU historical
    # image over the false statement "foto ausente"; it remains non-final until
    # a human approves it, and conflicts still fail closed.
    return measured('image_recent_source_seconds', recent_exact_source_image, product, link=link)


def _square_target_for(im) -> int:
    """Pick the output square size without ever upscaling the source.

    Breno's requirement is that promo images are always square. The old fixed
    500px also *shrank* them: a 1440x1440 approved photo lost 88% of its pixels
    before WhatsApp even applied its own compression. Small-frame text (batch
    codes, "100ML", brand engraving) is the first thing to go.

    Never upscales, so a small asset is never blown up into the padded "white
    postcard" that killed the earlier 900px attempt. The 1000px ceiling was
    also arbitrary: 706 approved assets are already square above it, and
    capping them threw away real pixels for nothing (2026-07-26).
    """
    shortest = min(im.width, im.height)
    if shortest >= 1000:
        # Keep native resolution; WhatsApp will recompress once, not twice.
        return min(shortest, 1600)
    for candidate in (800, 640):
        if shortest >= candidate:
            return candidate
    return 500


def prepare_image_for_send(media_path: str) -> str:
    """Trim light template frames and export a clean square for WhatsApp.

    Output is ALWAYS square — that is the publishing requirement. The side
    length adapts to what the source can honestly fill (see
    _square_target_for), instead of the old hardcoded 500px that silently
    downsampled every high-resolution approved asset.
    """
    if not media_path:
        return ""
    media_src = Path(media_path)
    # Approved images are publishing-approved, but the send artifact still must
    # be the canonical 500x500 WhatsApp square. The selector upstream still
    # chooses the highest-quality approved source before this final downsample.
    if "approved_final" in media_src.parts and media_src.exists():
        try:
            from PIL import Image, ImageChops, ImageOps
            im = Image.open(media_src).convert("RGB")
            # Even 500x500 approved assets may contain white side bars; run the
            # trim/final-fit pass instead of returning them blindly.
            # Remove obvious solid/light bars before the final square fit. Some
            # approved imports are good product photos inside a white 4:5 frame;
            # blindly fitting preserves the ugly side borders Breno flagged.
            def rgb_tuple(px) -> tuple[int, int, int]:
                if isinstance(px, tuple):
                    return int(px[0] or 0), int(px[1] or 0), int(px[2] or 0)
                v = int(px or 0)
                return v, v, v
            corner_samples = [rgb_tuple(im.getpixel((0, 0))), rgb_tuple(im.getpixel((im.width - 1, 0))), rgb_tuple(im.getpixel((0, im.height - 1))), rgb_tuple(im.getpixel((im.width - 1, im.height - 1)))]
            bg_rgb = tuple(int(sum(px[i] for px in corner_samples) / 4) for i in range(3))
            bg = Image.new("RGB", im.size, bg_rgb)
            diff = ImageChops.difference(im, bg).convert("L")
            bbox = diff.point(lambda px: 255 if px > 14 else 0).getbbox()  # type: ignore[operator]
            if bbox:
                left, top, right, bottom = bbox
                margin = min(left, top, im.width - right, im.height - bottom)
                bg_lightness = sum(bg_rgb) / 3
                if bg_lightness >= 215 and margin <= max(80, int(min(im.size) * 0.18)):
                    pad = max(4, int(min(im.size) * 0.02))
                    im = im.crop((max(0, left - pad), max(0, top - pad), min(im.width, right + pad), min(im.height, bottom + pad)))
            PREPARED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            # Square is the hard requirement (Breno, 2026-07-25) — the SIZE was
            # never the point. 500x500 threw away up to 88% of the pixels of a
            # 1440px approved asset, and WhatsApp recompresses on top of that:
            # two stacked losses. Scale up to 1000 only when the source really
            # has the pixels; never upscale. The old 900px experiment looked
            # like a "white postcard" because small assets were PADDED into a
            # big canvas, which this guard prevents.
            target = _square_target_for(im)
            # Already square, already big enough, nothing trimmed: re-encoding
            # only costs a generation of JPEG quality before WhatsApp adds its
            # own. Ship the original bytes (2026-07-26).
            if (
                im.width == im.height
                and im.size == Image.open(media_src).size
                and im.width == target
            ):
                return str(media_src)
            out = PREPARED_IMAGE_DIR / f"prepared_{media_src.stem}_{target}_{__import__('uuid').uuid4().hex}.jpg"
            ImageOps.fit(im, (target, target), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)).save(out, quality=95, optimize=True, progressive=True)
            return str(out)
        except Exception:
            return str(media_src)
    src = Path(approved_source_candidate(media_path))
    if not src.exists():
        return media_path
    # Final images mined from Breno's promo/correction groups are the approved
    # artifact. Send original bytes: no resize, no canvas, no recompress.
    if src.name.lower().startswith("breno_final_"):
        return str(src)
    try:
        from PIL import Image, ImageChops, ImageOps

        im = Image.open(src).convert("RGB")
        def rgb_at(x: int, y: int) -> tuple[int, int, int]:
            px = im.getpixel((x, y))
            if not isinstance(px, tuple):
                return (0, 0, 0)
            return int(px[0] or 0), int(px[1] or 0), int(px[2] or 0)

        corner_samples = [
            rgb_at(0, 0),
            rgb_at(im.width - 1, 0),
            rgb_at(0, im.height - 1),
            rgb_at(im.width - 1, im.height - 1),
        ]
        bg_rgb = tuple(int(sum(px[i] for px in corner_samples) / len(corner_samples)) for i in range(3))
        bg_lightness = sum(bg_rgb) / 3

        # Breno-approved final screenshots/photos that already fill the square
        # should not be re-carded onto our beige/white canvas. That created the
        # exact visible border Breno complained about.
        if Path(media_path).name.lower().startswith("breno_final_") and im.width == im.height and bg_lightness < 215:
            return str(src)

        # Source already square: never re-card it. The old path always cropped 1%,
        # zoomed 1.08 and pasted at 92% of the canvas, which produced a beige
        # border (250,250,248) by construction on perfectly good square sources.
        # Only bypass when the frame is actually filled (or the background isn't a
        # light studio card), so the tiny-product-on-white-card fix still applies.
        if im.width == im.height:
            _probe = Image.new("RGB", im.size, bg_rgb)
            _pmask = ImageChops.difference(im, _probe).convert("L").point(lambda px: 255 if px > 14 else 0)  # type: ignore[operator]
            _pbox = _pmask.getbbox()
            _fill = 0.0
            if _pbox:
                _fill = ((_pbox[2] - _pbox[0]) * (_pbox[3] - _pbox[1])) / max(1, im.width * im.height)
            if bg_lightness < 220 or _fill >= 0.88:
                return str(src)

        # Trim obvious flat/light frame. This catches beige borders and rounded-card
        # leftovers better than trusting a single corner pixel.
        bg = Image.new("RGB", im.size, bg_rgb)
        diff = ImageChops.difference(im, bg).convert("L")
        mask = diff.point(lambda px: 255 if px > 14 else 0)  # type: ignore[operator]
        bbox = mask.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            edge = max(4, int(min(im.size) * 0.08))
            bg_lightness = sum(bg_rgb) / 3
            content_area = max(1, (right - left) * (bottom - top))
            image_area = max(1, im.width * im.height)
            # For light studio/template backgrounds, crop to actual content even
            # when the margin is large. This fixes the tiny-product-on-white-card
            # failure. For non-light/lifestyle backgrounds, stay conservative.
            if (
                (left <= edge and top <= edge and im.width - right <= edge and im.height - bottom <= edge)
                or (bg_lightness >= 220 and content_area / image_area <= 0.88)
            ):
                pad = max(6, int(min(im.size) * 0.035))
                im = im.crop((max(0, left - pad), max(0, top - pad), min(im.width, right + pad), min(im.height, bottom + pad)))

        # Safety pass: remove any residual solid/light edge after the first trim.
        im = ImageOps.crop(im, border=max(0, int(min(im.size) * 0.01)))

        # Slightly stronger center zoom removes leftover frame and makes the product
        # less timid in the WhatsApp card.
        zoom = 1.08
        zw, zh = int(im.width / zoom), int(im.height / zoom)
        if zw > 50 and zh > 50:
            l = (im.width - zw) // 2
            t = (im.height - zh) // 2
            im = im.crop((l, t, l + zw, t + zh))
        target = _square_target_for(Image.open(src))
        min_fill = int(target * 0.92)
        # Always square, at the best size the source can honestly fill. A fixed
        # 500 discarded most of the detail of a large photo (Thahaani: 828x814
        # -> 500x500, 63% of pixels gone) before WhatsApp's own recompression.
        # _square_target_for() never picks a size the image cannot fill, so the
        # padded "white postcard" failure of the old 900px try cannot recur.
        scale = min_fill / max(1, max(im.width, im.height))
        if scale > 1:
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.LANCZOS)
        im.thumbnail((target, target), Image.Resampling.LANCZOS)

        # If the asset is still much smaller than the canvas, don't send a giant
        # padded square. Use the image itself; WhatsApp can frame it less badly than us.
        if im.width < min_fill and im.height < min_fill:
            out_im = im
        else:
            canvas = Image.new("RGB", (target, target), (250, 250, 248))
            canvas.paste(im, ((target - im.width) // 2, (target - im.height) // 2))
            out_im = canvas
        PREPARED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        out = PREPARED_IMAGE_DIR / f"prepared_{src.stem}_{__import__('uuid').uuid4().hex}.jpg"
        out_im.save(out, quality=95, optimize=True, progressive=True)
        return str(out)
    except Exception:
        return media_path


def append_normalizer_feedback(source_body: str, published_copy: str, meta: dict, image_path: str, report: str,
                               content_delivered: bool, report_delivered: bool) -> None:
    """Append the exact normalized card that was published, never the source copy."""
    try:
        path = Path.home() / ".hermes/perfume_image_bank/logs/normalizer_feedback.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        approved = bool("✅ APROVADO" in (report or "") and "❌ NÃO APROVADO" not in (report or ""))
        row = {
            "ts": int(time.time()),
            "normalizer_version": (
                "headline-v3-personalized-lanes-title-v3"
                if config_value("PERFUME_HEADLINE_ARCHITECTURE", "v2").strip().lower() == "v3"
                else "headline-v2-title-v2"
            ),
            "link": extract_link(published_copy) or extract_link(source_body) or "",
            "product": meta.get("product", ""),
            "display_product": meta.get("display_product", ""),
            "published_copy": published_copy or "",
            "stage_timings": meta.get("stage_timings", {}),
            "normalization_seconds": meta.get("normalization_seconds"),
            "receipt_to_delivery_seconds": meta.get("receipt_to_delivery_seconds"),
            "source_body": source_body or "",
            # Extract from the card actually sent. The old code extracted from
            # source_body, corrupting Normalizer -> Edição comparisons.
            "headline": strip_wa(str(meta.get("headline") or "")).strip(),
            # Keep the same diagnostic fields as headline_audit: comparison and
            # the delivery report must not make a fallback look indistinguishable
            # from Luna/repertoire output.
            "headline_source": meta.get("headline_source", ""),
            "headline_reason": meta.get("headline_reason", ""),
            "headline_attempts": meta.get("headline_attempts", []),
            "headline_model": meta.get("headline_model", ""),
            "headline_options": meta.get("headline_options", []),
            "headline_judge_reason": meta.get("headline_judge_reason", ""),
            "price_checked": bool(meta.get("price_checked")),
            "price_source": meta.get("price_source", ""),
            "trust": meta.get("trust") or "",
            "image_path": image_path or "",
            "content_delivered": bool(content_delivered),
            "report_delivered": bool(report_delivered),
            "approval_schema_version": 2,
            "approval_checks": meta.get("approval_checks", {}),
            "approval_failed_checks": meta.get("approval_failed_checks", []),
            "approval_grade": meta.get("approval_grade", "unknown"),
            "approval_alerts": meta.get("approval_alerts", []),
            "approval_lightweight_status": meta.get("approval_lightweight_status", ""),
            "image_decision": meta.get("image_decision", {}),
            "report": report or "",
            "delivery_failed_checks": [name for name, ok in (("content", content_delivered), ("report", report_delivered)) if not ok],
            "store_evidence": {key: (meta.get("browser_card") or {}).get(key) for key in ("item_id", "product_url", "seller", "official_store", "pdp_seller", "pdp_seller_badged", "pdp_url")},
            "approved": approved and bool(content_delivered) and bool(report_delivered),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def send_media(chat_id: str, media_path: str, caption: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"MEDIA {media_path}\n{caption}")
        return True
    r = subprocess.run([GUARDED_SENDER, "--chat", chat_id, "--media", media_path, "--media-type", "image", "--caption", caption], text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        print((r.stdout or r.stderr).strip(), file=sys.stderr)
        return False
    print((r.stdout or "").strip())
    return True


def refresh_breno_approved_images() -> None:
    """Pull Breno's latest curation posts before resolving images.

    The normalizer runs on the intake group, while Breno approves final images in
    the edição/curation group. If we do not refresh here, the next promo can miss
    an image that was approved seconds earlier and fall back to the source media.
    Read-only mining: copies media into approved_final, never sends messages.
    """
    miner = Path.home() / ".hermes/scripts/perfume_breno_correction_miner.py"
    if not miner.exists():
        return
    try:
        run_dir = Path.home() / '.hermes/run'
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / 'perfume_intake_miner.log').open('a') as log:
            # The lock and timeout belong to the child, not to this interpreter.
            # A completed delivery can exit without killing curation or starting
            # overlapping miners on the next intake. No inherited run-lock FD.
            subprocess.run(
                ['flock', '-n', str(run_dir / 'perfume_intake_miner.lock'),
                 'timeout', '45', sys.executable, str(miner), '--group',
                 '120363991547992305@g.us', '--limit', '120'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                close_fds=True, start_new_session=True,
            )
    except Exception:
        # Do not block normalization because image-bank refresh hiccupped.
        pass


def prepare_delivery_artifacts(copy: str, meta: dict, incoming_media: str = "") -> tuple[str, str]:
    """Prepare per-message artifacts in the worker; never send from a worker."""
    started = time.monotonic()
    timings = meta.setdefault('stage_timings', {})
    canonical = str(meta.get('product') or '')
    canonical = str(meta.get('image_identity') or canonical)
    is_kit = bool(re.search(r'(?i)\b(?:kit|combo|conjunto)\b', canonical))
    image = ''
    for product in (meta.get('image_products') or [canonical]):
        product = str(product or '').strip()
        if is_kit and not re.search(r'(?i)\b(?:kit|combo|conjunto|casal)\b', product):
            continue
        tokens = [t for t in product_key(product).split('-') if len(t) >= 3 and not re.fullmatch(r'\d{2,3}ml', t)]
        if (not looks_like_product_line(product) or not tokens
                or (is_kit and not kit_identity_matches(canonical, product))
                or not identity_compatible(canonical, product, DISTINCTIVE_VARIANT_TOKENS, strict=True)
                or product_variant_conflict(canonical, product)
                or fakhar_variant_conflict(canonical, product)
                or identity_token_conflict(canonical, product)):
            continue
        product = preserve_gender(product, canonical)
        image = find_image(product, incoming_media, timings=timings, link=meta.get('link', ''))
        if image:
            break
    timings['image_lookup_seconds'] = round(time.monotonic() - started, 4)
    if (not image and incoming_media and Path(incoming_media).exists()
            and not image_identity_hold(incoming_media)
            and source_image_identity_proved(canonical, Path(incoming_media), str(meta.get('source_product') or ''))):
        image = incoming_media
    if image and image_identity_hold(image):
        meta['image_identity_review'] = image_identity_hold(image)
        image = ''
    report_image = image
    stage = time.monotonic()
    if image:
        image = prepare_image_for_send(image)
    timings['image_prepare_seconds'] = round(time.monotonic() - stage, 4)
    stage = time.monotonic()
    report = normalizer_report(meta, report_image or image, incoming_media)
    timings['image_report_seconds'] = round(time.monotonic() - stage, 4)
    timings['image_seconds'] = round(time.monotonic() - started, 4)
    meta['_image_report_path'] = report_image
    return image, report


def main() -> int:
    dry = "--dry-run" in sys.argv
    group = target_group()
    if not group:
        print(f"PERFUME_NORMALIZER_GROUP not configured. Put it in {CONFIG_FILE}", file=sys.stderr)
        return 2
    if not RULES_FILE.exists():
        print(f"rules file missing: {RULES_FILE}", file=sys.stderr)
        return 2
    msgs = collect_new_messages(group)
    if not msgs:
        return 0
    # Sliding admission keeps free workers useful while another offer is slow.
    # Persistent acknowledgements remain exclusively in the serial sender below.
    from perfume_intake_scheduler import admitted_results
    import threading as _thr

    # Mining Breno's Edição corrections is maintenance, not a prerequisite for
    # replying. The independent, locked child survives this daemon thread;
    # neither this round nor the next intake must wait for maintenance exit.
    miner_t = _thr.Thread(target=refresh_breno_approved_images, daemon=True)
    miner_t.start()

    def _norm(msg: dict, queued_at: float) -> tuple[str, dict]:
        b = (msg.get('body') or '').strip()
        media_path = str(msg.get('mediaPath') or '')
        if not b:
            return '', {}
        started = time.monotonic()
        worker_started_wall = time.time()
        try:
            copy, meta = normalize_copy(b, media_path)
            meta = dict(meta)
            meta['normalization_seconds'] = round(time.monotonic() - started, 4)
            timings = meta.setdefault('stage_timings', {})
            timings['worker_queue_seconds'] = round(max(0, started - queued_at), 4)
            received = msg_ts(msg.get('timestamp'))
            if received:
                timings['receipt_to_worker_seconds'] = round(max(0, worker_started_wall - received), 4)
            if not copy.startswith('Nao normalizei:'):
                try:
                    meta['_delivery_artifacts'] = prepare_delivery_artifacts(copy, meta, media_path)
                except Exception as exc:
                    # Visual failure must not discard an independently valid copy.
                    meta['image_prepare_error'] = str(exc)
                    meta['_delivery_artifacts'] = ('', 'Imagem NÃO APROVADA: falha na preparação.')
            meta['_delivery_ready_at'] = time.monotonic()
            return copy, meta
        except Exception as e:
            return (f"Nao normalizei: {e}", {})

    collect_more = (lambda: []) if dry else (lambda: collect_new_messages(group))
    for msg, result in admitted_results(msgs, collect_more, _norm):
        # Edição mining runs in parallel; image lookup uses the bank's current
        # approved state or the incoming promo image and replies immediately.
        body = (msg.get("body") or "").strip()
        meta: dict = {}
        if not body:
            copy = (
                "Recebi um evento desse grupo, mas o WhatsApp bridge entregou sem texto/caption "
                "(falha de descriptografia/sender-key). Manda a promo copiada como texto aqui de novo; "
                "se repetir, vou religar a sessão do WhatsApp."
            )
        else:
            copy, meta = result
        # 2026-07-07: headline audit trail. Answers "is the LLM writing these or
        # are we living on fallback?" without guessing. Append-only, tiny.
        if meta:
            try:
                audit = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "product": meta.get("product", ""),
                    "headline": next((l.strip() for l in copy.splitlines() if l.strip()), ""),
                    "headline_source": meta.get("headline_source", ""),
                    "normalization_seconds": meta.get("normalization_seconds"),
                    "stage_timings": meta.get("stage_timings", {}),
                    "headline_reason": meta.get("headline_reason", ""),
            "headline_attempts": meta.get("headline_attempts", []),
                    # Telemetry: which chain member answered, and every option
                    # the ranker saw (with score + veto flag). Lets a bad
                    # headline be attributed to the generator or to the ranker
                    # instead of guessed at.
                    "headline_model": meta.get("headline_model", ""),
                    "headline_judge_reason": meta.get("headline_judge_reason", ""),
                    "headline_options": meta.get("headline_options", []),
                }
                audit_path = Path(os.path.expanduser("~/.hermes/perfume_image_bank/headline_audit.jsonl"))
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                with audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(audit, ensure_ascii=False) + "\n")
            except Exception:
                pass
        incoming_media = msg.get("mediaPath") or ""
        image_path, report = meta.pop('_delivery_artifacts', ('', ''))
        image_report_path = meta.pop('_image_report_path', '')
        ready_at = meta.pop('_delivery_ready_at', time.monotonic())
        if meta:
            meta.setdefault('stage_timings', {})['delivery_queue_seconds'] = round(time.monotonic() - ready_at, 4)
        _send_started = time.monotonic()
        content_delivered = False
        if image_path:
            content_delivered = send_media(group, image_path, copy, dry_run=dry)
            media_delivered = content_delivered
            if not media_delivered:
                # A path on disk is not delivery evidence. Never approve a post
                # whose guarded media send failed.
                report = normalizer_report(meta, "", incoming_media)
        else:
            content_delivered = send_text(group, copy, dry_run=dry)
        report_delivered = True
        if report:
            report_delivered = send_text(group, report.strip(), dry_run=dry)
        if meta:
            meta.setdefault("stage_timings", {})["send_seconds"] = round(time.monotonic() - _send_started, 4)
            received_ts = msg_ts(msg.get("timestamp"))
            if received_ts:
                meta["receipt_to_delivery_seconds"] = round(time.time() - received_ts, 3)
        if meta and not dry:
            append_normalizer_feedback(
                body, copy, meta, image_report_path or image_path, report,
                content_delivered, report_delivered,
            )
        mark_processed(str(msg.get("messageId") or ""))
        time.sleep(1.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
