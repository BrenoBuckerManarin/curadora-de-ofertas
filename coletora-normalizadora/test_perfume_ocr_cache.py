"""OCR cache invariants using temporary files and explicit OCR fixtures."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import perfume_manual_normalizer as n
import perfume_ocr_cache as c

class OCRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root/'photo.jpg'; self.image.write_bytes(b'fixture-image')
        p = patch.object(c, 'CACHE', self.root/'cache.sqlite3'); p.start(); self.addCleanup(p.stop)
        n._IMAGE_WATERMARK_CACHE.clear()
        self.addCleanup(n._IMAGE_WATERMARK_CACHE.clear)

    def test_persistent_hit_preserves_positive_and_negative(self):
        for text, expected in [('www.example.com', (True,'www.example.com')), ('perfume', (False,''))]:
            self.image.write_text(text)
            with patch.object(n.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=text)) as ocr:
                self.assertEqual(n.image_explicit_watermark(self.image), expected)
                n._IMAGE_WATERMARK_CACHE.clear()  # simulate the next process
                self.assertEqual(n.image_explicit_watermark(self.image), expected)
                self.assertEqual(ocr.call_count, 1)

    def test_same_size_replacement_with_restored_mtime_invalidates(self):
        with patch.object(n.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='')) as ocr:
            n.image_explicit_watermark(self.image)
            original = self.image.stat()
            replacement = self.root/'replacement'; replacement.write_bytes(b'x'*original.st_size)
            os.utime(replacement, ns=(original.st_atime_ns,original.st_mtime_ns))
            replacement.replace(self.image)
            n.image_explicit_watermark(self.image)
            self.assertEqual(ocr.call_count, 2)

    def test_failure_is_not_cached(self):
        for value in [SimpleNamespace(returncode=1,stdout=''), subprocess.TimeoutExpired('tesseract',12)]:
            mocked = patch.object(n.subprocess, 'run', side_effect=value) if isinstance(value, Exception) else patch.object(n.subprocess, 'run', return_value=value)
            with mocked as ocr:
                n.image_explicit_watermark(self.image); n.image_explicit_watermark(self.image)
                self.assertEqual(ocr.call_count, 2)

    def test_mutation_during_ocr_not_persisted(self):
        def mutate(*a,**k):
            self.image.write_bytes(b'changed-content')
            return SimpleNamespace(returncode=0,stdout='')
        with patch.object(n.subprocess,'run',side_effect=mutate):
            n.image_explicit_watermark(self.image)
        self.assertIsNone(c.get(c.fingerprint(self.image)))

    def test_engine_and_rule_changes_invalidate(self):
        engine = self.root/'engine'; engine.write_bytes(b'v1')
        old = c.fingerprint(self.image,engine)
        engine.write_bytes(b'v2')
        self.assertNotEqual(old,c.fingerprint(self.image,engine))
        current = c.fingerprint(self.image,engine)
        with patch.object(c,'VERSION','new-detector'):
            self.assertNotEqual(current,c.fingerprint(self.image,engine))

if __name__ == '__main__':
    unittest.main(verbosity=2)
