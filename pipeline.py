"""Pipeline (09/10/2026, pedido do Breno: "tente rodar umas promos, hoje minha meta é 34"):

  curadora escolhe -> link de afiliado (API do gerador; reserva: bot BotGerador no grupo Gerador) -> caixa da normalizadora
  -> normalizadora posta no grupo Normalizador Promo (com a segunda leitura da curadora no relatório).

O Breno continua editando e postando no GrupoFinal: a curadora NUNCA posta lá.

Escolha: as melhores Excelentes/Boas frescas (conferidas no ML há pouco), sem as postadas no GrupoFinal ou mandadas
nas últimas 8h, sem as que passaram pela normalizadora nas últimas 3h (o Breno pode estar com elas na mão), sem
compra internacional e sem a mesma marca duas vezes seguidas. Se acabarem, as melhores Médias (nota 50+). Antes
de mandar, confere no ML de novo; se piorou, passa para a próxima.

Para ligar e desligar use o pipelinectl (é o que o Hermes chama):
      ./pipelinectl on --ate 22:00           liga agora até as 22h (1 a cada 4 min, no máximo 3 na fila do Breno)
      ./pipelinectl on --de 15:00 --ate 22:00
      ./pipelinectl off | status
      python3 pipeline.py --simular          só mostra o que mandaria
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import painel
from curadora import RAIZ, carregar_config
from curadora import grupo as vitrine
from curadora.aovivo import epoch
from curadora.banco import conectar, registrar_avaliacao
from curadora.conferente import LINK, chave_link
from curadora.esgotados import CUPOM_FRESCO_S
from curadora.esgotados import confirmado_na_loja_da_marca, esgotados as cupons_esgotados, loja_da_marca
from curadora.esgotados import fresco, trava_da_marca
from curadora.cupons import cupons_vencidos
from curadora import travas
from curadora.identidade import CHEIROS, FEMININO, GENERICAS, MASCULINO, VARIANTES, sem_acento
from curadora.esgotados import quentes as cupons_em_uso_agora
try:
    from curadora import ficha_cupom
except Exception:  # noqa: BLE001 - 30/10: a ficha do cupom (só sombra) nunca impede o pipeline de ligar
    ficha_cupom = None
from curadora.postagens import (MAIS_BARATO, REPETIR_APOS_S, REPETIR_EQUIPE_S, VEIO_DO_PIPELINE_S, ainda_vale_o_post,
                                 enviados_pelo_pipeline, marcar_mesmo_anuncio, post_da_equipe, ultimos_posts)
from curadora.ml import ConsultorML
from curadora.ml_api import ClienteML
from curadora.parser import ler_promo, parece_perfume
from curadora.score import Curadora, para_dict

GERADOR = "120363000000000013@g.us"
PONTE = "http://127.0.0.1:3000/send"
TAP = Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
CAIXA = Path(os.path.expanduser("~/.hermes/run/curadora_normalizer_inbox.jsonl"))
FEEDBACK = Path(os.path.expanduser("~/.hermes/perfume_image_bank/logs/normalizer_feedback.jsonl"))
REGISTRO = RAIZ / "dados" / "pipeline.jsonl"
TRAVAS = RAIZ / "dados" / "pipeline_travas.json"   # 21/10: por que pulou cada produto (o painel mostra)
PARAR = RAIZ / "dados" / "pipeline.parar"
ESTADO = RAIZ / "dados" / "pipeline.estado.json"   # o que está rodando agora (para o pipelinectl status)
ESPERA_GERADOR_S = 120    # o BotGerador responde em ~9 s (mediana); 90% em menos de 1 min
# 24/10 (Breno: "acredito que ficaria melhor, nem iríamos precisar do gerador"): o link de afiliado vem da API do
# gerador, que responde em 1-4 s e devolve o texto igual, só com o link trocado. O BotGerador (WhatsApp) fica de
# reserva: se a API falhar ou não trocar o link, vai pelo grupo como antes. A chave fica fora do git, neste arquivo
# ({"url", "slug", "token"}); sem ele, tudo vai pelo WhatsApp.
GERADOR_API = Path(os.path.expanduser("~/.config/curadora/gerador_api.json"))
GERADOR_API_S = 8   # a API responde em ~1 s; se demorar, vai pelo WhatsApp sem segurar a rodada
MELI = re.compile(r"https?://meli\.la/[A-Za-z0-9]+")
NOTAS_BOAS = ("IMPERDÍVEL", "BOA")
MEDIA_MINIMA = 50
NA_NORMALIZADORA_S = 8 * 3600   # 11/10: eram 3h, e o pipeline mandou de novo o Philos Pura que o Breno tinha mandado à
                                # mão 3h03 antes (a normalizadora fez o post duas vezes). Agora vale o mesmo das 8h.
FILA_JANELA_S = 25 * 60    # "equipe postando" e "fila" olham os últimos 25 min
FOTOS = RAIZ / "dados" / "fotos"   # foto do anúncio do ML, para quando a normalizadora não tem foto do produto
FOTO_DO_GRUPO_S = 24 * 3600        # a ponte do WhatsApp guarda as fotos recebidas por ~1 dia
NOSSOS_GRUPOS = ("120363000000000013", "120363000000000012", "120363000000000001", "120363000000000002",
                 "120363000000000014")   # Gerador, Normalizador, Edição, GrupoFinal e Vitrine da Equipe: não são fonte de foto
CATALOGO = re.compile(r"mercadoli(?:vre|bre)\.com\.br/(?:[^/\s?]+/)?p/(MLB\d+)")   # página de catálogo do ML
LINKS_DO_CATALOGO = RAIZ / "dados" / "links_do_catalogo.json"   # produto do catálogo -> link de afiliado da página
LINK_DO_CATALOGO_S = 7 * 86400
CARA, BARATA = 350.0, 200.0   # 11/10 (Breno: "se vem um perfume acima de 350 era pra vir 2 no preço melhor")
SAEM_MAIS = re.compile(r"\bnatura\b|\bboticario\b")   # 11/10 (Breno): "Natura e Boticário saem mais"
NB_A_CADA = 5                 # "a cada 5 promos, se não vier um Boticário ou Natura, colocar 1" (Breno, 11/10)
OUTRO_TAMANHO_S = 4 * 3600    # o mesmo perfume em outro tamanho só depois de 4 horas
# 16/10 (Breno: "dá um boost nos kits Primacial, o povo gosta; nos Vodka"). No dia, 13 kits/Vodka foram vistos e
# conferidos (Média 50-59 no preço normal, a nota de "vende bem") e só 1 foi: sempre havia uma Boa na frente. Como a
# regra de Natura/Boticário: a cada 6 promos (Breno, 16/10: "pode ser a cada 6"), se não veio nenhum, o melhor kit
# de body splash (Primacial) ou Vodka da
# lista passa na frente. Só entra quem já está na lista (Boa, ou Média 50+) e passa na conferência do ML na hora.
IMPULSO = re.compile(r"\bvodka\b|\bprimacial\b|\bkit\b.*\bbody\s*splash\b|\bbody\s*splash\b.*\b(?:kit|barbarius|"
                     r"enigma|midtown|infalivel|banker|adventus|vibration|blunn)\b")
IMPULSO_A_CADA = 6
MARCAS_RECENTES = 4           # rodízio: quem repete a marca de uma das últimas 4 mandadas vai para depois
RECENTES_PARA_A_ORDEM = max(MARCAS_RECENTES, NB_A_CADA - 1, IMPULSO_A_CADA - 1)   # quantas mandadas a ordem olha
# Para o rodízio de marcas: o nome nem sempre traz a marca ("Polo 67", "Stronger With You", "Hawas Chrome").
LINHAS_DE_MARCA = tuple((marca_, re.compile(padrao)) for marca_, padrao in (
    ("ralph lauren", r"ralph lauren|\bpolo\b|ralph'?s club"),
    ("armani", r"armani|acqua di gio|stronger with you|\bmy way\b|power of you"),
    ("hugo boss", r"hugo boss|\bboss\b"),
    ("carolina herrera", r"carolina herrera|\b212\b|good girl|bad boy"),
    ("lancome", r"lancome|\bidole\b|la vie est belle"),
    ("versace", r"versace|\beros\b|dylan blue"),
    ("rabanne", r"rabanne|invictus|1 million|one million|phantom|olympea"),
    ("jean paul gaultier", r"gaultier|\bscandal\b|le male"),
    ("rasasi", r"rasasi|\bhawas\b"),
    ("al wataniah", r"wataniah|sabah al ward|bareeq|wazeer"),
    ("lattafa", r"lattafa|\basad\b|\byara\b|fakhar|khamrah"),
    ("natura", r"\bnatura\b|\bkaiak\b"),
    ("o boticario", r"boticario|\bmalbec\b|floratta|\begeo\b|\bquasar\b"),
))
PISTA_DE_PRODUTO = re.compile(r"(?i)\d{2,4}\s*ml\b|\bed[pt]\b|\bparfum\b")
# 14/10 (Breno: "não quero que baixe a régua, mas mesmo com pouca promoção eu preciso enviar algo, então tem que sair
# algo, mesmo uma promo de 6 horas atrás"). Sem promo nova boa, a RESERVA repete uma boa que já foi: primeiro as de 6h
# ou mais, depois as de 4h ou mais. A régua (Excelente, Boa, Média 50+) e a conferência no ML são as mesmas.
RESERVA_S = (6 * 3600, 4 * 3600)
_janela_repetir = REPETIR_APOS_S   # só muda enquanto a reserva escolhe
_na_reserva = False
_recusadas_reserva: dict[str, float] = {}
# 15/10 (Breno: "mesmo promos ruins têm que ir; claro, se tiver uma melhor, obviamente postar a melhor; essas ruins devem
# ser postadas nesses momentos em que estamos sem promo"). Depois da reserva, o FUNDO DA FILA: a melhor das que sobraram
# abaixo da régua (Média baixa e Ruim, nota 20+), novas (não foram em 8h). As travas continuam: vendedor de risco,
# compra internacional, o que não é perfume, cupom velho.
PISO_DO_FUNDO = 20
# 16/10 (Breno: "tem uma certa repetição nas rodadas do dia a dia… tinha que expandir o leque de produtos"; "faz a 1 e
# a 2"). Medido: 77% do que o pipeline mandou no dia já tinha ido em outro dia; às 20:35 não sobrava nenhuma Boa que não
# tivesse ido na semana, e havia 11 Médias novas na fila. NOVIDADE NA FRENTE: produto que foi (pelo pipeline, ou postado
# no GrupoFinal) nos últimos 3 dias perde 5,5 pontos NA ORDEM, e a nota mostrada não muda: um novo de até 5 pontos abaixo
# passa na frente (Média 56 nova antes de Boa 60 repetida; Boa 70 repetida continua antes de Média 56). Os queridinhos
# (lista dos mais vendidos e kits de body splash) não perdem.
NOVIDADE_S = 3 * 24 * 3600
NOVIDADE_PONTOS = 5.5
LIMITE_BOA = 60
# 18/10 (Breno: "mandou mais promos duvidosas agora há pouco, vendedor com poucas vendas; isso tem que evitar como
# padrão, dar uma boa preferência em lojas oficiais"). Na semana, das promos do pipeline a equipe postou no GrupoFinal 52%
# das de loja oficial e 42% das de vendedor comum com 5.000+ vendas, mas 17% das de 500 a 4.999 e nenhuma das com
# menos de 500 (às 17:25 foi um Club de Nuit Intense de vendedor com 18 vendas). Quem não é loja oficial só vai com
# 5.000+ vendas e reputação que não seja fraca, em todas as filas (até no fundo). Na ordem, a loja oficial ganha 8
# pontos dentro da sua faixa: uma Boa de loja oficial passa na frente de Boas parecidas, mas uma Média não vira Boa.
VENDAS_MINIMAS = travas.VENDAS_MINIMAS
OFICIAL_PONTOS = 8
# 18/10 (Breno: "parece que parou de chegar promo no normalizador"): o Gerador não responde para o Mawwal Rayhan (BOA 74,
# sempre no topo) e o pipeline tentava de novo a cada rodada, 4 min de espera cada vez (15:46, 17:35, 17:48, 17:55). O
# que o Gerador não respondeu nas 2 tentativas fica fora por 6h; vem do registro, então vale depois de reiniciar.
GERADOR_FALHOU_S = 6 * 3600
_no_fundo = False
_recusadas_fundo: dict[str, float] = {}


_calado = False   # a vitrine (grupo da equipe) confere em silêncio: o que vai para o log é do pipeline


REVIEWS = RAIZ / "dados" / "rodadas"


def guardar_review() -> None:
    """18/10 (Breno: "por padrão coloque isso no fim das rodadas, esse review padrão do fim"): no fim de cada rodada o
    review do dia (review_rodada.py: acertos, erros e correções) fica em dados/rodadas/AAAA-MM-DD.txt. Falha aqui
    nunca derruba o pipeline."""
    try:
        saida = subprocess.run([sys.executable, str(RAIZ / "review_rodada.py")], capture_output=True, text=True,
                               timeout=300, cwd=str(RAIZ)).stdout
        if not saida.strip():
            return
        REVIEWS.mkdir(parents=True, exist_ok=True)
        (REVIEWS / f"{time.strftime('%Y-%m-%d')}.txt").write_text(saida, encoding="utf-8")
        log(f"review da rodada em dados/rodadas/{time.strftime('%Y-%m-%d')}.txt")
    except Exception as exc:
        log(f"review da rodada não saiu: {type(exc).__name__}: {exc}")


def log(msg: str) -> None:
    if not _calado:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def ambiente_do_servico() -> None:
    """O acesso à API do ML (reputação, catálogo, compra internacional) fica no ambiente do serviço da curadora.
    Rodando fora dele (ssh, Hermes), o pipeline copia de lá as variáveis CURADORA_*. 10/10: sem isso o pipeline
    não via quem vende e não tinha como barrar compra internacional sozinho."""
    try:
        saida = subprocess.run(["systemctl", "--user", "show", "curadora", "-p", "Environment"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return
    for par in saida.removeprefix("Environment=").split():
        nome, _, valor = par.partition("=")
        if nome.startswith("CURADORA_") and valor:
            os.environ.setdefault(nome, valor)


def gravar_estado(dados: dict | None) -> None:
    if dados is None:
        ESTADO.unlink(missing_ok=True)
        return
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def registrar(dados: dict) -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRO.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(dados, ts=int(time.time())), ensure_ascii=False) + "\n")


def ja_enviadas(desde: float) -> set[str]:
    if not REGISTRO.exists():
        return set()
    saida = set()
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        # 31/10 (revisão): só o que foi mandado. A falha do Gerador já tem o seu bloqueio de 6 h
        # (gerador_nao_respondeu); contada aqui, sumia por 8 h, 24 h e 3 dias (3 casos em 25/10, de volta só no dia 26).
        if r.get("ts", 0) >= desde and r.get("chave") and r.get("status", "enviado") == "enviado":
            saida.add(r["chave"])
    return saida


def gerador_nao_respondeu(desde: float) -> set[str]:
    """Produtos em que o Gerador não respondeu (as 2 tentativas) desde `desde` (ver GERADOR_FALHOU_S)."""
    if not REGISTRO.exists():
        return set()
    saida = set()
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "gerador_sem_resposta" and r.get("ts", 0) >= desde and r.get("chave"):
            saida.add(r["chave"])
    return saida


def ultimas_mandadas(quantas: int = 2, janela_s: float = 3600) -> list[dict]:
    """As últimas promos mandadas (a mais nova por último), só as da última hora."""
    if not REGISTRO.exists():
        return []
    saida = []
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines()[-40:]:
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "enviado" and r.get("preco") and r.get("ts", 0) >= time.time() - janela_s:
            saida.append(r)
    return saida[-quantas:]


def ultimos_precos(quantos: int = 2, janela_s: float = 3600) -> list[float]:
    return [float(r["preco"]) for r in ultimas_mandadas(quantos, janela_s)]


def misturar(lista: list[dict], nome, ultimas: list[dict], marca_de=None, linhas_da_hora: set | None = None) -> list[dict]:
    """A ordem em que as promos saem, sem mexer em quais saem. `nome(card)` dá o nome do produto.

    0. Marcas (11/10, Breno: "tem hora que parece que manda vários Armanis, vários Ralph Lauren e nada de
       Boticário e Natura"): quem repete a marca de uma das últimas 4 mandadas vai para depois.
    1. Faixas de preço: depois de uma cara (R$350+), as 2 seguintes são de até R$200 (ver `vez`).
    2. Natura e Boticário (no dia, o Breno postou 78% das que o pipeline mandou e 47% das outras). Regra dele, de
       11/10 às 16h30: "a cada 5 promos, se não vier um Boticário ou Natura, colocar 1". Quando as 4 últimas
       mandadas não tiveram nenhuma, a melhor Natura/Boticário da lista passa na frente; fora isso, vale a nota."""
    if marca_de:
        recentes = {marca_de(r.get("produto") or "") for r in ultimas[-MARCAS_RECENTES:]} - {""}
        lista = sorted(lista, key=lambda c: marca_de(nome(c)) in recentes)
    if linhas_da_hora:
        # 0b. 31/10: a mesma linha mandada na última hora vai para depois (ver linha); as regras abaixo mandam mais.
        lista = sorted(lista, key=lambda c: bool(linha(c.get("chave") or "") & linhas_da_hora))
    ultimos = [float(r["preco"]) for r in ultimas[-2:]]
    if any(p >= CARA for p in ultimos):
        lista = sorted(lista, key=lambda c: vez(c["preco"] or 0, ultimos, sai_mais(nome(c))))
    if vez_de_natura_ou_boticario(ultimas):
        lista = sorted(lista, key=lambda c: not sai_mais(nome(c)))
    if vez_do_impulso(ultimas):   # 3. kits de body splash (Primacial) e Vodka: ver IMPULSO
        lista = sorted(lista, key=lambda c: not impulsionado(nome(c)))
    return lista


def impulsionado(produto: str) -> bool:
    return bool(IMPULSO.search(sem_acento(str(produto or "").lower())))


def vez_do_impulso(ultimas: list[dict]) -> bool:
    """As 5 últimas mandadas (na última hora) não tiveram kit de body splash nem Vodka: a 6ª é deles."""
    recentes = ultimas[-(IMPULSO_A_CADA - 1):]
    return len(recentes) >= IMPULSO_A_CADA - 1 and not any(impulsionado(r.get("produto")) for r in recentes)


def sai_mais(produto: str) -> bool:
    return bool(SAEM_MAIS.search(sem_acento(str(produto or "").lower())))


def vez_de_natura_ou_boticario(ultimas: list[dict]) -> bool:
    """As 4 últimas mandadas (na última hora) não tiveram Natura nem Boticário: a 5ª é delas."""
    recentes = ultimas[-(NB_A_CADA - 1):]
    return len(recentes) >= NB_A_CADA - 1 and not any(sai_mais(r.get("produto")) for r in recentes)


def marca_visivel(cur: Curadora, produto: str) -> str:
    """A marca como quem lê o grupo enxerga (para o rodízio): "Polo 67" é Ralph Lauren, "My Way" é Armani."""
    texto = sem_acento(str(produto or "").lower())
    for marca_, padrao in LINHAS_DE_MARCA:
        if padrao.search(texto):
            return marca_
    return marca(cur, produto)


# 31/10 (Breno: "pode já montar as 3 correções"). O GrupoFinal postou "Kit Floratta Blue" (kit:blue-floratta|?) a R$96,81
# às 13:46 e o pipeline mandou "Kit Floratta Blue (Perfume 30ml + Hidratante 75ml)" (kit:blue-floratta-hidratante|30)
# a R$113,90 às 16:09: a repetição olhava a chave exata. No kit, o item de acompanhamento e o tamanho não mudam o produto.
ACOMPANHAMENTO_DE_KIT = {"hidratante", "locao", "creme", "desodorante", "sabonete", "necessaire", "oleo", "gel",
                         "esfoliante", "shampoo", "condicionador", "presente", "estojo", "caixa"}


def mesmo_kit(chave: str) -> str:
    """O kit sem tamanho e sem item de acompanhamento: "kit:blue-floratta-hidratante|30" e "kit:blue-floratta|?" são o
    mesmo kit. Devolve "" para o que não é kit."""
    base = (chave or "").split("|")[0]
    if not base.startswith("kit:"):
        return ""
    partes = sorted(t for t in base[4:].split("-") if t and t not in ACOMPANHAMENTO_DE_KIT)
    return "kit:" + "-".join(partes) if partes else ""


# 31/10 (Breno: "pode já montar as 3 correções"; em 31/10: Yara Tous 15:32 e Yara Elixir 16:18, Essencial Exclusivo
# 15:45 e Essencial Sentir 16:05, dois kits Vibration + Blunn 15:49 e 16:13). A LINHA do produto são as palavras da
# chave que não são versão, tamanho, cheiro nem palavra genérica: "tous-yara" e "elixir-yara" são a linha Yara.
NAO_E_LINHA = set(VARIANTES) | set(GENERICAS) | set(CHEIROS) | ACOMPANHAMENTO_DE_KIT
LINHA_RECENTE_S = 3600   # a mesma linha só depois de 1 h, se houver outra opção (só muda a ordem)


def linha(chave: str) -> set[str]:
    base = (chave or "").split("|")[0].removeprefix("kit:")
    return {t for t in base.split("-") if len(t) >= 4 and not t.isdigit() and t not in NAO_E_LINHA}


def linhas_recentes(janela_s: float = LINHA_RECENTE_S) -> set[str]:
    """As linhas dos produtos mandados na última hora."""
    return set().union(*(linha(r.get("chave") or "") for r in ultimas_mandadas(40, janela_s)))


def familia(chave: str) -> str:
    """O perfume sem o tamanho e sem palavra de embalagem: "my-way|30" e "my-way-refilavel|90" são a mesma."""
    base = (chave or "").split("|")[0]
    if not base or base.startswith("kit"):
        return chave
    fora = {"refilavel", "refil", "recarga", "recarregavel", "tradicional", "original"}
    return "-".join(sorted(t for t in base.split("-") if t and t not in fora)) or chave


def aceitavel(av) -> bool:
    # Conflito de produto nunca vira alternativa de fundo, mesmo com nota 20.
    if getattr(av, "conflito", ""):
        return False
    if _no_fundo:
        return av.nota in ("IMPERDÍVEL", "BOA", "OK", "NÃO VALE") and av.score >= PISO_DO_FUNDO
    """Boa/Excelente, ou Média de 50 para cima. Natura e Boticário não têm mais régua própria aqui: desde 11/10 à
    noite é a nota delas que sobe 20 pontos na curadora (score.py), e o pipeline só garante 1 a cada 5."""
    return av.nota in NOTAS_BOAS or (av.nota == "OK" and av.score >= MEDIA_MINIMA)


def _sem_enfeite(linha: str) -> str:
    return re.sub(r"[*_~`]", "", linha or "").strip()


def _mesmo_nome(a: str, b: str) -> bool:
    a, b = (re.sub(r"[^a-z0-9]", "", sem_acento(x.lower())) for x in (a, b))
    return bool(a and b) and (a == b or a in b or b in a)


def arrumar_resposta(corpo: str, produto: str, preco: float | None = None) -> str:
    """O texto que o Gerador devolveu, antes de ir para a normalizadora.

    11/10, dois posts saíram com a CHAMADA do grupo no lugar do nome ("Da Pro Ano Inteiro 200ml" era o Banderas
    The Icon; "O Mais Hypado Versão 50ml" era o Jequiti Cebolinha) e um com o preço do grupo (R$39; no ML, R$44,90).
    Quando a normalizadora não consegue abrir o anúncio, ela usa a 1ª linha como nome se tiver cara de produto
    ("200ML", "EDP") e o preço do texto. Então: (1) chamada com cara de produto sai, e o nome do produto vai
    para a 1ª linha; (2) com `preco` (o conferido agora no ML, quando não há cupom no texto), o "por R$" vira ele."""
    linhas = corpo.split("\n")
    cheias = [i for i, l in enumerate(linhas) if _sem_enfeite(l)]
    if len(cheias) >= 2:
        primeira = _sem_enfeite(linhas[cheias[0]])
        do_produto = next((i for i in cheias[1:4] if _mesmo_nome(_sem_enfeite(linhas[i]), produto)), None)
        if PISTA_DE_PRODUTO.search(primeira) and not _mesmo_nome(primeira, produto) and do_produto is not None:
            resto = [l for i, l in enumerate(linhas) if i not in (cheias[0], do_produto)]
            while resto and not resto[0].strip():
                resto.pop(0)
            corpo = re.sub(r"\n{3,}", "\n\n", "\n".join([linhas[do_produto], ""] + resto))
    if preco:
        def troca(m):
            atual = float(m.group(2).replace(".", "").replace(",", "."))
            # 31/10 (revisão): perto do conferido o texto fica, mas não R$1 ou mais ABAIXO dele (R$159 com o ML a
            # R$160,20, 26/10). Só os centavos cortados continuam ("R$ 194" para R$194,49: é o estilo da casa).
            if abs(atual - preco) <= max(1.0, preco * 0.012) and atual > preco - 1.0:
                return m.group(0)
            return m.group(1) + f"{preco:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        corpo = re.sub(r"(?i)(\bpor\s*:?\s*[*_~]*\s*R\$\s*[*_~]*\s*)(\d[\d.]*(?:,\d{1,2})?)", troca, corpo, count=1)
    return corpo


def vez(preco: float, ultimos: list[float], natura_ou_boticario: bool = False) -> int:
    """Mistura de faixas de preço: 0 = é a vez dela; quanto maior, mais para depois.

    11/10, Breno: "tava vindo tipo 5, 6 perfumes acima de 400" (10:55-11:07: Polo, Boss, Polo, Idôle em fila).
    "Não tá errado mandar as boas, Polos, Armani etc."; "se vem um perfume acima de 350 era pra vir 2 no preço
    melhor"; "Natura e Boticário saem mais". Então: depois de uma cara (R$350 ou mais), as 2 seguintes são de
    até R$200, com Natura e Boticário na frente. Só muda a ordem: se não houver outra, a cara vai assim mesmo."""
    if not any(p >= CARA for p in ultimos[-2:]):
        return 0
    if preco < BARATA:
        return 0 if natura_ou_boticario else 1
    return 2 if preco < CARA else 3


def na_normalizadora(cur: Curadora, desde: float) -> set[str]:
    """Produtos que passaram pela normalizadora há pouco (o Breno pode estar postando agora)."""
    saida = set()
    if not FEEDBACK.exists():
        return saida
    with FEEDBACK.open("rb") as f:
        f.seek(max(0, FEEDBACK.stat().st_size - 3_000_000))
        f.readline()
        for bruto in f:
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if (r.get("ts") or 0) >= desde:
                nome = r.get("display_product") or r.get("product") or ""
                if nome:
                    saida.add(cur.chave_de(nome))
    return saida


def recentes(con, cur: Curadora) -> set[str]:
    """Produtos que não podem ir agora: mandados pelo pipeline ou postados no GrupoFinal há menos de 8h, ou que
    passaram pela normalizadora nas últimas 3h (o Breno pode estar com eles na mão)."""
    agora = time.time()
    postados = {r[0] for r in con.execute("SELECT chave FROM postagens WHERE ts >= ? AND chave IS NOT NULL",
                                          (agora - REPETIR_APOS_S,))}
    return ja_enviadas(agora - REPETIR_APOS_S) | postados | na_normalizadora(cur, agora - NA_NORMALIZADORA_S)


COM_CUPOM_MINIMA = 30   # com cupom da equipe valendo, promo de nota 30+ entra na lista para ser refeita com ele


def cupons_da_equipe_agora(cur: Curadora) -> set[str]:
    """Os cupons que o Breno está usando agora (esgotados.quentes), se a chave geral estiver ligada."""
    if not (cur.cfg.get("cupom_quente") or {}).get("ligado"):
        return set()
    return set(cupons_em_uso_agora(cur.con))


def motivo_da_parada(cur: Curadora, codigos: set[str]) -> str:
    """30/10: por que a rodada de cupom parou, no log (frescor: nenhum grupo postou o cupom há 45 min)."""
    agora = time.time()
    partes = []
    for codigo in sorted(codigos):
        r = cur.con.execute("SELECT ultimo_ts FROM cupons WHERE codigo = ?", (codigo,)).fetchone()
        if r and r[0] and not fresco(cur.con, codigo, agora):
            partes.append(f"{codigo}: nenhum grupo postou há {int((agora - r[0]) // 60)} min")
    return f" ({'; '.join(partes)})" if partes else ""


def cupom_da_marca_agora(cur: Curadora) -> str:
    """29/10 (Breno: "como a gente sabe que está rolando cupom nessas lojas, focar em enviar produtos que valem para
    esse momento"): o cupom de loja da marca (LOJAS<data>) valendo agora, ou "". Vale quando você confirmou hoje num
    post de loja da marca (Natura, Boticário, Eudora...) e ele não esgotou nem venceu."""
    if not (cur.cfg.get("cupom_quente") or {}).get("ligado"):
        return ""
    agora = time.time()
    mortos = cupons_esgotados(cur.con, agora)
    for (codigo,) in cur.con.execute("SELECT codigo FROM cupons WHERE codigo LIKE 'LOJAS%' AND ultimo_ts >= ? "
                                     "ORDER BY ultimo_ts DESC", (agora - 24 * 3600,)):
        if (codigo not in mortos and not cupons_vencidos([codigo], agora)
                and confirmado_na_loja_da_marca(cur.con, codigo, agora)
                and trava_da_marca(cur.con, codigo, agora) and fresco(cur.con, codigo, agora)):   # 30/10
            return codigo
    return ""


def da_loja_da_marca(con, cards: list[dict]) -> set[int]:
    """Os cards (id do dict) cujo anúncio é da loja oficial da própria marca, pela avaliação mais recente."""
    ids = {}
    for c in cards:
        av_id = (c.get("atual") or {}).get("id") or c.get("id")
        if av_id:
            ids[int(av_id)] = id(c)
    if not ids:
        return set()
    marca = set()
    lista = list(ids)
    for i in range(0, len(lista), 500):
        parte = lista[i:i + 500]
        for av_id, vendedor, oficial in con.execute(
                "SELECT id, json_extract(dados, '$.ml.vendedor'), json_extract(dados, '$.ml.oficial') FROM avaliacoes "
                "WHERE id IN (%s)" % ",".join("?" * len(parte)), parte):
            if oficial and loja_da_marca(vendedor):
                marca.add(ids[av_id])
    return marca


def queridinhos(con, desde: float) -> set[str]:
    """Os produtos da lista do Breno (config top_vendidos) e os kits de body splash vistos desde `desde`.

    11/10, lista confirmada por ele: Club de Nuit Intense, Asad, Sabah Al Ward, Fakhar Platin, Sabah Al Ward
    Sugar, The Kingdom, Bareeq Al Dhahab, Yara, Attracione Men, Wazeer Al Noble e "Kit Body Splash (todos valem
    muito a pena)". A curadora já marca cada avaliação com top_vendido e com a categoria."""
    return {r[0] for r in con.execute(
        "SELECT DISTINCT chave FROM avaliacoes WHERE ts >= ? AND chave IS NOT NULL AND nota != 'FORA' AND "
        "(json_extract(dados, '$.top_vendido') = 1 OR json_extract(dados, '$.categoria') = 'kit_body_splash')",
        (desde,))}


def candidatas(con, cur: Curadora, excluir: set[str]) -> tuple[list[dict], list[dict]]:
    """Devolve (prioridade, resto). A prioridade só existe enquanto há cupom da equipe valendo."""
    agora = time.time()
    pulados = {r[0] for r in con.execute("SELECT chave FROM pulos WHERE ts >= ?", (agora - painel.PULO_S,))}
    # Recalculado a cada escolha. Produto postado ou mandado há menos de 8h só volta se estiver 8% mais barato
    # (10/10 à noite, Breno: "sim"); o que passou pela normalizadora e ainda não foi postado fica na mão dele.
    posts = {k: v for k, v in ultimos_posts(con, agora, REGISTRO).items() if agora - v[1] < _janela_repetir}
    na_mao = na_normalizadora(cur, agora - min(NA_NORMALIZADORA_S, _janela_repetir)) - set(posts)
    # 31/10: o mesmo kit com outro nome conta como o mesmo produto (ver mesmo_kit); vale o post mais recente dele
    kits_postados: dict[str, tuple] = {}
    for k_, v_ in posts.items():
        mk = mesmo_kit(k_)
        if mk and (mk not in kits_postados or v_[1] > kits_postados[mk][1]):
            kits_postados[mk] = v_

    def ja_foi(k: str, preco) -> bool:
        mk = mesmo_kit(k)
        return ainda_vale_o_post(posts.get(k), preco) or (bool(mk) and ainda_vale_o_post(kits_postados.get(mk), preco))

    # 11/10 (Breno: "tá repetindo muito, tipo o mesmo produto de tamanhos diferentes; não é necessariamente
    # errado, mas quem olha às vezes acha que o grupo tá repetindo"; no dia: My Way 30ml e 90ml em 1h, Idôle 50ml
    # e 100ml em 45 min). Outro tamanho de um perfume mandado ou postado há menos de 4h espera.
    familias = {familia(k): k for k, (_, ts) in posts.items() if agora - ts <= OUTRO_TAMANHO_S}
    sem_gerador = gerador_nao_respondeu(agora - GERADOR_FALHOU_S)

    def pode(c):
        k = c["chave"]
        return (k not in excluir and k not in pulados and k not in na_mao and k not in sem_gerador
                and not ja_foi(k, c["preco"]) and familias.get(familia(k), k) == k)

    todos = [c for c in painel._cards(con) if c.get("chave")]
    # 21/10 (Breno: "sim pros 2"): marcado como postado por post da EQUIPE (o pipeline não tinha mandado) volta
    # depois de 6h; o que veio do pipeline continua com 8h (ver postagens.REPETIR_EQUIPE_S).
    enviados = enviados_pelo_pipeline(REGISTRO, agora - REPETIR_APOS_S - VEIO_DO_PIPELINE_S)

    def janela_postada(c) -> float:
        if post_da_equipe(c["chave"], c["postada"], enviados):
            return min(_janela_repetir, REPETIR_EQUIPE_S)
        return _janela_repetir
    cards = [c for c in todos if painel._fresca(c, agora) and pode(c)
             and not (c["postada"] and agora - c["postada"] <= janela_postada(c))]
    # 15/10 (Breno: "tem algumas repetições… será que tá olhando muito as mesmas coisas?"): das 50 da noite, 12 já
    # tinham ido no turno de ontem. Produto que o pipeline mandou nas últimas 24h vai para o fim da sua faixa (Boa
    # continua antes de Média); se não houver outra, ainda vai.
    # Os queridinhos podem repetir (Breno: "veja aquela lista do Colega, não tem problema repetir, mas tem outros que
    # já não são tão tops"): a lista dos mais vendidos (top_vendidos) e os kits de body splash não vão para o fim.
    queridos = queridinhos(con, agora - painel.JANELA_FEED_S)
    de_ontem = ja_enviadas(agora - 24 * 3600) - queridos
    repetidos = repetidos_recentes(con, agora) - queridos   # 16/10: ver NOVIDADE_S
    oficiais = lojas_oficiais(con, {c["chave"] for c in todos}, agora - painel.JANELA_FEED_S)   # ver OFICIAL_PONTOS
    ordem = lambda c: (c["chave"] in de_ontem, -(c["score"] + (OFICIAL_PONTOS if c["chave"] in oficiais else 0)),
                       -c["ts"])
    # 29/10 (Breno: "focar em enviar produtos que valem para esse momento"): com cupom de loja da marca valendo, os
    # produtos da loja oficial da própria marca (Natura, Boticário, Eudora...) vão na frente; a conferência põe o cupom.
    frente: list[dict] = []
    if not _no_fundo and cupom_da_marca_agora(cur):
        marca = da_loja_da_marca(con, cards)
        frente = sorted((c for c in cards if id(c) in marca and c["score"] >= COM_CUPOM_MINIMA), key=ordem)
    na_frente_marca = {id(c) for c in frente}
    if _no_fundo:
        return [], sorted((c for c in cards if c["nota"] in ("OK", "NÃO VALE") and c["score"] >= PISO_DO_FUNDO
                           and not (c["nota"] == "OK" and c["score"] >= MEDIA_MINIMA)), key=ordem)
    boas = sorted((c for c in cards if c["nota"] in NOTAS_BOAS), key=ordem)
    medias = sorted((c for c in cards if c["nota"] == "OK" and c["score"] >= MEDIA_MINIMA), key=ordem)
    # A ordem é só pela nota. 10/10: pôr na frente as promos com cupom "quente" trouxe promo pior (16:30-16:45).
    # 11/10 (Breno mandou 6 à mão em 1 minuto com o MODA1110): com cupom da equipe valendo, as que estão abaixo
    # da linha também são conferidas, por último; só vão se COM o cupom ficarem Boas (ver conferir e uma_promo).
    resto, prioridade = [], []
    if cupons_da_equipe_agora(cur):
        ja = {id(c) for c in boas + medias}
        resto = sorted((c for c in cards if id(c) not in ja and c["score"] >= COM_CUPOM_MINIMA), key=ordem)
        # 11/10 (Breno: "devia ter dado uma priorizada naquela listinha dos perfumes mais vendidos"; o Fakhar
        # Black com cupom ele mandou à mão): na rodada de cupom os queridinhos vão PRIMEIRO, mesmo que nenhum grupo
        # tenha postado hoje (vale o último anúncio visto em 48h) e mesmo que já tenham ido hoje sem o cupom.
        # Quem decide se vai é a conferência no ML, com o preço de agora e o cupom (conferir e uma_promo).
        lista = queridinhos(con, agora - painel.JANELA_FEED_S)
        prioridade = sorted((c for c in todos if c["chave"] in lista and c["chave"] not in excluir
                             and c["chave"] not in pulados and c["chave"] not in na_mao
                             and c["chave"] not in sem_gerador), key=ordem)
        na_frente = {id(c) for c in prioridade} | na_frente_marca
        return frente + [c for c in prioridade if id(c) not in na_frente_marca], \
            [c for c in na_ordem_da_novidade(boas + medias, de_ontem, repetidos, oficiais) + resto
             if id(c) not in na_frente]
    # Natura e Boticário a preço perto do normal agora chegam aqui como Médias (a nota delas sobe na curadora) e a
    # ordem (misturar) garante 1 a cada 5: "a cada 5 promos, se não vier um Boticário ou Natura, colocar 1".
    return frente, [c for c in na_ordem_da_novidade(boas + medias, de_ontem, repetidos, oficiais)
                    if id(c) not in na_frente_marca]


def repetidos_recentes(con, agora: float) -> set[str]:
    """Produtos que o pipeline mandou ou que a equipe postou no GrupoFinal nos últimos 3 dias (ver NOVIDADE_S)."""
    saida = ja_enviadas(agora - NOVIDADE_S)
    saida |= {r[0] for r in con.execute("SELECT DISTINCT chave FROM postagens WHERE ts >= ? AND chave IS NOT NULL",
                                         (agora - NOVIDADE_S,))}
    return saida


def na_ordem_da_novidade(cards: list[dict], de_ontem: set[str], repetidos: set[str],
                         oficiais: set[str] = frozenset()) -> list[dict]:
    """A ordem da fila: Boa antes de Média (pela nota de ordem); dentro de cada faixa, o que foi nas últimas 24h por
    último; depois a nota de ordem (a nota menos 5,5 para quem foi nos últimos 3 dias; mais 8 para loja oficial, que
    não muda a faixa) e o card mais recente."""
    def ordem(c):
        efetiva = c["score"] - (NOVIDADE_PONTOS if c["chave"] in repetidos else 0)
        oficial = OFICIAL_PONTOS if c["chave"] in oficiais else 0
        return (efetiva < LIMITE_BOA, c["chave"] in de_ontem, -(efetiva + oficial), -c["ts"])
    return sorted(cards, key=ordem)


def marca(cur: Curadora, produto: str) -> str:
    return cur.ident.analisar(produto).marca or ""


ID_ML = re.compile(r"MLB-?(\d{6,})")
_ids_por_link: dict[str, set] = {}   # envios antigos, de antes de o registro guardar os ids do anúncio


def ids_do_card(card) -> set[str]:
    """Os ids do anúncio no ML (item e produto de catálogo): a identidade que não depende de como o grupo escreve."""
    ids = {str(x).replace("-", "") for x in (card.item_id, card.produto_id) if x}
    return ids | {"MLB" + n for n in ID_ML.findall(card.url_produto or "")}


def anuncios_recentes(con, cur: Curadora) -> dict[str, float]:
    """Anúncios do ML mandados pelo pipeline ou postados no GrupoFinal há menos de 8h, com o menor preço em que foram
    (0 = preço desconhecido). 10/10: o pipeline ia mandar o Bareeq de novo porque outro grupo escreveu "Bareeq"
    em vez de "Bareeq Al Dhahab" (mesmo anúncio, outro nome)."""
    desde = time.time() - _janela_repetir
    ids: dict[str, float] = {}

    def junta(novos, preco):
        for i in novos:
            ids[i] = min(ids[i], preco or 0) if i in ids else (preco or 0)

    if REGISTRO.exists():
        for linha in REGISTRO.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            if r.get("ts", 0) < desde or r.get("status") != "enviado":
                continue
            if r.get("ml_ids"):
                junta(r["ml_ids"], r.get("preco"))
            elif r.get("link_origem") and cur.ml is not None:
                link = r["link_origem"]
                if link not in _ids_por_link:
                    card = cur.ml.consultar(link)
                    _ids_por_link[link] = ids_do_card(card) if card.ok else set()
                junta(_ids_por_link[link], r.get("preco"))
    enviados = enviados_pelo_pipeline(REGISTRO, desde - VEIO_DO_PIPELINE_S)
    for dados, preco, ts, chave in con.execute("SELECT dados, preco, ts, chave FROM postagens WHERE ts >= ? "
                                               "AND dados IS NOT NULL", (desde,)):
        if time.time() - ts > REPETIR_EQUIPE_S and post_da_equipe(chave, ts, enviados):
            continue   # 21/10: post da equipe há mais de 6h já pode voltar
        try:
            url = ((json.loads(dados).get("ml") or {}).get("url")) or ""
        except (ValueError, TypeError, AttributeError):
            continue
        junta({"MLB" + n for n in ID_ML.findall(url)}, preco)
    return ids


def vendedor_duvidoso(card) -> str:
    """O motivo para não mandar por causa de quem vende ("" se é loja oficial ou vendedor grande; ver VENDAS_MINIMAS)."""
    return travas.vendedor_fraco(card.loja_oficial, card.reputacao)   # 21/10: a mesma conta que o painel mostra


def lojas_oficiais(con, chaves: set[str], desde: float) -> set[str]:
    """Produtos cuja conferência mais recente no ML (desde `desde`) mostrou loja oficial: passam na frente na fila."""
    ultimo: dict[str, bool] = {}
    lista = sorted(chaves)
    for i in range(0, len(lista), 400):
        lote = lista[i:i + 400]
        for chave, oficial in con.execute(
                "SELECT chave, json_extract(dados, '$.ml.oficial') FROM avaliacoes WHERE ts >= ? AND chave IN "
                f"({','.join('?' * len(lote))}) AND json_extract(dados, '$.ml.oficial') IS NOT NULL ORDER BY ts, id",
                (desde, *lote)):
            ultimo[chave] = bool(oficial)
    return {k for k, v in ultimo.items() if v}


def ressalva_da_curadora(av) -> str:
    """O motivo, se a própria curadora poria ressalva nesta promo ao chegar na normalizadora ("" se não).

    11/10: a regra "Natura e Boticário na frente" puxou duas Naturas de R$130 com nota média, e as duas saíram
    com ressalva da curadora: "loja de 1 produto (só vista vendendo este perfume)" e "25% abaixo de todos os outros
    46 vendedores sem vendedor confiável: confira se é original". O Breno não postou a de 10/10. Promo que a
    curadora já sabe que vai marcar não vai pelo pipeline: continua no painel para quem quiser olhar."""
    if getattr(av, "conflito", ""):
        return f"link aponta para outro produto: {av.conflito}"
    if av.risco:
        return av.risco_motivo or "vendedor de risco"
    if av.produtos_vendedor == 1 and not av.loja_selo:
        return "loja de 1 produto (vendedor só visto vendendo este perfume)"
    return ""


def conferir(con, cur: Curadora, card: dict):
    """Abre no ML agora. Devolve (avaliação, dados) se continua boa o bastante; senão None."""
    # promo achada pelo cupom quente: vale o texto do post que trazia o cupom, não o do card do painel
    dados = {"texto": card["texto_quente"]} if card.get("texto_quente") else painel._completar(con, card)
    texto = dados.get("texto") or ""
    conferir.motivo = ""   # 11/10: sem isso o log repetia o motivo da promo anterior
    if not texto or not LINK.search(texto):
        conferir.motivo = "sem link do Mercado Livre (promo de outra loja)"
        return None
    av = cur.avaliar(texto, agora=int(time.time()), conferir_ml=True)
    if av is None or not (av.card and av.card.ok):
        conferir.motivo = "não abriu no ML"
        return None
    novo = para_dict(av)
    novo.update(reconferida_de=card["id"], preco_antes=card["preco"], nota_antes=card["nota"])
    registrar_avaliacao(con, int(time.time()), "reconferida", card.get("grupo", ""), av.identidade.chave, av.nota,
                        av.score, False, novo)  # o painel passa a mostrar o preço de agora
    if av.card.internacional:
        conferir.motivo = "compra internacional"
        return None  # 09/10 (Breno): compra internacional não pode ser postada
    if vendedor_duvidoso(av.card):
        conferir.motivo = vendedor_duvidoso(av.card)
        return None  # 18/10 (Breno): vendedor com poucas vendas não vai (ver VENDAS_MINIMAS)
    if not parece_perfume(av.card.titulo or av.promo.produto):
        # 10/10: foram um kit de shampoo e uma loção "sem perfume"; o nome do grupo era curto, o do anúncio diz o que é
        conferir.motivo = f"não é perfume ({(av.card.titulo or '')[:40]})"
        return None
    if ressalva_da_curadora(av):
        conferir.motivo = f"sairia com ressalva da curadora: {ressalva_da_curadora(av)[:80]}"
        return None
    # Já foi há menos de 8h (o produto, ou o mesmo anúncio do ML com outro nome)? Só volta se, conferido agora,
    # estiver 8% ou mais abaixo do preço em que foi.
    vistos = anuncios_recentes(con, cur)
    foi_por = [vistos[i] for i in ids_do_card(av.card) if i in vistos]
    ultimo = ultimos_posts(con, time.time(), REGISTRO).get(av.identidade.chave)
    if ultimo and time.time() - ultimo[1] < _janela_repetir:
        foi_por.append(ultimo[0])
    if foi_por:
        # Com cupom da equipe valendo, o preço de agora é o com o cupom: o que foi de manhã sem cupom pode voltar.
        cupom = None if av.cupom_quente else cur.cupom_da_equipe(av.card.preco, card=av.card, so_da_equipe=True)
        agora_sai = min(av.preco_efetivo, cupom["preco"]) if cupom else av.preco_efetivo
        if not agora_sai <= min(foi_por) * (1 - MAIS_BARATO):
            conferir.motivo = f"já foi há menos de 8h a R${min(foi_por):.2f} e agora está R${agora_sai:.2f}"
            return None
        if cupom and not av.preco_efetivo <= min(foi_por) * (1 - MAIS_BARATO):
            dados["_so_com_cupom"] = True   # só volta porque o cupom baixou o preço: sem ele, não vai
        log(f"   já foi há menos de 8h a R${min(foi_por):.2f}; agora R${agora_sai:.2f} "
            f"({(1 - agora_sai / min(foi_por)) * 100:.0f}% mais barato): pode ir de novo")
    aceita = aceitavel(av)
    if not aceita and not av.cupom_quente and cur.cupom_da_equipe(av.card.preco, card=av.card, so_da_equipe=True):
        dados["_so_com_cupom"] = True   # sem o cupom não vale; uma_promo refaz com ele e só manda se ficar Boa
        conferir.motivo = ""
        return av, dados
    conferir.motivo = "" if aceita else f"conferida no ML, a nota caiu para {av.nota} {av.score} (R${av.preco_efetivo:.2f})"
    return (av, dados) if aceita else None


conferir.motivo = ""


def _links_do_catalogo() -> dict:
    try:
        return json.loads(LINKS_DO_CATALOGO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar_link_do_catalogo(produto_id: str, link: str | None) -> None:
    guardados = _links_do_catalogo()
    if link:
        guardados[produto_id] = {"link": link, "ts": int(time.time())}
    else:
        guardados.pop(produto_id, None)
    tmp = LINKS_DO_CATALOGO.with_suffix(".tmp")
    tmp.write_text(json.dumps(guardados, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LINKS_DO_CATALOGO)


GERADOR_FALHOU_S = 6 * 3600   # 14/10 (Breno: "ele tá insistindo no erro, esse link não vale mais")


def _guardar_falha_do_gerador(produto_id: str) -> None:
    guardados = _links_do_catalogo()
    guardados[produto_id] = {"falhou": int(time.time())}
    tmp = LINKS_DO_CATALOGO.with_suffix(".tmp")
    tmp.write_text(json.dumps(guardados, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LINKS_DO_CATALOGO)


def link_da_pagina(cur: Curadora, produto_id: str, texto: str, simular: bool = False) -> str:
    """O link de afiliado (meli.la) da página de catálogo `produto_id`. Pede ao Gerador uma vez e guarda por 7 dias:
    o link é da página, e a página é que muda de preço."""
    guardado = _links_do_catalogo().get(produto_id) or {}
    if guardado.get("link") and time.time() - guardado.get("ts", 0) < LINK_DO_CATALOGO_S:
        return guardado["link"]
    if time.time() - guardado.get("falhou", 0) < GERADOR_FALHOU_S:
        return ""   # 14/10: o Gerador já deu erro com esta página há pouco; não insiste
    if simular:
        return ""
    # 25/10: a API devolve o mesmo link da página que o BotGerador (3 de 3); ele fica de reserva aqui também
    resposta = gerar_pela_api(texto)
    if resposta is not None:
        log(f"-> API do gerador (só o link da página {produto_id}): {resposta['link']}")
        _guardar_link_do_catalogo(produto_id, resposta["link"])
        return resposta["link"]
    inicio = TAP.stat().st_size
    enviar_ao_gerador(texto)
    log(f"-> Gerador (só o link da página {produto_id}): {texto.strip().splitlines()[0][:50]}")
    resposta = esperar_resposta(cur, texto.strip().splitlines()[0], inicio, ESPERA_GERADOR_S)
    if resposta is None:
        _guardar_falha_do_gerador(produto_id)
        return ""
    link = LINK.search(resposta.get("body") or "").group(0)
    _guardar_link_do_catalogo(produto_id, link)
    return link


def pela_pagina(con, cur: Curadora, card: dict, simular: bool = False) -> dict | None:
    """Card achado pelo radar: o link dele é a página de catálogo (/p/MLB...), que o ML não deixa abrir direto.
    Troca pelo link de afiliado da mesma página, abre e refaz o texto com o preço que o cliente vê lá.

    11/10: o Fakhar Platin e o The Kingdom (queridinhos) eram pulados a manhã toda com "não abriu no ML". E o
    preço da API não serve para o post: no Club de Nuit Intense a API dava R$199 no anúncio que a página vende
    a R$183,08. Devolve o card pronto para a conferência normal, o mesmo card se não é de catálogo, ou None."""
    if card.get("texto_quente"):
        return card
    completo = painel._completar(con, card)
    texto = completo.get("texto") or ""
    achado = CATALOGO.search(texto)
    if not achado:
        return card
    if completo.get("risco") or (completo.get("pct_abaixo") or 0) >= 50:
        # 14/10: Attracione Men a R$24,50 (58% abaixo, vendedor fraco) foi pedido ao Gerador duas vezes na rodada de
        # cupom (queridinho vai primeiro). Promo do radar com risco nem pede link: não iria mesmo.
        conferir.motivo = "radar com risco ou preço bom demais: nem pedi o link ao Gerador"
        return None
    produto_id = achado.group(1)
    link = link_da_pagina(cur, produto_id, texto, simular)
    if not link:
        conferir.motivo = "é do radar e ainda não tem o link da página" if simular else "o Gerador não deu o link da página"
        return None
    pagina = cur.ml.consultar(link, int(time.time()))
    if not (pagina.ok and pagina.preco):
        _guardar_link_do_catalogo(produto_id, None)   # link que não abre mais: pede outro na próxima
        conferir.motivo = "não abriu no ML"
        return None
    preco = f"R$ {pagina.preco:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    nome = (pagina.titulo or texto.strip().splitlines()[0]).strip()
    return dict(card, texto_quente=f"{nome}\nPor {preco}\n{link}")


RECUSADA_S = 30 * 60        # promo recusada na conferência não é conferida de novo por 30 min
_recusadas: dict[str, float] = {}


def cupons_que_ficam(cur: Curadora, av) -> list[str]:
    """Cupons que ficam no texto: vistos nos grupos na última hora, não esgotados/vencidos e com regra conhecida
    que dá o preço anunciado (±3%). 09/10: nos 3 posts do pipeline com cupom que foram postados, o Breno tirou
    o cupom (os textos tinham horas, e cupom some dos grupos em ~1h30)."""
    if av.cupom_quente:
        return [av.cupom_quente["codigo"]]   # 10/10: confirmado pela equipe há pouco e a regra dá desconto aqui
    ml = av.card.preco if av.card and av.card.ok else None
    fora = cur.cupons_fora_da_loja(av.card)   # o Breno tirou de um post dessa loja: aqui o cupom não vale
    ficam = []
    for codigo in av.promo.cupons:
        if codigo in av.cupons_mortos or codigo in fora or not ml:
            continue
        r = cur.con.execute("SELECT ultimo_ts FROM cupons WHERE codigo = ?", (codigo,)).fetchone()
        if not r or time.time() - r[0] > CUPOM_FRESCO_S:
            continue
        calculado = cur._preco_com_cupom_conhecido([codigo], ml)
        if calculado is not None and abs(calculado - av.promo.preco) <= av.promo.preco * 0.03:
            ficam.append(codigo)
    return ficam


POR_NO_TEXTO = re.compile(r"(?i)\bpor\s*:?\s*[*_~]*\s*R\$\s*[*_~]*\s*(\d[\d.]*(?:,\d{1,2})?)")


def preco_no_texto(corpo: str) -> float | None:
    """31/10: o preço "por R$" do texto que vai para a normalizadora (o que foi de fato enviado)."""
    m = POR_NO_TEXTO.search(corpo or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def preco_do_texto(cur: Curadora, av, ficam: list, tirar: set) -> float | None:
    """O preço que o texto ao Gerador mostra quando não há cupom quente (None = fica o do grupo).

    Cupom saiu e nenhum ficou: o preço do ML. 31/10 (revisão): com o cupom do grupo que fica, o texto nunca mostra
    menos do que a conta da regra dele (grupo R$180, regra R$184: R$184). Os 3% de folga servem para reconhecer a
    oferta, não para publicar abaixo da conta (24-27/10: 3 de 69 envios com cupom saíram até R$0,92 abaixo). A nota
    não muda: ela compara o número do grupo com o histórico do grupo."""
    if tirar and not ficam:
        return av.card.preco
    calcular = getattr(cur, "_preco_com_cupom_conhecido", None)
    if ficam and calcular and av.card is not None and getattr(av.card, "ok", False) and av.card.preco:
        conta = calcular(list(ficam), float(av.card.preco))
        if conta is not None and conta > av.promo.preco + 0.005:
            return conta
    return None


def texto_para_o_gerador(texto: str, tirar: set, preco_ml: float | None = None,
                         preco_antigo: float | None = None, por: str | None = None) -> str:
    """O texto do grupo de origem sem as linhas dos cupons que saem. Com `preco_ml` (o preço certo de agora: o do
    ML sem cupom quando o cupom sai, ou o do ML menos o cupom quente), o "por" vira esse preço, inclusive no
    título ("ZAAD DE 335 POR 211")."""
    linhas = []
    for linha in texto.splitlines():
        limpa = linha.upper()
        if any(c in limpa for c in tirar) and ("CUPO" in limpa or "🎟" in linha) and "R$" not in limpa:
            continue
        linhas.append(linha)
    novo = "\n".join(linhas).strip()
    if preco_ml:
        valor = f"R$ {preco_ml:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        # 21/10 (Breno: "por que essas 2 foram sem o cupom?"): "Por: R$*168,00*" (o negrito entre o R$ e o número)
        # não era trocado; o preço com o cupom não entrava no texto e o K Max foi sem o CAMISA10.
        novo = re.sub(r"(?i)(\bpor\s*:?\s*\*?\s*)R\$\s*([*_~]*)\s*[\d.]+(?:,\d{1,2})?",
                      lambda m: m.group(1) + m.group(2) + valor, novo, count=1)
        if preco_antigo and not re.search(r"(?i)\bpor\s*:?\s*\*?\s*R\$", novo):
            # 21/10: sem "por" antes do preço ("✅ R$ 162", Qaed Al Fursan Untamed) troca o R$ do preço antigo
            novo = re.sub(r"R\$\s*([*_~]*)\s*" + re.escape(f"{preco_antigo:.0f}") + r"(?:,\d{1,2})?(?!\d)",
                          lambda m: m.group(1) + valor, novo, count=1)
        if preco_antigo:
            antigo = f"{preco_antigo:.0f}"
            novo = re.sub(r"(?i)(\bpor\s+)" + re.escape(antigo) + r"(?:,\d{2})?\b", lambda m: m.group(1) + f"{preco_ml:.0f}",
                          novo)
    if por and por.upper() not in novo.upper():
        # Cupom que o Breno está pondo à mão: entra numa linha própria, logo antes do link.
        ls = novo.splitlines()
        i = next((n for n, l in enumerate(ls) if "http" in l.lower()), len(ls))
        while i > 0 and not ls[i - 1].strip():
            i -= 1
        ls.insert(i, f"🎟️ Cupom: {por}")
        novo = "\n".join(ls)
    return novo


ANUNCIO = "https://produto.mercadolivre.com.br/MLB-{}"


def com_link_do_anuncio(texto: str, card) -> str:
    """19/10 (Breno: "não acho que a culpa seja dele, acho que o ML troca sessões"): o link do grupo abre a PÁGINA do
    produto, e o ML escolhe a loja a cada sessão — no mesmo minuto a página do Wazeer mostrou R$149,90, R$138,99 e
    R$149,90 de novo, e a normalizadora e a curadora viam lojas diferentes (❌ "curadora vê outro preço"; e no Maleka
    o post dizia uma loja e o cliente caía em outra). Recebendo o link do ANÚNCIO, o Gerador devolve um link preso
    nessa loja (teste de 19/10: 3 de 3 aberturas na Lipx). Vai o anúncio que a curadora conferiu."""
    achado = re.fullmatch(r"MLB-?(\d+)", str(getattr(card, "item_id", "") or ""))
    if not (card is not None and getattr(card, "ok", False) and achado):
        return texto
    return LINK.sub(ANUNCIO.format(achado.group(1)), texto)


def _numero_do_anuncio(item_id) -> str:
    return re.sub(r"\D", "", str(item_id or ""))


def _abrir_oferta(cur: Curadora, o: dict, texto: str):
    """(card, resposta do Gerador) do anúncio `o` visto agora. O consultor do ML lê a página social do link de
    afiliado (bloco "card-featured"); a página /p/ ou o anúncio direto não têm esse bloco (por isso pela_pagina pede o
    link ao Gerador antes). Aqui também: o link de afiliado do ANÚNCIO pela API do gerador (que prende a loja, 19/10),
    sem o BotGerador (esperar minutos por uma conferência atrasaria o envio).
    01/11 (revisão): sem esse link, (None, None) e o ML nem é consultado (a página /p/...?pdp_filters não tem o card em
    destaque); a resposta volta junto porque o link enviado tem de ser o mesmo que foi conferido."""
    resposta = gerar_pela_api(LINK.sub(ANUNCIO.format(_numero_do_anuncio(o["item_id"])), texto))
    if resposta is None or not resposta.get("link"):
        return None, None
    return cur.ml.consultar(resposta["link"], int(time.time())), resposta


def pela_loja_oficial(con, cur: Curadora, av, texto: str, sem_cupom: bool, simular: bool = False):
    """31/10 (Breno: "loja oficial vence revenda"). Malbec Bleu, 16:58: foi a revenda PASSOSBARBARA a R$180, e no MESMO
    produto do ML a loja oficial O Boticário vendia a R$167,93. Revenda um pouco mais barata não tira o lugar da
    oficial; revenda com a oficial igual ou mais barata ao lado é erro.

    Quando a curadora achou no catálogo uma loja oficial igual ou mais barata (av.oferta_oficial), abre agora o anúncio
    dela com o mesmo consultor do ML e avalia de novo. Vai ELA (link, preço do texto e registro) só se: abriu no mesmo
    anúncio e no mesmo produto do catálogo; o selo de loja oficial aparece na página E a conta tem pelo menos 5 mil
    vendas (o selo só da API não basta: 21/10, PERFUMELATTAFA7; sem as vendas conferidas, não troca); o produto é o
    mesmo (sem conflito); o preço da página é igual ou menor que o da revenda (página com página); e a nota continua
    boa. Com cupom no texto não troca: o cupom é da loja da revenda. Qualquer passo que falhe: vai a original e o log
    diz por quê. Devolve (avaliação, texto, resposta do Gerador) da oficial, ou None.

    01/11 (revisão): só confere pelo link de afiliado do anúncio dela (sem a API do gerador, não troca), e esse link
    conferido é o que vai (uma chamada a menos ao gerador); o anúncio dela também respeita as 8 h sem repetir; com
    cupom no texto da avaliação dela, não troca. Em --simular só diz o que faria, sem abrir nada."""
    o = getattr(av, "oferta_oficial", None)
    if not o or not (av.card is not None and av.card.ok and av.card.preco):
        return None
    from curadora.radar import OFICIAL_API_MIN_VENDAS
    quem = o.get("nome") or f"anúncio {o.get('item_id')}"
    revenda = av.card.preco

    def fica(motivo: str):
        log(f"   loja oficial ({quem}, R${o['preco']:.2f} na API) não entrou: {motivo}; vai a revenda a R${revenda:.2f}")
        return None
    if not sem_cupom:
        return fica("o texto vai com cupom, e o cupom é da loja da revenda")
    if not o.get("item_id"):
        return fica("sem o número do anúncio dela")
    if simular:   # 01/11: a simulação não pede link ao gerador nem abre o ML
        log(f"   [simulação] trocaria pela oficial {quem} a R${o['preco']:.2f} (não conferido)")
        return None
    try:
        card, resposta = _abrir_oferta(cur, o, texto)
    except sqlite3.OperationalError:
        raise   # 01/11: banco travado sobe para o rodar() fazer o rollback
    except Exception as exc:   # a conferência é um extra: nunca derruba o envio da original
        return fica(f"erro ao abrir no ML ({type(exc).__name__})")
    if resposta is None:
        return fica("sem o link de afiliado do anúncio dela")
    if not (card is not None and card.ok and card.preco):
        return fica("não abriu no ML")
    if _numero_do_anuncio(card.item_id) != _numero_do_anuncio(o["item_id"]):
        return fica(f"a página abriu outro anúncio ({card.item_id or 'sem número'})")
    if o.get("produto_id") and card.produto_id and card.produto_id != o["produto_id"]:
        return fica(f"a página é de outro produto do catálogo ({card.produto_id})")
    if card.preco > revenda + 0.005:
        return fica(f"na página está R${card.preco:.2f}, mais que a revenda")
    # 01/11: a mesma trava de 8 h que conferir põe na revenda, agora com os ids do anúncio dela
    vistos = anuncios_recentes(con, cur)
    foi_por = [vistos[i] for i in ids_do_card(card) if i in vistos]
    if foi_por and not card.preco <= min(foi_por) * (1 - MAIS_BARATO):
        return fica(f"o anúncio dela já foi há menos de 8h a R${min(foi_por):.2f}")
    # o link do anúncio da oficial (com_link_do_anuncio põe o mesmo antes do Gerador) e o preço da página dela
    novo = texto_para_o_gerador(LINK.sub(ANUNCIO.format(_numero_do_anuncio(card.item_id)), texto), set(), card.preco,
                                av.promo.preco)
    try:
        av2 = cur.avaliar(novo, agora=int(time.time()), conferir_ml=True, card=card)
    except sqlite3.OperationalError:
        raise   # 01/11: idem, o rollback é do rodar()
    except Exception as exc:
        return fica(f"erro na avaliação ({type(exc).__name__})")
    if av2 is None or av2.card is not card:
        return fica("o texto com o link dela não foi lido")
    if av2.conflito or av2.identidade.chave != av.identidade.chave:
        return fica(f"outro produto ({av2.conflito or av2.identidade.chave})")
    if av2.cupom_quente or av2.promo.cupons:
        return fica("o texto ainda tem cupom")   # 01/11: o cupom seria da loja da revenda
    if not card.loja_oficial:
        return fica("a página não mostra o selo de loja oficial")
    vendas = (card.reputacao or {}).get("vendas")
    if vendas is None or vendas < OFICIAL_API_MIN_VENDAS:
        return fica("selo só pela API e vendas não conferidas" if vendas is None else f"só {vendas} vendas")
    if card.internacional:
        return fica("compra internacional")
    if ressalva_da_curadora(av2) or not aceitavel(av2):
        return fica(ressalva_da_curadora(av2)[:60] or f"a nota dela ficou {av2.nota} {av2.score}")
    av2.no_lugar_de = {"item_id": av.card.item_id, "vendedor": av.card.vendedor, "preco": revenda}
    log(f"   LOJA OFICIAL no lugar da revenda: {card.vendedor or quem} a R${card.preco:.2f} "
        f"(a revenda {av.card.vendedor or av.card.item_id} estava a R${revenda:.2f})")
    # 01/11: vai o link de afiliado que foi conferido, com o texto dela (o gerador só troca o link)
    link = resposta["link"]
    return av2, novo, dict(resposta, body=LINK.sub(lambda _m: link, novo))


def precos_do_envio(cur: Curadora, av) -> dict:
    """03/11 (relatório da Astra): o que a conta do envio usou, para conferir depois a ordem cupom x Pix sem misturar
    momentos. "preco_card" é o destaque do card (com o Pix, quando a loja dá desconto no Pix), "preco_outros_meios"
    o "ou R$ X em outros meios", "preco_cupom_ml" o "com cupom" que o próprio card mostra, e "regras_cupom" a regra
    de cada cupom naquele momento: o da equipe ("quente") e os que vieram no texto da oferta ("oferta"; com o
    motivo, quando a curadora o tirou). Não muda a conta."""
    card = av.card if av.card and av.card.ok else None
    saida = {}
    if card and card.preco:
        saida["preco_card"] = card.preco
        if card.preco_outros:
            saida["preco_outros_meios"] = card.preco_outros
        if card.preco_cupom:
            saida["preco_cupom_ml"] = card.preco_cupom
    quente = (getattr(av, "cupom_quente", None) or {}).get("codigo")
    mortos = getattr(av, "cupons_mortos", None) or {}
    codigos = [(quente, "quente")] if quente else []
    codigos += [(c, "oferta") for c in (getattr(getattr(av, "promo", None), "cupons", None) or []) if c != quente]
    regras = []
    for codigo, origem in codigos:
        r = cur.con.execute("SELECT pct, valor, minimo, limite FROM cupons WHERE codigo = ?", (codigo,)).fetchone()
        regra = {"codigo": codigo, "origem": origem, "pct": r[0], "valor": r[1], "minimo": r[2], "limite": r[3]} if r \
            else {"codigo": codigo, "origem": origem}
        if codigo in mortos:
            regra["tirado"] = str(mortos[codigo])[:120]
        regras.append(regra)
    if regras:
        saida["regras_cupom"] = regras
    return saida


def com_cupom_da_equipe(cur: Curadora, av, texto: str):
    """10/10, 17h30: o Breno pôs um cupom à mão em vários posts seguidos (MODA1010) e refez o preço de cada um.
    Enquanto ele não tirar, a promo já vai com esse cupom e com o preço do ML menos o cupom. Não muda a ordem da
    fila: a escolha continua sendo pela nota. Devolve (avaliação, texto) ou None se não se aplica."""
    ml = av.card.preco if av.card and av.card.ok else None
    cupom = cur.cupom_da_equipe(ml, card=av.card)
    if not cupom:
        fora = (cur.cupons_fora_da_loja(av.card) & cupons_da_equipe_agora(cur)) if ml else set()
        if fora:
            log(f"   cupom {', '.join(sorted(fora))} não vale nessa loja (você tirou de um post dela): vai sem ele")
        return None
    novo = texto_para_o_gerador(texto, set(av.promo.cupons) - {cupom["codigo"]}, cupom["preco"], av.promo.preco,
                                por=cupom["codigo"])
    # 21/10: com o MESMO anúncio já conferido (card). Reabrir o link do grupo, que era página de catálogo, mostrava
    # outra loja e outro preço, a conta do cupom não fechava e o Durrat Al Aroos foi sem o CAMISA10.
    av2 = cur.avaliar(novo, agora=int(time.time()), conferir_ml=True, card=av.card)
    # 21/10 (Breno: "não tá mandando os cupons em vários anúncios"): com o cupom, a nota da curadora é medida contra o
    # histórico de preços COM cupom (mais baixo) e pode cair (Bareeq: Excelente sem, Não vale 42 com), mas o preço para
    # quem compra é melhor. A promo que já ia sem o cupom vai com ele e fica com a nota de sem o cupom.
    ja_ia = aceitavel(av)
    if (av2 is None or not av2.cupom_quente or abs(av2.promo.preco - cupom["preco"]) > 0.05
            or not (aceitavel(av2) or ja_ia)):
        # não deu para pôr o preço novo no texto, ou com ele a nota não fica boa: vai como sempre, e o log diz por quê
        motivo = ("o texto não foi lido" if av2 is None else "o cupom não valeu no texto" if not av2.cupom_quente
                  else f"o preço no texto ficou R${av2.promo.preco:.2f}" if abs(av2.promo.preco - cupom["preco"]) > 0.05
                  else f"com ele a nota fica {av2.nota} {av2.score}")
        log(f"   cupom da equipe {cupom['codigo']} não entrou ({motivo}): vai sem ele")
        return None
    if not aceitavel(av2):
        log(f"   com o cupom a nota cairia para {av2.nota} {av2.score} (comparada com os preços com cupom); "
            f"fica a de sem ele, {av.nota} {av.score}")
        av2.nota, av2.score = av.nota, av.score
    log(f"   cupom da equipe {cupom['codigo']} ({cupom['por']} às {cupom['hora']}): "
        f"R${ml:.2f} no ML, vai a R${cupom['preco']:.2f} com ele")
    return av2, novo


EDICAO_ATIVA_S = 20 * 60   # mensagem no grupo de Edição nos últimos 20 min = o Breno está trabalhando nas promos


def equipe_postando(con, cur: Curadora | None = None) -> bool:
    """A equipe está na ativa: alguém postou no GrupoFinal nos últimos 25 min, OU o Breno mexeu no grupo de Edição nos
    últimos 20 min.

    11/10 (Breno, 09:00: "chegou 3 e parou?"): no começo do turno ninguém postou no GrupoFinal ainda, e do envio ao
    post no GrupoFinal passam ~43 min; olhando só o GrupoFinal, o pipeline parava em 3 justo quando ele chegava. A edição
    dele acontece em ~4 min, então é o sinal de que ele está ali."""
    if con.execute("SELECT 1 FROM postagens WHERE ts >= ? LIMIT 1", (time.time() - FILA_JANELA_S,)).fetchone():
        return True
    grupo = str(((cur.cfg.get("conferente") or {}) if cur is not None else {}).get("grupo_edicao") or "").split("@")[0]
    if not grupo or not TAP.exists():
        return False
    desde = time.time() - EDICAO_ATIVA_S
    with TAP.open("rb") as f:
        f.seek(max(0, TAP.stat().st_size - 1_500_000))
        f.readline()
        for bruto in f:
            if grupo.encode() not in bruto:
                continue
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if str(r.get("chatId") or "").split("@")[0] == grupo and (epoch(r.get("timestamp")) or 0) >= desde:
                return True
    return False


def na_fila(con) -> int:
    """Quantas promos o pipeline mandou nos últimos 25 min que ainda não foram postadas no GrupoFinal.

    10/10 (Breno: "preciso dele rodando e não chegou promo nova"): com a equipe postando, o pipeline NÃO espera a
    fila andar: manda no ritmo, e ele escolhe o que postar (esperar a fila deixava o Breno sem opção nova quando
    não queria postar uma das que estavam lá). A fila só segura quando ninguém está postando (equipe ausente),
    para não empilhar promo com preço envelhecendo."""
    desde = time.time() - FILA_JANELA_S
    if not REGISTRO.exists():
        return 0
    n = 0
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines()[-80:]:
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "enviado" and r.get("ts", 0) >= desde and not con.execute(
                "SELECT 1 FROM postagens WHERE ts >= ? AND (chave = ? OR link = ?)",
                (r["ts"], r.get("chave"), (r.get("link_afiliado") or "-").split("?")[0])).fetchone():
            n += 1
    return n


def enviar_ao_gerador(texto: str) -> None:
    req = urllib.request.Request(PONTE, data=json.dumps({"chatId": GERADOR, "message": texto}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


_api_sem_chave_avisado = False
VENDEDOR_NO_TEXTO = re.compile(r"(?i)vendido\s+por|selecione\s+(?:a\s+loja|o\s+vendedor|vendedor)|verificado\s+pelo")


def gerar_pela_api(texto: str) -> dict | None:
    """O texto com o link de afiliado, pela API do gerador: {"body", "link"}. None (aí vai pelo BotGerador): sem a
    chave, API fora, ou resposta em que sobrou link que não é nosso ou veio mais de um link. A API é o mesmo gerador
    do BotGerador (24/10: o mesmo meli.la em 59 de 59 promos), mas só troca os links: o texto volta como o do grupo."""
    global _api_sem_chave_avisado
    try:
        conf = json.loads(GERADOR_API.read_text(encoding="utf-8"))
        url, token = str(conf.get("url") or ""), str(conf.get("token") or "")
        if not (url.startswith("https://") and token):
            raise ValueError("arquivo da chave sem url https ou sem token")
    except (OSError, ValueError, AttributeError) as exc:
        if not _api_sem_chave_avisado:
            log(f"   sem a chave da API do gerador ({type(exc).__name__}): o link vai pelo WhatsApp")
            _api_sem_chave_avisado = True
        return None
    pedido = json.dumps({"generatorSlug": str(conf.get("slug") or "gerador-grupofinal-perfumes-api"), "text": texto}).encode()
    for tentativa in (1, 2):
        try:
            # 24/10: sem User-Agent próprio o Cloudflare da API recusa o Python (403, erro 1010)
            req = urllib.request.Request(url, data=pedido, method="POST", headers={
                "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "curadora-pipeline/1.0",
                "Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=GERADOR_API_S) as resp:
                corpo = str(json.loads(resp.read().decode("utf-8")).get("processedText") or "")
        except Exception as exc:  # rede, timeout, 4xx/5xx, resposta que não é JSON
            codigo = getattr(exc, "code", None)
            log(f"   API do gerador falhou ({type(exc).__name__} {codigo or ''}), tentativa {tentativa}")
            if isinstance(codigo, int) and 400 <= codigo < 500 and codigo != 429:
                return None   # chave errada, recusa: tentar de novo não muda
            if tentativa == 1:
                # 24/10 (Breno: "não exagere na cadência nem precisa esperar tanto"): 1 nova tentativa, 3 s depois;
                # no limite de taxa, o que a API pedir, até 5 s. Depois disso, essa promo vai pelo WhatsApp.
                espera = 3.0
                if codigo == 429:
                    try:
                        espera = float((getattr(exc, "headers", None) or {}).get("Retry-After") or 5)
                    except (TypeError, ValueError):
                        espera = 5.0
                time.sleep(max(0.0, min(5.0, espera)))
                continue
            return None
        nossos = {chave_link(m.group(0)) for m in MELI.finditer(texto)}
        links = re.findall(r"https?://[^\s*_~)]+", corpo)
        codigos = {chave_link(l) for l in links}
        if links and all(MELI.fullmatch(l) for l in links) and len(codigos) == 1 and not (codigos & nossos):
            return {"body": corpo, "link": links[0]}
        log("   API do gerador: a resposta não veio com um link só, nosso: vai pelo WhatsApp")
        return None
    return None


def texto_da_api(corpo: str, produto: str) -> str:
    """O que o BotGerador arrumava e a API não: o nome do produto na 1ª linha (a normalizadora usa a 1ª linha como nome
    quando não abre o anúncio) e sem "Vendido por" / "Selecione a loja" (o link é de um anúncio que a curadora
    conferiu, que pode ser de outra loja: o erro de 19/10)."""
    linhas = [l for l in corpo.split("\n") if not VENDEDOR_NO_TEXTO.search(l)]
    cheias = [i for i, l in enumerate(linhas) if _sem_enfeite(l)]
    if cheias and not _mesmo_nome(_sem_enfeite(linhas[cheias[0]]), produto):
        do_produto = next((i for i in cheias[1:4] if _mesmo_nome(_sem_enfeite(linhas[i]), produto)), None)
        if do_produto is not None:
            linhas = [linhas[do_produto], ""] + [l for i, l in enumerate(linhas) if i != do_produto]
        else:
            linhas = [f"*{produto}*", ""] + linhas
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def mesma_promo(cur: Curadora, produto_enviado: str, corpo_resposta: str) -> bool:
    p = ler_promo(corpo_resposta, cur.cfg["marcas"])
    if not p.valida:
        return False
    # 18/10: "Perfume Árabe Mawwal Rayhan EDP 100ml" só tem marca (Mawwal; "Rayhan" vira a marca Rayhaan) e palavra
    # genérica, fica sem identidade, e a resposta do Gerador nunca era reconhecida: 7 respostas perdidas das 17:35 às
    # 17:58, 4 min de espera cada. O mesmo nome, escrito igual, é a mesma promo.
    simples = lambda s: " ".join(re.sub(r"[^a-z0-9]+", " ", sem_acento((s or "").lower())).split())
    if simples(produto_enviado) and simples(produto_enviado) == simples(p.produto):
        return True
    a, b = cur.ident.analisar(produto_enviado), cur.ident.analisar(p.produto)
    if a.base and a.base == b.base:
        return True
    comum = len(a.tokens & b.tokens) / max(1, len(a.tokens | b.tokens))
    return comum >= 0.5 and (not a.volume or not b.volume or a.volume == b.volume)


def esperar_resposta(cur: Curadora, produto: str, desde: int, prazo_s: float) -> dict | None:
    fim = time.time() + prazo_s
    pos = desde
    while time.time() < fim:
        time.sleep(3)
        with TAP.open("rb") as f:
            f.seek(pos)
            for bruto in f:
                if not bruto.endswith(b"\n"):
                    break
                pos += len(bruto)
                try:
                    r = json.loads(bruto)
                except ValueError:
                    continue
                if str(r.get("chatId") or "") != GERADOR or "botgerador" not in str(r.get("senderName") or "").lower():
                    continue
                corpo = r.get("body") or ""
                if LINK.search(corpo) and mesma_promo(cur, produto, corpo):
                    return r
    return None


def marcar_feita(con, ids: list[int], quando: int) -> None:
    """Promo mandada ao Gerador = feita (pedido do Breno 09/10): no painel vai para baixo, apagada, e sai da
    sugestão, para ninguém da equipe mandar de novo."""
    con.executemany("UPDATE avaliacoes SET postada=?, postada_por='pipeline' WHERE id=? AND COALESCE(postada, 0)=0",
                    [(quando, i) for i in ids])
    con.commit()


def marcar_enviadas_de_hoje(con) -> int:
    """As que o pipeline já mandou hoje (antes desta regra) também viram feitas."""
    inicio = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    n = 0
    if not REGISTRO.exists():
        return n
    for linha in REGISTRO.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if r.get("status") == "enviado" and r.get("chave") and r.get("ts", 0) >= inicio:
            n += con.execute("UPDATE avaliacoes SET postada=?, postada_por='pipeline' WHERE chave=? AND ts>=? "
                             "AND COALESCE(postada, 0)=0", (r["ts"], r["chave"], r["ts"] - 48 * 3600)).rowcount
    con.commit()
    return n


def para_a_normalizadora(corpo: str, n: int, foto: str = "") -> str:
    """Deposita a promo na caixa da normalizadora. `foto`: caminho da foto do anúncio; ela só usa se o banco de
    fotos dela não tiver uma aprovada do produto (é a regra dela para foto recebida junto com a promo)."""
    mid = f"curadora-{int(time.time())}-{n}"
    CAIXA.parent.mkdir(parents=True, exist_ok=True)
    with CAIXA.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"messageId": mid, "timestamp": int(time.time()), "body": corpo,
                            **({"mediaPath": foto} if foto else {})}, ensure_ascii=False) + "\n")
    return mid


def produtos_com_foto(cur: Curadora) -> set[str]:
    """Produtos cujo último post na normalizadora saiu com foto (ela tem foto deles no banco)."""
    ultimo: dict[str, bool] = {}
    if not FEEDBACK.exists():
        return set()
    with FEEDBACK.open("rb") as f:
        f.seek(max(0, FEEDBACK.stat().st_size - 8_000_000))
        f.readline()
        for bruto in f:
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            nome = r.get("display_product") or r.get("product") or ""
            if nome:
                ultimo[cur.chave_de(nome)] = bool(r.get("image_path"))
    return {k for k, tem in ultimo.items() if tem}


def tamanho_da_imagem(caminho: Path) -> tuple[int, int] | None:
    """(largura, altura) de um JPEG ou PNG, lendo só o cabeçalho (a curadora não usa biblioteca de imagem)."""
    try:
        with Path(caminho).open("rb") as f:
            cab = f.read(24)
            if cab[:8] == b"\x89PNG\r\n\x1a\n":
                return int.from_bytes(cab[16:20], "big"), int.from_bytes(cab[20:24], "big")
            if cab[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    return None
                marca_ = b[0]
                if 0xC0 <= marca_ <= 0xCF and marca_ not in (0xC4, 0xC8, 0xCC):   # início do quadro: traz o tamanho
                    f.read(3)
                    altura, largura = int.from_bytes(f.read(2), "big"), int.from_bytes(f.read(2), "big")
                    return (largura, altura) if largura and altura else None
                if marca_ in (0x01, 0xD8) or 0xD0 <= marca_ <= 0xD7:
                    continue
                f.seek(int.from_bytes(f.read(2), "big") - 2, 1)
    except (OSError, ValueError):
        return None


# 15/10: palavras de gênero de um texto (as da identidade, mais o feminino das palavras: "fragrância feminina")
_MASC = MASCULINO | {"masculina", "masculinos", "masculinas"}
_FEM = FEMININO | {"feminina", "femininos", "femininas"}
GENERO_DA_LINHA_S = 3 * 24 * 3600


def generos(texto: str) -> set[str]:
    """{"m"}, {"f"}, os dois ou nenhum."""
    palavras = set(re.findall(r"[a-z]+", sem_acento(texto or "").lower()))
    return {g for g, lista in (("m", _MASC), ("f", _FEM)) if palavras & lista}


def foto_do_grupo(con, chave: str, link: str, produto: str = "") -> str:
    """A foto que veio com a promo no grupo de origem, ou em outro post do mesmo produto nas últimas 24h.

    11/10 (Breno: "ver o que pode consertar para ir saindo cada vez menos com ressalvas"). Em 3 dias, 27 posts
    saíram SEM foto; em 5 deles o post do grupo tinha foto boa e a normalizadora não achou sozinha (ela procura
    pelo título exato: "LAB 8 SONATY BLUE" não bate com "Perfume Unissex Sonaty Blue 100ml - Lab 8 Niche").
    A curadora sabe de que post a promo veio, então manda a foto junto, como o Breno faz ao encaminhar à mão.
    Só foto quadrada de 600 px ou mais (print de tela e miniatura ficam de fora). Não achou: devolve "".

    15/10 (Breno: "erros de foto masculina no feminino e vice-versa"): a chave não separa o masculino do feminino
    (Club de Nuit Intense Man e Woman são a mesma chave) e o Club de Nuit FEMININO foi ao grupo com a foto de um post
    do MAN. Com o nome do produto (`produto`), a foto respeita o gênero: post do outro gênero nunca empresta a foto;
    post de OUTRO anúncio sem gênero no texto só vale se a linha não tem as duas versões nos grupos."""
    proprios = {m.group(1) for m in [re.search(r"meli\.la/([A-Za-z0-9]+)", link or "")] if m}
    codigos = set(proprios)
    for (dados,) in con.execute("SELECT dados FROM avaliacoes WHERE chave = ? AND ts >= ? AND origem = 'ao vivo'",
                                (chave, time.time() - FOTO_DO_GRUPO_S)):
        achado = re.search(r'"link": "https?://meli\.la/([A-Za-z0-9]+)', dados or "")
        if achado:
            codigos.add(achado.group(1))
    if not codigos or not TAP.exists():
        return ""
    genero, duas_versoes = generos(produto), False
    if produto:
        da_linha: set[str] = set()
        for (dados,) in con.execute("SELECT dados FROM avaliacoes WHERE chave = ? AND ts >= ?",
                                    (chave, time.time() - GENERO_DA_LINHA_S)):
            nome = re.search(r'"produto": "([^"]*)"', dados or "")
            da_linha |= generos(nome.group(1)) if nome else set()
        duas_versoes = {"m", "f"} <= da_linha | genero
    marcas_ = [f"meli.la/{c}".encode() for c in codigos]
    marcas_proprias = [f"meli.la/{c}".encode() for c in proprios]
    melhores = []
    with TAP.open("rb") as f:
        inicio = max(0, TAP.stat().st_size - 40_000_000)
        f.seek(inicio)
        if inicio:
            f.readline()   # caiu no meio de uma linha: descarta o pedaço
        for bruto in f:
            if b"mediaPath" not in bruto or not any(m in bruto for m in marcas_):
                continue
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            foto = str(r.get("mediaPath") or "")
            if not foto or str(r.get("chatId") or "").split("@")[0] in NOSSOS_GRUPOS:
                continue
            if produto:
                g = generos(r.get("body") or "")
                if genero and g - genero:
                    continue   # o post é (também) do outro gênero
                if not any(m in bruto for m in marcas_proprias) and (g != genero if duas_versoes else g and g != genero):
                    continue   # post de outro anúncio que não prova ser a mesma versão
            tamanho = tamanho_da_imagem(Path(foto))
            if not tamanho:
                continue   # arquivo que já saiu do cache, ou que não é JPEG/PNG
            largura, altura = tamanho
            if min(largura, altura) < 600 or max(largura, altura) >= min(largura, altura) * 1.2:
                continue
            melhores.append((min(largura, altura), epoch(r.get("timestamp")) or 0, foto))
    return max(melhores)[2] if melhores else ""


def url_grande(url: str) -> str:
    """A mesma foto do anúncio no maior tamanho e em JPG (o card traz a miniatura: D_Q_NP_...-O.webp)."""
    return re.sub(r"-[A-Z]\.(?:webp|jpe?g|png)$", "-F.jpg", url.replace("/D_Q_NP_", "/D_NQ_NP_2X_"))


def baixar_foto(url: str, nome: str) -> str:
    """10/10 (Breno: "sim"): 13 de 72 posts saíram sem foto, quase todos de produto novo para o banco de fotos.
    A foto do próprio anúncio é do produto certo por definição. Devolve o caminho, ou "" se não deu."""
    if not url or not url.startswith("https://"):
        return ""
    FOTOS.mkdir(parents=True, exist_ok=True)
    for arq in FOTOS.glob("*.jpg"):                      # limpeza: só os últimos 2 dias
        if time.time() - arq.stat().st_mtime > 2 * 86400:
            arq.unlink(missing_ok=True)
    for tentativa in dict.fromkeys((url_grande(url), url)):
        try:
            pedido = urllib.request.Request(tentativa, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(pedido, timeout=12) as resp:
                dados, tipo = resp.read(6_000_000), resp.headers.get("Content-Type", "")
        except Exception:  # noqa: BLE001
            continue
        if tipo.startswith("image/") and len(dados) > 8_000:
            destino = FOTOS / (nome + (".jpg" if "jpeg" in tipo or "jpg" in tipo else "." + tipo.split("/")[-1].split(";")[0]))
            destino.write_bytes(dados)
            return str(destino.resolve())
    return ""


# ── A vitrine: a curadora no grupo da equipe, no lugar da coletora (13/10, Breno: "pense uma curadora, mas
# versão WhatsApp"; "a promo separada e a avaliação embaixo, para ter a promo limpa"). Roda junto com o pipeline:
# mesma escolha (candidatas) e mesma conferência (conferir); só não passa pelo Gerador nem pela normalizadora.
# Desliga em config.json -> grupo_curadora.ligado (vale na hora).
_recusadas_vitrine: dict[str, float] = {}
CONFERIDAS_POR_PASSO = 3   # no máximo 3 idas ao ML por passo da vitrine


def texto_da_vitrine(texto: str, av, sem_cupom: bool) -> str:
    """O texto do post de origem com o preço conferido agora no ML, quando ele não depende de cupom."""
    if sem_cupom and av.card and av.card.ok and av.card.preco:
        return texto_para_o_gerador(texto, set(), av.card.preco, av.promo.preco)
    return texto


def para_a_vitrine(con, cur: Curadora, av, texto: str, card: dict | None, do_pipeline: bool, http=None,
                   link_do_grupo: str = "") -> bool:
    """Põe a promo no grupo da equipe: a promo limpa e, embaixo, a avaliação. Cada produto uma vez a cada 8h (volta
    antes se estiver 8% mais barato). Nunca derruba quem chamou: o pipeline não pode parar por causa do grupo.
    01/11: `link_do_grupo` é o link do post original (depois da troca pela loja oficial, av.promo.link é o dela)."""
    try:
        v = vitrine.configuracao()
        if not v["ligado"]:
            return False
        chave = av.identidade.chave
        ja = vitrine.mostradas(time.time() - REPETIR_APOS_S)
        if chave in ja and not av.preco_efetivo <= ja[chave] * (1 - MAIS_BARATO):
            return False
        if vitrine.na_ultima_hora() >= vitrine.MAXIMO_POR_HORA:
            return False
        foto = foto_do_grupo(con, chave, link_do_grupo or av.promo.link or "", av.promo.produto) if v["foto"] else ""
        vitrine.enviar(v["grupo"], texto, vitrine.avaliacao(av, vitrine.fonte_do_card(card), do_pipeline), foto, http)
        vitrine.registrar({"chave": chave, "produto": av.promo.produto, "nota": av.nota, "score": av.score,
                           "preco": round(av.preco_efetivo, 2), "do_pipeline": do_pipeline, "foto": bool(foto)})
        log(f"   no grupo da equipe: {av.promo.produto[:50]} · {av.nota} {av.score} · R${av.preco_efetivo:.2f}"
            + (" (com foto)" if foto else ""))
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"   grupo da equipe: não foi ({type(exc).__name__}: {exc})")
        return False


def preparar_para_a_vitrine(con, cur: Curadora, card: dict):
    """A mesma conferência e o mesmo texto que o pipeline faz para esta promo (ver uma_promo), sem falar com o
    Gerador: card do radar só entra se o link da página já estiver guardado. Devolve (avaliação, texto) ou None."""
    card = pela_pagina(con, cur, card, simular=True)
    conferida = conferir(con, cur, card) if card else None
    if conferida is None:
        return None
    av, dados = conferida
    da_equipe = None if av.cupom_quente else com_cupom_da_equipe(cur, av, dados["texto"])
    if dados.get("_so_com_cupom") and not (da_equipe and da_equipe[0].nota in NOTAS_BOAS):
        return None
    if da_equipe:
        av, texto = da_equipe
    ficam = cupons_que_ficam(cur, av)
    tirar = set(av.promo.cupons) - set(ficam)
    if da_equipe:
        pass
    elif av.cupom_quente:
        texto = texto_para_o_gerador(dados["texto"], tirar, av.cupom_quente["preco"], av.promo.preco)
    else:
        texto = texto_para_o_gerador(dados["texto"], tirar, preco_do_texto(cur, av, ficam, tirar), av.promo.preco)
    if tirar and not av.cupom_quente:
        av2 = cur.avaliar(texto, agora=int(time.time()), conferir_ml=True)   # sem o cupom, a nota tem de continuar boa
        if av2 is None or not aceitavel(av2):
            return None
        av = av2
    return av, texto_da_vitrine(texto, av, not (ficam or av.cupom_quente or da_equipe))


def uma_da_vitrine(con, cur: Curadora, http=None) -> bool:
    """A melhor promo aprovada que ainda não apareceu no grupo da equipe. True se mandou."""
    global _calado
    agora = time.time()
    ja = vitrine.mostradas(agora - REPETIR_APOS_S)
    fora = {k for k, t in _recusadas_vitrine.items() if agora - t < RECUSADA_S}
    prioridade, lista = candidatas(con, cur, fora)
    fila = [c for c in prioridade + lista
            if not (c["chave"] in ja and not (c.get("preco") or 0) <= ja[c["chave"]] * (1 - MAIS_BARATO))
            and agora - _recusadas.get(c["chave"], 0) >= RECUSADA_S]   # o pipeline acabou de recusar: vale aqui também
    for card in fila[:CONFERIDAS_POR_PASSO]:
        _recusadas_vitrine[card["chave"]] = agora   # mostrada ou recusada: só volta a ser conferida em 30 min
        _calado = True
        try:
            pronta = preparar_para_a_vitrine(con, cur, card)
        finally:
            _calado = False
        if pronta is not None:
            return para_a_vitrine(con, cur, pronta[0], pronta[1], card, do_pipeline=False, http=http)
    return False


def passo_da_vitrine(con, cur: Curadora) -> float:
    """Um passo da vitrine dentro da rodada do pipeline. Devolve em quantos segundos dar o próximo."""
    global _calado
    try:
        v = vitrine.configuracao()
        if not v["ligado"]:
            return 60
        return v["intervalo_s"] if uma_da_vitrine(con, cur) else 45
    except sqlite3.OperationalError:
        _calado = False
        con.rollback()
        return 60
    except Exception as exc:  # noqa: BLE001
        _calado = False
        log(f"   grupo da equipe: erro ({type(exc).__name__}: {exc})")
        return 120


def _sem_cupom_medido(av, av_conferido, dados: dict) -> dict:
    """30/10 (ficha do cupom, sombra): a promo passaria SEM nenhum cupom (nota e score com o preço do ML)? Só com o que
    o envio já mediu: não abre o ML de novo nem avalia outra vez (avaliar grava preço no histórico)."""
    saida: dict = {"aceitavel": None, "medido": "não medido (o envio só foi avaliado com cupom)"}
    for nome, a in (("envio", av), ("conferida", av_conferido)):
        card = getattr(a, "card", None)
        ml = getattr(card, "preco", None) if card is not None and getattr(card, "ok", False) else None
        efetivo = getattr(a, "preco_efetivo", None)
        if ml and efetivo is not None and abs(efetivo - ml) <= 0.05:
            saida = {"nota": a.nota, "score": a.score, "aceitavel": aceitavel(a), "medido": nome}
            break
    if dados.get("_so_com_cupom"):
        saida.update(aceitavel=False, so_com_cupom=True)   # a conferência já disse: sem o cupom não vai
    return saida


def sombra_da_ficha(con, cur: Curadora, card: dict, av, dados: dict, ficam: list, link: str,
                    av_conferido=None, preco_enviado: float | None = None) -> None:
    """30/10 (Breno: "Ficha do cupom"): a ficha do cupom em MODO SOMBRA. Registra em dados/ficha_cupom_sombra.jsonl o
    que ela faria com os cupons deste envio (texto do grupo, cupom da equipe, LOJAS da marca e os que o Breno
    confirmou hoje). Não muda nada do envio; erro aqui vira só uma linha de aviso no registro."""
    try:
        if ficha_cupom is None or ficha_cupom.modo() != "sombra":
            return
        from curadora.cupons import cupons_da_mensagem
        quente = (av.cupom_quente or {}).get("codigo", "")
        ficha_cupom.sombra_do_envio(
            con, chave=av.identidade.chave, produto=av.promo.produto, link=link, card=av.card,
            cupons_grupo=sorted(cupons_da_mensagem(dados.get("texto") or "")),
            equipe=sorted(cupons_da_equipe_agora(cur) | ({quente} if quente else set())),
            loja_da_marca_cupom=cupom_da_marca_agora(cur),
            atual={"cupom": quente, "cupons_no_texto": list(ficam), "preco": round(av.preco_efetivo, 2),
                   "preco_anunciado": av.promo.preco, "preco_enviado": preco_enviado,
                   "preco_ml": getattr(av.card, "preco", None), "nota": av.nota, "score": av.score,
                   "aceitavel_sem_cupom": _sem_cupom_medido(av, av_conferido, dados)},
            queridinho=card.get("chave") in queridinhos(con, time.time() - painel.JANELA_FEED_S))
    except Exception as exc:  # noqa: BLE001 - a sombra nunca derruba o pipeline
        try:
            ficha_cupom.aviso("pipeline", exc)
        except Exception:  # noqa: BLE001
            pass


def sombra_do_pulo(con, av, av2, tirar: set) -> None:
    """30/10 (ficha do cupom, sombra): a promo foi PULADA porque sem o cupom não vale; registra o que a ficha diria
    desses cupons nesta loja. Só registra."""
    try:
        if ficha_cupom is None or ficha_cupom.modo() != "sombra":
            return
        ficha_cupom.sombra_do_pulo(
            con, chave=av.identidade.chave, produto=av.promo.produto, card=av.card, tirados=sorted(tirar),
            sem_cupom=None if av2 is None else {"nota": av2.nota, "score": av2.score,
                                                "preco_ml": getattr(av.card, "preco", None)})
    except Exception as exc:  # noqa: BLE001 - a sombra nunca derruba o pipeline
        try:
            ficha_cupom.aviso("pulo", exc)
        except Exception:  # noqa: BLE001
            pass


def sombra_das_candidatas(con, cur: Curadora, lista: list, prioridade: list) -> None:
    """30/10 (ficha do cupom, sombra): numa rodada de cupom (cupom da equipe ou LOJAS da marca valendo), grava as
    candidatas que o pipeline vai conferir, com a faixa de cada uma na ficha. Só registra."""
    try:
        if ficha_cupom is None or ficha_cupom.modo() != "sombra" or not lista:
            return
        codigos = cupons_da_equipe_agora(cur) | ({cupom_da_marca_agora(cur)} - {""})
        if not codigos:
            return
        ids = {}
        for c in lista:
            av_id = (c.get("atual") or {}).get("id") or c.get("id")
            if av_id:
                ids[int(av_id)] = None
        if ids:
            for av_id, vendedor, oficial in con.execute(
                    "SELECT id, json_extract(dados, '$.ml.vendedor'), json_extract(dados, '$.ml.oficial') FROM avaliacoes "
                    "WHERE id IN (%s)" % ",".join("?" * len(ids)), list(ids)):
                ids[av_id] = (vendedor or "", None if oficial is None else bool(oficial))
        queridos = queridinhos(con, time.time() - painel.JANELA_FEED_S)
        na_frente = {id(c) for c in prioridade}
        saida = []
        for c in lista:
            av_id = (c.get("atual") or {}).get("id") or c.get("id")
            vendedor, oficial = (ids.get(int(av_id)) if av_id else None) or ("", None)
            saida.append({"chave": c.get("chave"), "vendedor": vendedor, "oficial": oficial,
                          "queridinho": c.get("chave") in queridos, "prioridade": id(c) in na_frente})
        ficha_cupom.sombra_das_candidatas(con, saida, codigos)
    except Exception as exc:  # noqa: BLE001 - a sombra nunca derruba o pipeline
        try:
            ficha_cupom.aviso("candidatas", exc)
        except Exception:  # noqa: BLE001
            pass


def sombra_da_rodada(cur: Curadora, liga: set, desliga: set) -> None:
    """30/10: a rodada de cupom ligou/desligou; a ficha (sombra) anota como via cada código. Só registra."""
    try:
        if ficha_cupom is not None:
            ficha_cupom.sombra_da_rodada(cur.con, sorted(liga), sorted(desliga))
    except Exception:  # noqa: BLE001 - a sombra nunca derruba o pipeline
        pass


def uma_promo(con, cur: Curadora, excluir: set[str], ultima_marca: str, n: int, simular: bool) -> dict | None:
    prioridade, lista = candidatas(con, cur, excluir)
    # Variedade: não repete a marca da anterior, se houver outra opção boa.
    nomes: dict[int, str] = {}

    def nome(c: dict) -> str:
        if id(c) not in nomes:
            nomes[id(c)] = c.get("produto") or painel._completar(con, c).get("produto", "")
        return nomes[id(c)]
    # rodízio de marcas, faixas de preço e Natura/Boticário na frente
    lista = misturar(lista, nome, ultimas_mandadas(RECENTES_PARA_A_ORDEM),
                     lambda produto: marca_visivel(cur, produto), linhas_recentes())
    lista = prioridade + lista   # queridinhos na rodada de cupom: sempre antes, sem rodízio de marca
    sombra_das_candidatas(con, cur, lista[:12], prioridade)   # 30/10: ficha do cupom, só registra
    recusadas = _recusadas_fundo if _no_fundo else _recusadas_reserva if _na_reserva else _recusadas
    for card in lista[:12]:
        chave = card["chave"]
        excluir.add(chave)
        if time.time() - recusadas.get(chave, 0) < RECUSADA_S:
            continue   # 10/10: os mesmos 10 produtos eram reconferidos (e recusados) a cada 4 minutos
        preco_do_card = card.get("preco")
        card = pela_pagina(con, cur, card, simular)   # card do radar: passa a valer o que a página mostra agora
        conferida = conferir(con, cur, card) if card else None
        if conferida is None:
            recusadas[chave] = time.time()
            log(f"   pulei ({conferir.motivo or 'sem texto ou link'}): {chave}")
            if not simular:
                travas.guardar(TRAVAS, chave, conferir.motivo or "sem texto ou link", preco_do_card)
            continue
        av, dados = conferida
        av_conferido = av   # 30/10: só para a sombra da ficha do cupom (a nota sem o cupom da equipe)
        da_equipe = None if av.cupom_quente else com_cupom_da_equipe(cur, av, dados["texto"])
        if dados.get("_so_com_cupom") and not (da_equipe and da_equipe[0].nota in NOTAS_BOAS):
            _recusadas[card["chave"]] = time.time()
            log(f"   pulei (nem com o cupom da equipe fica Boa): {av.promo.produto[:40]}")
            if not simular:
                travas.guardar(TRAVAS, chave, "nem com o cupom da equipe fica Boa", preco_do_card)
            continue
        if da_equipe:
            av, texto = da_equipe
        ficam = cupons_que_ficam(cur, av)
        tirar = set(av.promo.cupons) - set(ficam)
        preco_calculado = None   # 31/10: o preço que a curadora pôs no texto (None = ficou o do grupo)
        if da_equipe:
            pass   # o texto já está pronto, com o cupom e o preço
        elif av.cupom_quente:
            # Cupom quente: o texto vai com o cupom e com o preço de agora (ML menos o cupom, pela regra).
            preco_calculado = av.cupom_quente["preco"]
            texto = texto_para_o_gerador(dados["texto"], tirar, preco_calculado, av.promo.preco)
            log(f"   cupom QUENTE {av.cupom_quente['codigo']} (confirmado às {av.cupom_quente['hora']}): "
                f"R${av.card.preco:.2f} no ML, vai a R${av.cupom_quente['preco']:.2f} com ele")
        else:
            preco_calculado = preco_do_texto(cur, av, ficam, tirar)
            texto = texto_para_o_gerador(dados["texto"], tirar, preco_calculado, av.promo.preco)
        if tirar and not av.cupom_quente:
            # Sem o cupom o preço é o do ML: a nota precisa continuar boa com ele.
            av2 = cur.avaliar(texto, agora=int(time.time()), conferir_ml=True, card=av.card)   # 21/10: o mesmo anúncio
            if av2 is None or not aceitavel(av2):
                log(f"   pulei (sem o cupom {', '.join(sorted(tirar))} não vale): {av.promo.produto[:40]}")
                sombra_do_pulo(con, av, av2, tirar)   # 30/10: ficha do cupom, só registra
                if not simular:
                    travas.guardar(TRAVAS, chave, f"sem o cupom {', '.join(sorted(tirar))} não vale", preco_do_card)
                continue
            log(f"   cupom fora do texto ({', '.join(sorted(tirar))}): vai a R${av.card.preco:.2f}, o preço do ML")
            av = av2
        # 01/11 (revisão): o preço e o link do GRUPO ficam guardados antes da troca (registro e foto do grupo)
        preco_do_grupo, link_do_grupo = av.promo.preco, av.promo.link
        resposta_oficial = None
        oficial = pela_loja_oficial(con, cur, av, texto, not (ficam or av.cupom_quente or da_equipe), simular)
        if oficial:
            # 31/10 (Breno: "loja oficial vence revenda"): link, preço do texto e registro dela; 01/11: e o link de
            # afiliado que foi conferido (sem pedir outro ao gerador)
            av, texto, resposta_oficial = oficial
            preco_calculado = av.card.preco
        # 31/10 (revisão): três preços separados. "preco" continua o que a nota usou (as regras de repetição comparam
        # com ele); "preco_anunciado" é o do grupo; "preco_calculado" o que a curadora pôs no texto; "preco_enviado"
        # (gravado no envio) o que foi de fato para a normalizadora.
        resumo = {"chave": av.identidade.chave, "produto": av.promo.produto, "nota": av.nota, "score": av.score,
                  "preco": round(av.preco_efetivo, 2), "preco_anunciado": preco_do_grupo,
                  "preco_calculado": None if preco_calculado is None else round(preco_calculado, 2),
                  "link_origem": link_do_grupo,
                  **({"no_lugar_de": av.no_lugar_de} if getattr(av, "no_lugar_de", None) else {}),
                  "ml_ids": sorted(ids_do_card(av.card)), "cupom_quente": (av.cupom_quente or {}).get("codigo", ""),
                  **precos_do_envio(cur, av),
                  **({"reserva": True} if _na_reserva else {}), **({"fundo": True} if _no_fundo else {})}
        if simular:
            log(f"[simulação] mandaria: {av.promo.produto[:50]} · {av.nota} {av.score} · R${av.preco_efetivo:.2f}")
            return dict(resumo, status="simulado", marca=marca(cur, av.promo.produto))
        preso = com_link_do_anuncio(texto, av.card)
        if preso != texto:
            log(f"   vai o link do anúncio {av.card.item_id} ({av.card.vendedor or 'loja sem nome'}), não o da página")
            texto = preso
        comeco = time.time()
        # 01/11: na troca pela oficial, o link de afiliado já conferido (o mesmo anúncio que a curadora abriu)
        resposta, gerador = resposta_oficial or gerar_pela_api(texto), "api"
        if resposta is not None:
            resposta["body"] = texto_da_api(resposta["body"], av.promo.produto)
            log(f"-> API do gerador{' (link já conferido da loja oficial)' if resposta_oficial else ''}: "
                f"{av.promo.produto[:50]} · {av.nota} {av.score} · R${av.preco_efetivo:.2f} "
                f"({time.time() - comeco:.1f} s)")
        else:
            gerador = "whatsapp"
            inicio = TAP.stat().st_size
            enviar_ao_gerador(texto)
            log(f"-> Gerador: {av.promo.produto[:50]} · {av.nota} {av.score} · R${av.preco_efetivo:.2f}")
            resposta = esperar_resposta(cur, av.promo.produto, inicio, ESPERA_GERADOR_S)
            if resposta is None:  # uma nova tentativa (o BotGerador às vezes não responde)
                log("   sem resposta do Gerador: mando de novo")
                inicio = TAP.stat().st_size
                enviar_ao_gerador(texto)
                resposta = esperar_resposta(cur, av.promo.produto, inicio, ESPERA_GERADOR_S)
        if resposta is None:
            log("   o Gerador não respondeu: passo para a próxima")
            registrar(dict(resumo, status="gerador_sem_resposta"))
            # no grupo ela aparece: dá para mandar à mão
            para_a_vitrine(con, cur, av, texto_da_vitrine(texto, av, not (ficam or av.cupom_quente or da_equipe)),
                           card, do_pipeline=False, link_do_grupo=link_do_grupo)
            continue
        gerador_s = round(time.time() - comeco, 1)
        link = resposta.get("link") or LINK.search(resposta.get("body") or "").group(0)
        foto = ""
        if av.identidade.chave not in produtos_com_foto(cur):
            # 1º a foto do post do grupo (config "foto_do_grupo": false desliga); 2º a do anúncio do ML, que está
            # desligada (config "foto_do_anuncio": false: é de catálogo, fundo branco; as do Breno são de vitrine)
            if cur.cfg.get("foto_do_grupo", True):
                foto = foto_do_grupo(con, av.identidade.chave, resumo["link_origem"], av.promo.produto)
                if foto:
                    log("   foto do grupo de origem anexada (a normalizadora não tem foto desse produto)")
            if not foto and cur.cfg.get("foto_do_anuncio", True):
                foto = baixar_foto(av.card.foto or "", f"curadora-{int(time.time())}-{n}")
                if foto:
                    log("   foto do anúncio anexada (a normalizadora não tem foto desse produto)")
        # Sem cupom no texto, o preço que vai é o que foi conferido agora no ML; e a 1ª linha é o nome do produto.
        sem_cupom = not (ficam or av.cupom_quente or da_equipe)
        corpo = arrumar_resposta(resposta["body"], av.promo.produto, av.card.preco if sem_cupom else None)
        if corpo != resposta["body"]:
            log("   texto arrumado antes da normalizadora (preço conferido no ML e/ou nome do produto na 1ª linha)")
        mid = para_a_normalizadora(corpo, n, foto)
        marcar_feita(con, card.get("ids") or [card["id"]], int(time.time()))
        # o mesmo anúncio com outro nome (Bareeq x Bareeq Al Dhahab) também sai da lista do painel
        marcar_mesmo_anuncio(con, {re.sub(r"\D", "", i) for i in resumo["ml_ids"]}, av.preco_efetivo, int(time.time()), "pipeline")
        con.commit()
        log(f"   <- {'API' if gerador == 'api' else 'Gerador'} respondeu ({link}); na caixa da normalizadora: {mid}")
        preco_enviado = preco_no_texto(corpo)
        registrar(dict(resumo, status="enviado", link_afiliado=link, caixa=mid, gerador=gerador, gerador_s=gerador_s,
                       preco_enviado=preco_enviado))
        para_a_vitrine(con, cur, av, texto_da_vitrine(texto, av, sem_cupom), card, do_pipeline=True,
                       link_do_grupo=link_do_grupo)
        sombra_da_ficha(con, cur, card, av, dados, ficam, link, av_conferido,   # 30/10: ficha do cupom, só registra
                        preco_enviado=preco_enviado)
        return dict(resumo, status="enviado", marca=marca(cur, av.promo.produto))
    return None


def promo_da_reserva(con, cur: Curadora, n: int, simular: bool = False) -> dict | None:
    """Sem promo nova boa: repete a melhor que já foi há 6h ou mais; se não houver, há 4h ou mais."""
    global _janela_repetir, _na_reserva
    for janela in RESERVA_S:
        _janela_repetir, _na_reserva = janela, True
        try:
            r = uma_promo(con, cur, set(), "", n, simular)
        finally:
            _janela_repetir, _na_reserva = REPETIR_APOS_S, False
        if r is not None:
            log(f"   (reserva: não havia promo nova boa; repeti uma que já tinha ido há {janela // 3600}h ou mais)")
            return r
    return None


def promo_do_fundo(con, cur: Curadora, n: int, simular: bool = False) -> dict | None:
    """Sem promo boa nem reserva: a melhor das que sobraram abaixo da régua (ver PISO_DO_FUNDO)."""
    global _no_fundo
    _no_fundo = True
    try:
        r = uma_promo(con, cur, set(), "", n, simular)
    finally:
        _no_fundo = False
    if r is not None:
        log("   (fundo da fila: sem promo boa nem reserva; mandei a melhor das que sobraram)")
    return r


def promo_de_reserva_ou_fundo(con, cur: Curadora, n: int, ultima_foi_reserva: bool, simular: bool = False) -> dict | None:
    """Sem promo nova boa: reserva (repete uma que já foi) ou fundo da fila (produto novo, nota mais baixa).

    25/10 (Breno, sobre o kit Cebolinha e Cascão que voltou às 21:13: "era melhor outro produto pra variar, talvez uma
    intercalada"). Às 21h de domingo foram 4 repetidas seguidas pela reserva. Agora, depois de uma repetida, a próxima
    tenta antes o fundo da fila: repetida e produto novo se alternam; se um lado não tiver nada, vai o outro."""
    ordem = (promo_do_fundo, promo_da_reserva) if ultima_foi_reserva else (promo_da_reserva, promo_do_fundo)
    for escolher in ordem:
        r = escolher(con, cur, n, simular)
        if r is not None:
            return r
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quantas", type=int, default=11)
    ap.add_argument("--ja", type=int, default=1, help="quantas mandar já, uma atrás da outra")
    ap.add_argument("--intervalo-min", type=float, default=4,
                    help="09/10 (Breno): 4 em 4 min, porque o caminho até o post às vezes passa dos 7 min")
    ap.add_argument("--intervalo-cupom-min", type=float, default=1.5,
                    help="minutos entre promos enquanto o Breno está usando um cupom (ritmo mais rápido)")
    ap.add_argument("--fila-max", type=int, default=0,
                    help="0 = nunca espera (padrão); N = espera se há N mandadas e não postadas e a equipe sumiu")
    ap.add_argument("--de", default="", help="só começa neste horário (HH:MM); vazio = agora")
    ap.add_argument("--ate", default="21:55", help="não manda depois deste horário")
    ap.add_argument("--simular", action="store_true")
    args = ap.parse_args()
    PARAR.unlink(missing_ok=True)
    ambiente_do_servico()
    con = conectar(RAIZ / "dados" / "curadora.db")
    cur = Curadora(carregar_config(), con, ConsultorML(con, ttl=60), cliente_api=ClienteML())
    if not cur.api.configurado():
        # Sem a API não dá para saber quem vende nem barrar compra internacional (regra do Breno): não roda.
        log("SEM ACESSO à API do ML: o pipeline não roda assim (falta o ambiente do serviço da curadora)")
        return 2
    hoje = dt.date.today()
    hora = lambda texto: dt.datetime.combine(hoje, dt.time(*(int(x) for x in texto.split(":")))).timestamp()
    limite = hora(args.ate)
    estado = {"pid": os.getpid(), "ligado_em": int(time.time()), "de": args.de, "ate": args.ate,
              "quantas": args.quantas, "intervalo_min": args.intervalo_min, "fila_max": args.fila_max, "feitas": 0}
    if not args.simular:
        gravar_estado(estado)
    try:
        if args.de and time.time() < hora(args.de):
            log(f"pipeline ligado: começa às {args.de} e vai até {args.ate}")
            while time.time() < hora(args.de) and not PARAR.exists():
                time.sleep(10)
        return rodar(args, con, cur, limite, estado)
    finally:
        if not args.simular:
            gravar_estado(None)


def rodar(args, con, cur: Curadora, limite: float, estado: dict) -> int:
    excluir: set[str] = set()   # o que esta rodada já tentou (mandou, pulou ou ficou sem resposta)
    if not args.simular:
        log(f"marcadas como feitas no painel (já mandadas hoje): {marcar_enviadas_de_hoje(con)} avaliações")
    log(f"pipeline: até {args.quantas} promos ({args.ja} já, depois 1 a cada {args.intervalo_min:g} min, até "
        f"{args.ate}); {len(recentes(con, cur))} produtos fora agora (mandados/postados há menos de 8h ou na "
        f"normalizadora há pouco)")
    feitas, ultima_marca, proxima, esperando = 0, "", time.time(), False
    ultima_foi_reserva = False   # 25/10: reserva e fundo da fila se alternam (ver promo_de_reserva_ou_fundo)
    vazia = False   # a última tentativa não achou nada: espera os 3 min mesmo antes da 1ª promo
    proxima_vitrine = time.time()   # a curadora no grupo da equipe anda no passo dela, entre uma promo e outra
    em_uso: set[str] = set()
    while feitas < args.quantas:
        if PARAR.exists():
            log("parado (dados/pipeline.parar)")
            break
        if time.time() > limite:
            log(f"passou das {args.ate}: encerrado")
            break
        # 11/10 (Breno: "em momentos assim vale a pena melhorar o ritmo"): enquanto ele está usando um cupom, o
        # pipeline manda mais rápido e reconsidera o que tinha pulado (com o cupom, a conta de cada promo muda).
        try:
            agora_em_uso = cupons_da_equipe_agora(cur)
        except sqlite3.OperationalError:
            agora_em_uso = em_uso
        if agora_em_uso != em_uso:
            if agora_em_uso - em_uso:
                log(f"cupom da equipe valendo ({', '.join(sorted(agora_em_uso))}): 1 promo a cada "
                    f"{args.intervalo_cupom_min:g} min, queridinhos primeiro, e a lista é refeita com ele")
                excluir.clear()
                _recusadas.clear()
                proxima = min(proxima, time.time() + 5)
            elif not agora_em_uso:
                log(f"o cupom da equipe parou{motivo_da_parada(cur, em_uso)}: volta a 1 promo a cada "
                    f"{args.intervalo_min:g} min")
            if not args.simular:
                sombra_da_rodada(cur, agora_em_uso - em_uso, em_uso - agora_em_uso)   # 30/10: ficha do cupom, só registra
            em_uso = agora_em_uso
        if not args.simular and time.time() >= proxima_vitrine:
            proxima_vitrine = time.time() + passo_da_vitrine(con, cur)
        if (feitas >= args.ja or vazia) and time.time() < proxima:
            time.sleep(min(5, proxima - time.time()))
            continue
        # 11/10 (Breno): "tira essa regra, é melhor eu ter promos extras sobrando, facilita meu trabalho".
        # Sem --fila-max o pipeline nunca espera a equipe: manda no ritmo dele, do início ao fim da janela.
        if (args.fila_max and not args.simular and not equipe_postando(con, cur)
                and na_fila(con) >= args.fila_max):
            if not esperando:
                log(f"ninguém postou no GrupoFinal nem mexeu na Edição há mais de 20 min e já há {args.fila_max} promos "
                    "esperando: espero a equipe voltar")
            esperando, proxima = True, time.time() + 30
            continue
        if esperando:
            log("a equipe voltou a postar: volto a mandar")
            esperando = False
        try:
            r = uma_promo(con, cur, excluir, ultima_marca, feitas + 1, args.simular)
        except sqlite3.OperationalError as exc:  # banco ocupado (ex.: a curadora acabou de reiniciar)
            log(f"banco ocupado ({exc}): tento de novo em 30 s")
            con.rollback()  # sem desfazer, a escrita que falhou no meio prende a conexão para sempre
            time.sleep(30)
            continue
        except Exception as exc:  # um link estranho não derruba a rodada
            log(f"erro ({type(exc).__name__}: {exc}): tento de novo em 30 s")
            time.sleep(30)
            continue
        if r is None and not args.simular:
            try:
                r = promo_de_reserva_ou_fundo(con, cur, feitas + 1, ultima_foi_reserva)
            except sqlite3.OperationalError:
                con.rollback()
        if r is None:
            log("nenhuma candidata boa agora: tento de novo em 3 min")
            proxima, vazia = time.time() + 180, True
            if args.simular:
                break
            continue
        feitas += 1
        vazia = False
        ultima_marca = r.get("marca") or ""
        ultima_foi_reserva = bool(r.get("reserva"))
        proxima = time.time() + (args.intervalo_cupom_min if em_uso else args.intervalo_min) * 60
        if not args.simular:
            gravar_estado(dict(estado, feitas=feitas, ultima=r.get("produto", "")[:60], ultima_em=int(time.time())))
    log(f"fim: {feitas} promos {'escolhidas' if args.simular else 'mandadas para a normalizadora'}")
    if not args.simular:
        guardar_review()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
