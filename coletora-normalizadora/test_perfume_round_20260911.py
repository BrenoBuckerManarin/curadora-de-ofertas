"""Regression cases from actual Sept 11 outputs; no sends or network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import perfume_manual_normalizer as n


class RoundTests(unittest.TestCase):
    def test_flankers_cannot_reuse_generic_folder_or_context(self):
        for variant in ('Oud', 'Mirra', 'Exclusivo', 'Atrai'):
            wanted = f'Natura Essencial {variant} Masculino 100ml'
            self.assertFalse(n.identity_compatible(wanted, 'natura-essencial-100ml', n.DISTINCTIVE_VARIANT_TOKENS, strict=True))
            self.assertTrue(n.identity_compatible(wanted, f'natura-essencial-{variant.lower()}-100ml', n.DISTINCTIVE_VARIANT_TOKENS, strict=True))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = root/'context.json'
            key = 'natura-essencial-100ml'
            context.write_text(json.dumps({key: {'human_fact_source': 'legacy', 'safe_angles': ['presença feminina'], 'olfactive_terms':['jasmim']}}))
            with patch.object(n, 'EDITORIAL_CONTEXT_FILE', context), patch.object(n, 'editorial_identity_keys', return_value={key}), patch.object(n, '_context_keys_for_product', return_value=[key]):
                self.assertEqual(n._load_editorial_context_base('Natura Essencial Masculino 100ml'), {})
                self.assertEqual(n._load_editorial_context_base('Natura Essencial Mirra Masculino 100ml'), {})

    def test_all_final_routes_reject_generic_even_with_emoji(self):
        for text in ('ACHADO PRA SAIR DO ÓBVIO', 'BOTICÁRIO COM CHEIRO DE CARO', 'LUXO DE DUBAI', 'ÁRABE OPULENTO', 'KIT PRESENTE PRA APROVEITAR'):
            for suffix in ('', ' 🤌🏻'):
                self.assertEqual(n.enforce_headline_output(text + suffix), '')
        with patch.object(n, '_make_headline_impl', return_value='*ACHADO PRA SAIR DO ÓBVIO*'), patch.object(n, 'llm_headline_retry', return_value='*O ESSENCIAL QUE ATRAI*') as retry:
            self.assertEqual(n.make_headline('Essencial Atrai',''), '*O ESSENCIAL QUE ATRAI*')
            retry.assert_called_once()
        with patch.object(n, '_make_headline_impl', return_value='*LUXO DE DUBAI*'), patch.object(n, 'llm_headline_retry', return_value=''):
            self.assertTrue(n.make_headline('Eudora Pulse Action Perfume Masculino 100ml',''))
            self.assertEqual(n.LAST_HEADLINE_SOURCE, 'fallback_rule')
        self.assertEqual(n.enforce_headline_output('CEBOLINHA CÍTLICO SUAVE 🍊'), '*CEBOLINHA CÍTLICO SUAVE 🍊*')

    def test_titles_keep_kit_component_sizes_and_hair_type(self):
        kit = n.compact_product_name('Kit Hidratante Corporal Angel 200ml + Perfume Angel 100ml Marca Linha Isabelle Labelle Original Presente Perfeito Alta Fixação Parfum')
        self.assertIn('200ml', kit)
        self.assertIn('100ml', kit)
        self.assertIn('+', kit)
        self.assertNotIn('Marca Linha', kit)
        self.assertNotIn('Presente Perfeito', kit)
        hair = n.compact_product_name('Para Os Cabelos Carolina Herrera Good Girl Hair Mist Feminino 30ml')
        self.assertEqual(hair, 'Carolina Herrera Good Girl Hair Mist Feminino 30ml')


if __name__ == '__main__':
    unittest.main()
