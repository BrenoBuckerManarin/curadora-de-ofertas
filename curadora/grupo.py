"""A curadora no grupo de WhatsApp da equipe, no lugar da coletora antiga.

13/10/2026 (Breno): "hoje tem a curadora e a coletora; qual traz promos melhores? A curadora. Seriam essas promos
curadas que iriam para o grupo da coletora: pense uma curadora, mas versão WhatsApp". E o formato: "nesse modelo
[o da coletora]: a promo separada e a avaliação embaixo, separada, para ter a promo limpa; quero que o aviso
seja mais palatável, direto".

Cada promo vira DUAS mensagens no grupo dos avisos (config whatsapp_alertas.grupo):
  1. a promo limpa, pronta para encaminhar ao Gerador (com a foto do post de origem, quando há);
  2. a avaliação curta da curadora: nota, preço conferido no ML, por que vale e de onde veio.

E o caminho de volta ("ter a função avaliar promos quando eu enviar uma promo no grupo"): alguém da equipe manda
uma promo no grupo e a curadora responde, citando a mensagem, com a avaliação. É o "Avaliar uma promo" do painel.

Aqui ficam só o texto e as regras; quem escolhe e manda é o pipeline (mesmo horário dele) e, fora desse horário,
o alerta das Excelentes (curadora/alertas.py).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

from . import RAIZ, carregar_config

REGISTRO = RAIZ / "dados" / "grupo.jsonl"
ESTADO_PIPELINE = RAIZ / "dados" / "pipeline.estado.json"
DESLIGADO = RAIZ / "dados" / "grupo.desligado"   # `pipelinectl grupo off` cria; `pipelinectl grupo on` apaga
PONTE = "http://127.0.0.1:3000"
INTERVALO_S = 90          # no máximo uma promo a cada 1,5 min no grupo (a coletora chegava a 4 por minuto)
MAXIMO_POR_HORA = 60      # trava de segurança: nunca mais que isso, aconteça o que acontecer
PEDIDO_MAX_S = 5 * 60     # promo mandada no grupo há mais que isso (ex.: o serviço esteve parado) fica sem resposta
NOMES = {"IMPERDÍVEL": "Excelente", "BOA": "Boa", "OK": "Média", "NÃO VALE": "Ruim", "FORA": "Fora (não é perfume)"}
SINAL = {"IMPERDÍVEL": "🔥", "BOA": "🟢", "OK": "🟡", "NÃO VALE": "🔴", "FORA": "⚪"}
# A curadora só fala no grupo dos avisos. Estes nunca: GrupoFinal (público), Gerador, Normalizador e Edição.
PROIBIDOS = ("120363000000000002", "120363000000000013", "120363000000000012", "120363000000000001")


def reais(valor: float) -> str:
    """R$ 93 · R$ 93,41 · R$ 1.128,80."""
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + (texto[:-3] if texto.endswith(",00") else texto)


def configuracao(cfg: dict | None = None) -> dict:
    """{"ligado", "grupo", "intervalo_s", "foto"}; o grupo é o dos avisos. Lido a cada uso: desligar no config.json
    vale na hora, sem reiniciar nada."""
    cfg = cfg if cfg is not None else carregar_config()
    g = cfg.get("grupo_curadora") or {}
    grupo = str((cfg.get("whatsapp_alertas") or {}).get("grupo") or "").strip()
    if grupo and "@" not in grupo:
        grupo += "@g.us"
    if not grupo.endswith("@g.us") or grupo.split("@")[0] in PROIBIDOS:
        grupo = ""
    return {"ligado": bool(g.get("ligado")) and bool(grupo) and not DESLIGADO.exists(), "grupo": grupo,
            "intervalo_s": max(30, float(g.get("intervalo_s") or INTERVALO_S)), "foto": g.get("foto", True) is not False,
            "avaliar": g.get("avaliar", True) is not False}


def pipeline_rodando(agora: float | None = None) -> bool:
    """O pipeline está ligado e já dentro do horário dele (o arquivo de estado existe e o processo está vivo)."""
    try:
        estado = json.loads(ESTADO_PIPELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    pid = estado.get("pid")
    if not pid or not Path(f"/proc/{pid}").exists():
        return False
    de = str(estado.get("de") or "")
    if re.fullmatch(r"\d{1,2}:\d{2}", de):
        hora, minuto = (int(x) for x in de.split(":"))
        agora_ = time.localtime(agora or time.time())
        if (agora_.tm_hour, agora_.tm_min) < (hora, minuto):
            return False
    return True


def ativo(cfg: dict | None = None) -> bool:
    """A curadora está falando no grupo agora (ligada no config e com o pipeline rodando)?"""
    return configuracao(cfg)["ligado"] and pipeline_rodando()


# ── o texto ────────────────────────────────────────────────────

_MOTIVOS = (
    (re.compile(r"^NOVO RECORDE"), lambda m, r: "recorde de preço"),
    (re.compile(r"^menor preço em (\d+) dias"), lambda m, r: f"menor preço em {m.group(1)} dias"),
    (re.compile(r"^TOP vendido"), lambda m, r: "top vendido"),
    (re.compile(r"^queridinho do público"), lambda m, r: "queridinho do público"),
    (re.compile(r"^loja TOP: (.+?) \(confirmado"), lambda m, r: f"loja top ({m.group(1)})"),
    (re.compile(r"^loja oficial confirmada"), lambda m, r: "loja oficial"),
    (re.compile(r"^selo MAIS VENDIDO"), lambda m, r: "mais vendido no ML"),
    (re.compile(r"^kit: o público gosta"), lambda m, r: "kit (o grupo curte)"),
    (re.compile(r"^enviado pelo FULL"), lambda m, r: "Full"),
)
_SERIOS = ("preço SUBIU", "grupo diz", "o ML destaca outro vendedor")


def motivos(av, quantos: int = 3) -> list[str]:
    """Por que vale, em poucas palavras e na ordem do que mais pesa."""
    achados = []
    for padrao, curto in _MOTIVOS:
        for razao in getattr(av, "razoes", None) or []:
            m = padrao.search(razao)
            if m:
                texto = curto(m, razao)
                if texto not in achados:
                    achados.append(texto)
                break
    return achados[:quantos]


def avaliacao(av, fonte: str = "", do_pipeline: bool = False, completo: bool = False) -> str:
    """A segunda mensagem: a avaliação da curadora, curta. `completo` (resposta a um pedido de avaliação): com os
    avisos que fazem a promo não valer (as que a curadora manda sozinha já passaram por esses filtros)."""
    linhas = [f"🧠 Curadora {SINAL.get(av.nota, '')} {NOMES.get(av.nota, av.nota)} {av.score}".replace("  ", " ")]
    card = getattr(av, "card", None)
    ml = card.preco if card is not None and getattr(card, "ok", False) and card.preco else None
    cupons = list(getattr(av.promo, "cupons", None) or [])
    quente = getattr(av, "cupom_quente", None) or {}
    codigo = quente.get("codigo") or (cupons[0] if cupons else "")
    if codigo and ml and av.preco_efetivo < ml - 0.5:
        preco = f"💰 {reais(av.preco_efetivo)} com o cupom {codigo} ({reais(ml)} no ML)"
    elif ml:
        preco = f"💰 {reais(av.preco_efetivo)} · ML confirma ✅"
    else:
        preco = f"💰 {reais(av.preco_efetivo)}"
    pct, mediana = getattr(av, "pct_abaixo", None), getattr(getattr(av, "historico", None), "mediana", None)
    if pct is not None and mediana:
        if pct >= 3:
            preco += f" · {pct:.0f}% abaixo do normal ({reais(round(mediana))})"
        elif pct > -3:
            preco += f" · no preço normal ({reais(round(mediana))})"
        else:
            preco += f" · {-pct:.0f}% acima do normal ({reais(round(mediana))})"
    linhas.append(preco)
    porque = motivos(av)
    if porque:
        linhas.append("👍 " + ", ".join(porque))
    if completo:
        avisos = []
        if card is not None and not getattr(card, "ok", False) and getattr(av.promo, "link", ""):
            avisos.append("não consegui abrir o link no ML agora: a nota é pelo preço da mensagem")
        if card is not None and getattr(card, "internacional", False):
            avisos.append("compra internacional: não pode postar")
        if getattr(av, "risco", False):
            avisos.append(getattr(av, "risco_motivo", "") or "vendedor de risco")
        for aviso in getattr(av, "alertas", None) or []:
            if "do normal" not in aviso and aviso not in avisos:   # acima/abaixo do normal já está na linha do preço
                avisos.append(aviso)
        linhas += ["⚠️ " + a[:120] for a in avisos[:3]]
    else:
        serio = next((a for a in getattr(av, "alertas", None) or [] if a.startswith(_SERIOS)), "")
        if serio:
            linhas.append("⚠️ " + serio[:110])
    rodape = [p for p in (f"🛍️ {fonte}" if fonte else "", "🤖 já foi pelo pipeline" if do_pipeline else "") if p]
    if rodape:
        linhas.append(" · ".join(rodape))
    return "\n".join(linhas)


def promo_montada(av) -> str:
    """A promo limpa quando não há o texto do post de origem (achado do radar, alerta fora do horário)."""
    p = av.promo
    card = getattr(av, "card", None)
    linhas = [f"*{(p.produto or '')[:90]}*"]
    if card is not None and getattr(card, "ok", False) and card.loja_oficial and card.vendedor:
        linhas.append(f"🏬 Loja oficial {card.vendedor}")
    de = getattr(p, "preco_de", None)
    if de and de > av.preco_efetivo * 1.03:
        linhas += ["", f"✅ De {reais(de)} por {reais(av.preco_efetivo)}"]
    else:
        linhas += ["", f"✅ Por {reais(av.preco_efetivo)}"]
    if getattr(p, "cupons", None):
        linhas.append("🎟️ Cupom: " + " ou ".join(p.cupons[:2]))
    if p.link:
        linhas += ["", f"🛒 {p.link}"]
    return "\n".join(linhas)


def fonte_do_card(card: dict | None) -> str:
    card = card or {}
    origem, grupo = str(card.get("origem") or ""), str(card.get("grupo") or "")
    # achado da própria curadora no ML (o "grupo" desses cards é "radar ML: <produto>" ou "vigia: <produto>")
    if origem.startswith("radar") or grupo.lower().startswith("radar"):
        return "radar do ML"
    if origem.startswith("vigia") or grupo.lower().startswith("vigia"):
        return "vigia do ML"
    return grupo


# ── o registro do que já foi para o grupo ──────────────────────

def registrar(dados: dict) -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRO.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(dados, ts=int(time.time())), ensure_ascii=False) + "\n")


def _linhas(desde: float) -> list[dict]:
    if not REGISTRO.exists():
        return []
    saida = []
    with REGISTRO.open("rb") as f:
        f.seek(max(0, REGISTRO.stat().st_size - 400_000))
        if f.tell():
            f.readline()
        for bruto in f:
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if r.get("ts", 0) >= desde and r.get("chave"):
                saida.append(r)
    return saida


def mostradas(desde: float) -> dict[str, float]:
    """{produto: menor preço} do que a curadora já pôs no grupo desde `desde`."""
    saida: dict[str, float] = {}
    for r in _linhas(desde):
        preco = float(r.get("preco") or 0)
        saida[r["chave"]] = min(saida[r["chave"]], preco) if r["chave"] in saida else preco
    return saida


def na_ultima_hora(agora: float | None = None) -> int:
    return len(_linhas((agora or time.time()) - 3600))


# ── o envio ────────────────────────────────────────────────────

def _post(url: str, payload: dict) -> dict:
    pedido = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(pedido, timeout=30) as resp:
        corpo = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(corpo) if corpo else {"success": True}
    except ValueError:
        return {"success": True}


def responder(grupo: str, texto: str, citar: str = "", http=None, ponte: str = PONTE) -> None:
    """Uma mensagem só, em resposta a outra (`citar` = id da mensagem que pediu a avaliação)."""
    if not grupo.endswith("@g.us") or grupo.split("@")[0] in PROIBIDOS:
        raise ValueError(f"grupo não permitido para a curadora: {grupo or '(vazio)'}")
    resposta = (http or _post)(f"{ponte}/send", {"chatId": grupo, "message": texto, **({"replyTo": citar} if citar else {})})
    if not resposta.get("success", True):
        raise RuntimeError(f"a ponte do WhatsApp recusou: {resposta}")


def enviar(grupo: str, promo: str, avaliacao_: str, foto: str = "", http=None, ponte: str = PONTE,
           pausa_s: float | None = None) -> None:
    """Manda as duas mensagens, nesta ordem. Levanta erro se a ponte recusar a promo (aí a avaliação nem vai)."""
    if not grupo.endswith("@g.us") or grupo.split("@")[0] in PROIBIDOS:
        raise ValueError(f"grupo não permitido para a curadora: {grupo or '(vazio)'}")
    pausa = (1.0 if http is None else 0) if pausa_s is None else pausa_s
    http = http or _post
    resposta = None
    if foto and os.path.isfile(foto):
        try:
            resposta = http(f"{ponte}/send-media", {"chatId": grupo, "filePath": foto, "mediaType": "image",
                                                    "caption": promo})
            if not resposta.get("success", True):
                resposta = None
        except Exception:  # noqa: BLE001 - a foto é um extra: sem ela, vai só o texto
            resposta = None
    if resposta is None:
        resposta = http(f"{ponte}/send", {"chatId": grupo, "message": promo})
        if not resposta.get("success", True):
            raise RuntimeError(f"a ponte do WhatsApp recusou: {resposta}")
    time.sleep(pausa)   # a avaliação tem de chegar depois da promo
    resposta = http(f"{ponte}/send", {"chatId": grupo, "message": avaliacao_})
    if not resposta.get("success", True):
        raise RuntimeError(f"a ponte do WhatsApp recusou a avaliação: {resposta}")
