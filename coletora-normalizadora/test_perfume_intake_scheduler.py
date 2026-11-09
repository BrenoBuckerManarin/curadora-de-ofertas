"""Offline scheduler and curation isolation regressions; no real messages sent."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import perfume_manual_normalizer as n
from perfume_intake_scheduler import admitted_results


class IntakeTests(unittest.TestCase):
    def test_new_arrival_runs_before_slow_offer_finishes_and_dedupes(self):
        release = threading.Event()
        slow_started = threading.Event()
        started = []
        def process(msg, queued):
            mid = msg['messageId']
            started.append(mid)
            if mid == 'slow':
                slow_started.set()
                release.wait(3)
            return mid
        def collect():
            return [{'messageId':'slow'}, {'messageId':'new'}] if slow_started.is_set() else []
        stream = admitted_results([{'messageId':'slow'}], collect, process, poll_seconds=.01)
        try:
            msg, result = next(stream)
            self.assertEqual(result, 'new')
            self.assertFalse(release.is_set())
        finally:
            release.set()
        self.assertEqual([result for msg,result in stream], ['slow'])
        self.assertEqual(sorted(started), ['new','slow'])

    def test_bounded_concurrency_and_total_leave_excess_unacknowledged(self):
        current = peak = 0
        lock = threading.Lock()
        rows = [{'messageId':str(i)} for i in range(12)]
        def process(msg, queued):
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(.02)
            with lock:
                current -= 1
            return msg['messageId']
        out = list(admitted_results(rows, lambda:rows, process, max_messages=7, poll_seconds=.005))
        self.assertEqual(len(out), 7)
        self.assertLessEqual(peak, 3)
        self.assertEqual({v for _,v in out}, {str(i) for i in range(7)})

    def test_admission_deadline_drains_without_starting_pending(self):
        def work(msg, queued):
            time.sleep(.03)
            return msg['messageId']
        out = list(admitted_results([{'messageId':str(i)} for i in range(5)], lambda:[], work,
                                    max_workers=1, admission_seconds=.01, poll_seconds=.005))
        self.assertEqual([r for _,r in out], ['0'])

    def test_persistent_pending_survives_admission_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tap = root/'tap.jsonl'
            rows = [{'messageId':str(i), 'chatId':'test@g.us', 'body':f'offer-{i}', 'timestamp':int(time.time())} for i in range(5)]
            tap.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with patch.object(n,'TAP_FILE',tap), patch.object(n,'STATE_FILE',root/'state.json'):
                collect = lambda:n.collect_new_messages('test@g.us')
                initial = collect()
                for msg, result in admitted_results(initial,collect,lambda msg,queued:msg['messageId'],max_messages=2):
                    n.mark_processed(msg['messageId'])
                self.assertEqual({r['messageId'] for r in collect()}, {'2','3','4'})
                self.assertEqual(n.load_state().get('last_position',0),0)

    def test_miner_child_owns_lock_timeout_and_no_parent_pipes(self):
        with tempfile.TemporaryDirectory() as td, patch.object(Path, 'home', return_value=Path(td)):
            miner = Path(td)/'.hermes/scripts/perfume_breno_correction_miner.py'
            miner.parent.mkdir(parents=True)
            miner.write_text('')
            with patch.object(n.subprocess, 'run') as run:
                n.refresh_breno_approved_images()
            args, kwargs = run.call_args
            self.assertEqual(args[0][:2], ['flock','-n'])
            self.assertEqual(args[0][3:5], ['timeout','45'])
            self.assertTrue(kwargs['start_new_session'])
            self.assertTrue(kwargs['close_fds'])
            self.assertNotIn('capture_output', kwargs)

    def test_main_exits_without_waiting_for_miner(self):
        # Actual interpreter exit, not just a mock asserting daemon=True.
        code = '''import time
from unittest.mock import patch
import perfume_manual_normalizer as n
with patch.object(n,'target_group',return_value='test'), patch.object(n,'collect_new_messages',return_value=[{'messageId':'x','body':''}]), patch.object(n,'refresh_breno_approved_images',side_effect=lambda:time.sleep(10)), patch.object(n,'send_text',return_value=True), patch.object(n,'mark_processed'), patch.object(n.time,'sleep',side_effect=None):
    # Restore a real sleeper for the miner only.
    import threading
    with patch.object(n,'refresh_breno_approved_images',side_effect=lambda:threading.Event().wait(10)):
        assert n.main()==0
print('exited_without_miner_wait')
'''
        result = subprocess.run([sys.executable,'-c',code],cwd=str(Path(n.__file__).parent), capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('exited_without_miner_wait',result.stdout)

    def test_image_comparison_once_per_candidate_and_stage_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a=root/'a.jpg'; b=root/'b.jpg'
            a.write_bytes(b'a'); b.write_bytes(b'b')
            timing={}
            with patch.object(n,'approved_final_candidates',return_value=[root]), patch.object(n,'image_quality_tier',return_value=('acceptable','test')), patch.object(n,'looks_like_same_image_base',side_effect=lambda p,other: p==str(a)) as compare, patch.object(n,'best_image_candidate',side_effect=lambda paths:paths[0]):
                self.assertEqual(n.find_image('test','incoming',timings=timing),str(b))
            self.assertEqual(compare.call_count,2)
            self.assertEqual(timing['image_candidates_checked'],2)
            for key in ('image_identity_scan_seconds','image_quality_seconds','image_comparison_seconds','image_ranking_seconds'):
                self.assertGreaterEqual(timing[key],0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
