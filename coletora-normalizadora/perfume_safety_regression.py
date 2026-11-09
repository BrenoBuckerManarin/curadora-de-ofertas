#!/usr/bin/env python3
from pathlib import Path
import sys
sys.path.insert(0, '/home/ubuntu/.hermes/scripts')
import perfume_manual_normalizer as pmn
import perfume_breno_correction_miner as miner

# Coupon tokens beginning with CUPOM are data, not labels.
assert pmn.extract_cupons('Cupom: CUPOMVAI ou 8DO8MELI') == ['CUPOMVAI', '8DO8MELI']
assert pmn.extract_cupons('Cupom: MELIACHAPROMO ou CUPOMLIBERADO') == ['MELIACHAPROMO', 'CUPOMLIBERADO']
assert pmn.extract_cupons('Use o cupom CUPOMVAI') == ['CUPOMVAI']
assert pmn.extract_cupons('Cupom: CUPOM') == []

# Catalog labels are hard failures; concrete editorial lines remain eligible.
assert pmn.headline_looks_like_catalog_title('Rayhaan Pacific Aura EDP 100ml', 'Rayhaan Pacific Aura EDP 100ml')
assert pmn.headline_looks_like_catalog_title('Lattafa Khamrah EDP 100ml', 'Lattafa Khamrah EDP 100ml')
assert not pmn.headline_looks_like_catalog_title('TÂMARAS COM CANELA', 'Lattafa Khamrah EDP 100ml')
assert not pmn.headline_looks_like_catalog_title('ÁRABE ABSURDO', 'Lattafa Asad 100ml')  # style penalty, not hard veto

# Explicit social watermarks are ineligible.
watermark_cases = [
    Path('/home/ubuntu/.hermes/image_cache/img_b1ba4521fb25.jpg'),
    Path('/home/ubuntu/.hermes/image_cache/img_9284dc69f032.jpg'),
]
for image in watermark_cases:
    flagged, marker = pmn.image_explicit_watermark(image)
    assert flagged, (image, marker)
    assert pmn.image_quality_tier(image)[0] == 'rejected'

# Edição miner: exact variant contradiction and unconfirmed risky family quarantine.
hawas = Path('/home/ubuntu/.hermes/image_cache/img_d0a4141b8c51.jpg')
rayhaan = Path('/home/ubuntu/.hermes/image_cache/img_06f4037b3ea7.jpg')
decision, reason, _ = miner.correction_identity_qc('Hawas Kobra Rasasi EDP 100ml', hawas)
assert decision == 'pending_identity_qc' and 'conflict' in reason, (decision, reason)
decision, reason, _ = miner.correction_identity_qc('Rayhaan Elixir 100ml', rayhaan)
assert decision == 'pending_identity_qc', (decision, reason)

print('perfume safety regression: OK')
