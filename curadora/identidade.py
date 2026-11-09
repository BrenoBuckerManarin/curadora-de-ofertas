"""Identidade do produto: transforma nomes diferentes do mesmo perfume numa chave única.

Duas etapas:
1. `Identificador.analisar(nome)` lê o nome: palavras da linha (sem marca, sem palavras
   genéricas, sem ordem), volume, gênero, kit e body splash.
2. `Catalogo` aprende com o histórico o que o nome sozinho não resolve:
   - gênero só separa produtos quando muda o preço (CH masculino x feminino separa;
     Natura Essencial Oud não separa, porque custa o mesmo);
   - volume não informado assume o volume dominante da linha, quando houver um claro.
"""
from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

GENERICAS = {
    "perfume", "perfumes", "arabe", "arabes", "original", "originais", "importado", "lacrado",
    "unissex", "unisex", "de", "da", "do", "das", "dos", "la", "le", "the", "al", "el", "for", "pour",
    "e", "and", "com", "edp", "edt", "edc", "eau", "toilette", "cologne", "spray", "vapo", "vaporizador",
    "natural", "fragrancia", "fragrance", "frasco", "novo", "nova", "lancamento", "oficial", "ml",
    "oferta", "promo", "promocao", "barato", "imperdivel", "selo", "nota", "fiscal", "nf",
    "colonia", "tradicional", "classico", "classica", "classic",
}
# 21/10 (Breno: "tá viciada nesse yara"): "Lipx" é a loja (distribuidora da Lattafa) e vem no título do anúncio do ML.
# Entrava na identidade: "elixir-lipx-yara" não era o "elixir-yara" que o pipeline tinha mandado às 18:38, e o mesmo
# Yara Elixir foi de novo às 19:15. Em 14 dias, 9 produtos e 2214 avaliações estavam com a loja no meio do nome.
LOJAS_NO_NOME = {"lipx", "erian"}   # 25/10: "Perfume Hawas Kobra Rasasi EDP 100ml Arabe Erian" virava outro Hawas Kobra
# 25/10 (Breno: "a curadora achava que cada Yara era diferente"): palavras soltas que entravam na identidade e partiam o
# mesmo perfume em duas chaves: "Kaiak K EDP para masculino" (kaiak-para), "Bareeq Al Dhahab ... Parf" (bareeq-dhahab-
# parf), "Kaiak Ultra ... Relançamento", "Island Khadlaj Luxo" (extrato-island-luxo). Notas olfativas ficam: no body
# splash elas são o nome da versão (Blunn Cítrico x Blunn Amadeirado).
SOLTAS = {"para", "parf", "relancamento", "luxo"}
# 28/10 (Breno: "esse preço nem deveria ser excelente"): "Perfume Árabe Asad Lattafa 100ml Edp Amadeirado Masculino"
# virava "amadeirado-asad", outro produto com histórico próprio (mediana R$250 contra R$199 do Asad): R$206 saiu
# Excelente 78, e a trava de 8h não viu o Asad de R$158,87 das 18:01. Em perfume, a descrição do cheiro sai da
# identidade; em body splash e kit fica, porque lá ela é o nome da versão (Blunn Cítrico x Blunn Amadeirado).
CHEIROS = {"amadeirado", "amadeirada", "amadeirados", "amadeiradas", "citrico", "citrica", "especiado", "especiada",
           "adocicado", "adocicada", "frutado", "frutada", "ambarado", "ambarada", "oriental", "fresco"}
