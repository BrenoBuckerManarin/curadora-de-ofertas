#!/usr/bin/env python3
"""
Perfume promo collector — reads WhatsApp tap file, extracts new messages
from perfume groups, tracks seen messageIds, outputs structured data.

Usage: python3 perfume_collector.py

Output: JSON array of new messages for cron job LLM processing.
State file: ~/.hermes/whatsapp/taps/.perfume_state.json
"""

import json
import os
import re
import sys
from pathlib import Path

# --- Price extraction (deterministic regex — no LLM hallucination) ---

def _strip_wa_formatting(text: str) -> str:
    """Strip WhatsApp bold/italic/strikethrough markers."""
    return re.sub(r'[_*~]', '', text)

def extract_price(body: str) -> str | None:
    """Extract price from promo message. Returns 'R$182' or None."""
    if not body:
        return None
    clean = _strip_wa_formatting(body)

    # Line-by-line: find line with BOTH "por" and "R$", extract from it.
    # This avoids matching "preço" from one line with "R$" from another.
    for line in clean.split('\n'):
        if re.search(r'(?i)(?:por|preço|preco)\s*:?\s*.*R[$]', line):
            m = re.search(r'R[$]\s*(\d[\d.,]*)', line)
            if m:
                value = m.group(1).strip()
                value = re.sub(r'\.(?=\d{3})', '', value)
                return f"R${value}"

    # Fallback: standalone "R$ xxx" line with price indicator
    for line in clean.split('\n'):
        if re.search(r'(?:💰|R[$]\s*\d)', line):
            m = re.search(r'R[$]\s*(\d[\d.,]*)', line)
            if m:
                value = m.group(1).strip()
                value = re.sub(r'\.(?=\d{3})', '', value)
                return f"R${value}"

    # Last resort: first R$ in text
    m = re.search(r'R[$]\s*(\d[\d.,]*)', clean)
    if m:
        value = m.group(1).strip()
        value = re.sub(r'\.(?=\d{3})', '', value)
        return f"R${value}"

    return None

def extract_cupom(body: str) -> str | None:
    """Extract coupon code from promo message."""
    if not body:
        return None
    clean = _strip_wa_formatting(body)
    # Patterns: "Cupom: ABC123" or "🎟️ Cupom: ABC123" or "com cupom ABC"
    m = re.search(r'(?i)(?:cupom|cupom|código|codigo)\s*:?\s*(?:aplicar\s*)?(\S+)', clean)
    if m:
        cupom = m.group(1).strip().rstrip('.,;:!').strip('`')  # strip backticks
        # Filter out non-coupon matches
        if cupom.lower() in ('não', 'nao', 'nenhum', 'sem', 'não menciona', 'não informado'):
            return None
        if cupom.startswith('R$') or cupom.startswith('r$'):
            return None  # "R$15 OFF" is a discount amount, not a coupon code
        if cupom.endswith('%') or cupom.replace('%','').isdigit():
            return None  # "15%" or "15" — discount percentage, not a code
        if re.search(r'(?i)\bOFF\b', cupom):
            return None  # "15% OFF" — discount instruction
        if len(cupom) < 3 or len(cupom) > 30:
            return None
        return cupom
    return None

# --- Config ---
TAP_FILE = os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl")
STATE_FILE = os.path.expanduser("~/.hermes/whatsapp/taps/.perfume_state.json")

# Perfume source groups (by JID)
PERFUME_GROUPS = {
    "120363991092469570": "#84 FONTE ALFA | PERFUMES",
    "120363995307638674": "FONTE EPSILON | 346",
    "120363997084299777": "Fonte Zeta | 133",
    "120363999077277988": "FONTE EPSILON | 348",
    "120363998926675391": "FONTE BETA #27",
    "120363994228670851": "Fonte Eta 03",
    "120363999145243998": "Promo da Medeirosa #101",
    "120363997452979005": "Fonte Teta 3",
    "120363992376882261": "FONTE GAMA #45",
    "120363993556613173": "Fonte Delta #52",
}

# --- Load state ---
state = {"seen_ids": [], "last_position": 0}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        pass

seen = set(state.get("seen_ids", []))
last_pos = state.get("last_position", 0)

# --- Read new entries ---
new_messages = []
if os.path.exists(TAP_FILE):
    with open(TAP_FILE) as f:
        # Seek to last known position (if file hasn't been truncated)
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        if file_size < last_pos:
            # File was rotated/truncated
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

            # Skip empty body (media-only without caption — rare in promo groups)
            body = (msg.get("body") or "").strip()
            if not body and not msg.get("hasMedia"):
                continue

            seen.add(mid)
            new_messages.append({
                "messageId": mid,
                "chatId": chat_id,
                "groupName": PERFUME_GROUPS.get(chat_id, chat_id),
                "body": body,
                "bodyClean": _strip_wa_formatting(body),  # sem *, _, ~ — pronto pra WhatsApp
                "price": extract_price(body),      # deterministic — script, not LLM
                "cupom": extract_cupom(body),      # deterministic — script, not LLM
                "hasMedia": msg.get("hasMedia", False),
                "mediaType": msg.get("mediaType", ""),
                "mediaPath": msg.get("mediaPath", ""),
                "timestamp": msg.get("timestamp", 0),
            })

        last_pos = f.tell()

# --- Save state ---
state["seen_ids"] = list(seen)[-5000:]  # Keep last 5000
state["last_position"] = last_pos
Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
with open(STATE_FILE, "w") as f:
    json.dump(state, f)

# --- Output ---
print(json.dumps(new_messages, ensure_ascii=False, indent=2))
