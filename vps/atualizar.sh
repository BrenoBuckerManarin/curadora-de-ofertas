#!/usr/bin/env bash
# Atualiza o código da curadora na VPS a partir do git local, testa lá e reinicia o serviço.
# O config.json da VPS NÃO é sobrescrito (lá ficam os ajustes da calibração e o grupo de alertas),
# a não ser com --com-config.
#
# Uso (no PC, dentro da pasta curadora):  bash vps/atualizar.sh [--com-config]
set -euo pipefail
VPS="${VPS:-ubuntu@203.0.113.10}"
CHAVE="${CHAVE:-$HOME/.ssh/claude_vps_ed25519}"
EXCLUIR="--exclude=config.json"
[ "${1:-}" = "--com-config" ] && EXCLUIR=""

git archive --format=tar HEAD | ssh -i "$CHAVE" -o BatchMode=yes "$VPS" "
  set -e
  cd ~/curadora
  [ -f config.json ] || EXCLUIR=''
  tar -xf - $EXCLUIR
  python3 -m unittest discover -s tests 2>&1 | tail -1
  systemctl --user restart curadora
  sleep 3
  systemctl --user is-active curadora
"
