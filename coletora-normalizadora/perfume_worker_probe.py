#!/usr/bin/env python3
"""Harmless real worker-scope check after a Hermes update; never sends offers."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from tools.process_registry import _systemd_scope_argv

if __name__ == '__main__':
    command = _systemd_scope_argv('/usr/bin/systemd-run', f'perfume-compat-probe-{os.getpid()}', '/usr/bin/true')
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    print(f'worker_scope_rc={result.returncode}')
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    raise SystemExit(result.returncode)
