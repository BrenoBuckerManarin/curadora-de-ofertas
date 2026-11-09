"""Targeted offline quality regressions; no network, delivery or live bank writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import perfume_manual_normalizer as n
import perfume_deliver as d


class QualityTests(unittest.TestCase):
    def test_headline_does_not_invent_name_hooks(self):
        for product in ('Spicebomb EDP 100ml', 'Lattafa The Kingdom 100ml'):
            facts = n.ranked_editorial_facts(product, '', {})
            self.assertFalse(any('gancho semântico' in x for x in facts), facts)

    def test_headline_preserves_real_hooks(self):
        for product, hook in [('Kaiak Aero Ice 100ml', 'frio/gelo'),
                              ('Natura Homem Dom 100ml', 'dom/talento'),
                              ('Natura Homem Evolutio 100ml', 'evolução')]:
            self.assertTrue(any(hook in x for x in n.ranked_editorial_facts(product, '', {})))

    def test_volume_correction_cannot_cross_concentrations(self):
        self.assertFalse(n.same_product_except_volume('Dior Sauvage Eau de Parfum 60ml', 'Dior Sauvage Parfum 100ml'))
        self.assertFalse(n.same_product_except_volume('Dior Sauvage Extrait de Parfum 60ml', 'Dior Sauvage Parfum 100ml'))
        self.assertTrue(n.same_product_except_volume('Dior Sauvage Eau de Parfum 60ml', 'Dior Sauvage EDP 100ml'))

    def test_unrelated_editorial_price_is_not_pix_confirmation(self):
        result = n.lightweight_final_price_check('R$ 99', 'R$ 180', 'R$ 200', [], True)
        self.assertEqual(result['status'], 'unverified_light')
        self.assertEqual(n.lightweight_final_price_check('R$ 180', 'R$ 180', 'R$ 200', [], True)['status'], 'listing_match_light')
        self.assertEqual(n.lightweight_final_price_check('R$ 200', 'R$ 180', 'R$ 200', [], True)['status'], 'pix_listing_light')
        self.assertEqual(n.lightweight_final_price_check('R$ 160', 'R$ 180', 'R$ 200', ['TEST'], True)['status'], 'probable_final_light')

    def test_media_does_not_inflate_opportunity(self):
        base = dict(product='Lattafa Asad 100ml', body='Lattafa Asad 100ml', price='R$ 169', validated_price='R$ 169')
        with patch.object(d, 'recent_product_prices', return_value=[]):
            without = d.validated_opportunity_score(base)
            with_media = d.validated_opportunity_score(dict(base, bank_image='/bank.jpg', source_media='/source.jpg'))
        self.assertEqual(without['opportunity_score'], with_media['opportunity_score'])
        self.assertIn('foto banco', with_media['opportunity_reasons'])

    def test_legacy_images_filter_and_rank_all_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'test'
            folder.mkdir()
            bad, small, large, same = [folder / x for x in ('bad.jpg', 'small.jpg', 'large.jpg', 'same.jpg')]
            for p in (bad, small, large, same):
                p.touch()
            (folder / 'index.json').write_text(json.dumps({'approved': [str(bad), {'path': str(small)}, str(same), str(large)]}))
            with patch.object(n, 'IMAGE_BANK_PRODUCTS', root), patch.object(n, 'approved_final_candidates', return_value=[]), patch.object(n, 'image_lookup_keys', return_value=['test']), patch.object(n, 'image_quality_tier', side_effect=lambda p: ('rejected' if p == bad else 'good', 'test')), patch.object(n, 'looks_like_same_image_base', side_effect=lambda p, incoming: p == str(same)), patch.object(n, 'best_image_candidate', return_value=large) as rank:
                self.assertEqual(n.find_image('Test', '/incoming.jpg'), str(large))
                rank.assert_called_once_with([small, large])


if __name__ == '__main__':
    unittest.main(verbosity=2)