# "male" fica de fora de propósito: "Le Male" e "Ultra Male" são nomes de perfume.
MASCULINO = {"masculino", "masculina", "masc", "man", "men", "homme", "him", "homem", "homens"}
FEMININO = {"feminino", "feminina", "fem", "woman", "women", "femme", "her", "mulher"}
# Palavras que fazem de um perfume OUTRO produto da mesma linha (flanker).
VARIANTES = {
    "intense", "intenso", "elixir", "parfum", "extrait", "absolu", "absolute", "sport", "black", "gold",
    "silver", "blue", "bleu", "red", "rouge", "white", "blanc", "noir", "night", "oud", "sugar", "bourbon",
    "platin", "tobacco", "leather", "aqua", "fresh", "iconic", "sillage", "milestone", "untold", "imperiale",
    "royal", "private", "prive", "privee", "limited", "edition", "summer", "winter", "candy", "moi", "tous",
    "body", "splash", "kit", "mini", "miniatura", "corporal",
}
# Marcas com mais de um nome: quando a marca entra na identidade, usa uma forma só.
MARCA_CANONICA = {
    "yves saint laurent": "ysl", "giorgio armani": "armani", "paco rabanne": "rabanne",
    "o boticario": "boticario", "antonio banderas": "banderas", "dolce gabbana": "dolce",
}
# Erros de digitação dos grupos, levantados no histórico real em 04/10 (cada um dividia o mesmo produto
# em dois e enfraquecia o histórico de preço). Só entram erros claros: palavra parecida que é outro
# produto (Botica x Boticário, Extreme x Extremo, Ameer x Ameeri) NÃO entra.
GRAFIAS = {
    # marcas
    "oboticario": "boticario", "obotocario": "boticario", "lataffa": "lattafa", "lattada": "lattafa",
    "lattafao": "lattafa", "lattfa": "lattafa", "alhmabra": "alhambra", "alambra": "alhambra",
    "ahlambra": "alhambra", "rabane": "rabanne", "pacco": "paco", "parys": "paris", "elysee": "elysees",
    "mawval": "mawwal", "rayhan": "rayhaan", "riffs": "riiffs", "riifs": "riiffs", "charnel": "chanel",
    "gaultire": "gaultier", "gautier": "gaultier", "anotnio": "antonio", "banders": "banderas",
    "evenue": "avenue", "vnatura": "natura",
    # produtos
    "khamah": "khamrah", "dhabab": "dhahab", "choc": "choco", "cost": "coast", "impreiale": "imperiale",
    "emocion": "emocione", "shyaaka": "shiyaaka", "eletric": "electric", "ousadai": "ousadia",
    "arctic": "artic", "effervescente": "effervescent", "intesa": "intensa", "magnifical": "magnifica",
    "toscada": "toscana", "intenso": "intense", "clube": "club", "malbecao": "malbec", "milion": "million",
    "diamods": "diamonds", "blac": "black", "exremo": "extremo", "kordova": "cordova", "mahir": "maahir",
    "tuma": "turma", "shaghaf": "shagaf", "eddition": "edition", "collector": "collectors",
    "pharoh": "pharaoh", "qimma": "qimmah", "emporino": "emporio", "botic": "botica", "presete": "presente",
    "pefume": "perfume", "pafum": "parfum", "fragrancias": "fragrancia", "fragance": "fragrance",
    "miyake": "", "ssey": "issey", "lqalb": "qalb",  # "L'Eau d'Issey" com ou sem "Miyake" é o mesmo perfume
}
# Marca citada sem o resto do nome ("Salvo Alhambra" = Maison Alhambra Salvo).
MARCAS_EXTRAS = ["alhambra"]
# 18/10 (Breno: "ele confundiu com o Asad, mas esse é diferente, outro perfume"): "Perfume Arábe Asad Bourbon 100ml -
# Marca Isabelle La Belle" é a versão nacional inspirada no Asad Bourbon, a R$79, e o grupo escreveu só "Asad Bourbon":
# saiu "62% abaixo" do da Lattafa (R$209). Marca de perfume inspirado no nome, ou "Marca X" de outra marca no título
# do anúncio, é outro produto. Em 60 dias, todo título do ML com "Marca X" era da Isabelle La Belle.
MARCAS_INSPIRADAS = {"isabelle": "isabelle la belle"}
MARCA_DECLARADA = re.compile(r"\bmarca\s+([a-z][a-z0-9]*(?:\s+[a-z][a-z0-9]*){0,2})")
NAO_E_MARCA = {"original", "originais", "registrada", "propria", "famosa", "lider", "de", "da", "do", "e", "sua"}
VOLUME = re.compile(r"\b(\d{2,3})\s*ml\b")
KIT =re.compile(r"\b(kit|combo|conjunto)\b")
# 31/10 (auditoria da Astra): a concentração sai do nome (edp/edt/edc são GENERICAS) e o conflito não via "Sauvage EDT"
# no link de um "Sauvage EDP", nem "The Icon EDP" no de um "The Icon EDT". A identidade continua sem ela (a chave e o
# histórico não mudam); só o conflito compara.
# Medido em 14 dias (18 a 31/10, 237 mil avaliações) antes de subir, ficaram de fora duas palavras que no Brasil não
# dizem concentração:
# - "extrato": "Extrato de Perfume Árabe ... Khadlaj Island" é propaganda. Contra o "Island Dreams EDP" dos grupos,
#   viraria conflito em 750 avaliações Boas, e o Breno vinha postando essas ofertas.
# - "colônia"/"edc": a mesma colônia sai como "Colônia Eau De Toilette" e "Desodorante Colônia", e os grupos chamam
#   Deo Parfum de colônia.
CONCENTRACOES = [("extrait", re.compile(r"\bextrait\b")), ("parfum", re.compile(r"\bparfum\b")),
                 ("edp", re.compile(r"\bedp\b")), ("edt", re.compile(r"\bedt\b"))]
