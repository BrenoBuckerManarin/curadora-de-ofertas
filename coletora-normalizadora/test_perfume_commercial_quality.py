"""Offline commercial/image regressions; temporary fixtures, no sends."""
import itertools
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
import perfume_deliver as d
import perfume_manual_normalizer as n


def offer(price=160, page=None, **kw):
    page = price if page is None else page
    row = dict(product='Lattafa Asad 100ml', body='Lattafa Asad 100ml\nhttps://meli.la/TestA', price=f'R${price}', validated_price=f'R${page}', validation_ok=True)
    row.update(kw)
    return d.validated_opportunity_score(row)


class CommercialTests(unittest.TestCase):
    def test_gap_is_not_rewarded_as_proven_discount(self):
        with patch.object(d, 'recent_product_prices', return_value=[]):
            a, b, verified = offer(99, 150), offer(99, 299), offer(99, 99)
        self.assertEqual(a['opportunity_score'], b['opportunity_score'])
        self.assertGreater(verified['opportunity_score'], b['opportunity_score'])
        self.assertEqual(b['price_confidence'], 'low_coupon_unconfirmed')

    def test_cheapest_comparable_wins_regardless_of_order(self):
        a, b = offer(160), offer(170)
        a['opportunity_score'], b['opportunity_score'] = 50, 100
        for rows in itertools.permutations([a, b]):
            result = d.collapse_same_product_offers(list(rows))
            self.assertEqual(result[0]['effective_price'], 160)
            self.assertEqual(result[0]['alternative_offer_count'], 1)

    def test_unknown_or_restricted_not_assumed_equivalent(self):
        a, b = offer(160), offer(170)
        a['body'] += '\nPrimeira compra no app'
        self.assertNotEqual(d.commercial_comparison_context(a), d.commercial_comparison_context(b))
        a['opportunity_score'], b['opportunity_score'] = 50, 100
        self.assertEqual(d.collapse_same_product_offers([a, b])[0]['effective_price'], 170)
        a['validation_ok'] = False
        self.assertEqual(d.commercial_comparison_context(a), '')

    def test_history_floor(self):
        with patch.object(d, 'recent_product_prices', return_value=[200]):
            small = offer()
        self.assertNotIn('menor histórico local', small['opportunity_reasons'])
        with patch.object(d, 'recent_product_prices', return_value=[200, 201, 202, 203, 204]):
            sufficient = offer()
        self.assertIn('menor histórico local', sufficient['opportunity_reasons'])

    def test_history_does_not_mix_legacy_or_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.jsonl'
            base = dict(product_key='test', ts=int(time.time()), link='a')
            rows = [dict(base, effective_price=100), dict(base, effective_price=110, comparison_context='other'), dict(base, effective_price=120, comparison_context='same')]
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            with patch.object(d, 'PRICE_HISTORY_FILE', path):
                self.assertEqual(d.recent_product_prices('test', comparison_context='same'), [120])

    def test_history_writer_preserves_context_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history.jsonl'
            row = offer()
            with patch.object(d, 'PRICE_HISTORY_FILE', path):
                d.append_price_observations([row, row])
                d.append_price_observations([row])
                saved = [json.loads(x) for x in path.read_text().splitlines()]
                self.assertEqual(len(saved), 1)
                context = d.commercial_comparison_context(row)
                self.assertEqual(saved[0]['comparison_context'], context)
                self.assertEqual(d.recent_product_prices(d.canonical_product_key(row['product']), comparison_context=context), [160])

    def test_same_photo_resolution_upgrade_preserves_other_compositions(self):
        with tempfile.TemporaryDirectory() as tmp:
            small, large = Path(tmp)/'breno_final_small.png', Path(tmp)/'source.png'
            Image.new('RGB', (500, 500), 'red').save(small)
            Image.new('RGB', (590, 590), 'red').save(large)
            with patch.object(n, 'looks_like_same_image_base', return_value=True):
                self.assertEqual(n.best_image_candidate([small, large]), large)
            with patch.object(n, 'looks_like_same_image_base', return_value=False):
                self.assertEqual(n.best_image_candidate([small, large]), small)

if __name__ == '__main__':
    unittest.main(verbosity=2)
