#!/usr/bin/env bash
# One-shot 17:15 check (2026-09-25): did the Friday window really resume the Perfume Collector?
cd /home/ubuntu/.hermes || exit 0
echo "== agora: $(date '+%F %T')"
echo "== cron da Coletora:"
hermes cron list 2>/dev/null | grep -A8 -E '^\s+8d7d488a8520' | grep -E '8d7d488a8520|Last run|Next run|Execution'
echo "== últimas linhas do deliver_last.log (depois das 17:00):"
python3 - <<'E'
import os, time
p = os.path.expanduser('~/.hermes/perfume_image_bank/logs/deliver_last.log')
st = os.stat(p)
print('log modificado em', time.strftime('%F %T', time.localtime(st.st_mtime)))
with open(p, 'rb') as f:
    f.seek(max(0, st.st_size - 20000)); tail = f.read().decode('utf-8', 'replace').splitlines()[-8:]
for l in tail: print(' ', l[:220])
E
echo "== registro de curadoria (rodadas hoje):"
python3 - <<'E'
import json, os, time
p = os.path.expanduser('~/.hermes/perfume_image_bank/logs/collector_curation.jsonl')
if not os.path.exists(p):
    print('  ainda não existe'); raise SystemExit
rows = [json.loads(l) for l in open(p) if l.strip()]
today = [r for r in rows if time.strftime('%F', time.localtime(r['ts'])) == time.strftime('%F')]
print('  rodadas hoje:', len(today), '| selecionadas:', sum(len(r['selected']) for r in today),
      '| com vendedor verificado:', sum(1 for r in today for s in r['selected'] if s.get('verified_seller')))
E
