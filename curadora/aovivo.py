"""Modo ao vivo: lê as mensagens novas dos grupos assim que chegam e alerta as melhores.

Fonte: o arquivo JSONL que a bridge do WhatsApp vai escrevendo na VPS
(~/.hermes/whatsapp/taps/messages.jsonl), uma mensagem por linha. Só lê; nunca escreve nele.

Para cada mensagem nova de um grupo-fonte:
  1. score rápido sem abrir o ML (milissegundos);
  2. confere no ML (cerca de 1 s): preço atual, vendedor, selo, reputação;
  3. grava a oferta no histórico (o preço normal fica sempre atualizado);
  4. se a nota for IMPERDÍVEL/BOA, alerta (sem repetir a mesma oferta).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import fotos_ruins
from . import grupo as vitrine
from .banco import aprender_catalogo, gravar_meta, inserir_oferta, ler_meta, registrar_avaliacao
from .cupons import cupons_da_mensagem, registrar_cupons
from .esgotados import registrar_aviso_da_equipe, registrar_sinais_de_grupo
from .parser import VOLUME, ler_promo, parece_perfume
from .postagens import registrar_postagem
from .score import para_dict

LIMIAR_CONFERIR_ML = 0    # toda promo do ML é conferida (04/10: até Médias e Ruins com vendedor e preço confirmados)
IDADE_MAX_S = 2 * 3600    # mensagem mais velha que isso (ex.: backlog) não gera alerta
REAPRENDER_S = 6 * 3600   # de quanto em quanto tempo o catálogo é recalculado com os dados novos


def epoch(ts) -> int:
    if isinstance(ts, dict):
        ts = ts.get("low") or 0
    ts = int(ts or 0)
    return ts // 1000 if ts > 10**12 else ts


class Escuta:
    def __init__(self, arquivo: Path, curadora, alertador, grupos: dict[str, str],
                 limiar_ml: int = LIMIAR_CONFERIR_ML, relogio=time.time):
        self.arquivo = Path(arquivo)
        self.cur = curadora
        self.con = curadora.con
        self.alertador = alertador
        self.grupos = grupos
        self.postagens = curadora.cfg.get("grupo_postagens") or {}
        # grupo da equipe (o dos avisos): promo mandada lá por alguém é um pedido de avaliação
        self.grupo_equipe = str((curadora.cfg.get("whatsapp_alertas") or {}).get("grupo") or "").split("@")[0]
        self.http_pedidos = None
        self.limiar_ml = limiar_ml
        self.relogio = relogio
        self.estatisticas = {"lidas": 0, "promos": 0, "conferidas_ml": 0, "alertas": 0}

    # ── posição no arquivo (sobrevive a reinício) ──────────────
    def _posicao(self) -> int:
        try:
            pos = int(ler_meta(self.con, "aovivo_posicao", "0"))
        except ValueError:
            pos = 0
        tamanho = self.arquivo.stat().st_size if self.arquivo.exists() else 0
        return 0 if pos > tamanho else pos  # arquivo foi trocado/rotacionado: recomeça

    def comecar_do_fim(self) -> None:
        """Ignora o que já está no arquivo e passa a ler só o que chegar daqui em diante."""
        gravar_meta(self.con, "aovivo_posicao", str(self.arquivo.stat().st_size if self.arquivo.exists() else 0))

    def processar_disponiveis(self) -> list:
        """Lê as linhas completas novas desde a última vez. Devolve as avaliações que viraram alerta."""
        if not self.arquivo.exists():
            return []
        pos = self._posicao()
        alertadas = []
        with self.arquivo.open("rb") as fh:
            fh.seek(pos)
            while True:
                bruto = fh.readline()
                if not bruto or not bruto.endswith(b"\n"):
                    break  # linha ainda sendo escrita: lê na próxima volta
                try:
                    av = self.processar_linha(bruto.decode("utf-8", "replace"))
                except sqlite3.OperationalError as exc:
                    # Banco ocupado (ex.: manutenção): para nesta linha e tenta de novo na próxima volta, sem
                    # perder nem repetir as anteriores (06/10: "database is locked" durante um recálculo).
                    print(f"[ao vivo] banco ocupado, tento de novo: {exc}", flush=True)
                    self.con.rollback()  # desfaz a escrita que falhou no meio, senão a conexão fica presa
                    break
                pos = fh.tell()
                if av is not None:
                    alertadas.append(av)
        gravar_meta(self.con, "aovivo_posicao", str(pos))
        return alertadas

    def processar_linha(self, linha: str):
        self.estatisticas["lidas"] += 1
        try:
            r = json.loads(linha)
        except ValueError:
            return None
        grupo_id = str(r.get("chatId", "")).split("@")[0]
        if r.get("mediaType") == "reaction" or r.get("nativeType") == "reactionMessage":
            # 17/10 (Breno: "só se eu puser o emoji de negativo"): 👎 num post do Normalizador ou da Edição tira a
            # foto dele do rodízio da normalizadora (fotos_ruins.py). Nunca derruba a escuta.
            if grupo_id in fotos_ruins.GRUPOS:
                try:
                    fotos_ruins.reacao(r, self.arquivo)
                except Exception as exc:  # noqa: BLE001
                    print(f"[fotos] reação não tratada: {type(exc).__name__}: {exc}", flush=True)
            return None
        if grupo_id == str(self.postagens.get("id", "")).split("@")[0] and r.get("body"):
            # Grupo da equipe (Grupo da Equipe): não é fonte de promo, é o que foi POSTADO.
            ts_post = epoch(r.get("timestamp")) or int(self.relogio())
            promo_grupofinal = ler_promo(r["body"], self.cur.cfg["marcas"])
            com_regra = {c for c, x in cupons_da_mensagem(r["body"]).items() if x.pct or x.valor}
            # 01/11 (2ª revisão): aviso com título que o leitor de promo toma por produto ("*NOVOS CUPONS MERCADO LIVRE*"
            # e o "máx R$20" como preço) também é aviso, e não post: o post da equipe tem o volume no nome e não traz a
            # regra do cupom. Gravado como post, o aviso seria a própria prova de que o cupom vale em perfume.
            if promo_grupofinal.valida and not (com_regra and not VOLUME.search(promo_grupofinal.produto or "")):
                registrar_postagem(self.con, self.cur, r["body"], ts_post,
                                   r.get("messageId") or "", self.postagens.get("nome", "grupo da equipe"))
            else:
                # Aviso de cupom da própria equipe ("MODA1010: 20% OFF máx R$40"): a regra dela vale mais.
                registrar_cupons(self.con, r["body"], ts_post, em_promo_de_perfume=True, da_equipe=True)
                # 01/11 (Breno: "quando chegar cupom no grupo, ativar a rodada"): ver esgotados.avisos_com_prova
                try:
                    avisados = registrar_aviso_da_equipe(self.con, r["body"], ts_post, str(r.get("messageId") or ""))
                    if avisados:
                        print(f"[cupom] aviso da equipe no GrupoFinal: {', '.join(avisados)}", flush=True)
                    # e o "esgotou"/"voltou" da equipe no GrupoFinal vale como o de um grupo-fonte: para a rodada que o aviso
                    # ligou (antes ela seguia até completar os 40 min do aviso)
                    registrar_sinais_de_grupo(self.con, r["body"], ts_post, self.postagens.get("nome", "GrupoFinal"),
                                              str(r.get("messageId") or ""), ignorar=com_regra)
                except sqlite3.OperationalError:
                    raise   # banco ocupado: a linha é lida de novo na próxima volta
                except Exception as exc:  # noqa: BLE001 - o aviso nunca derruba a escuta
                    print(f"[cupom] aviso da equipe não lido: {type(exc).__name__}: {exc}", flush=True)
                self.con.commit()
            return None
        if self.grupo_equipe and grupo_id == self.grupo_equipe:
            try:
                self.avaliar_pedido(r)
            except sqlite3.OperationalError:
                raise   # banco ocupado: a linha é lida de novo na próxima volta (nada foi respondido ainda)
            except Exception as exc:  # noqa: BLE001
                print(f"[pedido] erro: {type(exc).__name__}: {exc}", flush=True)
            return None
        if r.get("fromMe") or grupo_id not in self.grupos:
            return None
        promo = ler_promo(r.get("body") or "", self.cur.cfg["marcas"])
        ts_msg = epoch(r.get("timestamp")) or int(self.relogio())
        e_perfume = promo.valida and parece_perfume(promo.produto, r.get("body") or "")
        if registrar_cupons(self.con, r.get("body") or "", ts_msg, em_promo_de_perfume=e_perfume):
            self.con.commit()
        # "cupom X esgotou" num grupo: memória de cupom esgotado (esgotados.py)
        registrar_sinais_de_grupo(self.con, r.get("body") or "", ts_msg, self.grupos[grupo_id],
                                  str(r.get("messageId") or ""))
        self.con.commit()  # sempre: INSERT OR IGNORE que não insere nada também abre uma escrita
        if not e_perfume:
            return None
        self.estatisticas["promos"] += 1
        grupo = self.grupos[grupo_id]

        rapida = self.cur.avaliar(promo, agora=ts_msg, conferir_ml=False)
        av = rapida
        if rapida and rapida.score >= self.limiar_ml and self.cur.ml is not None and promo.marketplace == "ml":
            av = self.cur.avaliar(promo, agora=ts_msg, conferir_ml=True)
            self.estatisticas["conferidas_ml"] += 1
        if av is None:
            return None
        av.grupo = grupo
        analise = self.cur.ident.analisar(promo.produto)
        if analise.base:
            inserir_oferta(self.con, r.get("messageId") or "", ts_msg, grupo, promo, av.identidade)
            self.con.commit()
        agora = self.relogio()
        alertou = agora - ts_msg <= IDADE_MAX_S and \
            self.alertador.talvez_alertar(av, "ao vivo", int(agora), atraso_s=round(agora - ts_msg, 1))
        registrar_avaliacao(self.con, ts_msg, "ao vivo", grupo, av.identidade.chave, av.nota, av.score,
                            alertou, para_dict(av))
        if alertou:
            self.estatisticas["alertas"] += 1
            return av
        return None

    def avaliar_pedido(self, r: dict) -> str | None:
        """13/10 (Breno): "ter a função avaliar promos quando eu enviar uma promo no grupo". É o "Avaliar uma promo"
        do painel, no WhatsApp: alguém manda a promo no grupo da equipe e a curadora responde, citando a mensagem,
        com a nota, o preço conferido no ML e os avisos. Não grava nada: é só a resposta. Devolve o texto mandado."""
        corpo = (r.get("body") or "").strip()
        mid = str(r.get("messageId") or "")
        agora = self.relogio()
        if (r.get("fromMe") or not corpo or str(r.get("mediaType") or "") == "reaction"
                or corpo.startswith(("[Reaction", "🧠")) or agora - (epoch(r.get("timestamp")) or agora) > vitrine.PEDIDO_MAX_S):
            return None
        v = vitrine.configuracao(self.cur.cfg)
        if not (v["ligado"] and v["avaliar"]):
            return None
        promo = ler_promo(corpo, self.cur.cfg["marcas"])
        if not promo.valida and not promo.link:
            return None   # conversa da equipe, não é promo
        try:
            respondidos = json.loads(ler_meta(self.con, "pedidos_respondidos", "[]"))
        except ValueError:
            respondidos = []
        if mid and mid in respondidos:
            return None
        av = self.cur.avaliar(promo, agora=int(agora), conferir_ml=self.cur.ml is not None) if promo.valida else None
        if av is None:
            texto = "🧠 Curadora: não consegui ler o produto e o preço dessa mensagem. Mande a promo inteira (nome, preço e link)."
        else:
            texto = vitrine.avaliacao(av, completo=True)
        if mid:   # marca antes de responder: se a ponte falhar, fica sem resposta, mas nunca responde duas vezes
            gravar_meta(self.con, "pedidos_respondidos", json.dumps((respondidos + [mid])[-200:]))
            self.con.commit()
        vitrine.responder(v["grupo"], texto, citar=mid, http=self.http_pedidos)
        return texto

    def reaprender_se_preciso(self, agora: float | None = None) -> bool:
        """Recalcula o catálogo (gênero/volume) com o histórico novo, no máximo a cada REAPRENDER_S."""
        agora = agora if agora is not None else time.time()
        try:
            ultimo = float(ler_meta(self.con, "catalogo_aprendido_em", "0"))
        except ValueError:
            ultimo = 0.0
        if agora - ultimo < REAPRENDER_S:
            return False
        self.cur.catalogo = aprender_catalogo(self.con, self.cur.ident)
        gravar_meta(self.con, "catalogo_aprendido_em", str(int(agora)))
        return True

    def rodar(self, intervalo_s: float = 1.0, parar=lambda: False) -> None:
        while not parar():
            inicio = time.monotonic()
            try:
                self.processar_disponiveis()
                self.reaprender_se_preciso()
            except Exception as exc:  # nunca morrer por causa de uma mensagem estranha
                print(f"[ao vivo] erro: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(max(0.0, intervalo_s - (time.monotonic() - inicio)))


def arquivo_padrao() -> Path:
    return Path(os.path.expanduser("~/.hermes/whatsapp/taps/messages.jsonl"))
