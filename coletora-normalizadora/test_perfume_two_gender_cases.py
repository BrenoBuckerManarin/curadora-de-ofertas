"""Offline regressions from Breno's two screenshots, 2026-09-22."""
import unittest
from pathlib import Path
import perfume_manual_normalizer as n
from perfume_identity_guard import headline_gender_conflict, gender_safe_headline, fakhar_variant_conflict


class TwoGenderCases(unittest.TestCase):
    SENTIR = 'Deo Parfum Natura Essencial Sentir Masculino 100ml'
    EXCLUSIVO = 'Natura Essencial Exclusivo Eau De Parfum Masculino 100ml'
    PURPLE = Path('/home/ubuntu/.hermes/perfume_image_bank/products/natura-essencial-exclusivo-100ml/approved_final/approved_vitrine_breno_20260815-164348_b1e3293f7825.jpg')
    PURPLE_2 = Path('/home/ubuntu/.hermes/perfume_image_bank/products/natura-essencial-exclusivo-100ml/approved_final/approved_vitrine_breno_20260720-170502_1309c65385c0.jpg')

    def test_sentir_gender_phrase_veto(self):
        self.assertTrue(headline_gender_conflict('ESSENCIAL PRA ELA SENTIR 🤌🏻', self.SENTIR))
        self.assertFalse(headline_gender_conflict('ESSENCIAL PRA ELA SENTIR', 'Natura Essencial Sentir Feminino 100ml'))
        self.assertFalse(headline_gender_conflict('ESSENCIAL PRA SENTIR', self.SENTIR))
        self.assertLess(n.headline_score('*ESSENCIAL PRA ELA SENTIR*', {'_product': self.SENTIR}), 0)
        self.assertNotIn('*ESSENCIAL PRA ELA SENTIR 🤌🏻*', n.eligible_repertoire_candidates(self.SENTIR))

    def test_final_publication_boundary(self):
        self.assertEqual(gender_safe_headline('*ESSENCIAL PRA ELA SENTIR 🤌🏻*', self.SENTIR), '*ESSENCIAL PRA SENTIR*')
        self.assertEqual(gender_safe_headline('*PARA ELE*', 'Natura Essencial Feminino 100ml'), '')
        self.assertEqual(gender_safe_headline('*ESSENCIAL PRA SENTIR*', self.SENTIR), '*ESSENCIAL PRA SENTIR*')

    def test_audience_matrix(self):
        for female in ('PRA ELA', 'PRA MULHER', 'DAS MENINAS', 'FOR HER', '♀ PERFUME'):
            self.assertTrue(headline_gender_conflict(female, 'Perfume Masculino 100ml'), female)
            self.assertFalse(headline_gender_conflict(female, 'Perfume Feminino 100ml'), female)
            self.assertFalse(headline_gender_conflict(female, 'Perfume Unissex 100ml'), female)
            self.assertFalse(headline_gender_conflict(female, 'Perfume 100ml'), female)
        for male in ('PRA ELE', 'PRA HOMEM', 'DOS MENINOS', 'FOR HIM', '♂ PERFUME'):
            self.assertTrue(headline_gender_conflict(male, 'Perfume Feminino 100ml'), male)
            self.assertFalse(headline_gender_conflict(male, 'Perfume Masculino 100ml'), male)
        self.assertFalse(headline_gender_conflict('AS MULHERES AMAM ESSE', 'Bad Boy Masculino 100ml'))
        self.assertFalse(headline_gender_conflict('👩 PERFUMADA', 'Perfume Masculino 100ml'))

    def test_exclusivo_photo_identity(self):
        for image in (self.PURPLE, self.PURPLE_2):
            self.assertTrue(image.is_file())
            self.assertFalse(n.bank_asset_identity_allowed(self.EXCLUSIVO, image))
            self.assertTrue(n.bank_asset_identity_allowed('Natura Essencial Exclusivo Feminino 100ml', image))

    def test_known_wrong_photos_blocked_without_quarantining_folder(self):
        folder = self.PURPLE.parent
        accepted = [p.name for p in folder.iterdir() if p.is_file()
                    and n.bank_asset_identity_allowed(self.EXCLUSIVO, p)]
        self.assertGreater(len(accepted), 2)
        self.assertNotIn(self.PURPLE.name, accepted)
        self.assertNotIn(self.PURPLE_2.name, accepted)

    def test_raw_media_needs_variant_evidence(self):
        self.assertFalse(n.source_image_identity_proved(self.EXCLUSIVO, self.PURPLE, 'Natura Essencial Exclusivo 100ml'))
        self.assertTrue(n.source_image_identity_proved('Natura Essencial Exclusivo Feminino 100ml', self.PURPLE, 'Natura Essencial Exclusivo 100ml'))
        other = self.PURPLE.parent / 'breno_final_20260718-114831_12bfe14a1d80.jpg'
        self.assertTrue(n.source_image_identity_proved(self.EXCLUSIVO, other, 'Natura Essencial Exclusivo 100ml'))
        self.assertTrue(n.source_image_identity_proved(self.EXCLUSIVO, other, self.EXCLUSIVO))

    def test_fakhar_rose_cannot_become_gold_extrait_headline(self):
        extrait = 'Lattafa Fakhar Gold Extrait EDP 100ml'
        raw = 'Lattafa Fakhar Gold EDP 100ml'
        rose = 'Lattafa Fakhar Rose Gold Feminino 100ml'
        self.assertTrue(fakhar_variant_conflict(extrait, raw))
        self.assertTrue(fakhar_variant_conflict(extrait, rose))
        self.assertTrue(fakhar_variant_conflict(raw, rose))
        self.assertFalse(fakhar_variant_conflict(extrait, 'Fakhar Extrait Gold Lattafa 100ml'))
        self.assertEqual(n.headline_identity_product(extrait, raw), extrait)
        self.assertNotIn(n.product_key(rose), n.editorial_identity_keys(extrait))
        self.assertNotIn('*FLORAL FRUTADA CREMOSA 🫦*', n.eligible_repertoire_candidates(extrait))
        self.assertNotIn('*FLORAL FRUTADA CREMOSA 🫦*', n.eligible_repertoire_candidates(raw))


if __name__ == '__main__':
    unittest.main()
