#!/usr/bin/env bash
# Watchdog: perfume normalizer 15s loop. Restarts a dead loop and alerts once.
# Registered: crontab every 5min. Alert -> WA Automações group.
set -u
PID_FILE="/home/ubuntu/.hermes/run/perfume_manual_normalizer_15s.pid"
CTL="/home/ubuntu/.hermes/scripts/perfume_normalizer_ctl.sh"
ALERT_FLAG="/home/ubuntu/.hermes/run/perfume_normalizer_watchdog_alerted"
SENDER="/home/ubuntu/.hermes/scripts/whatsapp_send_guarded.py"
WA_GROUP="120363995412228683@g.us"  # Normalizador Promo (alertas junto do fluxo)
LOG="/home/ubuntu/.hermes/run/perfume_normalizer_watchdog.log"

# Only act during the scheduled perfume window: weekends, 08:50-22:00 local.
# A time-only check used to resurrect the normalizer on weekdays.
# A same-day manual stop expires automatically at the next local date.
PAUSE_DAY=""
if [[ -f /home/ubuntu/.hermes/run/perfume_pause_day ]]; then
  read -r PAUSE_DAY < /home/ubuntu/.hermes/run/perfume_pause_day || true
fi
if [[ "$PAUSE_DAY" == "$(date +%F)" ]]; then exit 0; fi
weekday=$(date +%u)
hour=$(date +%H)
minute=$(date +%M)
# Silent incremental editorial observation on the EXISTING five-minute tick.
# Keep this separate from watchdog restart policy (which remains unchanged).
clock=$((10#$hour * 60 + 10#$minute))
if (( (10#$weekday >= 6 && clock >= 530 && clock <= 1320) ||
      (10#$weekday == 2 && clock >= 1080 && clock <= 1350) ||
      (10#$weekday == 5 && clock >= 1020 && clock <= 1290) )); then
  timeout 120 python3 /home/ubuntu/.hermes/scripts/perfume_comparison_refresh_new.py --renewal >> "$LOG" 2>&1 || \
    printf '%s\n' 'editorial incremental refresh failed; inspect editorial_refresh_recent.json' >> "$LOG"
fi
if (( 10#$weekday < 6 || 10#$hour >= 22 )); then exit 0; fi
if (( 10#$hour < 8 || (10#$hour == 8 && 10#$minute < 50) )); then exit 0; fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  rm -f "$ALERT_FLAG"
  exit 0
fi

# Loop is down inside the window: restart and alert once.
echo "[$(date -Is)] loop down, restarting" >> "$LOG"
bash "$CTL" on >> "$LOG" 2>&1 &
CTL_PID=$!
# ctl 'on' can hang on slow cron-pause import; give it 30s then move on.
for _ in $(seq 1 30); do
  sleep 1
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then break; fi
done
kill "$CTL_PID" 2>/dev/null || true

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  status="religuei sozinho (pid $(cat "$PID_FILE"))"
else
  status="NÃO consegui religar — checar manualmente: bash $CTL on"
fi
echo "[$(date -Is)] $status" >> "$LOG"

# Alert once per incident (flag cleared when loop is healthy again).
if [[ ! -f "$ALERT_FLAG" ]]; then
  python3 "$SENDER" --chat "$WA_GROUP" --text "⚠️ Watchdog: loop do Normalizador Perfume caiu. $status" >> "$LOG" 2>&1 || true
  touch "$ALERT_FLAG"
fi
