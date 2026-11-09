#!/usr/bin/env python3
from decimal import Decimal as D
from perfume_offer_engine import Evidence as E, IdentityStatus, SourceOffer, collect_source_prices, products_conflict, reconcile_offer


def ev(field, value, item, **kw):
    return E(field, value, kw.pop('source', 'fixture'), item_id=item, **kw)


def main():
    assert collect_source_prices('✅ R$ 161\nCupom: X') == (None, D('161.00'))
    assert collect_source_prices('De R$ ~379~ por R$ 166') == (D('379.00'), D('166.00'))
    assert collect_source_prices('De ~R$349,00~ ❌\nPor *R$245,00* ✅️') == (D('349.00'), D('245.00'))
    assert collect_source_prices('MENOS DE 90 CONTO\nProduto X\nPor R$ 88') == (None, D('88.00'))
    assert products_conflict('Kit Boticário Egeo Original', 'O Boticário Malbec Icon Presente 2 Itens')
    assert products_conflict('Lattafa Asad 100ml', 'Perfume Eclaire Lattafa 100ml')
    assert not products_conflict('Al Wataniah Sabah 100ml', 'Perfume Sedutor Árabe Sabah 100ml Original')
    source = SourceOffer('Kit Boticário Egeo Original', 'x', D('135'), coupons=('FULL',))
    rows = [ev('product_name', 'O Boticário Malbec Icon Presente 2 Itens', 'MLBX', confidence='exact_card', card_role='featured')]
    assert reconcile_offer(source, rows).identity.status == IdentityStatus.CONFLICT

    # Same item enriches; sibling cannot lend Pix/installments/store.
    source = SourceOffer('Mawwal Malek', 'x', D('195'), D('299'))
    rows = [
        ev('product_name', 'Mawwal Malek', 'MLB1', confidence='exact', card_role='featured'),
        ev('price', D('195'), 'MLB1', confidence='exact'),
        ev('pix_price', D('180'), 'MLB2', confidence='exact'),
        ev('installments', '6x', 'MLB2', confidence='exact', no_interest=True, price_base=D('195')),
        ev('official_store', 'Vizinha', 'MLB2', confidence='exact'),
    ]
    r = reconcile_offer(source, rows)
    assert r.identity.item_id == 'MLB1'
    assert r.price_kind == 'unknown' and r.installments is None and r.official_store is None
    assert r.decisions['price'].status == 'confirmed'

    # A different public same-item price is a conflict, not replacement authority.
    source = SourceOffer('Campaign X', 'x', D('88'), coupons=('FULL',))
    rows = [
        ev('product_name', 'Campaign X', 'MLBC', confidence='exact', card_role='featured'),
        ev('price', D('129.90'), 'MLBC', confidence='exact'),
    ]
    r = reconcile_offer(source, rows)
    assert r.price == D('88') and r.decisions['price'].status == 'campaign_claim'

    source = SourceOffer('Link authority', 'x', D('88'))
    rows = [
        ev('product_name', 'Link authority', 'MLBL', confidence='exact', card_role='featured'),
        ev('price', D('129.90'), 'MLBL', confidence='exact'),
    ]
    r = reconcile_offer(source, rows)
    assert r.price == D('129.90') and r.decisions['price'].status == 'corrected_from_exact_link'

    # Liquid Brun: coupon below same official item's public Pix tier.
    source = SourceOffer('Liquid Brun', 'x', D('274'), D('469'), coupons=('FULL',))
    rows = [
        ev('product_name', 'Liquid Brun', 'MLB3', confidence='exact_card', card_role='featured'),
        ev('pix_price', D('299.92'), 'MLB3', confidence='exact_card', price_kind='pix'),
        ev('original_price', D('469'), 'MLB3', confidence='exact_card'),
        ev('official_store', 'Lipx', 'MLB3', confidence='exact_card'),
    ]
    r = reconcile_offer(source, rows)
    assert r.price == D('274') and r.price_kind == 'pix' and r.official_store == 'Lipx'
    assert r.original_price == D('469')

    # Nautica: same product context/another offer cannot classify source as Pix.
    source = SourceOffer('Nautica Voyage', 'x', D('98'), coupons=('FULL',))
    rows = [
        ev('product_name', 'Nautica Voyage', 'MLB4', confidence='exact', card_role='featured'),
        ev('pix_price', D('111.16'), 'MLB5', confidence='exact', product_id='P1'),
    ]
    r = reconcile_offer(source, rows)
    assert r.price == D('98') and r.price_kind == 'unknown'

    # Explicit source Pix survives a public tier mismatch caused by coupon.
    source = SourceOffer('Sabah', 'x', D('88'), D('230'), says_pix=True, coupons=('FULL',))
    rows = [
        ev('product_name', 'Sabah', 'MLB6', confidence='exact', card_role='featured'),
        ev('pix_price', D('103.99'), 'MLB6', confidence='exact'),
    ]
    r = reconcile_offer(source, rows)
    assert r.price_kind == 'pix' and r.original_price == D('230')

    # Small De is noise.
    source = SourceOffer('X', 'x', D('95'), D('100'))
    rows = [ev('product_name', 'X', 'MLB7', confidence='exact', card_role='featured')]
    assert reconcile_offer(source, rows).original_price is None

    # Installments require same item, same price base and no_interest.
    source = SourceOffer('CH', 'x', D('200'))
    rows = [
        ev('product_name', 'CH', 'MLB8', confidence='exact', card_role='featured'),
        ev('installments', '10x', 'MLB8', confidence='exact', no_interest=True, price_base=D('200')),
    ]
    assert reconcile_offer(source, rows).installments == '10x'
    source = SourceOffer('D&G', 'x', D('302'), coupons=('SUPERPROMO',))
    rows = [
        ev('product_name', 'D&G', 'MLB9', confidence='exact_card', card_role='featured'),
        ev('installments', '7x', 'MLB9', confidence='exact_card', no_interest=True, price_base=D('356')),
    ]
    assert reconcile_offer(source, rows).installments == '7x'
    print('OK perfume_offer_engine regression')


if __name__ == '__main__':
    main()
