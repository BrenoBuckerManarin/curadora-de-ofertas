#!/usr/bin/env python3
"""Focused no-network tests for the approved-headline repertoire."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("perfume_manual_normalizer.py")
spec = importlib.util.spec_from_file_location("pmn_repertoire_test", SCRIPT)
pmn = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pmn)


def load_module(tmp: Path) -> None:
    pmn.EDITORIAL_CONTEXT_FILE = tmp / "editorial_context.json"
    pmn.IMAGE_BANK_ALIASES = tmp / "aliases.json"
    pmn.HEADLINE_USAGE_FILE = tmp / "headline_repertoire_usage.json"
    pmn.EDITORIAL_CYCLES_FILE = tmp / "editorial_comparison_cycles.jsonl"
    pmn._HEADLINE_RESERVED.clear()
    pmn.approved_headline_for_product = lambda product: ""
    os.environ["PERFUME_HEADLINE_EXPLORATION_RATE"] = "0"
    os.environ["PERFUME_REUSE_APPROVED_HEADLINE"] = "1"


def write_cycles(path: Path, product: str, headlines: list[str]) -> None:
    rows = [
        {"product": product, "after": {"headline": h}, "edition_final_ts": i + 1}
        for i, h in enumerate(headlines)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_repertoire_uses_three_comparator_after_headlines_and_rotates() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        product = "Nautica Voyage EDT 100ml"
        write_cycles(tmp / "editorial_comparison_cycles.jsonl", product, [
            "VOYAGE AQUÁTICO 🌊", "FRESCOR DE MAÇÃ VERDE", "VOYAGE PUXA O VERÃO 🌊",
        ])
        pmn.llm_headline = lambda *args: (_ for _ in ()).throw(AssertionError("LLM should not run"))
        got = pmn.make_headline(product, "", price_context={"current_price": 79})
        assert got == "*VOYAGE PUXA O VERÃO 🌊*", got
        assert pmn.LAST_HEADLINE_SOURCE == "approved_repertoire"
        assert "3 eligible" in pmn.LAST_HEADLINE_REASON
        assert json.loads((tmp / "headline_repertoire_usage.json").read_text())[pmn.product_key(product)][-1]["headline"] == got

        got2 = pmn.make_headline(product, "", price_context={"current_price": 79})
        assert got2 == "*FRESCOR DE MAÇÃ VERDE*", got2


def test_price_claim_is_ineligible_and_falls_back_to_other_repertoire_headline() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        product = "Al Wataniah Bareeq Al Dhahab EDP 100ml"
        write_cycles(tmp / "editorial_comparison_cycles.jsonl", product, [
            "O LEGÍTIMO OUD ÁRABE A 112 ✨", "O OUD ÁRABE QUE CONQUISTA", "O MARCANTE OUD ÁRABE 🤌🏻",
            "O OUD ÁRABE DE PRESENÇA",        ])
        pmn.llm_headline = lambda *args: (_ for _ in ()).throw(AssertionError("LLM should not run"))
        got = pmn.make_headline(product, "", price_context={"current_price": 149})
        assert got == "*O OUD ÁRABE DE PRESENÇA*", got
        assert "price" in pmn.LAST_HEADLINE_REASON.lower()


def test_fewer_than_two_comparator_headlines_keeps_existing_llm_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        product = "Nautica Voyage EDT 100ml"
        write_cycles(tmp / "editorial_comparison_cycles.jsonl", product, ["VOYAGE AQUÁTICO 🌊"])
        def llm(*args):
            pmn.LAST_HEADLINE_SOURCE = "llm_stub"
            return "*LLM NOVO*"
        pmn.llm_headline = llm
        got = pmn.make_headline(product, "", price_context={"current_price": 79})
        assert got == "*LLM NOVO*", got
        assert pmn.LAST_HEADLINE_SOURCE.startswith("llm")


def test_editorial_context_is_only_fallback_after_llm() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        product = "Natura Homem Sagaz Deo Parfum 100ml"
        pmn.context_approved_headline = lambda _product: "*SAGAZ PRA TODO DIA*"
        def llm(*args):
            pmn.LAST_HEADLINE_SOURCE = "llm_stub"
            return "*SAGAZ QUE CHEGA LIGEIRO*"
        pmn.llm_headline = llm
        got = pmn.make_headline(product, "", price_context={"current_price": 139})
        assert got == "*SAGAZ QUE CHEGA LIGEIRO*", got
        assert pmn.LAST_HEADLINE_SOURCE == "llm_stub"


def test_editorial_context_survives_as_llm_failure_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        product = "Natura Homem Sagaz Deo Parfum 100ml"
        pmn.context_approved_headline = lambda _product: "*SAGAZ PRA TODO DIA*"
        pmn.llm_headline = lambda *args: ""
        got = pmn.make_headline(product, "", price_context={"current_price": 139})
        assert got == "*SAGAZ PRA TODO DIA*", got
        assert pmn.LAST_HEADLINE_SOURCE == "editorial_context_fallback"


def test_quasar_below_zero_is_not_misread_as_price() -> None:
    allowed, reason = pmn._repertoire_price_allowed("QUASAR ABAIXO DE 0 ❄️", {"current_price": 79})
    assert allowed, reason


def test_generic_pra_sair_tail_cannot_be_rescued_by_product_name() -> None:
    ctx = {
        "_product": "Malbec Tradicional 100ml",
        "_price_attractive": False,
        "_recent_molds": {},
        "olfactive_terms": [],
        "safe_angles": [],
    }
    generic = pmn.headline_score("*MALBEC PRA SAIR 🤵🏻‍♂️*", dict(ctx))
    concrete = pmn.headline_score("*MALBEC DE JEITO CLÁSSICO*", dict(ctx))
    assert concrete > generic + 30, (generic, concrete)


def test_exploration_rotates_by_appearance_not_permanent_sku_bucket() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        load_module(tmp)
        os.environ["PERFUME_HEADLINE_EXPLORATION_RATE"] = "0.20"
        product = "Nautica Voyage EDT 100ml"
        write_cycles(tmp / "editorial_comparison_cycles.jsonl", product, [
            "VOYAGE AQUÁTICO 🌊", "FRESCOR DE MAÇÃ VERDE", "VOYAGE PUXA O VERÃO 🌊",
        ])
        # Three options are still growing: the fourth appearance explores.
        def llm(*args):
            pmn.LAST_HEADLINE_SOURCE = "llm_stub"
            return "*LLM RENOVOU*"
        pmn.llm_headline = llm
        from unittest.mock import patch
        with patch('perfume_editorial_renewal.enqueue') as enqueue:
            got = pmn.make_headline(product, "", price_context={"current_price": 79})
            assert got != "*LLM RENOVOU*", got
            assert pmn.LAST_HEADLINE_SOURCE == 'approved_repertoire'
            enqueue.assert_called_once()
        assert (tmp / "headline_repertoire_usage.json").exists()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("OK perfume_headline_repertoire_test")
