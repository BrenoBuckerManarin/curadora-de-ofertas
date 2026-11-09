#!/usr/bin/env python3
import importlib.util
from pathlib import Path

path=Path.home()/'.hermes/scripts/perfume_editorial_pair_eval.py'
spec=importlib.util.spec_from_file_location('pair_eval',path)
assert spec and spec.loader
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

cases={
    ('ATTARZÃO POR R$126','ATTARZÃO POR R$113 🚨'):'price_fact_corrected',
    ('CADA SPLASH SAI POR 35','CADA SPLASH SAI POR 35'):'unit_economics_approved',
    ('ÁRABE 100ML POR R$118','UM OUD ÁRABE AUTÊNTICO 🤌🏻'):'price_angle_replaced',
    ('KHAMRAH, DOCE E ÂMBAR','KHAMRAH, DOCE E ÂMBAR'):'no_price_angle',
}
for pair,expected in cases.items():
    actual=mod.classify_price_angle(*pair).get('class')
    if actual != expected:
        raise AssertionError(f'{pair}: {actual} != {expected}')

title_cases={
    ('Khamrah Lattafa','Khamrah Lattafa EDP 100 ML'):'source_missing_volume_added_by_editor',
    ('Produto EDP 100ml','Produto 100ml'):'editor_removed_type',
}
for pair,expected in title_cases.items():
    actual=mod.classify_title(*pair).get('class')
    if actual != expected:
        raise AssertionError(f'{pair}: {actual} != {expected}')

headline_cases={
    ('FRESCOR ELEGANTE DE DIA','PRECINHO QUE COMPENSA 👌🏻'):'strategy_changed',
    ('FRESCOR HERBAL TODO DIA','FRESCOR HERBAL TODO DIA 🍃'):'emoji_or_punctuation_only',
    ('VOYAGE COM MAÇÃ VERDE','MENOS DE 100 NO VOYAGE 🤌🏻'):'strategy_price_added',
}
for pair,expected in headline_cases.items():
    actual=mod.classify_headline(*pair,'Produto Voyage',{}).get('class')
    if actual != expected:
        raise AssertionError(f'{pair}: {actual} != {expected}')
print('OK perfume editorial pair eval regression')