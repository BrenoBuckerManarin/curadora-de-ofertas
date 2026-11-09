#!/usr/bin/env python3
"""No-network benchmark for local repertoire vs controlled generation stub."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).with_name("perfume_manual_normalizer.py")
spec = importlib.util.spec_from_file_location("pmn_benchmark", SCRIPT)
pmn = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pmn)

PRODUCTS = [
    "Qaed Al Fursan Lattafa EDP 90ml",
    "Nautica Voyage EDT 100ml",
    "Al Wataniah Bareeq Al Dhahab EDP 100ml",
    "Al Noble Safeer Lattafa 100ml",
    "Lattafa Fakhar Gold Extrait EDP 100ml",
]

with tempfile.TemporaryDirectory() as td:
    pmn.HEADLINE_USAGE_FILE = Path(td) / "usage.json"
    os.environ["PERFUME_REUSE_APPROVED_HEADLINE"] = "0"
    pmn._HEADLINE_RESERVED.clear()
    price = {"current_price": 79}
    local_ns = []
    selected = []
    for product in PRODUCTS:
        start = time.perf_counter_ns()
        headline = pmn.approved_headline_repertoire(product, price)
        local_ns.append(time.perf_counter_ns() - start)
        selected.append((product, headline or "<no eligible repertoire>", pmn.LAST_HEADLINE_REASON))

    def controlled_generation(*args):
        time.sleep(0.050)
        return "*STUB GERADO*"

    pmn.llm_headline = controlled_generation
    pmn.approved_headline_for_product = lambda product: ""
    pmn.context_approved_headline = lambda product: ""
    generation_ns = []
    for product in PRODUCTS:
        start = time.perf_counter_ns()
        result = pmn.make_headline(product, "", price_context=price)
        generation_ns.append(time.perf_counter_ns() - start)
        if result != "*STUB GERADO*":
            raise AssertionError(result)

    def ms(values):
        return [round(v / 1_000_000, 3) for v in values]
    print("BENCHMARK no_network=true whatsapp=false products=5")
    print("local_ms=" + repr(ms(local_ns)))
    print("local_total_ms=" + repr(round(sum(local_ns) / 1_000_000, 3)))
    print("stub_generation_ms=" + repr(ms(generation_ns)))
    print("stub_generation_total_ms=" + repr(round(sum(generation_ns) / 1_000_000, 3)))
    print("local_selected=" + repr([(p, h) for p, h, _ in selected]))
    print("provider_timing=excluded; deterministic 50ms sleep stub only")
