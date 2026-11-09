import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_renewal_review as r


class RenewalReviewTests(unittest.TestCase):
    def test_review_is_required_and_invalidated_by_changed_facts_or_candidate(self):
        product='Test Vanilla 100ml'
        ctx={'olfactive_terms':['baunilha','amêndoa']}
        candidate={'product':product,'headline':'BAUNILHA COM AMÊNDOA','status':'candidate_needs_review'}
        with tempfile.TemporaryDirectory() as td, patch.object(r,'ROOT',Path(td)), patch.object(n,'load_editorial_context',return_value=ctx), patch.object(n,'eligible_repertoire_candidates',return_value=[]):
            source=Path(td)/'case.result.json';source.write_text(json.dumps(candidate))
            keys={n.product_key(product)}
            self.assertEqual(r.reviewed_candidates(product,keys,ctx),[])
            verdict={'approved':True,'fact_ids':['note_0','note_1'],'reason':'both notes supported'}
            with patch.object(n,'run_headline_llm',return_value=(json.dumps(verdict),'')):
                result=r.review_one()
            self.assertEqual(result['status'],'machine_reviewed')
            saved=json.loads((Path(td)/'case.review.json').read_text())
            self.assertFalse(saved['human_approved']);self.assertFalse(saved['sent'])
            self.assertEqual(r.reviewed_candidates(product,keys,ctx),['*BAUNILHA COM AMÊNDOA*'])
            self.assertEqual(r.reviewed_candidates(product,keys,{}),[])
            candidate['headline']='OTHER';source.write_text(json.dumps(candidate))
            self.assertEqual(r.reviewed_candidates(product,keys,ctx),[])

    def test_unsupported_claims_duplicate_and_malformed_review_fail_closed(self):
        product='Test 100ml';facts={'identity':product}
        for text in ['ACHADO PRA SAIR DO ÓBVIO','FIXAÇÃO BRUTA','POR MENOS DE R$ 100','FIEL AO INVICTUS']:
            self.assertFalse(r.hard_gate(product,text,facts,[])[0])
        self.assertFalse(r.hard_gate(product,'BAUNILHA COM AMÊNDOA',facts,['*BAUNILHA COM AMÊNDOA*'])[0])
        with tempfile.TemporaryDirectory() as td, patch.object(r,'ROOT',Path(td)), patch.object(n,'load_editorial_context',return_value={}), patch.object(n,'eligible_repertoire_candidates',return_value=[]):
            source=Path(td)/'case.result.json'
            source.write_text(json.dumps({'product':product,'headline':'UM ANJO EM DOSE DUPLA','status':'candidate_needs_review'}))
            with patch.object(n,'run_headline_llm',return_value=('[]','')):
                self.assertEqual(r.review_one()['status'],'review_error')
            self.assertEqual(r.reviewed_candidates(product,{n.product_key(product)},{}),[])
            self.assertIsNone(next(r.pending(),None))


if __name__=='__main__':unittest.main()
