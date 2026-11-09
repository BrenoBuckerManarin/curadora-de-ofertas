"""Curadora de promoções de perfume: leitura, histórico de preço e score."""
from __future__ import annotations

import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO_CONFIG = RAIZ / "config.json"


def carregar_config(caminho: Path | None = None) -> dict:
    """Lê o config. A chave "_arquivo" diz de onde veio, para recarregar quando ele mudar."""
    caminho = Path(caminho or ARQUIVO_CONFIG)
    cfg = json.loads(caminho.read_text(encoding="utf-8"))
    cfg["_arquivo"] = str(caminho)
    return cfg


def salvar_config(cfg: dict, caminho: Path | None = None) -> None:
    caminho = Path(caminho or cfg.get("_arquivo") or ARQUIVO_CONFIG)
    limpo = {k: v for k, v in cfg.items() if not k.startswith("_")}
    temporario = caminho.with_name(caminho.name + ".novo")
    temporario.write_text(json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(caminho)
