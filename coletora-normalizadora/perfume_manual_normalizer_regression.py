#!/usr/bin/env python3
"""Regression checks for Perfume Manual Normalizer editorial/runtime rules.

No network, no WhatsApp send. Fails fast if future edits make the normalizer
forget validated ML prices, product-specific headlines, or image fallback.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

SCRIPT = Path('/home/ubuntu/.hermes/scripts/perfume_manual_normalizer.py')
spec = importlib.util.spec_from_file_location('pmn', SCRIPT)
pmn = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pmn)

ASAD_RAW = '''*FIXAÇÃO ABSURDA*

*Lattafa Asad 100ml*

De R$ 279 por *R$ 156* 🦸🏻‍♂️

https://meli.la/3MEngCl'''

ASAD_JINA = '''
Title: Perfume Lattafa Asad 100ml Eau De Parfum Original Árabe Original Com Nota Fiscal E Selo Original Lataffa

~~R$289,99~~

R$156,32 46% OFF no Pix

ou R$195,90 em outros meios
'''

QUASAR_RAW = '''*Quasar Fire 100ml*

De R$ 219 por *R$ 197* 🦸🏻‍♂️
Cupom: *BOLABOA* 🎟️

https://meli.la/3GngrKI'''

MALBEC_RAW = '''*MALBEC SIGNATURE*

*Evolui na pele igual vinho, o melhor da linha*

Por *R$ 296* 🦸🏻‍♂️
🎟️Cupom: *BOLABOA*

https://meli.la/WN93xin'''

MALBEC_JINA = '''
Title: O Boticário Malbec Signature Eau De Parfum Masculino 90ml Volume Da Unidade 90 Ml

# GrupoFinal das Promoções | Perfil Social

[O Boticário Malbec Signature Eau De Parfum Masculino 90ml Volume Da Unidade 90 Ml](https://www.mercadolivre.com.br/o-boticario-malbec-signature-eau-de-parfum-masculino-90ml-volume-da-unidade-90-ml/p/MLB36213701)

R$343

## Quem viu este produto também comprou

Perfume Malbec Signature Eau De Parfum 90ml O Boticário Masculino

R$333
'''

YARA_RAW = '''*PREÇÃO NESSE ÁRABE PRA ELAS*

*Perfume Árabe Yara Lattafa EDP 100ml Feminino*

De R$ 239 por *R$ 147* no Pix 🦸🏻‍♂️
Cupom: *FESTAGOL* 🎟️
_Selecione "Pix" na tela de pagamento_

https://meli.la/XRW3ihX'''

YARA_JINA = '''
Title: Perfume Arabe Yara Lattafa Eau De Parfum 100ml Original C Nf

# GrupoFinal das Promoções | Perfil Social

MAIS VENDIDO[Perfume Arabe Yara Lattafa Eau De Parfum 100ml Original C Nf](https://www.mercadolivre.com.br/perfume-arabe-yara-lattafa-eau-de-parfum-100ml-original-c-nf/up/MLBU3619526469)Por Lipx

~~R$257,90~~

R$163,92 36% OFF no Pix

ou R$204,90 em outros meios

[Ir para produto](https://www.mercadolivre.com.br/perfume-arabe-yara-lattafa-eau-de-parfum-100ml-original-c-nf/up/MLBU3619526469)

Para você

## Quem viu este produto também comprou

R$226
'''


def fake_jina(link: str | None) -> str:
    if link and '2yaYpnQ' in link:
        return ASAD_JINA
    if link and '1hKe7FC' in link:
        return MALBEC_JINA
    if link and '2eqUyGw' in link:
        return YARA_JINA
    return ''


def assert_in(needle: str, haystack: str) -> None:
    if needle not in haystack:
        raise AssertionError(f'missing {needle!r} in:\n{haystack}')


def main() -> int:
    os.environ['PERFUME_HEADLINE_LLM'] = '0'
    # Keep the legacy deterministic suite isolated from the production
    # approved-repertoire feature. Repertoire behavior has focused tests in
    # perfume_headline_repertoire_test.py.
    os.environ['PERFUME_REUSE_APPROVED_HEADLINE'] = '0'

    # "De" is editorial signal, not decoration: small deltas stay hidden.
    if pmn.meaningful_de_price('R$ 100', 'R$ 95') is not None:
        raise AssertionError('tiny De/Por delta should be omitted')
    if pmn.meaningful_de_price('R$ 100', 'R$ 70') is None:
        raise AssertionError('meaningful De/Por discount should be preserved')
    if not pmn.product_variant_conflict('Lattafa Asad 100ml', 'Lattafa Eclaire 100ml'):
        raise AssertionError('Asad/Eclaire must be treated as a product-link conflict')
    if pmn.product_variant_conflict('Afnan Turathi EDP 90ml', 'Afnan Turathi Blue EDP 90ml'):
        raise AssertionError('underspecified Turathi must allow exact-card Blue enrichment')
    if pmn.product_variant_conflict('Lattafa Fakhar 100ml', 'Lattafa Fakhar Gold 100ml'):
        raise AssertionError('underspecified Fakhar must allow exact-card Gold enrichment')
    if not pmn.product_variant_conflict('Lattafa Fakhar Gold 100ml', 'Lattafa Fakhar Black 100ml'):
        raise AssertionError('explicit Gold/Black variants must conflict')

    # Link resolver contract: /p/ is product context, never an item id; only a
    # candidate whose API payload agrees with product_id/title becomes exact.
    if pmn.ml_api_item_id('https://www.mercadolivre.com.br/x/p/MLB12345678'):
        raise AssertionError('/p/ catalog id was mistaken for listing item id')
    old_item_payload = pmn.ml_api_item_payload
    try:
        payloads = {
            'MLB1111111111': {'catalog_product_id': 'MLB12345678', 'title': 'Perfume Teste Gold 100ml'},
            'MLB2222222222': {'catalog_product_id': 'MLB99999999', 'title': 'Produto Vizinho 100ml'},
        }
        pmn.ml_api_item_payload = lambda iid: payloads[iid]
        html = ('<meta property="og:title" content="Perfume Teste Gold 100ml">'
                '<a href="https://x/p/MLB12345678?wid=MLB2222222222">vizinho</a>'
                ' pdp_filters=item_id:MLB1111111111')
        ident = pmn.resolve_link_offer('https://meli.la/teste', html)
        if ident.get('item_id') != 'MLB1111111111' or ident.get('confidence') != 'exact':
            raise AssertionError(f'link resolver missed exact promoted item: {ident}')
    finally:
        pmn.ml_api_item_payload = old_item_payload

    # Exact-card boundary: Pix/parcel from a sibling must not cross item_id.
    exact_html = '''
    "title":{"text":"Produto Certo"},"price":{"previous_price":{"value":200},
    "current_price":{"value":150},"discount_label":{"text":"25% OFF no Pix"},
    "installments":{"text":"ou {price_total} em outros meios","no_interest":false,
    "values":[{"price":{"value":180}}]}},"url":"/p/MLB10?wid=MLB1111111111"
    "title":{"text":"Produto Vizinho"},"price":{"current_price":{"value":150},
    "installments":{"text":"10x {price}","no_interest":true}},
    "url":"/p/MLB20?wid=MLB2222222222"
    '''
    facts = pmn.extract_exact_item_facts(exact_html, 'MLB1111111111')
    if facts.get('pix_price') != 150.0 or facts.get('other_methods_price') != 180.0:
        raise AssertionError(f'exact Pix modalities not extracted: {facts}')
    if facts.get('installments'):
        raise AssertionError(f'sibling installments leaked into exact card: {facts}')
    sibling_facts = pmn.extract_exact_item_facts(exact_html, 'MLB2222222222')
    if sibling_facts.get('installments') != '10x' or sibling_facts.get('pix_price') is not None:
        raise AssertionError(f'sibling typed facts wrong: {sibling_facts}')

    # Offer-identity contract: commercial facts may enrich only the same item.
    same = pmn.resolve_offer_evidence(
        {'item_id': 'MLB111', 'product_id': 'MLBPROD', 'price': 100.0},
        [{'source': 'exact_item_html', 'item_id': 'MLB111', 'product_id': 'MLBPROD',
          'pix': True, 'installments': '10x', 'store_name': 'Loja Certa'}],
    )
    if not same.get('pix') or same.get('installments') != '10x' or same.get('store_name') != 'Loja Certa':
        raise AssertionError(f'same-offer evidence failed to enrich: {same}')
    sibling = pmn.resolve_offer_evidence(
        {'item_id': 'MLB111', 'product_id': 'MLBPROD', 'price': 100.0},
        [{'source': 'catalog_sibling', 'item_id': 'MLB222', 'product_id': 'MLBPROD',
          'price': 80.0, 'pix': True, 'installments': '12x', 'store_name': 'Loja Errada'}],
    )
    if sibling.get('price') != 100.0 or sibling.get('pix') or sibling.get('installments') or sibling.get('store_name'):
        raise AssertionError(f'different offer contaminated source: {sibling}')
    if not sibling.get('evidence_rejected') or sibling['evidence_rejected'][0].get('relation') != 'different_offer':
        raise AssertionError(f'different-offer rejection not audited: {sibling}')
    exact = pmn.resolve_offer_evidence(
        {'item_id': None, 'product_id': 'MLBPROD', 'price': 100.0},
        [{'source': 'ml_api_exact_item', 'item_id': 'MLB333', 'product_id': 'MLBPROD',
          'exact_link': True, 'allow_price_override': True, 'price': 120.0, 'official_store_id': 7}],
    )
    if exact.get('item_id') != 'MLB333' or exact.get('price') != 120.0 or exact.get('official_store_id') != 7:
        raise AssertionError(f'exact-link evidence did not establish identity: {exact}')

    social = '''<div class="poly-card"><a href="/p/MLB6093757?wid=MLB1945750874&amp;c_id=/home/card-featured/element" class="poly-component__title">CK Be 200ml</a><span class="poly-component__seller">Por Rocha Importados <svg aria-label="Loja oficial"></svg></span></div>
    <div class="poly-card"><a href="/p/MLB1?wid=MLB9999999999" class="poly-component__title">Outro perfume</a><span class="poly-component__seller">Por Loja Errada <svg aria-label="Loja oficial"></svg></span></div>'''
    store = pmn.extract_ml_official_store_from_html(
        social, expected_product='CK Be 200ml', expected_item_id='MLB1945750874')
    if store != 'Rocha Importados':
        raise AssertionError(f'exact featured official store not detected: {store!r}')
    wrong = pmn.extract_ml_official_store_from_html(
        social, expected_product='Produto Sem Relação', expected_item_id='MLB8888888888')
    if wrong:
        raise AssertionError(f'unrelated official card leaked into trust: {wrong!r}')

    pmn.jina_fetch = fake_jina
    pmn.browser_social_card_probe = lambda *a, **k: {}
    # Hermetic run: live ML fetches leaked real catalog pages into the test
    # (2026-07-09: meli.la/XRW3ihX served a Victoria Lattafa page and the Yara
    # case failed on data drift, not code). Regression must not hit network.
    pmn._ml_fetch_html = lambda *a, **k: ''
    pmn.ml_catalog_lookup = lambda *a, **k: {}
    if hasattr(pmn, '_ml_catalog_cache_get'):
        pmn._ml_catalog_cache_get = lambda *a, **k: None

    copy, meta = pmn.normalize_copy(ASAD_RAW)
    assert meta['product'] == 'Lattafa Asad 100ml', meta
    first_line = next((line for line in copy.splitlines() if line.strip()), '')
    if not re.fullmatch(r'\*[^*]{6,42}\*', first_line):
        raise AssertionError(f'headline format regressed: {first_line!r}\n{copy}')
    assert_in('De R$ 289 por *R$ 156* no Pix', copy)
    if 'R$ 289,99' in copy or 'R$ 156,32' in copy:
        raise AssertionError(f'price cents leaked into copy:\n{copy}')
    if '*ÁRABE BAIXOU BEM*' in copy or '*FIXAÇÃO ABSURDA*' in copy:
        raise AssertionError(f'bad/generic copied headline in:\n{copy}')

    # Must use curated image-bank approved_final before falling back to incoming
    # WhatsApp media. This guards the exact integration Breno asked not to lose.
    asad_fallback = '/home/ubuntu/.hermes/image_cache/img_98d01243ad23.jpg'
    image = pmn.find_image(meta['product'], asad_fallback)
    if not image or not Path(image).exists():
        raise AssertionError(f'expected Asad approved_final image, got {image!r}')
    if '/approved_final/' not in image:
        raise AssertionError(f'Asad must use approved_final before fallback, got {image!r}')
    report = pmn.normalizer_report(meta, image, asad_fallback)
    assert_in('APROVADO', report)
    if 'sem banco' in report or 'sem imagem' in report:
        raise AssertionError(f'approved Asad image reported as missing:\n{report}')

    copy2, meta2 = pmn.normalize_copy(QUASAR_RAW)
    fallback = '/home/ubuntu/.hermes/image_cache/img_bbe983190090.jpg'
    image2 = pmn.find_image(meta2['product'], fallback)
    # 2026-07-07 update: Breno approved a new Quasar Fire final on 2026-07-04
    # (breno_final_20260704-*). The old expectation of "no usable image" is
    # stale. Now the guard is: if an image is returned it MUST be a Quasar Fire
    # approved_final (never a sibling flanker like Brave/Ice/Deep Blue).
    if image2:
        if '/approved_final/' not in image2:
            raise AssertionError(f'Quasar image must come from approved_final, got {image2!r}')
        if 'quasar-fire' not in image2 and 'quasar_fire' not in image2:
            raise AssertionError(f'Quasar Fire must not borrow sibling variant image, got {image2!r}')
        report2 = pmn.normalizer_report(meta2, image2, fallback)
        if 'sem banco' in report2 or 'sem imagem' in report2:
            raise AssertionError(f'approved Quasar image reported as missing:\n{report2}')
    else:
        report2 = pmn.normalizer_report(meta2, image2, fallback)
        assert_in('sem imagem', report2)

    copy3, meta3 = pmn.normalize_copy(MALBEC_RAW)
    if meta3['product'] == 'Evolui na pele igual vinho, o melhor da linha':
        raise AssertionError(f'marketing phrase used as product: {meta3}')
    # House style 2026-07-09: drop well-known national brand (Natura/Boticário)
    # from display title when the line name stands alone (Breno request,
    # matches other editors' copy).
    # Gender is marketplace SEO, not identity (Breno's title standard).
    assert_in('Malbec Signature EDP 90ml', copy3)
    if 'O Boticário Malbec Signature' in copy3:
        raise AssertionError(f'brand not stripped from display title:\n{copy3}')
    # Copy format flips between 'Por *R$ 296*' (no De from API) and
    # 'De R$ X por *R$ 296*' (API exposes original_price). Both are correct.
    if 'Por *R$ 296*' not in copy3 and 'por *R$ 296*' not in copy3:
        raise AssertionError(f'missing R$ 296 price in:\n{copy3}')
    if 'R$ 343' in copy3 or 'R$ 333' in copy3 or 'R$ 259' in copy3 or 'R$ 233' in copy3:
        raise AssertionError(f'social/profile page price leaked into validated copy:\n{copy3}')
    if meta3.get('price_checked') and not meta3.get('catalog_item_matched'):
        # Social page alone must never count as checked. But since the
        # pdp_filters featured-item resolution (2026-07-09), the promoted
        # listing CAN be matched exactly and API-validated — that is legit.
        raise AssertionError(f'social/profile page must not count as checked price: {meta3}')

    copy4, meta4 = pmn.normalize_copy(YARA_RAW)
    assert_in('*YARA PRA ELAS*', copy4)
    # Source-centric validation keeps the supplied final/Pix price, but a source
    # crossed-out De without exact-offer evidence must be omitted.
    assert_in('Por *R$ 147* no Pix', copy4)
    if 'De R$ 239' in copy4:
        raise AssertionError(f'unproved source De leaked into Yara copy:\n{copy4}')
    if meta4.get('price_checked'):
        raise AssertionError(f'social card must not claim price validation: {meta4}')

    promo_edicao_raw = '''*RAWR PERFUME DAS TIGRESAS*
_Loja Oficial O Boticário_

*Glamour Fever Desodorante Colônia 75ml*

De R$ 199 Por *R$ 125* 🦸🏻‍♂️
Cupom: *SUPERGOL ou CAMISA10MELI* 🎟️

https://meli.la/LMukVM4'''
    copy5, meta5 = pmn.normalize_copy(promo_edicao_raw)
    if meta5['product'] != 'Glamour Fever Desodorante Colônia 75ml':
        raise AssertionError(f'headline leaked as product in promo edição sample: {meta5}')
    assert_in('*Glamour Fever Desodorante Colônia 75ml*', copy5)
    if 'RAWR PERFUME DAS TIGRESAS' in copy5:
        raise AssertionError(f'raw headline leaked into final copy:\n{copy5}')
    assert_in('Cupom: *SUPERGOL* ou *CAMISA10MELI* 🎟️', copy5)

    generic_coupon_raw = '''*LATTAFA DOCE COM CARA DE RICA*

*Sakeena Lattafa Feminino EDP 100ml*

De R$ 299 por *R$ 148* no Pix 🦸🏻‍♂️
Cupom: *CUPOM* 🎟️

https://meli.la/x'''
    copy6, meta6 = pmn.normalize_copy(generic_coupon_raw)
    if meta6['product'] != 'Sakeena Lattafa Feminino EDP 100ml':
        raise AssertionError(f'headline with brand leaked as product: {meta6}')
    if 'Cupom: *CUPOM*' in copy6 or meta6.get('coupon_count'):
        raise AssertionError(f'generic CUPOM should be ignored:\n{copy6}\n{meta6}')

    hair_raw = '''*PERFUME PRO CABELO?? ME VÊ 2*
_Loja Oficial Hannity Cosméticos_

*Perfume Capilar E Body Splash Living Grace Barbour's*

Por *R$ 87* 🦸🏻‍♂️

https://meli.la/qNuIAhY'''
    copy7, meta7 = pmn.normalize_copy(hair_raw)
    if meta7['product'] != "Perfume Capilar E Body Splash Living Grace Barbour's":
        raise AssertionError(f'headline leaked as hair perfume product: {meta7}')
    if 'PERFUME PRO CABELO' in copy7:
        raise AssertionError(f'raw hair-perfume headline leaked into final copy:\n{copy7}')

    clash_image = pmn.find_image('O Boticário Clash 100ml')
    if not clash_image or '/approved_final/' not in clash_image:
        raise AssertionError(f'expected Boticario typo-alias approved_final image, got {clash_image!r}')

    azzaro_image = pmn.find_image('Azzaro Sport 100ml', '/home/ubuntu/.hermes/image_cache/img_c71d81d3b06c.jpg')
    if not azzaro_image or '/approved_final/' not in azzaro_image or 'azzaro-sport' not in azzaro_image:
        raise AssertionError(f'expected concentration-tolerant Azzaro approved_final image, got {azzaro_image!r}')

    malbec_black_image = pmn.find_image('Malbec Black 100ml', '/home/ubuntu/.hermes/image_cache/img_7749d4c4ebcd.jpg')
    # Both malbec-black-100ml and o-boticario-malbec-black-100ml are curated
    # folders for the same SKU; assert the product+curation, not the folder.
    if (not malbec_black_image or '/approved_final/' not in malbec_black_image
            or 'malbec-black-100ml' not in malbec_black_image):
        raise AssertionError(f'expected manually approved Malbec Black image, got {malbec_black_image!r}')

    # Variant safety: a missing exact image is better than a beautiful sibling.
    le_beau_image = pmn.find_image('Jean Paul Gaultier Le Beau EDT 125ml')
    if le_beau_image and 'le-male' in le_beau_image:
        raise AssertionError(f'Le Beau borrowed Le Male image: {le_beau_image}')
    acqua_image = pmn.find_image('Armani Beauty Acqua Di Giò EDT 100ml')
    if acqua_image and ('profondo' in acqua_image or 'profumo' in acqua_image):
        raise AssertionError(f'Acqua Di Gio EDT borrowed sibling image: {acqua_image}')

    # Global identity cleanup: remove proven editorial/SEO contamination while
    # preserving legitimate comma names and sibling flankers.
    identity_cases = {
        'DEFINIÇÃO DE RAIZ, Azzaro Pour Homme 50ml': 'azzaro-pour-homme-50ml',
        'Perfume Masculino Eau De Toilette Azzaro - Azzaro Pour Homme 50 Ml': 'azzaro-pour-homme-50ml',
        'Azzaro Chrome - Azzaro Chrome EDT 100 ml': 'azzaro-chrome-100ml',
        '212 VIP Black, Carolina Herrera 100ml': '212-vip-black-carolina-herrera-100ml',
        'La Vie Est Belle, Lancôme 100ml': 'la-vie-est-belle-lancome-100ml',
        'Hawas For Him Malibu Rasasi Masculino 100ml': 'hawas-for-him-malibu-rasasi-100ml',
        'Salvo Tradicional Maison Alhambra Edp 100ml Perfume Masculino': 'salvo-tradicional-maison-alhambra-100ml',
        'Lattafa Asad Bourbon 100 ml': 'lattafa-asad-bourbon-100ml',
    }
    for source_name, expected_key in identity_cases.items():
        got_key = pmn.product_key(source_name)
        if got_key != expected_key:
            raise AssertionError(f'identity regression for {source_name!r}: {got_key!r} != {expected_key!r}')
    for azzaro_title in list(identity_cases)[:2]:
        resolved = pmn.find_image(azzaro_title)
        if '/approved_final/' not in resolved or 'azzaro-pour-homme-50ml' not in resolved:
            raise AssertionError(f'Azzaro 50ml approved image not resolved from {azzaro_title!r}: {resolved}')

    polo_image = pmn.find_image('Perfume Eau de Toilette Polo Sport Ralph Lauren 100ml')
    # Folder spelling/order is not the contract: consolidation and a newer
    # Breno-approved asset may legitimately move the canonical path. Assert the
    # SKU, approval tier and variant instead of one historical directory name.
    polo_key = pmn.product_key(Path(polo_image).parent.parent.name.replace('-', ' ')) if polo_image else ''
    if (not polo_image or '/approved_final/' not in polo_image
            or not {'polo', 'sport', 'ralph'}.issubset(set(polo_key.split('-')))):
        raise AssertionError(f'Polo Sport clean approved image not resolved: {polo_image}')
    kit_image = pmn.find_image('Malbec Kit Presente O Boticário (4 Itens)')
    if 'kit-presente-malbec-4-itens/approved_final' not in kit_image:
        raise AssertionError(f'Malbec 4-item kit did not resolve to kit image: {kit_image}')
    wrong_single = pmn.find_image('Kit Malbec 2 Itens')
    if wrong_single:
        raise AssertionError(f'Unknown 2-item Malbec kit borrowed another SKU image: {wrong_single}')
    seller_line = pmn.format_official_store_trust('Ralph Lauren Fragrance')
    if seller_line != '_Loja Oficial *Ralph Lauren Fragrance*_':
        raise AssertionError(f'Official seller was shortened or long suffix kept: {seller_line}')
    verified_line = pmn.rendered_card_store_trust({
        'pdp_seller_badged': True, 'pdp_seller': 'WATHIQ PARFUMS'})
    if verified_line != '_Loja Oficial *Wathiq Parfums* no ML_':
        raise AssertionError(f'Verified seller badge did not produce Loja Oficial: {verified_line}')
    official_line = pmn.rendered_card_store_trust({
        'official_store': True, 'seller': 'WATHIQ PARFUMS'})
    if official_line != '_Loja Oficial *Wathiq Parfums* no ML_':
        raise AssertionError(f'Explicit official store lost its class: {official_line}')
    image_state = pmn.classify_image_decision(polo_image, polo_image)
    if (not image_state['approved_asset'] or not image_state['same_composition']
            or image_state['replacement_approved']):
        raise AssertionError(f'Same-composition approved asset was flattened into wrong state: {image_state}')
    different_state = pmn.classify_image_decision(polo_image, '')
    if not different_state['approved_asset'] or not different_state['replacement_approved']:
        raise AssertionError(f'Approved asset without incoming comparison was rejected: {different_state}')
    weak_malbec = Path('/home/ubuntu/.hermes/perfume_image_bank/products/malbec-tradicional/approved_final/approved_source_so_promos3_20260619-161142_4fbca5bf0deb.png')
    stronger_malbec = Path('/home/ubuntu/.hermes/perfume_image_bank/products/malbec-tradicional-100ml/approved_final/breno_final_20260704-221037_1912aeca0a2e.jpg')
    if weak_malbec.exists() and stronger_malbec.exists() and pmn.best_image_candidate([weak_malbec, stronger_malbec]) == weak_malbec:
        raise AssertionError('Human low-priority Malbec image beat a compatible preferred image')

    # Screenshot provenance and image-replacement gates (2026-07-15): a WhatsApp
    # conversation screenshot must never donate a neighboring promo's price.
    whatsapp_screen = Path('/home/ubuntu/.hermes/image_cache/img_99dfa1111108.jpg')
    if whatsapp_screen.exists() and pmn.screenshot_offer_facts(str(whatsapp_screen), 'Armaf Club De Nuit Milestone EDP 105ml'):
        raise AssertionError('WhatsApp conversation screenshot was accepted as an ML offer card')
    sentir_screen = Path('/home/ubuntu/.hermes/image_cache/img_a3fd85b162a3.jpg')
    if sentir_screen.exists():
        sentir_facts = pmn.screenshot_offer_facts(str(sentir_screen), 'Natura Essencial Sentir 100ml')
        if sentir_facts.get('final_price') != 152.0:
            raise AssertionError(f'Essencial Sentir screenshot price regression: {sentir_facts}')
    milestone_seo = 'Armaf Club De Nuit Milestone - Compartilhável EDP 105ml'
    if pmn.compact_product_name(milestone_seo) != 'Armaf Club De Nuit Milestone EDP 105ml':
        raise AssertionError(f'Milestone SEO title not cleaned: {pmn.compact_product_name(milestone_seo)}')
    sabah_source = '''*SABAH 100ML PELA METADE DO PREÇO*

*Perfume Árabe Al Wataniah Sabah 100ml Feminino*

Por *R$ 116* no Pix
https://meli.la/UlnvNPb'''
    if pmn.extract_product(sabah_source) != 'Perfume Árabe Al Wataniah Sabah 100ml Feminino':
        raise AssertionError(f'commercial headline hijacked Sabah identity: {pmn.extract_product(sabah_source)!r}')
    approved_link_name = pmn.approved_product_name_for_link(
        'https://meli.la/UlnvNPb',
        'Perfume Sedutor Árabe Sabah 100ml Original Feminino',
    )
    if approved_link_name and approved_link_name != 'Sabah Al Ward 100ml':
        raise AssertionError(f'wrong same-link canonical Sabah title: {approved_link_name!r}')
    if pmn.headline_looks_like_catalog_title('O ELEGANTE GRANDÃO 200ML', 'Ck Be Calvin Klein EDT 200ml'):
        raise AssertionError('editorial volume hook was mistaken for a catalog title')
    if not pmn.headline_looks_like_catalog_title('PERFUME MASCULINO 100ML', 'Rayhaan Pacific Aura Pour Homme EDP 100ml'):
        raise AssertionError('pure marketplace taxonomy escaped the catalog-title gate')
    generic_ctx = {'_product': 'Sabah Al Ward 100ml', 'olfactive_terms': [], '_recent_molds': {}}
    if pmn.headline_score('PERFUME FEMININO PRA HOJE', generic_ctx) >= pmn.headline_score('SEDUTOR FAVORITO DO GRUPO', generic_ctx):
        raise AssertionError('measured generic Sabah mold still outranks Breno-specific correction')
    title_identity_cases = {
        'Lattafa Asad E Selo Lataffa EDP 100ml': 'Lattafa Asad EDP 100ml',
        'E Ck Be Calvin Klein EDT 200ml': 'Ck Be Calvin Klein EDT 200ml',
        'Quasar Ice Deo Colônia 100ml': 'Quasar Ice Deo Colonia 100ml',
        'Quasar Ice Desodorante Colônia 100ml': 'Quasar Ice Desodorante Colônia 100ml',
        'O Boticário Quasar Brave Desodorante Colônia Masculino 100ml (Original)': 'Quasar Brave Desodorante Colônia 100ml',
        'Homem Sagaz Deo Parfum 100ml': 'Homem Sagaz Deo Parfum 100ml',
        'Egeo Bomb Black Desodorante 90ml': 'Egeo Bomb Black Desodorante 90ml',

        'Tommy Hilfiger Eau De Toilette 100ml': 'Tommy Hilfiger EDT 100ml',
        'Afnan Turathi Electric Eau De Parfum 90ml': 'Afnan Turathi Electric EDP 90ml',
        'Acqua Classica Eau de Cologne 100ml': 'Acqua Classica EDC 100ml',
        'Essencial Oud Extrait de Parfum 100ml': 'Essencial Oud Extrait de Parfum 100ml',
        'Kaiak Aero Colônia Masculino 100ml': 'Kaiak Aero Colônia 100ml',
        'Al Wataniah Bareeq Al Dhahab Eau De Parf 100ml': 'Al Wataniah Bareeq Al Dhahab EDP 100ml',
        'Mawwal Sama Amadeirado Cítrico EDP 100ml': 'Mawwal Sama EDP 100ml',
        'Attar Al Wesal Al Wataniah Lacrad EDP 100ml': 'Attar Al Wesal Al Wataniah EDP 100ml',
        'Masculina Eudora H Tradicional Deo Colonia 100ml': 'Eudora H Tradicional Deo Colonia 100ml',
        'Essencial Exclusivo EDP 100ml': 'Essencial Exclusivo EDP 100ml',
        'Natura Homem Coragio 100ml': 'Natura Homem Coragio 100ml',
        'Malbec Tradicional EDP 100ml': 'Malbec Tradicional EDP 100ml',
        'Lattafa Asad Elixir EDP 100ml': 'Lattafa Asad Elixir EDP 100ml',
        'French Avenue Royal Blend French Avenue Royal Blend Nero Nero Extrait De Parfum 100ml': 'French Avenue Royal Blend Nero Extrait de Parfum 100ml',
    }
    for raw_title, expected_title in title_identity_cases.items():
        actual_title = pmn.compact_product_name(raw_title)
        if actual_title != expected_title:
            raise AssertionError(f'canonical title identity regression: {raw_title!r} -> {actual_title!r}, expected {expected_title!r}')

    quasar_ctx = {'_product': 'Quasar Ice Deo Colonia 100ml', 'olfactive_terms': []}
    semantic_hook = '*QUASAR ABAIXO DE 0 ❄️*'
    generic_descriptor = '*AQUÁTICO FRESCO ESPECIADO*'
    if pmn.headline_name_hook_score(semantic_hook, quasar_ctx['_product']) < 45:
        raise AssertionError('Quasar Ice semantic hook is not strong enough to clear override margin')
    if pmn.headline_score(semantic_hook, quasar_ctx) - pmn.headline_score(generic_descriptor, quasar_ctx) < 30:
        raise AssertionError('product-specific Quasar hook still cannot override generic descriptor')
    semantic_name_pairs = [
        ('Natura Essencial Tradicional Deo Parfum 100ml',
         'TRADICIONAL COM JASMIM', 'TRADICIONAL QUE É ESSENCIAL'),
        ('Essencial Atrai Deo Parfum 100ml',
         'ESSENCIAL DOCE NACIONAL', 'ESSENCIAL DOCE QUE ATRAI'),
        ('Natura Homem Dom Deo Perfume 100ml',
         'DOÇURA NACIONAL NO DOM', 'O NACIONAL QUE TEM DOM'),
    ]
    for product, mechanical, semantic in semantic_name_pairs:
        semantic_ctx = {
            '_product': product,
            'olfactive_terms': ['jasmim', 'doce', 'baunilha'],
            'known_inspiration': '',
            '_recent_molds': {},
        }
        if pmn.headline_name_hook_score(semantic, product) < 65:
            raise AssertionError(f'natural semantic name clause lacks ranker weight: {semantic!r}')
        if pmn.headline_score(semantic, dict(semantic_ctx)) <= pmn.headline_score(mechanical, dict(semantic_ctx)):
            raise AssertionError(f'mechanical descriptor still outranks semantic name hook: {product!r}')
    weak_ctx = {'_product': 'Natura Homem Coragio 100ml', 'olfactive_terms': [], '_recent_molds': {}}
    if pmn.headline_score('*PRESENÇA NA ROTINA*', weak_ctx) >= pmn.headline_score('*A CORAGEM TEM CHEIRO*', weak_ctx):
        raise AssertionError('generic abstraction still outranks product-specific semantic hook')
    inspiration_cases = {
        'aromático amadeirado; lembra Sauvage': 'Sauvage',
        'no estilo 212 VIP Men': '212 VIP Men',
        'inspirado no Pacific Chill\npor R$ 100': 'Pacific Chill',
        'LEMBRA O LUXUOSO SAND DANCE': 'SAND DANCE',
        'LEMBRA MUITO MIND GAMES': 'MIND GAMES',
        'DUPE DO ICÔNICO IMAGINATION': 'IMAGINATION',
        'FIEL DEMAIS AO ULTRA MALE': 'ULTRA MALE',
    }
    for source_text, expected_inspiration in inspiration_cases.items():
        actual_inspiration = pmn.known_inspiration_from_text(source_text)
        if actual_inspiration != expected_inspiration:
            raise AssertionError(f'inspiration extraction regression: {source_text!r} -> {actual_inspiration!r}')
    fact_stack = pmn.ranked_editorial_facts(
        'Quasar Ice Deo Colonia 100ml',
        'aquático fresco',
        {'known_inspiration': '', 'olfactive_terms': ['fresco', 'aquático'], 'safe_angles': []},
    )
    if not any('gancho semântico' in row and 'abaixo de zero' in row for row in fact_stack):
        raise AssertionError(f'Quasar Ice semantic fact priority missing: {fact_stack}')
    milestone_variants = [
        milestone_seo,
        'Armaf Club De Nuit Milestone Unissex Spray 105ml',
        'Armaf Club De Nuit Milestone Uso Adulto Vaporizador 105ml',
        'Armaf Club De Nuit Milestone Oferta Exclusiva 105ml',
    ]
    for milestone_variant in milestone_variants:
        milestone_image = pmn.find_image(milestone_variant)
        # Folder consolidated into the canonical Milestone SKU (2026-08-02
        # migration); the invariant is the SKU, not the old folder spelling.
        if 'milestone' not in milestone_image or '/approved_final/' not in milestone_image:
            raise AssertionError(f'Milestone rich identity failed with SEO attribute {milestone_variant!r}: {milestone_image}')
    for sibling, required in [
        ('Armaf Club De Nuit Sillage EDP 105ml', 'sillage'),
        ('Armaf Club De Nuit Iconic EDP 105ml', 'iconic'),
    ]:
        sibling_image = pmn.find_image(sibling)
        if required not in sibling_image or 'milestone' in sibling_image:
            raise AssertionError(f'Milestone cleanup contaminated sibling {sibling!r}: {sibling_image}')
    if pmn.find_image('Kit Malbec 2 Itens'):
        raise AssertionError('Identity weighting borrowed a different Malbec kit cardinality')
    # A clean incoming image remains publishable even when the bank contains a
    # different approved composition; only missing media/send failure is fatal.
    sentir_incoming = Path('/home/ubuntu/.hermes/image_cache/img_e0ca50318dca.jpg')
    if sentir_incoming.exists():
        received_meta = {
            'raw_por': 'R$136', 'final_por': 'R$136', 'published_price_line': 'Por R$136',
            'price_checked': True, 'headline_source': 'llm_ranked', 'headline': '*TESTE*',
            'display_product': 'Essencial Sentir 100ml', 'product': 'Essencial Sentir 100ml',
            'browser_card': {}, 'screenshot_card': {}, 'lightweight_price': {},
            'coupon_count': 1, 'image_products': ['Essencial Sentir 100ml'],
        }
        received_report = pmn.normalizer_report(received_meta, str(sentir_incoming), str(sentir_incoming))
        if '✅ APROVADO' not in received_report:
            raise AssertionError(f'Valid received Natura image was rejected: {received_report}')

    old_approved_headline = pmn.approved_headline_for_product
    pmn.approved_headline_for_product = lambda product: ''
    headline_cases = {
        'Perfume Lattafa Asad Zanzibar Limited 100ml': '*O AZULZINHO É DIFERENTE*',
        'Vulcan Sable French Avenue EDP 100ml': '*INSPIRADO NO SAND DANCE*',
        'Malbec Ultra Bleu 100ml': '*O QUERIDINHO DOS MALBEC*',
        'Árabe Bareeq Al Dhahab EDP 100ml': '*LUXO DE DUBAI*',
        'Natura Homem EDP 100ml': '*NACIONAL COM CARA DE CARO*',
        'Lattafa Fakhar Platin EDP 100ml': '*PREÇO BAIXO NO PLATINADO*',
        'Ralph Lauren Polo Green EDT 125ml': '*RALPH LAUREN VERDIN*',
        'Malbec Black Deo Colônia 100ml': '*UM DOS MAIORES SUCESSOS*',
        'Malbec Pure Gold 100ml': '*MALBEC PURO OURO*',
        'Quasar Fire 100ml': '*PARA OS HOMENS CALIENTES*',
        'O Boticário Floratta Red Kit Presente (3 Itens)': '*BAIXOU!! KIT PARA AS DIVAS*',
        'Perfume Mercedes Benz Club Black EDT 100ml': '*CLUB BLACK DE RESPEITO*',
        'Alpine Homme Sport Maison Alhambra 100ml Edp': '*AMADEIRADO AROMÁTICO LUXUOSO*',
        'Perfume Lab 8 Niche - Strategy 100ml': '*AMBARADO LUXUOSO*',
        # 2026-07-10: 31-char approved headline now rejected by the 28-char
        # hard cap (mobile line-wrap complaint); falls back to LLM/pool.
        'Rayhaan Pacific Aura EDP 100ml': '*INSPIRADO NO PACIFIC CHILL*',
        'Lattafa Asad EDP 100ml': '*PEGADA NOTURNA ÁRABE*',
        'Lattafa The Kingdom EDP 100ml': '*INSPIRADO NO LE MALE ELIXIR*',
        'Lab 8 Effervescent 100ml': '*INSPIRADO NO SOSPIRO VIBRATO*',
        'Perfume Polo 67 Ralph Lauren EDT 125ml': '*PREÇÃO NO RALPH LAUREN*',
        'Club De Nuit Intense Da Armaf Edt 105ml': '*CONTRATIPO CREED AVENTUS*',
        'O Boticário Clash 100ml': '*FRESCOR AZUL DO BOTICÁRIO*',
    }
    for product, expected in headline_cases.items():
        got = pmn.make_headline(product, '')
        if got != expected:
            raise AssertionError(f'headline regression for {product!r}: expected {expected!r}, got {got!r}')
    pmn.approved_headline_for_product = old_approved_headline

    # The examples above are fallback/taste guardrails, not a permanent
    # product->headline table. When enabled, LLM headline must override fallback
    # and still be sanitized/wrapped for WhatsApp.
    old_llm = pmn.llm_headline
    try:
        pmn.approved_headline_for_product = lambda product: ''
        pmn.llm_headline = lambda product, raw, body='': '*CHEIRO DE RICO SEM ESFORÇO*'
        got = pmn.make_headline('Perfume Novo Aleatório EDP 100ml', 'PROMO IMPERDÍVEL')
        if got != '*CHEIRO DE RICO SEM ESFORÇO*':
            raise AssertionError(f'LLM headline path not used, got {got!r}')
        if pmn.clean_headline('R$ 199 NO PIX', ''):
            raise AssertionError('clean_headline accepted price text')
        if pmn.clean_headline('PROMO IMPERDÍVEL', 'PROMO IMPERDÍVEL'):
            raise AssertionError('clean_headline accepted copied raw headline')
        for candidate in ('*CHEIRO DE MILHÕES* ✨', '*DESIGNER COM CHEIRO DE CARO 🤌🏻*'):
            final = pmn.enforce_headline_output(candidate)
            if not re.fullmatch(r'\*[^*\n]{1,28}\*', final):
                raise AssertionError(f'final headline gate failed: {candidate!r} -> {final!r}')
    finally:
        pmn.llm_headline = old_llm
        pmn.approved_headline_for_product = old_approved_headline

    assert pmn.meaningful_de_price('R$ 279,90', 'R$ 251,91') is None
    assert pmn.meaningful_de_price('R$ 279,90', 'R$ 251,91', proved=True) is None
    assert pmn.meaningful_de_price('R$ 299', 'R$ 174', proved=True) == 'R$ 299'
    assert not pmn.image_bank_key_matches('lattafa-khamrah-100ml', 'lattafa-khamrah-qahwa-100ml')
    assert pmn._tokens_overlap('Stronger With You EDT 100ml', 'Tênis Reserva Type-R Estiloso') < 0.45
    kit_body = '''Kit Body Splash Masculino
- Infalivel 200ml
- Enigma 200ml
- Banker 200ml
- Adventus 200ml

De R$278
Por R$99 no pix'''
    kit_identity = pmn.kit_image_identity(kit_body, 'Kit Body Splash Masculino')
    assert '4 itens' in kit_identity and all(x in kit_identity for x in ('Infalivel', 'Enigma', 'Banker', 'Adventus'))
    assert pmn.image_bank_fuzzy_score(
        pmn.product_key(kit_identity),
        'kit-body-splash-masculino-barbarius-enigma-midtown-200ml',
    ) == 0

    # Breno's house style: store names in aesthetic capitalization, never ALL CAPS.
    assert pmn.rendered_card_store_trust({'seller': 'OUD', 'official_store': True}) == '_Loja Oficial *Oud* no ML_'
    assert pmn.rendered_card_store_trust({'seller': 'WATHIQ PARFUMS', 'official_store': True}) == '_Loja Oficial *Wathiq Parfums* no ML_'
    assert pmn.rendered_card_store_trust({'seller': 'SELLER SEM SELO', 'official_store': False}) is None
    assert pmn.rendered_card_store_trust({'official_store': True}) is None

    # Editorial calibration invariants: authority is a relation, not a product
    # table. Exact linked identity wins over human history; same-link human
    # history wins over SKU history; lexical cleanup is the last resort.
    old_link_title = pmn.approved_product_name_for_link
    old_sku_title = pmn.approved_product_name_for_product
    try:
        pmn.approved_product_name_for_link = lambda link, product: 'Le Beau Le Parfum 125ml'
        pmn.approved_product_name_for_product = lambda product: 'Le Beau Intense 125ml'
        exact_title, exact_source = pmn.editorial_title_authority(
            'https://meli.la/exact', 'Jean Paul Gaultier Le Beau 125ml',
            exact_title='Jean Paul Gaultier Le Beau Le Parfum EDP 125ml',
            exact_offer=True,
        )
        if exact_source != 'exact_item' or 'Le Parfum' not in exact_title or 'EDP' not in exact_title:
            raise AssertionError(f'exact item did not win title authority: {exact_title!r}, {exact_source!r}')
        human_title, human_source = pmn.editorial_title_authority(
            'https://meli.la/exact', 'Jean Paul Gaultier Le Beau 125ml',
        )
        if human_source != 'human_link' or 'Le Parfum' not in human_title:
            raise AssertionError(f'same-link human title did not beat SKU history: {human_title!r}, {human_source!r}')
    finally:
        pmn.approved_product_name_for_link = old_link_title
        pmn.approved_product_name_for_product = old_sku_title

    if pmn.compact_product_name('O Boticário Malbec Signature EDP 90ml', preserve_house=True) != 'O Boticário Malbec Signature EDP 90ml':
        raise AssertionError('authoritative house name was stripped from canonical title')
    if pmn.compact_product_name('O Boticário Malbec Signature EDP 90ml') != 'Malbec Signature EDP 90ml':
        raise AssertionError('legacy redundant-house cleanup changed unexpectedly')

    facts = pmn.ranked_editorial_facts(
        'Lab 8 Strategy 100ml',
        'É o melhor inspirado no Queening Mind Games',
        {'known_inspiration': '', 'olfactive_terms': ['âmbar', 'amadeirado'],
         'safe_angles': ['pra rotina'], 'proof_points': ['aprovado no grupo']},
    )
    if not facts[0].startswith('PRIORIDADE 1') or not any('PRIORIDADE 5' in row for row in facts):
        raise AssertionError(f'editorial fact priority stack is not ordered: {facts}')
    if not any('PROIBIDO/SEM EVIDÊNCIA' in row for row in facts):
        raise AssertionError('editorial fact stack lacks explicit unsupported-claim rail')

    score_ctx = {'_product': 'Natura Homem Coragio 100ml', 'olfactive_terms': [],
                 'known_inspiration': '', '_recent_molds': {}}
    if pmn.headline_score('PRESENÇA NA ROTINA', score_ctx) >= pmn.headline_score('A CORAGEM TEM CHEIRO', score_ctx):
        raise AssertionError('generic abstraction outranks a name-anchored semantic hook')
    for weak_headline in ('ARMAF ICONIC EM DESTAQUE', 'BLACK PRA REFORÇAR', 'PERFUME PRA SE ARRUMAR'):
        if pmn.headline_score(weak_headline, score_ctx) >= pmn.headline_score('A CORAGEM TEM CHEIRO', score_ctx):
            raise AssertionError(f'measured generic tail still outranks a specific hook: {weak_headline!r}')
    if pmn.headline_score('FIEL AO SAUVAGE', score_ctx) >= 60:
        raise AssertionError('unsupported inspiration survived the deterministic veto floor')
    price_ctx = {
        '_product': 'Bottled Infinite Hugo Boss EDP 100ml',
        'olfactive_terms': [], 'known_inspiration': '', '_recent_molds': {},
        '_price_attractive': True,
    }
    for mechanical in ('41% MENOS NO BOTTLED', '42% MENOS NO SAFEER', '43% DE DESCONTO'):
        if pmn.headline_score(mechanical, dict(price_ctx)) >= 60:
            raise AssertionError(f'mechanical percentage headline survived veto: {mechanical!r}')
    for milestone in ('50% OFF NO BOTTLED', '75% OFF NO BOTTLED'):
        if pmn.headline_score(milestone, dict(price_ctx)) < 60:
            raise AssertionError(f'psychological discount milestone was vetoed: {milestone!r}')
    tuberose_ctx = {
        '_product': 'Lattafa Fakhar Gold EDP 100ml',
        'olfactive_terms': ['tuberosa'], 'known_inspiration': '',
        '_recent_molds': {}, '_price_attractive': True,
    }
    if pmn.headline_score('TUBEROSA INTENSA A R$169', tuberose_ctx) >= 60:
        raise AssertionError('raw note + intensity + price mold survived veto')
    gentleman_ctx = {
        '_product': 'Givenchy Gentleman Boisée EDP 100ml',
        'olfactive_terms': [], 'known_inspiration': '', '_recent_molds': {},
    }
    dressed = pmn.headline_score('UM GENTLEMAN BEM VESTIDO', dict(gentleman_ctx))
    fragrant = pmn.headline_score('UM GENTLEMAN BEM CHEIROSO', dict(gentleman_ctx))
    if dressed >= 60 or fragrant <= dressed:
        raise AssertionError(f'perfume-category payoff regression: vestido={dressed}, cheiroso={fragrant}')

    print('OK perfume_manual_normalizer regression')
    print('Asad copy:', copy.replace('\n', ' | '))
    print('Asad image:', image)
    print('Quasar image:', image2)
    print('Clash image:', clash_image)
    print('Azzaro image:', azzaro_image)
    print('Malbec Black image:', malbec_black_image)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
