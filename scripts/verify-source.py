#!/usr/bin/env python3
"""Verify the KV result against public content hashes without exporting patches."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
source = Path(sys.argv[1]).resolve(strict=True)
lock = json.loads((root / 'sources.lock.json').read_text())
head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
if head != lock['upstream']['commit']:
    raise SystemExit('Wrong upstream commit')
for name, hashes in lock['protected_files'].items():
    file = source / name
    if file.resolve() != file or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != hashes['patched']:
        raise SystemExit(f'KV customization verification failed: {name}')
print('KV customization matches locked file hashes')
