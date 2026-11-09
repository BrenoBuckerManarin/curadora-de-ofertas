"""Utilidades compartilhadas pelos testes."""
from __future__ import annotations

import atexit
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RAIZ))

from curadora import banco as _banco  # noqa: E402
from curadora import carregar_config  # noqa: E402

# Os testes abrem muitos bancos em memória; guardamos todos e fechamos no fim,
# para não encher a saída de avisos "unclosed database".
_ABERTAS: list = []
_conectar_original = _banco.conectar


def _conectar_e_guardar(*args, **kwargs):
    con = _conectar_original(*args, **kwargs)
    _ABERTAS.append(con)
    return con


_banco.conectar = _conectar_e_guardar
atexit.register(lambda: [c.close() for c in _ABERTAS])

from curadora.banco import conectar, inserir_oferta  # noqa: E402
from curadora.identidade import Catalogo, Identificador  # noqa: E402
from curadora.parser import ler_promo  # noqa: E402

CFG = carregar_config()
MARCAS = CFG["marcas"]
DIA = 86400
T0 = 1_790_000_000


def html_com_card(*fixtures: str) -> str:
    """Monta uma página mínima com blocos salvos de uma página real do ML (destaque, recomendações)."""
    blocos = [json.dumps(json.loads((FIXTURES / f).read_text(encoding="utf-8")), ensure_ascii=False,
                         separators=(",", ":")) for f in fixtures]  # compacto, como vem na página real
    return f"<html><script>window.__PRELOADED__={{\"components\":[{','.join(blocos)}]}}</script></html>"


def banco_com_historico(ofertas: list[tuple[str, float, int]]):
    """Banco em memória com ofertas (produto, preço, dia relativo a T0) e catálogo aprendido."""
    from curadora.banco import aprender_catalogo
    con = conectar(":memory:")
    ident = Identificador(MARCAS, CFG["sinonimos"])
    for n, (produto, preco, dia) in enumerate(ofertas):
        promo = ler_promo(f"*{produto}*\nPor R$ {preco}\nhttps://meli.la/x{n}", MARCAS)
        inserir_oferta(con, f"m{n}", T0 + dia * DIA, "grupo", promo, Catalogo().chave(ident.analisar(produto)))
    con.commit()
    aprender_catalogo(con, ident)
    return con


class MLFalso:
    """Consultor do ML que devolve cards prontos, sem rede."""

    def __init__(self, cards: dict):
        self.cards = cards
        self.consultas: list[str] = []

    def consultar(self, link: str, agora=None):
        from curadora.ml import CardML
        self.consultas.append(link)
        return self.cards.get(link) or CardML(ok=False, erro="sem card")


# O painel olha o registro do pipeline (dados/pipeline.jsonl) na regra das 8h; nos testes, um que não existe
# (na VPS os testes rodam ao lado do registro de verdade).
import painel as _painel  # noqa: E402
_painel.REGISTRO_PIPELINE = Path(__file__).parent / "registro-que-nao-existe.jsonl"

# 30/10: a ficha do cupom (modo sombra) grava em dados/ficha_cupom_sombra.jsonl; nos testes, num arquivo temporário.
import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from curadora import ficha_cupom as _ficha  # noqa: E402
_ficha.REGISTRO_SOMBRA = Path(_tempfile.gettempdir()) / f"ficha_cupom_sombra_testes_{_os.getpid()}.jsonl"
atexit.register(lambda: _ficha.REGISTRO_SOMBRA.unlink(missing_ok=True))