# Palavras de VARIANTES que, faltando só no anúncio, não provam outro produto: "Limited Edition" o anúncio costuma
# omitir; "Parfum" e "Extrait" já são conferidos como concentração acima ("The Icon Parfum EDP" do vigia = "The Icon
# The Perfume ... Eau de Parfum" do anúncio, 48 avaliações de 31/10 que virariam conflito falso; "Club de Nuit Intense
# Man Extrait" do grupo = "Extrato de perfume ... Club de Nuit Intense Masculino" do anúncio).
SO_ENFEITE = {"limited", "edition", "parfum", "extrait"}
BODY = re.compile(r"\b(body\s*splash|body\s*spray|splash)\b")


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


@dataclass(frozen=True)
class NomeAnalisado:
    base: str              # palavras da linha, ordenadas ("club-intense-nuit"); "kit:" na frente se for kit
    tokens: frozenset
    volume: str            # "100" ou ""
    genero: str            # "m", "f" ou ""
    kit: bool
    body_splash: bool
    marca: str
    marca_declarada: str = ""   # marca que o nome afirma ("Marca Isabelle La Belle") ou de perfume inspirado
    concentracoes: frozenset = frozenset()   # {"edp"}, {"edt"}, {"parfum", "edp"}...; vazio = o nome não diz


@dataclass
class Identidade:
    chave: str
    analise: NomeAnalisado
    notas: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> frozenset:
        return self.analise.tokens

    @property
    def kit(self) -> bool:
        return self.analise.kit

    @property
    def body_splash(self) -> bool:
        return self.analise.body_splash

    @property
    def marca(self) -> str:
        return self.analise.marca

    @property
    def volume(self) -> str:
        return self.chave.split("|")[1] if "|" in self.chave else ""


