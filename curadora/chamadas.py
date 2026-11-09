"""Chamadas prontas (10/10/2026): repertório de títulos escrito antes de o produto chegar.

Medido em 14 dias: o Breno troca 10% das chamadas que vêm do repertório da normalizadora, 67% das que a IA escreve
na hora e 80% das de reserva (quando o tempo da IA acaba). A ideia dele: deixar chamadas boas já prontas por
produto. Aqui ficam as sugestões (escritas em lote, fora daqui); ele aprova, recusa ou edita no painel (/chamadas)
e cada aprovada entra na lista de chamadas manuais que a normalizadora já consulta
(`manual_exact_sku_repertoire`, em headline_preferences_v6.json), com as mesmas travas das outras.

Só o que ele aprova é gravado lá. Antes da primeira gravação do dia fica uma cópia do arquivo ao lado.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path

PREFERENCIAS = Path(os.path.expanduser("~/.hermes/perfume_image_bank/headline_preferences_v6.json"))
MAX_VISUAL = 28  # o limite da própria normalizadora (enforce_headline_output); emoji conta como 1
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿]"
                   "(?:[\U0001F3FB-\U0001F3FF]|️|‍[\U0001F000-\U0001FAFF☀-➿])*")
ESTADOS = ("pendente", "aprovada", "recusada")

ESQUEMA_CHAMADAS = """
CREATE TABLE IF NOT EXISTS chamadas (
    id INTEGER PRIMARY KEY,
    chave TEXT NOT NULL,              -- identidade do produto na normalizadora
    produto TEXT NOT NULL,            -- nome do produto como a normalizadora chama
    posts INTEGER DEFAULT 0,          -- vezes que passou pela normalizadora em 45 dias (ordem da fila)
    contexto TEXT DEFAULT '',         -- json: o que já existe para o produto (prontas, do Breno, da IA, notas)
    chamada TEXT NOT NULL,
    tipo TEXT DEFAULT '',             -- notas | nome | inspiração
    base TEXT DEFAULT '',             -- de onde veio o fato usado
    estado TEXT DEFAULT 'pendente',   -- pendente | aprovada | recusada
    criada_ts INTEGER NOT NULL,
    decidida_ts INTEGER,
    decidida_por TEXT DEFAULT '',
    gravada_ts INTEGER,               -- quando entrou na lista da normalizadora
    usos INTEGER DEFAULT 0,           -- vezes que saiu num post e o Breno manteve
    UNIQUE (chave, chamada)
);
"""


def tamanho_visual(texto: str) -> int:
    return len(EMOJI.sub("\u0001", texto or ""))


def limpar(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").replace("*", "").replace("_", " ")).strip().upper()


def importar(con: sqlite3.Connection, itens: list[dict], agora: float | None = None) -> int:
    """Sugestões novas. Cada item: {chave, produto, posts, contexto: {...}, chamadas: [{texto, tipo, base}]}."""
    agora = int(agora or time.time())
    novas = 0
    for item in itens:
        contexto = json.dumps(item.get("contexto") or {}, ensure_ascii=False)
        for c in item.get("chamadas") or []:
            texto = limpar(c.get("texto"))
            if not texto or tamanho_visual(texto) > MAX_VISUAL:
                continue
            cur = con.execute("INSERT OR IGNORE INTO chamadas (chave, produto, posts, contexto, chamada, tipo, base, "
                              "criada_ts) VALUES (?,?,?,?,?,?,?,?)",
                              (item["chave"], item["produto"], int(item.get("posts") or 0), contexto, texto,
                               c.get("tipo") or "", c.get("base") or "", agora))
            novas += cur.rowcount
    con.commit()
    return novas


def fila(con: sqlite3.Connection) -> dict:
    """Os produtos com sugestão para ver (os mais frequentes primeiro) e a contagem geral."""
    resumo = {e: 0 for e in ESTADOS}
    produtos: dict[str, dict] = {}
    for r in con.execute("SELECT id, chave, produto, posts, contexto, chamada, tipo, base, estado, gravada_ts, "
                         "COALESCE(usos, 0), decidida_por FROM chamadas ORDER BY posts DESC, produto, id"):
        ident, chave, produto, posts, contexto, chamada, tipo, base, estado, gravada, usos, por = r
        resumo[estado] = resumo.get(estado, 0) + 1
        p = produtos.setdefault(chave, {"chave": chave, "produto": produto, "posts": posts, "pendentes": 0,
                                        "contexto": json.loads(contexto or "{}"), "chamadas": []})
        p["pendentes"] += estado == "pendente"
        p["chamadas"].append({"id": ident, "chamada": chamada, "tipo": tipo, "base": base, "estado": estado,
                              "gravada": bool(gravada), "usos": usos, "por": por or ""})
    lista = sorted(produtos.values(), key=lambda p: (p["pendentes"] == 0, -p["posts"], p["produto"]))
    return {"resumo": resumo, "produtos": lista, "limite": MAX_VISUAL}


def decidir(con: sqlite3.Connection, ident: int, estado: str, texto: str | None = None, por: str = "",
            agora: float | None = None, preferencias: Path = PREFERENCIAS) -> dict:
    """Aprova, recusa ou volta para pendente; `texto` troca a chamada antes (edição do Breno)."""
    agora = int(agora or time.time())
    if estado not in ESTADOS:
        return {"erro": "estado inválido"}
    atual = con.execute("SELECT chave, chamada, estado, gravada_ts FROM chamadas WHERE id = ?", (ident,)).fetchone()
    if not atual:
        return {"erro": "chamada não encontrada"}
    chave, chamada, estado_atual, gravada = atual
    nova = limpar(texto) if texto is not None else chamada
    if not nova:
        return {"erro": "a chamada ficou vazia"}
    if tamanho_visual(nova) > MAX_VISUAL:
        return {"erro": f"passou de {MAX_VISUAL} caracteres ({tamanho_visual(nova)})"}
    if nova != chamada and con.execute("SELECT 1 FROM chamadas WHERE chave = ? AND chamada = ? AND id != ?",
                                       (chave, nova, ident)).fetchone():
        return {"erro": "já existe essa chamada para o produto"}
    aviso = ""
    if gravada and (nova != chamada or estado != "aprovada"):
        aviso = _regravar(con, preferencias, tirar=(chave, chamada)) or ""   # sai da normalizadora como estava
        con.execute("UPDATE chamadas SET gravada_ts = NULL WHERE id = ?", (ident,))
    con.execute("UPDATE chamadas SET chamada = ?, estado = ?, decidida_ts = ?, decidida_por = ? WHERE id = ?",
                (nova, estado, agora, por, ident))
    con.commit()
    if estado == "aprovada":
        aviso = _regravar(con, preferencias, agora=agora) or aviso
    gravada = bool(con.execute("SELECT gravada_ts FROM chamadas WHERE id = ?", (ident,)).fetchone()[0])
    return {"ok": True, "chamada": nova, "estado": estado, "gravada": gravada, **({"aviso": aviso} if aviso else {})}


def preencher(con: sqlite3.Connection, por: str, agora: float | None = None,
              preferencias: Path = PREFERENCIAS) -> dict:
    """Tudo que está pendente passa a valer na normalizadora, sem esperar aprovação uma a uma.

    10/10: foi o que o Breno pediu ("era pra você preencher repertórios de headlines"). Quem tira uma chamada
    ruim é a própria edição dele (`apos_edicao`), ou o ✕ na tela.
    """
    agora = int(agora or time.time())
    n = con.execute("UPDATE chamadas SET estado = 'aprovada', decidida_ts = ?, decidida_por = ? "
                    "WHERE estado = 'pendente'", (agora, por)).rowcount
    con.commit()
    aviso = _regravar(con, preferencias, agora=agora)
    return {"preenchidas": n, **({"aviso": aviso} if aviso else {})}


def titulo_do_post(copia: str) -> str:
    """A chamada de um post: a primeira linha, sem os asteriscos."""
    for linha in (copia or "").splitlines():
        if linha.strip():
            return limpar(linha)
    return ""


def _sem_enfeite(texto: str) -> str:
    """Para comparar chamadas: sem emoji, acento nem pontuação (trocar só o emoji não é trocar a chamada)."""
    t = unicodedata.normalize("NFD", EMOJI.sub("", texto or "").upper())
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", "".join(c for c in t if not unicodedata.combining(c))).split())


# Palavras que não distinguem o produto. "BODY", "SPLASH" e "SPRAY" NÃO estão aqui: Body Spray e EDP são versões
# diferentes (31/10, revisão). O gênero é conferido à parte (_genero).
NOME_GENERICO = {"PERFUME", "PERFUMES", "MASCULINO", "FEMININO", "UNISSEX", "TOILETTE", "PARFUM", "COLONIA",
                 "DESODORANTE", "ARABE", "ORIGINAL", "HOMEM", "MULHER", "PARA", "TRADICIONAL"}


_MARCAS: set[str] | None = None


def _marcas() -> set[str]:
    """Palavras das marcas do config: a marca sozinha não diz que dois nomes são o mesmo produto (Yara x Asad)."""
    global _MARCAS
    if _MARCAS is None:
        try:
            from . import carregar_config
            _MARCAS = {p for m in carregar_config().get("marcas") or [] for p in _sem_enfeite(m).split()}
        except Exception:  # noqa: BLE001 - sem o config, só não tira a marca
            _MARCAS = set()
    return _MARCAS


def _palavras_do_nome(nome: str) -> set[str]:
    """Palavras que distinguem o produto (sem volume, número, marca, 'perfume', 'masculino', 'EDP'...)."""
    return {p for p in _sem_enfeite(nome or "").split()
            if len(p) >= 4 and not any(c.isdigit() for c in p) and p not in NOME_GENERICO and p not in _marcas()}


def _genero(nome: str) -> str:
    palavras = set(_sem_enfeite(nome or "").split())
    m, f = bool(palavras & {"MASCULINO", "HOMEM"}), bool(palavras & {"FEMININO", "MULHER"})
    return "m" if m and not f else "f" if f and not m else ""


def _mesmo_produto(nome_post: str, nome_linha: str) -> bool:
    """Mesmo produto pelo nome: exatamente as mesmas palavras que distinguem (a marca não conta) e sem gênero oposto.

    31/10 (revisão): "metade das palavras em comum" juntava versões (Fakhar Gold x Fakhar Black têm FAKHAR em comum;
    Yara x Yara Elixir; Body Spray x EDP), e uma troca do Breno numa tiraria a chamada da outra. Preferimos deixar de
    pegar uma variação de nome (a próxima troca naquela linha pega pelo nome exato) a recusar a chamada de outra versão."""
    a, b = _palavras_do_nome(nome_post), _palavras_do_nome(nome_linha)
    generos = {_genero(nome_post), _genero(nome_linha)} - {""}
    return bool(a) and a == b and len(generos) <= 1


def apos_edicao(con: sqlite3.Connection, produto: str, copia_normalizada: str, copia_final: str,
                agora: float | None = None, preferencias: Path = PREFERENCIAS) -> str | None:
    """Para cada post que o Breno fechou na Edição: se ele saiu com uma chamada preenchida aqui e o Breno
    manteve, conta um uso; se ele trocou, a chamada sai do repertório. Devolve "mantida", "trocada" ou None."""
    antes = _sem_enfeite(titulo_do_post(copia_normalizada))
    if not antes:
        return None
    iguais = [r for r in con.execute("SELECT id, produto, chamada, chave FROM chamadas WHERE estado = 'aprovada' "
                                     "AND gravada_ts IS NOT NULL") if _sem_enfeite(r[2]) == antes]
    if len(iguais) > 1:
        # A mesma chamada em mais de uma linha: ou são produtos diferentes (vale só o deste post), ou é o MESMO
        # produto guardado com dois nomes ("chave" e "chave~2": o nome do grupo e o do anúncio do ML), e aí
        # o que vale para uma vale para todas.
        deste = [r for r in iguais if r[1] == produto]
        mesma_chave = iguais if len({r[3].split("~")[0] for r in iguais}) == 1 else []
        # 31/10 (revisão): o nome do post quase nunca é igual ao de uma linha ("Lattafa Fakhar Gold Extrait EDP" x
        # "Fakhar Gold Extrait EDP 100ml" e "Lattafa Fakhar Gold 100ml"), e a troca do Breno não virava recusa: das 6
        # trocas de 17 a 30/10, 5 a 6 se perderam, e a "DOCE, FLORAL E SOLAR" voltou no Fakhar Gold em 30/10. Vale
        # então, quando ele TROCOU, sai de todas as linhas do mesmo produto pelas palavras do nome (ver _mesmo_produto),
        # senão a chamada volta pela outra. O "manteve" segue contando uso só como antes (nome exato ou mesma chave).
        if _sem_enfeite(titulo_do_post(copia_final)) != antes:
            iguais = [r for r in iguais if r in deste or r in mesma_chave or _mesmo_produto(produto, r[1])]
        else:
            iguais = deste or mesma_chave
    if not iguais:
        return None
    if _sem_enfeite(titulo_do_post(copia_final)) == antes:
        con.executemany("UPDATE chamadas SET usos = COALESCE(usos, 0) + 1 WHERE id = ?", [(r[0],) for r in iguais])
        con.commit()
        return "mantida"
    for r in iguais:
        decidir(con, r[0], "recusada", por="trocada por você na Edição", agora=agora, preferencias=preferencias)
    return "trocada"


def _regravar(con: sqlite3.Connection, caminho: Path, agora: int | None = None,
              tirar: tuple[str, str] | None = None) -> str | None:
    """Põe na lista da normalizadora as aprovadas que ainda não estão lá (e tira `tirar`). Devolve o erro, se houver.

    Lê o arquivo na hora, mexe só nas entradas "curadora:..." e troca o arquivo de uma vez (a normalizadora nunca
    lê pela metade). O resto do arquivo fica como está.
    """
    agora = int(agora or time.time())
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"não consegui ler a lista da normalizadora ({type(exc).__name__}); a decisão ficou guardada aqui"
    lista = dados.setdefault("manual_exact_sku_repertoire", {})
    mudou = False
    if tirar:
        entrada = lista.get(f"curadora:{tirar[0]}")
        if entrada and f"*{tirar[1]}*" in (entrada.get("headlines") or []):
            entrada["headlines"].remove(f"*{tirar[1]}*")
            if not entrada["headlines"]:
                del lista[f"curadora:{tirar[0]}"]
            mudou = True
    novas = con.execute("SELECT id, chave, produto, chamada FROM chamadas WHERE estado = 'aprovada' "
                        "AND gravada_ts IS NULL ORDER BY id").fetchall()
    for _, chave, produto, chamada in novas:
        entrada = lista.setdefault(f"curadora:{chave}", {"product": produto, "headlines": [],
                                                         "source": "curadora_painel_aprovada_pelo_breno",
                                                         "human_approved": True})
        if f"*{chamada}*" not in entrada["headlines"]:
            entrada["headlines"].append(f"*{chamada}*")
            mudou = True
    if mudou:
        try:
            copia = caminho.with_name(caminho.name + ".bak-curadora-" + time.strftime("%Y%m%d", time.localtime(agora)))
            if not copia.exists():
                shutil.copy2(caminho, copia)
            tmp = caminho.with_name(caminho.name + ".curadora.tmp")
            tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
            shutil.copymode(caminho, tmp)
            os.replace(tmp, caminho)
        except OSError as exc:
            return f"não consegui gravar na lista da normalizadora ({type(exc).__name__}); a decisão ficou guardada aqui"
    if novas:
        con.executemany("UPDATE chamadas SET gravada_ts = ? WHERE id = ?", [(agora, r[0]) for r in novas])
        con.commit()
    return None
