"""Calibração pelo olho do Breno: compara a nota da curadora com o que ele aprovou.

Sinais usados (gravados pelo painel), só os votos explícitos:
  👍 "eu postaria"   -> boa
  👎 "não postaria"  -> ruim
Copiar/encaminhar NÃO conta (o Breno copiava só para testar o botão, 03/10). Cada oferta conta
uma vez: a vigia reavalia a mesma oferta a cada rodada e o painel junta essas repetições num card.

Testa combinações de pesos (relevância, loja, sinais do ML) e do limite da nota BOA,
recalculando o score de cada promo votada com os mesmos tetos da avaliação, e mostra a
que mais concorda com ele. Não muda nada sozinha: `aplicar()` só roda quando pedido.
"""
from __future__ import annotations

import itertools
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .score import LIMITES_NOTA, teto_da_nota

FATORES = (0.5, 0.75, 1.0, 1.25, 1.5)
LIMITES_BOA = (50, 55, 60, 65, 70)
MINIMO_VOTOS = 20


@dataclass
class Exemplo:
    produto: str
    componentes: dict
    flags: dict
    boa: bool
    nota_atual: str


@dataclass
class Resultado:
    exemplos: int
    positivos: int
    concordancia_atual: float
    concordancia_melhor: float
    fatores: dict
    limite_boa: int
    discordancias: list = field(default_factory=list)


def carregar_exemplos(con) -> list[Exemplo]:
    """Uma entrada por oferta votada (mesmo produto e preço = mesma oferta), com o voto mais recente."""
    por_oferta: dict[tuple, Exemplo] = {}
    for r in con.execute("SELECT nota, voto, chave, dados FROM avaliacoes WHERE voto != 0 ORDER BY id"):
        dados = json.loads(r["dados"])
        flags = dados.get("flags") or {"pct_abaixo": dados.get("pct_abaixo")}
        oferta = (r["chave"], round(float(dados.get("preco") or 0)))
        por_oferta[oferta] = Exemplo(dados.get("produto", ""), dados.get("componentes") or {}, flags,
                                     r["voto"] == 1, r["nota"])
    return list(por_oferta.values())


def _score(ex: Exemplo, pesos: dict, fr: float, fl: float, fs: float) -> float:
    c = ex.componentes
    total = (c.get("preco", 0) + fr * c.get("relevancia", 0) + fl * c.get("loja", 0) + fs * c.get("sinais_ml", 0)
             + c.get("desconto", 0) + c.get("conflito", 0))
    return min(total, teto_da_nota(pesos, ex.flags)[0])


def _concordancia(exemplos: list[Exemplo], acertos: list[bool]) -> float:
    """Acerto equilibrado: média do acerto nas boas e nas ruins (não premia dizer sempre 'ruim')."""
    boas = [a for e, a in zip(exemplos, acertos) if e.boa]
    ruins = [a for e, a in zip(exemplos, acertos) if not e.boa]
    partes = [sum(x) / len(x) for x in (boas, ruins) if x]
    return sum(partes) / len(partes) if partes else 0.0


def calibrar(exemplos: list[Exemplo], cfg: dict) -> Resultado | None:
    if len(exemplos) < MINIMO_VOTOS:
        return None
    pesos = cfg["pesos"]
    limite_atual = (cfg.get("limites_nota") or LIMITES_NOTA)["BOA"]

    def acertos(fr, fl, fs, limite):
        return [(_score(e, pesos, fr, fl, fs) >= limite) == e.boa for e in exemplos]

    atual = _concordancia(exemplos, acertos(1.0, 1.0, 1.0, limite_atual))
    melhor = (atual, 1.0, 1.0, 1.0, limite_atual)
    for fr, fl, fs, limite in itertools.product(FATORES, FATORES, FATORES, LIMITES_BOA):
        c = _concordancia(exemplos, acertos(fr, fl, fs, limite))
        # Só troca se for melhor de verdade; no empate, fica o mais perto do atual.
        distancia = abs(fr - 1) + abs(fl - 1) + abs(fs - 1) + abs(limite - limite_atual) / 10
        distancia_melhor = abs(melhor[1] - 1) + abs(melhor[2] - 1) + abs(melhor[3] - 1) + abs(melhor[4] - limite_atual) / 10
        if c > melhor[0] + 1e-9 or (abs(c - melhor[0]) < 1e-9 and distancia < distancia_melhor):
            melhor = (c, fr, fl, fs, limite)
    discord = [(e.produto, e.nota_atual, "👍" if e.boa else "👎")
               for e, ok in zip(exemplos, acertos(1.0, 1.0, 1.0, limite_atual)) if not ok]
    return Resultado(len(exemplos), sum(e.boa for e in exemplos), atual, melhor[0],
                     {"relevancia": melhor[1], "loja": melhor[2], "sinais_ml": melhor[3]}, melhor[4], discord[:15])


def para_painel(r: Resultado | None, exemplos: list[Exemplo]) -> dict:
    base = {"exemplos": len(exemplos), "minimo": MINIMO_VOTOS, "positivos": sum(e.boa for e in exemplos)}
    if r is None:
        return dict(base, pronto=False)
    return dict(base, pronto=True, concordancia_atual=round(r.concordancia_atual, 3),
                concordancia_melhor=round(r.concordancia_melhor, 3), fatores=r.fatores, limite_boa=r.limite_boa,
                vale_aplicar=r.concordancia_melhor > r.concordancia_atual + 0.02,
                discordancias=[{"produto": p, "nota": n, "voto": v} for p, n, v in r.discordancias])


def gravar(cfg: dict, r: Resultado) -> str:
    """Aplica o ajuste no config.json, guardando uma cópia do anterior. Devolve o nome do backup."""
    from . import ARQUIVO_CONFIG, salvar_config
    origem = Path(cfg.get("_arquivo") or ARQUIVO_CONFIG)
    backup = origem.with_name(f"config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy(origem, backup)
    salvar_config(aplicar(cfg, r), origem)
    return backup.name


def aplicar(cfg: dict, r: Resultado) -> dict:
    """Devolve uma cópia do config com os pesos e o limite da nota ajustados."""
    novo = json.loads(json.dumps(cfg))
    p = novo["pesos"]
    for chave in ("top_vendido", "favorito_publico"):
        p[chave] = round(p[chave] * r.fatores["relevancia"], 1)
    for chave in ("loja_top", "loja_oficial_confiavel", "loja_oficial_alegada", "loja_verificada_alegada"):
        p[chave] = round(p[chave] * r.fatores["loja"], 1)
    for chave in ("mais_vendido", "full"):
        p[chave] = round(p[chave] * r.fatores["sinais_ml"], 1)
    limites = dict(novo.get("limites_nota") or LIMITES_NOTA)
    deslocamento = r.limite_boa - limites["BOA"]
    novo["limites_nota"] = {n: v + deslocamento for n, v in limites.items()}
    return novo