class Identificador:
    def __init__(self, marcas: list[str], sinonimos: dict[str, str] | None = None):
        self.marcas = sorted({sem_acento(m.lower()) for m in list(marcas) + MARCAS_EXTRAS}, key=len, reverse=True)
        self.sinonimos = {sem_acento(k.lower()): sem_acento(v.lower()) for k, v in (sinonimos or {}).items()}
        self._cache: dict[str, NomeAnalisado] = {}

    @staticmethod
    def _normalizar(nome: str) -> str:
        s = sem_acento(nome.lower())
        s = re.sub(r"eau\s+de\s+parfum", " edp ", s)
        s = re.sub(r"eau\s+de\s+toilette", " edt ", s)
        # Nomes de concentração da Natura/Boticário: "Deo Parfum" = EDP, "Desodorante Colônia" = colônia.
        s = re.sub(r"\bdeo\s*-?\s*parfum\b", " edp ", s)
        s = re.sub(r"\beau\s+de\s+parf\b", " edp ", s)       # "Eau De Parf": título cortado no limite do ML
        s = re.sub(r"\b(desodorante|deo)\s+colonia\b", " edc ", s)
        s = re.sub(r"(\d{2,3})[.,]\d+\s*ml\b", r"\1ml", s)
        s = re.sub(r"(\d{2,3})\s*m\s*l\b", r"\1ml", s)
        s = s.replace("’", "'").replace("´", "'").replace("`", "'")
        # "L'Eau D'Issey" = "Eau Issey"; "L'qalb" = "Qalb". "L'Homme" fica: é nome de perfume, não gênero.
        s = re.sub(r"\b[dl]'\s*(?!homme)(?=[a-z])", "", s)
        s = s.replace("&", " ").replace("'", "")
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"[a-z0-9]+", lambda m: GRAFIAS.get(m.group(0), m.group(0)), s)

    def analisar(self, nome: str) -> NomeAnalisado:
        if nome in self._cache:
            return self._cache[nome]
        s = self._normalizar(nome)
        declarada = self._marca_declarada(s)
        concentracoes = frozenset(c for c, rx in CONCENTRACOES if rx.search(s))
        vol_m = VOLUME.search(s)
        volume = vol_m.group(1) if vol_m else ""
        kit = bool(KIT.search(s))
        body = bool(BODY.search(s))
        marca = ""
        for m in self.marcas:
            if re.search(r"\b" + re.escape(m) + r"\b", s):
                marca = marca or m
                s = re.sub(r"\b" + re.escape(m) + r"\b", " ", s)
        s = VOLUME.sub(" ", s)
        tokens: set[str] = set()
        generos: set[str] = set()
        palavras_genero: set[str] = set()
        letras: set[str] = set()
        for t in s.split():
            t = self.sinonimos.get(t, t)
            if t in MASCULINO or t in FEMININO:
                generos.add("m" if t in MASCULINO else "f")
                palavras_genero.add(t)
            elif len(t) == 1 and t.isalpha():
                letras.add(t)  # nome de uma letra só: "Y" da YSL, "K" da Dolce & Gabbana
            elif t in GENERICAS or t in LOJAS_NO_NOME or t in SOLTAS or t.isdigit() or len(t) < 2 or t in {"kit", "combo", "conjunto"}:
                continue
            elif t in CHEIROS and not body and not kit:
                continue
            else:
                tokens.add(t)
        # 25/10: o "K" do Kaiak K EDP é o nome (sem ele, virava o Kaiak tradicional, que custa bem menos).
        # O "K Max" fica como estava (kaiak-max / max).
        if "kaiak" in tokens and "k" in letras and "max" not in tokens:
            tokens.add("k")
        if tokens <= VARIANTES:
            # Sobrou só "intense"/"sport"/nada: a marca, o gênero e letras soltas fazem parte do nome
            # ("Dior Homme Intense" não pode virar o mesmo que "Azzaro Intense"; "Y" da YSL é o nome).
            tokens |= {t for t in palavras_genero if t not in {"masculino", "feminino", "masc", "fem"}}
            tokens |= letras if marca else set()
            if tokens and marca:
                tokens |= set(MARCA_CANONICA.get(marca, marca).split())
        genero = generos.pop() if len(generos) == 1 else ""
        base = "-".join(sorted(tokens))  # vazio = só marca/palavras genéricas: produto não identificado
        analise = NomeAnalisado(("kit:" if kit else "") + base, frozenset(tokens), volume, genero, kit, body, marca,
                                declarada, concentracoes)
        if len(self._cache) > 50000:
            self._cache.clear()
        self._cache[nome] = analise
        return analise

    def mesmo_produto_da_lista(self, ident: Identidade, nome_lista: str) -> bool:
        """Lista de mais vendidos: mesmas palavras de linha, qualquer volume.

        "Lattafa Asad" casa com "Asad 100ml", mas não com "Asad Bourbon".
        """
        alvo = self.analisar(nome_lista)
        return bool(alvo.tokens) and alvo.tokens == ident.tokens and not ident.kit

    def _marca_declarada(self, s: str) -> str:
        """A marca que o nome (já normalizado) afirma: marca de perfume inspirado citada, ou o que vem depois de
        "Marca" ("... - Marca Isabelle La Belle | Fixação"). "" se não afirma nenhuma."""
        for palavra, marca in MARCAS_INSPIRADAS.items():
            if re.search(r"\b" + palavra + r"\b", s):
                return marca
        achado = MARCA_DECLARADA.search(s)
        if not achado or achado.group(1).split()[0] in NAO_E_MARCA:
            return ""
        palavras = achado.group(1)
        return next((m for m in self.marcas if re.match(re.escape(m) + r"\b", palavras)), palavras.split()[0])

    @staticmethod
    def outra_marca(promo: NomeAnalisado, anuncio: NomeAnalisado) -> str:
        """A marca que o anúncio afirma quando ela não é a do produto da promo; "" se é a mesma ou se não afirma."""
        marca = anuncio.marca_declarada
        if not marca or marca in (promo.marca, promo.marca_declarada):
            return ""
        if marca == anuncio.marca and not promo.marca:
            return ""   # "Marca Lattafa" no anúncio de um "Asad" que o grupo escreveu sem marca: é a marca dele
        return marca

    @staticmethod
    def concentracao_diferente(promo: NomeAnalisado, outro: NomeAnalisado) -> str:
        """"EDT x EDP" quando os dois nomes dizem a concentração e elas não se cruzam; "" se compatível ou se um não diz.
        31/10: não é conflito (não bloqueia). Em 14 dias, o texto do post com a concentração errada barraria 3 Boas
        legítimas (Club de Nuit Intense Woman escrito "EDT"; Stronger With You Powerfully escrito "EDT"), contra 1 caso
        de outro produto. O score usa isto para não dar Excelente e avisar."""
        if promo.concentracoes and outro.concentracoes and not (promo.concentracoes & outro.concentracoes):
            return "/".join(sorted(promo.concentracoes)).upper() + " x " + "/".join(sorted(outro.concentracoes)).upper()
        return ""

    @staticmethod
    def conflito(promo: NomeAnalisado, outro: NomeAnalisado) -> str:
        """Motivo se `outro` (ex.: título do ML) for um produto diferente da promo; "" se compatível."""
        if promo.volume and outro.volume and promo.volume != outro.volume:
            return f"volume diferente ({promo.volume}ml x {outro.volume}ml)"
        if promo.genero and outro.genero and promo.genero != outro.genero:
            return "gênero diferente"
        if not promo.tokens:
            return ""
        faltando = promo.tokens - outro.tokens
        if len(faltando) > len(promo.tokens) / 2:
            return "nome do produto não bate (" + ", ".join(sorted(faltando)) + ")"
        # 31/10: a versão vale nos dois sentidos. Antes só pegava a versão a mais no anúncio; "Club de Nuit Intense"
        # no grupo com o link do "Club de Nuit" passava (falta 1 de 3 palavras) e o preço do outro saía Excelente.
        a_mais = ((outro.tokens - promo.tokens) | (faltando - SO_ENFEITE)) & VARIANTES
        if a_mais:
            return "versão diferente (" + ", ".join(sorted(a_mais)) + ")"
        return ""


