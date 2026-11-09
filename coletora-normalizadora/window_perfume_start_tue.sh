#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/.hermes/hermes-agent
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import pause_job, update_job
canonical='8d7d488a8520'
for jid in ['45516849dc2b','0d72ca48785f','9f05a8553f24']:
    pause_job(jid, 'paused by Tuesday perfume window controller')
update_job(canonical, {'schedule': {'kind':'interval','minutes':1,'display':'every 1m'}, 'schedule_display':'every 1m'})
PY
/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh on
# The scheduler owns every round; opening the window must not run a delivery.
cd /home/ubuntu/.hermes/hermes-agent
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import resume_job
if not resume_job('8d7d488a8520'):
    raise SystemExit('canonical perfume collector could not be resumed')
PY
