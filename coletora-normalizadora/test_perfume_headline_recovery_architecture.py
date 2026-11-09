import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n


class HeadlineRecoveryArchitectureTests(unittest.TestCase):
    def test_noisy_card_uses_clean_strict_source_identity_for_repertoire(self):
        card = 'Lattafa PRIDE OF LATTAFA Fakhar Black Masculino Eau de parfum Spray 100ml'
        source = 'Lattafa Fakhar Black EDP 100ml'
        selected = n.headline_identity_product(card, source)
        self.assertEqual(selected, source)
        self.assertGreaterEqual(len(n.eligible_repertoire_candidates(selected)), 2)
        with patch('perfume_editorial_renewal.enqueue'), patch.object(n, 'llm_headline') as llm:
            headline = n.make_headline(selected, '', '', {'final_price': 162})
        self.assertTrue(headline)
        self.assertEqual(n.headline_diagnostics().get('source'), 'approved_repertoire')
        llm.assert_not_called()

    def test_variant_conflict_never_uses_sender_identity(self):
        canonical = 'Natura Essencial Oud Masculino 100ml'
        received = 'Natura Essencial Tradicional Masculino 100ml'
        self.assertEqual(n.headline_identity_product(canonical, received), canonical)

    def test_message_ids_remain_independent_without_content_dedupe(self):
        body = '*Pulse Action*\nPor R$ 61\nhttps://meli.la/test'
        now = int(time.time())
        rows = [
            {'messageId':'a','chatId':'g','body':body,'timestamp':now},
            {'messageId':'b','chatId':'g','body':body,'timestamp':now},
        ]
        with tempfile.TemporaryDirectory() as td:
            tap = Path(td)/'tap.jsonl'
            tap.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
            state = Path(td)/'state.json'
            with patch.object(n,'TAP_FILE',tap), patch.object(n,'STATE_FILE',state):
                self.assertEqual([x['messageId'] for x in n.collect_new_messages('g')], ['a', 'b'])

    def test_empty_generation_uses_factual_publishing_fallback(self):
        body = '*FRESCURA PERFEITA PRA DIAS QUENTES*\nEudora Pulse Action 100ml'
        with patch.object(n, '_make_headline_impl', return_value=''), patch.object(n, 'llm_headline_retry', return_value=''):
            self.assertEqual(n.make_headline('Eudora Pulse Action Perfume Masculino 100ml', 'FRESCURA PERFEITA PRA DIAS QUENTES', body), '*FRESCOR PRA DIAS QUENTES*')
        self.assertEqual(n.headline_diagnostics().get('source'), 'fallback_rule')


if __name__ == '__main__':
    unittest.main()
