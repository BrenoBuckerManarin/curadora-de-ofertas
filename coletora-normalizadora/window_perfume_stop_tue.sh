#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/.hermes/hermes-agent
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import pause_job
for jid in ['8d7d488a8520','45516849dc2b','0d72ca48785f','9f05a8553f24']:
    pause_job(jid, 'paused by Tuesday perfume window controller')
PY
/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh off
