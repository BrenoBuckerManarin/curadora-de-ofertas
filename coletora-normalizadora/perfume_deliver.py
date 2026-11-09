#!/usr/bin/env python3
"""
Perfume GrupoFinal — pipeline completo sem LLM.
Coleta, valida e entrega promos no grupo WhatsApp com imagens.

Formato de entrega (2 mensagens por promo):
  1. Imagem da promo (se houver) com texto cru como legenda
  2. Validação: Link | Preço | Cupom | Fonte
"""

import datetime as dt
import json
import os
import random
import re
import unicodedata
import sys
import time
import urllib.request
from pathlib import Path
from functools import lru_cache
from perfume_round_runtime import run_exclusive
from perfume_identity_memo import identity_memo_scope

# ── Config ──────────────────────────────────────────────
TAP_FILE = os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl")
# Separate delivery state from the image collector. The collector/miner must not
# consume messages before the delivery pipeline can post them.
STATE_FILE = os.path.expanduser("~/.hermes/whatsapp/taps/.perfume_deliver_state.json")
GUARDED_SENDER = os.path.expanduser("~/.hermes/scripts/whatsapp_send_guarded.py")
BANK_DIR = Path.home() / ".hermes/perfume_image_bank"
APPROVED_ROOT = BANK_DIR / "products"
DELIVERY_GROUP = "120363993116679759@g.us"
# Keep today's candidates competing across the working shift instead of ranking
# only the latest four-minute burst. Availability and price are still checked
# live immediately before a candidate can occupy a delivery slot.
MAX_PROMO_AGE_SECONDS = 10 * 60 * 60
MAX_PROMOS_PER_RUN = 4  # contracted cadence: up to 4 promos per 4-minute round
PREVALIDATE_LIMIT = 20  # multi-axis shortlist; enough coverage without validating all candidates
VALIDATION_WORKERS = 4  # bounded parallelism: fast without hammering ML/Jina
# Cross-group duplicates are blocked only inside the same local calendar day.
# A perfume sent yesterday is eligible again today when a source reposts it;
# a rolling 18h TTL incorrectly suppressed morning offers after an evening post.
EDITORIAL_CONTEXT_FILE = BANK_DIR / "editorial_context.json"
COLLECTOR_PREFERENCES_FILE = BANK_DIR / "collector_preferences.json"
COLLECTOR_FUNNEL_FILE = BANK_DIR / "logs/collector_funnel_last.json"
NIGHT_SHIFT_OVERRIDE_FILE = BANK_DIR / "night_shift_collector_override.json"
_CANONICAL_PRODUCT_KEY_CACHE: dict[str, str] = {}
PRICE_HISTORY_FILE = BANK_DIR / "logs" / "collector_price_history.jsonl"

PERFUME_GROUPS = {
    "120363991092469570": "#84 FONTE ALFA | PERFUMES",
    "120363995307638674": "FONTE EPSILON | 346",
    "120363997084299777": "Fonte Zeta | 133",
    "120363999077277988": "FONTE EPSILON | 348",
    "120363998926675391": "FONTE BETA #27",
    "120363994228670851": "Fonte Eta 03",
    "120363997452979005": "Fonte Teta 3",
    "120363992376882261": "FONTE GAMA #45",
    "120363993556613173": "Fonte Delta #52",
}

# ── Helpers ─────────────────────────────────────────────

def send_whatsapp_text(chat_id: str, text: str):
    """Send plain text through the hard-allowlisted guarded sender."""
    import subprocess
    try:
        result = subprocess.run(
            [GUARDED_SENDER, "--chat", chat_id, "--text", text],
            text=True,
            capture_output=True,
            timeout=75,
        )
        if result.returncode == 0:
            try:
                receipt = json.loads((result.stdout or "{}").strip())
            except Exception:
                receipt = {"raw": (result.stdout or "").strip()[:500]}
            service_log(f"deliver: bridge_receipt type=text chat={chat_id!r} receipt={json.dumps(receipt, ensure_ascii=False)!r}")
            return True
        print(f"  ⚠️ guarded send_text failed: {result.stdout or result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ⚠️ send_text failed: {e}", file=sys.stderr)
        return False


def send_whatsapp_image(chat_id: str, image_path: str, caption: str = ""):
    """Send image through the hard-allowlisted guarded sender."""
    import subprocess
    if not os.path.exists(image_path):
        return False
    try:
        result = subprocess.run(
            [GUARDED_SENDER, "--chat", chat_id, "--media", image_path, "--media-type", "image", "--caption", caption],
            text=True,
            capture_output=True,
            timeout=75,
        )
        if result.returncode == 0:
            try:
                receipt = json.loads((result.stdout or "{}").strip())
            except Exception:
                receipt = {"raw": (result.stdout or "").strip()[:500]}
            service_log(f"deliver: bridge_receipt type=image chat={chat_id!r} receipt={json.dumps(receipt, ensure_ascii=False)!r}")
            return True
        print(f"  ⚠️ guarded send_image failed: {result.stdout or result.stderr}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ⚠️ send_image failed: {e}", file=sys.stderr)
        return False


def _message_epoch_seconds(raw_ts) -> int | None:
    """Normalize WhatsApp/Baileys timestamp shapes to epoch seconds."""
    if raw_ts is None:
        return None
    if isinstance(raw_ts, dict):
        raw_ts = raw_ts.get("low")
    try:
        ts = int(raw_ts)
    except (TypeError, ValueError):
        return None
    # Some systems emit milliseconds; WhatsApp normally emits seconds.
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def _is_today_and_recent(raw_ts, now: float | None = None) -> bool:
    """Accept only messages from local today and <= 90 minutes old."""
    ts = _message_epoch_seconds(raw_ts)
    if ts is None:
        return False
    now = time.time() if now is None else now
    age = now - ts
    if age < -300:  # future clock weirdness beyond 5min: don't trust it
        return False
    max_age_seconds = MAX_PROMO_AGE_SECONDS
    try:
        override = json.loads(NIGHT_SHIFT_OVERRIDE_FILE.read_text(encoding="utf-8"))
        if int(now) < int(override.get("expires_at") or 0):
            max_age_seconds = max(max_age_seconds, int(float(override.get("max_age_hours") or 0) * 3600))
    except Exception:
        pass
    try:
        # Breno 2026-09-25: the pool starts when he starts working; old morning
        # offers must not compete. File holds the freshness horizon in minutes.
        # Rollback: rm perfume_image_bank/collector_fresh_minutes
        fresh = int((BANK_DIR / "collector_fresh_minutes").read_text().strip())
        if fresh > 0:
            max_age_seconds = min(max_age_seconds, fresh * 60)
    except Exception:
        pass
    if age > max_age_seconds:
        return False
    msg_day = dt.datetime.fromtimestamp(ts).date()
    today = dt.datetime.fromtimestamp(now).date()
    if msg_day == today:
        return True
    # Explicit, expiring rescue mode for exceptional night shifts. The marker
    # never weakens identity, stock or price checks.
    try:
        override = json.loads(NIGHT_SHIFT_OVERRIDE_FILE.read_text(encoding="utf-8"))
        expires_at = int(override.get("expires_at") or 0)
        if bool(override.get("include_previous_day")) and int(now) < expires_at:
            return msg_day == today - dt.timedelta(days=1)
    except Exception:
        pass
    return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("seen_ids", [])
            state.setdefault("last_position", 0)
            state.setdefault("delivered_offer_keys", {})
            state.setdefault("delivery_progress", {})
            return state
    return {"seen_ids": [], "last_position": 0, "delivered_offer_keys": {}, "delivery_progress": {}}


def save_state(state):
    """Atomic state write so a killed run cannot corrupt the delivery cursor."""
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def mark_messages_evaluated(messages):
    """Mark rejected/invalid source messages seen without claiming delivery."""
    state = load_state()
    seen = set(state.get("seen_ids", []))
    for msg in messages:
        mid = msg.get("messageId", "")
        if mid:
            seen.add(mid)
    state["seen_ids"] = list(seen)[-5000:]
    save_state(state)


def delivery_progress(message_id: str) -> str:
    if not message_id:
        return ""
    state = load_state()
    row = (state.get("delivery_progress") or {}).get(message_id)
    return str((row or {}).get("stage") or "") if isinstance(row, dict) else str(row or "")


def set_delivery_progress(message_id: str, stage: str) -> None:
    if not message_id:
        return
    state = load_state()
    progress = dict(state.get("delivery_progress") or {})
    progress[message_id] = {"stage": stage, "ts": int(time.time())}
    # Partial progress older than the offer horizon is useless and noisy.
    cutoff = int(time.time()) - MAX_PROMO_AGE_SECONDS
    state["delivery_progress"] = {
        mid: row for mid, row in progress.items()
        if isinstance(row, dict) and int(row.get("ts") or 0) >= cutoff
    }
    save_state(state)


def clear_delivery_progress(message_ids: list[str]) -> None:
    if not message_ids:
        return
    state = load_state()
    progress = dict(state.get("delivery_progress") or {})
    for mid in message_ids:
        progress.pop(mid, None)
    state["delivery_progress"] = progress
    save_state(state)


def mark_messages_seen(messages):
    """Mark only messages that were actually attempted for delivery as seen."""
    state = load_state()
    seen = set(state.get("seen_ids", []))
    delivered_offer_keys = dict(state.get("delivered_offer_keys", {}) or {})
    now = int(time.time())
    for msg in messages:
        mid = msg.get("messageId", "")
        if mid:
            seen.add(mid)
        offer_keys = msg.get("offer_keys") or ([msg.get("offer_key")] if msg.get("offer_key") else normalize_offer_keys(msg.get("body") or ""))
        offer_keys = list(offer_keys)
        if msg.get("validation_ok") and msg.get("product"):
            offer_keys.append(validated_delivery_key(msg))
        for offer_key in offer_keys:
            if offer_key:
                delivered_offer_keys[offer_key] = now
    state["seen_ids"] = list(seen)[-5000:]
    state["delivered_offer_keys"] = prune_offer_keys(delivered_offer_keys, now)
    save_state(state)


def prune_offer_keys(keys: dict, now: int | None = None) -> dict:
    """Keep offer keys from the current local day only.

    Groups cannibalize each other heavily during a day, but yesterday's perfume
    is allowed to compete again today after live price/stock validation.
    """
    now = int(time.time()) if now is None else int(now)
    today = dt.datetime.fromtimestamp(now).date()
    out = {}
    for key, ts in (keys or {}).items():
        try:
            its = int(ts)
        except Exception:
            continue
        if dt.datetime.fromtimestamp(its).date() == today:
            out[str(key)] = its
    return out


def extract_link(body: str) -> str | None:
    m = re.search(r'https?://\S+', body or '')
    return m.group(0).rstrip('.,;:!?)>') if m else None


def normalize_offer_key(body: str) -> str:
    keys = normalize_offer_keys(body)
    return keys[0] if keys else ''


