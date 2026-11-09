"""Equipe do painel: convites e acessos (rodar na VPS, na pasta ~/curadora).

  python3 usuarios.py convidar breno --admin     gera o link de convite (a pessoa cria a própria senha)
  python3 usuarios.py convidar colega
  python3 usuarios.py listar
  python3 usuarios.py desativar colega            tira o acesso e encerra as sessões

O link usa o endereço público do config.json ("endereco_publico").
"""
from __future__ import annotations

import sys

from curadora import carregar_config
from curadora.acesso import Acesso
from curadora.banco import conectar


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("convidar", "listar", "desativar"):
        print(__doc__)
        return 1
    acesso = Acesso(conectar())
    if args[0] == "listar":
        for u in acesso.listar():
            print(f"{u['nome']:20} {'admin' if u['admin'] else 'equipe':7} {'ativo' if u['ativo'] else 'desativado'}")
        return 0
    if len(args) < 2:
        print(__doc__)
        return 1
    if args[0] == "desativar":
        acesso.desativar(args[1])
        print(f"{args[1]}: acesso removido.")
        return 0
    token = acesso.convidar(args[1], admin="--admin" in args)
    base = carregar_config().get("endereco_publico", "").rstrip("/") or "http://127.0.0.1:8765"
    print(f"Convite para {args[1]} (vale 48h, uma vez só):\n  {base}/entrar?convite={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
