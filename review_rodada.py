"""Review padrão do fim da rodada (18/10, Breno: "por padrão coloque isso no fim das rodadas, esse review padrão do
fim"). Só leitura. Junta o que o pipeline mandou, o que a normalizadora fez, o que o Breno corrigiu na Edição e o que
foi para o GrupoFinal, e mostra acertos, erros e correções.

19/10: tudo é casado pela HORA, não só pelo link. O link de catálogo se repete (o mesmo meli.la sai de manhã, à tarde e
no dia seguinte): casado só pelo link, a correção de ontem aparecia como de hoje e o post da manhã contava como "foi ao
GrupoFinal" para o envio da tarde.

Uso (na VPS, em ~/curadora):  python3 review_rodada.py [AAAA-MM-DD]   (sem data: hoje)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict

from curadora.cupons import cupons_da_mensagem

BANCO_FOTOS = os.path.expanduser("~/.hermes/perfume_image_bank/")
FEEDBACK = BANCO_FOTOS + "logs/normalizer_feedback.jsonl"
CICLOS = BANCO_FOTOS + "logs/editorial_comparison_cycles.jsonl"
POR = re.compile(r"[Pp]or\s+\*?R\$\s*([\d.]+(?:,\d{2})?)")
NOTAS = ("IMPERDÍVEL", "BOA", "OK", "NÃO VALE", "FORA")
JANELA_POST_S = 30 * 60       # o post da normalizadora sai até 30 min depois do envio
JANELA_CICLO_S = 15 * 60      # o ciclo da Edição aponta o post da normalizadora (normalizer_ts)
JANELA_EDICAO_S = 12 * 3600   # Edição e GrupoFinal contam até 12 h depois do envio


def codigo(link: str | None) -> str:
    achado = re.search(r"meli\.la/([A-Za-z0-9]+)", link or "")
    return achado.group(1) if achado else ""


def hora(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def ler_jsonl(caminho: str, desde: float, campo_ts: str, ultimos_bytes: int | None = None) -> list[dict]:
    if not os.path.exists(caminho):
        return []
    saida = []
    with open(caminho, "rb") as f:
        if ultimos_bytes:
            offset = max(0, os.path.getsize(caminho) - ultimos_bytes)
            f.seek(offset)
            if offset:
                f.readline()
        for bruto in f:
            try:
                r = json.loads(bruto)
            except ValueError:
                continue
            if (r.get(campo_ts) or 0) >= desde:
                saida.append(r)
    return saida


def mesma_foto(ciclo: dict) -> bool:
    """18/10: 4 kits apareceram como "foto trocada", mas a foto da Edição era a mesma da normalizadora (o registro de
    antes vem sem assinatura; o nome do arquivo do banco termina com o começo dela)."""
    ic = ciclo.get("image_change") or {}
    achado = re.search(r"_([0-9a-f]{12})[0-9a-f]*\.(?:jpe?g|png|webp)$", ic.get("before_path") or "")
    return bool(achado) and (ic.get("after_sha256") or "").startswith(achado.group(1))


def correcoes(ciclo: dict | None) -> list[str] | None:
    """O que o Breno mudou na Edição; None se a promo não passou pela Edição."""
    if not ciclo:
        return None
    antes = (ciclo.get("before") or {}).get("copy") or ""
    depois = (ciclo.get("after") or {}).get("copy") or ""
    feitas = []
    pa, pd = POR.search(antes), POR.search(depois)
    if pa and pd and pa.group(1) != pd.group(1):
        feitas.append("preço")
    if set(cupons_da_mensagem(antes)) != set(cupons_da_mensagem(depois)):
        feitas.append("cupom")
    if (ciclo.get("headline_change") or {}).get("class") not in ("unchanged", "emoji_or_punctuation_only", None):
        feitas.append("chamada")
    if (ciclo.get("title_change") or {}).get("class") not in ("unchanged", "cleanup_only", None):
        feitas.append("título")
    if (ciclo.get("image_change") or {}).get("class") in ("changed", "added_in_edition") and not mesma_foto(ciclo):
        feitas.append("foto")
    if ("Loja Oficial" in antes) != ("Loja Oficial" in depois):
        feitas.append("loja")
    return feitas


def campo(ciclo: dict, lado: str, nome: str) -> str:
    return str((ciclo.get(lado) or {}).get(nome) or "").strip("* ").strip()


def juntar(envios: list[dict], posts: list[dict], ciclos: list[dict], grupofinal: list[tuple]) -> list[dict]:
    """Para cada envio do pipeline (em ordem de hora): o post da normalizadora, o ciclo da Edição e a hora no GrupoFinal.
    Cada post, ciclo e postagem do GrupoFinal serve a um envio só."""
    posts_link, ciclos_link, grupofinal_link, grupofinal_chave = (defaultdict(list) for _ in range(4))
    for p in posts:
        posts_link[codigo(p.get("link"))].append(p)
    for c in ciclos:
        ciclos_link[codigo(c.get("link"))].append(c)
    for i, (ts, link, chave) in enumerate(grupofinal):
        grupofinal_link[codigo(link)].append((ts, i))
        if chave:
            grupofinal_chave[chave].append((ts, i))
    usados_p, usados_c, usados_h = set(), set(), set()
    saida = []
    for e in sorted(envios, key=lambda r: r["ts"]):
        cod = codigo(e.get("link_afiliado"))
        p = min((x for x in posts_link.get(cod, []) if id(x) not in usados_p
                 and e["ts"] - 60 <= x["ts"] <= e["ts"] + JANELA_POST_S), key=lambda x: x["ts"], default=None)
        if p:
            usados_p.add(id(p))
            cands = [c for c in ciclos_link.get(cod, []) if id(c) not in usados_c
                     and abs((c.get("normalizer_ts") or 0) - p["ts"]) <= JANELA_CICLO_S]
            c = min(cands, key=lambda c: abs(c["normalizer_ts"] - p["ts"]), default=None)
        else:
            c = min((c for c in ciclos_link.get(cod, []) if id(c) not in usados_c
                     and e["ts"] <= (c.get("edition_final_ts") or 0) <= e["ts"] + JANELA_EDICAO_S),
                    key=lambda c: c["edition_final_ts"], default=None)
        if c:
            usados_c.add(id(c))
        h = min((x for x in grupofinal_link.get(cod, []) + grupofinal_chave.get(e.get("chave"), [])
                 if x[1] not in usados_h and e["ts"] - 60 <= x[0] <= e["ts"] + JANELA_EDICAO_S), default=None)
        if h:
            usados_h.add(h[1])
        mud = correcoes(c)
        situacao = (("postou sem edição identificada" if mud is None else
                     "postou sem mexer" if not mud else "postou corrigida") if h else
                    "editou e não postou" if mud is not None else "não usou")
        saida.append({"envio": e, "post": p, "ciclo": c, "grupofinal": h[0] if h else None, "mud": mud, "situacao": situacao})
    return saida


def main() -> None:
    dia = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")
    inicio = time.mktime(time.strptime(dia, "%Y-%m-%d"))
    fim = inicio + 86400
    base = os.path.dirname(os.path.abspath(__file__))
    con = sqlite3.connect(f"file:{os.path.join(base, 'dados/curadora.db')}?mode=ro", uri=True, timeout=30)

    envios, sem_resposta = [], 0
    for r in ler_jsonl(os.path.join(base, "dados/pipeline.jsonl"), inicio, "ts"):
        if r["ts"] >= fim:
            continue
        if r.get("status") == "enviado":
            envios.append(r)
        elif r.get("status") == "gerador_sem_resposta":
            sem_resposta += 1
    if not envios:
        print(f"{dia}: o pipeline não mandou nada nesse dia.")
        return
    posts = ler_jsonl(FEEDBACK, inicio - 3600, "ts", 60_000_000)
    ciclos = ler_jsonl(CICLOS, inicio - 86400, "edition_final_ts")
    grupofinal = list(con.execute("SELECT ts, link, chave FROM postagens WHERE ts >= ? AND ts < ? ORDER BY ts",   # só o GrupoFinal
                             (inicio, fim + JANELA_EDICAO_S)))
    itens = juntar(envios, posts, ciclos, grupofinal)

    primeiro, ultimo = itens[0]["envio"]["ts"], itens[-1]["envio"]["ts"]
    print(f"REVIEW DA RODADA · {time.strftime('%d/%m', time.localtime(inicio))} · {hora(primeiro)}–{hora(ultimo)}")
    selos = Counter("✅" if "✅" in str(i["post"].get("approval_grade")) else "⚠️" if "⚠" in str(i["post"].get("approval_grade"))
                    else "❌" for i in itens if i["post"])
    print(f"Pipeline mandou {len(itens)} (+{sem_resposta} sem resposta do Gerador) · normalizadora postou "
          f"{sum(1 for i in itens if i['post'])} ({', '.join(f'{k} {v}' for k, v in selos.items())})")
    contagem = Counter(i["situacao"] for i in itens)
    no_h = sum(1 for i in itens if i["grupofinal"] is not None)
    print(f"No GrupoFinal: {no_h} de {len(itens)} ({no_h / len(itens) * 100:.0f}%) · sem mexer {contagem['postou sem mexer']} · "
          f"corrigidas {contagem['postou corrigida']} · sem edição identificada "
          f"{contagem['postou sem edição identificada']} · editou e não postou {contagem['editou e não postou']} · "
          f"não usou {contagem['não usou']}")
    tempos = [i["grupofinal"] - i["envio"]["ts"] for i in itens if i["grupofinal"]]
    if tempos:
        print(f"Do envio ao GrupoFinal: mediana {statistics.median(tempos) / 60:.0f} min")
    etapas = {
        "Gerador": [i["envio"]["gerador_s"] for i in itens if isinstance(i["envio"].get("gerador_s"), (int, float))],
        "Envio → normalizadora": [i["post"]["ts"] - i["envio"]["ts"] for i in itens if i["post"]],
        "Normalizadora → edição final": [(i["ciclo"].get("edition_final_ts") or 0) - i["post"]["ts"]
                                         for i in itens if i["ciclo"] and i["post"]],
        "Edição final → GrupoFinal": [i["grupofinal"] - i["ciclo"]["edition_final_ts"]
                                for i in itens if i["grupofinal"] and i["ciclo"] and i["ciclo"].get("edition_final_ts")],
    }
    for nome, valores in etapas.items():
        validos = [v for v in valores if v >= 0]
        if validos:
            m = statistics.median(validos)   # gerador_s já vem em segundos; as outras etapas também, mas longas
            print(f"  {nome}: mediana {f'{m:.1f} s' if nome == 'Gerador' else f'{m / 60:.1f} min'} ({len(validos)} casos)")
    print("  (cada etapa tem a sua amostra: as medianas não se somam)")
    n_ciclos = sum(1 for i in itens if i["ciclo"])
    if not n_ciclos:
        # 31/10: em 21 e 28/10 a Edição não gravou nenhum ciclo; sem ciclo, "sem mexer" não é prova de nada.
        print("  ATENÇÃO: nenhum ciclo da Edição gravado para os envios deste dia: correções e 'sem mexer' não dá para medir")
    print(f"GrupoFinal no dia: {sum(1 for h in grupofinal if h[0] < fim)} posts, {no_h} associados ao pipeline")

    print("\nPOR NOTA (mandadas → no GrupoFinal)")
    for nota in NOTAS:
        grupo = [i for i in itens if str(i["envio"].get("nota", "")).startswith(nota[:4])]
        if grupo:
            n = sum(1 for i in grupo if i["situacao"].startswith("postou"))
            print(f"  {nota:10} {len(grupo):3} → {n:3} ({n / len(grupo) * 100:3.0f}%)")

    tipos = Counter(x for i in itens for x in (i["mud"] or []))
    print("\nCORREÇÕES QUE VOCÊ FEZ (nas que passaram pela Edição)")
    print("  " + (" · ".join(f"{k} {v}" for k, v in tipos.most_common()) or "nenhuma"))
    exemplos = defaultdict(list)
    for i in itens:
        e, c, p = i["envio"], i["ciclo"], i["post"] or {}
        nome = str(p.get("display_product") or e["produto"])
        for tipo in i["mud"] or []:
            if tipo == "chamada":
                exemplos[tipo].append(f"{hora(e['ts'])} {campo(c, 'before', 'headline')[:34]} → {campo(c, 'after', 'headline')[:34]}")
            elif tipo == "título":
                exemplos[tipo].append(f"{hora(e['ts'])} {campo(c, 'before', 'title')[:40]} → {campo(c, 'after', 'title')[:40]}")
            elif tipo in ("preço", "cupom", "loja"):
                a = POR.search((c.get("before") or {}).get("copy") or "")
                d = POR.search((c.get("after") or {}).get("copy") or "")
                exemplos[tipo].append(f"{hora(e['ts'])} {nome[:40]} R${a.group(1) if a else '?'} → R${d.group(1) if d else '?'}")
            else:
                exemplos[tipo].append(f"{hora(e['ts'])} {nome[:50]}")
    for tipo, linhas in exemplos.items():
        print(f"  {tipo}:")
        for linha in linhas[:6]:
            print(f"    {linha}")
        if len(linhas) > 6:
            print(f"    (+{len(linhas) - 6})")

    print("\nACERTOS (postadas sem mexer)")
    acertos = [i["envio"] for i in itens if i["situacao"] == "postou sem mexer"]
    for e in acertos[:8]:
        print(f"  {hora(e['ts'])} {str(e.get('nota'))[:5]:5} {e.get('score', 0):3} R${e['preco']:>7.2f} {e['produto'][:50]}")
    if len(acertos) > 8:
        print(f"  (+{len(acertos) - 8})")

    print("\nNÃO USADAS (nem Edição nem GrupoFinal)")
    for i in itens:
        if i["situacao"] == "não usou":
            e, p = i["envio"], i["post"] or {}
            alerta = "; ".join(str(a)[:34] for a in (p.get("approval_alerts") or [])[:2])
            print(f"  {hora(e['ts'])} {str(e.get('nota'))[:5]:5} {e.get('score', 0):3} R${e['preco']:>7.2f} {e['produto'][:44]:44} {alerta}")


if __name__ == "__main__":
    main()
