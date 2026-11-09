import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_editorial_pair_eval as e


class FocusedEditorialTests(unittest.TestCase):
    def test_identity_change_excluded_not_all_waha(self):
        self.assertFalse(n.editorial_cycle_eligible({'cycle_id':'0748bb169f7088edfb94'}))
        self.assertTrue(n.editorial_cycle_eligible({'cycle_id':'new-cycle','product':'Khamrah Waha'}))

    def test_summary_excludes_but_audit_is_preserved(self):
        rows=e.load_jsonl(e.DEFAULT_CYCLES)
        excluded=[r for r in rows if not n.editorial_cycle_eligible(r)]
        self.assertEqual(len(excluded),1)
        result=e.summarize(rows)
        self.assertEqual(result['cycles'],len(rows)-1)
        self.assertIn(excluded[0],rows)

    def test_identity_change_cannot_promote_human_facts(self):
        row={'cycle_id':'0748bb169f7088edfb94','product':'Lattafa Khamrah Waha 100ml',
             'product_key':'lattafa-khamrah-waha-100ml','after':{'headline':'FRESCO GELADO AQUATICO'}}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'facts.json'
            e.persist_human_editorial_facts([row],p)
            self.assertEqual(json.loads(p.read_text()),{})

    def test_excluded_cycle_cannot_enter_repertoire(self):
        p='Lattafa Khamrah Waha 100ml'
        rows=[{'cycle_id':'0748bb169f7088edfb94','product':p,'after':{'headline':'FRESCO GELADO AQUATICO'}},
              {'cycle_id':'valid','product':p,'after':{'headline':'FRESCOR AQUATICO'}}]
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'cycles.jsonl';f.write_text('\n'.join(json.dumps(r) for r in rows))
            with patch.object(n,'EDITORIAL_CYCLES_FILE',f):
                # One valid human final is deliberately reusable; the excluded
                # identity-changed cycle must never become the selected angle.
                self.assertEqual(n.approved_headline_repertoire(p),'*FRESCOR AQUATICO*')
                self.assertNotIn('*FRESCO GELADO AQUATICO*',n.eligible_repertoire_candidates(p))

if __name__=='__main__':unittest.main(verbosity=2)
