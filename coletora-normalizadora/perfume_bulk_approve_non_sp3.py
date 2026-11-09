#!/usr/bin/env python3
"""Approve the clean curation backlog and archive watermarked-source material.

Breno's call (2026-08-02): approve everything that did NOT come from
"Fonte Teta 3", whose images carry the @sopromosperfumes tarja.

Two evidences decide the origin, because they disagree: index.json describes the
FIRST raw_item of the folder, which is not necessarily the file that got picked,
while the mined filename carries the real source (source_<label>_...). A folder
is treated as watermarked if EITHER says Fonte Teta 3.

Nothing is deleted. Approved files are COPIED into approved_final (original
stays in raw/candidate as provenance); watermarked ones are MOVED into
rejected_watermark_source/.
"""
from __future__ import annotations
import argparse, json, shutil
from datetime import datetime, timezone
from pathlib import Path

BANK = Path.home()/'.hermes/perfume_image_bank'
P = BANK/'products'
SP3_ID = '120363997452979005'
SP3_LABEL = 'Fonte Teta 3'
SP3_FILE_MARKS = ('so_promos3', 'so_promos_perfume_3', 'sopromos')


def origin_is_sp3(folder: str, rel: str) -> bool:
    if any(mark in Path(rel).name.lower() for mark in SP3_FILE_MARKS):
        return True
    idx = P/folder/'index.json'
    if idx.exists():
        try:
            d = json.loads(idx.read_text())
            for it in (d.get('raw_items') or []):
                cid = str(it.get('chat_id', '')).replace('@g.us', '')
                if cid == SP3_ID:
                    return True
        except Exception:
            pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    rows = json.load(open('/tmp/real.json'))  # [name, n, dim, rel]
    stamp = datetime.now(timezone.utc).isoformat()
    approved, archived, skipped = [], [], []

    for name, n, dim, rel in rows:
        src = P/rel
        if not src.exists():
            skipped.append({'folder': name, 'why': 'arquivo sumiu', 'rel': rel})
            continue
        if origin_is_sp3(name, rel):
            dest = P/name/'rejected_watermark_source'/src.name
            archived.append({'folder': name, 'from': str(src), 'to': str(dest)})
            if args.apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
        else:
            dest = P/name/'approved_final'/f'bulk_approved_{stamp[:10]}_{src.name}'
            approved.append({'folder': name, 'from': str(src), 'to': str(dest), 'dim': dim})
            if args.apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))
                idx = P/name/'index.json'
                if idx.exists():
                    try:
                        d = json.loads(idx.read_text())
                        d.setdefault('approved', []).append({
                            'file': dest.name,
                            'approved_at': stamp,
                            'mode': 'bulk_non_sp3_breno_20260802',
                        })
                        idx.write_text(json.dumps(d, ensure_ascii=False, indent=2)+'\n')
                    except Exception:
                        pass

    out = BANK/'migration_manifests'/(
        f'bulk-approval-{datetime.now().strftime("%Y%m%d-%H%M%S")}-'
        f'{"apply" if args.apply else "dry-run"}.json'
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        'created_at': stamp,
        'mode': 'apply' if args.apply else 'dry-run',
        'approved': approved,
        'archived_watermark': archived,
        'skipped': skipped,
    }, ensure_ascii=False, indent=2)+'\n')

    print(json.dumps({
        'mode': 'apply' if args.apply else 'dry-run',
        'aprovadas': len(approved),
        'arquivadas_watermark': len(archived),
        'puladas': len(skipped),
        'manifesto': str(out),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
