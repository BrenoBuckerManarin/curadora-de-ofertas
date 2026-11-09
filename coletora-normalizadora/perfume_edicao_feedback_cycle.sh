#!/usr/bin/env bash
# Fecha o loop de feedback do grupo Edição.
#
# Sem isto, tudo que Breno corrige no Edição (foto trocada, headline reescrita)
# fica parado no WhatsApp até alguém rodar o miner na mão. Em 2026-07-26 havia
# 5 imagens e 214 pares de correção acumulados, nunca aprendidos.
#
# Só LÊ WhatsApp e grava no banco local. Não posta, não envia mensagem.
# Roda 22:10 sáb/dom: 10min depois da janela OFF do perfume, para não competir
# com o coletor sendo pausado às 22:00.
set -uo pipefail
FAILED=0
TS="$(date -Is)"
LOG_DIR=/home/ubuntu/.hermes/perfume_image_bank/logs
mkdir -p "$LOG_DIR"
OUT="$LOG_DIR/edicao_feedback_last.log"

{
  echo "[$TS] === Ciclo de aprendizado do grupo Edição ==="

  # Snapshot antes, para relatar só o que é novo.
  BEFORE_IMG=$(find /home/ubuntu/.hermes/perfume_image_bank/products \
    -path '*/approved_final/*' -type f 2>/dev/null | wc -l)

  echo "--- correcoes (imagem aprovada + headline reescrita)"
  cd /home/ubuntu/.hermes/scripts
  timeout 900 python3 perfume_breno_correction_miner.py 2>&1 | tail -20 || \
    { echo "AVISO: correction_miner falhou/timeout"; FAILED=1; }

  # The miner already refreshes incremental editorial observations. No second
  # 720-hour replay: reviewed facts are promoted separately, not by silence.
  echo "--- preferencias editoriais: atualização incremental executada pelo miner"

  echo "--- imagens novas dos grupos de origem"
  timeout 900 python3 perfume_group_image_miner.py 2>&1 | tail -12 || \
    { echo "AVISO: group_image_miner falhou/timeout"; FAILED=1; }

  AFTER_IMG=$(find /home/ubuntu/.hermes/perfume_image_bank/products \
    -path '*/approved_final/*' -type f 2>/dev/null | wc -l)

  echo "--- resultado"
  echo "imagens aprovadas: $BEFORE_IMG -> $AFTER_IMG (novas: $((AFTER_IMG - BEFORE_IMG)))"

  # Pares de reescrita disponíveis para o prompt de headline.
  timeout 120 python3 -c "
import sys; sys.path.insert(0,'/home/ubuntu/.hermes/scripts')
import perfume_manual_normalizer as m
p = m.breno_corrected_headline_pairs(200)
print(f'pares de reescrita do Breno disponiveis: {len(p)}')
for a, b in p[:5]:
    print(f'  {a}  ->  {b}')
" 2>&1 || echo "AVISO: nao consegui contar pares"

  echo "[$(date -Is)] === fim ==="
} > "$OUT" 2>&1

# stdout do cron: resumo curto. Silêncio longo não ajuda ninguém.
tail -n 12 "$OUT"
exit "$FAILED"