def normalize_offer_keys(body: str) -> list[str]:
    """Cross-group duplicate keys: canonical SKU, link, then SKU+price.

    Perfume groups repost the same deal with tiny headline/caption differences.
    MessageId is useless for dedupe; SKU identity must survive affiliate-link and
    cents-level price differences.
    """
    keys: list[str] = []
    link = extract_link(body or "")
    if link:
        link_key = re.sub(r'(?i)[?#].*$', '', link).rstrip('/')
        keys.append('link:' + link_key.lower())
    product = slugify_product(extract_product_name(body or ""))
    # Marketplace wrappers vary across groups and must not split one SKU into
    # separate dedupe identities ("➡️ X Oferta no Meli" vs plain "X").
    product = re.sub(r'^(?:oferta-)?', '', product)
    product = re.sub(r'-(?:oferta-no-meli|oferta-mercado-livre|no-meli|mercado-livre)$', '', product)
    if product and product != 'produto':
        keys.insert(0, f'product:{product}')
    price = extract_price(body or "") or ""
    price_key = re.sub(r'[^0-9]', '', price)
    if product and product != 'produto' and price_key:
        keys.append(f'product-price:{product}:{price_key}')
    seen: set[str] = set()
    return [k for k in keys if k and not (k in seen or seen.add(k))]


def message_media_path(msg: dict) -> str:
    """Return the actual media path from both old and current tap schemas."""
    media_path = str(msg.get("mediaPath") or "").strip()
    if media_path:
        return media_path
    media_urls = msg.get("mediaUrls") or []
    if isinstance(media_urls, list) and media_urls:
        return str(media_urls[0] or "").strip()
    media_url = str(msg.get("mediaUrl") or "").strip()
    return media_url


def collect_new_messages():
    """Read new messages from tap file. Returns list of dicts."""
    state = load_state()
    seen = set(state.get("seen_ids", []))
    delivered_offer_keys = prune_offer_keys(dict(state.get("delivered_offer_keys", {}) or {}))
    batch_offer_keys: set[str] = set()
    batch_key_to_index: dict[str, int] = {}
    last_pos = state.get("last_position", 0)
    new_msgs = []

    if not os.path.exists(TAP_FILE):
        return new_msgs, state

    with open(TAP_FILE) as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        if file_size < last_pos:
            last_pos = 0
            seen = set()
        f.seek(last_pos)

        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mid = msg.get("messageId", "")
            if mid in seen:
                continue

            chat_id = msg.get("chatId", "").replace("@g.us", "").replace("@s.whatsapp.net", "")
            if chat_id not in PERFUME_GROUPS:
                continue

            # Hard freshness gate: only today's promos, max 1h30 old.
            # Mark stale source messages as seen so a resumed/lagging cron won't post old leftovers later.
            if not _is_today_and_recent(msg.get("timestamp")):
                seen.add(mid)
                continue

            body = (msg.get("body") or "").strip()
            if not body and not msg.get("hasMedia"):
                continue

            media_path = message_media_path(msg)
            offer_keys = normalize_offer_keys(body)
            delivered_duplicate_key = next((k for k in offer_keys if k in delivered_offer_keys), "")
            if delivered_duplicate_key:
                seen.add(mid)
                service_log(f"deliver: duplicate_skip key={delivered_duplicate_key} source={PERFUME_GROUPS.get(chat_id, chat_id)!r}")
                continue

            batch_duplicate_key = next((k for k in offer_keys if k in batch_offer_keys), "")
            if batch_duplicate_key:
                existing_idx = batch_key_to_index.get(batch_duplicate_key)
                existing = new_msgs[existing_idx] if existing_idx is not None and existing_idx < len(new_msgs) else None
                # Groups can post the same deal with and without media. Prefer the
                # real source image when available instead of letting a text-only
                # duplicate block the later photo post.
                if existing_idx is not None and existing is not None and media_path and not existing.get("mediaPath"):
                    replacement = {
                        "messageId": mid,
                        "offer_key": offer_keys[0] if offer_keys else "",
                        "offer_keys": offer_keys,
                        "chatId": chat_id,
                        "groupName": PERFUME_GROUPS.get(chat_id, chat_id),
                        "body": body,
                        "hasMedia": msg.get("hasMedia", False),
                        "mediaPath": media_path,
                        "timestamp": msg.get("timestamp", 0),
                    }
                    new_msgs[existing_idx] = replacement
                    for offer_key in offer_keys:
                        batch_key_to_index[offer_key] = existing_idx
                    seen.add(str(existing.get("messageId", "")))
                    service_log(f"deliver: duplicate_replace_with_media key={batch_duplicate_key} source={PERFUME_GROUPS.get(chat_id, chat_id)!r}")
                    continue
                seen.add(mid)
                service_log(f"deliver: duplicate_skip key={batch_duplicate_key} source={PERFUME_GROUPS.get(chat_id, chat_id)!r}")
                continue

            msg_index = len(new_msgs)
            for offer_key in offer_keys:
                batch_offer_keys.add(offer_key)
                batch_key_to_index[offer_key] = msg_index

            new_msgs.append({
                "messageId": mid,
                "offer_key": offer_keys[0] if offer_keys else "",
                "offer_keys": offer_keys,
                "chatId": chat_id,
                "groupName": PERFUME_GROUPS.get(chat_id, chat_id),
                "body": body,
                "hasMedia": msg.get("hasMedia", False),
                "mediaPath": media_path,
                "timestamp": msg.get("timestamp", 0),
            })

        # If there are eligible messages, keep the file cursor where it was.
        # The sender caps each run at MAX_PROMOS_PER_RUN; advancing to EOF here
        # would silently discard the rest of a burst/backlog.
        if new_msgs:
            last_pos = state.get("last_position", 0)
        else:
            last_pos = f.tell()

    state["seen_ids"] = list(seen)[-5000:]
    state["delivered_offer_keys"] = delivered_offer_keys
    state["last_position"] = last_pos
    save_state(state)
    return new_msgs, state


# ── Price & Cupom extraction (deterministic) ────────────

def _strip_wa(text: str) -> str:
    return re.sub(r'[_*~]', '', text)


def extract_price(body: str) -> str | None:
    if not body:
        return None
    clean = _strip_wa(body)
    # Find line with both "por" and "R$"
    for line in clean.split('\n'):
        if re.search(r'(?i)(?:por|preço|preco)\s*:?\s*.*R[$]', line):
            # If a line says "De R$199 por R$100", the promo price is the last
            # money value on that line, not the first. This also strengthens
            # product+price dedupe across groups with slightly different copy.
            matches = re.findall(r'R[$]\s*(\d[\d.,]*)', line)
            if matches:
                value = matches[-1].strip()
                value = re.sub(r'\.(?=\d{3})', '', value)
                return f"R${value}"
    # Fallback: first R$ in text
    m = re.search(r'R[$]\s*(\d[\d.,]*)', clean)
    if m:
        value = m.group(1).strip()
        value = re.sub(r'\.(?=\d{3})', '', value)
        return f"R${value}"
    return None


def extract_cupom(body: str) -> str | None:
    if not body:
        return None
    clean = _strip_wa(body)
    m = re.search(r'(?i)(?:cupom|cupom|código|codigo)\s*:?\s*(?:aplicar\s*)?(\S+)', clean)
    if m:
        cupom = m.group(1).strip().rstrip('.,;:!').strip('`')
        if cupom.lower() in ('não', 'nao', 'nenhum', 'sem', 'não menciona', 'não informado'):
            return None
        if cupom.startswith('R$') or cupom.startswith('r$'):
            return None
        if cupom.endswith('%') or cupom.replace('%', '').isdigit():
            return None
        if re.search(r'(?i)\bOFF\b', cupom):
            return None
        if len(cupom) < 3 or len(cupom) > 30:
            return None
        return cupom
    return None


def _looks_like_non_product_line(line: str) -> bool:
    return bool(re.search(
        r'(?i)^(de\b|por\b|cupom\b|use o cupom|https?://|selecion|vendido|loja\b|link\b|pre[çc]o\b|dupe\b|contratipo\b|inspirado\b|🔥|🏷️|🎟️|✅|❌|👉|🛒|🔗|💰)',
        line.strip(),
    ))


def extract_product_name(body: str) -> str:
    """Extract the actual product line, not the promo headline."""
    pmn = _load_normalizer()
    if pmn is not None:
        try:
            shared = str(pmn.extract_product(body) or "").strip()
            if shared and shared != "Produto":
                return shared[:110]
        except Exception:
            pass
    clean = _strip_wa(body)
    lines = [line.strip() for line in clean.split('\n') if line.strip()]

    # Prefer factual product lines with volume/concentration. This avoids using
    # headlines like "TÁ MUITO BARATO" as the image-bank key.
    for line in lines:
        if _looks_like_non_product_line(line):
            continue
        if re.search(r'(?i)\b\d{2,3}\s*ml\b', line) or re.search(r'(?i)\b(edp|edt|parfum|perfume|col[oô]nia)\b', line):
            return line[:80]

    # Fallback: first meaningful non-operational line.
    for line in lines:
        if not _looks_like_non_product_line(line) and len(line) > 5 and not line.startswith('~'):
            return line[:80]
    return "Produto"


def slugify_product(s: str) -> str:
    s = re.sub(r'\s+', ' ', (s or '').replace('*', '').replace('`', '').replace('~', '')).strip().lower()
    s = re.sub(r'[^a-z0-9à-ÿ]+', '-', s).strip('-')
    s = unicodedata.normalize('NFKD', s).encode('ascii','ignore').decode()
    s = re.sub(r'-+', '-', s).strip('-')
    return s[:90] or 'unknown-product'


@lru_cache(maxsize=4096)
def _approved_files_for_key(product_key: str) -> list[Path]:
    """Return only strict final assets; never candidate/approved/sheets/quarantine."""
    approved_dir = APPROVED_ROOT / product_key / "approved_final"
    if not approved_dir.is_dir():
        return []
    files = []
    for path in approved_dir.iterdir():
        if path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}:
            # Defensive guard: approved_final is the only allowed source.
            pstr = str(path)
            if all(block not in pstr for block in ('/quarantine/', '/sheets/', '/final_500/', '/candidate/', '/approved/')):
                files.append(path)
    def ranked(path: Path) -> tuple[int, int, float, str]:
        """Prefer the best source image at send time, not just the newest file.

        Breno approved using the promo-edition photos as final assets, but when a
        product has several approved finals the sender should pick the highest
        resolution version first. File size is a decent fallback for JPEG/WebP
        detail when Pillow is unavailable or the image is corrupt.
        """
        pixels = 0
        try:
            from PIL import Image

            with Image.open(path) as im:
                pixels = int(im.width) * int(im.height)
        except Exception:
            pixels = 0
        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except Exception:
            size = 0
            mtime = 0.0
        return pixels, size, mtime, path.name

    return sorted(files, key=ranked, reverse=True)


def _load_normalizer():
    """Import the manual normalizer as a module for shared bank lookup + Jina cache.

    Unifies collector/normalizer image-bank resolution (aliases, family
    expansions, variant guards) so 'Banco ✅/❌' in the collector report matches
    what the normalizer will actually do. Falls back to legacy local logic when
    import fails, so the collector never dies because of normalizer changes.
    """
    global _NORMALIZER
    if _NORMALIZER is not None:
        return _NORMALIZER or None
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
        import perfume_manual_normalizer as pmn  # type: ignore
        _NORMALIZER = pmn
    except Exception as e:
        service_log(f"deliver: normalizer_import_failed {e}")
        _NORMALIZER = False
    return _NORMALIZER or None


