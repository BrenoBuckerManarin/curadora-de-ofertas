import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n

class VarietyTests(unittest.TestCase):
    def test_pool_maturity_and_disable(self):
        for size in [0,1,2,3,4]:self.assertEqual(n.repertoire_exploration_interval(size,.2),2)
        self.assertEqual(n.repertoire_exploration_interval(5,.2),5)
        self.assertEqual(n.repertoire_exploration_interval(3,0),0)

    def test_cosmetic_variants_do_not_fill_pool(self):
        self.assertEqual(len(n.distinct_repertoire(['ABACAXI TROPICAL ARABE 🍍','ÁRABE TROPICAL DE ABACAXI','FIEL AO AVENTUS'])),2)

    def test_least_recently_used_when_all_used(self):
        product='Test EDP 100ml'
        with patch.object(n,'eligible_repertoire_candidates',return_value=['*NOVO*','*ANTIGO*']),patch.object(n,'_load_headline_usage',return_value={n.product_key(product):[{'headline':'*ANTIGO*'},{'headline':'*NOVO*'}]}):
            self.assertEqual(n.approved_headline_repertoire(product),'*ANTIGO*')

    def test_exploration_cannot_fall_into_legacy_reuse(self):
        product='Nautica Voyage EDT 100ml'
        def cfg(key,default=''):
            return {'PERFUME_REUSE_APPROVED_HEADLINE':'1','PERFUME_HEADLINE_EXPLORATION_RATE':'.2'}.get(key,default)
        with tempfile.TemporaryDirectory() as td, patch('perfume_editorial_renewal.enqueue'):
            f=Path(td)/'cycles.jsonl';f.write_text(json.dumps({'product':product,'cycle_id':'valid'}))
            with patch.object(n,'EDITORIAL_CYCLES_FILE',f),patch.object(n,'config_value',side_effect=cfg),patch.object(n,'eligible_repertoire_candidates',return_value=[]),patch.object(n,'approved_headline_for_product',return_value='*OLD*') as old,patch.object(n,'context_approved_headline',return_value=''),patch.object(n,'llm_headline',return_value='*FRESCOR PARA O VERÃO*') as generate:
                self.assertEqual(n.make_headline(product,''),'*FRESCOR PARA O VERÃO*')
                old.assert_not_called();generate.assert_called_once()

if __name__=='__main__':unittest.main(verbosity=2)
