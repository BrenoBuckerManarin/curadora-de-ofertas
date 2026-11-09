#!/usr/bin/env bash
# One-off exception: Perfume opened Wed 2026-09-30, close at 22:00 that day only.
set -euo pipefail
[ "$(date +%F)" = "2026-09-30" ] || { echo "perfume_stop_20260930_2200: data diferente, ignorado"; exit 0; }
exec /home/ubuntu/.hermes/scripts/window_perfume_stop_tue.sh
