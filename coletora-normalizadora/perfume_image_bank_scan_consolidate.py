#!/usr/bin/env python3
"""Propose consolidation groups by SCANNING folders, not just aliases.json.

The existing safe-consolidation pass walks aliases.json, so it only ever sees
SKUs that already had an alias recorded (1 group on 2026-08-02). The real
fragmentation -- 325 SKUs across 516 redundant folders -- never entered the
alias file: each new promo title minted a fresh folder silently.

This pass buckets live folders by a coarse identity core and then validates
every pair with the SAME conservative safe_pair() from the alias-based script.
No new merge rule is introduced: volume, variant, kit, kit count, parfum
concentration and identity-core mismatches all still veto. Gender words stay
identity (Club de Nuit Woman must never absorb the generic folder).

Dry-run by default. --apply reuses apply_group() so moves, duplicate archiving,
rollback manifests and MIGRATED_TO markers behave exactly as the audited path.
Nothing is ever deleted.
"""
from __future__ import annotations
import argparse, importlib.util, json, re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BANK = HOME / '.hermes/perfume_image_bank'
PRODUCTS = BANK / 'products'
ALIASES = BANK / 'aliases.json'

_cs_path = HOME / '.hermes/scripts/perfume_image_bank_consolidate_safe.py'
_spec = importlib.util.spec_from_file_location('cs', _cs_path)
cs = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(cs)

# Coarse bucket key only. Its job is to keep the pair count tractable; every
# real decision is still made by cs.safe_pair().
BUCKET_NOISE = {
    'perfume', 'arabe', 'rabe', 'masculino', 'feminino', 'original', 'loja',
    'oficial', 'deo', 'desodorante', 'colonia', 'eau', 'de', 'toilette',
    'edp', 'edt', 'parfum', 'para', 'importado', 'importada', 'spray',
}


def bucket(name: str) -> tuple:
    norm = cs.normalize_folder_key(name)
    toks = [x for x in norm.split('-') if x]
    vol = [x for x in toks if re.fullmatch(r'\d{2,3}ml', x)]
    body = frozenset(
        x for x in toks
        if x not in BUCKET_NOISE and not re.fullmatch(r'\d{2,3}ml', x)
    )
    return (body, vol[-1] if vol else '')


# Gender is SKU, not filler (Club de Nuit Woman vs Club de Nuit; Scandal For Him
# vs Scandal). cs.safe_pair() strips masculino/feminino as CATEGORY_WORDS, which
# is fine for the alias path where a human recorded the link, but too loose for a
# blind scan: dry-run produced armaf-club-nuit-105ml absorbing
# perfume-armaf-club-de-nuit-edt-105ml-feminino. Veto any pair whose gender
# marking actually disagrees. Absence on one side is not disagreement -- most
# folders simply omit the word -- only two DIFFERENT genders are.
_MASC = {'masculino', 'homem', 'homens', 'men', 'man', 'him', 'homme', 'pour-homme'}
_FEM = {'feminino', 'mulher', 'mulheres', 'women', 'woman', 'her', 'femme'}


def gender_of(name: str) -> str:
    toks = {x for x in cs.normalize_folder_key(name).split('-') if x}
    m, f = bool(toks & _MASC), bool(toks & _FEM)
    if m and not f:
        return 'm'
    if f and not m:
        return 'f'
    return ''


def safe_pair_scan(a: str, b: str, bucket_genders: frozenset = frozenset()):
    """cs.safe_pair plus a strict gender veto for blind scanning.

    cs.safe_pair() strips masculino/feminino as CATEGORY_WORDS. That is fine on
    the alias path, where a human already asserted the link, but too loose for a
    blind scan: the dry-run had armaf-club-nuit-105ml absorbing
    perfume-armaf-club-de-nuit-edt-105ml-feminino, and the bank shows that line
    really does split (armaf-club-nuit-woman-105ml,
    perfume-rabe-club-de-nuit-intense-woman-edp-105ml exist as separate SKUs).

    So ANY gender disagreement blocks -- including marked vs unmarked, since an
    unmarked folder is ambiguous exactly when the house sells both. That leaves
    some genuinely-equal folders unmerged ("Yara feminino" vs "Yara"), which is
    the cheap error: an unmerged folder costs a lookup miss we can fix, a wrong
    merge publishes the wrong bottle.
    """
    ga, gb = gender_of(a), gender_of(b)
    if ga != gb:
        return False, 'gender_conflict' if (ga and gb) else 'gender_unmarked_ambiguous'
    return cs.safe_pair(a, b)


def build_scan_plan():
    live = [d.name for d in PRODUCTS.iterdir() if d.is_dir() and cs.images(d)]
    buckets = defaultdict(list)
    for name in live:
        buckets[bucket(name)].append(name)

    graph = defaultdict(set)
    rejected = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        members = sorted(members)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                ok, why = safe_pair_scan(a, b)
                if ok:
                    graph[a].add(b)
                    graph[b].add(a)
                else:
                    rejected.append({'source': a, 'target': b, 'reason': why})

    seen, groups = set(), []
    for node in sorted(graph):
        if node in seen:
            continue
        stack, comp = [node], []
        seen.add(node)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in graph[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        comp = [x for x in comp if cs.images(PRODUCTS / x)]
        if len(comp) < 2:
            continue

        def rank(x):
            d = PRODUCTS / x
            approved = len([p for p in cs.images(d) if 'approved_final' in p.parts])
            total = len(cs.images(d))
            explicit = 1 if (d / 'index.json').exists() else 0
            return approved, total, explicit, -len(x), x

        canonical = max(comp, key=rank)
        groups.append({
            'canonical': canonical,
            'sources': sorted(x for x in comp if x != canonical),
            'folders': sorted(comp),
            'image_count': sum(len(cs.images(PRODUCTS / x)) for x in comp),
        })
    return groups, rejected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='only act on N groups')
    args = ap.parse_args()

    groups, rejected = build_scan_plan()
    if args.limit:
        groups = groups[:args.limit]
    stamp = datetime.now(timezone.utc).isoformat()
    summary = {
        'created_at': stamp,
        'mode': 'apply' if args.apply else 'dry-run',
        'safe_groups': len(groups),
        'folders_to_merge': sum(len(g['sources']) for g in groups),
        'images_seen': sum(g['image_count'] for g in groups),
        'groups': groups,
        'rejected_edges': rejected,
    }
    if args.apply:
        aliases = json.loads(ALIASES.read_text())
        all_moves, all_dups = [], []
        for g in groups:
            moved, dups = cs.apply_group(g, stamp)
            all_moves += moved
            all_dups += dups
            for key in g['folders']:
                aliases[key] = [g['canonical']]
        ALIASES.write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
        )
        summary.update({
            'moved': len(all_moves),
            'exact_duplicates': len(all_dups),
            'moves': all_moves,
            'duplicates': all_dups,
        })
    out = BANK / 'migration_manifests' / (
        f'scan-consolidation-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{summary["mode"]}.json'
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')

    reasons = defaultdict(int)
    for r in rejected:
        reasons[r['reason']] += 1
    print(json.dumps({
        'mode': summary['mode'],
        'safe_groups': summary['safe_groups'],
        'folders_to_merge': summary['folders_to_merge'],
        'images_seen': summary['images_seen'],
        'rejected_edges': len(rejected),
        'rejected_by_reason': dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        'moved': summary.get('moved', 0),
        'exact_duplicates': summary.get('exact_duplicates', 0),
        'manifest': str(out),
        'sample_groups': groups[:12],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
