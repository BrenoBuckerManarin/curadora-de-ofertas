"""Dá à curadora acesso PRÓPRIO à API do Mercado Livre (uma vez só; depois renova sozinho).

Passo a passo:
  1. Entre em https://developers.mercadolivre.com.br/devcenter com a conta do ML e crie um aplicativo
     ("Criar aplicação"). Em "URI de redirect" coloque, por exemplo: https://www.google.com
  2. Crie o arquivo ~/.curadora/ml-app.env com:
        ML_CLIENT_ID=<App ID do aplicativo>
        ML_CLIENT_SECRET=<Secret Key do aplicativo>
        ML_REDIRECT_URI=https://www.google.com
  3. Rode:  python configurar_ml.py
     Abra o link que aparecer, autorize, e cole aqui o endereço para onde o ML te mandou
     (ele termina com ?code=TG-...).

As credenciais ficam só em ~/.curadora/ (fora da pasta do projeto e do git).
"""
from __future__ import annotations

import sys
import urllib.parse

from curadora.ml_api import ClienteML, PASTA_PADRAO


def main() -> int:
    cliente = ClienteML()
    app = cliente.app()
    if not (app.get("ML_CLIENT_ID") and app.get("ML_CLIENT_SECRET") and app.get("ML_REDIRECT_URI")):
        print(f"Falta o arquivo {PASTA_PADRAO / 'ml-app.env'} com ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI.")
        print(__doc__)
        return 1
    if "--testar" not in sys.argv:
        print("1) Abra este link, entre na conta do ML e autorize:\n")
        print("   " + cliente.url_autorizacao() + "\n")
        resposta = input("2) Cole aqui o endereço para onde o ML te mandou (ou só o código TG-...): ").strip()
        codigo = urllib.parse.parse_qs(urllib.parse.urlparse(resposta).query).get("code", [resposta])[0]
        cliente.trocar_codigo(codigo)
        print("\nAcesso salvo. Ele se renova sozinho.")
    produtos = cliente.buscar_produtos("Lattafa Asad 100ml", limite=3)
    print("Teste: busca 'Lattafa Asad 100ml' ->", ", ".join(p["nome"] for p in produtos) or "(nada)")
    if produtos:
        ofertas = cliente.ofertas(produtos[0]["id"])
        if ofertas:
            print(f"Teste: {len(ofertas)} ofertas; a mais barata R$ {ofertas[0].preco:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
