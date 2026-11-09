#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path.home() / ".hermes/scripts/perfume_manual_normalizer.py"
spec = importlib.util.spec_from_file_location("pmn", SCRIPT)
pmn = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pmn)

# Store trust regression: Breno treats a verified seller badge on the exact
# offer as sufficient for the public Loja Oficial line. Unbadged sellers and
# unscoped names remain forbidden.
assert pmn.rendered_card_store_trust({
    "pdp_seller": "CÁTIA SAMPAIO COSMÉTICOS",
    "pdp_seller_badged": True,
}) == "_Loja Oficial *Cátia Sampaio Cosméticos*_"
assert pmn.rendered_card_store_trust({
    "pdp_seller": "VENDEDOR COMUM",
    "pdp_seller_badged": False,
}) is None

CASES = [
    ("Perfume Árabe Salvo EDP 100ml", True, "arabe-salvo-100ml"),
    ("Arabe Vulcan Black French Avenue Eua De Parfum 100ml", True, "arabe-vulcan-black"),
    ("Lab 8 Niche Effervescent 100ml", True, "lab-8-niche-effervescent-100ml"),
    ("Lab 8 Niche Effervescent Summer 100ml", True, "summer"),
    ("Natura Essencial Tradicional 100ml", True, "natura-essencial-tradicional-100ml"),
    ("Lattafa Bade'e Al Oud For Glory EDP 100ml", True, "for-glory"),
    ("Malbec Magnetic 100ml", True, "malbec-magnetic"),
    # 2026-08-30: marketplace merchandising words hid existing curated assets.
    ("Kit Presente Malbec Tradicional Masculino O Boticário Clássico Original", True, "o-boticario-malbec-tradicional-100ml"),
    ("Perfumes Colônia Miniatura Cebolinha Turma Da Mônica Jequiti 25ml", True, "cebolinha"),
    ("Eudora H Marine Colônia 100ml", True, "eudora-h-marine-100ml"),
    # Consolidated 2026-08-02: versace-eros-edt-100ml was merged into
    # versace-eros-100ml (MIGRATED_TO.json), which now holds the 3 approved
    # photos. Assert the canonical folder, not the retired spelling.
    ("Versace Eros Edt 100ml", True, "versace-eros"),
    # Short 2-token keys must resolve to a volume-qualified Khamrah folder; the
    # canonical folder spelling may include category/concentration after bank consolidation.
    ("LATTAFA KHAMRAH", True, "khamrah"),
    # "desodorante" is a category word, never identity.
    ("Clash Desodorante Colônia 100ml", True, "clash-100ml"),
    ("Mont Blanc Legend Spirit EDT 200ml", False, ""),
]

for product, should_find, needle in CASES:
    image = pmn.find_image(product, "")
    print(f"{product} -> {image or 'MISS'}")
    if should_find and not image:
        raise AssertionError(f"expected image for {product}")
    if needle and needle not in image:
        raise AssertionError(f"expected {needle!r} in image for {product}, got {image}")

