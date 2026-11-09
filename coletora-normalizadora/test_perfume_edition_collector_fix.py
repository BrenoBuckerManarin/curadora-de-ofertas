import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_breno_correction_miner as m
import perfume_deliver as d

class FixTests(unittest.TestCase):
    def test_attracione_without_volume_or_word_perfume(self):
        for title in ('Attracione By Attracione Woman','Attracione By Attracione Woman Mulher Sensual'):
            body=f'*SENSUALIDADE NA PELE*\n_Loja Oficial *Attracione* no ML_\n\n*{title}*\n\nPor *R$ 44*\nhttps://meli.la/5riQj5H'
            self.assertEqual(m.extract_corrected_product(body),title)
        self.assertEqual(m.extract_corrected_product('*KIT COMPLETO*\n*Attracione Men 25ml*\nR$ 50'), 'Attracione Men 25ml')

    def test_newest_first_and_missing_media_does_not_resolve_bank(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            rows=[(1,{'messageId':'old','body':'*old*\n*Attracione Woman*','mediaPath':str(root/'missing1')}),(2,{'messageId':'new','body':'*new*\n*Attracione Men 25ml*','mediaPath':str(root/'missing2')})]
            with patch.object(m,'BANK',root/'bank'), patch.object(m,'REPORT',root/'report.json'), patch.object(m,'iter_rows',return_value=iter(rows)),patch.object(m,'_normalizer_output_by_link',return_value={}),patch.object(m,'resolve_write_key') as resolve:
                result=m.mine(['fixture'])
            self.assertEqual([r['messageId'] for r in result['items']],['new','old'])
            self.assertEqual(result['missing_media'],2)
            resolve.assert_not_called()

    def test_validated_duplicates_leave_shortlist_without_claiming_new_send(self):
        with tempfile.TemporaryDirectory() as td,patch.object(d,'STATE_FILE',str(Path(td)/'state.json')),patch.object(d,'service_log'),patch.object(d,'validated_delivery_key',side_effect=lambda msg:msg['key']):
            now=int(d.time.time())
            d.save_state({'seen_ids':[],'last_position':123,'delivered_offer_keys':{'already':now},'delivery_progress':{}})
            duplicate={'messageId':'dup','key':'already','validation_ok':True}
            fresh={'messageId':'fresh','key':'new','validation_ok':True}
            self.assertEqual(d.filter_delivered_validated([duplicate,fresh]),[fresh])
            state=d.load_state()
            self.assertEqual(state['seen_ids'],['dup'])
            self.assertEqual(state['delivered_offer_keys'],{'already':now})
            self.assertEqual(state['last_position'],123)
            # A fresh source message of the same SKU tomorrow remains eligible.
            yesterday=int((dt.datetime.now()-dt.timedelta(days=1)).timestamp())
            state['delivered_offer_keys']={'already':yesterday};d.save_state(state)
            tomorrow_repost={**duplicate,'messageId':'repost'}
            self.assertEqual(d.filter_delivered_validated([tomorrow_repost]),[tomorrow_repost])

if __name__=='__main__':unittest.main(verbosity=2)
