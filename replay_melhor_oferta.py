"""Replay em leitura da escolha da melhor oferta (04/11, fase 1 do PLANO_MELHOR_OFERTA_2026-11-04.md).

Para cada post da equipe no GrupoFinal, e (com --simulacao) para cada 7 min do turno, o que a curadora escolheria NAQUELE
minuto, só com o que existia até ali (curadora.momento), e por quê (a frase "examinei, descartei, escolheria").
Também: a nota do post sobre o preço publicado, os inícios de cupom percebidos pelos grupos e o custo de consultas que
o desenho gastaria (sem fazer nenhuma consulta: esta fase não aumenta consulta da produção).

Só lê: banco em modo somente leitura, tap e registros só lidos. Saída: JSON na saída padrão.
  python3 replay_melhor_oferta.py --de 2026-11-04 --ate 2026-11-04 --simulacao > replay.json
  python replay_melhor_oferta.py --relatorio replay.json > RELATORIO.md     (no PC: o relatório em texto)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent if "__file__" in globals() and not str(__file__).startswith("<") \
    else Path.cwd()
sys.path.insert(0, str(RAIZ))

TETO_HORA, RESERVA_HORA, TETO_DIA, RESERVA_DIA = 50, 15, 700, 150   # o orçamento proposto (consultas à API)
RESERVA_JANELA_S = 45 * 60          # a reserva abre por 45 min a cada início de cupom
CACHE_CATALOGO_S = 30 * 60          # um catálogo lido serve por 30 min
BUSCA_S = 7 * 86400                 # a busca de catálogos novos de um perfume vale 7 dias
CATALOGOS_POR_DECISAO = 4
RELEITURA_S = 3600                  # depois da escolha: o preço se sustentou na hora seguinte?


def _h(ts) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "-"


def _dia(texto: str, fim: bool = False) -> float:
    t = time.mktime(time.strptime(texto, "%Y-%m-%d"))
    return t + 86400 - 1 if fim else t


# ── decisões ─────────────────────────────────────────────────────

def decidir_post(con, comparador, post, ident):
    from curadora import momento, ofertas
    dados = json.loads(post["dados"] or "{}")
    t = float(post["ts"])
    ref = ofertas.observacao_da_avaliacao(f"post {post['id']}", t, "post da equipe", post["chave"], post["nota"],
                                          post["score"], dados)
    if ref is None:
        return None, dados, set()
    outras = momento.observacoes_do_perfume(con, post["chave"], t, comparador)
    catalogos = momento.catalogos_conhecidos(con, [post["chave"]], t)[post["chave"]]
    lidos = {o.catalogo for o in outras if o.base == "api" and o.catalogo and t - o.ts <= ofertas.FRESCOR_COMPARAR_S}
    sem_leitura = catalogos - lidos
    lacunas = [f"{len(sem_leitura)} catálogo(s) do perfume conhecido(s) sem leitura da API nas 2 h antes"] \
        if sem_leitura else []
    publicado = dados.get("preco_grupo") or post["preco"]
    d = ofertas.escolher(ref, outras, t, ident, momento="post", produto=post["produto"], lacunas=lacunas,
                         publicado=float(publicado) if publicado else None)
    return d, dados, catalogos


def curadora_sem_quente(cfg: dict, con):
    """A curadora para a nota sobre o preço publicado: o preço do post já traz o cupom, então o "cupom quente" (que
    aplica o cupom em uso no preço da página) fica desligado, senão o cupom entraria duas vezes (1ª rodada do replay,
    04/11: o Yara publicado a R$164 com DESCONTOS virava R$147,60). Sem `_arquivo`, o config do disco não volta."""
    import copy
    from curadora.score import Curadora
    cfg = copy.deepcopy(cfg)
    cfg.pop("_arquivo", None)
    cfg["cupom_quente"] = {**(cfg.get("cupom_quente") or {}), "ligado": False}
    return Curadora(cfg, con, None)


def nota_publicada(cur, dados: dict, t: float):
    """A nota do post sobre o preço publicado: a mesma conta da curadora, com o card da página como estava (loja, selo,
    reputação e o preço da página) e o preço efetivo forçado para o do texto do post. Com cupom vivo e o publicado
    abaixo da página, o histórico comparado é o "com cupom", como na nota de hoje (2ª rodada do replay, 04/11: com a
    página trocada pelo publicado, o Yara de R$164 com DESCONTOS ia para o histórico sem cupom e saía IMPERDÍVEL 88,
    quando a própria nota de hoje, sobre os mesmos R$164, é NÃO VALE 44). `cur` vem de curadora_sem_quente."""
    from curadora import ofertas
    from curadora.ml import CardML
    ml, texto, publicado = dados.get("ml") or {}, dados.get("texto") or "", dados.get("preco_grupo")
    if not (texto and publicado and ml.get("preco")):
        return None
    card = CardML(ok=True, titulo=ml.get("titulo") or "", preco=float(ml["preco"]),
                  preco_outros=float(ml["preco_outros"]) if ml.get("preco_outros") else None,
                  vendedor=ml.get("vendedor") or "", loja_oficial=bool(ml.get("oficial")), full=bool(ml.get("full")),
                  url_produto=ml.get("url") or "", produto_id=ofertas.catalogo_da_url(ml.get("url") or ""),
                  reputacao=dados.get("reputacao"), internacional=bool(dados.get("internacional")))
    cur._preco_efetivo = lambda promo, card, razoes, alertas, agora=None, mortos=None: float(promo.preco)
    try:
        av = cur.avaliar(texto, agora=int(t), conferir_ml=False, card=card)
    except Exception as exc:  # noqa: BLE001 - o replay nunca para por um post
        return {"erro": f"{type(exc).__name__}: {exc}"}
    finally:
        del cur._preco_efetivo   # volta o método da classe
    if av is None:
        return None
    return {"nota": av.nota, "score": av.score, "preco": round(av.preco_efetivo, 2),
            "historico": getattr(av.historico, "tipo", None)}


def _decisao_em_dict(d) -> dict:
    esc = d.escolhida
    return {"status": d.status, "troca": d.troca, "pronta": d.pronta, "economia": d.economia,
            "economia_sobre_publicado": d.economia_sobre_publicado, "inconclusiva": d.inconclusiva,
            "examinadas": 1 + len(d.outras),
            "frescas": sum(1 for e in [d.referencia] + d.outras if d.ts - e.obs.ts <= 7200),
            "situacoes": dict(Counter(e.situacao for e in d.outras)),
            "motivos": dict(Counter(e.motivo.split(":")[0].split("(")[0].strip() for e in d.outras if e.motivo)),
            "referencia": {"vendedor": d.referencia.obs.vendedor, "anuncio": d.referencia.obs.anuncio,
                           "categoria": d.referencia.categoria, "preco": d.referencia.preco_comparado,
                           "onde": d.referencia.obs.onde()},
            "escolhida": None if esc is None else {
                "vendedor": esc.obs.vendedor, "anuncio": esc.obs.anuncio, "categoria": esc.categoria,
                "preco": esc.preco_comparado, "base": esc.base_comparada, "lida": int(esc.obs.ts), "fonte": esc.obs.fonte,
                "onde": esc.obs.onde(), "link": esc.obs.link},
            "possiveis_com_cupom": [{"vendedor": o.vendedor, "codigo": c.codigo, "nivel": c.nivel, "preco": c.preco,
                                     "evidencia": c.evidencia} for o, c in d.possiveis_com_cupom],
            "lacunas": d.lacunas, "frase": d.frase()}


# ── custo de consultas (modelo, sem consultar nada) ──────────────

class Custo:
    """Quantas consultas à API o desenho faria: até 4 catálogos por decisão (cache de 30 min por catálogo) e 1 busca
    por perfume a cada 7 dias. Reputações (/users, cache de 7 dias) ficam de fora do modelo: ver o relatório."""

    def __init__(self):
        self.lido: dict[str, float] = {}
        self.buscado: dict[str, float] = {}
        self.por_hora: Counter = Counter()
        self.por_dia: Counter = Counter()
        self.por_uso: Counter = Counter()

    def decisao(self, t: float, chave: str, catalogos: set, uso: str) -> int:
        n = 0
        for c in sorted(catalogos, key=lambda c: self.lido.get(c, 0))[:CATALOGOS_POR_DECISAO]:
            if t - self.lido.get(c, -1e12) > CACHE_CATALOGO_S:
                self.lido[c] = t
                n += 1
        if t - self.buscado.get(chave, -1e12) > BUSCA_S:
            self.buscado[chave] = t
            n += 1
        hora = time.strftime("%Y-%m-%d %H", time.localtime(t))
        self.por_hora[hora] += n
        self.por_dia[hora[:10]] += n
        self.por_uso[uso] += n
        return n

    def resumo(self, inicios: list[float]) -> dict:
        acima = {}
        for hora, n in sorted(self.por_hora.items()):
            t0 = time.mktime(time.strptime(hora, "%Y-%m-%d %H"))
            reserva = any(t0 - RESERVA_JANELA_S <= i < t0 + 3600 for i in inicios)
            teto = TETO_HORA + (RESERVA_HORA if reserva else 0)
            if n > teto:
                acima[hora] = {"consultas": n, "teto": teto}
        return {"por_dia": dict(self.por_dia), "por_uso": dict(self.por_uso), "pico_hora": max(self.por_hora.values(), default=0),
                "horas_acima_do_teto": acima, "teto": {"hora": TETO_HORA, "reserva_hora": RESERVA_HORA,
                                                       "dia": TETO_DIA, "reserva_dia": RESERVA_DIA},
                "modelo": "até 4 catálogos por decisão, cache de 30 min por catálogo, 1 busca por perfume a cada "
                          "7 dias; reputações (/users) fora do modelo"}


# ── depois da escolha: o preço se sustentou? (avaliação, usa o que veio depois de propósito) ──

def sustentou(con, chave: str, anuncio: str, preco: float, t: float) -> str:
    from curadora import ofertas
    precos = []
    for ts, origem, dados in con.execute("SELECT ts, origem, dados FROM main.avaliacoes WHERE chave = ? AND ts > ? AND "
                                         "ts <= ? AND nota != 'FORA'", (chave, int(t), int(t + RELEITURA_S))):
        try:
            d = json.loads(dados or "{}")
        except ValueError:
            continue
        ml = d.get("ml") or {}
        if ml.get("url") and ofertas.anuncio_de(ml["url"], ml.get("vendedor") or d.get("loja") or "") == anuncio \
                and ml.get("preco"):
            precos.append(float(ml["preco"]))
    if not precos:
        return "sem releitura em 1 h"
    return "subiu em até 1 h" if max(precos) > preco * 1.03 else "se sustentou em 1 h"


# ── o replay ─────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--de")
    ap.add_argument("--ate")
    ap.add_argument("--simulacao", action="store_true")
    ap.add_argument("--banco", default=str(RAIZ / "dados" / "curadora.db"))
    ap.add_argument("--comparador", default=str(RAIZ / "dados" / "comparador.db"))
    ap.add_argument("--registro", default=str(RAIZ / "dados" / "pipeline.jsonl"))
    ap.add_argument("--relatorio", help="no PC: gera o relatório em Markdown a partir do JSON do replay")
    args = ap.parse_args(argv)
    if args.relatorio:
        print(relatorio_md(json.loads(Path(args.relatorio).read_text(encoding="utf-8"))))
        return 0
    from curadora import carregar_config, cupom_antecipado, momento, ofertas, simulacao
    from curadora.identidade import Identificador
    comeco = time.time()
    cfg = carregar_config()
    ident = Identificador(cfg["marcas"], cfg.get("sinonimos"))
    grupos = {str(k).split("@")[0]: v for k, v in (cfg.get("grupos_fonte") or {}).items()}
    grupofinal = str((cfg.get("grupo_postagens") or {}).get("id", "")).split("@")[0]
    de, ate = _dia(args.de), min(_dia(args.ate, fim=True), time.time())
    detector = cupom_antecipado.Detector(cfg["marcas"])
    cupons = momento.CuponsNoTempo(momento.mensagens_do_tap(momento.TAP, grupos, grupofinal), cfg["marcas"],
                                   ouvintes=[detector.ver])
    con = momento.conectar_leitura(args.banco)
    comparador = momento.conectar_leitura(args.comparador) if Path(args.comparador).exists() else None
    posts = con.execute("SELECT id, ts, produto, chave, preco, link, nota, score, dados FROM main.postagens WHERE ts BETWEEN ? "
                        "AND ? ORDER BY ts", (int(de), int(ate))).fetchall()
    eventos = [(float(p["ts"]), "post", p) for p in posts]
    dias = []
    d = de
    while d <= ate:
        dias.append(time.strftime("%Y-%m-%d", time.localtime(d)))
        d += 86400
    if args.simulacao:
        for dia in dias:
            eventos += [(t, "vez", dia) for t in simulacao.horarios_do_turno(dia) if t <= ate]
    eventos.sort(key=lambda e: (e[0], e[1] == "vez"))
    custo, cur, sims = Custo(), None, {}
    saida_posts, saida_vezes = [], []
    for t, tipo, item in eventos:
        if tipo == "post":
            momento.no_horario(con, t)
            cupons.ate(t).aplicar(con)
            if cur is None:
                cur = curadora_sem_quente(cfg, con)
            dec, dados, catalogos = decidir_post(con, comparador, item, ident)
            linha = {"ts": int(t), "hora": _h(t), "produto": item["produto"], "chave": item["chave"],
                     "publicado": dados.get("preco_grupo"), "preco_conferido": item["preco"],
                     "nota_conferida": item["nota"],
                     "score_conferido": item["score"], "cupons": dados.get("cupons") or [],
                     "publicada": nota_publicada(cur, dados, t)}
            if dec is None:
                linha.update(status="sem anúncio conferido", frase="o post não tem o anúncio do ML conferido")
            else:
                linha.update(_decisao_em_dict(dec))
            linha["consultas"] = custo.decisao(t, item["chave"] or "", catalogos, "post do GrupoFinal")
            saida_posts.append(linha)
        else:
            sim = sims.get(item)
            if sim is None:
                inicio = simulacao.horarios_do_turno(item)[0]
                sim = sims[item] = simulacao.Simulacao(args.banco, cfg, ident, inicio, args.registro, cupons=cupons,
                                                       comparador=args.comparador)
            v = sim.vez(t)
            r = v.resumo()
            if v.decisao is not None and v.chave:
                momento.no_horario(con, t)
                cats = momento.catalogos_conhecidos(con, [v.chave], t)[v.chave]
                r["consultas"] = custo.decisao(t, v.chave, cats, "simulação")
            saida_vezes.append(r)
    cupons.ate(ate)
    for sim in sims.values():
        sim.fechar()
    # depois da escolha (avaliação, com dados posteriores de propósito): o preço escolhido se sustentou na hora seguinte?
    for r in saida_vezes + [p for p in saida_posts if p.get("troca")]:
        esc = r.get("escolhida")
        if esc and esc.get("preco"):
            r["depois"] = sustentou(con, r["chave"], esc["anuncio"], esc["preco"], r["ts"])
    inicios = []
    for i in detector.inicios:
        if not de <= i.ts <= ate:
            continue
        posts_com = [int(r[0]) for r in con.execute(
            "SELECT ts FROM main.postagens WHERE ts BETWEEN ? AND ? AND dados LIKE ? ORDER BY ts",
            (int(i.ts - 1800), int(i.ts + 6 * 3600), f'%"{i.codigo}"%'))]
        quente = con.execute("SELECT min(ts) FROM main.avaliacoes WHERE ts BETWEEN ? AND ? AND "
                             "json_extract(dados, '$.cupom_quente.codigo') = ?",
                             (int(i.ts - 1800), int(i.ts + 6 * 3600), i.codigo)).fetchone()[0]
        inicios.append({"codigo": i.codigo, "ts": int(i.ts), "hora": _h(i.ts), "frase": i.frase(),
                        "parado_desde": i.parado_desde, "regra": i.regra,
                        "sinais": [{"tipo": s.tipo, "grupo": s.nome, "ts": int(s.ts), "trecho": s.trecho} for s in i.sinais],
                        "primeiro_post_da_equipe": posts_com[0] if posts_com else None,
                        "posts_da_equipe_6h": len(posts_com), "quente_na_curadora": quente})
    fontes = {}
    for nome in ("curadora/ofertas.py", "curadora/momento.py", "curadora/cupom_antecipado.py", "curadora/simulacao.py",
                 "replay_melhor_oferta.py"):
        caminho = RAIZ / nome
        if caminho.exists():
            fontes[nome] = hashlib.sha256(caminho.read_bytes()).hexdigest()[:12]
    print(json.dumps({"gerado": _h(time.time()), "janela": {"de": int(de), "ate": int(ate)}, "dias": dias,
                      "posts": saida_posts, "vezes": saida_vezes, "inicios_de_cupom": inicios,
                      "custos": custo.resumo([x["ts"] for x in inicios]), "politica": momento.POLITICA,
                      "tap_lidas": cupons.lidas, "tempo_s": round(time.time() - comeco, 1),
                      "versao": fontes or globals().get("VERSAO_DO_PACOTE", {})},
                     ensure_ascii=False))
    return 0


# ── o relatório (no PC) ──────────────────────────────────────────

def _brl(v) -> str:
    if v is None:
        return "-"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def relatorio_md(r: dict) -> str:
    linhas = [f"# Replay da melhor oferta conhecida — {', '.join(r['dias'])}", "",
              f"Gerado em {r['gerado']} na VPS, só leitura ({r['tempo_s']} s). Código: "
              + ", ".join(f"`{k}` {v}" for k, v in (r.get("versao") or {}).items()) + ".", ""]
    posts = r["posts"]
    st = Counter(p.get("status") for p in posts)
    linhas += ["## Posts do GrupoFinal", "",
               f"{len(posts)} posts. Resultado da escolha no minuto do post, só com o que existia até ali:", ""]
    linhas += [f"- {k}: {v}" for k, v in st.most_common()]
    trocas = [p for p in posts if p.get("troca")]
    conf = [p for p in trocas if p.get("status") == "troca confirmada"]
    linhas += ["", f"Trocas confirmadas (página com página, mesma confiança ou maior): {len(conf)}; "
               f"economia na mesma base: {_brl(sum(p.get('economia') or 0 for p in conf))}; "
               f"sobre o preço publicado: {_brl(sum(p.get('economia_sobre_publicado') or 0 for p in conf))}.", ""]
    for p in sorted(conf, key=lambda p: -(p.get("economia") or 0))[:8]:
        linhas += [f"### {p['hora']} · {p['produto']}", "", "```", p["frase"], "```", ""]
    # nota da equipe: conferida x publicada
    dif = Counter()
    for p in posts:
        pub = p.get("publicada") or {}
        if pub.get("nota"):
            dif["mesma nota" if pub["nota"] == p["nota_conferida"] else
                f"{p['nota_conferida']} → {pub['nota']}"] += 1
    linhas += ["## Nota da equipe sobre o preço publicado", "",
               "Conferida (a de hoje, com o preço da página) → publicada (o preço do texto do post):", ""]
    linhas += [f"- {k}: {v}" for k, v in dif.most_common(12)]
    # simulação
    vezes = r.get("vezes") or []
    if vezes:
        modos = Counter(v["modo"] for v in vezes)
        linhas += ["", "## Simulação da rodada diurna (fila paralela, de 7 em 7 min)", ""]
        linhas += [f"- {k}: {v}" for k, v in modos.most_common()]
        sim_chaves = {v["chave"]: v for v in vezes if v.get("chave")}
        equipe = defaultdict(list)
        for p in posts:
            hora = time.strftime("%H:%M", time.localtime(p["ts"]))
            if "08:00" <= hora <= "17:50":
                equipe[p["chave"]].append(p)
        ambos = set(sim_chaves) & set(equipe)
        antes = [k for k in ambos if sim_chaves[k]["ts"] < min(p["ts"] for p in equipe[k])]
        linhas += ["", f"Perfumes escolhidos pela simulação: {len(sim_chaves)}; postados pela equipe no turno: "
                   f"{len(equipe)}; em comum: {len(ambos)} (a simulação pegaria antes em {len(antes)}).", ""]
        depois = Counter(v.get("depois") for v in vezes if v.get("depois"))
        linhas += ["Depois da escolha (releitura em 1 h, avaliação com dados posteriores): "
                   + ", ".join(f"{k}: {v}" for k, v in depois.most_common()), ""]
        for v in vezes[:3] + [x for x in vezes if x.get("troca")][:3]:
            if v.get("frase"):
                linhas += [f"### {v['hora']} · {v['modo']} · {v['produto']}", "", "```", v["frase"], "```", ""]
    # inícios de cupom
    linhas += ["## Início de cupom percebido pelos grupos (sombra)", ""]
    for i in r.get("inicios_de_cupom") or []:
        pp = i.get("primeiro_post_da_equipe")
        q = i.get("quente_na_curadora")
        linhas.append(f"- {i['frase']}. 1º post da equipe com ele: {_h(pp) if pp else 'nenhum em 6 h'}"
                      + (f" ({(pp - i['ts']) / 60:+.0f} min)" if pp else "")
                      + f"; quente na curadora (1ª avaliação com ele): {_h(q) if q else '-'}.")
    c = r["custos"]
    linhas += ["", "## Custo de consultas (modelo, nenhuma consulta feita)", "", f"Modelo: {c['modelo']}.", "",
               f"Por dia: {c['por_dia']}. Por uso: {c['por_uso']}. Pico numa hora: {c['pico_hora']}. "
               f"Horas acima do teto ({c['teto']['hora']}/h, +{c['teto']['reserva_hora']}/h na reserva): "
               f"{len(c['horas_acima_do_teto'])}.", ""]
    return "\n".join(linhas)


if __name__ == "__main__":
    sys.exit(main())
