"""Alertas das melhores promos: texto curto, sem repetir a mesma oferta, para várias saídas.

Saídas prontas: console e arquivo JSONL. Telegram/WhatsApp entram depois como mais uma
saída (uma função que recebe o texto), sem mexer no resto.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from . import grupo as vitrine
from .banco import registrar_alerta

NOTAS_ALERTA = {"IMPERDÍVEL", "BOA"}
NAO_REPETIR_HORAS = 12
EMOJI = {"IMPERDÍVEL": "🔥🔥", "BOA": "✅"}


def chave_alerta(av) -> str:
    """Produto + preço exato; a regra de "não repetir" está em Alertador._repetido."""
    return f"{av.identidade.chave}|{av.preco_efetivo:.2f}"


def formatar_alerta(av, origem: str = "") -> str:
    p, h = av.promo, av.historico
    # Cabeçalho próprio: no grupo da coletora, a equipe vê na hora que o aviso é da curadora.
    linhas = [f"🧠 CURADORA · {EMOJI.get(av.nota, '')} {av.nota} {av.score}/100", f"*{p.produto[:80]}*"]
    preco = f"R${av.preco_efetivo:.2f}".replace(".", ",")
    if av.pct_abaixo is not None and h.mediana:
        linhas.append(f"💰 {preco} · {av.pct_abaixo:.0f}% abaixo do normal (R${h.mediana:.0f})")
    else:
        linhas.append(f"💰 {preco}")
    destaque = [r for r in av.razoes if r.startswith(("NOVO RECORDE", "menor preço", "TOP vendido", "loja TOP",
                                                      "loja oficial confirmada", "selo MAIS VENDIDO"))]
    linhas += [f"• {r}" for r in destaque[:4]]
    if p.cupons:
        linhas.append("🎟️ cupom: " + " ou ".join(p.cupons[:2]))
    serios = [a for a in av.alertas if a.startswith(("preço SUBIU", "LINK", "grupo diz"))]
    linhas += [f"⚠️ {a}" for a in serios[:2]]
    if p.link:
        linhas.append(f"🔗 {p.link}")
    fonte = f"{origem}" + (f" · {av.grupo}" if getattr(av, "grupo", "") else "")
    if fonte:
        linhas.append(f"({fonte})")
    return "\n".join(linhas)


def saida_console(texto: str, dados: dict) -> None:
    print(texto, flush=True)
    print("-" * 40, flush=True)


class SaidaArquivo:
    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, texto: str, dados: dict) -> None:
        with self.caminho.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(dados, texto=texto), ensure_ascii=False) + "\n")


class SaidaWhatsApp:
    """Manda o alerta para UM grupo de WhatsApp pela ponte da VPS (a mesma que a coletora usa).

    Só aceita o grupo configurado em config.json -> whatsapp_alertas.grupo; sem grupo, fica desligada.
    Só manda as notas escolhidas (padrão: só IMPERDÍVEL), para o grupo não virar spam.
    """

    def __init__(self, grupo: str, notas: list[str] | None = None, ponte: str = "http://127.0.0.1:3000", http=None):
        grupo = str(grupo or "").strip()
        if grupo and "@" not in grupo:
            grupo += "@g.us"
        self.grupo = grupo
        self.notas = set(notas or ["IMPERDÍVEL"])
        self.ponte = ponte.rstrip("/")
        self.http = http or self._post
        self.enviados = 0

    @classmethod
    def do_config(cls, cfg: dict, http=None) -> "SaidaWhatsApp | None":
        w = cfg.get("whatsapp_alertas") or {}
        return cls(w["grupo"], w.get("notas"), w.get("ponte", "http://127.0.0.1:3000"), http) if w.get("grupo") else None

    @staticmethod
    def _post(url: str, payload: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            corpo = resp.read().decode("utf-8", "replace")
        return json.loads(corpo) if corpo else {"success": True}

    def _destino_permitido(self) -> bool:
        return (self.grupo.endswith("@g.us")
                and self.grupo.split("@")[0] not in vitrine.PROIBIDOS)

    def enviar_texto(self, texto: str) -> None:
        """Mensagem que não é alerta de promo (ex.: relatório semanal): vai para o mesmo grupo, sem filtro de nota."""
        if not self._destino_permitido():
            return
        resposta = self.http(f"{self.ponte}/send", {"chatId": self.grupo, "message": texto})
        if not resposta.get("success", True):
            raise RuntimeError(f"a ponte do WhatsApp recusou: {resposta}")
        self.enviados += 1

    def __call__(self, texto: str, dados: dict) -> None:
        if not self._destino_permitido() or (dados.get("nota") not in self.notas and not dados.get("forcar")):
            return
        if not dados.get("forcar") and vitrine.ativo():
            return   # 13/10: com o pipeline rodando quem fala no grupo é ele (todas as aprovadas, não só Excelentes)
        if dados.get("promo") and dados.get("avaliacao"):
            # 13/10 (Breno): "a promo separada e a avaliação embaixo, para ter a promo limpa"
            vitrine.enviar(self.grupo, dados["promo"], dados["avaliacao"], http=self.http, ponte=self.ponte,
                           pausa_s=1.0 if self.http == SaidaWhatsApp._post else 0)
            self.enviados += 1
            return
        resposta = self.http(f"{self.ponte}/send", {"chatId": self.grupo, "message": texto})
        if not resposta.get("success", True):
            raise RuntimeError(f"a ponte do WhatsApp recusou: {resposta}")
        self.enviados += 1


class SaidaWhatsAppDoConfig:
    """Lê o grupo do config.json a cada alerta: configurar ou desligar o grupo vale sem reiniciar."""

    def __init__(self, http=None):
        self.http = http

    def __call__(self, texto: str, dados: dict) -> None:
        from . import carregar_config
        saida = SaidaWhatsApp.do_config(carregar_config(), self.http)
        if saida:
            saida(texto, dados)


class Alertador:
    def __init__(self, con, saidas: list, notas: set[str] | None = None, nao_repetir_horas: float = NAO_REPETIR_HORAS):
        self.con = con
        self.saidas = saidas
        self.notas = notas or NOTAS_ALERTA
        self.janela = int(nao_repetir_horas * 3600)

    def _repetido(self, av, agora: int) -> bool:
        """Mesmo produto já alertado na janela por um preço igual ou até 3% acima: não repete.

        Só alerta de novo quando o preço cai de verdade (R$151,90 na loja A e R$152,90 na loja B
        é a mesma notícia).
        """
        prefixo = av.identidade.chave + "|"
        for r in self.con.execute("SELECT chave_alerta FROM alertas WHERE substr(chave_alerta, 1, ?) = ? AND ts >= ?",
                                  (len(prefixo), prefixo, agora - self.janela)):
            try:
                preco_anterior = float(r["chave_alerta"][len(prefixo):])
            except ValueError:
                continue
            if av.preco_efetivo >= preco_anterior * 0.97:
                return True
        return False

    def _alvos(self, av, origem: str, agora: int) -> None:
        """Alerta por produto: avisa quem cadastrou o alvo, qualquer que seja a nota."""
        from .vigiados import alvos_atingidos, formatar, marcar_avisado
        for v in alvos_atingidos(self.con, av, agora):
            texto = formatar(av, v)
            dados = {"ts": agora, "nota": av.nota, "score": av.score, "chave": av.identidade.chave,
                     "preco": av.preco_efetivo, "link": av.promo.link, "origem": origem, "alvo": v["preco_alvo"],
                     "forcar": True}
            marcar_avisado(self.con, v, agora, av.preco_efetivo)
            for saida in self.saidas:
                try:
                    saida(texto, dados)
                except Exception as exc:
                    print(f"[alvo] saída falhou: {type(exc).__name__}: {exc}", flush=True)

    def talvez_alertar(self, av, origem: str, agora: int | None = None, atraso_s: float | None = None) -> bool:
        if av is None:
            return False
        agora = agora or int(time.time())
        try:
            self._alvos(av, origem, agora)
        except Exception as exc:  # problema no alerta por produto não pode travar o alerta normal
            print(f"[alvo] erro: {type(exc).__name__}: {exc}", flush=True)
        if av.nota not in self.notas:
            return False
        chave = chave_alerta(av)
        if self._repetido(av, agora):
            return False
        texto = formatar_alerta(av, origem)
        dados = {"ts": agora, "nota": av.nota, "score": av.score, "chave": av.identidade.chave,
                 "preco": av.preco_efetivo, "link": av.promo.link, "origem": origem, "grupo": getattr(av, "grupo", ""),
                 "atraso_s": atraso_s}
        try:   # o modelo do grupo: promo limpa + avaliação curta (o texto antigo continua indo para o arquivo)
            dados.update(promo=vitrine.promo_montada(av),
                         avaliacao=vitrine.avaliacao(av, getattr(av, "grupo", "") or origem))
        except Exception as exc:  # noqa: BLE001
            print(f"[alerta] modelo do grupo falhou: {type(exc).__name__}: {exc}", flush=True)
        registrar_alerta(self.con, chave, agora, av.nota, av.score, origem, texto)
        for saida in self.saidas:
            try:
                saida(texto, dados)
            except Exception as exc:  # uma saída com problema não derruba as outras
                print(f"[alerta] saída falhou: {type(exc).__name__}: {exc}", flush=True)
        return True
