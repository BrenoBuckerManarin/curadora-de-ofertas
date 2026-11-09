#!/bin/sh
# 14/10: depois do turno de segunda, quarta e quinta, desliga a normalizadora que o pipeline ligou (terça, sexta e fim
# de semana quem desliga são as janelas da coletora). Se o pipeline ainda estiver ligado, não mexe.
# Na VPS fica em ~/curadora/dados/normalizadora_desliga.sh (é o caminho que o serviço usa).
cd /home/ubuntu/curadora || exit 0
if ./pipelinectl status 2>/dev/null | grep -q "^pipeline: LIGADO"; then
  echo "pipeline ainda ligado: a normalizadora fica"
  exit 0
fi
exec /home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh off
