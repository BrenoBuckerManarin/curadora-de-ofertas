"""Private editorial renewal queue; generated candidates are never approved/sent."""
import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path.home()/'.hermes/perfume_image_bank/editorial_renewal'


def enqueue(product, body, price_context, pool):
    payload={'product':product,'body':body,'price_context':price_context or {},'pool':sorted(pool)}
    key=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    ROOT.mkdir(parents=True,exist_ok=True)
    target=ROOT/(key+'.request.json')
    if target.exists() or (ROOT/(key+'.result.json')).exists():return key
    temp=ROOT/(key+f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps({**payload,'created_at':int(time.time())},ensure_ascii=False))
    os.replace(temp,target)
    return key


def has_work():
    from perfume_renewal_review import pending
    return next(pending(), None) is not None or any(
        not p.with_name(p.name.replace('.request.json', '.result.json')).exists()
        for p in ROOT.glob('*.request.json'))


def run_one(review=False):
    import fcntl
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        if review:
            from perfume_renewal_review import review_one
            reviewed = review_one()
            if reviewed['status'] != 'review_idle':
                return reviewed
        pending=sorted(ROOT.glob('*.request.json'),key=lambda p:p.stat().st_mtime)
        for path in pending:
            result=path.with_name(path.name.replace('.request.json','.result.json'))
            if result.exists():continue
            import perfume_manual_normalizer as n
            request=json.loads(path.read_text())
            if request.get('attempts',0) >= 2:
                result.write_text(json.dumps({'status':'attempt_limit','approved':False,'sent':False}))
                continue
            request['attempts']=request.get('attempts',0)+1
            path.write_text(json.dumps(request,ensure_ascii=False))
            start=time.monotonic()
            with n.headline_budget(60):
                headline=n.llm_headline(request['product'],'',request['body'],request['price_context'])
            headline = n.enforce_headline_output(headline)
            pool=request['pool']
            novel=bool(headline) and len(n.distinct_repertoire(pool+[headline]))>len(n.distinct_repertoire(pool))
            row={'product':request['product'],'headline':headline,'source':n.LAST_HEADLINE_SOURCE,
                 'status':'candidate_needs_review' if novel else 'no_distinct_candidate',
                 'approved':False,'sent':False,'seconds':round(time.monotonic()-start,3),
                 'created_at':int(time.time())}
            tmp=result.with_suffix('.tmp');tmp.write_text(json.dumps(row,ensure_ascii=False,indent=2));os.replace(tmp,result)
            return row
    return {'status':'idle'}

if __name__=='__main__':print(json.dumps(run_one(review=True),ensure_ascii=False))
