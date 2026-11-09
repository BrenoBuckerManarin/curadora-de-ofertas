#!/usr/bin/env python3
"""Mine Breno's corrected perfume posts as gold-standard examples.

Reads the WhatsApp tap for Breno/fromMe media posts in the promo group, copies the
original cached media bytes into the perfume image bank approved_final folder, and
stores the corrected caption as editorial reference. Never sends messages.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

TAP = Path.home() / ".hermes/whatsapp/taps/messages.jsonl"
BANK = Path.home() / ".hermes/perfume_image_bank/products"
REPORT = Path.home() / ".hermes/perfume_image_bank/breno_corrections/breno_corrections_scan.json"
SCRIPT = Path.home() / ".hermes/scripts/perfume_manual_normalizer.py"
EDITORIAL_COMPARATOR = Path.home() / ".hermes/scripts/perfume_comparison_refresh_new.py"
DEFAULT_GROUPS = [
    "120363997955672313@g.us",  # old promo group where Breno corrected final posts
    "120363995412228683@g.us",  # current normalizador/promo intake; use only latest by link
    "120363991547992305@g.us",  # current correction/gold-standard perfume group
]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Families where a pretty bottle from a sibling flanker is a recurring risk.
# OCR is free/local; when it cannot confirm one of these identities, fail closed
# into a pending queue for selective vision QC instead of poisoning approved_final.
RISKY_IDENTITY_TERMS = {
    "hawas", "rayhaan", "asad", "fakhar", "salvo", "khamrah", "yara",
    "le male", "invictus", "212", "good girl", "club de nuit", "kit",
}
VARIANT_MARKERS = {
    "black", "gold", "green", "blue", "kobra", "cobra", "elixir", "nocturno",
    "intense", "intenso", "absolu", "bourbon", "zanzibar", "platin", "platinum",
}


def image_ocr_text(media: Path) -> str:
    if not media.is_file() or not Path("/usr/bin/tesseract").exists():
        return ""
    try:
        proc = subprocess.run(
            ["/usr/bin/tesseract", str(media), "stdout", "-l", "por+eng", "--psm", "11"],
            text=True, capture_output=True, timeout=30,
        )
        return re.sub(r"\s+", " ", proc.stdout or "").strip()
    except Exception:
        return ""


def correction_identity_qc(product: str, media: Path) -> tuple[str, str, str]:
    """Cheap identity gate: approve normal cases; quarantine contradictions/risk."""
    from perfume_round_guards import image_identity_hold, EXPLICIT_IMAGE_APPROVALS
    if EXPLICIT_IMAGE_APPROVALS.get(pmn.file_digest(media)) == pmn.product_key(product):
        return "identity_ok", "breno_explicit_exact_asset_approval", ""
    hold = image_identity_hold(media)
    if hold:
        return "pending_identity_qc", hold, ""
    ocr = image_ocr_text(media)
    expected = pmn.product_key(product).replace("-", " ")
    observed = pmn.product_key(ocr).replace("-", " ") if ocr else ""
    # A family/brand word alone cannot prove every component of a kit, nor
    # distinguish SAZ from KAMAN on a mixed bottle/box photo. These go to
    # selective visual review; do not teach their identity from an Edição echo.
    if re.search(r'\b(?:kit|combo|conjunto)\b', expected):
        return "pending_identity_qc", "kit_components_require_visual_review", ocr
    symphony = {'saz', 'kaman'}
    wanted_symphony = set(expected.split()) & symphony
    observed_symphony = set(observed.split()) & symphony
    if wanted_symphony and observed_symphony != wanted_symphony:
        return "pending_identity_qc", "symphony_variant_not_exclusively_confirmed", ocr
    expected_markers = {m for m in VARIANT_MARKERS if re.search(rf"\b{re.escape(m)}\b", expected)}
    observed_markers = {m for m in VARIANT_MARKERS if re.search(rf"\b{re.escape(m)}\b", observed)}
    contradictions = observed_markers - expected_markers
    if contradictions and expected_markers:
        return "pending_identity_qc", "ocr_variant_conflict:" + ",".join(sorted(contradictions)), ocr
    risky = any(term in expected for term in RISKY_IDENTITY_TERMS)
    core = {
        t for t in expected.split()
        if len(t) >= 4 and t not in {"perfume", "arabe", "parfum", "original", "masculino", "feminino"}
        and not re.fullmatch(r"\d+ml", t)
    }
    confirmed = bool(core and any(re.search(rf"\b{re.escape(t)}\b", observed) for t in core))
    if risky and not confirmed:
        return "pending_identity_qc", "risky_family_not_confirmed_by_ocr", ocr
    return "identity_ok", "ocr_confirmed" if confirmed else "human_trusted_no_conflict", ocr

spec = importlib.util.spec_from_file_location("pmn", SCRIPT)
assert spec is not None and spec.loader is not None
pmn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pmn)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def msg_ts(raw) -> int:
    if isinstance(raw, dict):
        raw = raw.get("low")
    if raw is None:
        return int(datetime.now(timezone.utc).timestamp())
    try:
        return int(raw)
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())


def extract_corrected_product(body: str) -> str:
    lines = [x.strip() for x in (body or "").splitlines() if x.strip()]
    # Breno's final format is usually: headline, optional trust, product, price...
    # Never treat the first line as product unless it has hard product structure.
    for idx, raw in enumerate(lines):
        line = raw.strip(" *_`~")
        # In a Breno final post line 0 is editorial copy, never the SKU. The
        # former "hard SKU structure" exception still misread *KIT COMPLETO*
        # as a product and contaminated the generic `kit-completo` key.
        if idx == 0:
            continue
        if re.search(r"(?i)https?://|cupom|^de\b|\bpor\b|loja|oficial|selecion", line):
            continue
        if re.search(r"(?i)\b(perfume|edp|edt|parfum|body\s+splash|primacial|barbarius|malbec|rabanne|mercedes|rayhaan|lattafa|mawwal|azzaro|armani|attracione|french avenue|botic[aá]rio|natura|asad|salvo)\b|\b\d{2,3}\s*ml\b", line):
            return line[:110]
    product = pmn.extract_product(body)
    if product and product != "Produto":
        return product
    return "Produto"


def resolve_write_key(product: str) -> str:
    """Route new correction photos into an existing canonical bank folder.

    aliases.json is the durable migration map: once synonymous folders are
    consolidated, the miner must append to the canonical folder instead of
    recreating the old spelling on the next corrected post.

    Delegates to the normalizer so every writer in the bank shares ONE routing
    rule (2026-08-02). The local implementation below is kept only as a
    fallback for the case where the shared helper is unavailable.
    """
    shared = getattr(pmn, "resolve_bank_write_key", None)
    if callable(shared):
        try:
            resolved = shared(product)
            if resolved:
                return str(resolved)
        except Exception:
            pass
    key = pmn.product_key(product)
    aliases_path = BANK.parent / "aliases.json"
    try:
        payload = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {}
        values = payload.get(key) if isinstance(payload, dict) else None
        candidate_values: list[str] = []
        if isinstance(values, str):
            candidate_values.append(values)
        elif isinstance(values, list):
            candidate_values.extend(str(x) for x in values if x)
        # Historical alias keys may retain noise words that product_key now
        # removes. Mirror the normalizer's backward-compatible alias lookup so
        # the writer and reader always resolve to the same canonical folder.
        if isinstance(payload, dict):
            for alias_key, alias_values in payload.items():
                if pmn.product_key(str(alias_key).replace("-", " ")) != key:
                    continue
                if isinstance(alias_values, str):
                    candidate_values.append(alias_values)
                elif isinstance(alias_values, list):
                    candidate_values.extend(str(x) for x in alias_values if x)
        wanted_concentration = pmn.product_concentration_key(product)
        ranked_candidates: list[tuple[int, str]] = []
        for candidate in candidate_values:
            canonical = str(candidate or "").strip()
            target = BANK / canonical
            candidate_concentration = pmn.product_concentration_key(canonical)
            if wanted_concentration and candidate_concentration and candidate_concentration != wanted_concentration:
                continue
            if canonical and target.is_dir() and not (target / "MIGRATED_TO.json").exists():
                rank = 2 if wanted_concentration and candidate_concentration == wanted_concentration else 1
                ranked_candidates.append((rank, canonical))
        if ranked_candidates and max(row[0] for row in ranked_candidates) >= 2:
            return sorted(ranked_candidates, key=lambda row: row[0], reverse=True)[0][1]
        if wanted_concentration:
            exact_existing: list[str] = []
            for tier_dir in pmn.approved_final_candidates(product):
                product_dir = tier_dir.parent
                if (
                    product_dir.is_dir()
                    and not (product_dir / "MIGRATED_TO.json").exists()
                    and pmn.product_concentration_key(product_dir.name) == wanted_concentration
                ):
                    exact_existing.append(product_dir.name)
            if exact_existing:
                return sorted(set(exact_existing))[0]
        if ranked_candidates:
            return sorted(ranked_candidates, key=lambda row: row[0], reverse=True)[0][1]
    except Exception:
        pass
    return key


def existing_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for sub in ("approved_final", "human_final"):
        d = path / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file():
                try:
                    hashes.add(sha256(p))
                except Exception:
                    pass
    return hashes


def is_breno_or_bot_final(obj: dict) -> bool:
    sender = (obj.get("senderName") or "").lower()
    return bool(obj.get("fromMe") or "breno" in sender)


def iter_rows(groups: list[str]):
    if not TAP.exists():
        return
    with TAP.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("chatId") not in groups:
                continue
            if not is_breno_or_bot_final(obj):
                continue
            # The bridge's canonical event calls the downloaded local files
            # `mediaUrls`; older tap consumers use `mediaPath`. Normalize at
            # the boundary so every new Edição SKU is ingestible regardless of
            # which tap schema produced the row.
            if not obj.get("mediaPath"):
                media_urls = obj.get("mediaUrls") or []
                if isinstance(media_urls, list) and media_urls:
                    obj = dict(obj)
                    obj["mediaPath"] = media_urls[0]
            if not obj.get("hasMedia") or not obj.get("mediaPath"):
                continue
            yield line_no, obj


def _normalizer_output_by_link() -> dict[str, dict]:
    """What the normalizer itself published, keyed by link.

    Breno's edit loop is: normalizer version -> his corrected version(s), always
    on the SAME link. So a correction is only authoritative when it actually
    DIFFERS from our own output. Without this, we re-learn our own bugs as if
    Breno had approved them (2026-07-26 Malbec/Kaiak incident).
    """
    out: dict[str, dict] = {}
    log = Path.home() / ".hermes/perfume_image_bank/logs/normalizer_feedback.jsonl"
    if not log.exists():
        return out
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
            if link:
                out[link] = obj
    except Exception:
        return {}
    return out


def _source_raw_hashes() -> set[str]:
    """Hashes of raw source photos already indexed in the bank.

    A "final" image byte-identical to a source raw is not a curated asset: it is
    the source photo passed through, hand and competitor watermark included.
    Breno's rule is explicit — reusing an image visually identical to the one
    received is NOT a valid swap/approval. Cache it once per run.
    """
    global _RAW_HASHES
    if _RAW_HASHES is not None:
        return _RAW_HASHES
    out: set[str] = set()
    try:
        for pdir in BANK.iterdir():
            raw = pdir / "raw"
            if not raw.is_dir():
                continue
            for f in raw.iterdir():
                if f.is_file():
                    try:
                        out.add(sha256(f))
                    except Exception:
                        pass
    except Exception:
        pass
    _RAW_HASHES = out
    return out


_RAW_HASHES: set[str] | None = None


def mine(groups: list[str], limit: int = 5000) -> dict:
    BANK.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    rows = list(iter_rows(groups))[-limit:]
    # Breno often posts: raw/auto attempt → one or more corrections. For the same
    # final link, only the latest media message across the promo/normalizer/
    # correction groups is the gold-standard image/caption.
    latest_by_link: dict[str, tuple[int, dict]] = {}
    versions_by_link: dict[str, int] = {}
    no_link: list[tuple[int, dict]] = []
    for line_no, obj in rows:
        body = obj.get("body") or ""
        link = pmn.extract_link(body) or ""
        if link:
            latest_by_link[link] = (line_no, obj)
            versions_by_link[link] = versions_by_link.get(link, 0) + 1
        else:
            no_link.append((line_no, obj))
    latest_rows = no_link + list(latest_by_link.values())
    # Intake maintenance is time-bounded: newest human corrections must not sit
    # behind expired historical cache paths on every invocation.
    latest_rows.sort(key=lambda row: row[0], reverse=True)
    rows = latest_rows
    items = []
    copied = skipped_dup = missing_media = rejected = pending_identity = 0
    normalizer_out = _normalizer_output_by_link()
    for line_no, obj in rows:
        body = (obj.get("body") or "").strip()
        # Explicit simulation posts in Edição are diagnostics, never human gold.
        if "🧪 TESTE ALEATÓRIO" in body.upper():
            continue
        media = Path(obj.get("mediaPath") or "")
        product = extract_corrected_product(body)
        source_key = pmn.product_key(product)
        key = resolve_write_key(product) if media.is_file() else source_key
        link = pmn.extract_link(body) or ""
        mine_out = normalizer_out.get(link) or {}
        # Did Breno actually change the product identity, or is this just our
        # own output echoed back? Only a real divergence is a human decision.
        our_product = str(mine_out.get("product") or "")
        edited_product = bool(
            our_product and product
            and pmn.product_key(our_product) != source_key
        )
        item = {
            "chatId": obj.get("chatId"),
            "senderName": obj.get("senderName") or "",
            "line_no": line_no,
            "messageId": obj.get("messageId"),
            "ts": msg_ts(obj.get("timestamp")),
            "product": product,
            "source_product_key": source_key,
            "product_key": key,
            "headline": next((x.strip() for x in body.splitlines() if x.strip()), ""),
            "body": body,
            "media_original": str(media),
            "exists": media.is_file(),
            "link": link,
            "versions_for_link": versions_by_link.get(link, 0),
            "normalizer_product": our_product,
            "edited_product": edited_product,
        }
        if not media.is_file():
            missing_media += 1
            items.append(item)
            continue
        if key in {"produto", "unknown-product"} or product.strip().lower() == "produto":
            rejected += 1
            item["decision"] = "rejected_unresolved_product"
            items.append(item)
            continue
        # 2026-07-07: generic/weak keys like "75ml"/"100ml" poison the bank —
        # any same-volume perfume could later borrow this image. Require at
        # least 2 non-volume identity tokens, unless the single token is a
        # known family/brand (malbec-100ml is legit via family expansion).
        core_tokens = [p for p in key.split("-") if p and not re.fullmatch(r"\d{2,3}ml", p)]
        known_families = set(getattr(pmn, "CANONICAL_FAMILY_EXPANSIONS", {}) or {})
        if len(core_tokens) < 2 and not (core_tokens and core_tokens[0] in known_families):
            rejected += 1
            item["decision"] = "rejected_generic_weak_key"
            items.append(item)
            continue
        if media.suffix.lower() not in IMAGE_EXT:
            rejected += 1
            item["decision"] = "rejected_non_image_ext"
            items.append(item)
            continue
        # Echo guard: only ONE version on this link AND the product matches what
        # the normalizer itself published => Breno never edited it. Storing the
        # image is fine, but calling it a human decision lets our own bugs come
        # back as authoritative training data (Malbec/Kaiak, 2026-07-26).
        echo_only = (
            versions_by_link.get(link, 0) <= 1
            and bool(our_product)
            and not edited_product
        )
        pdir = BANK / key
        h = sha256(media)
        # Source-photo guard: an image identical to a raw source photo is not a
        # curated approval (hand/watermark passthrough). Breno may have been
        # approving the TITLE while the image was still the source one — the two
        # decisions must not be conflated (Liz Flora, 2026-07-26).
        if h in _source_raw_hashes():
            rejected += 1
            item["decision"] = "rejected_source_photo_passthrough"
            items.append(item)
            continue
        identity_decision, identity_reason, ocr_text = correction_identity_qc(product, media)
        item.update({
            "identity_qc": identity_decision,
            "identity_reason": identity_reason,
            "identity_ocr": ocr_text[:1000],
        })
        if identity_decision != "identity_ok":
            pending_dir = pdir / "pending_identity_qc"
            pending_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(item["ts"], timezone.utc).strftime("%Y%m%d-%H%M%S")
            dst = pending_dir / f"breno_pending_{stamp}_{h[:12]}{media.suffix.lower()}"
            # A pre-gate run may already have promoted the same bad bytes. Move
            # one matching legacy file directly to the pending destination;
            # never delete, and keep rollback simple.
            legacy_match: Path | None = None
            for tier in ("approved_final", "human_final"):
                tier_dir = pdir / tier
                if not tier_dir.is_dir():
                    continue
                for old in tier_dir.iterdir():
                    if not old.is_file() or old.suffix.lower() not in IMAGE_EXT:
                        continue
                    try:
                        if sha256(old) == h:
                            legacy_match = old
                            break
                    except Exception:
                        continue
                if legacy_match:
                    break
            if legacy_match and not dst.exists():
                shutil.move(str(legacy_match), str(dst))
            elif not dst.exists():
                shutil.copy2(media, dst)
            pending_identity += 1
            item.update({"pending_identity_qc": str(dst), "decision": "pending_identity_qc"})
            items.append(item)
            continue
        if h in existing_hashes(pdir):
            skipped_dup += 1
            item["decision"] = "skipped_duplicate"
            items.append(item)
            continue
        approved_dir = pdir / "approved_final"
        approved_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(item["ts"], timezone.utc).strftime("%Y%m%d-%H%M%S")
        dst = approved_dir / f"breno_final_{stamp}_{h[:12]}{media.suffix.lower()}"
        # Preserve original bytes. No recompress, no resize: Breno's final image is the approval source.
        shutil.copy2(media, dst)
        copied += 1
        item.update({"sha256": h, "approved_final": str(dst),
                     "decision": "normalizer_echo_not_human_edit" if echo_only
                     else "breno_final_human_approved"})
        idx = pdir / "index.json"
        data = {"product_key": key, "canonical_guess": product, "approved": [], "raw_items": [], "updated_at": None}
        if idx.exists():
            try:
                data.update(json.loads(idx.read_text(encoding="utf-8")))
            except Exception:
                pass
        raw_items = data.get("raw_items") or []
        raw_items.append({"media_path": str(dst), "body": body, "product_guess": product, "source_group_label": "breno-corrected-final", "sha256": h})
        approved = data.get("approved") or []
        approved.append({"media_path": str(dst), "body": body, "product_guess": product, "source_group_label": "breno-corrected-final", "sha256": h})
        data.update({"product_key": key, "canonical_guess": product, "raw_items": raw_items[-30:], "approved": approved[-30:], "updated_at": datetime.now(timezone.utc).isoformat()})
        idx.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with (pdir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({**item, "indexed_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
        items.append(item)
    report = {"groups": groups, "scanned_breno_or_bot_media": len(rows), "copied": copied, "pending_identity_qc": pending_identity, "skipped_duplicates": skipped_dup, "missing_media": missing_media, "rejected": rejected, "created_at": datetime.now(timezone.utc).isoformat(), "items": items}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", action="append", dest="groups", help="Group JID to scan; repeatable. Defaults to known Breno correction groups.")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()
    groups = args.groups or DEFAULT_GROUPS
    report = mine(groups, args.limit)
    # Refresh the silent Normalizer -> final Edição dataset whenever this miner
    # scans the correction group. Failure is reported locally and never blocks
    # image mining; the comparator has no delivery or posting side effects.
    comparator = subprocess.run(
        ["python3", str(EDITORIAL_COMPARATOR)],
        text=True, capture_output=True, timeout=120,
    )
    report["editorial_comparator"] = {
        "ok": comparator.returncode == 0,
        "output": (comparator.stdout or comparator.stderr or "").strip()[-1000:],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["groups", "scanned_breno_or_bot_media", "copied", "pending_identity_qc", "skipped_duplicates", "missing_media", "rejected", "created_at"]}, ensure_ascii=False, indent=2))
    return 0 if comparator.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