# Safety checks: fuzzy must not cross distinct variants.
assert not pmn.image_bank_key_matches("lab-8-niche-effervescent-100ml", "unissex-lab-8-niche-effervescent-summer-100ml")
assert pmn.image_bank_fuzzy_score("lab-8-niche-effervescent-100ml", "unissex-lab-8-niche-effervescent-summer-100ml") == 0
assert pmn.image_bank_fuzzy_score("mont-blanc-legend-spirit-200ml", "montblanc-legend-spirit-200ml") > 0
# 2026-07-07: Eros EDT must never borrow Eros Flame (and vice versa).
assert not pmn.image_bank_key_matches("versace-eros-edt-100ml", "versace-eros-flame-100ml")
assert pmn.image_bank_fuzzy_score("versace-eros-edt-100ml", "versace-eros-flame-100ml") == 0
assert pmn.image_bank_fuzzy_score("versace-eros-flame-100ml", "versace-eros-edt-100ml") == 0
# Quasar Fire must not borrow Brave/Ice/Deep Blue siblings.
assert not pmn.image_bank_key_matches("quasar-fire-100ml", "quasar-brave-100ml")
assert pmn.image_bank_fuzzy_score("quasar-fire-100ml", "quasar-ice-100ml") == 0
assert pmn.image_bank_fuzzy_score("quasar-fire-100ml", "quasar-deep-blue-100ml") == 0
# Cardinality is SKU identity: a single unit must never borrow a kit image even
# when every unit token is a subset of the kit folder name (2026-08-02).
assert not pmn.image_bank_key_matches(
    "natura-essencial-tradicional-100ml",
    "kit-natura-essencial-classico-tradicional-100ml",
)
assert pmn.image_bank_fuzzy_score(
    "natura-essencial-tradicional-100ml",
    "kit-natura-essencial-classico-tradicional-100ml",
) == 0
# 2026-08-02 brand-house drift: the same Armani SKU lives under Giorgio/Emporio
# depending on the source title that created the folder. These must match...
assert pmn.image_bank_key_matches(
    "emporio-armani-stronger-with-you-100ml",
    "giorgio-armani-stronger-with-you-100ml",
)
assert pmn.image_bank_key_matches(
    "giorgio-armani-stronger-with-you-100ml",
    "emporio-armani-stronger-with-you-edt-100ml",
)
# ...but brand substitution must never also cross a concentration LINE or a
# flanker. Parfum is a different bottle from the EDT; Intensely is a flanker.
assert not pmn.image_bank_key_matches(
    "giorgio-armani-stronger-with-you-100ml",
    "armani-beauty-stronger-with-you-parfum-100ml",
)
assert not pmn.image_bank_key_matches(
    "armani-stronger-with-you-100ml",
    "armani-stronger-with-you-intensely-100ml",
)
# Volume spelling drift in folder names: "-100" and "-100-ml" are the same SKU
# as "-100ml" and must be reachable by lookup.
_vibe_keys = pmn.image_lookup_keys("Vibe Tropical Rayhaan 100ml")
assert "vibe-tropical-rayhaan-100" in _vibe_keys
assert "vibe-tropical-rayhaan-100-ml" in _vibe_keys
# 2026-08-02 brand-omission drift: curated folders built from short source
# titles omit the house name while the live promo carries it. Must match...
assert pmn.image_bank_key_matches(
    "jean-paul-gaultier-le-beau-le-125ml", "le-beau-le-125ml"
)
# ...but never across a DIFFERENT house, and never on a weak core.
assert not pmn.image_bank_key_matches("lattafa-asad-100ml", "armaf-asad-100ml")
assert not pmn.image_bank_key_matches(
    "french-avenue-vulcan-black-100ml", "lattafa-vulcan-black-100ml"
)
assert not pmn.image_bank_key_matches("armani-code-100ml", "code-100ml")

# --- Writer routing (2026-08-02) -------------------------------------------
# Every writer must land new photos in the canonical folder for the SKU, or the
# bank re-fragments one promo at a time. Reader and writer must not disagree.
_bank = pmn.IMAGE_BANK_PRODUCTS


def _write_key(title: str) -> str:
    return pmn.resolve_bank_write_key(title)


# Alias-mapped SKU routes to its canonical folder, not the raw slug.
assert _write_key("Perfume Attar Al Wesal 100ml EDP Al Wataniah") == (
    "al-wataniah-attar-al-wesal-edp-100ml"
)
# Brand-house drift routes to the folder that actually holds the curated set.
assert _write_key("Emporio Armani Stronger With You 100ml") == (
    "giorgio-armani-stronger-with-you-100ml"
)
# A genuinely new product still gets its own folder.
assert _write_key("Produto Inexistente Zzz 100ml") == "produto-inexistente-zzz-100ml"
assert not (_bank / "produto-inexistente-zzz-100ml").exists()
# A flanker must never be routed into the base SKU's folder.
assert _write_key("Armani Stronger With You Intensely 100ml") != (
    "giorgio-armani-stronger-with-you-100ml"
)
# Writer never points at a retired folder.
for _title in (
    "Perfume Attar Al Wesal 100ml EDP Al Wataniah",
    "Malbec Tradicional 100ml",
    "Turathi Blue Afnan EDP 90ml",
):
    assert not (_bank / _write_key(_title) / "MIGRATED_TO.json").exists()

# --- Editorial/title regressions (2026-08-30) -------------------------------
# Marketplace SEO is description, never canonical SKU identity.
assert pmn.compact_product_name("Lattafa Asad Zanzibar Limited Genuino 100ml") == (
    "Lattafa Asad Zanzibar Limited 100ml"
)
assert pmn.compact_product_name(
    "Rasasi Hawas 100ml Cítrico Fresco Toques Aquáticos"
) == "Rasasi Hawas 100ml"
assert pmn.compact_product_name(
    "212 VIP Men Black Body Blz Única EDP 100ml"
) == "212 VIP Men Black EDP 100ml"

# Human-confirmed inspiration must beat an interchangeable generic fallback.
_ctx = {
    "_product": "Maison Alhambra Glacier Ultra EDP",
    "known_inspiration": "Jean Paul Gaultier Ultra Male",
    "olfactive_terms": [], "safe_angles": [], "avoid_terms": [],
    "_recent_molds": {},
}
assert pmn.headline_score("DUPE DO ULTRA MALE", _ctx) > pmn.headline_score(
    "UM PERFUME BEM CHEIROSO", _ctx
)
print("OK perfume_image_lookup_regression")
