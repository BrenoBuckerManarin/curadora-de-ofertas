"""Painel da curadora: feed ao vivo, avaliar promo colada, histórico de preço e voto 👍/👎.

Uso (no PC, para testar):
  python painel.py --simular 6        as últimas 6h de mensagens reais chegam de novo, como se fosse agora
  python painel.py                    só o painel (avaliar promos coladas e ver o que já foi avaliado)
  python painel.py --banco dados/simulacao.db      reabre a última simulação (feed e votos continuam)
  python painel.py --arquivo messages.jsonl --do-fim    lendo um arquivo de mensagens de verdade
  python painel.py --arquivo ... --do-fim --vigia --radar   tudo junto (é como roda na VPS)
Abre em http://127.0.0.1:8765

Só biblioteca padrão do Python. Escuta apenas em 127.0.0.1 (o login vem antes de hospedar).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from http.cookies import SimpleCookie

from curadora import RAIZ, carregar_config
from curadora.acesso import SESSAO_S, Acesso, AcessoNegado, Limitador, Usuario, normalizar_nome
from curadora.alertas import Alertador, SaidaArquivo, SaidaWhatsAppDoConfig
from curadora.aovivo import Escuta, epoch
from curadora.banco import CAMINHO_PADRAO, conectar, foto_do_produto, ler_meta, registrar_avaliacao
from curadora.calibracao import calibrar, carregar_exemplos, gravar, para_painel
from curadora.conferente import Conferente
from curadora.ml import ConsultorML
from curadora.ml_api import ClienteML
from curadora.parser import _marketplace
from curadora.postagens import (REPETIR_APOS_S, REPETIR_EQUIPE_S, VEIO_DO_PIPELINE_S, ainda_vale_o_post,
                                 enviados_pelo_pipeline, post_da_equipe, ultimos_posts)
from curadora import travas
from curadora.postagens import resumo as resumo_postagens
from curadora import chamadas, vigiados
from curadora.identidade import Identificador
from curadora.radar import Radar
from curadora.experimento import Experimento
from curadora import reconferir
from curadora.reconferir import Reconferidor
from curadora.score import Curadora, para_dict
from curadora.vigia import Vigia

PAGINA = RAIZ / "painel" / "index.html"
PAGINA_CHAMADAS = RAIZ / "painel" / "chamadas.html"
MENSAGENS = RAIZ.parent / "dados" / "mensagens_grupos_perfume.jsonl.gz"
ESTADO: dict = {"modo": "parado", "banco": CAMINHO_PADRAO, "sim_total": 0, "sim_feitas": 0, "sem_ml": False}


# ── modo ao vivo / simulação (thread de fundo) ─────────────────
def preparar_simulacao(horas: float) -> tuple[Path, list[dict]]:
    """Cópia do banco sem as ofertas da janela, e as mensagens da janela em ordem."""
    with gzip.open(MENSAGENS, "rt", encoding="utf-8") as fh:
        todas = [json.loads(l) for l in fh]
    fim = max(epoch(r.get("timestamp")) for r in todas)
    inicio = fim - int(horas * 3600)
    janela = sorted((r for r in todas if epoch(r.get("timestamp")) >= inicio), key=lambda r: epoch(r.get("timestamp")))
    banco = RAIZ / "dados" / "simulacao.db"
    for sufixo in ("", "-wal", "-shm"):
        Path(str(banco) + sufixo).unlink(missing_ok=True)
    shutil.copy(CAMINHO_PADRAO, banco)
    con = conectar(banco)
    con.execute("DELETE FROM ofertas WHERE ts >= ?", (inicio,))
    for tabela in ("alertas", "avaliacoes", "ml_cards"):
        con.execute(f"DELETE FROM {tabela}")
    con.execute("DELETE FROM meta WHERE nome = 'aovivo_posicao'")
    con.commit()
    con.close()
    return banco, janela


def rodar_simulacao(janela: list[dict], intervalo_s: float, cfg: dict, sem_ml: bool) -> None:
    con = conectar(ESTADO["banco"])
    cur = Curadora(cfg, con, None if sem_ml else ConsultorML(con))
    tap = RAIZ / "dados" / "simulacao_messages.jsonl"
    tap.write_text("", encoding="utf-8")
    escuta = Escuta(tap, cur, Alertador(con, [SaidaArquivo(RAIZ / "dados" / "simulacao_alertas.jsonl")]),
                    cfg["grupos_fonte"])
    ESTADO.update(modo="simulação", sim_total=len(janela), sim_feitas=0)
    for r in janela:
        with tap.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(r, timestamp=int(time.time())), ensure_ascii=False) + "\n")
        escuta.processar_disponiveis()
        ESTADO["sim_feitas"] += 1
        time.sleep(intervalo_s)
    ESTADO["modo"] = "simulação concluída"


def saidas_reais() -> list:
    """Alertas de verdade: arquivo de registro + grupo de WhatsApp (se configurado em config.json).

    A simulação usa só arquivo: um ensaio nunca manda nada para o WhatsApp.
    """
    return [SaidaArquivo(RAIZ / "dados" / "alertas.jsonl"), SaidaWhatsAppDoConfig()]


def rodar_arquivo(arquivo: Path, do_fim: bool, cfg: dict, sem_ml: bool) -> None:
    con = conectar(ESTADO["banco"])
    cur = Curadora(cfg, con, None if sem_ml else ConsultorML(con), cliente_api=None if sem_ml else ClienteML())
    escuta = Escuta(arquivo, cur, Alertador(con, saidas_reais()), cfg["grupos_fonte"])
    # --do-fim só na primeira vez: num reinício, continua de onde parou (não perde o que chegou no meio).
    if do_fim and not ler_meta(con, "aovivo_posicao", ""):
        escuta.comecar_do_fim()
    ESTADO["modo"] = "ao vivo"
    ESTADO["escuta"] = escuta
    escuta.rodar()


def iniciar_vigia(cfg: dict, intervalo_s: float) -> None:
    con = conectar(ESTADO["banco"])
    vigia = Vigia(Curadora(cfg, con, ConsultorML(con, ttl=max(60, int(intervalo_s) - 60)), cliente_api=ClienteML()),
                  Alertador(con, saidas_reais()))
    ESTADO["vigia"] = vigia
    threading.Thread(target=vigia.rodar, kwargs={"intervalo_s": intervalo_s}, daemon=True, name="vigia").start()


def escolha_da_curadora(con, excluir: set) -> dict | None:
    """O que a curadora postaria agora no experimento: a melhor promo vista nas últimas 6h, sem as que ela
    mesma já escolheu nas últimas 6h (independente do que a equipe postou)."""
    agora = time.time()
    candidatas = [c for c in _cards(con) if _fresca(c, agora) and c.get("chave") not in excluir]
    melhor = min(candidatas, key=lambda c: (-c["score"], -c["ts"]), default=None)
    return _completar(con, melhor) if melhor else None


def iniciar_experimento(cfg: dict) -> None:
    con = conectar(ESTADO["banco"])
    exp = Experimento(con, lambda excluir: escolha_da_curadora(con, excluir), cfg)
    ESTADO["experimento"] = exp
    threading.Thread(target=exp.rodar, daemon=True, name="experimento").start()


def iniciar_reconferidor(cfg: dict, intervalo_s: float = 300) -> None:
    """Reabre no ML as promos de Excelentes e Boas (a cada 15 min cada uma) para não mostrar preço velho."""
    con = conectar(ESTADO["banco"])

    def ler_dados(id_: int) -> dict:
        linha = con.execute("SELECT dados FROM avaliacoes WHERE id = ?", (id_,)).fetchone()
        return json.loads(linha["dados"]) if linha else {}
    rec = Reconferidor(Curadora(cfg, con, ConsultorML(con, ttl=600), cliente_api=ClienteML()),
                       lambda: _cards(con), ler_dados)
    ESTADO["reconferidor"] = rec
    threading.Thread(target=rec.rodar, kwargs={"intervalo_s": intervalo_s}, daemon=True, name="reconferir").start()


def iniciar_conferente(cfg: dict) -> None:
    """Segunda leitura de tudo que entra na normalizadora + memória de cupom esgotado (curadora/conferente.py)."""
    c = cfg.get("conferente") or {}
    if not c.get("ligado"):
        return
    con = conectar(ESTADO["banco"])
    hermes = Path(os.path.expanduser("~/.hermes"))
    cur = Curadora(cfg, con, ConsultorML(con, ttl=60), cliente_api=ClienteML())
    cur.sem_cupom_velho = True
    conf = Conferente(cur, cfg,
                      tap=hermes / "whatsapp/taps/messages.jsonl",
                      feedback=hermes / "perfume_image_bank/logs/normalizer_feedback.jsonl",
                      caixa=hermes / "run/curadora_normalizer_inbox.jsonl",
                      saida=hermes / "run/curadora_para_normalizadora.json",
                      preferencias_chamadas=chamadas.PREFERENCIAS)
    ESTADO["conferente"] = conf
    threading.Thread(target=conf.rodar, daemon=True, name="conferente").start()


def iniciar_pagina_de_cupons(cfg: dict) -> None:
    """13/10: a página oficial de cupons dos afiliados do ML como sinal de cupom esgotado (curadora/pagina_cupons.py)."""
    if not (cfg.get("pagina_de_cupons") or {}).get("ligado"):
        return
    from curadora.pagina_cupons import Leitor
    leitor = Leitor(conectar(ESTADO["banco"]))
    ESTADO["pagina_de_cupons"] = leitor
    threading.Thread(target=leitor.rodar, daemon=True, name="pagina-de-cupons").start()


def iniciar_radar(cfg: dict, intervalo_s: float) -> None:
    con = conectar(ESTADO["banco"])
    radar = Radar(Curadora(cfg, con, None), ClienteML(), Alertador(con, saidas_reais()))
    ESTADO["radar"] = radar
    threading.Thread(target=radar.rodar, kwargs={"intervalo_s": intervalo_s}, daemon=True, name="radar").start()


def fontes() -> list[dict]:
    """O que está alimentando a curadora agora (aparece nas configurações do painel)."""
    saida = []
    escuta = ESTADO.get("escuta")
    if ESTADO["modo"] in ("simulação", "simulação concluída"):
        saida.append({"nome": "Grupos (simulação)", "ok": True,
                      "detalhe": f"{ESTADO['sim_feitas']}/{ESTADO['sim_total']} mensagens reproduzidas"})
    elif escuta:
        e = escuta.estatisticas
        saida.append({"nome": "Grupos ao vivo", "ok": True,
                      "detalhe": f"{e['promos']} promos lidas, {e['conferidas_ml']} conferidas no ML"})
    else:
        saida.append({"nome": "Grupos ao vivo", "ok": False, "detalhe": "desligado neste modo"})
    vigia = ESTADO.get("vigia")
    saida.append({"nome": "Vigia dos links", "ok": bool(vigia),
                  "detalhe": f"{len(vigia.nomes)} top vendidos" if vigia else "desligada (use --vigia)"})
    exp = ESTADO.get("experimento")
    if exp:
        saida.append({"nome": "Experimento curadora × equipe", "ok": True,
                      "detalhe": f"{exp.anotados_hoje} escolhas anotadas desde que ligou "
                                 f"({exp.c['hora_ini']}–{exp.c['hora_fim']}, a cada {exp.c['intervalo_min']} min)"})
    conf = ESTADO.get("conferente")
    if conf:
        e = conf.estatisticas
        saida.append({"nome": "Segunda leitura da normalizadora", "ok": not conf.ultimo_erro,
                      "detalhe": conf.ultimo_erro or f"{e['conferidas']} promos conferidas, {e['edicoes']} edições lidas, "
                                                     f"{e['sinais']} sinais de cupom"})
    rec = ESTADO.get("reconferidor")
    if rec:
        e = rec.estatisticas
        saida.append({"nome": "Reconferência (Excelentes e Boas)", "ok": True,
                      "detalhe": f"{e['reconferidas']} reconferidas, {e['mudaram']} mudaram de preço ou nota"})
    radar = ESTADO.get("radar")
    if not radar:
        detalhe, ok = "desligado (use --radar)", False
    elif not radar.cliente.configurado():
        detalhe, ok = "falta acesso à API do ML: rode configurar_ml.py", False
    else:
        r = radar.estatisticas
        ok = not radar.ultimo_erro
        detalhe = radar.ultimo_erro or f"{r['rodadas']} voltas, {r['ofertas_lidas']} ofertas lidas"
        if radar.cliente.emprestado:
            detalhe += " · acesso da coletora (só leitura)"
    saida.append({"nome": "Radar da API do ML", "ok": ok, "detalhe": detalhe})
    return saida


# ── API ─────────────────────────────────────────────────────────
def api_status(con, q) -> dict:
    hoje = int(time.time()) - 24 * 3600
    linha = con.execute("""SELECT COUNT(*) n, SUM(alertado) a, SUM(nota IN ('IMPERDÍVEL','BOA')) b, MAX(ts) u
                           FROM avaliacoes WHERE ts >= ?""", (hoje,)).fetchone()
    exemplos = carregar_exemplos(con)  # mesma contagem da calibração: só 👍/👎, uma vez por oferta
    votos = {"bons": sum(e.boa for e in exemplos), "ruins": sum(not e.boa for e in exemplos)}
    lista = fontes()
    grupo_equipe = (carregar_config().get("grupo_postagens") or {}).get("nome")
    if grupo_equipe:
        r = resumo_postagens(con, int(time.time()) - 7 * 86400)
        lista.append({"nome": f"Postagens no {grupo_equipe} (7 dias)", "ok": True,
                      "detalhe": f"{r['postadas']} postadas; a curadora já tinha apontado {r['curadora_apontou_antes']} "
                                 f"como Boa/Imperdível e viu {r['curadora_ja_tinha_visto']} antes"})
    return {"modo": ESTADO["modo"], "sim_total": ESTADO["sim_total"], "sim_feitas": ESTADO["sim_feitas"],
            "avaliadas_24h": linha["n"] or 0, "alertas_24h": linha["a"] or 0, "boas_24h": linha["b"] or 0,
            "ultima": linha["u"], "votos_bons": votos["bons"] or 0, "votos_ruins": votos["ruins"] or 0,
            "fontes": lista}


# As 4 seções do painel e a nota de cada uma.
SECOES = {"excelentes": "IMPERDÍVEL", "boas": "BOA", "medias": "OK", "ruins": "NÃO VALE"}
JANELA_FEED_S = 48 * 3600
TRAVAS_PIPELINE = RAIZ / "dados" / "pipeline_travas.json"   # por que o pipeline pulou cada produto (travas.py)
REGISTRO_PIPELINE = RAIZ / "dados" / "pipeline.jsonl"   # o que o pipeline mandou (para a regra das 8h)


ORDEM_NOTA = {"NÃO VALE": 0, "OK": 1, "BOA": 2, "IMPERDÍVEL": 3}
MELHORA_MINIMA = 3  # pontos de score para o mesmo produto voltar a aparecer (ou subir de seção)
MESMA_OFERTA = 0.02  # preço a até 2%: é a mesma oferta repostada/reconferida
QUEDA_MINIMA = 0.03  # ou preço 3% menor (sem nota pior): também volta a aparecer
_cache_cards: dict = {}  # por conexão: {id(con): (assinatura, cards)}
_trava_cards = threading.Lock()


def _trocar_chave(por_produto: dict, por_id: dict, card: dict, nova: str) -> None:
    """25/10 (Breno: "ainda está duplicado aqui no painel"): a identidade mudou (Lipx, "Parf", "para"...) e a
    reconferência já vem com a chave nova, mas o card nascido com a antiga seguia vivo ao lado do novo (o Bareeq de 23/10
    como "bareeq-dhahab-parf", reconferido a cada 20 min). O card passa para a chave nova; se ela já tem a mesma oferta,
    os dois viram um só; se tem outra oferta, fica a reconferida por último."""
    if por_produto.get(card["chave"]) is card:
        del por_produto[card["chave"]]
    outro = por_produto.get(nova)
    card["chave"] = nova
    if outro is None or outro is card:
        por_produto[nova] = card
        return
    if abs(card["preco"] - outro["preco"]) > MESMA_OFERTA * max(card["preco"], outro["preco"]):
        if card["ts"] >= outro["ts"]:
            por_produto[nova] = card
        return
    fica, sai = (card, outro) if card["score"] > outro["score"] else (outro, card)
    fica["ids"] += [i for i in sai["ids"] if i not in fica["ids"]]
    fica["ts"], fica["primeiro_ts"] = max(fica["ts"], sai["ts"]), min(fica["primeiro_ts"], sai["primeiro_ts"])
    fica["postada"] = max(fica["postada"] or 0, sai["postada"] or 0)
    fica["grupos"] += [g for g in sai["grupos"] if g not in fica["grupos"]]
    fica["vista_de_novo"] += sai["vista_de_novo"]
    por_produto[nova] = fica
    for i, c in list(por_id.items()):
        if c is sai:
            por_id[i] = fica


def _cards(con) -> list[dict]:
    """UM card por produto nas últimas 48h (pedido do Breno em 04/10: "só deve aparecer o mesmo produto
    de novo se tiver score melhor").

    Em ordem de chegada, cada avaliação de um produto que já tem card:
      - mesma oferta (preço a até 2%): junta no card (grupos, "conferido há", postada);
      - MELHOR (score +3, seção acima ou preço 3% menor sem nota pior): vira o card do produto; o antigo some
        e o novo mostra "antes";
      - pior ou igual: some (só conta como "vista de novo").
    Só lê colunas leves; o JSON completo é aberto depois, apenas para os cards que vão para a tela.
    Resultado em cache até chegar avaliação nova, voto ou postagem.
    """
    desde = int(time.time()) - JANELA_FEED_S
    assinatura = tuple(con.execute(
        "SELECT max(id), count(*), total(ts), total(postada), count(voto), total(voto) FROM avaliacoes WHERE ts >= ?",
        (desde,)).fetchone()) + (desde // 300, id(con),
                                 REGISTRO_PIPELINE.stat().st_mtime_ns if REGISTRO_PIPELINE.exists() else 0)
    with _trava_cards:
        guardado = _cache_cards.get(id(con))
        if guardado and guardado[0] == assinatura:
            return guardado[1]
    por_produto: dict[str, dict] = {}
    por_anuncio: dict[str, dict] = {}   # anúncio do ML conferido -> card (25/10: o mesmo anúncio com outro nome)
    por_id: dict[int, dict] = {}
    for r in con.execute("SELECT id, ts, origem, grupo, nota, score, alertado, voto, postada, chave, "
                         "json_extract(dados, '$.preco') AS preco, json_extract(dados, '$.reconferida_de') AS ref, "
                         "json_extract(dados, '$.link') AS link, json_extract(dados, '$.ml.url') AS anuncio "
                         "FROM avaliacoes WHERE ts >= ? ORDER BY ts, id", (desde,)):
        if r["nota"] not in ORDEM_NOTA:
            # "FORA": não é perfume (Lego, cartas...), não aparece nem na busca. 11/10: se foi a reconferência que
            # descobriu (o título do anúncio no ML é de shampoo, camisa...), o card que já existia sai junto.
            fora = por_id.pop(int(r["ref"]), None) if r["ref"] else None
            if fora is not None:
                for k in [k for k, v in por_produto.items() if v is fora]:
                    del por_produto[k]
            continue
        if r["link"] and _marketplace(r["link"]) != "ml":
            continue  # 11/10: promo da Amazon (ou de outra loja) não entra: a curadora é de Mercado Livre
        preco = r["preco"] or 0.0
        if r["ref"]:
            # Reconferência: é a situação ATUAL do card (vale mesmo se piorou: preço velho não fica no topo).
            card = por_id.get(int(r["ref"]))
            if card is not None:
                card["ts"] = max(card["ts"], r["ts"])
                mudou = r["nota"] != card["nota_original"] or \
                    abs(preco - card["preco_original"]) > MESMA_OFERTA * max(preco, card["preco_original"])
                card["atual"] = {"id": r["id"], "preco": preco, "nota": r["nota"], "score": r["score"]} if mudou else None
                card.update(nota=r["nota"], score=r["score"], preco=preco)
                if r["chave"] and r["chave"] != card["chave"]:
                    _trocar_chave(por_produto, por_id, card, r["chave"])
            continue
        chave = r["chave"] or f"#{r['id']}"  # produto não identificado: não junta com nada
        card = por_produto.get(chave)
        if card and preco and abs(preco - card["preco"]) <= MESMA_OFERTA * max(preco, card["preco"]):
            por_id[r["id"]] = card
            card["ids"].append(r["id"])
            card["ts"] = max(card["ts"], r["ts"])
            card["postada"] = max(card["postada"], r["postada"] or 0)
            if r["grupo"] and r["grupo"] not in card["grupos"]:
                card["grupos"].append(r["grupo"])
            if r["score"] > card["score"]:  # mesma oferta reavaliada com nota um pouco maior: mostra a melhor
                card.update(id=r["id"], score=r["score"], nota=r["nota"], voto=r["voto"], alertado=bool(r["alertado"]),
                            nota_original=r["nota"], atual=None)
            if r["anuncio"]:
                por_anuncio.setdefault(r["anuncio"].split("?")[0], card)
            continue
        # 25/10 (Breno: "ainda está duplicado"): o mesmo anúncio conferido no ML, no mesmo preço, com outro nome que
        # caiu em outra chave ("Natura Kaiak Tradicional" em kaiak|100 x "Kaiak Masculino Tradicional" em kaiak|100|m)
        # junta no card que já existe, em vez de abrir um segundo.
        anuncio = (r["anuncio"] or "").split("?")[0]
        mesmo = por_anuncio.get(anuncio) if anuncio else None
        if (mesmo is not None and mesmo is not card and preco and por_produto.get(mesmo["chave"]) is mesmo
                and abs(preco - mesmo["preco"]) <= MESMA_OFERTA * max(preco, mesmo["preco"])):
            por_id[r["id"]] = mesmo
            mesmo["ids"].append(r["id"])
            mesmo["ts"] = max(mesmo["ts"], r["ts"])
            mesmo["postada"] = max(mesmo["postada"], r["postada"] or 0)
            if r["grupo"] and r["grupo"] not in mesmo["grupos"]:
                mesmo["grupos"].append(r["grupo"])
            continue
        # A nota satura (R$150 e R$130 podem dar o mesmo 78): preço bem menor sem nota pior também é melhora.
        nota_nova, nota_card = ORDEM_NOTA.get(r["nota"], 0), (ORDEM_NOTA.get(card["nota"], 0) if card else 0)
        melhorou = card is None or r["score"] >= card["score"] + MELHORA_MINIMA or nota_nova > nota_card \
            or (nota_nova >= nota_card and preco and preco <= card["preco"] * (1 - QUEDA_MINIMA))
        if not melhorou:
            card["vista_de_novo"] += 1
            continue
        por_produto[chave] = {
            "id": r["id"], "ids": [r["id"]], "ts": r["ts"], "primeiro_ts": r["ts"], "origem": r["origem"],
            "grupo": r["grupo"] or "", "grupos": [r["grupo"]] if r["grupo"] else [], "nota": r["nota"],
            "score": r["score"], "preco": preco, "voto": r["voto"], "postada": r["postada"] or 0,
            "alertado": bool(r["alertado"]), "vista_de_novo": 0, "atual": None, "chave": chave,
            "nota_original": r["nota"], "preco_original": preco,
            "antes": None if card is None else {"id": card["id"], "score": card["score"], "nota": card["nota"],
                                                "preco": card["preco"], "ts": card["primeiro_ts"]},
        }
        por_id[r["id"]] = por_produto[chave]
        if anuncio:
            por_anuncio[anuncio] = por_produto[chave]
    cards = sorted(por_produto.values(), key=lambda c: (c["primeiro_ts"], c["id"]), reverse=True)
    # 10/10 (Breno: "se ainda está como Excelente devia ter ido no pipeline"): produto postado há menos de 8h só
    # aparece como não postado se estiver 8% mais barato que o post. É a mesma regra que o pipeline usa.
    # 11/10 (Breno, olhando as Boas: "alguns desses já passaram na pipeline, aí não deveriam estar aí"): eram 19.
    # O painel só olhava o GrupoFinal, e o card postado na véspera ficava com a marca velha. Agora vale a mesma lista
    # do pipeline (GrupoFinal + o que ele mandou) e a marca mais nova.
    posts = ultimos_posts(con, time.time(), REGISTRO_PIPELINE)
    for c in cards:
        ultimo = posts.get(c["chave"])
        if ultimo and ultimo[1] > (c["postada"] or 0) and ainda_vale_o_post(ultimo, c["preco"]):
            c["postada"] = ultimo[1]
    with _trava_cards:
        _cache_cards[id(con)] = (assinatura, cards)
    return cards


def _completar(con, card: dict, grupofinal: dict | None = None) -> dict:
    """Abre o JSON completo só do card que vai para a tela (o da reconferência, se o preço mudou)."""
    atual = card.get("atual")
    linha = con.execute("SELECT dados FROM avaliacoes WHERE id = ?", (atual["id"] if atual else card["id"],)).fetchone()
    item = json.loads(linha["dados"]) if linha else {}
    item.update({k: v for k, v in card.items() if k != "preco" and not (k == "grupo" and not v)})
    if atual:
        item["era"] = {"preco": card["preco_original"], "nota": card["nota_original"]}
    item["postada_em"] = (grupofinal or {}).get(card.get("chave"))  # postado no grupo da equipe (6h)
    item["alvo"] = _alvo_do_card(card, vigiados.ativos(con))      # alerta por produto atingido
    if card["nota"] in ("IMPERDÍVEL", "BOA") and time.time() - card["ts"] > CONFERIDA_S:
        item["sem_conferir_h"] = int((time.time() - card["ts"]) // 3600)  # o ML não deixou reconferir
    # Foto do anúncio conferido; se não foi conferido, a última foto que o ML mostrou desse produto.
    item["foto"] = (item.get("ml") or {}).get("foto") or foto_do_produto(con, item.get("chave", ""))
    item["costuma"] = preco_de_costume(con, item.get("chave", ""))
    return item


COSTUME_DIAS = 30
COSTUME_MIN_DIAS = 5
COSTUME_CACHE_S = 30 * 60
_cache_costume: dict[str, tuple[float, float | None]] = {}


def preco_de_costume(con, chave: str, agora: float | None = None) -> float | None:
    """Por quanto o produto costuma sair nos grupos: a mediana do menor preço de cada dia, em 30 dias.

    25/10 (Breno: "não mexe na nota e mostra no card"). Só informação: o "normal" da nota vem quase todo dos
    anúncios do ML (Kaiak: normal R$ 115, mas nos grupos sai por R$ 87), e o preço de sempre aparecia como 19%
    abaixo. Precisa de 5 dias com promo do produto; sem isso, None."""
    import statistics
    if not chave:
        return None
    agora = agora or time.time()
    guardado = _cache_costume.get(chave)
    if guardado and agora - guardado[0] < COSTUME_CACHE_S:
        return guardado[1]
    linhas = con.execute("SELECT ts, preco FROM ofertas WHERE chave = ? AND ts >= ? AND preco > 0",
                         (chave, agora - COSTUME_DIAS * 86400)).fetchall()
    valor = None
    if linhas:
        centro = statistics.median(r[1] for r in linhas)
        por_dia: dict[str, float] = {}
        for ts, preco in linhas:
            if centro * 0.5 <= preco <= centro * 2:   # erro de leitura ou outro produto com o mesmo nome
                dia = time.strftime("%Y-%m-%d", time.localtime(ts))
                por_dia[dia] = min(por_dia.get(dia, preco), preco)
        if len(por_dia) >= COSTUME_MIN_DIAS:
            valor = round(statistics.median(por_dia.values()), 2)
    _cache_costume[chave] = (agora, valor)
    return valor


def _partes_da_chave(chave: str) -> tuple[frozenset, str, bool]:
    base, _, resto = (chave or "").partition("|")
    return frozenset(t for t in base.replace("kit:", "").split("-") if t), resto.split("|")[0], base.startswith("kit:")


def _alvo_do_card(card: dict, lista) -> float | None:
    tokens, volume, kit = _partes_da_chave(card.get("chave", ""))
    precos = [v["preco_alvo"] for v in lista if vigiados.casa(v, tokens, volume, kit) and card["preco"] <= v["preco_alvo"]]
    return max(precos) if precos else None


def _normal_do_vigiado(con, v) -> float | None:
    """Mediana de 30 dias das ofertas do mesmo produto (para a pessoa escolher um alvo realista)."""
    import statistics
    prefixo = ("kit:" if v["kit"] else "") + "-".join(json.loads(v["tokens"])) + "|" + (v["volume"] or "")
    precos = [r["preco"] for r in con.execute("SELECT preco FROM ofertas WHERE ts >= ? AND chave LIKE ?",
                                              (time.time() - 30 * 86400, prefixo + "%"))]
    return round(statistics.median(precos), 2) if len(precos) >= 3 else None


def api_vigiados(con, q) -> list:
    """Alertas por produto cadastrados, com o menor preço visto nas últimas 48h e o normal de 30 dias."""
    cards = _cards(con)
    saida = []
    for v in vigiados.ativos(con):
        iguais = [c for c in cards if vigiados.casa(v, *_partes_da_chave(c["chave"]))]
        menor = min(iguais, key=lambda c: c["preco"], default=None)
        saida.append({"id": v["id"], "nome": v["nome"], "volume": v["volume"], "preco_alvo": v["preco_alvo"],
                      "criado_por": v["criado_por"], "alerta_ts": v["alerta_ts"], "alerta_preco": v["alerta_preco"],
                      "menor": None if menor is None else {"preco": menor["preco"], "nota": menor["nota"],
                                                            "ts": menor["ts"]},
                      "normal": _normal_do_vigiado(con, v)})
    return saida


def api_vigiados_adicionar(con, corpo: dict) -> dict:
    cfg = carregar_config()
    return vigiados.adicionar(con, Identificador(cfg["marcas"], cfg.get("sinonimos")), corpo.get("nome", ""),
                              corpo.get("preco_alvo"), corpo.get("_usuario", ""))


def api_pular(con, corpo: dict) -> dict:
    """Sugestão que a equipe não quer agora (ex.: já foi postada antes de a curadora ver): some da sugestão por 2h."""
    chave = str(corpo.get("chave") or "")
    if not chave:
        return {"erro": "sem produto"}
    con.execute("INSERT INTO pulos (chave, ts, usuario) VALUES (?,?,?)", (chave, int(time.time()), corpo.get("_usuario", "")))
    con.commit()
    return {"ok": True}


def api_vigiados_remover(con, corpo: dict) -> dict:
    return vigiados.remover(con, int(corpo.get("id", 0)))


FRESCA_S = 6 * 3600  # "melhores primeiro": promo vista/conferida nas últimas 6h vem antes das velhas
CONFERIDA_S = 2 * 3600  # Excelente/Boa é reconferida a cada 15 min: mais de 2h sem conferir = o ML não deixou


def _fresca(c: dict, agora: float) -> bool:
    """Excelente/Boa sem reconferência há mais de 2h (o ML bloqueou o link) não é fresca: o preço pode ter mudado
    (visto em 06/10: Sabah a R$76 em Excelentes com preço de 20h atrás)."""
    limite = CONFERIDA_S if c["nota"] in ("IMPERDÍVEL", "BOA") else FRESCA_S
    return agora - c["ts"] <= limite


POSTADA_RECENTE_S = 6 * 3600  # postada (no GrupoFinal ou copiada no painel) há menos de 6h: vai para baixo
RITMO_POSTAGEM_S = 7 * 60     # a equipe posta uma promo a cada ~7 min
PULO_S = 2 * 3600             # "Pular" tira o produto da sugestão por 2h


def _postados_grupofinal(con, agora: float) -> dict:
    """Produtos postados nas últimas 6h no grupo da equipe:
    {chave: {"ts": do envio mais recente, "grupo": onde}}."""
    return {r["chave"]: {"ts": r["ts"], "grupo": r["grupo"]} for r in con.execute(
        "SELECT chave, grupo, max(ts) AS ts FROM postagens WHERE ts >= ? AND chave IS NOT NULL GROUP BY chave",
        (agora - POSTADA_RECENTE_S,))}


def _postada_recente(c: dict, grupofinal: dict, agora: float) -> bool:
    return bool(c["postada"] and agora - c["postada"] <= POSTADA_RECENTE_S) or c.get("chave") in grupofinal


def _ordem_melhores(agora: float, grupofinal: dict | None = None):
    """Pedido do Breno em 04/10: a equipe posta uma promo a cada ~7 min, então às vezes precisa tirar da
    seção Ruins/Médias; as melhores de cada seção ficam no topo. Ordem: não postada e fresca (6h) >
    não postada e velha > postada nas últimas 6h (no GrupoFinal, mesmo que por outra oferta do mesmo produto,
    ou copiada no painel); dentro de cada grupo, score maior primeiro e depois a mais recente."""
    grupofinal = grupofinal or {}

    def chave(c: dict):
        nivel = 2 if _postada_recente(c, grupofinal, agora) else (0 if _fresca(c, agora) else 1)
        return (nivel, -c["score"], -c["ts"])
    return chave


_cache_travas: dict = {}
_trava_travas = threading.Lock()


def _travas(con, agora: float | None = None) -> dict[str, str]:
    """21/10 (Breno: "sim pros 2"; a regra dele de 10/10: o que o painel mostra como Excelente não postada tem de ser o
    que o pipeline mandaria). Por que o pipeline não mandaria cada Excelente/Boa agora, com as mesmas regras dele:
    repetição (equipe 6h, pipeline 8h, ou 8% mais barato), compra internacional, vendedor (travas.vendedor_fraco),
    ressalva da curadora, e o que o próprio pipeline viu ao conferir no ML nas últimas 2h (mesmo preço): cupom, nota que
    caiu... {chave: motivo curto}. Refeito no máximo 1 vez por minuto, ou quando mudam os cards ou os arquivos."""
    agora = agora or time.time()
    cards = _cards(con)
    marcas = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (TRAVAS_PIPELINE, REGISTRO_PIPELINE))
    assinatura = (id(cards), marcas, int(agora // 60))
    with _trava_travas:
        guardado = _cache_travas.get(id(con))
        if guardado and guardado[0] == assinatura:
            return guardado[1]
    alvo = {(c.get("atual") or {}).get("id") or c["id"]: c for c in cards
            if c["nota"] in ("IMPERDÍVEL", "BOA") and c.get("chave") and not str(c["chave"]).startswith("#")}
    dados: dict[int, tuple] = {}
    ids = list(alvo)
    for i in range(0, len(ids), 400):
        lote = ids[i:i + 400]
        for r in con.execute(
                "SELECT id, json_extract(dados, '$.ml.oficial'), json_extract(dados, '$.reputacao'), "
                "json_extract(dados, '$.internacional'), json_extract(dados, '$.risco'), "
                "json_extract(dados, '$.risco_motivo'), json_extract(dados, '$.produtos_vendedor'), "
                f"json_extract(dados, '$.loja_selo') FROM avaliacoes WHERE id IN ({','.join('?' * len(lote))})", lote):
            dados[r[0]] = tuple(r)[1:]
    enviados = enviados_pelo_pipeline(REGISTRO_PIPELINE, agora - REPETIR_APOS_S - VEIO_DO_PIPELINE_S)
    vistas = travas.recentes(TRAVAS_PIPELINE, agora)
    hora = lambda t: time.strftime("%H:%M", time.localtime(t))
    saida: dict[str, str] = {}
    for id_, c in alvo.items():
        motivo = ""
        if c.get("postada"):
            equipe = post_da_equipe(c["chave"], c["postada"], enviados)
            janela = REPETIR_EQUIPE_S if equipe else REPETIR_APOS_S
            if agora - c["postada"] < janela:
                motivo = (f"{'a equipe postou' if equipe else 'o pipeline mandou'} às {hora(c['postada'])}; "
                          f"volta às {hora(c['postada'] + janela)} ou se cair 8%")
        if not motivo and id_ in dados:
            oficial, rep, internacional, risco, risco_motivo, produtos, selo = dados[id_]
            try:
                rep = json.loads(rep) if isinstance(rep, str) else rep
            except ValueError:
                rep = None
            com_dados = isinstance(rep, dict) and (rep.get("vendas") is not None or rep.get("classe") == "fraca")
            fraco = travas.vendedor_fraco(bool(oficial), rep) if com_dados else ""   # sem dado do vendedor: não afirma
            if internacional:
                motivo = "compra internacional"
            elif fraco:
                motivo = travas.curto(fraco)
            elif risco:
                motivo = f"ressalva: {(risco_motivo or 'vendedor de risco')[:38]}"
            elif produtos == 1 and not selo:
                motivo = "ressalva: loja de 1 produto"
        vista = vistas.get(c["chave"])
        if (not motivo and vista and (not vista.get("preco")
                                      or abs(vista["preco"] - c["preco"]) <= MESMA_OFERTA * max(vista["preco"], c["preco"]))):
            motivo = travas.curto(vista.get("motivo") or "")
        if motivo:
            saida[c["chave"]] = motivo
    with _trava_travas:
        _cache_travas[id(con)] = (assinatura, saida)
    return saida


def api_feed(con, q) -> list:
    secao = q.get("secao", ["excelentes"])[0]
    limite = min(400 if secao == "todas" else 200, int(q.get("limite", ["80"])[0]))
    nota = None if secao == "todas" else SECOES.get(secao, "IMPERDÍVEL")
    agora = time.time()
    grupofinal = _postados_grupofinal(con, agora)
    escolhidos = [c for c in _cards(con) if nota is None or c["nota"] == nota]
    if q.get("ordem", ["recentes"])[0] == "melhores":
        escolhidos = sorted(escolhidos, key=_ordem_melhores(agora, grupofinal))
    # 21/10 (Breno: "sim pros 2"): a travada sai da seção Excelentes/Boas e vai para o fim, com o motivo no card.
    travadas = _travas(con, agora)
    if secao in ("excelentes", "boas"):
        escolhidos = ([c for c in escolhidos if c.get("chave") not in travadas]
                      + [c for c in escolhidos if c.get("chave") in travadas])
    saida = []
    for c in escolhidos[:limite]:
        item = _completar(con, c, grupofinal)
        if c.get("chave") in travadas:
            item["trava"] = travadas[c["chave"]]
        saida.append(item)
    return saida


def api_sugestao(con, q) -> dict:
    """O que postar agora: a melhor promo de qualquer seção, vista nas últimas 6h e não postada nas últimas 6h
    (nem o mesmo produto no grupo da equipe). Mais o último post da equipe, para o ritmo de ~7 min."""
    agora = time.time()
    grupofinal = _postados_grupofinal(con, agora)
    pulados = {r[0] for r in con.execute("SELECT chave FROM pulos WHERE ts >= ?", (agora - PULO_S,))}
    pulados |= set(_travas(con, agora))   # 21/10: a travada (o pipeline não mandaria) também não é sugerida
    candidatas = sorted((c for c in _cards(con) if _fresca(c, agora) and c.get("chave") not in pulados
                         and not _postada_recente(c, grupofinal, agora)), key=lambda c: (-c["score"], -c["ts"]))
    ultimo = con.execute("SELECT ts, produto, preco FROM postagens ORDER BY ts DESC LIMIT 1").fetchone()
    return {"itens": [_completar(con, c, grupofinal) for c in candidatas[:3]], "ritmo_s": RITMO_POSTAGEM_S,
            "ultimo_post": dict(ultimo) if ultimo else None,
            "grupo_equipe": (carregar_config().get("grupo_postagens") or {}).get("nome", "")}


def api_secoes(con, q) -> dict:
    """Quantos produtos (um card por produto) há em cada seção nas últimas 48h."""
    contagem = {secao: 0 for secao in SECOES}
    por_nota = {v: k for k, v in SECOES.items()}
    travadas = _travas(con)   # 21/10: a travada não conta (nem dispara o "chegou promo excelente")
    for c in _cards(con):
        if c["nota"] in por_nota and c.get("chave") not in travadas:
            contagem[por_nota[c["nota"]]] += 1
    return contagem


def api_historico(con, q) -> dict:
    chave = q.get("chave", [""])[0]
    dias = int(q.get("dias", ["60"])[0])
    desde = int(time.time()) - dias * 86400
    pontos = [{"ts": r["ts"], "preco": r["preco"], "tipo": "com_cupom" if r["cupons"] != "[]" else "sem_cupom",
               "fonte": r["grupo"]}
              for r in con.execute("SELECT ts, preco, cupons, grupo FROM ofertas WHERE chave=? AND ts>=? ORDER BY ts",
                                   (chave, desde))]
    pontos += [{"ts": r["ts"], "preco": r["preco"], "tipo": "ml", "fonte": r["vendedor"] or "ML"}
               for r in con.execute("SELECT ts, preco, vendedor FROM precos_ml WHERE chave=? AND ts>=? ORDER BY ts",
                                    (chave, desde))]
    return {"chave": chave, "pontos": sorted(pontos, key=lambda p: p["ts"])}


def api_config(con, q) -> dict:
    cfg = carregar_config()
    return {"top_vendidos": cfg.get("top_vendidos", []), "lojas_top": cfg.get("lojas_top", []),
            "janela_dias": cfg.get("janela_dias")}


def api_avaliar(con, corpo: dict) -> dict:
    texto = str(corpo.get("texto") or "").strip()
    if not texto:
        return {"erro": "Cole o texto da promo."}
    cfg = carregar_config()
    usar_ml = bool(corpo.get("ml", True)) and not ESTADO["sem_ml"]
    cur = Curadora(cfg, con, ConsultorML(con) if usar_ml else None, cliente_api=ClienteML() if usar_ml else None)
    av = cur.avaliar(texto)
    if av is None:
        return {"erro": "Não consegui ler produto e preço dessa mensagem."}
    av.grupo = "colada no painel"
    dados = para_dict(av)
    novo_id = registrar_avaliacao(con, int(time.time()), "colada", av.grupo, av.identidade.chave, av.nota,
                                  av.score, False, dados)
    return dict(id=novo_id, ids=[novo_id], ts=int(time.time()), origem="colada", voto=0, postada=0, alertado=False,
                **dados)


def api_conferir(con, corpo: dict) -> dict:
    """Confere no ML AGORA o card que alguém vai copiar/abrir (09/10: "conferir na hora de postar").

    O reconferidor passa a cada ~15 min, mas 8% das Boas/Excelentes mudam de preço na primeira hora. Grava como
    reconferência (o painel passa a mostrar o preço novo) e devolve o antes e o depois.
    """
    linha = con.execute("SELECT id, grupo, nota, dados FROM avaliacoes WHERE id = ?",
                        (int(corpo.get("id") or 0),)).fetchone()
    if not linha or ESTADO["sem_ml"]:
        return {"ok": False}
    dados = json.loads(linha["dados"])
    if not reconferir.link_do_ml(dados.get("link", "")) or not dados.get("texto"):
        return {"ok": False}
    cur = Curadora(carregar_config(), con, ConsultorML(con, ttl=20), cliente_api=ClienteML())
    agora = int(time.time())
    av = cur.avaliar(dados["texto"], agora=agora, conferir_ml=True)
    if av is None or not (av.card and av.card.ok):
        return {"ok": False, "erro": "o ML não abriu agora"}
    novo = para_dict(av)
    antes = float(corpo.get("preco") or dados.get("preco") or 0)
    novo.update(reconferida_de=int(corpo.get("card_id") or linha["id"]), preco_antes=antes, nota_antes=linha["nota"])
    registrar_avaliacao(con, agora, "reconferida", linha["grupo"] or "", av.identidade.chave, av.nota, av.score,
                        False, novo)
    return {"ok": True, "preco_antes": antes, "preco": round(av.preco_efetivo, 2), "nota": av.nota,
            "nota_antes": linha["nota"], "mudou": abs(av.preco_efetivo - antes) > max(1.0, antes * 0.02),
            "cupons_mortos": av.cupons_mortos}


def api_voto(con, corpo: dict) -> dict:
    voto = int(corpo.get("voto", 0))
    if voto not in (-1, 0, 1):
        return {"erro": "voto inválido"}
    con.execute("UPDATE avaliacoes SET voto=?, votado_por=? WHERE id=?",
                (voto, corpo.get("_usuario", ""), int(corpo.get("id", 0))))
    con.commit()
    return {"ok": True}


def api_calibracao(con, q) -> dict:
    """Quanto a curadora concorda com os votos/postadas e qual ajuste concordaria mais."""
    exemplos = carregar_exemplos(con)
    return para_painel(calibrar(exemplos, carregar_config()), exemplos)


def api_calibracao_aplicar(con, corpo: dict) -> dict:
    exemplos = carregar_exemplos(con)
    cfg = carregar_config()
    r = calibrar(exemplos, cfg)
    if r is None or r.concordancia_melhor <= r.concordancia_atual + 0.02:
        return {"erro": "Ainda não há um ajuste que melhore de verdade a concordância."}
    backup = gravar(cfg, r)  # quem estiver rodando recarrega o config sozinho na próxima avaliação
    return {"ok": True, "backup": backup, "concordancia": round(r.concordancia_melhor, 3)}


def api_painel(con, q) -> dict:
    """Tudo que o painel atualiza de tempos em tempos, numa resposta só (1 ida e volta em vez de 3).

    A VPS fica em Helsinque: cada ida e volta do Brasil custa ~330 ms; a resposta em si sai em ~2 ms.
    """
    return {"status": api_status(con, q), "secoes": api_secoes(con, q), "feed": api_feed(con, q),
            "sugestao": api_sugestao(con, q)}


ROTAS_GET = {"/api/status": api_status, "/api/feed": api_feed, "/api/secoes": api_secoes,
             "/api/historico": api_historico, "/api/config": api_config, "/api/painel": api_painel,
             "/api/calibracao": api_calibracao, "/api/sugestao": api_sugestao, "/api/vigiados": api_vigiados}
def api_postada(con, corpo: dict) -> dict:
    """Marca (ou desmarca) promos como postadas: copiar ou encaminhar já marca."""
    ids = [int(i) for i in corpo.get("ids", []) if str(i).isdigit()][:200]
    valor = int(time.time()) if corpo.get("postada", True) else 0
    con.executemany("UPDATE avaliacoes SET postada=?, postada_por=? WHERE id=?",
                    [(valor, corpo.get("_usuario", ""), i) for i in ids])
    con.commit()
    return {"ok": True, "postada": valor}


# ── equipe (só admin) ───────────────────────────────────────────
def api_convidar(con, corpo: dict) -> dict:
    nome = normalizar_nome(corpo.get("nome", ""))
    if not nome:
        return {"erro": "Escreva o nome da pessoa (só letras, números, ponto ou traço)."}
    token = Acesso(con, limitador=LIMITADOR).convidar(nome, admin=bool(corpo.get("admin")))
    return {"ok": True, "nome": nome, "link": f"{corpo.get('_base', '')}/entrar?convite={token}", "vale_horas": 48}


def api_usuarios(con, q) -> list:
    return [{"nome": r["nome"], "admin": bool(r["admin"]), "ativo": bool(r["ativo"])}
            for r in Acesso(con, limitador=LIMITADOR).listar()]


def api_desativar(con, corpo: dict) -> dict:
    nome = normalizar_nome(corpo.get("nome", ""))
    if nome == corpo.get("_usuario"):
        return {"erro": "Você não pode desativar a si mesmo."}
    Acesso(con, limitador=LIMITADOR).desativar(nome)
    return {"ok": True}


ROTAS_POST = {"/api/avaliar": api_avaliar, "/api/voto": api_voto, "/api/postada": api_postada,
              "/api/calibracao/aplicar": api_calibracao_aplicar, "/api/convidar": api_convidar,
              "/api/desativar": api_desativar, "/api/vigiados": api_vigiados_adicionar,
              "/api/vigiados/remover": api_vigiados_remover, "/api/pular": api_pular, "/api/conferir": api_conferir}
ROTAS_GET["/api/usuarios"] = api_usuarios


def api_chamadas(con, q) -> dict:
    """Sugestões de chamada (título) por produto, para o Breno aprovar em lote."""
    return chamadas.fila(con)


def api_chamadas_decidir(con, corpo: dict) -> dict:
    """Aprova/recusa/edita uma chamada. Aprovada entra na lista de chamadas manuais da normalizadora."""
    if not str(corpo.get("id", "")).isdigit():
        return {"erro": "chamada inválida"}
    return chamadas.decidir(con, int(corpo["id"]), str(corpo.get("estado") or ""), corpo.get("texto"),
                            corpo.get("_usuario", ""))


ROTAS_GET["/api/chamadas"] = api_chamadas
ROTAS_POST["/api/chamadas/decidir"] = api_chamadas_decidir


def api_novo_codigo(con, corpo: dict) -> dict:
    """Código de recuperação novo para quem está logado (o anterior deixa de valer). Mostrado uma vez só."""
    if not ESTADO.get("exigir_login"):
        return {"erro": "No PC, sem login, não há senha para recuperar."}
    try:
        return {"codigo": Acesso(con, limitador=LIMITADOR).novo_codigo(corpo.get("_usuario", ""))}
    except AcessoNegado as exc:
        return {"erro": str(exc)}


ROTAS_POST["/api/recuperacao"] = api_novo_codigo
# decidir chamada grava num arquivo da normalizadora: só o admin (o Breno)
SO_ADMIN = {"/api/calibracao/aplicar", "/api/convidar", "/api/desativar", "/api/usuarios", "/api/chamadas/decidir"}
PAGINA_ENTRAR = RAIZ / "painel" / "entrar.html"
LIMITADOR = Limitador()
COOKIE = "curadora_sessao"


class Handler(BaseHTTPRequestHandler):
    # Conexão mantida aberta entre as atualizações: longe do servidor, abrir uma nova a cada vez custa caro.
    protocol_version = "HTTP/1.1"

    def _enviar(self, codigo: int, corpo: bytes, tipo: str, extras: dict | None = None) -> None:
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(corpo) > 1024:
            corpo = gzip.compress(corpo, compresslevel=5)
            comprimido = True
        else:
            comprimido = False
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(corpo)))
        # Proteções básicas do navegador: não abrir dentro de outro site, não adivinhar tipo de arquivo.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for nome, valor in (extras or {}).items():
            self.send_header(nome, valor)
        if comprimido:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados, codigo: int = 200, extras: dict | None = None) -> None:
        self._enviar(codigo, json.dumps(dados, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                     extras)

    # ── quem está pedindo ───────────────────────────────────────
    def _https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _base(self) -> str:
        return f"{'https' if self._https() else 'http'}://{self.headers.get('Host', '127.0.0.1')}"

    def _ip(self) -> str:
        # Atrás do Caddy (na mesma máquina), o IP real vem no X-Forwarded-For.
        if self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def _token(self) -> str:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        return cookies[COOKIE].value if COOKIE in cookies else ""

    def _cookie(self, token: str, idade: int) -> dict:
        seguro = "; Secure" if self._https() else ""
        return {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={idade}{seguro}"}

    def _usuario(self, con) -> Usuario | None:
        if not ESTADO.get("exigir_login"):
            return Usuario("local", True)  # no PC, sem login: dono da máquina
        return Acesso(con, limitador=LIMITADOR).usuario_da_sessao(self._token())

    def _mesma_origem(self) -> bool:
        """POST só vale vindo do próprio painel (outro site não consegue votar/convidar em seu nome)."""
        if not ESTADO.get("exigir_login"):
            return True
        origem = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return bool(origem) and urlparse(origem).netloc == self.headers.get("Host", "")

    # ── rotas ───────────────────────────────────────────────────
    def do_GET(self) -> None:
        url = urlparse(self.path)
        con = conectar(ESTADO["banco"])
        try:
            if url.path == "/entrar":
                self._enviar(200, PAGINA_ENTRAR.read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/api/convite":
                convite = Acesso(con, limitador=LIMITADOR).ver_convite(parse_qs(url.query).get("t", [""])[0])
                self._json({"nome": convite["nome"]} if convite else {"erro": "Convite inválido, já usado ou vencido."})
                return
            usuario = self._usuario(con)
            if url.path in ("/", "/index.html", "/chamadas"):
                if usuario is None:
                    self._enviar(302, b"", "text/plain", {"Location": "/entrar"})
                else:
                    pagina = PAGINA_CHAMADAS if url.path == "/chamadas" else PAGINA
                    self._enviar(200, pagina.read_bytes(), "text/html; charset=utf-8")
                return
            if usuario is None:
                self._json({"erro": "entre de novo", "login": True}, 401)
                return
            if url.path == "/api/eu":
                self._json({"nome": usuario.nome, "admin": usuario.admin, "login": bool(ESTADO.get("exigir_login"))})
                return
            rota = ROTAS_GET.get(url.path)
            if not rota:
                self._json({"erro": "não encontrado"}, 404)
            elif url.path in SO_ADMIN and not usuario.admin:
                self._json({"erro": "só o admin pode fazer isso"}, 403)
            else:
                self._json(rota(con, parse_qs(url.query)))
        finally:
            con.close()

    def do_POST(self) -> None:
        caminho = urlparse(self.path).path
        tamanho = int(self.headers.get("Content-Length") or 0)
        try:
            corpo = json.loads(self.rfile.read(min(tamanho, 200_000)) or b"{}")
            if not isinstance(corpo, dict):
                raise ValueError
        except ValueError:
            self._json({"erro": "JSON inválido"}, 400)
            return
        if not self._mesma_origem():
            self._json({"erro": "pedido de fora do painel recusado"}, 403)
            return
        con = conectar(ESTADO["banco"])
        try:
            acesso = Acesso(con, limitador=LIMITADOR)
            if caminho == "/api/entrar":
                try:
                    token = acesso.entrar(corpo.get("nome", ""), corpo.get("senha", ""), self._ip())
                    self._json({"ok": True}, extras=self._cookie(token, SESSAO_S))
                except AcessoNegado as exc:
                    self._json({"erro": str(exc)}, 401)
                return
            if caminho == "/api/convite":
                try:
                    token = acesso.aceitar_convite(corpo.get("token", ""), corpo.get("senha", ""))
                    # Senha criada: já sai com o código de recuperação (a tela mostra uma vez só).
                    codigo = acesso.novo_codigo(acesso.usuario_da_sessao(token).nome)
                    self._json({"ok": True, "codigo": codigo}, extras=self._cookie(token, SESSAO_S))
                except (AcessoNegado, ValueError) as exc:
                    self._json({"erro": str(exc)}, 400)
                return
            if caminho == "/api/recuperar":  # esqueceu a senha: nome + código de recuperação + senha nova
                try:
                    token, codigo = acesso.recuperar(corpo.get("nome", ""), corpo.get("codigo", ""),
                                                     corpo.get("senha", ""), self._ip())
                    self._json({"ok": True, "codigo": codigo}, extras=self._cookie(token, SESSAO_S))
                except (AcessoNegado, ValueError) as exc:
                    self._json({"erro": str(exc)}, 400)
                return
            if caminho == "/api/sair":
                acesso.sair(self._token())
                self._json({"ok": True}, extras=self._cookie("", 0))
                return
            usuario = self._usuario(con)
            rota = ROTAS_POST.get(caminho)
            if usuario is None:
                self._json({"erro": "entre de novo", "login": True}, 401)
            elif not rota:
                self._json({"erro": "não encontrado"}, 404)
            elif caminho in SO_ADMIN and not usuario.admin:
                self._json({"erro": "só o admin pode fazer isso"}, 403)
            else:
                corpo["_usuario"], corpo["_base"] = usuario.nome, self._base()  # o servidor decide quem é
                self._json(rota(con, corpo))
        finally:
            con.close()

    def log_message(self, formato, *args) -> None:  # silencia o log de cada requisição
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--simular", type=float, metavar="HORAS", help="reproduz as últimas HORAS de mensagens reais")
    ap.add_argument("--intervalo", type=float, default=0.5, help="segundos entre mensagens na simulação")
    ap.add_argument("--arquivo", type=Path, help="arquivo de mensagens para ler ao vivo")
    ap.add_argument("--do-fim", action="store_true")
    ap.add_argument("--sem-ml", action="store_true")
    ap.add_argument("--banco", type=Path, help="banco a abrir (ex.: dados/simulacao.db para rever uma simulação)")
    ap.add_argument("--vigia", action="store_true", help="reabre os links dos top vendidos de tempos em tempos")
    ap.add_argument("--radar", action="store_true", help="vigia os top vendidos pela API do ML (precisa de configurar_ml.py)")
    ap.add_argument("--intervalo-vigia", type=float, default=600)
    ap.add_argument("--intervalo-radar", type=float, default=900)
    ap.add_argument("--exigir-login", action="store_true", help="pede nome e senha (obrigatório quando hospedado)")
    args = ap.parse_args()
    cfg = carregar_config()
    ESTADO["sem_ml"] = args.sem_ml
    ESTADO["exigir_login"] = args.exigir_login
    if args.banco:
        ESTADO["banco"] = args.banco

    if args.simular:
        banco, janela = preparar_simulacao(args.simular)
        ESTADO["banco"] = banco
        threading.Thread(target=rodar_simulacao, args=(janela, args.intervalo, cfg, args.sem_ml),
                         daemon=True, name="simulacao").start()
        print(f"simulação: {len(janela)} mensagens das últimas {args.simular:g}h, uma a cada {args.intervalo:g}s")
    elif args.arquivo:
        threading.Thread(target=rodar_arquivo, args=(args.arquivo, args.do_fim, cfg, args.sem_ml),
                         daemon=True, name="ao-vivo").start()
    if args.vigia and not args.sem_ml:
        iniciar_vigia(cfg, args.intervalo_vigia)
        iniciar_reconferidor(cfg)
        iniciar_experimento(cfg)
        iniciar_conferente(cfg)
        iniciar_pagina_de_cupons(cfg)
    if args.radar:
        iniciar_radar(cfg, args.intervalo_radar)

    servidor = ThreadingHTTPServer(("127.0.0.1", args.porta), Handler)
    print(f"painel em http://127.0.0.1:{args.porta}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
