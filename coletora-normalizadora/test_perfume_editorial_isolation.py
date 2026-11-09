import unittest
from unittest.mock import patch
import perfume_manual_normalizer as n

class Isolation(unittest.TestCase):
 def setUp(self):
  n._HEADLINE_DIAGNOSTICS.approved_final_cache = {}
  n._HEADLINE_DIAGNOSTICS.approved_final_cache_active = False

 def tearDown(self):
  n._HEADLINE_DIAGNOSTICS.approved_final_cache = {}
  n._HEADLINE_DIAGNOSTICS.approved_final_cache_active = False

 def test_approved_bank_resolution_is_cached_within_request_window(self):
  folders = [n.Path('/tmp/approved-final')]
  n._HEADLINE_DIAGNOSTICS.approved_final_cache_active = True
  with patch.object(n, '_approved_final_candidates_impl', return_value=folders) as resolve:
   first = n.approved_final_candidates('Arabian Oasis EDP 75ml')
   first.append(n.Path('/tmp/caller-mutation'))
   second = n.approved_final_candidates('Arabian Oasis EDP 75ml')
  self.assertEqual(resolve.call_count, 1)
  self.assertEqual(second, folders)

 def test_approved_bank_resolution_is_not_cached_outside_headline_request(self):
  with patch.object(n, '_approved_final_candidates_impl', return_value=[]) as resolve:
   n.approved_final_candidates('Arabian Oasis EDP 75ml')
   n.approved_final_candidates('Arabian Oasis EDP 75ml')
  self.assertEqual(resolve.call_count, 2)

 def test_provider_error_survives_final_reason(self):
  n._HEADLINE_DIAGNOSTICS.attempts=[]
  with patch.object(n,'_run_headline_llm_impl',return_value=('', 'timeout primary')):
   self.assertEqual(n.run_headline_llm('private test')[0],'')
  n.LAST_HEADLINE_REASON='generic fallback'
  n._capture_headline_diagnostics()
  self.assertEqual(n.headline_diagnostics()['attempts'][0]['error'],'timeout primary')
 def test_editorial_exception_does_not_propagate(self):
  with patch.object(n,'_make_headline_bounded',side_effect=ValueError('bad response')):
   self.assertEqual(n.make_headline('Product','',''), '')
  self.assertEqual(n.headline_diagnostics()['source'],'editorial_pending')
 def test_attempts_do_not_leak_between_requests(self):
  n._HEADLINE_DIAGNOSTICS.attempts=[{'error':'old request'}]
  with patch.object(n,'_make_headline_bounded',side_effect=ValueError()):
   n.make_headline('Product','')
  self.assertEqual(n.headline_diagnostics()['attempts'],[])
 def test_all_diagnostics_are_cleared_before_request(self):
  n._HEADLINE_DIAGNOSTICS.value={'options':[{'headline':'OLD SKU'}], 'judge_reason':'old'}
  with patch.object(n,'_make_headline_bounded',side_effect=ValueError()):
   n.make_headline('New Product','')
  diag=n.headline_diagnostics()
  self.assertEqual(diag['options'],[])
  self.assertEqual(diag['judge_reason'],'')
if __name__=='__main__': unittest.main()
