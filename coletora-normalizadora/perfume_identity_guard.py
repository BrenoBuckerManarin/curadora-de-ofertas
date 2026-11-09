"""Pure identity constraints independent of display slugs. Never writes the bank."""
import re
import unicodedata
import hashlib
import json
from functools import lru_cache
from pathlib import Path


def normalized(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).strip()


def gender(value):
    s = normalized(value)
    female = bool(re.search(r'\b(?:feminin[oa]|fem|for her|pour femme|woman|women|mulher|mulheres)\b', s))
    male = bool(re.search(r'\b(?:masculin[oa]|masc|for him|pour homme|man|men|homem|homens)\b', s))
    if re.search(r'\b(?:unissex|unisex|sem genero|compartilhavel)\b', s) or (female and male):
        return 'unissex'
    return 'feminino' if female else 'masculino' if male else ''


def headline_gender_conflict(headline, product):
    """Only explicit audience language can contradict an explicit SKU gender."""
    wanted = gender(product)
    if wanted not in {'masculino', 'feminino'}:
        return False
    text = normalized(headline)
    # Mentioning "mulheres" as admirers of a male perfume is not a female SKU.
    # Only audience/ownership language or an explicit gender label is a veto.
    female = bool(re.search(r'\b(?:ela|dela|delas|feminin[oa]s?|for her|pour femme|(?:pra|para|das?|pelas?)\s+(?:mulher(?:es)?|meninas?|garotas?|damas?))\b', text))
    male = bool(re.search(r'\b(?:ele|dele|deles|masculin[oa]s?|for him|pour homme|(?:pro|pra|para|dos?|pelos?)\s+(?:homem|homens|meninos?|garotos?|cavalheiros?))\b', text))
    female = female or '♀' in str(headline)
    male = male or '♂' in str(headline)
    if not female and not male:
        return False
    if wanted == 'masculino':
        return female
    if wanted == 'feminino':
        return male
    return False


def gender_safe_headline(headline, product):
    """Fail closed at publication even when a stale approved headline slips through."""
    if not headline_gender_conflict(headline, product):
        return headline
    if re.search(r'\bessencial\s+sentir\b', normalized(product)):
        return '*ESSENCIAL PRA SENTIR*'
    return ''


def fakhar_variant_conflict(left, right):
    """Fakhar Gold Extrait, Gold and Rose Gold are distinct fragrance identities."""
    a, b = set(normalized(left).split()), set(normalized(right).split())
    if 'fakhar' not in a or 'fakhar' not in b:
        return False
    variants = {'rose', 'gold', 'extrait'}
    return bool(a & variants and b & variants and (a & variants) != (b & variants))


def preserve_gender(title, source):
    """Restore an explicit source qualifier, never infer from brand/bottle."""
    wanted = gender(source)
    if not wanted or gender(title):
        return title
    match = re.search(r'\b\d{2,3}\s*ml\b', title, re.I)
    if match:
        return title[:match.start()].rstrip() + ' ' + wanted.capitalize() + ' ' + title[match.start():]
    return title.rstrip() + ' ' + wanted.capitalize()


def presentation(value):
    s = normalized(value)
    if re.search(r'\b(?:hair|cabelo|cabelos|capilar)\b', s):
        return 'hair'
    if re.search(r'\b(?:desodorante corporal|body spray)\b', s):
        return 'body_deodorant'
    if re.search(r'\bbody splash\b', s):
        return 'body_splash'
    return ''


def compatible(wanted, candidate, variants, *, strict=False):
    """strict forbids lost flankers; unknown gender is handled by folder evidence."""
    wg, cg = gender(wanted), gender(candidate)
    if wg and cg and wg != cg:
        return False
    wp, cp = presentation(wanted), presentation(candidate)
    if wp != cp:
        return False
    wt, ct = set(normalized(wanted).split()), set(normalized(candidate).split())
    wv, cv = wt & variants, ct & variants
    if strict and wv != cv:
        return False
    if not strict and wv and cv and not (wv <= cv or cv <= wv):
        return False
    return True


def fingerprint(path):
    stat = Path(path).stat()
    # Same-size rewrites can share a filesystem clock tick. These journals are
    # append-only/tail-revised; sample the tail too so a just-edited final cannot
    # remain hidden behind a stale metadata-only cache entry.
    with Path(path).open('rb') as handle:
        handle.seek(max(0, stat.st_size - 512))
        tail = hashlib.sha256(handle.read()).hexdigest()
    return (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, tail)


@lru_cache(maxsize=4096)
def _digest(stamp):
    return hashlib.sha256(Path(stamp[0]).read_bytes()).hexdigest()


def file_digest(path):
    try:
        return _digest(fingerprint(path))
    except OSError:
        return ''


@lru_cache(maxsize=2)
def human_final_records(cycles_stamp, feedback_stamp):
    """Join exact cycle/time/link; final selected image is evidence, not any echo."""
    feedback = {}
    for line in Path(feedback_stamp[0]).read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
            feedback[(row.get('ts'),row.get('link'))] = row
        except ValueError:
            continue
    records = []
    for line in Path(cycles_stamp[0]).read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        source = feedback.get((row.get('normalizer_ts'),row.get('link')), {})
        if not source or not row.get('edition_messageId'):
            continue
        image = row.get('image_change') or {}
        from perfume_round_guards import IMAGE_IDENTITY_HOLDS
        if image.get('after_sha256') in IMAGE_IDENTITY_HOLDS:
            continue
        hashes = {image.get('after_sha256') or ''}
        if image.get('class') == 'unchanged':
            hashes.add(image.get('before_sha256') or '')
        records.append((row, source.get('product') or '', frozenset(hashes - {''})))
    return tuple(records)
