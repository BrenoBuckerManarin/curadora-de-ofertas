#!/usr/bin/env bash
set -euo pipefail

JOB_ID="f5af14f0b4ca"
CRON_SCHEDULE="every 1m"   # Hermes cron does not accept 15s schedules.
LOOP_SECONDS="3"  # Idle intake only; active admission is bounded inside Python.
PID_FILE="/home/ubuntu/.hermes/run/perfume_manual_normalizer_15s.pid"
RUN_LOCK="/home/ubuntu/.hermes/run/perfume_manual_normalizer_run.lock"
LOG_FILE="/home/ubuntu/.hermes/run/perfume_manual_normalizer_15s.log"
SCRIPT="/home/ubuntu/.hermes/scripts/perfume_manual_normalizer.py"
MINER="/home/ubuntu/.hermes/scripts/perfume_breno_correction_miner.py"
EDITION_GROUP="120363991547992305@g.us"

usage() {
  cat <<EOF
Usage: $(basename "$0") {on|off|status|now|mine|fast|normal|logs}

Controls Perfume Manual Normalizer + Breno approved-image miner.

Commands:
  on      pause Hermes cron and start direct 15s loop; normalizer runs miner before processing new promos
  off     stop 15s loop and pause Hermes cron
  status  show cron entry, loop pid, and related processes
  now     run normalizer once immediately; this also triggers miner if there are new promos
  mine    run Edição approved-image miner once now
  fast    alias for on
  normal  stop 15s loop, set Hermes cron to $CRON_SCHEDULE, resume cron
  logs    tail direct-loop log
EOF
}

is_loop_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

loop_pids() {
  /usr/bin/python3 - "$$" <<'PY'
import os, sys
self_pid = int(sys.argv[1])
script = "/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh"
excluded = {self_pid, os.getpid()}
pid = os.getppid()
while pid > 1 and pid not in excluded:
    excluded.add(pid)
    try:
        status = open(f"/proc/{pid}/status", encoding="utf-8").read()
        pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
    except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration, ValueError):
        break
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) in excluded:
        continue
    try:
        argv = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")
        args = [part.decode("utf-8", "replace") for part in argv if part]
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    if len(args) == 3 and args[0].endswith("bash") and args[1] == script and args[2] in {"on", "fast"}:
        print(name)
PY
}

start_loop() {
  mkdir -p "$(dirname "$PID_FILE")"
  existing="$(loop_pids | tr '\n' ' ' | xargs)"
  if [[ -n "$existing" ]]; then
    echo "15s loop already running: pid(s) $existing"
    return 0
  fi
  : > "$LOG_FILE"
  (
    while true; do
      ts="$(date -Is)"
      echo "[$ts] run" >> "$LOG_FILE"
      /usr/bin/flock -n "$RUN_LOCK" /usr/bin/python3 "$SCRIPT" >> "$LOG_FILE" 2>&1 || echo "[$ts] busy_or_exit=$?" >> "$LOG_FILE"
      sleep "$LOOP_SECONDS"
    done
  ) </dev/null >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  disown 2>/dev/null || true
  echo "Started 15s loop: pid $(cat "$PID_FILE")"
  # Cron pause is slow (heavy import) and usually a no-op; do it after the
  # loop is already up, in background, so 'on' answers in ~1s.
  (
    HERMES_HOME=/home/ubuntu/.hermes python - <<'PY' >/dev/null 2>&1 || true
from cron.jobs import pause_job
pause_job('f5af14f0b4ca', 'paused by perfume_normalizer_ctl direct 15s loop')
PY
    systemctl --user disable --now perfume-normalizer.timer >/dev/null 2>&1 || true
  ) >/dev/null 2>&1 </dev/null &
  disown 2>/dev/null || true
}

stop_loop() {
  pids="$(loop_pids | tr '\n' ' ' | xargs)"
  if [[ -n "$pids" ]]; then
    kill $pids || true
    sleep 1
  fi
  rm -f "$PID_FILE"
}

case "${1:-}" in
  on|fast)
    start_loop
    ;;
  off)
    stop_loop
    HERMES_HOME=/home/ubuntu/.hermes python - <<'PY' || true
from cron.jobs import pause_job
j = pause_job('f5af14f0b4ca', 'paused by perfume_normalizer_ctl off')
if j:
    print(f"paused {j.get('id')}: {j.get('name')}")
PY
    systemctl --user disable --now perfume-normalizer.timer >/dev/null 2>&1 || true
    ;;
  status)
    HERMES_HOME=/home/ubuntu/.hermes python - <<'PY' || true
from cron.jobs import load_jobs
for j in load_jobs():
    if j.get('id') == 'f5af14f0b4ca' or j.get('name') == 'Perfume Manual Normalizer WhatsApp':
        print(f"cron: {j.get('id')} | {j.get('name')} | enabled={j.get('enabled')} state={j.get('state')} next={j.get('next_run_at')} last={j.get('last_run_at')}")
PY
    if is_loop_running; then echo "15s loop: active pid $(cat "$PID_FILE")"; else echo "15s loop: stopped"; fi
    systemctl --user is-active perfume-normalizer.timer >/dev/null 2>&1 && echo "systemd timer: active" || echo "systemd timer: stopped"
    ps -eo pid,pcpu,pmem,etime,cmd | grep -E 'perfume_manual_normalizer|perfume_breno_correction_miner' | grep -v grep || true
    ;;
  now)
    /usr/bin/flock -n "$RUN_LOCK" /usr/bin/python3 "$SCRIPT"
    ;;
  mine)
    /usr/bin/python3 "$MINER" --group "$EDITION_GROUP" --limit 220
    ;;
  normal)
    stop_loop
    HERMES_HOME=/home/ubuntu/.hermes python - <<'PY'
from cron.jobs import update_job, resume_job
update_job('f5af14f0b4ca', {'schedule': 'every 1m', 'schedule_display': 'every 1m'})
j = resume_job('f5af14f0b4ca')
if j:
    print(f"resumed {j.get('id')}: {j.get('name')} next={j.get('next_run_at')}")
PY
    ;;
  logs)
    tail -120 "$LOG_FILE" 2>/dev/null || echo "No log yet: $LOG_FILE"
    ;;
  *)
    usage
    exit 2
    ;;
esac
