#!/usr/bin/env python3
"""Labeled benchmark of known legacy failures.

Expected values come from the received promotion plus the exact-item evidence
observed during incident investigation. This is deliberately hermetic because
meli.la short links and buy-boxes mutate over time.
"""
from decimal import Decimal as D
from perfume_offer_engine import Evidence as E, IdentityStatus, SourceOffer, reconcile_offer


def evidence(item, title, public=None, pix=None, original=None, store=None, sibling=None):
    rows=[E('product_name',title,'exact_item_card',item_id=item,confidence='exact',card_role='featured')]
    if public is not None: rows.append(E('price',D(str(public)),'item_api',item_id=item,confidence='exact',price_kind='regular'))
    if pix is not None: rows.append(E('pix_price',D(str(pix)),'exact_item_card',item_id=item,confidence='exact',price_kind='pix'))
    if original is not None: rows.append(E('original_price',D(str(original)),'item_api',item_id=item,confidence='exact'))
    if store: rows.append(E('official_store',store,'official_store_id',item_id=item,confidence='exact'))
    if sibling:
        rows += [E(k,v,'sibling_card',item_id=sibling['item'],confidence='exact',**kw) for k,v,kw in sibling['fields']]
    return rows


CASES=[
 dict(name='Jean Lowe cupom', source=SourceOffer('Jean Lowe Azure','x',D('161'),coupons=('MLMELHOROFERTA',)), ev=evidence('J1','Jean Lowe Azure',public='189.91'), expected={'price':D('161'),'price_status':'campaign_claim'}, legacy={'price':D('189.91')}),
 dict(name='Portinari cupom', source=SourceOffer('Portinari Absoluto','x',D('84'),D('219'),says_pix=True,coupons=('MLMELHOROFERTA',)), ev=evidence('P1','Portinari Absoluto',public='144.90'), expected={'price':D('84'),'kind':'pix'}, legacy={'price':D('144.90')}),
 dict(name='Malbec kit cupom', source=SourceOffer('Malbec Kit Body Sprays','x',D('67'),coupons=('MLMELHOROFERTA',)), ev=evidence('M1','Malbec Kit Body Sprays',public='79.90'), expected={'price':D('67')}, legacy={'price':D('79.90')}),
 dict(name='Asad Elixir cupom', source=SourceOffer('Asad Elixir','x',D('196'),coupons=('MLMELHOROFERTA',)), ev=evidence('A1','Asad Elixir',public='289'), expected={'price':D('196')}, legacy={'price':D('289')}),
 dict(name='Asad versus Eclaire', source=SourceOffer('Lattafa Asad 100ml','x',D('169'),coupons=('MLMELHOROFERTA',)), ev=evidence('E1','Lattafa Eclaire 100ml',public='199.92',pix='199.92',store='Lipx'), expected={'identity':IdentityStatus.CONFLICT,'store':None}, legacy={'product':'Eclaire','store':'Lipx'}),
 dict(name='Sabah nome de catalogo', source=SourceOffer('Al Wataniah Sabah 100ml','x',D('88'),says_pix=True,coupons=('FULL',)), ev=evidence('S1','Perfume Sedutor Arabe Sabah 100ml',public='129.99',pix='103.99'), expected={'identity':IdentityStatus.EXACT_ITEM,'price':D('88'),'kind':'pix'}, legacy={'blocked_as':'Sabah Al Ward'}),
 dict(name='Mawwal parcela vizinha', source=SourceOffer('Mawwal Malek','x',D('195')), ev=evidence('MM1','Mawwal Malek',public='195',sibling={'item':'MF2','fields':[('installments','6x',{'no_interest':True,'price_base':D('195')})]}), expected={'installments':None}, legacy={'installments':'6x'}),
 dict(name='Loja de item vizinho', source=SourceOffer('Azzaro Most Wanted','x',D('237')), ev=evidence('AZ1','Azzaro Most Wanted',public='237',sibling={'item':'AZ2','fields':[('official_store','Selvagem Essence',{})]}), expected={'store':None}, legacy={'store':'Selvagem Essence'}),
 dict(name='Liquid Brun loja e Pix', source=SourceOffer('Liquid Brun','x',D('274'),D('469'),coupons=('FULLANITTACOCATEG',)), ev=evidence('LB1','Liquid Brun',public='374.90',pix='299.92',original='469',store='Lipx'), expected={'price':D('274'),'original':D('469'),'kind':'pix','store':'Lipx'}, legacy={'price':D('274'),'original':D('469'),'kind':'unknown','store':'Lipx'}),
 dict(name='Fakhar Gold loja e Pix', source=SourceOffer('Lattafa Fakhar Gold','x',D('113'),D('294'),coupons=('MLMELHOROFERTA',)), ev=evidence('FG1','Lattafa Fakhar Gold',public='176',pix='140.80',original='294.90',store='Lipx'), expected={'price':D('113'),'original':D('294.90'),'kind':'pix','store':'Lipx'}, legacy={'price':D('113'),'original':D('294'),'kind':'unknown','store':None}),
 dict(name='Malbec Magnetic De e Pix', source=SourceOffer('Malbec Magnetic 100ml','x',D('179'),D('269'),coupons=('MLMELHOROFERTA',)), ev=evidence('MB1','Malbec Magnetic 100ml',public='258',pix='211.56',original='258'), expected={'price':D('179'),'original':D('258'),'kind':'pix'}, legacy={'price':D('179'),'original':D('269'),'kind':'unknown'}),
 dict(name='Khamrah seller selecionado diverge da buy-box', source=SourceOffer('Lattafa Khamrah 100ml','x',D('151'),D('299'),coupons=('MLMELHOROFERTA',),claimed_store='Lsouzaimport'), ev=evidence('KH1','Lattafa Khamrah 100ml',public='179.90',original='327.09',store='WATHIQ PARFUMS'), expected={'price':D('151'),'original':D('327.09'),'store':None}, legacy={'price':D('151'),'original':D('299'),'store':'WATHIQ PARFUMS'}),
 dict(name='Salvo sem De no item', source=SourceOffer('Salvo Intense 100ml','x',D('136'),D('299'),coupons=('MLMELHOROFERTA',)), ev=evidence('SV1','Salvo Intense 100ml',public='159.90'), expected={'price':D('136'),'original':None}, legacy={'price':D('136'),'original':D('299')}),
 dict(name='Essencial Sentir sem De no item', source=SourceOffer('Natura Essencial Sentir 100ml','x',D('130'),D('289'),coupons=('MLMELHOROFERTA',)), ev=evidence('ES1','Natura Essencial Sentir 100ml',public='152'), expected={'price':D('130'),'original':None}, legacy={'price':D('130'),'original':D('289')}),
 dict(name='Club 6 seller verificado', source=SourceOffer('Eudora Club 6 Exclusive 95ml','x',D('85'),D('199'),coupons=('MLMELHOROFERTA',)), ev=evidence('C61','Eudora Club 6 Exclusive 95ml',public='101.13',original='299',store='Selvagem Essence'), expected={'price':D('85'),'original':D('299'),'store':'Selvagem Essence'}, legacy={'price':D('85'),'original':D('199'),'store':None}),
 dict(name='Salvo seller verificado', source=SourceOffer('Salvo Intense 100ml','x',D('136'),D('299'),coupons=('MLMELHOROFERTA',)), ev=evidence('SV2','Salvo Intense 100ml',public='159.90',store="KID'S LIFE"), expected={'price':D('136'),'original':None,'store':"KID'S LIFE"}, legacy={'price':D('136'),'original':D('299'),'store':None}),
 dict(name='Vulcan Feu cupom sobre Pix', source=SourceOffer('Vulcan Feu 100ml','x',D('260'),D('379'),coupons=('MLMELHOROFERTA',)), ev=evidence('VF1','Vulcan Feu 100ml',public='384.90',pix='307.92',original='599.80',store='Lipx'), expected={'price':D('260'),'original':D('599.80'),'kind':'pix','store':'Lipx'}, legacy={'price':D('260'),'original':D('379'),'kind':'unknown'}),
 dict(name='Malbec regular rotulado Pix', source=SourceOffer('Malbec Magnetic 100ml','x',D('258')), ev=evidence('MB2','Malbec Magnetic 100ml',public='258',pix='211.56',original='258'), expected={'price':D('211.56'),'kind':'pix'}, legacy={'price':D('258'),'kind':'pix'}),
 dict(name='Vulcan regular rotulado Pix', source=SourceOffer('Vulcan Feu 100ml','x',D('384.90'),D('599.80')), ev=evidence('VF2','Vulcan Feu 100ml',public='384.90',pix='307.92',original='599.80',store='Lipx'), expected={'price':D('307.92'),'original':D('599.80'),'kind':'pix','store':'Lipx'}, legacy={'price':D('384.90'),'original':D('599.80'),'kind':'pix'}),
]


