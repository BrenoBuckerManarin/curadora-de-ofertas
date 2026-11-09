"""Segunda leitura e memória da edição (09/10/2026, pedido do Breno: "errar menos e me tirar do loop").

Roda junto do painel e lê, sem nunca escrever neles, três arquivos da coletora:
  * o tap do WhatsApp: as promos que chegam no grupo Normalizador Promo e as versões finais do Breno no grupo
    Edição/Correções;
  * a caixa da curadora (~/.hermes/run/curadora_normalizer_inbox.jsonl): as promos que a própria curadora manda;
  * o registro da normalizadora (normalizer_feedback.jsonl): o post que ela entregou para cada link.

Toda promo que entra na normalizadora é conferida na hora: preço no ML agora, cupom esgotado, quem vende e a nota
da curadora. Cupom que estava no post da normalizadora e saiu da versão final do Breno vira "suspenso" na memória de
cupons (esgotados.py; até 03/11, "esgotado"). O resultado vai para ~/.hermes/run/curadora_para_normalizadora.json: a normalizadora tira o
cupom esgotado antes de normalizar e põe a conferência no relatório dela (✅ só quando as duas leituras batem).
Esse arquivo é a única coisa que a curadora escreve fora da pasta dela.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .aovivo import epoch
from .chamadas import apos_edicao
from .cupons import cupons_da_mensagem
from .esgotados import (chaves_da_loja, guardar_loja_do_link, lista_para_normalizadora, loja_do_link, registrar_sinal,
                        sinais_da_edicao, tirou_do_post)
from .parser import ler_promo
from .score import texto_confianca

NOMES_NOTA = {"IMPERDÍVEL": "Excelente", "BOA": "Boa", "OK": "Média", "NÃO VALE": "Ruim"}
QUIETO_EDICAO_S = 3 * 60          # o Breno às vezes reedita o mesmo post: vale a última versão depois de 3 min
#                                   (03/11: também para o cupom tirado; a Edição é rascunho)
RECENTE_S = 30 * 60               # ao ligar, relê o fim do tap: só mensagens dos últimos 30 min contam
CAIXA_RECENTE_S = 5 * 60          # da caixa do pipeline, só o que chegou nos últimos 5 min (ver rodada)
GUARDA_CONFERENCIA_S = 3 * 3600
EXPORTAR_A_CADA_S = 30            # a normalizadora só confia no arquivo se ele foi atualizado há pouco
LINK = re.compile(r"https?://(?:meli\.la/[A-Za-z0-9]+|(?:www\.|produto\.)?mercadoli(?:vre|bre)\.com[^\s*]*)")


def chave_link(link: str) -> str:
    """A mesma chave nos dois lados (a normalizadora tem uma cópia desta função): meli.la/XXXX ou MLB123..."""
    m = re.search(r"meli\.la/([A-Za-z0-9]+)", link or "")
    if m:
        return "meli.la/" + m.group(1)
    m = re.search(r"MLB-?(\d{6,})", link or "", re.I)
    return f"MLB{m.group(1)}" if m else (link or "").strip()


def _id(grupo) -> str:
    return str(grupo or "").split("@")[0]


class LeitorJsonl:
    """Lê as linhas novas de um arquivo que só cresce. Começa `recuo_bytes` antes do fim (para reler o que chegou
    pouco antes de ligar); se o arquivo encolher (trocado), recomeça do início."""

    def __init__(self, caminho: Path, recuo_bytes: int = 0):
        self.caminho = Path(caminho)
        tamanho = self.caminho.stat().st_size if self.caminho.exists() else 0
        self.pos = max(0, tamanho - recuo_bytes)
        self._no_meio = self.pos > 0   # caiu no meio de uma linha: descarta o pedaço

    def novas(self) -> list[dict]:
        if not self.caminho.exists():
            return []
        if self.caminho.stat().st_size < self.pos:
            self.pos, self._no_meio = 0, False
        saida = []
        with self.caminho.open("rb") as fh:
            fh.seek(self.pos)
            if self._no_meio:
                pedaco = fh.readline()
                if not pedaco.endswith(b"\n"):
                    return saida
                self.pos, self._no_meio = fh.tell(), False
            while True:
                bruto = fh.readline()
                if not bruto or not bruto.endswith(b"\n"):
                    break  # linha ainda sendo escrita: lê na próxima volta
                self.pos = fh.tell()
                try:
                    saida.append(json.loads(bruto.decode("utf-8", "replace")))
                except ValueError:
                    continue
        return saida


# 28/10 (Breno: "queria saber se algum grupo já postou ela ou se o radar ML pegou, pq se o radar pegou significa que a
# nossa automação está sendo melhor que os concorrentes"): uma linha no relatório da normalizadora dizendo de onde a
# promo veio. Mesma promo = mesmo produto e preço até 5% de diferença, vista nas últimas 48h.
ORIGEM_JANELA_S = 48 * 3600
ORIGEM_ROBOS = ("radar do ML", "vigia do ML")
ORIGEM_ROBO_NOVO_S = 24 * 3600   # 31/10: o robô só conta como origem se viu o preço nas últimas 24 h


def _quando(ts: float, agora: float) -> str:
    dia = time.strftime("%Y-%m-%d", time.localtime(ts))
    if dia == time.strftime("%Y-%m-%d", time.localtime(agora)):
        return "hoje " + time.strftime("%H:%M", time.localtime(ts))
    if dia == time.strftime("%Y-%m-%d", time.localtime(agora - 86400)):
        return "ontem " + time.strftime("%H:%M", time.localtime(ts))
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


def _duracao(segundos: float) -> str:
    minutos = max(1, int(segundos // 60))
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def origem_da_promo(con, chave: str, preco: float | None, agora: float) -> str:
    """De onde veio a promo: o primeiro grupo que postou (e quantos mais), ou o radar/vigia do ML quando ele achou antes
    de qualquer grupo. "" quando não há registro (ex.: promo que o Breno mandou direto)."""
    if not chave or not preco:
        return ""
    vistas = []
    for ts, origem, grupo, dados in con.execute(
            "SELECT ts, origem, grupo, dados FROM avaliacoes WHERE ts >= ? AND chave = ? AND origem IN (?, ?, ?) "
            "ORDER BY ts", (agora - ORIGEM_JANELA_S, chave, "ao vivo") + ORIGEM_ROBOS):
        try:
            p = float(json.loads(dados or "{}").get("preco") or 0)
        except (TypeError, ValueError):
            continue
        if p and abs(p - preco) <= preco * 0.05:
            vistas.append((ts, origem, grupo or ""))
    grupos: dict[str, float] = {}
    for ts, origem, grupo in vistas:
        if origem == "ao vivo" and grupo and grupo not in grupos:
            grupos[grupo] = ts
    robo = next(((ts, origem) for ts, origem, _ in vistas if origem in ORIGEM_ROBOS), None)
    primeiro = min(grupos.items(), key=lambda kv: kv[1]) if grupos else None
    # 31/10 (Breno, no Khamrah: "📡 radar do ML (29/10 16:45), 1h51 antes do grupo"): o robô só "achou antes" se viu
    # esse preço nas últimas 24 h; preço parado no ML há dias não é descoberta (é o grupo que aparece como origem).
    if robo and agora - robo[0] > ORIGEM_ROBO_NOVO_S:
        robo = None
    if robo and (not primeiro or robo[0] < primeiro[1]):
        linha = f"📡 Origem: {robo[1]} ({_quando(robo[0], agora)})"
        if primeiro:
            return linha + f", {_duracao(primeiro[1] - robo[0])} antes do grupo {primeiro[0]}"
        return linha + " · nenhum grupo postou"
    if not primeiro:
        return ""
    linha = f"📍 Origem: {primeiro[0]} ({_quando(primeiro[1], agora)})"
    if len(grupos) > 1:
        linha += f" · +{len(grupos) - 1} grupo" + ("s" if len(grupos) > 2 else "")
    return linha


def _curto(texto, limite: int = 90) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= limite else texto[:limite - 1].rstrip() + "…"


def bloqueio_do_cupom_tirado(tirados: dict, preco_ml: float | None) -> list[str]:
    """04/11 (Breno: "retirar o cupom suspenso dos próximos posts, garantindo que o preço também seja corrigido. Sem
    preço confirmado, sinalize para revisão"). A normalizadora tira do post o cupom suspenso, esgotado ou vencido
    (lista_para_normalizadora) e o preço fica assim:
    - com o card do anúncio aberto, ela publica o preço do card (sem cupom no texto, o do card manda);
    - em qualquer caso, compara o preço final com o desta conferência, que já é sem o cupom tirado (preco_regra só
      com os cupons que valem; sem cupom, preco_ml): sobrou o desconto, "curadora vê R$ X" e ❌ comercial;
    - sem o preço do ML nesta conferência, não há preço confirmado: esta marca manda o post para revisão."""
    if not tirados or preco_ml:
        return []
    motivo = _curto(next(iter(tirados.values())).get("motivo"))
    return [f"cupom {', '.join(tirados)} sai do post ({motivo}) e a curadora não tem o preço do ML sem ele: revisar"]


def cupom_que_passou_a_sair(conf: dict, lista: dict) -> dict:
    """03/11: o cupom do post ficou suspenso ou esgotado DEPOIS da conferência. A normalizadora vai tirá-lo e o preço
    que a conferência calculou ainda tinha o desconto dele: a conferência sai marcada para conferir o preço."""
    novos = [c for c in conf.get("cupons_promo") or [] if c in lista and c not in (conf.get("tirados") or [])]
    if not novos:
        return conf
    aviso = (f"cupom {', '.join(novos)} passou a sair do post depois da conferência "
             f"({_curto(lista[novos[0]].get('motivo'))}): confira o preço sem ele")
    return {**conf, "bloqueios": list(conf.get("bloqueios") or []) + [aviso]}


def resumo_conferencia(cur, av, ts: int) -> dict:
    """O que a normalizadora precisa saber deste link (preços para comparar e uma linha para o relatório)."""
    card = av.card if av.card and av.card.ok else None
    preco_ml = float(card.preco) if card and card.preco else None
    lista = lista_para_normalizadora(cur.con, ts)
    tirados = {c: lista[c] for c in av.promo.cupons if c in lista}   # o que a normalizadora vai tirar deste post
    validos = [c for c in av.promo.cupons if c not in av.cupons_mortos]
    # Com cupom, só compara quando a regra do cupom é conhecida (vista nos grupos); sem regra não dá para afirmar nada.
    preco_regra = cur._preco_com_cupom_conhecido(validos, preco_ml) if (validos and preco_ml) else None
    quem = texto_confianca(av) if card else "não abriu no ML"
    if av.cupom_quente:  # cupom confirmado pela equipe há pouco: o preço esperado é o do ML menos esse cupom
        preco_regra = av.cupom_quente["preco"]
        quem += f" · 🔥 cupom {av.cupom_quente['codigo']} quente (confirmado às {av.cupom_quente['hora']})"
    try:
        origem = origem_da_promo(cur.con, av.identidade.chave, av.preco_efetivo, ts)
    except Exception:   # a linha de origem nunca derruba a conferência
        origem = ""
    return {
        "origem": origem,
        "ts": int(ts), "link": av.promo.link, "produto": av.promo.produto[:80],
        "nota": av.nota, "score": av.score, "conferido_ml": bool(card),
        "preco_ml": preco_ml, "preco_regra": preco_regra, "cupons_mortos": av.cupons_mortos,
        "risco": av.risco, "risco_motivo": av.risco_motivo, "conflito": av.conflito,
        "loja_1_produto": bool(av.produtos_vendedor == 1 and not av.loja_selo),
        "quem_vende": quem, "linha": f"{NOMES_NOTA.get(av.nota, av.nota)} {av.score} · {quem}",
        # 18/10 (Breno: "bom achado, mas cadê a frase loja oficial?"): a loja oficial que o card do ML deste link mostra
        # (vendedor + selo). A normalizadora usa quando o card que ela abre não mostra o vendedor (verificação de conta).
        "loja_oficial": re.sub(r"[_\s]+", " ", card.vendedor).strip() if card and card.loja_oficial and card.vendedor else "",
        # o que impede o post (a normalizadora marca ❌): compra internacional não pode ir para o grupo; 04/11: cupom que
        # ela tira do post, quando a conferência não tem o preço do ML para confirmar o preço sem ele
        "bloqueios": (["compra internacional (vendedor de fora do Brasil): não pode ser postado"]
                      if card and card.internacional else []) + bloqueio_do_cupom_tirado(tirados, preco_ml),
        "cupons_promo": list(av.promo.cupons), "tirados": list(tirados),   # 03/11: ver cupom_que_passou_a_sair
    }


class Conferente:
    def __init__(self, curadora, cfg: dict, tap: Path, feedback: Path, caixa: Path, saida: Path,
                 relogio=time.time, recuo_bytes: int = 3_000_000, preferencias_chamadas: Path | None = None):
        self.cur = curadora
        # arquivo de chamadas manuais da normalizadora; None = não mexe nele (testes, simulação)
        self.preferencias_chamadas = preferencias_chamadas
        self.con = curadora.con
        c = cfg.get("conferente") or {}
        self.grupo_normalizadora = _id(c.get("grupo_normalizadora"))
        self.grupo_edicao = _id(c.get("grupo_edicao"))
        self.tap = LeitorJsonl(tap, recuo_bytes)
        self.feedback = LeitorJsonl(feedback, recuo_bytes)
        self.caixa = LeitorJsonl(caixa, 200_000)
        self.saida = Path(saida)
        self.relogio = relogio
        self.conferencias: dict[str, dict] = {}
        self.normalizadas: dict[str, dict] = {}   # chave do link -> post que a normalizadora entregou
        self.edicoes: dict[str, dict] = {}        # chave do link -> última versão do Breno, esperando 3 min
        self.vistas: set[str] = set()
        self.ultima_exportacao = 0.0
        self.estatisticas = {"conferidas": 0, "edicoes": 0, "sinais": 0, "erros": 0, "chamadas_trocadas": 0}
        self.ultimo_erro = ""

    # ── entradas ───────────────────────────────────────────────
    def _ler_feedback(self, agora: float) -> None:
        for r in self.feedback.novas():
            link, copia = r.get("link") or "", r.get("published_copy") or ""
            if link and copia:
                self.normalizadas[chave_link(link)] = {
                    "ts": int(r.get("ts") or agora), "copia": copia,
                    "produto": r.get("display_product") or r.get("product") or ""}

    def _mensagem(self, r: dict, agora: float) -> tuple[str, str] | None:
        """Promo que chegou na normalizadora: devolve (texto, id) para conferir. Edição do Breno: guarda."""
        corpo = r.get("body") or ""
        achado = LINK.search(corpo)
        if r.get("fromMe") or not achado:
            return None
        ts = epoch(r.get("timestamp")) or int(agora)
        if agora - ts > RECENTE_S:
            return None
        grupo = _id(r.get("chatId"))
        if grupo and grupo == self.grupo_normalizadora:
            return corpo, str(r.get("messageId") or "")
        if grupo and grupo == self.grupo_edicao:
            bots = r.get("botIds") if isinstance(r.get("botIds"), list) else []
            if str(r.get("senderId") or "") not in bots:
                self.edicoes[chave_link(achado.group(0))] = {"ts": ts, "corpo": corpo,
                                                             "id": str(r.get("messageId") or "")}
        return None

    def _marcar_pendente(self, corpo: str, mid: str, agora: float) -> bool:
        """Avisa a normalizadora que a conferência deste link está a caminho (ela espera só nesse caso)."""
        achado = LINK.search(corpo)
        if not achado or (mid and mid in self.vistas):
            return False
        if mid:
            self.vistas.add(mid)
        self.conferencias[chave_link(achado.group(0))] = {"ts": int(agora), "pendente": True}
        return True

    def _conferir(self, corpo: str, agora: float) -> None:
        promo = ler_promo(corpo, self.cur.cfg["marcas"])
        achado = LINK.search(corpo)
        if not promo.valida or not promo.link:
            # Não é uma promo que a curadora lê: avisa a normalizadora para ela não ficar esperando.
            self.conferencias[chave_link(achado.group(0))] = {"ts": int(agora), "sem_conferencia": True}
            return
        try:
            av = self.cur.avaliar(promo, agora=int(agora), conferir_ml=True)
        except Exception as exc:  # um link com problema não para a conferência dos outros
            self.estatisticas["erros"] += 1
            self.ultimo_erro = f"{type(exc).__name__}: {exc}"
            av = None
        chave = chave_link(achado.group(0))
        if av is None:
            self.conferencias[chave] = {"ts": int(agora), "sem_conferencia": True}
            return
        self.conferencias[chave] = resumo_conferencia(self.cur, av, int(agora))
        self.estatisticas["conferidas"] += 1
        loja, nome = chaves_da_loja(av.card)
        if loja:   # se o Breno tirar o cupom deste post, é nesta loja que o cupom não vale
            guardar_loja_do_link(self.con, chave, int(agora), loja, nome)
            self.con.commit()

    def _tirou(self, chave: str, e: dict, norm: dict, codigo: str) -> bool:
        """Grava que ele tirou o cupom deste post (a regra está em esgotados.tirou_do_post)."""
        loja, nome = loja_do_link(self.con, chave)
        # edições dele com o cupom que ainda esperam os 3 min contam para o "2 seguidos". 03/11: também as de depois
        # desta (o lote que ele está editando): em 30/10 ele tirou de 2 posts e manteve em outros 6 três segundos depois
        confirmado = max((o["ts"] for o in self.edicoes.values() if o is not e
                          and codigo in cupons_da_mensagem(o["corpo"])), default=0)
        # 03/11 (Astra): as versões contam como uma retirada só dentro desta postagem, a partir de quando a normalizadora
        # a publicou (o mesmo link pode voltar outro dia, em outra campanha); 60 s de folga, como na comparação abaixo
        return tirou_do_post(self.con, codigo, e["ts"], (norm["produto"] or "")[:40], f"edicao:{e['id']}", loja, nome,
                             chave, confirmado, desde=norm["ts"] - 60)[1]

    def _processar_edicoes(self, agora: float) -> bool:
        """Versão final do Breno x post da normalizadora: cupom que ele tirou = esgotado; o que ficou = ativo."""
        mudou, gravou = False, False
        for chave, e in list(self.edicoes.items()):
            norm = self.normalizadas.get(chave)
            # 03/11 (Breno, pelo relatório da Astra: "a Edição é um rascunho"; ele tira e recoloca o cupom enquanto
            # monta o post). Até aqui (10/10) o cupom tirado valia na hora, sem esperar os 3 min: às 17:31 ele tirou
            # o MODA1010 e às 17:33 ainda saiu uma promo com ele. Agora a retirada também espera a versão assentar.
            # É uma proteção provisória contra mudança passageira, não prova de que ele terminou o post (3 min parado
            # e depois mais uma edição continua possível). O fim na hora fica com o "esgotado" que a equipe escreve no
            # GrupoFinal e com o "esgotou" dos grupos.
            if agora - e["ts"] < QUIETO_EDICAO_S:
                continue
            del self.edicoes[chave]
            if not norm or norm["ts"] > e["ts"] + 60:
                continue  # não saiu da normalizadora (ou ela normalizou de novo depois da edição)
            self.estatisticas["edicoes"] += 1
            hora = time.strftime("%H:%M", time.localtime(e["ts"]))
            produto = (norm["produto"] or "")[:40]
            loja = loja_do_link(self.con, chave)[0]
            for codigo, tipo in sinais_da_edicao(norm["copia"], e["corpo"]):
                gravou = True
                if tipo == "esgotou":
                    novo = self._tirou(chave, e, norm, codigo)
                else:
                    detalhe = (f"você pôs à mão no post do {produto} às {hora}" if tipo == "posto"
                               else f"você manteve no post do {produto} às {hora}")
                    novo = registrar_sinal(self.con, codigo, e["ts"], tipo, "breno", detalhe, f"edicao:{e['id']}",
                                           loja, chave)
                if novo:
                    self.estatisticas["sinais"] += 1
                    mudou = True
            if self.preferencias_chamadas is not None:
                # Chamada que a curadora preencheu no repertório: se ele trocou, ela sai; se manteve, conta um uso.
                if apos_edicao(self.con, norm["produto"] or "", norm["copia"], e["corpo"], agora=e["ts"],
                               preferencias=self.preferencias_chamadas) == "trocada":
                    self.estatisticas["chamadas_trocadas"] += 1
        if gravou:
            # 09/10: sempre fecha a transação. INSERT OR IGNORE que não insere nada (edição relida depois de um
            # reinício) também abre uma escrita; sem o commit, o banco ficava travado para todos (20:31-20:46).
            self.con.commit()
        return mudou

    # ── saída ──────────────────────────────────────────────────
    def exportar(self, agora: float) -> None:
        self.conferencias = {k: v for k, v in self.conferencias.items() if v["ts"] >= agora - GUARDA_CONFERENCIA_S}
        lista = lista_para_normalizadora(self.con, agora)
        dados = {"versao": 1, "atualizado": int(agora), "cupons_esgotados": lista,
                 "conferencias": {k: cupom_que_passou_a_sair(v, lista) for k, v in self.conferencias.items()}}
        self.saida.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.saida.with_name(self.saida.name + ".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.saida)  # troca atômica: a normalizadora nunca lê um arquivo pela metade
        self.ultima_exportacao = agora

    def rodada(self) -> None:
        agora = self.relogio()
        self._ler_feedback(agora)
        fila = [m for m in (self._mensagem(r, agora) for r in self.tap.novas()) if m]
        # 11/10: ao religar, a caixa do pipeline era relida inteira (quase 200 promos no fim do dia) e todas eram
        # conferidas de novo, uma a uma. Enquanto isso, as promos novas saíam da normalizadora sem a nota (13 posts
        # no dia) ou com a conferência velha do mesmo link. Promo de mais de 5 min já foi normalizada: não confere.
        fila += [(r.get("body") or "", str(r.get("messageId") or "")) for r in self.caixa.novas()
                 if agora - (epoch(r.get("timestamp")) or agora) <= CAIXA_RECENTE_S]
        fila = [(corpo, mid) for corpo, mid in fila if self._marcar_pendente(corpo, mid, agora)]
        if fila:
            self.exportar(agora)  # primeiro o "pendente" de todas, depois uma por uma
        for corpo, _ in fila:
            self._conferir(corpo, self.relogio())
            self.exportar(self.relogio())
        if self._processar_edicoes(agora) or agora - self.ultima_exportacao >= EXPORTAR_A_CADA_S:
            self.exportar(agora)

    def rodar(self, intervalo_s: float = 1.0, parar=lambda: False) -> None:
        while not parar():
            inicio = time.monotonic()
            try:
                self.rodada()
            except Exception as exc:  # nunca morrer por causa de uma linha estranha
                self.con.rollback()  # escrita que falhou no meio não pode prender o banco
                self.estatisticas["erros"] += 1
                self.ultimo_erro = f"{type(exc).__name__}: {exc}"
                print(f"[conferente] erro: {self.ultimo_erro}", flush=True)
            time.sleep(max(0.0, intervalo_s - (time.monotonic() - inicio)))