_NORMALIZER = None


# Tokens that make two bottles DIFFERENT products even when the family name is
# identical (Malbec Vert vs Malbec Tradicional, Rayhaan Lion vs Tiger). A fuzzy
# name match that crosses one of these publishes the wrong bottle to the client,
# so it must never be allowed to pick a bank image.
VARIANT_TOKENS = {
    'vert', 'bleu', 'blue', 'black', 'icon', 'gold', 'ice', 'feu', 'fire',
    'rush', 'lion', 'tiger', 'sable', 'silver', 'red', 'rouge', 'noir',
    'intense', 'elixir', 'extreme', 'absolu', 'sport', 'summer', 'night',
    'blanc', 'white', 'magnetic', 'signature', 'pure', 'ultra', 'prive',
    'privee', 'aqua', 'marine', 'oud', 'energy', 'flame', 'verde', 'green',
    'sagaz', 'verum', 'essence', 'iconic', 'legacy', 'platinum', 'titanium',
}
# Neutral markers: their absence/presence must not split one SKU in two, since
# groups write "Malbec 100ml" and the bank stores "malbec-tradicional-100ml".
VARIANT_BASE_TOKENS = {'tradicional', 'original', 'classic', 'classico'}
_VARIANT_ALIASES = {'bleu': 'blue', 'blanc': 'white', 'rouge': 'red', 'verde': 'vert', 'green': 'vert', 'prive': 'privee'}


def _variant_signature(text: str) -> frozenset:
    """Distinctive variant tokens of a product name or bank slug."""
    parts = re.split(r'[^a-z0-9]+', _strip_accents(str(text or "")).lower())
    return frozenset(
        _VARIANT_ALIASES.get(p, p) for p in parts
        if p in VARIANT_TOKENS and p not in VARIANT_BASE_TOKENS
    )


def _volume_signature(text: str) -> str:
    m = re.search(r'(\d{2,4})\s*ml\b', str(text or ""), re.I)
    return m.group(1) if m else ""


def _bank_match_ok(product: str, bank_key: str) -> tuple[bool, str]:
    """Reject a bank folder whose variant/volume contradicts the requested SKU."""
    want_v, got_v = _variant_signature(product), _variant_signature(bank_key)
    if want_v != got_v:
        return False, f"variante {sorted(want_v) or '∅'}≠{sorted(got_v) or '∅'}"
    want_vol, got_vol = _volume_signature(product), _volume_signature(bank_key)
    if want_vol and got_vol and want_vol != got_vol:
        return False, f"volume {want_vol}≠{got_vol}"
    return True, ""


def _strip_accents(s: str) -> str:
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


@lru_cache(maxsize=1024)
def resolve_approved_image(product: str) -> tuple[str | None, str]:
    """Find a reusable approved bank image for the product.

    Primary path: the manual normalizer's own lookup (approved_final_candidates),
    so collector and normalizer agree on bank coverage. Legacy strict token match
    remains as fallback only.

    Every candidate folder passes _bank_match_ok first: name similarity alone is
    not enough, because the bank holds dozens of same-family variants and a fuzzy
    hit across them means publishing the wrong bottle. No safe image => no image,
    which is the intended outcome (sem foto, não aprovar).
    """
    pmn = _load_normalizer()
    if pmn is not None:
        try:
            for tier_dir in pmn.approved_final_candidates(product):
                img = pmn.first_file_in_dir(tier_dir)
                if not img:
                    continue
                ok, why = _bank_match_ok(product, tier_dir.parent.name)
                if not ok:
                    service_log(
                        f"deliver: bank_variant_reject product={product!r} "
                        f"bank={tier_dir.parent.name!r} reason={why}"
                    )
                    continue
                return img, f"normalizer:{tier_dir.parent.name}/{tier_dir.name}"
        except Exception as e:
            service_log(f"deliver: normalizer_lookup_failed product={product!r} {e}")

    product_key = canonical_product_key(product)
    candidates = _approved_files_for_key(product_key)
    if candidates:
        return str(candidates[0]), f"approved-final:{product_key}"

    terms = [t for t in re.split(r'[-\s]+', product_key) if len(t) > 2]
    if not terms:
        return None, "no-approved:no-key"

    matches: list[tuple[int, float, str, Path]] = []
    for product_dir in APPROVED_ROOT.iterdir() if APPROVED_ROOT.exists() else []:
        if not product_dir.is_dir():
            continue
        approved = _approved_files_for_key(product_dir.name)
        if not approved:
            continue
        key = product_dir.name
        hit_count = sum(1 for term in terms if term in key)
        # Strict enough to avoid random Black/EDP/100ml collisions.
        required = max(2, min(4, len(terms)))
        if hit_count < required:
            continue
        # Token overlap says "same family", not "same bottle". Malbec Vert and
        # Malbec Tradicional share every token but one, so the variant/volume
        # signature has the final word.
        ok, why = _bank_match_ok(product, key)
        if not ok:
            service_log(f"deliver: bank_variant_reject_fuzzy product={product!r} bank={key!r} reason={why}")
            continue
        # Prefer canonical directory names that include the volume/concentration terms too.
        score = hit_count / max(len(terms), 1)
        matches.append((hit_count, score, key, approved[0]))

    if not matches:
        return None, f"no-approved:{product_key}"
    matches.sort(key=lambda x: (x[0], x[1], x[3].stat().st_mtime), reverse=True)
    best_key = matches[0][2]
    best_files = _approved_files_for_key(best_key)
    return str(best_files[0]), f"approved-final-fuzzy:{best_key}"


