"""Incremental recent Edição observation. No sends or historical preference replay."""
import fcntl
import json
import os
import time
from pathlib import Path
import perfume_editorial_pair_eval as evaluator

REPORT = Path('/home/ubuntu/.hermes/perfume_image_bank/logs/editorial_refresh_recent.json')


def select_changes(existing, candidates, now, settle_seconds=120):
    indexed = {row['cycle_id']: row for row in existing}
    changes = []
    pending = 0
    for row in candidates:
        if now - row['edition_final_ts'] < settle_seconds:
            pending += 1
            continue
        old = indexed.get(row['cycle_id'])
        if old is None or any(old.get(k) != row.get(k) for k in ('edition_final_ts', 'edition_messageId', 'after')):
            changes.append(row)
    return changes, pending


def refresh(*, allow_renewal=False):
    with Path('/home/ubuntu/.hermes/perfume_comparison_refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        now = int(time.time())
        existing = evaluator.load_jsonl(evaluator.DEFAULT_CYCLES)
        result = evaluator.build(evaluator.DEFAULT_FEEDBACK, evaluator.DEFAULT_TAP, 24)
        changes, pending = select_changes(existing, result['pairs'], now)
        indexed = {row['cycle_id']:row for row in existing}
        added = sum(row['cycle_id'] not in indexed for row in changes)
        for row in changes:
            indexed[row['cycle_id']] = row
        if changes:
            # Scoped upsert: retain every existing cycle; update only proven revisions.
            rows = sorted(indexed.values(), key=lambda r:r['normalizer_ts'])
            temp = evaluator.DEFAULT_CYCLES.with_suffix('.refresh.tmp')
            temp.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
            os.replace(temp, evaluator.DEFAULT_CYCLES)
        report = {'generated_at':now, 'status':'ok', 'added':added,
                  'updated':len(changes)-added, 'pending_settle':pending,
                  'stored_cycles':len(indexed), 'preferences_changed':False,
                  'latest_compared_normalizer_ts':max((r['normalizer_ts'] for r in indexed.values()), default=0)}
        stored_rows = list(indexed.values())
        summary = evaluator.summarize(stored_rows)
        edition_images = evaluator.persist_edition_images(stored_rows)
        report['edition_images'] = edition_images
        evaluator.DEFAULT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
        evaluator.DEFAULT_OUT.write_text(json.dumps({
            **result, 'window_hours':24, 'stored_cycles':len(indexed),
            'summary':summary, 'refresh':report, 'edition_images':edition_images,
            'preferences':{'updated':False, 'reason':'observation_only'}
        }, ensure_ascii=False, indent=2)+'\n')
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        # Maintenance tick, never the delivery worker. One bounded candidate.
        from perfume_editorial_renewal import ROOT, has_work
        pause = Path.home()/'.hermes/run/perfume_pause_day'
        paused = pause.exists() and pause.read_text().strip() == time.strftime('%Y-%m-%d')
        if allow_renewal and not paused and ROOT.exists() and has_work():
            import subprocess
            try:
                child = subprocess.run(['python3',str(Path(__file__).with_name('perfume_editorial_renewal.py'))],capture_output=True,text=True,timeout=75)
                report['renewal'] = {'returncode':child.returncode,'output':child.stdout[-2000:]}
            except subprocess.TimeoutExpired:
                report['renewal'] = {'status':'timeout'}
            REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report


if __name__ == '__main__':
    try:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--renewal', action='store_true', help='Maintenance tick only; never enable from delivery mining')
        args = parser.parse_args()
        print(json.dumps(refresh(allow_renewal=args.renewal),ensure_ascii=False))
    except Exception as exc:
        REPORT.write_text(json.dumps({'generated_at':int(time.time()),'status':'error','error':str(exc)}))
        raise