class Catalogo:
    """Regras aprendidas do histórico. Recalculado pelo importador e salvo no banco."""

    MIN_POR_GENERO = 3
    DIFERENCA_PRECO_GENERO = 0.15
    DOMINANCIA_GENERO = 0.8
    DOMINANCIA_VOLUME = 0.85
    MIN_VOLUME = 5

    def __init__(self, generos_relevantes: dict[str, str] | None = None, volumes: dict[str, str] | None = None):
        # linha|vol -> gênero padrão quando não informado ("m", "f" ou "" = ambíguo)
        self.generos_relevantes = generos_relevantes or {}
        # linha -> volume dominante
        self.volumes = volumes or {}

    @classmethod
    def aprender(cls, linhas: list[tuple[str, str, str, float]]) -> "Catalogo":
        """`linhas`: (base, volume, genero, preco) de cada oferta histórica."""
        precos: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        vols: dict[str, Counter] = defaultdict(Counter)
        for base, volume, genero, preco in linhas:
            if volume:
                vols[base][volume] += 1
                precos[f"{base}|{volume}"][genero or "?"].append(preco)
        generos: dict[str, str] = {}
        for linha, por_genero in precos.items():
            m, f = por_genero.get("m", []), por_genero.get("f", [])
            if len(m) < cls.MIN_POR_GENERO or len(f) < cls.MIN_POR_GENERO:
                continue
            med_m, med_f = statistics.median(m), statistics.median(f)
            if abs(med_m - med_f) / min(med_m, med_f) < cls.DIFERENCA_PRECO_GENERO:
                continue  # mesmo preço: tratar como um produto só
            padrao = "m" if len(m) / (len(m) + len(f)) >= cls.DOMINANCIA_GENERO else \
                "f" if len(f) / (len(m) + len(f)) >= cls.DOMINANCIA_GENERO else ""
            sem_genero = por_genero.get("?", [])
            if not padrao and len(sem_genero) >= cls.MIN_POR_GENERO:
                # Quem não diz o gênero costuma ser a versão com o mesmo preço (Kaiak sem
                # gênero custa o mesmo que o masculino, não o feminino).
                med_s = statistics.median(sem_genero)
                dist_m, dist_f = abs(med_s - med_m) / med_m, abs(med_s - med_f) / med_f
                if min(dist_m, dist_f) <= 0.10 and max(dist_m, dist_f) >= 0.20:
                    padrao = "m" if dist_m < dist_f else "f"
            generos[linha] = padrao
        volumes = {}
        for base, cont in vols.items():
            total = sum(cont.values())
            vol, n = cont.most_common(1)[0]
            if total >= cls.MIN_VOLUME and n / total >= cls.DOMINANCIA_VOLUME:
                volumes[base] = vol
        return cls(generos, volumes)

    def chave(self, a: NomeAnalisado) -> Identidade:
        notas: list[str] = []
        volume = a.volume
        if not volume and a.base in self.volumes:
            volume = self.volumes[a.base]
            notas.append(f"volume não informado: assumido {volume}ml (o mais comum dessa linha)")
        chave = f"{a.base}|{volume or '?'}"
        if chave in self.generos_relevantes:
            genero = a.genero or self.generos_relevantes[chave]
            if not a.genero and genero:
                notas.append(f"gênero não informado: assumido {'masculino' if genero == 'm' else 'feminino'}")
            if not genero:
                notas.append("gênero não informado numa linha em que masculino e feminino têm preços diferentes")
            chave += "|" + (genero or "?")
        return Identidade(chave, a, notas)

    def para_json(self) -> str:
        return json.dumps({"generos": self.generos_relevantes, "volumes": self.volumes}, ensure_ascii=False)

    @classmethod
    def de_json(cls, texto: str) -> "Catalogo":
        d = json.loads(texto)
        return cls(d.get("generos"), d.get("volumes"))
