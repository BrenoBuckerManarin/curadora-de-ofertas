#!/usr/bin/env python3
"""Read-only operational evidence. No alerts, scheduling or sends."""
import datetime as dt
import json
import os
from pathlib import Path

HOME = Path.home()/'.hermes'

def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None

def main():
    data = read(HOME/'cron/jobs.json') or {}
    jobs = data.get('jobs', [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    selected = []
    for j in jobs:
        if j.get('id') in {'8d7d488a8520', 'f5af14f0b4ca'} or ('Window ' in j.get('name','') and 'Perfume' in j.get('name','')):
            selected.append({k:j.get(k) for k in ('id','name','enabled','state','schedule','next_run_at','last_run_at','last_status')})
    current = read(HOME/'perfume_image_bank/logs/collector_round_last.json')
    if current and current.get('status') == 'running':
        try:
            os.kill(int(current['pid']),0)
        except (ProcessLookupError, ValueError, KeyError):
            current['status'] = 'interrupted_process_missing'
    pidfile = HOME/'run/perfume_manual_normalizer_15s.pid'
    loop = 'stopped'
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        cmd = (Path('/proc')/str(pid)/'cmdline').read_bytes()
        loop = 'active' if b'perfume_normalizer_ctl.sh' in cmd else 'stale_pid'
    except (OSError, ValueError):
        pass
    print(json.dumps({'checked_at':dt.datetime.now().astimezone().isoformat(), 'jobs':selected,
        'normalizer_loop':loop, 'collector_round':current,
        'funnel':read(HOME/'perfume_image_bank/logs/collector_funnel_last.json')}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
