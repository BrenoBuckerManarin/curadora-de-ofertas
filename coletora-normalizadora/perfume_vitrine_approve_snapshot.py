#!/usr/bin/env python3
"""Promote only explicitly approved candidates from one Vitrine review snapshot."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, shutil
from datetime import datetime, timezone
from pathlib import Path

BANK = Path.home() / ".hermes/perfume_image_bank/products"
_PMN_PATH = Path.home() / ".hermes/scripts/perfume_manual_normalizer.py"
_spec = importlib.util.spec_from_file_location("pmn_vitrine_snapshot", _PMN_PATH)
assert _spec and _spec.loader
pmn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pmn)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", type=Path)
    args = ap.parse_args()
    report = json.loads(args.snapshot.read_text(encoding="utf-8"))
    items = report.get("new_candidates") or []
    if not items:
        raise SystemExit("snapshot sem candidatos")

    # Verify the complete batch before any write.
    for item in items:
        src = Path(item["media_path"])
        if not src.is_file():
            raise SystemExit(f"mídia ausente: {src}")
        actual = digest(src)
        if actual != item["sha256"]:
            raise SystemExit(f"hash divergente: {src}")

    promoted, duplicate = [], []
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        src = Path(item["media_path"])
        # Canonical folder wins over the snapshot's stored key: the same SKU may
        # already own a curated folder under a different spelling.
        key = pmn.resolve_bank_write_key(item.get("product") or item["product_key"].replace("-", " ")) or item["product_key"]
        pdir = BANK / key
        approved = pdir / "approved_final"
        approved.mkdir(parents=True, exist_ok=True)
        existing = []
        for p in approved.iterdir():
            if p.is_file() and digest(p) == item["sha256"]:
                existing.append(p)
        if existing:
            row = {**item, "decision": "already_approved_same_hash", "approved_final": str(existing[0]), "approved_at": now}
            duplicate.append(row)
        else:
            stamp = datetime.fromtimestamp(int(item["timestamp"]), timezone.utc).strftime("%Y%m%d-%H%M%S")
            dst = approved / f"approved_vitrine_breno_{stamp}_{item['sha256'][:12]}{src.suffix.lower()}"
            shutil.copy2(src, dst)
            row = {**item, "decision": "approved_by_breno_from_vitrine_2130_review", "approved_final": str(dst), "approved_at": now}
            promoted.append(row)

        pdir.mkdir(parents=True, exist_ok=True)
        with (pdir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        idx = pdir / "index.json"
        data = {"product_key": key, "canonical_guess": item["product"], "approved": [], "raw_items": [], "updated_at": now}
        if idx.exists():
            try: data.update(json.loads(idx.read_text(encoding="utf-8")))
            except Exception: pass
        approved_list = data.get("approved") or []
        if row["approved_final"] not in approved_list:
            approved_list.append(row["approved_final"])
        data["approved"] = approved_list
        data["updated_at"] = now
        idx.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    approval = {"approved_at": now, "approved_by": "Breno", "snapshot": str(args.snapshot), "requested": len(items), "promoted": len(promoted), "already_approved": len(duplicate), "items": promoted + duplicate}
    out = args.snapshot.with_name(args.snapshot.stem + "-approval.json")
    out.write_text(json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"requested": len(items), "promoted": len(promoted), "already_approved": len(duplicate), "report": str(out)}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
