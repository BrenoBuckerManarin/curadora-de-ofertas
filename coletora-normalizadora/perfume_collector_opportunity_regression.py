#!/usr/bin/env python3
"""Offline regression for Perfume Collector opportunity selection. No sends/network."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perfume_deliver as p


def candidate(product, price, public, body=None):
    return p.validated_opportunity_score({
        "product": product,
        "body": body or f"{product}\nPor {price}",
        "price": price,
        "validated_price": public,
        "bank_image": "/approved.jpg",
        "source_media": "/source.jpg",
        "validation_ok": True,
    })


def main():
    ok, reason = p.is_fragrance_candidate(
        "Principia Kit 2 Gel de Limpeza GL-02",
        "Principia Kit 2 Gel de Limpeza GL-02 por R$ 80",
    )
    assert not ok and "não-perfume" in reason
    assert p.is_fragrance_candidate("The Blend Saffron 100ml", "The Blend Saffron por R$ 200")[0]
    assert p.is_fragrance_candidate("MAWWAL NAJMA", "MAWWAL NAJMA por R$ 200")[0]

    assert not p.is_fragrance_candidate(
        "Combo Cuide-se Bem Deleite: Loção Corporal 400ml + Refil 350ml",
        "O Boticário Combo Deleite por R$71",
    )[0]
    assert p.kit_item_count("Kit Natura Homem 3 itens") == 3
    assert p.kit_item_count("Combo Perfume 100ml + Gel 100ml") == 2
    kit_three = candidate("Kit Natura Homem 3 itens", "R$210", "R$240")
    assert "R$70/item" in kit_three["opportunity_reasons"]
    noisy_body = "IDEIA DE PRESENTE PRO PAIZÃO 150ML\nPerfume Natura Homem 100ml\nPor R$99"
    assert p.extract_product_name(noisy_body) == "Perfume Natura Homem 100ml"

    kit = candidate("Kit Natura Homem 2 itens", "R$159", "R$199")
    generic = candidate("Perfume Aleatório EDP 100ml", "R$129", "R$159")
    assert kit["opportunity_score"] > generic["opportunity_score"]
    assert "kit" in kit["opportunity_reasons"]

    favorite = candidate("Rabanne Invictus EDT 100ml", "R$299", "R$399")
    assert favorite["audience_score"] > 0
    assert any("favorito:invictus" == x for x in favorite["opportunity_reasons"])

    exceptional = candidate("Lattafa Asad 100ml", "R$99", "R$299")
    assert exceptional["inferred_discount_pct"] > 35
    assert "low_coupon_unconfirmed" == exceptional["price_confidence"]
    assert any(x.startswith("vantagem anunciada não validada:") for x in exceptional["opportunity_reasons"])
    assert "cupom não confirmado" in exceptional["opportunity_reasons"]
    assert exceptional["opportunity_score"] > generic["opportunity_score"]

    source_a = dict(exceptional, groupName="Fonte A")
    source_b = dict(exceptional, groupName="Fonte B", price="R$101", effective_price=101.0)
    consensus = p.apply_source_consensus([source_a, source_b])
    assert all(x["source_consensus_count"] == 2 for x in consensus)
    assert all(x["price_confidence"] == "medium_source_consensus" for x in consensus)
    assert all(any(r == "consenso:2 fontes" for r in x["opportunity_reasons"]) for x in consensus)

    a = candidate("Natura Homem Evolut.io 100ml", "R$147", "R$173")
    a["validated_product"] = "Perfume Natura Homem Evolutio Deo Parfum 100 ml"
    a["groupName"] = "Fonte A"
    b = candidate("Natura Homem Evolutio 100ml", "R$148", "R$173")
    b["validated_product"] = "Natura Homem Evolutio 100ml Masculino"
    b["groupName"] = "Fonte B"
    collapsed = p.collapse_same_product_offers([b, a])
    assert len(collapsed) == 1
    assert collapsed[0]["price"] == "R$147"
    assert collapsed[0]["alternative_offer_count"] == 1

    asad_expensive = candidate("Lattafa Asad 100ml", "R$169", "R$199")
    asad_expensive["validated_product"] = "Perfume Lattafa Asad Eau de Parfum Masculino 100 ml"
    asad_expensive["groupName"] = "#84 FONTE ALFA"
    asad_better = candidate("Lattafa Asad 100ml", "R$146", "R$172")
    asad_better["validated_product"] = "Lattafa Asad EDP Spray 100ml"
    asad_better["groupName"] = "Fonte Delta"
    asad_collapsed = p.collapse_same_product_offers([asad_expensive, asad_better])
    assert len(asad_collapsed) == 1
    assert asad_collapsed[0]["price"] == "R$146"
    assert asad_collapsed[0]["groupName"] == "Fonte Delta"

    first = "Perfume Árabe Bidaya Maktub La Vie EDP 100ml\nPor R$190\nhttps://meli.la/A"
    second = "➡️ Perfume Árabe Bidaya Maktub La Vie EDP 100ml Oferta no Meli\nPor R$190\nhttps://meli.la/B"
    assert set(p.normalize_offer_keys(first)).intersection(p.normalize_offer_keys(second))

    diverse = p.select_diverse_batch([
        {"product": "N1", "offer_category": "national", "opportunity_score": 110},
        {"product": "N2", "offer_category": "national", "opportunity_score": 108},
        {"product": "N3", "offer_category": "national", "opportunity_score": 106},
        {"product": "D1", "offer_category": "designer", "opportunity_score": 102},
        {"product": "K1", "offer_category": "kit", "opportunity_score": 80},
    ], 3)
    assert sum(x["offer_category"] == "national" for x in diverse) <= 2
    assert any(x["offer_category"] == "designer" for x in diverse)
    assert [x["opportunity_score"] for x in diverse] == sorted((x["opportunity_score"] for x in diverse), reverse=True)

    assert p.classify_offer_category("Kaiak Tradicional 100ml", "") == "national"
    assert p.classify_offer_category("Fakhar Gold 100ml", "") == "arabic"
    assert p.classify_offer_category("Antonio Banderas The Icon 100ml", "") == "budget_imported"
    assert p.classify_offer_category("Jimmy Choo Man Aqua 30ml", "") == "designer"
    assert p.canonical_product_key("Armaf Club De Nuit Milestone - Compartilhável EDP 105ml") == p.canonical_product_key("Armaf Club De Nuit Milestone 105ml")

    ranked = []
    for i in range(15):
        ranked.append({
            "messageId": f"n{i}", "product": f"Natura Kaiak Linha {i} 100ml",
            "body": f"Natura Kaiak Linha {i} 100ml por R${200+i}", "price": f"R${200+i}",
            "candidate_score": 100-i, "groupName": "Fonte Nacional",
        })
    hidden_arabic = {
        "messageId": "arabic-gem", "product": "Lattafa Asad 100ml",
        "body": "Lattafa Asad 100ml por R$99", "price": "R$99",
        "candidate_score": 10, "groupName": "Fonte Árabe",
    }
    ranked.append(hidden_arabic)
    multi_axis = p.build_validation_shortlist(ranked, 12)
    assert hidden_arabic in multi_axis
    assert len(multi_axis) == 12

    old_funnel = p.COLLECTOR_FUNNEL_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            p.COLLECTOR_FUNNEL_FILE = Path(td) / "funnel.json"
            p.write_collector_funnel(ranked, ranked, multi_axis, [], [], [], [])
            funnel = __import__('json').loads(p.COLLECTOR_FUNNEL_FILE.read_text())
            assert funnel["counts"]["shortlist"] == 12
            assert funnel["shortlist_categories"]["arabic"] >= 1
    finally:
        p.COLLECTOR_FUNNEL_FILE = old_funnel

    # Competitive kit within 12 points of the cutoff gets one slot.
    with_kit = p.select_diverse_batch([
        {"product": "N1", "offer_category": "national", "opportunity_score": 110},
        {"product": "D1", "offer_category": "designer", "opportunity_score": 105},
        {"product": "A1", "offer_category": "arabic", "opportunity_score": 100},
        {"product": "K1", "offer_category": "kit", "opportunity_score": 92},
    ], 3)
    assert any(x["offer_category"] == "kit" for x in with_kit)

    old_state = p.STATE_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            p.STATE_FILE = str(Path(td) / "state.json")
            p.save_state({"seen_ids": [], "last_position": 0, "delivered_offer_keys": {}, "delivery_progress": {}})
            p.set_delivery_progress("m1", "promo_sent")
            assert p.delivery_progress("m1") == "promo_sent"
            # Partial progress is not the same as delivered/seen.
            assert "m1" not in p.load_state().get("seen_ids", [])
            p.mark_messages_seen([{"messageId": "m1", "body": "Perfume X por R$100 https://meli.la/x"}])
            p.clear_delivery_progress(["m1"])
            assert "m1" in p.load_state().get("seen_ids", [])
            assert p.delivery_progress("m1") == ""
    finally:
        p.STATE_FILE = old_state

    print("collector opportunity regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
