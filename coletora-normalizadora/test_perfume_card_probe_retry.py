import json
import subprocess
import unittest
from unittest.mock import patch

import perfume_manual_normalizer as n


def completed(payload, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=json.dumps(payload) + "\n", stderr="")


class CardProbeRetryTests(unittest.TestCase):
    def test_editorial_first_line_does_not_replace_salvo_product(self):
        body = """*UM DOS MELHORES ÁRABES, dupe do sauvage edp*

*Perfume Árabe Salvo EDP 100ml*

Por *R$ 147* no Pix"""
        self.assertEqual(n.extract_product(body), "Perfume Árabe Salvo EDP 100ml")

    def test_marketplace_by_connector_does_not_hide_khamrah_bank_identity(self):
        self.assertEqual(
            n.product_key("Perfume Khamrah Unissex By Edp 100ml"),
            "khamrah-100ml",
        )
        canonical = n.canonicalize_known_product_name("Khamrah By EDP Unissex 100ml")
        self.assertEqual(canonical, "Lattafa Khamrah EDP 100ml")
        candidates = n.approved_final_candidates(canonical)
        self.assertTrue(any("lattafa-khamrah-100ml" in str(path) for path in candidates))

    @patch.object(n.subprocess, "run")
    def test_retries_incomplete_card_and_keeps_second_success(self, run):
        run.side_effect = [
            completed({"ok": False, "screenshot": "/tmp/first.png"}),
            completed({"ok": True, "title": "Salvo Alhambra 100ml", "current_price": 171.92,
                       "seller": "Lipx", "official_store": True, "screenshot": "/tmp/second.png"}),
        ]
        card = n.browser_social_card_probe("https://meli.la/yYaqlLI")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(card["seller"], "Lipx")
        self.assertEqual([a["status"] for a in card["_probe_attempts"]], ["page_not_rendered", "ok"])

    @patch.object(n.subprocess, "run")
    def test_does_not_retry_complete_card(self, run):
        run.return_value = completed({"ok": True, "title": "Salvo", "current_price": 171.92,
                                      "screenshot": "/tmp/one.png"})
        card = n.browser_social_card_probe("https://meli.la/yYaqlLI")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(card["_probe_attempts"][0]["status"], "ok")

    @patch.object(n.subprocess, "run")
    def test_preserves_partial_evidence_and_failure_reason(self, run):
        run.side_effect = [
            completed({"ok": False, "title": "Salvo", "seller": "Lipx", "official_store": True,
                       "screenshot": "/tmp/partial.png"}),
            subprocess.TimeoutExpired(["node"], 110),
        ]
        card = n.browser_social_card_probe("https://meli.la/yYaqlLI")
        self.assertEqual(card["seller"], "Lipx")
        self.assertTrue(card["_probe_failed"])
        self.assertEqual(card["_probe_attempts"][1]["status"], "probe_timeout")

    @patch.object(n.subprocess, "run")
    def test_retries_complete_but_wrong_featured_product(self, run):
        common = {"ok": True, "current_price": 100, "screenshot": "/tmp/card.png"}
        run.side_effect = [
            completed({**common, "title": "Perfume Egeo Blue 90ml"}),
            completed({**common, "title": "Perfume Árabe Salvo EDP Masculino 100ml", "seller": "Lipx"}),
        ]
        card = n.browser_social_card_probe(
            "https://meli.la/yYaqlLI", "Perfume Árabe Salvo EDP Masculino 100ml"
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(card["seller"], "Lipx")
        self.assertEqual([a["status"] for a in card["_probe_attempts"]], ["identity_conflict", "ok"])


if __name__ == "__main__":
    unittest.main()