def has_editorial_context(product: str) -> bool:
    """True when the manual normalizer has product context/headline seeds."""
    try:
        data = json.loads(EDITORIAL_CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    key = slugify_product(product)
    if key in data:
        return True
    terms = {t for t in re.split(r'[-\s]+', key) if len(t) > 2 and t not in {'perfume', 'arabe', 'edp', 'edt', '100ml', '90ml'}}
    if not terms:
        return False
    for ctx_key in data.keys():
        ctx_terms = {t for t in re.split(r'[-\s]+', str(ctx_key)) if len(t) > 2}
        if len(terms & ctx_terms) >= min(3, len(terms)):
            return True
    return False


def parse_price_number(price: str | None) -> float | None:
    if not price:
        return None
    digits = re.sub(r'[^0-9,.]', '', price)
    if not digits:
        return None
    if ',' in digits and digits.rfind(',') > digits.rfind('.'):
        digits = digits.replace('.', '').replace(',', '.')
    else:
        digits = digits.replace(',', '')
    try:
        return float(digits)
    except Exception:
        return None


def load_collector_preferences() -> dict:
    try:
        return json.loads(COLLECTOR_PREFERENCES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def canonical_product_key(product: str) -> str:
    """One SKU identity across ranking, history, consensus and normalizer.
    
    Base-variant aliases (tradicional/original/classic) are stripped so
    'Eudora H' and 'Eudora H Tradicional' dedupe as one SKU.
    """
    raw = str(product or "")
    if raw in _CANONICAL_PRODUCT_KEY_CACHE:
        return _CANONICAL_PRODUCT_KEY_CACHE[raw]
    pmn = _load_normalizer()
    try:
        key = pmn.product_key(raw) if pmn is not None else ""
    except Exception:
        key = ""
    key = key or slugify_product(raw)
    # Strip neutral base tokens that don't distinguish variants
    base_tokens_pattern = r'\b(tradicional|original|classic|classico)\b'
    key = re.sub(base_tokens_pattern, '', key)
    key = re.sub(r'-{2,}', '-', key).strip('-')
    if len(_CANONICAL_PRODUCT_KEY_CACHE) > 4096:
        _CANONICAL_PRODUCT_KEY_CACHE.clear()
    _CANONICAL_PRODUCT_KEY_CACHE[raw] = key
    return key


def classify_offer_category(product: str, body: str) -> str:
    # Category belongs to the identified product, not to comparisons/editorial
    # prose in the source body ("inspirado no Natura..." contaminated Salvo).
    text = _strip_wa(product or "").lower()
    fallback = _strip_wa(body or "").lower()
    if len(text) < 5 or text in {"produto", "perfume"}:
        text = fallback
    if re.search(r'\bkit\b|\bcombo\b', text):
        return "kit"
    if re.search(r'body\s*splash|splash corporal|body\s*spray|desodorante corporal', text):
        return "body_splash"
    if any(x in text for x in ('natura', 'boticário', 'boticario', 'eudora', 'jequiti', 'avon', 'granado', 'the blend', 'floratta', 'egeo', 'zaad', 'kaiak', 'malbec', 'quasar', 'club 6', 'botica 214')):
        return "national"
    if any(x in text for x in ('lattafa', 'afnan', 'rasasi', 'armaf', 'maison alhambra', 'french avenue', 'rayhaan', 'mawwal', 'al wataniah', 'khadlaj', 'bidaya', 'árabe', 'arabe', 'fakhar', 'asad', 'salvo', 'bareq al dhahab', 'club de nuit', 'hawas', 'turathi', 'liquid brun', 'khamrah', 'qaed fursan', 'radio 100ml')):
        return "arabic"
    if any(x in text for x in ('armani', 'azzaro', 'ysl', 'dior', 'versace', 'rabanne', 'paco rabanne', 'jean paul', 'chanel', 'prada', 'carolina herrera', 'givenchy', 'dolce', 'gabbana', 'mercedes', 'ralph lauren', 'hugo boss', 'tommy hilfiger', 'montblanc', 'bvlgari', 'issey miyake', 'lacoste', 'davidoff', 'nautica', 'jimmy choo', 'calvin klein', 'ck one', 'stronger with you', 'acqua di gio', 'acqua di giò', 'le male', 'ultra male', '212 men', '212 vip', 'boss bottled', 'the one', 'gentleman society', 'gentleman bois', 'cedrat boise')):
        return "designer"
    if any(x in text for x in ('antonio banderas', 'banderas', 'paris elysees', 'shakira', 'benetton', 'cuba')):
        return "budget_imported"
    return "other"


def kit_item_count(product: str, body: str = "") -> int | None:
    text = _strip_wa(f"{product}\n{body}").lower()
    if not re.search(r"\b(?:kit|combo|conjunto)\b", text):
        return None
    patterns = (
        r"\b(?:kit|combo|conjunto)\s+(?:com\s+|de\s+)?(\d{1,2})\b",
        r"\b(\d{1,2})\s*(?:itens|produtos|pe[çc]as|unidades)\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if m and 2 <= int(m.group(1)) <= 20:
            return int(m.group(1))
    first_line = next((x for x in text.splitlines() if x.strip()), "")
    plus_count = first_line.count("+")
    return plus_count + 1 if 1 <= plus_count <= 9 else None


def preference_score(product: str, body: str, prefs: dict) -> tuple[int, list[str]]:
    product_text = _strip_wa(product or "").lower()
    body_text = _strip_wa(body or "").lower()
    reasons: list[str] = []
    product_hits = [(term, int(weight)) for term, weight in (prefs.get("favorite_products") or {}).items() if term.lower() in product_text]
    brand_hits = [(term, int(weight)) for term, weight in (prefs.get("favorite_brands") or {}).items() if term.lower() in product_text]
    score = 0
    if product_hits:
        term, weight = max(product_hits, key=lambda x: x[1]); score += weight; reasons.append(f"favorito:{term}")
    else:
        # Inspiration/comparison is commercially useful, but weaker than the
        # actual favorite product. Never grant full favorite weight from prose.
        association_hits = [(term, int(weight)) for term, weight in (prefs.get("favorite_products") or {}).items() if term.lower() in body_text]
        if association_hits:
            term, weight = max(association_hits, key=lambda x: x[1]); association = min(5, max(2, weight // 4)); score += association; reasons.append(f"associação:{term}")
    if brand_hits:
        term, weight = max(brand_hits, key=lambda x: x[1]); score += weight; reasons.append(f"marca:{term}")
    fmt = prefs.get("format_bonus") or {}
    if re.search(r'\bkit\b', product_text):
        score += int(fmt.get("kit", 18)); reasons.append("kit")
    elif re.search(r'\bcombo\b', product_text):
        score += int(fmt.get("combo", 16)); reasons.append("combo")
    if re.search(r'\b(?:200|250|300)\s*ml\b', product_text):
        score += int(fmt.get("200ml_or_more", 5)); reasons.append("volume grande")
    return score, reasons


def commercial_comparison_context(msg: dict) -> str:
    """Conservative equivalence: same link and same stated purchase conditions.

    No seller/freight inference across different listings. Unknown or coupon
    prices remain rankable, but cannot override a peer purely on price.
    """
    body = str(msg.get("body") or "")
    link = extract_link(body) or msg.get("link") or ""
    if not link or not msg.get("validation_ok") or msg.get("price_confidence") != "high_public_match":
        return ""
    condition_lines = sorted({line.strip().lower() for line in body.splitlines()
                              if re.search(r"(?i)pix|cupom|cart[aã]o|primeira|conta|app|frete|assin|clube|m[ií]nim|parcela|juros|vendedor|loja|\d+\s*x", line)})
    return json.dumps([link, condition_lines, msg.get("seller_id"), msg.get("shipping_price")], ensure_ascii=False)


def recent_product_prices(product_key: str, max_rows: int = 5000, comparison_context: str = "") -> list[float]:
    if not product_key or not PRICE_HISTORY_FILE.exists():
        return []
    prices: list[float] = []
    unique_observations: set[tuple[float, str]] = set()
    try:
        lines = PRICE_HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-max_rows:]
        cutoff = int(time.time()) - 60 * 24 * 3600
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            row_key = str(row.get("product_key") or "")
            if row.get("product"):
                row_key = canonical_product_key(str(row.get("product") or ""))
            if row_key != product_key or int(row.get("ts") or 0) < cutoff:
                continue
            if comparison_context and row.get("comparison_context") != comparison_context:
                continue
            value = row.get("effective_price")
            if isinstance(value, (int, float)) and value > 0:
                signature = (round(float(value), 2), str(row.get("link") or ""))
                if signature in unique_observations:
                    continue
                unique_observations.add(signature)
                prices.append(float(value))
    except Exception:
        return []
    return prices


def validated_opportunity_score(msg: dict) -> dict:
    """Re-rank a validated offer by audience fit and evidence-backed price quality."""
    out = dict(msg)
    prefs = load_collector_preferences()
    product = out.get("product") or ""
    body = out.get("body") or ""
    category = classify_offer_category(product, body)
    source_num = parse_price_number(out.get("price"))
    page_num = parse_price_number(out.get("validated_price"))
    pref_score, reasons = preference_score(product, body, prefs)
    curation_v2 = curation_v2_enabled()
    score = 40 + pref_score

    bands = (prefs.get("category_price_bands") or {}).get(category) or (prefs.get("category_price_bands") or {}).get("other", {})
    effective = source_num or page_num
    item_count = kit_item_count(product, body) if category == "kit" else None
    if effective is not None and item_count:
        per_item = effective / item_count
        if per_item <= 80:
            score += 22; reasons.extend([f"kit:{item_count} itens", f"R${per_item:.0f}/item"])
        elif per_item <= 120:
            score += 12; reasons.extend([f"kit:{item_count} itens", f"R${per_item:.0f}/item"])
    elif effective is not None:
        excellent = float(bands.get("excellent", 0) or 0)
        good = float(bands.get("good", 0) or 0)
        if excellent and effective <= excellent:
            depth_bonus = min(12, int(round(((excellent - effective) / excellent) * 50)))
            score += 22 + depth_bonus
            reasons.append(f"preço excelente:{category}")
            if depth_bonus:
                reasons.append(f"muito abaixo da faixa:+{depth_bonus}")
        elif good and effective <= good:
            score += 12; reasons.append(f"preço bom:{category}")

    inferred_discount = None
    if source_num and page_num and source_num < page_num:
        inferred_discount = (1 - source_num / page_num) * 100
        discount_bands = prefs.get("discount_bands") or {}
        suspicious = float(discount_bands.get("suspicious_pct", 35))
        # Source/page gap is a lead, not a proven discount. Preserve the
        # opportunity (price bands still apply), but cap this uncertain bonus
        # below the public-match bonus rather than rewarding a larger mismatch.
        if inferred_discount >= float(discount_bands.get("minimum_signal_pct", 5)):
            score += 6
            reasons.append(f"vantagem anunciada não validada:{inferred_discount:.0f}%")
            reasons.append("cupom não confirmado")
    elif source_num and page_num and source_num > page_num * 1.02:
        score -= 10; reasons.append("fonte mais cara que ML")
    elif source_num and page_num:
        score += 8; reasons.append("preço confirmado")

    product_key = canonical_product_key(product)
    public_match = bool(source_num and page_num and abs(source_num - page_num) <= max(2.0, page_num * 0.02))
    comparison_context = commercial_comparison_context(dict(out, price_confidence="high_public_match" if public_match else "unknown"))
    if curation_v2:
        # Each affiliate group has its own meli.la link, so "same link" never
        # accumulated history (0/3822 offers since 2026-09-04). Compare the
        # same product across sources, trusting only publicly matched prices,
        # one observation per source link per day.
        history = product_price_baseline(product_key)
    else:
        history = recent_product_prices(product_key, comparison_context=comparison_context) if comparison_context else []
    deal_pct = None
    # Five distinct comparable observations is a conservative initial floor,
    # not a claim of a market-wide minimum. Legacy rows remain on disk.
    if curation_v2 and effective and len(history) >= 5:
        ordered = sorted(history)
        midpoint = len(ordered) // 2
        median = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
        deal_pct = (1 - effective / median) * 100
        if deal_pct >= 30:
            score += 40; reasons.append(f"{deal_pct:.0f}% abaixo do normal")
        elif deal_pct >= 20:
            score += 30; reasons.append(f"{deal_pct:.0f}% abaixo do normal")
        elif deal_pct >= 10:
            score += 18; reasons.append(f"{deal_pct:.0f}% abaixo do normal")
        elif deal_pct >= 5:
            score += 8; reasons.append(f"{deal_pct:.0f}% abaixo do normal")
        elif deal_pct <= -10:
            score -= 25; reasons.append(f"{-deal_pct:.0f}% acima do normal")
        elif deal_pct <= -5:
            score -= 12; reasons.append(f"{-deal_pct:.0f}% acima do normal")
        if deal_pct <= -5 and pref_score > 0:
            # Breno 2026-09-24: favorite at an above-normal price is not an offer.
            score -= pref_score; reasons.append("favorito ignorado: preço acima do normal")
        if effective < ordered[0] * 0.98:
            score += 8; reasons.append("menor histórico local")
    elif effective and len(history) >= 5:
        ordered = sorted(history)
        midpoint = len(ordered) // 2
        median = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
        previous_min = ordered[0]
        if effective < previous_min * 0.98:
            score += 18; reasons.append("menor histórico local")
        elif effective <= median * 0.90:
            score += 12; reasons.append("abaixo da mediana")
        elif effective > median * 1.10:
            score -= 8; reasons.append("acima da mediana")

    verified = verified_seller_signal(out)
    if verified and os.environ.get("PERFUME_COLLECTOR_VERIFIED_BONUS", "1").strip() != "0":
        # Breno 2026-09-24 (ordem explícita): verified/official sellers are highly
        # valued by the audience. On by default; kill: PERFUME_COLLECTOR_VERIFIED_BONUS=0.
        score += 15; reasons.append(f"vendedor verificado:{verified}")

    # Media readiness is reported separately and remains a batch tie-breaker;
    # it is not evidence that the commercial opportunity is better.
    if out.get("bank_image"):
        reasons.append("foto banco")
    if out.get("source_media"):
        reasons.append("foto fonte")
    price_confidence = "unknown"
    if source_num and page_num:
        if inferred_discount is not None and inferred_discount > float((prefs.get("discount_bands") or {}).get("suspicious_pct", 35)):
            price_confidence = "low_coupon_unconfirmed"
        elif abs(source_num - page_num) <= max(2.0, page_num * 0.02):
            price_confidence = "high_public_match"
        else:
            price_confidence = "medium_coupon_plausible"
    out.update({
        "offer_category": category,
        "audience_score": pref_score,
        "price_confidence": price_confidence,
        "inferred_discount_pct": round(inferred_discount, 1) if inferred_discount is not None else None,
        "effective_price": effective,
        "history_count": len(history),
        "deal_pct": round(deal_pct, 1) if deal_pct is not None else None,
        "verified_seller": verified or "",
        "curation_version": "v2" if curation_v2 else "v1",
        "opportunity_score": score,
        "opportunity_reasons": reasons,
    })
    return out


CURATION_V2_FLAG = BANK_DIR / "collector_curation_v2.enabled"


def curation_v2_enabled() -> bool:
    """Deal-vs-normal curation. On: env PERFUME_COLLECTOR_CURATION=v2 or flag file.

    Env PERFUME_COLLECTOR_CURATION=v1 forces the old behavior (kill switch that
    wins over the file). Rollback: rm perfume_image_bank/collector_curation_v2.enabled
    """
    env = os.environ.get("PERFUME_COLLECTOR_CURATION", "").strip().lower()
    if env in {"v1", "v2"}:
        return env == "v2"
    return CURATION_V2_FLAG.exists()


_BASELINE_CACHE: dict = {"mtime": None, "by_key": {}}


def product_price_baseline(product_key: str, days: int = 60) -> list[float]:
    """Publicly matched prices for this product, one per (source link, day), last `days` days."""
    if not product_key or not PRICE_HISTORY_FILE.exists():
        return []
    mtime = PRICE_HISTORY_FILE.stat().st_mtime
    if _BASELINE_CACHE["mtime"] != mtime:
        by_key: dict[str, dict[tuple[str, str], float]] = {}
        cutoff = int(time.time()) - days * 24 * 3600
        with PRICE_HISTORY_FILE.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = int(row.get("ts") or 0)
                if ts < cutoff or row.get("price_confidence") != "high_public_match":
                    continue
                value = row.get("effective_price")
                if not isinstance(value, (int, float)) or value <= 0:
                    continue
                key = canonical_product_key(str(row.get("product") or "")) or str(row.get("product_key") or "")
                obs_key = (str(row.get("link") or ""), time.strftime("%F", time.localtime(ts)))
                by_key.setdefault(key, {})[obs_key] = float(value)
        _BASELINE_CACHE.update(mtime=mtime, by_key=by_key)
    return list(_BASELINE_CACHE["by_key"].get(product_key, {}).values())


_VERIFIED_STORES_CACHE: dict = {"mtime": None, "stores": {}}
NORMALIZER_FEEDBACK_FILE = BANK_DIR / "logs" / "normalizer_feedback.jsonl"


def _seller_norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _strip_accents(str(name or "")).lower())


def known_verified_stores() -> dict[str, str]:
    """Stores the normalizer's real Chrome already proved official (blue badge on the exact card).

    Jina text never carries the badge (it is an SVG: 0/1231 cached pages), so the
    collector cannot see it directly. Learned from normalizer_feedback trust lines.
    """
    if not NORMALIZER_FEEDBACK_FILE.exists():
        return {}
    mtime = NORMALIZER_FEEDBACK_FILE.stat().st_mtime
    if _VERIFIED_STORES_CACHE["mtime"] != mtime:
        stores: dict[str, str] = {}
        with NORMALIZER_FEEDBACK_FILE.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"Loja Oficial \*(.+?)\* no ML", line)
                if m:
                    name = m.group(1).strip()
                    if _seller_norm(name) and _seller_norm(name) != "mercadolivre":
                        stores.setdefault(_seller_norm(name), name)
        _VERIFIED_STORES_CACHE.update(mtime=mtime, stores=stores)
    return _VERIFIED_STORES_CACHE["stores"]


def verified_seller_signal(msg: dict) -> str:
    """Return the store name when the validated ML card's seller is a proven official store.

    Only the validated page counts (Jina text of the exact card) matched against
    stores already proven official by the rendered-card probe. A source-group
    claim alone ("Loja oficial!") is ignored: sources say that to sell.
    """
    jina = str(msg.get("validated_jina") or "")
    if not jina:
        return ""
    pmn = _load_normalizer()
    if pmn is None:
        return ""
    try:
        name = pmn.extract_ml_official_store_name(jina) or ""
        if name:
            return name
        seller = pmn.extract_ml_seller(jina) or ""
    except Exception:
        return ""
    return known_verified_stores().get(_seller_norm(seller), "")


def apply_source_consensus(messages: list[dict]) -> list[dict]:
    """Independent sources corroborate exceptional coupon prices; repetition does not."""
    pmn = _load_normalizer()
    groups: dict[tuple[str, int], set[str]] = {}
    for msg in messages:
        product = msg.get("product") or ""
        try:
            key = pmn.product_key(product) if pmn is not None else slugify_product(product)
        except Exception:
            key = slugify_product(product)
        price = msg.get("effective_price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        bucket = int(round(float(price) / 5.0) * 5)
        groups.setdefault((key, bucket), set()).add(str(msg.get("groupName") or "unknown"))
    out = []
    for msg in messages:
        row = dict(msg)
        product = row.get("product") or ""
        try:
            key = pmn.product_key(product) if pmn is not None else slugify_product(product)
        except Exception:
            key = slugify_product(product)
        price = row.get("effective_price")
        bucket = int(round(float(price) / 5.0) * 5) if isinstance(price, (int, float)) and price > 0 else -1
        count = len(groups.get((key, bucket), set()))
        row["source_consensus_count"] = count
        if count >= 2:
            row["opportunity_score"] = int(row.get("opportunity_score") or 0) + min(14, 6 + 4 * count)
            reasons = list(row.get("opportunity_reasons") or [])
            reasons.append(f"consenso:{count} fontes")
            row["opportunity_reasons"] = reasons
            if row.get("price_confidence") == "low_coupon_unconfirmed":
                row["price_confidence"] = "medium_source_consensus"
        out.append(row)
    return out


def collapse_same_product_offers(messages: list[dict]) -> list[dict]:
    """One delivery slot per SKU; keep the strongest validated offer/source."""
    pmn = _load_normalizer()
    best: dict[str, dict] = {}
    alternatives: dict[str, list[dict]] = {}
    # First resolve price dominance within explicitly comparable offers.
    # Doing this before the general ranking makes the result order-independent.
    comparable_best: dict[tuple[str, str], dict] = {}
    for msg in messages:
        context = commercial_comparison_context(msg)
        price = msg.get("effective_price")
        if not context or not isinstance(price, (int, float)) or price <= 0:
            continue
        identity = canonical_product_key(msg.get("product") or "")
        ck = (identity, context)
        previous = comparable_best.get(ck)
        if previous is None or (price, -msg.get("opportunity_score", 0)) < (previous["effective_price"], -previous.get("opportunity_score", 0)):
            comparable_best[ck] = msg
    for msg in messages:
        # Deduplication identity comes from the normalized source product. The ML
        # page title is evidence, not identity: affiliate/social pages decorate
        # the same SKU differently ("Perfume Asad..." vs "Lattafa Asad EDP...").
        product = msg.get("product") or msg.get("validated_product") or ""
        # Commercial stylization inside a word must not create another SKU:
        # Evolut.io == Evolutio. Keep spaces/hyphens that separate real tokens.
        identity_product = re.sub(r'(?<=[A-Za-zÀ-ÿ])\.(?=[A-Za-zÀ-ÿ])', '', product)
        try:
            key = pmn.product_key(identity_product) if pmn is not None else slugify_product(identity_product)
        except Exception:
            key = slugify_product(identity_product)
        key = key or slugify_product(msg.get("product") or "")
        context = commercial_comparison_context(msg)
        comparable = comparable_best.get((canonical_product_key(msg.get("product") or ""), context)) if context else None
        if comparable is not None and comparable is not msg:
            alternatives.setdefault(key, []).append(msg)
            continue
        current = best.get(key)
        candidate_rank = (msg.get("opportunity_score", 0), -(msg.get("effective_price") or 10**9))
        current_rank = ((current or {}).get("opportunity_score", -10**9), -((current or {}).get("effective_price") or 10**9))
        if current is None or candidate_rank > current_rank:
            if current is not None:
                alternatives.setdefault(key, []).append(current)
            best[key] = msg
        else:
            alternatives.setdefault(key, []).append(msg)
    out = []
    for key, msg in best.items():
        row = dict(msg)
        row["product_identity_key"] = key
        row["alternative_offer_count"] = len(alternatives.get(key, []))
        row["alternative_sources"] = sorted({str(x.get("groupName") or "") for x in alternatives.get(key, []) if x.get("groupName")})
        out.append(row)
    return out


def select_diverse_batch(messages: list[dict], limit: int = MAX_PROMOS_PER_RUN) -> list[dict]:
    """Keep quality first while preventing a three-slot monoculture."""
    selected: list[dict] = []
    category_counts: dict[str, int] = {}
    for msg in messages:
        category = str(msg.get("offer_category") or "other")
        if category_counts.get(category, 0) >= 2:
            continue
        selected.append(msg)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for msg in messages:
            if msg not in selected:
                selected.append(msg)
                if len(selected) >= limit:
                    break

    # Audience likes kits. Give one competitive kit a slot, but never rescue a
    # weak kit merely to satisfy a quota.
    kit = next((m for m in messages if m.get("offer_category") == "kit" and m not in selected), None)
    if kit and selected and not any(m.get("offer_category") == "kit" for m in selected):
        lowest = min(selected, key=lambda m: m.get("opportunity_score", 0))
        if kit.get("opportunity_score", 0) >= lowest.get("opportunity_score", 0) - 12:
            selected[selected.index(lowest)] = kit
    return sorted(selected, key=lambda m: m.get("opportunity_score", 0), reverse=True)[:limit]


def append_price_observations(messages: list[dict]) -> None:
    PRICE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    existing: set[tuple[str, float, str, str]] = set()
    if PRICE_HISTORY_FILE.exists():
        try:
            for line in PRICE_HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]:
                row = json.loads(line)
                existing.add((
                    str(row.get("product_key") or ""),
                    round(float(row.get("effective_price") or 0), 2),
                    str(row.get("link") or ""),
                    str(row.get("comparison_context") or ""),
                ))
        except Exception:
            existing = set()
    rows = []
    for msg in messages:
        effective = msg.get("effective_price")
        if not isinstance(effective, (int, float)) or effective <= 0:
            continue
        discount = msg.get("inferred_discount_pct")
        if isinstance(discount, (int, float)) and discount > 35 and msg.get("price_confidence") != "medium_source_consensus":
            continue
        product_key = canonical_product_key(msg.get("product") or "")
        link = extract_link(msg.get("body") or "") or ""
        signature = (product_key, round(float(effective), 2), link, commercial_comparison_context(msg))
        if signature in existing:
            continue
        existing.add(signature)
        rows.append({
            "ts": now,
            "product_key": product_key,
            "product": msg.get("product") or "",
            "category": msg.get("offer_category"),
            "effective_price": round(float(effective), 2),
            "public_price": parse_price_number(msg.get("validated_price")),
            "link": extract_link(msg.get("body") or ""),
            "source": msg.get("groupName"),
            "comparison_context": commercial_comparison_context(msg),
            "price_confidence": msg.get("price_confidence"),
        })
    if rows:
        with PRICE_HISTORY_FILE.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def product_appeal_score(product: str, body: str) -> int:
    text = f"{product}\n{body}".lower()
    score = 0
    if any(term in text for term in HIGH_APPEAL_BRANDS):
        score += 18
    if any(term in text for term in HYPE_TERMS):
        score += 12
    # Breno 2026-07-12: o grupo gosta bastante de kits hoje.
    if re.search(r'(?i)\bkit\b|\bcombo\b', text):
        score += 14
    return score


HIGH_APPEAL_BRANDS = [
    'armani', 'azzaro', 'ysl', 'dior', 'versace', 'rabanne', 'paco rabanne',
    'dolce', 'gabbana', 'jean paul', 'givenchy', 'chanel', 'prada',
    'carolina herrera', 'mercedes', 'natura essencial', 'natura homem',
    'malbec', 'kaiak', 'boticario', 'eudora', 'lattafa', 'afnan', 'rasasi',
    'armaf', 'maison alhambra', 'french avenue', 'rayhaan', 'mawwal',
    'hawas', 'asad', 'khamrah', 'fakhar', '9pm', 'liquid brun', 'turathi',
]
HYPE_TERMS = ['aventus', 'sauvage', 'hacivat', 'ultra male', 'le male', 'god of fire', 'torino 21', 'althair', 'erba pura', 'invictus', 'acqua di gio']


def opportunity_reasons(product: str, body: str, price: str | None) -> list[str]:
    """Human 'why forward this' reasons for the group report, not score debug."""
    text = f"{product}\n{body}".lower()
    out: list[str] = []
    pnum = parse_price_number(price)
    if pnum is not None:
        if pnum <= 180:
            out.append("preço forte")
        elif pnum <= 260:
            out.append("preço bom")
    if re.search(r'(?i)\bkit\b|\bcombo\b', text):
        out.append("kit (o grupo curte)")
    hit = next((b for b in HYPE_TERMS if b in text), None) or next((b for b in HIGH_APPEAL_BRANDS if b in text), None)
    if hit:
        out.append(f"marca/linha que vende ({hit})")
    if re.search(r'(?i)(menor preço|menor preco|hist[oó]rico|baixou)', _strip_wa(body or "")):
        out.append("sinal de menor preço")
    if re.search(r'(?i)(loja oficial|loja verificada)', _strip_wa(body or "")):
        out.append("loja oficial")
    return out


def commercial_quality_score(body: str, product: str, price: str | None, cupom: str | None) -> tuple[int, list[str]]:
    """Score opportunity first; production readiness is only a small bonus."""
    clean = _strip_wa(body or "")
    score = 0
    reasons: list[str] = []
    if extract_link(body or ""):
        score += 10; reasons.append("link")
    else:
        score -= 100; reasons.append("sem link")
    if price:
        score += 12; reasons.append("preço")
        pnum = parse_price_number(price)
        if pnum is not None:
            if pnum <= 180:
                score += 18; reasons.append("preço forte")
            elif pnum <= 260:
                score += 10; reasons.append("preço bom")
            elif pnum <= 400:
                score += 4; reasons.append("preço ok")
    else:
        score -= 25; reasons.append("sem preço")
    if cupom:
        score += 10; reasons.append("cupom")
    if re.search(r'(?i)(menor preço|menor preco|hist[oó]rico|baixou|pre[çc][aã]o|barato|valendo muito|no pix|pix)', clean):
        score += 8; reasons.append("sinal de oferta")
    if re.search(r'(?i)(loja oficial|loja verificada|vendido por loja oficial)', clean):
        score += 10; reasons.append("loja")
    appeal = product_appeal_score(product, clean)
    if appeal:
        score += appeal; reasons.append("produto forte")
    if product and slugify_product(product) not in {'produto', 'unknown-product'}:
        score += 8; reasons.append("produto")
    else:
        score -= 25; reasons.append("produto ruim")
    return score, reasons


def production_readiness_score(source_media: str, bank_image: str | None, has_context: bool) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if bank_image:
        score += 12; reasons.append("banco")
    if has_context:
        score += 8; reasons.append("contexto")
    if source_media:
        score += 5; reasons.append("source")
    if not source_media and not bank_image:
        score -= 5; reasons.append("produção crua")
    return score, reasons


def annotate_candidate(msg: dict) -> dict:
    body = msg.get("body") or ""
    product = extract_product_name(body)
    price = extract_price(body)
    cupom = extract_cupom(body)
    source_media = str(msg.get("mediaPath") or "") if msg.get("hasMedia") else ""
    bank_image, bank_source = resolve_approved_image(product)
    has_ctx = has_editorial_context(product)
    commercial, commercial_reasons = commercial_quality_score(body, product, price, cupom)
    production, production_reasons = production_readiness_score(source_media, bank_image, has_ctx)
    pre_deal, pre_deal_pts = None, 0
    if curation_v2_enabled():
        # Pre-validation deal signal: only 20 of ~50 candidates get validated per
        # round, and the old pre-rank (appeal/format/media) never let an ordinary-
        # brand deal into the shortlist. Source price vs the product's normal price.
        pre_deal = source_deal_pct(product, price)
        if pre_deal is not None:
            pre_deal_pts = 30 if pre_deal >= 20 else 18 if pre_deal >= 10 else 6 if pre_deal >= 5 else -15 if pre_deal <= -10 else 0
            if pre_deal_pts:
                commercial_reasons = commercial_reasons + [f"fonte {pre_deal:+.0f}% vs normal"]
    out = dict(msg)
    out.update({
        "product": product,
        "price": price,
        "cupom": cupom,
        "source_media": source_media,
        "bank_image": bank_image,
        "bank_source": bank_source,
        "has_context": has_ctx,
        "commercial_score": commercial,
        "production_score": production,
        "pre_deal_pct": round(pre_deal, 1) if pre_deal is not None else None,
        "candidate_score": commercial + production + pre_deal_pts,
        "score_reasons": commercial_reasons + production_reasons,
    })
    return out


CURATION_LOG_FILE = BANK_DIR / "logs" / "collector_curation.jsonl"


def write_curation_log(ranked: list[dict], shortlist: list[dict], eligible: list[dict], selected: list[dict]) -> None:
    """One row per round: did the best deals reach validation and delivery? Never raises."""
    try:
        def brief(m: dict) -> dict:
            return {"product": m.get("product"), "source": m.get("groupName"),
                    "pre_deal_pct": m.get("pre_deal_pct"), "deal_pct": m.get("deal_pct"),
                    "verified_seller": m.get("verified_seller") or "", "score": m.get("opportunity_score"),
                    "price": m.get("price")}
        sel_ids = {id(m) for m in selected}
        row = {
            "ts": int(time.time()),
            "curation_version": "v2" if curation_v2_enabled() else "v1",
            "candidates": len(ranked),
            "candidates_with_deal_signal": sum(1 for m in ranked if m.get("pre_deal_pct") is not None),
            "candidate_deals_ge10": sum(1 for m in ranked if (m.get("pre_deal_pct") or 0) >= 10),
            "shortlist_deals_ge10": sum(1 for m in shortlist if (m.get("pre_deal_pct") or 0) >= 10),
            "eligible": len(eligible),
            "selected": [brief(m) for m in selected],
            "best_not_selected": [brief(m) for m in sorted(
                (m for m in eligible if id(m) not in sel_ids),
                key=lambda m: -(m.get("deal_pct") if m.get("deal_pct") is not None else -999))[:3]],
        }
        CURATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CURATION_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # telemetry must never block delivery
        service_log(f"deliver: curation_log_failed {type(exc).__name__}")


def source_deal_pct(product: str, price: str | None) -> float | None:
    """% below (+) / above (-) the product's normal public price; None without >=5 observations."""
    value = parse_price_number(price)
    if not value:
        return None
    history = product_price_baseline(canonical_product_key(product))
    if len(history) < 5:
        return None
    ordered = sorted(history)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return (1 - value / median) * 100


def is_fragrance_candidate(product: str, body: str) -> tuple[bool, str]:
    """Category gate: reject only on positive evidence of NON-fragrance.

    Polarity note: these are curated perfume source groups. Requiring an explicit
    "edp/edt/perfume" token or a hardcoded brand whitelist silently killed real
    offers whose caption is just "*Nautica Voyage 100ml*" + price + link, and the
    whitelist aged badly against new arab houses. Ad identity is verified later
    against the marketplace page (jina/ML), which is real evidence; this gate now
    only has to keep obvious non-perfume junk (tools, electronics, skincare) out
    of the validation budget.
    """
    text = _strip_wa(f"{product}\n{body}").lower()
    # Coupon/category landing pages are acquisition messages, not product offers.
    # Never let "mínimo R$79" become a fake perfume and consume a delivery slot.
    # Polarity note (2026-08-02): the previous version searched the coupon regex
    # over product+body and rejected on ANY of {operational title, >1 link, no
    # "ml/edp" token in title}. Every healthy promo in these groups mentions a
    # coupon in the body and ships link+shortlink, and real SKUs are often titled
    # without a volume token ("ASAD ELIXIR", "LIQUID BRUN"). That killed 1524 real
    # offers vs 75 correct catches and drove the valid rate from ~89% to ~14%.
    # A coupon in the BODY is a sign of a good offer, not of junk. Only the TITLE
    # can tell us the message itself is an acquisition/landing announcement.
    title = product or ""
    # The title announces a coupon/selection campaign instead of naming a product.
    operational_title = bool(re.search(
        r'(?i)(\bcupo(?:m|ns)\b|\bcarta\b|\bliberad[oa]s?\b|\bdomingou\b|'
        r'\bsele[cç][aã]o\b|\boff no site\b|\bleia antes de comprar\b|'
        r'\bcompras? (?:a )?acima de\b|\blimitado a r\$)',
        title,
    ))
    # Positive evidence that the title actually names a purchasable item.
    product_evidence = bool(re.search(
        r'(?i)\b\d{2,3}\s*ml\b|\b(perfume|parfum|eau de|edp|edt|col[oô]nia|'
        r'body splash|kit|refil|desodorante)\b',
        title,
    ))
    if operational_title and not product_evidence:
        return False, "cupom/seleção genérica sem produto"
    fragrance = re.search(
        r"\b(perfume|parfum|eau de|edp|edt|col[oô]nia|deo col[oô]nia|body splash|fragr[aâ]ncia|kit.{0,30}(perfume|col[oô]nia|splash))\b",
        text,
    )
    non_fragrance = re.search(
        r"\b(gel de limpeza|s[eé]rum|protetor solar|shampoo|condicionador|maquiagem|base facial|"
        r"hidratante facial|creme facial|sabonete facial|lo[çc][aã]o corporal|creme hidratante|"
        r"refil corporal|sabonete l[ií]quido|escova de dentes|absorvente|fralda|"
        r"air fryer|fone|smartphone|celular|notebook|monitor|smartwatch|caixa de som|"
        r"m[aá]quina de (?:acabamento|cortar|lavar)|barbeador|aparador|secador|"
        r"chapinha|prancha alisadora|panela|liquidificador|cafeteira|aspirador|"
        r"t[êe]nis|camiseta|mochila|cadeira|colch[aã]o|pneu|furadeira|parafusadeira)\b",
        text,
    )
    if non_fragrance and not fragrance:
        return False, f"categoria não-perfume: {non_fragrance.group(1)}"
    if fragrance:
        return True, "fragrância"
    return True, "categoria presumida (grupo de perfume)"


def validate_shortlist_candidate(msg: dict) -> dict:
    """Validate one pre-ranked candidate before it can occupy a delivery slot."""
    out = dict(msg)
    body = out.get("body") or ""
    product = out.get("product") or extract_product_name(body)
    link = extract_link(body)
    ok_category, category_reason = is_fragrance_candidate(product, body)
    out.update({"validation_ok": False, "validation_reason": category_reason, "validated_jina": ""})
    if not ok_category:
        return out
    if not link:
        out["validation_reason"] = "sem link"
        return out
    pmn = _load_normalizer()
    if pmn is None:
        out["validation_reason"] = "validador indisponível"
        return out
    try:
        jina = pmn.jina_fetch(link) or ""
    except Exception as exc:
        out["validation_reason"] = f"falha Jina: {type(exc).__name__}"
        return out
    out["validated_jina"] = jina
    if not jina or len(jina) < 200:
        out["validation_reason"] = "página não verificável"
        return out
    try:
        validated_product = pmn.extract_product_from_jina(jina)
        _de, page_price, _other = pmn.social_primary_offer_prices(jina)
        page_price = page_price or pmn.jina_price(jina)
    except Exception:
        validated_product, page_price = "", None
    # A generic ML/profile/recommendations page can contain unrelated paused
    # listings. Never apply page-wide availability words until the target offer's
    # identity/primary block is established.
    if not validated_product and not page_price:
        out["validation_reason"] = "identidade do anúncio não confirmada"
        return out
    if not pmn.availability_ok(jina):
        out["validation_reason"] = "indisponível no ML"
        return out
    source_product = str(out.get("product") or "")
    if validated_product:
        source_has_volume = bool(re.search(r'(?i)\b\d{2,3}\s*ml\b', source_product))
        validated_has_volume = bool(re.search(r'(?i)\b\d{2,3}\s*ml\b', validated_product))
        editorial_source = bool(re.search(r'(?i)(esse [ée] top|o melhor|n[aã]o perde|corre|fixa[cç][aã]o|aroma inesquec[ií]vel)', source_product))
        if (validated_has_volume and not source_has_volume) or editorial_source:
            out["source_product"] = source_product
            out["product"] = validated_product
    out.update({
        "validation_ok": True,
        "validation_reason": "disponível",
        "validated_price": page_price,
        "validated_product": validated_product,
    })
    return out


def validate_shortlist(messages: list[dict]) -> list[dict]:
    """Bounded parallel validation; preserves input rank order."""
    import concurrent.futures
    if not messages:
        return []
    results: list[dict | None] = [None] * len(messages)
    with concurrent.futures.ThreadPoolExecutor(max_workers=VALIDATION_WORKERS) as pool:
        futures = {pool.submit(validate_shortlist_candidate, msg): i for i, msg in enumerate(messages)}
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                failed = dict(messages[i])
                failed.update({"validation_ok": False, "validation_reason": f"erro: {type(exc).__name__}"})
                results[i] = failed
    return [r for r in results if r is not None]


def rank_candidates(messages: list[dict]) -> list[dict]:
    # One ranking snapshot; next pass sees new bank files and identity rules.
    _approved_files_for_key.cache_clear()
    resolve_approved_image.cache_clear()
    with identity_memo_scope():
        annotated = [annotate_candidate(m) for m in messages]
    return sorted(
        annotated,
        key=lambda m: (
            m.get("candidate_score", 0),
            m.get("commercial_score", 0),
            bool(m.get("bank_image")),
            bool(m.get("source_media")),
            _message_epoch_seconds(m.get("timestamp")) or 0,
        ),
        reverse=True,
    )


def build_validation_shortlist(ranked: list[dict], limit: int = PREVALIDATE_LIMIT) -> list[dict]:
    """Cover several opportunity axes before spending Jina/browser validation."""
    selected: list[dict] = []
    seen: set[str] = set()

    def identity(msg: dict) -> str:
        return str(msg.get("messageId") or "") or "|".join(normalize_offer_keys(msg.get("body") or ""))

    def add(msg: dict) -> None:
        key = identity(msg)
        if msg not in selected and key not in seen and len(selected) < limit:
            selected.append(msg)
            seen.add(key)

    if curation_v2_enabled():
        # Guarantee validation slots for the strongest source-price deals.
        deals = sorted((m for m in ranked if (m.get("pre_deal_pct") or 0) >= 10),
                       key=lambda m: m.get("pre_deal_pct") or 0, reverse=True)
        for msg in deals[:6]:
            add(msg)

    # Keep ranking quality as the backbone, but do not let it monopolize every
    # validation slot before category/history signals are known.
    for msg in ranked[: max(8, limit // 2)]:
        add(msg)

    categories = ("arabic", "designer", "national", "kit", "body_splash", "budget_imported", "other")
    for category in categories:
        category_rows = [m for m in ranked if classify_offer_category(m.get("product") or "", m.get("body") or "") == category]
        if not category_rows:
            continue
        add(category_rows[0])  # best pre-ranked in category
        priced = [(parse_price_number(m.get("price")), m) for m in category_rows]
        priced = [(price, msg) for price, msg in priced if isinstance(price, (int, float)) and price > 0]
        if priced:
            add(min(priced, key=lambda x: x[0])[1])  # cheap outlier gets validated, not blindly selected

    prefs = load_collector_preferences()
    audience_ranked = sorted(
        ranked,
        key=lambda m: preference_score(m.get("product") or "", m.get("body") or "", prefs)[0],
        reverse=True,
    )
    for msg in audience_ranked[:3]:
        add(msg)

    represented_sources = {str(m.get("groupName") or "") for m in selected}
    for msg in ranked:
        source = str(msg.get("groupName") or "")
        if source and source not in represented_sources:
            add(msg)
            represented_sources.add(source)

    for msg in ranked:
        add(msg)
    return selected[:limit]


# ── Main ────────────────────────────────────────────────

def service_log(line: str) -> None:
    # Routine duplicate/variant diagnostics belong in the service log, not in
    # cron stderr. On a real failure Hermes used to dump thousands of harmless
    # lines into Telegram and present the first duplicate_skip as the cause.
    path = Path.home() / ".hermes" / "perfume_image_bank" / "logs" / "deliver_last.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def write_collector_funnel(messages: list[dict], ranked: list[dict], shortlist: list[dict],
                           validated: list[dict], eligible: list[dict], selected: list[dict],
                           delivered: list[dict]) -> None:
    """Local actionable telemetry; never adds a WhatsApp/Telegram report."""
    try:
        rejection_reasons: dict[str, int] = {}
        for msg in validated:
            if msg.get("validation_ok"):
                continue
            reason = str(msg.get("validation_reason") or "unknown")
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        shortlist_categories: dict[str, int] = {}
        for msg in shortlist:
            category = classify_offer_category(msg.get("product") or "", msg.get("body") or "")
            shortlist_categories[category] = shortlist_categories.get(category, 0) + 1
        report = {
            "generated_at": int(time.time()),
            "counts": {
                "candidates": len(messages), "ranked": len(ranked), "shortlist": len(shortlist),
                "validated_ok": sum(bool(m.get("validation_ok")) for m in validated),
                "rejected": sum(not bool(m.get("validation_ok")) for m in validated),
                "eligible_unique_skus": len(eligible), "selected": len(selected), "delivered": len(delivered),
            },
            "shortlist_categories": shortlist_categories,
            "rejection_reasons": rejection_reasons,
            "selected": [
                {"product": m.get("product"), "category": m.get("offer_category"),
                 "score": m.get("opportunity_score"), "confidence": m.get("price_confidence"),
                 "source": m.get("groupName")}
                for m in selected
            ],
            "delivered_message_ids": [str(m.get("messageId") or "") for m in delivered],
        }
        COLLECTOR_FUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COLLECTOR_FUNNEL_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(COLLECTOR_FUNNEL_FILE)
    except Exception as exc:
        service_log(f"deliver: funnel_write_failed {type(exc).__name__}: {exc}")

def prune_jina_cache(max_age_hours: float = 24.0) -> int:
    """Auto-delete stale shared Jina cache files (Breno 2026-07-12)."""
    cache_dir = BANK_DIR / ".jina_cache"
    if not cache_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in cache_dir.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def validated_delivery_key(msg: dict) -> str:
    # Keep base/flanker tokens; use the normalizer identity, not title wrappers.
    product = str(msg.get('product') or '')
    pmn = _load_normalizer()
    key = pmn.product_key(product) if pmn is not None else slugify_product(product)
    # The title cleaner can omit commercial gender/concentration words. Dedupe
    # must not merge identities just because their display titles are similar.
    signature = sorted(set(re.findall(r'\b(?:edp|edt|parfum|elixir|woman|women|man|men|masculino|feminino)\b', product.lower())))
    return 'validated-product:' + key + ':' + ','.join(signature)


def filter_delivered_validated(messages: list[dict]) -> list[dict]:
    keys = prune_offer_keys(load_state().get('delivered_offer_keys', {}))
    result = []
    evaluated_duplicates = []
    for msg in messages:
        key = validated_delivery_key(msg)
        if key in keys:
            service_log(f'deliver: validated_duplicate_skip key={key}')
            evaluated_duplicates.append(msg)
        else:
            result.append(msg)
    if evaluated_duplicates:
        # These exact source message IDs have now been conclusively evaluated.
        # Without this acknowledgement the same top 20 consumed every shortlist
        # forever, starving unsent offers below them. Do not invent a new send,
        # extend product dedupe into tomorrow, or mark unvalidated rows here.
        mark_messages_evaluated(evaluated_duplicates)
    return result


def main(run):
    # Round-scoped only: bank edits become visible on the very next run.
    resolve_approved_image.cache_clear()
    with run.phase('collect'):
        messages, state = collect_new_messages()

    if not messages:
        service_log("deliver: no_new_messages")
        write_collector_funnel([], [], [], [], [], [], [])
        run.finish('no_candidates', candidates=0, delivered=0)
        return

    # Source photo is preferred and forwarded unchanged when present. Text-only
    # offers are still valid collector inputs: send the original text/link and
    # let the downstream normalizer report image readiness independently. Never
    # substitute an image-bank asset in the collector.

    with run.phase('rank_and_bank'):
        ranked_messages = rank_candidates(messages)
        shortlist = build_validation_shortlist(ranked_messages, PREVALIDATE_LIMIT)
    with run.phase('validate'):
        validated_shortlist = validate_shortlist(shortlist)
    rejected_messages = [m for m in validated_shortlist if not m.get("validation_ok")]
    eligible_messages = [validated_opportunity_score(m) for m in validated_shortlist if m.get("validation_ok")]
    eligible_messages = filter_delivered_validated(eligible_messages)
    eligible_messages = apply_source_consensus(eligible_messages)
    eligible_messages = collapse_same_product_offers(eligible_messages)
    eligible_messages.sort(
        key=lambda m: (
            m.get("opportunity_score", 0),
            m.get("audience_score", 0),
            m.get("candidate_score", 0),
            _message_epoch_seconds(m.get("timestamp")) or 0,
        ),
        reverse=True,
    )
    append_price_observations(eligible_messages)
    if rejected_messages:
        mark_messages_evaluated(rejected_messages)
        for m in rejected_messages:
            service_log(
                f"deliver: validation_reject product={m.get('product')!r} "
                f"reason={m.get('validation_reason')!r} source={m.get('groupName')!r}"
            )
    service_log(
        f"deliver: candidates={len(messages)} ranked={len(ranked_messages)} "
        f"shortlist={len(shortlist)} valid={len(eligible_messages)} rejected={len(rejected_messages)} max={MAX_PROMOS_PER_RUN} "
        + " | ".join(
            f"opp={m.get('opportunity_score')}:{m.get('product')}:{m.get('groupName')}"
            for m in eligible_messages[:5]
        )
    )
    # Rescue rounds may revisit messages whose temporary source image has already
    # been cleaned from image_cache. Such rows cannot be delivered faithfully and
    # must not occupy every batch slot forever; text-only source posts remain valid.
    deliverable_messages = []
    for candidate in eligible_messages:
        if candidate.get("hasMedia") or candidate.get("has_media"):
            source_path = str(candidate.get("source_media") or message_media_path(candidate) or "")
            if not source_path or not os.path.isfile(source_path):
                service_log(
                    f"deliver: stale_source_media_skip product={candidate.get('product')!r} "
                    f"source={candidate.get('groupName')!r}"
                )
                continue
        deliverable_messages.append(candidate)
    if curation_v2_enabled():
        # A curator does not forward an ordinary price >=10% above normal just
        # because the round had free slots. It stays unseen and can still win
        # later if its price drops; it is only not sent now.
        too_expensive = [m for m in deliverable_messages if (m.get("deal_pct") is not None and m["deal_pct"] <= -10)]
        for m in too_expensive:
            service_log(f"deliver: curation_skip_above_normal product={m.get('product')!r} deal_pct={m.get('deal_pct')} source={m.get('groupName')!r}")
        deliverable_messages = [m for m in deliverable_messages if m not in too_expensive]
    selected_messages = select_diverse_batch(deliverable_messages, MAX_PROMOS_PER_RUN)
    write_curation_log(ranked_messages, shortlist, eligible_messages, selected_messages)
    delivery_started = time.monotonic()
    run.data['phase'] = 'delivery'
    run.save()
    delivery_messages = list(selected_messages)
    service_log(
        f"deliver: delivery_order=best_to_worst "
        + " | ".join(
            f"opp={m.get('opportunity_score')}:{m.get('product')}:{m.get('groupName')}"
            for m in delivery_messages
        )
    )
    delivered = 0
    successfully_delivered: list[dict] = []

    for msg in delivery_messages:
        body = msg["body"]
        price = extract_price(body)
        cupom = extract_cupom(body)
        group = msg["groupName"]
        # Validation may replace an editorial slogan with the marketplace SKU.
        # Preserve that corrected identity in reports and downstream dedupe.
        product = str(msg.get("product") or extract_product_name(body))

        # Extract link for validation
        link = None
        for word in body.split():
            if word.startswith("http"):
                link = word.rstrip(".,;:!")
                break

        # A successful shortlist validation already fetched this offer. Keep
        # the fallback probe only for callers without that same-round evidence.
        link_status = "✅" if msg.get("validation_ok") and msg.get("validated_jina") else "?"
        if link and link_status == "?":
            try:
                import subprocess
                r = subprocess.run(
                    ["curl", "-sI", "--max-time", "6", "-L", link],
                    capture_output=True, text=True, timeout=8
                )
                output = r.stdout + r.stderr
                # Connection refused / DNS failure / timeout → ❌
                if r.returncode != 0 and "HTTP/" not in output:
                    link_status = "❌"
                # HTTP 404 or 410 → truly gone
                elif "HTTP/1.1 404" in output or "HTTP/2 404" in output or \
                     "HTTP/1.1 410" in output or "HTTP/2 410" in output:
                    link_status = "❌"
                # Any other HTTP response (200, 301, 302, 403, etc.) → link exists
                elif "HTTP/" in output:
                    link_status = "✅"
            except Exception:
                link_status = "?"

        # D - Disponibilidade via Jina Reader (bypass ML anti-bot).
        # Uses the normalizer's cached jina_fetch when available so the manual
        # normalizer reuses the same page minutes later (shared cache, #1/#5).
        disp_status = "✅" if msg.get("validation_ok") else "não verificável"
        jina_text = str(msg.get("validated_jina") or "")
        if link and not jina_text:
            pmn = _load_normalizer()
            try:
                if pmn is not None:
                    jina_text = pmn.jina_fetch(link) or ""
                else:
                    import subprocess
                    r = subprocess.run(
                        ["curl", "-sL", "--max-time", "12",
                         "-H", "Accept: text/markdown",
                         f"https://r.jina.ai/{link}"],
                        capture_output=True, text=True, timeout=15
                    )
                    jina_text = r.stdout or ""
            except Exception:
                jina_text = ""
            # Availability was already gated against the identified offer block
            # before ranking. Do not reinterpret unrelated recommendation text here.

        # B+ — validated ML price from the same Jina page (no extra fetch).
        # Social/affiliate pages: trust only the featured 'Ir para produto' block.
        ml_price = msg.get("validated_price")
        price_check = ""
        if jina_text and "validated_price" not in msg:
            pmn = _load_normalizer()
            try:
                if pmn is not None:
                    _de, _por, _outros = pmn.social_primary_offer_prices(jina_text)
                    ml_price = _por or pmn.jina_price(jina_text)
            except Exception:
                ml_price = None
        if ml_price and price:
            source_num = parse_price_number(price)
            ml_num = parse_price_number(ml_price)
            if source_num is not None and ml_num is not None:
                if abs(source_num - ml_num) <= max(2.0, source_num * 0.02):
                    price_check = f" | ML confirma ✅"
                elif ml_num < source_num:
                    price_check = f" | ⚠️ ML já mostra {ml_price} (fonte está mais cara)"
                elif cupom or msg.get("inferred_discount_pct"):
                    price_check = f" · ML {ml_price} antes do cupom"
                else:
                    price_check = f" · preço público ML {ml_price}"
        elif ml_price and not price:
            price_check = f" | ML mostra {ml_price}"

        # Build validation line. The collector always sends source-media, but it
        # also reports whether our bank already has an approved image so Breno can
        # know if manual normalization should pass cleanly.
        price_str = price or "?"
        cupom_str = cupom or "não menciona"
        source_media = str(msg.get("source_media") or message_media_path(msg) or "")
        bank_image, bank_source = resolve_approved_image(product)
        bank_status = f"✅ {bank_source}" if bank_image else "❌ sem approved_final"
        has_ctx = bool(msg.get("has_context")) if "has_context" in msg else has_editorial_context(product)
        context_status = "✅" if has_ctx else "❌"
        commercial_score = int(msg.get("commercial_score") or 0)
        production_score = int(msg.get("production_score") or 0)
        total_score = int(msg.get("opportunity_score") or msg.get("candidate_score") or (commercial_score + production_score))
        audience_score = int(msg.get("audience_score") or 0)
        score_color = "🟢" if total_score >= 85 else ("🟡" if total_score >= 65 else "🔴")
        reasons = ", ".join((msg.get("opportunity_reasons") or msg.get("score_reasons") or [])[:6])
        # Compact report (Breno 2026-07-12): the report is a forwarding argument,
        # not a technical checklist. Score stays as short badge only; full score
        # breakdown lives in deliver_last.log.
        price_confirmed = "confirma" in price_check
        # Preço divergente NÃO rebaixa readiness: corrigir preço é a função do
        # normalizador (Breno 2026-07-12). Readiness = disponibilidade + foto.
        if disp_status == "❌":
            readiness = "🔴 indisponível no ML"
        elif bank_image and price_confirmed:
            readiness = "🟢 pronto (foto banco ✅, preço confirmado)"
        elif bank_image:
            readiness = "🟢 foto banco ✅ (normalizador ajusta preço)"
        elif source_media:
            readiness = "🟡 foto banco ❌ (usaria a da fonte)"
        else:
            readiness = "🔴 sem foto (nem banco nem fonte)"
        motivos = opportunity_reasons(product, body, price)
        score_badge = f"{score_color} {total_score}"
        # Link status only matters when broken; Jina stock check already proves
        # the link resolves (Breno 2026-07-12).
        lines = []
        if link_status == "❌":
            lines.append("⚠️ LINK QUEBRADO")
        lines.append(f"Estoque {disp_status}")
        lines.append(f"💰 {price_str}{price_check}")
        if msg.get("price_confidence") == "low_coupon_unconfirmed":
            discount = msg.get("inferred_discount_pct")
            lines.append(f"⚡ oportunidade excepcional ({discount:.0f}% OFF inferido); cupom ainda não confirmado")
        if motivos:
            lines.append("👍 " + ", ".join(motivos[:3]))
        lines.append(f"Normalizador: {readiness}")
        lines.append(f"Score {score_badge} · 🛍 {group}")
        validation = "\n".join(lines)
        service_log(
            f"deliver: report product={product!r} score_c={commercial_score} score_p={production_score} "
            f"total={total_score} bank={bank_source} ctx={context_status} reasons={reasons!r}"
        )

        # 1. Send exactly what the collector saw in the source group.
        # This pipeline is a collector, not the normalizer: never swap in image-bank
        # assets here. Bank image replacement belongs only to the manual normalizer.
        body_clean = _strip_wa(body.strip())
        image_to_send = source_media
        message_id = str(msg.get("messageId") or "")
        stage = delivery_progress(message_id)
        if stage != "promo_sent":
            if image_to_send:
                promo_ok = send_whatsapp_image(DELIVERY_GROUP, image_to_send, caption=body_clean)
            else:
                promo_ok = send_whatsapp_text(DELIVERY_GROUP, body_clean)
            if not promo_ok:
                service_log(f"deliver: promo_send_failed product={product!r} image={bool(image_to_send)} source={group!r}")
                continue
            set_delivery_progress(message_id, "promo_sent")
            time.sleep(1.5)
        else:
            service_log(f"deliver: resume_validation_only product={product!r} source={group!r}")

        # 2. Send validation. Only both confirmed sends constitute delivery.
        validation_ok = send_whatsapp_text(DELIVERY_GROUP, validation)
        if validation_ok:
            service_log(f"deliver: sent_promo product={product!r} image={bool(image_to_send)} source={group!r}")
            successfully_delivered.append(msg)
            # Save complete pairs immediately: later failure cannot lose them.
            mark_messages_seen([msg])
            clear_delivery_progress([message_id])
            delivered += 1
            time.sleep(1.5)
        else:
            service_log(f"deliver: validation_send_failed product={product!r} promo_already_sent=True source={group!r}")

    if successfully_delivered:
        mark_messages_seen(successfully_delivered)
        clear_delivery_progress([str(m.get("messageId") or "") for m in successfully_delivered])

    write_collector_funnel(
        messages, ranked_messages, shortlist, validated_shortlist,
        eligible_messages, selected_messages, successfully_delivered,
    )
    run.data['timings_seconds']['delivery'] = round(time.monotonic() - delivery_started, 4)
    incomplete = len(successfully_delivered) < len(selected_messages)
    run.finish(
        'partial_delivery_retry_pending' if incomplete
        else ('delivered' if successfully_delivered else 'no_eligible'),
        candidates=len(messages), selected=len(selected_messages),
        delivered=len(successfully_delivered),
        bank_cache=resolve_approved_image.cache_info()._asdict(),
    )
    if incomplete:
        # Complete pairs were already marked seen and failed items were not. The
        # cursor intentionally stays put while unseen candidates exist, so the
        # next scheduled round retries only the incomplete items. A partial
        # bridge outage is operational state, not a crashing cron job.
        service_log(
            f"deliver: partial_delivery_retry_pending "
            f"selected={len(selected_messages)} delivered={len(successfully_delivered)}"
        )

    # No batch report. Breno prefers the collector to send only each promo + its
    # validation; the old summary was noisy and often misleading when source
    # headlines were parsed as products.


if __name__ == "__main__":
    run_exclusive(main)
