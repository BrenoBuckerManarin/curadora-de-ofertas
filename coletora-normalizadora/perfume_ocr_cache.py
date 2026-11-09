"""Persistent OCR evidence for unchanged files, not image approval decisions."""
import json
import hashlib
import sqlite3
from pathlib import Path

CACHE = Path.home()/'.hermes/run/perfume_ocr_cache.sqlite3'
VERSION = 'explicit-watermark-por-eng-psm11-v1'

def fingerprint(path, engine=Path('/usr/bin/tesseract')):
    def stamp(p):
        s = Path(p).stat()
        # stat timestamps can collide during rapid in-place edits. Content is
        # cheap to hash compared with OCR and closes that stale-result hole.
        digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        return [str(Path(p).resolve()), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, digest]
    # Missing engine => no persistent negative evidence.
    return json.dumps([VERSION, stamp(path), stamp(engine)], separators=(',', ':'))

def get(key, database=None):
    database = Path(database) if database is not None else CACHE
    if not database.exists():
        return None
    try:
        with sqlite3.connect(str(database), timeout=2) as db:
            row = db.execute('SELECT result FROM watermark WHERE key=?', (key,)).fetchone()
        if row:
            value = json.loads(row[0])
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], bool) and isinstance(value[1], str):
                return tuple(value)
    except (sqlite3.Error, ValueError, OSError):
        pass
    return None

def put(key, result, database=None):
    database = Path(database) if database is not None else CACHE
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(database), timeout=2) as db:
            db.execute('CREATE TABLE IF NOT EXISTS watermark (key TEXT PRIMARY KEY, result TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO watermark VALUES (?,?)', (key, json.dumps(result)))
    except (sqlite3.Error, OSError):
        # Cache failure must not change the OCR result or block normalization.
        pass
