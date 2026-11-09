"""Confere a oferta no Mercado Livre pelo próprio link da promo.

O link de afiliado (meli.la) abre a página social do afiliado. O produto promovido
é o "card em destaque" (id "card-featured"), que vem como JSON dentro do HTML com:
título oficial, preço atual (Pix), preço em outros meios, preço anterior, vendedor
com o selo "Loja oficial", envio FULL, selo MAIS VENDIDO e o id de catálogo do produto.

Mesma ideia da normalizadora (que lia o selo no HTML do card em destaque), mas lendo
o JSON estruturado: mais simples e menos frágil.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TTL_SEGUNDOS = 30 * 60          # preço e vendedor mudam rápido; 30 min é o máximo seguro
INTERVALO_MIN = 1.5             # entre requisições ao ML

ESQUEMA_CACHE = """
CREATE TABLE IF NOT EXISTS ml_cards (
    link TEXT PRIMARY KEY,
    ts   INTEGER NOT NULL,
    dados TEXT NOT NULL
);
"""


@dataclass
class CardML:
    ok: bool
    erro: str = ""
    item_id: str = ""
    produto_id: str = ""          # id de catálogo: a identidade do produto no ML
    titulo: str = ""
    preco: float | None = None    # preço atual em destaque (geralmente Pix)
    preco_outros: float | None = None
    preco_anterior: float | None = None
    preco_cupom: float | None = None  # "R$ X com Cupom" que o próprio ML mostra no card (cupom do anúncio)
    desconto: str = ""
    vendedor: str = ""
    loja_oficial: bool = False
    full: bool = False
    mais_vendido: bool = False
    url_produto: str = ""
    afiliado: str = ""
    foto: str = ""
    seller_id: int | None = None
    reputacao: dict | None = None   # vem da API (radar ou consulta pelo produto de catálogo)
    catalogo: dict | None = None    # os outros vendedores do mesmo produto no ML (vendedores, 2º preço, oficial)
    internacional: bool = False     # compra internacional (vendedor de fora do Brasil): o grupo não posta


def _icones(componente: dict) -> list[str]:
    valores = componente.get("values") or []
    return [str((v.get("icon") or {}).get("alt_text") or "") for v in valores if v.get("type") == "icon"]


def _bloco(html: str, bloco_id: str) -> dict | None:
    achado = re.search(r'\{\s*"id"\s*:\s*"' + re.escape(bloco_id) + '"', html)
    if not achado:
        return None
    try:
        return json.JSONDecoder().raw_decode(html, achado.start())[0]["recommendation_data"]["recommendation_info"]
    except (ValueError, KeyError, TypeError):
        return {}


def ler_card(html: str) -> CardML:
    """Extrai o card em destaque (o produto do link) do HTML da página social do ML."""
    info = _bloco(html, "card-featured")
    if info is None:
        return CardML(ok=False, erro="card em destaque não encontrado")
    try:
        card = info["polycards"][0]
    except (KeyError, IndexError, TypeError) as exc:
        return CardML(ok=False, erro=f"card ilegível: {type(exc).__name__}")
    return _ler_polycard(card, str(info.get("affiliateName") or ""))


def ler_recomendacoes(html: str) -> list[CardML]:
    """Os ~20 produtos parecidos que a página do afiliado mostra, com preço e loja atuais."""
    info = _bloco(html, "affiliate-profile-recommendations") or {}
    cards = [_ler_polycard(c) for c in info.get("polycards") or [] if isinstance(c, dict)]
    return [c for c in cards if c.ok]


def _ler_polycard(card: dict, afiliado: str = "") -> CardML:
    meta = card.get("metadata") or {}
    out = CardML(ok=True, item_id=str(meta.get("id") or ""), produto_id=str(meta.get("product_id") or ""),
                 afiliado=afiliado)
    if meta.get("url"):
        out.url_produto = "https://" + str(meta["url"]).split("?")[0]
    fotos = (card.get("pictures") or {}).get("pictures") or []
    if fotos and isinstance(fotos[0], dict) and re.fullmatch(r"[\w-]+", str(fotos[0].get("id") or "")):
        out.foto = f"https://http2.mlstatic.com/D_Q_NP_{fotos[0]['id']}-O.webp"
    for comp in card.get("components") or []:
        tipo = comp.get("type")
        dados = comp.get(tipo) or {}
        if tipo == "title":
            out.titulo = str(dados.get("text") or "").strip()
        elif tipo == "highlight":
            out.mais_vendido = "mais vendido" in str(dados.get("text") or "").lower()
        elif tipo == "seller":
            texto = re.sub(r"\{[^}]+\}", "", str(dados.get("text") or "")).strip()
            out.vendedor = re.sub(r"(?i)^por\s+", "", texto).strip()
            out.loja_oficial = any("loja oficial" in a.lower() or "tienda oficial" in a.lower()
                                   for a in _icones(dados))
        elif tipo == "price":
            out.preco = (dados.get("current_price") or {}).get("value")
            out.preco_anterior = (dados.get("previous_price") or {}).get("value")
            out.desconto = str((dados.get("discount_label") or {}).get("text") or "")
            for v in (dados.get("installments") or {}).get("values") or []:
                if v.get("type") == "price":
                    out.preco_outros = (v.get("price") or {}).get("value")
        elif tipo == "promotions":
            # 10/10: [{"type": "coupon", "text": "{1} com Cupom", "values": [{"type": "price", "price": {...}}]}]
            for promocao in comp.get("promotions") or []:
                if isinstance(promocao, dict) and promocao.get("type") == "coupon":
                    for v in promocao.get("values") or []:
                        valor = (v.get("price") or {}).get("value") if v.get("type") == "price" else None
                        if isinstance(valor, (int, float)):
                            out.preco_cupom = float(valor)
        elif tipo == "shipping":
            out.full = any("full" in a.lower() for a in _icones(dados)) or \
                "full" in str((dados.get("icon") or {}).get("alt_text") or "").lower()
    if not out.titulo or out.preco is None:
        out.ok = False
        out.erro = "card sem título ou preço"
    return out


class ConsultorML:
    """Busca e guarda em cache o card de cada link. `buscar_html` é injetável para testes."""

    def __init__(self, con: sqlite3.Connection, buscar_html=None, ttl: int = TTL_SEGUNDOS):
        self.con = con
        self.con.executescript(ESQUEMA_CACHE)
        self.buscar_html = buscar_html or self._baixar
        self.ttl = ttl
        # Chamado com (card, fonte, ts) para cada preço visto; a Curadora usa para alimentar o histórico.
        self.observador = None

    _trava = threading.Lock()      # limite de velocidade vale para o processo inteiro
    _ultimo_global = 0.0

    def _baixar(self, link: str) -> str:
        with ConsultorML._trava:
            espera = INTERVALO_MIN - (time.monotonic() - ConsultorML._ultimo_global)
            if espera > 0:
                time.sleep(espera)
            ConsultorML._ultimo_global = time.monotonic()
        req = urllib.request.Request(link, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read(2_000_000).decode("utf-8", "ignore")

    def consultar(self, link: str, agora: int | None = None) -> CardML:
        if not link or not re.search(r"meli\.la/|mercadolivre\.com", link):
            return CardML(ok=False, erro="link não é do Mercado Livre")
        agora = agora or int(time.time())
        linha = self.con.execute("SELECT ts, dados FROM ml_cards WHERE link=?", (link,)).fetchone()
        if linha and agora - linha[0] < self.ttl:
            return CardML(**json.loads(linha[1]))
        recomendacoes: list[CardML] = []
        try:
            html = self.buscar_html(link)
            card = ler_card(html)
            recomendacoes = ler_recomendacoes(html)
            if "account-verification" in html[:3000] and not card.ok:
                card.erro = "o ML pediu verificação de conta"
        except Exception as exc:  # rede, timeout, bloqueio: a avaliação segue sem o ML
            card = CardML(ok=False, erro=f"falha ao abrir o link: {type(exc).__name__}")
        if self.observador:
            if card.ok:
                self.observador(card, "destaque", agora)
            for rec in recomendacoes:
                self.observador(rec, "recomendacao", agora)
        self.con.execute("INSERT OR REPLACE INTO ml_cards (link, ts, dados) VALUES (?,?,?)",
                         (link, agora, json.dumps(asdict(card), ensure_ascii=False)))
        self.con.commit()
        return card
