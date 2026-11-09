import unittest

import perfume_manual_normalizer as normalizer


class EgeoRecoveryTests(unittest.TestCase):
    def test_exact_sku_token_order_is_equivalent(self):
        received = "Combo Egeo Choc High Desodorante Colônia 90ml (2 itens)"
        approved = "Combo Egeo Choc High (2 Itens) Desodorante Colônia 90ml"
        self.assertTrue(normalizer.same_product_tokens_reordered(received, approved))
        candidates = normalizer.eligible_repertoire_candidates(
            received, {"current_price": 156, "source_price": 156}
        )
        self.assertIn("*OQ É MELHOR QUE UM EGEO?*", candidates)
        self.assertIn("*DOIS EGEOS CHOC HIGH 🍬*", candidates)

    def test_reordering_does_not_cross_quantity_or_volume(self):
        received = "Combo Egeo Choc High 90ml 2 itens"
        self.assertFalse(normalizer.same_product_tokens_reordered(
            received, "Combo Egeo Choc High 90ml 3 itens"
        ))
        self.assertFalse(normalizer.same_product_tokens_reordered(
            received, "Combo Egeo Choc High 100ml 2 itens"
        ))

    def test_cosmetic_descriptors_do_not_block_same_sku(self):
        self.assertTrue(normalizer.same_product_tokens_reordered(
            "Perfume Eudora Club 6 Exclusive Desodorante Colônia Masculino 95ml",
            "Eudora Club 6 Exclusive Masculino 95ml",
        ))
        self.assertFalse(normalizer.same_product_tokens_reordered(
            "Eudora Club 6 Exclusive Masculino 95ml",
            "Eudora Club 6 Fever Masculino 95ml",
        ))
        self.assertFalse(normalizer.same_product_tokens_reordered(
            "Eudora Club 6 Exclusive Masculino 95ml",
            "Eudora Club 6 Exclusive Feminino 95ml",
        ))

    def test_broken_singular_egeo_is_rejected(self):
        self.assertEqual(normalizer.clean_headline("DOIS EGEO CHOC HIGH 🍬", ""), "")
        self.assertEqual(
            normalizer.clean_headline("DOIS EGEOS CHOC HIGH 🍬", ""),
            "*DOIS EGEOS CHOC HIGH 🍬*",
        )

    def test_initial_visual_card_survives_later_account_verification(self):
        card = {
            "final_url": "https://www.mercadolivre.com.br/gz/account-verification?go=offer",
            "title": "Combo Egeo Choc High Desodorante Colônia 90ml (2 itens)",
            "current_price": 170,
            "screenshot": "/tmp/retained-card.png",
            "seller": "",
            "official_store": False,
        }
        self.assertTrue(normalizer.browser_card_commercially_authoritative(card))

    def test_redirect_without_rendered_card_is_not_authority(self):
        self.assertFalse(normalizer.browser_card_commercially_authoritative({
            "final_url": "https://www.mercadolivre.com.br/gz/account-verification",
            "current_price": 170,
        }))

    def test_normal_exact_card_remains_authoritative(self):
        card = {
            "final_url": "https://produto.mercadolivre.com.br/MLB-1",
            "title": "Combo Egeo Choc High Desodorante Colônia 90ml (2 itens)",
            "current_price": 170,
            "screenshot": "/tmp/retained-card.png",
            "seller": "O Boticário",
            "official_store": True,
        }
        self.assertTrue(normalizer.browser_card_commercially_authoritative(card))


if __name__ == "__main__":
    unittest.main()