def check(case,r):
    e=case['expected']; tests=[]
    if 'price' in e: tests.append(r.price==e['price'])
    if 'price_status' in e: tests.append(r.decisions['price'].status==e['price_status'])
    if 'original' in e: tests.append(r.original_price==e['original'])
    if 'kind' in e: tests.append(r.price_kind==e['kind'])
    if 'identity' in e: tests.append(r.identity.status==e['identity'])
    if 'store' in e: tests.append(r.official_store==e['store'])
    if 'installments' in e: tests.append(r.installments==e['installments'])
    return all(tests)


def main():
    new_ok=old_ok=0; field_new={}; field_old={}; field_total={}
    for c in CASES:
        r=reconcile_offer(c['source'],c['ev']); ok=check(c,r);new_ok+=ok
        resolved={'price':r.price,'original':r.original_price,'kind':r.price_kind,
                  'store':r.official_store,'installments':r.installments,'identity':r.identity.status}
        old_checks=[]
        for field,expected in c['expected'].items():
            if field=='price_status':
                continue
            field_total[field]=field_total.get(field,0)+1
            new_match=resolved.get(field)==expected
            old_match=c['legacy'].get(field)==expected
            field_new[field]=field_new.get(field,0)+int(new_match)
            field_old[field]=field_old.get(field,0)+int(old_match)
            old_checks.append(old_match)
        old=bool(old_checks) and all(old_checks);old_ok+=old
        print(('PASS' if ok else 'FAIL'),c['name'],'new=',{'identity':r.identity.status.value,'price':r.price,'original':r.original_price,'kind':r.price_kind,'store':r.official_store,'installments':r.installments,'price_status':r.decisions['price'].status},'legacy_observed=',c['legacy'])
    print(f'NEW {new_ok}/{len(CASES)} | LEGACY {old_ok}/{len(CASES)} on known-failure benchmark')
    for field in sorted(field_total):
        print(f'FIELD {field}: NEW {field_new.get(field,0)}/{field_total[field]} | LEGACY {field_old.get(field,0)}/{field_total[field]}')
    if new_ok != len(CASES): raise SystemExit(1)

if __name__=='__main__':main()
