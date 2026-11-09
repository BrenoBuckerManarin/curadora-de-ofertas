"""Offline behavioral tests; no sends, no production-state writes."""
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from functools import lru_cache
from perfume_identity_memo import identity_memo_scope, scoped_identity_memo
import perfume_deliver as d
from perfume_round_runtime import run_exclusive

class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('STATE_FILE', str(self.root/'state.json')), ('TAP_FILE', str(self.root/'tap.jsonl')), ('COLLECTOR_FUNNEL_FILE', self.root/'funnel.json')]:
            p = patch.object(d, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(d, 'service_log'); p.start(); self.addCleanup(p.stop)
        self.status = self.root/'status.json'

    def test_empty_round_replaces_stale_success(self):
        self.status.write_text('{"status":"delivered"}')
        run_exclusive(d.main, self.status)
        state = json.loads(self.status.read_text())
        self.assertEqual(state['status'], 'no_candidates')
        self.assertEqual(json.loads(d.COLLECTOR_FUNNEL_FILE.read_text())['counts']['delivered'], 0)
        self.assertIn('collect', state['timings_seconds'])

    def test_window_controllers_resume_without_direct_delivery(self):
        # Execute the real shell controllers with only external dependencies
        # redirected to a temporary sandbox, never the production cron store.
        cron = self.root/'cron'; cron.mkdir()
        (cron/'__init__.py').write_text('')
        (cron/'jobs.py').write_text(
            "from pathlib import Path\n"
            "import os\n"
            "def record(s):\n    with open(os.environ['TEST_EVENTS'],'a') as f: f.write(s+'\\n')\n"
            "def pause_job(*a): return {'name':'duplicate'}\n"
            "def update_job(*a): record('update'); return {'id':a[0]}\n"
            "def resume_job(*a): record('resume'); return {'id':a[0]}\n")
        ctl = self.root/'normalizer.sh'
        ctl.write_text('#!/bin/bash\nprintf "normalizer\\n" >> "$TEST_EVENTS"\n')
        ctl.chmod(0o700)
        source = Path(d.__file__).parent
        for day in ('fri','tue','weekend'):
            events = self.root/f'{day}.events'
            script = (source/f'window_perfume_start_{day}.sh').read_text()
            script = script.replace('/home/ubuntu/.hermes/hermes-agent', str(self.root))
            script = script.replace('/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh', str(ctl))
            # A direct delivery attempt fails immediately in this sandbox.
            script = script.replace('/home/ubuntu/.hermes/scripts/perfume_deliver.py', str(self.root/'forbidden.py'))
            env = dict(os.environ, TEST_EVENTS=str(events), PYTHONPATH=str(self.root))
            result = subprocess.run(['bash','-c',script], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events.read_text().splitlines(), ['update','normalizer','resume'])

    def test_exception_is_observable_and_propagated(self):
        def fail(run):
            raise RuntimeError('test failure')
        with self.assertRaises(RuntimeError):
            run_exclusive(fail, self.status)
        self.assertEqual(json.loads(self.status.read_text())['status'], 'failed')

    def test_identity_memo_expires_after_ranking_scope(self):
        calls = []
        @scoped_identity_memo
        def identity(value):
            calls.append(value)
            return value.upper()
        with identity_memo_scope():
            self.assertEqual(identity('x'), identity('x'))
        self.assertEqual(len(calls), 1)
        with identity_memo_scope():
            identity('x')
        self.assertEqual(len(calls), 2)

    def test_delivery_reuses_validation_without_network_probe(self):
        msg = {'messageId':'test', 'body':'Lattafa Fakhar Black EDP 100ml\nR$ 150\nhttps://example.invalid/offer',
               'product':'Lattafa Fakhar Black EDP 100ml','groupName':'fixture',
               'validation_ok':True,'validated_jina':'validated page fixture', 'validated_price':'R$ 150',
               'has_context':False,'offer_keys':['test-key']}
        @lru_cache(maxsize=10)
        def bank(product): return None, 'no-approved'
        with ExitStack() as stack:
            for name in ('rank_candidates','validate_shortlist','apply_source_consensus','collapse_same_product_offers','filter_delivered_validated'):
                stack.enter_context(patch.object(d,name,side_effect=lambda rows: rows))
            for name in ('build_validation_shortlist','select_diverse_batch'):
                stack.enter_context(patch.object(d,name,side_effect=lambda rows, limit: rows))
            stack.enter_context(patch.object(d,'collect_new_messages',return_value=([msg],{})))
            stack.enter_context(patch.object(d,'validated_opportunity_score',side_effect=lambda m:m))
            stack.enter_context(patch.object(d,'append_price_observations'))
            stack.enter_context(patch.object(d,'resolve_approved_image',bank))
            stack.enter_context(patch('subprocess.run',side_effect=AssertionError('unexpected network/process')))
            sender=stack.enter_context(patch.object(d,'send_whatsapp_text',return_value=True))
            sleeper=stack.enter_context(patch.object(d.time,'sleep'))
            run_exclusive(d.main,self.status)
            self.assertEqual(sender.call_count,2)
            self.assertIn('Estoque',sender.call_args_list[1].args[1])
            self.assertEqual([call.args[0] for call in sleeper.call_args_list],[1.5,1.5])
            self.assertEqual(json.loads(self.status.read_text())['status'],'delivered')

    def test_overlap_does_not_replace_owner_evidence(self):
        self.status.write_text('{"status":"owner"}')
        with self.status.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_exclusive(lambda run: self.fail('overlap executed'), self.status)
        self.assertEqual(json.loads(self.status.read_text())['status'], 'owner')

    def test_validated_identity_survives_different_source_and_link(self):
        msg = {'messageId':'first', 'body':'Source slogan https://example.invalid/a', 'product':'Lattafa Fakhar Black EDP 100ml', 'validation_ok':True}
        d.mark_messages_seen([msg])
        repost = dict(msg, messageId='second', body='Another source https://example.invalid/b')
        self.assertEqual(d.filter_delivered_validated([repost]), [])
        self.assertIn(d.validated_delivery_key(msg), d.load_state()['delivered_offer_keys'])

    def test_dedupe_preserves_volume_flanker_concentration_gender(self):
        base = 'Lattafa Fakhar Black EDP 100ml'
        key = d.validated_delivery_key({'product':base})
        for other in [base.replace('100ml','50ml'), base.replace('Black','Rose'), base.replace('EDP','EDT'), base+' Woman']:
            self.assertNotEqual(key, d.validated_delivery_key({'product':other}), other)

    def test_yesterdays_validated_key_is_eligible(self):
        msg = {'product':'Lattafa Fakhar Black EDP 100ml'}
        yesterday = int((dt.datetime.now()-dt.timedelta(days=1)).timestamp())
        d.save_state({'delivered_offer_keys':{d.validated_delivery_key(msg):yesterday}})
        self.assertEqual(d.filter_delivered_validated([msg]), [msg])

    def test_image_lookup_reused_only_until_next_round(self):
        d.resolve_approved_image.cache_clear()
        with patch.object(d, '_load_normalizer', return_value=None), patch.object(d, 'canonical_product_key', return_value='test'), patch.object(d, '_approved_files_for_key', return_value=[self.root/'image.jpg']) as lookup:
            d.resolve_approved_image('test'); d.resolve_approved_image('test')
            self.assertEqual(lookup.call_count, 1)
            run_exclusive(d.main, self.status)
            d.resolve_approved_image('test')
            self.assertEqual(lookup.call_count, 2)

if __name__ == '__main__':
    unittest.main(verbosity=2)
