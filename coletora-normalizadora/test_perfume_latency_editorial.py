"""Offline latency/editorial tests: no real sends or production-state writes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import json
import unittest
from unittest.mock import patch
import perfume_manual_normalizer as n

class LatencyTests(unittest.TestCase):
    def test_initial_reads_overlap_and_preserve_values(self):
        barrier = threading.Barrier(2)
        def browser(link):
            barrier.wait(timeout=2)
            return {'title':'Exact item', 'price':100}
        def jina(link):
            barrier.wait(timeout=2)
            return 'Public evidence'
        with patch.object(n,'browser_social_card_probe',side_effect=browser), patch.object(n,'jina_fetch',side_effect=jina):
            card,text,timing=n.fetch_initial_offer_evidence('https://example.invalid')
        self.assertEqual(card['price'],100)
        self.assertEqual(text,'Public evidence')
        self.assertIn('browser_seconds',timing)
        self.assertIn('jina_seconds',timing)

    def test_ready_promo_does_not_wait_for_first(self):
        release=threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow=pool.submit(release.wait,2)
            fast=pool.submit(lambda:'ready')
            order=n.completed_batch_indices({0:slow,1:fast})
            try: self.assertEqual(next(order),1)
            finally: release.set()
            self.assertEqual(next(order),0)

    def test_missing_directories_do_not_authorize_identity(self):
        with patch.object(n,'approved_final_candidates',return_value=[Path('/nonexistent/wrong-100ml/approved_final')]):
            self.assertEqual(n.editorial_identity_keys('Test 100ml'),{n.product_key('Test 100ml')})

    def test_exact_bank_identity_restores_context_and_repertoire(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); approved=root/'arabe-yara-tous-lattafa-100ml'/'approved_final'; approved.mkdir(parents=True)
            ctx=root/'context.json'; ctx.write_text(json.dumps({'arabe-yara-tous-lattafa-100ml':{'olfactive_terms':['coco']}}))
            cycles=root/'cycles.jsonl'; cycles.write_text('\n'.join(json.dumps({'product':'Yara Tous Lattafa 100ml','after':{'headline':h}}) for h in ['YARA FRUTADO E TROPICAL','COCO CREMOSO NO YARA']))
            with patch.object(n,'approved_final_candidates',return_value=[approved]), patch.object(n,'EDITORIAL_CONTEXT_FILE',ctx), patch.object(n,'EDITORIAL_CYCLES_FILE',cycles), patch.object(n,'_load_headline_usage',return_value={}):
                self.assertEqual(n.load_editorial_context('Perfume Árabe Yara Tous 100ml')['olfactive_terms'],['coco'])
                self.assertTrue(n.approved_headline_repertoire('Perfume Árabe Yara Tous 100ml'))

    def test_observed_filler_is_vetoed(self):
        for text in ['UM A MAIS NA COLEÇÃO 🤌🏻','UM ÁRABE PRA COLEÇÃO 🤌🏻','UM ÁRABE BEM CHEIROSO 🤌🏻','FOUGÈRE BEM CHEIROSO']:
            self.assertLess(n.headline_score(text,{}),60)

if __name__=='__main__': unittest.main(verbosity=2)
