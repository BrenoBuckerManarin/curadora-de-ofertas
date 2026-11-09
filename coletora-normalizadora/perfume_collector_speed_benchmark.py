"""Read-only ranking benchmark on a bounded sample from authorized source taps."""
import collections
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch
import perfume_deliver as d

FIXTURE = Path('/tmp/perfume_collector_speed_fixture.json')

def main():
    label = sys.argv[1]
    if not FIXTURE.exists():
        with open(d.TAP_FILE, 'rb') as f:
            f.seek(0,2); size=f.tell(); f.seek(max(0,size-2_000_000)); lines=f.read().splitlines()
        pool=[]; seen=set()
        for line in reversed(lines[1:]):
            try: msg=json.loads(line)
            except ValueError: continue
            group=str(msg.get('chatId','')).replace('@g.us','')
            body=str(msg.get('body') or '')
            if group not in d.PERFUME_GROUPS or not d.extract_link(body): continue
            product=d.extract_product_name(body)
            if product in seen: continue
            seen.add(product); msg['groupName']=d.PERFUME_GROUPS[group]; pool.append(msg)
            if len(pool)>=20: break
        if not pool: raise SystemExit('No eligible source sample in bounded tap window')
        FIXTURE.write_text(json.dumps(pool))
    pool=json.loads(FIXTURE.read_text())
    d.resolve_approved_image.cache_clear()
    start=time.perf_counter()
    with patch.object(d,'service_log'):
        ranked=d.rank_candidates(pool)
    result={'seconds':time.perf_counter()-start, 'count':len(pool), 'ranked':[
        {k:m.get(k) for k in ('messageId','product','candidate_score','commercial_score','production_score','bank_image','bank_source','has_context')}
        for m in ranked]}
    target=Path(f'/tmp/perfume_collector_speed_{label}.json'); target.write_text(json.dumps(result,indent=2))
    print(json.dumps({'label':label,'seconds':result['seconds'],'count':result['count'],'output':str(target)}))

if __name__=='__main__': main()
