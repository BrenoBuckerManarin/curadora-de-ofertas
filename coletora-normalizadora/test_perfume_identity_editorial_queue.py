"""2026-09-07: offline identity/editorial/queue regressions. No network or sends."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n
from perfume_intake_scheduler import admitted_results


class IdentityTests(unittest.TestCase):
    def test_display_preserves_both_genders(self):
        for word in ('Masculino', 'Feminino'):
            title = n.compact_product_name(f'Perfume Natura Essencial {word} EDP 100ml')
            self.assertIn(word, title)
            self.assertIn('100ml', title)
            self.assertIn('EDP', title)

    def test_unisex_not_female_first(self):
        self.assertEqual(n.product_gender_key('Perfume feminino e masculino 100ml'), 'unissex')
        self.assertEqual(n.product_gender_key('Perfume Sem Gênero 100ml'), 'unissex')
        self.assertEqual(n.product_gender_key('Perfume 100ml'), '')

    def test_conflicting_source_card_gender(self):
        self.assertTrue(n.product_variant_conflict('Natura Essencial Feminino 100ml', 'Natura Essencial Masculino 100ml'))
        self.assertTrue(n.product_variant_conflict('Natura Essencial for her 100ml', 'Natura Essencial for him 100ml'))

    def bank(self, product, folders, aliases=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder, evidence in folders.items():
                tier = root / folder / 'approved_final'
                tier.mkdir(parents=True)
                (tier/'test.jpg').write_bytes(b'test fixture; not a production asset')
                if evidence is not None:
                    (tier.parent/'index.json').write_text(json.dumps({'product_guess':evidence}))
            af = root/'aliases.json'; af.write_text(json.dumps(aliases or {}))
            with patch.object(n,'IMAGE_BANK_PRODUCTS',root), patch.object(n,'IMAGE_BANK_ALIASES',af), patch.object(n,'audit_image_bank_miss'):
                return {p.parent.name for p in n._approved_final_candidates_impl(product) if p.is_dir()}

    def test_alias_cannot_bypass_gender(self):
        product='Natura Essencial Feminino EDP 100ml'
        bad='natura-essencial-masculino-100ml'
        self.assertNotIn(bad, self.bank(product,{bad:None},{n.product_key(product):[bad]}))

    def test_unmarked_folder_allows_unknown_but_blocks_opposite_evidence(self):
        name='natura-essencial-100ml'
        self.assertIn(name,self.bank('Natura Essencial Feminino 100ml',{name:None}))
        self.assertNotIn(name,self.bank('Natura Essencial Feminino 100ml',{name:'Natura Essencial Masculino 100ml'}))
        self.assertIn(name,self.bank('Natura Essencial Feminino 100ml',{name:'Natura Essencial Feminino 100ml'}))

    def test_exact_gender_still_recoverable(self):
        name='natura-essencial-feminino-100ml'
        self.assertIn(name,self.bank('Natura Essencial Feminino 100ml',{name:None}))

    def test_powerfully_cannot_borrow_traditional(self):
        bad='armani-stronger-with-you-100ml'
        good='armani-stronger-with-you-powerfully-100ml'
        found=self.bank('Armani Stronger With You Powerfully EDP 100ml',{bad:None,good:None})
        self.assertNotIn(bad,found); self.assertIn(good,found)

    def test_cool_water_intense_alias_cannot_borrow_regular(self):
        p='Cool Water Intense Davidoff EDP 125ml';bad='cool-water-davidoff-125ml'
        self.assertNotIn(bad,self.bank(p,{bad:None},{n.product_key(p):[bad]}))

    def test_kaiak_perfume_cannot_borrow_body_refill(self):
        p='Kaiak Pulso 100ml';bad='refil-desodorante-corporal-natura-kaiak-pulso-100ml'
        self.assertNotIn(bad,self.bank(p,{bad:None},{n.product_key(p):[bad]}))

    def test_generic_alternative_cannot_drop_flanker(self):
        meta={'product':'Armani Stronger With You Powerfully EDP 100ml','image_products':['Armani Stronger With You EDT 100ml']}
        with patch.object(n,'find_image') as find, patch.object(n,'normalizer_report',return_value='not approved'):
            self.assertEqual(n.prepare_delivery_artifacts('copy',meta)[0],'')
            find.assert_not_called()

    def test_generic_alternative_gets_gender_before_lookup(self):
        meta={'product':'Natura Essencial Feminino EDP 100ml','image_products':['Natura Essencial EDP 100ml']}
        with patch.object(n,'find_image',return_value='') as find, patch.object(n,'normalizer_report',return_value='not approved'):
            n.prepare_delivery_artifacts('copy',meta)
            self.assertEqual(n.product_gender_key(find.call_args.args[0]),'feminino')

    def test_bank_keys_unchanged_no_migration(self):
        self.assertEqual(n.product_key('Natura Essencial Masculino 100ml'),'natura-essencial-100ml')

    def test_human_final_proof_is_per_image_not_whole_folder(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'natura-essencial-100ml'
            tier=folder/'approved_final';tier.mkdir(parents=True)
            good=tier/'good.jpg';good.write_bytes(b'approved final fixture')
            unknown=tier/'unknown.jpg';unknown.write_bytes(b'unknown fixture')
            with patch.object(n,'human_final_hashes_for_product',return_value={n.file_digest(good)}):
                self.assertTrue(n.bank_asset_identity_allowed('Natura Essencial Feminino 100ml',good))
                self.assertFalse(n.bank_asset_identity_allowed('Natura Essencial Feminino 100ml',unknown))

    def test_legacy_path_cannot_bypass_folder_gender(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'natura-essencial-masculino-100ml';folder.mkdir()
            media=folder/'legacy.jpg';media.write_bytes(b'fixture')
            with patch.object(n,'human_final_hashes_for_product',return_value={n.file_digest(media)}):
                self.assertFalse(n.bank_asset_identity_allowed('Natura Essencial Feminino 100ml',media,folder))

    def test_exact_cycle_join_uses_changed_final_not_replaced_image(self):
        from perfume_identity_guard import human_final_records,fingerprint
        with tempfile.TemporaryDirectory() as td:
            c=Path(td)/'cycles.jsonl';f=Path(td)/'feedback.jsonl'
            row={'normalizer_ts':1,'link':'exact','edition_messageId':'human',
                 'image_change':{'class':'changed','before_sha256':'old','after_sha256':'final'}}
            c.write_text(json.dumps(row)+'\n')
            f.write_text(json.dumps({'ts':1,'link':'exact','product':'Natura Essencial Feminino 100ml'})+'\n')
            records=human_final_records(fingerprint(c),fingerprint(f))
            self.assertEqual(records[0][2],frozenset({'final'}))
            row['link']='other';c.write_text(json.dumps(row)+'\n')
            self.assertEqual(human_final_records(fingerprint(c),fingerprint(f)),())


class EditorialTests(unittest.TestCase):
    def rows(self, product):
        return [{'cycle_id':'edited','product':product,'edition_final_ts':100,
                 'before':{'headline':'FIEL DEMAIS AO ULTRA MALE'},
                 'after':{'headline':'FIEL DEMAIS AO SOSPIRO'}}]

    def context(self, product, rows):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'cycles.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rows))
            with patch.object(n,'EDITORIAL_CYCLES_FILE',p), patch.object(n,'editorial_identity_keys',return_value={n.product_key(product)}), patch.object(n,'_load_editorial_context_base',return_value={'known_inspiration':'Ultra Male','avoid_terms':['Sospiro']}):
                return n.load_editorial_context(product)

    def test_human_correction_replaces_stale_inspiration_and_avoid_list(self):
        p='Al Wataniah Attar Al Wesal Gold EDP 100ml'
        ctx=self.context(p,self.rows(p))
        self.assertEqual(ctx['known_inspiration'],'SOSPIRO')
        self.assertEqual(ctx['human_fact_source'],'edited')
        self.assertNotIn('avoid_terms',ctx)

    def test_unchanged_output_is_not_promoted(self):
        p='Attar Al Wesal Gold 100ml'; rows=self.rows(p)
        rows[0]['before']=dict(rows[0]['after'])
        self.assertEqual(self.context(p,rows)['known_inspiration'],'Ultra Male')

    def test_cross_gender_human_fact_blocked(self):
        p='Natura Essencial Feminino 100ml'
        self.assertEqual(self.context(p,self.rows('Natura Essencial Masculino 100ml'))['known_inspiration'],'Ultra Male')

    def test_wrong_inspiration_is_vetoed(self):
        self.assertLess(n.headline_score('FIEL DEMAIS AO ULTRA MALE',{'known_inspiration':'Sospiro'}),0)
        self.assertFalse(n.headline_inspiration_conflict('FIEL AO SOSPIRO',{'known_inspiration':'Sospiro Vibrato'}))

    def test_exact_fact_fallback_precedes_generic_family(self):
        with patch.object(n,'config_value',return_value='0'), patch.object(n,'llm_headline',return_value=''), patch.object(n,'load_editorial_context',return_value={'known_inspiration':'Babycat'}):
            self.assertIn('BABYCAT',n.make_headline('Rifaaqat Paris Corner EDP','',''))


class QueueTests(unittest.TestCase):
    def test_refills_slot_while_consumer_is_sending(self):
        second_started=threading.Event()
        def work(msg,queued):
            if msg['messageId']=='2': second_started.set()
            return msg['messageId']
        stream=admitted_results([{'messageId':'1'},{'messageId':'2'}],lambda:[],work,max_workers=1,poll_seconds=.001)
        self.assertEqual(next(stream)[1],'1')
        try:
            self.assertTrue(second_started.wait(.3),'worker stayed idle while sender consumed result')
        finally:
            list(stream)


if __name__=='__main__': unittest.main(verbosity=2)
