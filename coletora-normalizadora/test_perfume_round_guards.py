"""Offline regressions for round 2026-09-08; no network, sends or bank writes."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_breno_correction_miner as miner
import perfume_editorial_pair_eval as comparator
import perfume_round_guards as g
from perfume_identity_guard import human_final_records, fingerprint


class KitTests(unittest.TestCase):
    def test_components_and_hydratant_cannot_disappear(self):
        p = 'Kit Body Splash Barbarius + Hidratante Desodorante Masculino'
        for candidate in ('kit-body-splash', 'Kit Body Splash Barbarius', 'Kit Body Splash Infalivel Enigma Banker Adventus'):
            self.assertFalse(g.kit_identity_matches(p, candidate))
        self.assertTrue(g.kit_identity_matches(p, 'kit-body-splash-barbarius-hidratante'))

    def test_reordered_exact_kit_and_house_fillers(self):
        self.assertTrue(g.kit_identity_matches('Kit Body Splash Barbarius Enigma Midtown Masculino 200ml', 'kit-body-splash-primacial-barbarius-midtown-enigma-cada-200ml'))

    def test_flanker_and_counts(self):
        self.assertFalse(g.kit_identity_matches('Egeo Dolce Kit Presente 2 Itens', 'O Boticario Egeo Original Kit Presente 2 Itens'))
        self.assertFalse(g.kit_identity_matches('Kit Egeo Dolce 2 itens', 'Kit Egeo Dolce 3 itens'))
        self.assertFalse(g.kit_identity_matches('Egeo Dolce 100ml', 'Kit Egeo Dolce 100ml'))

    def test_legacy_generic_folder_cannot_authorize_unrelated_asset(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)/'kit-body-splash'; folder.mkdir()
            media = folder/'photo.jpg'; media.write_bytes(b'fixture')
            (folder/'index.json').write_text(json.dumps({'product_guess':'Kit Body Splash Masculino','approved':[{'media_path':str(media),'product_guess':'Kit Body Splash Masculino'}]}))
            self.assertFalse(n.bank_asset_identity_allowed('Kit Body Splash Barbarius + Hidratante Masculino',media,folder))

    def test_hash_linked_specific_metadata_preserves_valid_asset(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'kit-body-splash'; folder.mkdir()
            media=folder/'photo.jpg'; media.write_bytes(b'fixture')
            other=folder/'other.jpg'; other.write_bytes(b'unrelated')
            p='Kit Body Splash Barbarius + Hidratante Masculino'
            (folder/'index.json').write_text(json.dumps({'product_guess':'Kit Body Splash Masculino','raw_items':[{'sha256':n.file_digest(media),'product_guess':p}]}))
            self.assertTrue(n.bank_asset_identity_allowed(p,media,folder))
            self.assertFalse(n.bank_asset_identity_allowed(p,other,folder))

    def test_alternative_title_cannot_drop_components(self):
        meta={'product':'Kit Body Splash Barbarius + Hidratante Masculino','image_products':['Kit Body Splash Masculino']}
        with patch.object(n,'find_image') as find, patch.object(n,'normalizer_report',return_value='review'):
            self.assertEqual(n.prepare_delivery_artifacts('copy',meta)[0],'')
            find.assert_not_called()


class FeedbackTests(unittest.TestCase):
    def test_kit_family_word_is_not_full_identity_confirmation(self):
        with patch.object(miner,'image_ocr_text',return_value='PRIMACIAL BODY SPLASH BARBARIUS'):
            self.assertEqual(miner.correction_identity_qc('Kit Body Splash Barbarius + Hidratante',Path('/nonexistent-fixture'))[0],'pending_identity_qc')

    def test_saz_mixed_box_conflict_and_clean_positive(self):
        with patch.object(miner,'image_ocr_text',return_value='MAWWAL SYMPHONY SAZ KAMAN'):
            self.assertEqual(miner.correction_identity_qc('Mawwal Symphony SAZ 100ml',Path('/nonexistent-fixture'))[0],'pending_identity_qc')
        with patch.object(miner,'image_ocr_text',return_value='MAWWAL SYMPHONY SAZ'):
            self.assertEqual(miner.correction_identity_qc('Mawwal Symphony SAZ 100ml',Path('/nonexistent-fixture'))[0],'identity_ok')

    def test_explicit_hold_cannot_become_gender_proof(self):
        with tempfile.TemporaryDirectory() as td:
            c=Path(td)/'cycles.jsonl';f=Path(td)/'feedback.jsonl'
            c.write_text(json.dumps({'normalizer_ts':1,'link':'exact','edition_messageId':'human','image_change':{'class':'changed','after_sha256':'fixture-held-hash'}})+'\n')
            f.write_text(json.dumps({'ts':1,'link':'exact','product':'Mawwal Symphony SAZ 100ml'})+'\n')
            with patch.dict(g.IMAGE_IDENTITY_HOLDS, {'fixture-held-hash':'fixture hold'}):
                self.assertEqual(human_final_records(fingerprint(c),fingerprint(f)),())

    def test_explicit_hold_not_reintroduced_by_incoming_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            incoming=Path(td)/'held.jpg';incoming.write_bytes(b'fixture')
            with patch.object(n,'find_image',return_value=''),patch.object(n,'image_identity_hold',return_value='hold'),patch.object(n,'normalizer_report',return_value='review'),patch.object(n,'prepare_image_for_send') as prepare:
                self.assertEqual(n.prepare_delivery_artifacts('copy',{'product':'Mawwal Symphony SAZ 100ml'},str(incoming))[0],'')
                prepare.assert_not_called()

    def test_near_visual_match_is_review_not_approval(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.png';b=Path(td)/'b.png'
            Image.new('RGB',(64,64),(100,110,120)).save(a)
            Image.new('RGB',(64,64),(104,113,121)).save(b)
            self.assertTrue(g.visually_similar_review(a,b,10))
            self.assertFalse(g.visually_similar_review(a,b,140))
            self.assertFalse(g.visually_similar_review(a,b,None))

    def test_comparator_integrates_soft_review_label(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);a=root/'a.png';b=root/'b.png';f=root/'f.jsonl';t=root/'tap.jsonl'
            Image.new('RGB',(64,64),'red').save(a);Image.new('RGB',(64,64),'blue').save(b)
            copy='*HEADLINE*\n\n*Perfume Fixture EDP 100ml*\n\nPor R$ 100\nhttps://meli.la/aEPW89A'
            f.write_text(json.dumps({'ts':1000,'link':'https://meli.la/aEPW89A','published_copy':copy,'content_delivered':True,'image_path':str(a)})+'\n')
            t.write_text(json.dumps({'timestamp':1010,'chatId':comparator.EDITION,'messageId':'human','body':copy,'mediaPath':str(b)})+'\n')
            # Flat colours collide in dHash; pixel distance must still veto a
            # false same-image claim. Real near-pair checked in the replay.
            with patch.object(comparator,'evidence',return_value={'normalizer_version':'fixture'}),patch.object(comparator,'classify_headline',return_value={}),patch.object(comparator,'classify_title',return_value={}),patch.object(comparator,'classify_price_angle',return_value={}):
                result=comparator.build(f,t,0)
            self.assertEqual(result['paired_cycles'],1)
            self.assertEqual(result['pairs'][0]['image_change']['class'],'changed')


class BudgetTests(unittest.TestCase):
    def test_nested_budget_never_extends_and_resets(self):
        with patch.object(g.time,'monotonic',return_value=100):
            with g.headline_budget(60):
                self.assertEqual(g.remaining_headline_seconds(90),60)
                with g.headline_budget(90):
                    self.assertEqual(g.remaining_headline_seconds(90),60)
            self.assertEqual(g.remaining_headline_seconds(90),90)

    def test_generator_judge_retry_share_remaining_budget(self):
        clock=[100.0];timeouts=[]
        def run(cmd,**kwargs):
            timeouts.append(kwargs['timeout']);clock[0]+=20
            return SimpleNamespace(stdout='{"headline":"fixture"}',stderr='',returncode=0)
        def cfg(k,default=''):
            return {'PERFUME_HEADLINE_DIRECT_HTTP':'0','PERFUME_HEADLINE_TIMEOUT':'90'}.get(k,default)
        with patch.object(g.time,'monotonic',side_effect=lambda:clock[0]),patch.object(n,'config_value',side_effect=cfg),patch.object(n,'_headline_llm_chain',return_value=[('fixture','fixture')]),patch.object(n.subprocess,'run',side_effect=run):
            with g.headline_budget(40):
                self.assertTrue(n.run_headline_llm('generation')[0])
                self.assertTrue(n.run_headline_llm('judge')[0])
                self.assertIn('budget exhausted',n.run_headline_llm('retry')[1])
        self.assertEqual(timeouts,[40,20])

    def test_make_headline_enters_shared_budget(self):
        observed = []
        def generate(*args):
            observed.append(g.remaining_headline_seconds(90))
            return '*O ESSENCIAL QUE ATRAI*'
        with patch.object(n,'config_value',side_effect=lambda k,d='':d), patch.object(n,'_make_headline_impl',side_effect=generate):
            result = n.make_headline('fixture','')
        self.assertEqual(result, '*O ESSENCIAL QUE ATRAI*')
        self.assertGreater(observed[0], 0)
        self.assertLess(observed[0], 60)  # remainder reserved for final-gate retry

    def test_real_subprocess_timeout_is_exercised_offline(self):
        # A real bounded child proves the timeout path, not fabricated API timing.
        real_run=subprocess.run
        def fixture_run(cmd,**kwargs):
            return real_run(['/usr/bin/python3','-c','import time; time.sleep(2)'],**kwargs)
        cfg=lambda k,d='':{'PERFUME_HEADLINE_DIRECT_HTTP':'0','PERFUME_HEADLINE_TIMEOUT':'90'}.get(k,d)
        with patch.object(n,'config_value',side_effect=cfg),patch.object(n,'_headline_llm_chain',return_value=[('fixture','fixture')]),patch.object(n.subprocess,'run',side_effect=fixture_run):
            with g.headline_budget(.05):
                out,error=n.run_headline_llm('offline bounded child')
        self.assertEqual(out,'');self.assertIn('timed out',error)


if __name__=='__main__':unittest.main(verbosity=2)
