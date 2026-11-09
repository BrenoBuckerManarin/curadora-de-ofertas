"""API oficial do Mercado Livre, com acesso PRÓPRIO da curadora (separado da coletora antiga).

Sem login a API recusa tudo (testado em 03/10: 401/403). Com um token de app ML, os endpoints
de catálogo funcionam (eram usados pela normalizadora antiga):
  /products/search?site_id=MLB&q=...    acha o produto de catálogo
  /products/{id}                        nome oficial do produto
  /products/{id}/items                  TODAS as ofertas: preço, preço anterior, loja oficial, frete
  /users/{seller_id}                    nome do vendedor

Credenciais (fora do código e do git), em ~/.curadora/:
  ml-app.env     ML_CLIENT_ID=...  ML_CLIENT_SECRET=...  ML_REDIRECT_URI=...
  ml-token.json  gerado por `python configurar_ml.py`; renova sozinho (validade de 6h)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PASTA_PADRAO = Path(os.path.expanduser("~/.curadora"))
API = "https://api.mercadolibre.com"
INTERVALO_MIN = 0.4  # a API aguenta bem mais que a página; ainda assim, sem rajadas
VALIDADE_EMPRESTADO = 5.75 * 3600  # o token do ML vale 6h; margem para não usar um que está vencendo
RENOVAR_COMPARTILHADO = 5.25 * 3600  # a coletora renova com 5h; a curadora só entra se ela não renovou


@dataclass
class OfertaML:
    item_id: str
    preco: float
    preco_anterior: float | None
    seller_id: int | None
    loja_oficial: bool
    frete_gratis: bool
    vendedor: str = ""
    internacional: bool = False   # compra internacional (CBT): vendedor de fora do Brasil


NICK_GERADO = re.compile(r"^[A-Z]{2}(20\d{2})(\d{2})(\d{2})\d{6}$")  # ex.: MB20250628193724 (conta sem nome de loja)


def reputacao(nome: str, nivel: str | None, medalha: str | None, vendas: int | None) -> dict:
    """Resume o vendedor: "otima" (medalha gold/platinum), "boa", "fraca" (vermelho/laranja ou poucas vendas)
    ou "desconhecida". O nick gerado automaticamente (sigla + data) indica conta sem nome de loja."""
    numero = int(str(nivel or "0")[0]) if str(nivel or "")[:1].isdigit() else 0
    gerado = NICK_GERADO.match(nome or "")
    if medalha in ("platinum", "gold"):
        classe = "otima"
    elif numero in (1, 2) or (vendas is not None and vendas < 50):
        classe = "fraca"
    elif numero >= 4:
        classe = "boa"
    else:
        classe = "desconhecida"
    return {"nome": nome, "nivel": nivel or "", "medalha": medalha or "", "vendas": vendas, "classe": classe,
            "conta_sem_nome": bool(gerado), "conta_desde": f"{gerado.group(2)}/{gerado.group(1)}" if gerado else ""}


class SemCredencial(Exception):
    """A curadora ainda não tem acesso próprio à API do ML (falta rodar configurar_ml.py)."""


def _ler_env(caminho: Path) -> dict:
    dados: dict = {}
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            if "=" in linha and not linha.strip().startswith("#"):
                k, v = linha.split("=", 1)
                dados[k.strip()] = v.strip()
    return dados


def _http_padrao(metodo: str, url: str, cabecalhos: dict, corpo: bytes | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=corpo, headers=cabecalhos, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "ignore") or "{}")
        except ValueError:
            return exc.code, {}


class ClienteML:
    _trava = threading.Lock()
    _trava_renovacao = threading.Lock()
    _ultimo = 0.0

    def __init__(self, pasta: Path | None = None, http=None, relogio=time.time, intervalo: float = INTERVALO_MIN,
                 emprestado: Path | None = None, app_compartilhado: Path | None = None):
        self.pasta = Path(pasta or PASTA_PADRAO)
        self.http = http or _http_padrao
        self.relogio = relogio
        self.intervalo = intervalo
        # Acesso EMPRESTADO da coletora (autorizado pelo Breno em 03/10): por padrão só LÊ o arquivo dela.
        emprestado = emprestado or os.environ.get("CURADORA_ML_TOKEN_EMPRESTADO")
        self.emprestado = Path(os.path.expanduser(str(emprestado))) if emprestado else None
        # Renovação COMPARTILHADA (autorizada pelo Breno em 04/10, quando a coletora parou de renovar):
        # com o app da coletora, a curadora também renova e grava no MESMO arquivo, no mesmo formato.
        # A coletora relê o arquivo a cada uso, então pega o acesso novo sem perceber.
        app_compartilhado = app_compartilhado or os.environ.get("CURADORA_ML_APP_COMPARTILHADO")
        self.app_compartilhado = Path(os.path.expanduser(str(app_compartilhado))) if app_compartilhado else None
        self._nomes: dict[int, str] = {}

    # ── credenciais ─────────────────────────────────────────────
    @property
    def arquivo_token(self) -> Path:
        return self.pasta / "ml-token.json"

    def app(self) -> dict:
        return _ler_env(self.pasta / "ml-app.env")

    def configurado(self) -> bool:
        if self.emprestado:
            return self.emprestado.exists()
        app = self.app()
        return bool(app.get("ML_CLIENT_ID") and app.get("ML_CLIENT_SECRET") and self.arquivo_token.exists())

    def _token_emprestado(self) -> str:
        tok = json.loads(self.emprestado.read_text(encoding="utf-8"))
        idade = self.relogio() - float(tok.get("fetched_at") or tok.get("obtido_em") or 0)
        if tok.get("access_token") and idade < VALIDADE_EMPRESTADO:
            return str(tok["access_token"])
        raise SemCredencial("acesso emprestado da coletora vencido: volta quando a coletora renovar "
                            "(ela renova nas janelas em que roda)")

    def _ler_compartilhado(self) -> tuple[dict, float]:
        tok = json.loads(self.emprestado.read_text(encoding="utf-8"))
        return tok, self.relogio() - float(tok.get("fetched_at") or 0)

    def _token_compartilhado(self, forcar: bool) -> str:
        """Usa o acesso da coletora e renova sozinho quando ela não renovou.

        - Renova só depois de RENOVAR_COMPARTILHADO (5h15): se a coletora estiver ligada, ela renova antes (5h).
        - Relê o arquivo dentro da trava: se alguém acabou de renovar, usa o novo em vez de renovar de novo
          (cada renovação do ML só vale uma vez).
        - Grava de forma atômica (arquivo temporário + troca), permissão 600, no formato da coletora (fetched_at).
        """
        with ClienteML._trava_renovacao:
            tok, idade = self._ler_compartilhado()
            recente = idade < 10 * 60  # renovado há pouco (por nós ou pela coletora): não renova de novo
            if tok.get("access_token") and (recente or (not forcar and idade < RENOVAR_COMPARTILHADO)):
                return str(tok["access_token"])
            app = _ler_env(self.app_compartilhado)
            if not (app.get("ML_CLIENT_ID") and app.get("ML_CLIENT_SECRET") and tok.get("refresh_token")):
                raise SemCredencial("faltam os dados do app da coletora para renovar o acesso")
            status, novo = self._oauth({"grant_type": "refresh_token", "client_id": app["ML_CLIENT_ID"],
                                        "client_secret": app["ML_CLIENT_SECRET"],
                                        "refresh_token": tok["refresh_token"]})
            if status == 200 and novo.get("access_token"):
                novo["fetched_at"] = self.relogio()
                self._gravar_compartilhado(novo)
                print(f"[ml] acesso compartilhado renovado pela curadora (o anterior tinha {idade / 3600:.1f}h)",
                      flush=True)
                return str(novo["access_token"])
            # Recusado: talvez a coletora tenha renovado no mesmo instante. Relê antes de desistir.
            tok2, idade2 = self._ler_compartilhado()
            if tok2.get("access_token") and tok2.get("access_token") != tok.get("access_token") \
                    and idade2 < RENOVAR_COMPARTILHADO:
                return str(tok2["access_token"])
            if tok.get("access_token") and idade < 6 * 3600 and not forcar:
                return str(tok["access_token"])
            raise SemCredencial(f"o ML recusou a renovação do acesso compartilhado ({status}: "
                                f"{novo.get('error') or novo.get('message') or ''})")

    def _gravar_compartilhado(self, tok: dict) -> None:
        temp = self.emprestado.with_name(f".{self.emprestado.name}.curadora-{os.getpid()}.tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tok, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.emprestado)
        except OSError:
            # Não pode perder o refresh novo (o antigo já não vale): guarda uma cópia de resgate.
            resgate = self.pasta / "ml-token-resgate.json"
            self.pasta.mkdir(parents=True, exist_ok=True)
            resgate.write_text(json.dumps(tok), encoding="utf-8")
            os.chmod(resgate, 0o600)
            raise

    def _salvar_token(self, tok: dict) -> None:
        tok["obtido_em"] = self.relogio()
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arquivo_token.write_text(json.dumps(tok), encoding="utf-8")
        try:
            os.chmod(self.arquivo_token, 0o600)
        except OSError:
            pass

    def url_autorizacao(self) -> str:
        app = self.app()
        return ("https://auth.mercadolivre.com.br/authorization?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": app.get("ML_CLIENT_ID", ""),
             "redirect_uri": app.get("ML_REDIRECT_URI", "")}))

    def trocar_codigo(self, codigo: str) -> None:
        """Primeira autorização: troca o código que o ML devolveu por um token (com renovação)."""
        app = self.app()
        status, tok = self._oauth({"grant_type": "authorization_code", "client_id": app.get("ML_CLIENT_ID", ""),
                                   "client_secret": app.get("ML_CLIENT_SECRET", ""), "code": codigo.strip(),
                                   "redirect_uri": app.get("ML_REDIRECT_URI", "")})
        if status != 200 or not tok.get("access_token"):
            raise RuntimeError(f"o ML recusou o código ({status}): {tok.get('message') or tok.get('error') or tok}")
        self._salvar_token(tok)

    def _oauth(self, campos: dict) -> tuple[int, dict]:
        corpo = urllib.parse.urlencode(campos).encode()
        return self.http("POST", f"{API}/oauth/token",
                         {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, corpo)

    def token(self, forcar_renovacao: bool = False) -> str:
        if not self.configurado():
            raise SemCredencial("rode `python configurar_ml.py` para dar à curadora acesso próprio à API do ML")
        if self.emprestado:
            if self.app_compartilhado:
                return self._token_compartilhado(forcar_renovacao)
            if forcar_renovacao:  # o ML recusou o acesso emprestado: não renova (é da coletora), só espera
                raise SemCredencial("o ML recusou o acesso emprestado; volta quando a coletora renovar")
            return self._token_emprestado()
        tok = json.loads(self.arquivo_token.read_text(encoding="utf-8"))
        idade = self.relogio() - float(tok.get("obtido_em") or 0)
        if not forcar_renovacao and tok.get("access_token") and idade < 5 * 3600:
            return str(tok["access_token"])
        app = self.app()
        status, novo = self._oauth({"grant_type": "refresh_token", "client_id": app["ML_CLIENT_ID"],
                                    "client_secret": app["ML_CLIENT_SECRET"],
                                    "refresh_token": tok.get("refresh_token", "")})
        if status == 200 and novo.get("access_token"):
            self._salvar_token(novo)
            return str(novo["access_token"])
        if tok.get("access_token") and idade < 6 * 3600:
            return str(tok["access_token"])
        raise SemCredencial(f"não consegui renovar o acesso ao ML ({status}); rode configurar_ml.py de novo")

    # ── chamadas ────────────────────────────────────────────────
    def get(self, caminho: str) -> dict:
        for tentativa in (0, 1):
            with ClienteML._trava:
                espera = self.intervalo - (time.monotonic() - ClienteML._ultimo)
                if espera > 0:
                    time.sleep(espera)
                ClienteML._ultimo = time.monotonic()
            status, dados = self.http("GET", f"{API}{caminho}", {"Authorization": f"Bearer {self.token(tentativa == 1)}",
                                                                 "Accept": "application/json"})
            if status == 401 and tentativa == 0:
                continue  # token venceu antes da hora: renova e tenta de novo
            if status != 200:
                raise RuntimeError(f"ML {status} em {caminho}: {dados.get('message') or dados.get('code') or ''}")
            return dados
        return {}

    def buscar_produtos(self, termo: str, limite: int = 10) -> list[dict]:
        q = urllib.parse.urlencode({"site_id": "MLB", "status": "active", "q": termo, "limit": limite})
        return [{"id": r.get("id", ""), "nome": r.get("name", "")} for r in self.get(f"/products/search?{q}").get("results", [])]

    def ofertas(self, produto_id: str, limite: int = 50) -> list[OfertaML]:
        resultados = self.get(f"/products/{produto_id}/items?limit={limite}").get("results", [])
        ofertas = []
        for r in resultados:
            if not isinstance(r.get("price"), (int, float)):
                continue
            ofertas.append(OfertaML(
                item_id=str(r.get("item_id") or ""), preco=float(r["price"]),
                preco_anterior=r.get("original_price") if isinstance(r.get("original_price"), (int, float)) else None,
                seller_id=r.get("seller_id"), loja_oficial=bool(r.get("official_store_id")),
                frete_gratis=bool((r.get("shipping") or {}).get("free_shipping")),
                # 09/10 (Breno: "o Fierce era internacional, não poderia ser postado"): o ML marca com "cbt_item".
                internacional=("cbt_item" in (r.get("tags") or [])
                               or "cbt_fulfillment" in ((r.get("shipping") or {}).get("tags") or []))))
        return sorted(ofertas, key=lambda o: o.preco)

    def vendedor(self, seller_id: int | None) -> dict:
        """Nome e reputação do vendedor (guardado em memória: muda pouco ao longo do dia)."""
        if not seller_id:
            return {}
        if seller_id not in self._nomes:
            try:
                u = self.get(f"/users/{seller_id}")
                rep = u.get("seller_reputation") or {}
                self._nomes[seller_id] = reputacao(str(u.get("nickname") or ""), rep.get("level_id"),
                                                   rep.get("power_seller_status"),
                                                   (rep.get("transactions") or {}).get("total"))
                self._nomes[seller_id]["pais"] = str(u.get("country_id") or "")
            except Exception:
                self._nomes[seller_id] = {}
        return self._nomes[seller_id]

    def vendedor_guardado(self, seller_id: int | None) -> dict:
        """31/10: a reputação só se já estiver na memória (não chama a API). Para anotar quem vende as outras ofertas
        do catálogo sem uma consulta a mais por vendedor."""
        return dict(self._nomes.get(seller_id) or {}) if seller_id else {}

    def nome_vendedor(self, seller_id: int | None) -> str:
        return self.vendedor(seller_id).get("nome", "")
