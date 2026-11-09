"""Local round evidence, overlap protection and monotonic phase timing."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

STATUS = Path.home() / '.hermes/perfume_image_bank/logs/collector_round_last.json'

class Round:
    def __init__(self, path=STATUS):
        self.path = Path(path)
        self.data = {'pid': os.getpid(), 'started_at': time.time(), 'status': 'running', 'timings_seconds': {}}
        self.started = time.monotonic()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + '\n')
        tmp.replace(self.path)

    @contextmanager
    def phase(self, name):
        start = time.monotonic()
        self.data['phase'] = name
        self.save()
        try:
            yield
        finally:
            self.data['timings_seconds'][name] = round(time.monotonic() - start, 4)
            self.save()

    def finish(self, status, **fields):
        self.data.update(fields, status=status, finished_at=time.time(), duration_seconds=round(time.monotonic()-self.started, 4))
        self.save()


def run_exclusive(callback, path=STATUS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('deliver: overlap_skipped (another round owns the lock)')
            return
        run = Round(path)
        run.save()
        try:
            callback(run)
        except BaseException as exc:
            run.finish('failed', error=f'{type(exc).__name__}: {exc}')
            raise
        if run.data['status'] == 'running':
            run.finish('completed_unknown')
