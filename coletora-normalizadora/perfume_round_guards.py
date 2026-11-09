"""Scoped, offline guards from the 2026-09-08 round. No bank writes or network."""
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

_HEADLINE_DEADLINE: ContextVar[float | None] = ContextVar('perfume_headline_deadline', default=None)


@contextmanager
def headline_budget(seconds):
    existing = _HEADLINE_DEADLINE.get()
    deadline = time.monotonic() + max(0.0, float(seconds))
    token = _HEADLINE_DEADLINE.set(min(existing, deadline) if existing is not None else deadline)
    try:
        yield
    finally:
        _HEADLINE_DEADLINE.reset(token)


def remaining_headline_seconds(default):
    deadline = _HEADLINE_DEADLINE.get()
    return max(0.0, min(float(default), deadline - time.monotonic())) if deadline is not None else float(default)


def kit_signature(value):
    from perfume_identity_guard import normalized
    text = normalized(value)
    tokens = set(text.split())
    is_kit = bool(tokens & {'kit', 'combo', 'conjunto'})
    counts = set(re.findall(r'\b(\d{1,2})\s*(?:itens|items|unidades?|un|pecas|splashes|perfumes)\b', text))
    counts.update(re.findall(r'\b(?:kit|combo|conjunto)\s+(\d{1,2})\b', text))
    text = re.sub(r'\b(?:marca linha|alta fixacao|alta concentracao|presente perfeito)\b', ' ', text)
    tokens = set(text.split())
    # Presentation and component names remain identity. Gender, house fillers and
    # packaging units do not make an otherwise generic kit specific.
    fillers = {'kit','combo','conjunto','com','de','do','da','e','o','a','para',
               'masculino','feminino','unissex','unisex','original','perfume','perfumes',
               'edp','edt','eau','parfum','toilette','colonia','itens','items','un','unidade',
               'unidades','pecas','ml','boticario','boticrio','primacial','cada','desodorante'}
    core = {t for t in tokens - fillers if not re.fullmatch(r'\d+(?:ml)?', t)}
    return is_kit, core, counts


def kit_identity_matches(wanted, candidate):
    wk, wc, wn = kit_signature(wanted)
    ck, cc, cn = kit_signature(candidate)
    if not wk and not ck:
        return True
    if wk != ck or (wn and cn and wn != cn):
        return False
    distinctive = wc - {'body','splash','splashes','hidratante','locao','corporal','spray','presente'}
    return bool(distinctive) and wc == cc


# Breno explicitly approved this exact Symphony SAZ photo after visual review.
# Approval is scoped to the asset AND product; no family-wide OCR bypass.
IMAGE_IDENTITY_HOLDS: dict[str, str] = {
    'db3127bb9cbffbbf4da3b63fc90dd5403deeae7f11cf79b05b25b666a43302f2':
        '2026-09-12 visual audit: Pulse regular bottle/box filed as Pulse Action; preserve for relocation',
    # Visual audit 2026-09-11: bottle/box says only Essencial, not Mirra/Oud.
    # Same bytes were learned into three different SKUs. Preserve files; exclude
    # from selection/learning until exact SKU attribution is reviewed.
    '11e6f8724a83a594236f719142569b7a0eee5b2ef09babea28716d9bd9abb322':
        'Essencial photo cross-attributed to Tradicional/Mirra/Oud; identity review pending',
}
EXPLICIT_IMAGE_APPROVALS = {
    'e4a97c96d3b4a33f4ed4924e3151897ef61fddd38617da9a5a845c70e4206d3e':
        'mawwal-symphony-saz-100ml',
}


# Identity verification, NOT a new human quality approval. The asset must still
# pass the existing final-tier, hold, variant and quality gates. Visual label:
# Eudora Pulse Action 100ml. Male identity: manufacturer's exact product page
# https://www.eudora.com.br/eudora-pulse-action-desodorante-colonia-100ml/
REVIEWED_ASSET_IDENTITIES = {
    'f5f5bbb8da9dd19acbf68b5a3d3ae344a7133f7dede87e7c4425dd7fe7d652d8':
        'Eudora Pulse Action Desodorante Colônia Masculino 100ml',
    # Breno's visual correction 2026-09-22: purple bottle/box is the feminino
    # Exclusivo, despite living in an unqualified folder. Identity only; keep
    # the image available to its correct SKU, never to Masculino.
    'b1e3293f7825a4a12a57b3ae4b3051fdd72aaf671fb1ba245da6fceaa095938e':
        'Natura Essencial Exclusivo Feminino 100ml',
    '1309c65385c0009522d2020cee96e629ff1ed56c3e66526bdfa5d132dc2a23ea':
        'Natura Essencial Exclusivo Feminino 100ml',
    # Different flanker visually reads Essencial Supreme, not Exclusivo.
    '57faca666db10b61d1ef8fcdf2f9edb90f4d8a32d4c3441349e9759316f3b7ab':
        'Natura Essencial Supreme 100ml',
}


def image_identity_hold(media):
    from perfume_identity_guard import file_digest
    return IMAGE_IDENTITY_HOLDS.get(file_digest(media), '')


def visually_similar_review(before, after, perceptual_distance):
    """A soft ambiguity label, not approval, identity proof, or same-image truth."""
    if perceptual_distance is None or not 0 <= perceptual_distance <= 16:
        return False
    try:
        from PIL import Image, ImageChops, ImageStat
        with Image.open(before) as a, Image.open(after) as b:
            a = a.convert('RGB').resize((256, 256))
            b = b.convert('RGB').resize((256, 256))
            mean = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
        return mean < 8
    except (OSError, ValueError):
        return False
