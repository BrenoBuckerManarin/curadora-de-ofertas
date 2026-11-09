"""Relê o texto original de todo o histórico com o parser atual (depois de uma correção no parser).

Corrige o nome/identidade das ofertas que tinham sido lidas errado (ex.: em 04/10, "*NATURA HOMEM EVOLUTIO*"
seguido de "O Le Male Elixir nacional." virava Le Male Elixir e sujava o histórico dele) e tira o que não é
perfume (Lego, cartas Pokémon...). No painel, as avaliações das últimas 48h são refeitas do mesmo jeito.

Uso:  python reprocessar.py            mostra o que mudaria (não grava nada)
      python reprocessar.py --aplicar  faz backup do banco e grava
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter

from curadora import carregar_config
from curadora.banco import CAMINHO_PADRAO, aprender_catalogo, carregar_catalogo, conectar
from curadora.parser import ler_promo, parece_perfume
from curadora.score import Curadora, para_dict

JANELA_PAINEL_S = 48 * 3600


def reprocessar(con, cfg: dict, aplicar: bool, agora: float | None = None) -> dict:
    cur = Curadora(cfg, con)
    marcas = cfg["marcas"]
    res = {"ofertas_renomeadas": [], "ofertas_removidas": [], "avaliacoes_refeitas": 0, "avaliacoes_fora": 0,
           "chaves_mudadas": Counter()}
    for r in con.execute("SELECT id, produto, texto FROM ofertas WHERE texto IS NOT NULL AND texto != ''").fetchall():
        p = ler_promo(r["texto"], marcas)
        if not p.valida:
            continue  # o parser atual não leu: mantém como estava (só remove o que tem certeza que não é perfume)
        if not parece_perfume(p.produto, r["texto"]):
            res["ofertas_removidas"].append(r["produto"])
            if aplicar:
                con.execute("DELETE FROM ofertas WHERE id = ?", (r["id"],))
        elif p.produto != r["produto"]:
            res["ofertas_renomeadas"].append((r["produto"], p.produto))
            if aplicar:
                a = cur.ident.analisar(p.produto)
                con.execute("UPDATE ofertas SET produto=?, base=?, volume=?, genero=? WHERE id=?",
                            (p.produto, a.base, a.volume, a.genero, r["id"]))
    # Identidade nova (ex.: erros de digitação juntados): quais chaves do histórico mudam.
    catalogo = carregar_catalogo(con)
    for r in con.execute("SELECT produto, chave FROM ofertas").fetchall():
        nova = catalogo.chave(cur.ident.analisar(r["produto"])).chave
        if nova != r["chave"]:
            res["chaves_mudadas"][(r["chave"], nova)] += 1
    if aplicar:
        con.commit()
        aprender_catalogo(con, cur.ident)  # recalcula as chaves de todo o histórico
        cur = Curadora(cfg, con)            # recarrega o catálogo novo

    desde = int(agora or time.time()) - JANELA_PAINEL_S
    for r in con.execute("SELECT id, ts, grupo, chave, dados FROM avaliacoes WHERE ts >= ? AND origem = 'ao vivo'",
                         (desde,)).fetchall():
        dados = json.loads(r["dados"])
        texto = dados.get("texto") or ""
        if not texto:
            continue
        p = ler_promo(texto, marcas)
        if not p.valida:
            continue
        if not parece_perfume(p.produto, texto):
            res["avaliacoes_fora"] += 1
            if aplicar:  # não é perfume: sai de todas as seções (fica no banco, com nota FORA)
                con.execute("UPDATE avaliacoes SET nota='FORA' WHERE id=?", (r["id"],))
        elif p.produto == dados.get("produto"):
            if aplicar and r["chave"] != (nova := cur.chave_de(p.produto)):
                dados["chave"] = nova  # mesmo nome, identidade nova: o painel junta no produto certo
                con.execute("UPDATE avaliacoes SET chave=?, dados=? WHERE id=?",
                            (nova, json.dumps(dados, ensure_ascii=False), r["id"]))
        else:
            res["avaliacoes_refeitas"] += 1
            if aplicar:
                av = cur.avaliar(p, agora=r["ts"], conferir_ml=False)
                av.grupo = r["grupo"] or ""
                con.execute("UPDATE avaliacoes SET chave=?, nota=?, score=?, dados=? WHERE id=?",
                            (av.identidade.chave, av.nota, av.score, json.dumps(para_dict(av), ensure_ascii=False),
                             r["id"]))
    if aplicar:
        con.commit()
    return res


def renotar(con, cfg: dict, aplicar: bool, horas: float = 48, notas=("IMPERDÍVEL", "BOA")) -> list[tuple]:
    """Recalcula a nota das avaliações recentes com as regras atuais, usando o que o ML mostrou na hora
    (vendedor, selo, reputação, preço), sem abrir o ML de novo. Serve para links que o ML não deixa reabrir
    (06/10: vendedores suspeitos que continuavam em Excelentes depois da regra nova de risco)."""
    from curadora.ml import CardML
    cur = Curadora(cfg, con)
    mudaram = []
    marcas = ",".join("?" * len(notas))
    for r in con.execute(f"SELECT id, ts, grupo, nota, score, dados FROM avaliacoes WHERE ts >= ? AND nota IN ({marcas})",
                         (time.time() - horas * 3600, *notas)).fetchall():
        d = json.loads(r["dados"])
        ml = d.get("ml") or {}
        if not d.get("texto"):
            continue
        p = ler_promo(d["texto"], cfg["marcas"])
        if not p.valida or not parece_perfume(p.produto, d["texto"]):
            # Item que não é perfume (camiseta, skincare...) avaliado antes do filtro novo: sai de todas as seções.
            mudaram.append((d.get("produto", "")[:45], r["nota"], r["score"], "FORA", 0, "não é perfume"))
            if aplicar:
                con.execute("UPDATE avaliacoes SET nota='FORA' WHERE id=?", (r["id"],))
            continue
        card = None
        if ml.get("preco"):
            card = CardML(ok=True, titulo=ml.get("titulo") or "", preco=ml["preco"], vendedor=ml.get("vendedor") or "",
                          loja_oficial=bool(ml.get("oficial")), full=bool(ml.get("full")),
                          mais_vendido=bool(ml.get("mais_vendido")), url_produto=ml.get("url") or "",
                          foto=ml.get("foto") or "", reputacao=d.get("reputacao") or {})
        av = cur.avaliar(d["texto"], agora=r["ts"], conferir_ml=False, card=card)
        if av is None or (av.nota == r["nota"] and av.score == r["score"]):
            continue
        mudaram.append((d.get("produto", "")[:45], r["nota"], r["score"], av.nota, av.score, av.risco_motivo[:70]))
        if aplicar:
            novo = para_dict(av)
            for k in ("reconferida_de", "preco_antes", "nota_antes"):  # mantém o vínculo da reconferência
                if k in d:
                    novo[k] = d[k]
            av.grupo = r["grupo"] or ""
            con.execute("UPDATE avaliacoes SET nota=?, score=?, dados=? WHERE id=?",
                        (av.nota, av.score, json.dumps(novo, ensure_ascii=False), r["id"]))
            if len(mudaram) % 300 == 0:
                con.commit()  # em lotes: não segura o banco enquanto a curadora ao vivo grava
    if aplicar:
        con.commit()
    return mudaram


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aplicar", action="store_true", help="grava (faz backup antes)")
    ap.add_argument("--banco", default=str(CAMINHO_PADRAO))
    ap.add_argument("--renotar", action="store_true",
                    help="recalcula a nota das Excelentes/Boas das últimas 48h com as regras atuais")
    args = ap.parse_args()
    if args.aplicar:
        from manutencao import backup
        print("backup:", backup())
    con = conectar(args.banco)
    if args.renotar:
        mudaram = renotar(con, carregar_config(), args.aplicar, notas=("IMPERDÍVEL", "BOA", "OK", "NÃO VALE"))
        print(f"{len(mudaram)} avaliações mudam de nota")
        for m in mudaram[:60]:
            print(f"   {m[0]!r:48} {m[1]} {m[2]} -> {m[3]} {m[4]}  {m[5]}")
        print("GRAVADO" if args.aplicar else "(só simulação; use --aplicar para gravar)")
        return 0
    res = reprocessar(con, carregar_config(), args.aplicar)
    print(f"ofertas renomeadas: {len(res['ofertas_renomeadas'])}")
    for antes, depois in res["ofertas_renomeadas"][:40]:
        print(f"   {antes[:55]!r} -> {depois[:45]!r}")
    print(f"ofertas removidas (não é perfume / não é promo): {len(res['ofertas_removidas'])}")
    for nome in res["ofertas_removidas"][:30]:
        print(f"   {nome[:70]!r}")
    print(f"identidade: {sum(res['chaves_mudadas'].values())} ofertas mudam de chave "
          f"({len(res['chaves_mudadas'])} pares)")
    for (antes, depois), n in res["chaves_mudadas"].most_common(60):
        print(f"   {n:4d}  {antes[:45]:45} -> {depois[:45]}")
    print(f"painel: {res['avaliacoes_refeitas']} refeitas, {res['avaliacoes_fora']} tiradas (não é perfume)")
    print("GRAVADO" if args.aplicar else "(só simulação; use --aplicar para gravar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
