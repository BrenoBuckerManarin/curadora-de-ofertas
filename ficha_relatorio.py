"""Relatório da ficha do cupom em modo sombra (30/10, Breno: "Ficha do cupom"). Só leitura.

Junta o que a ficha faria (dados/ficha_cupom_sombra.jsonl, gravado pelo pipeline e pela 2ª leitura) com o que o Breno
decidiu na Edição (cupom_sinais do banco da curadora): manteve ou pôs = cupom bom; tirou = cupom errado. Mostra, por
dia e por cupom, quantas a lógica de hoje acertou e quantas a ficha acertaria, os cupons errados que a ficha evitaria,
os bons que ela perderia ou recuperaria, e as retiradas que a ficha leria diferente do que foi gravado.
30/10 (revisor): o placar também sai separado pela decisão da ficha (faixa 1, faixa 2, sonda, segurado pelo ritmo, sem),
com a linha "a ficha poria e você não pôs" (post que foi para o GrupoFinal sem o cupom), as promos que o pipeline pulou por
"sem o cupom não vale" e que a ficha mandaria com ele, e as faixas das candidatas nas rodadas de cupom.

Uso (na VPS, em ~/curadora):  python3 ficha_relatorio.py [--dia AAAA-MM-DD]   (sem data: todos os dias do registro)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
SOMBRA = RAIZ / "dados" / "ficha_cupom_sombra.jsonl"
BANCO = RAIZ / "dados" / "curadora.db"
BOM = ("manteve", "pôs")
JANELA_DECISAO_S = 12 * 3600


def hora(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def dia_de(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def ler_registro(caminho: Path) -> list[dict]:
    linhas = []
    try:
        texto = Path(caminho).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return linhas
    for linha in texto.splitlines():
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ts"):
            linhas.append(r)
    return linhas


def decisao_do_breno(con, codigo: str, post: str, desde: float, ate: float | None = None) -> str:
    """A última palavra do Breno sobre o cupom neste post: 'pôs' | 'manteve' | 'tirou' | '' (não decidiu)."""
    if not post:
        return ""
    ate = min(desde + JANELA_DECISAO_S, ate) if ate is not None else desde + JANELA_DECISAO_S
    por_ref: dict = {}
    for ref, tipo in con.execute("SELECT ref, tipo FROM cupom_sinais WHERE codigo = ? AND post = ? AND fonte = 'breno' "
                                 "AND ts >= ? AND ts < ? ORDER BY ts, id", (codigo, post, desde, ate)):
        tipos = por_ref.pop(ref, set())   # a ordem é a do último sinal de cada edição
        por_ref[ref] = tipos | {tipo}
    if not por_ref:
        return ""
    tipos = list(por_ref.values())[-1]
    return "pôs" if "posto" in tipos else "tirou" if tipos & {"esgotou", "fora"} else "manteve"


def _item(r: dict, d: dict) -> str:
    motivo = d.get("motivo") if d.get("vivo") else d.get("vivo_motivo")
    return f"{hora(r['ts'])} {str(r.get('produto') or '')[:28]} ({r.get('vendedor') or 'loja ?'}, {d.get('classe')}): {motivo}"


def postado(con, post: str, desde: float, ate: float | None = None, sem_cupom: str = "", chave: str = "") -> str:
    """O post (meli.la/...) apareceu no GrupoFinal? Devolve "link" (confirmado: o mesmo link), "chave" (provável: o mesmo
    produto, outro link) ou "" (não achou).

    31/10 (revisão): desde 20/10 o GrupoFinal sai com OUTRO meli.la (o link que o Breno gera ao postar), então o link do
    envio quase nunca aparece lá (1 de 275 em 24-30/10). Vale também a mesma chave de produto, como na review da
    rodada, a partir de 2 min depois do envio (antes disso o post é da equipe, não deste envio). Pela chave é só
    PROVÁVEL: pode ser um post da equipe do mesmo produto (4% dos casos de 20-30/10 tinham outra chamada)."""
    if not post:
        return ""
    ate = min(desde + JANELA_DECISAO_S, ate) if ate is not None else desde + JANELA_DECISAO_S
    link = "https://" + post.removeprefix("https://").removeprefix("http://")
    achado = ""
    for (dados, mesmo_link) in con.execute(
            "SELECT dados, link IN (?, ?, ?) FROM postagens WHERE ((link IN (?, ?, ?) AND ts >= ?) "
            "OR (? != '' AND chave = ? AND ts >= ?)) AND ts < ?",
            (link, link.replace("https://", "http://", 1), link[8:],
             link, link.replace("https://", "http://", 1), link[8:], desde,
             chave, chave, desde + 120, ate)):
        tipo = "link" if mesmo_link else "chave"
        if not sem_cupom:
            if tipo == "link":
                return "link"
            achado = achado or "chave"
            continue
        try:
            cupons = json.loads(dados).get("cupons")
        except (ValueError, TypeError, AttributeError):
            continue
        # Ausência de dados não prova ausência do cupom no texto publicado.
        if isinstance(cupons, list) and sem_cupom not in cupons:
            if tipo == "link":
                return "link"
            achado = achado or "chave"
    return achado


# 30/10 (revisor): o placar separado pela decisão da ficha, para decidir sobre a sonda (o ponto mais incerto)
TIPOS = (("com1", "com o cupom, faixa 1 (loja confirmada)"), ("com2", "com o cupom, faixa 2 (classe aberta)"),
         ("sonda", "sonda (faixa 3: sem prova, 1 por loja)"), ("segura", "segurado pelo ritmo (3 até você decidir)"),
         ("sem_conc", "sem o cupom: classe fechada só pela concentração dos grupos em marca"),
         ("sem", "sem o cupom (outros motivos)"))


def tipo_da_decisao(d: dict) -> str:
    dec = d.get("decisao")
    if dec == "com":
        return "com1" if d.get("faixa") == 1 else "com2"
    if dec in ("sonda", "segura"):
        return dec
    if d.get("vale") == "não" and "posts de grupo" in str(d.get("motivo") or ""):
        return "sem_conc"   # ficha_cupom.CONC_FECHA: o ponto que o revisor questionou (cupom de site inteiro)
    return "sem"


def montar(linhas: list[dict], con, dia: str | None = None) -> str:
    """O relatório em texto, para o Breno ler."""
    dias = sorted({dia_de(r["ts"]) for r in linhas if r.get("tipo") in ("envio", "retirada", "pulo")})
    if dia:
        dias = [dia]
    if not dias:
        return "A ficha do cupom ainda não registrou nada (modo sombra desligado ou nenhum envio com cupom)."
    saida = []
    # Um link reutilizado pertence ao próximo envio a partir da hora dele.
    proximos = {}
    ultimos = {}
    for r in sorted((r for r in linhas if r.get("tipo") == "envio"), key=lambda r: r["ts"], reverse=True):
        post = r.get("post") or ""
        proximos[id(r)] = ultimos.get(post, r["ts"] + JANELA_DECISAO_S)
        if post:
            ultimos[post] = r["ts"]
    for d_ in dias:
        do_dia = [r for r in linhas if dia_de(r["ts"]) == d_]
        envios = [r for r in do_dia if r.get("tipo") == "envio"]
        saida.append(f"══ {d_[8:]}/{d_[5:7]} ══  {len(envios)} envios registrados pela ficha (modo sombra)")
        por_cupom: dict = defaultdict(lambda: {"n": Counter(), "evita": [], "perde": [], "recupera": [], "erra": []})
        por_tipo: dict = defaultdict(Counter)
        poria: list = []
        poria_provavel: list = []   # 31/10: GrupoFinal achado só pela chave (outro link): provável, não confirmado
        for r in envios:
            for codigo, d in (r.get("ficha") or {}).items():
                breno = decisao_do_breno(con, codigo, r.get("post") or "", r["ts"], proximos[id(r)])
                atual = d.get("atual")
                ficha = "com" if d.get("decisao") in ("com", "sonda") else "sem"
                if atual == "sem" and ficha == "sem" and breno != "pôs":
                    continue   # cupom que ninguém pôs: não conta
                if atual == "sem" and ficha == "com" and breno != "pôs":
                    # 30/10 (revisor): o risco principal ("cupom novo sem ninguém pedir") não fica mais escondido
                    # em "sem decisão": a ficha poria, o post foi para o GrupoFinal e você não pôs.
                    no_grupofinal = postado(con, r.get("post") or "", r["ts"], proximos[id(r)], sem_cupom=codigo,
                                       chave=r.get("chave") or "")
                    if no_grupofinal:
                        (poria if no_grupofinal == "link" else poria_provavel).append(f"{codigo}: {_item(r, d)}")
                        por_tipo[tipo_da_decisao(d)]["poria e você não pôs"] += 1
                        continue
                c = por_cupom[codigo]
                c["n"]["envios"] += 1
                tipo = tipo_da_decisao(d)
                if not breno:
                    c["n"]["sem decisão"] += 1
                    por_tipo[tipo]["sem decisão"] += 1
                    continue
                c["n"]["decididos"] += 1
                bom = breno in BOM
                por_tipo[tipo]["bom" if bom else "tirou"] += 1
                if atual == "com":
                    c["n"]["atual certo" if bom else "atual errado"] += 1
                elif breno == "pôs":
                    c["n"]["atual faltou"] += 1
                if ficha == "com":
                    c["n"]["ficha certo" if bom else "ficha errado"] += 1
                    if not bom:
                        c["erra"].append(_item(r, d))
                elif bom:
                    c["n"]["ficha faltou"] += 1
                if atual == "com" and ficha == "sem":
                    (c["evita"] if not bom else c["perde"]).append(_item(r, d))
                if atual == "sem" and ficha == "com" and breno == "pôs":
                    c["recupera"].append(_item(r, d))
        if not por_cupom and not poria and not poria_provavel:
            saida.append("  Nenhum envio com cupom (nem cupom posto à mão) neste dia.")
        tot = Counter()
        for codigo, c in sorted(por_cupom.items()):
            n = c["n"]
            tot.update(n)
            saida.append(f"\n  {codigo}: {n['envios']} envios, {n['decididos']} com decisão sua"
                         + (f" ({n['sem decisão']} sem decisão: não postados ou ainda na fila)" if n["sem decisão"] else ""))
            saida.append(f"    hoje (no ar):  {_placar(n['atual certo'], n['atual errado'], n['atual faltou'])}")
            saida.append(f"    com a ficha:   {_placar(n['ficha certo'], n['ficha errado'], n['ficha faltou'])}")
            for titulo, lista in (("cupons errados que a ficha EVITARIA", c["evita"]),
                                  ("cupons bons que a ficha PERDERIA", c["perde"]),
                                  ("cupons que você pôs à mão e a ficha teria posto", c["recupera"]),
                                  ("cupons errados que a ficha também mandaria", c["erra"])):
                if lista:
                    saida.append(f"    {titulo} ({len(lista)}):")
                    saida.extend(f"      · {x}" for x in lista)
        if len(por_cupom) > 1:
            saida.append(f"\n  TOTAL do dia: hoje {_placar(tot['atual certo'], tot['atual errado'], tot['atual faltou'])}"
                         f" | ficha {_placar(tot['ficha certo'], tot['ficha errado'], tot['ficha faltou'])}")
        if por_tipo:
            saida.append("\n  Pela decisão da ficha (você manteve ou pôs / você tirou / sem decisão / a ficha poria e "
                         "você não pôs):")
            for tipo, nome in TIPOS:
                n = por_tipo.get(tipo)
                if n:
                    saida.append(f"    {nome}: {n['bom']} / {n['tirou']} / {n['sem decisão']} / "
                                 f"{n['poria e você não pôs']}")
        saida.append(f"\n  A ficha poria o cupom e você não pôs (o post foi para o GrupoFinal sem ele): {len(poria)} "
                     f"confirmados (mesmo link) + {len(poria_provavel)} prováveis (mesmo produto, outro link)")
        saida.extend(f"    · {x}" for x in poria)
        saida.extend(f"    · (provável) {x}" for x in poria_provavel)
        pulos = [r for r in do_dia if r.get("tipo") == "pulo"]
        if pulos:
            com = [r for r in pulos if r.get("ficha_mandaria")]
            saida.append(f"\n  Promos puladas por 'sem o cupom não vale': {len(pulos)}; a ficha mandaria com o cupom: "
                         f"{len(com)}")
            for r in com:
                sem = r.get("sem_cupom") or {}
                saida.append(f"    · {hora(r['ts'])} {str(r.get('produto') or '')[:28]} ({r.get('vendedor') or 'loja ?'}, "
                             f"{r.get('classe')}): {', '.join(r['ficha_mandaria'])}; sem o cupom "
                             f"{sem.get('nota', '?')} {sem.get('score', '')}".rstrip())
        fotos = [r for r in do_dia if r.get("tipo") == "candidatas"]
        if fotos:
            faixas = Counter()
            topo = Counter()
            for r in fotos:
                cands = r.get("candidatas") or []
                faixas.update(str(c.get("melhor_faixa") or "não") for c in cands)
                if cands:
                    topo[str(cands[0].get("melhor_faixa") or "não")] += 1
            total = sum(faixas.values()) or 1
            partes = ", ".join(f"faixa {k} {faixas[k] * 100 // total}%" for k in ("1", "2", "3") if faixas[k])
            saida.append(f"\n  Candidatas nas rodadas de cupom: {len(fotos)} fotos ({partes or 'nenhuma com o cupom'}"
                         f"{', não vale ' + str(faixas['não'] * 100 // total) + '%' if faixas['não'] else ''}); "
                         f"a 1ª da fila estava na faixa 1 em {topo['1']}, na 2 em {topo['2']}, na 3 em {topo['3']}")
        retiradas = [r for r in do_dia if r.get("tipo") == "retirada"]
        diferentes = [r for r in retiradas if r.get("diferente")]
        saida.append(f"\n  Retiradas (você tirou o cupom de um post): {len(retiradas)}; a ficha leria diferente: "
                     f"{len(diferentes)}")
        for r in diferentes:
            gravado = "acabou (esgotou)" if r.get("gravado") == "esgotou" else "não vale nessa loja (fora)"
            ficha = "acabou ('fim')" if r.get("ficha") == "fim" else "não vale nessa classe ('fora da classe')"
            depois = {"esgotou": "o cupom esgotaria", "suspenso": "o cupom ficaria suspenso até você confirmar",
                      "segue": "o cupom seguiria valendo onde já foi confirmado"}.get(r.get("ficha_depois"), "")
            saida.append(f"    · {hora(r['ts'])} {r.get('codigo')} ({(r.get('loja') or '?').split('|')[-1]}, "
                         f"{r.get('classe')}): gravado \"{gravado}\"; a ficha leria \"{ficha}\" ({r.get('motivo')}); "
                         f"{depois}")
        rodadas = [r for r in do_dia if r.get("tipo") == "rodada"]
        for r in rodadas:
            partes = ([f"ligou {', '.join(r['liga'])}"] if r.get("liga") else []) + \
                     ([f"desligou {', '.join(r['desliga'])}"] if r.get("desliga") else [])
            saida.append(f"  Rodada de cupom {hora(r['ts'])}: {'; '.join(partes)}")
        avisos = [r for r in do_dia if r.get("tipo") == "aviso"]
        if avisos:
            saida.append(f"  Avisos da sombra: {len(avisos)} (último às {hora(avisos[-1]['ts'])}: {avisos[-1].get('erro')})."
                         " Nenhum deles mudou envio.")
        saida.append("")
    return "\n".join(saida)


def _placar(certos: int, errados: int, faltou: int) -> str:
    total = certos + errados
    pct = f" ({certos * 100 // total}% certos)" if total else ""
    extra = f"; {faltou} que você pôs à mão e não tinha ido" if faltou else ""
    return f"{certos} certos, {errados} errados{pct}{extra}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Relatório da ficha do cupom (modo sombra)")
    ap.add_argument("--dia", help="AAAA-MM-DD (sem: todos os dias do registro)")
    ap.add_argument("--sombra", default=str(SOMBRA), help="registro da sombra (jsonl)")
    ap.add_argument("--banco", default=str(BANCO), help="banco da curadora")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = sqlite3.connect(Path(args.banco).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        print(montar(ler_registro(Path(args.sombra)), con, args.dia))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
