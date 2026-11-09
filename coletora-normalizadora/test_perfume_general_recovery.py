import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import perfume_manual_normalizer as n


class GeneralRecoveryTests(unittest.TestCase):
    def test_source_flanker_cannot_disappear_from_listing(self):
        self.assertTrue(n.source_variant_lost(
            "Mawwal Energy Zuhoor EDP 100ml",
            "Mawwal Arabia Zuhoor 100ml Chipre Amadeirado",
        ))
        self.assertFalse(n.source_variant_lost(
            "Lattafa Fakhar 100ml",
            "Lattafa Fakhar Gold EDP 100ml",
        ))

    def test_explicit_source_house_survives_marketplace_omission(self):
        self.assertEqual(
            n.preserve_source_house("Perfume Thahaani EDP 100ml", "Al Wataniah Thahaani EDP 100ml"),
            "Perfume Al Wataniah Thahaani EDP 100ml",
        )

    def test_short_budget_skips_provider_call(self):
        n._HEADLINE_DIAGNOSTICS.attempts = []
        with patch.object(n, "remaining_headline_seconds", return_value=9.5), \
             patch.object(n, "_run_headline_llm_impl") as provider:
            out, error = n.run_headline_llm("prompt")
        self.assertEqual(out, "")
        self.assertIn("insufficient useful budget", error)
        provider.assert_not_called()
        self.assertEqual(n._HEADLINE_DIAGNOSTICS.attempts[-1]["status"], "skipped_budget")

    def test_legacy_gender_comes_only_from_prior_approved_final_use(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "products"
            folder = root / "legacy-product-100ml"
            image = folder / "approved_final" / "human.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            logs = root.parent / "logs"
            logs.mkdir()
            feedback = logs / "normalizer_feedback.jsonl"
            feedback.write_text(json.dumps({
                "approved": True,
                "display_product": "Legacy Product Masculino 100ml",
                "image_path": str(image),
            }) + "\n", encoding="utf-8")
            n._historical_approved_folder_genders.cache_clear()
            with patch.object(n, "IMAGE_BANK_PRODUCTS", root):
                self.assertEqual(n.historical_approved_folder_genders(folder), {"masculino"})
            feedback.write_text(json.dumps({
                "approved": False,
                "display_product": "Legacy Product Feminino 100ml",
                "image_path": str(image),
            }) + "\n", encoding="utf-8")
            n._historical_approved_folder_genders.cache_clear()
            with patch.object(n, "IMAGE_BANK_PRODUCTS", root):
                self.assertEqual(n.historical_approved_folder_genders(folder), set())

    def test_recent_source_fallback_keeps_exact_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            black = root / "black.jpg"
            rayhaan = root / "rayhaan.jpg"
            black.write_bytes(b"black")
            rayhaan.write_bytes(b"rayhaan")
            tap = root / "messages.jsonl"
            rows = [
                {"body": "*BLACK OBSIDIAN EDP 100ml*\nhttps://meli.la/wrong", "mediaPath": str(black)},
                {"body": "*Rayhaan Obsidian EDP 100ml*\nhttps://meli.la/right", "mediaPath": str(rayhaan)},
            ]
            tap.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            with patch.object(n, "TAP_FILE", tap), \
                 patch.object(n, "image_quality_tier", return_value=("acceptable", "ok")):
                self.assertEqual(n.recent_exact_source_image("Obsidian Rayhaan EDP 100ml"), str(rayhaan))
                self.assertEqual(n.recent_exact_source_image("Black Obsidian EDP 100ml"), str(black))

    def test_approved_asset_recovers_sku_from_legacy_body_not_headline_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "products"
            folder = root / "perfume-de-quem-bate-bola"
            image = folder / "approved_final" / "human.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"approved polo")
            body = "*PERFUME DE QUEM BATE BOLA*\n\n*Perfume Polo Sport EDT 100ml*\n\nhttps://meli.la/wZEd7G4"
            record = {
                "media_path": str(image),
                "product_guess": "PERFUME DE QUEM BATE BOLA",
                "body": body,
            }
            (folder / "index.json").write_text(json.dumps({
                "product_key": "perfume-de-quem-bate-bola",
                "canonical_guess": "PERFUME DE QUEM BATE BOLA",
                "approved": [record],
                "raw_items": [record],
            }), encoding="utf-8")
            n._IMAGE_METADATA_CACHE.clear()
            with patch.object(n, "IMAGE_BANK_PRODUCTS", root), \
                 patch.object(n, "IMAGE_TIER_DIRS", ("approved_final",)):
                dirs = n.approved_final_candidates("Polo Sport Ralph EDT Masculino 100ml")
                self.assertIn(image.parent, dirs)
                self.assertTrue(n.bank_asset_identity_allowed(
                    "Polo Sport Ralph EDT Masculino 100ml", image, folder
                ))
                self.assertFalse(n.bank_asset_identity_allowed(
                    "Polo Sport Ralph Feminino EDP 90ml", image, folder
                ))


if __name__ == "__main__":
    unittest.main()
