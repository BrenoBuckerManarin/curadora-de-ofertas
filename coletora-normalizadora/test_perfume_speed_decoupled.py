import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_editorial_renewal as q

class SpeedTests(unittest.TestCase):
    def test_cache_hits_and_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'image.jpg';p.write_bytes(b'first')
            n._cached_image_quality_tier.cache_clear()
            with patch.object(n,'_uncached_image_quality_tier',return_value=('acceptable','test')) as quality:
                n.image_quality_tier(p);n.image_quality_tier(p)
                self.assertEqual(quality.call_count,1)
                p.write_bytes(b'second changed')
                n.image_quality_tier(p)
                self.assertEqual(quality.call_count,2)
                moved=p.with_name('rejected.jpg');p.rename(moved)
                n.image_quality_tier(moved)
                self.assertEqual(quality.call_count,3)

    def test_queue_dedup_and_unapproved_result(self):
        with tempfile.TemporaryDirectory() as td,patch.object(q,'ROOT',Path(td)):
            key=q.enqueue('Test','body',{},['*OLD*'])
            self.assertEqual(key,q.enqueue('Test','body',{},['*OLD*']))
            with patch.object(n,'llm_headline',return_value='*NEW FACTUAL ANGLE*') as llm:
                row=q.run_one();self.assertFalse(row['approved']);self.assertFalse(row['sent'])
                self.assertEqual(row['status'],'candidate_needs_review')
                self.assertEqual(q.run_one()['status'],'idle');llm.assert_called_once()
            self.assertEqual(len(list(Path(td).glob('*.request.json'))),1)

    def test_fast_reuse_queues_without_llm(self):
        product='Nautica Voyage EDT 100ml'
        def cfg(key,default=''):
            return {'PERFUME_REUSE_APPROVED_HEADLINE':'1','PERFUME_HEADLINE_EXPLORATION_RATE':'.2'}.get(key,default)
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'cycles';f.write_text(json.dumps({'product':product,'cycle_id':'valid'}))
            with patch.object(n,'EDITORIAL_CYCLES_FILE',f),patch.object(n,'config_value',side_effect=cfg),patch.object(n,'eligible_repertoire_candidates',return_value=['*VOYAGE AQUÁTICO*']),patch.object(n,'_record_repertoire_usage'),patch.object(n,'reserve_headline'),patch.object(n,'llm_headline') as llm,patch.object(q,'enqueue') as enqueue:
                self.assertEqual(n.make_headline(product,''),'*VOYAGE AQUÁTICO*')
                llm.assert_not_called();enqueue.assert_called_once()

class CompletionTests(unittest.TestCase):
    def test_metadata_cache_tracks_new_changed_and_removed_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            idx = root/'index.json'
            self.assertEqual(n.image_bank_metadata_keys(root), [])
            idx.write_text(json.dumps({'product_key':'First SKU'}))
            self.assertEqual(n.image_bank_metadata_keys(root), ['first-sku'])
            idx.write_text(json.dumps({'product_key':'Other exact SKU'}))
            self.assertEqual(n.image_bank_metadata_keys(root), ['other-exact-sku'])
            idx.rename(root/'archived.json')
            self.assertEqual(n.image_bank_metadata_keys(root), [])

    def test_only_maintenance_tick_can_start_renewal(self):
        import contextlib
        import subprocess
        import perfume_comparison_refresh_new as c
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            root = Path(td)
            (root/'pending.request.json').write_text('{}')
            stack.enter_context(patch.object(q, 'ROOT', root))
            stack.enter_context(patch.object(c, 'REPORT', root/'report.json'))
            stack.enter_context(patch.object(Path, 'home', return_value=root))
            for attr in ('DEFAULT_CYCLES','DEFAULT_SUMMARY','DEFAULT_OUT'):
                stack.enter_context(patch.object(c.evaluator, attr, root/attr))
            stack.enter_context(patch.object(c.evaluator, 'load_jsonl', return_value=[]))
            stack.enter_context(patch.object(c.evaluator, 'build', return_value={'pairs':[]}))
            stack.enter_context(patch.object(c.evaluator, 'summarize', return_value={}))
            run = stack.enter_context(patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'{}','')))
            c.refresh()
            run.assert_not_called()
            c.refresh(allow_renewal=True)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['timeout'],75)
            pause = root/'.hermes/run/perfume_pause_day'
            pause.parent.mkdir(parents=True)
            pause.write_text(c.time.strftime('%Y-%m-%d'))
            c.refresh(allow_renewal=True)
            run.assert_called_once()

if __name__=='__main__':unittest.main(verbosity=2)
