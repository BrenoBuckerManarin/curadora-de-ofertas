"""17/10: as linhas de perfume que existem nas DUAS versões (masculina e feminina), pelo que os grupos postaram em 60
dias (curadora: avaliações "ao vivo", radar e vigia). Grava ~/.hermes/perfume_image_bank/linhas_dois_generos.json, que
a normalizadora (patch 16) lê para preferir foto com prova de gênero SÓ onde há risco de trocar as versões.

Família = as palavras do nome, sem acento, em qualquer ordem, tirando marca (lista de marcas da curadora), gênero,
palavras genéricas e tamanho: "The Kingdom Lattafa Feminino 100ml" e "Perfume Árabe - Lattafa The Kingdom Man" são a
família "kingdom". As palavras tiradas vão no próprio arquivo ("fora"): a normalizadora monta a família do mesmo jeito.
Uso (em ~/curadora): python3 dados/linhas_dois_generos.py [--gravar]"""
import json, os, re, sqlite3, sys, time, unicodedata
from collections import defaultdict

sys.path.insert(0, ".")
from curadora import carregar_config  # noqa: E402

DESTINO = os.path.expanduser("~/.hermes/perfume_image_bank/linhas_dois_generos.json")
GENERO = {"man", "men", "woman", "women", "masculino", "masculina", "feminino", "feminina", "homme", "femme", "him",
          "her", "pour", "for", "masc", "fem", "homem", "mulher", "masculinos", "femininos", "unissex", "unisex"}
GENERICAS = {"perfume", "perfumes", "arabe", "arabes", "eau", "de", "du", "da", "do", "das", "dos", "des", "la", "le",
             "the", "el", "edp", "edt", "edc", "parfum", "toilette", "cologne", "colonia", "deo", "desodorante",
             "original", "originais", "spray", "refil", "refilavel", "ml", "importado", "lacrado", "novo", "nova",
             "oficial", "vaporizador", "natural", "fragrancia", "frasco", "e", "com", "em", "unidade", "unidades",
             "tradicional", "classico", "classica", "classic"}


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "").lower()) if not unicodedata.combining(c))


cfg = carregar_config()
MARCAS = {t for m in cfg.get("marcas") or [] for t in re.findall(r"[a-z0-9]+", sem_acento(m))}
FORA = GENERO | GENERICAS | MARCAS


def familia(nome: str) -> str:
    toks = {t for t in re.findall(r"[a-z0-9]+", sem_acento(nome)) if t not in FORA and len(t) > 1
            and not re.fullmatch(r"\d+(?:ml)?", t)}
    return "-".join(sorted(toks))


def genero(nome: str) -> str:
    toks = set(re.findall(r"[a-z]+", sem_acento(nome)))
    m = bool(toks & {"masculino", "masculina", "man", "men", "homme", "him", "homem"})
    f = bool(toks & {"feminino", "feminina", "woman", "women", "femme", "her", "mulher"})
    return "masculino" if m and not f else "feminino" if f and not m else ""


if __name__ == "__main__":
    con = sqlite3.connect("file:dados/curadora.db?mode=ro", uri=True, timeout=30)
    generos, exemplos = defaultdict(set), defaultdict(dict)
    for (produto,) in con.execute("SELECT DISTINCT json_extract(dados, '$.produto') FROM avaliacoes WHERE ts >= ? "
                                  "AND origem IN ('ao vivo', 'radar do ML', 'vigia do ML')", (time.time() - 60 * 86400,)):
        g = genero(produto or "")
        f = familia(produto or "")
        if g and f:
            generos[f].add(g)
            exemplos[f].setdefault(g, produto)
    duas = sorted(f for f, gs in generos.items() if gs == {"masculino", "feminino"})
    print(f"famílias com gênero em 60 dias: {len(generos)} · nas duas versões: {len(duas)} · palavras fora: {len(FORA)}")
    for f in duas:
        print(f"   {f}: {exemplos[f]['masculino'][:38]!r} / {exemplos[f]['feminino'][:38]!r}")
    for teste in ("The Kingdom Lattafa Feminino 100ml", "Armaf Club De Nuit Intense EDP Feminino 105ml",
                  "Club De Nuit Intense Da Armaf EDT Masculino 105ml", "Natura Kaiak Ultra Colônia Feminino 100ml",
                  "Maison Alhambra Salvo EDP Masculino 100ml", "Sabah Al Ward Sugar Feminino 100ml"):
        print(f"   teste: {teste[:46]:46} -> {familia(teste)} · duas versões: {familia(teste) in duas}")
    if "--gravar" in sys.argv:
        with open(DESTINO + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"gerado_em": time.strftime("%Y-%m-%d %H:%M"), "fonte": "curadora (grupos, 60 dias)",
                       "fora": sorted(FORA), "familias": duas}, fh, ensure_ascii=False, indent=1)
        os.replace(DESTINO + ".tmp", DESTINO)
        print("gravado:", DESTINO, len(duas))
