#!/usr/bin/env bash
set -euo pipefail
TS="$(date -Is)"
echo "[$TS] Stopping Perfume weekend window"
cd /home/ubuntu/.hermes/hermes-agent
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import pause_job
for jid in ['8d7d488a8520','45516849dc2b','0d72ca48785f','9f05a8553f24']:
    j=pause_job(jid, 'paused by weekend perfume window controller')
    if j: print(f'paused {jid}: {j.get("name")}')
PY
/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh off
echo "[$(date -Is)] Perfume normalizer status after stop:"
/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh status
