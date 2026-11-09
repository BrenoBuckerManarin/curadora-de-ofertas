import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_editorial_pair_eval as e

e.pmn = n


class RoundReportRegression(unittest.TestCase):
    def test_duplicate_slogan_only(self):
        self.assertEqual(n.compact_product_name('Lattafa PRIDE OF LATTAFA Fakhar Black EDP Masculino 100ml'), 'Lattafa Fakhar Black EDP Masculino 100ml')
        self.assertIn('Pride', n.sanitize_product_text('Lattafa Pride Pisa 100ml'))

    def test_separator_and_unknown_brand(self):
        self.assertEqual(n.compact_product_name('Shagaf Al Ward / EDP Feminino 100ml'), 'Shagaf Al Ward EDP Feminino 100ml')
        self.assertEqual(n.compact_product_name('Vision Imagination Fluy Lab 8'), 'Vision Imagination Fluy Lab 8')
        self.assertNotIn('Árebe', n.sanitize_product_text('Árebe Opulent Dubai Lattafa EDP 100ml'))

    def test_store_names_are_not_product_brands(self):
        for name in ('OUD', 'Mercado Livre'):
            self.assertTrue(n.rendered_card_store_trust({'seller': name, 'official_store': True}))
            self.assertIsNone(n.rendered_card_store_trust({'seller': name, 'official_store': False}))

    def test_html_fallback_must_match_expected_seller(self):
        self.assertEqual(n.extract_ml_official_store_from_html('Loja oficial Errada', expected_seller='Natura'), '')
        self.assertEqual(n.extract_ml_official_store_from_html('Loja oficial Natura', expected_seller='Natura'), 'Natura')

    def test_comparator_missing_headline_does_not_eat_store(self):
        text = '_Loja Oficial *Lipx* no ML_\n\n*Khamrah Qahwa 100ml*\nR$ 100'
        self.assertEqual(e.headline(text), '')
        self.assertEqual(e.title(text), 'Khamrah Qahwa 100ml')
        self.assertEqual(e.headline_level('', 'Khamrah Qahwa 100ml'), 'missing')
        self.assertEqual(e.headline('*DE R$369 POR R$184*\n\n*Azure 100ml*'), 'DE R$369 POR R$184')

    def test_unchanged_does_not_mean_quality(self):
        for h in ('PERFUME PRA APROVEITAR', 'IMPORTADO DE PRESENÇA', 'AQUÁTICO', 'ORIENTAL'):
            out = e.classify_headline(h, h, 'Ferrari Black 125ml', {})
            self.assertEqual(out['class'], 'unchanged')
            self.assertEqual(out['before_level'], 'generic', h)

    def test_fallback_does_not_copy_source_claim(self):
        with patch.object(n, 'load_editorial_context', return_value={}):
            h = n.publishing_fallback_headline('Body Splash 4un Árabe Feminino 200ml', 'ÁRABE DE FIXAÇÃO BRUTA')
        self.assertEqual(h, '*PRA PERFUMAR APÓS O BANHO*')

    def test_all_sources_reject_catalog_or_one_word_headline(self):
        for h in ('PERFUME ÁRABE 100ML 🧴', 'IMPORTADO DE PRESENÇA', 'AQUÁTICO', 'ORIENTAL'):
            self.assertEqual(n.enforce_headline_output(h), '', h)

    def test_factual_fallback_combines_notes(self):
        with patch.object(n, 'load_editorial_context', return_value={'olfactive_terms': ['aquático', 'fresco']}):
            h = n.publishing_fallback_headline('Natura Kaiak Ultra Masculino 100ml', 'ABSURDO DA NATURA')
        self.assertEqual(h, '*AQUÁTICO E FRESCO*')
        self.assertLessEqual(n.headline_visual_len(h.strip('*')), 28)

    def test_legacy_fallback_claim_is_guarded(self):
        def bad(*args, **kwargs):
            n.LAST_HEADLINE_SOURCE = 'fallback_rule'
            return '*ÁRABE DE FIXAÇÃO BRUTA*'
        with patch.object(n, '_make_headline_impl', side_effect=bad), patch.object(n, 'load_editorial_context', return_value={}):
            h = n.make_headline('Body Splash 4un Árabe 200ml', '')
        self.assertEqual(h, '*PRA PERFUMAR APÓS O BANHO*')

    def test_feedback_preserves_actual_decision(self):
        meta = {'product': 'Test 100ml', 'headline': '', 'headline_source': 'editorial_pending', 'price_checked': True,
                'lightweight_price': {'status': 'conflict_light'}, 'browser_card': {'seller': 'OUD', 'official_store': True, 'item_id': 'MLB3958162017'}}
        with contextlib.redirect_stderr(io.StringIO()):
            report = n.normalizer_report(meta)
        with tempfile.TemporaryDirectory() as td, patch.object(Path, 'home', return_value=Path(td)):
            n.append_normalizer_feedback('', '_Loja Oficial *Oud* no ML_\nTest 100ml', meta, '', report, True, False)
            row = json.loads((Path(td)/'.hermes/perfume_image_bank/logs/normalizer_feedback.jsonl').read_text())
        self.assertEqual(row['headline'], '')
        self.assertEqual(set(row['approval_failed_checks']), {'headline', 'preço final', 'imagem'})
        self.assertTrue(row['approval_checks']['preço'])
        self.assertEqual(row['report'], report)
        self.assertEqual(row['delivery_failed_checks'], ['report'])
        self.assertFalse(row['approved'])
        self.assertEqual(row['store_evidence']['seller'], 'OUD')

    def test_pulse_historical_assets_only(self):
        folder = Path('/home/ubuntu/.hermes/perfume_image_bank/products/eudora-pulse-action-100ml/approved_final')
        candidates = n.approved_final_candidates('Eudora Pulse Action Masculino 100ml')
        self.assertIn(folder, candidates)
        historical = [folder/'breno_final_20260802-202325_db3127bb9cbf.jpg', folder/'breno_final_20260719-202237_f5f5bbb8da9d.jpg']
        self.assertTrue(all(p.is_file() for p in historical))
        original_iterdir = Path.iterdir
        def only_historical(path):
            return iter(historical) if path == folder else original_iterdir(path)
        with patch.object(n, 'approved_final_candidates', return_value=[folder]), patch.object(Path, 'iterdir', only_historical):
            image = n.find_image('Eudora Pulse Action Masculino 100ml')
        self.assertTrue(image)
        self.assertIn('20260', Path(image).name)
        self.assertTrue(Path(image).is_file())
        self.assertNotIn('20260912', Path(image).name)
        self.assertEqual(Path(image), historical[1])
        self.assertFalse(n.bank_asset_identity_allowed('Eudora Pulse Action Masculino 100ml', historical[0]))
        for wrong in ('Eudora Pulse Action Feminino 100ml', 'Eudora Pulse Action Masculino 50ml', 'Eudora Pulse Masculino 100ml'):
            self.assertFalse(n.bank_asset_identity_allowed(wrong, historical[1]))


if __name__ == '__main__':
    unittest.main()
