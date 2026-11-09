#!/usr/bin/env bash
set -euo pipefail
TS="$(date -Is)"
echo "[$TS] Starting Perfume weekend window"
# Avoid duplicate collectors from old/test jobs.
cd /home/ubuntu/.hermes/hermes-agent
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import pause_job, update_job
canonical='8d7d488a8520'
duplicates=['45516849dc2b','0d72ca48785f','9f05a8553f24']
for jid in duplicates:
    j=pause_job(jid, 'paused by weekend perfume window controller to avoid duplicates')
    if j: print(f'paused duplicate/test job {jid}: {j.get("name")}')
if not update_job(canonical, {
    'schedule': {'kind': 'interval', 'minutes': 1, 'display': 'every 1m'},
    'schedule_display': 'every 1m',
}):
    raise SystemExit(f'canonical perfume collector not found: {canonical}')
print(f'configured canonical collector {canonical}: every 1m after completion; duration varies')
PY
/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh on
# The scheduler owns every round; opening the window must not run a delivery.
HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import resume_job
j=resume_job('8d7d488a8520')
if not j:
    raise SystemExit('canonical perfume collector could not be resumed')
print(f'resumed canonical collector: next={j.get("next_run_at")}')
PY
echo "[$(date -Is)] Perfume weekend window started"
