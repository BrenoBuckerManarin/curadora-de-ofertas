"""Início de cupom percebido pelos grupos, em sombra (04/11, ajuste 6 da aprovação da fase 1).

Hoje a rodada de cupom começa pela equipe: dois posts no GrupoFinal com o cupom, ou o aviso dela (esgotados.quentes). Os
grupos-fonte costumam avisar antes. Caso de 04/11: o DESCONTOS estava parado desde 03/10; às 09:04 o FONTE GAMA #45
avisou "DESCONTOS 10% OFF acima de R$79, limitado a R$100" e às 09:07 o Fonte Zeta | 133 postou um perfume com ele.
A equipe postou com ele às 09:11 e ele ficou "quente" na curadora às 09:16 (2º post).

Regra (só registra; não muda gatilho, rodada nem envio):
- o cupom está parado: nenhum post de perfume com ele nos grupos há 6 h (o ATIVO_S de cupons.py) ou nunca visto;
- ele volta com um post de perfume num grupo-fonte e, em até 15 min, um aviso com a regra (de outro grupo ou do
  mesmo) ou um post de perfume com ele em outro grupo. Um grupo sozinho com um post só não basta.
Cupom de outra seção do ML (cupons.fora_de_perfume) não conta como aviso.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from .cupons import ATIVO_S, cupons_da_mensagem, fora_de_perfume
from .parser import ler_promo, parece_perfume

PARADO_S = ATIVO_S        # sem post de perfume com o cupom há 6 h: a próxima aparição pode ser um início
JANELA_S = 15 * 60        # os dois sinais precisam estar a até 15 min um do outro


@dataclass
class Sinal:
    ts: float
    grupo: str
    nome: str
    tipo: str             # "aviso" (regra do cupom, sem produto) ou "perfume" (post de perfume com o cupom)
    trecho: str
    regra: str = ""


@dataclass
class Inicio:
    codigo: str
    ts: float                                   # quando a curadora perceberia
    parado_desde: float | None                  # o último post de perfume com ele antes (None = nunca visto)
    sinais: list[Sinal] = field(default_factory=list)
    regra: str = ""

    def frase(self) -> str:
        h = lambda ts: time.strftime("%d/%m %H:%M:%S", time.localtime(ts))   # noqa: E731
        antes = f"parado desde {h(self.parado_desde)}" if self.parado_desde else "nunca visto antes com perfume"
        sinais = "; ".join(f"{s.tipo} no {s.nome} às {h(s.ts)}" for s in self.sinais)
        regra = f", regra {self.regra}" if self.regra else ""
        return f"{self.codigo}: início percebido às {h(self.ts)} ({antes}{regra}); {sinais}"


def _regra_em_texto(regra) -> str:
    partes = []
    if regra.pct:
        partes.append(f"{regra.pct:g}%")
    if regra.valor:
        partes.append(f"R${regra.valor:g}")
    if regra.minimo:
        partes.append(f"mín. R${regra.minimo:g}")
    if regra.limite:
        partes.append(f"até R${regra.limite:g}")
    return " ".join(partes)


class Detector:
    """Recebe as mensagens dos grupos-fonte em ordem (momento.mensagens_do_tap) e devolve os inícios percebidos."""

    def __init__(self, marcas: list[str], parado_s: float = PARADO_S, janela_s: float = JANELA_S,
                 ultimo_perfume: dict[str, float] | None = None):
        self.marcas = marcas
        self.parado_s, self.janela_s = parado_s, janela_s
        self.ultimo_perfume: dict[str, float] = dict(ultimo_perfume or {})
        self.pendentes: dict[str, list[Sinal]] = {}
        self.inicios: list[Inicio] = []

    def ver(self, m: dict) -> list[Inicio]:
        if m.get("grupofinal"):
            return []   # o GrupoFinal é a equipe; aqui a pergunta é se os grupos avisam antes dela
        texto = m.get("texto") or ""
        cupons = cupons_da_mensagem(texto)
        if not cupons:
            return []
        promo = ler_promo(texto, self.marcas)
        e_perfume = promo.valida and parece_perfume(promo.produto, texto)
        novos = []
        for codigo, regra in cupons.items():
            ts = float(m["ts"])
            ultimo = self.ultimo_perfume.get(codigo)
            if ultimo is not None and ts - ultimo <= self.parado_s:
                if e_perfume:
                    self.ultimo_perfume[codigo] = ts   # já estava ativo: só renova
                continue
            if e_perfume:
                tipo = "perfume"
            elif (regra.pct or regra.valor) and not fora_de_perfume(texto, codigo):
                tipo = "aviso"
            else:
                continue
            sinais = [s for s in self.pendentes.get(codigo, []) if ts - s.ts <= self.janela_s]
            sinais.append(Sinal(ts, m.get("grupo", ""), m.get("nome", ""), tipo, " ".join(texto.split())[:120],
                                _regra_em_texto(regra)))
            self.pendentes[codigo] = sinais
            perfumes = [s for s in sinais if s.tipo == "perfume"]
            avisos = [s for s in sinais if s.tipo == "aviso"]
            if perfumes and (avisos or len({s.grupo for s in perfumes}) >= 2):
                inicio = Inicio(codigo, ts, ultimo, list(sinais),
                                next((s.regra for s in reversed(sinais) if s.regra), ""))
                self.inicios.append(inicio)
                novos.append(inicio)
                self.ultimo_perfume[codigo] = ts
                self.pendentes.pop(codigo, None)
        return novos

    def ver_todas(self, mensagens: Iterable[dict]) -> list[Inicio]:
        for m in mensagens:
            self.ver(m)
        return self.inicios
