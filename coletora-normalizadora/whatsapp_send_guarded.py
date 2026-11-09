#!/usr/bin/env python3
"""Guarded WhatsApp sender for Breno's Hermes.

Purpose: WhatsApp is not a conversational Hermes platform. This script is the
only approved outbound path for automations that must post to WhatsApp.
It hard-blocks every destination except the allowlist and never sends a visible
WhatsApp error/status/start/stop notice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000").rstrip("/")
for _endpoint in ("/send-media", "/send", "/typing", "/edit"):
    if BRIDGE_URL.endswith(_endpoint):
        BRIDGE_URL = BRIDGE_URL[: -len(_endpoint)]
        break
ALLOWLIST = {
    "120363993116679759@g.us",  # Vitrine da Equipe
    "120363997955672313@g.us",  # promo
    "120363999570877129@g.us",  # Automações
    "120363997452979005@g.us",  # Normalizador Promo
    "120363995412228683@g.us",  # Normalizador Promo (active test group)
}
OUTBOUND_FEEDBACK_REGISTRY = Path.home() / ".hermes/whatsapp/outbound_media.jsonl"


def normalize_chat_id(chat_id: str) -> str:
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    if "@" not in cid and cid.isdigit():
        cid = f"{cid}@g.us"
    return cid.lower()


def allowed(chat_id: str) -> bool:
    return normalize_chat_id(chat_id) in ALLOWLIST


def post(endpoint: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if not body:
            return {"success": True}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"success": resp.status < 400, "raw": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded WhatsApp outbound sender")
    parser.add_argument("--chat", required=True, help="WhatsApp chat JID, e.g. 120...@g.us")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Text message to send")
    group.add_argument("--media", help="Local media path to send")
    parser.add_argument("--caption", default="", help="Caption for --media")
    parser.add_argument("--media-type", default="image", choices=["image", "video", "audio", "document"])
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not call bridge")
    args = parser.parse_args()

    chat_id = normalize_chat_id(args.chat)
    if not allowed(chat_id):
        print(json.dumps({"success": False, "blocked": True, "chatId": chat_id, "reason": "destination_not_allowlisted"}))
        return 3

    if args.dry_run:
        print(json.dumps({"success": True, "dry_run": True, "chatId": chat_id}))
        return 0

    media_path = ""
    try:
        if args.text is not None:
            result = post("/send", {"chatId": chat_id, "message": args.text}, args.timeout)
        else:
            media_path = os.path.abspath(os.path.expanduser(args.media))
            if not Path(media_path).exists():
                print(json.dumps({"success": False, "error": f"file_not_found: {media_path}"}))
                return 2
            result = post(
                "/send-media",
                {
                    "chatId": chat_id,
                    "filePath": media_path,
                    "mediaType": args.media_type,
                    "caption": args.caption,
                },
                max(args.timeout, 60),
            )
        if args.media and result.get("success") and result.get("messageId"):
            OUTBOUND_FEEDBACK_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
            with OUTBOUND_FEEDBACK_REGISTRY.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "messageId": str(result["messageId"]),
                    "chatId": chat_id,
                    "automation": "perfume" if chat_id == "120363993116679759@g.us" else "normalizer",
                    "media_path": media_path,
                    "caption": args.caption,
                }, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("success") else 1
    except urllib.error.URLError as exc:
        # Local-only failure. Never send any WhatsApp notification about this.
        print(json.dumps({"success": False, "error": f"bridge_unavailable: {exc}"}))
        return 1
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
