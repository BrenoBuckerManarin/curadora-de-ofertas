import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_comparison_refresh_new as c


class QualityTests(unittest.TestCase):
    def test_incremental_idempotence_revision_and_settle(self):
        old={'cycle_id':'a','edition_final_ts':100,'edition_messageId':'1','after':{'headline':'old'}}
        revised={**old,'edition_final_ts':200,'after':{'headline':'new'}}
        self.assertEqual(c.select_changes([old],[old],400),([],0))
        self.assertEqual(c.select_changes([old],[revised],250),([],1))
        self.assertEqual(c.select_changes([old],[revised],400),([revised],0))
        self.assertEqual(c.select_changes([revised],[revised],400),([],0))

    def test_reviewed_exact_facts(self):
        # Human-edited cycle may preserve its own casing over base catalog data.
        self.assertEqual(n.load_editorial_context('Rayhaan Lion EDP 100ml')['known_inspiration'].casefold(),'ultra male')
        self.assertEqual(n.load_editorial_context('Maktub La Vie Bidaya Unisex EDP 100ml')['known_inspiration'],'God of Fire')
        self.assertEqual(n.load_editorial_context('Lab 8 Niche - Windstorm 100ml')['known_inspiration'],'Absolu Aventus')
        self.assertEqual(n.load_editorial_context('Khadlaj Shiyaaka Shadow EDP 100ml')['olfactive_terms'],['toranja','lavanda','pimentas'])
        self.assertNotEqual(n.load_editorial_context('Rayhaan Lioness EDP 100ml').get('known_inspiration'),'Ultra Male')

    def test_main_slow_image_does_not_block_fast_send(self):
        messages=[{'body':'slow','mediaPath':'slow-input','messageId':'1'}, {'body':'fast','mediaPath':'fast-input','messageId':'2'}]
        for message in messages:
            message['timestamp'] = str(int(time.time()) - 10)
        sent=[]
        observed_timings=[]
        def artifacts(copy,meta,media):
            observed_timings.append(dict(meta['stage_timings']))
            if media=='slow-input': time.sleep(.15)
            meta['_image_report_path']=media
            return media+'-prepared', media+'-report'
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            audit=Path(td)/'audit.jsonl'
            mocks={'target_group':lambda:'test', 'collect_new_messages':lambda group:messages,
                   'refresh_breno_approved_images':lambda:None,
                   'normalize_copy':lambda b,m:(b,{'product':b}),
                   'prepare_delivery_artifacts':artifacts,
                   'send_media':lambda group,image,copy,**kw: sent.append((copy,image)) or True,
                   'send_text':lambda *a,**kw:True,
                   'append_normalizer_feedback':lambda *a,**kw:None,
                   'mark_processed':lambda *a:None,
                   'Path':lambda *a: audit}
            for k,v in mocks.items(): stack.enter_context(patch.object(n,k,v))
            stack.enter_context(patch.object(n,'RULES_FILE',Path(td)))
            self.assertEqual(n.main(),0)
        self.assertEqual(sent,[('fast','fast-input-prepared'),('slow','slow-input-prepared')])
        self.assertEqual(len(observed_timings), 2)
        for timing in observed_timings:
            self.assertGreaterEqual(timing['worker_queue_seconds'], 0)
            self.assertGreaterEqual(timing['receipt_to_worker_seconds'], 10)

    def test_artifact_guard_rejects_conflicting_identity(self):
        meta={'product':'Rayhaan Lion EDP 100ml','image_products':['Yara Tous EDP 100ml']}
        with patch.object(n,'find_image') as lookup, patch.object(n,'normalizer_report',return_value='report'):
            image,report=n.prepare_delivery_artifacts('copy',meta)
            self.assertEqual(image,'')
            lookup.assert_not_called()
        self.assertTrue({'image_lookup_seconds','image_prepare_seconds','image_report_seconds','image_seconds'} <= meta['stage_timings'].keys())

if __name__=='__main__':unittest.main(verbosity=2)
